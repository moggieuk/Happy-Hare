# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_MOTORS_OFF command
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


class MmuMotorsOffCommand(BaseCommand):
    """
    Turn off all MMU motors and servos.

    Note: This command will lose sync state.
    """

    CMD = "MMU_MOTORS_OFF"

    HELP_BRIEF = "Turn off all MMU motors and servos"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled():
            return

        # Explicitly drop sync state before powering down
        self.mmu.sync_gear_to_extruder(False, force_grip=True)
        self.mmu.motors_onoff(on=False)
