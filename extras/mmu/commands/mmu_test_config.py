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
        + "UNIT  = #(int) Optional if only one unit fitted to printer\n"
        + "QUIET = [0|1]  Suppress non essential console messages\n"
        + "(no parameters to dump of current settings)"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s extruder_homing_max=150             ...set the extruder_homing_max parameter to 150\n" % CMD
        + "%s toolhead_ooze_reduction=2.5 QUIET=1 ...silently set toolhead_ooze_reduction\n" % CMD
        + "%s UNIT=1 sync_to_extruder=1           ...turn on extruder syncing for mmu unit 1" % CMD
    )

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
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        raw_params = gcmd.get_command_parameters()
        raw_keys_lc = {k.lower() for k in raw_params.keys()}

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        unit_index = gcmd.get_int('UNIT', self.mmu.unit_selected, minval=0, maxval=self.mmu.mmu_machine.num_units)

        unit = self.mmu.mmu_unit(unit_index)
        machine_params  = self.mmu.p # MmuMachineParameters
        unit_params     = unit.p     # MmuUnitParameters
        # PAUL TODO selector_params = unit.p   # MmuSelectorParameters

        # Apply to both sets (non-strict so we can aggregate unknown + guarded across both)
        try:
            m_applied, m_guarded, m_unknown = machine_params.apply_gcmd(gcmd, strict=False)
            u_applied, u_guarded, u_unknown = unit_params.apply_gcmd(gcmd, strict=False)
# PAUL selector params
        except Exception as e:
            raise gcmd.error(str(e))

        applied    = sorted(set(m_applied + u_applied))
        guarded    = sorted(set(m_guarded + u_guarded))
        unknown    = sorted(set(m_unknown + u_unknown))

        # Determine unknown params
        known      = set(machine_params.get_known_param_names()) | set(unit_params.get_known_param_names())
        unknown    = [n for n in unknown if n not in known]

        # Determine set of legal params but that aren't available in current setup
        guarded    = [n for n in guarded if n in known]

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
            msg.append(machine_params.format_params(include_hidden=False, include_guarded_out=False))

            msg.append("")
            msg.append(f"\nMMU %s parameters ----------------" % unit.name)
            msg.append(unit_params.format_params(include_hidden=False, include_guarded_out=False))

            self.mmu.log_info("\n".join(msg))
