# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SOAKTEST_LOAD_SEQUENCE command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import random

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuSoaktestLoadSequenceCommand(BaseCommand):
    """
    Soak test tool load/unload sequence accross specified units
    """

    CMD = "MMU_SOAKTEST_LOAD_SEQUENCE"

    HELP_BRIEF = "Soak test tool load/unload sequence"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "UNIT   = #(int)|_name_ Optional to constrain test to specific unit\n"
        + "LOOP   = #(int)        How many times to do complete T0-Tx test loops (default 1)\n"
        + "RANDOM = 1             Randomize tool selection (tools may be skipped)\n"
        + "FULL   = [0|1]         Whether to perform full load to extruder enntry or quick partial bowden load\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} LOOP=2        ...Loop sequentially through all tools twice performing partial bowden load\n"
        + f"{CMD} UNIT=1 FULL=1 ...Loop through all tools on unit 1 loading filament to extruder entrance each time\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return

        mmu_unit = self.get_unit(gcmd, mode="optional")
        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, mmu_unit=mmu_unit): return

        loops = gcmd.get_int('LOOP', 1)
        rand = gcmd.get_int('RANDOM', 0)
        to_nozzle = gcmd.get_int('FULL', 0)

        if mmu_unit is not None:
            valid_gates = mmu_unit.gate_range()
        else:
            valid_gates = list(range(mmu.num_gates))

        try:
            with mmu.wrap_sync_gear_to_extruder():
                initial_tool = max(0, mmu.tool_selected)

                for l in range(loops):
                    mmu.log_always(f"Testing loop {l + 1} / {loops}")
                    for t in range(mmu.num_gates):
                        tool = t
                        if rand == 1:
                            tool = random.randint(0, mmu.num_gates - 1)

                        gate = mmu.ttg_map[tool]
                        if gate not in valid_gates:
                            mmu.log_always(f"Skipping T{tool} of {mmu.num_gates} because gate {gate} is not on selected unit")

                        elif mmu.gate_status[gate] == GATE_EMPTY:
                            mmu.log_always(f"Skipping T{tool} of {mmu.num_gates} because gate {gate} is empty")

                        else:
                            mmu.log_always(f"Testing T{tool} of {mmu.num_gates} (gate {gate})")

                            if not to_nozzle:
                                mmu.select_tool(tool)
                                mmu.load_sequence(bowden_move=100., skip_extruder=True)
                                mmu.unload_sequence(bowden_move=100.)
                            else:
                                mmu._select_and_load_tool(tool, purge=PURGE_NONE)
                                mmu._unload_tool()

                # End with same tool selected
                mmu.select_tool(initial_tool)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
