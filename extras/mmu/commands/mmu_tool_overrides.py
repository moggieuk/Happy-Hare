# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TOOL_OVERRIDES command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuToolOverridesCommand(BaseCommand):

    CMD = "MMU_TOOL_OVERRIDES"

    HELP_BRIEF = "Displays, sets or clears tool speed and extrusion factors (M220 & M221)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "TOOL   = t   (optional, -1 for all)\n"
        + "M220   = #(0-200) Speed multiplier percent (100 = unchanged)\n"
        + "M221   = #(0-200) Extrusion multiplier percent (100 = unchanged)\n"
        + "RESET  = [0|1]   Reset overrides to 100%% for specified tool (or all if TOOL omitted)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return

        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu.num_gates)
        speed = gcmd.get_int('M220', None, minval=0, maxval=200)
        extrusion = gcmd.get_int('M221', None, minval=0, maxval=200)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            # reset overrides (100% -> multiplier 1.0)
            self.mmu._set_tool_override(tool, 100, 100)
        elif tool >= 0:
            # set specific tool override (None means leave unchanged)
            self.mmu._set_tool_override(tool, speed, extrusion)

        msg_tool = "Tools: "
        msg_sped = "M220 : "
        msg_extr = "M221 : "
        for i in range(self.mmu.num_gates):
            range_end = 6 if i > 9 else 5
            tool_speed = int(self.mmu.tool_speed_multipliers[i] * 100)
            tool_extr = int(self.mmu.tool_extrusion_multipliers[i] * 100)
            msg_tool += ("| T%d  " % i)[:range_end]
            msg_sped += ("| %d  " % tool_speed)[:range_end]
            msg_extr += ("| %d  " % tool_extr)[:range_end]
        msg = "|\n".join([msg_tool, msg_sped, msg_extr]) + "|\n"
        self.mmu.log_always(msg)
