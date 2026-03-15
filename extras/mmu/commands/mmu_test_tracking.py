# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_TRACKING command
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


class MmuTestTrackingCommand(BaseCommand):
    """
    Test the tracking of gear feed and encoder sensing.
    """

    CMD = "MMU_TEST_TRACKING"

    HELP_BRIEF = "Test the tracking of gear feed and encoder sensing"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "DIRECTION   = [-1|1]   Move in retract or extruder direction\n"
        + "STEP        = #(float) mm of filament movement between encoder samples\n"
        + "SENSITIVITY = #(float) Override the default/calibrated encoder resolution\n"
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

        if mmu._check_has_encoder(): return
        if mmu.check_if_disabled(): return
        if mmu.check_if_bypass(): return
#PAUL        if mmu.check_if_not_homed(): return
        if mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[mmu.gate_selected]): return

        direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
        step = gcmd.get_float('STEP', 1, minval=0.5, maxval=20)
        sensitivity = gcmd.get_float('SENSITIVITY', mmu.encoder().get_resolution(), minval=0.1, maxval=10)

        if direction == 0:
            return

        try:
            with mmu.wrap_sync_gear_to_extruder():

                if mmu.filament_pos not in [FILAMENT_POS_START_BOWDEN, FILAMENT_POS_IN_BOWDEN]:
                    # Ready MMU for test if not already setup
                    mmu._unload_tool()
                    mmu.load_sequence(
                        bowden_move=100. if direction == DIRECTION_LOAD else 200.,
                        skip_extruder=True
                    )
                    mmu.selector().filament_drive()

                with mmu._require_encoder():
                    mmu._initialize_filament_position()

                    for i in range(1, int(100 / step)):
                        mmu.trace_filament_move(None, direction * step, encoder_dwell=None)
                        measured = mmu.get_encoder_distance()
                        moved = i * step
                        drift = int(round((moved - measured) / sensitivity))

                        if drift > 0:
                            drift_str = "++++++++!!"[0:drift]
                        elif (moved - measured) < 0:
                            drift_str = "--------!!"[0:-drift]
                        else:
                            drift_str = ""

                        mmu.log_info(
                            "Gear/Encoder : %05.2f / %05.2f mm %s"
                            % (moved, measured, drift_str)
                        )

                mmu._unload_tool()

        except MmuError as ee:
            mmu.handle_mmu_error("Tracking test failed: %s" % str(ee))
