# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_HOME command
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


class MmuHomeCommand(BaseCommand):

    CMD = "MMU_HOME"

    HELP_BRIEF = "Home the MMU selector"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT         = #(int)|_name_|ALL Specify unit by name, number or all-units (optional if single unit)\n"
        + "TOOL         = #(int) Optionally select tool number after homing\n"
        + "FORCE_UNLOAD = [0|1]  Force unloaded of filament\n"
        + "(no parameters: home selector on single unit setup and select T0)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s UNIT=ALL              ...Home all mmu units with selector kinimatics\n"
        + "%s UNIT=1 FORCE_UNLOAD=1 ...Home unit 1 unloading filament if necessary\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            per_unit=True
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return
        mmu._fix_started_state()

        if self.check_if_not_calibrated(CALIBRATED_SELECTOR):
            mmu.log_always("Not calibrated. Will home to endstop only!")
            tool = -1
            force_unload = 0
        else:
            tool = gcmd.get_int('TOOL', mmu.tool_selected, minval=0, maxval=mmu.num_gates - 1)
            force_unload = gcmd.get_int('FORCE_UNLOAD', None, minval=0, maxval=1)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu.home_unit(mmu_unit, force_unload=force_unload)
                mmu.log_always("Homed")

                # Always select chosen tool
                if tool == TOOL_GATE_BYPASS:
                    mmu.select_bypass()
                elif tool >= 0:
                    gate = mmu.ttg_map[tool]
                    mmu.select_tool(tool)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
