# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: NFC "noisy neighbor" reader-field arbitration
#
# A neighboring gate's tag can trigger gate G's own reader. MmuNfcFieldArbiter settles
# ownership before trusting it: registered to G -> mine; registered to a same-unit
# neighbor -> evict by jogging it aside; different unit -> impossible, map stale, ignore;
# unrecognised and un-evictable -> PROVISIONAL, confirmed later by the caller's own motion.
#
# Off by default: nfc_neighbor_check, nfc_neighbor_evict_distance, nfc_gate_clear_distance,
# nfc_preload_clear_distance are all independent and all 0/off out of the box.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import contextlib

# Happy Hare imports
from .mmu_constants import *
from .mmu_utils      import MmuError


class NfcFieldOutcome:
    """
    clear_field()'s yield value. 'verdict' is known immediately; 'ratified' only after the
    `with` block closes (None if not PROVISIONAL, else True/False).

    'reason': short explanation for a FOREIGN verdict, for the caller's own error message.
    """
    __slots__ = ('verdict', 'ratified', 'reason')

    def __init__(self, verdict, reason=None):
        self.verdict = verdict
        self.ratified = None
        self.reason = reason


class MmuNfcFieldArbiter:
    """
    Classification, candidate selection, eviction-by-jogging and ratification for NFC
    neighbor-field arbitration. One instance per machine.
    """

    def __init__(self, mmu):
        self.mmu = mmu


    # ---- Pure classification (no I/O, no motion) --------------------------------------------

    def _field_verdict(self, gate, uid):
        """
        Classify a UID against the gate map. No I/O, no motion.
        Returns (verdict, owner_gate, diagnostic):
            MINE     - registered to this gate
            NEIGHBOR - registered to a same-unit gate, or unrecognised (owner_gate=None)
            FOREIGN  - registered to a different unit; map is stale, never a candidate
        """
        if not uid:
            return NFC_FIELD_CLEAR, None, ""

        owner = self.mmu.gate_maps.find_gate_by_rfid(uid)
        if owner is None:
            # The gate map intentionally stores only the UID physically observed
            # here. Consult the complete Spoolman UID set as a secondary identity
            # source before treating an alternate tag as unknown.
            self.mmu.log_debug("NFC: gate %d: tag %s not found in observed RFID map; checking RFIDS"
                               % (gate, uid))
            owner = self.mmu.gate_maps.find_gate_by_rfid_alias(gate, uid)
            if owner is None:
                self.mmu.log_debug("NFC: gate %d: tag %s did not match cached RFIDS"
                                   % (gate, uid))
        if owner is None:
            return NFC_FIELD_NEIGHBOR, None, ""
        if owner == gate:
            return NFC_FIELD_MINE, owner, ""

        unit = self.mmu.mmu_unit(gate)
        if not unit.owns_gate(owner):
            return (NFC_FIELD_FOREIGN, owner,
                    "NFC: gate %d: tag %s is registered to gate %d on a different unit, which "
                    "is not physically possible - the gate map is stale. Not reading the tag; "
                    "clear it with 'MMU_GATE_MAP GATE=%d RFID='" % (gate, uid, owner, owner))

        return (NFC_FIELD_NEIGHBOR, owner,
                "NFC: gate %d: tag %s belongs to neighboring gate %d" % (gate, uid, owner))


    def _field_check(self, gate, nfc_mgr):
        """Probe and classify. No motion. Returns (verdict, uid, owner_gate, diagnostic)."""
        uid = nfc_mgr.probe_gate_field(gate)
        if not uid:
            return NFC_FIELD_CLEAR, None, None, ""
        verdict, owner, diag = self._field_verdict(gate, uid)
        return verdict, uid, owner, diag


    # ---- Candidate selection: identity first, physical neighbors as fallback ---------------

    def _neighbor_candidates(self, gate, owner):
        """Eviction candidates in priority order: identified owner first, then gate-1/gate+1."""
        unit = self.mmu.mmu_unit(gate)
        candidates = []
        if owner is not None and unit.owns_gate(owner):
            candidates.append(owner)
        for adjacent in (gate - 1, gate + 1):
            if unit.owns_gate(adjacent) and adjacent not in candidates:
                candidates.append(adjacent)
        return candidates


    def _evict_reject(self, gate, candidate):
        """
        Why 'candidate' can't be evicted right now, or None if eligible. Re-checked per
        candidate, not just once - evicting one can leave it occupying a shared endstop.
        """
        if candidate == gate:
            return "it is this gate"
        unit = self.mmu.mmu_unit(gate)
        if not unit.owns_gate(candidate):
            return "it is on a different unit"
        if self.mmu.gate_status[candidate] == GATE_EMPTY:
            return "it has no filament to move"
        candidate_endstop = self.mmu.mmu_unit(candidate).p.gate_homing_endstop
        if self.mmu._shared_gate_path_occupied(candidate_endstop, candidate):
            return "its shared gate path is already occupied by another gate's filament"
        return None


    def _next_candidate(self, gate, owner, tried):
        """The next untried, eligible candidate for gate 'gate', or None if none remain."""
        for candidate in self._neighbor_candidates(gate, owner):
            if candidate in tried:
                continue
            reason = self._evict_reject(gate, candidate)
            if reason:
                tried.add(candidate) # don't re-check it again
                self.mmu.log_debug("NFC: gate %d: candidate gate %d not evictable (%s)"
                                    % (gate, candidate, reason))
                continue
            return candidate
        return None


    # ---- Eviction motion ----------------------------------------------------------------------

    def _jog_off(self, distance):
        """
        Jog the CURRENTLY SELECTED gate's filament 'distance' mm off its reference (+ve
        forward, -ve back). Plain move, not homing. Used for both neighbor eviction and
        self-jog ratification.
        """
        self.mmu.log_debug("NFC: gate %d: jogging %.0fmm %s off its park reference"
                            % (self.mmu.gate_selected, abs(distance),
                               "forward" if distance > 0 else "back"))
        self.mmu.move_filament("NFC: gate jog", distance, motor="gear")


    def _evict_one(self, gate, candidate, distance, evicted):
        """
        Load 'candidate' and jog it 'distance' mm out of gate 'gate's field. Appends to
        'evicted' as soon as the load succeeds, so a failed jog still gets restored.
        """
        saved_status = self.mmu.gate_status[candidate]
        self.mmu.log_debug(
            "NFC: gate %d: evicting gate %d - load then jog %.0fmm %s; status %d will be restored"
            % (gate, candidate, abs(distance), "forward" if distance > 0 else "back",
               saved_status))
        try:
            self.mmu.select_gate(candidate)
            self.mmu._load_gate(allow_retry=False)
        except MmuError as e:
            # nothing moved, so nothing owes a re-park - just restore status
            self.mmu.gate_maps.set_gate_status(candidate, saved_status)
            self.mmu.log_debug("NFC: gate %d: could not load gate %d to move it aside: %s"
                                % (gate, candidate, str(e)))
            return
        evicted.append((candidate, saved_status))
        try:
            self._jog_off(distance)
        except MmuError as e:
            self.mmu.log_debug("NFC: gate %d: jog of gate %d failed: %s"
                                % (gate, candidate, str(e)))


    def _settle(self, gate, nfc_mgr, distance, evicted):
        """
        Probe and jog candidates out of the field until clear or exhausted (bounded to
        num_gates+1 tries). Returns (verdict, reason) - CLEAR/MINE/FOREIGN/PROVISIONAL only.
        """
        tried = set()
        verdict = uid = owner = diag = None
        unit = self.mmu.mmu_unit(gate)
        for probe in range(unit.num_gates + 1):
            verdict, uid, owner, diag = self._field_check(gate, nfc_mgr)
            self.mmu.log_debug(
                "NFC: gate %d: field probe %d: tag %s -> %s (owner gate %s, evict budget %.0fmm)"
                % (gate, probe + 1, uid or '(none)', NFC_FIELD_NAMES.get(verdict, verdict),
                   'none' if owner is None else owner, distance))
            if verdict != NFC_FIELD_NEIGHBOR or not distance:
                break
            candidate = self._next_candidate(gate, owner, tried)
            if candidate is None:
                self.mmu.log_debug("NFC: gate %d: no evictable candidate remains" % gate)
                break
            tried.add(candidate)
            self._evict_one(gate, candidate, distance, evicted)
            # loop back and re-probe with this candidate out of the way

        if verdict in (NFC_FIELD_CLEAR, NFC_FIELD_MINE):
            if diag:
                self.mmu.log_info(diag)
            return verdict, None
        if verdict == NFC_FIELD_FOREIGN:
            self.mmu.log_warning(diag)
            return NFC_FIELD_FOREIGN, "tag %s is registered to a gate on a different unit" % uid

        # still NEIGHBOR: no motion budget, or every candidate tried/rejected
        if owner is not None:
            # known owner, still uncleared: hard FOREIGN, not provisional
            self.mmu.log_warning(
                "NFC: gate %d: tag %s belongs to gate %d and could not be moved out of the way "
                "- proceeding without reading a tag. If that spool was moved by hand, clear "
                "the stale entry with 'MMU_GATE_MAP GATE=%d RFID='" % (gate, uid, owner, owner))
            return NFC_FIELD_FOREIGN, ("tag %s belongs to gate %d and could not be moved "
                                        "out of the way" % (uid, owner))
        return NFC_FIELD_PROVISIONAL, None


    # ---- Provisional-verdict ratification ------------------------------------------------------

    def _ratify(self, gate, nfc_mgr, endstop, clear_distance=0.0,
                parking_distance=None, homing_max=None):
        """
        Re-probe once after the caller's own motion completes, to confirm/reject a
        PROVISIONAL verdict. Raw presence check, not _field_check - re-deriving ownership
        would be circular since the held read is about to write that same map entry.

        If still present and 'clear_distance' gives a safe motion budget for 'endstop',
        escalate to a deliberate self-jog (_verify_by_self_jog) instead of trusting
        incidental motion alone. 'clear_distance'/'endstop' are the CALLER's own values
        (nfc_gate_clear_distance/gate_homing_endstop for scan, nfc_preload_clear_distance/
        gate_preload_endstop for preload).

        Returns True/False; caller (clear_field) commits or discards the held read.
        """
        uid = nfc_mgr.probe_gate_field(gate)
        if not uid:
            self.mmu.log_debug("NFC: gate %d: provisional tag attribution ratified (field "
                                "clear after the operation's own motion)" % gate)
            return True

        distance = clear_distance
        safe_reach = (
            homing_max is None
            or (abs(distance) <= homing_max
                and (parking_distance is None
                     or parking_distance + distance >= -homing_max))
        )
        if (distance and safe_reach
                and not (distance > 0 and endstop in SHARED_GATE_ENDSTOPS)):
            if self._verify_by_self_jog(gate, nfc_mgr, distance):
                self.mmu.log_debug(
                    "NFC: gate %d: provisional tag attribution ratified via a deliberate "
                    "self-jog (field cleared when this gate's own filament was jogged %.0fmm "
                    "further off its park position, and returned to it)" % (gate, abs(distance)))
                return True
            self.mmu.log_warning(
                "NFC: gate %d: could not confirm this gate's own tag - the reader still shows "
                "tag %s even after gate %d's own filament was deliberately jogged %.0fmm "
                "further off its park position and back, so the read is being discarded "
                "rather than attributed. If this is a new/unregistered spool, re-run once the "
                "neighboring gate's spool has been moved or registered" % (gate, uid, gate, abs(distance)))
            return False

        # no motion budget or unsafe direction - plain discard
        self.mmu.log_warning(
            "NFC: gate %d: could not confirm this gate's own tag - the reader still shows "
            "tag %s after gate %d's own filament settled, so the read is being discarded "
            "rather than attributed. If this is a new/unregistered spool, re-run once the "
            "neighboring gate's spool has been moved or registered" % (gate, uid, gate))
        return False


    def _verify_by_self_jog(self, gate, nfc_mgr, distance):
        """
        Jog this gate's own already-parked filament 'distance' mm further, re-probe, jog
        back. Plain jog-and-restore, not _repark_evicted's reverse-home+park (that's for a
        freshly loaded neighbor whose position is less certain; reusing it here risks the
        wrong profile's park). Returns True if the field cleared.
        """
        self.mmu.select_gate(gate)  # a neighbor may still be selected at this point
        with self.mmu.wrap_suspend_filament_monitoring():
            try:
                self._jog_off(distance)
                try:
                    uid = nfc_mgr.probe_gate_field(gate)
                finally:
                    self._jog_off(-distance)
            except Exception as e:
                self.mmu.gate_maps.set_gate_status(gate, GATE_UNKNOWN)
                self.mmu.log_error(
                    "NFC: gate %d: self-jog verification move failed: %s. Gate marked "
                    "unknown - check for a jam" % (gate, str(e)))
                raise
        return not uid


    # ---- Restore ------------------------------------------------------------------------------

    # _verify_by_self_jog does NOT reuse this restore for the gate under test (wrong-profile risk).
    def _repark_evicted(self, gate, distance, evicted):
        """Re-park half of _restore_evicted, reverse eviction order."""
        while evicted:
            candidate, saved_status = evicted.pop()
            try:
                self.mmu.select_gate(candidate)
                if distance > 0:
                    self.mmu._unload_gate(extra_homing=abs(distance)) # forward: reverse-home + park
                else:
                    self.mmu._load_gate(allow_retry=False) # behind: home to gate, then reverse-home + park
                    self.mmu._unload_gate()
                self.mmu.gate_maps.set_gate_status(candidate, saved_status)
            except Exception as e:
                self.mmu.gate_maps.set_gate_status(candidate, GATE_UNKNOWN)
                self.mmu.log_error(
                    "NFC: failed to re-park gate %d after moving it out of gate %d's reader "
                    "field: %s. Gate %d marked unknown - check for a jam"
                    % (candidate, gate, str(e), candidate))


    def _restore_evicted(self, gate, distance, evicted):
        """Re-park all evicted gates in reverse order; always leaves 'gate' selected."""
        if evicted:
            self.mmu.log_debug(
                "NFC: gate %d: restoring %d evicted neighbor(s) %s, then reselecting gate %d"
                % (gate, len(evicted), [candidate for candidate, _ in reversed(evicted)], gate))
            self._repark_evicted(gate, distance, evicted)
        try:
            self.mmu.select_gate(gate)
        except Exception as e:
            self.mmu.log_error("NFC: could not reselect gate %d after neighbor eviction: %s"
                                % (gate, str(e)))


    # ---- Public entry point ---------------------------------------------------------------------

    @contextlib.contextmanager
    def clear_field(self, gate, nfc_mgr, endstop=None, clear_distance=0.0,
                    parking_distance=None, homing_max=None):
        """
        Settle gate 'gate's NFC field for the enclosed operation, evicting neighbors when
        armed.

        'endstop'/'clear_distance' are the caller's own context (nfc_gate_clear_distance/
        gate_homing_endstop for scan, nfc_preload_clear_distance/gate_preload_endstop for
        preload) - used only if ratification escalates to a self-jog.

        Yields a NfcFieldOutcome:
            CLEAR       - nothing there, or arbitration not armed
            MINE        - confirmed this gate's own, attributed immediately
            FOREIGN     - known not this gate's; caller must not attribute (see outcome.reason)
            PROVISIONAL - unregistered, tentatively mine; read is HELD until ratified after
                          the `with` block (outcome.ratified, readable only afterward)

        Re-parks evicted gates on exit, even on error, leaving 'gate' selected. Suspends
        filament monitoring around its own moves only, never across the yield.
        """
        if nfc_mgr is None:
            yield NfcFieldOutcome(NFC_FIELD_CLEAR)
            return

        distance = self.mmu.mmu_unit(gate).p.nfc_neighbor_evict_distance
        evicted = []
        provisional = False
        outcome = None
        try:
            with self.mmu.wrap_suspend_filament_monitoring():
                verdict, reason = self._settle(gate, nfc_mgr, distance, evicted)
                self.mmu.select_gate(gate)
            provisional = (verdict == NFC_FIELD_PROVISIONAL)
            outcome = NfcFieldOutcome(verdict, reason)
            action = (
                "proceeds and attributes its read normally"
                if verdict in (NFC_FIELD_CLEAR, NFC_FIELD_MINE)
                else "proceeds with its read held for ratification"
                if provisional
                else "must proceed without attributing a tag"
            )
            self.mmu.log_debug(
                "NFC: gate %d: field settled as %s - %d neighbor(s) evicted; operation %s"
                % (gate, NFC_FIELD_NAMES.get(verdict, verdict), len(evicted), action))
            if provisional:
                # hold rather than commit - trust isn't known until ratification below
                nfc_mgr.hold_attribution(gate)
            yield outcome
        finally:
            # ratify before restoring neighbors (restore is itself motion that could disturb
            # the read); guarded so an error here can't skip the restore below
            if provisional:
                try:
                    ratified = self._ratify(
                        gate, nfc_mgr, endstop, clear_distance,
                        parking_distance, homing_max)
                except Exception as e:
                    self.mmu.log_error("NFC: gate %d: ratification check failed: %s" % (gate, str(e)))
                    ratified = False # safer to discard than commit blind
                if outcome is not None:
                    outcome.ratified = ratified
                if ratified:
                    nfc_mgr.release_held_attribution()
                else:
                    nfc_mgr.discard_held_attribution()
            # unconditional: must restore even if settling raised before recording anything
            with self.mmu.wrap_suspend_filament_monitoring():
                self._restore_evicted(gate, distance, evicted)
