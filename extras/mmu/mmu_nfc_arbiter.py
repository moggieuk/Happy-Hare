# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: NFC "noisy neighbor" reader-field arbitration
#
# Per-gate NFC readers can sit close enough that a spool parked at a neighboring gate is
# physically inside gate G's own RF field - satisfying the preload NFC endstop, or a jog-scan's
# fast path, with a tag that was never gate G's own. Before either trusts "a tag is at my
# reader" to mean "this gate's tag", MmuNfcFieldArbiter settles who is actually there:
#
#   - a UID the gate map already attributes to gate G (or that it doesn't recognise at all)
#     is treated as this gate's own,
#   - a UID registered to a specific neighboring gate on the SAME unit is evicted by
#     temporarily loading that gate and jogging its filament out of the way,
#   - a UID registered to a gate on a DIFFERENT unit is physically impossible, so the map is
#     stale - never attributed, never a candidate,
#   - an unregistered tag that survives eviction attempts (or that arbitration has no motion
#     budget to evict at all) is only a PROVISIONAL "assumed mine": the caller's own necessary
#     motion (preload's homing+park; a jog-scan's sweep) is the only thing that can actually
#     tell it apart from a neighbor's unregistered spool, so clear_field()'s caller must run
#     that motion as normal and this class re-checks the field afterwards.
#
# Deliberately kept out of mmu_filament_movement.py: _preload_gate and _jog_scan gain only the
# clear_field() context manager and an arm check, so this bookkeeping doesn't grow those
# already-large functions further.
#
# Off by default: neither nfc_neighbor_check nor nfc_neighbor_evict_distance is armed out of
# the box, and clear_field() is an inert passthrough when arbitration isn't armed for a gate -
# a stock machine does no extra reader I/O and behaves exactly as it did before this module
# existed.
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


class MmuNfcFieldArbiter:
    """
    Owns the classification ladder, candidate selection, eviction-by-jogging and provisional-
    verdict ratification for NFC neighbor-field arbitration. One instance serves the whole
    machine (candidates are always resolved within a single unit, so no per-unit state is
    needed here); constructed once by the controller alongside its other manager objects.
    """

    def __init__(self, mmu):
        self.mmu = mmu


    # ---- Pure classification (no I/O, no motion) --------------------------------------------

    def _field_verdict(self, gate, uid):
        """
        Classify a UID found in gate 'gate's reader field against the gate map. No I/O and no
        motion, so the whole ladder is exercisable with _MMU_TEST NFC_FIELD=1.

        Returns (verdict, owner_gate, diagnostic):
            NFC_FIELD_MINE      registered to this gate - positively confirmed.
            NFC_FIELD_NEIGHBOR registered to another gate on the SAME unit (evictable), OR
                                 unrecognised by the gate map at all (owner_gate is then None -
                                 most likely this gate's own spool that hasn't been scanned
                                 yet, but not yet distinguishable from an unregistered
                                 neighbor's spool without motion; see _settle/clear_field).
            NFC_FIELD_FOREIGN   registered to a gate on a DIFFERENT unit - physically
                                 impossible, so the gate map itself is stale. Never a
                                 candidate for eviction under any circumstances.
        'owner_gate' is None only for the unregistered sub-case of NFC_FIELD_NEIGHBOR.
        """
        if not uid:
            return NFC_FIELD_CLEAR, None, ""

        owner = self.mmu.gate_maps.find_gate_by_rfid(uid)
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
        """
        Probe gate 'gate's reader field and classify whatever is in it. No motion.
        Returns (verdict, uid, owner_gate, diagnostic); NFC_FIELD_CLEAR when nothing is there.
        """
        uid = nfc_mgr.probe_gate_field(gate)
        if not uid:
            return NFC_FIELD_CLEAR, None, None, ""
        verdict, owner, diag = self._field_verdict(gate, uid)
        return verdict, uid, owner, diag


    # ---- Candidate selection: identity first, physical neighbors as fallback ---------------

    def _neighbor_candidates(self, gate, owner):
        """
        Gates worth trying to evict out of gate 'gate's reader field, in priority order: the
        positively-identified owner first (if it's a real gate on the same unit), then the
        physical neighbors (gate-1, gate+1), bounds-checked to the unit and deduplicated.
        """
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
        Why can gate 'candidate' not be jogged out of gate 'gate's reader field right now?
        Returns a short reason for the diagnostic, or None when it is eligible to try.
        "Already tried this call" is filtered by the caller (_next_candidate), not here - this
        is purely about the candidate's own current state.

        The shared-gate-path-occupancy check is re-evaluated for every candidate, not just
        once up front: evicting one neighbor can leave it homed at its own gate (not parked),
        which - if that gate's endstop is a per-unit SHARED_GATE_ENDSTOPS resource - would
        make loading a second candidate onto the same shared path unsafe. See the
        gate-endstop-invariants skill for why this check can't be skipped.
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
                tried.add(candidate) # Don't re-derive/re-reject it again next iteration
                self.mmu.log_debug("NFC: gate %d: candidate gate %d not evictable (%s)"
                                    % (gate, candidate, reason))
                continue
            return candidate
        return None


    # ---- Eviction motion ----------------------------------------------------------------------

    def _jog_off(self, distance):
        """
        Move the CURRENTLY SELECTED gate's filament 'distance' mm off the gate reference so
        its RFID tag leaves a *neighboring* gate's reader field. Signed: positive travels
        forward of the gate, negative behind it. Assumes the filament is homed at the gate
        (the caller does _load_gate() first).

        A plain move, not a homing move - there is nothing to home against here, the whole
        point is to travel past wherever the tag currently sits.
        """
        self.mmu.log_debug("NFC: gate %d: jogging %.0fmm %s to clear a neighbor's reader field"
                            % (self.mmu.gate_selected, abs(distance),
                               "forward" if distance > 0 else "back"))
        self.mmu.move_filament("NFC: neighbor evict", distance, motor="gear")


    def _evict_one(self, gate, candidate, distance, evicted):
        """
        Load 'candidate' and jog it 'distance' mm off its park position, out of gate 'gate's
        reader field. Appends (candidate, saved_status) to 'evicted' the moment the load
        succeeds - even if the jog itself then fails - so the caller always re-parks/restores
        it; recorded before the jog so a failed jog (or an error escaping it) still gets
        restored instead of silently left loaded.
        """
        saved_status = self.mmu.gate_status[candidate]
        try:
            self.mmu.select_gate(candidate)
            self.mmu._load_gate(allow_retry=False)
        except MmuError as e:
            # Nothing to jog (no filament, or a jam). Nothing was moved, so nothing owes a
            # re-park - just put the status back.
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
        Probe gate 'gate's field and, while motion is available, jog identified candidates out
        of it until it clears (or nothing more can be tried). Bounded to the unit's gate count
        (at worst every other gate gets tried once).

        Returns one of NFC_FIELD_CLEAR / NFC_FIELD_MINE / NFC_FIELD_FOREIGN / NFC_FIELD_PROVISIONAL -
        never the intermediate NFC_FIELD_NEIGHBOR, which is resolved into one of the above
        before returning.
        """
        tried = set()
        verdict = uid = owner = diag = None
        unit = self.mmu.mmu_unit(gate)
        for _ in range(unit.num_gates + 1):
            verdict, uid, owner, diag = self._field_check(gate, nfc_mgr)
            if verdict != NFC_FIELD_NEIGHBOR or not distance:
                break
            candidate = self._next_candidate(gate, owner, tried)
            if candidate is None:
                break
            tried.add(candidate)
            self._evict_one(gate, candidate, distance, evicted)
            # Loop back around to re-probe with this candidate out of the way

        if verdict in (NFC_FIELD_CLEAR, NFC_FIELD_MINE):
            if diag:
                self.mmu.log_info(diag)
            return verdict
        if verdict == NFC_FIELD_FOREIGN:
            self.mmu.log_warning(diag)
            return NFC_FIELD_FOREIGN

        # Still NFC_FIELD_NEIGHBOR: either there was no motion budget to begin with, or every
        # reachable candidate was tried/rejected and the field never cleared.
        if owner is not None:
            # Positive evidence the tag belongs to a specific other gate - "assume it's ours
            # anyway" would be wrong regardless of whether eviction succeeded, so this is a
            # hard FOREIGN, not a provisional one.
            self.mmu.log_warning(
                "NFC: gate %d: tag %s belongs to gate %d and could not be moved out of the way "
                "- proceeding without reading a tag. If that spool was moved by hand, clear "
                "the stale entry with 'MMU_GATE_MAP GATE=%d RFID='" % (gate, uid, owner, owner))
            return NFC_FIELD_FOREIGN
        return NFC_FIELD_PROVISIONAL


    # ---- Provisional-verdict ratification ------------------------------------------------------

    def _ratify(self, gate, nfc_mgr):
        """
        Re-probe gate 'gate's reader once, after the caller's own natural motion (preload's
        homing+park, or a jog-scan's sweep with its fast path suppressed) has completed, to
        confirm or reject a NFC_FIELD_PROVISIONAL verdict.

        This is purely diagnostic. The tag read already taken during the caller's operation is
        not undone either way (a Spoolman resolution may already be in flight) - this can only
        warn that the attribution just made may have been wrong.
        """
        verdict, uid, owner, diag = self._field_check(gate, nfc_mgr)
        if verdict == NFC_FIELD_CLEAR or (verdict == NFC_FIELD_MINE and owner == gate):
            self.mmu.log_debug("NFC: gate %d: provisional tag attribution ratified (field "
                                "clear after the operation's own motion)" % gate)
            return
        self.mmu.log_warning(
            "NFC: gate %d: failed to reliably detect this gate's own tag - the reader still "
            "shows a tag after gate %d's own filament settled, so a neighboring gate's tag may "
            "have been misattributed here. Verify gate %d's rfid/spool_id manually" % (gate, gate, gate))


    # ---- Restore ------------------------------------------------------------------------------

    def _repark_evicted(self, gate, distance, evicted):
        """The re-park half of _restore_evicted, in reverse eviction order."""
        while evicted:
            candidate, saved_status = evicted.pop()
            try:
                self.mmu.select_gate(candidate)
                if distance > 0:
                    # Filament is forward of the gate: reverse-home + park
                    self.mmu._unload_gate(extra_homing=abs(distance))
                else:
                    # Filament is behind the gate: home forward back to the gate, then
                    # reverse-home + park (reuses the proven parking, incl. encoder overshoot)
                    self.mmu._load_gate(allow_retry=False)
                    self.mmu._unload_gate()
                self.mmu.gate_maps.set_gate_status(candidate, saved_status)
            except Exception as e:
                self.mmu.gate_maps.set_gate_status(candidate, GATE_UNKNOWN)
                self.mmu.log_error(
                    "NFC: failed to re-park gate %d after moving it out of gate %d's reader "
                    "field: %s. Gate %d marked unknown - check for a jam"
                    % (candidate, gate, str(e), candidate))


    def _restore_evicted(self, gate, distance, evicted):
        """
        Re-park every gate that was jogged aside, in reverse order, and restore its saved
        gate_status. Always leaves 'gate' selected, as the caller expects - including when
        eviction blew up part-way and left a neighbor selected.
        """
        if evicted:
            self._repark_evicted(gate, distance, evicted)
        try:
            self.mmu.select_gate(gate)
        except Exception as e:
            self.mmu.log_error("NFC: could not reselect gate %d after neighbor eviction: %s"
                                % (gate, str(e)))


    # ---- Public entry point ---------------------------------------------------------------------

    @contextlib.contextmanager
    def clear_field(self, gate, nfc_mgr):
        """
        Ensure gate 'gate's NFC reader field is settled for the duration of the enclosed
        operation, temporarily jogging identified neighboring gates' filament off their park
        position when arbitration has a motion budget for it.

        Yields one of:
            NFC_FIELD_CLEAR       nothing in the field, or arbitration isn't armed for this
                                   gate (nfc_mgr is None) - either way the caller does exactly
                                   what it always did.
            NFC_FIELD_MINE        the tag is positively confirmed as this gate's own.
            NFC_FIELD_FOREIGN     a tag known not to be this gate's, uncleared - the caller
                                   must not attribute it (see per-caller handling in
                                   _preload_gate / _jog_scan).
            NFC_FIELD_PROVISIONAL an unregistered tag tentatively treated as MINE - the caller
                                   proceeds as it would for MINE, EXCEPT a jog-scan must not
                                   take its "already at reader" fast path, since there would be
                                   nothing left to observe clearing. Ratified/rejected in this
                                   method's own `finally`, once the caller's own natural motion
                                   has completed.

        On exit, every jogged gate is re-parked and its prior gate_status restored, in reverse
        order, even when the enclosed block raised, and 'gate' is left selected.

        Filament monitoring is suspended around this method's own moves only, never across the
        yield - both callers already suspend it inside their own enclosed block and that
        contextmanager does not nest.
        """
        if nfc_mgr is None:
            yield NFC_FIELD_CLEAR
            return

        distance = self.mmu.mmu_unit(gate).p.nfc_neighbor_evict_distance
        evicted = []
        provisional = False
        try:
            with self.mmu.wrap_suspend_filament_monitoring():
                verdict = self._settle(gate, nfc_mgr, distance, evicted)
                self.mmu.select_gate(gate) # The enclosed block runs on 'gate', as it did before
            provisional = (verdict == NFC_FIELD_PROVISIONAL)
            yield verdict
        finally:
            # Ratify BEFORE restoring evicted neighbors: the ratification re-probe must see the
            # field as the caller's own operation left it, not after neighbors are re-parked
            # (which is itself extra motion that could disturb the reading). Guarded on its own:
            # an unexpected error here must never skip the restore below and leave an evicted
            # neighbor stranded off its park position.
            if provisional:
                try:
                    self._ratify(gate, nfc_mgr)
                except Exception as e:
                    self.mmu.log_error("NFC: gate %d: ratification check failed: %s" % (gate, str(e)))
            # Unconditional: eviction/settling can raise part-way with a neighbor selected and
            # nothing yet recorded in 'evicted', and the caller must still get its gate back.
            with self.mmu.wrap_suspend_filament_monitoring():
                self._restore_evicted(gate, distance, evicted)
