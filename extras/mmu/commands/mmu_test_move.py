# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_MOVE command
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
from ..mmu_utils         import MmuError, DebugStepperMovement
from .mmu_base_command   import *
from .mmu_misc_mixins    import MoveMixin


class MmuTestMoveCommand(MoveMixin, BaseCommand):
    """
    Test filament move to help debug setup / options.
    """

    CMD = "MMU_TEST_MOVE"

    HELP_BRIEF = "Test filament move to help debug setup / options"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "ALLOW_BYPASS = [0|1]  Ignore bypass check\n"
        + "MOVE         = mm     Specify the move distance (default 100)\n"
        + "SPEED        = mm/s   Optionally override the default speed\n"
        + "ACCEL        = mm/s^2 Optionally override the default accelarateion\n"
        + "MOTOR        = [gear|extruder|gear+extruder|synced] Select motor to operation on (default: gear)\n"
        + "GRIP         = 1      To retain grip on filament after move for type-A testing\n"
        + "WAIT         = [0|1]  Wait for move to complete (make move synchronous)\n"
        + "DEBUG        = [0|1]  Turn on developer stepper movement debugging\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s SPEED=100                   ...Move filament default 100mm at 100mm/s speed\n" % CMD
        + "%s MOVE=50 MOTOR=gear+extruder ...Move filament 50mm sync extruder synced to gear\n" % CMD
        + "%s MOVE=100 GRIP=1             ...Move filament 100mm retaining filament grip (useful for MMU_CALIBRATE_GEAR on type-A)\n" % CMD
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

        if self.check_if_disabled(): return

        debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1))  # Hidden option
        grip = bool(gcmd.get_int('GRIP', 0, minval=0, maxval=1))
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))

        # If retaining grip must establish before wrap_sync_gear_to_extruder()
        if grip:
            mmu.selector().filament_drive()

        with mmu.wrap_sync_gear_to_extruder():
            with DebugStepperMovement(mmu, debug):
                actual, _, measured, _ = self._move_cmd(gcmd, "Test move", allow_bypass=allow_bypass) # From Mixin

            measured_str = f" (measured {measured:.1f}mm)" if mmu.can_use_encoder() else ""
            mmu.log_always(f"Moved {actual:.1f}mm{measured_str}")

        mmu.log_always(f"Filament position: {mmu.drive().get_filament_position():.2f}")
