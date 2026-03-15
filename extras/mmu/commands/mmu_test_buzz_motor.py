# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_BUZZ_MOTOR command
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


class MmuTestBuzzMotorCommand(BaseCommand):
    """
    Simple buzz the selected motor (default gear) for setup testing.
    """

    CMD = "MMU_TEST_BUZZ_MOTOR"

    HELP_BRIEF = "Simple buzz the selected motor (default gear) for setup testing"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "MOTOR = [gear|gears|<selector_motor_name>]\n"
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
        if mmu.check_if_bypass(): return

        motor = gcmd.get('MOTOR', "gear")

        with mmu.wrap_sync_gear_to_extruder():

            if motor == "gear":
                found = mmu.buzz_gear_motor()
                if found is not None:
                    mmu.log_info(
                        "Filament %s by gear motor buzz"
                        % ("detected" if found else "not detected")
                    )

            elif motor == "gears":
                try:
                    for gate in range(mmu.num_gates):
                        mmu.mmu_toolhead().select_gear_stepper(gate)
                        found = mmu.buzz_gear_motor()
                        if found is not None:
                            mmu.log_info(
                                "Filament %s in gate %d by gear motor buzz"
                                % ("detected" if found else "not detected", gate)
                            )
                finally:
                    # Restore original gear selection
                    mmu.mmu_toolhead().select_gear_stepper(mmu.gate_selected)

            elif not mmu.selector().buzz_motor(motor):
                raise gcmd.error("Motor '%s' not known" % motor)
