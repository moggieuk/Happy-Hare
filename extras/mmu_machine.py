# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
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
from .mmu                        import mmu_unit
from .mmu.mmu_unit               import MmuUnit
from .mmu.mmu_constants          import *
from .mmu.mmu_utils              import SaveVariableManager
from .mmu.mmu_sensor_utils       import MmuSensorFactory
from .mmu.mmu_machine_parameters import MmuMachineParameters
from .mmu.mmu_controller         import MmuController


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

        self.num_gates = 0           # Total number gates on system
        self.units = []              # Unit by index
        self.unit_by_name = {}       # Unit lookup by name
        self.unit_by_gate = []       # Quick unit lookup by gate
        self.unit_status = {}        # Aggregated status for backward comptability
        self.unit_with_bypass = None # Unit with selectable bypass (only one allowed)

        logging.info("MMU: Loaded [%s]" % config.get_name())

        for i, name in enumerate(self.unit_names):
            section = "mmu_unit %s" % name
            logging.info("MMU: Building mmu_unit #%d [%s] ---------------------------" % (i, section))

            if not config.has_section(section):
                raise config.error("Expected [%s] section not found" % section)
            c = config.getsection(section)
            unit = MmuUnit(c, self, i, self.num_gates)
            logging.info("MMU: Created: [%s]" % c.get_name())

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates
            self.unit_status["unit_%d" % i] = unit.get_status(0)
            if unit.has_bypass:
                if self.unit_with_bypass is not None:
                    raise config.error("Only one mmu_unit can have bypass gate. Configured on %s and %s" % (self.unit_with_bypass.name, unit.name))
                self.unit_with_bypass = unit

            self.num_gates += unit.num_gates

        self.unit_status['num_units'] = self.num_units
        self.unit_status['num_gates'] = self.num_gates

        # Load parameters config for mmu machine
        if not config.has_section('mmu_parameters'):
            raise config.error("Expected [mmu_parameters] section not found")
        c = config.getsection('mmu_parameters')
        self.params = MmuMachineParameters(c, self)
        logging.info("MMU: Read: [%s]" % c.get_name())

        # Create master mmu operations
        self.mmu_controller = self.mmu = MmuController(c, self)
        self.printer.add_object('mmu', self.mmu_controller) # Register with klipper for get_status() under legacy name
        logging.info("MMU: Created MmuController")

        # Create efficient and namespaced save variable management
        self.var_manager = SaveVariableManager(c, self)
        logging.info("MMU: Created SaveVariableManager")


    def reinit(self):
        for unit in self.units:
            unit.reinit()


    def get_mmu_unit_by_index(self, index):
        if index >= 0 and index < self.num_units:
            return self.units[index]
        return None


    def get_mmu_unit_by_gate(self, gate):
        if gate >= 0 and gate < self.num_gates:
            return self.unit_by_gate[gate]
        if gate == TOOL_GATE_BYPASS:
            return self.unit_with_bypass
        return None


    def get_mmu_unit_by_name(self, name):
        return self.unit_by_name.get(name, None)


    def get_status(self, eventtime):
        return self.unit_status


def load_config(config):
    return MmuMachine(config)
