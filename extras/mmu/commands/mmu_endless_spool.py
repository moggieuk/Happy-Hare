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
        f"{CMD}: {HELP_BRIEF}\n"
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

        if self.check_if_disabled(): return

        enabled = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', None)

        def show():
            if mmu.gate_maps.endless_spool_enabled:
                msg = f"EndlessSpool is enabled\n"
                msg += mmu.gate_maps.es_groups_to_string()
            else:
                msg = f"EndlessSpool is disabled\n"
            if not quiet:
                mmu.log_info(msg)

        if reset:
            mmu.gate_maps.reset_endless_spool()
            show()
            return

        if enabled is not None:
            if mmu.gate_maps.endless_spool_enabled != enabled:
                mmu.gate_maps.endless_spool_enabled = enabled
                mmu.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, enabled, write=True)

        if groups is not None:
            raw_groups = [group.strip() for group in groups.split(",")]

            if len(raw_groups) != mmu.num_gates:
                mmu.log_error(
                    f"The number of group values ({len(raw_groups)}) does not match "
                    f"the number of gates ({mmu.num_gates})"
                )
                return

            try:
                parsed_groups = [int(group) for group in raw_groups]
            except ValueError:
                mmu.log_error(
                    f"Invalid GROUPS value: {groups!r}. "
                    "Expected comma-separated integers."
                )
                return

            if any(group < 0 for group in parsed_groups):
                mmu.log_error(
                    f"Invalid GROUPS value: {groups!r}. "
                    "Group values must be non-negative integers."
                )
                return

            mmu.gate_maps.endless_spool_groups = parsed_groups
            mmu.gate_maps.persist_endless_spool()

        if not quiet:
            show()
