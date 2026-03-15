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
        + "UNIT  = #(int)|_name_ Specify unit by name, number (optional if single unit or changing shared parameters))\n"
        + "ALL   = [0|1]  Report all parameters even if not in user configfile (i.e system default values)\n"
        + "QUIET = [0|1]  Suppress non essential console messages\n"
        + "(no parameters to dump of current settings)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} extruder_homing_max=150             ...set the extruder_homing_max parameter to 150\n"
        + f"{CMD} toolhead_ooze_reduction=2.5 QUIET=1 ...silently set toolhead_ooze_reduction\n"
        + f"{CMD} UNIT=1 sync_to_extruder=1           ...turn on extruder syncing for mmu unit 1\n"
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
        mmu = self.mmu

        mmu_unit = self.get_unit(gcmd) # None if not specified by user
        unit_param = gcmd.get('UNIT', None)
        if mmu_unit is None and unit_param is not None:
            raise gcmd.error(f"Unit {unit_param} not found!")

        raw_params = gcmd.get_command_parameters()
        raw_keys_lc = {k.lower() for k in raw_params.keys()}

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        show_all = bool(gcmd.get_int('ALL', 0, minval=0, maxval=1))

        machine_params  = mmu.p                   # MmuMachineParameters
        param_sets = [machine_params]
        if mmu_unit is not None:
            unit_params     = mmu_unit.p          # MmuUnitParameters
            selector_params = mmu_unit.selector.p # *Selector*Parameters
            param_sets.extend([unit_params, selector_params])

        applied = set()
        guarded = set()
        unknown = set()
        known = set()

        try:
            for params in param_sets:
                p_applied, p_guarded, p_unknown = params.apply_gcmd(gcmd, strict=False)
                applied.update(p_applied)
                guarded.update(p_guarded)
                unknown.update(p_unknown)
                known.update(params.get_known_param_names())

        except Exception as e:
            raise gcmd.error(str(e))

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
            mmu.log_info("Applied parameters: %s" % ", ".join(applied))
            mmu.log_info("(remember these are temporary changes until next restart)")

        # Nothing applied so list current params unless QUIET=1
        if not applied and not quiet:
            msg = []
            msg.append("Shared MMU machine parameters ----------------")
            msg.append(machine_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))

            if mmu_unit:
                msg.append(f"\nMMU %s parameters ----------------" % mmu_unit.name)
                msg.append(unit_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))

                if selector_params.get_known_param_names():
                    msg.append(f"\nMMU %s selector parameters ----------------" % mmu_unit.name)
                    msg.append(selector_params.format_params(include_hidden=False, include_guarded_out=show_all, include_not_in_configfile=show_all))
            else:
                msg.append(f"\nNo MMU unit parameters because UNIT wasn't specified")

            mmu.log_info("\n".join(msg))
