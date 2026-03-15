# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_PURGE command
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


class MmuTestPurgeCommand(BaseCommand):
    """
    Convenience macro for calling the standalone purging macro.
    """

    CMD = "MMU_TEST_PURGE"

    HELP_BRIEF = "Convenience macro for calling the standalone purging macro"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "LAST_TOOL = t\n"
        + "NEXT_TOOL = t\n"
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

        last_tool = gcmd.get_int('LAST_TOOL', mmu._last_tool, minval=0, maxval=mmu.num_gates - 1)
        next_tool = gcmd.get_int('NEXT_TOOL', mmu.tool_selected, minval=0, maxval=mmu.num_gates - 1)
        if next_tool < 0: next_tool = 0

        if not mmu.p.purge_macro:
            mmu.log_warning("Purge not possible because `purge_macro` is not defined")
            return

        try:
            # Determine purge volume for test (mimick regular call to purge macro)
            mmu.toolchange_purge_volume = mmu._calc_purge_volume(last_tool, next_tool)

            _last_tool, _next_tool = mmu._last_tool, mmu._next_tool
            mmu._last_tool, mmu._next_tool = last_tool, next_tool  # Valid only during this test

            msg = "Note that the suggested purge volume is based on the current MMU_SLICER_TOOL_MAP"
            msg += "\nIf this is not set you might find it useful to run 'MMU_CALC_PURGE_VOLUMES MULTIPLIER=..'"
            msg += "\nto create a purge volume map from current filament colors. You can also specify"
            msg += "'LAST_TOOL=.. NEXT_TOOL=..' to this command to override currently loaded tool"
            mmu.log_info(msg)

            mmu.log_info("Calling purge macro '%s'" % mmu.p.purge_macro)
            with mmu.wrap_action(ACTION_PURGING):
                mmu.purge_standalone()

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))

        finally:
            mmu.toolchange_purge_volume = 0.
            mmu._last_tool, mmu._next_tool = _last_tool, _next_tool  # Restore real values
