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

"""
RFID tag format parser for the rfid Klipper extra.

Provides two public entry points:

    parse_tag(raw, uid_hex: str | None = None) -> dict | None
    is_parse_error(info: dict | None) -> bool

``raw`` may be:
  * ``bytes`` / ``bytearray`` — raw user-memory dump (starting at page 4 for
    NTAG21x / Type 2 tags, or a full MIFARE Classic sector dump).
  * ``dict`` — authenticated MIFARE Classic block data produced by
    ``read_authenticated_blocks()``.  Keys: ``"uid_bytes"`` (bytes) and
    ``"blocks"`` (dict mapping absolute block index → 16-byte bytes).

Supported formats
-----------------
elegoo          ELEGOO EPC-256 (NTAG213, binary)
bambu           Bambu Lab (MIFARE Classic 1K, HKDF-derived keys, pycryptodome)
anycubic_ace    Anycubic ACE (NTAG213/215, binary)
tigertag        TigerTag / TigerTag+ (NTAG213/215/216, binary)
creality        Creality CFS / K1 / K2 (MIFARE Classic, UID-derived Key B,
                AES-128-ECB-encrypted ASCII payload, pycryptodome)
creality_cfs    Creality CFS / K1 / K2 legacy hex-ASCII heuristic (unconfirmed,
                unauthenticated raw-dump fallback; superseded by "creality")
qidi            QIDI Box (MIFARE Classic 1K, binary codes)
opentag3d       OpenTag3D (NDEF MIME application/vnd.opentag3d, binary)
openspool       OpenSpool (NDEF JSON with protocol=openspool)
openprinttag    OpenPrintTag (NDEF MIME application/vnd.openprinttag, CBOR)
simplyprint_url SimplyPrint / QIDI standard URL tag (NDEF URI/Text with query string)
generic_ndef_json  Generic NDEF text record containing JSON filament data

Optional dependencies
---------------------
- pycryptodome (``pip3 install pycryptodome``) — required for Bambu Lab tag
  key derivation via HKDF-SHA256.  Without it, Bambu tag detection still works
  but parse_tag() returns an error dict (not a full filament dict) for Bambu tags.
- cbor2 (``pip3 install cbor2``) — required for OpenPrintTag CBOR payloads.

Bambu Lab tag authentication — how it works
--------------------------------------------
Bambu Lab spool tags are MIFARE Classic 1K chips with per-tag, per-sector
encryption keys derived via HKDF-SHA256.  The derivation procedure is adapted
from MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp (GPLv3):
  https://github.com/MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp/blob/c8aa59e6d4c132f9e78bde24d791bbb330a12b7d/source/app/src/main/java/app/mrb/bambuspoolpal/nfc/NfcTagProcessor.kt#L53-L139
No code was copied; the algorithm was reimplemented in Python from the
published procedure.

  IKM   = 4-byte tag UID (unique per spool) — uid is the HKDF input key material
  Salt  = static 16-byte Bambu device master key (see ``_BAMBU_MASTER_KEY``)
  Info  = b"RFID-A\\x00"  (Key A, used for all authenticated reads)
  Output = 96 bytes split into 16 × 6-byte MIFARE sector keys

Key B derivation (for write access)
------------------------------------
Key B is derived identically to Key A but with the context b"RFID-B\\x00".
This convention follows community tooling (e.g. open Bambu tag programmers).
On factory-programmed Bambu tags Key B is typically all-zeros; on custom/blank
MIFARE Classic tags programmed with this toolchain Key B is HKDF-derived.

  IKM   = 4-byte tag UID (same as Key A)
  Salt  = _BAMBU_MASTER_KEY (same as Key A)
  Info  = b"RFID-B\\x00"  (Key B, used for write authentication)
  Output = 96 bytes split into 16 × 6-byte sector Key B values

Use ``_bambu_derive_keys_b()`` to obtain Key B values.  Pass them with
``use_key_b=True`` to the reader's write method.

After key derivation, each of the 16 sectors is authenticated (Key A) and read
with the MFRC522 PCD_AUTHENT command before the data blocks can be accessed.
``parse_tag()`` expects these decrypted blocks as a dict:
  ``{"uid_bytes": bytes, "blocks": {abs_block_index: 16-byte-bytes}}``

Hardware requirements for Bambu tags:
  • Reader must support ISO/IEC 14443 Type A 3-pass authentication
    (MFRC522, PN532, ACR122U, RC663, Proxmark3 all qualify).
  • Standard "pass-through" USB HID card readers do NOT support this and
    will never be able to read Bambu tag sector data.
  • pycryptodome must be installed for HKDF key derivation.

Known limitations
-----------------
- QIDI Box tags are MIFARE Classic 1K and use the plain factory default Key
  A; Creality CFS/K1/K2 tags require a UID-derived Key B (see
  ``_creality_derive_key_b()``) and an AES-128-ECB-decrypted payload (see
  ``_try_creality_tag()``) — tag_handler.read_current_tag() tries Bambu Key
  A, then the default key, then the Creality Key B, in that order.  The
  legacy ``_try_creality_cfs()`` hex-ASCII heuristic only runs against
  unauthenticated raw dumps and is unconfirmed against real hardware.
- QIDI's official RFID guide documents FM11RF08S tags with the payload in
  sector 1/block 0 (absolute block 4): byte 0 material, byte 1 color, byte 2
  manufacturer.  If QIDI rich reads fail, also validate the QIDI-specific
  sector 1 Key A noted in docs/shared/qidi-rfid-reference.md.
- Factory Bambu tags carry an RSA-2048 signature; the Bambu printer firmware
  validates this signature so official firmware will reject reprogrammed tags.
  Writing Tray UID / spool metadata to custom (blank) MIFARE Classic tags
  programmed with this toolchain is fully supported via Key B authentication.
"""

from __future__ import annotations

import json
import logging
import re
import hashlib
import struct
from typing import Optional

_log = logging.getLogger("rfid.tag_parser")


def _make_trace(trace):
    if trace is None:
        return lambda level, msg, *args: None

    def _emit(level, msg, *args):
        try:
            trace(level, msg, *args)
        except Exception:
            pass

    return _emit


# ---------------------------------------------------------------------------
# pycryptodome – optional; required for Bambu Lab key derivation
# ---------------------------------------------------------------------------
try:
    from Cryptodome.Protocol.KDF import HKDF as _HKDF
    from Cryptodome.Hash import SHA256 as _SHA256
    from Cryptodome.Cipher import AES as _AES
    _PYCRYPTODOME_OK = True
except ImportError:
    try:
        from Crypto.Protocol.KDF import HKDF as _HKDF  # type: ignore[no-redef]
        from Crypto.Hash import SHA256 as _SHA256  # type: ignore[no-redef]
        from Crypto.Cipher import AES as _AES  # type: ignore[no-redef]
        _PYCRYPTODOME_OK = True
    except ImportError:
        _PYCRYPTODOME_OK = False
        _log.info(
            "rfid_tag_parser: pycryptodome not available — "
            "Bambu/Creality tag decryption disabled. "
            "Install with: pip3 install pycryptodome"
        )

# ---------------------------------------------------------------------------
# NDEF helpers
# ---------------------------------------------------------------------------

_TNF_WELL_KNOWN = 0x01
_TNF_MIME = 0x02
_TNF_ABSOLUTE_URI = 0x03
_TNF_EXTERNAL = 0x04


def _find_ndef_tlv(data: bytes) -> Optional[bytes]:
    """Return the raw bytes of the first NDEF message TLV (0x03) payload,
    or None if none is found."""
    i = 0
    while i < len(data):
        t = data[i]
        if t == 0x00:  # NULL TLV
            i += 1
            continue
        if t == 0xFE:  # Terminator TLV
            return None
        if i + 1 >= len(data):
            return None
        l = data[i + 1]
        if l == 0xFF:
            if i + 3 >= len(data):
                return None
            l = (data[i + 2] << 8) | data[i + 3]
            vstart = i + 4
        else:
            vstart = i + 2
        vend = vstart + l
        if vend > len(data):
            return None
        if t == 0x03:
            return data[vstart:vend]
        i = vend
    return None


def _parse_ndef_records(ndef: bytes) -> list[dict]:
    """Parse an NDEF message and return a list of record dicts.

    Each record dict has:
        tnf: int        Type Name Format (0-7)
        type: bytes     Record type field
        id: bytes       Record ID (may be empty)
        payload: bytes  Record payload
    """
    records = []
    idx = 0
    while idx < len(ndef):
        if idx >= len(ndef):
            break
        header = ndef[idx]
        idx += 1
        sr = bool(header & 0x10)   # Short Record
        il = bool(header & 0x08)   # ID Length present
        tnf = header & 0x07

        if idx >= len(ndef):
            break
        type_len = ndef[idx]
        idx += 1

        try:
            if sr:
                if idx >= len(ndef):
                    break
                payload_len = ndef[idx]
                idx += 1
            else:
                if idx + 3 >= len(ndef):
                    break
                payload_len = int.from_bytes(ndef[idx:idx + 4], "big")
                idx += 4

            id_len = 0
            if il:
                if idx >= len(ndef):
                    break
                id_len = ndef[idx]
                idx += 1

            rec_type = ndef[idx:idx + type_len]
            idx += type_len
            rec_id = ndef[idx:idx + id_len]
            idx += id_len

            if idx + payload_len > len(ndef):
                break
            payload = ndef[idx:idx + payload_len]
            idx += payload_len

            records.append({
                "tnf": tnf,
                "type": rec_type,
                "id": rec_id,
                "payload": payload,
            })

            # ME bit set — this is the last record
            if header & 0x40:
                break
        except Exception:
            break

    return records


def _decode_well_known_text(payload: bytes) -> Optional[str]:
    """Decode an NDEF Well Known Text record payload to a Python string."""
    if not payload:
        return None
    status = payload[0]
    lang_len = status & 0x3F
    text_bytes = payload[1 + lang_len:]
    try:
        if status & 0x80:
            return text_bytes.decode("utf-16")
        return text_bytes.decode("utf-8")
    except Exception:
        return text_bytes.decode("latin1", errors="ignore")


_URI_PREFIXES = {
    0x00: "", 0x01: "http://www.", 0x02: "https://www.", 0x03: "http://",
    0x04: "https://", 0x05: "tel:", 0x06: "mailto:", 0x07: "ftp://anonymous:anonymous@",
    0x08: "ftp://ftp.", 0x09: "ftps://", 0x0A: "sftp://", 0x0B: "smb://",
    0x0C: "nfs://", 0x0D: "ftp://", 0x0E: "dav://", 0x0F: "news:",
    0x10: "telnet://", 0x11: "imap:", 0x12: "rtsp://", 0x13: "urn:",
    0x14: "pop:", 0x15: "sip:", 0x16: "sips:", 0x17: "tftp:",
    0x18: "btspp://", 0x19: "btl2cap://", 0x1A: "btgoep://",
    0x1B: "tcpobex://", 0x1C: "irdaobex://", 0x1D: "file://",
    0x1E: "urn:epc:id:", 0x1F: "urn:epc:tag:", 0x20: "urn:epc:pat:",
    0x21: "urn:epc:raw:", 0x22: "urn:epc:", 0x23: "urn:nfc:",
}


def _decode_well_known_uri(payload: bytes) -> Optional[str]:
    """Decode an NDEF Well Known URI record payload to a string."""
    if not payload:
        return None
    prefix = _URI_PREFIXES.get(payload[0], "")
    try:
        return prefix + payload[1:].decode("utf-8")
    except Exception:
        return prefix + payload[1:].decode("latin1", errors="ignore")


def _extract_text_from_records(records: list[dict]) -> list[str]:
    """Extract all human-readable text strings from a list of NDEF records."""
    texts = []
    for rec in records:
        tnf = rec["tnf"]
        rtype = rec["type"]
        payload = rec["payload"]

        if tnf == _TNF_WELL_KNOWN:
            if rtype == b"T":
                t = _decode_well_known_text(payload)
                if t:
                    texts.append(t)
            elif rtype == b"U":
                u = _decode_well_known_uri(payload)
                if u:
                    texts.append(u)
        elif tnf in (_TNF_MIME, _TNF_ABSOLUTE_URI, _TNF_EXTERNAL):
            # Try UTF-8 decode for payloads that may be text/JSON
            try:
                t = payload.decode("utf-8").strip()
                if t:
                    texts.append(t)
            except Exception:
                pass

    return texts


def _get_ndef_mime_records(records: list[dict]) -> list[tuple[str, bytes]]:
    """Return (mime_type_str, payload_bytes) for all MIME (TNF=0x02) records."""
    results = []
    for rec in records:
        if rec["tnf"] == _TNF_MIME:
            try:
                mime = rec["type"].decode("ascii").lower().strip()
            except Exception:
                mime = rec["type"].decode("latin1", errors="ignore").lower().strip()
            results.append((mime, rec["payload"]))
    return results


# ---------------------------------------------------------------------------
# Minimal CBOR decoder (only what OpenPrintTag needs: map, text, uint, bytes)
# ---------------------------------------------------------------------------

def _cbor_decode(data: bytes, idx: int = 0) -> tuple:
    """Minimal CBOR decoder returning (value, next_idx).

    Supports: positive int (major 0), bytes (major 2), text (major 3),
    array (major 4), map (major 5), and simple values true/false/null.
    """
    if idx >= len(data):
        raise ValueError("CBOR: unexpected end of input")
    initial = data[idx]
    idx += 1
    major = (initial >> 5) & 0x07
    additional = initial & 0x1F

    def _read_uint(n):
        nonlocal idx
        v = int.from_bytes(data[idx:idx + n], "big")
        idx += n
        return v

    if additional <= 23:
        count = additional
    elif additional == 24:
        count = _read_uint(1)
    elif additional == 25:
        count = _read_uint(2)
    elif additional == 26:
        count = _read_uint(4)
    elif additional == 27:
        count = _read_uint(8)
    elif additional == 31:
        count = -1  # indefinite length
    else:
        raise ValueError(f"CBOR: unsupported additional={additional}")

    if major == 0:  # positive int
        return count, idx
    if major == 1:  # negative int
        return -(1 + count), idx
    if major == 2:  # bytes
        if count < 0:
            raise ValueError("CBOR: indefinite bytes not supported")
        result = data[idx:idx + count]
        return result, idx + count
    if major == 3:  # text
        if count < 0:
            raise ValueError("CBOR: indefinite text not supported")
        result = data[idx:idx + count].decode("utf-8", errors="replace")
        return result, idx + count
    if major == 4:  # array
        items = []
        if count >= 0:
            for _ in range(count):
                v, idx = _cbor_decode(data, idx)
                items.append(v)
        else:
            while idx < len(data) and data[idx] != 0xFF:
                v, idx = _cbor_decode(data, idx)
                items.append(v)
            idx += 1  # skip 0xFF break code
        return items, idx
    if major == 5:  # map
        result = {}
        if count >= 0:
            for _ in range(count):
                k, idx = _cbor_decode(data, idx)
                v, idx = _cbor_decode(data, idx)
                result[k] = v
        else:
            while idx < len(data) and data[idx] != 0xFF:
                k, idx = _cbor_decode(data, idx)
                v, idx = _cbor_decode(data, idx)
                result[k] = v
            idx += 1
        return result, idx
    if major == 7:  # float/simple
        if additional == 20:
            return False, idx
        if additional == 21:
            return True, idx
        if additional == 22:
            return None, idx
        if additional == 25:
            sign = -1.0 if (count & 0x8000) else 1.0
            exp = (count >> 10) & 0x1F
            frac = count & 0x03FF
            if exp == 0:
                return sign * (frac / 1024.0) * (2 ** -14), idx
            if exp == 0x1F:
                return sign * (float("inf") if frac == 0 else float("nan")), idx
            return sign * (1.0 + frac / 1024.0) * (2 ** (exp - 15)), idx
        if additional == 26:
            return struct.unpack(">f", count.to_bytes(4, "big"))[0], idx
        if additional == 27:
            return struct.unpack(">d", count.to_bytes(8, "big"))[0], idx
        raise ValueError(f"CBOR: unsupported simple/float additional={additional}")

    raise ValueError(f"CBOR: unsupported major={major}")


# ---------------------------------------------------------------------------
# Format parsers
# ---------------------------------------------------------------------------

# ---- ELEGOO ----------------------------------------------------------------

_ELEGOO_HEADER = 0x36
_ELEGOO_MFGR = b"\xEE\xEE\xEE\xEE"


def _try_elegoo(raw: bytes) -> Optional[dict]:
    """Parse ELEGOO EPC-256 binary tag format.

    Layout (user memory, starting at page 4):
        Byte 0:     Header 0x36
        Bytes 1-4:  Manufacturer code 0xEEEEEEEE
        Bytes 5-6:  Filament code uint16 BE
        Bytes 7-10: Material main (4 ASCII bytes)
        Bytes 11-14: Material subtype (4 ASCII bytes)
        Bytes 15-17: Color RGB888 (3 bytes)
        Bytes 18-19: Diameter uint16 BE (hundredths mm)
        Bytes 20-21: Weight uint16 BE (grams)
        Bytes 22-23: Production date YYMM uint16 BE
    """
    if len(raw) < 24:
        return None
    if raw[0] != _ELEGOO_HEADER or raw[1:5] != _ELEGOO_MFGR:
        return None

    mat_main = raw[7:11].decode("ascii", errors="ignore").strip("\x00 ")
    mat_sub = raw[11:15].decode("ascii", errors="ignore").strip("\x00 ")
    material = f"{mat_main}-{mat_sub}" if mat_sub else mat_main

    r, g, b = raw[15], raw[16], raw[17]
    color_hex = f"{r:02X}{g:02X}{b:02X}"

    diam_raw = struct.unpack_from(">H", raw, 18)[0]
    diameter_mm = round(diam_raw / 100.0, 2)

    weight_g = struct.unpack_from(">H", raw, 20)[0]

    return {
        "material": material,
        "brand": "ELEGOO",
        "color_hex": color_hex,
        "diameter_mm": diameter_mm,
        "weight_g": weight_g,
        "tag_format": "elegoo",
    }


# ---- Bambu Lab -------------------------------------------------------------

def _detect_bambu(raw: bytes) -> bool:
    """Heuristically detect a Bambu Lab MIFARE Classic 1K tag from a raw byte dump.

    Bambu tags use MIFARE Classic 1K with RSA-2048-signed, per-UID derived
    encryption keys.  A raw byte dump from an unauthenticated read cannot be
    decrypted here — the encrypted blocks look like random data.  Full sector
    data is only available after per-sector HKDF key derivation and MIFARE
    authentication (see ``_bambu_derive_keys`` and the module docstring).

    Detection is unreliable from raw bytes alone; we flag a candidate only when
    the raw dump is exactly a multiple of 64 bytes (MIFARE Classic sector size)
    with no readable NDEF and no known filament keywords.
    This is a best-effort heuristic only.
    """
    if len(raw) == 0:
        return False
    if _has_tigertag_magic(raw):
        return False
    # 64 bytes is one MIFARE Classic 1K sector (4 blocks × 16 bytes).
    # A full card dump is 1024 bytes (16 sectors × 4 blocks × 16 bytes).
    if len(raw) % 64 != 0 and len(raw) != 1024:
        return False
    # If we can find an NDEF TLV, it's not a Bambu tag.
    if _find_ndef_tlv(raw) is not None:
        return False
    # If the raw bytes contain readable ASCII filament keywords, not Bambu.
    try:
        text = raw.decode("utf-8", errors="ignore")
        for kw in ("PLA", "ABS", "PETG", "TPU", "spoolman_id", "openspool"):
            if kw in text:
                return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Bambu Lab HKDF sector-key derivation
# ---------------------------------------------------------------------------
# Adapted from MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp
# (GPLv3), NfcTagProcessor.kt lines 53–139, commit c8aa59e6d4c132f9e78bde24d791bbb330a12b7d:
#   https://github.com/MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp/blob/c8aa59e6d4c132f9e78bde24d791bbb330a12b7d/source/app/src/main/java/app/mrb/bambuspoolpal/nfc/NfcTagProcessor.kt
# No code was copied; the procedure was faithfully reimplemented in Python.
#
# The Android app uses BouncyCastle's HKDFBytesGenerator with:
#   HKDFParameters(uid, masterKey, context)
#   → HKDFParameters(IKM=uid, salt=masterKey, info=context)
#
# Key derivation overview:
#   IKM  (input key material) = tag UID bytes        (4 bytes, unique per tag)
#   Salt                       = _BAMBU_MASTER_KEY    (static 16-byte device key)
#   Info / context             = b"RFID-A\x00"        (7 bytes, incl. null)
#   Output length              = sector_count × 6 bytes (96 bytes for 16 sectors)
#
# pycryptodome HKDF signature:
#   HKDF(master, key_len, salt, hashmod, num_keys=1, context=b"")
#   where 'master' is the IKM (first positional argument).
#
#   Correct call: HKDF(uid_bytes, 6, _BAMBU_MASTER_KEY, SHA256, 16, context=...)
#                           ↑ IKM              ↑ salt
#
# Hardware requirements:
#   Authenticated MIFARE Classic reads require hardware that supports the
#   ISO/IEC 14443 Type A 3-pass authentication protocol (e.g. MFRC522,
#   PN532, ACR122U).  Cheap "pass-through" USB readers typically do NOT
#   support per-sector key authentication and will fail silently.
# ---------------------------------------------------------------------------

_BAMBU_MASTER_KEY = bytes([
    0x9a, 0x75, 0x9c, 0xf2, 0xc4, 0xf7, 0xca, 0xff,
    0x22, 0x2c, 0xb9, 0x76, 0x9b, 0x41, 0xbc, 0x96,
])


def _bambu_derive_keys(uid_bytes: bytes) -> list:
    """Derive the 16 MIFARE sector Key-A values for a Bambu Lab tag.

    Procedure adapted from MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp
    (GPLv3), NfcTagProcessor.kt lines 53–139, commit c8aa59e6d4c132f9e78bde24d791bbb330a12b7d.
    No code was copied; reimplemented from the published algorithm.

    Uses HKDF-SHA256 with the tag UID as the IKM and the static Bambu master
    key as the salt.  Returns a list of 16 × 6-byte keys (one per MIFARE
    Classic 1K sector, sectors 0-15).

    Parameters
    ----------
    uid_bytes : bytes
        Raw UID bytes read from the tag (typically 4 bytes for MIFARE Classic 1K).

    Returns
    -------
    list of 16 bytes objects, each exactly 6 bytes long.

    Raises
    ------
    ImportError
        If pycryptodome is not installed.  Install with: pip3 install pycryptodome

    Notes
    -----
    The Android reference (BouncyCastle HKDFParameters) uses:
      HKDFParameters(uid, masterKey, context)
      → IKM=uid, salt=masterKey, info=context

    pycryptodome's HKDF signature: HKDF(master, key_len, salt, hashmod, …)
    where 'master' is the IKM (first argument).

    Correct mapping:
      HKDF(master=uid_bytes, key_len=6, salt=_BAMBU_MASTER_KEY, …)

    WARNING: Do NOT swap uid_bytes and _BAMBU_MASTER_KEY.  The uid must be the
    IKM (first argument) and _BAMBU_MASTER_KEY must be the salt (third argument),
    matching the Android reference.  Swapping them produces completely wrong keys
    and silent authentication failure on every sector.
    """
    if not _PYCRYPTODOME_OK:
        raise ImportError(
            "pycryptodome required for Bambu tag key derivation. "
            "Install with: pip3 install pycryptodome"
        )
    # pycryptodome HKDF(master, key_len, salt, hashmod, num_keys, context):
    #   master  = uid_bytes          (IKM — tag UID, per Android HKDFParameters)
    #   key_len = 6                  (MIFARE Classic key width in bytes)
    #   salt    = _BAMBU_MASTER_KEY  (static Bambu device secret, used as salt)
    #   num_keys= 16                 (one key per sector; internally derives
    #                                 96 bytes and splits into 16 × 6-byte keys)
    #   context = b"RFID-A\x00"     (7-byte info/context string for Key A)
    raw = _HKDF(uid_bytes, 6, _BAMBU_MASTER_KEY, _SHA256, 16, context=b"RFID-A\x00")
    return list(raw)


def _bambu_derive_keys_b(uid_bytes: bytes) -> list:
    """Derive the 16 MIFARE sector Key-B values for a Bambu Lab tag.

    Identical to ``_bambu_derive_keys()`` except the HKDF context is
    ``b"RFID-B\\x00"`` instead of ``b"RFID-A\\x00"``.  Key B is used to
    authenticate sectors before writing data blocks.

    This convention is followed by community Bambu tag programming tools.
    On factory-programmed Bambu spools Key B is typically all-zeros (unused);
    on custom MIFARE Classic tags programmed with this toolchain the derived
    Key B values are written into the sector trailers at programming time so
    that subsequent writes always authenticate with the correct key.

    Parameters
    ----------
    uid_bytes : bytes
        Raw UID bytes from the tag (same bytes used for Key A derivation).
        The UID must be in the byte order returned by the RFID reader —
        do NOT reverse or hexify these bytes before passing them here.

    Returns
    -------
    list of 16 bytes objects, each exactly 6 bytes long.

    Raises
    ------
    ImportError
        If pycryptodome is not installed.  Install with: pip3 install pycryptodome
    """
    if not _PYCRYPTODOME_OK:
        raise ImportError(
            "pycryptodome required for Bambu tag key derivation. "
            "Install with: pip3 install pycryptodome"
        )
    # Same HKDF call as _bambu_derive_keys() but with context b"RFID-B\x00"
    # (Key B context) instead of b"RFID-A\x00" (Key A context).
    raw = _HKDF(uid_bytes, 6, _BAMBU_MASTER_KEY, _SHA256, 16, context=b"RFID-B\x00")
    return list(raw)


def _parse_bambu_blocks(blocks: dict) -> Optional[dict]:
    """Parse decrypted Bambu Lab tag blocks into a filament info dict.

    blocks: dict mapping absolute block index → 16-byte data bytes,
            produced by read_authenticated_blocks() after key auth.
    Returns filament info dict or None if the essential material block is missing.

    Block layout from Bambu-Research-Group/RFID-Tag-Guide/BambuLabRfid.md
    (all multi-byte numbers are Little Endian):

      Block 1  (sec 0, blk 1): Tray Info Index
                                  bytes  0-7:  Material Variant ID (ASCII)
                                  bytes  8-15: Material ID (ASCII, e.g. "GFA50")
      Block 2  (sec 0, blk 2): Filament Type — basic type string (e.g. "PLA")
      Block 4  (sec 1, blk 0): Detailed Filament Type (e.g. "PLA Basic")
      Block 5  (sec 1, blk 1): Color / Weight / Diameter
                                  bytes  0-3:  RGBA color
                                  bytes  4-5:  Spool weight uint16 (grams)
                                  bytes  8-11: Filament diameter float32 (mm)
      Block 6  (sec 1, blk 2): Temperatures and Drying Info
                                  bytes  0-1:  Drying temperature uint16 (°C)
                                  bytes  2-3:  Drying time uint16 (hours)
                                  bytes  4-5:  Bed temperature type uint16
                                  bytes  6-7:  Bed temperature uint16 (°C)
                                  bytes  8-9:  Max hotend temperature uint16 (°C)
                                  bytes 10-11: Min hotend temperature uint16 (°C)
      Block 9  (sec 2, blk 1): Tray UID — 16-byte ASCII hex string
      Block 12 (sec 3, blk 0): Production date — ASCII "yyyy_MM_dd_HH_mm"
      Block 14 (sec 3, blk 2): Filament length — uint16 at offset 4 (meters)
      Block 16 (sec 4, blk 0): Extra color info
                                  bytes  0-1:  Format ID uint16 (0x0002 = color present)
                                  bytes  2-3:  Color count uint16
                                  bytes  4-7:  Second color ABGR (note: reversed order)

    Blocks 3, 7, 11, 15, 19, … are MIFARE sector trailers (encryption keys)
    and are never present in the authenticated block dict.
    """
    def _read_str(blk_idx, offset=0, length=16):
        """Read a null-terminated ASCII string from a block."""
        b = blocks.get(blk_idx)
        if not b or len(b) < offset + length:
            return None
        try:
            return b[offset:offset + length].rstrip(b"\x00").decode("ascii", errors="ignore").strip() or None
        except Exception:
            return None

    def _read_u16(blk_idx, offset):
        """Read a uint16 LE from a block at the given byte offset."""
        b = blocks.get(blk_idx)
        if not b or len(b) < offset + 2:
            return None
        return struct.unpack_from("<H", b, offset)[0]

    def _read_f32(blk_idx, offset):
        """Read a float32 LE from a block at the given byte offset."""
        b = blocks.get(blk_idx)
        if not b or len(b) < offset + 4:
            return None
        return struct.unpack_from("<f", b, offset)[0]

    # --- Block 2: basic filament type (required) ---
    material = _read_str(2)
    if not material:
        return None

    # --- Block 1: tray info index ---
    # bytes 0-7 = material variant ID, bytes 8-15 = material ID
    material_variant_id = _read_str(1, offset=0, length=8)
    material_id = _read_str(1, offset=8, length=8)

    # --- Block 4: detailed filament type ---
    material_detail = _read_str(4)

    # --- Block 5: RGBA color, spool weight, filament diameter ---
    color_hex = None
    color_rgba = None
    weight_g = None
    diameter_mm = None
    b5 = blocks.get(5)
    if b5 and len(b5) >= 12:
        r, g, b_val, a = b5[0], b5[1], b5[2], b5[3]
        color_rgba = (r, g, b_val, a)
        color_hex = "%02X%02X%02X" % (r, g, b_val)  # 6-digit RGB; alpha kept in color_rgba

        raw_weight = _read_u16(5, 4)
        if raw_weight and raw_weight > 0:
            weight_g = int(raw_weight)

        raw_diam = _read_f32(5, 8)
        if raw_diam and 0.5 < raw_diam < 5.0:  # sanity: 0.5–5 mm
            diameter_mm = round(float(raw_diam), 2)

    # --- Block 6: temperatures and drying info ---
    drying_temp = None
    drying_time_h = None
    bed_temp = None
    max_temp = None
    min_temp = None
    b6 = blocks.get(6)
    if b6 and len(b6) >= 12:
        v_dry_temp = _read_u16(6, 0)
        v_dry_time = _read_u16(6, 2)
        # offset 4-5: bed temp type (ignored — types not publicly documented)
        v_bed_temp = _read_u16(6, 6)
        v_max_temp = _read_u16(6, 8)
        v_min_temp = _read_u16(6, 10)

        if v_dry_temp and 0 < v_dry_temp < 200:
            drying_temp = int(v_dry_temp)
        if v_dry_time and 0 < v_dry_time < 100:
            drying_time_h = int(v_dry_time)
        if v_bed_temp and 0 < v_bed_temp < 200:
            bed_temp = int(v_bed_temp)
        if v_max_temp and 0 < v_max_temp < 500:
            max_temp = int(v_max_temp)
        if v_min_temp and 0 < v_min_temp < 500:
            min_temp = int(v_min_temp)

    # --- Block 9: tray UID (16 raw bytes → 32-char uppercase hex string) ---
    # The tag stores the Tray UID as 16 raw binary bytes (not ASCII text).
    # When displayed it is shown as the 32-character hex representation.
    # An all-zero block means the UID slot has not been written; treat it
    # as absent so callers don't see a false "0000…" tray UID.
    tray_uid = None
    b9 = blocks.get(9)
    if b9 and len(b9) >= 16:
        uid_bytes = b9[:16]
        if any(byte != 0 for byte in uid_bytes):
            tray_uid = uid_bytes.hex().upper()

    # --- Block 12: production date "yyyy_MM_dd_HH_mm" ---
    production_date = _read_str(12)

    # --- Block 14: filament length in meters (uint16 at offset 4) ---
    filament_length_m = None
    v_len = _read_u16(14, 4)
    if v_len and v_len > 0:
        filament_length_m = int(v_len)

    # --- Block 16: extra color info (second color for multi-colour filaments) ---
    # Format ID 0x0002 signals that extra color data is present.
    # Second color is stored as ABGR (reversed), so we swap to RGBA for consistency.
    second_color_hex = None
    b16 = blocks.get(16)
    if b16 and len(b16) >= 8:
        fmt_id = struct.unpack_from("<H", b16, 0)[0]
        if fmt_id == 0x0002:
            # bytes 4-7: ABGR — index 0=A, 1=B, 2=G, 3=R
            a2, b2, g2, r2 = b16[4], b16[5], b16[6], b16[7]
            second_color_hex = "%02X%02X%02X" % (r2, g2, b2)

    info: dict = {
        "material": material,
        "brand": "Bambu Lab",
        "color_hex": color_hex,
        "color_rgba": color_rgba,
        "diameter_mm": diameter_mm if diameter_mm is not None else 1.75,
        "weight_g": weight_g,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "bed_temp": bed_temp,
        "drying_temp": drying_temp,
        "drying_time_h": drying_time_h,
        "spoolman_id": None,
        "writable": False,
        "tag_format": "bambu",
    }
    # Optional fields — only include when present so callers can check ``if key in info``
    if material_detail:
        info["material_detail"] = material_detail
    if material_id:
        info["material_id"] = material_id
    if material_variant_id:
        info["material_variant_id"] = material_variant_id
    if tray_uid:
        info["tray_uid"] = tray_uid
        info["spool_identity"] = "bambu_%s" % tray_uid
    if production_date:
        info["production_date"] = production_date
    if filament_length_m is not None:
        info["filament_length_m"] = filament_length_m
    if second_color_hex:
        info["second_color_hex"] = second_color_hex
    return info


_ANYCUBIC_MAGIC = b"\x7B\x00"


def _try_anycubic_ace(raw: bytes) -> Optional[dict]:
    """Parse Anycubic ACE binary tag format.

    Layout (user memory relative offsets):
        Bytes 0-1:   Magic 0x7B 0x00
        Bytes 4-19:  SKU (16-byte null-terminated ASCII)
        Bytes 24-39: Brand (16-byte null-terminated ASCII)
        Bytes 44-59: Material/type (16-byte null-terminated ASCII)
        Bytes 64-67: Color ABGR (A, B, G, R)
        Bytes 80-83: Extruder temp [min uint16 LE, max uint16 LE]
        Bytes 96-99: Bed temp [min uint16 LE, max uint16 LE]
        Bytes 100-103: Diameter uint16 LE (hundredths mm) + length uint16 LE
    """
    if len(raw) < 2:
        return None
    if raw[0:2] != _ANYCUBIC_MAGIC:
        return None

    def _str16(data, off):
        chunk = data[off:off + 16] if len(data) >= off + 16 else data[off:]
        return chunk.split(b"\x00")[0].decode("ascii", errors="ignore").strip()

    sku = _str16(raw, 4)
    brand = _str16(raw, 24) or "Anycubic"
    material = _str16(raw, 44)

    info: dict = {"tag_format": "anycubic_ace", "brand": brand}
    if material:
        info["material"] = material
    if sku:
        info["sku"] = sku

    if len(raw) >= 68:
        _a, b, g, r = raw[64], raw[65], raw[66], raw[67]  # ABGR: alpha, blue, green, red
        info["color_hex"] = f"{r:02X}{g:02X}{b:02X}"

    if len(raw) >= 84:
        min_temp = struct.unpack_from("<H", raw, 80)[0]
        max_temp = struct.unpack_from("<H", raw, 82)[0]
        if min_temp:
            info["min_temp"] = min_temp
        if max_temp:
            info["max_temp"] = max_temp

    if len(raw) >= 100:
        min_bed = struct.unpack_from("<H", raw, 96)[0]
        if min_bed:
            info["bed_temp"] = min_bed

    if len(raw) >= 104:
        diam_raw = struct.unpack_from("<H", raw, 100)[0]
        if diam_raw:
            info["diameter_mm"] = round(diam_raw / 100.0, 2)

    return info if info.get("material") else None


# ---- TigerTag --------------------------------------------------------------

_TIGERTAG_MAGIC = {
    0x5BF59264: "TigerTag",
    0xBC0FCB97: "TigerTag+",
    0x6C41A2E1: "TigerTag Init",
    # Earlier public database / app variants seen in the reference projects.
    0x5BF04674: "TigerTag",
    0xBC0A5927: "TigerTag+",
    0x6C2B2DF1: "TigerTag Init",
}

_TIGERTAG_TYPE_FILAMENT = 0x8E
_TIGERTAG_TYPE_RESIN = 0xAD

_TIGERTAG_MATERIALS: dict[int, str] = {
    425: "ABS-CF", 735: "ABS-AF", 1173: "PA6-GF", 2053: "PA12-GF",
    3368: "PC-ABS", 3481: "PCTG-GF", 4587: "PC-PBT", 5733: "TPU-AMS",
    7649: "PETG-HS", 7951: "PETG-rCF", 8345: "PLA+ Silk",
    9456: "PLA Marble", 9483: "PVA", 9691: "EVA", 10187: "PHA",
    10272: "PSU", 10478: "Cast Fil", 10602: "PLA Silk",
    10738: "PC-PTFE", 11053: "PET-CF", 11506: "PLA-LW",
    12264: "PA6-CF", 12844: "ASA", 13850: "PPA", 15041: "PCTG",
    18130: "PS", 18703: "PETP", 18775: "PE-CF", 18922: "PLA-ESD",
    20073: "PVC", 20562: "ABS", 24115: "SEBS", 24116: "TPC",
    24270: "PPS-CF", 24629: "PLA-HS", 26029: "HIPS",
    27268: "PCTPE", 27635: "PE", 27676: "ASA-CF", 28110: "SBC",
    29815: "PEEK", 30458: "PC", 30594: "PA-GF", 30884: "PP",
    31011: "ASA-LW", 33958: "TPE", 34049: "BVOH", 34409: "TPS",
    35100: "ASA-GF", 38219: "PLA", 38256: "PETG", 39667: "PA12-CF",
    39944: "PA-CF", 42623: "PMMA", 42962: "PP-GF", 43518: "TPU",
    45962: "PVB", 46154: "PPS", 46276: "PPA-GF", 46591: "PLA+",
    47651: "PC-PBT-CF", 48001: "PLA Wood", 48047: "TPU-HS",
    48310: "PLA-CF", 48815: "PAHT-CF", 49074: "ABS-GF",
    49152: "PPSU", 49804: "ASA-AF", 50206: "POM", 50497: "PP-CF",
    51007: "Biopolymer", 51861: "PETG-ESD", 52077: "PET",
    53890: "PCTG-CF", 53970: "PEKK", 54568: "ASA+",
    55279: "PBT", 55418: "PETG-CF", 55796: "PA12", 56527: "PEI",
    56666: "PA6", 57469: "PETG-HF", 58142: "TPU-GF",
    58498: "PEBA", 59328: "PA", 61048: "PVDF",
    61563: "PC-PBT-GF", 63946: "TPI",
}

_TIGERTAG_BRANDS: dict[int, str] = {
    1: "Atome3D", 1068: "SainSmart", 1120: "Proto-Pasta",
    1421: "3DJake", 2517: "Smart Materials 3D", 2833: "Xstrand",
    3132: "Hatchbox", 4011: "QIDI Tech", 4048: "Owa",
    4344: "MatterHackers", 4356: "Landu", 7674: "Extrudr",
    7812: "Jayo", 7980: "Fillamentum", 8182: "Fiberlogy",
    8303: "GST3D", 8384: "Taulman3D", 8586: "NinjaTek",
    8675: "SOVB 3D", 8756: "BlueCast", 8990: "Ice Filaments",
    9192: "3D Solutech", 9394: "Gizmo Dorks", 9596: "Ziro",
    9798: "AMOLEN", 11429: "3D4Makers", 11501: "InnovateFil",
    12345: "MakerBot", 12498: "Forshape", 14982: "3D-Fuel",
    15899: "Kimya", 15962: "Anycubic", 18629: "PrintoMax 3D",
    19265: "CC3D", 19961: "Rosa3D", 20523: "Raise3D",
    20851: "Tronxy", 22652: "Spectrum", 23181: "ArianePlast",
    23456: "Monoprice", 26595: "Sovol", 26956: "Creality",
    28055: "TAGin3D", 28136: "Polar Filament", 28940: "Eryone",
    29045: "Yousu", 29302: "IIIDMAX", 32587: "Amazon",
    33788: "Verbatim", 34567: "Push Plastic", 35123: "Bambu Lab",
    36702: "Tianse", 37434: "Winkle", 39382: "Longer",
    39652: "3DXTech", 41932: "Jamg He", 45670: "Panchroma",
    45678: "Atomic Filament", 46010: "AceAddity", 46203: "Overture",
    46392: "Prusament", 47560: "Wanhao", 47930: "eSun",
    48804: "R3D", 49784: "GIANTARM", 50311: "G3D Pro",
    50604: "Polymaker", 51443: "BASF", 51857: "Sunlu",
    52222: "ColorFabb", 52467: "Geeetech", 52757: "Yumi",
    53043: "FormFutura", 53640: "Magigoo", 53856: "Lattice Medical",
    54112: "Kexcelled", 55763: "Nanovia", 55869: "Biqu",
    56780: "Fiberon", 56789: "Coex 3D", 57209: "FrancoFil",
    57632: "ELEGOO", 58231: "IC3D", 58410: "AzureFilm",
    60882: "Recreus", 63340: "Flashforge", 65535: "Generic",
}

_TIGERTAG_ASPECTS: dict[int, str] = {
    0: "-", 21: "Clear", 24: "Tricolor", 64: "Glitter",
    67: "Translucent", 91: "Glow in the Dark", 92: "Silk",
    97: "Lithophane", 104: "Basic", 123: "Wood", 126: "Pearl",
    129: "Gloss", 134: "Satin", 145: "Rainbow", 168: "Thermoreactif",
    173: "Stone", 216: "Neon", 220: "Pastel", 226: "Metal",
    232: "Marble", 238: "Carbon", 247: "Matt", 252: "Bicolor",
    255: "None",
}

_TIGERTAG_UNITS: dict[int, str] = {
    10: "mg", 21: "g", 35: "kg", 48: "ml", 62: "cl", 79: "L",
    95: "m3", 112: "mm", 130: "cm", 149: "m", 170: "m2",
}


def _has_tigertag_magic(raw: bytes) -> bool:
    if len(raw) < 4:
        return False
    return struct.unpack_from(">I", raw, 0)[0] in _TIGERTAG_MAGIC


def _try_tigertag(raw: bytes) -> Optional[dict]:
    """Parse TigerTag core fields from raw Type-2 user memory.

    TigerTag is a binary NTAG payload beginning at page 0x04.  The first 32
    bytes contain the fields needed for spool identification and Spoolman
    creation; later optional data such as message, remaining amount, cloud
    product details, and signature verification is intentionally ignored here.
    """
    if len(raw) < 32 or not _has_tigertag_magic(raw):
        return None

    magic = struct.unpack_from(">I", raw, 0)[0]
    version = _TIGERTAG_MAGIC.get(magic, "TigerTag")
    if "Init" in version:
        return None

    product_id = struct.unpack_from(">I", raw, 4)[0]
    material_id = struct.unpack_from(">H", raw, 8)[0]
    aspect1_id = raw[10]
    aspect2_id = raw[11]
    type_id = raw[12]
    diameter_id = raw[13]
    brand_id = struct.unpack_from(">H", raw, 14)[0]

    info: dict = {
        "tag_format": "tigertag",
        "tigertag_version": version,
        "tigertag_magic": "0x%08X" % magic,
        "tigertag_product_id": product_id,
        "tigertag_material_id": material_id,
        "tigertag_brand_id": brand_id,
        "tigertag_type_id": type_id,
        "tigertag_diameter_id": diameter_id,
    }

    material = _TIGERTAG_MATERIALS.get(material_id)
    if material and material != "None":
        info["material"] = material

    brand = _TIGERTAG_BRANDS.get(brand_id)
    if brand and brand != "Generic":
        info["brand"] = brand

    aspect1 = _TIGERTAG_ASPECTS.get(aspect1_id)
    aspect2 = _TIGERTAG_ASPECTS.get(aspect2_id)
    if aspect1 and aspect1 not in ("-", "None"):
        info["tigertag_aspect"] = aspect1
        if material and material != "None":
            info["material_detail"] = "%s_%s" % (material, aspect1)
    if aspect2 and aspect2 not in ("-", "None"):
        info["tigertag_aspect_2"] = aspect2

    if type_id == _TIGERTAG_TYPE_FILAMENT:
        info["tigertag_type"] = "Filament"
    elif type_id == _TIGERTAG_TYPE_RESIN:
        info["tigertag_type"] = "Resin"

    if diameter_id == 56:
        info["diameter_mm"] = 1.75
    elif diameter_id == 221:
        info["diameter_mm"] = 2.85

    r, g, b, a = raw[16], raw[17], raw[18], raw[19]
    if r or g or b:
        info["color_hex"] = "%02X%02X%02X" % (r, g, b)
        info["tigertag_color_alpha"] = a

    measure = (raw[20] << 16) | (raw[21] << 8) | raw[22]
    unit_id = raw[23]
    unit = _TIGERTAG_UNITS.get(unit_id)
    info["tigertag_unit_id"] = unit_id
    if unit:
        info["tigertag_unit"] = unit
    if measure:
        info["tigertag_measure"] = measure
        if unit_id == 21:
            info["weight_g"] = measure

    min_temp = struct.unpack_from(">H", raw, 24)[0]
    max_temp = struct.unpack_from(">H", raw, 26)[0]
    if min_temp:
        info["min_temp"] = min_temp
    if max_temp:
        info["max_temp"] = max_temp

    dry_temp, dry_time_h = raw[28], raw[29]
    bed_min, bed_max = raw[30], raw[31]
    if dry_temp:
        info["drying_temp"] = dry_temp
    if dry_time_h:
        info["drying_time_h"] = dry_time_h
    if bed_min:
        info["bed_temp"] = bed_min
        info["bed_temp_min"] = bed_min
    if bed_max:
        info["bed_temp_max"] = bed_max

    # Twin Tag ID & Timestamp (page 0x0C, offset +32, u32 BE): seconds since
    # 2000-01-01 GMT. Written identically to both chips when a spool's left
    # and right tags are programmed together, so it doubles as a same-spool
    # pairing key independent of each chip's own (different) hardware UID.
    if len(raw) >= 36:
        twin_tag_id = struct.unpack_from(">I", raw, 32)[0]
        if twin_tag_id:  # 0 = unwritten
            info["tigertag_twin_tag_id"] = twin_tag_id
            # Same role as Bambu's "bambu_%s" % tray_uid: a same-spool
            # identity that survives the two physical tags having different
            # chip UIDs, feeding the existing spool_identity-based
            # left-neighbor interference check (scan_jog.py) with no
            # TigerTag-specific code needed there.
            info["spool_identity"] = "tigertag_%d" % twin_tag_id

    if product_id == 0xFFFFFFFF:
        info["tigertag_product_mode"] = "maker"
    else:
        info["tigertag_product_mode"] = "cloud"

    return info


# ---- Creality CFS / K1 / K2 AES tag (authenticated) ------------------------
#
# Confirmed encryption scheme (supersedes the unconfirmed hex-ASCII guess in
# _try_creality_cfs() below): Creality's own tag encoder derives a per-UID
# MIFARE Classic Key B and stores an AES-128-ECB-encrypted 48-byte ASCII
# payload in sector 1 (blocks 4-6). Two static keys are involved:
#
#   AES_KEY_GEN    -- AES-128-ECB-encrypt(uid repeated to 16 bytes)[:6] gives
#                     the sector 1 Key B, matching Creality's published Key-B
#                     write commands ("hf mf wrbl --blk 4 -b -k <key>").
#   AES_KEY_CIPHER -- AES-128-ECB over the concatenated plaintext blocks 4-6
#                     (48 bytes of the raw ASCII payload, not hex-encoded)
#                     produces the ciphertext stored on the tag; decrypting
#                     with the same key/mode recovers the ASCII payload.
#
# Both keys were community-sourced from a Creality RFID encryption helper
# script mirroring the JavaScript implementation used by Creality's tag
# writer tooling. No code was copied; reimplemented in Python from the two
# key constants and the encrypt/decrypt procedure it documents.
_CREALITY_AES_KEY_GEN = bytes([
    0x71, 0x33, 0x62, 0x75, 0x5E, 0x74, 0x31, 0x6E,
    0x71, 0x66, 0x5A, 0x28, 0x70, 0x66, 0x24, 0x31,
])  # "q3bu^t1nqfZ(pf$1"

_CREALITY_AES_KEY_CIPHER = bytes([
    0x48, 0x40, 0x43, 0x46, 0x6B, 0x52, 0x6E, 0x7A,
    0x40, 0x4B, 0x41, 0x74, 0x42, 0x4A, 0x70, 0x32,
])  # "H@CFkRnz@KAtBJp2"


def _creality_derive_key_b(uid_bytes: bytes) -> bytes:
    """Derive the MIFARE Classic sector-1 Key B for a Creality CFS/K1/K2 tag.

    Key = AES-128-ECB(_CREALITY_AES_KEY_GEN, uid repeated to 16 bytes)[:6].
    Matches Creality's own tag-writer tooling, which concatenates the UID
    hex string with itself until at least 16 bytes are available and
    truncates to exactly 16 bytes before encrypting.
    """
    if not _PYCRYPTODOME_OK:
        raise ImportError(
            "pycryptodome required for Creality tag key derivation. "
            "Install with: pip3 install pycryptodome"
        )
    if len(uid_bytes) not in (4, 7):
        raise ValueError(
            "Creality key derivation expects a 4 or 7 byte UID, got %d"
            % len(uid_bytes))
    uid_data = (bytes(uid_bytes) * 4)[:16]
    cipher = _AES.new(_CREALITY_AES_KEY_GEN, _AES.MODE_ECB)
    return cipher.encrypt(uid_data)[:6]


def _creality_decrypt_tag_data(block4: bytes, block5: bytes,
                                block6: bytes) -> bytes:
    """Decrypt sector-1 blocks 4-6 into the 48-byte ASCII tag-data payload."""
    cipher = _AES.new(_CREALITY_AES_KEY_CIPHER, _AES.MODE_ECB)
    return cipher.decrypt(bytes(block4)[:16] + bytes(block5)[:16] + bytes(block6)[:16])


# Material code -> name, from Creality's tag-writer material table.
_CREALITY_MATERIAL_MAP: dict[str, str] = {
    "10001": "HP-TPU", "11001": "CR-Nylon", "13001": "CR-PLACarbon",
    "14001": "CR-PLAMatte", "15001": "CR-PLAFluo", "16001": "CR-TPU",
    "17001": "CR-Wood", "18001": "HPUltraPLA", "19001": "HP-ASA",
    "07001": "CR-ABS", "06001": "CR-PETG", "04001": "CR-PLA",
    "05001": "CR-Silk", "09001": "EN-PLA+", "09002": "ENDERFASTPLA",
    "08001": "Ender-PLA", "00004": "GenericABS", "00007": "GenericASA",
    "00010": "GenericBVOH", "00012": "GenericHIPS", "00008": "GenericPA",
    "00009": "GenericPA-CF", "00015": "GenericPA6-CF",
    "00016": "GenericPAHT-CF", "00021": "GenericPC", "00020": "GenericPET",
    "00013": "GenericPET-CF", "00003": "GenericPETG",
    "00014": "GenericPETG-CF", "00001": "GenericPLA",
    "00006": "GenericPLA-CF", "00002": "GenericPLA-Silk", "00019": "GenericPP",
    "00017": "GenericPPS", "00018": "GenericPPS-CF", "00011": "GenericPVA",
    "00005": "GenericTPU", "03001": "HyperABS", "06002": "HyperPETG",
    "01001": "HyperPLA", "02001": "HyperPLA-CF",
}

# Length code -> spool weight in grams, from Creality's tag-writer table.
_CREALITY_LENGTH_TO_WEIGHT_G: dict[str, int] = {"0330": 1000, "0165": 500}

# Vendor ID -> name, from DnG-Crafts/K2-RFID's Creality tag examples/writers.
_CREALITY_VENDOR_MAP: dict[str, str] = {
    "0276": "Creality",
}


def _creality_ascii_preview(data: bytes, limit: int = 64) -> str:
    """Return a single-line printable preview for Creality debug traces."""
    preview = ''.join(
        chr(b) if 32 <= b <= 126 else '.'
        for b in bytes(data)[:limit])
    if len(data) > limit:
        preview += "..."
    return preview


def _creality_spool_identity(seed: str) -> str:
    """Return a stable decimal Creality spool identity from parsed tag fields."""
    digest = hashlib.sha1(seed.encode("ascii")).digest()
    # 64 bits is compact for logs/config while keeping collision risk tiny for
    # this local same-spool interference check.
    return str(int.from_bytes(digest[:8], "big"))


def _try_creality_tag(blocks: dict, uid_hex: Optional[str] = None,
                      trace=None) -> Optional[dict]:
    """Parse a Creality CFS/K1/K2 tag from authenticated sector-1 blocks.

    ``blocks`` must contain absolute blocks 4, 5 and 6 (sector 1), read after
    authenticating with the Key B from ``_creality_derive_key_b()`` — Bambu's
    HKDF Key A and the MIFARE factory default key both fail on genuine
    Creality tags, which is why this only runs as a third fallback in
    tag_handler.read_current_tag().

    Decrypted payload layout, aligned to DnG-Crafts/K2-RFID:
      date(5, MDDYY) + vendor_id(4) + batch(2) + filament_id(6) +
      color(7, "0RRGGBB") + length(4) + serial(6) + reserve/trailing data.

    DnG's writers build filament_id as a 1-character printer/type prefix plus
    the 5-character material code used by Creality's material database.
    """
    trace = _make_trace(trace)
    if not _PYCRYPTODOME_OK:
        trace("debug", "Creality AES: pycryptodome unavailable")
        return None
    b4, b5, b6 = blocks.get(4), blocks.get(5), blocks.get(6)
    if not b4 or not b5 or not b6:
        trace("debug",
              "Creality AES: missing sector-1 blocks have4=%s have5=%s have6=%s",
              bool(b4), bool(b5), bool(b6))
        return None
    raw_payload = bytes(b4)[:16] + bytes(b5)[:16] + bytes(b6)[:16]
    trace("debug", "Creality AES: encrypted blocks 4-6 hex=%s",
          raw_payload.hex().upper())
    try:
        decrypted = _creality_decrypt_tag_data(b4, b5, b6)
    except Exception as exc:
        trace("debug", "Creality AES: decrypt failed: %s", exc)
        return None
    trace("debug", "Creality AES: decrypted payload hex=%s",
          decrypted.hex().upper())
    trace("debug", "Creality AES: decrypted payload ascii=%r",
          _creality_ascii_preview(decrypted))
    structured_payload = decrypted[:40]
    trailing_payload = decrypted[40:]
    if trailing_payload:
        trace("debug", "Creality AES: trailing payload hex=%s",
              trailing_payload.hex().upper())
    try:
        ascii_data = structured_payload.decode("ascii")
    except Exception as exc:
        trace("debug", "Creality AES: structured payload is not ASCII: %s",
              exc)
        return None
    if len(ascii_data) < 40:
        trace("debug", "Creality AES: payload too short len=%d", len(ascii_data))
        return None
    if not ascii_data.isprintable():
        trace("debug", "Creality AES: payload contains non-printable chars")
        return None

    date_code     = ascii_data[0:5]
    vendor_id     = ascii_data[5:9]
    batch         = ascii_data[9:11]
    filament_id   = ascii_data[11:17]
    material_code = filament_id[1:]
    color         = ascii_data[17:24]
    length        = ascii_data[24:28]
    serial        = ascii_data[28:34]
    reserve       = ascii_data[34:40]
    trace(
        "debug",
        "Creality AES: parsed fields date_code=%r vendor_id=%r batch=%r "
        "filament_id=%r material_code=%r color=%r length=%r serial=%r "
        "reserve=%r",
        date_code, vendor_id, batch, filament_id, material_code, color,
        length, serial, reserve)

    # Sanity check: material/color fields must look like the encoder's own
    # format, otherwise this is the wrong key/tag rather than valid data.
    if not filament_id.isdigit():
        trace("debug", "Creality AES: reject filament_id not numeric: %r",
              filament_id)
        return None
    if not material_code.isdigit():
        trace("debug", "Creality AES: reject material_code not numeric: %r",
              material_code)
        return None
    if not re.fullmatch(r"[0-9A-Fa-f]{7}", color):
        trace("debug", "Creality AES: reject color not 7 hex chars: %r",
              color)
        return None

    vendor_name = _CREALITY_VENDOR_MAP.get(vendor_id)
    if vendor_name:
        trace("debug", "Creality AES: vendor_id=%s resolved to %s",
              vendor_id, vendor_name)
    else:
        trace("debug", "Creality AES: vendor_id=%s unknown; using Creality brand fallback",
              vendor_id)
    identity_seed = ":".join((
        vendor_id, date_code, batch, filament_id, color, length, serial))
    identity_numeric = _creality_spool_identity(identity_seed)
    trace("debug",
          "Creality AES: identity_seed=%r identity_numeric=%s uid=%s",
          identity_seed, identity_numeric, uid_hex or "unknown")

    info: dict = {
        "brand": vendor_name or "Creality",
        "tag_format": "creality",
        "creality_batch": batch,
        "creality_date_code": date_code,
        "creality_vendor_id": vendor_id,
        "creality_supplier": vendor_id,
        "creality_filament_id": filament_id,
        "material_code": material_code,
        "creality_serial": serial,
        "creality_identity_seed": identity_seed,
        "creality_identity_numeric": identity_numeric,
        "spool_identity": "creality_%s" % identity_numeric,
    }
    if vendor_name:
        info["creality_vendor"] = vendor_name
    material = _CREALITY_MATERIAL_MAP.get(
        material_code, "Unknown (%s)" % material_code)
    info["material"] = material
    trace("debug", "Creality AES: material_code=%s resolved to %s",
          material_code, material)

    color_hex = color[1:]  # leading nibble unused; remaining 6 = RRGGBB
    if re.fullmatch(r"[0-9A-Fa-f]{6}", color_hex):
        info["color_hex"] = color_hex.upper()
        trace("debug", "Creality AES: color=%r resolved to #%s",
              color, info["color_hex"])
    else:
        trace("debug", "Creality AES: color suffix not valid RGB: %r",
              color_hex)

    weight_g = _CREALITY_LENGTH_TO_WEIGHT_G.get(length)
    if weight_g:
        info["weight_g"] = weight_g
        trace("debug", "Creality AES: length=%s resolved to weight_g=%d",
              length, weight_g)
    else:
        trace("debug", "Creality AES: length=%s has no known weight mapping",
              length)

    if len(date_code) == 5 and date_code.isdigit():
        month = int(date_code[0])
        day = int(date_code[1:3])
        year = int(date_code[3:5])
        if 1 <= month <= 9 and 1 <= day <= 31:
            info["creality_production_date"] = "20%02d-%02d-%02d" % (
                year, month, day)
            trace("debug", "Creality AES: date_code=%s resolved to %s",
                  date_code, info["creality_production_date"])
        else:
            trace("debug", "Creality AES: date_code=%s outside expected MDDYY range",
                  date_code)
    else:
        trace("debug", "Creality AES: date_code=%r not numeric MDDYY",
              date_code)

    trace("debug", "Creality AES: parse success")
    return info


# ---- Creality CFS (legacy hex-ASCII heuristic, unconfirmed) ----------------

# Known Creality filament ID → material name (partial list from DnG-Crafts/K2-RFID)
_CREALITY_FILAMENT_IDS: dict[str, str] = {
    "000001": "PLA", "000002": "PETG", "000003": "ABS", "000004": "TPU",
    "000005": "PLA-CF", "000006": "PETG-CF", "000007": "ASA", "000008": "PA",
    "000009": "PA-CF", "000010": "PLA Silk", "000011": "PLA Matte",
    "000012": "PLA Basic", "000013": "PLA+", "000100": "PLA",
}


def _try_creality_cfs(raw: bytes) -> Optional[dict]:
    """Parse Creality CFS / K1 / K2 hex-encoded ASCII tag format.

    Sector 1 Block 0 (byte offset 64) contains a 40-character ASCII hex string
    (representing 20 bytes of data).

    Note: MIFARE Classic 1K requires sector-key authentication; this parser
    only runs when the data is present in the raw dump.
    """
    if len(raw) < 104:
        return None
    chunk = raw[64:104]
    try:
        hex_str = chunk.decode("ascii")
    except Exception:
        return None
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", hex_str):
        return None

    # Color nibbles 16-22 (0-indexed): first nibble ignored, last 6 = RRGGBB
    color_nibbles = hex_str[16:23]
    color_hex = color_nibbles[1:]  # Drop first nibble

    # Filament ID nibbles 10-15
    filament_id = hex_str[10:16]
    material = _CREALITY_FILAMENT_IDS.get(filament_id, "")

    # If no known material, try to derive from the nibbles
    if not material:
        material = "Unknown"

    info: dict = {
        "material": material,
        "brand": "Creality",
        "tag_format": "creality_cfs",
    }
    if re.fullmatch(r"[0-9A-Fa-f]{6}", color_hex):
        info["color_hex"] = color_hex.upper()

    return info


# ---- QIDI Box --------------------------------------------------------------

_QIDI_MATERIALS: dict[int, str] = {
    1: "PLA", 2: "PLA Matte", 3: "PLA Metal", 4: "PLA Silk", 5: "PLA-CF",
    6: "PLA-Wood", 7: "PLA Basic", 8: "PLA Matte Basic",
    11: "ABS", 12: "ABS-GF", 13: "ABS-Metal", 14: "ABS-Odorless",
    18: "ASA", 19: "ASA-AERO", 24: "UltraPA",
    25: "PA-CF", 26: "UltraPA-CF25", 27: "PA12-CF", 30: "PAHT-CF",
    31: "PAHT-GF", 32: "Support For PAHT", 33: "Support For PET/PA",
    34: "PC/ABS-FR", 37: "PET-CF", 38: "PET-GF", 39: "PETG Basic",
    40: "PETG Tough", 41: "PETG Rapido", 42: "PETG-CF",
    43: "PETG-GF", 44: "PPS-CF", 45: "PETG Translucent", 47: "PVA",
    49: "TPU-Aero", 50: "TPU",
}

_QIDI_COLORS: dict[int, str] = {
    1: "FAFAFA", 2: "060606", 3: "D9E3ED", 4: "5CF30F", 5: "63E492",
    6: "2850FF", 7: "FE98FE", 8: "DFD628", 9: "228332", 10: "99DEFF",
    11: "1714B0", 12: "CEC0FE", 13: "CADE4B", 14: "1353AB", 15: "5EA9FD",
    16: "A878FF", 17: "FE717A", 18: "FF362D", 19: "E2DFCD", 20: "898F9B",
    21: "6E3812", 22: "CAC59F", 23: "F28636", 24: "B87F2B",
}


def _try_qidi_box(raw: bytes) -> Optional[dict]:
    """Parse QIDI Box MIFARE Classic 1K binary tag format.

    Sector 1 Block 0 (byte offset 64):
        Byte 0: Material code (1-50)
        Byte 1: Color code (1-24)
        Byte 2: Manufacturer code (default 1=QIDI)

    Note: MIFARE Classic 1K requires sector-key auth; parser runs only if data is available.
    """
    if len(raw) < 67:
        return None
    mat_code = raw[64]
    col_code = raw[65]
    mfg_code = raw[66]

    if mat_code < 1 or mat_code > 50:
        return None
    if col_code < 1 or col_code > 24:
        return None

    # Extra sanity: bytes should not look like ASCII hex (which would indicate Creality)
    try:
        chunk = raw[64:67].decode("ascii")
        if re.fullmatch(r"[0-9A-Fa-f]{3}", chunk):
            return None
    except Exception:
        pass

    material = _QIDI_MATERIALS.get(mat_code, f"Unknown({mat_code})")
    color_hex = _QIDI_COLORS.get(col_code, "")
    if mfg_code == 1:
        brand = "QIDI"
    elif mfg_code == 0:
        brand = "Generic"
    else:
        brand = f"Unknown({mfg_code})"

    return {
        "material": material,
        "brand": brand,
        "color_hex": color_hex,
        "material_code": mat_code,
        "color_code": col_code,
        "manufacturer_code": mfg_code,
        "diameter_mm": 1.75,
        "tag_format": "qidi",
    }


# ---- OpenTag3D -------------------------------------------------------------

_OPENTAG3D_MIME = "application/vnd.opentag3d"


def _try_opentag3d(mime_type: str, payload: bytes) -> Optional[dict]:
    """Parse OpenTag3D binary NDEF payload.

    The spec stores fields as big-endian unsigned integers; temperatures
    are stored as Celsius * 5 (i.e. divide by 5 to get °C).

    Field layout (best-effort — spec v1 from queengooborg/OpenTag3D):
        Byte 0:      Version
        Byte 1:      Flags
        Bytes 2-3:   Manufacturer name length + string
        ...          Material name string
        ...          Color RGB (3 bytes)
        ...          Diameter uint16 BE (hundredths mm)
        ...          Weight uint16 BE (grams)
        ...          Min temp uint16 BE (Celsius*5)
        ...          Max temp uint16 BE (Celsius*5)
        ...          Bed temp uint16 BE (Celsius*5)
        ...          Drying temp uint16 BE (Celsius*5)
        ...          Drying time uint8 (hours)
    """
    if mime_type.lower() != _OPENTAG3D_MIME:
        return None
    if len(payload) < 4:
        return None

    idx = 0
    try:
        version = payload[idx]
        idx += 1
        _flags = payload[idx]
        idx += 1

        def _read_str():
            nonlocal idx
            if idx >= len(payload):
                return ""
            slen = payload[idx]
            idx += 1
            s = payload[idx:idx + slen].decode("utf-8", errors="ignore")
            idx += slen
            return s

        def _read_u16():
            nonlocal idx
            if idx + 1 >= len(payload):
                return 0
            val = struct.unpack_from(">H", payload, idx)[0]
            idx += 2
            return val

        def _read_u8():
            nonlocal idx
            if idx >= len(payload):
                return 0
            val = payload[idx]
            idx += 1
            return val

        brand = _read_str()
        material = _read_str()

        if idx + 3 > len(payload):
            raise ValueError("too short for color")
        r, g, b = payload[idx], payload[idx + 1], payload[idx + 2]
        idx += 3
        color_hex = f"{r:02X}{g:02X}{b:02X}"

        diameter_raw = _read_u16()
        weight_g = _read_u16()
        min_temp_raw = _read_u16()
        max_temp_raw = _read_u16()
        bed_temp_raw = _read_u16()
        drying_temp_raw = _read_u16()
        drying_time_h = _read_u8()

        info: dict = {"tag_format": "opentag3d", "version": version}
        if material:
            info["material"] = material
        if brand:
            info["brand"] = brand
        if color_hex != "000000":
            info["color_hex"] = color_hex
        if diameter_raw:
            info["diameter_mm"] = round(diameter_raw / 100.0, 2)
        if weight_g:
            info["weight_g"] = weight_g
        if min_temp_raw:
            info["min_temp"] = min_temp_raw // 5
        if max_temp_raw:
            info["max_temp"] = max_temp_raw // 5
        if bed_temp_raw:
            info["bed_temp"] = bed_temp_raw // 5
        if drying_temp_raw:
            info["drying_temp"] = drying_temp_raw // 5
        if drying_time_h:
            info["drying_time_h"] = drying_time_h
        return info if info.get("material") else None

    except Exception as exc:
        _log.debug("opentag3d parse error: %s", exc)
        return None


# ---- OpenSpool -------------------------------------------------------------

_SMART_JSON_QUOTES = {
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
}


def _loads_json_text(text: str, trace=None) -> tuple[Optional[dict], bool]:
    """Load JSON text, accepting common copy/paste punctuation from UIs."""
    trace = _make_trace(trace)
    raw = text.strip()
    try:
        data = json.loads(raw)
        return (data, False) if isinstance(data, dict) else (None, False)
    except Exception:
        pass

    has_smart_quotes = any(ch in raw for ch in _SMART_JSON_QUOTES)
    has_field_semicolon = re.search(r';\s*"\w+"\s*:', raw) is not None
    if not has_smart_quotes and not has_field_semicolon:
        return None, False

    normalized = raw
    for bad, good in _SMART_JSON_QUOTES.items():
        normalized = normalized.replace(bad, good)
    normalized = re.sub(r';\s*("\w+"\s*:)', r',\1', normalized)
    try:
        data = json.loads(normalized)
        if isinstance(data, dict):
            trace("info", "normalized nonstandard JSON punctuation")
            return data, True
    except Exception as exc:
        _log.debug("rfid: JSON parse failed after quote normalization: %s", exc)
    return None, True


def _try_openspool(text: str, trace=None) -> Optional[dict]:
    """Parse an OpenSpool JSON payload.

    Detection: JSON dict with "protocol": "openspool".
    """
    trace = _make_trace(trace)
    data, normalized_quotes = _loads_json_text(text, trace)
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("protocol", "")).lower() != "openspool":
        return None

    material = str(data.get("type", "") or data.get("material", "")).strip()
    if not material:
        return None

    info: dict = {
        "material": material,
        "brand": str(data.get("brand", "Generic")).strip(),
        "tag_format": "openspool",
    }
    if normalized_quotes:
        info["parse_warning"] = "normalized nonstandard JSON punctuation"
    ch = str(data.get("color_hex", "")).strip().lstrip("#")
    if ch:
        info["color_hex"] = ch.upper()
    try:
        info["min_temp"] = int(data["min_temp"])
    except Exception:
        pass
    try:
        info["max_temp"] = int(data["max_temp"])
    except Exception:
        pass
    info.setdefault("diameter_mm", 1.75)
    return info


# ---- OpenPrintTag (CBOR) ---------------------------------------------------

_OPENPRINTTAG_MIME = "application/vnd.openprinttag"

_OPT_META_MAIN_OFFSET = 0
_OPT_META_MAIN_SIZE = 1
_OPT_META_AUX_OFFSET = 2
_OPT_META_AUX_SIZE = 3

_OPT_MAIN_MATERIAL_CLASS = 8
_OPT_MAIN_MATERIAL_TYPE = 9
_OPT_MAIN_MATERIAL_NAME = 10
_OPT_MAIN_BRAND_NAME = 11
_OPT_MAIN_NOMINAL_FULL_WEIGHT = 16
_OPT_MAIN_ACTUAL_FULL_WEIGHT = 17
_OPT_MAIN_EMPTY_CONTAINER_WEIGHT = 18
_OPT_MAIN_PRIMARY_COLOR = 19
_OPT_MAIN_DENSITY = 29
_OPT_MAIN_FILAMENT_DIAMETER = 30
_OPT_MAIN_MIN_PRINT_TEMP = 34
_OPT_MAIN_MAX_PRINT_TEMP = 35
_OPT_MAIN_PREHEAT_TEMP = 36
_OPT_MAIN_MIN_BED_TEMP = 37
_OPT_MAIN_MAX_BED_TEMP = 38
_OPT_MAIN_MATERIAL_ABBREVIATION = 52
_OPT_MAIN_NOMINAL_FULL_LENGTH = 53
_OPT_MAIN_ACTUAL_FULL_LENGTH = 54

_OPT_AUX_CONSUMED_WEIGHT = 0
_OPT_AUX_GP_RANGE_USER = 2
_OPT_AUX_GP_SPOOLMAN_ID = 65400

_OPENPRINTTAG_MATERIAL_TYPES = {
    0: "PLA",
    1: "PETG",
    2: "TPU",
    3: "ABS",
    4: "ASA",
    5: "PC",
    6: "PCTG",
    7: "PP",
    8: "PA6",
    9: "PA11",
    10: "PA12",
    11: "PA66",
    12: "CPE",
    13: "TPE",
    14: "HIPS",
    15: "PHA",
    16: "PET",
    17: "PEI",
    18: "PBT",
    19: "PVB",
    20: "PVA",
    21: "PEKK",
    22: "PEEK",
    23: "BVOH",
    24: "TPC",
    25: "PPS",
    26: "PPSU",
    27: "PVC",
    28: "PEBA",
    29: "PVDF",
    30: "PPA",
    31: "PCL",
    32: "PES",
    33: "PMMA",
    34: "POM",
    35: "PPE",
    36: "PS",
    37: "PSU",
    38: "TPI",
    39: "SBS",
    40: "OBC",
    41: "EVA",
}


def _cbor_load_first(data: bytes) -> tuple:
    """Decode one CBOR item and return (value, next_idx).

    Prefer cbor2 when available, but keep the built-in decoder as the normal
    Klipper-friendly path so OpenPrintTag does not require extra packages.
    """
    try:
        import io
        import cbor2  # type: ignore
        stream = io.BytesIO(data)
        decoder = cbor2.CBORDecoder(stream)
        value = decoder.decode()
        return value, stream.tell()
    except ImportError:
        return _cbor_decode(data, 0)


def _as_int(value) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except Exception:
        return None


def _as_float(value) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except Exception:
        return None


def _openprinttag_color_hex(value) -> Optional[str]:
    if isinstance(value, (bytes, bytearray)) and len(value) >= 3:
        return "%02X%02X%02X" % (value[0], value[1], value[2])
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return "%02X%02X%02X" % (int(value[0]), int(value[1]), int(value[2]))
        except Exception:
            return None
    if isinstance(value, int):
        if value <= 0xFFFFFF:
            return "%06X" % value
        return "%06X" % (value & 0xFFFFFF)
    if isinstance(value, str):
        ch = value.strip().lstrip("#")
        if re.fullmatch(r"[0-9A-Fa-f]{6}", ch):
            return ch.upper()
    return None


def _openprinttag_legacy_from_map(data: dict) -> Optional[dict]:
    # Compatibility for early/simple payloads that used the OpenPrintTag MIME
    # type but stored the filament fields directly in one CBOR map.
    def _get(*keys):
        for k in keys:
            v = data.get(k)
            if v is not None:
                return v
        return None

    material = str(_get("material", 2) or "").strip()
    if not material:
        return None

    info: dict = {"material": material, "tag_format": "openprinttag"}
    brand = str(_get("brand", 1) or "").strip()
    if brand:
        info["brand"] = brand

    color = _openprinttag_color_hex(_get("color", 3))
    if color:
        info["color_hex"] = color

    diam = _as_float(_get("diameter", 4))
    if diam is not None:
        info["diameter_mm"] = diam

    weight = _as_float(_get("weight", 5))
    if weight is not None:
        info["weight_g"] = int(weight)

    min_t = _as_int(_get("min_temp", 6))
    if min_t is not None:
        info["min_temp"] = min_t

    max_t = _as_int(_get("max_temp", 7))
    if max_t is not None:
        info["max_temp"] = max_t

    return info


def _openprinttag_region_from_payload(payload: bytes, offset, size=None) -> Optional[dict]:
    offset_i = _as_int(offset)
    if offset_i is None or offset_i < 0 or offset_i >= len(payload):
        return None

    if size is None:
        end = len(payload)
    else:
        size_i = _as_int(size)
        if size_i is None or size_i <= 0:
            end = len(payload)
        else:
            end = min(len(payload), offset_i + size_i)
    if end <= offset_i:
        return None

    data, _ = _cbor_load_first(payload[offset_i:end])
    return data if isinstance(data, dict) else None


def _try_openprinttag_standard(meta: dict, meta_end: int,
                               payload: bytes) -> Optional[dict]:
    main_offset = meta.get(_OPT_META_MAIN_OFFSET, meta.get("main_offset"))
    main_size = meta.get(_OPT_META_MAIN_SIZE, meta.get("main_size"))
    aux_offset = meta.get(_OPT_META_AUX_OFFSET, meta.get("aux_offset"))
    aux_size = meta.get(_OPT_META_AUX_SIZE, meta.get("aux_size"))

    # Older writers may omit the explicit main offset; the official library
    # then treats the main region as starting immediately after the meta item.
    if main_offset is None:
        main_offset = meta_end
    if main_size is None and aux_offset is not None:
        main_offset_i = _as_int(main_offset)
        aux_offset_i = _as_int(aux_offset)
        if main_offset_i is not None and aux_offset_i is not None:
            main_size = aux_offset_i - main_offset_i

    main = _openprinttag_region_from_payload(payload, main_offset, main_size)
    if not main:
        return None

    aux = None
    if aux_offset is not None and (_as_int(aux_offset) or 0) > 0:
        aux = _openprinttag_region_from_payload(payload, aux_offset, aux_size)

    material_name = str(main.get(_OPT_MAIN_MATERIAL_NAME) or "").strip()
    material_type = _as_int(main.get(_OPT_MAIN_MATERIAL_TYPE))
    material_abbr = str(main.get(_OPT_MAIN_MATERIAL_ABBREVIATION) or "").strip()

    material = material_abbr or material_name
    if material_type is not None:
        material = _OPENPRINTTAG_MATERIAL_TYPES.get(material_type, material)
    if not material:
        return None

    info: dict = {"material": material, "tag_format": "openprinttag"}
    if material_name and material_name != material:
        info["material_detail"] = material_name
    if material_type is not None:
        info["material_type_id"] = material_type

    brand = str(main.get(_OPT_MAIN_BRAND_NAME) or "").strip()
    if brand:
        info["brand"] = brand

    material_class = _as_int(main.get(_OPT_MAIN_MATERIAL_CLASS))
    if material_class is not None:
        info["material_class"] = "sla" if material_class == 1 else "fff"

    color = _openprinttag_color_hex(main.get(_OPT_MAIN_PRIMARY_COLOR))
    if color:
        info["color_hex"] = color

    diameter = _as_float(main.get(_OPT_MAIN_FILAMENT_DIAMETER))
    if diameter is not None and diameter > 0:
        info["diameter_mm"] = diameter
    else:
        info["diameter_mm"] = 1.75

    weight = _as_float(main.get(_OPT_MAIN_ACTUAL_FULL_WEIGHT))
    if weight is None:
        weight = _as_float(main.get(_OPT_MAIN_NOMINAL_FULL_WEIGHT))
    if weight is not None:
        info["weight_g"] = int(round(weight))

    spool_weight = _as_float(main.get(_OPT_MAIN_EMPTY_CONTAINER_WEIGHT))
    if spool_weight is not None:
        info["spool_weight_g"] = int(round(spool_weight))

    density = _as_float(main.get(_OPT_MAIN_DENSITY))
    if density is not None and density > 0:
        info["density"] = density

    length = _as_float(main.get(_OPT_MAIN_ACTUAL_FULL_LENGTH))
    if length is None:
        length = _as_float(main.get(_OPT_MAIN_NOMINAL_FULL_LENGTH))
    if length is not None and length > 0:
        info["filament_length_mm"] = length

    min_t = _as_int(main.get(_OPT_MAIN_MIN_PRINT_TEMP))
    if min_t is not None:
        info["min_temp"] = min_t
    max_t = _as_int(main.get(_OPT_MAIN_MAX_PRINT_TEMP))
    if max_t is not None:
        info["max_temp"] = max_t
    preheat_t = _as_int(main.get(_OPT_MAIN_PREHEAT_TEMP))
    if preheat_t is not None:
        info["preheat_temp"] = preheat_t
    min_bed = _as_int(main.get(_OPT_MAIN_MIN_BED_TEMP))
    if min_bed is not None:
        info["min_bed_temp"] = min_bed
        info.setdefault("bed_temp", min_bed)
    max_bed = _as_int(main.get(_OPT_MAIN_MAX_BED_TEMP))
    if max_bed is not None:
        info["max_bed_temp"] = max_bed
        info.setdefault("bed_temp", max_bed)

    if aux:
        consumed = _as_float(aux.get(_OPT_AUX_CONSUMED_WEIGHT))
        if consumed is not None:
            info["consumed_weight_g"] = int(round(consumed))
        spoolman_id = _as_int(aux.get(_OPT_AUX_GP_SPOOLMAN_ID))
        if spoolman_id is not None:
            info["spoolman_id"] = spoolman_id
        gp_user = aux.get(_OPT_AUX_GP_RANGE_USER)
        if gp_user is not None:
            info["openprinttag_gp_range_user"] = str(gp_user)

    return info


def _try_openprinttag(mime_type: str, payload: bytes) -> Optional[dict]:
    """Parse an OpenPrintTag CBOR NDEF payload."""
    if mime_type.lower() != _OPENPRINTTAG_MIME:
        return None
    if not payload:
        return None

    try:
        data, next_idx = _cbor_load_first(payload)

        if not isinstance(data, dict):
            return None

        if (
            _OPT_META_MAIN_OFFSET in data or "main_offset" in data
            or _OPT_META_MAIN_SIZE in data or "main_size" in data
        ):
            result = _try_openprinttag_standard(data, next_idx, payload)
            if result is not None:
                return result

        return _openprinttag_legacy_from_map(data)

    except Exception as exc:
        _log.debug("openprinttag cbor parse error: %s", exc)
        return None


# ---- SimplyPrint / QIDI URL -----------------------------------------------

def _try_simplyprint_url(text: str) -> Optional[dict]:
    """Parse a SimplyPrint / QIDI-standard URL-encoded filament tag.

    Detects URLs containing 'simplyprint.io' or a query string with known
    filament parameters (m/c/b/w/d/mint/maxt).
    """
    import urllib.parse

    text = text.strip()
    parsed = None
    try:
        parsed = urllib.parse.urlparse(text)
    except Exception:
        return None

    is_simplyprint = (
        (parsed.netloc or "").lower() == "simplyprint.io"
        or (parsed.netloc or "").lower().endswith(".simplyprint.io")
    )

    qs = parsed.query
    if not qs and "?" in text:
        qs = text.split("?", 1)[1]

    if not qs:
        return None

    params = urllib.parse.parse_qs(qs, keep_blank_values=False)

    def _first(key):
        vals = params.get(key)
        return vals[0].strip() if vals else None

    material = _first("m") or _first("material")
    if not material and not is_simplyprint:
        return None

    info: dict = {"tag_format": "simplyprint_url"}
    if material:
        info["material"] = material

    brand = _first("b") or _first("brand")
    if brand:
        info["brand"] = brand

    color = _first("c") or _first("color") or _first("color_hex")
    if color:
        info["color_hex"] = color.lstrip("#").upper()

    weight = _first("w") or _first("weight")
    if weight:
        try:
            info["weight_g"] = int(float(weight))
        except Exception:
            pass

    diam = _first("d") or _first("diameter")
    if diam:
        try:
            info["diameter_mm"] = float(diam)
        except Exception:
            pass

    min_t = _first("mint") or _first("min_temp")
    if min_t:
        try:
            info["min_temp"] = int(float(min_t))
        except Exception:
            pass

    max_t = _first("maxt") or _first("max_temp")
    if max_t:
        try:
            info["max_temp"] = int(float(max_t))
        except Exception:
            pass

    return info if info.get("material") else None


# ---- Generic NDEF JSON -----------------------------------------------------

_GENERIC_JSON_FIELDS = {
    "material", "type", "filament_type", "color", "color_hex",
    "brand", "weight", "diameter",
}


def _try_generic_ndef_json(text: str, trace=None) -> Optional[dict]:
    """Parse a generic NDEF text record that contains JSON filament data."""
    trace = _make_trace(trace)
    data, normalized_quotes = _loads_json_text(text, trace)
    if data is None:
        return None
    if not isinstance(data, dict):
        return None

    # Must contain at least one known filament field
    if not (_GENERIC_JSON_FIELDS & set(k.lower() for k in data)):
        return None

    # Also exclude OpenSpool (handled above)
    if str(data.get("protocol", "")).lower() == "openspool":
        return None

    material = str(
        data.get("material") or data.get("type") or
        data.get("filament_type") or ""
    ).strip()
    if not material:
        return None

    info: dict = {"material": material, "tag_format": "generic_ndef_json"}
    if normalized_quotes:
        info["parse_warning"] = "normalized nonstandard JSON punctuation"

    brand = str(data.get("brand", "") or "").strip()
    if brand:
        info["brand"] = brand

    color = str(data.get("color_hex") or data.get("color") or "").strip().lstrip("#")
    if color:
        info["color_hex"] = color.upper()

    try:
        info["weight_g"] = int(float(data["weight"]))
    except Exception:
        pass

    try:
        info["diameter_mm"] = float(data["diameter"])
    except Exception:
        pass

    try:
        info["min_temp"] = int(data["min_temp"])
    except Exception:
        pass

    try:
        info["max_temp"] = int(data["max_temp"])
    except Exception:
        pass

    return info


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_tag(raw, uid_hex: Optional[str] = None, trace=None) -> Optional[dict]:
    """Detect tag format and return a normalized filament_info dict, or None.

    Parameters
    ----------
    raw:
        Either:
        * ``bytes`` / ``bytearray`` — raw user-memory dump (starting at page 4
          for NTAG21x / Type 2 tags; may also be a full MIFARE Classic dump).
        * ``dict`` with keys ``"uid_bytes"`` (bytes) and ``"blocks"`` (dict
          mapping absolute block index → 16-byte bytes) — authenticated MIFARE
          Classic sector data produced by ``read_authenticated_blocks()``.
    uid_hex:  Optional UID hex string (used for logging only).

    Returns
    -------
    dict with any of the following keys (all optional except 'material'):
        material: str          e.g. "PLA", "PLA-CF", "PETG"
        material_detail: str   Bambu-style detailed type
        brand: str             e.g. "ELEGOO", "Bambu Lab", "Anycubic"
        color_hex: str         e.g. "FF3700" (no '#')
        diameter_mm: float     e.g. 1.75
        weight_g: int          e.g. 1000
        spool_weight_g: int    empty spool weight if known
        min_temp: int          hotend min °C
        max_temp: int          hotend max °C
        bed_temp: int          bed temp °C
        drying_temp: int       drying temp °C
        drying_time_h: int     drying time hours
        sku: str               vendor SKU
        tag_format: str        one of the format identifiers listed above
        writable: bool         False for Bambu (RSA-signed, read-only)
    or None if the format is unrecognised or the tag cannot be parsed.
    """
    trace = _make_trace(trace)
    # --- Authenticated MIFARE Classic block dict (from read_authenticated_blocks) ---
    if isinstance(raw, dict):
        blocks = raw.get("blocks") or {}
        uid_bytes = raw.get("uid_bytes")
        trace("debug", "parse_tag: authenticated block input uid=%s block_count=%d",
              uid_hex or "unknown", len(blocks))
        if blocks:
            # Try Bambu block layout — _parse_bambu_blocks() is pure Python and
            # does not require pycryptodome (only key derivation does).
            try:
                trace("debug", "parse_tag: trying Bambu block layout")
                result = _parse_bambu_blocks(blocks)
                if result is not None:
                    _log.debug(
                        "rfid: parsed Bambu Lab blocks uid=%s",
                        uid_hex or "unknown",
                    )
                    trace("info", "parse_tag: matched Bambu Lab blocks")
                    return result
            except Exception as exc:
                _log.debug("rfid: Bambu block parse error: %s", exc)
                trace("debug", "parse_tag: Bambu block parse error: %s", exc)
            # Try Creality AES tag layout — needs sector 1 read with the
            # UID-derived Key B (see _creality_derive_key_b()); a Key-A read
            # (Bambu or default) never reaches this data, so it only matches
            # blocks produced by tag_handler's Creality Key B fallback.
            try:
                trace("debug", "parse_tag: trying Creality AES block layout")
                result = _try_creality_tag(
                    blocks,
                    uid_hex=uid_hex or (
                        bytes(uid_bytes).hex().upper()
                        if uid_bytes is not None else None),
                    trace=trace)
                if result is not None:
                    _log.debug(
                        "rfid: parsed Creality AES tag blocks uid=%s",
                        uid_hex or "unknown",
                    )
                    trace("info", "parse_tag: matched Creality AES tag blocks")
                    return result
            except Exception as exc:
                _log.debug("rfid: Creality AES block parse error: %s", exc)
                trace("debug", "parse_tag: Creality AES block parse error: %s", exc)
            # Build a flat byte string for Creality/QIDI parsers.
            # Use a fixed-size buffer indexed by absolute block number so that
            # block N always starts at offset N * 16, even if some blocks were
            # not read or are trailer blocks. Missing/failed blocks are zeroed.
            if blocks:
                max_block_index = max(blocks.keys())
                # For MIFARE Classic 1K there are 64 blocks; ensure the buffer
                # is at least large enough for both the card and the highest
                # observed block index.
                total_blocks = max(64, max_block_index + 1)
                buf = bytearray(total_blocks * 16)
                for block_index, data in blocks.items():
                    if not data:
                        continue
                    start = block_index * 16
                    end = start + 16
                    # Truncate or pad data to exactly 16 bytes when copying.
                    buf[start:end] = data[:16].ljust(16, b"\x00")
                flat_blocks = bytes(buf)
            else:
                flat_blocks = b""
            # Try Creality CFS (block-based)
            trace("debug", "parse_tag: trying Creality CFS blocks")
            result = _try_creality_cfs(flat_blocks)
            if result is not None:
                _log.debug("rfid: parsed Creality CFS blocks uid=%s", uid_hex or "unknown")
                trace("info", "parse_tag: matched Creality CFS blocks")
                return result
            # Try QIDI Box (block-based)
            trace("debug", "parse_tag: trying QIDI Box blocks")
            result = _try_qidi_box(flat_blocks)
            if result is not None:
                _log.debug("rfid: parsed QIDI Box blocks uid=%s", uid_hex or "unknown")
                trace("info", "parse_tag: matched QIDI Box blocks")
                return result
        trace("info", "parse_tag: no authenticated block parser matched")
        return None

    # --- Raw bytes path ---
    # Each format is tried exactly once in priority order; the first successful
    # parse is returned immediately.  No format is re-attempted or double-decoded.
    if not raw:
        trace("info", "parse_tag: no raw data")
        return None

    uid_info = f" uid={uid_hex}" if uid_hex else ""
    trace("debug", "parse_tag: raw byte input uid=%s raw_len=%d",
          uid_hex or "unknown", len(raw))

    # 1 — ELEGOO binary
    trace("debug", "parse_tag: trying ELEGOO binary")
    result = _try_elegoo(raw)
    if result is not None:
        _log.debug("rfid: parsed ELEGOO tag%s", uid_info)
        trace("info", "parse_tag: matched ELEGOO")
        return result

    # 2 — Anycubic ACE binary
    trace("debug", "parse_tag: trying Anycubic ACE binary")
    result = _try_anycubic_ace(raw)
    if result is not None:
        _log.debug("rfid: parsed Anycubic ACE tag%s", uid_info)
        trace("info", "parse_tag: matched Anycubic ACE")
        return result

    # 3 — TigerTag binary Type-2 payload
    trace("debug", "parse_tag: trying TigerTag binary")
    result = _try_tigertag(raw)
    if result is not None:
        _log.debug("rfid: parsed TigerTag tag%s", uid_info)
        trace("info", "parse_tag: matched TigerTag")
        return result

    # 4 — NDEF-based formats (OpenTag3D, OpenSpool, OpenPrintTag, URL, JSON)
    ndef_bytes = _find_ndef_tlv(raw)
    if ndef_bytes is not None:
        records = _parse_ndef_records(ndef_bytes)
        trace("debug", "parse_tag: found NDEF TLV length=%d records=%d",
              len(ndef_bytes), len(records))

        # 3a — MIME type records (OpenTag3D, OpenPrintTag)
        for mime_type, payload in _get_ndef_mime_records(records):
            trace("debug", "parse_tag: trying NDEF MIME %s payload_len=%d",
                  mime_type, len(payload))
            result = _try_opentag3d(mime_type, payload)
            if result is not None:
                _log.debug("rfid: parsed OpenTag3D tag%s", uid_info)
                trace("info", "parse_tag: matched OpenTag3D")
                return result
            result = _try_openprinttag(mime_type, payload)
            if result is not None:
                _log.debug("rfid: parsed OpenPrintTag tag%s", uid_info)
                trace("info", "parse_tag: matched OpenPrintTag")
                return result

        # 3b — Text/URI records
        for text in _extract_text_from_records(records):
            # OpenSpool JSON
            trace("debug", "parse_tag: trying NDEF text/URI length=%d", len(text))
            result = _try_openspool(text, trace=trace)
            if result is not None:
                _log.debug("rfid: parsed OpenSpool tag%s", uid_info)
                trace("info", "parse_tag: matched OpenSpool")
                return result
            # SimplyPrint / QIDI URL
            result = _try_simplyprint_url(text)
            if result is not None:
                _log.debug("rfid: parsed SimplyPrint URL tag%s", uid_info)
                trace("info", "parse_tag: matched SimplyPrint URL")
                return result
            # Generic NDEF JSON
            result = _try_generic_ndef_json(text, trace=trace)
            if result is not None:
                _log.debug("rfid: parsed generic NDEF JSON tag%s", uid_info)
                trace("info", "parse_tag: matched generic NDEF JSON")
                return result
    else:
        trace("debug", "parse_tag: no NDEF TLV found")

    # 4 — Creality CFS (MIFARE Classic hex-encoded ASCII)
    trace("debug", "parse_tag: trying Creality CFS raw bytes")
    result = _try_creality_cfs(raw)
    if result is not None:
        _log.debug("rfid: parsed Creality CFS tag%s", uid_info)
        trace("info", "parse_tag: matched Creality CFS")
        return result

    # 5 — QIDI Box (MIFARE Classic binary codes)
    trace("debug", "parse_tag: trying QIDI Box raw bytes")
    result = _try_qidi_box(raw)
    if result is not None:
        _log.debug("rfid: parsed QIDI Box tag%s", uid_info)
        trace("info", "parse_tag: matched QIDI Box")
        return result

    # 6 — Bambu Lab (encrypted raw dump — only detectable, cannot decrypt without auth)
    # Return a clear error dict instead of None so callers can surface a helpful
    # message.  Full decryption requires an authenticated MIFARE Classic read with
    # HKDF-derived Key A keys; a raw byte dump cannot be decrypted here.
    # Use is_parse_error() to distinguish this from a successful parse.
    if _detect_bambu(raw):
        _log.debug(
            "rfid: Bambu Lab tag detected in raw dump%s — "
            "use authenticated read for full data", uid_info
        )
        trace("info", "parse_tag: detected Bambu raw dump without authenticated data")
        return {
            "error": (
                "Detected Bambu Lab tag but decryption/authentication not available; "
                "see README for hardware requirements"
            ),
            "tag_format": "bambu",
            "brand": "Bambu Lab",
        }

    # 7 — Fallback: try raw bytes as UTF-8 text for JSON / URL formats
    try:
        text = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        if text:
            trace("debug", "parse_tag: trying raw UTF-8 fallback length=%d", len(text))
            result = _try_openspool(text, trace=trace)
            if result is not None:
                _log.debug("rfid: parsed OpenSpool (raw UTF-8) tag%s", uid_info)
                trace("info", "parse_tag: matched OpenSpool raw UTF-8")
                return result
            result = _try_simplyprint_url(text)
            if result is not None:
                _log.debug("rfid: parsed SimplyPrint URL (raw UTF-8) tag%s", uid_info)
                trace("info", "parse_tag: matched SimplyPrint URL raw UTF-8")
                return result
            result = _try_generic_ndef_json(text, trace=trace)
            if result is not None:
                _log.debug("rfid: parsed generic JSON (raw UTF-8) tag%s", uid_info)
                trace("info", "parse_tag: matched generic JSON raw UTF-8")
                return result
    except Exception:
        pass

    _log.debug("rfid: unrecognised tag format%s raw_len=%d", uid_info, len(raw))
    trace("info", "parse_tag: unrecognised tag format raw_len=%d", len(raw))
    return None


def is_bambu_tag(info: Optional[dict]) -> bool:
    """Return True if the parsed info dict originated from a Bambu Lab tag."""
    return isinstance(info, dict) and info.get("tag_format") == "bambu"


def format_bambu_info(info: dict, uid_hex: Optional[str] = None) -> str:
    """Format a parsed Bambu Lab filament info dict into a human-readable summary.

    Produces a labeled, multi-line string showing all available spool fields —
    matching the data visible in the Bambu Lab Android app and public tag decoders
    (e.g. queengooborg/Bambu-Lab-RFID-Tag-Guide, BambuSpoolPal).

    Parameters
    ----------
    info:
        Dict returned by ``parse_tag()`` for a Bambu Lab tag.
    uid_hex:
        Optional hardware UID hex string (4-byte MIFARE UID, e.g. ``"62F0E76B"``).
        Shown as "Tag UID" in the summary to distinguish it from the Tray UID
        stored inside the tag's block 9.

    Returns
    -------
    Multi-line string suitable for passing to ``logging.info()``.

    Example output
    --------------
    === Bambu Lab RFID Tag ===
      Tag UID (hardware) : 62F0E76B
      Tray UID           : 5F390A603AAB4B8FB1524EA53B16FA77
      Filament Type      : PLA Basic
      Material           : PLA
      Material ID        : GFA50
      Color              : #FF3700
      Diameter           : 1.75 mm
      Weight             : 1000 g
      Filament Length    : 330 m
      Production Date    : 2024_03_15_10_30
      Drying             : 55 °C x 8 h
      Bed Temperature    : 60 °C
      Hotend Range       : 190-220 °C
    """
    lines = ["=== Bambu Lab RFID Tag ==="]

    # Hardware UID (anti-collision UID from the MFRC522; 4 bytes for MIFARE Classic 1K)
    if uid_hex:
        lines.append(f"  Tag UID (hardware) : {uid_hex}")

    # Tray UID — 16-byte ASCII hex string stored in block 9 of the tag.
    # This is the "UID" shown by Bambu apps and tag decoders; it is NOT the
    # same as the MIFARE anti-collision UID above.
    tray_uid = info.get("tray_uid")
    if tray_uid:
        # Ensure tray_uid is shown as a hex string even if bytes slipped through.
        if isinstance(tray_uid, (bytes, bytearray)):
            tray_uid = tray_uid.hex().upper()
        lines.append(f"  Tray UID           : {tray_uid}")

    # Detailed filament type (e.g. "PLA Basic") comes from block 4.
    # Basic material name (e.g. "PLA") comes from block 2.
    material_detail = info.get("material_detail")
    material = info.get("material")
    if material_detail:
        lines.append(f"  Filament Type      : {material_detail}")
    if material:
        lines.append(f"  Material           : {material}")

    # Material IDs from the tray info index block (block 1).
    # material_id is the SKU-style code (e.g. "GFA50").
    # material_variant_id is the variant byte prefix (e.g. "GFL99").
    material_id = info.get("material_id")
    if material_id:
        lines.append(f"  Material ID        : {material_id}")
    material_variant_id = info.get("material_variant_id")
    if material_variant_id:
        lines.append(f"  Variant ID         : {material_variant_id}")

    # Color as 6-digit HTML hex (no '#') from block 5 bytes 0-3 (RGBA).
    color_hex = info.get("color_hex")
    if color_hex:
        lines.append(f"  Color              : #{color_hex}")
    # Second color for multi-colour filaments (block 16, ABGR → RGBA).
    second_color = info.get("second_color_hex")
    if second_color:
        lines.append(f"  Second Color       : #{second_color}")

    # Physical spool dimensions from block 5.
    diameter_mm = info.get("diameter_mm")
    if diameter_mm is not None:
        lines.append(f"  Diameter           : {diameter_mm:.2f} mm")
    weight_g = info.get("weight_g")
    if weight_g is not None:
        lines.append(f"  Weight             : {weight_g} g")

    # Filament length in metres from block 14 offset 4.
    filament_length_m = info.get("filament_length_m")
    if filament_length_m is not None:
        lines.append(f"  Filament Length    : {filament_length_m} m")

    # Production date as ASCII "yyyy_MM_dd_HH_mm" from block 12.
    production_date = info.get("production_date")
    if production_date:
        lines.append(f"  Production Date    : {production_date}")

    # Drying recommendation from block 6 bytes 0-3.
    drying_temp = info.get("drying_temp")
    drying_time_h = info.get("drying_time_h")
    if drying_temp is not None and drying_time_h is not None:
        lines.append(f"  Drying             : {drying_temp} \u00b0C x {drying_time_h} h")
    elif drying_temp is not None:
        lines.append(f"  Drying Temp        : {drying_temp} \u00b0C")

    # Bed temperature from block 6 bytes 6-7.
    bed_temp = info.get("bed_temp")
    if bed_temp is not None:
        lines.append(f"  Bed Temperature    : {bed_temp} \u00b0C")

    # Hotend temperature range from block 6 bytes 8-11.
    min_temp = info.get("min_temp")
    max_temp = info.get("max_temp")
    if min_temp is not None and max_temp is not None:
        lines.append(f"  Hotend Range       : {min_temp}-{max_temp} \u00b0C")
    elif max_temp is not None:
        lines.append(f"  Hotend Max         : {max_temp} \u00b0C")
    elif min_temp is not None:
        lines.append(f"  Hotend Min         : {min_temp} \u00b0C")

    return "\n".join(lines)


def is_parse_error(info: Optional[dict]) -> bool:
    """Return True if the info dict represents a detection-only result or parse error.

    parse_tag() returns a dict with an ``"error"`` key when a tag is detected
    but cannot be fully decoded (e.g. a Bambu Lab raw byte dump without hardware
    authentication support).  Use this helper to distinguish such partial results
    from a successful parse that contains actionable filament data.
    """
    return isinstance(info, dict) and bool(info.get("error"))
