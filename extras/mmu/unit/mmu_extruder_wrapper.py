# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Wrapper around klipper extruder that can track extruder movement, filament remaining,
#       current control, etc. Also optional implementation of a modified extruder stepper that
#       has homing ability
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Klipper imports
from kinematics.extruder   import ExtruderStepper

# Happy Hare imports
from ..mmu_constants       import *
from .mmu_extruder_monitor import ExtruderMonitor


class MmuExtruderWrapper():

    def __init__(self, config, name, mmu_unit):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.name = name.split()[-1]
        self.printer = config.get_printer()

        self.connected_units = [mmu_unit]       # mmu_unit is just the first to load, not necessarily all

        self.filament_remaining = 0.            # The amount of filament remaining in extruder hotend
        self.filament_remaining_color = UNKNOWN_FILAMENT_COLOR # Color of remaining filament

        # By default HH uses its modified homing extruder. Because this might have unknown consequences on certain
        # set-ups it can be disabled. If disabled, synced homing moves will still work, but the delay in mcu to mcu
        # comms can lead to several mm of error depending on speed. Also homing of just the extruder is not possible.
        self.homing_extruder = bool(config.getint('homing_extruder', 1, minval=0, maxval=1))

        # Build homing extruder stepper if option enabled ---------------------------------------------------

        self.homing_extruder_stepper = None
        if self.homing_extruder:
            # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
            self.homing_extruder_stepper = MmuExtruderStepper(config.getsection(self.name), self)

            # Nullify original extruder stepper definition so Klipper doesn't try to create it again. Restore in handle_connect()
            self.old_ext_options = {}
            for i in SHAREABLE_STEPPER_PARAMS + OTHER_STEPPER_PARAMS:
                if config.fileconfig.has_option('extruder', i):
                    self.old_ext_options[i] = config.fileconfig.get('extruder', i)
                    config.fileconfig.remove_option('extruder', i)

        # Register event handlers
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)


    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller

        # Always load length of filament remaining in extruder (after cut) and its color
        self.var_manager = self.mmu_machine.var_manager
        self.filament_remaining = self.var_manager.get(VARS_MMU_FILAMENT_REMAINING, self.filament_remaining, self.name)
        self.filament_remaining_color = self.var_manager.get(VARS_MMU_FILAMENT_REMAINING_COLOR, self.filament_remaining_color, self.name)


        # Setup extruder ------------------------------------------------------------------------------------

        # PAUL: how will this work for multiple extruders?
        # PAUL: Need better way to build [mmu_extruder_stepper], perhaps not by hacking existing extruder object!
        # PAUL: Or perhaps looking up directly and not via printer_extruder?
        self.klipper_printer_toolhead = self.printer.lookup_object('toolhead')
        printer_extruder = self.klipper_printer_toolhead.get_extruder()

        if self.homing_extruder and self.homing_extruder_stepper is not None:
            # Restore original extruder options in case user macros reference them
            for key, value in self.old_ext_options.items():
                self.config.fileconfig.set(self.name, key, value)

            # Now we can switch in homing MmuExtruderStepper
            printer_extruder.extruder_stepper = self.homing_extruder_stepper
            self.homing_extruder_stepper.stepper.set_trapq(printer_extruder.get_trapq())

        else:     
            self.mmu.log_debug("Warning: Using original klipper extruder stepper. Extruder homing not possible")

        self._extruder_stepper = printer_extruder.extruder_stepper


        # Find TMC for extruder for current control ---------------------------------------------------------

        self._extruder_tmc = self._extruder_current = None
        for chip in TMC_CHIPS:
            c = self.printer.lookup_object("%s %s" % (chip, self.name), None)
            if c is not None:
                self._extruder_tmc = c
                self._extruder_current = c.get_status(0).get("run_current")
                break

        if self._extruder_tmc:
            msg = (
                "Unit %s: Found %s on extruder '%s'. "
                "Current control enabled."
            ) % (self.mmu_unit.name, chip, self.name)

            if self.homing_extruder:
                msg += " Stallguard 'touch' extruder homing possible."

            self.mmu.log_debug(msg)
        else:
            self.mmu.log_debug(
                "Unit %s: TMC driver not found for extruder '%s'. "
                "Cannot use current increase for tip forming move."
                % (self.mmu_unit.name, self.name)
            )


        # This monitors extruder movement for toolhead connected to this MMU unit
        self.extruder_monitor = ExtruderMonitor(self.mmu)


    def _handle_ready(self):
        pass


    def extruder_name(self):
        return self.name

    def extruder_stepper_obj(self):
        return self._extruder_stepper

    def extruder_tmc_obj(self):
        return self._extruder_tmc

    def extruder_default_current(self):
        return self._extruder_current


    def set_filament_remaining(self, length, color=UNKNOWN_FILAMENT_COLOR):
        self.filament_remaining = length
        self.filament_remaining_color = color
        self.var_manager.set(VARS_MMU_FILAMENT_REMAINING, max(0, round(length, 1)), self.name)
        self.var_manager.set(VARS_MMU_FILAMENT_REMAINING_COLOR, color, self.name, write=True)


    def get_status(self, eventtime):
        return {
            'extruder_filament_remaining': self.filament_remaining + self.mmu_unit.toolhead_wrapper.p.toolhead_residual_filament,
            'filament_remaining': self.filament_remaining,
            'filament_remaining_color': self.filament_remaining_color,
        }



# -----------------------------------------------------------------------------------------------------------
# EXTENDED EXTRUDER STEPPER THAT ALLOWS HOMING
# (useful only when part of gear rail, not operating as an Extruder)
# -----------------------------------------------------------------------------------------------------------

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
