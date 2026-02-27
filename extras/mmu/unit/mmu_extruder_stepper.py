# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implements a modified 
#   - extruder endstops and extruder only homing
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# Implementation of MMU "Toolhead" to allow for:
#   - "drip" homing and movement without pauses
#   - bi-directional syncing of extruder to gear rail or gear rail to extruder
#   - extra "standby" endstops
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
import logging

# Klipper imports
import stepper, chelper, toolhead
from kinematics.extruder  import PrinterExtruder, DummyExtruder, ExtruderStepper

# Happy Hare imports
from .mmu_constants            import *


class MmuExtruderStepper:

    def __init__(self, config, mmu_unit, params):
        logging.info("PAUL: init() for MmuBuffer")
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.connected_units = [mmu_unit] # mmu_unit is just the first to load, not necessarily all

        # Setup homing extruder
        self.mmu_extruder_stepper = None
        if self.homing_extruder:
            # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
            self.mmu_extruder_stepper = MmuExtruderStepper(config.getsection(self.extruder_name), self)

            # Nullify original extruder stepper definition so Klipper doesn't try to create it again. Restore in handle_connect()
            self.old_ext_options = {}
            for i in SHAREABLE_STEPPER_PARAMS + OTHER_STEPPER_PARAMS:
                if config.fileconfig.has_option('extruder', i):
                    self.old_ext_options[i] = config.fileconfig.get('extruder', i)
                    config.fileconfig.remove_option('extruder', i)

    def handle_connect(self):
        # Find TMC for extruder for current control
        printer_extruder = self.printer.lookup_object('toolhead').get_extruder()
        self.extruder_tmc = None
        for chip in TMC_CHIPS:
            self.extruder_tmc = self.printer.lookup_object("%s %s" % (chip, printer_extruder.name), None) # PAUL fix, change printer_extruder.name
            break
        if self.extruder_tmc is not None:
            logging.info("MMU: Found %s on extruder '%s'. Current control enabled. %s" % (chip, printer_extruder.name, "Stallguard 'touch' extruder homing possible." if self.homing_extruder else ""))
        else:
            logging.info("MMU: TMC driver not found for extruder, cannot use current increase for tip forming move")

        # This monitors extruder movement. We create one per MMU unit to allow for each
        # unit to be connected to a different extruder.
        self.extruder_monitor = ExtruderMonitor(self.mmu)

    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


# Extend ExtruderStepper to allow for adding and managing endstops (useful only when part of gear rail, not operating as an Extruder)
class MmuExtruderStepper(ExtruderStepper, object):
    def __init__(self, config, unit):
        super(MmuExtruderStepper, self).__init__(config)

        # Ensure corresponding TMC section is loaded so endstops can be added and to prevent error later when toolhead is created
        for chip in TMC_CHIPS:
            try:
                section = '%s extruder' % chip
                _ = self.printer.load_object(config, section)
                logging.info("MMU: Loaded: %s" % section)
                break
            except:
                pass

        # This allows for setup of stallguard as an option for nozzle homing
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            gear_rail = unit.mmu_toolhead.get_kinematics().rails[1]
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

