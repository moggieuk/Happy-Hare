# Happy Hare test harness - milestone C: filament motion sequencing.
#
# Exercises HH's load/unload/preload CHOREOGRAPHY against the 1-D filament path model
# (test/hh/filament.py). There is no acceleration, step generation or drip pacing -
# what is under test is the sequencing and the position arithmetic, not Klipper's
# motion planner.
#
# Three integration points make this work, all in the fake layer with no monkeypatching
# of Happy Hare:
#   - HOMING moves: the fake HomingMove asks the model which endstop trips first and
#     how far the move gets (test/hh/klippy_root/extras/homing.py).
#   - PLAIN moves (notably the final park): observed via the fake motion_queuing's
#     trapq_append, from which the signed distance is exactly recoverable.
#   - TOOLHEAD extrusion: its E-axis delta advances filament only while the MMU gear is
#     synchronized to the extruder, matching print-time movement.
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
        Parked at -100 the GATE switch is clear and the ENTRY switch is covered. Required,
        not incidental: the entry switch is upstream of the gear, so a filament the gear
        can still grip necessarily runs back through it to the spool. HH depends on that -
        validate_gate_status() forces a non-EMPTY gate to GATE_EMPTY when entry reads clear
        (mmu_gate_maps.py:228-229), which a layout with entry between park and the gate
        would trigger for every parked lane.
        """
        self.hh.place_filament(0)
        self.assertEqual(self.fil.tip[0], TIP_PARKED)
        self.assertGreater(TIP_PARKED, self.fil.layout['mmu_entry'])
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)

    def test_a_parked_gate_survives_validate_gate_status(self):
        """
        The geometry check with teeth. validate_gate_status() runs at bootup
        (mmu_controller.py:363) and in reset_gate_map, and reads the entry switch to
        'correct' the gate map. Under the old inverted layout a successful preload left
        entry clear, so this demoted the gate it had just marked AVAILABLE to EMPTY.
        """
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.hh.mmu.gate_maps.validate_gate_status()
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE,
                         'a parked, preloaded gate must survive sensor validation')

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

    def test_tension_sprung_buffer_uses_configured_travel(self):
        """Only contact during extruder homing can build compression."""
        buffer = self.hh.mmu.mmu_machine.units[0].buffer
        entry = self.fil.layout['extruder_entry']
        compression = entry + buffer.buffer_maxrange * 0.7

        self.assertAlmostEqual(self.fil.position('filament_compression'), compression)

        self.hh.place_filament(0, position=entry - 1.)
        self.assertTrue(self.hh.sensor('filament_tension').present)
        self.assertFalse(self.hh.sensor('filament_compression').present)

        self.hh.place_filament(0, position=entry)
        self.assertFalse(self.hh.sensor('filament_tension').present)
        self.assertFalse(self.hh.sensor('filament_compression').present)

        # A calibrated Bowden move can overshoot the nominal coordinate without
        # proving that the filament has contacted the extruder. It must not squeeze
        # the buffer merely because the model's absolute tip position passed entry.
        self.fil.advance(0, buffer.buffer_maxrange, 'move')
        self.assertFalse(self.hh.sensor('filament_tension').present)
        self.assertFalse(self.hh.sensor('filament_compression').present)

        trip = self.fil.trip_distance(
            0, buffer.buffer_maxrange,
            ['unit0:filament_compression'],
        )
        self.assertAlmostEqual(trip[1], buffer.buffer_maxrange * 0.7)
        self.fil.advance(0, trip[1], 'homing -> unit0:filament_compression')
        self.assertFalse(self.hh.sensor('filament_tension').present)
        self.assertTrue(self.hh.sensor('filament_compression').present)


class TestGateMapCommand(MotionTestCase):

    def test_reset_requires_a_gate_target(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 MATERIAL=PLA')
        with self.assertRaisesRegex(Exception, 'requires GATE or GATES'):
            self.hh.run_gcode('MMU_GATE_MAP RESET=1')
        self.assertEqual(self.hh.mmu.gate_material[1], 'PLA')

    def test_reset_only_restores_the_requested_gates(self):
        mmu = self.hh.mmu
        mmu.p.default_gate_material[0] = 'ABS'
        mmu.p.default_gate_material[2] = 'PETG'
        self.hh.run_gcode('MMU_GATE_MAP GATES=0,1,2 MATERIAL=PLA')

        self.hh.run_gcode('MMU_GATE_MAP RESET=1 GATES=0,2')

        self.assertEqual(mmu.gate_material[0], 'ABS')
        self.assertEqual(mmu.gate_material[1], 'PLA')
        self.assertEqual(mmu.gate_material[2], 'PETG')

    def test_empty_transition_clears_all_gate_attributes(self):
        mmu = self.hh.mmu
        self.hh.run_gcode(
            'MMU_GATE_MAP GATE=1 AVAILABLE=1 NAME=Basic MATERIAL=PLA '
            'VENDOR=Maker COLOR=ff0000 TEMP=230 SPEED=50 SPOOLID=7 RFID=abc123'
        )

        self.hh.run_gcode('MMU_GATE_MAP GATE=1 AVAILABLE=0')

        self.assertEqual(mmu.gate_filament_name[1], '')
        self.assertEqual(mmu.gate_material[1], '')
        self.assertEqual(mmu.gate_vendor[1], '')
        self.assertEqual(mmu.gate_color[1], '')
        self.assertEqual(mmu.gate_temperature[1], int(mmu.p.default_extruder_temp))
        self.assertEqual(mmu.gate_speed_override[1], 100)
        self.assertEqual(mmu.gate_spool_id[1], -1)
        self.assertEqual(mmu.gate_spool_rfid[1], '')

        # Clearing is transition-based: metadata may intentionally be added later
        # without first changing the gate away from EMPTY.
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 MATERIAL=PETG RFID=deadbeef')
        self.assertEqual(mmu.gate_material[1], 'PETG')
        self.assertEqual(mmu.gate_spool_rfid[1], 'DEADBEEF')

    def test_gate_map_rejects_invalid_or_multiple_rfid_values(self):
        mmu = self.hh.mmu
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 RFID=aabbccdd QUIET=1')
        self.assertEqual(mmu.gate_spool_rfid[1], 'AABBCCDD')

        self.hh.run_gcode('MMU_GATE_MAP GATE=1 RFID=11111111,22222222 QUIET=1')
        self.assertEqual(mmu.gate_spool_rfid[1], 'AABBCCDD')

        self.hh.run_gcode('MMU_GATE_MAP GATE=1 RFID=not-hex QUIET=1')
        self.assertEqual(mmu.gate_spool_rfid[1], 'AABBCCDD')

    def test_spoolman_map_preserves_observed_uid_and_applies_aliases(self):
        mmu = self.hh.mmu
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 RFID=aabbccdd QUIET=1')
        gate_map = {
            1: {'spool_id': 7, 'rfids': 'AABBCCDD,BBBB1234'}
        }
        self.hh.run_gcode('MMU_GATE_MAP MAP="%s" FROM_SPOOLMAN=1 QUIET=1' % gate_map)

        self.assertEqual(mmu.gate_spool_rfid[1], 'AABBCCDD')
        self.assertEqual(mmu.gate_maps.gate_spool_rfid_aliases[1],
                         ('AABBCCDD', 'BBBB1234'))

    def test_gate_map_details_reports_rfid_aliases(self):
        mmu = self.hh.mmu
        mmu.gate_maps.set_gate_rfid_aliases(1, ('AABBCCDD', 'BBBB1234'))

        at = len(self.hh.console)
        self.hh.run_gcode('MMU_GATE_MAP DETAILS=1')
        report = '\n'.join(self.hh.console[at:])

        self.assertIn('[RFIDS: AABBCCDD,BBBB1234]', report)
        self.assertIn('[RFIDS: none]', report)
        self.assertNotIn('[RFIDS:', mmu.gate_maps.gate_map_to_string())

    def test_runtime_empty_transition_also_clears_attributes(self):
        mmu = self.hh.mmu
        self.hh.run_gcode(
            'MMU_GATE_MAP GATE=2 AVAILABLE=1 NAME=Basic MATERIAL=PLA '
            'SPOOLID=8 RFID=abc123'
        )

        mmu.gate_maps.set_gate_status(2, GATE_EMPTY)

        self.assertEqual(mmu.gate_filament_name[2], '')
        self.assertEqual(mmu.gate_material[2], '')
        self.assertEqual(mmu.gate_spool_id[2], -1)
        self.assertEqual(mmu.gate_spool_rfid[2], '')

    def test_help_shows_targeted_reset_example(self):
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_GATE_MAP HELP=1')
        help_text = '\n'.join(self.hh.console[at:])
        self.assertIn('RESET=1 GATES=4,5', help_text)

class TestEveryDriveModeMovesFilament(MotionTestCase):
    """
    Happy Hare drives filament in four sync modes (mmu_constants.py:169-172), and a plain move
    in ANY of them physically moves filament - so the model has to follow in all four.

    Two of them used to move nothing. _on_manual_move watched the GEAR stepper's trapq, but in
    'extruder' and 'synced' modes the gear is not what moves; the whole move was dropped and
    the filament silently stayed put. The consequence was not subtle once a machine had an
    encoder: no model movement means no encoder pulses, and HH concludes the filament is stuck.

    The gear filter was there for a real reason - a gear+extruder move appends to BOTH trapqs
    for ONE physical movement - so this asserts the exact distance, not just "moved", which is
    what would catch a regression to double counting.
    """

    MOVE = 20.0

    def setUp(self):
        super().setUp()
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.hh.heat_extruder(220)
        self.hh.mmu.select_gate(0)

    def _moved(self, motor):
        before = self.fil.tip[0]
        self.hh.run_gcode('MMU_TEST_MOVE MOVE=%.1f MOTOR=%s' % (self.MOVE, motor))
        self.assertEqual(self.hh.errors, [], 'MOTOR=%s errored' % motor)
        return self.fil.tip[0] - before

    def test_gear(self):
        self.assertAlmostEqual(self._moved('gear'), self.MOVE, places=3)

    def test_gear_plus_extruder(self):
        """Both trapqs see this one move; the model must advance ONCE."""
        self.assertAlmostEqual(self._moved('gear+extruder'), self.MOVE, places=3)

    def test_extruder_only(self):
        self.assertAlmostEqual(self._moved('extruder'), self.MOVE, places=3)

    def test_gear_synced_to_extruder(self):
        self.assertAlmostEqual(self._moved('synced'), self.MOVE, places=3)

    def test_unsynced_toolhead_extrusion_does_not_move_the_model(self):
        before = self.fil.tip[0]
        pos = self.hh.mmu.toolhead.get_position()
        pos[3] += self.MOVE
        self.hh.mmu.toolhead.move(pos, 25.)
        self.assertAlmostEqual(self.fil.tip[0] - before, 0., places=3)


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
        TIP_PRESENTED (-180) sits behind the entry switch at -150.
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
        through the ordinary MMU_PRELOAD command. It homes to the gate and parks - and the
        entry switch STAYS covered throughout, because every move it makes happens
        downstream of the gear. The user's push is the only thing that ever crosses it.
        """
        self.hh.place_filament(0, position=TIP_AT_GATE, quiet=False)
        self.hh.settle()
        homed = [r for _g, _d, r in self.fil.history if 'mmu_exit_0' in r]
        self.assertTrue(homed, 'expected a homing move to the gate sensor')
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=2)
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.assertTrue(self.hh.sensor('mmu_entry_0').present,
                        'the gear is downstream of the entry switch - preload cannot clear it')
        self.assertEqual(self.hh.errors, [])

    def test_the_preload_does_not_re_trigger_itself(self):
        """
        The duplicate-preload symptom, pinned at its root. An MMU move cannot cross the
        entry switch, so a preload raises no second insert event and runs exactly once.
        """
        at = len(self.hh.console)
        self.hh.place_filament(0, position=TIP_AT_GATE, quiet=False)
        self.hh.settle()
        banners = [l for l in self.hh.console[at:] if l.startswith('Preloading')]
        self.assertEqual(banners, ['Preloading gate 0...'])


class TestLoadGate(MotionTestCase):

    def setUp(self):
        super().setUp()
        self.hh.place_filament(0)
        self.hh.mmu.select_gate(0)

    def test_homes_forward_to_the_gate_sensor(self):
        """
        BoxTurtle's gate_homing_endstop is mmu_exit at 0, so _load_gate drives forward
        from the park position until that switch trips - exactly 100mm.

        It does NOT park: _load_gate leaves the filament standing ON the gate, which is
        the starting position the bowden load expects. (It used to appear to park here,
        but that was a nested MMU_PRELOAD fired by a phantom insert event off the old
        inverted entry-switch geometry - not _load_gate's doing at all.)
        """
        overshoot = self.hh.mmu._load_gate()
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_HOMED_GATE)
        self.assertEqual(overshoot, 0.0)
        self.assertAlmostEqual(self.fil.tip[0], self.fil.layout['mmu_exit'], places=2)
        self.assertEqual(self.hh.errors, [])

    def test_the_gate_sensor_was_tripped_on_the_way(self):
        """A forward homing move of exactly 100mm, from park to the gate switch."""
        self.hh.mmu._load_gate()
        trips = [d for _g, d, reason in self.fil.history
                 if 'homing -> mmu_exit_0' in reason and d > 0]
        self.assertTrue(trips, 'never homed forward onto the gate sensor')
        self.assertAlmostEqual(trips[0], 100.0, places=3)
        self.assertTrue(self.hh.sensor('mmu_exit_0').present,
                        '_load_gate leaves the filament on the gate switch')

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
    sensor signature down. _load_gate homes to the gate; _park_from_gate is what takes
    it back to the park, so the pair is exercised here rather than _load_gate alone.
    """

    def setUp(self):
        super().setUp()
        self.hh.place_filament(0)
        self.hh.mmu.select_gate(0)
        self.hh.mmu._load_gate()

    def test_the_gate_switch_is_reached_by_the_load(self):
        self.assertTrue(self.hh.sensor('mmu_exit_0').present)
        self.assertEqual(self.hh.errors, [])

    def test_parking_afterwards_clears_the_gate_switch(self):
        """
        However far the park retracts it must end behind the gate switch - HH relies on
        that switch being open to tell the next load the gate is free. The ENTRY switch
        stays covered the whole time; the park never reaches it.
        """
        mmu = self.hh.mmu
        mmu._park_from_gate(mmu._gate_profile())
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=2)
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
        self.assertTrue(self.hh.sensor('mmu_entry_1').present,
                        'a parked filament still runs back through the entry switch')
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

    def test_plain_preload_announces_itself_once_and_without_nfc(self):
        """
        One banner, from _preload_gate, naming the gate. The command used to log its own
        "Preloading filament in gate 1..." first, so the user saw two. BoxTurtle has no
        per-gate NFC reader, so the banner must NOT claim a scan.
        """
        self.hh.place_filament(1, position=TIP_AT_GATE)
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        banners = [l for l in self.hh.console[at:] if l.startswith('Preloading')]
        self.assertEqual(banners, ['Preloading gate 1...'])

    def test_plain_preload_parks_without_a_reverse_home(self):
        """
        Preload homes FORWARD onto the gate endstop, so it is already standing on the
        datum: the park is a single retraction. It used to borrow _unload_gate(), whose
        reverse-home leg was a pointless round trip from that position.
        """
        self.hh.place_filament(1, position=TIP_AT_GATE)
        at = len(self.fil.history)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        legs = [(d, r) for gate, d, r in self.fil.history[at:] if gate == 1]
        homing = [(d, r) for d, r in legs if 'homing' in r]
        self.assertEqual(len(homing), 1,
                         'expected exactly one homing leg, got %r' % (homing,))
        self.assertGreater(homing[0][0], 0, 'the one homing leg must be forward')

    def test_preload_with_no_filament_reports_empty(self):
        self.hh.place_filament(2, position=-100000.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.assertEqual(self.hh.mmu.gate_status[2], GATE_EMPTY)

    def test_eject_announces_itself_once_and_names_the_gate(self):
        """
        Same shape as the preload banner, and for the same reason: MMU_EJECT used to log
        its own "Ejecting filament out of gate 1" and then _eject_from_gate logged a bare
        "Ejecting...", so the user saw two lines and neither pairing was obvious.
        """
        self.hh.place_filament(1, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_EJECT GATE=1')
        banners = [l for l in self.hh.console[at:] if l.lower().startswith('ejecting')]
        self.assertEqual(banners, ['Ejecting gate 1...'])
        self.assertEqual(self.hh.mmu.gate_status[1], GATE_EMPTY)

    def test_a_failed_preload_keeps_an_assigned_spool_id(self):
        """
        The pending spool is assigned while this gate is already EMPTY, so a failed
        preload does not constitute a transition into EMPTY and must not clear it.
        """
        mmu = self.hh.mmu
        mmu.pending_spool_id = 7
        mmu._check_pending_filament(2)
        self.assertEqual(mmu.gate_maps.gate_spool_id[2], 7)

        self.hh.place_filament(2, position=-100000.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.assertEqual(mmu.gate_status[2], GATE_EMPTY)
        self.assertEqual(mmu.gate_maps.gate_spool_id[2], 7)

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
