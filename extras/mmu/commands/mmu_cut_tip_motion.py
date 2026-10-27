# Happy Hare MMU Software
# This file may be distributed under the terms of the GNU GPLv3 license.

from .mmu_base_command import *


class MmuCutTipMotionCommand(BaseCommand):
    """Run the cutter helpers with current restoration even when a move fails."""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register("_MMU_CUT_TIP_MOTION", self._run,
                      "Perform the cutter motion and restore cutting current",
                      "PIN_PARK_X_LOC, PIN_PARK_Y_LOC = pin approach coordinates",
                      category=CATEGORY_INTERNAL, log=False)

    def _run(self, gcmd):
        x = gcmd.get_float('PIN_PARK_X_LOC')
        y = gcmd.get_float('PIN_PARK_Y_LOC')
        park = "PIN_PARK_X_LOC=%s PIN_PARK_Y_LOC=%s" % (x, y)
        run = self.mmu.gcode.run_script_from_command
        try:
            run("_CUT_TIP_ADJUST_CURRENT")
            run("_CUT_TIP_MOVE_TO_CUTTER_PIN " + park)
            run("_CUT_TIP_GANTRY_SERVO_DOWN")
            run("_CUT_TIP_DO_CUT_MOTION " + park)
            run("_CUT_TIP_GANTRY_SERVO_UP")
        finally:
            run("_CUT_TIP_RESTORE_CURRENT")
