# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_LOG command
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


class MmuLogCommand(BaseCommand):

    CMD = "MMU_LOG"

    HELP_BRIEF = "Logs messages in MMU log"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "MSG   = _text_\n"
        + "ERROR = [0|1]\n"
        + "DEBUG = [0|1]\n"
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
            category=CATEGORY_MACRO
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        msg = gcmd.get('MSG', "").replace("\\n", "\n").replace(" ", UI_SPACE)

        if gcmd.get_int('ERROR', 0, minval=0, maxval=1):
            self.mmu.log_error(msg)
        elif gcmd.get_int('DEBUG', 0, minval=0, maxval=1):
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)
