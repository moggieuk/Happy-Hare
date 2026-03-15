# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SELECT_BYPASS command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants     import *
from ..mmu_utils         import MmuError
from .mmu_base_command   import *


class MmuSelectBypassCommand(BaseCommand):

    CMD = "MMU_SELECT_BYPASS"

    HELP_BRIEF = "Select the filament bypass (alias for MMU_SELECT BYPASS=1)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
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
            category=CATEGORY_OTHER
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return
        if mmu.check_if_loaded(): return
        if mmu.check_if_not_calibrated(CALIBRATED_SELECTOR): return
        mmu._fix_started_state()

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu.select_bypass()
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
