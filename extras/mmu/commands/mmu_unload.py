# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_UNLOAD command
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
from .mmu_base_command import BaseCommand


class MmuUnloadCommand(BaseCommand):

    CMD = "MMU_UNLOAD"

    HELP_BRIEF = "Unloads filament and parks it at the gate or optionally unloads just the extruder (EXTRUDER_ONLY=1)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "EXTRUDER_ONLY = [0|1]\n"
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
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[self.mmu.gate_selected]): return
        self.mmu._fix_started_state()

        if self.mmu.filament_pos == FILAMENT_POS_UNLOADED:
            self.mmu.log_always("Filament not loaded")
            return

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament unload
                    self.mmu._mmu_unload_eject(gcmd)

                    self.mmu._persist_swap_statistics()

        except MmuError as ee:
            self.mmu.handle_mmu_error("%s.\nOccured when unloading tool" % str(ee))
