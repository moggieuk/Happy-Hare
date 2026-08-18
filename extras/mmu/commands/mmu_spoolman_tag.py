# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SPOOLMAN_TAG command
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
from .mmu_base_command import *


class MmuSpoolmanTagCommand(BaseCommand):
    """
    Register an NFC/RFID tag UID onto an existing spoolman spool record, either by
    supplying the UID directly (RFID=) or by binding a gate's already-recorded UID
    (REGISTER=1). Split out of MMU_SPOOLMAN so that command's SPOOLID/GATE keep a
    single meaning (gate-spool assignment).
    """

    CMD = "MMU_SPOOLMAN_TAG"

    HELP_BRIEF = "Register an NFC/RFID tag UID onto a spoolman spool record"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "QUIET    = [0|1] Suppress non-critical console output\n"
        + "SPOOLID  = #(int) Spoolman spool id to register the tag against\n"
        + "GATE     = #(int)|LAST Gate whose assigned spool (RFID=) or recorded tag (REGISTER=) to use. If omitted implies current gate\n"
        + "RFID     = _uid_ (or comma-separated UIDs) to write onto the spool. RFID='' to clear\n"
        + "APPEND   = 1 Add to the existing UID(s) instead of replacing them\n"
        + "REGISTER = 1 Bind the gate's already-recorded UID onto SPOOLID (needs spoolman_support != pull)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} SPOOLID=45 RFID=E2003412          ...Register tag E2003412 against spool id 45 in the spoolman db (replaces any existing tags)\n"
        + f"{CMD} SPOOLID=45 RFID=E2003499 APPEND=1 ...Register a second tag on the same spool (e.g. one on each side), keeping E2003412\n"
        + f"{CMD} SPOOLID=45 RFID=''                ...Clear all tags registered against spool id 45\n"
        + f"{CMD} GATE=0 RFID=E2003412              ...Same, for whichever spool is assigned to gate 0\n"
        + f"{CMD} GATE=3 SPOOLID=87 REGISTER=1      ...Bind gate 3's already-known tag uid to newly-created spool 87\n"
        + f"{CMD} GATE=LAST SPOOLID=87 REGISTER=1   ...Bind last gate preloaded already-known tag uid to spool 87\n"
        + f"{CMD} SPOOLID=87 REGISTER=1             ...Bind currently selected gate's tag uid to spool 87\n"
        + "\nSee MMU_SPOOLMAN read or change gate-spool assignment in spoolman\n"
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
        spool_id = gcmd.get_int('SPOOLID', None, minval=1)

        # GATE= is usually a plain int, but LAST resolves to the gate MMU_PRELOAD most
        # recently completed - checked before the generic int parse since 'LAST' isn't one.
        gate_str = gcmd.get('GATE', None)
        if gate_str is not None and gate_str.strip().upper() == 'LAST':
            gate = mmu.last_preloaded_gate
            if gate < 0:
                mmu.log_error("GATE=LAST needs a gate to have been preloaded first")
                return
        elif gate_str is not None:
            try:
                gate = int(gate_str)
            except ValueError:
                raise gcmd.error("Invalid GATE parameter: %s" % gate_str)
            if not (-1 <= gate < mmu.num_gates):
                raise gcmd.error("GATE must be between -1 and %d" % (mmu.num_gates - 1))
        else:
            gate = None

        rfid = gcmd.get('RFID', None)        # Tag UID(s) to write onto a spool record
        append = bool(gcmd.get_int('APPEND', 0, minval=0, maxval=1))
        register = bool(gcmd.get_int('REGISTER', 0, minval=0, maxval=1))

        # The two ways to supply/derive a UID are mutually exclusive
        if register:
            # Bind a gate's already-recorded NFC/RFID uid onto a spool_id created after the
            # fact - unlike RFID= (below), no uid is supplied on the command line. GATE=
            # defaults to the currently selected gate (register's own default only - the
            # RFID= branch below keeps treating an omitted GATE= as "not given").
            if gate is None:
                gate = mmu.gate_selected
            if spool_id is None or gate is None or gate < 0:
                mmu.log_error("REGISTER=1 needs SPOOLID=<id> and a gate - specify GATE=<gate>, "
                              "GATE=LAST, or select a gate first")
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
            # No local assignment here - Moonraker confirms the write by calling back
            # 'MMU_GATE_MAP GATE=<gate> SPOOLID=<spool_id>', so the gate map only updates
            # once the uid has actually been registered, not optimistically.
            mmu._spoolman_set_spool_uid(spool_id, uid, append=append, quiet=quiet, gate=gate)

        elif rfid is not None:
            # Write an NFC/RFID tag UID onto an EXISTING spool record in the spoolman db.
            #
            # Note the direction: this is the opposite of 'MMU_NFC ... REGISTER=1', which
            # takes a UID and finds (or auto-creates) a spool for it. Here the spool
            # already exists and the tag is bound onto it - the case auto-create cannot
            # serve, e.g. sticking a blank tag on a spool spoolman already knows about.
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
            mmu._spoolman_set_spool_uid(target, rfid.strip(), append=append, quiet=quiet,
                                        gate=gate if gate is not None and gate >= 0 else None)

        else:
            mmu.log_error("%s needs RFID=<uid> or REGISTER=1" % self.CMD)
