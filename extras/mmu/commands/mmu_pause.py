# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_PAUSE command
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


class MmuPauseCommand(BaseCommand):

    CMD = "MMU_PAUSE"

    HELP_BRIEF = "Pause the current print and lock the MMU operations"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "MSG            = _text_\n"
        + "FORCE_IN_PRINT = [0|1]\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f'{CMD}                          ...Pause the MMU and enter the error/recovery state\n'
        + f'{CMD} MSG="Filament tangle"    ...Pause with a custom reason shown to the user\n'
        + f'{CMD} FORCE_IN_PRINT=1         ...Pause using in-print behaviour even when not detected as printing\n'
    )

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
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return

        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        msg = gcmd.get('MSG', "MMU_PAUSE macro was directly called")

        mmu.handle_mmu_error(msg, force_in_print)
