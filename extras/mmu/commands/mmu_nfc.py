# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Command class to control and inspect the MMU NFC/RFID readers
#
# Implements commands:
#   MMU_NFC
#
# The readers themselves are owned by each mmu_unit's MmuNfcManager. This command
# resolves the correct unit (implied from GATE, or the sole/only-shared unit, or
# an explicit UNIT) and then talks to that unit's nfc_manager.
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


class MmuNfcCommand(BaseCommand):

    CMD = "MMU_NFC"

    HELP_BRIEF = "Control and inspect the MMU NFC/RFID readers"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "SHARED   = [0|1] Target the unit's shared reader\n"
        + "GATE     = #(int) Target the reader for this gate (implies the unit)\n"
        + "GATES    = g,g,g Target multiple gates' readers (don't mix with GATE/SHARED)\n"
        + "UNIT     = #(int)/name Only needed to disambiguate multiple units with shared readers\n"
        + "ENABLE   = [0|1] Top-level on/off for the reader (re-inits when enabled)\n"
        + "READ     = [0|1] Read the addressed reader once and report the UID\n"
        + "DEEP     = [0|1] With READ=1, also parse and report the tag metadata (ignores nfc_deep_read setting)\n"
        + "REGISTER = [0|1] Read tag (implies READ=1 DEEP=1) and resolve it in Spoolman (may auto-create). Shared reader: report-only, Per-gate: updates gate map\n"
        + "INIT     = [0|1] (Re)initialize the addressed reader\n"
        + "RELEASE  = [0|1] Release the current target on the addressed reader\n"
        + "INIT_ALL = [0|1] (Re)initialize every reader on every unit\n"
        + "DETAILS  = [0|1] Include actual cached tag UIDs in the status report\n"
        + "(no parameters for status report of all readers)"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                        ...Report status of all readers (which have a cached tag)\n"
        + f"{CMD} DETAILS=1              ...As above but show the actual cached UIDs\n"
        + f"{CMD} SHARED=1 ENABLE=0      ...Disable the shared reader\n"
        + f"{CMD} GATE=3 READ=1          ...Read the reader on gate 3 and report the result\n"
        + f"{CMD} SHARED=1 READ=1 DEEP=1 ...Read the shared reader and report the parsed tag metadata\n"
        + f"{CMD} SHARED=1 REGISTER=1    ...Read tag and resolve/register it in Spoolman (report only, no assignment)\n"
        + f"{CMD} GATE=2 REGISTER=1      ...Read tag on gate 2 and apply to the gate map (as if auto-scanned)\n"
        + f"{CMD} GATE=2 INIT=1          ...(Re)initialize the reader on gate 2\n"
        + f"{CMD} GATES=0,1,2,3 ENABLE=0 ...Disable selected per-gate readers\n"
        + f"{CMD} INIT_ALL=1             ...Re-initialize every reader on all units\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        if self.check_if_disabled(): return
        mmu = self.mmu

        details  = gcmd.get_int('DETAILS', 0, minval=0, maxval=1)
        init_all = gcmd.get_int('INIT_ALL', 0, minval=0, maxval=1)
        shared   = bool(gcmd.get_int('SHARED', 0, minval=0, maxval=1))
        gate     = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
        gates    = gcmd.get('GATES', None)
        enable   = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        read     = gcmd.get_int('READ', 0, minval=0, maxval=1)
        deep     = bool(gcmd.get_int('DEEP', 0, minval=0, maxval=1))
        register = bool(gcmd.get_int('REGISTER', 0, minval=0, maxval=1))
        init     = gcmd.get_int('INIT', 0, minval=0, maxval=1)
        release  = gcmd.get_int('RELEASE', 0, minval=0, maxval=1)
        if register: # Registration needs the tag read and its metadata (for auto-create)
            read = 1
            deep = True

        units = mmu.mmu_machine.units

        # INIT_ALL: reset everything quickly (does not touch enable/active flags)
        if init_all:
            for unit in units:
                unit.nfc_manager.init_all()
            mmu.log_always("NFC: re-initialized all readers on all units")
            return

        # No reader addressed -> status report across all units
        if not shared and gate is None and gates is None:
            self._report_all(units, details)
            return

        if sum([shared, gate is not None, gates is not None]) > 1:
            raise gcmd.error("Specify only one of SHARED=1, GATE=<n> or GATES=<n,n,...>")

        if gates is not None:
            # Multi-gate form: apply the requested action(s) to each listed gate's reader
            # (each gate resolves its own owning unit)
            try:
                gatelist = [int(g) for g in gates.split(',')]
            except ValueError:
                raise gcmd.error("Invalid GATES parameter: %s" % gates)
            bad = [g for g in gatelist if not (0 <= g < mmu.num_gates)]
            if bad:
                raise gcmd.error("Invalid gate(s) in GATES: %s" % ",".join(map(str, bad)))
            skipped = []
            for g in gatelist:
                mmu_unit = mmu.mmu_unit(g)
                mgr = mmu_unit.nfc_manager
                if mgr is None or not mgr.has_reader(gate=g):
                    skipped.append(g)
                    continue
                if not self._do_actions(mmu, mmu_unit, mgr, False, g, enable, init, release, read, deep, register):
                    self._report_one(mmu_unit, mgr, shared=False, gate=g, details=details)
            if skipped:
                mmu.log_always("NFC: no reader on gate(s) %s - skipped" % ",".join(map(str, skipped)))
            return

        # Resolve the unit and its nfc_manager for the addressed reader
        mmu_unit = self._unit_for_gate(gcmd, gate) if gate is not None else self._unit_for_shared(gcmd)
        mgr = mmu_unit.nfc_manager
        label = "shared reader" if shared else ("gate %d" % gate)

        if not mgr.has_reader(shared=shared, gate=gate):
            raise gcmd.error("%s: no NFC %s configured" % (mmu_unit.name, label))

        # A bare selector (e.g. MMU_NFC GATE=3) just reports that reader's status
        if not self._do_actions(mmu, mmu_unit, mgr, shared, gate, enable, init, release, read, deep, register):
            self._report_one(mmu_unit, mgr, shared=shared, gate=gate, details=details)

    def _do_actions(self, mmu, mmu_unit, mgr, shared, gate, enable, init, release, read, deep=False, register=False):
        """
        Apply the requested action(s) to one addressed reader. Returns True if any
        action was performed (False -> caller falls back to a status report).
        """
        label = "shared reader" if shared else ("gate %d" % gate)
        did_action = False

        if enable is not None:
            mgr.set_enabled(enable, shared=shared, gate=gate)
            mmu.log_always("NFC: %s %s %s" % (mmu_unit.name, label, "enabled" if enable else "disabled"))
            did_action = True

        if init:
            alive = mgr.init_reader(shared=shared, gate=gate)
            mmu.log_always("NFC: %s %s init %s" % (mmu_unit.name, label, "OK" if alive else "did not respond"))
            did_action = True

        if release:
            mgr.release_reader(shared=shared, gate=gate)
            mmu.log_always("NFC: %s %s released" % (mmu_unit.name, label))
            did_action = True

        if read:
            # 'enabled' is a hard off - refuse the read; 'active' is only a guard on
            # automatic reads, so a manual READ deliberately overrides it.
            if not mgr.is_enabled(shared=shared, gate=gate):
                mmu.log_always("NFC: %s %s is disabled - use ENABLE=1 first" % (mmu_unit.name, label))
            else:
                uid, metadata = mgr.read_reader(shared=shared, gate=gate, deep=deep)
                if uid:
                    msg = "NFC: %s %s read UID=%s" % (mmu_unit.name, label, uid)
                    if deep:
                        if metadata:
                            msg += "\nParsed tag metadata:"
                            for k, v in metadata.items():
                                msg += "\n  %s: %s" % (k, v)
                        else:
                            msg += "\n(no parseable tag metadata - blank or unsupported tag format?)"
                    mmu.log_always(msg)
                    if register:
                        if shared:
                            # Report-only Spoolman resolve/auto-create: no pending, no gate map
                            mmu._spoolman_register_tag(uid, metadata)
                        else:
                            # Per-gate: full normal tag-read semantics - gate map updates
                            # (spool_id on async resolution; metadata per nfc_deep_read)
                            mmu.log_always("NFC: dispatching tag for gate %d - gate map will update on resolution" % gate)
                            mmu._nfc_tag_read(uid, gate=gate, metadata=metadata, unit=mmu_unit)
                else:
                    mmu.log_always("NFC: %s %s - no tag detected" % (mmu_unit.name, label))
            did_action = True

        return did_action

    #
    # Unit resolution -----------------------------------------------------------
    #

    def _unit_for_gate(self, gcmd, gate):
        # Explicit UNIT (or the sole unit) wins; otherwise derive from the gate.
        unit = self.get_unit(gcmd, mode="optional")
        return unit if unit is not None else self.mmu.mmu_unit(gate)

    def _unit_for_shared(self, gcmd):
        # Explicit UNIT (or the sole unit) wins; otherwise, with multiple units,
        # auto-pick the only one that actually has a shared reader.
        unit = self.get_unit(gcmd, mode="optional")
        if unit is not None:
            return unit
        candidates = [u for u in self.mmu.mmu_machine.units if u.nfc_manager.has_reader(shared=True)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise gcmd.error("No shared NFC reader configured on any unit")
        raise gcmd.error("UNIT parameter required: more than one unit has a shared NFC reader")

    #
    # Status reporting ----------------------------------------------------------
    #

    def _reader_line(self, label, rs, details):
        if rs is None:
            return None
        if rs.get('uid'):
            tag = rs['uid'] if details else "present"
        else:
            tag = "none"
        return "%-9s enabled=%d active=%d alive=%d tag=%s" % (
            label + ":", int(rs['enabled']), int(rs['active']), int(rs['alive']), tag)

    def _report_one(self, mmu_unit, mgr, shared, gate, details):
        status = mgr.get_status()
        if shared:
            rs, label = status['shared'], "shared"
        else:
            rs, label = status['gates'].get(gate), "gate %d" % gate
        self.mmu.log_always("NFC: %s %s" % (mmu_unit.name, self._reader_line(label, rs, details)))

    def _report_all(self, units, details):
        multi = len(units) > 1
        lines = []
        for unit in units:
            status = unit.nfc_manager.get_status()
            unit_lines = []
            shared_line = self._reader_line("shared", status['shared'], details)
            if shared_line:
                unit_lines.append(shared_line)
            for g in sorted(status['gates']):
                unit_lines.append(self._reader_line("gate %d" % g, status['gates'][g], details))
            if unit_lines:
                if multi:
                    lines.append("Unit %s:" % status['unit'])
                lines.extend(unit_lines)

        if not lines:
            self.mmu.log_always("No NFC readers configured")
        else:
            self.mmu.log_always("MMU NFC readers:\n" + "\n".join(lines))
