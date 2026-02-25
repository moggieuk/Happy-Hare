# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Definition of logical MMU
#   - allows for specification and aggregation of multiple mmu_units
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, contextlib

# Klipper imports
from kinematics.extruder   import ExtruderStepper

# Happy Hare imports
from .                     import mmu_unit
from .mmu_unit             import MmuUnit
from .mmu.mmu_constants    import *
from .mmu.mmu_sensor_utils import MmuSensorFactory
from .mmu.mmu_parameters   import MmuParameters
from .mmu.mmu_controller   import MmuController


class MmuMachine:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config

        # Instruct users to re-run ./install.sh if version number changes
        self.happy_hare_version = config.getfloat('happy_hare_version', 2.2) # v2.2 was the last release before versioning
        if self.happy_hare_version is not None and self.happy_hare_version < VERSION:
            raise self.config.error("Looks like you upgraded (v%s -> v%s)?\n%s" % (self.p.happy_hare_version, VERSION, UPGRADE_REMINDER))

        self.unit_names = list(config.getlist('units'))
        self.num_units = len(self.unit_names)

        # By default HH uses its modified homing extruder. Because this might have unknown consequences on certain
        # set-ups it can be disabled. If disabled, homing moves will still work, but the delay in mcu to mcu comms
        # can lead to several mm of error depending on speed. Also homing of just the extruder is not possible.
        self.extruder_name = config.get('extruder_name', 'extruder') # PAUL should be per-unit!
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
            SENSOR_EXTRUDER_ENTRY,
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
            SENSOR_TOOLHEAD,
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
#                target_name = SENSOR_EXTRUDER_ENTRY
#            elif hall_sensor_endstop == 'toolhead':
#                target_name = SENSOR_TOOLHEAD
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

        logging.info("MMU: Loaded [%s]" % config.get_name())

        for i, name in enumerate(self.unit_names):
            section = "mmu_unit %s" % name
            logging.info("MMU: Building mmu_unit #%d [%s] ---------------------------" % (i, section))

            if not config.has_section(section):
                raise config.error("Expected [%s] section not found" % section)
            c = config.getsection(section)
            unit = MmuUnit(c, self, i, self.num_gates)
            logging.info("MMU: Created: %s" % c.get_name())
            self.printer.add_object(c.get_name(), unit) # Register mmu_unit to prevent it being loaded by klipper

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates

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

        # Load parameters config for mmu machine
        if not config.has_section('mmu_parameters'):
            raise config.error("Expected [mmu_parameters] section not found")
        c = config.getsection('mmu_parameters')
        self.params = MmuParameters(self, c)
        logging.info("MMU: Read: [%s]" % c.get_name())

        # Create master mmu operations
        self.mmu_controller = MmuController(self, c)
        logging.info("MMU: Created MmuController")

        # Efficient and namespaced save variable management
        self.var_manager = SaveVariableManager(self, c)
        logging.info("MMU: Created SaveVariableManager")

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

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

        self.mmu_controller.handle_connect()

    def handle_disconnect(self):
        self.mmu_controller.handle_disconnect()

    def handle_ready(self):
        self.mmu_controller.handle_ready()

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


#
# Centralization of all save_variable manipulation for per-unit namespacing and efficiency
#
class SaveVariableManager:

    def __init__(self, mmu_machine, config):
        self.mmu_machine = mmu_machine
        self.gcode = self.mmu_machine.printer.lookup_object('gcode')
        self.save_variables = self.mmu_machine.printer.load_object(config, 'save_variables')

        self._can_write_variables = True

        # Sanity check to see that mmu_vars.cfg is included.  This will verify path
        # because default deliberately has 'mmu_revision' entry
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(VARS_MMU_REVISION, None)
            if revision_var is None:
                self.save_variables.allVariables[VARS_MMU_REVISION] = 0
        else:
            revision_var = None
        if not self.save_variables or revision_var is None:
            raise config.error("Calibration settings file (mmu_vars.cfg) not found. Check [save_variables] section in mmu_macro_vars.cfg\nAlso ensure you only have a single [save_variables] section defined in your printer config and it contains the line: mmu__revision = 0. If not, add this line and restart")

    # Namespace variable with mmu unit name if necessary
    def namespace(self, variable, namespace):
        if namespace is not None:
            return variable.replace("mmu_", "mmu_%s_" % namespace)
        return variable

    # Wrappers so we can minimize actual disk writes and batch updates
    def get(self, variable, default, namespace=None):
        return self.save_variables.allVariables.get(self.namespace(variable, namespace), default)

    def set(self, variable, value, namespace=None, write=False):
        self.save_variables.allVariables[self.namespace(variable, namespace)] = value
        if write:
            self.write()

    def delete(self, variable, namespace=None, write=False):
        _ = self.save_variables.allVariables.pop(self.namespace(variable, namespace), None)
        if write:
            self.write()

    def write(self):
        if self._can_write_variables:
            mmu_vars_revision = self.save_variables.allVariables.get(VARS_MMU_REVISION, 0) + 1
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (VARS_MMU_REVISION, mmu_vars_revision))

    def upgrade(self, variable, namespace): # PAUL need this method?
        val = self.get(variable, None)
        if val is not None:
            self.set(variable, val, namespace)
            self.delete(variable)

    @contextlib.contextmanager
    def wrap_suspend_write_variables(self):
        self._can_write_variables = False
        try:
            yield self
        finally:
            self._can_write_variables = True
            self.write()
