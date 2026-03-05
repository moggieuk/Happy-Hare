# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ENCODER command
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
from .mmu_base_command import BaseCommand


class MmuEncoderCommand(BaseCommand):

    CMD = "MMU_ENCODER"

    HELP_BRIEF = "Display encoder position and stats or enable/disable runout detection logic in encoder"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "ENABLE = [0|1]\n"
        + "VALUE  = #(float)\n"
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
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu._check_has_encoder(): return
        if self.mmu.check_if_disabled(): return
        value = gcmd.get_float('VALUE', -1, minval=0.)
        enable = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        if enable == 1:
# PAUL old            self.encoder().set_mode(self.enable_clog_detection)
            self.mmu.mmu_unit().sync_feedback.set_encoder_mode() # PAUL pass mode MOGGIE
        elif enable == 0:
# PAUL old            self.encoder().set_mode(self.encoder().RUNOUT_DISABLED)
            self.mmu.mmu_unit().sync_feedback.set_encoder_mode(self.mmu.RUNOUT_DISABLED) # PAUL contant name?? MOGGIE
        elif value >= 0.:
            self.mmu.set_encoder_distance(value)
            return
        self.mmu.log_info(self.mmu._get_encoder_summary(detail=True))
