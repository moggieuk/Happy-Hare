# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Macro Selector
#  - Universal selector control via macros
#  - Great for experimentation
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging
from typing                 import Sequence

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_base_parameters import TunableParametersBase, ParamSpec, _REQUIRED
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_base_selectors    import BaseSelector


# -----------------------------------------------------------------------------------------------------------
# Parameters for macro selector
# -----------------------------------------------------------------------------------------------------------

class MacroSelectorParameters(TunableParametersBase):

    _SPECS: Sequence[ParamSpec] = (
        ParamSpec('select_tool_macro',        'str', _REQUIRED, section="SELECTOR"),
        ParamSpec('select_tool_num_switches', 'int',  0,        section="SELECTOR", limits=dict(minval=0), hidden=True),
    )

    def __init__(self, config, selector):
        self._selector = selector
        super().__init__(config)


# -----------------------------------------------------------------------------------------------------------
# MacroSelector implementation
# -----------------------------------------------------------------------------------------------------------

class MacroSelector(BaseSelector):
    """
    Macro-based selector for MMU gate selection.

    Invokes a user-defined macro (e.g. SELECT_TOOL) to select a gate. Supports
    either demultiplexer-style binary parameters (S0=, S1=, ...) or direct gate
    selection via GATE= for optocoupler-style setups.
    """
    PARAMS_CLS = MacroSelectorParameters

    def __init__(self, config, mmu_unit, params):
        """
        Initialize macro selector configuration.

        Determines whether selection uses binary switch parameters based on
        select_tool_num_switches and validates gate count for demultiplexer mode.
        """
        super().__init__(config, mmu_unit, params)

        self.is_homed = True
        self.requires_homing = False

        # Check if using a demultiplexer-style setup
        if self.p.select_tool_num_switches > 0:
            self.binary_mode = True
            max_num_tools = 2**self.p.select_tool_num_switches
            # Verify that there aren't too many tools for the demultiplexer
            if mmu_unit.num_gates > max_num_tools:
                raise config.error('Maximum number of allowed tools is %d, but %d are present.' % (max_num_tools, mmu_unit.num_gates))
        else:
            self.binary_mode = False


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()

        self.mmu_unit.calibrator.mark_calibrated(CALIBRATED_SELECTOR) # No calibration necessary


    def handle_ready(self):
        super().handle_ready()


    def _select_gate(self, lgate):
        """
        Select the specified gate by invoking the configured macro.

        Passes the machine-wide GATE=<n> used by existing macros and the
        unit-local LGATE=<n>. In binary mode, also passes S0=..Sn= bits for
        demultiplexer-style selectors.
        """
        # Store parameters as list
        gate = self._logical_gate(lgate) if lgate >= 0 else lgate
        params = ['GATE=' + str(gate), 'LGATE=' + str(lgate)]
        if self.binary_mode and lgate >= 0: # If demultiplexer, pass binary parameters to the macro in the form of S0=, S1=, S2=, etc.
            binary = list(reversed('{0:b}'.format(lgate).zfill(self.p.select_tool_num_switches)))
            for i in range(self.p.select_tool_num_switches):
                char = binary[i]
                params.append('S' + str(i) + '=' + str(char))
        params = ' '.join(params)

        # Call selector macro
        self.mmu.wrap_gcode_command('%s %s' % (self.p.select_tool_macro, params))
