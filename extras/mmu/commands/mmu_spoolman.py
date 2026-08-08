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
        + "QUIET     = [0|1] Suppress non-critical console output\n"
        + "SYNC      = [0|1] Sync the local and remote (spoolman) gate maps\n"
        + "CLEAR     = [0|1] Clear all gate/spool assignments for this printer in the spoolman db\n"
        + "REFRESH   = [0|1] Rebuild spoolman's cache of this printer's assignments, then sync (unless SYNC= is also given)\n"
        + "FIX       = [0|1] With REFRESH=, also unassign any inconsistent spool/gate pairs found (partial or duplicate assignments)\n"
        + "SPOOLID   = #(int) Spoolman spool id. With GATE=, assign it to that gate; alone, unset\n"
        + "              its gate. Also the target spool for RFID=/REGISTER=/SPOOLINFO=\n"
        + "GATE      = #(int) Gate number. With SPOOLID=, assign that spool to it; alone, unset\n"
        + "              its spool. Also resolves RFID='s target spool, or REGISTER='s source gate\n"
        + "RFID      = # Write this NFC/RFID tag UID (or comma-separated UIDs) onto the spool record\n"
        + "              (needs SPOOLID or GATE). Replaces any existing UID(s); RFID='' clears them.\n"
        + "APPEND    = [0|1] With RFID=, add to the existing UID(s) instead of replacing them; with\n"
        + "              REGISTER=, add to the spool's existing UID(s) instead of replacing them\n"
        + "REGISTER  = [0|1] With GATE= and SPOOLID=, write GATE's already-recorded NFC/RFID uid\n"
        + "              onto SPOOLID in spoolman and assign it locally (no new scan - unlike\n"
        + "              'MMU_NFC ... REGISTER=1', which reads a fresh tag). Needs spoolman_support != pull.\n"
        + "PRINTER   = _name_ Show another printer's gate/spool assignments instead of this one\n"
        + "SPOOLINFO = [0|-1|spool_id] Display spoolman details for a spool (0 or -1 = the active spool)\n"
        + "(no parameters to shoe the current spoolman gate/spool assignments)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                              ...Show the current spoolman gate/spool assignments\n"
        + f"{CMD} REFRESH=1                    ...Refresh the local gate map from the spoolman database\n"
        + f"{CMD} GATE=0 SPOOLID=45            ...Assign spoolman spool id 45 to gate 0\n"
        + f"{CMD} SPOOLINFO=45                 ...Display spoolman details for spool id 45\n"
        + f"{CMD} SPOOLID=45 RFID=E2003412     ...Register tag E2003412 against spool id 45 in the spoolman db (replaces any existing tags)\n"
        + f"{CMD} SPOOLID=45 RFID=E2003499 APPEND=1 ...Register a second tag on the same spool (e.g. one on each side), keeping E2003412\n"
        + f"{CMD} SPOOLID=45 RFID=''           ...Clear all tags registered against spool id 45\n"
        + f"{CMD} GATE=0 RFID=E2003412         ...Same, for whichever spool is assigned to gate 0\n"
        + f"{CMD} GATE=3 SPOOLID=87 REGISTER=1 ...Bind gate 3's already-known tag uid onto newly-created spool 87\n"
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
        rfid = gcmd.get('RFID', None)        # Tag UID(s) to write onto a spool record
        append = bool(gcmd.get_int('APPEND', 0, minval=0, maxval=1))
        register = bool(gcmd.get_int('REGISTER', 0, minval=0, maxval=1))
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
        if register:
            # Bind a gate's already-recorded NFC/RFID uid onto a spool_id created after the
            # fact - unlike RFID= (below), no uid is supplied on the command line.
            if spool_id is None or gate is None or gate < 0:
                mmu.log_error("REGISTER=1 needs both GATE=<gate> (0..%d) and SPOOLID=<id>" % (mmu.num_gates - 1))
                return
            if mmu.p.spoolman_support == SPOOLMAN_PULL:
                mmu.log_error("REGISTER=1 is not applicable with spoolman_support=pull - use RFID=<uid> "
                              "to write a UID onto a spool directly, or re-scan the tag after creating it")
                return
            uid = mmu.gate_spool_rfid[gate]
            if not uid:
                mmu.log_error("Gate %d has no NFC/RFID tag UID recorded yet - scan one first, or "
                              "use RFID=<uid> to supply it explicitly" % gate)
                return
            mmu._spoolman_set_spool_uid(spool_id, uid, append=append, quiet=quiet)
            mod_gate_ids = mmu.gate_maps.assign_spool_id(gate, spool_id)
            mmu.gate_maps.persist_gate_map(spoolman_sync=True, gate_ids=mod_gate_ids)

        elif rfid is not None:
            # Write an NFC/RFID tag UID onto an EXISTING spool record in the spoolman db.
            #
            # Note the direction: this is the opposite of 'MMU_NFC ... REGISTER=1', which
            # takes a UID and finds (or auto-creates) a spool for it. Here the spool
            # already exists and the tag is bound onto it - the case auto-create cannot
            # serve, e.g. sticking a blank tag on a spool spoolman already knows about.
            #
            # Dispatched before the SPOOLID/GATE branch below, where a bare SPOOLID means
            # "unset that spool's gate" - which would otherwise swallow SPOOLID=n RFID=x.
            if append and not rfid.strip():
                mmu.log_error("APPEND=1 needs a tag UID to add. To clear all tags "
                              "registered against a spool, use RFID='' without APPEND=1.")
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
            mmu._spoolman_set_spool_uid(target, rfid.strip(), append=append, quiet=quiet)

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
