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
import logging

# Happy Hare imports
from .mmu                  import mmu_unit
from .mmu.mmu_unit         import MmuUnit
from .mmu.mmu_constants    import *
from .mmu.mmu_utils        import SaveVariableManager
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

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates

            self.unit_status["unit_%d" % i] = unit.get_status(0)

            self.num_gates += unit.num_gates

        self.unit_status['num_units'] = self.num_units
        self.unit_status['num_gates'] = self.num_gates

        # Load parameters config for mmu machine
        if not config.has_section('mmu_parameters'):
            raise config.error("Expected [mmu_parameters] section not found")
        c = config.getsection('mmu_parameters')
        self.params = MmuParameters(c, self)
        logging.info("MMU: Read: [%s]" % c.get_name())

        # Create master mmu operations
        self.mmu_controller = MmuController(c, self)
        self.printer.add_object('mmu', self.mmu_controller) # Register with klipper for get_status() under legacy name
        logging.info("MMU: Created MmuController")

        # Efficient and namespaced save variable management
        self.var_manager = SaveVariableManager(c, self)
        logging.info("MMU: Created SaveVariableManager")


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

def load_config(config):
    logging.info("PAUL: HERE")
    return MmuMachine(config)
