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
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET     = 1 To minimize console reporting\n"
        + "RESET     = 1 To reset filament attributes to configured defaults\n"
        + "DETAIL    = 1 Include additional details like EndlessSpool grouping\n"
        + "MAP       = g,g,g Comma separated list of gates where index is the tool number. For bulk update\n"
        + "GATE      = g \n"
        + "GATE      = g Specify the gate\n"
        + "TOOL      = t Specify the tool\n"
        + "AVAILABLE = [0|1] Optionally specify the filament availablity in the gate\n"
        + "(no parameters for status report)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} TOOL=2 GATES=5 ...Map T2 to gate 5\n"
        + f"{CMD} RESET=1        ...Reset TTG map to configured default (generally, Tx > gate_x for all gates\n"
        + f"{CMD} MAP=0,0,0,0    ...Quickly map all tools (on 4 gate MMU) to the same gate 0 (forced MMU print to single filament)\n"
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
                mmu.gate_maps.reset_ttg_map()

            elif ttg_map != "!":
                ttg_map = gcmd.get('MAP').split(",")
                if len(ttg_map) != mmu.num_gates:
                    mmu.log_always("The number of map values (%d) is not the same as number of gates (%d)" % (len(ttg_map), mmu.num_gates))
                    return
                mmu.gate_maps.ttg_map = []
                for gate_str in ttg_map:
                    if gate_str.isdigit():
                        mmu.ttg_map.append(int(gate_str))
                    else:
                        mmu.ttg_map.append(0)
                mmu.gate_maps.persist_ttg_map()

            elif gate != -1:
                status = mmu.gate_status[gate]
                if not available == GATE_UNKNOWN or (available == GATE_UNKNOWN and status == GATE_EMPTY):
                    status = available
                if tool == -1:
                    mmu.gate_maps.set_gate_status(gate, status)
                else:
                    mmu.gate_maps.remap_tool(tool, gate, status)

            else:
                quiet = False  # Display current TTG map

            if not quiet:
                msg = mmu.gate_maps.ttg_map_to_string(show_groups=detail)
                if not detail and mmu.endless_spool_enabled:
                    msg += "\nDETAIL=1 to see EndlessSpool map"
                mmu.log_info(msg, color=True)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
