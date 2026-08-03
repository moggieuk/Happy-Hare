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
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET     = [0|1]\n"
        + "SYNC      = [0|1]\n"
        + "CLEAR     = [0|1]\n"
        + "REFRESH   = [0|1]\n"
        + "FIX       = [0|1]\n"
        + "SPOOLID   = #(int)\n"
        + "GATE      = #(int)\n"
        + "RFID      = # Write this NFC/RFID tag UID onto the spool record (needs SPOOLID or GATE)\n"
        + "PRINTER   = _name_\n"
        + "SPOOLINFO = [0|-1|spool_id]\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                     ...Show the current spoolman gate/spool assignments\n"
        + f"{CMD} REFRESH=1           ...Refresh the local gate map from the spoolman database\n"
        + f"{CMD} GATE=0 SPOOLID=45   ...Assign spoolman spool id 45 to gate 0\n"
        + f"{CMD} SPOOLINFO=45        ...Display spoolman details for spool id 45\n"
        + f"{CMD} SPOOLID=45 RFID=E2003412  ...Register tag E2003412 against spool id 45 in the spoolman db\n"
        + f"{CMD} GATE=0 RFID=E2003412      ...Same, for whichever spool is assigned to gate 0\n"
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
        if self.check_if_spoolman_enabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        clear = bool(gcmd.get_int('CLEAR', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))
        fix = bool(gcmd.get_int('FIX', 0, minval=0, maxval=1))
        spool_id = gcmd.get_int('SPOOLID', None, minval=1)
        gate = gcmd.get_int('GATE', None, minval=-1, maxval=mmu.num_gates - 1)
        printer = gcmd.get('PRINTER', None)  # Option to see other printers
        spoolinfo = gcmd.get_int('SPOOLINFO', None, minval=-1)  # -1 or 0 is active spool
        rfid = gcmd.get('RFID', None)        # Tag UID to write onto a spool record
        run = False

        if refresh:
            # Rebuild cache in moonraker and sync local and remote
            mmu._spoolman_refresh(fix, quiet=quiet)
            if not sync:
                mmu._spoolman_sync(quiet=quiet)
            run = True

        if clear:
            # Clear the gate allocation in spoolman db
            mmu._spoolman_clear_gate_map(
                sync=mmu.p.spoolman_support == SPOOLMAN_PULL,
                quiet=quiet
            )
            run = True

        if sync:
            # Sync local and remote gate maps
            mmu._spoolman_sync(quiet=quiet)
            run = True

        # Rest of the options are mutually exclusive
        if rfid is not None:
            # Write an NFC/RFID tag UID onto an EXISTING spool record in the spoolman db.
            #
            # Note the direction: this is the opposite of 'MMU_NFC ... REGISTER=1', which
            # takes a UID and finds (or auto-creates) a spool for it. Here the spool
            # already exists and the tag is bound onto it - the case auto-create cannot
            # serve, e.g. sticking a blank tag on a spool spoolman already knows about.
            #
            # Dispatched before the SPOOLID/GATE branch below, where a bare SPOOLID means
            # "unset that spool's gate" - which would otherwise swallow SPOOLID=n RFID=x.
            if not rfid.strip():
                mmu.log_error("RFID= needs a tag UID. To clear a gate's locally recorded "
                              "tag use 'MMU_GATE_MAP GATE=%s RFID='" % (gate if gate is not None else 'n'))
                return
            target = spool_id
            if target is None:
                if gate is None or gate < 0:
                    mmu.log_error("RFID= needs SPOOLID=<id>, or GATE=<gate> to use the spool "
                                  "already assigned to that gate")
                    return
                target = mmu.gate_spool_id[gate]
                if target is None or target <= 0:
                    mmu.log_error("Gate %d has no spoolman spool assigned - use SPOOLID= to "
                                  "name the spool explicitly" % gate)
                    return
                mmu.log_debug("Gate %d resolves to spool id %d for tag registration" % (gate, target))
            mmu._spoolman_set_spool_uid(target, rfid.strip(), quiet=quiet)

        elif spoolinfo is not None:
            # Dump spool info for active spool or specified spool id
            mmu._spoolman_display_spool_info(
                spoolinfo if spoolinfo > 0 else None
            )

        elif spool_id is not None or gate is not None:
            # Update a record in spoolman db
            if spool_id is not None and gate is not None:
                mmu._spoolman_set_spool_gate(
                    spool_id,
                    gate,
                    sync=mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )
            elif spool_id is None and gate is not None:
                mmu._spoolman_unset_spool_gate(
                    gate=gate,
                    sync=mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )
            elif spool_id is not None and gate is None:
                mmu._spoolman_unset_spool_gate(
                    spool_id=spool_id,
                    sync=mmu.p.spoolman_support == SPOOLMAN_PULL,
                    quiet=quiet
                )

        elif not run:
            if mmu.p.spoolman_support in [SPOOLMAN_PULL, SPOOLMAN_PUSH]:
                # Display gate association table from spoolman db
                mmu._spoolman_display_spool_location(printer=printer)
            else:
                mmu.log_error(
                    "Spoolman gate map not available. Spoolman mode is: %s"
                    % mmu.p.spoolman_support
                )
