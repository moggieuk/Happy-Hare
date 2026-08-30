# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_PRINT_START / MMU_PRINT_END commands
#
# Goal: Bookends for print start / stop.
#       Automatically called if printing from virtual SD-card but better
#       to be added to slicer gcode begin/end blocks.
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


class MmuPrintStartCommand(BaseCommand):

    CMD = "MMU_PRINT_START"

    HELP_BRIEF = "Forces initialization of MMU state ready for print (usually automatic)"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
    )
    HELP_SUPPLEMENT = (
        "Call at the start of your print in the slicer's gcode start block\n"
        "Examples:\n"
        + f"{CMD} ...Initialize MMU state ready for a print\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_MACROS
        )

    def _run(self, gcmd):
        # BaseCommand already logs commandline + handles HELP=1.
        mmu = self.mmu

        if not mmu.is_in_print():
            mmu.on_print_start()
            mmu._clear_macro_state(reset=True)


class MmuPrintEndCommand(BaseCommand):

    CMD = "MMU_PRINT_END"

    HELP_BRIEF = "Forces clean up of state after after print end"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "IDLE_TIMEOUT = [0|1] Internally set if called by klipper idle_timeout\n"
        + "STATE        = [complete|error|cancelled|ready|standby] End state, defaults to complete\n"
    )
    HELP_SUPPLEMENT = (
        "Call without parameters at the end of your print in the slicer's gcode end block\n"
        "Examples:\n"
        + f"{CMD}               ...Clean up MMU state after a normal (complete) print\n"
        + f"{CMD} STATE=cancelled ...Clean up after a cancelled print\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_MACROS
        )

    def _run(self, gcmd):
        # BaseCommand already logs commandline + handles HELP=1.
        mmu = self.mmu

        idle_timeout = gcmd.get_int('IDLE_TIMEOUT', 0, minval=0, maxval=1)
        end_state = gcmd.get('STATE', "complete")

        if not mmu.is_in_endstate():
            # If an MMU error has paused us (typically MMU_UNLOAD failing inside
            # the slicer's end-gcode), the slicer's PRINT_END has been skipped
            # by pause_resume. Tearing down the safety net here would leave the
            # heaters on with no further owner. Defer the end-state transition
            # until the user recovers via MMU_UNLOCK / MMU_RECOVER / RESUME, at
            # which point PRINT_END will run and the automatic print_stats ->
            # complete transition will re-queue this command in a clean state.
            if mmu.is_mmu_paused():
                mmu.log_debug("MMU_PRINT_END(STATE=%s) ignored while in %s state; deferring end-state transition until print is resumed/cancelled"
                              % (end_state, mmu.psm.print_state))
                return
            if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
                if not idle_timeout and end_state in ["complete"]:
                    mmu._save_toolhead_position_and_park("complete")
                mmu.on_print_end(end_state)
            else:
                raise gcmd.error("Unknown endstate '%s'" % end_state)
