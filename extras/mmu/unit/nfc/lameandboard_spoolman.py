# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.
#
# Copyright (c) 2026 lameandboard

import json
import logging
import re
import time
from typing import Optional
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request

LOG = logging.getLogger("rfid.spoolman_client")

# ---------------------------------------------------------------------------
# SpoolmanDB fetch caches — populated at most once per process.
# _SPOOLMANDB_MATERIALS_CACHE (dict-based):
#   None = not yet attempted
#   {}   = attempted but failed (so we don't retry on every scan)
#   dict = successfully populated
# _SPOOLMANDB_BAMBU_CACHE (list-based):
#   None = not yet attempted
#   []   = attempted but failed (so we don't retry on every scan)
#   list = successfully populated
# _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE (str|None):
#   None  = not yet populated (or not available)
#   str   = manufacturer name from the top-level "manufacturer" key in bambulab.json
# ---------------------------------------------------------------------------
_SPOOLMANDB_MATERIALS_CACHE: Optional[dict] = None   # material_lower -> density (float)
_SPOOLMANDB_BAMBU_CACHE: Optional[list] = None        # list of filament dicts from bambulab.json
_SPOOLMANDB_BAMBU_MANUFACTURER_CACHE: Optional[str] = None  # top-level manufacturer name

# Hardcoded density fallback table (g/cm³) — used when SpoolmanDB is unreachable.
_DENSITY_FALLBACK: dict = {
    "pla":          1.24,
    "pla+":         1.24,
    "abs":          1.04,
    "petg":         1.27,
    "nylon":        1.52,
    "pa":           1.52,
    "tpu":          1.21,
    "flexible":     1.21,
    "asa":          1.05,
    "pc":           1.30,
    "hips":         1.03,
    "pva":          1.23,
    "tpe":          1.21,
    "peek":         1.32,
    "pei":          1.27,
    "pom":          1.41,
}
_DENSITY_DEFAULT: float = 1.24  # PLA — safe fallback for unknown materials

# Maps tag_format → brand name used when the parser does not supply one.
_TAG_FORMAT_BRANDS: dict = {
    "elegoo": "ELEGOO",
    "anycubic_ace": "Anycubic",
    "creality_cfs": "Creality",
    "qidi": "QIDI",
    "opentag3d": "Generic",
    "openspool": "Generic",
    "openprinttag": "Generic",
    "simplyprint_url": "Generic",
    "generic_ndef_json": "Generic",
}

# Default spool weight (grams) used when the tag does not supply one.
_DEFAULT_SPOOL_WEIGHT_G: int = 1000


def _to_int_safe(val) -> Optional[int]:
    """Convert *val* to int, returning None on failure.

    Used to safely coerce numeric tag or SpoolmanDB temperature/weight values
    without raising on invalid or missing data.
    """
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _fetch_spoolmandb_materials() -> dict:
    """Fetch and cache the SpoolmanDB materials.json density table.

    Returns a dict mapping lowercase material name -> density (float).
    On network failure returns {} so callers fall back to _DENSITY_FALLBACK.
    Result is cached in _SPOOLMANDB_MATERIALS_CACHE for the process lifetime.
    """
    global _SPOOLMANDB_MATERIALS_CACHE
    if _SPOOLMANDB_MATERIALS_CACHE is not None:
        return _SPOOLMANDB_MATERIALS_CACHE
    try:
        req = request.Request(
            "https://donkie.github.io/SpoolmanDB/materials.json",
            headers={"Accept": "application/json"},
        )
        with request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        result: dict = {}
        if isinstance(raw, list):
            for entry in raw:
                name = str(entry.get("name") or "").strip()
                density = entry.get("density")
                if name and density is not None:
                    try:
                        result[name.lower()] = float(density)
                    except (TypeError, ValueError):
                        pass
        elif isinstance(raw, dict):
            for name, entry in raw.items():
                density = entry.get("density") if isinstance(entry, dict) else entry
                if density is not None:
                    try:
                        result[name.lower()] = float(density)
                    except (TypeError, ValueError):
                        pass
        _SPOOLMANDB_MATERIALS_CACHE = result
        LOG.debug("spoolman: SpoolmanDB materials loaded (%d entries)", len(result))
        return result
    except Exception as exc:
        LOG.debug(
            "spoolman: SpoolmanDB materials fetch failed: %s — using fallback densities", exc
        )
        _SPOOLMANDB_MATERIALS_CACHE = {}
        return {}


def _fetch_spoolmandb_bambu() -> list:
    """Fetch and cache the SpoolmanDB Bambu Lab filament database.

    Returns a list of filament dicts from filaments/bambulab.json.
    On network failure returns [] so callers fall back to materials lookup.
    Result is cached in _SPOOLMANDB_BAMBU_CACHE for the process lifetime.

    Also populates _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE with the manufacturer
    name from the top-level "manufacturer" key in the JSON (e.g. "Bambu Lab"),
    which can be used for vendor name normalisation when creating Spoolman vendor
    records.  This cache is set to None when the field is absent or on failure.
    """
    global _SPOOLMANDB_BAMBU_CACHE, _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE
    if _SPOOLMANDB_BAMBU_CACHE is not None:
        return _SPOOLMANDB_BAMBU_CACHE
    # Reset the manufacturer cache now so that any outcome of this fetch
    # (missing key, list payload, or exception) leaves it in a known state
    # rather than potentially carrying a stale value from a previous run.
    _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = None
    try:
        req = request.Request(
            "https://donkie.github.io/SpoolmanDB/filaments/bambulab.json",
            headers={"Accept": "application/json"},
        )
        with request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        filaments: list = []
        if isinstance(raw, dict):
            filaments = raw.get("filaments") or []
            # Capture manufacturer name for vendor normalisation.
            _mfr = str(raw.get("manufacturer") or "").strip()
            if _mfr:
                _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = _mfr
            # If the key is absent or empty, the cache stays None (already set
            # above) — no stale name is carried forward.
        elif isinstance(raw, list):
            # Top-level list: no manufacturer metadata available.
            filaments = raw
        _SPOOLMANDB_BAMBU_CACHE = filaments if isinstance(filaments, list) else []
        LOG.debug(
            "spoolman: SpoolmanDB Bambu filaments loaded (%d entries, manufacturer=%r)",
            len(_SPOOLMANDB_BAMBU_CACHE),
            _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE,
        )
        return _SPOOLMANDB_BAMBU_CACHE
    except Exception as exc:
        LOG.debug(
            "spoolman: SpoolmanDB Bambu fetch failed: %s — falling back to materials lookup",
            exc,
        )
        _SPOOLMANDB_BAMBU_CACHE = []
        # Ensure the manufacturer cache is also cleared on failure so that no
        # stale name influences vendor resolution on subsequent calls.
        _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = None
        return []


class SpoolmanClient:
    def __init__(self, base_url, api_key=None, timeout=5.0, trace=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.trace = trace
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def _trace(self, level, msg, *args):
        if self.trace is None:
            return
        try:
            self.trace(level, msg, *args)
        except Exception:
            pass

    def _trace_info(self, msg, *args):
        self._trace("info", msg, *args)

    def _trace_debug(self, msg, *args):
        self._trace("debug", msg, *args)

    def _req(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body) if body is not None else ""
        data = body_str.encode("utf-8") if body_str else None

        # Build a curl command equivalent for easy copy-paste diagnosis.
        # Authorization header value is redacted so tokens never appear in logs.
        curl_parts = [f"curl -s -X {method} '{url}'"]
        for k, v in self.headers.items():
            if k.lower() == "authorization":
                curl_parts.append(f"-H '{k}: ***'")
            else:
                curl_parts.append(f"-H '{k}: {v}'")
        if body_str:
            # Escape single-quotes in the body so the shell command is valid.
            safe_body = body_str.replace("'", "'\\''")
            curl_parts.append(f"-d '{safe_body}'")
        curl_cmd = " ".join(curl_parts)

        _log_fn = LOG.info if method in ("POST", "PUT", "PATCH") else LOG.debug
        _log_fn("spoolman: → %s %s%s", method, url,
                f"  body={body_str[:800]}{'...' if len(body_str) > 800 else ''}"
                if body_str else "")
        LOG.debug("spoolman:   %s", curl_cmd)
        self._trace_debug("auto_create_spool: Spoolman request %s %s%s",
                         method, url,
                         " body=%s" % body_str[:800] if body_str else "")

        req = request.Request(url, data=data, headers=self.headers, method=method)
        t0 = time.monotonic()
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                elapsed_ms = (time.monotonic() - t0) * 1000
                resp_str = raw.decode("utf-8", errors="replace") if raw else ""
                LOG.debug(
                    "spoolman: ← HTTP %s %s (%.0fms)%s",
                    resp.status,
                    resp.reason,
                    elapsed_ms,
                    f"  resp={resp_str[:400]}{'...' if len(resp_str) > 400 else ''}"
                    if resp_str else "",
                )
                self._trace_debug(
                    "auto_create_spool: Spoolman response HTTP %s %s %.0fms%s",
                    resp.status, resp.reason, elapsed_ms,
                    " resp=%s" % resp_str[:400] if resp_str else "")
                if not raw:
                    return None
                return json.loads(resp_str)
        except url_error.HTTPError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            LOG.info(
                "spoolman: ← HTTP %s %s (%.0fms)%s",
                exc.code,
                exc.reason,
                elapsed_ms,
                f"  error={err_body[:400]}{'...' if len(err_body) > 400 else ''}"
                if err_body else "",
            )
            exc._body_text = err_body  # type: ignore[attr-defined]
            raise

    # --- UID extra-field helpers (rfid_uid_1 … rfid_uid_N numbered model) ---

    @staticmethod
    def uid_field_name(n):
        """Return the Spoolman extra-field key for UID slot *n* (1-based)."""
        return f"rfid_uid_{n}"

    def fields_exist(self, max_uids):
        """Return ``True`` only if all ``rfid_uid_1`` … ``rfid_uid_{max_uids}`` extra fields
        exist in Spoolman.

        Issues a single ``GET /api/v1/field/spool`` to list all spool extra fields and
        checks that every numbered slot key is present in the response.  Returns
        ``False`` if any are missing or if the request fails.  Logs at INFO so a
        missing-field situation is always visible.
        """
        try:
            fields = self._req("GET", "/api/v1/field/spool") or []
            if not isinstance(fields, list):
                fields = []
            existing_keys = {f.get("key") for f in fields if isinstance(f, dict)}
        except Exception as exc:
            LOG.info("spoolman: rfid_uid_N fields check failed: %s", exc)
            return False
        missing = [
            self.uid_field_name(n)
            for n in range(1, max_uids + 1)
            if self.uid_field_name(n) not in existing_keys
        ]
        if missing:
            LOG.info(
                "spoolman: rfid_uid_N fields check: missing %s", ", ".join(missing),
            )
            return False
        LOG.info("spoolman: rfid_uid_N fields check: all %d present", max_uids)
        return True

    def ensure_rfid_uid_fields(self, max_uids):
        """Ensure ``rfid_uid_1`` … ``rfid_uid_{max_uids}`` extra fields exist on Spoolman spools.

        Issues a single ``GET /api/v1/field/spool`` to find which fields already exist,
        then POSTs to create only the missing ones.  When all fields are already present,
        logs at INFO and returns immediately without any POST calls.
        A 409 on POST (concurrent create) is treated as success.

        Returns a ``(ok, permanent)`` tuple:
          ``(True,  True)``  — all fields are ready to use
          ``(False, True)``  — permanent skip (405/422 — won't be fixed by retrying)
          ``(False, False)`` — transient failure (network/timeout/5xx); retry later
        """
        try:
            fields = self._req("GET", "/api/v1/field/spool") or []
            if not isinstance(fields, list):
                fields = []
            existing_keys = {f.get("key") for f in fields if isinstance(f, dict)}
        except url_error.HTTPError as exc:
            if exc.code == 429:
                LOG.warning("ensure_rfid_uid_fields: rate limited listing fields")
                return False, False
            LOG.warning(
                "ensure_rfid_uid_fields: GET /api/v1/field/spool failed (HTTP %s): %s",
                exc.code, exc,
            )
            return False, 400 <= exc.code < 500
        except Exception as exc:
            LOG.warning(
                "ensure_rfid_uid_fields: GET /api/v1/field/spool failed: %s", exc
            )
            return False, False

        missing = [
            (n, self.uid_field_name(n))
            for n in range(1, max_uids + 1)
            if self.uid_field_name(n) not in existing_keys
        ]
        if not missing:
            LOG.info("spoolman: rfid_uid_N fields already exist, skipping creation")
            return True, True

        # Create only the missing fields using the keyed POST endpoint.
        for n, field_key in missing:
            try:
                self._req(
                    "POST",
                    f"/api/v1/field/spool/{field_key}",
                    {
                        "name": f"RFID UID {n}",
                        "field_type": "text",
                        "default_value": "\"\"",
                    },
                )
                LOG.info("Created Spoolman extra field: %s", field_key)
            except url_error.HTTPError as exc:
                if exc.code == 409:
                    # Concurrent create — treat as success.
                    continue
                if exc.code == 405:
                    LOG.warning(
                        "Spoolman rejected keyed field create for %s with HTTP 405",
                        field_key,
                    )
                    return False, True
                if exc.code == 422:
                    LOG.warning(
                        "Spoolman rejected field create for %s with HTTP 422",
                        field_key,
                    )
                    return False, True
                if exc.code == 429:
                    return False, False
                LOG.warning(
                    "Failed to create Spoolman extra field %s (HTTP %s): %s",
                    field_key, exc.code, exc,
                )
                return False, 400 <= exc.code < 500
            except Exception as exc:
                LOG.warning(
                    "ensure_rfid_uid_fields: POST /api/v1/field/spool/%s failed: %s",
                    field_key, exc,
                )
                return False, False

        return True, True

    def get_spool(self, spool_id):
        """Fetch a spool by ID.  Returns the spool dict."""
        return self._req("GET", f"/api/v1/spool/{spool_id}")

    def get_uid_slots(self, spool_id, max_uids):
        """Return occupied UID slots from a Spoolman spool.

        Returns a dict mapping slot index (1-based ``int``) to UID hex string for all
        ``rfid_uid_N`` fields that contain a non-empty value on ``spool_id``.
        Returns an empty dict when no UIDs are registered.
        Raises on HTTP/network errors so callers can abort to avoid data loss.
        """
        spool = self.get_spool(spool_id)
        extra = (spool or {}).get("extra") or {}
        slots = {}
        for n in range(1, max_uids + 1):
            val = (extra.get(self.uid_field_name(n)) or "").strip()
            if val:
                # Values are stored as JSON-encoded strings; decode with fallback
                # to the raw string for any legacy values written before this fix.
                # Only accept the decoded result when it is a str to avoid edge
                # cases where a digits-only UID (e.g. "1234") decodes to int.
                try:
                    decoded = json.loads(val)
                    if isinstance(decoded, str):
                        val = decoded
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                if val:
                    slots[n] = val
        return slots

    @staticmethod
    def _decode_extra_value(raw_v):
        """Decode a Spoolman extra-field value to a plain string.

        Values stored by this plugin are JSON-encoded (e.g. ``'"C2C304EB"'``).
        Older entries or values written by external tools may be bare strings
        (e.g. ``'C2C304EB'``).  Both forms are handled by attempting JSON
        decode first and falling back to the raw string.

        Returns the decoded string, or the raw string if decoding fails.
        """
        decoded = str(raw_v)
        try:
            candidate = json.loads(str(raw_v))
            if isinstance(candidate, str):
                decoded = candidate
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return decoded

    def find_spool_by_uid(self, uid_hex, max_uids):
        """Search Spoolman for a spool whose ``rfid_uid_N`` extra field equals ``uid_hex``.

        Pre-check: if the ``rfid_uid_N`` extra fields do not yet exist in Spoolman,
        no spool can possibly hold this UID — create the fields and return ``None``
        immediately to avoid false-positive matches.

        Primary path: queries each numbered slot with the JSON-encoded value (since
        Spoolman stores extra field values as JSON-encoded strings).  For each hit,
        the search-response ``extra`` dict is checked inline:

        * If the **queried field** is present in the response with a non-matching
          value, Spoolman returned a partial/substring match — the spool is rejected
          inline without any additional HTTP fetch and added to ``_rejected_ids`` so
          the same false-positive spool is silently skipped in subsequent slot queries.

        * If the response ``extra`` does not include the queried field, a single
          secondary GET fetch is performed to verify; this handles the (rare) case
          where the search API omits extra-field values from its response.

        Fallback path: if all per-slot queries return empty or only false-positives,
        fetches all spools and scans their ``extra`` dicts in Python, decoding
        JSON-encoded values with a fallback to the raw string.

        Returns ``None`` if not found.

        Raises ``RuntimeError`` when any HTTP/network failure makes the result
        inconclusive (slot query error, secondary verification fetch error, or
        fallback scan error).  Callers must treat this as "unknown — do not
        auto-create" to avoid duplicate spool creation on transient failures.
        """
        # Short-circuit: if rfid_uid_N fields don't exist yet, no spool can have
        # this UID — create them now and skip the search entirely.
        if not self.fields_exist(max_uids):
            LOG.info(
                "find_spool_by_uid: rfid_uid_N fields not present"
                " — creating them and skipping search (uid=%s)", uid_hex,
            )
            self.ensure_rfid_uid_fields(max_uids)
            return None

        primary_had_error = False
        # Track spool IDs that were definitively shown not to contain uid_hex so
        # the same false-positive candidate is not re-checked for every slot query.
        _rejected_ids = set()  # type: set[int]

        for n in range(1, max_uids + 1):
            field_key = self.uid_field_name(n)
            try:
                spools = self._req(
                    "GET",
                    f"/api/v1/spool?extra[{field_key}]={url_parse.quote(json.dumps(uid_hex), safe='')}",
                )
                if not (isinstance(spools, list) and spools):
                    continue
                for spool in spools:
                    spool_id_val = spool.get("id")
                    if spool_id_val is None:
                        continue
                    candidate_id = int(spool_id_val)

                    # Skip candidates already proved not to contain uid_hex.
                    if candidate_id in _rejected_ids:
                        LOG.debug(
                            "find_spool_by_uid: spool %d already rejected"
                            " this call — skipping (uid=%s field=%s)",
                            candidate_id, uid_hex, field_key,
                        )
                        continue

                    # --- Inline verification using the search-response extra dict ---
                    resp_extra = spool.get("extra") or {}
                    if isinstance(resp_extra, dict):
                        # Fast path: UID matches one of the response slots → confirmed.
                        for _n in range(1, max_uids + 1):
                            raw_v = resp_extra.get(self.uid_field_name(_n))
                            if raw_v is None:
                                continue
                            if self._decode_extra_value(raw_v) == uid_hex:
                                LOG.debug(
                                    "find_spool_by_uid: uid=%s confirmed in spool %d"
                                    " via inline response extra (slot %d)",
                                    uid_hex, candidate_id, _n,
                                )
                                return candidate_id

                        # The queried field is present in the response with a
                        # non-matching value — this is a Spoolman partial/substring
                        # match false-positive.  Reject inline; no secondary fetch.
                        queried_raw = resp_extra.get(field_key)
                        if queried_raw is not None:
                            LOG.info(
                                "find_spool_by_uid: spool %d returned for uid=%s"
                                " query on %s but response shows %s=%r"
                                " (decoded: %r) — Spoolman partial/substring match;"
                                " rejecting without secondary fetch",
                                candidate_id, uid_hex, field_key,
                                field_key, queried_raw,
                                self._decode_extra_value(queried_raw),
                            )
                            _rejected_ids.add(candidate_id)
                            continue

                    # The response either has no extra dict or did not include the
                    # queried field.  Fall back to one secondary fetch to verify.
                    LOG.debug(
                        "find_spool_by_uid: spool %d — response missing %s;"
                        " secondary fetch needed to verify uid=%s",
                        candidate_id, field_key, uid_hex,
                    )
                    try:
                        slots = self.get_uid_slots(candidate_id, max_uids)
                    except Exception as verify_exc:
                        LOG.warning(
                            "find_spool_by_uid: secondary fetch for spool %d"
                            " failed: %s — lookup is inconclusive",
                            candidate_id, verify_exc,
                        )
                        raise
                    if slots and uid_hex in slots.values():
                        LOG.debug(
                            "find_spool_by_uid: uid=%s confirmed in spool %d"
                            " via secondary fetch",
                            uid_hex, candidate_id,
                        )
                        return candidate_id
                    LOG.info(
                        "find_spool_by_uid: spool %d secondary fetch slots=%r —"
                        " uid=%s not present; spool rejected",
                        candidate_id, list(slots.values()) if slots else [],
                        uid_hex,
                    )
                    _rejected_ids.add(candidate_id)
            except Exception as exc:
                LOG.warning(
                    "find_spool_by_uid uid=%s field=%s: %s — lookup may be inconclusive",
                    uid_hex, field_key, exc,
                )
                primary_had_error = True

        if primary_had_error:
            raise RuntimeError(
                f"find_spool_by_uid: Spoolman lookup for uid={uid_hex} was inconclusive"
                " (one or more slot queries or secondary fetches failed) —"
                " treating as error, not miss"
            )

        # Fallback: fetch all spools and scan rfid_uid_N values in Python.
        # This handles legacy entries where the search index may not have been
        # updated, and covers Spoolman versions that do not index extra fields.
        LOG.debug(
            "find_spool_by_uid: no match in %d slot queries for uid=%s"
            " — running fallback full-spool scan",
            max_uids, uid_hex,
        )
        try:
            all_spools = self._req("GET", "/api/v1/spool") or []
            if isinstance(all_spools, list):
                for spool in all_spools:
                    extra = spool.get("extra") or {}
                    if not isinstance(extra, dict):
                        continue
                    for n in range(1, max_uids + 1):
                        raw_v = extra.get(self.uid_field_name(n))
                        if raw_v is None:
                            continue
                        if self._decode_extra_value(raw_v) == uid_hex:
                            spool_id_val = spool.get("id")
                            if spool_id_val is not None:
                                LOG.debug(
                                    "find_spool_by_uid: uid=%s found in spool %d"
                                    " via fallback full-spool scan",
                                    uid_hex, spool_id_val,
                                )
                                return int(spool_id_val)
        except Exception as exc:
            LOG.warning(
                "find_spool_by_uid fallback scan failed: %s — lookup is inconclusive",
                exc,
            )
            raise RuntimeError(
                f"find_spool_by_uid: fallback full-spool scan for uid={uid_hex} failed:"
                f" {exc}"
            ) from exc
        LOG.info(
            "find_spool_by_uid: uid=%s not found in Spoolman"
            " (checked %d slot queries + fallback scan;"
            " false-positives rejected: %d)",
            uid_hex, max_uids, len(_rejected_ids),
        )
        return None

    def _patch_uid_field(self, spool_id, field_key, value):
        """PATCH a single ``rfid_uid_N`` field on *spool_id*.

        Values are JSON-encoded before sending (Spoolman stores extra field values
        as JSON-encoded strings).
        Returns ``True`` on success.  On HTTP errors re-raises
        ``url_error.HTTPError`` (with ``_body_text`` attached by ``_req``) so
        callers can inspect the status code and act (e.g. auto-create fields on
        400 "Unknown extra field").  Returns ``False`` on non-HTTP errors.
        """
        payload = {"extra": {field_key: json.dumps(value)}}
        try:
            self._req("PATCH", f"/api/v1/spool/{spool_id}", payload)
            LOG.debug(
                "_patch_uid_field spool=%s field=%s: succeeded",
                spool_id, field_key,
            )
            return True
        except url_error.HTTPError:
            raise  # Let callers inspect status code and body
        except Exception as exc:
            LOG.debug(
                "_patch_uid_field spool=%s field=%s failed: %s",
                spool_id, field_key, exc,
            )
            return False

    def add_uid_to_spool(self, spool_id, uid_hex, max_uids):
        """Write ``uid_hex`` into the first empty ``rfid_uid_N`` slot on ``spool_id``.

        Safe read-modify-write: slots are fetched first; if the fetch fails an
        exception is raised so callers can abort rather than silently overwrite data.
        Returns ``True`` when ``uid_hex`` is now registered (was already there, or
        written to a free slot).  Returns ``False`` when all slots are occupied,
        when field creation fails (permanent or transient), or when the retry PATCH
        fails with a non-schema error.  Raises ``HTTPError`` for non-400 HTTP errors
        on the first PATCH attempt so callers can log them at an appropriate level.

        When the PATCH returns HTTP 400 "Unknown extra field", attempts to
        auto-create all ``rfid_uid_N`` fields via ``ensure_rfid_uid_fields`` and
        retries the PATCH once.
        """
        slots = self.get_uid_slots(spool_id, max_uids)
        for n, v in slots.items():
            if v == uid_hex:
                LOG.debug(
                    "add_uid_to_spool: uid=%s already in slot %d on spool %s",
                    uid_hex, n, spool_id,
                )
                return True
        # Ensure all rfid_uid_N extra fields exist before attempting any PATCH.
        # This prevents HTTP 400 "Unknown extra field" in normal operation.
        if not self.fields_exist(max_uids):
            LOG.info(
                "add_uid_to_spool: rfid_uid_N fields not all present"
                " — creating before PATCH"
            )
            self.ensure_rfid_uid_fields(max_uids)
        for n in range(1, max_uids + 1):
            if n not in slots:
                field_key = self.uid_field_name(n)
                try:
                    return self._patch_uid_field(spool_id, field_key, uid_hex)
                except url_error.HTTPError as exc:
                    body_text = getattr(exc, "_body_text", "")
                    if exc.code == 400 and "unknown extra field" in body_text.lower():
                        # Fields don't exist — try to create them and retry once.
                        LOG.info(
                            "add_uid_to_spool: HTTP 400 Unknown extra field for spool=%s"
                            " field=%s — attempting to auto-create rfid_uid_N fields",
                            spool_id, field_key,
                        )
                        ok, _permanent = self.ensure_rfid_uid_fields(max_uids)
                        if ok:
                            try:
                                retry_ok = self._patch_uid_field(spool_id, field_key, uid_hex)
                                if retry_ok:
                                    LOG.info(
                                        "add_uid_to_spool: retry PATCH succeeded for"
                                        " spool=%s field=%s uid=%s",
                                        spool_id, field_key, uid_hex,
                                    )
                                return retry_ok
                            except url_error.HTTPError as retry_exc:
                                retry_body_text = getattr(retry_exc, "_body_text", "")
                                if (
                                    retry_exc.code == 400
                                    and "unknown extra field" in retry_body_text.lower()
                                ):
                                    # Retry hit the same expected schema problem — give up gracefully.
                                    return False
                                raise
                            except Exception:
                                return False
                        # Field creation failed (permanent or transient) — give up.
                        if _permanent:
                            LOG.warning(
                                "add_uid_to_spool: field auto-create failed for spool=%s"
                                " field=%s (permanent=%s) — uid=%s will not be persisted",
                                spool_id, field_key, _permanent, uid_hex,
                            )
                        else:
                            LOG.warning(
                                "add_uid_to_spool: field auto-create failed for spool=%s"
                                " field=%s (permanent=%s) — uid=%s was not persisted"
                                " this attempt",
                                spool_id, field_key, _permanent, uid_hex,
                            )
                        return False
                    raise  # Propagate non-400 errors to callers
        LOG.warning(
            "add_uid_to_spool: all %d rfid_uid_N slots occupied on spool %s;"
            " cannot register uid=%s",
            max_uids, spool_id, uid_hex,
        )
        return False

    def remove_uid_from_spool(self, spool_id, uid_hex, max_uids):
        """Clear the ``rfid_uid_N`` slot that contains ``uid_hex`` on ``spool_id``.

        Safe read-modify-write: slots are fetched first; raises on fetch failure.
        Returns ``True`` when ``uid_hex`` is no longer on the spool (was absent, or
        successfully cleared).  Returns ``False`` on PATCH failure (HTTP or otherwise).
        """
        slots = self.get_uid_slots(spool_id, max_uids)
        for n, v in slots.items():
            if v == uid_hex:
                try:
                    return self._patch_uid_field(spool_id, self.uid_field_name(n), "")
                except Exception as exc:
                    LOG.debug(
                        "remove_uid_from_spool spool=%s uid=%s failed: %s",
                        spool_id, uid_hex, exc,
                    )
                    return False
        return True  # uid_hex not present — nothing to do

    # --- Vendor helpers ---

    def find_vendor(self, name):
        """Return the first vendor dict whose name matches (case-insensitive), or None.

        Raises on HTTP/network errors so callers can abort instead of creating
        duplicate records on lookup failure.
        """
        results = self._req("GET", f"/api/v1/vendor?name={url_parse.quote(name, safe='')}")
        items = results if isinstance(results, list) else (results or {}).get("items", [])
        for v in items:
            if str(v.get("name", "")).lower() == name.lower():
                return v
        return None

    def create_vendor(self, name):
        """Create a new vendor and return the vendor dict."""
        return self._req("POST", "/api/v1/vendor", {"name": name})

    def find_or_create_vendor(self, name):
        """Return the vendor_id for *name*, creating the vendor if it does not exist.

        Raises ``ValueError`` when both the lookup and the create call return an
        unexpected response (e.g. Spoolman returns an empty body).
        """
        vendor = self.find_vendor(name)
        if vendor is None:
            vendor = self.create_vendor(name)
        if not isinstance(vendor, dict) or vendor.get("id") is None:
            raise ValueError(
                f"Spoolman vendor find/create for {name!r} returned unexpected response: {vendor!r}"
            )
        return int(vendor["id"])

    # --- Filament helpers ---

    def find_filament(self, name, vendor_id):
        """Return the first filament dict matching name + vendor_id, or None.

        Raises on HTTP/network errors so callers can abort instead of creating
        duplicate records on lookup failure.
        """
        results = self._req(
            "GET",
            f"/api/v1/filament?name={url_parse.quote(name, safe='')}&vendor_id={vendor_id}",
        )
        items = results if isinstance(results, list) else (results or {}).get("items", [])
        for f in items:
            vendor_info = f.get("vendor") or {}
            if (str(f.get("name", "")).lower() == name.lower()
                    and int(vendor_info.get("id", -1)) == int(vendor_id)):
                return f
        return None

    def create_filament(self, name, vendor_id, material, density=None, color_hex=None, diameter=1.75):
        """Create a new filament and return the filament dict."""
        body = {"name": name, "vendor_id": int(vendor_id), "material": material}
        # density and diameter are required by the Spoolman API; always send them.
        try:
            body["density"] = float(density) if density is not None else _DENSITY_DEFAULT
        except (TypeError, ValueError):
            body["density"] = _DENSITY_DEFAULT
        try:
            body["diameter"] = float(diameter) if diameter is not None else 1.75
        except (TypeError, ValueError):
            body["diameter"] = 1.75
        if color_hex:
            body["color_hex"] = str(color_hex).lstrip("#").upper()
        return self._req("POST", "/api/v1/filament", body)

    def find_or_create_filament(self, name, vendor_id, material,
                                density=None, color_hex=None, diameter=1.75):
        """Return the filament_id for *name*+*vendor_id*, creating it if necessary.

        Raises ``ValueError`` when both the lookup and the create call return an
        unexpected response (e.g. Spoolman returns an empty body).
        """
        fil = self.find_filament(name, vendor_id)
        if fil is None:
            fil = self.create_filament(name, vendor_id, material, density, color_hex, diameter)
        if not isinstance(fil, dict) or fil.get("id") is None:
            raise ValueError(
                f"Spoolman filament find/create for {name!r} returned unexpected response: {fil!r}"
            )
        return int(fil["id"])

    # --- Spool helpers ---

    def create_spool(self, filament_id, initial_weight=None, remaining_weight=None,
                     spool_weight=None, lot_nr=None, extra=None):
        """Create a new spool and return the spool dict.

        Args:
            filament_id:      required — the filament to associate with the spool.
            initial_weight:   weight of the filament on a brand-new full spool (grams).
            remaining_weight: current remaining filament weight (grams).
            spool_weight:     weight of the empty spool holder (grams).
            lot_nr:           lot / tray UID string for identifying this spool.
            extra:            dict of extra field key→value pairs to store on the spool.
        """
        body = {"filament_id": int(filament_id)}
        if initial_weight is not None:
            try:
                body["initial_weight"] = float(initial_weight)
            except (ValueError, TypeError):
                pass
        if remaining_weight is not None:
            try:
                body["remaining_weight"] = float(remaining_weight)
            except (ValueError, TypeError):
                pass
        if spool_weight is not None:
            try:
                body["spool_weight"] = float(spool_weight)
            except (ValueError, TypeError):
                pass
        if lot_nr:
            body["lot_nr"] = str(lot_nr)
        if extra and isinstance(extra, dict):
            body["extra"] = {str(k): json.dumps(v) for k, v in extra.items() if v is not None}
        return self._req("POST", "/api/v1/spool", body)

    def auto_create_spool(self, filament_info: dict, uid_hex: Optional[str] = None) -> Optional[int]:
        """Find or create a Spoolman vendor/filament/spool from tag filament data.

        Returns the new Spoolman spool ID on success, or None on failure.

        Required before creating anything:
          * material  — filament type (e.g. "PLA", "PETG")

        Optional but used when present:
          * color_hex   — 6-digit hex color string; omitted from filament if absent
          * weight_g    — spool weight in grams; defaults to 1000 g if not supplied
          * brand       — inferred from tag_format, falls back to "Generic"
          * diameter_mm — defaults to 1.75 mm

        The optional uid_hex argument (hardware RFID UID) is stored in the
        spool's extra field (rfid_uid_1) at creation time when provided, after
        ensuring the extra-field schema exists in Spoolman.

        Steps:
        1. Determine density via SpoolmanDB (Bambu DB or materials.json) or fallback
           table.  For Bambu tags the DB is searched first by material_id (Bambu SKU
           stored in the per-colour ``id`` field, e.g. "GFA50") then by material +
           color_hex.  This yields a ``bambu_match`` (filament entry) and optional
           ``bambu_color_match`` (colour entry) which carry richer metadata.
        2. Resolve vendor_id: find or create, with Generic fallback if brand fails.
           For Bambu tags the manufacturer name from the SpoolmanDB top-level JSON
           is preferred over the raw tag brand when resolving the vendor.
        3. Search for existing filament by external_id, then material + vendor
           (prefer color_hex match).
        4. If no match: POST /api/v1/filament — create the filament.  Fields are
           populated from tag data first, then filled from SpoolmanDB (name,
           extruder_temp, bed_temp, spool_weight) to ensure Spoolman has complete
           metadata without requiring complete tag data.
        5. POST /api/v1/spool — create the spool referencing the filament,
           including lot_nr (tray_uid) and uid_hex in extra fields.
        """
        material = str(filament_info.get("material") or "").strip()
        if not material:
            LOG.debug("auto_create_spool: skipped — no material in tag data")
            self._trace_info(
                "auto_create_spool: skipped — tag metadata has no material "
                "(keys=%s)", sorted(filament_info.keys()))
            return None

        color_hex = str(filament_info.get("color_hex") or "").strip().lstrip("#").upper()
        if not color_hex:
            LOG.debug(
                "auto_create_spool: no color_hex in tag data — proceeding without color"
            )
            self._trace_info(
                "auto_create_spool: no color_hex in tag data — proceeding without color")

        weight_g = filament_info.get("weight_g")
        if weight_g is None:
            weight_g = _DEFAULT_SPOOL_WEIGHT_G
            LOG.debug(
                "auto_create_spool: weight not in tag data, defaulting to %d g",
                _DEFAULT_SPOOL_WEIGHT_G,
            )
        else:
            try:
                weight_g = float(weight_g)
            except (TypeError, ValueError):
                weight_g = _DEFAULT_SPOOL_WEIGHT_G
                LOG.debug(
                    "auto_create_spool: invalid weight_g in tag data: %r, defaulting to %d g",
                    filament_info.get("weight_g"), _DEFAULT_SPOOL_WEIGHT_G,
                )

        brand = str(filament_info.get("brand") or "").strip()
        if not brand:
            tag_fmt = str(filament_info.get("tag_format") or "")
            brand = _TAG_FORMAT_BRANDS.get(tag_fmt, "Generic")
            LOG.debug(
                "auto_create_spool: brand not in tag data, deduced %r from tag_format=%r",
                brand, tag_fmt,
            )
            self._trace_info(
                "auto_create_spool: brand not in tag data; deduced %r "
                "from tag_format=%r", brand, tag_fmt)

        diameter_mm = filament_info.get("diameter_mm") or 1.75
        is_bambu = bool(filament_info.get("is_bambu")) or "bambu" in brand.lower()

        # material_id (e.g. "GFA50", "GFG02") is the Bambu external filament DB id.
        # Used as Spoolman external_id to look up an existing matching filament entry.
        material_id = str(filament_info.get("material_id") or "").strip() or None
        self._trace_info(
            "auto_create_spool: begin material=%r brand=%r color=%r "
            "material_id=%r weight_g=%s diameter_mm=%s uid_hex=%s",
            material, brand, color_hex or None, material_id, weight_g,
            diameter_mm, uid_hex)
        _raw_tray_uid = str(filament_info.get("tray_uid") or "").strip()
        # Validate tray_uid: must be a non-empty even-length hex string (e.g. 32 hex chars
        # for a 16-byte Bambu Tray UID).  If bytes slipped through, convert; if garbled, discard.
        tray_uid: Optional[str] = None
        if _raw_tray_uid:
            if isinstance(filament_info.get("tray_uid"), (bytes, bytearray)):
                # Raw bytes — convert to uppercase hex string.
                tray_uid = filament_info["tray_uid"].hex().upper()
                LOG.debug("auto_create_spool: tray_uid converted from bytes → %s", tray_uid)
            elif re.fullmatch(r"[0-9A-Fa-f]+", _raw_tray_uid) and len(_raw_tray_uid) % 2 == 0:
                tray_uid = _raw_tray_uid.upper()
            else:
                LOG.warning(
                    "auto_create_spool: tray_uid %r is not a valid hex string"
                    " — discarding to avoid Spoolman 422 error",
                    _raw_tray_uid,
                )

        # ------------------------------------------------------------------
        # 1. Determine density (required by Spoolman POST /api/v1/filament)
        #    For Bambu tags, also fetch richer SpoolmanDB metadata (name,
        #    temperatures, spool weight) to use when creating a new filament.
        # ------------------------------------------------------------------
        density: float = _DENSITY_DEFAULT
        material_lower = material.lower().strip()
        # bambu_match: the matching filament entry from SpoolmanDB bambulab.json
        # bambu_color_match: the specific colour entry within bambu_match (may be None)
        bambu_match: Optional[dict] = None
        bambu_color_match: Optional[dict] = None

        if is_bambu:
            bambu_filaments = _fetch_spoolmandb_bambu()
            self._trace_info(
                "auto_create_spool: Bambu metadata detected; searching "
                "SpoolmanDB Bambu data by material_id=%r then material/color",
                material_id)

            # 1a. Try to match by Bambu SKU (material_id) via colours[].id — most
            #     precise, uniquely identifies brand + material + colour variant.
            if material_id:
                for entry in bambu_filaments:
                    for c in (entry.get("colors") or []):
                        if str(c.get("id") or "").upper() == material_id.upper():
                            bambu_match = entry
                            bambu_color_match = c
                            # Use the SpoolmanDB colour hex when available; it is
                            # the canonical value for this SKU.
                            if c.get("hex"):
                                color_hex = str(c["hex"]).upper().lstrip("#")
                            break
                    if bambu_match is not None:
                        break
                if bambu_match is not None:
                    LOG.debug(
                        "auto_create_spool: SpoolmanDB Bambu match by SKU"
                        " material_id=%s color=%r",
                        material_id, bambu_color_match.get("name") if bambu_color_match else None,
                    )
                    self._trace_info(
                        "auto_create_spool: SpoolmanDB Bambu match by "
                        "material_id=%s name=%r color=%r",
                        material_id, bambu_match.get("name"),
                        bambu_color_match.get("name") if bambu_color_match else None)

            # 1b. Fall back to material-type + color_hex matching when no SKU match.
            if bambu_match is None:
                self._trace_info(
                    "auto_create_spool: no Bambu SKU match; trying "
                    "material=%r color_hex=%r", material, color_hex or None)
                for entry in bambu_filaments:
                    if str(entry.get("material") or "").lower().strip() == material_lower:
                        bambu_match = entry
                        if color_hex:
                            for c in (entry.get("colors") or []):
                                c_hex = str(c.get("hex") or "").upper().lstrip("#")
                                if c_hex == color_hex:
                                    bambu_color_match = c
                                    color_hex = c_hex
                                    break
                        break

            if bambu_match is not None:
                try:
                    density = float(bambu_match["density"])
                    LOG.debug(
                        "auto_create_spool: density=%s from SpoolmanDB Bambu material=%s",
                        density, material,
                    )
                    self._trace_info(
                        "auto_create_spool: using density=%s from "
                        "SpoolmanDB Bambu match name=%r",
                        density, bambu_match.get("name"))
                except (KeyError, TypeError, ValueError):
                    self._trace_info(
                        "auto_create_spool: Bambu match had no usable density; "
                        "falling back to generic material density")
                    is_bambu = False
            else:
                self._trace_info(
                    "auto_create_spool: no SpoolmanDB Bambu match; "
                    "falling back to generic material density")
                is_bambu = False

        if not is_bambu:
            mat_db = _fetch_spoolmandb_materials()
            if mat_db:
                db_density = mat_db.get(material_lower)
                if db_density is not None:
                    density = db_density
                else:
                    density = _DENSITY_FALLBACK.get(material_lower, _DENSITY_DEFAULT)
                LOG.debug(
                    "auto_create_spool: density=%s from SpoolmanDB materials material=%s",
                    density, material,
                )
                self._trace_info(
                    "auto_create_spool: using density=%s for material=%r "
                    "(source=%s)",
                    density, material,
                    "SpoolmanDB materials" if db_density is not None else "fallback table")
            else:
                density = _DENSITY_FALLBACK.get(material_lower, _DENSITY_DEFAULT)
                LOG.debug(
                    "auto_create_spool: density=%s from fallback table"
                    " (SpoolmanDB materials unavailable) material=%s",
                    density, material,
                )
                self._trace_info(
                    "auto_create_spool: using density=%s for material=%r "
                    "(SpoolmanDB materials unavailable; fallback table)",
                    density, material)

        # ------------------------------------------------------------------
        # 2. Resolve or create vendor — try brand first, then Generic fallback.
        #    Raises are caught so a failed brand lookup doesn't abort the whole
        #    operation; we fall back to "Generic" instead.
        #    For Bambu tags, prefer the manufacturer name from SpoolmanDB (the
        #    canonical name) over the raw tag brand to avoid creating a duplicate
        #    vendor when the tag uses a slightly different spelling.
        # ------------------------------------------------------------------
        vendor_id: Optional[int] = None
        resolved_vendor_name: Optional[str] = None
        _vendor_candidates: list = []
        # Use the SpoolmanDB manufacturer name as the primary vendor candidate
        # for Bambu tags, since it is the canonical name used in the filament DB.
        if is_bambu and _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE:
            _spoolmandb_mfr = _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE
            if _spoolmandb_mfr.lower() != "generic":
                _vendor_candidates.append(_spoolmandb_mfr)
            # Also add the tag brand as a fallback (may differ from DB name).
            if brand and brand.lower() != "generic" and brand != _spoolmandb_mfr:
                _vendor_candidates.append(brand)
        elif brand and brand.lower() != "generic":
            _vendor_candidates.append(brand)
        _vendor_candidates.append("Generic")
        self._trace_info(
            "auto_create_spool: resolving vendor; candidates=%s",
            _vendor_candidates)

        for _vname in _vendor_candidates:
            try:
                self._trace_info(
                    "auto_create_spool: vendor step: find_or_create_vendor(%r)",
                    _vname)
                vendor_id = self.find_or_create_vendor(_vname)
                resolved_vendor_name = _vname
                LOG.debug(
                    "auto_create_spool: resolved vendor id=%s name=%r", vendor_id, _vname
                )
                self._trace_info(
                    "auto_create_spool: resolved vendor id=%s name=%r",
                    vendor_id, _vname)
                break
            except Exception as exc:
                LOG.debug(
                    "auto_create_spool: vendor find/create failed for %r: %s", _vname, exc
                )
                self._trace_info(
                    "auto_create_spool: vendor candidate %r failed: %s",
                    _vname, exc)
                if _vname != "Generic":
                    LOG.debug(
                        "auto_create_spool: vendor resolution failed for %r,"
                        " falling back to 'Generic'", brand,
                    )

        if vendor_id is None:
            LOG.debug(
                "auto_create_spool: vendor resolution failed"
                " — proceeding without vendor_id"
            )
            self._trace_info(
                "auto_create_spool: vendor resolution failed; "
                "continuing without vendor_id")

        # ------------------------------------------------------------------
        # 3. Search for an existing matching filament.
        #    First try by external_id (Bambu material_id like "GFA50"), then
        #    fall back to material + vendor search with color_hex preference.
        # ------------------------------------------------------------------
        filament_id: Optional[int] = None
        search_ok = True
        try:
            # 3a. Search by external_id when a Bambu material_id is available.
            if material_id:
                self._trace_info(
                    "auto_create_spool: filament step: searching by external_id=%s",
                    material_id)
                ext_results = self._req(
                    "GET",
                    "/api/v1/filament?" + url_parse.urlencode({"external_id": material_id}),
                )
                if isinstance(ext_results, list) and ext_results:
                    filament_id = int(ext_results[0]["id"])
                    LOG.debug(
                        "auto_create_spool: found filament id=%s by external_id=%s",
                        filament_id, material_id,
                    )
                    self._trace_info(
                        "auto_create_spool: found filament id=%s by external_id=%s",
                        filament_id, material_id)
                else:
                    self._trace_info(
                        "auto_create_spool: no filament found by external_id=%s",
                        material_id)

            # 3b. Fall back to material + vendor search.
            if filament_id is None:
                params: dict = {"material": material}
                if vendor_id is not None:
                    params["vendor_name"] = resolved_vendor_name
                self._trace_info(
                    "auto_create_spool: filament step: searching by %s",
                    params)
                filaments = self._req(
                    "GET", "/api/v1/filament?" + url_parse.urlencode(params)
                )
                if isinstance(filaments, list) and filaments:
                    self._trace_info(
                        "auto_create_spool: filament search returned %d "
                        "candidate(s); color preference=%r",
                        len(filaments), color_hex or None)
                    if color_hex:
                        for f in filaments:
                            if str(f.get("color_hex") or "").upper() == color_hex:
                                filament_id = int(f["id"])
                                break
                    if filament_id is None:
                        filament_id = int(filaments[0]["id"])
                    LOG.debug(
                        "auto_create_spool: found filament id=%s material=%s",
                        filament_id, material,
                    )
                    self._trace_info(
                        "auto_create_spool: using existing filament id=%s "
                        "material=%r vendor=%r",
                        filament_id, material, resolved_vendor_name)
                else:
                    self._trace_info(
                        "auto_create_spool: no existing filament matched "
                        "material=%r vendor=%r",
                        material, resolved_vendor_name)
        except url_error.URLError as exc:
            LOG.debug("auto_create_spool: filament search failed: %s", exc)
            self._trace_info("auto_create_spool: filament search failed: %s", exc)
            search_ok = False
        except Exception:
            LOG.exception("auto_create_spool: filament search error")
            self._trace_info("auto_create_spool: filament search raised an error")
            search_ok = False

        if not search_ok:
            self._trace_info(
                "auto_create_spool: aborting because filament search did not complete")
            return None

        # ------------------------------------------------------------------
        # 4. Create filament if no match found.
        #    Populate fields from tag data first, then fill gaps from SpoolmanDB
        #    (bambu_match) so the Spoolman record is as complete as possible even
        #    when the tag does not carry temperature or weight metadata.
        # ------------------------------------------------------------------
        if filament_id is None:
            self._trace_info(
                "auto_create_spool: no matching filament found; creating "
                "filament from tag/SpoolmanDB metadata")
            # Build a descriptive name. Prefer SpoolmanDB names first, then
            # tag-provided material_detail (e.g. "PLA_Basic" -> "PLA Basic")
            # before falling back to the generic "{brand} {material}" label.
            db_fil_name = str(bambu_match.get("name") or "").strip() if bambu_match else ""
            if db_fil_name:
                filament_name = f"{resolved_vendor_name} {db_fil_name}" if resolved_vendor_name else db_fil_name
            else:
                material_label = str(
                    filament_info.get("material_detail") or material).strip()
                material_label = material_label.replace("_", " ")
                filament_name = f"{brand} {material_label}" if brand else material_label

            filament_body: dict = {
                "name": filament_name,
                "material": material,
                "density": density,
                "diameter": float(diameter_mm),
                "weight": float(weight_g),
            }
            if color_hex:
                filament_body["color_hex"] = color_hex
            if vendor_id is not None:
                filament_body["vendor_id"] = vendor_id
            if material_id:
                filament_body["external_id"] = material_id

            # Temperature settings: use tag data when present, otherwise fall
            # back to SpoolmanDB values (Bambu only).  SpoolmanDB carries both
            # lower ("extruder_temp") and upper ("extruder_temp_max") temps.
            min_temp = filament_info.get("min_temp")
            max_temp = filament_info.get("max_temp")
            bed_temp = filament_info.get("bed_temp")
            if bambu_match is not None:
                if min_temp is None:
                    min_temp = bambu_match.get("extruder_temp")
                if max_temp is None:
                    max_temp = bambu_match.get("extruder_temp_max")
                if bed_temp is None:
                    bed_temp = bambu_match.get("bed_temp")

            # settings_extruder_temp is a default/recommended value in
            # Spoolman, not a lower bound.  Use the upper hotend temperature
            # for Bambu/high-speed purge behavior, and keep min_temp only as
            # parsed metadata.
            _ext_max = _to_int_safe(max_temp)
            _bed = _to_int_safe(bed_temp)
            if _ext_max is not None:
                filament_body["settings_extruder_temp"] = _ext_max
                filament_body["settings_extruder_temp_max"] = _ext_max
            if _bed is not None:
                filament_body["settings_bed_temp"] = _bed

            try:
                LOG.info(
                    "auto_create_spool: creating filament — payload: %s",
                    json.dumps(filament_body, default=str),
                )
                self._trace_info(
                    "auto_create_spool: creating filament name=%r material=%r "
                    "vendor_id=%s color=%r density=%s",
                    filament_name, material, vendor_id, color_hex or None, density)
                self._trace_debug(
                    "auto_create_spool: filament create payload: %s",
                    json.dumps(filament_body, default=str))
                created = self._req("POST", "/api/v1/filament", filament_body)
                if not isinstance(created, dict) or created.get("id") is None:
                    LOG.warning(
                        "auto_create_spool: filament create returned unexpected response (id missing): %r",
                        created,
                    )
                    return None
                filament_id = int(created["id"])
                LOG.info(
                    "auto_create_spool: created filament id=%s name=%r density=%s",
                    filament_id, filament_name, density,
                )
                self._trace_info(
                    "auto_create_spool: created filament id=%s name=%r",
                    filament_id, filament_name)
            except url_error.URLError as exc:
                LOG.warning("auto_create_spool: filament create failed: %s", exc)
                return None
            except Exception:
                LOG.exception("auto_create_spool: filament create error")
                return None

        # ------------------------------------------------------------------
        # 5. Create spool, including lot_nr (tray_uid) and uid_hex extra field.
        #    The rfid_uid_1 extra field must be registered in Spoolman before
        #    it can be set on the spool.  Attempt to ensure it exists first;
        #    if that fails, create the spool without the extra UID field (the
        #    caller's add_uid_to_spool call will register it on the next scan).
        #    spool_weight_g: use tag data when present, otherwise use the
        #    SpoolmanDB value (Bambu only) so the tare weight is accurate.
        # ------------------------------------------------------------------
        spool_weight_g = filament_info.get("spool_weight_g")
        if spool_weight_g is None and bambu_match is not None:
            _db_spool_w = bambu_match.get("spool_weight")
            if _db_spool_w is not None:
                try:
                    spool_weight_g = float(_db_spool_w)
                    LOG.debug(
                        "auto_create_spool: spool_weight_g=%s from SpoolmanDB",
                        spool_weight_g,
                    )
                except (TypeError, ValueError):
                    pass
        spool_extra: Optional[dict] = None
        if uid_hex:
            try:
                self._trace_info(
                    "auto_create_spool: ensuring RFID UID extra field before spool create")
                ok, _ = self.ensure_rfid_uid_fields(1)
                if ok:
                    spool_extra = {self.uid_field_name(1): uid_hex}
                    self._trace_info(
                        "auto_create_spool: spool create will include extra %s=%s",
                        self.uid_field_name(1), uid_hex)
            except Exception as exc:
                LOG.debug(
                    "auto_create_spool: ensure_rfid_uid_fields failed: %s"
                    " — creating spool without extra UID field", exc
                )
                self._trace_info(
                    "auto_create_spool: ensure_rfid_uid_fields failed: %s; "
                    "creating spool without extra UID field", exc)
        try:
            _spool_log = {
                "filament_id": filament_id,
                "remaining_weight": float(weight_g),
                "spool_weight": spool_weight_g,
                "lot_nr": tray_uid,
                "extra": spool_extra,
            }
            LOG.info(
                "auto_create_spool: creating spool — payload: %s",
                json.dumps(_spool_log, default=str),
            )
            self._trace_info(
                "auto_create_spool: creating spool filament_id=%s "
                "remaining_weight=%s spool_weight=%s lot_nr=%r extra_keys=%s",
                filament_id, float(weight_g), spool_weight_g, tray_uid,
                sorted((spool_extra or {}).keys()))
            self._trace_debug(
                "auto_create_spool: spool create payload: %s",
                json.dumps(_spool_log, default=str))
            created_spool = self.create_spool(
                filament_id,
                remaining_weight=float(weight_g),
                spool_weight=spool_weight_g,
                lot_nr=tray_uid,
                extra=spool_extra,
            )
            if not isinstance(created_spool, dict) or created_spool.get("id") is None:
                LOG.warning(
                    "auto_create_spool: spool create returned unexpected response (id missing): %r",
                    created_spool,
                )
                return None
            new_spool_id = int(created_spool["id"])
            LOG.info("auto_create_spool: created spool id=%s", new_spool_id)
            self._trace_info("auto_create_spool: created spool id=%s", new_spool_id)
            return new_spool_id
        except url_error.URLError as exc:
            LOG.warning("auto_create_spool: spool create failed: %s", exc)
            return None
        except Exception:
            LOG.exception("auto_create_spool: spool create error")
            return None

    @staticmethod
    def build_openspool_payload(spool_data: dict) -> Optional[str]:
        """Convert a Spoolman spool dict to an OpenSpool JSON string.

        Returns the JSON-encoded payload, or None if material is missing.
        The ``spoolman_id`` field is included so a subsequent read can
        immediately identify the spool without a Spoolman lookup.
        """
        filament = spool_data.get("filament") or {}
        material = str(filament.get("material") or "").strip()
        if not material:
            return None

        payload: dict = {
            "protocol": "openspool",
            "version": "1.0",
            "type": material,
        }

        color_hex = str(filament.get("color_hex") or "").strip().lstrip("#").upper()
        if color_hex:
            payload["color_hex"] = color_hex

        vendor = filament.get("vendor") or {}
        brand = str(vendor.get("name") or "").strip()
        if brand:
            payload["brand"] = brand

        min_temp = filament.get("settings_extruder_temp")
        if min_temp is not None:
            try:
                payload["min_temp"] = int(min_temp)
            except (TypeError, ValueError):
                pass

        max_temp = filament.get("settings_extruder_temp_max")
        if max_temp is not None:
            try:
                payload["max_temp"] = int(max_temp)
            except (TypeError, ValueError):
                pass

        remaining = spool_data.get("remaining_weight")
        if remaining is not None:
            try:
                payload["weight"] = float(remaining)
            except (TypeError, ValueError):
                pass

        spool_id_val = spool_data.get("id")
        if spool_id_val is not None:
            payload["spoolman_id"] = int(spool_id_val)

        return json.dumps(payload)
