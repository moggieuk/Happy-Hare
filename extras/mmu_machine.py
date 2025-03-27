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
from .mmu_unit           import MmuUnit

class MmuMachine:

    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config

        self.unit_names = list(config.getlist('units'))
        self.num_units = len(self.unit_names)
        self.num_gates = 0     # Total number of vitual mmu gates
        self.units = []        # Unit by index
        self.unit_by_name = {} # Unit lookup by name
        self.unit_by_gate = [] # Quick object lookup by gate
        self.unit_status = {}

        logging.info("PAUL: unit_names=%s" % self.unit_names)
        logging.info("PAUL: num_units=%s" % self.num_units)
       
        for i, name in enumerate(self.unit_names):
            section = "mmu_unit %s" % name
            logging.info("PAUL: load_object(%s)" % section)

            if not config.has_section(section):
                raise config.error("Expected [%s] section not found" % section)
            c = config.getsection(section)
            unit = MmuUnit(c, self, i, self.num_gates) # PAUL added
            logging.info("PAUL: loaded. Registering: %s" % c.get_name())
            self.printer.add_object(c.get_name(), unit) # Register mmu_unit to stop if being loaded by klipper

            self.units.append(unit)
            self.unit_by_name[name] = unit
            self.unit_by_gate[self.num_gates:self.num_gates + unit.num_gates] = [unit] * unit.num_gates

            unit_info = {}
            unit_info['name'] = unit.display_name
            unit_info['vendor'] = unit.mmu_vendor
            unit_info['version'] = unit.mmu_version_string
            unit_info['num_gates'] = unit.num_gates
            unit_info['first_gate'] = self.num_gates
            unit_info['selector_type'] = unit.selector_type
            unit_info['variable_rotation_distances'] = unit.variable_rotation_distances
            unit_info['variable_bowden_lengths'] = unit.variable_bowden_lengths
            unit_info['require_bowden_move'] = unit.require_bowden_move
            unit_info['filament_always_gripped'] = unit.filament_always_gripped
            unit_info['has_bypass'] = unit.has_bypass
            unit_info['multi_gear'] = unit.multigear
            self.unit_status["unit_%d" % i] = unit_info

            self.num_gates += unit.num_gates

        self.unit_status['num_units'] = self.num_units
        self.unit_status['num_gates'] = self.num_gates

    def get_mmu_unit_by_index(self, index):
        if index >= 0 and index < self.num_units:
            return self.units[index]
        return None

    def get_mmu_unit_by_gate(self, gate):
        if gate >= 0 and gate < self.num_gates:
            return self.unit_by_gate[gate]
        return None

    def get_mmu_unit_by_name(self, name):
        return self.unit_by_name(name, None)

    def get_status(self, eventtime):
        return self.unit_status

def load_config(config):
    return MmuMachine(config)
