# klippy/extras/mmu/unit/nfc/spoolman_client.py
#
# EMU NFC Gate Reader — Spoolman API client
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ─────────────────────────────────────────────────────────────────────────────
# Spoolman API client — looks up a spool record by NFC tag UID.
#
# Integration model (UID lookup)
# ───────────────────────────────────────────
# Tags are never written to.  Each tag's factory UID is registered in
# Spoolman by setting a custom extra field (default key: "rfid") to the
# tag's UID string.  When the reader detects a tag it reads only the UID
# (the fastest possible NFC operation), then this client queries the
# Spoolman REST API to find which spool record carries that UID.
#
# Spoolman extra fields
# ─────────────────────
# Spoolman stores arbitrary key-value metadata on each spool in a JSON
# dict called "extra".  You configure which extra fields exist in the
# Spoolman web UI:
#
#   Settings → Extra fields → Spool → Add field
#     Field name:  rfid        (or whatever spoolman_rfid_key is set to)
#     Field type:  Text
#
# Then on each spool record set the "rfid" field to the tag's UID string
# exactly as the reader reports it (uppercase hex, no separators):
#   e.g.  04A23BC1D45E80
#
# The stored value may optionally contain colons, hyphens, or spaces —
# this client normalises both sides before comparing.
#
# Configuration
# ─────────────
# This client never dials the Spoolman server directly.  Every request is
# proxied through Moonraker's POST /server/spoolman/proxy endpoint, so the
# only connection parameter that matters is Moonraker's own address:
#
#   moonraker_url: http://127.0.0.1:7125
#
# Whether to build this client at all is gated entirely on Happy Hare's own
# spoolman_support ([mmu] section, any value other than "off") -- see
# NFCGate._resolve_spoolman() in manager.py -- not a setting owned here.
#
# NFC-specific mapping settings such as spoolman_rfid_key remain owned by the
# NFC config, not Moonraker.
#
# API endpoint
# ────────────
# GET /api/v1/spool  (proxied via Moonraker as path "/v1/spool")
#
# Returns a JSON array of all spool objects.  Each object has an "extra"
# dict (may be null or absent for spools created before the field was
# added).  This client filters in Python; no server-side filtering is
# needed, so it works with all Spoolman versions that have the /spool
# endpoint (v0.14+).
#
# For a typical home collection (50–300 spools) the response is a few KB
# and the lookup completes in well under 100 ms on a local network.
#
# Caching
# ───────
# The result of a successful lookup is cached by UID for cache_ttl seconds
# (default 300 s = 5 min).  Polls that see the same tag within the TTL do
# not make a network request.  Set cache_ttl=0 to disable caching.

import configparser
import io
import json
import logging
import os
import time

try:
    from .log import logger
except ImportError:
    logger = logging.getLogger('spoolman_client')

from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Klipper's link to Moonraker is a Unix domain socket, unrelated to
# Moonraker's HTTP port -- Klipper is never told that port at connect time.
# This default is correct for every standard co-located Klipper/Moonraker
# install (Fluidd, Mainsail, KIAUH, ...) and is used whenever discovery
# (below) doesn't find anything, or the user hasn't overridden moonraker_url
# explicitly.
DEFAULT_MOONRAKER_URL = 'http://127.0.0.1:7125'

# Result of the one-shot moonraker.conf discovery attempt: None until first
# tried, then the discovered URL or False (attempted, found nothing) so
# repeated calls (one per configured gate) don't re-read/re-parse the file.
_MOONRAKER_CONF_DISCOVERY = None


def discover_moonraker_url(printer, debug=1):
    """Best-effort discovery of Moonraker's real address, entirely local and
    synchronous -- no Moonraker-side component, no network call, no push
    mechanism. Returns the discovered "http://host:port" string, or None if
    discovery wasn't possible (caller should fall back to the configured/
    default moonraker_url).

    Klipper always knows its own config file path (printer.start_args
    ['config_file'] -- the same mechanism MmuLogger uses to derive mmu.log's
    location). moonraker.conf sits next to it in the same directory in every
    standard install (the printer_data/config/ layout used by Mainsail/
    Fluidd/KIAUH) -- Klipper never reads Moonraker's config for anything
    else, but this one convention is reliable enough to read the [server]
    host/port from directly, synchronously, once.
    """
    global _MOONRAKER_CONF_DISCOVERY
    if _MOONRAKER_CONF_DISCOVERY is not None:
        return _MOONRAKER_CONF_DISCOVERY or None
    try:
        config_file = printer.start_args.get('config_file')
        if not config_file:
            raise ValueError("printer.start_args has no config_file")
        moonraker_conf = os.path.join(
            os.path.dirname(config_file), 'moonraker.conf')
        if not os.path.isfile(moonraker_conf):
            raise FileNotFoundError(moonraker_conf)
        parser = configparser.ConfigParser()
        parser.read(moonraker_conf)
        host = parser.get('server', 'host', fallback='0.0.0.0').strip()
        port = parser.getint('server', 'port', fallback=7125)
        if not host or host == '0.0.0.0':
            host = '127.0.0.1'
        url = 'http://{}:{}'.format(host, port)
        _MOONRAKER_CONF_DISCOVERY = url
        logger.info("spoolman: discovered Moonraker at %s from %s",
                    url, moonraker_conf)
        return url
    except Exception as e:
        _MOONRAKER_CONF_DISCOVERY = False
        if debug >= 3:
            logger.info(
                "spoolman: could not discover Moonraker from moonraker.conf "
                "(%s); using configured/default moonraker_url", e)
        return None


def resolve_moonraker_url(printer, configured_url, debug=1):
    """Return the Moonraker URL to actually connect to.

    An explicit moonraker_url in config (anything other than the schema
    default) is always respected. Otherwise, try discover_moonraker_url()
    and fall back to the default if that finds nothing.
    """
    if configured_url and configured_url != DEFAULT_MOONRAKER_URL:
        return configured_url
    return (discover_moonraker_url(printer, debug=debug)
            or configured_url or DEFAULT_MOONRAKER_URL)


class MoonrakerSpoolmanTransport:
    """
    Synchronous low-level transport for the Spoolman REST API, proxied
    through Moonraker's POST /server/spoolman/proxy endpoint instead of
    dialing the Spoolman server directly.  Every NFC Spoolman client shares
    one of these so there is exactly one code path that talks to Moonraker.

    Moonraker requires 'path' rooted at '/v1/...' (no '/api' prefix) and the
    query string supplied separately from path.  request() accepts the
    '/api/v1/...' paths already used throughout this package -- with or
    without an embedded '?query' -- and normalises them before proxying, so
    callers do not need to change how they build paths.
    """

    def __init__(self, moonraker_url=DEFAULT_MOONRAKER_URL, timeout=5.0, debug=1):
        self._moonraker_url = (moonraker_url or DEFAULT_MOONRAKER_URL).rstrip('/')
        self._timeout = timeout
        self._debug = debug

    def request(self, method, path, body=None):
        """
        Issue one Spoolman REST call via Moonraker's proxy and return the
        parsed JSON response (dict / list / None).

        Raises urllib.error.HTTPError on a Spoolman-side failure, with .code
        set to Spoolman's real HTTP status and ._body_text set to the error
        body -- matching what direct urlopen()/HTTPError handling already
        expects throughout this package, so callers need no changes.
        Connectivity failures to Moonraker itself (unreachable, timeout, ...)
        raise the same exceptions a direct urlopen() would.
        """
        spoolman_path = path[4:] if path.startswith('/api/') else path
        if '?' in spoolman_path:
            spoolman_path, query = spoolman_path.split('?', 1)
        else:
            query = None

        envelope = {'request_method': method, 'path': spoolman_path,
                    'use_v2_response': True}
        if query is not None:
            envelope['query'] = query
        if body is not None:
            envelope['body'] = body

        proxy_url = '{}/server/spoolman/proxy'.format(self._moonraker_url)
        data = json.dumps(envelope).encode('utf-8')
        req = Request(proxy_url, data=data,
                      headers={'Content-Type': 'application/json'},
                      method='POST')

        if self._debug >= 3:
            logger.info("spoolman: -> %s %s%s (via moonraker)",
                        method, spoolman_path, "?%s" % query if query else "")

        t0 = time.monotonic()
        with urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
        elapsed_ms = (time.monotonic() - t0) * 1000

        outer = json.loads(raw.decode('utf-8')) if raw else {}
        result = outer.get('result', outer) if isinstance(outer, dict) else outer
        error = result.get('error') if isinstance(result, dict) else None
        if error:
            code = int(error.get('status_code') or 502)
            message = str(error.get('message') or 'Spoolman proxy error')
            logger.warning("spoolman: <- HTTP %s %s %s (%.0fms): %s",
                           code, method, spoolman_path, elapsed_ms, message)
            http_err = HTTPError(proxy_url, code, message, None,
                                 io.BytesIO(message.encode('utf-8')))
            http_err._body_text = message
            raise http_err

        if self._debug >= 3:
            logger.info("spoolman: <- OK %s %s (%.0fms)",
                        method, spoolman_path, elapsed_ms)

        response = result.get('response') if isinstance(result, dict) else result
        if isinstance(response, str):
            if not response:
                return None
            try:
                return json.loads(response)
            except (TypeError, ValueError):
                return response
        return response


class SpoolmanClient:
    """
    Queries the Spoolman REST API (via Moonraker's proxy) to resolve a tag
    UID to a spool ID.

    Parameters
    ----------
    moonraker_url : str
        Root URL of the Moonraker instance that proxies requests to
        Spoolman.  Default: DEFAULT_MOONRAKER_URL ("http://127.0.0.1:7125"),
        correct for the standard co-located Klipper/Moonraker install.
    rfid_key : str
        Name of the extra field that holds the tag UID on each spool record.
        Default: "rfid".  Must match the field name you created in the
        Spoolman Settings → Extra fields → Spool panel.
    timeout : float
        HTTP request timeout in seconds.  Default: 5.0.
    cache_ttl : float
        Seconds to cache a successful UID → spool_id mapping.  Set to 0
        to disable.  Default: 300.
    debug : int
        0 = silent, 1 = warnings only, 2 = full trace.
    """

    def __init__(self, rfid_key='rfid', timeout=5.0, cache_ttl=300.0, debug=1,
                 moonraker_url=DEFAULT_MOONRAKER_URL):
        self._moonraker_url = (moonraker_url or DEFAULT_MOONRAKER_URL).rstrip('/')
        self._rfid_key = rfid_key
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._debug = debug
        self._transport = MoonrakerSpoolmanTransport(
            moonraker_url=self._moonraker_url, timeout=timeout, debug=debug)

        # UID → (spool_record, expiry_monotonic)
        self._cache = {}

        # Circuit breaker — prevents blocking the Klipper reactor thread on
        # repeated Spoolman failures.  After _CB_THRESHOLD consecutive request
        # failures the client backs off for _CB_BACKOFF seconds before trying
        # again.  A single success resets the counter.
        self._cb_failures   = 0
        self._cb_backoff_until = 0.0
        _CB_THRESHOLD       = 3
        _CB_BACKOFF         = 60.0
        self._CB_THRESHOLD  = _CB_THRESHOLD
        self._CB_BACKOFF    = _CB_BACKOFF

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_uid(uid_str):
        """
        Strip surrounding quotes, separators, and uppercase so that
        e.g. '"04:a2:3b"' == "04A23B".
        """
        return (uid_str.strip('"\'')
                       .upper()
                       .replace(':', '')
                       .replace('-', '')
                       .replace(' ', ''))

    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_spools(self, uid_hex):
        """Return the full Spoolman spool list, or None on request failure.

        Implements a circuit breaker: after _CB_THRESHOLD consecutive failures
        the client stops attempting requests for _CB_BACKOFF seconds.  This
        prevents a dead or slow Spoolman/Moonraker from blocking the Klipper
        reactor thread on every poll cycle.
        """
        now = time.monotonic()
        if self._cb_failures >= self._CB_THRESHOLD:
            if now < self._cb_backoff_until:
                if self._debug >= 4:
                    logger.debug(
                        "spoolman: circuit open — skipping request "
                        "(retry in %.0fs)", self._cb_backoff_until - now)
                return None
            # Backoff period elapsed — allow one probe through
            logger.info("spoolman: circuit probing after backoff")

        if self._debug >= 3:
            logger.info("spoolman: GET /api/v1/spool (looking for uid=%s, key=%s)",
                        uid_hex, self._rfid_key)
        try:
            spools = self._transport.request('GET', '/api/v1/spool')
        except Exception as e:
            self._cb_failures += 1
            self._cb_backoff_until = time.monotonic() + self._CB_BACKOFF
            if self._cb_failures >= self._CB_THRESHOLD:
                logger.warning(
                    "spoolman: %d consecutive failures — circuit open, "
                    "backing off for %.0fs (%s)",
                    self._cb_failures, self._CB_BACKOFF, e)
            else:
                logger.warning("spoolman: request failed: %s", e)
            return None

        if not isinstance(spools, list):
            logger.warning("spoolman: unexpected response type %s from GET /api/v1/spool",
                            type(spools).__name__)
            return None

        # Success — reset circuit breaker
        if self._cb_failures > 0:
            logger.info("spoolman: connection restored after %d failure(s)",
                        self._cb_failures)
            self._cb_failures      = 0
            self._cb_backoff_until = 0.0
        return spools

    def _find_spool_record_by_uid(self, spools, uid_hex):
        """Return the spool record whose configured RFID field matches uid_hex."""
        uid_norm = self._normalise_uid(uid_hex)

        for spool in spools:
            extra = spool.get('extra') or {}
            stored_raw = extra.get(self._rfid_key)
            if not stored_raw:
                continue
            stored_cleaned = str(stored_raw).strip('"\'')
            stored_norm = self._normalise_uid(stored_cleaned)
            if stored_norm == uid_norm:
                return spool
        return None

    def lookup_spool_by_id(self, spool_id):
        return self._fetch_spool_detail(spool_id)

    def _fetch_spool_detail(self, spool_id):
        """Return the full single-spool record, or None on request failure."""
        path = '/api/v1/spool/{}'.format(spool_id)
        if self._debug >= 3:
            logger.info("spoolman: GET %s", path)
        try:
            spool = self._transport.request('GET', path)
        except Exception as e:
            logger.warning("spoolman: detail request failed (spool_id=%s): %s", spool_id, e)
            return None

        if not isinstance(spool, dict):
            logger.warning("spoolman: unexpected detail response type %s for spool_id=%s",
                            type(spool).__name__, spool_id)
            return None
        return spool

    def _patch_spool(self, spool_id, payload, plural=False):
        """PATCH a Spoolman spool record, returning True on success.

        Raises the underlying exception (typically HTTPError, via
        MoonrakerSpoolmanTransport) on failure so set_spool_uid can inspect
        the status code and decide whether to retry against the plural
        endpoint.
        """
        endpoint = 'spools' if plural else 'spool'
        path = '/api/v1/{}/{}'.format(endpoint, spool_id)
        if self._debug >= 3:
            logger.info("spoolman: PATCH %s payload=%s", path, payload)
        self._transport.request('PATCH', path, body=payload)
        return True

    def set_spool_uid(self, spool_id, uid_hex):
        """
        Write this integration's configured UID extra field onto a spool.

        Spoolman stores extra-field values as JSON-encoded strings.  This method
        intentionally writes self._rfid_key (default: rfid_tag), not the
        vendored rfid_uid_N slot convention.
        """
        if spool_id is None or not uid_hex:
            logger.warning(
                "spoolman: cannot set uid extra field %s on spool_id=%s uid=%s",
                self._rfid_key, spool_id, uid_hex)
            return False
        payload = {"extra": {self._rfid_key: json.dumps(str(uid_hex))}}
        try:
            ok = self._patch_spool(spool_id, payload, plural=False)
        except HTTPError as e:
            if e.code not in (404, 405):
                logger.warning(
                    "spoolman: uid extra patch failed for spool_id=%s "
                    "key=%s uid=%s: %s",
                    spool_id, self._rfid_key, uid_hex, e)
                return False
            try:
                ok = self._patch_spool(spool_id, payload, plural=True)
            except Exception as fallback_error:
                logger.warning(
                    "spoolman: uid extra patch fallback failed for "
                    "spool_id=%s key=%s uid=%s: %s",
                    spool_id, self._rfid_key, uid_hex, fallback_error)
                return False
        except Exception as e:
            logger.warning(
                "spoolman: uid extra patch failed for spool_id=%s "
                "key=%s uid=%s: %s",
                spool_id, self._rfid_key, uid_hex, e)
            return False

        if ok:
            uid_norm = self._normalise_uid(str(uid_hex))
            self._cache.pop(uid_norm, None)
            if self._debug >= 3:
                logger.info(
                    "spoolman: spool_id=%s extra[%s]=%s",
                    spool_id, self._rfid_key, uid_hex)
        return ok

    def lookup_spool_record_by_uid(self, uid_hex):
        """
        Return the Spoolman spool record whose extra[rfid_key] matches uid_hex,
        or None if not found or if the API request fails.

        Parameters
        ----------
        uid_hex : str
            Tag UID as returned by read_tag() — uppercase hex, no separators.

        Returns
        -------
        dict or None
        """
        uid_norm = self._normalise_uid(uid_hex)

        # ── Cache hit ─────────────────────────────────────────────────────────
        if self._cache_ttl > 0 and uid_norm in self._cache:
            spool, expiry = self._cache[uid_norm]
            if time.monotonic() < expiry:
                if self._debug >= 3:
                    spool_id = spool.get('id')
                    logger.info(
                        "spoolman: cache hit uid=%s → spool_id=%s", uid_hex, spool_id)
                return spool
            # Expired — remove stale entry
            del self._cache[uid_norm]

        # ── API request ───────────────────────────────────────────────────────
        spools = self._fetch_spools(uid_hex)
        if spools is None:
            return None

        spool = self._find_spool_record_by_uid(spools, uid_hex)
        spool_id = spool.get('id') if spool else None
        if spool_id is not None:
            detail = self._fetch_spool_detail(spool_id)
            if detail is not None:
                spool = detail

        if self._debug >= 3:
            if spool_id is not None:
                logger.info("spoolman: uid=%s → spool_id=%s", uid_hex, spool_id)
            else:
                logger.info(
                    "spoolman: uid=%s not found in %d spool records "
                    "(check the '%s' extra field in Spoolman)",
                    uid_hex, len(spools), self._rfid_key)

        # ── Cache store ───────────────────────────────────────────────────────
        if self._cache_ttl > 0 and spool is not None:
            self._cache[uid_norm] = (spool, time.monotonic() + self._cache_ttl)

        return spool

    def lookup_spool_by_uid(self, uid_hex):
        """
        Return the Spoolman spool ID whose extra[rfid_key] matches uid_hex,
        or None if not found or if the API request fails.
        """
        spool = self.lookup_spool_record_by_uid(uid_hex)
        if not spool:
            return None
        raw_id = spool.get('id')
        spool_id = int(raw_id) if raw_id is not None else None
        return spool_id

    def get_uid_for_spool(self, spool_id):
        """Return the NFC UID registered for *spool_id*, or None.

        Fetches the spool detail record and reads extra[rfid_key].  Used at
        startup to pre-populate the NFC cache from the HH gate map so the UID
        is known before the first physical tag scan.
        """
        spool = self._fetch_spool_detail(spool_id)
        if not spool:
            return None
        uid_raw = (spool.get('extra') or {}).get(self._rfid_key, '')
        if not uid_raw:
            return None
        return self._normalise_uid(str(uid_raw))

    def clear_cache(self):
        """Flush all cached UID → spool_id mappings."""
        self._cache.clear()
        if self._debug >= 3:
            logger.info("spoolman: cache cleared")
