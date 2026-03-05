# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ENDLESS_SPOOL command
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


class MmuEndlessSpoolCommand(BaseCommand):

    CMD = "MMU_ENDLESS_SPOOL"

    HELP_BRIEF = "Diplay or Manage EndlessSpool functionality and groups"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "ENABLE = [0|1]\n"
        + "QUIET  = [0|1]\n"
        + "RESET  = [0|1]\n"
        + "GROUPS = comma,separated,group,map\n"
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
        )

    def _run(self, gcmd):
        # BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return

        enabled = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', "!")

        if enabled >= 0:
            self.mmu.endless_spool_enabled = enabled
            self.mmu.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.mmu.endless_spool_enabled, write=True)
            if enabled and not quiet:
                self.mmu.log_always("EndlessSpool is enabled")

        if not self.mmu.endless_spool_enabled:
            self.mmu.log_always("EndlessSpool is disabled")
            return

        if reset:
            self.mmu._reset_endless_spool()

        elif groups != "!":
            groups = gcmd.get('GROUPS', ",".join(map(str, self.mmu.endless_spool_groups))).split(",")
            if len(groups) != self.mmu.num_gates:
                self.mmu.log_always("The number of group values (%d) is not the same as number of gates (%d)" % (len(groups), self.mmu.num_gates))
                return
            self.mmu.endless_spool_groups = []
            for group in groups:
                if group.isdigit():
                    self.mmu.endless_spool_groups.append(int(group))
                else:
                    self.mmu.endless_spool_groups.append(0)
            self.mmu._persist_endless_spool()

        else:
            quiet = False  # Display current map

        if not quiet:
            self.mmu.log_info(self.mmu._es_groups_to_string())
