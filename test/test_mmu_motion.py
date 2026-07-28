# Happy Hare test harness - milestone C: filament motion sequencing.
#
# Exercises HH's load/unload/preload CHOREOGRAPHY against the 1-D filament path model
# (test/hh/filament.py). There is no acceleration, step generation or drip pacing -
# what is under test is the sequencing and the position arithmetic, not Klipper's
# motion planner.
#
# Two integration points make this work, both in the fake layer with no monkeypatching
# of Happy Hare:
#   - HOMING moves: the fake HomingMove asks the model which endstop trips first and
#     how far the move gets (test/hh/klippy_root/extras/homing.py).
#   - PLAIN moves (notably the final park): observed via the fake motion_queuing's
#     trapq_append, from which the signed distance is exactly recoverable.
#
# Geometry is the model's DEFAULT_LAYOUT: park -100, entry -50, gate/exit 0. The entry
# switch sits BETWEEN the park point and the gate sensor, so a parked filament leaves it
# clear - which is what Happy Hare requires (its preload failure tail marks a gate
# GATE_UNKNOWN when the entry switch is still covered afterwards).
#
# REALISTIC STARTING POSITIONS MATTER. A gate can only be preloaded from a state where
# the filament is ALREADY past the entry switch, because that is what a user pushing
# filament in produces (and the push is what fires the insert that triggers preload).
# Starting behind the entry switch and then preloading manufactures an entry-switch edge
# DURING the operation, which fires a nested insert-driven preload - the insert handler
# guards only on `not is_printing()` (extras/mmu/commands/mmu_sensor_insert.py:70-75),
# not on being mid-operation. That nesting is an artefact of an impossible start state,
# not something to test.
#
#   ./venv/bin/python -m unittest test.test_mmu_motion
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.filament import TIP_PARKED, TIP_PRESENTED

# Past the entry switch (-50): the only state a preload can realistically start from
TIP_AT_GATE = -40.0

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_HOMED_GATE = 1
GATE_EMPTY = 0
GATE_AVAILABLE = 1


class MotionTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()

    def tearDown(self):
        self.hh.close()


class TestModelItself(MotionTestCase):
    """The model is simple, but wrong geometry would invalidate everything below."""

    def test_sensor_triggers_when_tip_reaches_it(self):
        entry = self.fil.layout['mmu_entry']
        self.hh.place_filament(0, position=entry - 10.0)
        self.assertFalse(self.fil.triggered('mmu_entry_0'))
        self.hh.place_filament(0, position=entry + 1.0)
        self.assertTrue(self.fil.triggered('mmu_entry_0'))
        self.assertFalse(self.fil.triggered('mmu_exit_0'))

    def test_parked_state_is_coherent(self):
        """
        Parked at -100 both switches are clear. That is required, not incidental: HH
        marks a gate GATE_UNKNOWN if preload finishes with the entry switch covered,
        so a layout where parking leaves it triggered makes every preload "fail".
        """
        self.hh.place_filament(0)
        self.assertEqual(self.fil.tip[0], TIP_PARKED)
        self.assertLess(TIP_PARKED, self.fil.layout['mmu_entry'])
        self.assertFalse(self.hh.sensor('mmu_entry_0').present)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)

    def test_one_gate_does_not_affect_another(self):
        self.hh.place_filament(0)
        self.assertFalse(self.hh.sensor('mmu_entry_1').present)
        self.assertFalse(self.hh.sensor('mmu_entry_2').present)

    def test_trip_distance_is_direction_aware(self):
        self.hh.place_filament(0)                   # tip at -100
        trip = self.fil.trip_distance(0, 300., ['mmu_exit_0'])
        self.assertEqual(trip[0], 'mmu_exit_0')
        self.assertAlmostEqual(trip[1], 100.0)      # -100 -> 0
        # Retracting, the exit switch is already clear so it cannot trip again
        self.assertIsNone(self.fil.trip_distance(0, -300., ['mmu_exit_0']))

    def test_trip_distance_respects_move_length(self):
        self.hh.place_filament(0)
        self.assertIsNone(self.fil.trip_distance(0, 50., ['mmu_exit_0']),
                          'a 50mm move cannot reach a sensor 100mm away')


class TestQuietPlacement(MotionTestCase):
    """
    Placing filament is a real event, and the harness has to be explicit about it:
    tripping the entry switch is an INSERT, which HH answers by preloading the gate.
    """

    def test_quiet_placement_does_not_move_filament(self):
        self.hh.place_filament(0)                   # quiet=True by default
        self.assertEqual(self.fil.history, [],
                         'quiet placement must not provoke any HH motion')
        self.assertEqual(self.fil.tip[0], TIP_PARKED)

    def test_placement_short_of_the_entry_switch_is_inert(self):
        """
        No switch changes state, so there is no event for HH to react to - loud or not.
        TIP_PRESENTED (-60) sits behind the entry switch at -50.
        """
        self.hh.place_filament(0, position=TIP_PRESENTED, quiet=False)
        self.hh.settle()
        self.assertFalse(self.hh.sensor('mmu_entry_0').present)
        self.assertEqual(self.fil.history, [])

    def test_loud_placement_past_the_entry_switch_preloads_the_gate(self):
        """
        Covering the entry switch IS an insert event, and HH answers it by running
        MMU_PRELOAD for that gate (extras/mmu/commands/mmu_sensor_insert.py:70-75) -
        which is why scenario setup defaults to quiet.

        There is ONE preload implementation, _preload_gate; the insert route reaches it
        through the ordinary MMU_PRELOAD command. It homes to the gate and parks, so the
        entry switch it started from ends up CLEAR again.
        """
        self.hh.place_filament(0, position=TIP_AT_GATE, quiet=False)
        self.hh.settle()
        homed = [r for _g, _d, r in self.fil.history if 'mmu_exit_0' in r]
        self.assertTrue(homed, 'expected a homing move to the gate sensor')
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=2)
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.assertFalse(self.hh.sensor('mmu_entry_0').present,
                         'preload parks behind the entry switch, clearing it')
        self.assertEqual(self.hh.errors, [])


class TestLoadGate(MotionTestCase):

    def setUp(self):
        super().setUp()
        self.hh.place_filament(0)
        self.hh.mmu.select_gate(0)

    def test_homes_forward_to_the_gate_sensor(self):
        """
        BoxTurtle's gate_homing_endstop is mmu_exit at 0, so _load_gate drives forward
        from the park position until that switch trips - exactly 100mm.

        It also PARKS on the way out (a gate_parking_distance retraction), so the
        filament ends back where it started with the gate sensor confirmed. That makes
        _unload_gate not its inverse - _unload_gate unloads from the bowden.
        """
        overshoot = self.hh.mmu._load_gate()
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_HOMED_GATE)
        self.assertEqual(overshoot, 0.0)
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=2)
        self.assertEqual(self.hh.errors, [])

    def test_the_gate_sensor_was_tripped_on_the_way(self):
        """
        The switch reads clear at the end (parked behind it), so assert the SEQUENCE:
        a forward homing move of exactly 100mm that tripped mmu_exit_0.
        """
        self.hh.mmu._load_gate()
        trips = [d for _g, d, reason in self.fil.history
                 if 'homing -> mmu_exit_0' in reason and d > 0]
        self.assertTrue(trips, 'never homed forward onto the gate sensor')
        self.assertAlmostEqual(trips[0], 100.0, places=3)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)

    def test_second_home_against_an_already_triggered_switch_is_free(self):
        """
        _load_gate issues a confirming forward home after the first trip. With the
        switch already closed that must complete with ZERO travel, exactly as real
        hardware does - otherwise the model would shove another 200mm of filament
        into the bowden.
        """
        self.hh.mmu._load_gate()
        forward = [d for _g, d, reason in self.fil.history
                   if d > 0 and 'homing' in reason]
        self.assertEqual(len(forward), 1,
                         'expected exactly one forward homing advance, got %r' % forward)

    def test_marks_the_gate_available(self):
        self.hh.mmu._load_gate()
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)

    def test_no_filament_fails_and_marks_the_gate_empty(self):
        """
        With nothing to find, homing runs the full gate_homing_max and no switch
        trips. HH must report failure rather than believing it succeeded.
        """
        self.hh.place_filament(0, position=-100000.0)
        with self.assertRaises(Exception):
            self.hh.mmu._load_gate()
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)


class TestParkedState(MotionTestCase):
    """
    The park position is what every other sequence starts and ends at, so pin its
    sensor signature down.

    _unload_gate is deliberately NOT tested here: it unloads from the BOWDEN back to
    the gate, so exercising it needs a bowden load first. That is the next increment,
    along with a model-driven virtual NFC reader (see TestNfcEndstopTrips).
    """

    def setUp(self):
        super().setUp()
        self.hh.place_filament(0)
        self.hh.mmu.select_gate(0)
        self.hh.mmu._load_gate()

    def test_gate_sensor_is_clear_after_the_sequence(self):
        """
        However far the park retracts, it must end behind the gate switch - HH relies
        on that switch being open to detect the next insert.
        """
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        self.assertEqual(self.hh.errors, [])

    def test_hh_agrees_the_gate_is_loaded(self):
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_HOMED_GATE)
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)


class TestPreload(MotionTestCase):
    """MMU_PRELOAD as a user would run it - the full command, not an internal."""

    def test_preload_ends_parked_at_the_configured_distance(self):
        self.hh.place_filament(1, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        self.assertEqual(self.hh.mmu.gate_status[1], GATE_AVAILABLE)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertAlmostEqual(self.fil.tip[1], TIP_PARKED, places=2)
        self.assertFalse(self.hh.sensor('mmu_exit_1').present)
        self.assertFalse(self.hh.sensor('mmu_entry_1').present)
        self.assertEqual(self.hh.errors, [])

    def test_insert_driven_preload_ends_parked(self):
        """
        The realistic route: a user pushes filament past the entry switch, which fires
        an insert, which runs MMU_PRELOAD (mmu_sensor_insert.py:74). End state must be
        identical to the explicit command.
        """
        self.hh.place_filament(2, position=TIP_AT_GATE, quiet=False)
        self.hh.settle()
        self.assertAlmostEqual(self.fil.tip[2], TIP_PARKED, places=2)
        self.assertEqual(self.hh.mmu.gate_status[2], GATE_AVAILABLE)
        self.assertEqual(self.hh.errors, [])

    def test_preload_passes_the_gate_sensor_on_the_way(self):  # noqa: D401
        """
        Preload homes forward to the gate endstop and then retracts to park, so the
        exit switch must have been tripped mid-sequence even though it reads clear at
        the end. Confirms the sequence rather than just the endpoint.
        """
        self.hh.place_filament(1, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        reached = [d for gate, d, reason in self.fil.history
                   if gate == 1 and 'mmu_exit_1' in reason]
        self.assertTrue(reached, 'preload never homed to the gate sensor')
        self.assertFalse(self.hh.sensor('mmu_exit_1').present)

    def test_preload_with_no_filament_reports_empty(self):
        self.hh.place_filament(2, position=-100000.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.assertEqual(self.hh.mmu.gate_status[2], GATE_EMPTY)

    def test_preload_leaves_other_gates_alone(self):
        self.hh.place_filament(0)
        self.hh.place_filament(1, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=3)


class TestNfcEndstopTrips(unittest.TestCase):
    """
    The per-gate NFC reader doubles as a homing endstop. Here the model decides when
    the tag enters the read window, so this is the first exercise of MmuNfcEndstop's
    trigger path in a homing move.

    Scope note: this covers the ENDSTOP trip. It does not cover read_gate() returning
    the UID - that reads the real chip driver, which the harness currently answers only
    with a scripted RC522 init. A model-driven virtual reader is the next step, and
    until it exists the full MMU_NFC_SCAN jog cannot be asserted end to end.
    """

    def setUp(self):
        self.hh = session('nfc_per_gate')
        self.hh.boot()
        self.fil = self.hh.filament()

    def tearDown(self):
        self.hh.close()

    def test_tag_is_detected_inside_the_read_window(self):
        self.fil.attach_tag(0, '04A1B2C3', {'material': 'PLA'})
        reader = self.fil.layout['mmu_nfc']
        self.hh.place_filament(0, position=reader)          # tag right on the reader
        self.assertIsNotNone(self.fil.tag_detected(0))
        self.hh.place_filament(0, position=reader - 100.0)  # far away
        self.assertIsNone(self.fil.tag_detected(0))

    def test_forward_jog_reaches_the_tag_at_the_window_edge(self):
        """
        Distance to detection is the gap to the NEAR edge of the window, so a tag
        becomes readable slightly before it is centred on the reader.
        """
        self.fil.attach_tag(0, '04A1B2C3')
        self.hh.place_filament(0)                            # tip -100
        reader = self.fil.layout['mmu_nfc']                  # -60
        expected = (reader - self.fil.tag_window) - self.fil.tip[0]
        distance = self.fil.nfc_trip_distance(0, 300.)
        self.assertAlmostEqual(distance, expected, places=3)

    def test_no_tag_means_no_trip(self):
        self.hh.place_filament(0)
        self.assertIsNone(self.fil.nfc_trip_distance(0, 300.),
                          'a gate with no tag must never trip the NFC endstop')

    def test_nfc_endstop_wins_when_closer_than_the_gate_switch(self):
        """
        The whole point of the first-wins compound: from the park position the tag
        (window edge at -75) is nearer than the gate switch (0), so a forward homing
        move must stop on the NFC endstop.
        """
        self.fil.attach_tag(0, '04A1B2C3')
        self.hh.place_filament(0)
        nfc = self.fil.nfc_trip_distance(0, 300.)
        switch = self.fil.trip_distance(0, 300., ['mmu_exit_0'])
        self.assertIsNotNone(nfc)
        self.assertIsNotNone(switch)
        self.assertLess(nfc, switch[1])


if __name__ == '__main__':
    unittest.main()
