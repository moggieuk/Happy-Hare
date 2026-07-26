# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_HOMING_MOVE command
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


class MmuTestHomingMoveCommand(MoveMixin, BaseCommand):
    """
    Test filament homing move to help debug setup / options.
    """

    CMD = "MMU_TEST_HOMING_MOVE"

    HELP_BRIEF = "Test filament homing move to help debug setup / options"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "ALLOW_BYPASS = [0|1]  Ignore bypass check\n"
        + "MOVE         = mm     Specify the move distance (default 100)\n"
        + "ENDSTOP      = _endstop_name_\n"
        + "ENDSTOPS     = Comma separated list of endstops (only physical switch endstop possible)\n"
        + "STOP_ON_ENDSTOP = [-1|0|1] 1 for extrude, -1 for retract, 0 for don't stop\n"
        + "SPEED        = mm/s   Optionally override the default speed\n"
        + "ACCEL        = mm/s^2 Optionally override the default accelarateion\n"
        + "MOTOR        = [gear|extruder|gear+extruder] Select motor to operation on (default: gear)\n"
        + "WAIT         = [0|1]  Wait for move to complete (make move synchronous)\n"
        + "DEBUG        = [0|1]  Turn on developer stepper movement debugging\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} MOVE=50 ENDSTOP=extruder STOP_ON_ENDSTOP=1 ...Home up to 50mm in the extrude direction onto the extruder entry sensor\n"
        + f"{CMD} MOVE=100 MOTOR=gear ENDSTOP=encoder STOP_ON_ENDSTOP=1 ...Home the gear motor up to 100mm using the encoder as endstop\n"
        + f"{CMD} MOVE=-100 ENDSTOP=toolhead STOP_ON_ENDSTOP=-1 ...Home up to 100mm in the retract direction off the toolhead sensor\n"
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

        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))

        with mmu.wrap_sync_gear_to_extruder():
            debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1))  # Hidden option
            with DebugStepperMovement(mmu, debug):
                actual, homed, measured, _, trigger = self._homing_move_cmd(gcmd, "Test homing move", allow_bypass=allow_bypass) # From Mixin

                home_str = (
                    f"Homed on '{trigger}'"
                    if homed and trigger
                    else ("Homed" if homed else "Did not home")
                )
            measured_str = f" (measured {measured:.1f}mm)" if mmu.can_use_encoder() else ""
            mmu.log_always(f"{home_str} after {actual:.2f}mm{measured_str}")

        mmu.log_always(f"Filament position: {mmu.drive().get_filament_position():.2f}")
