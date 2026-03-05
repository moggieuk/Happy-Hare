# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_LOAD command
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


class MmuTestLoadCommand(BaseCommand):
    """
    For quick testing filament loading from gate to the extruder.
    """

    CMD = "MMU_TEST_LOAD"

    HELP_BRIEF = "For quick testing filament loading from gate to the extruder"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "FULL   = [0|1]\n"
        + "LENGTH = #(float) Bowden move length (when FULL=0)\n"
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

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_bypass(): return
        if self.mmu.check_if_loaded(): return
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[self.mmu.gate_selected]): return

        full = gcmd.get_int('FULL', 0, minval=0, maxval=1)

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                if full:
                    self.mmu.load_sequence(skip_extruder=True)
                else:
                    length = gcmd.get_float(
                        'LENGTH',
                        100.,
                        minval=10.,
                        maxval=self.mmu.mmu_unit().calibrator.get_bowden_length()
                    )
                    self.mmu.load_sequence(bowden_move=length, skip_extruder=True)

        except MmuError as ee:
            self.mmu.handle_mmu_error("Load test failed: %s" % str(ee))
