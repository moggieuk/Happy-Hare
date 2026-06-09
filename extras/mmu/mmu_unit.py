# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Definition of basic physical characteristics of MMU (including type/style)
#   - allows for hardware configuration validation
#
# Implementation of MMU "Toolhead" to allow for:
#   - "drip" homing and movement without pauses
#   - bi-directional syncing of extruder to gear rail or gear rail to extruder
#   - extra "standby" endstops
#   - extruder endstops and extruder only homing
#   - switchable drive steppers on rails
#
# Kinematics logic based on code by Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, importlib, math, os, time, re, traceback

from dataclasses                        import dataclass, replace
from itertools                          import chain

# Klipper imports

# Happy Hare imports
from ..mmu_stepper                      import MmuStepper
from .mmu_constants                     import *
from .unit.mmu_encoder                  import MmuEncoder
from .unit.mmu_buffer                   import MmuBuffer
from .unit.mmu_espooler                 import MmuESpooler
from .unit.mmu_leds                     import MmuLeds
from .unit.mmu_sensors                  import MmuSensors
from .unit.mmu_unit_parameters          import MmuUnitParameters
from .unit.mmu_calibrator               import MmuCalibrator
from .unit.mmu_toolhead_wrapper         import MmuToolheadWrapper
from .unit.mmu_extruder_wrapper         import MmuExtruderWrapper
from .unit.mmu_drive                    import MmuDrive
from .unit.mmu_sync_feedback            import MmuSyncFeedback
from .unit.selectors                    import SELECTOR_REGISTRY
from .unit.selectors.mmu_base_selectors import VirtualSelector
from .unit.mmu_environment_manager      import MmuEnvironmentManager


# Default selector classes for known vendors
SELECTOR_VIRTUAL           = 'VirtualSelector'         # Type-B design
SELECTOR_LINEAR            = 'LinearSelector'          # Type-A with linear stepper
SELECTOR_LINEAR_SERVO      = 'LinearServoSelector'     # Type-A with linear stepper and servo filament grip
SELECTOR_SERVO             = 'ServoSelector'           # Type-A with servo gate selector
SELECTOR_ROTARY            = 'RotarySelector'          # Type-A (Chameleon) design
SELECTOR_MACRO             = 'MacroSelector'           # Fully user implemented
SELECTOR_INDEXED           = 'IndexedSelector'         # Type-A with rotating indexed stepper gate selection (BTT ViViD design)
SELECTOR_LINEAR_MULTI_GEAR = 'LinearMultiGearSelector' # Type-C with linear stepper


# Define type/style of MMU and expand configuration for convenience. Validate hardware configuration
class MmuUnit:

    def __init__(self, config, mmu_machine, unit_index, first_gate):
        self.config = config
        self.mmu_machine = mmu_machine
        self.unit_index = unit_index
        self.first_gate = first_gate
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()

        self.num_gates = config.getint('num_gates')
        self.mmu_vendor = config.getchoice('vendor', {o: o for o in VENDORS}, VENDOR_OTHER)

        self.mmu_version_string = config.get('version', "1.0")
        version = re.sub("[^0-9.]", "", self.mmu_version_string) or "1.0"
        try:
            self.mmu_version = float(version)
        except ValueError:
            raise config.error("Invalid version parameter")

        self.display_name = config.get('display_name', UNIT_ALT_DISPLAY_NAMES.get(self.mmu_vendor, self.mmu_vendor))


        # ---------------------------------------------------------------------------------------------------
        # Determine default MMU "design" based on vendor
        # ---------------------------------------------------------------------------------------------------

        @dataclass(frozen=True)
        class MmuUnitProfile:
            selector_type: str = SELECTOR_VIRTUAL    # Selector type or no (Virtual) selector
            variable_rotation_distances: bool = True # Does each gate have different BMG drive gears (I.e. drive rotation distance is variable)
            variable_bowden_lengths: bool = False    # Does each gate of the MMU have different bowden path
            require_bowden_move: bool = True         # Does design require "bowden move" (i.e. non zero length bowden)
            filament_always_gripped: bool = False    # Is filament always gripped by MMU (overrides gear/extruder syncing assumptions)
            show_bypass: bool = False                # Does design has selectable filament bypass (only type-A and type-C). Only one allowed per mmu_machine!

        DEF_PROFILE = MmuUnitProfile()

        VENDOR_PROFILES = {
            VENDOR_ERCF:         replace(DEF_PROFILE, selector_type=SELECTOR_LINEAR_SERVO, show_bypass=True),
            VENDOR_TRADRACK:     replace(DEF_PROFILE, selector_type=SELECTOR_LINEAR_SERVO, variable_rotation_distances=False),
            VENDOR_ANGRY_BEAVER: replace(DEF_PROFILE, require_bowden_move=False, filament_always_gripped=True),
            VENDOR_BOX_TURTLE:   replace(DEF_PROFILE, filament_always_gripped=True),
            VENDOR_NIGHT_OWL:    replace(DEF_PROFILE, filament_always_gripped=True),
            VENDOR_3MS:          replace(DEF_PROFILE, require_bowden_move=False, filament_always_gripped=True),
            VENDOR_3D_CHAMELEON: replace(DEF_PROFILE, selector_type=SELECTOR_ROTARY, variable_rotation_distances=False, variable_bowden_lengths=True),
            VENDOR_PICO_MMU:     replace(DEF_PROFILE, selector_type=SELECTOR_SERVO,  variable_rotation_distances=False),
            VENDOR_QUATTRO_BOX:  replace(DEF_PROFILE, filament_always_gripped=True),
            VENDOR_MMX:          replace(DEF_PROFILE, selector_type=SELECTOR_SERVO),
            VENDOR_MMX6:         replace(DEF_PROFILE, selector_type=SELECTOR_ROTARY),
            VENDOR_VVD:          replace(DEF_PROFILE, selector_type=SELECTOR_INDEXED, filament_always_gripped=True),
            VENDOR_KMS:          replace(DEF_PROFILE, filament_always_gripped=True),
            VENDOR_EMU:          replace(DEF_PROFILE, variable_bowden_lengths=True, filament_always_gripped=True),
            VENDOR_LOW_RIDER:    replace(DEF_PROFILE, selector_type=SELECTOR_ROTARY),
        }

        if self.mmu_vendor == VENDOR_PRUSA:
            raise config.error("Prusa MMU is not yet supported")

        profile = VENDOR_PROFILES.get(self.mmu_vendor, DEF_PROFILE)

        # Still allow MMU design attributes to be altered or set for custom MMU
        self.selector_type =               config.getchoice('selector_type', {o: o for o in SELECTOR_REGISTRY.keys()}, profile.selector_type)
        self.variable_rotation_distances = bool(config.getint('variable_rotation_distances', profile.variable_rotation_distances))
        self.variable_bowden_lengths =     bool(config.getint('variable_bowden_lengths', profile.variable_bowden_lengths))
        self.require_bowden_move =         bool(config.getint('require_bowden_move', profile.require_bowden_move))
        self.filament_always_gripped =     bool(config.getint('filament_always_gripped', profile.filament_always_gripped))
        self.show_bypass =                 bool(config.getint('show_bypass', profile.show_bypass))

        # Can selector mechanism allow selection of other gates on unit when filament is loaded
        self.can_crossload = self.selector_type in [SELECTOR_VIRTUAL, SELECTOR_SERVO, SELECTOR_INDEXED, SELECTOR_MACRO, SELECTOR_ROTARY]


        # ---------------------------------------------------------------------------------------------------
        # Optional heater and evironment sensors
        # ---------------------------------------------------------------------------------------------------

        # Helper to try common name prefixes to avoid long specification like:
        # 'temperature_sensor MMU_enclosure' or 'heater_generic MMU_heater'
        def resolve_object_name(config, obj_name, prefix, kind):
            if not obj_name:
                return ''

            candidates = [obj_name] if obj_name.startswith(prefix) else [prefix + obj_name, obj_name]

            errors = []
            for candidate in candidates:
                try:
                    self.printer.load_object(config, candidate)
                    return candidate
                except Exception as e:
                    errors.append("%s: %s" % (candidate, str(e)))

            raise config.error(
                "Object '%s' could not be loaded as a valid %s in [mmu_machine]\n"
                "Tried:\n%s" % (obj_name, kind, "\n".join(errors))
            )

        self.environment_sensor = config.get('environment_sensor', '')
        self.filament_heater    = config.get('filament_heater', '')
        self.environment_sensors = list(config.getlist('environment_sensors', []))
        self.filament_heaters = list(config.getlist('filament_heaters', []))
        self.max_concurrent_heaters = config.getint('max_concurrent_heaters', self.num_gates)

        if len(self.environment_sensors) not in [0, self.num_gates]:
            raise config.error("'environment_sensors' must be empty or a comma separated list of 'num_gates' elements")

        if len(self.filament_heaters) not in [0, self.num_gates]:
            raise config.error("'filament_heaters' must be empty or a comma separated list of 'num_gates' elements")

        if (self.environment_sensor or self.filament_heater) and \
                (self.environment_sensors or self.filament_heaters):
            raise config.error("Can't configure both single and per-gate MMU heaters/environment sensors")

        self.filament_heater = resolve_object_name(
            config, self.filament_heater, "heater_generic ", "heater"
        )
        self.environment_sensor = resolve_object_name(
            config, self.environment_sensor, "temperature_sensor ", "environment sensor"
        )

        self.filament_heaters = [
            resolve_object_name(config, name, "heater_generic ", "heater")
            for name in self.filament_heaters
        ]
        self.environment_sensors = [
            resolve_object_name(config, name, "temperature_sensor ", "environment sensor")
            for name in self.environment_sensors
        ]


        # ---------------------------------------------------------------------------------------------------
        # MMU Extruder
        # ---------------------------------------------------------------------------------------------------

        # Create extruder wrapper (may be shared so check for existence first)
        # This encapsulates extruder stepper control and filament remaining
        extruder_name = config.get('extruder_name', 'extruder')

        mmu_extruder_name = f"mmu_extruder {extruder_name}"
        self.extruder_wrapper = self.printer.lookup_object(mmu_extruder_name, None)
        if not self.extruder_wrapper:
            self.extruder_wrapper = MmuExtruderWrapper(config, mmu_extruder_name, self)
            self.printer.add_object(mmu_extruder_name, self.extruder_wrapper)
            logging.info("MMU: Created: [%s]" % mmu_extruder_name)
        else:
            self.extruder_wrapper.add_unit(self)


        # ---------------------------------------------------------------------------------------------------
        # MMU Drive (Gears)
        # ---------------------------------------------------------------------------------------------------

        self.multigear = False
        if config.get('gear_steppers', None):
            self.multigear = True
            self.mmu_gear_names = list(config.getlist('gear_steppers'))
            if len(self.mmu_gear_names) != self.num_gates:
                raise config.error("gear_steppers is not the correct length, expected %d elements" % self.num_gates)
        else:
            self.mmu_gear_names = [config.get('gear_stepper')] * self.num_gates

        # Find the TMC controller for base gear stepper so we can fill in missing config for other matching steppers
        # and ensure all gear steppers can be loaded
        gear_name = self.mmu_gear_names[0]
        gear_tmc = None
        base_gear_tmc_chip = base_tmc_section = None
        for chip in TMC_CHIPS:
            base_stepper_section = f"mmu_stepper {gear_name}"
            base_tmc_section = '%s %s' % (chip, base_stepper_section)
            if config.has_section(base_tmc_section):
                base_gear_tmc_chip = chip
                gear_tmc = self.printer.load_object(config, base_tmc_section) # Load base gear stepper now
                logging.info("MMU: Loaded: [%s]" % base_tmc_section)
                break

        if gear_tmc is None:
            raise config.error("Gear stepper TMC configuration not found for %s on mmu_unit %s" % (gear_name, self.name))

        # If multiple gear steppers share all possible attributes (saves repeated configuration)
        if self.multigear:
            for i in range(1, self.num_gates):
                gear_name = self.mmu_gear_names[i]

                stepper_section = f"mmu_stepper {gear_name}"
                tmc_section = '%s %s' % (base_gear_tmc_chip, stepper_section)

                if not config.has_section(stepper_section):
                    raise config.error(f"Gear stepper configuration [{stepper_section}] not found for mmu_unit {self.name}")
                    break

                # Share base stepper config section with extra steppers
                for key in SHAREABLE_STEPPER_PARAMS:
                    if not config.fileconfig.has_option(stepper_section, key) and config.fileconfig.has_option(base_stepper_section, key):
                        base_value = config.fileconfig.get(base_stepper_section, key)
                        if base_value:
                            logging.info("MMU: Sharing gear stepper config %s=%s with [%s]" % (key, base_value, stepper_section))
                            config.fileconfig.set(stepper_section, key, base_value)

                # If tmc controller for this extra stepper matches the base we can fill in missing TMC config
                if config.has_section(tmc_section):
                    for key in SHAREABLE_TMC_PARAMS:
                        if config.fileconfig.has_option(base_tmc_section, key) and not config.fileconfig.has_option(tmc_section, key):
                            base_value = config.fileconfig.get(base_tmc_section, key)
                            if base_value:
                                logging.info("MMU: Sharing gear tmc config %s=%s with [%s]" % (key, base_value, tmc_section))
                                config.fileconfig.set(tmc_section, key, base_value)
                    _ = self.printer.load_object(config, tmc_section) # Load extra gear stepper now
                    logging.info("MMU: Loaded: [%s]" % tmc_section)
                else:
                    # Regardless of tmc chip type, find and try to load all extra gear steppers (may be complete config on different TMC)
                    for chip in TMC_CHIPS:
                        alt_tmc_section = '%s %s' % (chip, stepper_section)
                        if config.has_section(alt_tmc_section):
                            _ = self.printer.load_object(config, alt_tmc_section)
                            logging.info("MMU: Loaded: [%s]" % alt_tmc_section)
                            break

        # Now load the mmu_steppers and create control wrappers
        self.drives = []
        drive = None
        for i, sname in enumerate(self.mmu_gear_names):
            if drive is None or self.multigear:
                section = f"mmu_stepper {sname}"
                # Force load now to force_rail=True [aka gear = self.printer.load_object(config, section)]
                c = config.getsection(section)
                gear = MmuStepper(c, default_mode='manual', force_rail=True)
                self.printer.add_object(c.get_name(), gear)
                logging.info(f"MMU: Loaded: [{section}]")
                drive = MmuDrive(config, self, gear, self.extruder_wrapper.homing_extruder_stepper)

            logging.info(f"MMU: Created: MmuDrive for gate {self.first_gate + i} using mmu_stepper {sname}")
            self.drives.append(drive)


        # ---------------------------------------------------------------------------------------------------
        # Load subcomponents
        # ---------------------------------------------------------------------------------------------------

        # Load parameters config for this unit
        params = c = config.getsection('mmu_unit_parameters %s' % self.name)
        self.p = MmuUnitParameters(c, self)
        logging.info("MMU: Read: [%s]" % c.get_name())

        # Create calibrator to oversee autotune / calibration updates based on available telemetry
        self.calibrator = MmuCalibrator(params, self, self.p)
        logging.info("MMU: Created: Calibrator for %s" % self.name)

        # Load selector (reads it's own config from mmu_unit_parameters)
        selector_class = SELECTOR_REGISTRY.get(self.selector_type)
        if selector_class is None:
            raise self.config.error(
                "Invalid Selector class %s for unit %s. Options are: %s"
                % (self.selector_type, self.name, ",".join(SELECTOR_REGISTRY))
            )

        self.selector = selector_class(params, self, self.p)
        logging.info("MMU: Created %s selector" % self.selector_type)

        # Create toolhead wrapper (probably shared so check for existence first)
        # This encapsulates demensions and provides possibility of dissimilar toolheads on IDEX printer,
        # however, for now the toolhead is a shared "default" definition in mmu.cfg
        toolhead_name = config.get('toolhead')
        section = 'mmu_toolhead %s' % toolhead_name
        self.toolhead_wrapper = self.printer.lookup_object(section, None)
        if not self.toolhead_wrapper:
            if config.has_section(section):
                c = config.getsection(section)
                self.toolhead_wrapper = MmuToolheadWrapper(c, self)
                self.printer.add_object(c.get_name(), self.toolhead_wrapper)
                logging.info("MMU: Created: [%s]" % c.get_name())
            else:
                raise config.error("MMU Printer toolhead section [%s] not found!" % section)
        else:
            self.toolhead_wrapper.add_unit(self)

        # Load mmu_sensors
        self.sensors = None
        section = 'mmu_sensors %s' % self.name
        if config.has_section(section):
            c = config.getsection(section)
            self.sensors = MmuSensors(c, self, self.p)
            #self.printer.add_object(c.get_name(), self.sensors) # Not exposing because we don't want direct access
            logging.info("MMU: Created: [%s]" % c.get_name())
        else:
            logging.info("MMU: - No mmu_sensors specified")

        # Load optional mmu_espooler
        self.espooler = None
        espooler_name = config.get('espooler', None)
        section = 'mmu_espooler %s' % espooler_name
        if config.has_section(section):
            c = config.getsection(section)
            self.espooler = MmuESpooler(c, self, self.p)
            #self.printer.add_object(c.get_name(), self.espooler) # Note exposing because we don't want direct access
            logging.info("MMU: Created: [%s]" % c.get_name())
        else:
            logging.info("MMU: - No mmu_espooler specified")

        # Load optional mmu_leds
        self.leds = None
        section = 'mmu_leds %s' % self.name
        if config.has_section(section):
            c = config.getsection(section)
            self.leds = MmuLeds(c, self, self.p)
            self.printer.add_object(c.get_name(), self.leds) # Must register
            logging.info("MMU: Created: [%s]" % c.get_name())
        else:
            logging.info("MMU: - No mmu_leds specified")

        # Load optional mmu_encoder (can be a shared encoder so check for existance first)
        self.encoder = None
        encoder_name = config.get('encoder', None)
        if encoder_name:
            section = 'mmu_encoder %s' % encoder_name
            self.encoder = self.printer.lookup_object(section, None)
            if not self.encoder:
                if config.has_section(section):
                    c = config.getsection(section)
                    self.encoder = MmuEncoder(c, self, self.p)
                    self.printer.add_object(c.get_name(), self.encoder) # Must register
                    logging.info("MMU: Created: [%s]" % c.get_name())
                else:
                    raise config.error("Encoder section [%s] not found!" % section)
            else:
                self.encoder.add_unit(self)
        else:
            logging.info("MMU: - No mmu_encoder specified")

        # Load optional sync-feedback mmu_buffer (can be a shared buffer so check for existance first)
        self.buffer = None
        buffer_name = config.get('buffer', None)
        if buffer_name:
            section = 'mmu_buffer %s' % buffer_name
            self.buffer = self.printer.lookup_object(section, None)
            if not self.buffer:
                if config.has_section(section):
                    c = config.getsection(section)
                    self.buffer = MmuBuffer(c, self, self.p)
                    self.printer.add_object(c.get_name(), self.buffer) # Must register
                    logging.info("MMU: Created: [%s]" % c.get_name())
                else:
                    raise config.error("Buffer section [%s] not found!" % section)
            else:
                self.buffer.add_unit(self)
        else:
            logging.info("MMU: - No mmu_buffer specified")

        # Create sync-feedback controller (created even if no buffer or encoder)
        self.sync_feedback = MmuSyncFeedback(params, self, self.p)
        logging.info("MMU: Created: sync-feedback / autotune controller for unit %s" % self.name)

        # Create environment manager
        self.environment_manager = MmuEnvironmentManager(params, self, self.p)
        logging.info("MMU: Created: heater and environment manager for unit %s" % self.name)

        self.subcomponents = [
            self.calibrator,
            self.toolhead_wrapper,
            self.selector,
            self.sensors,
            self.espooler,
            self.leds,
            self.encoder,
            self.buffer,
            self.sync_feedback,
            self.environment_manager,
        ]


        # ---------------------------------------------------------------------------------------------------
        # Setup endstops for gear and extruder steppers
        # ---------------------------------------------------------------------------------------------------

        ANALOG_ENDSTOP_SENSOR_TYPES = {"MmuAdcSwitchSensor", "MmuHallEndstop"}
        EXTRUDER_EXTRA_ENDSTOPS = {SENSOR_TOOLHEAD, SENSOR_COMPRESSION, SENSOR_TENSION}

        def iter_endstop_sensors(per_gate=True):
            if self.sensors is None:
                return ()

            if per_gate:
                sensor_groups = (
                    (self.sensors.entry_sensors or {}).values(),
                    (self.sensors.exit_sensors or {}).values(),
                )
            else:
                sensor_groups = (
                    [self.sensors.shared_exit_sensor],
                    [self.buffer.compression_sensor] if self.buffer else [],
                    [self.buffer.tension_sensor] if self.buffer else [],
                    (self.toolhead_wrapper.sensors or {}).values(),
                )

            return (
                sensor
                for sensor in chain(*sensor_groups)
                if sensor is not None
            )

        def add_sensor_endstop(sensor, steppers):
            sensor_name = sensor.runout_helper.name
            sensor_pin = sensor.runout_helper.switch_pin

            is_analog_endstop = sensor.__class__.__name__ in ANALOG_ENDSTOP_SENSOR_TYPES
            mcu_endstop = None
            stepper_names = ", ".join(s.name for s in steppers)

            if is_analog_endstop:
                for s in steppers:
                    mcu_endstop = s.rail.add_extra_endstop(sensor_pin, sensor_name, mcu_endstop=sensor)

                e_type = "analog endstop"

            else:
                pin_params = ppins.parse_pin(sensor_pin, True, True)
                share_name = "%s:%s" % (pin_params["chip_name"], pin_params["pin"])
                ppins.allow_multi_use_pin(share_name)

                for s in steppers:
                    mcu_endstop = s.rail.add_extra_endstop(sensor_pin, sensor_name)

                e_type = "digital endstop"

            logging.info(f"MMU: Created {e_type} on stepper {stepper_names} for {self.name} using {sensor_name}")
            return mcu_endstop

        ext = self.extruder_wrapper.homing_extruder_stepper
        ppins = self.printer.lookup_object("pins")

        # First create all the per-gate endstops for the specific gear drives
        for sensor in iter_endstop_sensors(per_gate=True):
            sensor_name = sensor.runout_helper.name
            lgate = int(sensor_name.split("_")[-1])
            drives = [self.drives[lgate]] if self.multigear else self.drives[:1]
            steppers = [drive.mmu_gear_stepper for drive in drives]
            add_sensor_endstop(sensor, steppers)

        # Now create the (unit) shared endstops reusing existing endstops on shared components
        for sensor in iter_endstop_sensors(per_gate=False):
            sensor_name = sensor.runout_helper.name
            simple_sensor_name = sensor_name.split(":", 1)[-1]

            drives = self.drives if self.multigear else self.drives[:1]
            steppers = [drive.mmu_gear_stepper for drive in drives]
            stepper_names = ", ".join(s.name for s in steppers)

            # We create on the shared extruder first so existing mcu_endstop can be reused
            # when multiple units shared the same toolhead and/or sync-feedback-buffer
            if simple_sensor_name in EXTRUDER_EXTRA_ENDSTOPS:
                es = ext.rail.get_extra_endstop(sensor_name)

                if es is None:
                    # Binding with the extruder ensures rapid stopping of extruder stepper
                    # when endstop is hit on synced homing. Otherwise the extruder can continue
                    # to move a small 2-3mm, speed-dependent, distance.
                    mcu_endstop = add_sensor_endstop(sensor, [ext])
                else:
                    # Already exists
                    mcu_endstop = es[0][0]

                # Now just bind gear steppers to same endstop
                for s in steppers:
                    s.rail.add_extra_endstop("", sensor_name, register=False, mcu_endstop=mcu_endstop)

                logging.info(f"MMU: Shared {sensor_name} endstop with stepper {stepper_names} for {self.name}")

            else:
                # Create just for the gear steppers
                add_sensor_endstop(sensor, steppers)

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("mmu:unit_selected", self._handle_unit_selected)


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Master MMU controller

        # Find and record all gear steppers, controlling tmc chip (if available) and default current indexed by gate
        self.mmu_gear_tmcs = []
        self.mmu_gear_currents = []

        for name in self.mmu_gear_names:
            for chip in TMC_CHIPS:
                c = self.printer.lookup_object("%s mmu_stepper %s" % (chip, name), None)
                if c is not None:
                    self.mmu_gear_tmcs.append(c)
                    self.mmu_gear_currents.append(c.get_status(0).get("run_current"))
                    break
            else:
                self.mmu_gear_tmcs.append(None)
                self.mmu_gear_currents.append(None)

        gates_with_tmc    = [i + self.first_gate for i, tmc in enumerate(self.mmu_gear_tmcs) if tmc is not None]
        gates_without_tmc = [i + self.first_gate for i, tmc in enumerate(self.mmu_gear_tmcs) if tmc is None]

        if gates_with_tmc:
            self.mmu.log_debug(
                "Unit %s: Found TMC on gear_stepper (gates: %s). Current control enabled. Stallguard 'touch' homing possible"
                % (self.name, ", ".join(map(str, gates_with_tmc)))
            )

        if gates_without_tmc:
            self.mmu.log_debug(
                "Unit %s: TMC driver not found for gear stepper (gates: %s), cannot use current reduction for collision detection or while synchronized printing"
                % (self.name, ", ".join(map(str, gates_without_tmc)))
            )


    def _handle_unit_selected(self, unit, prev_unit):
        """
        Handler for unit changed event
        To avoid race conditions, only the selected unit takes action
        """
        if unit is not self: return

        if prev_unit is not None:
            # Tell previous unit to deactivate
            prev_unit.extruder_monitor().disable()

        # Activate this unit
        self.extruder_monitor().enable()


    def reinit(self):
        for obj in self.subcomponents:
            if obj is not None:
                method = getattr(obj, "reinit", None)
                if callable(method):
                    method()


    def has_buffer(self):
        return self.buffer is not None

    def has_encoder(self):
        return self.encoder is not None

    def has_espooler(self):
        return self.espooler is not None

    def has_leds(self):
        return self.leds is not None

    def has_heater(self):
        return self.filament_heater or self.filament_heaters

    def has_bypass(self):
        return self.show_bypass

    def motors_onoff(self, on=False, motor="all"):
        if motor in ["all", "gear", "gears"]:
            drives = self.drives if motor == "gears" else [self.drives[0]]
            for d in drives:
                s = d.mmu_gear_stepper
                s.do_enable(on)

        if motor in ["all", "selector"]:
            if on:
                self.selector.enable_motors()
                self.selector.filament_hold_move() # Aka selector move position
            else:
                self.selector.disable_motors()

    def manages_gate(self, gate):
        if not isinstance(gate, int): return False
        if gate == TOOL_GATE_UNKNOWN:
            return True
        if gate == TOOL_GATE_BYPASS and self.selector.has_bypass():
            return True
        return (self.first_gate <= gate < self.first_gate + self.num_gates)


    def gate_bounds(self):
        return self.first_gate, self.first_gate + self.num_gates - 1


    def gate_range(self):
        return list(range(self.first_gate, self.first_gate + self.num_gates))


    def local_gate(self, gate, force_physical=False):
        """
        Convert mmu_machine gate number to relative gate on mmu_unit
        Args:
          'force_physical' will default to local gate 0 if bypass/unknown
           and is safe for when using result for array lookup
        """
        if gate >= 0 and self.manages_gate(gate):
            lgate = gate - self.first_gate
        elif gate < 0:
            lgate = gate # bypass/unknown
        else:
            self.mmu.log_assertion("Fatal: Gate %d is not managed by %s" % (gate, self.name))
            lgate = TOOL_GATE_UNKNOWN

        return lgate if not force_physical else max(0, lgate)


    def logical_gate(self, lgate):
        """
        Convert mmu_unit local gate number to logical mmu_machine number
        """
        if lgate < 0: return lgate # Leave bypass/unknown as is
        return lgate + self.first_gate


    def gear_name(self, gate):
        """
        Return gear name associated with gate
        """
        lgate = self.local_gate(gate, True)
        return self.mmu_gear_names[lgate]

    def drive_obj(self, gate):
        lgate = self.local_gate(gate, True)
        return self.drives[lgate]

    def gear_tmc_obj(self, gate):
        lgate = self.local_gate(gate, True)
        return self.mmu_gear_tmcs[lgate]

    def gear_default_current(self, gate):
        lgate = self.local_gate(gate, True)
        return self.mmu_gear_currents[lgate]


    # Parallel accessors extruder stepper to match MMU gear stepper accessors
    def extruder_name(self):
        return self.extruder_wrapper.extruder_name()

    def extruder_stepper_obj(self):
        return self.extruder_wrapper.extruder_stepper_obj()

    def extruder_tmc_obj(self):
        return self.extruder_wrapper.extruder_tmc_obj()

    def extruder_default_current(self):
        return self.extruder_wrapper.extruder_default_current()

    def extruder_monitor(self):
        return self.extruder_wrapper.extruder_monitor


# -----------------------------------------------------------------------------------------------------------
# EXPERIMENTAL: RAW EJECT AND PRELOAD OPERATIONS ASYNC FROM REGULAR MMU MANAGEMENT
# -----------------------------------------------------------------------------------------------------------

    def can_async_eject(self, gate):
        """
        Return True if can eject, else the reason why not
        """
        if self.mmu.is_printing():  # TODO may be able to relax with new mmu_stepper
            return "because actively printing"

        if not self.manages_gate(self.mmu.gate_selected):
            return True

        if not self.can_crossload:
            return "because MMU can't crossload"

        return True


    def can_async_preload(self, gate):
        """
        Return True if can preload, else the reason why not
        """
        if self.mmu.is_printing():  # TODO may be able to relax with new mmu_stepper
            return "because actively printing"

        if not self.mmu.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate):
            return "because MMU doesn't have an exit sensor fot this gate"

        if not self.can_crossload:
            return "because MMU can't crossload"

        if not self.manages_gate(self.mmu.gate_selected):
            return True

        if self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
            return "because MMU is not unloaded"

        return True


    def preload(self, gate):
        """
        Preload filament into a gate
        """
        lgate = self.local_gate(gate, False)
        mmu = self.mmu

        can_preload = self.can_async_preload(gate)
        if can_preload is not True:
            raise MmuError(f"Not possible to preload filament right now: {can_preload}")

        gate_sensor = mmu.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, gate)
        if gate_sensor is not None:
            if gate_sensor:
                mmu.log_always("Filament already preloaded")
                mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
                return
            else:
                # Minimal load to mmu exit sensor if fitted
                endstop_name = mmu.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)
                mmu.log_always("Preloading...")
                msg = "Homing to %s sensor" % endstop_name
                actual, homed = home_gear_motor(msg, self.p.gate_preload_homing_max, homing_move=1, endstop_name=endstop_name)
                if homed:
                    self.move_gear_motor("Final parking", self.p.gate_preload_parking_distance)
                    mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
                    mmu._check_pending_spool_id(gate) # Have spool_id ready?
                    mmu.log_always("Filament detected and loaded in gate %d" % gate)
                    return
# TODO vvv this part of preload is problematic async
#        else:
#            # Full gate load if no mmu exit sensor
#            for _ in range(u.p.gate_preload_attempts):
#                mmu.log_always("Loading...")
#                try:
#                    mmu._load_gate(allow_retry=False)
#                    mmu._check_pending_spool_id(gate) # Have spool_id ready?
#                    mmu.log_always("Parking...")
#                    mmu._unload_gate()
#                    mmu.log_always("Filament detected and parked in gate %d" % mmu.gate_selected)
#                    return
#                except MmuError as ee:
#                    # Exception just means filament is not loaded yet, so continue
#                    mmu.log_trace("Exception on preload: %s" % str(ee))
# TODO ^^^ this part of preload is problematic async

        if mmu.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate):
            mmu.gate_maps.set_gate_status(gate, GATE_UNKNOWN)
            mmu.log_warning("Filament detected by mmu entry %d sensor but did not complete preload" % gate)
        else:
            mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)
            raise MmuError("Filament not detected")


    def eject(self, gate):
        """
        Fully eject filament from a gate so it can be removed safely
        """
        lgate = self.local_gate(gate, False)
        mmu = self.mmu

        can_eject = self.can_async_eject(gate)
        if can_eject is not True:
            raise MmuError(f"Not possible to eject filament right now: {can_eject}")

        mmu.log_always("Ejecting...")
        if (
            mmu.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate) and
            mmu.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, gate)
        ):
            endstop_name = mmu.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)
            msg = "Reverse homing off %s sensor" % endstop_name
            actual, homed = self._home_gear_motor(lgate, msg, -self.p.gate_homing_max, homing_move=-1, endstop_name=endstop_name)
            if homed:
                self.log_debug("Endstop %s reached after %.1fmm" % (endstop_name, actual))
            else:
                raise MmuError("Filament did not exit gate homing sensor: %s" % endstop_name)

        if self.p.gate_final_eject_distance > 0:
            msg = "Ejecting filament out of gate"
            if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate) is not None:
                # Use homing move so we don't "over eject"
                self._home_gear_motor(lgate, msg, -self.p.gate_final_eject_distance, homing_move=-1, endstop_name=SENSOR_ENTRY_PREFIX)
            else:
                self._move_gear_motor(lgate, msg, -self.p.gate_final_eject_distance)

        mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)
        mmu.log_always("The filament in gate %d can be removed" % gate)


    def _move_gear_motor(self, lgate, msg, move):
        mmu.log_warning(f"TODO: move_gear_motor(lgate={lgate}, msg={msg}, move={move}")
        # TODO future impl with no-toolhead logic


    def _home_gear_motor(self, lgate, msg, move, homing_move, endstop_name):
        mmu.log_warning(f"TODO: home_gear_motor(lgate={lgate}, msg={msg}, move={move}, homing_move={homing_move}, endstop_name={endstop_name})")
        # TODO future impl with no-toolhead logic


# -----------------------------------------------------------------------------------------------------------
# MMU UNIT STATUS
# -----------------------------------------------------------------------------------------------------------

    def get_status(self, eventtime):
        unit_info = {
            'name': self.display_name,
            'vendor': self.mmu_vendor,
            'version': self.mmu_version_string,
            'num_gates': self.num_gates,
            'first_gate': self.first_gate,
            'selector_type': self.selector_type,
            'variable_rotation_distances': self.variable_rotation_distances,
            'variable_bowden_lengths': self.variable_bowden_lengths,
            'require_bowden_move': self.require_bowden_move,
            'filament_always_gripped': self.filament_always_gripped,
            'has_bypass': self.show_bypass,
            'can_crossload': self.can_crossload,
            'multi_gear': self.multigear,
        }

        if self.environment_sensor or self.filament_heater:
            # Single heater/sensor
            unit_info['environment_sensor'] = self.environment_sensor
            unit_info['filament_heater'] = self.filament_heater

        elif self.environment_sensors or self.filament_heaters:
            # Per-gate heater/sensors
            unit_info['environment_sensors'] = self.environment_sensors
            unit_info['filament_heaters'] = self.filament_heaters

        return unit_info
