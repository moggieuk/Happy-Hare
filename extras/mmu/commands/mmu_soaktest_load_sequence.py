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
    Soak test tool load/unload sequence.
    """

    CMD = "MMU_SOAKTEST_LOAD_SEQUENCE"

    HELP_BRIEF = "Soak test tool load/unload sequence"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "LOOP   = #(int)\n"
        + "RANDOM = [0|1]\n"
        + "FULL   = [0|1]\n"
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
            category=CATEGORY_TESTING
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return
        if mmu.check_if_bypass(): return
        if mmu.check_if_loaded(): return
        if mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL): return

        loops = gcmd.get_int('LOOP', 2)
        rand = gcmd.get_int('RANDOM', 0)
        to_nozzle = gcmd.get_int('FULL', 0)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    mmu.log_always("Testing loop %d / %d" % (l, loops))
                    for t in range(mmu.num_gates):
                        tool = t
                        if rand == 1:
                            tool = random.randint(0, mmu.num_gates - 1)

                        gate = mmu.ttg_map[tool]

                        if mmu.gate_status[gate] == GATE_EMPTY:
                            mmu.log_always(
                                "Skipping tool %d of %d because gate %d is empty"
                                % (tool, mmu.num_gates, gate)
                            )
                        else:
                            mmu.log_always(
                                "Testing tool %d of %d (gate %d)"
                                % (tool, mmu.num_gates, gate)
                            )

                            if not to_nozzle:
                                mmu.select_tool(tool)
                                mmu.load_sequence(bowden_move=100., skip_extruder=True)
                                mmu.unload_sequence(bowden_move=100.)
                            else:
                                mmu._select_and_load_tool(tool, purge=PURGE_NONE)
                                mmu._unload_tool()

                mmu.select_tool(0)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
