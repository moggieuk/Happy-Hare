# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_FLOWGUARD command
#  - This is a "per-unit" command
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


class MmuFlowGuardCommand(BaseCommand):

    CMD = "MMU_FLOWGUARD"

    HELP_BRIEF = "Enable/disable FlowGuard (clog-tangle detection)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT   = #(int)|_name_|ALL Specify unit by name, number or all-units (optional if single unit)\n"
        + "ENABLE = [1|0] Enable/disable FlowGuard clog/tangle detection\n"
        + "(no parameters for status report)\n"
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
            category=CATEGORY_GENERAL,
            per_unit=True
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        sf = mmu_unit.sync_feedback # Get sync_feedback associated with unit

        if mmu.check_if_disabled(): return

        if not sf.p.sync_feedback_enabled:
            mmu.log_warning("Sync feedback is disabled or not configured. FlowGuard is unavailable")
            return

        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)

        if enable is not None:
            sf.config_flowguard_feature(enable)
            return

        # Just report status
        if sf.p.flowguard_enabled:
            active = " and currently active" if sf.flowguard_active else " (not currently active)"
            mmu.log_always("FlowGuard monitoring feature is enabled%s" % active)
        else:
            mmu.log_always("FlowGuard monitoring feature is disabled")
