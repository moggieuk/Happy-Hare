# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Macro Selector
#  - Universal selector control via macros
#  - Great for experimention
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ...mmu_constants    import *
from ..mmu_calibrator    import CALIBRATED_SELECTOR
from .mmu_base_selectors import BaseSelector


################################################################################
# Macro Selector
# Implements macro-based selector for MMU's
#
# Example demultiplexer-style SELECT_TOOL macro:
# [gcode_macro SELECT_TOOL]
# gcode:
#     SET_PIN PIN=d0 VALUE={params.S0}
#     SET_PIN PIN=d1 VALUE={params.S1}
#     SET_PIN PIN=d2 VALUE={params.S2}
# 
# Example optocoupler-style SELECT_TOOL macro:
# [gcode_macro SELECT_TOOL]
# gcode:
#     SET_PIN PIN=o{printer.mmu.gate} VALUE=0
#     SET_PIN PIN=o{params.GATE} VALUE=1
#
################################################################################

class MacroSelector(BaseSelector):

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.is_homed = True

        self.select_tool_macro = config.get('select_tool_macro')
        self.select_tool_num_switches = config.getint('select_tool_num_switches', default=0, minval=1)

        # Check if using a demultiplexer-style setup
        if self.select_tool_num_switches > 0:
            self.binary_mode = True
            max_num_tools = 2**self.select_tool_num_switches
            # Verify that there aren't too many tools for the demultiplexer
            if mmu_unit.num_gates > max_num_tools:
                raise config.error('Maximum number of allowed tools is %d, but %d are present.' % (max_num_tools, mmu_unit.num_gates))
        else:
            self.binary_mode = False

        # Read all controller parameters related to selector or servo to stop klipper complaining. This
        # is done to allow for uniform and shared mmu_parameters.cfg file regardless of configuration.
        for option in ['selector_', 'servo_', 'cad_']:
            for key in config.get_prefix_options(option):
                _ = config.get(key)

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()

        self.self.calibrator.mark_calibrated(CALIBRATED_SELECTOR) # No calibration necessary

    def handle_ready(self):
        super().handle_ready()

        logging.info("Happy Hare MacroSelector: Gate %d" % self.mmu.gate_selected)
        self.select_gate(self.mmu.gate_selected)

    def select_gate(self, gate):
        # Store parameters as list
        params = ['GATE=' + str(gate)]
        if self.binary_mode: # If demultiplexer, pass binary parameters to the macro in the form of S0=, S1=, S2=, etc.
            binary = list(reversed('{0:b}'.format(gate).zfill(self.select_tool_num_switches)))
            for i in range(self.select_tool_num_switches):
                char = binary[i]
                params.append('S' + str(i) + '=' + str(char))
        params = ' '.join(params)

        # Call selector macro
        self.mmu.wrap_gcode_command('%s %s' % (self.select_tool_macro, params))
