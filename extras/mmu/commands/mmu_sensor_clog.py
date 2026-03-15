# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SENSOR_CLOG command
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
from ..mmu_utils         import MmuError
from .mmu_base_command   import *
from .mmu_command_mixins import ClogTangleMixin


class MmuSensorClogCommand(ClogTangleMixin, BaseCommand):
    """
    Internal MMU filament clog handler.
    Triggered by clog detection sensor/event.
    """

    CMD = "__MMU_SENSOR_CLOG"

    HELP_BRIEF = "Internal MMU filament clog handler"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "EVENTTIME = #(float)\n"
        + "SENSOR    = _sensor_name_\n"
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
        mmu = self.mmu

        try:
            with mmu.wrap_sync_gear_to_extruder():
                self._handle_clog_tangle(gcmd, "clog") # From mixin
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
