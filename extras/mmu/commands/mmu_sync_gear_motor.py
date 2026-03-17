# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SYNC_GEAR_MOTOR command
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


class MmuSyncGearMotorCommand(BaseCommand):
    """
    Sync the MMU gear motor to the extruder stepper.
    """

    CMD = "MMU_SYNC_GEAR_MOTOR"

    HELP_BRIEF = "Sync the MMU gear motor to the extruder stepper"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "SYNC = [0|1] Specify whether to force extruder/mmu syncing out of a print\n"
        + "(no parameters will default SYNC=1)"
    )
    HELP_SUPPLEMENT = ""

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

        if self.check_if_disabled():
            return

        if self.check_if_bypass(): return

        sync = gcmd.get_int('SYNC', 1, minval=0, maxval=1)

        if not sync and self.check_if_always_gripped():
            return

        # Sticky standalone sync when not in print
        if not mmu.is_in_print() and mmu._standalone_sync != sync:
            mmu._standalone_sync = sync
            if mmu._standalone_sync:
                mmu.log_info(
                    "MMU gear stepper will be synced with extruder whenever filament is in extruder"
                )
            else:
                mmu.log_info(
                    "MMU gear stepper is unsynced from extruder"
                )

        if sync and mmu.filament_pos < FILAMENT_POS_EXTRUDER_ENTRY:
            mmu.log_warning(
                "Will temporarily sync, but filament position does not indicate in extruder!\n"
                "Use 'MMU_RECOVER' to correct the filament position."
            )

        mmu.reset_sync_gear_to_extruder(
            sync,
            force_grip=True,
            skip_extruder_check=True
        )
