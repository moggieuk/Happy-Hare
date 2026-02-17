# Happy Hare MMU Software
#
# Definition of virtual MMU
#   - allows for specification of multiple mmu_units
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from .                     import mmu_unit
from .mmu                  import Mmu
from .mmu.mmu_sensor_utils import MmuSensorFactory

# Klipper imports
from kinematics.extruder   import ExtruderStepper

class MmuMachine:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config

        self.unit_names = list(config.getlist('units'))
        self.num_units = len(self.unit_names)

        # By default HH uses its modified homing extruder. Because this might have unknown consequences on certain
        # set-ups it can be disabled. If disabled, homing moves will still work, but the delay in mcu to mcu comms
        # can lead to several mm of error depending on speed. Also homing of just the extruder is not possible.
        self.extruder_name = config.get('extruder_name', 'extruder')
        self.homing_extruder = bool(config.getint('homing_extruder', 1, minval=0, maxval=1))

        # Setup sensors common to all mmu_units
        #
        # extruder & toolhead sensor:
        #   Wrapper around `filament_switch_sensor` disabling all functionality - just for visability
        #   Named `extruder` & `toolhead`
        event_delay = config.get('event_delay', 0.5)
        self.sensor_factory = sf = MmuSensorFactory(self.printer)

        # Setup single extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        self.extruder_sensor = sf.create_mmu_sensor(
            config,
            Mmu.SENSOR_EXTRUDER_ENTRY,
            None,
            switch_pin,
            event_delay,
            insert=True,
            runout=True
        )

        # Setup single toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        self.toolhead_sensor = sf.create_mmu_sensor(
            config,
            Mmu.SENSOR_TOOLHEAD,
            None,
            switch_pin,
            event_delay
        )

# PAUL TODO integrate this for alternative extruder entrance sensor...
# PAUL force options of 'extruder' or 'toolhead' only
#        # For Qidi printers or any other that use a hall_filament_width_sensor as an endstop
#        hall_sensor_endstop = config.get('hall_sensor_endstop', None)
#        if hall_sensor_endstop is not None:
#            if hall_sensor_endstop == 'extruder':
#                target_name = Mmu.SENSOR_EXTRUDER_ENTRY
#            elif hall_sensor_endstop == 'toolhead':
#                target_name = Mmu.SENSOR_TOOLHEAD
#            else:
#                target_name = hall_sensor_endstop
#
#            self.hall_pin1 = config.get('hall_adc1')
#            self.hall_pin2 = config.get('hall_adc2')
#            self.hall_dia1 = config.getfloat('hall_cal_dia1', 1.5)
#            self.hall_dia2 = config.getfloat('hall_cal_dia2', 2.0)
#            self.hall_rawdia1 = config.getint('hall_raw_dia1', 9500)
#            self.hall_rawdia2 = config.getint('hall_raw_dia2', 10500)
#            self.hall_runout_dia = config.getfloat('hall_min_diameter', 1.0)
#            # self.hall_runout_dia_max = config.getfloat('hall_max_diameter', 2.0) - Unused for trigger
#
#            s = MmuHallEndstop(config, target_name, self.hall_pin1, self.hall_pin2,
#                               self.hall_dia1, self.hall_rawdia1, self.hall_dia2, self.hall_rawdia2,
#                               hall_runout_dia=self.hall_runout_dia,
#                               insert=True, runout=True)
# OLD:self.sensors[target_name] = s
#            if hall_sensor_endstop == 'extruder':
#                self.extruder_sensor = s
#            elif hall_sensor_endstop == 'toolhead':
#                self.toolhead_sensor = s

        self.num_gates = 0     # Total number of vitual mmu gates
        self.units = []        # Unit by index
        self.unit_by_name = {} # Unit lookup by name
        self.unit_by_gate = [] # Quick unit lookup by gate
        self.unit_status = {}  # Aggregated status for backward comptability

        for i, name in enumerate(self.unit_names):
            section = "mmu_unit %s" % name
            logging.info("MMU: Loading section %s" % section)

            if not config.has_section(section):
                raise config.error("Expected [%s] section not found" % section)
            c = config.getsection(section)
            unit = mmu_unit.MmuUnit(c, self, i, self.num_gates)
            logging.info("MMU: Created: %s" % c.get_name())
            self.printer.add_object(c.get_name(), unit) # Register mmu_unit to prevent it being loaded by klipper

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates

            # PAUL can't we just call get_status() on the mmu_unit?
            #unit_info = {}
            #unit_info['name'] = unit.display_name
            #unit_info['vendor'] = unit.mmu_vendor
            #unit_info['version'] = unit.mmu_version_string
            #unit_info['num_gates'] = unit.num_gates
            #unit_info['first_gate'] = self.num_gates
            #unit_info['selector_type'] = unit.selector_type
            #unit_info['variable_rotation_distances'] = unit.variable_rotation_distances
            #unit_info['variable_bowden_lengths'] = unit.variable_bowden_lengths
            #unit_info['require_bowden_move'] = unit.require_bowden_move
            #unit_info['filament_always_gripped'] = unit.filament_always_gripped
            #unit_info['has_bypass'] = unit.has_bypass
            #unit_info['multi_gear'] = unit.multigear
            self.unit_status["unit_%d" % i] = unit.get_status(0)

            self.num_gates += unit.num_gates

        self.unit_status['num_units'] = self.num_units
        self.unit_status['num_gates'] = self.num_gates

        # Setup homing extruder
        self.mmu_extruder_stepper = None
        if self.homing_extruder:
            # Create MmuExtruderStepper for later insertion into PrinterExtruder on Toolhead (on klippy:connect)
            self.mmu_extruder_stepper = MmuExtruderStepper(config.getsection(self.extruder_name), self.units)

            # Nullify original extruder stepper definition so Klipper doesn't try to create it again. Restore in handle_connect()
            self.old_ext_options = {}
            for i in mmu_unit.SHAREABLE_STEPPER_PARAMS + mmu_unit.OTHER_STEPPER_PARAMS:
                if config.fileconfig.has_option('extruder', i):
                    self.old_ext_options[i] = config.fileconfig.get('extruder', i)
                    config.fileconfig.remove_option('extruder', i)

        self.printer.register_event_handler('klippy:connect', self.handle_connect)

    def handle_connect(self):
        printer_extruder = self.printer.lookup_object('toolhead').get_extruder()
        if self.homing_extruder:
            # Restore original extruder options in case user macros reference them
            for key, value in self.old_ext_options.items():
                self.config.fileconfig.set('extruder', key, value)

            # Now we can switch in homing MmuExtruderStepper
            printer_extruder.extruder_stepper = self.mmu_extruder_stepper
            self.mmu_extruder_stepper.stepper.set_trapq(printer_extruder.get_trapq())
        else:
            self.mmu_extruder_stepper = printer_extruder.extruder_stepper

        # Find TMC for extruder
        self.extruder_tmc = None
        for chip in mmu_unit.TMC_CHIPS:
            self.extruder_tmc = self.printer.lookup_object("%s %s" % (chip, printer_extruder.name), None)
            break
        if self.extruder_tmc is not None:
            logging.info("MMU: Found %s on extruder '%s'. Current control enabled. %s" % (chip, printer_extruder.name, "Stallguard 'touch' extruder homing possible." if self.homing_extruder else ""))
        else:
            logging.info("MMU: TMC driver not found for extruder, cannot use current increase for tip forming move")

    def reinit(self):
        for unit in self.units:
            unit.reinit()

    def enable_motors(self):
        for unit in self.units:
            unit.enable_motors()

    def disable_motors(self):
        for unit in self.units:
            unit.disable_motors()

    def get_mmu_unit_by_index(self, index):
        if index >= 0 and index < self.num_units:
            return self.units[index]
        return None

    def get_mmu_unit_by_gate(self, gate):
        if gate >= 0 and gate < self.num_gates:
            return self.unit_by_gate[gate]
        return None

    def get_mmu_unit_by_name(self, name):
        return self.unit_by_name.get(name, None)

    def get_status(self, eventtime):
        return self.unit_status

# Extend ExtruderStepper to allow for adding and managing endstops (useful only when part of gear rail, not operating as an Extruder)
class MmuExtruderStepper(ExtruderStepper, object):
    def __init__(self, config, units):
        super(MmuExtruderStepper, self).__init__(config)

        # Ensure corresponding TMC section is loaded so endstops can be added and to prevent error later when toolhead is created
        for chip in mmu_unit.TMC_CHIPS:
            try:
                section = '%s extruder' % chip
                _ = self.printer.load_object(config, section)
                logging.info("MMU: Loaded: %s" % section)
                break
            except:
                pass

        # This allows for setup of stallguard as an option for nozzle homing on all mmu_units
        endstop_pin = config.get('endstop_pin', None)
        if endstop_pin:
            for unit in units:
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

def load_config(config):
    logging.info("PAUL: HERE")
    return MmuMachine(config)
