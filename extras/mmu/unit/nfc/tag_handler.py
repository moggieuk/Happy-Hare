# klippy/extras/mmu/mmu_nfc_tag_handler.py
#
# EMU NFC Gate Reader — tag reading and spool resolution pipeline
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# All functions follow the scan_jog.py convention: receive the NFCGate instance
# as their first argument so they can access gate state without subclassing.
#
# Pipeline order (called from NFCGate._poll via delegates):
#
#   read_current_tag(gate)   — hardware read + metadata capture
#   resolve_spool(gate, uid) — resolution ladder: embedded ID → UID → auto-create → metadata-direct

import inspect

from .mmu_nfc_gate_state import CurrentTag, DIRECT_METADATA_SPOOL
from .mmu_nfc_log import logger

LED_AUTO_CREATING = 'mmu_RFID_creating'
LED_UNRESOLVED    = 'mmu_RFID_unresolved'


def _lane_led_effect(gate, effect_name):
    """Start effect_name on this gate's exit LED (per-gate _exit_N naming)."""
    printer = getattr(gate, 'printer', None)
    if printer is None:
        return
    try:
        gate.mmu.led_manager.set_transient_effect(
            gate._mmu_unit, effect_name, segment='exit', gate=gate._gate)
    except Exception as e:
        if getattr(gate, '_debug', 0) >= 3:
            logger.info("[%s]: LED effect %s skipped: %s",
                        gate._name, effect_name, e)


# ── NTAG / NDEF helpers ───────────────────────────────────────────────────────

def _find_ndef_tlv(data):
    if data is None:
        return None
    raw = bytes(data)
    i = 0
    while i < len(raw):
        t = raw[i]
        if t == 0x00:
            i += 1
            continue
        if t == 0xFE:
            return None
        if i + 1 >= len(raw):
            return None
        length = raw[i + 1]
        if length == 0xFF:
            if i + 3 >= len(raw):
                return None
            length = (raw[i + 2] << 8) | raw[i + 3]
            value_start = i + 4
        else:
            value_start = i + 2
        value_end = value_start + length
        if t == 0x03:
            if value_end > len(raw):
                return {
                    'complete': False,
                    'ndef_len': length,
                    'tlv_len': value_end,
                    'payload': raw[value_start:],
                }
            return {
                'complete': True,
                'ndef_len': length,
                'tlv_len': value_end,
                'payload': raw[value_start:value_end],
            }
        if value_end > len(raw):
            return None
        i = value_end
    return None


def _decode_ndef_text_records(ndef):
    records = []
    idx = 0
    while idx < len(ndef):
        header = ndef[idx]
        idx += 1
        sr = bool(header & 0x10)
        il = bool(header & 0x08)
        tnf = header & 0x07
        if idx >= len(ndef):
            break
        type_len = ndef[idx]
        idx += 1
        if sr:
            if idx >= len(ndef):
                break
            payload_len = ndef[idx]
            idx += 1
        else:
            if idx + 4 > len(ndef):
                break
            payload_len = int.from_bytes(ndef[idx:idx + 4], 'big')
            idx += 4
        id_len = 0
        if il:
            if idx >= len(ndef):
                break
            id_len = ndef[idx]
            idx += 1
        rec_type = ndef[idx:idx + type_len]
        idx += type_len + id_len
        payload = ndef[idx:idx + payload_len]
        idx += payload_len
        if tnf == 0x01 and rec_type == b'T' and payload:
            status = payload[0]
            lang_len = status & 0x3F
            encoding = 'utf-16' if status & 0x80 else 'utf-8'
            text_payload = payload[1 + lang_len:]
            try:
                records.append(text_payload.decode(encoding, errors='replace'))
            except Exception:
                records.append(text_payload.decode('utf-8', errors='replace'))
        if header & 0x40:  # ME
            break
    return records


def _single_line_preview(text, limit=300):
    text = ' '.join(str(text).replace('\x00', '').split())
    if len(text) <= limit:
        return text
    return text[:limit - 3] + '...'


_META_SUMMARY_KEYS = (
    'tag_format', 'brand', 'vendor', 'material', 'material_detail',
    'material_id', 'material_variant_id', 'sku', 'color_hex',
    'diameter_mm', 'weight_g', 'spool_weight_g', 'min_temp', 'max_temp',
    'bed_temp', 'drying_temp', 'drying_time_h', 'tray_uid', 'spool_identity',
    'spoolman_id', 'material_code', 'color_code', 'manufacturer_code',
    'creality_vendor_id', 'creality_filament_id', 'creality_batch',
    'creality_date_code', 'creality_production_date', 'creality_serial',
    'creality_identity_seed', 'creality_identity_numeric',
    'parse_warning', 'parse_error', 'error',
)


def _summarize_meta(meta):
    if not isinstance(meta, dict):
        return {}
    return {k: meta.get(k) for k in _META_SUMMARY_KEYS
            if meta.get(k) not in (None, '')}


def _raw_tag_summary(raw):
    if isinstance(raw, dict):
        blocks = raw.get('blocks') or {}
        block_indexes = sorted(blocks.keys())
        return {
            'kind': 'mifare_blocks',
            'block_count': len(block_indexes),
            'blocks': block_indexes,
            'uid_bytes_len': len(raw.get('uid_bytes') or b''),
        }
    try:
        raw_bytes = bytes(raw)
    except Exception:
        return {'kind': type(raw).__name__}
    return {'kind': 'bytes', 'length': len(raw_bytes)}


def _raw_tag_preview(raw, limit=96):
    if isinstance(raw, dict):
        blocks = raw.get('blocks') or {}
        preview = {}
        for block_index in sorted(blocks.keys())[:8]:
            data = blocks.get(block_index) or b''
            try:
                preview[block_index] = bytes(data).hex().upper()
            except Exception:
                preview[block_index] = repr(data)
        return preview
    try:
        raw_bytes = bytes(raw)
    except Exception:
        return repr(raw)
    suffix = '...' if len(raw_bytes) > limit else ''
    return raw_bytes[:limit].hex().upper() + suffix


def _parse_attempt_summary(raw):
    if isinstance(raw, dict):
        return ('authenticated MIFARE blocks: Bambu, Creality CFS, '
                'then QIDI Box')
    return ('raw bytes: ELEGOO, Anycubic ACE, TigerTag, NDEF MIME/Text/URI, '
            'Creality CFS, QIDI Box, Bambu heuristic, then raw UTF-8 JSON/URL')


def _trace_for_gate(gate, prefix):
    def _trace(level, msg, *args):
        if level == 'debug':
            if gate._debug >= 4:
                logger.debug(prefix + msg, *args)
            return
        if gate._debug >= 3:
            logger.info(prefix + msg, *args)
    return _trace


def _accepts_kwarg(callable_obj, name):
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name:
            return True
    return False


def _spool_identity_from_meta(meta):
    if not isinstance(meta, dict):
        return None
    value = str(meta.get('spool_identity') or '').strip()
    return value or None


def _left_neighbor_identity_match(gate, identity):
    identity = str(identity or '').strip()
    if not identity or getattr(gate, '_gate', 0) <= 0:
        return False
    left_gate = gate._gate - 1
    left_nfc = gate._nfc_gate_for_gate_number(left_gate)
    if left_nfc is None:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — spool_identity=%s left-neighbor "
                "pre-create check: gate %d has no NFC instance",
                gate._name, gate._gate, identity, left_gate)
        return False
    left_tag = getattr(left_nfc._state, 'current_tag', None)
    left_identity = (
        getattr(left_tag, 'spool_identity', None)
        if left_tag is not None else None)
    if not left_identity:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — spool_identity=%s left-neighbor "
                "pre-create check: gate %d has no spool_identity",
                gate._name, gate._gate, identity, left_gate)
        return False
    left_hh = gate._read_hh_status_for_gate(left_gate)
    if left_hh.present and not left_hh.available:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — spool_identity=%s left-neighbor "
                "pre-create check: gate %d identity=%s suppressed "
                "because Happy Hare reports gate empty (status=%d)",
                gate._name, gate._gate, identity, left_gate, left_identity,
                left_hh.status)
        return False
    match = str(left_identity) == identity
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — spool_identity=%s left-neighbor "
            "pre-create check: gate %d identity=%s -> %s",
            gate._name, gate._gate, identity, left_gate, left_identity,
            "match" if match else "no match")
    return match


def _left_neighbor_spool_id(gate):
    if getattr(gate, '_gate', 0) <= 0:
        return None
    left_nfc = gate._nfc_gate_for_gate_number(gate._gate - 1)
    if left_nfc is None:
        return None
    return getattr(left_nfc._state, 'current_spool', None)


def _same_spool_id(left_spool, spool_id):
    if left_spool is None or spool_id is None:
        return False
    try:
        return int(left_spool) == int(spool_id)
    except (TypeError, ValueError):
        return str(left_spool) == str(spool_id)


# ── Tag classification ────────────────────────────────────────────────────────

def classify_tag_target(gate, target_info):
    if not isinstance(target_info, dict):
        return 'uid_only'
    protocol = str(target_info.get('protocol') or '').strip().lower()
    protocol_name = str(target_info.get('protocol_name') or '').strip().lower()
    if protocol == 'uid_only' or protocol_name.endswith('uid_only'):
        return 'uid_only'
    if protocol == 'iso15693_type5' or protocol_name == 'iso15693':
        return 'iso15693_type5'
    try:
        sak = int(target_info.get('sak', 0)) & 0xFF
        uid_length = int(target_info.get('uid_length', 0))
    except (TypeError, ValueError):
        return 'uid_only'
    # Conservative ISO14443A split:
    #   SAK bit 0x08 marks MIFARE Classic-compatible targets.
    #   SAK 0x00 is the common Type-2 / Ultralight / NTAG case.
    if sak & 0x08:
        return 'mifare_classic'
    if sak == 0x00 and uid_length in (4, 7, 10):
        return 'ntag_type2'
    return 'uid_only'


# ── Hardware helpers ──────────────────────────────────────────────────────────

def release_reader_target(gate, reason):
    release = getattr(gate._reader, '_release_current_target', None)
    if release is not None:
        try:
            release(reason=reason)
        except TypeError:
            release()
        except Exception as e:
            if gate._debug >= 4:
                logger.debug(
                    "[%s]: gate %d — target release failed "
                    "(%s): %s", gate._name, gate._gate, reason, e)


# ── Metadata capture ─────────────────────────────────────────────────────────

def resolve_spool_by_uid_before_metadata(gate, tag):
    if gate._spoolman is None:
        return None
    uid_hex = tag.uid
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  early UID lookup: checking "
            "Spoolman extra field %s before structured tag read",
            gate._name, gate._gate, uid_hex, gate._spoolman._rfid_key)
    try:
        spool_id = gate._spoolman.lookup_spool_by_uid(uid_hex)
    except Exception as e:
        tag.resolution = {'path': 'early_uid_lookup_failed',
                          'error': str(e)}
        logger.warning(
            "[%s]: gate %d — uid=%s  early UID lookup failed: %s; "
            "continuing structured tag read",
            gate._name, gate._gate, uid_hex, e)
        return None
    if spool_id is None:
        tag.resolution = {'path': 'early_uid_lookup_miss'}
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  early UID lookup found no "
                "Spoolman spool; continuing structured tag read",
                gate._name, gate._gate, uid_hex)
        return None
    try:
        spool_id = int(spool_id)
    except (TypeError, ValueError):
        logger.warning(
            "[%s]: gate %d — uid=%s  early UID lookup returned "
            "invalid spool_id=%r; continuing structured tag read",
            gate._name, gate._gate, uid_hex, spool_id)
        return None
    tag.spool_id = spool_id
    tag.resolution = {'path': 'early_uid_lookup', 'spool_id': spool_id}
    left_spool = _left_neighbor_spool_id(gate)
    force_identity = (
        getattr(gate, '_scan_mode', False)
        and getattr(gate, '_tag_parsing', False)
        and _same_spool_id(left_spool, spool_id))
    if force_identity:
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  early UID lookup resolved "
                "Spoolman spool_id=%s matching left_spool=%s; continuing "
                "structured tag read before interference handling",
                gate._name, gate._gate, uid_hex, spool_id, left_spool)
        return spool_id
    release_reader_target(gate, "early_uid_lookup")
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  early UID lookup resolved "
            "Spoolman spool_id=%s left_spool=%s; skipping structured tag read",
            gate._name, gate._gate, uid_hex, spool_id,
            left_spool if left_spool is not None else "None")
    return spool_id


def parse_current_tag(gate, tag):
    uid_hex = tag.uid
    if not tag.raw_tag_data:
        tag.meta = {'uid': uid_hex}
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  parse_tag skipped: no raw tag data",
                gate._name, gate._gate, uid_hex)
        return
    try:
        from .mmu_nfc_tag_parser import parse_tag
        raw = (bytes(tag.raw_tag_data)
               if isinstance(tag.raw_tag_data, (bytes, bytearray))
               else tag.raw_tag_data)
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  parse_tag begin raw=%s",
                gate._name, gate._gate, uid_hex, _raw_tag_summary(raw))
        if gate._debug >= 4:
            logger.debug(
                "[%s]: gate %d — uid=%s  raw tag preview: %s",
                gate._name, gate._gate, uid_hex, _raw_tag_preview(raw))
            logger.debug(
                "[%s]: gate %d — uid=%s  parse_tag attempt order: %s",
                gate._name, gate._gate, uid_hex, _parse_attempt_summary(raw))
        if _accepts_kwarg(parse_tag, 'trace'):
            info = parse_tag(
                raw,
                uid_hex=uid_hex,
                trace=_trace_for_gate(
                    gate,
                    "[%s]: gate %d — uid=%s  rfid_tag_parser: " %
                    (gate._name, gate._gate, uid_hex)))
        else:
            info = parse_tag(raw, uid_hex=uid_hex)
        if isinstance(info, dict) and 'uid' not in info:
            info = dict(info)
            info['uid'] = uid_hex
        if info is None:
            tag.meta = {'uid': uid_hex}
            tag.spool_identity = None
            tag.parse_error = None
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  parse_tag result: unrecognised format",
                    gate._name, gate._gate, uid_hex)
        else:
            tag.meta = info
            tag.spool_identity = _spool_identity_from_meta(info)
            tag.parse_error = info.get('parse_error') or info.get('error')
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  spool_identity resolved from "
                "parsed meta: %s",
                gate._name, gate._gate, uid_hex,
                tag.spool_identity if tag.spool_identity else "None")
            logger.info("[%s]: gate %d — uid=%s  parsed tag meta: %s",
                        gate._name, gate._gate, uid_hex, _summarize_meta(tag.meta))
        if gate._debug >= 4:
            logger.debug("[%s]: gate %d — uid=%s  full meta: %s",
                         gate._name, gate._gate, uid_hex, tag.meta)
    except Exception as e:
        tag.parse_error = 'parse failed: {}'.format(e)
        logger.error("[%s]: gate %d — uid=%s  parse_tag raised: %s",
                     gate._name, gate._gate, uid_hex, e)


def capture_ntag_metadata(gate, tag):
    uid_hex = tag.uid
    try:
        read_ndef = getattr(gate._reader, 'ntag_read_ndef_user_memory', None)
        if read_ndef is not None:
            raw = read_ndef(start_page=4, max_pages=gate._tag_max_pages)
        else:
            raw = gate._reader.ntag_read_user_memory(
                start_page=4, end_page=4 + gate._tag_max_pages - 1)
        tag.raw_tag_data = raw
        if gate._debug >= 3:
            tlv = _find_ndef_tlv(raw)
            if tlv is not None:
                logger.info(
                    "[%s]: gate %d — uid=%s  NTAG read %d bytes "
                    "(NDEF length=%d, %s)",
                    gate._name, gate._gate, uid_hex, len(raw),
                    tlv['ndef_len'], 'complete' if tlv['complete'] else 'partial')
                for text in _decode_ndef_text_records(tlv['payload']):
                    logger.info(
                        "[%s]: gate %d — uid=%s  NDEF text: %s",
                        gate._name, gate._gate, uid_hex,
                        _single_line_preview(text))
            else:
                logger.info("[%s]: gate %d — uid=%s  NTAG read %d bytes",
                            gate._name, gate._gate, uid_hex, len(raw))
    except Exception as e:
        tag.parse_error = 'ntag read failed: {}'.format(e)
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        tag.read_retry_reason = tag.parse_error
        logger.warning("[%s]: gate %d — uid=%s  NTAG read failed: %s",
                       gate._name, gate._gate, uid_hex, e)
        return
    if not raw:
        tag.parse_error = 'empty ntag read'
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        tag.read_retry_reason = tag.parse_error
        logger.warning("[%s]: gate %d — uid=%s  NTAG read returned no data",
                       gate._name, gate._gate, uid_hex)
        return
    parse_current_tag(gate, tag)


def _type5_parser_memory(raw):
    """Return Type-5 memory in the Type-2-like shape expected by parse_tag().

    ISO15693 / NFC Type 5 tags usually start with a 4-byte Capability
    Container (0xE1 or 0xE2), followed by TLVs.  The shared parser expects the
    byte stream to start at the TLV area, like NTAG page 4, so strip the CC only
    when it is clearly present.
    """
    data = bytes(raw or b'')
    if len(data) >= 5 and data[0] in (0xE1, 0xE2):
        return bytearray(data[4:])
    return bytearray(data)


def capture_iso15693_metadata(gate, tag):
    uid_hex = tag.uid
    try:
        read_type5 = getattr(gate._reader, 'iso15693_read_user_memory', None)
        if read_type5 is None:
            raise RuntimeError("reader does not support ISO15693 memory reads")
        raw = read_type5(tag=tag.target_info)
        parser_raw = _type5_parser_memory(raw)
        tag.raw_tag_data = parser_raw
        if gate._debug >= 3:
            tlv = _find_ndef_tlv(parser_raw)
            if tlv is not None:
                logger.info(
                    "[%s]: gate %d — uid=%s  ISO15693 read %d bytes "
                    "(parser bytes=%d, NDEF length=%d, %s)",
                    gate._name, gate._gate, uid_hex, len(raw),
                    len(parser_raw), tlv['ndef_len'],
                    'complete' if tlv['complete'] else 'partial')
                for text in _decode_ndef_text_records(tlv['payload']):
                    logger.info(
                        "[%s]: gate %d — uid=%s  NDEF text: %s",
                        gate._name, gate._gate, uid_hex,
                        _single_line_preview(text))
            else:
                logger.info(
                    "[%s]: gate %d — uid=%s  ISO15693 read %d bytes "
                    "(parser bytes=%d)",
                    gate._name, gate._gate, uid_hex, len(raw),
                    len(parser_raw))
    except Exception as e:
        tag.parse_error = 'iso15693 read failed: {}'.format(e)
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        tag.read_retry_reason = tag.parse_error
        logger.warning("[%s]: gate %d — uid=%s  ISO15693 read failed: %s",
                       gate._name, gate._gate, uid_hex, e)
        return
    if not tag.raw_tag_data:
        tag.parse_error = 'empty iso15693 read'
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        tag.read_retry_reason = tag.parse_error
        logger.warning("[%s]: gate %d — uid=%s  ISO15693 read returned no data",
                       gate._name, gate._gate, uid_hex)
        return
    parse_current_tag(gate, tag)


def resolve_auth_keys(gate, tag):
    """Derive MIFARE sector Key-A values for a Bambu tag via HKDF.

    Returns (keys, None) on success, (None, reason_str) on failure.
    """
    try:
        from .mmu_nfc_tag_parser import _bambu_derive_keys
        uid_bytes = bytes((tag.target_info or {}).get('uid_bytes') or [])
        if len(uid_bytes) < 4:
            return None, ('uid_bytes too short for Bambu key derivation '
                          '(%d bytes)' % len(uid_bytes))
        keys = _bambu_derive_keys(uid_bytes)
        return keys, None
    except ImportError as e:
        return None, 'pycryptodome not installed: %s' % e
    except Exception as e:
        return None, 'key derivation failed: %s' % e


def resolve_default_mifare_keys():
     """Return standard MIFARE Classic factory Key-A for all 16 sectors."""
     return [b'\xff\xff\xff\xff\xff\xff'] * 16


def resolve_creality_key_b(gate, tag):
    """Derive the Creality CFS/K1/K2 sector-1 MIFARE Key B for this tag's UID.

    Returns (key, None) on success, (None, reason_str) on failure. This key
    only unlocks sector 1 (blocks 4-6); Bambu's Key A and the MIFARE factory
    default key both fail on genuine Creality tags, so this is tried last.
    """
    try:
        from .mmu_nfc_tag_parser import _creality_derive_key_b
        uid_bytes = bytes((tag.target_info or {}).get('uid_bytes') or [])
        if len(uid_bytes) not in (4, 7):
            return None, ('uid_bytes wrong length for Creality key '
                          'derivation (%d bytes)' % len(uid_bytes))
        return _creality_derive_key_b(uid_bytes), None
    except ImportError as e:
        return None, 'pycryptodome not installed: %s' % e
    except Exception as e:
        return None, 'key derivation failed: %s' % e


def _retarget_same_uid(gate, uid_hex):
    """Re-select the reader target, returning True only if it's still the same tag.

    Each capture_mifare_metadata() call releases the target when it finishes,
    so successive authenticated-read attempts (different key/sector
    combinations) on one tag presence need to re-select it in between.
    """
    new_target = gate._reader.read_target()
    return new_target is not None and new_target.get('uid') == uid_hex


def capture_mifare_metadata(gate, tag, sector_keys,
                            sectors=(0, 1, 2, 3, 4), use_key_b=False):
    uid_hex   = tag.uid
    uid_bytes = bytes((tag.target_info or {}).get('uid_bytes') or [])
    tag.mifare_auth_failed_sectors = []
    tag.mifare_read_failed_blocks = []
    try:
        block_dict = gate._reader.mifare_read_authenticated_blocks(
            sector_keys, sectors=list(sectors), uid_bytes=uid_bytes,
            use_key_b=use_key_b)
    except Exception as e:
        tag.parse_error = 'mifare read failed: %s' % e
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        tag.read_retry_reason = tag.parse_error
        logger.warning(
            "[%s]: gate %d — uid=%s  MIFARE read failed: %s",
            gate._name, gate._gate, uid_hex, e)
        return
    if isinstance(block_dict, dict):
        tag.mifare_auth_failed_sectors = list(
            block_dict.get('auth_failed_sectors') or [])
        tag.mifare_read_failed_blocks = list(
            block_dict.get('read_failed_blocks') or [])
        if tag.mifare_auth_failed_sectors:
            tag.read_incomplete = True
            tag.read_retry_reason = (
                "auth failed sectors %s" %
                tag.mifare_auth_failed_sectors)
        elif tag.mifare_read_failed_blocks:
            tag.read_incomplete = True
            tag.read_retry_reason = (
                "read failed blocks %s" %
                tag.mifare_read_failed_blocks)
    if not block_dict or not block_dict.get('blocks'):
        tag.parse_error = 'mifare read returned no blocks'
        tag.meta = {'uid': uid_hex}
        tag.read_incomplete = True
        if not tag.read_retry_reason:
            tag.read_retry_reason = tag.parse_error
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  MIFARE read returned no "
                "blocks (auth failed on all sectors?)",
                gate._name, gate._gate, uid_hex)
        return
    tag.raw_tag_data = block_dict
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  MIFARE read %d blocks",
            gate._name, gate._gate, uid_hex, len(block_dict['blocks']))
    parse_current_tag(gate, tag)


# ── Tag read entry point ──────────────────────────────────────────────────────

def _target_scan_timeout(gate):
    if getattr(gate, '_scan_motion_mode', 'stopped') != 'continuous':
        return None
    if (getattr(gate, '_scan_continuous_pending_uid', None) is None
            and getattr(gate, '_scan_decode_retry_uid', None) is None):
        return None
    return getattr(gate, '_scan_continuous_poll_interval', None)


def read_current_tag(gate):
    if not gate._tag_parsing:
        return gate._reader.read_tag()

    target_info = gate._reader.read_target(timeout=_target_scan_timeout(gate))
    if target_info is None:
        return None

    uid_hex = target_info.get('uid')
    if not uid_hex:
        release_reader_target(gate, "missing_uid")
        return None

    tag = CurrentTag(uid=uid_hex, target_info=dict(target_info))
    tag.meta = {'uid': uid_hex}
    gate._state.current_tag = tag

    early_spool_id = resolve_spool_by_uid_before_metadata(gate, tag)
    if (early_spool_id is not None
            and not (getattr(gate, '_scan_mode', False)
                     and getattr(gate, '_tag_parsing', False))):
        return uid_hex

    strategy = classify_tag_target(gate, target_info)
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  target strategy=%s "
            "SAK=0x%02X ATQA=0x%04X",
            gate._name, gate._gate, uid_hex, strategy,
            int(target_info.get('sak', 0) or 0),
            int(target_info.get('atqa', target_info.get('sens_res', 0)) or 0))

    if strategy == 'ntag_type2':
        capture_ntag_metadata(gate, tag)
    elif strategy == 'iso15693_type5':
        capture_iso15693_metadata(gate, tag)
    elif strategy == 'mifare_classic':
        if not gate._bambu_reads:
            tag.parse_error = 'mifare_classic rich read disabled; uid-only fallback'
            release_reader_target(gate, "mifare_disabled")
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  MIFARE Classic "
                    "target seen but bambu_reads is disabled; UID-only fallback",
                    gate._name, gate._gate, uid_hex)
            return uid_hex
        keys, reason = resolve_auth_keys(gate, tag)
        bambu_read_worked = False
        if keys is None:
            tag.parse_error = 'mifare auth key derivation failed: %s' % reason
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  MIFARE key "
                    "derivation failed: %s; trying default key",
                    gate._name, gate._gate, uid_hex, reason)
        else:
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  MIFARE Classic "
                    "Bambu keys derived; reading sectors 0-4",
                    gate._name, gate._gate, uid_hex)
            capture_mifare_metadata(gate, tag, keys)
            bambu_read_worked = len(
                getattr(tag, 'mifare_auth_failed_sectors', [])) < 5

        if not bambu_read_worked:
            # Not a Bambu tag (or pycryptodome/derivation unavailable) --
            # retry with the plain MIFARE Classic factory default key.
            # Confirmed correct for QIDI Box (community-sourced from
            # BoxRFID-Touch, which authenticates block 4 with an all-0xFF
            # Key A). Creality CFS/K1/K2 uses a UID-derived Key B instead and
            # will fail here too; that's handled by the Key B retry below.
            tag.read_incomplete = False
            tag.read_retry_reason = None
            tag.raw_tag_data = None
            tag.parse_error = None
            default_key_worked = False
            if _retarget_same_uid(gate, uid_hex):
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — uid=%s  MIFARE Classic "
                        "retrying with default key",
                        gate._name, gate._gate, uid_hex)
                capture_mifare_metadata(
                    gate, tag, resolve_default_mifare_keys())
                default_key_worked = not getattr(
                    tag, 'mifare_auth_failed_sectors', None)
            else:
                release_reader_target(gate, "mifare_default_key_retarget_failed")
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — uid=%s  MIFARE Classic "
                        "default-key retry could not re-select the tag; "
                        "UID-only fallback",
                        gate._name, gate._gate, uid_hex)

            if not default_key_worked:
                # Not QIDI Box either -- try Creality CFS/K1/K2's UID-derived
                # Key B against sector 1 only (the only sector it unlocks).
                creality_key, creality_reason = resolve_creality_key_b(gate, tag)
                if creality_key is None:
                    if gate._debug >= 3:
                        logger.info(
                            "[%s]: gate %d — uid=%s  Creality Key B "
                            "derivation unavailable: %s; UID-only fallback",
                            gate._name, gate._gate, uid_hex, creality_reason)
                else:
                    tag.read_incomplete = False
                    tag.read_retry_reason = None
                    tag.raw_tag_data = None
                    tag.parse_error = None
                    if _retarget_same_uid(gate, uid_hex):
                        if gate._debug >= 3:
                            logger.info(
                                "[%s]: gate %d — uid=%s  MIFARE Classic "
                                "retrying sector 1 with Creality Key B",
                                gate._name, gate._gate, uid_hex)
                        creality_keys = [None] * 16
                        creality_keys[1] = creality_key
                        capture_mifare_metadata(
                            gate, tag, creality_keys, sectors=(1,),
                            use_key_b=True)
                    else:
                        release_reader_target(
                            gate, "mifare_creality_key_retarget_failed")
                        if gate._debug >= 3:
                            logger.info(
                                "[%s]: gate %d — uid=%s  MIFARE Classic "
                                "Creality Key B retry could not re-select "
                                "the tag; UID-only fallback",
                                gate._name, gate._gate, uid_hex)
    else:
        tag.parse_error = 'unsupported target; uid-only fallback'
        release_reader_target(gate, "unsupported_uid_only_fallback")
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  unsupported target; "
                "UID-only fallback", gate._name, gate._gate, uid_hex)

    return uid_hex


# ── Spool resolution ladder ───────────────────────────────────────────────────

def resolve_spool(gate, uid_hex):
    if uid_hex is None:
        return None
    tag = gate._state.current_tag
    if tag is not None and tag.uid != uid_hex:
        tag = None
    meta = {}
    if gate._tag_parsing and tag is not None and isinstance(tag.meta, dict):
        meta = tag.meta
    material = str(meta.get('material') or meta.get('type') or '').strip()
    color    = str(meta.get('color_hex') or meta.get('color') or '').strip()

    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  resolve_spool begin "
            "metadata=%s tag_parse_error=%s",
            gate._name, gate._gate, uid_hex, _summarize_meta(meta),
            getattr(tag, 'parse_error', None) if tag is not None else None)

    if (gate._spoolman is None and tag is not None
            and getattr(tag, 'read_incomplete', False)):
        if tag.resolution is None or not isinstance(tag.resolution, dict):
            tag.resolution = {'path': 'structured_read_incomplete'}
        else:
            tag.resolution = dict(tag.resolution)
            tag.resolution['path'] = 'structured_read_incomplete'
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  structured tag read "
                "is incomplete; deferring metadata assignment until "
                "scan-jog finds a complete read window",
                gate._name, gate._gate, uid_hex)
        return None

    if gate._spoolman is None:
        identity = (
            getattr(tag, 'spool_identity', None) if tag is not None else None)
        if (getattr(gate, '_scan_mode', False)
                and _left_neighbor_identity_match(gate, identity)):
            if tag is not None:
                tag.resolution = {
                    'path': 'left_neighbor_spool_identity',
                    'spool_identity': identity,
                }
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  spool_identity=%s matches "
                    "left neighbor; deferring metadata-direct assignment "
                    "to scan-jog interference handling",
                    gate._name, gate._gate, uid_hex, identity)
            return None
        if material or color:
            if tag is not None:
                tag.resolution = {'path': 'metadata_direct'}
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  no Spoolman; "
                    "using tag metadata material=%s color=%s",
                    gate._name, gate._gate, uid_hex, material, color)
            return DIRECT_METADATA_SPOOL
        if gate._debug >= 3:
            logger.info("[%s]: gate %d — uid=%s  no Spoolman configured",
                        gate._name, gate._gate, uid_hex)
        return None

    if tag is not None and isinstance(tag.resolution, dict):
        if tag.resolution.get('path') == 'early_uid_lookup':
            spool_id = tag.resolution.get('spool_id')
            if spool_id is not None:
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — uid=%s  "
                        "Spoolman→spool_id=%s (early UID lookup)",
                        gate._name, gate._gate, uid_hex, spool_id)
                return spool_id

    spoolman_id = meta.get('spoolman_id')
    if spoolman_id not in (None, ''):
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  resolution step: "
                "checking embedded spoolman_id=%r",
                gate._name, gate._gate, uid_hex, spoolman_id)
        try:
            spoolman_id = int(spoolman_id)
        except (TypeError, ValueError):
            spoolman_id = None
            logger.warning(
                "[%s]: gate %d — uid=%s  invalid embedded "
                "spoolman_id=%r; falling back to UID lookup",
                gate._name, gate._gate, uid_hex, meta.get('spoolman_id'))
        if spoolman_id is not None:
            spool = gate._spoolman.lookup_spool_by_id(spoolman_id)
            if spool:
                raw_id = spool.get('id', spoolman_id)
                try:
                    resolved_id = int(raw_id)
                except (TypeError, ValueError):
                    resolved_id = spoolman_id
                if tag is not None:
                    tag.resolution = {'path': 'embedded_spoolman_id',
                                      'spool_id': resolved_id}
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — uid=%s  "
                        "embedded spoolman_id=%s resolved",
                        gate._name, gate._gate, uid_hex, resolved_id)
                return resolved_id
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  "
                    "embedded spoolman_id=%s not found; falling back",
                    gate._name, gate._gate, uid_hex, spoolman_id)

    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  resolution step: "
            "checking Spoolman extra UID field %s",
            gate._name, gate._gate, uid_hex, gate._spoolman._rfid_key)
    spool_id = gate._spoolman.lookup_spool_by_uid(uid_hex)
    if spool_id is not None:
        if tag is not None:
            tag.resolution = {'path': 'uid_lookup', 'spool_id': spool_id}
        if gate._debug >= 3:
            logger.info("[%s]: gate %d — uid=%s  Spoolman→spool_id=%s",
                        gate._name, gate._gate, uid_hex, spool_id)
        return spool_id
    if gate._debug >= 3:
        logger.info(
            "[%s]: gate %d — uid=%s  UID lookup found no spool; "
            "checking whether metadata can create or directly represent a spool "
            "(material=%r color=%r)",
            gate._name, gate._gate, uid_hex, material, color)

    identity = getattr(tag, 'spool_identity', None) if tag is not None else None
    if (getattr(gate, '_scan_mode', False)
            and _left_neighbor_identity_match(gate, identity)):
        if tag is not None:
            tag.resolution = {
                'path': 'left_neighbor_spool_identity',
                'spool_identity': identity,
            }
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  spool_identity=%s matches left "
                "neighbor; deferring spool creation/resolution to "
                "scan-jog interference handling",
                gate._name, gate._gate, uid_hex, identity)
        return None

    if tag is not None and getattr(tag, 'read_incomplete', False):
        if tag.resolution is None or not isinstance(tag.resolution, dict):
            tag.resolution = {'path': 'structured_read_incomplete'}
        else:
            tag.resolution = dict(tag.resolution)
            tag.resolution['path'] = 'structured_read_incomplete'
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  structured tag read "
                "is incomplete; deferring auto-create/metadata assignment "
                "until scan-jog finds a complete read window",
                gate._name, gate._gate, uid_hex)
        return None

    try:
        base_url = gate._spoolman._resolve_base_url()
    except Exception as e:
        base_url = None
        logger.warning(
            "[%s]: gate %d — uid=%s  Spoolman URL resolution failed: %s",
            gate._name, gate._gate, uid_hex, e)
    if not base_url and (material or color):
        if tag is not None:
            tag.resolution = {'path': 'metadata_direct'}
        if gate._debug >= 3:
            logger.info(
                "[%s]: gate %d — uid=%s  Spoolman disabled "
                "or undiscovered; using tag metadata material=%s color=%s",
                gate._name, gate._gate, uid_hex, material, color)
        return DIRECT_METADATA_SPOOL

    if gate._spoolman_auto_create and material:
        if base_url:
            try:
                from .mmu_nfc_lameandboard_spoolman import (
                    SpoolmanClient as LBSpoolmanClient)
                if _accepts_kwarg(LBSpoolmanClient, 'trace'):
                    lb = LBSpoolmanClient(
                        base_url=base_url,
                        timeout=gate._spoolman._timeout,
                        trace=_trace_for_gate(
                            gate,
                            "[%s]: gate %d — uid=%s  " %
                            (gate._name, gate._gate, uid_hex)))
                else:
                    lb = LBSpoolmanClient(base_url=base_url,
                                          timeout=gate._spoolman._timeout)
                if gate._debug >= 3:
                    logger.info(
                        "[%s]: gate %d — uid=%s  "
                        "auto-create via lameandboard client "
                        "(uid_hex=None; patching %s next) metadata=%s",
                        gate._name, gate._gate, uid_hex,
                        gate._spoolman._rfid_key, _summarize_meta(meta))
                if getattr(gate, '_shared', False):
                    play_creating = getattr(
                        gate, '_shared_play_auto_create_effect', None)
                    if play_creating is not None:
                        play_creating()
                else:
                    _lane_led_effect(gate,
                        getattr(gate, '_lane_auto_create_effect', LED_AUTO_CREATING))
                new_spool_id = lb.auto_create_spool(meta, uid_hex=None)
                if new_spool_id is not None:
                    new_spool_id = int(new_spool_id)
                    if gate._debug >= 3:
                        logger.info(
                            "[%s]: gate %d — uid=%s  "
                            "auto-created Spoolman spool_id=%s; patching extra[%s]",
                            gate._name, gate._gate, uid_hex, new_spool_id,
                            gate._spoolman._rfid_key)
                    if not gate._spoolman.set_spool_uid(new_spool_id, uid_hex):
                        if tag is not None:
                            tag.resolution = {
                                'path': 'auto_create_uid_patch_failed',
                                'spool_id': new_spool_id,
                            }
                        logger.warning(
                            "[%s]: gate %d — uid=%s  "
                            "auto-created Spoolman spool_id=%s but "
                            "failed to patch extra[%s]; treating as "
                            "unresolved so the next read does not lose "
                            "the UID link",
                            gate._name, gate._gate, uid_hex,
                            new_spool_id, gate._spoolman._rfid_key)
                        return None
                    gate._spoolman.clear_cache()
                    if tag is not None:
                        tag.resolution = {'path': 'auto_create',
                                          'spool_id': new_spool_id}
                    if gate._debug >= 3:
                        logger.info(
                            "[%s]: gate %d — uid=%s  "
                            "auto-created Spoolman spool_id=%s and patched extra[%s]",
                            gate._name, gate._gate, uid_hex, new_spool_id,
                            gate._spoolman._rfid_key)
                    return new_spool_id
                logger.warning(
                    "[%s]: gate %d — uid=%s  auto-create returned no spool_id",
                    gate._name, gate._gate, uid_hex)
            except Exception as e:
                logger.warning(
                    "[%s]: gate %d — uid=%s  Spoolman auto-create failed: %s",
                    gate._name, gate._gate, uid_hex, e)
        elif material or color:
            if tag is not None:
                tag.resolution = {'path': 'metadata_direct'}
            if gate._debug >= 3:
                logger.info(
                    "[%s]: gate %d — uid=%s  Spoolman unavailable; "
                    "using tag metadata material=%s color=%s",
                    gate._name, gate._gate, uid_hex, material, color)
            return DIRECT_METADATA_SPOOL
    elif gate._debug >= 3:
        if not gate._spoolman_auto_create:
            reason = 'spoolman_auto_create disabled'
        elif not material:
            reason = 'tag metadata has no material'
        else:
            reason = 'unknown'
        logger.info(
            "[%s]: gate %d — uid=%s  auto-create skipped: %s",
            gate._name, gate._gate, uid_hex, reason)

    if tag is not None:
        tag.resolution = {'path': 'unresolved'}
    if gate._debug >= 3:
        logger.info("[%s]: gate %d — uid=%s  Spoolman→spool_id=None",
                    gate._name, gate._gate, uid_hex)
    if not getattr(gate, '_shared', False):
        _lane_led_effect(gate,
            getattr(gate, '_lane_unresolved_effect', LED_UNRESOLVED))
    return None
