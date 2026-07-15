# klippy/extras/mmu_rfid_reader.py
#
# mmu_rfid_reader — standalone RFID/NFC reader chip driver for Happy Hare
# Version 1.0.0
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
# [mmu_rfid_reader]                 # optional: shared defaults, no hardware
#   i2c_bus: i2c1                   # shared I2C bus name, if using I2C chips
#   i2c_address: 0x24               # shared I2C address default
#   reader_type: pn532              # default chip type for instances below
#   debug: 2                        # 0=silent .. 4=trace, logged to klippy.log
#
# [mmu_rfid_reader lane0]           # one reader instance; name = "lane0"
#   reader_type: rc522              # pn532 | pn7160 | rc522 (overrides default)
#   cs_pin: mcu:PA4                 # rc522 only (SPI chip-select)
#   #spi_bus:                       # optional, rc522 only
#   #spi_speed: 1000000             # optional, rc522 only
#
# [mmu_rfid_reader lane1]
#   reader_type: pn532
#   i2c_address: 0x24               # pn532/pn7160 only
#   #i2c_bus:
#   #i2c_speed: 100000
#
# GCode commands (per instance, NAME optional if only one instance exists)
# ─────────────────────────────────────────────────────────────────────────
#   MMU_RFID_INIT    [NAME=lane0]              - (re)initialize the reader
#   MMU_RFID_READ    [NAME=lane0] [TIMEOUT=.1] - read once, report UID
#   MMU_RFID_RELEASE [NAME=lane0]              - release the current target
#
# Macro / status access
# ──────────────────────
#   {printer["mmu_rfid_reader lane0"].last_uid}
#   {printer["mmu_rfid_reader lane0"].present}
#   {printer["mmu_rfid_reader lane0"].alive}

import logging

from .nfc import reader_factory
from .nfc import pn532_driver

# Tracks which printer object owns _instances, mirroring the reset-on-RESTART
# pattern from the source project: a new Printer is created on every Klipper
# RESTART, so a change here means a fresh config load and stale entries must
# be dropped.
_current_printer = None
_instances = []


class MmuRfidReaderDefaults:
    """Shared defaults from the base [mmu_rfid_reader] section, if present."""

    def __init__(self, config):
        self.reader_type = config.get('reader_type', None)
        self.i2c_bus = config.get('i2c_bus', None)
        self.i2c_address = config.getint(
            'i2c_address', 0x24, minval=0, maxval=127)
        self.debug = config.getint('debug', 2, minval=0, maxval=4)
        self.transceive_delay = config.getfloat(
            'transceive_delay', 0.250, minval=0.050, maxval=2.0)
        self.crc_delay = config.getfloat(
            'crc_delay', 0.050, minval=0.005, maxval=1.0)
        self.low_level_debug = pn532_driver.get_low_level_debug(config)


class MmuRfidReader:
    """One [mmu_rfid_reader <name>] instance: one physical reader chip."""

    def __init__(self, config, defaults, index):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]
        self._index = index
        self._defaults = defaults

        default_reader_type = (
            defaults.reader_type if defaults and defaults.reader_type
            else reader_factory.DEFAULT_READER_TYPE)
        self.reader_type = reader_factory.reader_type_from_config(
            config, default=default_reader_type)

        self.debug = config.getint(
            'debug', defaults.debug if defaults else 2, minval=0, maxval=4)
        transceive_delay = config.getfloat(
            'transceive_delay',
            defaults.transceive_delay if defaults else 0.250,
            minval=0.050, maxval=2.0)
        crc_delay = config.getfloat(
            'crc_delay', defaults.crc_delay if defaults else 0.050,
            minval=0.005, maxval=1.0)
        low_level_debug = pn532_driver.get_low_level_debug(
            config, defaults.low_level_debug if defaults else False)

        self.reader = reader_factory.create_reader(
            config, defaults, self.reader_type, index, self.debug,
            low_level_debug=low_level_debug,
            sleep_fn=self._reactor_sleep,
            transceive_delay=transceive_delay,
            crc_delay=crc_delay)

        self.alive = False
        self.last_uid = None
        self.last_target_info = None
        self.present = False

        self.printer.register_event_handler(
            'klippy:connect', self._handle_connect)

        self._register_commands()

    def _register_commands(self):
        # Register each command once globally; NAME= (or the sole instance,
        # if there's only one) picks which reader a call targets. Klipper's
        # GCodeDispatch raises on a duplicate register_command call, so the
        # second+ instance registering the same command name is expected
        # and simply skipped.
        for cmd, func, help_text in (
                ('MMU_RFID_INIT', self._cmd_init,
                 "(Re)initialize an RFID reader"),
                ('MMU_RFID_READ', self._cmd_read,
                 "Read a tag once from an RFID reader"),
                ('MMU_RFID_RELEASE', self._cmd_release,
                 "Release the current target on an RFID reader")):
            try:
                self.gcode.register_command(cmd, func, desc=help_text)
            except self.printer.config_error:
                pass

    def _reactor_sleep(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def _handle_connect(self):
        try:
            self.reader.init()
            self.alive = bool(self.reader.is_alive())
        except Exception:
            self.alive = False
            logging.exception(
                "mmu_rfid_reader %s: init failed", self.name)
        if self.alive:
            logging.info("mmu_rfid_reader %s: %s OK",
                         self.name, self.reader_type)
        else:
            logging.warning(
                "mmu_rfid_reader %s: %s did not respond at connect time",
                self.name, self.reader_type)

    # ---- GCode commands (module-level dispatch by NAME=) -----------------

    def _cmd_init(self, gcmd):
        _lookup(gcmd, self.name)._do_init(gcmd)

    def _cmd_read(self, gcmd):
        _lookup(gcmd, self.name)._do_read(gcmd)

    def _cmd_release(self, gcmd):
        _lookup(gcmd, self.name)._do_release(gcmd)

    def _do_init(self, gcmd):
        try:
            self.reader.init()
            self.alive = bool(self.reader.is_alive())
        except Exception as e:
            self.alive = False
            gcmd.respond_info(
                "mmu_rfid_reader %s: init error: %s" % (self.name, e))
            return
        gcmd.respond_info(
            "mmu_rfid_reader %s: %s %s" %
            (self.name, self.reader_type,
             "OK" if self.alive else "not responding"))

    def _do_read(self, gcmd):
        timeout = gcmd.get_float('TIMEOUT', 0.5, minval=0.01, maxval=5.0)
        uid = None
        target_info = None
        try:
            read_target = getattr(self.reader, 'read_target', None)
            if read_target is not None:
                target_info = read_target(timeout=timeout)
                if target_info is not None:
                    uid = target_info.get('uid')
            else:
                uid = self.reader.read_tag(timeout=timeout)
        except Exception as e:
            gcmd.respond_info(
                "mmu_rfid_reader %s: read error: %s" % (self.name, e))
            return
        self.last_uid = uid
        self.last_target_info = target_info
        self.present = uid is not None
        if uid is None:
            gcmd.respond_info(
                "mmu_rfid_reader %s: no tag detected" % self.name)
        else:
            gcmd.respond_info(
                "mmu_rfid_reader %s: UID=%s" % (self.name, uid))

    def _do_release(self, gcmd):
        release = getattr(self.reader, '_release_current_target', None)
        if release is None:
            gcmd.respond_info(
                "mmu_rfid_reader %s: nothing to release" % self.name)
            return
        try:
            release(reason="gcode_manual")
        except TypeError:
            release()
        self.present = False
        gcmd.respond_info("mmu_rfid_reader %s: released" % self.name)

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
        raise gcmd.error(
            "Multiple [mmu_rfid_reader] instances configured; "
            "specify NAME=<name>")
    for inst in _instances:
        if inst.name == name:
            return inst
    raise gcmd.error("No mmu_rfid_reader named '%s'" % name)


def load_config(config):
    # Handles the base [mmu_rfid_reader] section - shared defaults only.
    global _current_printer
    _current_printer = config.get_printer()
    del _instances[:]
    return MmuRfidReaderDefaults(config)


def load_config_prefix(config):
    # Handles [mmu_rfid_reader lane0], [mmu_rfid_reader lane1], etc.
    global _current_printer
    printer = config.get_printer()
    if printer is not _current_printer:
        _current_printer = printer
        del _instances[:]
    defaults = printer.lookup_object('mmu_rfid_reader', None)
    index = len(_instances)
    reader = MmuRfidReader(config, defaults, index)
    for i, existing in enumerate(_instances):
        if existing.name == reader.name:
            _instances[i] = reader
            return reader
    _instances.append(reader)
    return reader
