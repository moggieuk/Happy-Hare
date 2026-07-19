# klippy/extras/mmu/unit/nfc/rc522_driver.py
#
# EMU NFC Gate Reader — RC522 SPI driver
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
# RC522 NFC reader driver — communicates with the RC522 chip over SPI using
# Klipper's MCU_SPI interface.
#
# Integration model
# ─────────────────
# This driver uses direct ISO14443A commands over the RC522 FIFO.  It reads the
# tag UID for normal Spoolman lookup, and when tag parsing is enabled it exposes
# enough Type-2 / NTAG memory-read helpers for the shared NFC Reader pipeline to
# parse rich metadata.
#
# ISO14443A UID read sequence used here
# ──────────────────────────────────────
# The reader starts with the same UID probe used by the earlier UID-only path:
#   Stage 1  REQA   — broadcast to wake idle tags; expect 16-bit ATQA response.
#   Stage 2  ANTICOLL / SELECT cascade — returns UID bytes and SAK.
#   Stage 3  Type-2 READ — optional NTAG page reads for rich metadata.
#
## Threading notes: # PAUL: I dont think this is possible ..
##   All methods are designed to be called from a dedicated background thread
##   (not the Klipper reactor thread).  spi_send() and spi_transfer() route
##   commands to the MCU over CAN; the background thread blocks on each
##   response while the reactor continues processing other events normally.
#
# Reactor / threading notes: (PAUL)
#   All methods run on the Klipper reactor thread - they are NOT safe to call
#   from a background OS thread.  spi_send()/spi_transfer() issue MCU_SPI
#   transactions to the microcontroller, and spi_transfer() waits for the MCU
#   response via a reactor completion.  That wait is greenlet-based
#   (greenlet.getcurrent() + reactor.pause()), so it is bound to the reactor
#   thread and would fail if driven from another thread.
#   To avoid stalling the reactor during a tag wait, the read paths instead
#   yield cooperatively through the injected sleep_fn (reactor.pause): while a
#   read waits, the reactor keeps servicing its other work (timers, moves - e.g.
#   a drip-homing move), then resumes the read.  This cooperative yielding is
#   Klipper's substitute for the background-thread model this driver was
#   originally ported from.


import time
import traceback

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# RC522 register addresses
# ─────────────────────────────────────────────────────────────────────────────

_CommandReg     = 0x01
_ComIEnReg      = 0x02
_ComIrqReg      = 0x04
_DivIrqReg      = 0x05
_ErrorReg       = 0x06
_Status2Reg     = 0x08   # bit 3 = MFCrypto1On (MIFARE auth active)
_FIFODataReg    = 0x09
_FIFOLevelReg   = 0x0A
_ControlReg     = 0x0C
_BitFramingReg  = 0x0D
_ModeReg        = 0x11
_TxControlReg   = 0x14
_TxASKReg       = 0x15
_CRCResultRegH  = 0x21
_CRCResultRegL  = 0x22
_TModeReg       = 0x2A
_TPrescalerReg  = 0x2B
_TReloadRegH    = 0x2C
_TReloadRegL    = 0x2D

# RC522 PCD (reader chip) commands
_PCD_IDLE       = 0x00
_PCD_CALCCRC    = 0x03
_PCD_TRANSCEIVE = 0x0C
_PCD_MFAUTHENT  = 0x0E   # hardware MIFARE Classic auth — not a transceive
_PCD_RESETPHASE = 0x0F

# PICC (tag) commands
_PICC_REQIDL    = 0x26   # Request idle — wake tags in the RF field
_PICC_ANTICOLL_CL1 = 0x93
_PICC_ANTICOLL_CL2 = 0x95
_PICC_ANTICOLL_CL3 = 0x97
_PICC_SELECT_NVBC  = 0x70
_PICC_CASCADE_TAG  = 0x88
_PICC_MIFARE_READ  = 0x30
_PICC_AUTH_A    = 0x60   # MIFARE Classic Key A authentication
_PICC_AUTH_B    = 0x61   # MIFARE Classic Key B authentication

# Operation results
MI_OK  = 0
MI_ERR = 1

# Human-readable register names used in debug=2 trace output
_REG_NAMES = {
    _CommandReg:    'CommandReg',
    _ComIEnReg:     'ComIEnReg',
    _ComIrqReg:     'ComIrqReg',
    _DivIrqReg:     'DivIrqReg',
    _ErrorReg:      'ErrorReg',
    _Status2Reg:    'Status2Reg',
    _FIFODataReg:   'FIFODataReg',
    _FIFOLevelReg:  'FIFOLevelReg',
    _ControlReg:    'ControlReg',
    _BitFramingReg: 'BitFramingReg',
    _ModeReg:       'ModeReg',
    _TxControlReg:  'TxControlReg',
    _TxASKReg:      'TxASKReg',
    _CRCResultRegH: 'CRCResultRegH',
    _CRCResultRegL: 'CRCResultRegL',
    _TModeReg:      'TModeReg',
    _TPrescalerReg: 'TPrescalerReg',
    _TReloadRegH:   'TReloadRegH',
    _TReloadRegL:   'TReloadRegL',
}
_REG_BY_NAME = dict((v.lower(), k) for k, v in _REG_NAMES.items())
_REG_BY_NAME.update(dict((v.lower().replace('reg', ''), k)
                         for k, v in _REG_NAMES.items()))

class RC522Driver:
    """
    Driver for one RC522 NFC reader module.

    Reads ISO14443A UIDs for Spoolman lookup, performs anticollision/SELECT to
    capture SAK/ATQA, and exposes NTAG/Type-2 page reads for rich metadata.

    Parameters
    ----------
    spi : MCU_SPI
        A Klipper MCU_SPI object configured for this reader's CS pin.
        Must be fully initialised (klippy:connect completed) before calling
        init() or read_tag().
    gate : int
        Gate number (0-based), used only for logging.
    transceive_delay : float
        Seconds to wait after triggering TRANSCEIVE before reading the result.
        The RC522 internal timer fires at ~0.5 ms when no tag is present;
        35 ms gives tags (which respond in <2 ms) ample time while CAN
        round-trips add negligible overhead at 30-second poll intervals.
    debug : int
        0 = silent, 1 = major events, 2 = full trace.
    """

    def __init__(self, spi, gate,
                 transceive_delay=0.035,
                 debug=0,
                 sleep_fn=None):
        self._spi              = spi
        self._gate             = gate
        self._transceive_delay = transceive_delay
        self._debug            = debug
        self._sleep            = sleep_fn if sleep_fn is not None else time.sleep
        self._clear_current_card()

    def _clear_current_card(self):
        self.current_target = None
        self.current_uid = None
        self.current_uid_hex = ''
        self.current_target_info = None

    def _set_current_card(self, target_info):
        self.current_target_info = dict(target_info)
        self.current_target = target_info.get('target')
        self.current_uid = list(target_info.get('uid_bytes') or [])
        self.current_uid_hex = target_info.get('uid', '')

    # ─────────────────────────────────────────────────────────────────────────
    # Register read / write (one SPI transaction each, CS toggled by MCU_SPI)
    # ─────────────────────────────────────────────────────────────────────────

    def _write(self, reg, val):
        """Write one byte to an RC522 register (no response expected)."""
        if self._debug >= 4:
            logger.debug("RC522: gate %s  W %-15s (0x%02X) = 0x%02X",
                          self._gate, _REG_NAMES.get(reg, '?'), reg, val & 0xFF)
        self._spi.spi_send([(reg << 1) & 0x7E, val & 0xFF])

    def _read(self, reg):
        """Read one byte from an RC522 register and return it as an integer."""
        resp = self._spi.spi_transfer([((reg << 1) & 0x7E) | 0x80, 0x00])
        val = resp['response'][1]
        if self._debug >= 4:
            logger.debug("RC522: gate %s  R %-15s (0x%02X) -> 0x%02X",
                          self._gate, _REG_NAMES.get(reg, '?'), reg, val)
        return val

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def init(self):
        """
        Soft-reset the RC522 and configure it for 13.56 MHz ISO14443A operation.
        Must be called once after klippy:connect, before the first read_tag().
        """
        try:
            if self._debug >= 4:
                logger.debug("RC522: gate %s init — soft-resetting", self._gate)
            self._write(_CommandReg,    _PCD_RESETPHASE)
            self._sleep(0.050)           # Datasheet: max reset time 37.74 ms; 50 ms is safe
            if self._debug >= 4:
                logger.debug("RC522: gate %s init — reset done, configuring timer "
                             "and modulation", self._gate)
            self._write(_TModeReg,      0x8D)
            self._write(_TPrescalerReg, 0x3E)
            self._write(_TReloadRegH,   0x00)
            self._write(_TReloadRegL,   0x1E)
            self._write(_TxASKReg,      0x40)
            self._write(_ModeReg,       0x3D)
            # Enable antenna TX pins (bits 0-1 of TxControlReg)
            tx = self._read(_TxControlReg)
            if not (tx & 0x03):
                if self._debug >= 4:
                    logger.debug("RC522: gate %s init — enabling antenna TX pins "
                                 "(TxControl was 0x%02X)", self._gate, tx)
                self._write(_TxControlReg, tx | 0x03)
            tx_final = self._read(_TxControlReg)
            logger.info("RC522: gate %s init OK (TxControl=0x%02X)",
                        self._gate, tx_final)
        except Exception as e:
            logger.warning(
                "RC522: gate %s init failed — check SPI wiring, cs_pin, "
                "spi_bus/software SPI pins, power, and ground: %s",
                self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %s init traceback:\n%s",
                             self._gate, traceback.format_exc())
            raise

    def is_alive(self):
        """Return True if the reader is responding (antenna TX bits are set)."""
        try:
            tx = self._read(_TxControlReg)
            alive = bool(tx & 0x03)
            if not alive:
                logger.warning(
                    "RC522: gate %s not responding — antenna TX bits are off "
                    "(TxControl=0x%02X)", self._gate, tx)
            elif self._debug >= 4:
                logger.debug("RC522: gate %s alive (TxControl=0x%02X)",
                             self._gate, tx)
            return alive
        except Exception as e:
            logger.warning(
                "RC522: gate %s health check failed — SPI reader did not "
                "respond: %s", self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %s is_alive traceback:\n%s",
                             self._gate, traceback.format_exc())
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # FIFO transceive
    # ─────────────────────────────────────────────────────────────────────────

    def _transceive(self, send_data, timeout=None):
        """
        Load send_data into the RC522 FIFO, trigger TRANSCEIVE, wait
        transceive_delay for a tag response, then return the received bytes.

        Returns (MI_OK, data_bytes, bit_length) on success,
                (MI_ERR, [], 0) on timeout, collision, or protocol error.
        """
        if self._debug >= 4:
            logger.debug("RC522: gate %s  _transceive send=[%s]",
                          self._gate,
                          ' '.join('0x%02X' % b for b in send_data))

        # Enable all interrupt sources; clear pending flags; flush FIFO
        self._write(_ComIEnReg,    self._read(_ComIEnReg) | 0x80)
        self._write(_ComIrqReg,    self._read(_ComIrqReg) & 0x7F)
        self._write(_FIFOLevelReg, self._read(_FIFOLevelReg) | 0x80)
        self._write(_CommandReg,   _PCD_IDLE)

        # Load data into FIFO
        for byte in send_data:
            self._write(_FIFODataReg, byte)

        # Start transmission
        self._write(_CommandReg,    _PCD_TRANSCEIVE)
        self._write(_BitFramingReg, self._read(_BitFramingReg) | 0x80)  # StartSend

        delay = self._transceive_delay if timeout is None else max(
            0.0, min(float(timeout), self._transceive_delay))
        if self._debug >= 4:
            logger.debug("RC522: gate %s  _transceive — transmission started, "
                          "waiting %.0f ms for response",
                          self._gate, delay * 1000)

        # Wait for tag response (or internal timer timeout at ~0.5 ms)
        self._sleep(delay)

        # Clear StartSend
        self._write(_BitFramingReg, self._read(_BitFramingReg) & 0x7F)

        irq = self._read(_ComIrqReg)
        if self._debug >= 4:
            logger.debug("RC522: gate %s  _transceive IRQ=0x%02X "
                          "(TimerIRq=%d RxIRq=%d IdleIRq=%d)",
                          self._gate, irq,
                          (irq >> 0) & 1, (irq >> 5) & 1, (irq >> 4) & 1)

        # TimerIRq (bit 0) set with no RxIRq (bit 5) or IdleIRq (bit 4) → no tag
        if (irq & 0x01) and not (irq & 0x30):
            if self._debug >= 4:
                logger.debug("RC522: gate %s  _transceive -> MI_ERR (timer "
                              "expired, no tag response)", self._gate)
            return MI_ERR, [], 0

        # Protocol error (collision, CRC error, buffer overflow, parity error)
        err = self._read(_ErrorReg)
        if err & 0x1B:
            if self._debug >= 4:
                logger.debug("RC522: gate %s  _transceive -> MI_ERR "
                              "(ErrorReg=0x%02X: collision=%d CRC=%d overflow=%d "
                              "parity=%d)",
                              self._gate, err,
                              (err >> 3) & 1, (err >> 2) & 1,
                              (err >> 4) & 1, (err >> 1) & 1)
            return MI_ERR, [], 0

        # Read received bytes from FIFO
        fifo_len = self._read(_FIFOLevelReg)
        if fifo_len == 0:
            if self._debug >= 4:
                logger.debug("RC522: gate %s  _transceive -> MI_ERR "
                              "(FIFO empty after IRQ)", self._gate)
            return MI_ERR, [], 0

        last_bits = self._read(_ControlReg) & 0x07
        bit_len = (fifo_len - 1) * 8 + last_bits if last_bits else fifo_len * 8

        if fifo_len > 16:
            fifo_len = 16
        back_data = [self._read(_FIFODataReg) for _ in range(fifo_len)]

        if self._debug >= 4:
            logger.debug("RC522: gate %s  _transceive -> MI_OK "
                          "fifo=%d bits=%d data=[%s]",
                          self._gate, fifo_len, bit_len,
                          ' '.join('0x%02X' % b for b in back_data))

        return MI_OK, back_data, bit_len

    # ─────────────────────────────────────────────────────────────────────────
    # ISO14443A target select and Type-2 reads
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_crc(self, data):
        """Ask the RC522 hardware to calculate ISO14443A CRC_A bytes."""
        self._write(_CommandReg, _PCD_IDLE)
        self._write(_DivIrqReg, self._read(_DivIrqReg) & 0xFB)
        self._write(_FIFOLevelReg, self._read(_FIFOLevelReg) | 0x80)
        for byte in data:
            self._write(_FIFODataReg, byte)
        self._write(_CommandReg, _PCD_CALCCRC)
        for _i in range(255):
            if self._read(_DivIrqReg) & 0x04:
                self._write(_CommandReg, _PCD_IDLE)
                return [self._read(_CRCResultRegL), self._read(_CRCResultRegH)]
            self._sleep(0.001)
        self._write(_CommandReg, _PCD_IDLE)
        raise RuntimeError("RC522 CRC calculation timed out")

    def _transceive_crc(self, data, timeout=None):
        frame = list(data) + self._calc_crc(data)
        self._write(_BitFramingReg, 0x00)
        return self._transceive(frame, timeout=timeout)

    def _request_a(self, timeout=None):
        self._write(_BitFramingReg, 0x07)
        status, data, bits = self._transceive([_PICC_REQIDL], timeout=timeout)
        if status != MI_OK or bits != 0x10:
            return None
        atqa_bytes = list(data[:2])
        atqa = ((atqa_bytes[0] << 8) | atqa_bytes[1]
                if len(atqa_bytes) >= 2 else 0)
        return atqa, atqa_bytes

    def _anticoll_level(self, cascade_cmd, timeout=None):
        self._write(_BitFramingReg, 0x00)
        status, data, bits = self._transceive([cascade_cmd, 0x20],
                                              timeout=timeout)
        if status != MI_OK or len(data) < 5:
            return None, status, data, bits
        serial = list(data[:5])
        chk = serial[0] ^ serial[1] ^ serial[2] ^ serial[3]
        if chk != serial[4]:
            raise RuntimeError(
                "ANTICOLL checksum mismatch calc=0x%02X got=0x%02X"
                % (chk, serial[4]))
        return serial, status, data, bits

    def _select_level(self, cascade_cmd, serial, timeout=None):
        frame = [cascade_cmd, _PICC_SELECT_NVBC] + list(serial)
        status, data, bits = self._transceive_crc(frame, timeout=timeout)
        if status != MI_OK or not data:
            return None
        return data[0] & 0xFF

    def _uid_only_target(self, uid_bytes, atqa, atqa_bytes):
        uid_hex = ''.join('%02X' % (b & 0xFF) for b in uid_bytes)
        return {
            'reader': 'rc522',
            'protocol': 'uid_only',
            'protocol_name': 'ISO14443A_UID_ONLY',
            'target': 1,
            'tg': 1,
            'uid': uid_hex,
            'uid_bytes': list(uid_bytes),
            'uid_length': len(uid_bytes),
            'sak': None,
            'sens_res': atqa,
            'atqa': atqa,
            'sens_res_bytes': list(atqa_bytes),
        }

    def _selected_target(self, uid_bytes, atqa, atqa_bytes, sak):
        uid_hex = ''.join('%02X' % (b & 0xFF) for b in uid_bytes)
        if sak == 0x00:
            protocol = 'ntag_type2'
            protocol_name = 'ISO14443A_TYPE2'
        elif sak & 0x08:
            protocol = 'mifare_classic'
            protocol_name = 'ISO14443A_MIFARE_CLASSIC'
        else:
            protocol = 'iso14443a'
            protocol_name = 'ISO14443A'
        return {
            'reader': 'rc522',
            'protocol': protocol,
            'protocol_name': protocol_name,
            'target': 1,
            'tg': 1,
            'uid': uid_hex,
            'uid_bytes': list(uid_bytes),
            'uid_length': len(uid_bytes),
            'sak': sak,
            'sens_res': atqa,
            'atqa': atqa,
            'sens_res_bytes': list(atqa_bytes),
        }

    def _select_iso14443a_target(self, timeout=None):
        req = self._request_a(timeout=timeout)
        if req is None:
            return None
        atqa, atqa_bytes = req
        uid_bytes = []
        last_serial = None
        last_sak = None

        for cascade_cmd in (
                _PICC_ANTICOLL_CL1, _PICC_ANTICOLL_CL2, _PICC_ANTICOLL_CL3):
            serial, status, data, bits = self._anticoll_level(
                cascade_cmd, timeout=timeout)
            if serial is None:
                if not uid_bytes:
                    logger.warning(
                        "RC522: gate %s ANTICOLL failed after REQA "
                        "(status=%s data_len=%d)",
                        self._gate, 'OK' if status == MI_OK else 'ERR',
                        len(data))
                    if self._debug >= 4:
                        logger.debug(
                            "RC522: gate %s ANTICOLL response bits=%d data=[%s]",
                            self._gate, bits,
                            ' '.join('0x%02X' % b for b in data))
                    return None
                logger.warning(
                    "RC522: gate %s cascade ANTICOLL failed after partial "
                    "uid=%s; falling back to UID-only",
                    self._gate, ''.join('%02X' % b for b in uid_bytes))
                return self._uid_only_target(uid_bytes, atqa, atqa_bytes)

            last_serial = list(serial)
            if serial[0] == _PICC_CASCADE_TAG:
                uid_bytes.extend(serial[1:4])
            else:
                uid_bytes.extend(serial[0:4])

            sak = self._select_level(cascade_cmd, serial, timeout=timeout)
            if sak is None:
                logger.warning(
                    "RC522: gate %s SELECT failed for cascade 0x%02X "
                    "uid=%s; falling back to UID-only",
                    self._gate, cascade_cmd,
                    ''.join('%02X' % b for b in uid_bytes))
                return self._uid_only_target(uid_bytes, atqa, atqa_bytes)
            last_sak = sak
            if not (sak & 0x04):
                return self._selected_target(uid_bytes, atqa, atqa_bytes, sak)

        if uid_bytes:
            logger.warning(
                "RC522: gate %s cascade bit still set after CL3 uid=%s; "
                "falling back to UID-only",
                self._gate, ''.join('%02X' % b for b in uid_bytes))
            target = self._uid_only_target(uid_bytes, atqa, atqa_bytes)
            target['sak'] = last_sak
            target['last_cascade_serial'] = last_serial
            return target
        return None

    def read_target(self, timeout=None):
        """
        Attempt to select any ISO14443A tag in the RF field.

        Returns
        -------
        dict
            Target information with UID, ATQA, and SAK when SELECT succeeds.
            If anticollision succeeds but SELECT fails, a uid_only target is
            returned so normal UID lookup can still continue.
        None
            No tag in the RF field, or a communication error occurred.
        """
        try:
            target_info = self._select_iso14443a_target(timeout=timeout)
            if target_info is not None:
                self._set_current_card(target_info)
            else:
                self._clear_current_card()
            if self._debug >= 4 and target_info is not None:
                logger.debug(
                    "RC522: gate %s read_target — uid=%s protocol=%s "
                    "SAK=%s ATQA=0x%04X",
                    self._gate, target_info.get('uid'),
                    target_info.get('protocol'), target_info.get('sak'),
                    target_info.get('atqa', 0))
            elif self._debug >= 3 and target_info is not None:
                sak = target_info.get('sak')
                sak_text = "N/A" if sak is None else "0x%02X" % (sak & 0xFF)
                logger.info(
                    "RC522: gate %s target uid=%s protocol=%s SAK=%s ATQA=0x%04X",
                    self._gate, target_info.get('uid'),
                    target_info.get('protocol'), sak_text,
                    target_info.get('atqa', 0))
            return target_info
        except Exception as e:
            logger.warning(
                "RC522: gate %s target read failed — check SPI wiring and reader "
                "state: %s", self._gate, e)
            if self._debug >= 4:
                logger.debug("RC522: gate %s read_target traceback:\n%s",
                             self._gate, traceback.format_exc())
            self._clear_current_card()
            return None

    def read_tag(self, timeout=None):
        """Read and return an uppercase UID string, or None if no tag is present."""
        try:
            target_info = self.read_target(timeout=timeout)
            if target_info is None:
                return None
            return target_info.get('uid')
        except Exception as e:
            if self._debug >= 3:
                logger.info("RC522: gate %s read_tag error: %s\n%s",
                            self._gate, e, traceback.format_exc())
            self._clear_current_card()
            return None

    def _release_current_target(self, reason="manual"):
        """Clear cached target state."""
        if self._debug >= 4:
            logger.debug("RC522: gate %s release target reason=%s",
                         self._gate, reason)
        self._clear_current_card()

    def _ensure_selected_target(self, timeout=0.500):
        if self.current_target_info is not None:
            return self.current_target_info
        return self.read_target(timeout=timeout)

    def ntag_read_page(self, page, timeout=0.100):
        """Read four NTAG/Type-2 pages (16 bytes) starting at *page*."""
        if page < 0 or page > 255:
            raise ValueError("RC522 NTAG page out of range: %s" % page)
        if self._ensure_selected_target(timeout=0.500) is None:
            return None
        status, data, bits = self._transceive_crc(
            [_PICC_MIFARE_READ, page & 0xFF], timeout=timeout)
        if status != MI_OK or len(data) < 16:
            if self._debug >= 4:
                logger.debug(
                    "RC522: gate %s NTAG page %d read failed "
                    "(status=%s bits=%d data=[%s])",
                    self._gate, page, 'OK' if status == MI_OK else 'ERR',
                    bits, ' '.join('0x%02X' % b for b in data))
            return None
        return list(data[:16])

    def robust_page_read(self, page, attempts=3):
        if self._ensure_selected_target(timeout=0.500) is None:
            return None
        expected_uid = list(self.current_uid or [])
        for attempt in range(max(1, attempts)):
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
            if expected_uid and target_info.get('uid_bytes') != expected_uid:
                logger.warning(
                    "RC522: gate %s different tag detected during page %d retry",
                    self._gate, page)
                self._release_current_target(reason="uid_changed")
                return None
        self._release_current_target(reason="page_%d_max_retries" % page)
        return None

    def ntag_read_user_memory(self, start_page=4, end_page=67):
        """Read raw Type-2 user memory in 16-byte NTAG READ chunks."""
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
                if 0xFE in page_data:
                    if self._debug >= 4:
                        logger.debug(
                            "RC522: gate %s NTAG terminator found at "
                            "page %d offset %d",
                            self._gate, current_page, page_data.index(0xFE))
                    break
                current_page += 4
                self._sleep(0.005)
            return user_data
        finally:
            self._release_current_target(reason="user_memory_complete")

    @staticmethod
    def _ndef_tlv_extent(data):
        i = 0
        data_len = len(data)
        while i < data_len:
            tlv_type = data[i]
            if tlv_type == 0x00:
                i += 1
                continue
            if tlv_type == 0xFE:
                return None
            if i + 1 >= data_len:
                return None
            tlv_len = data[i + 1]
            if tlv_len == 0xFF:
                if i + 3 >= data_len:
                    return None
                tlv_len = (data[i + 2] << 8) | data[i + 3]
                value_start = i + 4
            else:
                value_start = i + 2
            value_end = value_start + tlv_len
            if tlv_type == 0x03:
                return value_end, tlv_len
            i = value_end
        return None

    def ntag_read_ndef_user_memory(self, start_page=4, max_pages=16,
                                   max_ndef_pages=135, timeout=0.100):
        """Read enough Type-2 user memory for tag_handler to parse payloads."""
        max_pages = max(4, int(max_pages))
        max_ndef_pages = max(max_pages, int(max_ndef_pages))
        fallback_bytes = max_pages * 4
        max_ndef_bytes = max_ndef_pages * 4
        target_bytes = min(16, fallback_bytes)
        user_data = bytearray()
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
                                "RC522: gate %s NTAG NDEF length=%d requires "
                                "%d bytes; capped at %d bytes",
                                self._gate, ndef_len, target_bytes,
                                max_ndef_bytes)
                        target_bytes = max_ndef_bytes
                elif len(user_data) >= 16:
                    target_bytes = fallback_bytes
                current_page += 4
                if current_page > 255:
                    break
                if len(user_data) < target_bytes:
                    self._sleep(0.005)
            result = user_data[:min(len(user_data), target_bytes)]
            if self._debug >= 4 and ndef_len is not None:
                logger.debug(
                    "RC522: gate %s NTAG NDEF length=%d read=%d bytes",
                    self._gate, ndef_len, len(result))
            return result
        finally:
            self._release_current_target(reason="ndef_user_memory_complete")

    # ─────────────────────────────────────────────────────────────────────────
    # MIFARE Classic authentication and block reads
    # ─────────────────────────────────────────────────────────────────────────

    def _stop_crypto1(self):
        """Clear the MIFARE Crypto1 state so the reader accepts plain RF frames."""
        self._write(_Status2Reg, self._read(_Status2Reg) & 0xF7)

    def mifare_authenticate(self, block_addr, key, uid_bytes,
                            use_key_b=False, timeout=None):
        """Authenticate a MIFARE Classic sector using the RC522 MFAuthent command.

        block_addr  : any block in the target sector (typically the trailer).
        key         : 6-byte Key-A or Key-B (list or bytes).
        uid_bytes   : first 4 bytes of the tag UID (used in the auth handshake).
        Returns True when MFCrypto1On is set (authenticated), False otherwise.
        """
        if self._ensure_selected_target() is None:
            return False
        auth_cmd = _PICC_AUTH_B if use_key_b else _PICC_AUTH_A
        uid = list(uid_bytes or self.current_uid or [])[:4]
        if len(uid) < 4:
            logger.warning(
                "RC522: gate %s MIFARE auth — uid too short (%d bytes)",
                self._gate, len(uid))
            return False
        self._write(_FIFOLevelReg, self._read(_FIFOLevelReg) | 0x80)
        self._write(_CommandReg, _PCD_IDLE)
        for byte in [auth_cmd, block_addr & 0xFF] + list(key)[:6] + uid:
            self._write(_FIFODataReg, byte)
        self._write(_CommandReg, _PCD_MFAUTHENT)
        # Poll for MFCrypto1On (auth ok) or TimerIRq (timeout); 1 ms per step.
        poll_ms = max(25, int((self._transceive_delay if timeout is None
                               else float(timeout)) * 1000))
        for _ in range(poll_ms):
            if self._read(_Status2Reg) & 0x08:
                if self._debug >= 4:
                    logger.debug(
                        "RC522: gate %s MIFARE auth block=%d key_%s OK",
                        self._gate, block_addr, 'B' if use_key_b else 'A')
                return True
            if self._read(_ComIrqReg) & 0x01:
                break
            self._sleep(0.001)
        if self._debug >= 3:
            logger.info(
                "RC522: gate %s MIFARE auth block=%d key_%s failed",
                self._gate, block_addr, 'B' if use_key_b else 'A')
        self._stop_crypto1()
        return False

    def mifare_read_block(self, block_addr, timeout=0.100):
        """Read 16 bytes from a MIFARE Classic block (sector must be authenticated).

        Uses the same TRANSCEIVE+CRC path as ntag_read_page; the RC522 handles
        encryption/decryption transparently when MFCrypto1On is active, and
        RxCRCEn in ModeReg strips the 2 CRC bytes so the FIFO holds only data.
        Returns bytes of length 16, or None on error.
        """
        status, data, bits = self._transceive_crc(
            [_PICC_MIFARE_READ, block_addr & 0xFF], timeout=timeout)
        if status != MI_OK or len(data) < 16:
            if self._debug >= 4:
                logger.debug(
                    "RC522: gate %s MIFARE block %d read failed "
                    "(status=%s bits=%d len=%d)",
                    self._gate, block_addr, 'OK' if status == MI_OK else 'ERR',
                    bits, len(data))
            return None
        return bytes(data[:16])

    def mifare_read_authenticated_blocks(self, sector_keys, sectors,
                                         uid_bytes=None, use_key_b=False):
        """Authenticate sectors and read their data blocks.

        Returns {"uid_bytes": bytes, "blocks": {abs_block: bytes}} matching the
        PN532 shape so tag_handler and the Bambu parser stay reader-agnostic.
        Optional "auth_failed_sectors" and "read_failed_blocks" lists are
        included when those failures occur. use_key_b authenticates with Key
        B instead of Key A (e.g. Creality CFS/K1/K2 sector 1, which uses a
        UID-derived Key B).

        Stops on the first auth or read failure (same policy as PN7160) because
        the RC522 hardware crypto state becomes unreliable after a rejected
        MFAuthent handshake.
        """
        blocks = {}
        auth_failed_sectors = []
        read_failed_blocks = []
        stop_on_failure = False
        try:
            if self._ensure_selected_target(timeout=0.500) is None:
                return {
                    "uid_bytes": bytes(uid_bytes or []),
                    "blocks": blocks,
                    "auth_failed_sectors": list(sectors),
                }
            uid = list(uid_bytes or self.current_uid or [])
            for sector in sectors:
                if stop_on_failure:
                    break
                trailer = sector * 4 + 3
                key = sector_keys[sector] if sector < len(sector_keys) else None
                if key is None:
                    continue
                if not self.mifare_authenticate(trailer, key, uid, use_key_b=use_key_b):
                    auth_failed_sectors.append(sector)
                    if self._debug >= 3:
                        logger.info(
                            "RC522: gate %s MIFARE sector %d auth failed — "
                            "stopping", self._gate, sector)
                    stop_on_failure = True
                    break
                for blk_offset in range(3):
                    block_addr = sector * 4 + blk_offset
                    data = self.mifare_read_block(block_addr)
                    if data is not None:
                        blocks[block_addr] = data
                    else:
                        read_failed_blocks.append(block_addr)
                        if self._debug >= 3:
                            logger.info(
                                "RC522: gate %s MIFARE block %d read failed "
                                "— stopping", self._gate, block_addr)
                        stop_on_failure = True
                        break
            result = {"uid_bytes": bytes(uid_bytes or []), "blocks": blocks}
            if auth_failed_sectors:
                result["auth_failed_sectors"] = auth_failed_sectors
            if read_failed_blocks:
                result["read_failed_blocks"] = read_failed_blocks
            return result
        finally:
            self._stop_crypto1()
            self._release_current_target(reason="mifare_read_complete")

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level debug helpers
    # ─────────────────────────────────────────────────────────────────────────

    def low_level_reg_read(self, reg):
        return self._read(_rc522_reg_value(reg))

    def low_level_reg_write(self, reg, value):
        reg_value = _rc522_reg_value(reg)
        self._write(reg_value, int(value) & 0xFF)
        return self._read(reg_value)

    def low_level_dump_registers(self):
        regs = [
            _CommandReg, _ComIEnReg, _ComIrqReg, _ErrorReg, _Status2Reg,
            _FIFOLevelReg, _ControlReg, _BitFramingReg, _ModeReg,
            _TxControlReg, _TxASKReg, _TModeReg, _TPrescalerReg,
            _TReloadRegH, _TReloadRegL,
        ]
        return [(reg, _REG_NAMES.get(reg, '0x%02X' % reg), self._read(reg))
                for reg in regs]

    def low_level_antenna(self, enable=None):
        before = self._read(_TxControlReg)
        after = before
        if enable is not None:
            if enable:
                after = before | 0x03
            else:
                after = before & ~0x03
            self._write(_TxControlReg, after)
            after = self._read(_TxControlReg)
        return before, after, bool(after & 0x03)

    def low_level_fifo_transceive(self, data, bit_framing=0x00, timeout=None):
        self._write(_BitFramingReg, int(bit_framing) & 0x07)
        return self._transceive(list(data), timeout=timeout)

    def low_level_tag_wake(self, timeout=None):
        return self.low_level_fifo_transceive(
            [_PICC_REQIDL], bit_framing=0x07, timeout=timeout)


# =============================================================================
# RC522 low-level debug command helpers
# =============================================================================

def _rc522_reg_value(value):
    if isinstance(value, int):
        reg = value
    else:
        token = str(value or '').strip().strip('"\'').lower()
        if token.startswith('0x'):
            reg = int(token, 16)
        elif token in _REG_BY_NAME:
            reg = _REG_BY_NAME[token]
        else:
            reg = int(token, 16 if any(c in token for c in 'abcdef') else 10)
    if reg < 0 or reg > 0x3F:
        raise ValueError("RC522 register out of range: %s" % value)
    return reg


def _rc522_parse_hex_bytes(value):
    value = str(value or '').replace(',', ' ').replace(':', ' ').replace('-', ' ')
    data = []
    for token in value.split():
        token = token.strip().strip('"\'')
        if not token:
            continue
        if token.lower().startswith('0x'):
            token = token[2:]
        data.append(int(token, 16) & 0xFF)
    return data


def _rc522_parse_byte(value):
    token = str(value or '').strip().strip('"\'')
    if token.lower().startswith('0x'):
        return int(token, 16) & 0xFF
    return int(token, 16 if any(c in token.lower() for c in 'abcdef') else 10) & 0xFF


def _rc522_hex(data):
    return ' '.join('%02X' % (b & 0xFF) for b in data)


def _rc522_response(gcmd, label, message):
    gcmd.respond_info("[%s]: %s" % (label, message))


def low_level_debug_requested(gcmd):
    return (
        gcmd.get_int("RC522_HELP", 0) or
        gcmd.get("RC522_REGISTER", None) is not None or
        gcmd.get("RC522_REG_READ", None) is not None or
        gcmd.get("RC522_REG_WRITE", None) is not None or
        gcmd.get_int("RC522_DUMP_REGS", 0) or
        gcmd.get("RC522_ANTENNA_ENABLE", None) is not None or
        gcmd.get("RC522_ANTENNA", None) is not None or
        gcmd.get("RC522_FIFO_TRANSCEIVE", None) is not None or
        gcmd.get("RC522_TRANSCEIVE", None) is not None or
        gcmd.get_int("RC522_TAG_WAKE", 0) or
        gcmd.get_int("RC522_REQA", 0) or
        gcmd.get_int("RC522_WAKE", 0))


def low_level_debug_help_lines(command_base):
    return [
        "--- RC522 SPI/register debug ---",
        "%s INIT=1                         - normal RC522 init/antenna enable" % command_base,
        "%s SCAN=1                         - normal REQA + ANTICOLL UID scan" % command_base,
        "%s RC522_DUMP_REGS=1              - read key RC522 registers" % command_base,
        "%s RC522_REGISTER=TxControlReg     - read one register" % command_base,
        "%s RC522_REGISTER=TxControlReg VALUE=83 - write one register, then read back" % command_base,
        "%s RC522_ANTENNA_ENABLE=1          - enable antenna TX bits" % command_base,
        "%s RC522_TAG_WAKE=1                - tag wake probe (7-bit REQA)" % command_base,
        "%s RC522_FIFO_TRANSCEIVE='93 20' BIT_FRAMING=0 - FIFO transceive raw bytes" % command_base,
        "%s RC522_REG_READ=TxControlReg    - read one register" % command_base,
        "%s RC522_REG_WRITE=TxControlReg VALUE=83 - write one register, then read back" % command_base,
        "%s RC522_ANTENNA=1                - enable antenna TX bits" % command_base,
        "%s RC522_ANTENNA=0                - disable antenna TX bits" % command_base,
        "%s RC522_REQA=1                   - tag wake probe (7-bit REQA)" % command_base,
        "%s RC522_TRANSCEIVE='93 20' BIT_FRAMING=0 - FIFO transceive raw bytes" % command_base,
    ]


def _rc522_require_reader(gcmd, reader, label):
    if not hasattr(reader, 'low_level_reg_read'):
        _rc522_response(gcmd, label, "reader does not support RC522 low-level debug")
        return False
    return True


def _rc522_report_transceive(gcmd, label, op, status, data, bits):
    status_text = "OK" if status == MI_OK else "ERR"
    _rc522_response(
        gcmd, label, "%s result: status=%s bits=%d data=%s" %
        (op, status_text, bits, _rc522_hex(data)))
    return status == MI_OK


def _rc522_optional_float(gcmd, name, minval=0.0, maxval=2.0):
    if gcmd.get(name, None) is None:
        return None
    return gcmd.get_float(name, minval=minval, maxval=maxval)


def run_low_level_debug(gcmd, reader, label, command_base, enabled):
    if not low_level_debug_requested(gcmd):
        return False
    if not enabled:
        _rc522_response(gcmd, label, "low_level_debug is disabled in config")
        return True
    if not _rc522_require_reader(gcmd, reader, label):
        return True

    if gcmd.get_int("RC522_HELP", 0):
        gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
        return True

    register = gcmd.get("RC522_REGISTER", None)
    if register is not None:
        reg = _rc522_reg_value(register)
        value_param = gcmd.get("VALUE", None)
        if value_param is None:
            value = reader.low_level_reg_read(reg)
            _rc522_response(
                gcmd, label, "RC522_REGISTER %s (0x%02X) -> 0x%02X" %
                (_REG_NAMES.get(reg, '?'), reg, value))
            return True
        value = _rc522_parse_byte(value_param)
        readback = reader.low_level_reg_write(reg, value)
        _rc522_response(
            gcmd, label,
            "RC522_REGISTER %s (0x%02X) = 0x%02X; readback=0x%02X" %
            (_REG_NAMES.get(reg, '?'), reg, value, readback))
        return True

    reg_read = gcmd.get("RC522_REG_READ", None)
    if reg_read is not None:
        reg = _rc522_reg_value(reg_read)
        value = reader.low_level_reg_read(reg)
        _rc522_response(
            gcmd, label, "RC522_REG_READ %s (0x%02X) -> 0x%02X" %
            (_REG_NAMES.get(reg, '?'), reg, value))
        return True

    reg_write = gcmd.get("RC522_REG_WRITE", None)
    if reg_write is not None:
        value = _rc522_parse_byte(gcmd.get("VALUE", "0"))
        reg = _rc522_reg_value(reg_write)
        readback = reader.low_level_reg_write(reg, value)
        _rc522_response(
            gcmd, label,
            "RC522_REG_WRITE %s (0x%02X) = 0x%02X; readback=0x%02X" %
            (_REG_NAMES.get(reg, '?'), reg, value, readback))
        return True

    if gcmd.get_int("RC522_DUMP_REGS", 0):
        lines = ["[%s]: RC522 register dump" % label]
        for reg, name, value in reader.low_level_dump_registers():
            lines.append("  %-15s 0x%02X = 0x%02X" % (name, reg, value))
        gcmd.respond_info('\n'.join(lines))
        return True

    antenna_value = gcmd.get("RC522_ANTENNA_ENABLE", None)
    antenna_op = "RC522_ANTENNA_ENABLE"
    if antenna_value is None:
        antenna_value = gcmd.get("RC522_ANTENNA", None)
        antenna_op = "RC522_ANTENNA"
    if antenna_value is not None:
        enable = bool(int(str(antenna_value).strip(), 0))
        before, after, enabled_state = reader.low_level_antenna(enable=enable)
        _rc522_response(
            gcmd, label,
            "%s before=0x%02X after=0x%02X enabled=%s" %
            (antenna_op, before, after, enabled_state))
        return True

    tag_wake_op = None
    if gcmd.get_int("RC522_TAG_WAKE", 0):
        tag_wake_op = "RC522_TAG_WAKE"
    elif gcmd.get_int("RC522_REQA", 0):
        tag_wake_op = "RC522_REQA"
    elif gcmd.get_int("RC522_WAKE", 0):
        tag_wake_op = "RC522_WAKE"
    if tag_wake_op is not None:
        timeout = _rc522_optional_float(gcmd, "TIMEOUT")
        status, data, bits = reader.low_level_tag_wake(timeout=timeout)
        if _rc522_report_transceive(gcmd, label, tag_wake_op, status, data, bits):
            if bits == 16 and len(data) >= 2:
                atqa = (data[0] << 8) | data[1]
                _rc522_response(gcmd, label, "%s ATQA=0x%04X" %
                                (tag_wake_op, atqa))
            else:
                _rc522_response(
                    gcmd, label,
                    "%s expected 16 ATQA bits; got bits=%d" %
                    (tag_wake_op, bits))
        return True

    transceive = gcmd.get("RC522_FIFO_TRANSCEIVE", None)
    transceive_op = "RC522_FIFO_TRANSCEIVE"
    if transceive is None:
        transceive = gcmd.get("RC522_TRANSCEIVE", None)
        transceive_op = "RC522_TRANSCEIVE"
    if transceive is not None:
        data = _rc522_parse_hex_bytes(transceive)
        bit_framing = gcmd.get_int("BIT_FRAMING", 0, minval=0, maxval=7)
        timeout = _rc522_optional_float(gcmd, "TIMEOUT")
        status, response, bits = reader.low_level_fifo_transceive(
            data, bit_framing=bit_framing, timeout=timeout)
        _rc522_report_transceive(
            gcmd, label, transceive_op, status, response, bits)
        return True

    gcmd.respond_info('\n'.join(low_level_debug_help_lines(command_base)))
    return True
