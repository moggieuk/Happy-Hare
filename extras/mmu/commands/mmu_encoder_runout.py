# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ENCODER_RUNOUT command
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


class MmuEncoderRunoutCommand(BaseCommand):
    """
    Internal encoder filament runout handler.
    """

    CMD = "__MMU_ENCODER_RUNOUT"

    HELP_BRIEF = "Internal encoder filament runout handler"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = ""  # Internal callback command

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_INTERNAL
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.

        if not self.mmu.is_enabled:
            # Undo what runout sensor handling did
            self.mmu.pause_resume.send_resume_command()
            return

        self.mmu._fix_started_state()

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu._runout(sensor="Encoder")
        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
