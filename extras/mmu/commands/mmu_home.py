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
from .mmu_base_command import BaseCommand


class MmuHomeCommand(BaseCommand):

    CMD = "MMU_HOME"

    HELP_BRIEF = "Home the MMU selector"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "TOOL         = #(int) Gate/tool index (0..num_gates-1)\n"
        + "FORCE_UNLOAD = [0|1]\n"
        + "(no parameters: home current selector / default tool)\n"
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
        self.mmu._fix_started_state()

        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_SELECTOR):
            self.mmu.log_always("Not calibrated. Will home to endstop only!")
            tool = -1
            force_unload = 0
        else:
            tool = gcmd.get_int('TOOL', 0, minval=0, maxval=self.mmu.num_gates - 1)
            force_unload = gcmd.get_int('FORCE_UNLOAD', None, minval=0, maxval=1)

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu.home(tool, force_unload=force_unload)
                if tool == -1:
                    self.mmu.log_always("Homed")
        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
