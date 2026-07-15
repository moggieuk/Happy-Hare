# klippy/extras/mmu/mmu_nfc_spoolman_client.py
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
# The client may be given an explicit Spoolman URL:
#
#   spoolman_url: http://127.0.0.1:7912
#
# Or it may discover the URL from Moonraker's [spoolman] section:
#
#   spoolman_url: auto
#   moonraker_url: http://127.0.0.1:7125
#
# NFC-specific mapping settings such as spoolman_rfid_key remain owned by the
# NFC config, not Moonraker.
#
# API endpoint
# ────────────
# GET {spoolman_url}/api/v1/spool
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

import json
import logging
import time

try:
    from .mmu_nfc_log import logger
except ImportError:
    logger = logging.getLogger('spoolman_client')

from urllib.error import HTTPError
from urllib.request import Request, urlopen


class SpoolmanClient:
    """
    Queries the Spoolman REST API to resolve a tag UID to a spool ID.

    Parameters
    ----------
    base_url : str
        Root URL of the Spoolman instance, e.g. "http://192.168.1.50:7912",
        or "auto" to discover it from Moonraker's [spoolman] section.
        Trailing slash is stripped automatically.
    moonraker_url : str
        Root URL of the Moonraker instance to query when base_url is "auto".
        Default: "http://127.0.0.1:7125".
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

    def __init__(self, base_url, rfid_key='rfid',
                 timeout=5.0, cache_ttl=300.0, debug=1,
                 moonraker_url='http://127.0.0.1:7125'):
        self._base_url_config = (base_url or '').strip()
        self._base_url = (None if self._base_url_config.lower() == 'auto'
                          else self._normalise_url(self._base_url_config))
        self._moonraker_url = (moonraker_url or 'http://127.0.0.1:7125').rstrip('/')
        self._rfid_key = rfid_key
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._debug = debug

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

    @staticmethod
    def _normalise_url(url):
        if not url:
            return ''
        url = str(url).strip().rstrip('/')
        if url.startswith('http://') or url.startswith('https://'):
            return url
        return 'http://' + url

    def _discover_base_url_from_moonraker(self):
        """
        Return Moonraker's configured Spoolman server URL, or None.

        Moonraker exposes its config through /server/config.  In current
        Moonraker installs, the [spoolman] section normally appears at:
            result.config.spoolman.server
        The fallback keys are intentionally conservative to tolerate older or
        renamed fields without treating arbitrary values as URLs.
        """
        url = '{}/server/config'.format(self._moonraker_url)
        if self._debug >= 3:
            logger.info("spoolman: discovering URL from Moonraker %s", url)
        try:
            with urlopen(url, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            logger.warning("spoolman: Moonraker discovery failed (%s): %s", url, e)
            return None

        config = data.get('result', {}).get('config', {})
        section = config.get('spoolman') or config.get('spoolman_proxy') or {}
        for key in ('server', 'url', 'spoolman_url'):
            value = section.get(key)
            if value:
                discovered = self._normalise_url(value)
                logger.info("spoolman: Moonraker discovery found %s=%s",
                            key, discovered)
                return discovered

        logger.warning("spoolman: Moonraker config has no [spoolman] server/url")
        return None

    def _resolve_base_url(self):
        if self._base_url:
            return self._base_url
        if self._base_url_config.lower() == 'auto':
            self._base_url = self._discover_base_url_from_moonraker()
            if self._base_url:
                return self._base_url
        return None

    def _fetch_spools(self, uid_hex):
        """Return the full Spoolman spool list, or None on request failure.

        Implements a circuit breaker: after _CB_THRESHOLD consecutive failures
        the client stops attempting requests for _CB_BACKOFF seconds.  This
        prevents a dead or slow Spoolman from blocking the Klipper reactor
        thread on every poll cycle.
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

        base_url = self._resolve_base_url()
        if not base_url:
            logger.warning("spoolman: no Spoolman URL configured or discovered")
            return None
        url = '{}/api/v1/spool'.format(base_url)
        if self._debug >= 3:
            logger.info("spoolman: GET %s (looking for uid=%s, key=%s)",
                        url, uid_hex, self._rfid_key)
        try:
            with urlopen(url, timeout=self._timeout) as resp:
                spools = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            self._cb_failures += 1
            self._cb_backoff_until = time.monotonic() + self._CB_BACKOFF
            if self._cb_failures >= self._CB_THRESHOLD:
                logger.warning(
                    "spoolman: %d consecutive failures — circuit open, "
                    "backing off for %.0fs (%s)",
                    self._cb_failures, self._CB_BACKOFF, e)
            else:
                logger.warning("spoolman: request failed (%s): %s", url, e)
            return None

        if not isinstance(spools, list):
            logger.warning("spoolman: unexpected response type %s from %s",
                            type(spools).__name__, url)
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
        base_url = self._resolve_base_url()
        if not base_url:
            logger.warning("spoolman: no Spoolman URL configured or discovered")
            return None
        url = '{}/api/v1/spool/{}'.format(base_url, spool_id)
        if self._debug >= 3:
            logger.info("spoolman: GET %s", url)
        try:
            with urlopen(url, timeout=self._timeout) as resp:
                spool = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            logger.warning("spoolman: detail request failed (%s): %s", url, e)
            return None

        if not isinstance(spool, dict):
            logger.warning("spoolman: unexpected detail response type %s from %s",
                            type(spool).__name__, url)
            return None
        return spool

    def _patch_spool(self, spool_id, payload, plural=False):
        """PATCH a Spoolman spool record, returning True on success."""
        base_url = self._resolve_base_url()
        if not base_url:
            logger.warning("spoolman: no Spoolman URL configured or discovered")
            return False
        endpoint = 'spools' if plural else 'spool'
        url = '{}/api/v1/{}/{}'.format(base_url, endpoint, spool_id)
        body = json.dumps(payload).encode('utf-8')
        req = Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json'},
            method='PATCH')
        if self._debug >= 3:
            logger.info("spoolman: PATCH %s payload=%s", url, payload)
        # Spoolman has applied the PATCH once urlopen returns a response.  Do
        # not read the response body here: this command runs on Klipper's
        # reactor thread, and waiting for a slow/kept-open body can make the
        # whole host appear locked up after the RFID field has already changed.
        with urlopen(req, timeout=self._timeout):
            pass
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
