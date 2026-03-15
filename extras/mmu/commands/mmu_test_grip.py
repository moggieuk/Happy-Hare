# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_GRIP command
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


class MmuTestGripCommand(BaseCommand):
    """
    Test the MMU grip for a Tool.
    """

    CMD = "MMU_TEST_GRIP"

    HELP_BRIEF = "Test the MMU grip for a Tool"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = ""  # Simple test command

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

        if mmu.check_if_disabled(): 
            return
        if mmu.check_if_bypass(): 
            return

        # Drive filament slightly then disable gear motor to test grip behavior
        mmu.selector().filament_drive()
        mmu.motors_onoff(on=False, motor="gear")
