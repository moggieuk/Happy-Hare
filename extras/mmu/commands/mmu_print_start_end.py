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
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = "Call the start of your print in gcode start block"

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
            mmu._on_print_start()
            mmu._clear_macro_state(reset=True)


class MmuPrintEndCommand(BaseCommand):

    CMD = "MMU_PRINT_END"

    HELP_BRIEF = "Forces clean up of state after after print end"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "IDLE_TIMEOUT = [0|1] Internally set if called by klipper idle_timeout\n"
        + "STATE        = [complete|error|cancelled|ready|standby] End state, defaults to complete\n"
    )
    HELP_SUPPLEMENT = "Call without parmeters at the end of your print in gcode end block"

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
            if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
                if not idle_timeout and end_state in ["complete"]:
                    mmu._save_toolhead_position_and_park("complete")
                mmu._on_print_end(end_state)
            else:
                raise gcmd.error("Unknown endstate '%s'" % end_state)
