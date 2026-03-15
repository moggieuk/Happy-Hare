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
        + "GROUPS = comma separated list of group membership\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s GROUPS=1,1,1,1   ...Put all four gates into same endless spool group\n" % CMD
        + "%s RESET=1          ...Reset to default grouping. Typically each gate is in own group\n" % CMD
        + "%s ENABLE=0 QUIET=1 ...Disable endspool feature supressing console/log output\n" % CMD
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
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return

        enabled = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', "!")

        if enabled >= 0:
            mmu.endless_spool_enabled = enabled
            mmu.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, mmu.endless_spool_enabled, write=True)
            if enabled and not quiet:
                mmu.log_always("EndlessSpool is enabled")

        if not mmu.endless_spool_enabled:
            mmu.log_always("EndlessSpool is disabled")
            return

        if reset:
            mmu._reset_endless_spool()

        elif groups != "!":
            groups = gcmd.get('GROUPS', ",".join(map(str, mmu.endless_spool_groups))).split(",")
            if len(groups) != mmu.num_gates:
                mmu.log_always("The number of group values (%d) is not the same as number of gates (%d)" % (len(groups), mmu.num_gates))
                return
            mmu.endless_spool_groups = []
            for group in groups:
                if group.isdigit():
                    mmu.endless_spool_groups.append(int(group))
                else:
                    mmu.endless_spool_groups.append(0)
            mmu._persist_endless_spool()

        else:
            quiet = False  # Display current map

        if not quiet:
            mmu.log_info(mmu._es_groups_to_string())
