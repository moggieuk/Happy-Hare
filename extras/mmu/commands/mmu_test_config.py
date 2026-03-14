# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_CONFIG command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuTestConfigCommand(BaseCommand):

    CMD = "MMU_TEST_CONFIG"

    HELP_BRIEF = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT         = #(int)|_name_|ALL Specify unit by name, number or all-units (optional if single unit)\n"
        + "ALL   = [0|1]  Report all parameters even if not in user configfile (i.e system default values)\n"
        + "QUIET = [0|1]  Suppress non essential console messages\n"
        + "(no parameters to dump of current settings)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s extruder_homing_max=150             ...set the extruder_homing_max parameter to 150\n" % CMD
        + "%s toolhead_ooze_reduction=2.5 QUIET=1 ...silently set toolhead_ooze_reduction\n" % CMD
        + "%s UNIT=1 sync_to_extruder=1           ...turn on extruder syncing for mmu unit 1\n" % CMD
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        raw_params = gcmd.get_command_parameters()
        raw_keys_lc = {k.lower() for k in raw_params.keys()}

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        show_all = bool(gcmd.get_int('ALL', 0, minval=0, maxval=1))
        machine_params  = self.mmu.p          # MmuMachineParameters
        unit_params     = mmu_unit.p          # MmuUnitParameters
        selector_params = mmu_unit.selector.p # *Selector*Parameters

        # Apply to both sets (non-strict so we can aggregate unknown + guarded across both)
        try:
            m_applied, m_guarded, m_unknown = machine_params.apply_gcmd(gcmd, strict=False)
            u_applied, u_guarded, u_unknown = unit_params.apply_gcmd(gcmd, strict=False)
            s_applied, s_guarded, s_unknown = selector_params.apply_gcmd(gcmd, strict=False)
        except Exception as e:
            raise gcmd.error(str(e))

        applied = set(m_applied) | set(u_applied) | set(s_applied)
        guarded = set(m_guarded) | set(u_guarded) | set(s_guarded)
        unknown = set(m_unknown) | set(u_unknown) | set(s_unknown)

        known = (
            set(machine_params.get_known_param_names())
            | set(unit_params.get_known_param_names())
            | set(selector_params.get_known_param_names())
        )

        # There shouldn't be overlap in parameter names but be sure
        unknown -= known
        guarded &= known

        applied = sorted(applied)
        guarded = sorted(guarded)
        unknown = sorted(unknown)

        # Fail if user attempted anything invalid
        if unknown:
            raise gcmd.error("Unknown parameter(s): %s" % ", ".join(unknown))
        if guarded:
            raise gcmd.error("Parameter(s) not available for runtime change: %s" % ", ".join(guarded))
        # Report what changed
        if applied and not quiet:
            self.mmu.log_info("Applied parameters: %s" % ", ".join(applied))
            self.mmu.log_info("(remember these are temporary changes until next restart)")

        # Nothing applied so list current params unless QUIET=1
        if not applied and not quiet:
            msg = []
            msg.append("Shared MMU machine parameters ----------------")
            msg.append(machine_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))

            msg.append(f"\nMMU %s parameters ----------------" % mmu_unit.name)
            msg.append(unit_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))

            if selector_params.get_known_param_names():
                msg.append(f"\nMMU %s selector parameters ----------------" % mmu_unit.name)
                msg.append(selector_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))

            self.mmu.log_info("\n".join(msg))
