# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_RUNOUT command
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


class MmuTestRunoutCommand(BaseCommand):
    """
    Manually invoke the clog/runout detection logic for testing.
    """

    CMD = "MMU_TEST_RUNOUT"

    HELP_BRIEF = "Manually invoke the clog/runout detection logic for testing"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "TYPE = _event_type_ (optional, e.g. runout or clog)\n"
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

        event_type = gcmd.get('TYPE', None)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu._runout(event_type=event_type, sensor="TEST")
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
