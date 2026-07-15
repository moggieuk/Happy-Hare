# klippy/extras/mmu/unit/nfc/pn532_driver.py
#
# EMU NFC Gate Reader — PN532 I2C driver
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
# PN532 NFC reader driver — I2C and SPI variants.
#
# init(), is_alive(), and read_tag() are the public reader interface used by
# NFCGate.
#
# Integration model
# ─────────────────
# This driver uses UID lookup: it reads only the tag's factory
# UID — the simplest possible NFC operation.  No data is ever written to the
# tag.  The UID is passed up to the gate manager, which queries the Spoolman
# API to resolve it to a spool ID.  Tags can be blank NTAG stickers straight
# from the packet.
#
# Driver responsibility boundary
# ──────────────────────────────
# PN532Driver / PN532SPIDriver own only the hardware protocol:
#
#   - wake and initialise the PN532
#   - build and validate PN532 frames
#   - send InListPassiveTarget and parse tag identity fields
#   - release/deselect targets after reads
#   - optionally expose raw NTAG page-read primitives in the future
#
# The driver returns hardware facts only, such as UID, target number, ATQA /
# SENS_RES, SAK, and raw bytes if page-read support is added.  It must not
# interpret tag payloads as spool IDs, know about lanes/gates, query or write
# Spoolman, or issue Happy Hare commands.
#
# NFCGate owns the application state.  It receives UID/tag identity from this
# driver, asks SpoolmanClient to resolve UID →
# spool record / spool_id, debounces changed/removed states, and dispatches
# all Happy Hare-facing commands (MMU_GATE_MAP and MMU_SPOOLMAN).  This keeps
# Happy Hare as the source of truth for gate maps and Spoolman sync.
#
# Why PN532 for I2C?
# ─────────────────
# The PN532 implements the full ISO14443A stack in hardware.  One
# InListPassiveTarget command hands back the tag UID — no manual REQA /
# ANTICOLL / SELECT sequence required.  One InRelease cleans up and keeps
# CAN bus traffic low.
#
# PN532 I2C protocol overview
# ───────────────────────────
# All communication uses length-framed packets with checksums:
#
#   Write frame:  [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, params..., DCS, 0x00]
#   Read  frame:  [STATUS, 0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, data..., DCS, 0x00]
#
#   STATUS byte (first byte of every I2C read):
#     0x01 = ready     (response is in the buffer)
#     0x00 = busy      (PN532 still processing)
#
#   LEN  = number of bytes in the data field (TFI + CMD + payload)
#   LCS  = (-LEN) & 0xFF   (LEN + LCS = 0 mod 256)
#   TFI  = 0xD4 host→PN532 / 0xD5 PN532→host
#   DCS  = (-sum(data_field)) & 0xFF
#
# Since we cannot use the IRQ pin directly from a Klipper reactor callback
# (it would require a custom MCU command), we use fixed time.sleep() delays:
#
#   transceive_delay  maps to InListPassiveTarget wait (250 ms default).
#     The PN532 scans until a tag is found or its internal timer expires.
#     250 ms covers the no-tag timeout safely.
#   crc_delay         maps to InRelease wait (50 ms default).
#     Deselect is fast; 50 ms is very conservative.
#
# I2C address and wiring
# ──────────────────────
# The PN532 I2C address is fixed at 0x24 (decimal 36) by the chip — it cannot
# be changed.  The two pads/jumpers on the breakout board (SEL0/SEL1, sometimes
# labeled A0/A1) select the communication protocol, not the address:
#
#   SEL0=1, SEL1=0 → I2C  (address fixed at 0x24)
#   SEL0=0, SEL1=0 → SPI
#   SEL0=0, SEL1=1 → HSU/UART
#
# EBB42 v1.x I2C1 pins: SCL = PB6, SDA = PB7.
#
# Threading notes
# ───────────────
# All methods are called from the background polling thread.  i2c_write() and
# i2c_read() block that thread waiting for CAN round-trips; the Klipper
# reactor thread continues normally.

import time
import traceback

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# PN532 frame constants
# ─────────────────────────────────────────────────────────────────────────────

_TFI_HOST_TO_PN532 = 0xD4
_TFI_PN532_TO_HOST = 0xD5

# PN532 command codes, mirrored from HH_code/pn532.py.
PN532_COMMAND_GETFIRMWAREVERSION = 0x02
PN532_COMMAND_SAMCONFIGURATION = 0x14
PN532_COMMAND_RFCONFIGURATION = 0x32
PN532_COMMAND_INLISTPASSIVETARGET = 0x4A
PN532_COMMAND_INDATAEXCHANGE = 0x40
PN532_COMMAND_INRELEASE = 0x52

# MIFARE/NTAG commands, mirrored from HH_code/pn532.py.
MIFARE_CMD_READ = 0x30
MIFARE_ULTRALIGHT_CMD_WRITE = 0xA2
MIFARE_CMD_AUTH_A = 0x60
MIFARE_CMD_AUTH_B = 0x61

PN532_ACK = [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]

# Internal aliases retained for the existing driver implementation.
_CMD_GETFIRMWAREVERSION = PN532_COMMAND_GETFIRMWAREVERSION
_CMD_SAMCONFIGURATION = PN532_COMMAND_SAMCONFIGURATION
_CMD_RFCONFIGURATION = PN532_COMMAND_RFCONFIGURATION
_CMD_INLISTPASSIVETARGET = PN532_COMMAND_INLISTPASSIVETARGET
_CMD_INDATAEXCHANGE = PN532_COMMAND_INDATAEXCHANGE
_CMD_INRELEASE = PN532_COMMAND_INRELEASE

# InListPassiveTarget baud-rate/type codes
_BRTY_ISO14443A_106KBPS  = 0x00   # Standard NFC Type A — covers NTAG and Mifare

# Byte offsets inside a parsed I2C read buffer
# [STATUS, 0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, payload...]
_OFF_STATUS  = 0
_OFF_LEN     = 4
_OFF_TFI     = 6
_OFF_CMD     = 7
_OFF_PAYLOAD = 8

# Maximum bytes to read for any PN532 response (covers all commands used here)
_MAX_RESPONSE_BYTES = 32


def _hex(data, sep=''):
    return sep.join('%02X' % b for b in data)


def _parse_inlist_payload(payload):
    """
    Parse the payload from an InListPassiveTarget response.

    Returns a dictionary containing hardware/protocol facts only, or None if
    no target is present or the frame is malformed.
    """
    if not payload or payload[0] == 0:
        return None
    if len(payload) < 7:
        return None

    tg = payload[1]
    sens_res_bytes = list(payload[2:4])
    sak = payload[4]
    uid_len = payload[5]
    if uid_len == 0 or uid_len > 10 or len(payload) < 6 + uid_len:
        return None

    uid_bytes = list(payload[6:6 + uid_len])
    sens_res = (sens_res_bytes[0] << 8) | sens_res_bytes[1]
    return {
        'target': tg,
        'tg': tg,
        'sens_res': sens_res,
        'atqa': sens_res,
        'sens_res_bytes': sens_res_bytes,
        'sak': sak,
        'uid_length': uid_len,
        'uid_bytes': uid_bytes,
        'uid': _hex(uid_bytes),
    }


# =============================================================================
# _PN532Base — shared PN532 protocol logic
# =============================================================================
#
# All PN532 command logic lives here.  Subclasses provide only the transport
# layer (_send, _read_ack, _recv, _check_frame) and transport-specific
# low-level debug helpers.
#
# Subclass contract
# ─────────────────
# Before calling super().__init__(), each subclass must set:
#   self._transport_name  — short label used in log messages ('PN532' / 'PN532 SPI')
#
# After super().__init__(), the subclass transport handle is already stored
# (self._i2c or self._spi) so any transport method can use it immediately.

class _PN532Base:

    def __init__(self, gate, transceive_delay, crc_delay, debug, low_level_debug,
                 sleep_fn=None):
        self._gate           = gate
        self._scan_delay     = transceive_delay   # InListPassiveTarget wait
        self._release_delay  = crc_delay          # InRelease wait
        self._debug          = debug
        self._low_level_debug = low_level_debug
        self._sleep          = sleep_fn if sleep_fn is not None else time.sleep
        self._clear_current_card()

    # ─────────────────────────────────────────────────────────────────────────
    # Frame construction (transport-agnostic)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_frame(cmd_and_params):
        """
        Build a complete PN532 host-to-chip command frame.

        Parameters
        ----------
        cmd_and_params : list of int
            The command byte followed by any parameters.
            TFI (0xD4) is prepended automatically.

        Returns
        -------
        list of int
            The full frame: preamble + start + LEN + LCS + TFI + data + DCS + postamble.
        """
        data = [_TFI_HOST_TO_PN532] + list(cmd_and_params)
        length = len(data)
        lcs    = (-length) & 0xFF
        dcs    = (-sum(data)) & 0xFF
        return [0x00, 0x00, 0xFF, length, lcs] + data + [dcs, 0x00]

    def _transceive(self, cmd_and_params, expected_cmd_resp,
                    read_len=_MAX_RESPONSE_BYTES, timeout=1.0):
        """Send a command frame and return the parsed response payload."""
        self._send(cmd_and_params)
        if not self._read_ack(timeout=min(max(timeout, 0.050), 1.000)):
            if self._debug >= 3:
                logger.info("_transceive: gate %d (%s) no valid ACK for "
                            "cmd=0x%02X", self._gate, self._transport_name,
                            cmd_and_params[0])
            return None
        return self._recv(expected_cmd_resp, read_len=read_len, timeout=timeout)

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level debug helpers (transport-agnostic portion)
    # ─────────────────────────────────────────────────────────────────────────

    def _require_low_level_debug(self):
        if not self._low_level_debug:
            raise RuntimeError("PN532 low_level_debug is disabled")

    def low_level_command_frame(self, cmd_and_params):
        """Build a PN532 command frame for manual RAW_WRITE use."""
        self._require_low_level_debug()
        if not isinstance(cmd_and_params, list):
            cmd_and_params = [cmd_and_params]
        return self._build_frame(cmd_and_params)

    # ─────────────────────────────────────────────────────────────────────────
    # Target state helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _clear_current_card(self):
        """Clear cached target/card information."""
        self.current_target = None
        self.current_uid = None
        self.current_uid_hex = ''
        self.current_target_info = None

    def _set_current_card(self, target_info):
        """Cache the currently selected target information."""
        self.current_target_info = dict(target_info)
        self.current_target = target_info.get('target')
        self.current_uid = list(target_info.get('uid_bytes') or [])
        self.current_uid_hex = target_info.get('uid', _hex(self.current_uid))
        if self._debug >= 4:
            logger.debug(
                "_set_current_card: gate %d (%s) Tg=%s UID=%s "
                "SENS_RES=0x%04X SAK=0x%02X",
                self._gate, self._transport_name,
                self.current_target, self.current_uid_hex,
                target_info.get('sens_res', 0),
                target_info.get('sak', 0))

    def _release_current_target(self, reason="manual"):
        """
        Send InRelease to deselect the active target, then clear cached state.

        If no target is cached, release all targets (Tg=0x00).  That preserves
        the old driver behavior and gives the next scan a clean RF state.
        """
        target = self.current_target
        release_tg = target if target is not None else 0x00
        try:
            if self._debug >= 4:
                logger.debug(
                    "_release_current_target: gate %d (%s) Tg=0x%02X "
                    "reason=%s", self._gate, self._transport_name,
                    release_tg, reason)
            payload = self._transceive([_CMD_INRELEASE, release_tg], 0x53,
                                       read_len=12,
                                       timeout=max(self._release_delay, 0.200))
            if self._debug >= 4:
                if payload is None:
                    logger.debug("_release_current_target: gate %d (%s) "
                                 "no response (non-fatal)",
                                 self._gate, self._transport_name)
                else:
                    status = payload[0] if payload else 0xFF
                    logger.debug("_release_current_target: gate %d (%s) "
                                 "status=0x%02X",
                                 self._gate, self._transport_name, status)
            return payload is not None
        except Exception as e:
            if self._debug >= 4:
                logger.debug("_release_current_target: gate %d (%s) "
                             "error: %s\n%s", self._gate, self._transport_name,
                             e, traceback.format_exc())
            return False
        finally:
            self._clear_current_card()

    # ─────────────────────────────────────────────────────────────────────────
    # PN532 command helpers ported from HH_code/pn532.py
    # ─────────────────────────────────────────────────────────────────────────

    def get_firmware_version(self):
        """
        Return PN532 firmware metadata, or None if the chip does not respond.

        The returned dictionary is intentionally protocol-only; callers can
        format it however they want.
        """
        payload = self._transceive([_CMD_GETFIRMWAREVERSION], 0x03,
                                   read_len=15, timeout=0.500)
        if payload is None or len(payload) < 4:
            return None
        return {
            'ic': payload[0],
            'version': payload[1],
            'revision': payload[2],
            'support': payload[3],
            'text': 'v%d.%d (IC: 0x%02X, Support: 0x%02X)' %
                    (payload[1], payload[2], payload[0], payload[3]),
        }

    def sam_config(self, timeout=0x00, irq=0x00):
        """
        Configure the PN532 SAM in normal mode.

        HH_code uses timeout=0x14 and irq=0x01.  This driver defaults to the
        previous MMU NFC values because Klipper is polling readiness instead
        of consuming the IRQ pin directly.
        """
        payload = self._transceive([_CMD_SAMCONFIGURATION, 0x01,
                                    timeout & 0xFF, irq & 0xFF],
                                   0x15, read_len=12, timeout=0.200)
        return payload is not None

    def rf_config(self, enable=True):
        """Enable or disable the RF field using PN532 RFConfiguration."""
        payload = self._transceive([_CMD_RFCONFIGURATION, 0x01,
                                    0x01 if enable else 0x00],
                                   0x33, read_len=12, timeout=0.200)
        return payload is not None

    def read_target(self, timeout=None):
        """
        Scan for one ISO14443A target and return structured hardware details.

        Returns a dictionary with UID, target number, SENS_RES/ATQA, SAK, and
        UID bytes.  Returns None on no tag or communication error.
        """
        if timeout is None:
            timeout = self._scan_delay + 0.100
        if self._debug >= 4:
            logger.debug("read_target: gate %d (%s) scanning (timeout=%.3fs)",
                         self._gate, self._transport_name, timeout)

        payload = self._transceive([_CMD_INLISTPASSIVETARGET, 0x01,
                                    _BRTY_ISO14443A_106KBPS],
                                   0x4B, read_len=_MAX_RESPONSE_BYTES,
                                   timeout=timeout)
        target_info = _parse_inlist_payload(payload)
        if target_info is None:
            self._clear_current_card()
            if self._debug >= 4:
                logger.debug("read_target: gate %d (%s) no tag",
                             self._gate, self._transport_name)
            return None

        self._set_current_card(target_info)
        if self._debug >= 4:
            logger.debug(
                "read_target: gate %d (%s) Tg=%d SENS_RES=0x%04X "
                "SAK=0x%02X UIDLen=%d UID=%s",
                self._gate, self._transport_name,
                target_info['target'], target_info['sens_res'],
                target_info['sak'], target_info['uid_length'],
                target_info['uid'])
        return target_info

    def read_passive_target_id(self, timeout=1.0):
        """
        HH-compatible target read helper.

        Returns (True, uid_bytes) on success or (False, None) otherwise.
        """
        target_info = self.read_target(timeout=timeout)
        if target_info is None:
            return False, None
        return True, list(target_info['uid_bytes'])

    def ntag_read_page(self, page):
        """
        Read four NTAG pages (16 bytes) starting at *page*.

        This is a raw hardware primitive only.  It does not parse NDEF,
        Spoolman IDs, JSON, or any other application payload.
        """
        if page < 0 or page > 255:
            raise ValueError("PN532 NTAG page out of range: %s" % page)
        if self.current_target is None:
            target_info = self.read_target(timeout=0.500)
            if target_info is None:
                return None

        cmd = [_CMD_INDATAEXCHANGE, self.current_target, MIFARE_CMD_READ,
               page & 0xFF]
        payload = self._transceive(cmd, 0x41, read_len=_MAX_RESPONSE_BYTES,
                                   timeout=1.000)
        if not payload or payload[0] != 0x00:
            if self._debug >= 4 and payload:
                logger.debug("ntag_read_page: gate %d (%s) page=%d "
                             "status=0x%02X",
                             self._gate, self._transport_name,
                             page, payload[0])
            return None
        data = list(payload[1:17])
        if len(data) != 16:
            return None
        return data

    def robust_page_read(self, page, attempts=3):
        """
        Read an NTAG page block with target re-verification between attempts.

        Ported from HH_code/pn532.py, but kept as raw bytes only.
        """
        if self.current_target is None:
            if self.read_target(timeout=0.500) is None:
                return None
        expected_uid = list(self.current_uid or [])

        for attempt in range(attempts):
            page_data = self.ntag_read_page(page)
            if page_data:
                return page_data
            if attempt >= attempts - 1:
                break

            self._release_current_target(
                reason="page_%d_retry_%d" % (page, attempt + 1))
            self._sleep(0.025)
            target_info = self.read_target(timeout=0.500)
            if target_info is None:
                self._sleep(0.050)
                target_info = self.read_target(timeout=0.500)
                if target_info is None:
                    return None
            if expected_uid and target_info['uid_bytes'] != expected_uid:
                logger.warning("robust_page_read: gate %d (%s) different "
                            "tag detected during page %d retry",
                            self._gate, self._transport_name, page)
                self._release_current_target(reason="uid_changed")
                return None

        self._release_current_target(reason="page_%d_max_retries" % page)
        return None

    def ntag_read_user_memory(self, start_page=4, end_page=67):
        """
        Read raw NTAG user memory in 16-byte page blocks.

        Unlike HH_code/pn532.py, this method intentionally avoids NDEF or
        spool payload parsing.  It returns a bytearray of raw tag memory.
        """
        user_data = bytearray()
        try:
            current_page = start_page
            while current_page <= end_page:
                page_data = self.robust_page_read(current_page)
                if not page_data:
                    break
                remaining_pages = end_page - current_page + 1
                if remaining_pages >= 4:
                    user_data.extend(page_data)
                else:
                    user_data.extend(page_data[:remaining_pages * 4])
                current_page += 4
                self._sleep(0.005)
            return user_data
        finally:
            self._release_current_target(reason="user_memory_complete")

    @staticmethod
    def _ndef_tlv_extent(data):
        """Return (tlv_bytes, ndef_len) for the first complete/partial NDEF TLV."""
        i = 0
        data_len = len(data)
        while i < data_len:
            t = data[i]
            if t == 0x00:  # NULL TLV
                i += 1
                continue
            if t == 0xFE:  # Terminator TLV
                return None
            if i + 1 >= data_len:
                return None
            l = data[i + 1]
            if l == 0xFF:
                if i + 3 >= data_len:
                    return None
                l = (data[i + 2] << 8) | data[i + 3]
                value_start = i + 4
            else:
                value_start = i + 2
            value_end = value_start + l
            if t == 0x03:
                return value_end, l
            i = value_end
        return None

    def ntag_read_ndef_user_memory(self, start_page=4, max_pages=16,
                                   max_ndef_pages=135):
        """
        Read NTAG user memory, expanding to the NDEF TLV's advertised length.

        The first read grabs enough bytes to inspect the Type-2 TLV header.  If
        an NDEF TLV (0x03) is present, the advertised NDEF length determines how
        many user-memory pages are read.  max_pages is the fallback window for
        non-NDEF/binary formats; max_ndef_pages is a hard safety cap for NDEF.
        """
        max_pages = max(4, int(max_pages))
        max_ndef_pages = max(max_pages, int(max_ndef_pages))
        fallback_bytes = max_pages * 4
        max_ndef_bytes = max_ndef_pages * 4
        user_data = bytearray()
        target_bytes = min(16, fallback_bytes)
        ndef_len = None
        try:
            current_page = start_page
            while len(user_data) < target_bytes:
                page_data = self.robust_page_read(current_page)
                if not page_data:
                    break
                user_data.extend(page_data)

                extent = self._ndef_tlv_extent(user_data)
                if extent is not None:
                    target_bytes, ndef_len = extent
                    if target_bytes > max_ndef_bytes:
                        if self._debug >= 3:
                            logger.info(
                                "ntag_read_ndef_user_memory: gate %d (%s) "
                                "NDEF length=%d requires %d bytes; capped at "
                                "%d bytes by max_ndef_pages",
                                self._gate, self._transport_name, ndef_len,
                                target_bytes, max_ndef_bytes)
                        target_bytes = max_ndef_bytes
                elif len(user_data) >= 16:
                    # No NDEF TLV in the initial chunk. Preserve the older
                    # fixed-window behavior for binary/non-NDEF tag formats.
                    target_bytes = fallback_bytes

                current_page += 4
                if current_page > 255:
                    break
                self._sleep(0.005)

            result = user_data[:min(len(user_data), target_bytes)]
            if self._debug >= 4 and ndef_len is not None:
                logger.debug(
                    "ntag_read_ndef_user_memory: gate %d (%s) "
                    "NDEF length=%d read=%d bytes",
                    self._gate, self._transport_name, ndef_len, len(result))
            return result
        finally:
            self._release_current_target(reason="ndef_user_memory_complete")

    def mifare_authenticate(self, block_addr, key, use_key_b=False):
        """Authenticate a MIFARE Classic sector using InDataExchange.

        block_addr is any block in the target sector (typically the sector trailer).
        key is a 6-byte sequence (list or bytes).  Returns True on success.
        """
        if self.current_target is None:
            return False
        auth_cmd = MIFARE_CMD_AUTH_B if use_key_b else MIFARE_CMD_AUTH_A
        uid = list(self.current_uid or [])[:4]
        cmd = ([_CMD_INDATAEXCHANGE, self.current_target, auth_cmd,
                block_addr & 0xFF]
               + list(key)[:6] + uid)
        payload = self._transceive(cmd, 0x41, read_len=12, timeout=1.0)
        if not payload or payload[0] != 0x00:
            if self._debug >= 3 and payload:
                logger.info(
                    "mifare_authenticate: gate %d (%s) block=%d key_%s "
                    "status=0x%02X",
                    self._gate, self._transport_name, block_addr,
                    'B' if use_key_b else 'A', payload[0])
            return False
        return True

    def mifare_read_block(self, block_addr):
        """Read 16 bytes from a MIFARE Classic block (sector must be pre-authenticated).

        Returns bytes of length 16, or None on error.
        """
        if self.current_target is None:
            return None
        cmd = [_CMD_INDATAEXCHANGE, self.current_target,
               MIFARE_CMD_READ, block_addr & 0xFF]
        payload = self._transceive(cmd, 0x41,
                                   read_len=_MAX_RESPONSE_BYTES, timeout=1.0)
        if not payload or payload[0] != 0x00 or len(payload) < 17:
            return None
        return bytes(payload[1:17])

    def mifare_read_authenticated_blocks(self, sector_keys, sectors,
                                         uid_bytes=None, use_key_b=False):
        """Authenticate and read data blocks from the given sectors.

        sector_keys : list of 16 × 6-byte key values (index = sector number),
                      Key A by default or Key B when use_key_b is True.
        sectors     : list of sector numbers to read (e.g. [0, 1, 2, 3, 4]).
        uid_bytes   : tag UID bytes (4 bytes for MIFARE Classic 1K); stored in
                      the returned dict for parse_tag().
        use_key_b   : authenticate with Key B instead of Key A (e.g. Creality
                      CFS/K1/K2 sector 1, which uses a UID-derived Key B).

        Returns {"uid_bytes": bytes, "blocks": {abs_block_index: bytes}} where
        abs_block_index is the absolute block number (0-63 for MIFARE Classic 1K).
        Sector trailer blocks (4*s+3) are never included.  Failed sectors are
        reported as auth_failed_sectors/read_failed_blocks so callers can
        distinguish a clean partial decode from an incomplete rich read.

        Releases the target in a finally block (same pattern as ntag_read_user_memory).
        """
        blocks = {}
        auth_failed_sectors = []
        read_failed_blocks = []
        try:
            for sector in sectors:
                trailer = sector * 4 + 3
                key = sector_keys[sector] if sector < len(sector_keys) else None
                if key is None:
                    continue
                if not self.mifare_authenticate(trailer, key, use_key_b=use_key_b):
                    auth_failed_sectors.append(sector)
                    if self._debug >= 3:
                        logger.info(
                            "mifare_read_authenticated_blocks: gate %d (%s) "
                            "sector %d auth failed — skipping",
                            self._gate, self._transport_name, sector)
                    continue
                for blk_offset in range(3):
                    block_addr = sector * 4 + blk_offset
                    data = self.mifare_read_block(block_addr)
                    if data is not None:
                        blocks[block_addr] = data
                    else:
                        read_failed_blocks.append(block_addr)
                        if self._debug >= 3:
                            logger.info(
                                "mifare_read_authenticated_blocks: gate %d (%s) "
                                "block %d read failed",
                                self._gate, self._transport_name, block_addr)
            result = {"uid_bytes": bytes(uid_bytes or []), "blocks": blocks}
            if auth_failed_sectors:
                result["auth_failed_sectors"] = auth_failed_sectors
            if read_failed_blocks:
                result["read_failed_blocks"] = read_failed_blocks
            return result
        finally:
            self._release_current_target(reason="mifare_read_complete")

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation and lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _wake_pn532(self, attempts=3):
        """
        Wake the PN532 from power-save mode using GetFirmwareVersion.

        Uses a single i2c_write + i2c_read per attempt to avoid Klipper MCU
        command re-entrancy (two sequential i2c_read calls in one attempt
        caused recursive send → _do_send loops in mcu.py).

        First attempt waits 150 ms after TX (cold-start settling).
        Subsequent attempts wait 75 ms.  A 50 ms gap separates each attempt.

        Returns True if the chip responded, False if all attempts failed.
        """
        for attempt in range(attempts):
            if self._debug >= 4:
                logger.debug(
                    "_wake_pn532: gate %d (%s) attempt %d/%d — "
                    "sending GetFirmwareVersion",
                    self._gate, self._transport_name, attempt + 1, attempts)
            try:
                version = self.get_firmware_version()
                if version is not None:
                    logger.info(
                        "_wake_pn532: gate %d (%s) OK on attempt %d — "
                        "IC=0x%02X Ver=%d.%d",
                        self._gate, self._transport_name, attempt + 1,
                        version['ic'], version['version'], version['revision'])
                    return True
                if self._debug >= 4:
                    logger.debug(
                        "_wake_pn532: gate %d (%s) attempt %d — "
                        "no valid response",
                        self._gate, self._transport_name, attempt + 1)
            except Exception as e:
                if attempt == 0:
                    logger.debug(
                        "_wake_pn532: gate %d (%s) attempt %d failed: %s\n%s",
                        self._gate, self._transport_name, attempt + 1,
                        e, traceback.format_exc())
                else:
                    logger.info(
                        "_wake_pn532: gate %d (%s) attempt %d failed: %s\n%s",
                        self._gate, self._transport_name, attempt + 1,
                        e, traceback.format_exc())
            self._sleep(0.050)

        logger.warning("_wake_pn532: gate %d (%s) failed after %d attempts — "
                    "check wiring",
                    self._gate, self._transport_name, attempts)
        return False

    def init(self):
        """
        Wake the PN532 then configure it for ISO14443A normal operation.

        Sends GetFirmwareVersion (with retries) to bring the chip out of
        power-save, then SAMConfiguration (Normal mode, no SAM timeout,
        no IRQ output).  Must be called once after klippy:connect.

        Raises RuntimeError if the chip does not respond after retries.

        When low_level_debug is enabled the init sequence is skipped entirely.
        The reader is left in its power-on state so all I2C/SPI transactions
        can be driven manually from the Klipper console (RAW_WRITE, RAW_CMD,
        etc.) without any driver-initiated traffic interfering.
        """
        if self._low_level_debug:
            logger.info("init: gate %d (%s) low_level_debug enabled — "
                     "skipping wake and SAMConfiguration",
                     self._gate, self._transport_name)
            return

        if self._debug >= 4:
            logger.debug("init: gate %d (%s) starting wake sequence",
                         self._gate, self._transport_name)

        if not self._wake_pn532():
            raise RuntimeError(
                "PN532 gate %d (%s) did not respond — check wiring"
                % (self._gate, self._transport_name))

        if self._debug >= 4:
            logger.debug("init: gate %d (%s) sending SAMConfiguration "
                         "(Normal mode, timeout=0, no IRQ)",
                         self._gate, self._transport_name)

        # SAMConfiguration: Normal mode(0x01), timeout=0x00, IRQ=0x00.
        # See sam_config() for why the defaults differ from HH_code.
        if not self.sam_config():
            logger.warning("init: gate %d (%s) SAMConfiguration "
                        "no response — reader may be unstable",
                        self._gate, self._transport_name)
        elif self._debug >= 4:
            logger.debug("init: gate %d (%s) SAMConfiguration OK",
                         self._gate, self._transport_name)

    def is_alive(self):
        """
        Return True if the PN532 responds to GetFirmwareVersion.

        Kept for API compatibility with older reader drivers — callers should call
        init() first and check for RuntimeError rather than calling is_alive()
        standalone.
        """
        try:
            return self.get_firmware_version() is not None
        except Exception as e:
            logger.debug("is_alive: gate %d (%s) error: %s\n%s",
                         self._gate, self._transport_name,
                         e, traceback.format_exc())
            return False

    def read_tag(self, timeout=None):
        """
        Attempt to read the UID of any tag in the RF field.

        Uses InListPassiveTarget to let the PN532 handle REQA / ANTICOLL /
        SELECT internally, then InRelease to deselect so the next scan starts
        clean.  No data is read from tag memory.

        Parameters
        ----------
        timeout : float or None
            Override the scan timeout passed to read_target().  When None the
            driver default (transceive_delay + 0.100) is used.  Pass a shorter
            value for in-flight continuous probes to reduce blocking time.

        Returns
        -------
        str
            Tag UID as uppercase hex (8, 10, or 14 chars for 4-, 5-, 7-byte UIDs).
        None
            No tag in the RF field, or a communication error occurred.
        """
        try:
            target_info = self.read_target(timeout=timeout)
            if target_info is None:
                return None

            uid_hex = target_info['uid']
            self._release_current_target(reason="read_tag_complete")

            if self._debug >= 3:
                logger.debug("read_tag: gate %d (%s) uid=%s",
                             self._gate, self._transport_name, uid_hex)

            return uid_hex
        except Exception as e:
            if self._debug >= 3:
                logger.info("read_tag: gate %d (%s) error "
                            "(tag removed mid-scan?): %s\n%s",
                            self._gate, self._transport_name,
                            e, traceback.format_exc())
            return None


# =============================================================================
# PN532Driver — I2C transport
# =============================================================================

class PN532Driver(_PN532Base):
    """
    Driver for one PN532 NFC reader module connected via I2C.

    Reads only the tag UID.
    No data is read from tag memory; tags never need to be written to.

    Provides the reader interface used by NFCGate.

    Parameters
    ----------
    i2c : MCU_I2C
        A Klipper MCU_I2C object configured for this reader's I2C address.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used for logging.
    transceive_delay : float
        Seconds to wait after InListPassiveTarget before reading the result.
        The PN532 scans until a tag is found or its internal timer expires.
        250 ms is a safe default.
    crc_delay : float
        Seconds to wait after InRelease.
        50 ms is conservative; 20 ms usually works.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, i2c, gate,
                 transceive_delay=0.250,
                 crc_delay=0.050,
                 debug=1,
                 low_level_debug=False,
                 sleep_fn=None):
        self._i2c = i2c
        self._transport_name = 'PN532'
        super().__init__(gate, transceive_delay, crc_delay, debug, low_level_debug,
                         sleep_fn=sleep_fn)

    # ─────────────────────────────────────────────────────────────────────────
    # Frame parsing — I2C frames include a leading STATUS byte
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_frame(raw, expected_cmd_resp):
        """
        Validate a raw I2C read buffer and return the payload bytes.

        Parameters
        ----------
        raw : list / bytearray
            The full byte sequence returned by i2c_read(), including the
            leading STATUS byte.
        expected_cmd_resp : int
            The command-response code we expect at raw[_OFF_CMD].

        Returns
        -------
        list of int or None
            Payload bytes (after TFI and CMD_RESP), or None on any error.
        """
        if len(raw) < _OFF_PAYLOAD:
            return None
        if raw[_OFF_STATUS] != 0x01:              # PN532 not ready
            return None
        if raw[1] != 0x00 or raw[2] != 0x00 or raw[3] != 0xFF:
            return None                            # Corrupted start code
        if raw[_OFF_TFI] != _TFI_PN532_TO_HOST:
            return None
        if raw[_OFF_CMD] != expected_cmd_resp:
            return None
        length  = raw[_OFF_LEN]
        payload = list(raw[_OFF_PAYLOAD: _OFF_PAYLOAD + length - 2])
        return payload                             # Bytes after TFI and CMD_RESP

    # ─────────────────────────────────────────────────────────────────────────
    # I2C transport
    # ─────────────────────────────────────────────────────────────────────────

    def _send(self, cmd_and_params):
        """Write a command frame to the PN532."""
        frame = self._build_frame(cmd_and_params)
        if self._debug >= 4:
            logger.debug("_send: gate %d (PN532) TX  cmd=0x%02X  frame=%s",
                          self._gate, cmd_and_params[0],
                          ' '.join('%02X' % b for b in frame))
        self._i2c.i2c_write(frame)

    def _read_ack(self, timeout=1.0, poll_interval=0.005):
        """
        Wait for and validate the PN532 ACK frame after a command write.

        I2C ACK reads include the leading PN532 status byte, so the complete
        successful read is:
          [0x01, 0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ready_result = self._i2c.i2c_read([], 1)
                ready_raw = bytearray(ready_result['response'])
                status = ready_raw[0] if ready_raw else 0xFF
            except Exception as e:
                logger.error("_read_ack: gate %d (PN532) ready read failed: %s\n%s",
                          self._gate, e, traceback.format_exc())
                return False

            if self._debug >= 4:
                logger.debug("_read_ack: gate %d (PN532) ready=%s",
                             self._gate,
                             ' '.join('%02X' % b for b in ready_raw))

            if status == 0x01:
                try:
                    ack_result = self._i2c.i2c_read([], 7)
                    raw = bytearray(ack_result['response'])
                    ack = list(raw[1:])
                    ok = len(raw) >= 7 and raw[0] == 0x01 and ack == PN532_ACK
                    if self._debug >= 4 or not ok:
                        logger.debug("_read_ack: gate %d (PN532) raw=%s ok=%s",
                                     self._gate,
                                     ' '.join('%02X' % b for b in raw),
                                     ok)
                    return ok
                except Exception as e:
                    logger.error("_read_ack: gate %d (PN532) ACK read failed: %s\n%s",
                              self._gate, e, traceback.format_exc())
                    return False

            self._sleep(poll_interval)

        if self._debug >= 4:
            logger.debug("_read_ack: gate %d (PN532) timeout after %.3fs",
                         self._gate, timeout)
        return False

    def _recv(self, expected_cmd_resp, read_len=_MAX_RESPONSE_BYTES,
              timeout=1.0, poll_interval=0.005):
        """
        Poll the PN532 with 1-byte reads until STATUS=0x01 (ready),
        then read the full response frame.

        Parameters
        ----------
        expected_cmd_resp : int
            The response command byte expected at raw[_OFF_CMD].
        read_len : int
            Number of bytes to read for the full response frame.
        timeout : float
            Maximum seconds to poll before giving up.
        poll_interval : float
            Seconds to wait between poll attempts.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self._i2c.i2c_read([], 1)
                raw1 = bytearray(result['response'])
                pn_status = raw1[0] if raw1 else 0xFF
            except Exception as e:
                logger.error("_recv: gate %d (PN532) poll failed: %s\n%s",
                          self._gate, e, traceback.format_exc())
                return None

            if self._debug >= 4:
                logger.debug("_recv: gate %d (PN532) poll result=%s pn_status=0x%02X",
                             self._gate,
                             ' '.join('%02X' % b for b in raw1),
                             pn_status)

            if pn_status == 0x01:
                try:
                    params = self._i2c.i2c_read([], read_len)
                    raw = bytearray(params['response'])
                    payload = self._check_frame(raw, expected_cmd_resp)
                    if self._debug >= 4:
                        status_byte = raw[0] if raw else 0xFF
                        if payload is not None:
                            logger.debug(
                                "_recv: gate %d (PN532) DATA: expect=0x%02X "
                                "pn_status=0x%02X raw=%s",
                                self._gate, expected_cmd_resp, status_byte,
                                ' '.join('%02X' % b for b in raw))
                            logger.debug("_recv: gate %d (PN532) payload: %s",
                                         self._gate,
                                         ' '.join('%02X' % b for b in payload))
                        else:
                            logger.debug(
                                "_recv: gate %d (PN532) DATA ERROR: expect=0x%02X "
                                "pn_status=0x%02X raw=%s",
                                self._gate, expected_cmd_resp, status_byte,
                                ' '.join('%02X' % b for b in raw) if raw else '(empty)')
                    return payload
                except Exception as e:
                    logger.error("_recv: gate %d (PN532) DATA read failed: %s\n%s",
                              self._gate, e, traceback.format_exc())
                    return None

            self._sleep(poll_interval)

        if self._debug >= 4:
            logger.debug("_recv: gate %d (PN532) timeout after %.3fs waiting for ready",
                         self._gate, timeout)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level I2C debug tools
    # ─────────────────────────────────────────────────────────────────────────

    def low_level_raw_write(self, data):
        """
        Write raw bytes to the PN532 I2C device.

        This intentionally bypasses frame construction and ACK handling.  It is
        only available when low_level_debug is enabled in config.
        """
        self._require_low_level_debug()
        payload = [b & 0xFF for b in data]
        self._i2c.i2c_write(payload)
        return payload

    def low_level_raw_read(self, length):
        """
        Read raw bytes directly from the PN532 I2C device.

        The first byte returned by PN532 I2C reads is the PN532 status byte.
        """
        self._require_low_level_debug()
        result = self._i2c.i2c_read([], length)
        return list(bytearray(result.get('response', [])))

    def low_level_command_write(self, cmd_and_params):
        """Build and write a PN532 command frame without reading ACK/response."""
        self._require_low_level_debug()
        frame = self.low_level_command_frame(cmd_and_params)
        self._i2c.i2c_write(frame)
        return frame

    def low_level_ready_read(self):
        """Read the one-byte PN532 I2C ready/busy status."""
        return self.low_level_raw_read(1)

    def low_level_ack_read(self, length=7):
        """
        Run the console-style ACK probe: read READY, then read ACK bytes.

        Returns (ready_bytes, ack_bytes).  ack_bytes includes the leading I2C
        status byte because this is deliberately raw diagnostic output.
        """
        self._require_low_level_debug()
        ready = self.low_level_raw_read(1)
        if not ready or ready[0] != 0x01:
            return ready, []
        return ready, self.low_level_raw_read(length)


# =============================================================================
# PN532SPIDriver — SPI transport
# =============================================================================
#
# PN532 SPI protocol overview
# ───────────────────────────
# SPI mode 0 (CPOL=0, CPHA=0), LSB first.  Each transaction is framed by CS.
#
# Direction bytes (sent as first byte of every CS transaction):
#   0x01  Data Writing  — host sends a command frame to the PN532
#   0x02  Status Reading — host polls whether the PN532 has a response ready
#                          PN532 returns 0x01 when ready, 0x00 when busy
#   0x03  Data Reading  — host reads the response frame from the PN532
#
# All bytes (direction byte and frame bytes) are transmitted LSB first.
# Most SPI controllers (including the RP2040 default) send MSB first, so
# every byte is bit-reversed in software before sending and after receiving.
#
# The response frame in SPI mode does NOT include the STATUS prefix byte that
# appears in the I2C response.  The frame starts directly with the preamble:
#   [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, payload..., DCS, 0x00]
#
# Public interface is identical to PN532Driver (I2C).

# SPI frame byte offsets (no STATUS prefix, unlike I2C)
_SPI_OFF_LEN     = 3
_SPI_OFF_TFI     = 5
_SPI_OFF_CMD     = 6
_SPI_OFF_PAYLOAD = 7

# PN532 SPI direction bytes (before bit reversal)
_SPI_DIR_WRITE        = 0x01
_SPI_DIR_READ_STATUS  = 0x02
_SPI_DIR_READ_DATA    = 0x03


def _rev8(b):
    """Reverse the bits in a single byte (PN532 SPI is LSB first)."""
    b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
    b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
    b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
    return b


def _rev_list(data):
    """Bit-reverse every byte in a list."""
    return [_rev8(b) for b in data]


class PN532SPIDriver(_PN532Base):
    """
    Driver for one PN532 NFC reader module connected via SPI.

    Reads only the tag UID.  No data is ever written to the tag.
    The public interface is identical to PN532Driver (I2C variant).

    Parameters
    ----------
    spi : MCU_SPI
        A Klipper MCU_SPI object configured for this reader's CS pin.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used for logging.
    transceive_delay : float
        Scan timeout passed to InListPassiveTarget poll loop.
    crc_delay : float
        Timeout for InRelease and SAMConfiguration responses.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, spi, gate,
                 transceive_delay=0.250,
                 crc_delay=0.050,
                 debug=1,
                 low_level_debug=False,
                 sleep_fn=None):
        self._spi = spi
        self._transport_name = 'PN532 SPI'
        super().__init__(gate, transceive_delay, crc_delay, debug, low_level_debug,
                         sleep_fn=sleep_fn)

    # ─────────────────────────────────────────────────────────────────────────
    # Frame parsing — SPI frames have no STATUS prefix
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_frame(raw, expected_cmd_resp):
        """
        Validate a raw SPI read buffer and return the payload bytes.

        SPI frames have no STATUS prefix byte — the frame starts with the
        preamble [0x00, 0x00, 0xFF, ...] at offset 0.
        """
        if len(raw) < _SPI_OFF_PAYLOAD:
            return None
        if raw[0] != 0x00 or raw[1] != 0x00 or raw[2] != 0xFF:
            return None
        if raw[_SPI_OFF_TFI] != _TFI_PN532_TO_HOST:
            return None
        if raw[_SPI_OFF_CMD] != expected_cmd_resp:
            return None
        length  = raw[_SPI_OFF_LEN]
        payload = list(raw[_SPI_OFF_PAYLOAD: _SPI_OFF_PAYLOAD + length - 2])
        return payload

    # ─────────────────────────────────────────────────────────────────────────
    # SPI transport
    # ─────────────────────────────────────────────────────────────────────────
    #WORK IN PROGRESS — NOT IMPLEMENTED / NOT SUPPORTED -------------------------
    # ██╗    ██╗ ██████╗ ██████╗ ██╗  ██╗     ██╗███╗   ██╗
    # ██║    ██║██╔═══██╗██╔══██╗██║ ██╔╝     ██║████╗  ██║
    # ██║ █╗ ██║██║   ██║██████╔╝█████╔╝      ██║██╔██╗ ██║
    # ██║███╗██║██║   ██║██╔══██╗██╔═██╗      ██║██║╚██╗██║
    # ╚███╔███╔╝╚██████╔╝██║  ██║██║  ██╗     ██║██║ ╚████║
    #  ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝     ╚═╝╚═╝  ╚═══╝
    #
    # ██████╗ ██████╗  ██████╗  ██████╗ ██████╗ ███████╗███████╗███████╗
    # ██╔══██╗██╔══██╗██╔═══██╗██╔════╝ ██╔══██╗██╔════╝██╔════╝██╔════╝
    # ██████╔╝██████╔╝██║   ██║██║  ███╗██████╔╝█████╗  ███████╗███████╗
    # ██╔═══╝ ██╔══██╗██║   ██║██║   ██║██╔══██╗██╔══╝  ╚════██║╚════██║
    # ██║     ██║  ██║╚██████╔╝╚██████╔╝██║  ██║███████╗███████║███████║
    # ╚═╝     ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝

    def _send(self, cmd_and_params):
        """Write a command frame to the PN532 (direction byte 0x01)."""
        frame = self._build_frame(cmd_and_params)
        wire  = _rev_list([_SPI_DIR_WRITE] + frame)
        if self._debug >= 4:
            logger.debug("_send: gate %d (PN532 SPI) TX  cmd=0x%02X  frame=%s",
                          self._gate, cmd_and_params[0],
                          ' '.join('%02X' % b for b in frame))
        self._spi.spi_send(wire)

    def _read_ack(self, timeout=1.0, poll_interval=0.005):
        """Wait for and validate the PN532 ACK frame after a SPI command write."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self._spi.spi_transfer(_rev_list([_SPI_DIR_READ_STATUS, 0x00]))
                status = _rev8(bytearray(resp['response'])[1])
            except Exception as e:
                logger.error("_read_ack: gate %d (PN532 SPI) status read failed: %s\n%s",
                          self._gate, e, traceback.format_exc())
                return False

            if self._debug >= 4:
                logger.debug("_read_ack: gate %d (PN532 SPI) status=0x%02X",
                             self._gate, status)

            if status == 0x01:
                try:
                    out = _rev_list([_SPI_DIR_READ_DATA] + [0x00] * 6)
                    params = self._spi.spi_transfer(out)
                    raw = bytearray(_rev8(b) for b in bytearray(params['response'])[1:])
                    ack = list(raw)
                    ok = ack == PN532_ACK
                    if self._debug >= 4 or not ok:
                        logger.debug("_read_ack: gate %d (PN532 SPI) raw=%s ok=%s",
                                     self._gate,
                                     ' '.join('%02X' % b for b in raw),
                                     ok)
                    return ok
                except Exception as e:
                    logger.error("_read_ack: gate %d (PN532 SPI) ACK read failed: %s\n%s",
                              self._gate, e, traceback.format_exc())
                    return False

            self._sleep(poll_interval)

        if self._debug >= 4:
            logger.debug("_read_ack: gate %d (PN532 SPI) timeout after %.3fs",
                         self._gate, timeout)
        return False

    def _recv(self, expected_cmd_resp, read_len=_MAX_RESPONSE_BYTES,
              timeout=1.0, poll_interval=0.005):
        """
        Poll the PN532 with status reads (direction byte 0x02) until ready
        (0x01), then read the full response frame (direction byte 0x03).

        Parameters
        ----------
        expected_cmd_resp : int
            The response command byte expected at raw[_SPI_OFF_CMD].
        read_len : int
            Number of frame bytes to read (not including the direction byte).
        timeout : float
            Maximum seconds to poll before giving up.
        poll_interval : float
            Seconds between poll attempts.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Send direction byte 0x02, read 1 status byte back
                resp   = self._spi.spi_transfer(_rev_list([_SPI_DIR_READ_STATUS, 0x00]))
                status = _rev8(bytearray(resp['response'])[1])
            except Exception as e:
                logger.error("_recv: gate %d (PN532 SPI) poll failed: %s\n%s",
                          self._gate, e, traceback.format_exc())
                return None

            if self._debug >= 4:
                logger.debug("_recv: gate %d (PN532 SPI) poll status=0x%02X",
                             self._gate, status)

            if status == 0x01:
                try:
                    # Send direction byte 0x03, read read_len data bytes
                    out    = _rev_list([_SPI_DIR_READ_DATA] + [0x00] * read_len)
                    params = self._spi.spi_transfer(out)
                    raw    = bytearray(_rev8(b) for b in bytearray(params['response'])[1:])
                    payload = self._check_frame(raw, expected_cmd_resp)
                    if self._debug >= 4:
                        if payload is not None:
                            logger.debug(
                                "_recv: gate %d (PN532 SPI) DATA: expect=0x%02X raw=%s",
                                self._gate, expected_cmd_resp,
                                ' '.join('%02X' % b for b in raw))
                            logger.debug("_recv: gate %d (PN532 SPI) payload: %s",
                                         self._gate,
                                         ' '.join('%02X' % b for b in payload))
                        else:
                            logger.debug(
                                "_recv: gate %d (PN532 SPI) DATA ERROR: expect=0x%02X raw=%s",
                                self._gate, expected_cmd_resp,
                                ' '.join('%02X' % b for b in raw) if raw else '(empty)')
                    return payload
                except Exception as e:
                    logger.error("_recv: gate %d (PN532 SPI) DATA read failed: %s\n%s",
                              self._gate, e, traceback.format_exc())
                    return None

            self._sleep(poll_interval)

        if self._debug >= 4:
            logger.debug("_recv: gate %d (PN532 SPI) timeout after %.3fs",
                         self._gate, timeout)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level SPI debug tools -- WIP, not implementtedho
    # ─────────────────────────────────────────────────────────────────────────

    def low_level_raw_write(self, data):
        """Send raw bytes over SPI without adding PN532 framing."""
        self._require_low_level_debug()
        payload = [b & 0xFF for b in data]
        self._spi.spi_send(_rev_list(payload))
        return payload

    def low_level_raw_read(self, length):
        """
        Read raw bytes using PN532 SPI data-read direction.

        Returned bytes are de-bit-reversed and do not include the direction byte.
        """
        self._require_low_level_debug()
        out = _rev_list([_SPI_DIR_READ_DATA] + [0x00] * length)
        result = self._spi.spi_transfer(out)
        return list(_rev8(b) for b in bytearray(result.get('response', []))[1:])

    def low_level_command_write(self, cmd_and_params):
        """Build and write a PN532 command frame using SPI write direction."""
        self._require_low_level_debug()
        frame = self.low_level_command_frame(cmd_and_params)
        self._spi.spi_send(_rev_list([_SPI_DIR_WRITE] + frame))
        return frame

    def low_level_ready_read(self):
        """Read the one-byte PN532 SPI ready/busy status."""
        self._require_low_level_debug()
        result = self._spi.spi_transfer(_rev_list([_SPI_DIR_READ_STATUS, 0x00]))
        return [_rev8(bytearray(result.get('response', []))[1])]

    def low_level_ack_read(self, length=6):
        """Read READY status, then read the ACK frame bytes."""
        self._require_low_level_debug()
        ready = self.low_level_ready_read()
        if not ready or ready[0] != 0x01:
            return ready, []
        return ready, self.low_level_raw_read(length)


# =============================================================================
# NFC low-level debug command helpers
# =============================================================================

def get_low_level_debug(config, default=False):
    """Read the guarded raw PN532 debug flag."""
    return config.getboolean(
        'low_level_debug',
        config.getboolean('Low_Level_debug', default))


def _ll_parse_hex_bytes(value):
    value = value.replace(',', ' ').replace(':', ' ').replace('-', ' ')
    data = []
    for token in value.split():
        token = token.strip().strip('"\'')
        if not token:
            continue
        if token.lower().startswith('0x'):
            token = token[2:]
        data.append(int(token, 16) & 0xFF)
    return data


def _ll_hex(data):
    return ' '.join('%02X' % (b & 0xFF) for b in data)


def low_level_debug_requested(gcmd):
    return (
        gcmd.get_int("HELP", 0) or
        gcmd.get("STEP", None) is not None or
        gcmd.get_int("RAW_READ", 0) or
        gcmd.get("RAW_WRITE", None) is not None or
        gcmd.get("RAW_CMD", None) is not None or
        gcmd.get_int("READY_READ", 0) or
        gcmd.get_int("ACK_READ", 0))


def low_level_debug_help_lines(command_base):
    return [
        "PN532 is NOT initialized. Run Phase 1 + Phase 2 before anything else.",
        "--- Phase 1: Wake and firmware check (REQUIRED) ---",
        "1. %s STEP=WAKEUP" % command_base,
        "2. %s STEP=READY" % command_base,
        "3. %s STEP=FIRMWARE_WRITE" % command_base,
        "4. %s STEP=FIRMWARE_ACK" % command_base,
        "5. %s STEP=FIRMWARE_READY" % command_base,
        "6. %s STEP=FIRMWARE_RESPONSE" % command_base,
        "   Direct ACK timing probe (optional):",
        "   %s STEP=FIRMWARE_ACK_DIRECT DELAY=0.050" % command_base,
        "--- Phase 2: SAMConfiguration (REQUIRED) ---",
        "7. %s STEP=SAM_WRITE" % command_base,
        "8. %s STEP=SAM_ACK" % command_base,
        "9. %s STEP=SAM_READY" % command_base,
        "10. %s STEP=SAM_RESPONSE" % command_base,
        "--- Phase 3: Tag detect (optional, requires Phase 1 + 2) ---",
        "11. %s STEP=PASSIVE_WRITE" % command_base,
        "12. %s STEP=PASSIVE_ACK" % command_base,
        "13. %s STEP=PASSIVE_READY" % command_base,
        "14. %s STEP=PASSIVE_RESPONSE LEN=30" % command_base,
        "--- Raw tools ---",
        "%s RAW_READ=1 LEN=1" % command_base,
        "%s RAW_WRITE=00" % command_base,
        "%s RAW_CMD=02" % command_base,
        "%s READY_READ=1" % command_base,
        "%s ACK_READ=1 LEN=7" % command_base,
    ]


def _ll_response(gcmd, label, message):
    gcmd.respond_info("[%s]: %s" % (label, message))


def _ll_next(gcmd, label, command_base, next_args):
    _ll_response(gcmd, label, "NEXT: %s %s" % (command_base, next_args))


def _ll_write(gcmd, reader, label, op, data):
    _ll_response(gcmd, label, "%s WRITE before: %s" % (op, _ll_hex(data)))
    written = reader.low_level_raw_write(data)
    _ll_response(gcmd, label, "%s WRITE after: OK" % op)
    return written


def _ll_command_write(gcmd, reader, label, op, cmd_and_params):
    frame = reader.low_level_command_frame(cmd_and_params)
    _ll_write(gcmd, reader, label, op, frame)
    return frame


def _ll_read(gcmd, reader, label, op, length):
    _ll_response(gcmd, label, "%s READ before: %d byte(s)" % (op, length))
    data = reader.low_level_raw_read(length)
    _ll_response(gcmd, label, "%s READ after: %s" % (op, _ll_hex(data)))
    return data


def _ll_ready(gcmd, reader, label):
    data = _ll_read(gcmd, reader, label, "READY", 1)
    if not data:
        _ll_response(gcmd, label, "READY result: no bytes returned")
        return False
    if data[0] == 0x01:
        _ll_response(gcmd, label, "READY result: ready (0x01)")
        return True
    if data[0] == 0x00:
        _ll_response(gcmd, label, "READY result: busy (0x00)")
    else:
        _ll_response(gcmd, label, "READY result: unknown status 0x%02X" % data[0])
    return False


def _ll_ack(gcmd, reader, label, command_base, length):
    ready_data = _ll_read(gcmd, reader, label, "ACK_READY", 1)
    if not ready_data:
        _ll_response(gcmd, label, "ACK_READY result: no bytes returned")
        return False
    if ready_data[0] != 0x01:
        _ll_response(
            gcmd, label,
            "ACK_READY result: busy/unknown 0x%02X; not reading ACK yet" %
            ready_data[0])
        _ll_next(gcmd, label, command_base, "STEP=%s" %
                 gcmd.get("STEP", "FIRMWARE_ACK").upper())
        return False
    ack_data = _ll_read(gcmd, reader, label, "ACK", length)
    return _ll_report_ack(gcmd, label, "ACK", ack_data, length)


def _ll_report_ack(gcmd, label, op, ack_data, length):
    if not ack_data:
        _ll_response(gcmd, label, "%s result: no bytes returned" % op)
        return False
    if length < 7:
        _ll_response(gcmd, label, "%s probe only: read %d byte(s), raw=%s" %
                     (op, length, _ll_hex(ack_data)))
        _ll_response(gcmd, label, "Try the same ACK step with LEN=%d next" %
                     min(length + 1, 7))
        return False
    if len(ack_data) >= 7 and ack_data[1:] == [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]:
        _ll_response(gcmd, label, "%s status byte: 0x%02X" %
                     (op, ack_data[0]))
        _ll_response(gcmd, label, "%s frame: %s" %
                     (op, _ll_hex(ack_data[1:])))
        _ll_response(gcmd, label, "%s result: valid PN532 ACK" % op)
        return True
    if ack_data == [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]:
        _ll_response(gcmd, label, "%s frame: %s" % (op, _ll_hex(ack_data)))
        _ll_response(gcmd, label, "%s result: valid PN532 ACK" % op)
        return True
    _ll_response(gcmd, label,
                 "%s result: invalid, expected 00 00 FF 00 FF 00" % op)
    return False


def _ll_parse_response(gcmd, label, name, data, expected_cmd):
    if not data:
        _ll_response(gcmd, label, "%s response: no bytes returned" % name)
        return False
    status = None
    if len(data) >= 4 and data[0] == 0x00 and data[1] == 0x00 and data[2] == 0xFF:
        frame = data
    else:
        status = data[0]
        frame = data[1:]
    if status is not None:
        _ll_response(gcmd, label, "%s status byte: 0x%02X" % (name, status))
    if len(frame) >= 7 and frame[0] == 0x00 and frame[1] == 0x00 and \
            frame[2] == 0xFF and frame[5] == 0xD5 and frame[6] == expected_cmd:
        if expected_cmd == 0x03 and len(frame) >= 11:
            _ll_response(gcmd, label,
                         "Firmware parsed: v%d.%d IC=0x%02X support=0x%02X" %
                         (frame[8], frame[9], frame[7], frame[10]))
        elif expected_cmd == 0x15:
            _ll_response(gcmd, label, "SAM response parsed: OK")
        elif expected_cmd == 0x4B:
            _ll_response(gcmd, label, "Passive response parsed header: OK")
        return True
    _ll_response(gcmd, label, "%s response did not match expected PN532 frame" % name)
    return False


def run_low_level_debug(gcmd, reader, label, command_base, enabled):
    if not low_level_debug_requested(gcmd):
        return False
    if not enabled:
        _ll_response(gcmd, label, "low_level_debug is disabled in config")
        return True
    if not hasattr(reader, 'low_level_raw_read'):
        _ll_response(gcmd, label, "reader does not support low-level debug")
        return True

    raw_write = gcmd.get("RAW_WRITE", None)
    if raw_write is not None:
        data = _ll_parse_hex_bytes(raw_write)
        _ll_write(gcmd, reader, label, "RAW", data)
        _ll_next(gcmd, label, command_base, "RAW_READ=1 LEN=1")
        return True
    raw_cmd = gcmd.get("RAW_CMD", None)
    if raw_cmd is not None:
        cmd = _ll_parse_hex_bytes(raw_cmd)
        _ll_command_write(gcmd, reader, label, "RAW_CMD", cmd)
        _ll_next(gcmd, label, command_base, "ACK_READ=1 LEN=7")
        return True
    if gcmd.get_int("RAW_READ", 0):
        length = gcmd.get_int("LEN", 1, minval=1, maxval=64)
        _ll_read(gcmd, reader, label, "RAW", length)
        return True
    if gcmd.get_int("READY_READ", 0):
        _ll_ready(gcmd, reader, label)
        return True
    if gcmd.get_int("ACK_READ", 0):
        length = gcmd.get_int("LEN", 7, minval=1, maxval=64)
        _ll_ack(gcmd, reader, label, command_base, length)
        return True

    step = gcmd.get("STEP", "HELP").upper()
    if step == "HELP":
        gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
    elif step == "WAKEUP":
        _ll_write(gcmd, reader, label, "WAKEUP", [0x00])
        time.sleep(0.05)
        _ll_next(gcmd, label, command_base, "STEP=READY")
    elif step == "READY":
        if _ll_ready(gcmd, reader, label):
            _ll_next(gcmd, label, command_base, "STEP=FIRMWARE_WRITE")
    elif step == "FIRMWARE_WRITE":
        _ll_command_write(gcmd, reader, label, "FIRMWARE",
                          [PN532_COMMAND_GETFIRMWAREVERSION])
        _ll_next(gcmd, label, command_base, "STEP=FIRMWARE_ACK")
    elif step == "FIRMWARE_ACK":
        if _ll_ack(gcmd, reader, label, command_base,
                   gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _ll_next(gcmd, label, command_base, "STEP=FIRMWARE_READY")
    elif step == "FIRMWARE_READY":
        if _ll_ready(gcmd, reader, label):
            _ll_next(gcmd, label, command_base, "STEP=FIRMWARE_RESPONSE")
    elif step == "FIRMWARE_RESPONSE":
        data = _ll_read(gcmd, reader, label, "FIRMWARE_RESPONSE",
                        gcmd.get_int("LEN", 14, minval=1, maxval=64))
        if _ll_parse_response(gcmd, label, "Firmware", data, 0x03):
            _ll_next(gcmd, label, command_base, "STEP=SAM_WRITE")
    elif step == "FIRMWARE_ACK_DIRECT":
        delay = gcmd.get_float("DELAY", 0.050, minval=0.0, maxval=2.0)
        _ll_command_write(gcmd, reader, label, "FIRMWARE_DIRECT",
                          [PN532_COMMAND_GETFIRMWAREVERSION])
        _ll_response(gcmd, label,
                     "FIRMWARE_DIRECT waiting %.3f seconds before ACK read" % delay)
        time.sleep(delay)
        length = gcmd.get_int("LEN", 7, minval=1, maxval=64)
        data = _ll_read(gcmd, reader, label, "FIRMWARE_DIRECT_ACK", length)
        if _ll_report_ack(gcmd, label, "FIRMWARE_DIRECT_ACK", data, length):
            _ll_next(gcmd, label, command_base, "STEP=FIRMWARE_READY")
    elif step == "SAM_WRITE":
        _ll_command_write(gcmd, reader, label, "SAM",
                          [PN532_COMMAND_SAMCONFIGURATION, 0x01, 0x14, 0x01])
        _ll_next(gcmd, label, command_base, "STEP=SAM_ACK")
    elif step == "SAM_ACK":
        if _ll_ack(gcmd, reader, label, command_base,
                   gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _ll_next(gcmd, label, command_base, "STEP=SAM_READY")
    elif step == "SAM_READY":
        if _ll_ready(gcmd, reader, label):
            _ll_next(gcmd, label, command_base, "STEP=SAM_RESPONSE")
    elif step == "SAM_RESPONSE":
        data = _ll_read(gcmd, reader, label, "SAM_RESPONSE",
                        gcmd.get_int("LEN", 9, minval=1, maxval=64))
        if _ll_parse_response(gcmd, label, "SAM", data, 0x15):
            _ll_next(gcmd, label, command_base, "STEP=PASSIVE_WRITE")
    elif step == "PASSIVE_WRITE":
        _ll_command_write(gcmd, reader, label, "PASSIVE",
                          [PN532_COMMAND_INLISTPASSIVETARGET, 0x01, 0x00])
        _ll_next(gcmd, label, command_base, "STEP=PASSIVE_ACK")
    elif step == "PASSIVE_ACK":
        if _ll_ack(gcmd, reader, label, command_base,
                   gcmd.get_int("LEN", 7, minval=1, maxval=64)):
            _ll_next(gcmd, label, command_base, "STEP=PASSIVE_READY")
    elif step == "PASSIVE_READY":
        if _ll_ready(gcmd, reader, label):
            _ll_next(gcmd, label, command_base, "STEP=PASSIVE_RESPONSE LEN=30")
    elif step == "PASSIVE_RESPONSE":
        data = _ll_read(gcmd, reader, label, "PASSIVE_RESPONSE",
                        gcmd.get_int("LEN", 30, minval=1, maxval=64))
        if data:
            _ll_response(gcmd, label,
                         "Passive response raw includes leading transport/status byte")
        _ll_parse_response(gcmd, label, "Passive", data, 0x4B)
    else:
        _ll_response(gcmd, label, "Unknown STEP=%s" % step)
        gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
    return True
