# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_RESET command
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


class MmuResetCommand(BaseCommand):

    CMD = "MMU_RESET"

    HELP_BRIEF = "Forget persisted state and re-initialize defaults"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "CONFIRM = [0|1]  Must be set to 1 to proceed\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s CONFIRM=1  ...reset all persisted MMU state back to defaults\n" % CMD
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

        if self.mmu.check_if_disabled():
            return

        confirm = gcmd.get_int('CONFIRM', 0, minval=0, maxval=1)
        if confirm != 1:
            self.mmu.log_always("You must re-run and add 'CONFIRM=1' to reset all state back to default")
            return

        self.mmu.reinit()
        self.mmu._reset_statistics()
        self.mmu._reset_endless_spool()
        self.mmu._reset_ttg_map()
        self.mmu._reset_gate_map()

        # Persist key variables
        self.mmu.var_manager.set(VARS_MMU_GATE_SELECTED, self.mmu.gate_selected)
        self.mmu.var_manager.set(VARS_MMU_TOOL_SELECTED, self.mmu.tool_selected)
        self.mmu.var_manager.set(VARS_MMU_FILAMENT_POS, self.mmu.filament_pos)
        self.mmu.var_manager.write()

        self.mmu.log_always("MMU state reset")
        self.mmu._schedule_mmu_bootup_tasks()
