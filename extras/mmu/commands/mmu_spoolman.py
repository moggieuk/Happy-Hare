# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SPOOLMAN command
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


class MmuSpoolmanCommand(BaseCommand):
    """
    Manage spoolman integration.
    """

    CMD = "MMU_SPOOLMAN"

    HELP_BRIEF = "Manage spoolman integration"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "QUIET     = [0|1]\n"
        + "SYNC      = [0|1]\n"
        + "CLEAR     = [0|1]\n"
        + "REFRESH   = [0|1]\n"
        + "FIX       = [0|1]\n"
        + "SPOOLID   = #(int)\n"
        + "GATE      = #(int)\n"
        + "PRINTER   = _name_\n"
        + "SPOOLINFO = [0|-1|spool_id]\n"
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
        # BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_spoolman_enabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        clear = bool(gcmd.get_int('CLEAR', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))
        fix = bool(gcmd.get_int('FIX', 0, minval=0, maxval=1))
        spool_id = gcmd.get_int('SPOOLID', None, minval=1)
        gate = gcmd.get_int('GATE', None, minval=-1, maxval=self.mmu.num_gates - 1)
        printer = gcmd.get('PRINTER', None)  # Option to see other printers
        spoolinfo = gcmd.get_int('SPOOLINFO', None, minval=-1)  # -1 or 0 is active spool
        run = False

        if refresh:
            # Rebuild cache in moonraker and sync local and remote
            self.mmu._spoolman_refresh(fix, quiet=quiet)
            if not sync:
                self.mmu._spoolman_sync(quiet=quiet)
            run = True

        if clear:
            # Clear the gate allocation in spoolman db
            self.mmu._spoolman_clear_gate_map(
                sync=self.mmu.p.spoolman_support == SPOOLMAN_PULL,
                quiet=quiet
            )
            run = True

        if sync:
            # Sync local and remote gate maps
            self.mmu._spoolman_sync(quiet=quiet)
            run = True

        # Rest of the options are mutually exclusive
        if spoolinfo is not None:
            # Dump spool info for active spool or specified spool id
            self.mmu._spoolman_display_spool_info(
                spoolinfo if spoolinfo > 0 else None
            )

        elif spool_id is not None or gate is not None:
            # Update a record in spoolman db
            if spool_id is not None and gate is not None:
                self.mmu._spoolman_set_spool_gate(
                    spool_id,
                    gate,
                    sync=self.mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )
            elif spool_id is None and gate is not None:
                self.mmu._spoolman_unset_spool_gate(
                    gate=gate,
                    sync=self.mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )
            elif spool_id is not None and gate is None:
                self.mmu._spoolman_unset_spool_gate(
                    spool_id=spool_id,
                    sync=self.mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )

        elif not run:
            if self.mmu.p.spoolman_support in [SPOOLMAN_PULL, SPOOLMAN_PUSH]:
                # Display gate association table from spoolman db
                self.mmu._spoolman_display_spool_location(printer=printer)
            else:
                self.mmu.log_error(
                    "Spoolman gate map not available. Spoolman mode is: %s"
                    % self.mmu.p.spoolman_support
                )
