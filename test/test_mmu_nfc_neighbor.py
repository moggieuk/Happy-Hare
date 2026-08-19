# Happy Hare test harness - NFC "noisy neighbor" field arbitration (MmuNfcFieldArbiter).
#
# Off by default (nfc_neighbor_check=0, nfc_neighbor_evict_distance=0.0): most of this file
# exercises the classification ladder, candidate selection and eligibility PURELY (no reader
# I/O, no motion), which is fully testable. Genuine cross-gate RF crosstalk cannot be
# simulated at all - the virtual NFC chip (test/hh/nfc_fixtures.py) is per-gate isolated by
# design, so no fixture can put one gate's tag in a neighboring gate's field. Two things get
# around that for the end-to-end tests near the bottom of this file:
#
#   1. The classification ladder only cares what the GATE MAP says a UID belongs to, never
#      which physical chip actually reported it - so registering a real, physically-present
#      tag's UID to a DIFFERENT gate (one with no filament of its own) faithfully exercises
#      NEIGHBOR/FOREIGN classification and the eviction-rejection path end to end, even
#      though eviction itself cannot be shown to physically clear a genuinely different
#      reader's field.
#   2. The "ratified" half of a PROVISIONAL verdict (row 3 of the plan's state table) IS
#      reachable for real: an unregistered tag that is genuinely this gate's own clears the
#      reader once the operation's natural motion (preload's park, or a jog-scan's forced
#      sweep) moves it away, which the virtual chip models correctly. The "not ratified" half
#      (row 6) is not reachable, for the same isolation reason as neighbor eviction.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.bootstrap import install

install()   # Put the fake klippy root on sys.path before importing MMU modules

from extras.mmu.mmu_constants import (
    NFC_FIELD_CLEAR, NFC_FIELD_MINE, NFC_FIELD_NEIGHBOR, NFC_FIELD_FOREIGN,
    NFC_FIELD_PROVISIONAL, NFC_FIELD_NAMES, GATE_EMPTY, GATE_AVAILABLE,
    SPOOLMAN_PULL, SPOOLMAN_OFF,
)

logging.getLogger().setLevel(logging.CRITICAL)

TAG = '04A1B2C3'


class NeighborTestCase(unittest.TestCase):
    """Shared setup: BoxTurtle + per-gate NFC readers, virtual chips."""
    PROFILE = 'nfc_per_gate'

    def setUp(self):
        self.hh = session(self.PROFILE, virtual_nfc=True)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        self.arbiter = self.hh.mmu.nfc_arbiter

    def tearDown(self):
        self.hh.close()

    def preload(self, gate, position=None):
        """Place a gate's filament AND mark the gate available (mirrors test_mmu_nfc_scan.py)."""
        if position is None:
            self.hh.place_filament(gate)
        else:
            self.hh.place_filament(gate, position=position)
        self.hh.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)


class TestFieldVerdictLadder(NeighborTestCase):
    """Pure classification: no reader I/O, no motion - MmuNfcFieldArbiter._field_verdict."""

    def test_no_uid_is_clear(self):
        verdict, owner, diag = self.arbiter._field_verdict(0, None)
        self.assertEqual(verdict, NFC_FIELD_CLEAR)
        self.assertIsNone(owner)

    def test_symbolic_names_cover_every_verdict(self):
        self.assertEqual(
            set(NFC_FIELD_NAMES),
            {NFC_FIELD_CLEAR, NFC_FIELD_MINE, NFC_FIELD_NEIGHBOR,
             NFC_FIELD_FOREIGN, NFC_FIELD_PROVISIONAL})

    def test_registered_to_this_gate_is_mine(self):
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)
        verdict, owner, diag = self.arbiter._field_verdict(0, TAG)
        self.assertEqual(verdict, NFC_FIELD_MINE)
        self.assertEqual(owner, 0)
        self.assertEqual(diag, '', 'a positively-confirmed MINE needs no diagnostic')

    def test_unregistered_uid_is_neighbor_with_no_owner(self):
        """
        NFC_FIELD_NEIGHBOR with owner=None is the internal "ambiguous" rung - _settle()
        resolves it into MINE (optimistic, no motion), PROVISIONAL (motion available), or
        never FOREIGN directly. _field_verdict itself does not decide between those.
        """
        verdict, owner, diag = self.arbiter._field_verdict(0, TAG)
        self.assertEqual(verdict, NFC_FIELD_NEIGHBOR)
        self.assertIsNone(owner)

    def test_registered_to_a_same_unit_gate_is_neighbor(self):
        self.hh.mmu.gate_maps.set_gate_rfid(1, TAG)
        verdict, owner, diag = self.arbiter._field_verdict(0, TAG)
        self.assertEqual(verdict, NFC_FIELD_NEIGHBOR)
        self.assertEqual(owner, 1)
        self.assertIn('neighboring gate 1', diag)

    def test_registered_to_a_different_unit_is_hard_foreign(self):
        """
        Cross-unit is physically impossible (a reader cannot see across units), so this is
        always FOREIGN - never a NEIGHBOR/eviction candidate, regardless of motion budget.
        """
        hh = session('ercf_vvd', virtual_nfc=True)
        try:
            hh.boot()
            self.assertEqual(hh.errors, [])
            hh.mmu.gate_maps.set_gate_rfid(0, TAG)  # unit0 gate
            arbiter = hh.mmu.nfc_arbiter
            verdict, owner, diag = arbiter._field_verdict(9, TAG)  # unit1 gate
            self.assertEqual(verdict, NFC_FIELD_FOREIGN)
            self.assertEqual(owner, 0)
            self.assertIn('different unit', diag)
        finally:
            hh.close()


class TestNeighborCandidates(NeighborTestCase):
    """Candidate selection: identity first, then physical neighbors, bounded to the unit."""

    def test_identity_owner_comes_first(self):
        candidates = self.arbiter._neighbor_candidates(1, owner=3)
        self.assertEqual(candidates[0], 3)
        self.assertIn(0, candidates)
        self.assertIn(2, candidates)

    def test_no_identity_owner_falls_back_to_positional_neighbors_only(self):
        candidates = self.arbiter._neighbor_candidates(1, owner=None)
        self.assertEqual(sorted(candidates), [0, 2])

    def test_owner_equal_to_a_positional_neighbor_is_not_duplicated(self):
        candidates = self.arbiter._neighbor_candidates(1, owner=0)
        self.assertEqual(candidates.count(0), 1)
        self.assertEqual(candidates, [0, 2])

    def test_edge_gate_excludes_out_of_range_neighbor(self):
        """BoxTurtle here is 4 gates (0-3); gate 0 has no gate -1, gate 3 has no gate 4."""
        self.assertEqual(sorted(self.arbiter._neighbor_candidates(0, owner=None)), [1])
        self.assertEqual(sorted(self.arbiter._neighbor_candidates(3, owner=None)), [2])

    def test_owner_on_a_different_unit_is_excluded(self):
        hh = session('ercf_vvd', virtual_nfc=True)
        try:
            hh.boot()
            self.assertEqual(hh.errors, [])
            candidates = hh.mmu.nfc_arbiter._neighbor_candidates(9, owner=0)  # owner is unit0
            self.assertNotIn(0, candidates)
            self.assertEqual(sorted(candidates), [10])  # gate 9's only in-unit neighbor
        finally:
            hh.close()


class TestEvictReject(NeighborTestCase):
    """Per-candidate eligibility: gate_reject reasons, re-checked every call."""

    def test_this_gate_itself_is_rejected(self):
        self.assertIsNotNone(self.arbiter._evict_reject(1, 1))

    def test_a_gate_with_no_filament_is_rejected(self):
        # Fresh boot: every gate starts GATE_EMPTY
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)
        reason = self.arbiter._evict_reject(1, 0)
        self.assertIsNotNone(reason)
        self.assertIn('no filament', reason)

    def test_a_loaded_gate_on_the_same_unit_is_eligible(self):
        self.preload(0)
        self.assertIsNone(self.arbiter._evict_reject(1, 0))

    def test_shared_gate_path_already_occupied_is_rejected(self):
        """
        Mirrors TestSharedGateOccupancy in test_mmu_nfc_scan.py: if candidate 0's own gate
        endstop is a per-UNIT shared resource (mmu_shared_exit) already occupied by another
        gate's filament, loading it onto that same shared path is unsafe - the exact hazard
        _shared_gate_path_occupied exists to catch, re-checked per candidate (not just once)
        because a candidate's own endstop, not gate 1's, is what matters here.
        """
        self.hh.mmu.mmu_unit(0).p.gate_homing_endstop = 'mmu_shared_exit'
        self.preload(0)
        # Gate 2 (a different gate on the same unit) is already sitting on the shared switch
        self.hh.place_filament(2, position=self.fil.layout['mmu_shared_exit'] + 5.0)
        self.hh.mmu.gate_maps.set_gate_status(2, GATE_AVAILABLE)
        reason = self.arbiter._evict_reject(1, 0)
        self.assertIsNotNone(reason)
        self.assertIn('occupied', reason)


class TestFieldArm(NeighborTestCase):
    """Shared neighbor options arm both operations; clear distances arm only their own."""

    def test_off_by_default(self):
        self.assertIsNone(self.hh.mmu._nfc_field_arm(0))

    def test_neighbor_check_alone_arms(self):
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_check = 1
        self.assertIsNotNone(self.hh.mmu._nfc_field_arm(0))

    def test_evict_distance_alone_arms(self):
        self.hh.mmu.mmu_unit(0).p.gate_homing_endstop = 'mmu_exit'
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance = -40.0
        self.assertIsNotNone(self.hh.mmu._nfc_field_arm(0))

    def test_gate_clear_distance_alone_arms(self):
        """
        A machine that only wants the scan-context self-jog ratification escalation, with
        neither neighbor check nor eviction nor the preload distance, must still reach
        clear_field()/_ratify() - otherwise nfc_gate_clear_distance would be configured but
        silently do nothing.
        """
        self.hh.mmu.mmu_unit(0).p.gate_homing_endstop = 'mmu_exit'
        self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance = -40.0
        self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance = 0.0
        self.assertIsNotNone(self.hh.mmu._nfc_field_arm(0, clear_distance=-40.0))
        self.assertIsNone(self.hh.mmu._nfc_field_arm(
            0, profile_endstop='mmu_exit', clear_distance=0.0))

    def test_preload_clear_distance_alone_arms(self):
        """Same as above, but for the independent preload-context distance."""
        self.hh.mmu.mmu_unit(0).p.gate_homing_endstop = 'mmu_exit'
        self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance = 0.0
        self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance = -40.0
        self.assertIsNotNone(self.hh.mmu._nfc_field_arm(
            0, profile_endstop='mmu_exit', clear_distance=-40.0))
        self.assertIsNone(self.hh.mmu._nfc_field_arm(0, clear_distance=0.0))

    def test_encoder_homing_never_arms(self):
        """Encoder homing can't be compounded with the reader - nothing to protect."""
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_check = 1
        self.assertIsNone(self.hh.mmu._nfc_field_arm(0, profile_endstop='encoder'))

    def test_gate_arm_agrees_with_the_preload_compound_check(self):
        """
        _gate_nfc_reader is the single source of truth both _build_gate_nfc_compound and
        _nfc_field_arm ask - they must never disagree about "can this gate read a tag".
        """
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_check = 1
        self.hh.mmu.nfc_arbiter  # sanity: constructed
        reader_says_yes = self.hh.mmu._gate_nfc_reader(0) is not None
        arm_says_yes = self.hh.mmu._nfc_field_arm(0) is not None
        self.assertEqual(reader_says_yes, arm_says_yes)


class TestNeighborEvictDistanceValidation(NeighborTestCase):
    """Config validation for nfc_neighbor_evict_distance, and its on_change revalidation."""

    def test_zero_disables_and_needs_no_window(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=0')
        self.assertEqual(self.hh.errors, [])

    def test_forward_jog_rejected_on_shared_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=40')
        self.assertIn('nfc_neighbor_evict_distance', str(cm.exception))
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance, 0.0)

    def test_backward_jog_accepted_on_shared_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=-40')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance, -40.0)

    def test_forward_jog_accepted_on_a_per_gate_exit_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.assertEqual(self.hh.errors, [])
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=40')
        self.assertEqual(self.hh.errors, [])

    def test_out_of_window_distance_is_rejected(self):
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=-999')
        self.assertIn('nfc_gate_jog_scan_window', str(cm.exception))

    def test_switching_to_a_shared_endstop_rechecks_a_stale_evict_distance(self):
        """
        Mirrors test_switching_to_a_shared_endstop_rechecks_a_stale_parking_distance in
        test_mmu_nfc_scan.py: set a positive distance while legally on mmu_exit, then switch
        the endstop live to a shared one in a SEPARATE command - the on_change hook on
        gate_homing_endstop must catch the now-unsafe value rather than leave it stale.
        """
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=40')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertIn('nfc_neighbor_evict_distance', str(cm.exception))
        # The switch must not land half-applied with a stale, now-unsafe positive distance
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'mmu_shared_exit')


class TestClearDistanceValidation(NeighborTestCase):
    """
    Config validation for nfc_gate_clear_distance / nfc_preload_clear_distance, and their
    on_change revalidation. These are park-relative rather than scan-window targets, but the
    open-loop jog and return must stay within the corresponding profile's homing reach so the
    filament cannot be pulled beyond its configured recoverable range. The forward-jog-vs-
    shared-endstop rule is checked against each parameter's own endstop too.
    """

    def test_zero_disables(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=0')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=0')
        self.assertEqual(self.hh.errors, [])

    def test_jog_magnitude_cannot_exceed_gate_homing_max(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-999')
        self.assertIn('homing maximum', str(cm.exception))

    def test_backward_target_includes_the_existing_park_offset(self):
        # BoxTurtle parks at -100 with a 300mm homing reach. A -250 jog is individually
        # smaller than 300, but its final target is -350 and therefore unsafe.
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-250')
        self.assertIn('behind the homing datum', str(cm.exception))

    def test_normal_clear_distance_inside_recovery_reach_is_accepted(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-40')
        self.assertEqual(self.hh.errors, [])

    def test_parking_change_rechecks_a_stale_clear_reach(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_parking_distance=-10')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-250')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_parking_distance=-100')
        self.assertIn('nfc_gate_clear_distance', str(cm.exception))

    def test_homing_max_change_rechecks_a_stale_clear_reach(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-150')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_max=200')
        self.assertIn('nfc_gate_clear_distance', str(cm.exception))

    def test_gate_forward_jog_rejected_on_shared_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=40')
        self.assertIn('nfc_gate_clear_distance', str(cm.exception))
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance, 0.0)

    def test_gate_backward_jog_accepted_on_shared_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-40')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance, -40.0)

    def test_gate_forward_jog_accepted_on_a_per_gate_exit_endstop(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.assertEqual(self.hh.errors, [])
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=40')
        self.assertEqual(self.hh.errors, [])

    def test_switching_to_a_shared_endstop_rechecks_a_stale_gate_clear_distance(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=40')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertIn('nfc_gate_clear_distance', str(cm.exception))
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'mmu_shared_exit')

    def test_preload_can_diverge_from_gate_including_in_sign(self):
        """
        Confirms the two are genuinely independent, not just two names for one value - the
        motivating case from the user's own request: opposite-signed clear jogs for preload
        (forward) vs scan (backward), since the two operations can home/park via different
        endstops. (The "preload defaults to gate" relationship is a boot-time ParamSpec
        default - like nfc_preload_jog_scan_window's own default - not something a later
        MMU_TEST_CONFIG change re-derives live, so that half isn't exercised here.)
        """
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-40')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=25')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance, -40.0,
                         'setting the preload distance must not disturb the gate distance')
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance, 25.0)

    def test_preload_forward_jog_rejected_on_its_own_shared_endstop(self):
        """The preload distance is checked against the PRELOAD endstop, not the gate one."""
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=mmu_shared_exit')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=40')
        self.assertIn('nfc_preload_clear_distance', str(cm.exception))

    def test_preload_endstop_unset_inherits_gate_homing_endstop_for_validation(self):
        """
        gate_preload_endstop='' inherits gate_homing_endstop, same as everywhere else - the
        test profile's own boot-time default already resolves it to a concrete value
        ('mmu_exit'), so it has to be forced back to '' explicitly to exercise inheritance.
        """
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=40')
        self.assertIn('nfc_preload_clear_distance', str(cm.exception))

    def test_switching_preload_endstop_rechecks_a_stale_preload_clear_distance(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=40')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=mmu_shared_exit')
        self.assertIn('nfc_preload_clear_distance', str(cm.exception))
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.gate_preload_endstop, 'mmu_shared_exit')

    def test_switching_inherited_gate_endstop_rechecks_preload_clear_distance(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_preload_endstop=')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_exit')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=40')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=mmu_shared_exit')
        self.assertIn('nfc_preload_clear_distance', str(cm.exception))

    def test_independent_of_neighbor_evict_distance(self):
        """The three parameters must not interfere with each other's validation or value."""
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_neighbor_evict_distance=-10')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_gate_clear_distance=-40')
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 nfc_preload_clear_distance=-25')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance, -10.0)
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance, -40.0)
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance, -25.0)


class TestGateRfidPlumbing(NeighborTestCase):
    """MmuGateMaps.set_gate_rfid / find_gate_by_rfid, and the unconditional recording fix."""

    def test_set_and_find_round_trip(self):
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG)
        self.assertEqual(self.hh.mmu.gate_maps.find_gate_by_rfid(TAG), 2)

    def test_find_is_case_insensitive(self):
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG.lower())
        self.assertEqual(self.hh.mmu.gate_maps.find_gate_by_rfid(TAG.upper()), 2)

    def test_alias_identifies_second_tag_on_same_spool(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        verdict, owner, _diag = self.arbiter._field_verdict(2, alias.lower())
        self.assertEqual((verdict, owner), (NFC_FIELD_MINE, 2))

    def test_alias_identifies_tag_bleeding_from_neighbor(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        verdict, owner, _diag = self.arbiter._field_verdict(3, alias)
        self.assertEqual((verdict, owner), (NFC_FIELD_NEIGHBOR, 2))

    def test_alias_lookup_checks_left_neighbor_first(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(1, (alias,))
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(3, (alias,))
        self.assertEqual(self.hh.mmu.gate_maps.find_gate_by_rfid_alias(2, alias), 1)

    def test_own_alias_beats_neighbor_alias(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(1, (alias,))
        verdict, owner, _diag = self.arbiter._field_verdict(2, alias)
        self.assertEqual((verdict, owner), (NFC_FIELD_MINE, 2))

    def test_observed_rfid_wins_before_neighbor_rfids(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        self.hh.mmu.gate_maps.set_gate_rfid(3, alias)
        verdict, owner, _diag = self.arbiter._field_verdict(3, alias)
        self.assertEqual((verdict, owner), (NFC_FIELD_MINE, 3))

    def test_rfids_survive_spool_assignment_that_delivered_them(self):
        alias = 'BBBB1234'
        self.hh.run_gcode('MMU_GATE_MAP GATE=2 SPOOLID=77 RFID=%s RFIDS=%s,%s QUIET=1'
                          % (TAG, TAG, alias))
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], (TAG, alias))

    def test_rfids_are_recorded_under_spoolman_pull(self):
        alias = 'BBBB1234'
        self.hh.mmu.p.spoolman_support = SPOOLMAN_PULL
        self.hh.run_gcode('MMU_GATE_MAP GATE=2 SPOOLID=77 RFIDS=%s,%s QUIET=1'
                          % (TAG, alias))
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], (TAG, alias))

    def test_bulk_pull_applies_rfids(self):
        alias = 'BBBB1234'
        self.hh.mmu.p.spoolman_support = SPOOLMAN_PULL
        gate_map = {2: {'spool_id': 77, 'rfids': '%s,%s' % (TAG, alias)}}
        self.hh.run_gcode('MMU_GATE_MAP MAP="%s" REPLACE=1 FROM_SPOOLMAN=1 QUIET=1' % gate_map)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], (TAG, alias))

    def test_bulk_pull_clears_rfids_from_replaced_spool(self):
        self.hh.mmu.gate_maps.assign_spool_id(2, 77)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, 'BBBB1234'))
        self.hh.mmu.p.spoolman_support = SPOOLMAN_PULL
        gate_map = {2: {'spool_id': 99, 'rfids': 'CCCC9999'}}
        self.hh.run_gcode('MMU_GATE_MAP MAP="%s" REPLACE=1 FROM_SPOOLMAN=1 QUIET=1' % gate_map)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], ('CCCC9999',))
        self.assertIsNone(self.hh.mmu.gate_maps.find_gate_by_rfid_alias(3, 'BBBB1234'))

    def test_ui_map_without_rfids_preserves_cache(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.assign_spool_id(2, 77)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        gate_map = {2: {'spool_id': 77, 'name': 'PLA Basic', 'material': 'PLA', 'color': 'ff0000'}}
        self.hh.run_gcode('MMU_GATE_MAP MAP="%s" QUIET=1' % gate_map)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], (TAG, alias))

    def test_rfids_accept_comma_separated_string(self):
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, '%s, BBBB1234' % TAG)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], (TAG, 'BBBB1234'))

    def test_dev_field_report_shows_symbolic_verdict_and_rfids(self):
        alias = 'BBBB1234'
        self.hh.mmu.gate_maps.assign_spool_id(2, 77)
        self.hh.mmu.gate_maps.set_gate_rfid(2, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, alias))
        at = len(self.hh.console)
        self.hh.run_gcode('_MMU_TEST NFC_FIELD=1 GATE=2 UID=%s' % alias)
        report = '\n'.join(self.hh.console[at:])
        self.assertIn('-> MINE', report)
        self.assertIn('spool_id=77 rfid=%s rfids=%s,%s' % (TAG, TAG, alias), report)

    def test_aliases_clear_when_spool_assignment_changes(self):
        self.hh.mmu.gate_maps.assign_spool_id(2, 7)
        self.hh.mmu.gate_maps.set_gate_rfid_aliases(2, (TAG, 'BBBB1234'))
        self.hh.mmu.gate_maps.assign_spool_id(2, 8)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid_aliases[2], tuple())

    def test_unknown_uid_finds_nothing(self):
        self.assertIsNone(self.hh.mmu.gate_maps.find_gate_by_rfid('NOTAREALTAG'))

    def test_empty_rfid_is_a_noop(self):
        self.assertIsNone(self.hh.mmu.gate_maps.find_gate_by_rfid(''))
        self.assertIsNone(self.hh.mmu.gate_maps.find_gate_by_rfid(None))

    def test_uid_is_recorded_locally_even_under_spoolman_pull(self):
        """
        THE PREREQUISITE FIX. Before this, _apply_tag_to_gate returned early under
        SPOOLMAN_PULL (remote owns filament attributes there) without ever calling
        set_gate_rfid, so gate_spool_rfid stayed blank forever on a PULL-mode machine and
        find_gate_by_rfid could never answer "whose tag is this?" - silently disabling NFC
        neighbor-field arbitration entirely in that mode. _nfc_tag_read now calls
        set_gate_rfid unconditionally, before the spoolman_support branch.
        """
        self.hh.mmu.p.spoolman_support = SPOOLMAN_PULL
        self.hh.run_gcode('_MMU_TEST NFC_READ=1 GATE=0 UID=%s' % TAG)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG)
        self.assertEqual(self.hh.mmu.gate_maps.find_gate_by_rfid(TAG), 0)

    def test_a_repeat_read_of_the_same_uid_is_a_no_op(self):
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)
        before = self.hh.mmu.gate_maps.gate_spool_rfid[:]
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)  # same value again
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid, before)


class TestArbitrationEndToEnd(NeighborTestCase):
    """
    Real MMU_PRELOAD / MMU_NFC_SCAN runs through the arbiter, using the map/chip
    decoupling described at the top of this file to reach NEIGHBOR/FOREIGN classification
    without needing genuine cross-gate RF contamination.
    """

    def setUp(self):
        super().setUp()
        # Arm check-only mode; individual tests add eviction distance where needed.
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_check = 1

    def test_mine_registered_reads_normally(self):
        self.fil.attach_tag(0, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)
        self.hh.mmu.select_gate(0)
        self.preload(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertIn('tag read', ' '.join(self.hh.console).lower())

    def test_preload_only_clear_distance_does_not_arm_scan(self):
        """A preload-only option must leave MMU_NFC_SCAN's old zero-motion fast path alone."""
        u = self.hh.mmu.mmu_unit(0)
        u.p.nfc_neighbor_check = 0
        u.p.nfc_neighbor_evict_distance = 0.0
        u.p.nfc_gate_clear_distance = 0.0
        u.p.nfc_preload_clear_distance = -40.0
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)

        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG)
        self.assertEqual(self.fil.history, [],
                         'a preload-only clear distance must not force scan motion')

    def test_runtime_guard_skips_a_stale_out_of_reach_clear_jog(self):
        """Direct/stale state cannot bypass the physical reach guard in _ratify."""
        u = self.hh.mmu.mmu_unit(0)
        u.p.nfc_neighbor_check = 0
        u.p.nfc_neighbor_evict_distance = 0.0
        u.p.nfc_gate_clear_distance = -999.0  # bypass config validator deliberately
        self.hh.mmu.p.log_level = 4
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)

        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], '')
        self.assertNotIn('jogging', ' '.join(self.hh.console).lower(),
                         'an out-of-reach stale value must not execute a self-jog')

    def test_scan_raises_when_tag_belongs_to_an_unclearable_neighbor(self):
        """
        The physically-present tag is real (gate 0's own virtual chip, placed AT the reader
        so the pre-motion field-settle probe - which runs before any scan motion - actually
        sees it), but the gate map claims it belongs to gate 1, which has no filament
        (GATE_EMPTY) and so cannot be evicted. check-only mode has no motion budget at all,
        so this fast-fails: MMU_NFC_SCAN must refuse rather than silently attribute a tag it
        knows isn't gate 0's. run_gcode() reports a command failure via hh.errors rather than
        raising, hence checking that list instead of assertRaises.
        """
        self.fil.attach_tag(0, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid(1, TAG)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertTrue(
            any('could not be moved out of the way' in e for e in self.hh.errors),
            self.hh.errors)
        self.assertNotEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG,
                            "gate 0's map entry must not be set from a tag known not to be its own")

    def test_preload_degrades_to_plain_load_for_the_same_case(self):
        """
        Preload's handling of the identical FOREIGN verdict: drop the NFC leg, load anyway.
        The tag is placed AT the reader (not at mmu_exit, which would trip preload's
        "already preloaded" shortcut before arbitration is ever reached) so the field-settle
        probe genuinely sees it before the homing move starts.
        """
        self.fil.attach_tag(0, TAG)
        self.hh.mmu.gate_maps.set_gate_rfid(1, TAG)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.assertNotIn('with nfc scan', ' '.join(self.hh.console).lower(),
                         'a FOREIGN verdict must drop the NFC leg, not attempt it')
        self.assertNotEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG,
                            "gate 0's map entry must not be set from a tag known not to be its own")

    def test_provisional_verdict_is_ratified_when_the_tag_is_genuinely_this_gates_own(self):
        """
        Row 3 of the plan's state table: an unregistered tag, genuinely gate 0's own, placed
        AT the reader so the pre-motion field-settle probe finds it unregistered rather than
        CLEAR. With eviction armed there is nothing to evict (both neighbors are GATE_EMPTY),
        so the verdict is PROVISIONAL and the scan's own forced sweep is what actually
        confirms it - the fast path must be suppressed or there would be nothing to observe
        clearing once the filament re-parks away from the reader.
        """
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance = -40.0
        self.fil.attach_tag(0, TAG)  # deliberately NOT registered to any gate
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG)

    def test_provisional_attribution_is_never_committed_when_not_ratified_scan(self):
        """
        Row 6, made genuinely reachable end to end (not just as a unit test on _ratify):
        offset=-20 puts the tag exactly at the reader whenever gate 0's own filament is
        parked at its normal park position (park -100, reader -80, tag_pos = tip - offset).
        The sweep's own motion moves the tag out of range briefly (e.g. while homing to the
        gate datum at position 0), but the FINAL park brings it right back into range - by
        construction, indistinguishable from a stationary neighbour's tag that just happens
        to sit at this reader. This is the actual point of the deferred-commit fix: before
        it, the read taken mid-sweep would already have been committed to the gate map by
        the time this assertion runs, warning or no warning.

        nfc_gate_clear_distance is deliberately -10.0, not some larger value: the passive
        check fails (tag_pos=-80, dead center), and ratification now escalates to a self-jog
        of -10mm too (tip -110 -> tag_pos -90, |-90-(-80)|=10 <= the 15 tag_window - still
        detected). Kept small on purpose, so this test exercises "escalation was attempted
        and still correctly failed to clear", not "escalation never ran at all" - see
        test_provisional_verdict_is_ratified_via_self_jog_when_passive_check_fails_scan
        for the -40.0 case where the self-jog actually clears it. nfc_neighbor_evict_distance
        is deliberately left at its default 0 - proves self-jog escalation runs on its own,
        independent of neighbor eviction (see MmuNfcFieldArbiter._ratify).
        """
        self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance = -10.0
        self.fil.attach_tag(0, TAG, offset=-20.0)  # unregistered, and never truly clears
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)  # normal park position
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
        self.assertIsNotNone(self.fil.tag_detected(0), 'precondition: tag must be in range at rest')
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], '',
                         "a never-ratified provisional read must never be committed to the gate map")
        self.assertIn('no tag found', ' '.join(self.hh.console).lower())
        self.assertTrue(
            any('could not confirm this gate' in c for c in self.hh.console), self.hh.console)

    def test_provisional_attribution_is_never_committed_when_not_ratified_preload(self):
        """
        Preload's side of the same scenario: drops to 'no tag found' and never writes the
        gate map, same as the scan case above. Starts from the normal PARK position (not at
        the reader) - that keeps the per-gate exit sensor unTRIGGERED so preload takes its
        normal homing path rather than the "already preloaded" shortcut (which would bypass
        arbitration entirely), while still putting the tag in range from the very start (see
        the scan test's docstring for the offset/park/reader arithmetic, and for why
        nfc_preload_clear_distance is -10.0 here too - self-jog escalation must run and
        still fail, independent of neighbor eviction which stays at its default 0).
        """
        self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance = -10.0
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)  # normal park position, exit sensor not yet triggered
        self.assertIsNotNone(self.fil.tag_detected(0), 'precondition: tag must be in range at rest')
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE, 'filament still physically arrived')
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], '',
                         "a never-ratified provisional read must never be committed to the gate map")
        self.assertIn('no tag found', ' '.join(self.hh.console).lower())

    def test_provisional_verdict_is_ratified_via_self_jog_when_passive_check_fails_scan(self):
        """
        Same physical setup as the "never ratified" scan test above, but with the ORIGINAL
        -40.0 self-verify distance: the passive check fails exactly the same way
        (tag_pos=-80, dead center), but escalation's self-jog moves the tip an ADDITIONAL
        40mm (tip=-140 -> tag_pos=-120, |-120-(-80)|=40 > the 15 tag_window) - clear this
        time, so the provisional read IS ratified and committed. This is the motivating
        case from the real captured log this feature was built to fix: a genuinely-owned
        tag that the passive check alone would discard forever. nfc_neighbor_evict_distance
        is deliberately left at its default 0 here - proves self-jog ratification doesn't
        need neighbor eviction armed at all.
        """
        self.hh.mmu.p.log_level = 4  # so the self-jog's own log_debug lines are visible
        self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance = -40.0
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
        self.assertIsNotNone(self.fil.tag_detected(0), 'precondition: tag must be in range at rest')
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG,
                         "a self-jog-ratified provisional read must be committed to the gate map")
        console = ' '.join(self.hh.console).lower()
        self.assertIn('tag read', console)
        self.assertIn('ratified via a deliberate self-jog', console)

    def test_provisional_verdict_is_ratified_via_self_jog_when_passive_check_fails_preload(self):
        """Preload's side of the self-jog-succeeds scenario above."""
        self.hh.mmu.p.log_level = 4
        self.hh.mmu.mmu_unit(0).p.nfc_preload_clear_distance = -40.0
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)
        self.assertIsNotNone(self.fil.tag_detected(0), 'precondition: tag must be in range at rest')
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG,
                         "a self-jog-ratified provisional read must be committed to the gate map")
        console = ' '.join(self.hh.console).lower()
        self.assertIn('tag read', console)
        self.assertIn('ratified via a deliberate self-jog', console)

    def test_check_only_mode_spends_zero_extra_motion_on_a_never_ratified_read(self):
        """
        nfc_neighbor_check=1 with nfc_neighbor_evict_distance=0 and both nfc_gate_clear_distance
        and nfc_preload_clear_distance=0 (all default) must keep the "no motion budget at
        all" promise even with self-jog escalation added: _ratify's `if distance and ...`
        guard is false for distance=0, so it falls straight to the unchanged passive-only
        discard, exactly as before this feature existed. log_level=4 makes _jog_off's own
        "jogging ... off its park reference" debug line visible if it ran at all -
        asserting its absence is a direct check that neither neighbor eviction nor the
        new self-jog spent any motion.
        """
        self.hh.mmu.p.log_level = 4
        self.hh.mmu.mmu_unit(0).p.nfc_neighbor_evict_distance = 0.0
        self.hh.mmu.mmu_unit(0).p.nfc_gate_clear_distance = 0.0
        self.fil.attach_tag(0, TAG, offset=-20.0)
        self.hh.mmu.select_gate(0)
        self.hh.place_filament(0)
        self.hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
        self.assertIsNotNone(self.fil.tag_detected(0), 'precondition: tag must be in range at rest')
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], '',
                         "check-only mode must never commit an unconfirmed read")
        console = ' '.join(self.hh.console).lower()
        self.assertIn('no tag found', console)
        self.assertIn('could not confirm this gate', console)
        self.assertNotIn('jogging', console,
                        'check-only mode (both distances at 0) must spend zero motion')


class TestRatifyDoesNotSelfPoison(NeighborTestCase):
    """
    REGRESSION GUARD: _ratify() used to re-derive ownership via _field_check /
    find_gate_by_rfid - but by the time it runs, the read it's validating has already
    called set_gate_rfid(gate, uid), so that lookup would tautologically resolve back to
    'gate' every time and _ratify would report "ratified" no matter what. Unlike genuine
    cross-gate RF contamination, this is fully reachable with gate 0's own (isolated)
    virtual chip: _ratify only cares whether gate 0's own reader still reports a tag, not
    where that tag physically comes from.
    """

    def test_still_present_after_attribution_is_not_ratified(self):
        # _ratify logs via log_warning -> respond_info -> console, NOT errors (that's
        # log_error/log_assertion only, via respond_raw('!! ...')). hh.console/hh.errors are
        # properties returning a fresh copy each call, so clear the real underlying list on
        # hh.gcode directly rather than the copy.
        mgr = self.hh.mmu.mmu_unit(0).nfc_manager
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc']) # Tag stays in range
        # Simulate the read _ratify is meant to validate having just run: the gate map
        # already attributes this exact UID to gate 0, same as _nfc_tag_read would leave it.
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)
        self.hh.gcode.console.clear()
        self.assertFalse(self.arbiter._ratify(0, mgr, None))
        self.assertTrue(
            any('could not confirm this gate' in c for c in self.hh.console), self.hh.console)

    def test_a_different_uid_still_present_is_also_not_ratified(self):
        """The fix must not special-case "same UID as just attributed" vs "a different one" -
        either way, something is still in the field and that alone must not ratify."""
        mgr = self.hh.mmu.mmu_unit(0).nfc_manager
        self.fil.attach_tag(0, 'AABBCCDD')
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG) # Map says something else entirely
        self.hh.gcode.console.clear()
        self.assertFalse(self.arbiter._ratify(0, mgr, None))
        self.assertTrue(
            any('could not confirm this gate' in c for c in self.hh.console), self.hh.console)

    def test_genuinely_clear_field_is_still_ratified(self):
        """Sanity check the fix didn't break the success path: nothing in the field at all
        (the tag moved away, or there was never a reader) is still ratified with no warning."""
        mgr = self.hh.mmu.mmu_unit(0).nfc_manager
        self.hh.mmu.gate_maps.set_gate_rfid(0, TAG)
        self.hh.gcode.console.clear()
        self.assertTrue(self.arbiter._ratify(0, mgr, None))
        self.assertFalse(
            any('could not confirm this gate' in c for c in self.hh.console), self.hh.console)


if __name__ == '__main__':
    unittest.main()
