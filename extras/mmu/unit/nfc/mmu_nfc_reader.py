# klippy/extras/mmu_nfc_reader.py
#
# mmu_nfc_reader — standalone RFID/NFC reader chip driver for Happy Hare
# Version 1.1.0
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Extracted from a larger NFC gate-management extension. This module keeps
# only the hardware layer: it builds the configured reader chip driver
# (PN532 / PN5180 / PN7160 / RC522) from config and exposes read_tag/read_target as
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
#   #interface: i2c                 # default transport for instances below
#   debug: 2                        # 0=silent .. 4=trace, for THIS reader, to klippy.log
#   #transceive_delay: 0.250        # pn532/pn7160 tag-wait (min 0.050)
#   #crc_delay: 0.050               # pn532 InRelease wait
#   #tag_max_pages: 16              # NTAG/Type-5 pages read during a deep read (4..135)
#   #rx_gain: 0                      # 0=chip default; valid dB values depend on reader_type
#
# [mmu_nfc_reader gate0]            # one reader instance; name = "gate0"
#   reader_type: rc522              # pn532 | pn5180 | pn7160 | rc522 (overrides default)
#   cs_pin: mcu:PA4                 # rc522/pn5180 only (SPI chip-select)
#   #spi_bus:                       # optional, rc522/pn5180 - omit for the MCU default bus
#   #spi_speed: 1000000             # optional, rc522/pn5180 only
#   #rc522_transceive_delay: 0.035  # rc522 only, per-transceive tag wait
#
# [mmu_nfc_reader gate1]
#   reader_type: pn5180
#   cs_pin: mcu:PA5
#   busy_pin: mcu:PB0               # pn5180 only, required (BUSY, active high)
#   reset_pin: mcu:PB1              # pn5180 only, required (RST, active low)
#
# [mmu_nfc_reader gate2]
#   reader_type: pn532
#   #interface: i2c                 # i2c | spi | uart - see below. Default per chip
#   i2c_address: 0x24               # pn532/pn7160 only
#   #i2c_mcu: mcu                   # which MCU owns the bus (default 'mcu')
#   #i2c_bus:
#   #i2c_speed: 100000              # Klipper rejects anything below 100000
#   #ven_pin: mcu:PG13              # pn7160 only, optional hardware enable/reset
#   #irq_pin: mcu:PG14              # pn7160 only, optional - recommended for tag homing.
#                                   # Wired, the presence probe asks the IRQ line and costs
#                                   # no bus traffic at all; without it the probe reads on
#                                   # spec once per tick, which works but needs a Klipper
#                                   # new enough to report an I2C NACK
#
# Transport selection - 'interface'
# ─────────────────────────────────
# Each chip defaults to the transport it has always used here, so leaving this out
# changes nothing. What is implemented:
#
#   pn532   i2c (default) | uart | spi     spi is UNTESTED - warns at startup
#   pn7160  i2c
#   pn5180  spi
#   rc522   spi
#
# Several of these chips speak other interfaces in silicon; this option only selects
# among the DRIVERS that exist, and says so if you pick one that does not.
#
# PN532 over HSU/UART - pn532 + interface: uart
# ─────────────────────────────────────────────
# HSU is the PN532's UART mode, reached over a USB-serial adapter on the host:
#
# [mmu_nfc_reader gate0]
#   reader_type: pn532
#   interface: uart
#   serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
#   #baud: 115200                   # chip powers up here; faster needs a command
#                                   # this driver does not send
#
# Set the breakout board's mode pads to HSU: SEL0=0, SEL1=1 (sometimes labeled
# A0/A1). Wire adapter TX->PN532 RX, RX->TX, plus GND and 3V3/5V per your board.
#
# Notes:
#   - This is the ONE transport that does not go through the MCU. The klippy process
#     opens the serial port itself, so the reader plugs into the host (Pi), not into
#     an MMU/toolhead board. Everything else here is driven over I2C/SPI by an MCU.
#   - Use the /dev/serial/by-id/ path. /dev/ttyUSB0 is not stable across reboots or
#     replugs, and swapping with another adapter is a confusing failure.
#   - One reader per port, exclusively - two readers on one tty would interleave
#     frames on a single stream. So UART suits ONE shared reader, or a small number
#     of gates with an adapter each; software I2C (below) is the answer for a
#     reader-per-gate build.
#   - A missing or unplugged adapter does not stop klippy from starting: the port
#     opens during reader init, and the reader is simply reported not alive. Replug
#     and run MMU_RFID_INIT to recover.
#
# Software (bit-banged) I2C - pn532/pn7160
# ────────────────────────────────────────
# Klipper can drive I2C on any two GPIO pins instead of a hardware bus. Use it when
# you need MORE THAN ONE reader of the same type: a PN532 is fixed at address 0x24,
# so two of them cannot share a bus. Give each its own pin pair and each becomes its
# own private bus, so the address no longer collides:
#
# [mmu_nfc_reader gate0]
#   reader_type: pn532
#   i2c_address: 0x24
#   i2c_software_scl_pin: mmu:PB8   # instead of i2c_bus
#   i2c_software_sda_pin: mmu:PB9
#
# [mmu_nfc_reader gate1]
#   reader_type: pn532
#   i2c_address: 0x24               # same address, different bus - fine
#   i2c_software_scl_pin: mmu:PC4
#   i2c_software_sda_pin: mmu:PC5
#
# Notes:
#   - Specify EITHER i2c_bus OR the two software pins. Klipper checks the software
#     pins first, so an i2c_bus alongside them is silently ignored.
#   - Both pins are required, must differ, and must be on i2c_mcu. reader_factory
#     checks all of this because Klipper does not - it accepts a blank pin, and it
#     lets two readers share a pin pair without complaint.
#   - Slower than hardware I2C, and it supports neither clock stretching nor bus
#     timeouts: a wedged bus returns bad data rather than raising. Each software bus
#     needs its own pull-up resistors on SCL and SDA.
#   - Software SPI works too, for rc522/pn5180 - Klipper accepts
#     spi_software_sclk_pin / spi_software_mosi_pin / spi_software_miso_pin on these
#     sections already. There is no menuconfig option for it; hand-edit if wanted.
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

from . import log as reader_log
from . import reader_factory
from . import pn532_driver

_instances = []

# Bounded read_target() wait used by the blocking probe shim (seconds), for
# drivers that don't implement the non-blocking probe contract. Sized to one
# shim-rate homing poll tick - see the "Homing presence probe" section below.
PROBE_SHIM_TIMEOUT = 0.050


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
        self.interface = config.get('interface', None)
        self.i2c_bus = config.get('i2c_bus', None)
        self.i2c_address = config.getint('i2c_address', 0x24, minval=0, maxval=127)
        self.debug = config.getint('debug', 2, minval=0, maxval=4)
        self.transceive_delay = config.getfloat('transceive_delay', 0.250, minval=0.050, maxval=2.0)
        self.crc_delay = config.getfloat('crc_delay', 0.050, minval=0.005, maxval=1.0)
        self.tag_max_pages = config.getint('tag_max_pages', 16, minval=4, maxval=135)
        self.rx_gain = config.getint('rx_gain', 0, minval=0)
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
        # until the manager calls init(gate). This is the only record of which gate
        # this reader serves - the driver's own log lines carry self.name instead.
        self.gate = None

        default_reader_type = (self._defaults.reader_type if self._defaults and self._defaults.reader_type
                               else reader_factory.DEFAULT_READER_TYPE)
        self.reader_type = reader_factory.reader_type_from_config(config, default=default_reader_type)
        # Which host<->chip transport this reader uses. Defaults per chip, so configs
        # that never mention 'interface' keep the transport they already had.
        default_interface = (self._defaults.interface if self._defaults and self._defaults.interface
                             else None)
        self.interface = reader_factory.interface_from_config(config, self.reader_type,
                                                              default=default_interface)

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
        self.rx_gain = reader_factory.rx_gain_from_config(
            config, self.reader_type,
            default=self._defaults.rx_gain if self._defaults else 0)
        low_level_debug = pn532_driver.get_low_level_debug(
            config, self._defaults.low_level_debug if self._defaults else False)
        if self.rx_gain and low_level_debug:
            raise config.error(
                "[mmu_nfc_reader %s]: rx_gain cannot be used with low_level_debug; "
                "low_level_debug deliberately suppresses all driver-initiated chip traffic"
                % self.name)

        # The driver labels its own log lines with this section's name, which it
        # derives from config itself - nothing to pass in or patch later.
        self.reader = reader_factory.create_reader(config, self._defaults, self.reader_type, self.debug,
                                                   low_level_debug=low_level_debug, sleep_fn=self._reactor_sleep,
                                                   transceive_delay=transceive_delay, crc_delay=crc_delay,
                                                   interface=self.interface)

        self.alive = False
        self.last_uid = None
        self.last_target_info = None
        self.present = False
        # Why the last deep read produced no metadata, or None if it did not fail.
        # Distinguishes "parse/auth failed" from "tag carries no rich data".
        self.last_deep_error = None

        # Register for NAME-based GCode dispatch, replacing any stale same-named
        # instance (e.g. after a restart)
        for i, existing in enumerate(_instances):
            if existing.name == self.name:
                _instances[i] = self
                break
        else:
            _instances.append(self)

        # No klippy:connect handler: reader initialization is owned by MmuNfcManager,
        # which schedules it a short delay after MMU bootup so other I2C devices have
        # settled first (see MmuNfcManager._delayed_bootup_init).

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


    # ---- Public Python API (no gcmd required) -----------------------------

    def init(self, gate=None):
        """(Re)initialize the reader chip.

        The manager passes 'gate' - the logical gate number for a per-gate
        reader, or the mmu_unit name for a shared reader. Updates and returns
        self.alive. Raises on hardware/driver error; callers that just want a
        best-effort init (e.g. GCode handlers) should catch exceptions themselves.
        """
        if gate is not None:
            self.gate = gate
            # Driver log lines carry the reader name, not the gate, so record the
            # binding once here - otherwise klippy.log alone cannot tell you which
            # gate a reader named 'left' or 'right' actually serves.
            reader_log.info("[mmu_nfc_reader %s] init: gate=%s", self.name, gate)
        # A (re)init starts from a clean slate - no stale sticky read state
        self.last_uid = None
        self.last_target_info = None
        self.present = False
        self.reader.init()
        self._apply_rx_gain()
        self.alive = bool(self.reader.is_alive())
        return self.alive


    def _apply_rx_gain(self):
        """Apply the static startup gain after the driver's reset/init sequence."""
        if not self.rx_gain:
            return
        set_rx_gain = getattr(self.reader, 'set_rx_gain', None)
        if set_rx_gain is None or not set_rx_gain(self.rx_gain):
            reader_log.warning(
                "[mmu_nfc_reader %s] rx_gain=%ddB was not applied by %s",
                self.name, self.rx_gain, self.reader_type)


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


    # ---- Homing presence probe (NFC-as-endstop) ---------------------------
    #
    # Used only while a gate is homing filament to its NFC reader. A drip-homing
    # move needs the reactor back promptly, so the probe asks "is a tag here?"
    # and nothing more - it does NOT read the UID. The endstop only needs a
    # boolean to complete its homing completion, and the gate map gets its UID
    # from the stationary post-move read (MmuNfcManager.read_gate_after_home).
    #
    # Why a probe rather than a UID read, in reactor terms: a read cannot return
    # until it has an answer, and "nothing there" is the most expensive answer a
    # reader gives - which is the answer every poll gets until the very last one.
    # On PN532 there is no "nothing there" at all: InListPassiveTarget retries
    # activation forever (MxRtyPassiveActivation defaults 0xFF), so the read sat
    # yielding every 5ms until its own 250ms timeout expired, ~50 pause/resume
    # cycles per poll, and then abandoned a command still running on the chip -
    # whose late reply the next command's ACK wait then read as garbage. A probe
    # tick is one bus transaction and a real return, so "not yet" costs nothing
    # and nothing is ever abandoned.
    #
    # Drivers implementing the non-blocking contract (probe_start / probe_poll /
    # probe_stop) are driven directly, one bus transaction per tick. Any driver
    # that doesn't falls back to the shim here: one bounded read_target() per
    # tick. The shim still blocks, so it is no better than the old path on a miss
    # - the manager just polls it at a slower interval (see
    # MmuNfcManager._homing_poll_interval) rather than making things worse by
    # ticking a blocking read faster.

    def clear_uid(self):
        """Forget the sticky last-read UID and release any held target, so the
        next read reflects a fresh, live detection."""
        self.last_uid = None
        self.last_target_info = None
        self.present = False


    def has_probe_support(self):
        """True if the driver implements the full non-blocking probe contract.

        A driver may also expose probe_supported() to answer per *configuration*
        rather than per class - PN7160 probes via its IRQ line when one is wired and
        via a speculative read otherwise, so its answer depends on wiring, on config,
        and on whether the MCU firmware can report an I2C NACK rather than shutting
        down on one.
        """
        if not all(callable(getattr(self.reader, name, None))
                   for name in ('probe_start', 'probe_poll', 'probe_stop')):
            return False
        supported = getattr(self.reader, 'probe_supported', None)
        if callable(supported):
            try:
                return bool(supported())
            except Exception:
                return False
        return True


    def probe_start(self):
        """Kick off one presence scan.

        A no-op for the shim, which does all its work in probe_poll(). Returns
        True if a scan is now underway (or the shim will run one next tick).
        """
        if not self.has_probe_support():
            return True
        try:
            return bool(self.reader.probe_start())
        except Exception as e:
            reader_log.warning("[mmu_nfc_reader %s] probe_start failed: %s", self.name, e)
            return False


    def probe_poll(self):
        """Non-blocking presence check driven by the manager's homing poll.

        Returns True (tag present), False (scan finished, nothing there) or None
        (still scanning - ask again next tick).

        Deliberately updates NO reader state. A probe is not a read, so last_uid
        and last_target_info stay as they were and 'present' is left alone:
        get_status() publishes 'present' and 'uid' as a pair, and reporting
        present=True with uid=None is a combination no consumer has seen. Sensor
        state moves only via the endstop's trigger_handler().
        """
        if self.has_probe_support():
            try:
                return self.reader.probe_poll()
            except Exception as e:
                reader_log.warning("[mmu_nfc_reader %s] probe_poll failed: %s", self.name, e)
                return False
        # Shim: one bounded blocking scan per tick. Never returns None - a
        # blocking read always has an answer. No release here; probe_stop()
        # owns that.
        read_target = getattr(self.reader, 'read_target', None)
        if read_target is None:
            return bool(self.reader.read_tag(timeout=PROBE_SHIM_TIMEOUT))
        return read_target(timeout=PROBE_SHIM_TIMEOUT) is not None


    def probe_stop(self):
        """Abort/drain any scan in flight, leaving the chip clean for a normal
        read_target(). Idempotent - both _homing_poll and home_wait call it."""
        if self.has_probe_support():
            try:
                self.reader.probe_stop()
            except Exception as e:
                reader_log.warning("[mmu_nfc_reader %s] probe_stop failed: %s", self.name, e)
            return
        # Shim: read_target() selects a target on a hit, so release it here so
        # nothing is held into the next operation.
        self.release(reason="probe_stop")


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

        The UID is banked (and returned) BEFORE any metadata work, so a deep read
        that fails costs the metadata and never the UID - the UID is the datum the
        gate map and Spoolman lookup actually need. 'metadata is None' alone can't
        tell a failure from a tag that simply carries no rich data, so the reason is
        recorded in last_deep_error for the manager to report.
        """
        self.last_deep_error = None
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
            self.last_deep_error = str(e) or e.__class__.__name__
            reader_log.warning("[mmu_nfc_reader %s] deep tag read failed: %s", self.name, e)
        return uid, metadata


    def _parse_trace(self, uid_hex):
        """Bridge tag_parser's internal trace() diagnostics into klippy.log.

        parse_tag() reports every format it tries and why each one was rejected
        (including the Creality AES steps: key derivation, sector-1 block
        presence, decrypt result) through an optional trace callback. Without it
        a failed parse is completely silent. Conclusions ('info') are always
        logged; the verbose per-step detail ('debug', which includes tag hex
        dumps) needs debug: 3 on the reader.
        """
        verbose = self.debug >= 3

        def _trace(level, msg, *args):
            if level != 'info' and not verbose:
                return
            try:
                text = msg % args if args else msg
            except Exception:
                text = "%s %r" % (msg, args)
            reader_log.info("[mmu_nfc_reader %s] uid=%s parse: %s", self.name, uid_hex, text)

        return _trace


    def _read_tag_metadata(self, target_info):
        """Capture raw tag memory per tag type and parse it, returning a metadata
        dict or None. Structured reads release the target themselves (driver
        finally blocks); an unsupported target is released here.

        Every failure path logs its own distinct reason - a deep read that yields
        no metadata is otherwise indistinguishable from one that never ran.
        """
        from . import tag_parser as parser
        uid_hex = target_info.get('uid')
        strategy = _classify_target(target_info)
        reader_log.info(
            "[mmu_nfc_reader %s] deep read uid=%s strategy=%s SAK=0x%02X ATQA=0x%04X uid_len=%d",
            self.name, uid_hex, strategy,
            int(target_info.get('sak', 0) or 0),
            int(target_info.get('atqa', target_info.get('sens_res', 0)) or 0),
            int(target_info.get('uid_length', 0)))
        if strategy == 'ntag_type2':
            raw = self._capture_ntag()
        elif strategy == 'iso15693_type5':
            raw = self._capture_iso15693(target_info)
        elif strategy == 'mifare_classic':
            raw = self._capture_mifare(target_info)
        else:
            reader_log.info("[mmu_nfc_reader %s] uid=%s deep read skipped - unsupported target type",
                         self.name, uid_hex)
            self.release(reason="deep_read_unsupported")
            return None
        if not raw:
            reader_log.info("[mmu_nfc_reader %s] uid=%s %s capture returned no data - no metadata",
                         self.name, uid_hex, strategy)
            return None
        if isinstance(raw, dict):
            reader_log.info("[mmu_nfc_reader %s] uid=%s captured %d authenticated block(s): %s",
                         self.name, uid_hex, len(raw.get('blocks') or {}),
                         sorted((raw.get('blocks') or {}).keys()))
        else:
            reader_log.info("[mmu_nfc_reader %s] uid=%s captured %d raw byte(s)",
                         self.name, uid_hex, len(raw))
        info = parser.parse_tag(raw, uid_hex=uid_hex, trace=self._parse_trace(uid_hex))
        if info is None:
            reader_log.info("[mmu_nfc_reader %s] uid=%s parse_tag matched no known tag format",
                         self.name, uid_hex)
            return None
        if parser.is_parse_error(info):
            reader_log.info("[mmu_nfc_reader %s] uid=%s parse_tag reported an error: %s",
                         self.name, uid_hex, info.get('error'))
            return None
        reader_log.info("[mmu_nfc_reader %s] uid=%s parsed tag_format=%s material=%s brand=%s color=%s",
                     self.name, uid_hex, info.get('tag_format'), info.get('material'),
                     info.get('brand'), info.get('color_hex'))
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
        Every read releases its target on completion, so a later attempt would
        find none; the drivers re-select themselves via _ensure_active_target()
        (and reject a re-selection that picked up a different tag). 
        Bambu/Creality key derivation needs pycryptodome; if it is missing those attempts are
        skipped. Returns the block dict for the first usable read, or None.
        Propagates the driver error if the tag leaves the field mid-sequence.
        """
        from . import tag_parser as parser
        uid_bytes = bytes(target_info.get('uid_bytes') or [])
        uid_hex = target_info.get('uid')
        if len(uid_bytes) < 4:
            reader_log.info("[mmu_nfc_reader %s] uid=%s MIFARE read skipped - UID too short (%d bytes)",
                         self.name, uid_hex, len(uid_bytes))
            return None

        def _log_attempt(attempt, blocks, usable):
            # auth_failed_sectors on every requested sector is the signature of a
            # wrong key OR of no selected target (the PN532 primitives refuse to
            # run without one) - the block count distinguishes them.
            reader_log.info(
                "[mmu_nfc_reader %s] uid=%s MIFARE attempt '%s': usable=%s blocks=%d "
                "auth_failed_sectors=%s read_failed_blocks=%s",
                self.name, uid_hex, attempt, usable,
                len((blocks or {}).get('blocks') or {}),
                (blocks or {}).get('auth_failed_sectors') or [],
                (blocks or {}).get('read_failed_blocks') or [])

        try:
            bambu_keys = parser._bambu_derive_keys(uid_bytes)
        except Exception as e:
            # Almost always a missing pycryptodome; previously discarded silently
            reader_log.info("[mmu_nfc_reader %s] uid=%s Bambu key derivation unavailable - "
                         "skipping attempt 'bambu': %s", self.name, uid_hex, e)
            bambu_keys = None
        if bambu_keys is not None:
            blocks = self.reader.mifare_read_authenticated_blocks(
                bambu_keys, sectors=[0, 1, 2, 3, 4], uid_bytes=uid_bytes)
            usable = _mifare_usable(blocks, [0, 1, 2, 3, 4], allow_partial=True)
            _log_attempt('bambu', blocks, usable)
            if usable:
                return blocks

        blocks = self.reader.mifare_read_authenticated_blocks(
            [b'\xff\xff\xff\xff\xff\xff'] * 16, sectors=[0, 1, 2, 3, 4], uid_bytes=uid_bytes)
        usable = _mifare_usable(blocks, [0, 1, 2, 3, 4], allow_partial=False)
        _log_attempt('default_key', blocks, usable)
        if usable:
            return blocks

        try:
            creality_key = parser._creality_derive_key_b(uid_bytes)
        except Exception as e:
            reader_log.info("[mmu_nfc_reader %s] uid=%s Creality Key B derivation unavailable - "
                         "skipping attempt 'creality': %s", self.name, uid_hex, e)
            creality_key = None
        if creality_key is not None:
            sector_keys = [None] * 16
            sector_keys[1] = creality_key
            blocks = self.reader.mifare_read_authenticated_blocks(
                sector_keys, sectors=[1], uid_bytes=uid_bytes, use_key_b=True)
            usable = _mifare_usable(blocks, [1], allow_partial=False)
            _log_attempt('creality', blocks, usable)
            if usable:
                return blocks
        reader_log.info("[mmu_nfc_reader %s] uid=%s all MIFARE key attempts failed - no metadata",
                     self.name, uid_hex)
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
            'interface': self.interface,
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
