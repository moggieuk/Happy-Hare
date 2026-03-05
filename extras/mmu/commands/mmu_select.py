# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SELECT and MMU_SELECTO_BYPASS commands
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


class MmuSelectCommand(BaseCommand):

    CMD = "MMU_SELECT"

    HELP_BRIEF = "Select the specified logical tool (following TTG map) or physical gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "TOOL   = #(int) Logical tool index (0..num_gates-1)\n"
        + "GATE   = #(int) Physical gate index (0..num_gates-1)\n"
        + "BYPASS = [0|1]\n"
        + "(must specify TOOL, GATE, or BYPASS)\n"
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
        if self.mmu.check_if_not_homed(): return
        if self.mmu.check_if_loaded(): return
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu.num_gates - 1)
        if self.mmu.check_if_not_calibrated(CALIBRATED_SELECTOR, check_gates=[gate] if gate >= 0 else None): return
        self.mmu._fix_started_state()

        bypass = gcmd.get_int('BYPASS', -1, minval=0, maxval=1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu.num_gates - 1)
        if tool == -1 and gate == -1 and bypass == -1:
            raise gcmd.error("Error on 'MMU_SELECT': missing TOOL, GATE or BYPASS")

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._select(bypass, tool, gate)
                msg = self.mmu._mmu_visual_to_string()
                msg += "\n%s" % self.mmu._state_to_string()
                self.mmu.log_info(msg, color=True)
        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
