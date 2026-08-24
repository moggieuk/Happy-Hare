# Happy Hare test harness - machine profiles.
#
# A profile is a set of Kconfig symbol overrides, exactly as menuconfig would
# produce, from which the REAL shipped templates under config/ are rendered. We do
# not hand-write .cfg fixtures: the ones already in test/installer/ demonstrably
# rotted (they are still on the v3.00 `{param_x}` placeholder format with sections
# that no longer exist), and rendering the real templates means a renamed parameter
# or a broken [% if %] guard shows up as a bootup failure.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os


class UnitProfile:
    """
    One unit of a multi-unit machine: its own Kconfig symbols, plus the identity env
    install.sh:401-432 hands each unit (UNIT_NAME / MCU_NAME / UNIT_INDEX).

    A unit gets a SEPARATE Kconfig parse, not a slice of a shared one - see the MULTI-UNIT
    note in cfg.py for why that is forced rather than chosen.

    index must match the unit's position in Profile.units: it becomes UNIT_INDEX, which
    Kconfig.name:6 uses to build the default display name ('BoxTurtle-0', 'VVD-1', ...).
    mcu_name defaults to name, as it does in install.sh:421-422.
    """

    def __init__(self, name, syms=None, index=0, mcu_name=None):
        self.name = name
        self.syms = dict(syms or {})
        self.index = index
        self.mcu_name = mcu_name or name

    def derive(self, name=None, syms=None, index=None, mcu_name=None):
        merged = dict(self.syms)
        merged.update(syms or {})
        return UnitProfile(name or self.name, merged,
                           self.index if index is None else index,
                           mcu_name or self.mcu_name)

    def __repr__(self):
        return 'UnitProfile(%r)' % (self.name,)


class Profile:
    """
    syms:         Kconfig symbol -> value. True/False for bool/tristate, str/int
                  otherwise. Applied in order, so a later entry can refine an
                  earlier one. On a MULTI-UNIT profile these are the shared
                  (entry-point) symbols only; per-unit ones live in `units`.
    extra_params: jinja render params applied AFTER the Kconfig dict, for the
                  handful the installer computes rather than stores
                  (PARAM_TOTAL_NUM_GATES is filled in automatically by
                  cfg.render(), as the cross-unit sum where that applies).
    units:        list of UnitProfile, or None/empty for a single-unit machine.
                  Supplying it is what selects cfg.py's multi-unit render path;
                  every profile below except ERCF_VVD is the one-unit case.
    """

    def __init__(self, name, syms=None, extra_params=None, description='', units=None):
        self.name = name
        self.syms = dict(syms or {})
        self.extra_params = dict(extra_params or {})
        self.description = description
        self.units = list(units or [])

    def derive(self, name, syms=None, extra_params=None, description='', units=None):
        merged_syms = dict(self.syms)
        merged_syms.update(syms or {})
        merged_params = dict(self.extra_params)
        merged_params.update(extra_params or {})
        return Profile(name, merged_syms, merged_params, description or self.description,
                       units if units is not None else self.units)

    def __repr__(self):
        return 'Profile(%r)' % (self.name,)


def clone_across_units(name, base, unit_names, description=''):
    """
    Turn a SINGLE-unit profile into a multi-unit one by repeating it per unit.

    A test fixture builder, not a machine: no real printer is two identical BoxTurtles. It
    exists because the honest way to get a multi-unit config is to render one, and the
    alternative that tests reached for instead - deriving a single-unit profile with
    extra_params={'UNIT_NAME': 'unit1', ...} - is quietly WRONG. That injects the names as
    jinja params after the Kconfig was already parsed under unit0's env, so the pins still say
    'unit0:' inside sections named 'unit1': a config wired to the wrong board.

    Deliberately NOT added to PROFILES. Use it when a test needs a multi-unit shape rather than
    a particular machine; use ERCF_VVD when it needs a real one.
    """
    return Profile(
        name,
        units=[UnitProfile(unit, syms=base.syms, index=index)
               for index, unit in enumerate(unit_names)],
        description=description or ('%s repeated across %s'
                                    % (base.name, ', '.join(unit_names))))


# BoxTurtle is the first-milestone profile on purpose: a Type-B machine with selector_type
# VirtualSelector, so there is no carriage to model at all. It exercises 4 gates, multigear
# steppers, an espooler, LEDs, a buffer and entry/exit/shared-exit sensors.
#
# It is NOT true (as this comment used to say) that bootup depends on a VirtualSelector-only
# skip of home_unit. Nothing autohomes at bootup on any of these machines:
# config/base/mmu_parameters.cfg renders startup_home_selector: 0 for EVERY physical selector,
# and mmu_controller.py:386-388 additionally logs-and-continues for an uncalibrated one. Physical
# selectors now home and move filament too - see test_mmu_selector.py.
BOXTURTLE = Profile(
    'boxturtle',
    syms={'MMU_TYPE_BOX_TURTLE_1_0': True},
    description='BoxTurtle 1.0 - Type B, VirtualSelector, 4 gates, multigear')

# BoxTurtle + one COMMON NFC reader serving all gates and the bypass (RC522 over SPI).
#
# MMU_HAS_COMMON_NFC_READER is what gates both the [mmu_nfc_reader NAME] section AND
# the `nfc_reader:` key on [mmu_unit]. Both it and MMU_HAS_PER_GATE_NFC_READERS default
# to n, so MMU_HAS_NFC_READER alone renders NO reader - a reader is opt-in.
#
# History worth keeping: this used to be gated on MMU_HAS_SHARED_NFC_READER, which meant
# "shared across MMU UNITS" and carried `depends on MULTI_UNIT`, making it unreachable on
# a one-unit machine. The section rendered but `nfc_reader:` did not, so the reader was
# orphaned and NFC silently did nothing. The harness caught that; it is now fixed.
NFC_SINGLE = BOXTURTLE.derive(
    'nfc_single',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True},
    description='BoxTurtle + one common NFC reader (RC522/SPI)')

# One reader per gate. This is the profile that exercises the per-gate read/LED path
# and MmuNfcEndstop, and the one that surfaces the mmu_nfc_reader.py:132 crash.
NFC_PER_GATE = BOXTURTLE.derive(
    'nfc_per_gate',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True},
    description='BoxTurtle + per-gate NFC readers')

# Per-gate readers with NFC neighbor-field CHECKING switched on (no eviction motion): a tag
# positively registered to a neighboring gate is refused rather than attributed, but nothing
# is jogged. This is the "fast-fail" half of the feature (MmuNfcFieldArbiter), reachable with
# no window/direction configuration at all.
NFC_NEIGHBOR_CHECK = NFC_PER_GATE.derive(
    'nfc_neighbor_check',
    syms={'PARAM_NFC_NEIGHBOR_CHECK': True},
    description='BoxTurtle + per-gate NFC readers, neighbor field check (no eviction)')

# Per-gate readers with neighbor EVICTION switched on too. The jog is BACKWARD (-40): a
# backward jog is unconditionally legal regardless of gate_homing_endstop (only a FORWARD
# jog is restricted to the per-gate mmu_exit sensor - see _validate_nfc_neighbor_evict_distance),
# so this value works whatever endstop a derived profile happens to use. -40 also has to fit
# inside the negative half of nfc_gate_jog_scan_window (default -50, 50), which it does.
NFC_NEIGHBOR_EVICT = NFC_PER_GATE.derive(
    'nfc_neighbor_evict',
    syms={'PARAM_NFC_NEIGHBOR_EVICT_DISTANCE': '-40'},
    description='BoxTurtle + per-gate NFC readers, neighbor eviction enabled (backward jog)')

# Per-gate readers with the self-jog ratification escalation switched on for MMU_NFC_SCAN,
# independent of neighbor eviction above (nfc_neighbor_evict_distance stays 0 here) - a
# still-detected tag is confirmed/discarded by jogging THIS gate's own filament further off
# park, not a neighbor's. Backward (-40): no window-fit check applies (this is a plain
# jog-and-restore off park, not a re-home), only the same shared-endstop direction rule as
# NFC_NEIGHBOR_EVICT. nfc_preload_clear_distance is left at its default (mirrors this value)
# so preload gets the same escalation for free.
NFC_GATE_CLEAR = NFC_PER_GATE.derive(
    'nfc_gate_clear',
    syms={'PARAM_NFC_GATE_CLEAR_DISTANCE': '-40'},
    description='BoxTurtle + per-gate NFC readers, self-jog clear distance enabled (backward jog)')

# PN5180 is the second SPI reader type, and the only one needing pins beyond the SPI
# bus: BUSY (how the driver paces every command) and RST (its only recovery route).
# Both are required by the driver, so the template must emit them or config load dies
# with "Option 'busy_pin' ... must be specified".
NFC_PN5180 = BOXTURTLE.derive(
    'nfc_pn5180',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
          'CHOICE_NFC_READER_TYPE_PN5180': True,
          'PARAM_NFC_READER_CS_PIN': 'unit0:PA4',
          'PARAM_NFC_READER_BUSY_PIN': 'unit0:PB0',
          'PARAM_NFC_READER_RESET_PIN': 'unit0:PB1'},
    description='BoxTurtle + one common NFC reader (PN5180/SPI)')

# Deliberately MIXED: gate 0 is PN5180, gates 1-3 stay RC522. The per-gate params are
# rendered from lists built by symbol-name suffix (installer/build.py:252-268), so a
# type chosen for one gate only is the case where an off-by-one or a missing list entry
# shows up - a uniform-PN5180 profile would not catch it.
NFC_PN5180_PER_GATE = BOXTURTLE.derive(
    'nfc_pn5180_per_gate',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
          'CHOICE_NFC_READER_TYPE_PN5180_0': True,
          'PARAM_NFC_READER_CS_PIN_0': 'unit0:PA4',
          'PARAM_NFC_READER_BUSY_PIN_0': 'unit0:PB0',
          'PARAM_NFC_READER_RESET_PIN_0': 'unit0:PB1'},
    description='BoxTurtle + per-gate readers, gate 0 PN5180 and the rest RC522')

# PN532 is the first I2C reader type to get a profile at all - before this the whole
# MCU_I2C_from_config path was unrendered and untested. Common reader, hardware bus.
NFC_PN532 = BOXTURTLE.derive(
    'nfc_pn532',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
          'CHOICE_NFC_READER_TYPE_PN532': True,
          'PARAM_NFC_READER_I2C_BUS': 'i2c1'},
    description='BoxTurtle + one common NFC reader (PN532/hardware i2c)')

# THE POINT OF SOFTWARE I2C. Every PN532 is hardwired to address 0x24 (36), so they
# cannot share a bus. Two gates, two DISTINCT pin pairs, both at 36 - which is only a
# valid config because each pin pair is its own bit-banged bus.
#
# Two gates rather than one on purpose: installer/build.py:252-259 un-groups an indexed
# symbol back to a scalar when only ONE exists under a prefix, so a single-gate profile
# would not render through the (PARAM_..._|d)[i] list form the template uses.
NFC_PN532_SW_I2C = BOXTURTLE.derive(
    'nfc_pn532_sw_i2c',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
          'CHOICE_NFC_READER_TYPE_PN532_0': True,
          'CHOICE_NFC_READER_I2C_SOFTWARE_0': True,
          'PARAM_NFC_READER_SCL_PIN_0': 'unit0:PB8',
          'PARAM_NFC_READER_SDA_PIN_0': 'unit0:PB9',
          'CHOICE_NFC_READER_TYPE_PN532_1': True,
          'CHOICE_NFC_READER_I2C_SOFTWARE_1': True,
          'PARAM_NFC_READER_SCL_PIN_1': 'unit0:PC4',
          'PARAM_NFC_READER_SDA_PIN_1': 'unit0:PC5'},
    description='Two per-gate PN532 readers, each on its own software i2c bus')

# PN532 over HSU/UART - the only reader that is NOT MCU-mediated. klippy opens a host
# serial port itself, so the rendered section has no pins at all: just serial + baud.
#
# The template used to dispatch SPI-vs-I2C on reader_type ("is it rc522/pn5180?"), which
# stopped working the moment pn532 gained a second transport - a UART reader would have
# rendered a full I2C block. Dispatch is on PARAM_NFC_READER_INTERFACE now, and this
# profile is what pins that: it is a pn532 that must NOT emit i2c_address.
NFC_PN532_UART = BOXTURTLE.derive(
    'nfc_pn532_uart',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
          'CHOICE_NFC_READER_TYPE_PN532_UART': True,
          'PARAM_NFC_READER_SERIAL':
              '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'},
    description='BoxTurtle + one common NFC reader (PN532/HSU UART)')

# MIXED per-gate, for the same reason NFC_PN5180_PER_GATE is mixed: gate 0 on UART and
# the rest on RC522 is where a missing per-gate list entry shows up. A uniform-UART
# profile would not catch an interface list that silently fell back to a scalar.
#
# Two UART gates would need two USB adapters in reality; one is enough to prove the
# rendering, and the port-uniqueness check has its own unit test.
NFC_PN532_UART_PER_GATE = BOXTURTLE.derive(
    'nfc_pn532_uart_per_gate',
    syms={'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
          'CHOICE_NFC_READER_TYPE_PN532_UART_0': True,
          'PARAM_NFC_READER_SERIAL_0':
              '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'},
    description='BoxTurtle + per-gate readers, gate 0 PN532/UART and the rest RC522')

# The round-trip profile: per-gate NFC + Spoolman in a WRITABLE mode + auto-create
# + deep read. All four are needed or the interesting paths gate themselves off:
#   spoolman_support defaults to 'off' (mmu_machine_parameters.py), and MMU_GATE_MAP
#     refuses NEXT_SPOOLID in 'pull' mode, so 'push' is the mode to test
#   spoolman_nfc_auto_create gates the create-from-tag path
#   nfc_deep_read (per-unit) gates whether tag metadata is applied at all, and
#     nfc_auto_create_enabled() is deep_read AND auto_create AND writable
NFC_SPOOLMAN = NFC_PER_GATE.derive(
    'nfc_spoolman',
    syms={
        'CHOICE_SPOOLMAN_SUPPORT_PUSH': True,
        'PARAM_SPOOLMAN_NFC_AUTO_CREATE': True,
        'PARAM_NFC_DEEP_READ': True,
    },
    description='Per-gate NFC + Spoolman push + auto-create + deep read')

# Same but with a unit-level (non-per-gate) reader, for the shared-reader pending
# spool_id flow. NOTE: the reader is currently orphaned by a template bug (see
# test_mmu_nfc.TestSingleReaderWiring), so tests needing a live shared reader inject
# through _MMU_TEST NFC_READ=1 instead, which bypasses the reader layer by design.
NFC_SPOOLMAN_SHARED = NFC_SINGLE.derive(
    'nfc_spoolman_shared',
    syms={
        'CHOICE_SPOOLMAN_SUPPORT_PUSH': True,
        'PARAM_SPOOLMAN_NFC_AUTO_CREATE': True,
        'PARAM_NFC_DEEP_READ': True,
    },
    description='Unit-level NFC + Spoolman push + auto-create + deep read')

# Tradrack: a second real machine, and importantly a PHYSICAL selector
# (LinearServoSelector) rather than BoxTurtle's VirtualSelector - so the tests are not all
# shaped around one selector type.
TRADRACK = Profile(
    'tradrack',
    syms={'MMU_TYPE_TRADRACK_1_0': True},
    description='Tradrack 1.0 - physical LinearServoSelector')

# 3D Chameleon: the ONLY profile with a RotarySelector, and the only machine where selecting a
# gate is not a bijection with a carriage position. There is one gear motor for all four gates,
# reversed on half of them (selector_gate_directions), and no servo - so "release the filament"
# is expressed as "drive the carriage to the offset of the OPPOSING gate"
# (selector_release_gates, [2, 3, 0, 1]). Nothing else in the harness exercises either idea, and
# the AttributeError at mmu_rotary_selector.py:229 sat in the release path until this profile
# reached it. See TestRotarySelector in test_mmu_selector.py.
#
# TWO SYMBOLS SET BEYOND THE MACHINE TYPE, both because the vendor Kconfig leaves them open and
# a real user would have to answer them at menuconfig:
#
#   BOARD_TYPE_MMB_2_0        Kconfig.3d_chameleon sets no board default (unlike box_turtle,
#       tradrack and emu, which each default one), so the choice falls to BOARD_TYPE_OTHER and
#       every pin renders empty - config load dies on "Invalid pin description ''". MMB 2.0 is
#       tradrack's own default and supplies the gear stepper, a selector stepper WITH an
#       endstop, and PIN_SHARED_EXIT_SENSOR. Note that boards/Kconfig.chameleon_x5_1, despite
#       the name, is a QuattroBox per-gate board with four gear steppers and no selector pins.
#   MMU_HAS_SENSOR_SHARED_EXIT  The type ships no sensor and no encoder, which leaves the gate
#       homing choice on CHOICE_GATE_HOMING_ENDSTOP_NONE and renders gate_homing_endstop as ''
#       - a state Kconfig.endstops:129 itself calls "actually a config error", and which fails
#       as "Choice '' for option 'gate_homing_endstop' is not a valid choice". A single sensor
#       at the combiner exit is the shape of the machine (four gates, one output path) and the
#       cheapest honest answer; it is also what tradrack selects.
#
# filament_always_gripped renders 0 (no vendor override, and DEF_PROFILE's default is False in
# mmu_unit.py:97), which is what puts the lazy-grip release path in play at all. That is not
# unique - tradrack and both ercf_vvd units render 0 too - but on those it means a servo lift.
CHAMELEON = Profile(
    'chameleon',
    syms={'MMU_TYPE_3D_CHAMELEON_1_0': True,
          'BOARD_TYPE_MMB_2_0': True,
          'MMU_HAS_SENSOR_SHARED_EXIT': True},
    description='3D Chameleon 1.0 - 4 gates, the only RotarySelector')

# PicoMMU and MMX are the two shipped ServoSelector machines. Like Chameleon, neither
# chooses a controller board or a gate-homing sensor on its own, so the harness supplies
# the same complete, ordinary setup a user must choose in menuconfig.
PICO_MMU = Profile(
    'pico_mmu',
    syms={'MMU_TYPE_PICO_MMU_1_0': True,
          'BOARD_TYPE_MMB_2_0': True,
          'MMU_HAS_SENSOR_SHARED_EXIT': True},
    description='PicoMMU 1.0 - 4 gates, ServoSelector requiring calibration')

MMX = Profile(
    'mmx',
    syms={'MMU_TYPE_MMX_1_0': True,
          'BOARD_TYPE_MMB_2_0': True,
          'MMU_HAS_SENSOR_SHARED_EXIT': True},
    description='MMX 1.0 - 4 gates, ServoSelector with vendor gate angles')

# EMU: 5 gates, and the only shipped profile that brings a PROPORTIONAL (analog) buffer
# sensor with it. That makes it the profile that exercises MmuAdcHelper's ADC compat shim
# for real, and the virtual compression/tension sensors derived from an analog reading
# rather than from switches.
#
# NOTE this is 5 gates on ONE unit. Genuine multi-unit needs F_MULTI_UNIT plus per-unit
# Kconfig loading (installer/build.py:481-491), which cfg.py deliberately bypasses - so
# multi-unit remains uncovered.
EMU = Profile(
    'emu',
    syms={'MMU_TYPE_EMU_1_0': True},
    description='EMU 1.0 - 5 gates, proportional (analog) buffer sensor')

# BoxTurtle plus an encoder, homing to it instead of to the gate switch. None of the
# three shipped machine profiles ships an encoder, and _home_to_gate's encoder branch
# (extras/mmu/mmu_filament_movement.py:206-231) is a completely different algorithm from
# the endstop branch: it does not home at all, it makes a fixed-length move and asks
# whether the filament MOVED. Nothing else covers it.
#
# Unlike the proportional-buffer attempt described below, adding an encoder is safe:
# [mmu_encoder] has a real default for every dependent parameter (resolution 0.979,
# desired_headroom, the sample counts), so the section renders complete. That is the
# test for whether hand-enabling a feature is legitimate - render it and read the
# section, do not assume.
ENCODER = BOXTURTLE.derive(
    'encoder',
    syms={
        'MMU_HAS_ENCODER': True,
        'PIN_ENCODER': 'unit0:PA6',
        'CHOICE_GATE_HOMING_ENDSTOP_ENCODER': True,
    },
    description='BoxTurtle + encoder, gate_homing_endstop=encoder')

# NOTE on ADC coverage: `emu` brings a proportional sensor with its machine type and is the
# shipped profile used to exercise that path. MmuAdcHelper's compat shim is covered directly
# by test_mmu_adc_compat.py.
# The only MULTI-UNIT profile, and a transcription of a REAL machine rather than a
# combination assembled to hit features. Two genuinely different units on one printer:
#
#   unit0  ERCF 1.1sb on ERB v1   9 gates  LinearServoSelector  encoder gate homing
#   unit1  ViViD 1.0 on ViViD 1.0 4 gates  IndexedSelector      mmu_exit gate homing
#
# 13 gates in total, which is the point of PARAM_TOTAL_NUM_GATES being a cross-unit sum.
#
# What ONLY this profile covers:
#   - two units at all: per-unit Kconfig parses, contiguous gate numbering (unit1 owns
#     gates 9-12), and sensors qualified per unit
#   - IndexedSelector, a third selector class - and one that self-calibrates and
#     self-homes at handle_ready (mmu_indexed_selector.py:137-140)
#   - a common reader coexisting with a per-gate list, on DIFFERENT units. unit0 has a
#     shared PN532 (nfc_reader='unit0_nfc') through the generic wiring prompts; unit1's
#     CUSTOM_NFC_READER_SETUP hides that menu entirely and hand-writes two custom readers
#     instead (boards/custom/Kconfig.vvd), one per adjacent gate pair, so nfc_readers
#     renders DENSE - 'unit1_nfc01, unit1_nfc01, unit1_nfc23, unit1_nfc23' - with no
#     common reader of its own (nfc_reader is blank).
#   - a machine whose filament_heater must resolve, i.e. the heater_generic fake
#   - LEDs on a chain declared OUTSIDE Happy Hare's own config (unit0 points at
#     'neopixel:cabinet_leds' from the user's printer.cfg - see bootstrap.PRINTER_STUB)
#
# TRANSCRIPTION RULES, learned the hard way - see the omit list below. Only symbols the
# user actually CHOSE are set here; everything else is left to Kconfig's select/imply/
# default chains, which is the whole reason profiles are symbol sets and not saved .cfg
# files. Notably absent, and deliberately:
#
#   MULTI_UNIT / MULTI_UNIT_ENTRY_POINT / MMU_UNITS
#       No prompt (Kconfig:146-162) - env-driven. Supplying `units` below IS the
#       declaration; cfg.py sets the env and MMU_UNITS derives from the joined names.
#   PREFERS_CHOICE_GATE_HOMING_ENDSTOP_ENCODER
#       No prompt, and unnecessary: Kconfig.ercf:9 selects MMU_HAS_ENCODER at family
#       level and :26 implies the preference, so gate_homing_endstop lands on 'encoder'
#       with nothing set. Verified by rendering.
#   CUSTOM_LED_SETUP / CUSTOM_ENVIRONMENT_SENSOR_SETUP / CUSTOM_HEATER_SETUP
#       No prompt; derived from MMU_TYPE_VVD_1_0. They suppress the generic template
#       blocks so the board's own PARAM_MISC_HARDWARE text wins.
#   CHOICE_MMU_SERIAL_DEVICE_USB_... / CHOICE_BUFFER_SERIAL_DEVICE_...
#       Kconfig:112-116 builds these symbol NAMES by shelling out to
#       `ls /dev/serial/by-id/*`, so on a host with no MMU plugged in they do not exist
#       and setting one raises KeyError. Harmless to omit: the choice falls back to
#       ..._OTHER and the harness fakes every [mcu] while ignoring the transport
#       (klippy_root/mcu.py:44-48).
ERCF_VVD = Profile(
    'ercf_vvd',
    syms={
        # Printer-level, shared by both units. MMU_HAS_SENSOR_TOOLHEAD/_EXTRUDER are read
        # back off this parse and handed down to each unit as env, mirroring
        # install.sh:424-426.
        'MMU_HAS_SENSOR_TOOLHEAD': True,
        'MMU_HAS_SENSOR_EXTRUDER': True,
        'PIN_TOOLHEAD_SENSOR': 'PG13',
        'PIN_EXTRUDER_SENSOR': 'PG14',
        'TOOLHEAD_TYPE_STEALTHBURNER_CLOCKWORK2_REVO_VORON': True,
        'PARAM_ENDLESS_SPOOL_ENABLED': True,
        # Spoolman READONLY, with auto-create on. Readonly rather than off so a UID->spool
        # lookup is actually dispatched on an NFC read (mmu_controller.py:3284 gates that on
        # spoolman_support != off) - which is what the console is for. Readonly rather than
        # push/pull because it is the mode that still lets nfc_auto_create_enabled() (deep_read
        # AND auto_create AND WRITABLE) come out False, so the guard is exercised rather than
        # the happy path. NFC_SPOOLMAN covers push.
        'CHOICE_SPOOLMAN_SUPPORT_RO': True,
        'PARAM_SPOOLMAN_NFC_AUTO_CREATE': True,
        'CHOICE_LOG_FILE_LEVEL_STEPPER': True,
        # Both default y (macro_vars/Kconfig.software:30,41); turned off on this machine, so
        # mmu_macro_vars.cfg gets check_gates/load_initial_tool = False at print start.
        'BOOL_SOFTWARE_CHECK_GATES': False,
        'BOOL_SOFTWARE_LOAD_INITIAL_TOOL': False,
    },
    units=[
        # ERCF 1.1 with the Springy + Binky mods. Those two are NOT cosmetic: together they
        # set PARAM_VERSION to '1.1sb' and switch the encoder from TCRT5000 to Binky-8
        # (Kconfig.ercf:157-162, :198-199), taking encoder resolution 0.7059 -> 0.979.
        UnitProfile('unit0', index=0, syms={
            'MMU_FAMILY_ERCF': True,     # MMU_TYPE_ERCF_1_1 is in a choice nested under it
            'MMU_TYPE_ERCF_1_1': True,
            'BOARD_TYPE_ERB_1': True,
            'MOD_BINKY': True,
            'MOD_SPRINGY': True,
            # Set EXPLICITLY, and not redundantly: this machine has a Binky-12 wheel
            # (resolution 0.979), but ERCF 1.1 + MOD_BINKY now defaults the choice to
            # Binky-8 (Kconfig.ercf:198) for a resolution of 1.469 - a 50% difference. The
            # generic default at Kconfig.encoder:67 is still Binky-12, which is what this
            # machine's stored config recorded. Pinning it keeps the profile faithful to the
            # hardware rather than to whichever default currently wins.
            'CHOICE_ENCODER_TYPE_BINKY_12': True,
            'PARAM_HAS_BYPASS': True,
            'SERVO_TYPE_SAVOX_SH0255MG': True,
            'PARAM_SERVO_MAX_ANGLE': 180,
            # ALL FOUR segments, so the console exercises every LED effect path.
            #
            # exit stays on 'cabinet_leds', an external chain Happy Hare does not declare
            # (see PRINTER_STUB) - that combination is the point of it. The other three go
            # on '_unit0_leds', the chain the template already emits on PIN_NEOPIXEL
            # (unit0:gpio21, boards/Kconfig.erb_1:70) because ERCF does not set
            # CUSTOM_LED_SETUP. Nothing referenced it before, so it was dead config on a
            # perfectly good fake pin. PARAM_CHAIN_COUNT defaults to PARAM_NUM_GATES = 9
            # (Kconfig.leds:103-107); 16 covers entry 9 + status 4 + logo 3.
            #
            # 9 entry and 9 exit LEDs over 9 gates satisfy mmu_leds.py:101-102's
            # num_leds % num_gates == 0; status and logo are unconstrained.
            'MMU_HAS_LEDS': True,
            'PARAM_CHAIN_COUNT': 16,
            'PARAM_EXIT_LEDS': 'neopixel:cabinet_leds (1-9)',
            'PARAM_ENTRY_LEDS': 'neopixel:_unit0_leds (1-9)',
            'PARAM_STATUS_LEDS': 'neopixel:_unit0_leds (10-13)',
            'PARAM_LOGO_LEDS': 'neopixel:_unit0_leds (14-16)',
            'CHOICE_EXTRUDER_HOMING_ENDSTOP_ENCODER': True,
            # A shared PN532 over host serial - the only NFC transport that is not
            # MCU-mediated. Lives on unit0 (generic wiring prompts) rather than unit1: VVD's
            # CUSTOM_NFC_READER_SETUP hides that whole menu and hand-writes its own per-gate
            # readers instead (boards/custom/Kconfig.vvd), so it has no common reader at all.
            'MMU_HAS_NFC_READER': True,
            'MMU_HAS_COMMON_NFC_READER': True,
            'CHOICE_NFC_READER_TYPE_PN532_UART': True,
            'PARAM_NFC_READER_SERIAL': '/dev/serial/shared_nfc',
        }),
        # ViViD 1.0. Its buffer lives on a SECOND mcu (OPTION_VVD_BUFFER selects
        # MMU_HAS_BUFFER_MCU), so this unit alone renders two [mcu] sections.
        UnitProfile('unit1', index=1, syms={
            'MMU_TYPE_VVD_1_0': True,
            'BOARD_TYPE_VVD_1_0': True,
            'OPTION_VVD_BUFFER': True,
            # Explicit, because the derived default from UNIT_INDEX would be 'VVD-1'
            'PARAM_DISPLAY_NAME': 'VVD-11',
            'MMU_HAS_EJECT_BUTTONS': True,
            'PIN_EJECT_BUTTON_0': 'unit1:pin0',
            'PIN_EJECT_BUTTON_1': 'unit1:pin1',
            'PIN_EJECT_BUTTON_2': 'unit1:pin2',
            'PIN_EJECT_BUTTON_3': 'unit1:pin3',
        }),
    ],
    description='ERCF 1.1sb (9 gates) + ViViD 1.0 (4 gates) - the only multi-unit profile')

# Synthetic variant for tests that specifically need unit-scoped sync-feedback state on BOTH
# sides of a unit hand-off. Keep it out of the default console profile: the real ERCF unit0
# above has no sync-feedback buffer, and adding one makes its filament status line lie.
ERCF_VVD_BUFFERS = ERCF_VVD.derive(
    'ercf_vvd_buffers',
    units=[
        ERCF_VVD.units[0].derive(syms={
            'MMU_HAS_SYNC_FEEDBACK_BUFFER': True,
            'CHOICE_BUFFER_SPRING_STATE_TENSION': True,
            'MMU_HAS_SENSOR_BUFFER_PROPORTIONAL': True,
            'PIN_BUFFER_ANALOG': 'PF6',
        }),
        ERCF_VVD.units[1],
    ],
    description='Synthetic ercf_vvd variant with buffers on both units')

# The only machine whose two units drive DIFFERENT physical extruders. Everything else, including
# ercf_vvd itself, leaves both on the default one and so shares a single MmuExtruderWrapper - which
# hides anything that treats extruder state as machine-wide. Needs [extruder1] and its mandatory
# TMC section added to the printer stub; see EXTRA_EXTRUDER_STUB.
ERCF_VVD_DUAL_EXTRUDER = ERCF_VVD.derive(
    'ercf_vvd_dual_extruder',
    units=[
        ERCF_VVD.units[0],
        ERCF_VVD.units[1].derive(syms={'PARAM_EXTRUDER_NAME': 'extruder1'}),
    ],
    description='ercf_vvd with each unit on its own extruder')

# Appended to bootstrap.PRINTER_STUB by tests using the profile above. The TMC section is not
# optional - MmuExtruderWrapper raises without one for the extruder it is given.
EXTRA_EXTRUDER_STUB = """
[extruder1]
step_pin: mcu:PB1
dir_pin: mcu:PB2
enable_pin: !mcu:PB3
microsteps: 16
full_steps_per_rotation: 200
rotation_distance: 22.0
nozzle_diameter: 0.400
filament_diameter: 1.750
heater_pin: mcu:PB4
sensor_type: EPCOS 100K B57560G104F
sensor_pin: mcu:PB5
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
min_temp: 0
max_temp: 300

[tmc2209 extruder1]
uart_pin: mcu:PB6
run_current: 0.6
"""

# Profiles that `make console` can boot without extra fixture-only printer sections. This is
# also the startup picker's source, so its list and the accepted profile objects stay one
# thing. The buffered and dual-extruder ERCF variants below are registered for tests but are
# deliberately absent: one is synthetic and the other needs EXTRA_EXTRUDER_STUB.
CONSOLE_PROFILES = (ERCF_VVD, BOXTURTLE, TRADRACK, CHAMELEON, PICO_MMU, MMX, EMU, ENCODER,
                    NFC_SINGLE, NFC_PER_GATE, NFC_NEIGHBOR_CHECK, NFC_NEIGHBOR_EVICT,
                    NFC_GATE_CLEAR,
                    NFC_PN5180, NFC_PN5180_PER_GATE, NFC_PN532, NFC_PN532_SW_I2C,
                    NFC_PN532_UART, NFC_PN532_UART_PER_GATE,
                    NFC_SPOOLMAN, NFC_SPOOLMAN_SHARED)

PROFILES = {p.name: p for p in CONSOLE_PROFILES +
            (ERCF_VVD_BUFFERS, ERCF_VVD_DUAL_EXTRUDER)}


def get(name):
    """
    A registered profile name, or a path to an installed config directory.

    The path form lets the harness run against what './install.sh -z -t' actually
    produced (in /tmp/mmu_test/printer_data/config) rather than against templates the
    harness rendered itself - hand edits included. See cfg.InstallDirProfile.
    """
    try:
        return PROFILES[name]
    except KeyError:
        pass
    if os.sep in name or name.startswith('~') or os.path.isdir(name):
        from .cfg import InstallDirProfile
        profile = InstallDirProfile(name)
        if not os.path.isdir(profile.path):
            raise KeyError("no such install directory: %s" % profile.path)
        return profile
    raise KeyError("unknown profile %r; known: %s (or a path to an installed "
                   "config directory)" % (name, ', '.join(sorted(PROFILES))))
