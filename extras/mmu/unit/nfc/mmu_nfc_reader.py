# klippy/extras/mmu_nfc_reader.py
#
# mmu_nfc_reader — standalone RFID/NFC reader chip driver for Happy Hare
# Version 1.1.0
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Extracted from a larger NFC gate-management extension. This module keeps
# only the hardware layer: it builds the configured reader chip driver
# (PN532 / PN7160 / RC522) from config and exposes read_tag/read_target as
# both a Python API (for other extras) and GCode commands (for macros).
# It deliberately does not do lane state machines, Spoolman lookups, LED
# effects, or scan-jog motion — those live in your macros if you want them.
#
# Config
# ──────
# [mmu_nfc_reader]                  # optional: shared defaults, no hardware
#   i2c_bus: i2c1                   # shared I2C bus name, if using I2C chips
#   i2c_address: 0x24               # shared I2C address default
#   reader_type: pn532              # default chip type for instances below
#   debug: 2                        # 0=silent .. 4=trace, logged to klippy.log
#
# [mmu_nfc_reader gate0]            # one reader instance; name = "gate0"
#   reader_type: rc522              # pn532 | pn7160 | rc522 (overrides default)
#   cs_pin: mcu:PA4                 # rc522 only (SPI chip-select)
#   #spi_bus:                       # optional, rc522 only
#   #spi_speed: 1000000             # optional, rc522 only
#
# [mmu_nfc_reader gate1]
#   reader_type: pn532
#   i2c_address: 0x24               # pn532/pn7160 only
#   #i2c_bus:
#   #i2c_speed: 100000
#
# GCode commands (per instance, NAME optional if only one instance exists)
# ─────────────────────────────────────────────────────────────────────────
#   MMU_RFID_INIT    [NAME=gate0]              - (re)initialize the reader
#   MMU_RFID_READ    [NAME=gate0] [TIMEOUT=.1] - read once, report UID
#   MMU_RFID_RELEASE [NAME=gate0]              - release the current target
#
# Macro / status access
# ──────────────────────
#   {printer["mmu_nfc_reader gate0"].last_uid}
#   {printer["mmu_nfc_reader gate0"].present}
#   {printer["mmu_nfc_reader gate0"].alive}
#
# Python API (for other extras)
# ──────────────────────────────
#   inst = printer.lookup_object("mmu_nfc_reader gate0")
#   inst.init()                      # (re)initialize, returns bool alive
#   uid, target_info = inst.read(timeout=0.5)
#   inst.release(reason="...")       # returns True if a release was issued

import logging

from . import reader_factory
from . import pn532_driver

_instances = []


# ── Deep-read helpers (tag-type classification + memory shaping) ───────────────

def _classify_target(target_info):
    """Map a reader read_target() dict to a deep-read strategy:
    'mifare_classic' | 'ntag_type2' | 'iso15693_type5' | 'uid_only'."""
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
    # SAK bit 0x08 marks MIFARE Classic-compatible targets; SAK 0x00 is the
    # common Type-2 / Ultralight / NTAG case.
    if sak & 0x08:
        return 'mifare_classic'
    if sak == 0x00 and uid_length in (4, 7, 10):
        return 'ntag_type2'
    return 'uid_only'


def _type5_parser_memory(raw):
    """Strip the 4-byte ISO15693/Type-5 Capability Container so the byte stream
    starts at the TLV area, as parse_tag() expects (like NTAG page 4)."""
    data = bytes(raw or b'')
    if len(data) >= 5 and data[0] in (0xE1, 0xE2):
        return bytearray(data[4:])
    return bytearray(data)


def _mifare_usable(blocks, requested_sectors, allow_partial):
    """True if an authenticated MIFARE read returned decodable blocks. When
    allow_partial (the Bambu probe), at least one requested sector must have
    authenticated; otherwise every requested sector must have."""
    if not blocks or not blocks.get('blocks'):
        return False
    failed = blocks.get('auth_failed_sectors') or []
    if allow_partial:
        return len(failed) < len(requested_sectors)
    return not failed


class MmuNfcReaderDefaults:
    """Shared defaults from the base [mmu_nfc_reader] section, if present."""

    def __init__(self, config):
        self.reader_type = config.get('reader_type', None)
        self.i2c_bus = config.get('i2c_bus', None)
        self.i2c_address = config.getint('i2c_address', 0x24, minval=0, maxval=127)
        self.debug = config.getint('debug', 2, minval=0, maxval=4)
        self.transceive_delay = config.getfloat('transceive_delay', 0.250, minval=0.050, maxval=2.0)
        self.crc_delay = config.getfloat('crc_delay', 0.050, minval=0.005, maxval=1.0)
        self.tag_max_pages = config.getint('tag_max_pages', 16, minval=4, maxval=135)
        self.low_level_debug = pn532_driver.get_low_level_debug(config)


class MmuNfcReader:
    """One [mmu_nfc_reader <name>] instance: one physical reader chip."""

    def __init__(self, config, mmu_unit):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]
        self.mmu_unit = mmu_unit
        self._defaults = self.printer.lookup_object('mmu_nfc_reader', None)

        # Logical gate number, or the mmu_unit name for a shared reader. Not known
        # until the manager calls init(gate); used only as a driver logging label.
        self.gate = None

        default_reader_type = (self._defaults.reader_type if self._defaults and self._defaults.reader_type
                               else reader_factory.DEFAULT_READER_TYPE)
        self.reader_type = reader_factory.reader_type_from_config(config, default=default_reader_type)

        self.debug = config.getint('debug', self._defaults.debug if self._defaults else 2, minval=0, maxval=4)
        transceive_delay = config.getfloat('transceive_delay',
                                           self._defaults.transceive_delay if self._defaults else 0.250,
                                           minval=0.050, maxval=2.0)
        crc_delay = config.getfloat('crc_delay', self._defaults.crc_delay if self._defaults else 0.050,
                                    minval=0.005, maxval=1.0)
        # Max NTAG/Type-5 user-memory pages read during a deep (metadata) read
        self.tag_max_pages = config.getint('tag_max_pages',
                                           self._defaults.tag_max_pages if self._defaults else 16,
                                           minval=4, maxval=135)
        low_level_debug = pn532_driver.get_low_level_debug(config,
                                                           self._defaults.low_level_debug if self._defaults else False)

        # The driver takes a 'gate' label; the manager supplies the real value via
        # init(gate). Seed with the reader name until then.
        self.reader = reader_factory.create_reader(config, self._defaults, self.reader_type, self.name, self.debug,
                                                   low_level_debug=low_level_debug, sleep_fn=self._reactor_sleep,
                                                   transceive_delay=transceive_delay, crc_delay=crc_delay)

        self.alive = False
        self.last_uid = None
        self.last_target_info = None
        self.present = False

        # Register for NAME-based GCode dispatch, replacing any stale same-named
        # instance (e.g. after a restart)
        for i, existing in enumerate(_instances):
            if existing.name == self.name:
                _instances[i] = self
                break
        else:
            _instances.append(self)

        self.printer.register_event_handler('klippy:connect', self._handle_connect)

        self._register_commands()


    def _register_commands(self):
        # Register each command once globally; NAME= (or the sole instance,
        # if there's only one) picks which reader a call targets. Klipper's
        # GCodeDispatch raises on a duplicate register_command call, so the
        # second+ instance registering the same command name is expected
        # and simply skipped.
        for cmd, func, help_text in (
                ('MMU_RFID_INIT', self._cmd_init, "(Re)initialize an RFID reader"),
                ('MMU_RFID_READ', self._cmd_read, "Read a tag once from an RFID reader"),
                ('MMU_RFID_RELEASE', self._cmd_release, "Release the current target on an RFID reader")):
            try:
                self.gcode.register_command(cmd, func, desc=help_text)
            except self.printer.config_error:
                pass


    def _reactor_sleep(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)


    def _handle_connect(self):
        try:
            self.init()
        except Exception:
            self.alive = False
            logging.exception("mmu_nfc_reader %s: init failed", self.name)
        if self.alive:
            logging.info("mmu_nfc_reader %s: %s OK", self.name, self.reader_type)
        else:
            logging.warning("mmu_nfc_reader %s: %s did not respond at connect time", self.name, self.reader_type)


    # ---- Public Python API (no gcmd required) -----------------------------

    def init(self, gate=None):
        """(Re)initialize the reader chip.

        The manager passes 'gate' - the logical gate number for a per-gate
        reader, or the mmu_unit name for a shared reader - which becomes the
        driver's logging label. Updates and returns self.alive. Raises on
        hardware/driver error; callers that just want a best-effort init
        (e.g. GCode handlers) should catch exceptions themselves.
        """
        if gate is not None:
            self.gate = gate
            self.reader._gate = gate # Driver uses this only as a logging label
        # A (re)init starts from a clean slate - no stale sticky read state
        self.last_uid = None
        self.last_target_info = None
        self.present = False
        self.reader.init()
        self.alive = bool(self.reader.is_alive())
        return self.alive


    def read(self, timeout=0.5):
        """Read a tag/target once.

        Returns a (uid, target_info) tuple. target_info is the dict from
        the driver's read_target() if supported, else None. Updates
        last_uid, last_target_info and present as a side effect. Raises on
        hardware/driver error.
        """
        uid = None
        target_info = None
        read_target = getattr(self.reader, 'read_target', None)
        if read_target is not None:
            target_info = read_target(timeout=timeout)
            if target_info is not None:
                uid = target_info.get('uid')
        else:
            uid = self.reader.read_tag(timeout=timeout)
        self.last_uid = uid
        self.last_target_info = target_info
        self.present = uid is not None
        return uid, target_info


    def read_uid(self, timeout=0.5):
        """Read just the tag UID (uppercase hex), or None if no tag is present.

        Uses the driver's read_tag(), which auto-releases the target on readers
        that hold one (PN532/PN7160), leaving the reader clean for the next scan.
        Preferred over read() for simple presence/UID polling - no separate
        release() is needed.
        """
        uid = self.reader.read_tag(timeout=timeout)
        self.last_uid = uid
        self.present = uid is not None
        return uid


    # ---- Homing poll (NFC-as-endstop) -------------------------------------
    #
    # Used only while a gate is homing filament to its NFC reader. The reader's
    # last UID is "sticky", so it's cleared first, then polled tightly with a
    # small timeout (which caps the per-read transceive delay) so each poll stays
    # short enough not to disturb the drip-homing move.

    def clear_uid(self):
        """Forget the sticky last-read UID and release any held target, so the
        next read reflects a fresh, live detection."""
        self.last_uid = None
        self.last_target_info = None
        self.present = False
        try:
            self.release(reason="nfc_homing_clear")
        except Exception:
            pass


    def homing_poll_read(self, timeout=0.020):
        """One fast UID-only poll for the homing loop. Returns the UID hex or None.
        'timeout' caps the driver transceive wait (min(timeout, transceive_delay))
        so a poll stays short; keep it well under the ~50ms drip-segment budget."""
        return self.read_uid(timeout=timeout)


    # ---- Deep read (UID + parsed tag metadata) ----------------------------
    #
    # A "deep read" reads the tag's full memory (NDEF pages / MIFARE sectors)
    # and parses it into a filament metadata dict via tag_parser. It is
    # more expensive than read_uid() and is performed only when Spoolman
    # auto-create is enabled (the manager decides), so the default polling path
    # neither parses tag contents nor produces anything beyond the UID.

    def read_tag_data(self, timeout=0.5):
        """Read a tag and parse its contents.

        Returns (uid, metadata): metadata is the parsed tag dict (material,
        color_hex, brand, weight_g, temps, tag_format, ...) from
        tag_parser, or None if the tag carries no recognised rich data
        or the driver can't do structured reads. Updates last_uid/present.
        """
        read_target = getattr(self.reader, 'read_target', None)
        if read_target is None:
            # Driver has no target concept - can't do a structured read
            return self.read_uid(timeout=timeout), None
        target_info = read_target(timeout=timeout)
        if target_info is None:
            self.last_uid = None
            self.last_target_info = None
            self.present = False
            return None, None
        uid = target_info.get('uid')
        self.last_uid = uid
        self.last_target_info = target_info
        self.present = uid is not None
        if not uid:
            self.release(reason="deep_read_no_uid")
            return None, None
        metadata = None
        try:
            metadata = self._read_tag_metadata(target_info)
        except Exception as e:
            logging.warning("mmu_nfc_reader %s: deep tag read failed: %s", self.name, e)
        return uid, metadata


    def _read_tag_metadata(self, target_info):
        """Capture raw tag memory per tag type and parse it, returning a metadata
        dict or None. Structured reads release the target themselves (driver
        finally blocks); an unsupported target is released here.
        """
        from . import tag_parser as parser
        strategy = _classify_target(target_info)
        if strategy == 'ntag_type2':
            raw = self._capture_ntag()
        elif strategy == 'iso15693_type5':
            raw = self._capture_iso15693(target_info)
        elif strategy == 'mifare_classic':
            raw = self._capture_mifare(target_info)
        else:
            self.release(reason="deep_read_unsupported")
            return None
        if not raw:
            return None
        info = parser.parse_tag(raw, uid_hex=target_info.get('uid'))
        if info is None or parser.is_parse_error(info):
            return None
        return info


    def _capture_ntag(self):
        """Read NTAG/Type-2 user memory from page 4 (NDEF-aware if the driver
        supports it, else a fixed page span)."""
        read_ndef = getattr(self.reader, 'ntag_read_ndef_user_memory', None)
        if read_ndef is not None:
            return read_ndef(start_page=4, max_pages=self.tag_max_pages)
        return self.reader.ntag_read_user_memory(start_page=4, end_page=4 + self.tag_max_pages - 1)


    def _capture_iso15693(self, target_info):
        """Read ISO15693/Type-5 user memory and strip the capability container."""
        read_type5 = getattr(self.reader, 'iso15693_read_user_memory', None)
        if read_type5 is None:
            return None
        return _type5_parser_memory(read_type5(tag=target_info))


    def _capture_mifare(self, target_info):
        """Authenticated MIFARE Classic read, trying keys in order:
          1. Bambu    - HKDF-derived Key A, sectors 0-4 (partial auth still
             identifies a Bambu tag)
          2. Factory default Key A, sectors 0-4 (e.g. QIDI Box)
          3. Creality - UID-derived Key B, sector 1 only
        Each attempt re-selects the tag via the driver. Bambu/Creality key
        derivation needs pycryptodome; if it is missing those attempts are
        skipped. Returns the block dict for the first usable read, or None.
        """
        from . import tag_parser as parser
        uid_bytes = bytes(target_info.get('uid_bytes') or [])
        if len(uid_bytes) < 4:
            return None

        try:
            bambu_keys = parser._bambu_derive_keys(uid_bytes)
        except Exception:
            bambu_keys = None
        if bambu_keys is not None:
            blocks = self.reader.mifare_read_authenticated_blocks(
                bambu_keys, sectors=[0, 1, 2, 3, 4], uid_bytes=uid_bytes)
            if _mifare_usable(blocks, [0, 1, 2, 3, 4], allow_partial=True):
                return blocks

        blocks = self.reader.mifare_read_authenticated_blocks(
            [b'\xff\xff\xff\xff\xff\xff'] * 16, sectors=[0, 1, 2, 3, 4], uid_bytes=uid_bytes)
        if _mifare_usable(blocks, [0, 1, 2, 3, 4], allow_partial=False):
            return blocks

        try:
            creality_key = parser._creality_derive_key_b(uid_bytes)
        except Exception:
            creality_key = None
        if creality_key is not None:
            sector_keys = [None] * 16
            sector_keys[1] = creality_key
            blocks = self.reader.mifare_read_authenticated_blocks(
                sector_keys, sectors=[1], uid_bytes=uid_bytes, use_key_b=True)
            if _mifare_usable(blocks, [1], allow_partial=False):
                return blocks
        return None


    def release(self, reason="manual"):
        """Release the current target, if the driver supports it.

        Returns True if a release call was actually issued to the driver,
        False if the driver has no releasable-target concept.
        """
        release_fn = getattr(self.reader, '_release_current_target', None)
        if release_fn is None:
            return False
        try:
            release_fn(reason=reason)
        except TypeError:
            release_fn()
        self.present = False
        return True


    # ---- GCode commands (module-level dispatch by NAME=) -------------------

    def _cmd_init(self, gcmd):
        _lookup(gcmd, self.name)._do_init(gcmd)

    def _cmd_read(self, gcmd):
        _lookup(gcmd, self.name)._do_read(gcmd)

    def _cmd_release(self, gcmd):
        _lookup(gcmd, self.name)._do_release(gcmd)


    def _do_init(self, gcmd):
        try:
            alive = self.init()
        except Exception as e:
            self.alive = False
            gcmd.respond_info("mmu_nfc_reader %s: init error: %s" % (self.name, e))
            return
        gcmd.respond_info("mmu_nfc_reader %s: %s %s" % (self.name, self.reader_type, "OK" if alive else "not responding"))


    def _do_read(self, gcmd):
        timeout = gcmd.get_float('TIMEOUT', 0.5, minval=0.01, maxval=5.0)
        try:
            uid, _target_info = self.read(timeout=timeout)
        except Exception as e:
            gcmd.respond_info("mmu_nfc_reader %s: read error: %s" % (self.name, e))
            return
        if uid is None:
            gcmd.respond_info("mmu_nfc_reader %s: no tag detected" % self.name)
        else:
            gcmd.respond_info("mmu_nfc_reader %s: UID=%s" % (self.name, uid))


    def _do_release(self, gcmd):
        released = self.release(reason="gcode_manual")
        if not released:
            gcmd.respond_info("mmu_nfc_reader %s: nothing to release" % self.name)
            return
        gcmd.respond_info("mmu_nfc_reader %s: released" % self.name)


    def get_status(self, eventtime=None):
        return {
            'reader_type': self.reader_type,
            'alive': self.alive,
            'present': self.present,
            'last_uid': self.last_uid,
        }


def _lookup(gcmd, default_name):
    name = gcmd.get('NAME', None)
    if name is None:
        if len(_instances) == 1:
            return _instances[0]
        for inst in _instances:
            if inst.name == default_name:
                return inst
        raise gcmd.error("Multiple [mmu_nfc_reader] instances configured; specify NAME=<name>")
    for inst in _instances:
        if inst.name == name:
            return inst
    raise gcmd.error("No mmu_nfc_reader named '%s'" % name)
