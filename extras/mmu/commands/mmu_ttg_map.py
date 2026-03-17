# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TTG_MAP (aka MMU_REMAP_TTG) command
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


class MmuTtgMapCommand(BaseCommand):

    CMD = "MMU_TTG_MAP"

    HELP_BRIEF = "aka MMU_REMAP_TTG Display or remap a tool to a specific gate and set gate availability"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "QUIET     = [0|1]\n"
        + "RESET     = [0|1]\n"
        + "DETAIL    = [0|1]\n"
        + "MAP       = comma,separated,tool,map\n"
        + "GATE      = g\n"
        + "TOOL      = t\n"
        + "AVAILABLE = [GATE_EMPTY|GATE_AVAILABLE|...]\n"
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
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        ttg_map = gcmd.get('MAP', "!")
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=mmu.num_gates - 1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=mmu.num_gates - 1)
        available = gcmd.get_int('AVAILABLE', GATE_UNKNOWN, minval=GATE_EMPTY, maxval=GATE_AVAILABLE)

        try:
            if reset == 1:
                mmu._reset_ttg_map()

            elif ttg_map != "!":
                ttg_map = gcmd.get('MAP').split(",")
                if len(ttg_map) != mmu.num_gates:
                    mmu.log_always("The number of map values (%d) is not the same as number of gates (%d)" % (len(ttg_map), mmu.num_gates))
                    return
                mmu.ttg_map = []
                for gate_str in ttg_map:
                    if gate_str.isdigit():
                        mmu.ttg_map.append(int(gate_str))
                    else:
                        mmu.ttg_map.append(0)
                mmu._persist_ttg_map()

            elif gate != -1:
                status = mmu.gate_status[gate]
                if not available == GATE_UNKNOWN or (available == GATE_UNKNOWN and status == GATE_EMPTY):
                    status = available
                if tool == -1:
                    mmu._set_gate_status(gate, status)
                else:
                    mmu._remap_tool(tool, gate, status)

            else:
                quiet = False  # Display current TTG map

            if not quiet:
                msg = mmu._ttg_map_to_string(show_groups=detail)
                if not detail and mmu.endless_spool_enabled:
                    msg += "\nDETAIL=1 to see EndlessSpool map"
                mmu.log_info(msg, color=True)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
