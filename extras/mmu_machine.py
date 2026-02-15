# Happy Hare MMU Software
#
# Definition of basic physical characteristics of MMU (including type/style)
#   - allows for hardware configuration validation
#
# Implementation of "MMU Toolhead" to allow for:
#   - "drip" homing and movement without pauses
#   - bi-directional syncing of extruder to gear rail or gear rail to extruder
#   - extra "standby" endstops
#   - extruder endstops and extruder only homing
#   - switchable drive steppers on rails
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Based on code by Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, importlib, math, os, time, re

# Klipper imports
import stepper, chelper, toolhead
from kinematics.extruder import PrinterExtruder, DummyExtruder, ExtruderStepper
from .homing import Homing, HomingMove

from . import mmu_leds

# For toolhead synchronization
EPS           = 1e-6       # ~1 microsecond safety
SYNC_AIR_GAP  = 0.001      # Sync time air gap
MOVE_HISTORY_EXPIRE = 30.0 # From motion_queuing.py

# TMC chips to search for
TMC_CHIPS = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]

# Stepper config sections
SELECTOR_STEPPER_CONFIG = "stepper_mmu_selector" # Optional
GEAR_STEPPER_CONFIG     = "stepper_mmu_gear"

SHAREABLE_STEPPER_PARAMS = ['rotation_distance', 'gear_ratio', 'microsteps', 'full_steps_per_rotation']
OTHER_STEPPER_PARAMS     = ['step_pin', 'dir_pin', 'enable_pin', 'endstop_pin', 'rotation_distance', 'pressure_advance', 'pressure_advance_smooth_time']

SHAREABLE_TMC_PARAMS     = ['run_current', 'hold_current', 'interpolate', 'sense_resistor', 'stealthchop_threshold']

# Vendor MMU's supported
VENDOR_ERCF           = "ERCF"
VENDOR_TRADRACK       = "Tradrack"
VENDOR_PRUSA          = "Prusa"
VENDOR_ANGRY_BEAVER   = "AngryBeaver"
VENDOR_BOX_TURTLE     = "BoxTurtle"
VENDOR_NIGHT_OWL      = "NightOwl"
VENDOR_3MS            = "3MS"
VENDOR_3D_CHAMELEON   = "3DChameleon"
VENDOR_PICO_MMU       = "PicoMMU"
VENDOR_QUATTRO_BOX    = "QuattroBox"
VENDOR_MMX            = "MMX"
VENDOR_VVD            = "VVD"
VENDOR_KMS            = "KMS"
VENDOR_EMU            = "EMU"
VENDOR_OTHER          = "Other"

UNIT_ALT_DISPLAY_NAMES = {
    VENDOR_ANGRY_BEAVER: "Angry Beaver",
    VENDOR_BOX_TURTLE:   "Box Turtle",
    VENDOR_NIGHT_OWL:    "Night Owl",
    VENDOR_VVD:          "BTT VVD",
}

VENDORS = [VENDOR_ERCF, VENDOR_TRADRACK, VENDOR_PRUSA, VENDOR_ANGRY_BEAVER, VENDOR_BOX_TURTLE, VENDOR_NIGHT_OWL, VENDOR_3MS, VENDOR_3D_CHAMELEON, VENDOR_PICO_MMU, VENDOR_QUATTRO_BOX, VENDOR_MMX, VENDOR_VVD, VENDOR_KMS, VENDOR_EMU, VENDOR_OTHER]


# Define type/style of MMU and expand configuration for convenience. Validate hardware configuration
class MmuMachine:

    def __init__(self, config):
        # Essential information for validation and setup
        self.config = config
        self.printer = config.get_printer()
        self.gate_counts = list(config.getintlist('num_gates', []))
        self.num_units = len(self.gate_counts)
        if self.num_units < 1:
            raise config.error("Invalid or missing num_gates parameter in section [mmu_machine]")
        self.num_gates = sum(self.gate_counts)
        self.mmu_vendor = config.getchoice('mmu_vendor', {o: o for o in VENDORS}, VENDOR_OTHER)
        self.mmu_version_string = config.get('mmu_version', "1.0")
        version = re.sub("[^0-9.]", "", self.mmu_version_string) or "1.0"
        try:
            self.mmu_version = float(version)
        except ValueError:
            raise config.error("Invalid version parameter")

        # Hack to bring some v4 functionality into v3
        # vvv

        # Create a minimal fake MmuUnit object
        self.units = []        # Unit by index
        self.unit_by_gate = [] # Quick unit lookup by gate
        next_gate = 0
        for i in range(self.num_units):
            unit = MmuUnit("unit%d" % i, i, next_gate, self.gate_counts[i])
            self.units.append(unit)
            self.unit_by_gate[next_gate:next_gate + self.gate_counts[i]] = [unit] * self.gate_counts[i]
            next_gate += self.gate_counts[i]

        orig_section = 'mmu_leds'
        if config.has_section(orig_section):
            raise config.error("v3.4 requires update of [mmu_leds] section for multiple mmu units")

        # Load optional mmu_leds module for each mmu unit in v4 style
        for unit in self.units:
            section = 'mmu_leds %s' % unit.name
            if config.has_section(section):
                c = config.getsection(section)
                unit.leds = mmu_leds.MmuLeds(c, self, unit, unit.first_gate, unit.num_gates)
                logging.info("MMU: Created: %s" % c.get_name())
                self.printer.add_object(c.get_name(), unit.leds) # Register mmu_leds to stop it being loaded by klipper

        # ^^^
        # Hack to bring some v4 functionality into v3

        # MMU design for control purposes can be broken down into the following choices:
        #  - Selector type or no (Virtual) selector
        #  - Does each gate of the MMU have different BMG drive gears (or similar). I.e. drive rotation distance is variable
        #  - Does each gate of the MMU have different bowden path
        #  - Does design require "bowden move" (i.e. non zero length bowden)
        #  - Is filament always gripped by MMU
        #  - Does selector mechanism still allow selection of gates when filament is loaded (implied for Multigear designs)
        #  - Does design has a filament bypass
        selector_type = 'LinearServoSelector'
        variable_rotation_distances = 1
        variable_bowden_lengths = 0
        require_bowden_move = 1     # Will allow mmu_gate sensor and extruder sensor to share the same pin
        filament_always_gripped = 0 # Whether MMU design has ability to release filament (overrides gear/extruder syncing)
        can_crossload = 0           # Design allows preloading/eject in one gate while another gate is loaded (also requires post_gear sensor)
        has_bypass = 0              # Whether MMU design has bypass gate (also has to be calibrated on type-A designs with LinearServoSelector)

        if self.mmu_vendor == VENDOR_ERCF:
            selector_type = 'LinearServoSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 0
            can_crossload = 0
            has_bypass = 1

        elif self.mmu_vendor == VENDOR_TRADRACK:
            selector_type = 'LinearServoSelector'
            variable_rotation_distances = 0
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 0
            can_crossload = 0
            has_bypass = 1

        elif self.mmu_vendor == VENDOR_PRUSA:
            raise config.error("Prusa MMU is not yet supported")

        elif self.mmu_vendor == VENDOR_ANGRY_BEAVER:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 0
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_BOX_TURTLE:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_NIGHT_OWL:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_3MS:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 0
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_3D_CHAMELEON:
            selector_type = 'RotarySelector'
            variable_rotation_distances = 0
            variable_bowden_lengths = 1
            require_bowden_move = 1
            filament_always_gripped = 0
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_PICO_MMU:
            selector_type = 'ServoSelector'
            variable_rotation_distances = 0
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 0
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_QUATTRO_BOX:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_MMX:
            selector_type = 'ServoSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 0
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_VVD:
            selector_type = 'IndexedSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_KMS:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 0
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 0

        elif self.mmu_vendor == VENDOR_EMU:
            selector_type = 'VirtualSelector'
            variable_rotation_distances = 1
            variable_bowden_lengths = 1
            require_bowden_move = 1
            filament_always_gripped = 1
            can_crossload = 1
            has_bypass = 1

        # Still allow MMU design attributes to be altered or set for custom MMU
        self.selector_type = config.getchoice('selector_type', {o: o for o in ['VirtualSelector', 'LinearSelector', 'LinearServoSelector', 'LinearMultiGearSelector', 'RotarySelector', 'MacroSelector', 'ServoSelector', 'IndexedSelector']}, selector_type)
        self.variable_rotation_distances = bool(config.getint('variable_rotation_distances', variable_rotation_distances))
        self.variable_bowden_lengths = bool(config.getint('variable_bowden_lengths', variable_bowden_lengths))
        self.require_bowden_move = bool(config.getint('require_bowden_move', require_bowden_move))
        self.filament_always_gripped = bool(config.getint('filament_always_gripped', filament_always_gripped))
        self.can_crossload = bool(config.getint('can_crossload', can_crossload))
        self.has_bypass = bool(config.getint('has_bypass', has_bypass))

        # Other attributes
        self.display_name = config.get('display_name', UNIT_ALT_DISPLAY_NAMES.get(self.mmu_vendor, self.mmu_vendor))

        self.environment_sensor = config.get('environment_sensor', '')
        self.filament_heater = config.get('filament_heater', '')

        # Special handling for EMU MMU's that can have a heater and environment sensor per gate
        self.environment_sensors = list(config.getlist('environment_sensors', []))
        if len(self.environment_sensors) not in [0, self.num_gates]:
            raise config.error("'environment_sensors' must be empty or a comma separated list of 'num_gate' elements")
        self.filament_heaters = list(config.getlist('filament_heaters', []))
        if len(self.filament_heaters) not in [0, self.num_gates]:
            raise config.error("'filament_heaters' must be empty, a single value or a comma separated list of 'num_gate' elements")
        self.max_concurrent_heaters = config.getint('max_concurrent_heaters', self.num_gates)

        # Check mutually exclusive environment options
        if (self.environment_sensor or self.filament_heater) and (self.environment_sensors or self.filament_heaters):
            raise config.error("Can't configure single multiple MMU heaters/environment sensors")

        # Check all heater and environment sensor objects are valid
        for obj_name in self.filament_heaters + [self.filament_heater] + self.environment_sensors + [self.environment_sensor]:
            if obj_name:
                try:
                    # If we can't load heater/sensor then it is misconfigured
                    obj = self.printer.load_object(config, obj_name)
                except Exception as e:
                    raise config.error("Object '%s' could not be loaded as a valid heater or environment sensor in [mmu_machine]\nError: %s" % (obj_name, str(e)))

        # By default HH uses a modified homing extruder. Because this might have unknown consequences on certain
        # set-ups it can be disabled. If disabled, homing moves will still work, but the delay in mcu to mcu comms
        # can lead to several mm of error depending on speed. Also homing of just the extruder is not possible.
        self.homing_extruder = bool(config.getint('homing_extruder', 1, minval=0, maxval=1))

        # Expand config to allow lazy (incomplete) repetitious gear configuration for type-B MMU's
        self.multigear = False

        # Find the TMC controller for base stepper so we can fill in missing config for other matching steppers
        base_tmc_chip = base_tmc_section = None
        for chip in TMC_CHIPS:
            base_tmc_section = '%s %s' % (chip, GEAR_STEPPER_CONFIG)
            if config.has_section(base_tmc_section):
                base_tmc_chip = chip
                break

        last_gear = 24
        for i in range(1, last_gear): # Don't allow "_0" or it is confusing with unprefixed initial stepper
            section = "%s_%d" % (GEAR_STEPPER_CONFIG, i)
            if not config.has_section(section):
                last_gear = i
                break

            self.multigear = True

            # Share stepper config section with additional steppers
            stepper_section = "%s_%d" % (GEAR_STEPPER_CONFIG, i)
            for key in SHAREABLE_STEPPER_PARAMS:
                if not config.fileconfig.has_option(stepper_section, key) and config.fileconfig.has_option(GEAR_STEPPER_CONFIG, key):
                    base_value = config.fileconfig.get(GEAR_STEPPER_CONFIG, key)
                    if base_value:
                        logging.info("MMU: Sharing gear stepper config %s=%s with %s" % (key, base_value, stepper_section))
                        config.fileconfig.set(stepper_section, key, base_value)

            # IF TMC controller for this additional stepper matches the base we can fill in missing TMC config
            if base_tmc_chip:
                tmc_section = '%s %s_%d' % (base_tmc_chip, GEAR_STEPPER_CONFIG, i)
                if config.has_section(tmc_section):
                    for key in SHAREABLE_TMC_PARAMS:
                        if config.fileconfig.has_option(base_tmc_section, key) and not config.fileconfig.has_option(tmc_section, key):
                            base_value = config.fileconfig.get(base_tmc_section, key)
                            if base_value:
                                logging.info("MMU: Sharing gear tmc config %s=%s with %s" % (key, base_value, tmc_section))
                                config.fileconfig.set(tmc_section, key, base_value)

        # H/W validation checks
        if self.multigear and last_gear != self.num_gates:
            raise config.error("MMU is configured with %d gates but %d gear stepper configurations were found" % (self.num_gates, last_gear))

        # TODO add more h/w validation here based on num_gates & vendor, virtual selector, etc
        # TODO would allow for easier to understand error messages for conflicting or missing
        # TODO hardware definitions.

        # TODO: Temp until restructured to allow multiple MMU's of different types.
        gate_count = 0
        self.unit_status = {}
        for i, unit in enumerate(self.gate_counts):
            unit_info = {}
            unit_info['name'] = self.display_name
            unit_info['vendor'] = self.mmu_vendor
            unit_info['version'] = self.mmu_version_string
            unit_info['num_gates'] = unit
            unit_info['first_gate'] = gate_count
            unit_info['selector_type'] = self.selector_type
            unit_info['variable_rotation_distances'] = self.variable_rotation_distances
            unit_info['variable_bowden_lengths'] = self.variable_bowden_lengths
            unit_info['require_bowden_move'] = self.require_bowden_move
            unit_info['filament_always_gripped'] = self.filament_always_gripped
            unit_info['can_crossload'] = self.can_crossload
            unit_info['has_bypass'] = self.has_bypass
            unit_info['multi_gear'] = self.multigear
            if self.environment_sensor or self.filament_heater:
                # Single heater/sensor
                unit_info['environment_sensor'] = self.environment_sensor
                unit_info['filament_heater'] = self.filament_heater
            elif self.environment_sensors or self.filament_heaters:
                # Per-gate heater/sensors
                unit_info['environment_sensors'] = self.environment_sensors[gate_count:gate_count + unit]
                unit_info['filament_heaters'] = self.filament_heaters[gate_count:gate_count + unit]
            gate_count += unit
            self.unit_status["unit_%d" % i] = unit_info
            self.unit_status['num_units'] = len(self.gate_counts)

    def get_mmu_unit_by_index(self, index): # Hack to allow some v4 functionality into the v3 line
        if index >= 0 and index < self.num_units:
            return self.units[index]
        return None
        
    def get_mmu_unit_by_gate(self, gate): # Hack to allow some v4 functionality into the v3 line
        if gate >= 0 and gate < self.num_gates:
            return self.unit_by_gate[gate]
        return None

    def get_status(self, eventtime):
        return self.unit_status

# This is a Hack to allow some v4 functionality into the v3 line. Skeletal MmuUnit object
class MmuUnit:
    def __init__(self, name, unit_index, first_gate, num_gates):
        self.name = name
        self.unit_index = unit_index
        self.first_gate = first_gate
        self.num_gates = num_gates
        self.leds = None 

    def manages_gate(self, gate):
        return self.first_gate <= gate < self.first_gate + self.num_gates


# Main code to track events (and their timing) on the MMU Machine implemented as additional "toolhead"
# (code pulled from toolhead.py)
class MmuToolHead(toolhead.ToolHead, object):

    # Gear/Extruder synchronization modes (None = unsynced)
    EXTRUDER_SYNCED_TO_GEAR = 1 # Aka 'gear+extruder'
    EXTRUDER_ONLY_ON_GEAR   = 2 # Aka 'extruder' (only)
    GEAR_SYNCED_TO_EXTRUDER = 3 # Aka 'extruder+gear'
    GEAR_ONLY               = 4 # Aka 'gear' (same state as unsync() but with protective wait)

    def __init__(self, config, mmu):
        self.mmu = mmu
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        self.all_mcus = [m for n, m in self.printer.lookup_objects(module='mcu')] # Older Klipper
        self.mcu = self.all_mcus[0]                                               # Older Klipper
        #self.mcu = self.printer.lookup_object('mcu') # Klipper approx >= 0.13.0-328 (safer lookup, guarantee's primary mcu or config error)

        self._resync_lock = self.reactor.mutex()

        if hasattr(toolhead, 'BUFFER_TIME_HIGH'):
            time_high = toolhead.BUFFER_TIME_HIGH
        else:
            # Backward compatibility for older klipper, like on Sovol or Creality K1 series printers
            # On Creality K1, these attributes are expected to exist in any Toolhead
            self.buffer_time_low = config.getfloat('buffer_time_low', 1.000, above=0.)
            self.buffer_time_high = config.getfloat('buffer_time_high', 2.000, above=self.buffer_time_low)
            self.buffer_time_start = config.getfloat('buffer_time_start', 0.250, above=0.)
            self.move_flush_time = config.getfloat('move_flush_time', 0.050, above=0.)
            self.last_kin_flush_time = self.force_flush_time = self.last_kin_move_time = 0.
            time_high = self.buffer_time_high

        if hasattr(toolhead, 'LookAheadQueue'):
            try:
                self.lookahead = toolhead.LookAheadQueue(self) # Klipper < 3.13.0-46
            except:
                self.lookahead = toolhead.LookAheadQueue() # >= Klipper 3.13.0-46
            self.lookahead.set_flush_time(time_high)
        else:
            # Klipper backward compatibility
            self.move_queue = toolhead.MoveQueue(self)
            self.move_queue.set_flush_time(time_high)
        self.commanded_pos = [0., 0., 0., 0.]

        # For Creality Ender3v3 series (custom klipper)
        self.gap_auto_comp = None

        # MMU velocity and acceleration control
        self.gear_max_velocity = config.getfloat('gear_max_velocity', 300, above=0.)
        self.gear_max_accel = config.getfloat('gear_max_accel', 500, above=0.)
        self.selector_max_velocity = config.getfloat('selector_max_velocity', 250, above=0.)
        self.selector_max_accel = config.getfloat('selector_max_accel', 1500, above=0.)

        self.max_velocity = max(self.selector_max_velocity, self.gear_max_velocity)
        self.max_accel = max(self.selector_max_accel, self.gear_max_accel)

        min_cruise_ratio = 0.5
        if config.getfloat('minimum_cruise_ratio', None) is None:
            req_accel_to_decel = config.getfloat('max_accel_to_decel', None, above=0.)
            if req_accel_to_decel is not None:
                config.deprecate('max_accel_to_decel')
                min_cruise_ratio = 1. - min(1., (req_accel_to_decel / self.max_accel))
        self.min_cruise_ratio = config.getfloat('minimum_cruise_ratio', min_cruise_ratio, below=1., minval=0.)
        self.square_corner_velocity = config.getfloat('square_corner_velocity', 5., minval=0.)
        self.junction_deviation = self.max_accel_to_decel = 0.
        self.requested_accel_to_decel = self.min_cruise_ratio * self.max_accel # Backward klipper compatibility 31de734d193d
        self._calc_junction_deviation()

        # This is now a big switch that gates changes to the Toolhead in Klipper
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing', None)

        # Input stall detection
        self.check_stall_time = 0.
        self.print_stall = 0
        # Input pause tracking
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.need_check_pause = -1.
        # Print time tracking
        self.print_time = 0.
        self.special_queuing_state = "NeedPrime"
        self.priming_timer = None
        self.drip_completion = None # TODO No longer part of Klipper >v0.13.0-46

        if not self.motion_queuing:
            # Flush tracking
            self.flush_timer = self.reactor.register_timer(self._flush_handler)
            self.do_kick_flush_timer = True
            self.last_flush_time = self.last_sg_flush_time = self.min_restart_time = 0. # last_sg_flush_time deprecated
            self.need_flush_time = self.step_gen_time = self.clear_history_time = 0.
            # Kinematic step generation scan window time tracking
            self.kin_flush_delay = toolhead.SDS_CHECK_TIME # Happy Hare: Use base class
            self.kin_flush_times = []

        if self.motion_queuing:
            ffi_main, ffi_lib = chelper.get_ffi()
            self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves # Want my own binding so I know its available

            # Setup for generating moves
            try:
                self.motion_queuing.register_flush_callback(self._handle_step_flush, can_add_trapq=True) # Latest klipper >= v0.13.0-267
            except AttributeError:
                self.motion_queuing.setup_lookahead_flush_callback(self._check_flush_lookahead)

            self.trapq = self.motion_queuing.allocate_trapq()
            self.trapq_append = self.motion_queuing.lookup_trapq_append()
        else:
            # Setup iterative solver
            ffi_main, ffi_lib = chelper.get_ffi()
            self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
            self.trapq_append = ffi_lib.trapq_append
            self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
            # Motion flushing
            self.step_generators = []
            self.flush_trapqs = [self.trapq]

        # Create kinematics class
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        self.extruder = DummyExtruder(self.printer)
        self.extra_axes = [self.extruder]

        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

        # Create MMU kinematics
        try:
            self.kin = MmuKinematics(self, config)
            steppers = list(self.kin.rails[1].get_steppers())
            self.all_gear_rail_steppers = steppers.copy()
            self.selected_gear_steppers = steppers.copy()
        except config.error:
            raise
        except self.printer.lookup_object('pins').error:
            raise
        except:
            msg = "Error loading MMU kinematics"
            logging.exception("MMU: %s" % msg)
            raise config.error(msg)

        self.mmu_machine = self.printer.lookup_object("mmu_machine")
        self.mmu_extruder_stepper = None
        if self.mmu_machine.homing_extruder:
            # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
            self.mmu_extruder_stepper = MmuExtruderStepper(config.getsection('extruder'), self.kin.rails[1]) # Only first extruder is handled

            # Nullify original extruder stepper definition so Klipper doesn't try to create it again. Restore in handle_connect() so config lookups succeed
            self.old_ext_options = {}
            self.config = config
            for i in SHAREABLE_STEPPER_PARAMS + OTHER_STEPPER_PARAMS:
                if config.fileconfig.has_option('extruder', i):
                    self.old_ext_options[i] = config.fileconfig.get('extruder', i)
                    config.fileconfig.remove_option('extruder', i)

        self.printer.register_event_handler('klippy:connect', self.handle_connect)

        # Add useful debugging command
        gcode.register_command('_MMU_DUMP_TOOLHEAD', self.cmd_DUMP_RAILS, desc=self.cmd_DUMP_RAILS_help)

        # Bi-directional sync management of gear(s) and extruder(s)
        self.mmu_toolhead = self # Make it easier to read code and distinquish printer_toolhead from mmu_toolhead
        self.sync_mode = None

    def handle_connect(self):
        self.printer_toolhead = self.printer.lookup_object('toolhead')

        printer_extruder = self.printer_toolhead.get_extruder()
        if self.mmu_machine.homing_extruder:
            # Restore original extruder options in case user macros reference them
            for key, value in self.old_ext_options.items():
                self.config.fileconfig.set('extruder', key, value)

            # Now we can switch in homing MmuExtruderStepper
            printer_extruder.extruder_stepper = self.mmu_extruder_stepper
            self.mmu_extruder_stepper.stepper.set_trapq(printer_extruder.get_trapq())
        else:
            self.mmu_extruder_stepper = printer_extruder.extruder_stepper

    # Ensure the correct number of axes for convenience - MMU only has two
    # Also, handle case when gear rail is synced to extruder
    def set_position(self, newpos, homing_axes=()):
        for _ in range(4 - len(newpos)):
            newpos.append(0.)
        super(MmuToolHead, self).set_position(newpos, homing_axes)

    def get_selector_limits(self):
        return self.selector_max_velocity, self.selector_max_accel

    def get_gear_limits(self):
        return self.gear_max_velocity, self.gear_max_accel

    # Gear/Extruder synchronization and stepper swapping management...

    def select_gear_stepper(self, gate):
        if not self.mmu_machine.multigear: return
        with self._resync_lock:
            if gate == 0:
                self._reconfigure_rail_no_lock([GEAR_STEPPER_CONFIG])
            elif gate > 0:
                self._reconfigure_rail_no_lock(["%s_%d" % (GEAR_STEPPER_CONFIG, gate)])
            else:
                self._reconfigure_rail_no_lock(None)

    def _reconfigure_rail_no_lock(self, selected):
        m_th = self.mmu_toolhead
        gear_rail = m_th.get_kinematics().rails[1]
        mmu_trapq = m_th.get_trapq()

        prev_sync_mode = self.sync_mode
        if self.sync_mode not in [self.GEAR_ONLY, None]:
            self._resync_no_lock(None) # Unsync first
        else:
            ths = [self.printer_toolhead, self.mmu_toolhead]
            t_cut = self._quiesce_align_get_tcut(ths, full=False)

        # Activate only the desired gear steppers
        pos = [0., self.mmu_toolhead.get_position()[1], 0.]
        gear_rail.steppers = []

        self.selected_gear_steppers = []
        for s in self.all_gear_rail_steppers:
            if selected and s.get_name() in selected:
                self.selected_gear_steppers.append(s)
                gear_rail.steppers.append(s)
                self._register(m_th, s, trapq=mmu_trapq) # s.set_trapq(mmu_trapq)
            else:
                # Cripple unused/unwanted gear steppers
                self._unregister(m_th, s) # s.set_trapq(None)

        if selected:
            if not gear_rail.steppers:
                raise self.printer.command_error("None of these '%s' gear steppers where found!" % selected)
            gear_rail.set_position(pos)

        # Restore previous syncing mode
        # (Not needed in practice)
        # if prev_sync_mode != self.sync_mode:
        #     self._resync_no_lock(prev_sync_mode)

    # Register stepper step generator with desired toolhead and add toolhead's trapq
    def _register(self, toolhead, stepper, trapq=None):
        trapq = trapq or toolhead.get_trapq()
        stepper.set_trapq(trapq) # Restore movement
        if not self.motion_queuing:
            # klipper 0.13.0 <= 195 we also register step generators from mmu_toolhead
            if stepper.generate_steps not in self.mmu_toolhead.step_generators:
                if hasattr(toolhead, 'register_step_generator'):
                    toolhead.register_step_generator(stepper.generate_steps)
                else:
                    toolhead.step_generators.append(stepper.generate_steps)

    # Unregister stepper step generator with desired toolhead and remove from trapq
    def _unregister(self, toolhead, stepper):
        stepper.set_trapq(None) # Cripple movement
        if not self.motion_queuing:
            # klipper 0.13.0 <= 195 we also unregister step generators from mmu_toolhead
            if stepper.generate_steps in self.mmu_toolhead.step_generators:
                if hasattr(toolhead, 'unregister_step_generator'):
                    toolhead.unregister_step_generator(stepper.generate_steps)
                else:
                    # Why did Kalico remove this function?
                    toolhead.step_generators.remove(stepper.generate_steps)

    def quiesce(self, full_quiesce=True):
        with self._resync_lock:
            ths = [self.printer_toolhead, self.mmu_toolhead]
            t_cut = self._quiesce_align_get_tcut(ths, full=full_quiesce, wait=full_quiesce)

    # Drain required toolheads, align to a common future time, materialize it,
    # and return a strict fence time t_cut. 'wait' shouldn't be needed but
    # is helpful when debugging
    def _quiesce_align_get_tcut(self, ths, full=False, wait=False):
        start = time.time()
        # Drain whatever is already planned
        for th in ths:
            th.flush_step_generation()
        if full:
            for th in ths:
                th.wait_moves()
            for th in ths:
                th.flush_step_generation()

        # Align planners to a common future time
        last_times = [th.get_last_move_time() for th in ths]
        t_future = max(last_times) + SYNC_AIR_GAP
        for th, lm in zip(ths, last_times):
            dt = t_future - lm
            if dt > 0.0:
                th.dwell(dt)

        # Materialize the air gap before choosing the fence
        for th in ths:
            th.flush_step_generation()

        # Optional wait and flush to aid debugging
        if wait:
            for th in ths:
                th.wait_moves()
            for th in ths:
                th.flush_step_generation()

        self.mmu.log_stepper("_quiesce_align_get_tcut(full=%s, wait=%s) Elapsed:%.6f" % (full, wait, time.time() - start))
        return t_future + EPS

    def is_synced(self):
        return self.sync_mode is not None

    # Is extruder stepper synced to gear rail (general MMU synced movement)
    def is_extruder_synced_to_gear(self):
        return self.sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]

    # Is gear rail synced to extruder (for in print syncing)
    def is_gear_synced_to_extruder(self):
        return self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER

    def sync(self, new_sync_mode):
        with self._resync_lock:
            return self._resync_no_lock(new_sync_mode)

    def unsync(self):
        with self._resync_lock:
            return self._resync_no_lock(None)

    # This resets the state of mmu/extruder synchronization carefully avoiding stepcompress issues.
    # It is quite tricky to do efficiently without timing windows. The goal is to do the minimum
    # of waiting and only drain everything IF the extruder is being synced to gear rail or
    # gear rail synced to the extruder
    def _resync_no_lock(self, new_sync_mode):

        if new_sync_mode == self.sync_mode:
            return new_sync_mode

        prev_sync_mode = self.sync_mode
        ffi_main, ffi_lib = chelper.get_ffi()

        def _finalize_if_valid(tq, t):
            if tq is not None and tq != ffi_main.NULL:
                self.trapq_finalize_moves(tq, t, t - MOVE_HISTORY_EXPIRE)

        self.mmu.log_stepper("resync(%s --> %s)" % (self.sync_mode_to_string(self.sync_mode), self.sync_mode_to_string(new_sync_mode)))

        # ---------------- Phase A: FENCE if messing with extruder -----------

        user_control_states       = [self.GEAR_SYNCED_TO_EXTRUDER, None]
        relocated_extruder_states = [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]
        mmu_control_states        = relocated_extruder_states + [self.GEAR_ONLY]
        full_quiesce = (
            (new_sync_mode in user_control_states and self.sync_mode in mmu_control_states) or
            (new_sync_mode in mmu_control_states and self.sync_mode in user_control_states)
        )

        ths = [self.printer_toolhead, self.mmu_toolhead]
        gear_rail = self.mmu_toolhead.get_kinematics().rails[1]
        t0 = self._quiesce_align_get_tcut(ths, full=full_quiesce, wait=full_quiesce) # Build cutover fence

        # UNSYNC current mode (if any) to base state at t0
        if self.sync_mode not in [self.GEAR_ONLY, None]:

            # ---------------- Phase B: UNSYNC current mode at t0 ----------------

            self.mmu.log_stepper("unsync(%s)" % ("mode=gear_only" if new_sync_mode == self.GEAR_ONLY  else ""))

            # Figure out who is CURRENTLY driving (old owner) and who will receive (new owner)
            if self.sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]:
                driving_toolhead   = self.mmu_toolhead        # OLD owner (mmu/gear)
                following_toolhead = self.printer_toolhead    # NEW owner (printer/extruder)
                following_steppers = [following_toolhead.get_extruder().extruder_stepper.stepper]
                old_trapq = driving_toolhead.get_trapq()      # trapq we’re finalizing
                new_trapq = self._prev_trapq                  # trapq saved during sync()
                pos = [following_toolhead.get_position()[3], 0., 0.]

                # Always remove the extruder stepper we appended to the gear rail (will always be at end of list)
                gear_rail.steppers = gear_rail.steppers[:-len(following_steppers)]

            elif self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
                driving_toolhead   = self.printer_toolhead    # OLD owner (printer/extruder)
                following_toolhead = self.mmu_toolhead        # NEW owner (mmu/gear)
                # All gear-rail steppers were following the extruder
                following_steppers = following_toolhead.get_kinematics().rails[1].get_steppers()
                old_trapq = driving_toolhead.get_extruder().get_trapq() # trapq we’re finalizing
                new_trapq = self._prev_trapq                  # trapq saved during sync()
                pos = [0., following_toolhead.get_position()[1], 0.]

            # Hard close the old trapq up to the fence (don’t wipe)
            # Anything <= t0 moves to history so it can’t be emitted later.
            _finalize_if_valid(old_trapq, t0)

            # Rebind steppers back to the NEW owner’s trapq and restore kinematics
            for i, s in enumerate(following_steppers):
                s.set_stepper_kinematics(self._prev_sk[i])
                s.set_rotation_distance(self._prev_rd[i])
                # s.set_trapq(new_trapq)                        # Attach to NEW owner on the pre-saved trapq
                self._unregister(driving_toolhead, s)                  # Detach from OLD owner…
                self._register(following_toolhead, s, trapq=new_trapq) # …then attach to NEW owner on the pre-saved trapq
                # Coordinate-only seed (timing will be enforced by advancing the receiver)
                s.set_position(pos)

            # Restore previously disabled gear steppers (after the extruder is back on printer toolhead)
            if self.sync_mode == self.EXTRUDER_ONLY_ON_GEAR:
                for s in self.selected_gear_steppers:
                    self._register(self.mmu_toolhead, s) # s.set_trapq(self.mmu_toolhead.get_trapq())
                    s.set_position([0., self.mmu_toolhead.get_position()[1], 0.])

            ## Required for klipper >= 0.13.0-330
            #if self.motion_queuing and hasattr(self.motion_queuing, 'check_step_generation_scan_windows'):
            #    self.motion_queuing.check_step_generation_scan_windows()

            # Debugging
            #logging.info("MMU: ////////// CUTOVER fence t_cut=%.6f, old_trapq=%s, new_trapq=%s, from.last=%.6f, to.last=%.6f",
            #             t0, self._match_trapq(old_trapq), self._match_trapq(new_trapq),
            #             driving_toolhead.get_last_move_time(),
            #             following_toolhead.get_last_move_time())

            # FORCE the RECEIVER (following_toolhead) planner to >= t0 and materialize it
            dt = max(EPS, t0 - following_toolhead.get_last_move_time())
            if dt: following_toolhead.dwell(dt)
            following_toolhead.flush_step_generation()

            if self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
                self.printer.send_event("mmu:unsynced")

            # Now “unsynced” at t0

        # Required for klipper >= 0.13.0-330
        if self.motion_queuing and hasattr(self.motion_queuing, 'check_step_generation_scan_windows'):
            self.motion_queuing.check_step_generation_scan_windows()

        self.sync_mode = self.GEAR_ONLY if new_sync_mode == self.GEAR_ONLY else None
        if new_sync_mode in [self.GEAR_ONLY, None]:
            return prev_sync_mode

        # ---------------- Phase C: SYNC into new mode at t1 -----------------

        self.mmu.log_stepper("sync(mode=%d %s)" % (new_sync_mode, ("gear+extruder" if new_sync_mode == self.EXTRUDER_SYNCED_TO_GEAR  else "extruder" if new_sync_mode == self.EXTRUDER_ONLY_ON_GEAR else "extruder+gear")))

        t1 = t0 + EPS  # Later fence for the second cut over (t1 = t0 + EPS)

        # Figure out driver and follower based on sync mode
        if new_sync_mode in [self.EXTRUDER_SYNCED_TO_GEAR, self.EXTRUDER_ONLY_ON_GEAR]:
            driving_toolhead   = self.mmu_toolhead       # NEW owner (mmu/gear)
            following_toolhead = self.printer_toolhead   # OLD owner (printer/extruder)
            following_steppers = [following_toolhead.get_extruder().extruder_stepper.stepper]
            self._prev_trapq = following_steppers[0].get_trapq() # Save the *old* trapq **before** any rebind/unregister
            driving_trapq = driving_toolhead.get_trapq()
            s_alloc = ffi_lib.cartesian_stepper_alloc(b"y")
            pos = [0., driving_toolhead.get_position()[1], 0.]

            # Cripple gear steppers and inject the extruder steppers into the gear rail
            if new_sync_mode == self.EXTRUDER_ONLY_ON_GEAR:
                for s in self.all_gear_rail_steppers:
                    self._unregister(self.mmu_toolhead, s) # s.set_trapq(None)
            # Inject the extruder steppers to the end of the gear rail
            gear_rail.steppers.extend(following_steppers)

        elif new_sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            driving_toolhead = self.printer_toolhead     # NEW owner (printer/extruder)
            following_toolhead = self.mmu_toolhead       # OLD owner (mmu/gear)
            following_steppers = list(following_toolhead.get_kinematics().rails[1].get_steppers())
            self._prev_trapq = following_toolhead.get_trapq()
            driving_trapq = driving_toolhead.get_extruder().get_trapq()
            s_alloc = ffi_lib.extruder_stepper_alloc()
            pos = [driving_toolhead.get_position()[3], 0., 0.]

        else:
            raise ValueError("Invalid sync_mode: %d" % new_sync_mode)

        # Hard close the old trapq up to the fence
        _finalize_if_valid(self._prev_trapq, t1)

        # Switch ownership: unregister from the old TH, register on the new TH
        self._prev_sk, self._prev_rd = [], []
        for s in following_steppers:
            s_kinematics = ffi_main.gc(s_alloc, ffi_lib.free)
            self._prev_sk.append(s.set_stepper_kinematics(s_kinematics))
            self._prev_rd.append(s.get_rotation_distance()[0])

            # Remove from following toolhead, then attach to driving toolhead’s trapq
            # s.set_trapq(driving_trapq)
            self._unregister(following_toolhead, s)
            self._register(driving_toolhead, s, trapq=driving_trapq)

            # Fix position
            s.set_position(pos)

        # Debugging
        #logging.info("MMU: ////////// CUTOVER fence t_cut=%.6f, old_trapq=%s, new_trapq=%s, from.last=%.6f, to.last=%.6f",
        #             t1, self._match_trapq(self._prev_trapq), self._match_trapq(driving_trapq),
        #             following_toolhead.get_last_move_time(),
        #             driving_toolhead.get_last_move_time())

        # FORCE the NEW/RECEIVER (driving_toolhead) planner to >= t1 and materialize it
        dt = max(EPS, t1 - driving_toolhead.get_last_move_time())
        driving_toolhead.dwell(dt)
        driving_toolhead.flush_step_generation()

        # Now “synced” at t1

        self.sync_mode = new_sync_mode
        if self.sync_mode == self.GEAR_SYNCED_TO_EXTRUDER:
            self.printer.send_event("mmu:synced")

        return prev_sync_mode

    def is_selector_homed(self):
        return self.kin.get_status(self.reactor.monotonic())["selector_homed"]

    def get_status(self, eventtime):
        res = super(MmuToolHead, self).get_status(eventtime)
        res.update(dict(self.get_kinematics().get_status(eventtime)))
        res.extend({
            'filament_pos': self.mmu_toolhead.get_position()[1],
            'sync_mode': self.sync_mode
        })
        return res

    cmd_DUMP_RAILS_help = "For debugging: dump current configuration of MMU Toolhead rails"
    def cmd_DUMP_RAILS(self, gcmd):
        show_endstops = gcmd.get_int('ENDSTOPS', 1, minval=0, maxval=1)
        msg = self.dump_rails(show_endstops)
        gcmd.respond_raw(msg)

    def _match_trapq(self, trapq):
        p_th_trapq = self.printer_toolhead.get_trapq()
        m_th_trapq = self.mmu_toolhead.get_trapq()
        e_trapq = self.printer_toolhead.get_extruder().get_trapq()
        ffi_main, ffi_lib = chelper.get_ffi()
        if trapq is p_th_trapq:
            return "Printer Toolhead"
        elif trapq is m_th_trapq:
            return "MMU Toolhead"
        elif trapq is e_trapq:
            return "Extruder"
        elif trapq is None:
            return "None"
        elif trapq is ffi_main.NULL:
            return "NULL"
        return hex(id(trapq))

    def sync_mode_to_string(self, mode):
        if mode == self.EXTRUDER_SYNCED_TO_GEAR:
            return "gear+extruder"
        elif mode == self.EXTRUDER_ONLY_ON_GEAR:
            return "extruder"
        elif mode == self.GEAR_SYNCED_TO_EXTRUDER:
            return "extruder+gear"
        elif mode == self.GEAR_ONLY:
            return "unsynced/gear"
        return "unsynced"

    def dump_rails(self, show_endstops=True):
        msg = "MMU TOOLHEAD: %s Last move time: %s\n" % (self.get_position(), self.mmu_toolhead.get_last_move_time())
        extruder_name = self.printer_toolhead.get_extruder().get_name()
        for axis, rail in enumerate(self.get_kinematics().rails):
            msg += "\n" if axis > 0 else ""
            header = "RAIL: %s (Steppers: %d, Default endstops: %d, Extra endstops: %d) %s" % (rail.rail_name, len(rail.steppers), len(rail.endstops), len(rail.extra_endstops), '-' * 100)
            msg += header[:100] + "\n"
            gsd = None
            if axis == 1:
                rail_steppers = list(self.all_gear_rail_steppers)
                for s in rail.get_steppers():
                    if s not in rail_steppers:
                        rail_steppers.append(s)
            else:
                rail_steppers = rail.get_steppers()
            for idx, s in enumerate(rail_steppers):
                suffix = ""
                if axis == 1:
                    if gsd is None:
                        gsd = s.get_step_dist()
                    if s in self.all_gear_rail_steppers and s not in self.selected_gear_steppers:
                        suffix = "*** INACTIVE ***"
                msg += "Stepper %d: %s (trapq: %s) %s\n" % (idx, s.get_name(), self._match_trapq(s.get_trapq()), suffix)
                msg += "  - Commanded Pos: %.2f, " % s.get_commanded_position()
                msg += "MCU Pos: %.2f, " % s.get_mcu_position()
                rd = s.get_rotation_distance()
                msg += "Rotation Dist: %.6f (in %d steps, step_dist=%.6f)\n" % (rd[0], rd[1], s.get_step_dist())

            if show_endstops:
                msg += "Endstops:\n"
                for (mcu_endstop, name) in rail.endstops:
                    if mcu_endstop.__class__.__name__ == "MockEndstop":
                        msg += "- None (Mock - cannot home rail)\n"
                    else:
                        mcu_name = mcu_endstop.get_mcu().get_name() if hasattr(mcu_endstop, 'get_mcu') else mcu_endstop.__class__.__name__
                        msg += "- %s%s, mcu: %s, pin: %s" % (name," (virtual)" if rail.is_endstop_virtual(name) else "", mcu_name, mcu_endstop._pin)
                        msg += " on: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
                msg += "Extra Endstops:\n"
                for (mcu_endstop, name) in rail.extra_endstops:
                    mcu_name = mcu_endstop.get_mcu().get_name() if hasattr(mcu_endstop, 'get_mcu') else mcu_endstop.__class__.__name__
                    msg += "- %s%s, mcu: %s, pin: %s" % (name, " (virtual)" if rail.is_endstop_virtual(name) else "", mcu_name, mcu_endstop._pin)
                    msg += " on: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]

            if axis == 1: # Gear rail
                if self.is_gear_synced_to_extruder():
                    msg += "SYNCHRONIZED: Gear rail synced to extruder '%s'\n" % extruder_name
                if self.is_extruder_synced_to_gear():
                    msg += "SYNCHRONIZED: Extruder '%s' synced to gear rail\n" % extruder_name

        e_stepper = self.printer_toolhead.get_extruder().extruder_stepper
        msg +=  "\nPRINTER TOOLHEAD: %s Last move time: %s\n" % (self.printer_toolhead.get_position(), self.printer_toolhead.get_last_move_time())
        header = "Extruder Stepper: %s %s %s" % (extruder_name, "(MmuExtruderStepper)" if isinstance(e_stepper, MmuExtruderStepper) else "(Non Homing Default)", '-' * 100)
        msg += header[:100] + "\n"
        msg += "  - Stepper trapq: %s\n" % self._match_trapq(e_stepper.stepper.get_trapq())
        msg += "  - Commanded Pos: %.2f, " % e_stepper.stepper.get_commanded_position()
        msg += "MCU Pos: %.2f, " % e_stepper.stepper.get_mcu_position()
        rd = e_stepper.stepper.get_rotation_distance()
        esd = e_stepper.stepper.get_step_dist()
        msg += "Rotation Dist: %.6f (in %d steps, step_dist=%.6f)\n" % (rd[0], rd[1], esd)
        if gsd is not None:
            msg += "Step size ratio of gear:extruder = %.2f:1" % (gsd/esd)
        return msg


# MMU Kinematics class
# (loosely based on corexy.py)
class MmuKinematics:
    def __init__(self, toolhead, config):
        self.printer = config.get_printer()
        self.toolhead = toolhead
        self.mmu_machine = self.printer.lookup_object('mmu_machine')

        # Setup "axis" rails
        self.rails = []
        if self.mmu_machine.selector_type in ['LinearSelector', 'LinearServoSelector', 'LinearMultiGearSelector', 'RotarySelector']:
            self.rails.append(MmuLookupMultiRail(config.getsection(SELECTOR_STEPPER_CONFIG), need_position_minmax=True, default_position_endstop=0.))
            self.rails[0].setup_itersolve('cartesian_stepper_alloc', b'x')
        elif self.mmu_machine.selector_type in ['IndexedSelector']:
            self.rails.append(MmuLookupMultiRail(config.getsection(SELECTOR_STEPPER_CONFIG), need_position_minmax=False, default_position_endstop=0.))
            self.rails[0].setup_itersolve('cartesian_stepper_alloc', b'x')
        else:
            self.rails.append(DummyRail())
        self.rails.append(MmuLookupMultiRail(config.getsection(GEAR_STEPPER_CONFIG), need_position_minmax=False, default_position_endstop=0.))
        self.rails[1].setup_itersolve('cartesian_stepper_alloc', b'y')

        for s in self.get_steppers():
            s.set_trapq(toolhead.get_trapq())
            if not self.toolhead.motion_queuing:
                toolhead.register_step_generator(s.generate_steps)

        # Setup boundary checks
        self.selector_max_velocity, self.selector_max_accel = toolhead.get_selector_limits()
        self.gear_max_velocity, self.gear_max_accel = toolhead.get_gear_limits()
        self.move_accel = None
        self.limits = [(1.0, -1.0)] * len(self.rails)

    def get_steppers(self):
        return [s for rail in self.rails for s in rail.get_steppers()]

    # Aka which stepper to choose to report movement
    def calc_position(self, stepper_positions):
        positions = []
        for i, r in enumerate(self.rails):
            if isinstance(r, DummyRail):
                positions.append(0.)
                continue
            s = next((s for s in r.steppers if s.get_trapq()), None) if i == 1 else None
            name = s.get_name() if s else r.get_name()
            positions.append(stepper_positions[name])
        return positions

    def set_position(self, newpos, homing_axes):
        for i, rail in enumerate(self.rails):
            if i == 1 and self.toolhead.is_gear_synced_to_extruder():
                continue
            rail.set_position(newpos)
            if i in homing_axes:
                self.limits[i] = rail.get_range()

    def home(self, homing_state):
        for axis in homing_state.get_axes():
            if not axis == 0: # Saftey: Only selector (axis[0]) can be homed TODO: make dependent on exact configuration
                continue
            rail = self.rails[axis]
            position_min, position_max = rail.get_range()
            hi = rail.get_homing_info()
            homepos = [None, None, None, None]
            homepos[axis] = hi.position_endstop
            forcepos = list(homepos)
            if hi.positive_dir:
                forcepos[axis] -= 1.5 * (hi.position_endstop - position_min)
            else:
                forcepos[axis] += 1.5 * (position_max - hi.position_endstop)
            homing_state.home_rails([rail], forcepos, homepos) # Perform homing

    def set_accel_limit(self, accel):
        self.move_accel = accel

    def check_move(self, move):
        if self.mmu_machine.selector_type in ['LinearSelector', 'LinearServoSelector', 'LinearMultiGearSelector', 'RotarySelector']:
            limits = self.limits
            xpos, _ = move.end_pos[:2]
            if xpos != 0. and (xpos < limits[0][0] or xpos > limits[0][1]):
                raise move.move_error()

        if move.axes_d[0]: # Selector
            move.limit_speed(self.selector_max_velocity, min(self.selector_max_accel, self.move_accel or self.selector_max_accel))
        elif move.axes_d[1]: # Gear
            move.limit_speed(self.gear_max_velocity, min(self.gear_max_accel, self.move_accel or self.gear_max_accel))

    def get_status(self, eventtime):
        axes = [a for a, (l, h) in zip("xy", self.limits) if l <= h]
        return {
            'homed_axes': "".join(axes),
            'selector_homed': self.limits[0][0] <= self.limits[0][1],
        }


# Extend Klipper homing module to leverage MMU "toolhead"
# (code pulled from homing.py)
class MmuHoming(Homing, object):
    def __init__(self, printer, mmu_toolhead):
        super(MmuHoming, self).__init__(printer)
        self.toolhead = mmu_toolhead # Override default toolhead

    def home_rails(self, rails, forcepos, movepos):
        # Notify of upcoming homing operation
        self.printer.send_event("homing:home_rails_begin", self, rails)
        # Alter kinematics class to think printer is at forcepos
        homing_axes = [axis for axis in range(3) if forcepos[axis] is not None]
        startpos = self._fill_coord(forcepos)
        homepos = self._fill_coord(movepos)
        self.toolhead.set_position(startpos, homing_axes=homing_axes)
        # Perform first home
        endstops = [es for rail in rails for es in rail.get_endstops()]
        hi = rails[0].get_homing_info()
        hmove = HomingMove(self.printer, endstops, self.toolhead) # Happy Hare: Override default toolhead
        hmove.homing_move(homepos, hi.speed)
        # Perform second home
        if hi.retract_dist:
            # Retract
            startpos = self._fill_coord(forcepos)
            homepos = self._fill_coord(movepos)
            axes_d = [hp - sp for hp, sp in zip(homepos, startpos)]
            move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
            retract_r = min(1., hi.retract_dist / move_d)
            retractpos = [hp - ad * retract_r
                          for hp, ad in zip(homepos, axes_d)]
            self.toolhead.move(retractpos, hi.retract_speed)
            # Home again
            startpos = [rp - ad * retract_r
                        for rp, ad in zip(retractpos, axes_d)]
            self.toolhead.set_position(startpos)
            hmove = HomingMove(self.printer, endstops, self.toolhead) # Happy Hare: Override default toolhead
            hmove.homing_move(homepos, hi.second_homing_speed)
            if hmove.check_no_movement() is not None:
                raise self.printer.command_error(
                    "Endstop %s still triggered after retract"
                    % (hmove.check_no_movement(),))
        # Signal home operation complete
        self.toolhead.flush_step_generation()
        self.trigger_mcu_pos = {sp.stepper_name: sp.trig_pos
                                for sp in hmove.stepper_positions}
        self.adjust_pos = {}
        self.printer.send_event("homing:home_rails_end", self, rails)
        if any(self.adjust_pos.values()):
            # Apply any homing offsets
            kin = self.toolhead.get_kinematics()
            homepos = self.toolhead.get_position()
            kin_spos = {s.get_name(): (s.get_commanded_position()
                                       + self.adjust_pos.get(s.get_name(), 0.))
                        for s in kin.get_steppers()}
            newpos = kin.calc_position(kin_spos)
            for axis in homing_axes:
                homepos[axis] = newpos[axis]
            self.toolhead.set_position(homepos)


_StepperPrinterRail = stepper.PrinterRail if hasattr(stepper, 'PrinterRail') else stepper.GenericPrinterRail


# Extend PrinterRail to allow for multiple (switchable) endstops and to allow for no default endstop
# (defined in stepper.py)
class MmuPrinterRail(_StepperPrinterRail, object):
    def __init__(self, config, **kwargs):
        self.printer = config.get_printer()
        self.config = config
        self.rail_name = config.get_name()
        self.query_endstops = self.printer.load_object(config, 'query_endstops')
        self.extra_endstops = []
        self.virtual_endstops = []
        # Starting with klipper v0.13.0-79 there is a `config.get('endstop_pin')` with no default which throws an error
        # So we must put a fake value in there to avoid the error, but we don't want to do that on older versions
        if config.get('endstop_pin', None) is None and hasattr(super(MmuPrinterRail, self), 'lookup_endstop'):
            config.fileconfig.set(config.section, 'endstop_pin', 'mock')
        super(MmuPrinterRail, self).__init__(config, **kwargs)

        # Prior to klipper v0.13.0-79 this was done in the base class
        if (len(self.get_steppers()) == 0):
            self.add_stepper_from_config(config)

    def lookup_endstop(self, endstop_pin, name, **kwargs):
        if endstop_pin == 'mock':
            return self.MockEndstop()
        elif hasattr(super(MmuPrinterRail, self), 'lookup_endstop'):
            return super(MmuPrinterRail, self).lookup_endstop(endstop_pin, name, **kwargs)

    def add_stepper_from_config(self, config, **kwargs):
        if not self.endstops and config.get('endstop_pin', None) is None:
            # No endstop defined, so configure a mock endstop. The rail is, of course, only homable
            # if it has a properly configured endstop at runtime
            self.endstops = [(self.MockEndstop(), "mock")] # Hack: pretend we have a default endstop so super class will work

        logging.info("MMU: Setting up mmu stepper %s" % config.section)

        if hasattr(super(MmuPrinterRail, self), 'add_extra_stepper'):
            super(MmuPrinterRail, self).add_extra_stepper(config, **kwargs)
        else:
            super(MmuPrinterRail, self).add_stepper_from_config(config, **kwargs)

        # Setup default endstop similarly to "extra" endstops with vanity sensor name
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin and endstop_pin != 'mock':
            last_mcu_es=self.endstops[-1]
            # Remove the default endstop name if alternative name is specified
            endstop_name = config.get('endstop_name', None)
            if endstop_name:
                self.endstops.pop()
                self.endstops.append((last_mcu_es[0], endstop_name))
                qee = self.query_endstops.endstops
                if qee:
                    qee.pop()
                self.query_endstops.register_endstop(self.endstops[0][0], endstop_name)
                self.extra_endstops.append((last_mcu_es[0], endstop_name))
                self.extra_endstops.append((last_mcu_es[0], 'default'))
                if 'virtual_endstop' in endstop_pin:
                    self.virtual_endstops.append(endstop_name)
            if 'virtual_endstop' in endstop_pin:
                self.virtual_endstops.append('default')

        # Handle any extra endstops
        extra_endstop_pins = config.getlist('extra_endstop_pins', [])
        extra_endstop_names = config.getlist('extra_endstop_names', [])
        if extra_endstop_pins:
            if len(extra_endstop_pins) != len(extra_endstop_names):
                raise self.config.error("`extra_endstop_pins` and `extra_endstop_names` are different lengths")
            for idx, pin in enumerate(extra_endstop_pins):
                name = extra_endstop_names[idx]
                self.add_extra_endstop(pin, name)

    # backwards compatibility (klipper v0.13.0-79) ... old: add_extra_stepper. new: add_stepper_from_config
    def add_extra_stepper(self, config, **kwargs):
        self.add_stepper_from_config(config, **kwargs)

    def add_extra_endstop(self, pin, name, register=True, bind_rail_steppers=True, mcu_endstop=None):
        is_virtual = 'virtual_endstop' in pin
        if is_virtual:
            if name not in self.virtual_endstops:
                self.virtual_endstops.append(name)
            else:
                raise self.config.error("Extra virtual endstop '%s' defined more than once" % name)
        if mcu_endstop is None:
            ppins = self.printer.lookup_object('pins')
            mcu_endstop = ppins.setup_pin('endstop', pin)
        self.extra_endstops.append((mcu_endstop, name))
        if bind_rail_steppers:
            for s in self.steppers if not is_virtual else [self.steppers[-1]]:
                try:
                    mcu_endstop.add_stepper(s)
                except Exception as e:
                    logging.info("MMU: Not possible to add stepper %s to endstop %s because: %s" % (s.get_name(), name, str(e)))
        if register: # and not self.is_endstop_virtual(name):
            self.query_endstops.register_endstop(mcu_endstop, name)
        return mcu_endstop

    def get_extra_endstop_names(self):
        return [x[1] for x in self.extra_endstops]

    # Returns the endstop of given name as list to match get_endstops()
    def get_extra_endstop(self, name):
        for x in self.extra_endstops:
            if x[1] == name:
                return [x]
        return None

    def is_endstop_virtual(self, name):
        return name in self.virtual_endstops if name else False

    def set_direction(self, direction):
        for stepper in self.steppers:
            if not isinstance(stepper, (MmuExtruderStepper, ExtruderStepper)):
                stepper.set_dir_inverted(direction)

    class MockEndstop:
        def add_stepper(self, *args, **kwargs):
            pass


# Wrapper for multiple stepper motor support
def MmuLookupMultiRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuPrinterRail(config, need_position_minmax=need_position_minmax, default_position_endstop=default_position_endstop, units_in_radians=units_in_radians)
    for i in range(1, 23): # Don't allow "_0" or it is confusing with unprefixed initial stepper
        section_name = "%s_%d" % (config.get_name(), i)
        if not config.has_section(section_name):
            break
        rail.add_stepper_from_config(config.getsection(section_name))
    return rail


# Extend ExtruderStepper to allow for adding and managing endstops (useful only when part of gear rail, not operating as an Extruder)
class MmuExtruderStepper(ExtruderStepper, object):
    def __init__(self, config, gear_rail):
        super(MmuExtruderStepper, self).__init__(config)

        # Ensure corresponding TMC section is loaded so endstops can be added and to prevent error later when toolhead is created
        for chip in TMC_CHIPS:
            try:
                _ = self.printer.load_object(config, '%s extruder' % chip)
                break
            except:
                pass

        # This allows for setup of stallguard as an option for nozzle homing
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            mcu_endstop = gear_rail.add_extra_endstop(endstop_pin, 'mmu_ext_touch', bind_rail_steppers=False)
            mcu_endstop.add_stepper(self.stepper)

    # Override to add QUIET option to control console logging
    def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
        pressure_advance = gcmd.get_float('ADVANCE', self.pressure_advance, minval=0.)
        smooth_time = gcmd.get_float('SMOOTH_TIME', self.pressure_advance_smooth_time, minval=0., maxval=.200)
        self._set_pressure_advance(pressure_advance, smooth_time)
        msg = "pressure_advance: %.6f\n" "pressure_advance_smooth_time: %.6f" % (pressure_advance, smooth_time)
        self.printer.set_rollover_info(self.name, "%s: %s" % (self.name, msg))
        if not gcmd.get_int('QUIET', 0, minval=0, maxval=1):
            gcmd.respond_info(msg, log=False)

class DummyRail:
    def __init__(self):
        self.steppers = []
        self.endstops = []
        self.extra_endstops = []
        self.rail_name = "Dummy"

    def get_name(self):
        return self.rail_name

    def get_steppers(self):
        return self.steppers

    def get_endstops(self):
        return self.endstops

    def set_position(self, newpos):
        pass


def load_config(config):
    return MmuMachine(config)
