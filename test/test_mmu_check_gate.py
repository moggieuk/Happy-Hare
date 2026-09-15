# Regression tests for safe pre-print gate checks and EndlessSpool substitution.
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
import itertools
from unittest.mock import patch
from test.hh import session, profiles
from test.hh.bootstrap import install

install()   # Fake klippy root on sys.path before importing MMU modules

from extras.mmu.mmu_constants import (
    OCCUPANCY_PRESENT, OCCUPANCY_EMPTY, OCCUPANCY_UNKNOWN)
from test.test_mmu_endless_spool import EndlessSpoolTestCase

# Encoder gate homing with NO per-gate switches - the classic ERCF shape, and the only
# configuration where the encoder is both the gate detector AND the sole source of
# "did filament move". Needed because an entry switch makes gate_occupancy() EMPTY or
# PRESENT, never UNKNOWN, so BoxTurtle's switches leave this path unreachable.
SENSORLESS_ENCODER = profiles.get('encoder').derive(
    'sensorless_encoder',
    syms={'MMU_HAS_SENSOR_ENTRY': False, 'MMU_HAS_SENSOR_EXIT': False},
    description='encoder gate homing, no per-gate switches')


class TestCheckGateSafety(EndlessSpoolTestCase):
    GROUPS = '1,1,2,2'

    def setUp(self):
        super().setUp()
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1')
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL=PETG COLOR=abcdef TEMP=240 SPOOLID=42 QUIET=1')

    def assert_metadata_preserved(self):
        self.assertEqual(self.hh.mmu.gate_material[0], 'PETG')
        self.assertEqual(self.hh.mmu.gate_color[0], 'abcdef')
        self.assertEqual(self.hh.mmu.gate_temperature[0], 240)
        self.assertEqual(self.hh.mmu.gate_spool_id[0], 42)

    def remove(self, gate):
        with self.hh.quiet_sensors():
            self.fil.remove(gate)
        self.hh.settle()

    def test_failed_homing_with_filament_in_path_must_stop(self):
        # A missed endstop after forward motion must not authorize feeding another lane.
        original_trip = self.fil.trip_distance
        original_triggered = self.fil.triggered

        def trip(gate, delta, names, sought=True):
            if gate == 0 and any(n.endswith('mmu_exit_0') for n in names):
                return None  # gate 0 exit switch fails to trip; motion still runs
            return original_trip(gate, delta, names, sought)

        def triggered(name, gate=None):
            if name.endswith('mmu_exit_0'):
                return False
            return original_triggered(name, gate)

        self.fil.trip_distance = trip
        self.fil.triggered = triggered
        self.addCleanup(setattr, self.fil, 'trip_distance', original_trip)
        self.addCleanup(setattr, self.fil, 'triggered', original_triggered)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertGreater(self.fil.tip[0], self.fil.layout['mmu_shared_exit'])
        self.assertEqual(self.gate_maps.ttg_map[0], 0,
                         'Homing did not establish absence or safe parking')
        self.assertTrue(self.hh.errors)
        self.assertFalse(any(g == 1 for g, *_ in self.fil.history))
        self.assertEqual(self.hh.mmu.filament_pos, -1)
        self.assertEqual(self.hh.mmu.gate_status[0], -1)
        self.assert_metadata_preserved()

    def test_driver_error_with_entry_presence_stops_sweep(self):
        mmu = self.hh.mmu
        self.fil.history.clear()
        with patch.object(mmu.drive(0), 'move',
                          side_effect=mmu.printer.command_error('Communication timeout')):
            self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1')
        self.assertEqual(mmu.gate_status[0], -1)
        self.assertEqual(mmu.filament_pos, -1)
        self.assertTrue(self.hh.errors)
        self.assertFalse(any(g == 1 for g, *_ in self.fil.history))
        self.assert_metadata_preserved()

    def test_two_tools_share_empty_gate_and_substitute(self):
        self.gate_maps.remap_tool(1, 0)
        with self.hh.quiet_sensors():
            self.fil.remove(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,1')
        self.assertEqual(self.gate_maps.ttg_map[:2], [1, 1])
        self.assertEqual(self.hh.errors, [])

    def test_multiple_empty_alternatives_are_exhausted(self):
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,1,1 ENABLE=1')
        with self.hh.quiet_sensors():
            for gate in (0, 1, 2):
                self.fil.remove(gate)
        self.hh.settle()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,1,2')
        self.assertEqual(self.gate_maps.ttg_map[:3], [3, 3, 3])
        self.assertEqual(self.hh.errors, [])

    def test_confirmed_empty_gate_is_not_moved_and_keeps_metadata(self):
        self.remove(0)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 1)
        self.assertFalse(any(g == 0 for g, *_ in self.fil.history))
        self.assertTrue(any(g == 1 and distance > 0 for g, distance, *_ in self.fil.history))
        self.assertAlmostEqual(self.fil.tip[1], -100., places=3)
        self.assertEqual(self.hh.errors, [])
        self.assert_metadata_preserved()

    def test_disabled_entry_sensor_uses_physical_discovery(self):
        self.remove(0)
        self.hh.mmu.sensor_manager.all_sensors_map['mmu_entry_0'].runout_helper.sensor_enabled = False
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.mmu.gate_status[0], 0)
        self.assertFalse(any(g == 1 for g, *_ in self.fil.history))
        self.assert_metadata_preserved()

    def test_missing_entry_sensor_uses_physical_discovery(self):
        self.remove(0)
        sm = self.hh.mmu.sensor_manager
        check = sm.check_gate_sensor
        def without_entry(name, gate):
            return None if name == 'mmu_entry' and gate == 0 else check(name, gate)
        with patch.object(sm, 'check_gate_sensor', side_effect=without_entry):
            self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.mmu.gate_status[0], 0)
        self.assert_metadata_preserved()

    def test_entry_only_empty_gate_can_substitute(self):
        self.remove(0)
        sm = self.hh.mmu.sensor_manager
        with patch.dict(sm.all_sensors_map):
            sm.all_sensors_map.pop('mmu_exit_0')
            self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 1)
        self.assertEqual(self.hh.errors, [])
        self.assert_metadata_preserved()

    def test_exit_presence_overrides_a_clear_entry(self):
        # A short remnant still spans the gear/exit; it has left the entry switch.
        with self.hh.quiet_sensors():
            self.hh.place_filament(0, position=5.)
            self.fil.tail[0] = -120.
            self.fil.sync()
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertTrue(any(g == 0 for g, *_ in self.fil.history))
        self.assertEqual(self.hh.errors, [])

    def test_disabled_homing_sensor_preserves_pre_motion_state(self):
        sm = self.hh.mmu.sensor_manager
        sm.all_sensors_map['mmu_exit_0'].runout_helper.sensor_enabled = False
        before = (self.hh.mmu.filament_pos, self.hh.mmu.gate_status[0])
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertEqual(self.fil.history, [])
        self.assertEqual((self.hh.mmu.filament_pos, self.hh.mmu.gate_status[0]), before)
        self.assertTrue(any('disabled' in e for e in self.hh.errors))
        self.assert_metadata_preserved()

    def test_selection_failure_stops_without_changing_availability(self):
        from extras.mmu.mmu_utils import MmuError
        with patch.object(self.hh.mmu, 'select_gate', side_effect=MmuError('selector failure')):
            self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,1')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertEqual(self.hh.mmu.gate_status[0], 1)
        self.assertTrue(any('selector failure' in e for e in self.hh.errors))
        self.assert_metadata_preserved()

    def test_parking_failure_stops_even_outside_a_print(self):
        from extras.mmu.mmu_utils import MmuError
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=0')
        self.fil.history.clear()
        with patch.object(self.hh.mmu, '_unload_gate', side_effect=MmuError('parking failure')):
            self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,1')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertFalse(any(g == 1 for g, *_ in self.fil.history))
        self.assertNotEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.mmu.gate_status[0], 1)
        self.assertTrue(any('parking failure' in e for e in self.hh.errors))
        self.assert_metadata_preserved()

    def test_shared_exit_occupancy_blocks_selection(self):
        self.hh.place_filament(1, position=self.fil.layout['mmu_shared_exit'] + 5.)
        self.fil.history.clear()
        before = self.hh.mmu.gate_selected
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.hh.mmu.gate_selected, before)
        self.assertEqual(self.fil.history, [])
        self.assertTrue(any('occupied' in e for e in self.hh.errors))
        self.assertEqual(self.gate_maps.ttg_map[0], 0)

    def test_empty_gate_without_endless_spool_still_pauses(self):
        self.remove(0)
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=0')
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0)
        self.assertTrue(any('marked EMPTY' in e for e in self.hh.errors))
        self.assert_metadata_preserved()

    def test_duplicate_tools_only_probe_the_available_gate_once(self):
        self.gate_maps.remap_tool(1, 0)
        real_load = self.hh.mmu._load_gate
        with patch.object(self.hh.mmu, '_load_gate', wraps=real_load) as load:
            self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,1')
        self.assertEqual(load.call_count, 1)
        self.assertEqual(self.hh.errors, [])

    def test_empty_gate_does_not_abort_remaining_checks_during_print(self):
        self.remove(0)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1,2')
        moved = {gate for gate, *_ in self.fil.history}
        self.assertEqual(moved, {1, 2})
        for gate in (1, 2):
            self.assertAlmostEqual(self.fil.tip[gate], -100., places=3)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertTrue(any('marked EMPTY' in error for error in self.hh.errors))
        self.assert_metadata_preserved()

    def test_safe_configuration_failure_does_not_abort_remaining_checks(self):
        self.hh.mmu.sensor_manager.all_sensors_map['mmu_exit_0'].runout_helper.sensor_enabled = False
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1,2')
        moved = {gate for gate, *_ in self.fil.history}
        self.assertEqual(moved, {1, 2})
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertTrue(any('disabled' in error for error in self.hh.errors))
        self.assert_metadata_preserved()

    def test_standalone_safe_failures_are_logged_after_checking_other_gates(self):
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=0')
        self.remove(0)
        self.hh.mmu.sensor_manager.all_sensors_map['mmu_exit_1'].runout_helper.sensor_enabled = False
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE ALL=1')
        self.assertEqual({gate for gate, *_ in self.fil.history}, {2, 3})
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(any('marked EMPTY' in line for line in self.hh.console))
        self.assertTrue(any('disabled' in line for line in self.hh.console))
        self.assertFalse(self.hh.mmu.is_mmu_paused())

    def test_entry_presence_still_requires_pickup_and_parking(self):
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE GATE=0')
        self.assertTrue(any(gate == 0 and distance > 0 for gate, distance, _ in self.fil.history))
        self.assertTrue(any(gate == 0 and distance < 0 for gate, distance, _ in self.fil.history))
        self.assertAlmostEqual(self.fil.tip[0], -100., places=3)
        self.assertEqual(self.hh.errors, [])

    def test_substitution_excludes_a_gate_that_could_not_be_checked(self):
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,1,1 ENABLE=1')
        self.hh.mmu.sensor_manager.all_sensors_map['mmu_exit_0'].runout_helper.sensor_enabled = False
        self.remove(3)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0,3')
        # Gate 0 still has an available status, but this sweep could not verify it.
        # T3 must choose and physically verify gate 1 instead of wrapping to gate 0.
        self.assertEqual(self.hh.mmu.ttg_map[3], 1)
        self.assertEqual({gate for gate, *_ in self.fil.history}, {1})
        self.assertAlmostEqual(self.fil.tip[1], -100., places=3)
        self.assertTrue(self.hh.mmu.is_mmu_paused())  # T0's failure is still unresolved.

    def test_selection_failure_keeps_normal_recovery(self):
        from extras.mmu.mmu_utils import MmuError
        mmu = self.hh.mmu
        with patch.object(mmu, 'select_gate', side_effect=MmuError('selector failure')):
            with patch.object(mmu, 'handle_mmu_error') as handle:
                self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1')
        self.assertTrue(handle.call_args.kwargs['recover'])

    def test_completed_sweep_keeps_normal_recovery(self):
        self.remove(0)
        with patch.object(self.hh.mmu, 'handle_mmu_error') as handle:
            self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1')
        self.assertTrue(handle.call_args.kwargs['recover'])

    def test_failed_parking_suppresses_recovery(self):
        from extras.mmu.mmu_utils import MmuError
        mmu = self.hh.mmu
        with patch.object(mmu, '_unload_gate', side_effect=MmuError('parking failure')):
            with patch.object(mmu, 'handle_mmu_error') as handle:
                self.hh.run_gcode('MMU_CHECK_GATE GATES=0,1')
        self.assertFalse(handle.call_args.kwargs['recover'])

    def test_regular_pickup_keeps_legacy_failure_behavior(self):
        from extras.mmu.mmu_utils import MmuError
        self.remove(0)
        self.hh.mmu.select_gate(0)
        with self.assertRaises(MmuError):
            self.hh.mmu._load_gate(allow_retry=False)
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.mmu.gate_status[0], 0)
        self.assertEqual(self.hh.mmu.gate_spool_id[0], -1)


class TestCheckGateEncoderFailure(unittest.TestCase):
    def test_insufficient_encoder_motion_stops_without_remapping(self):
        hh = session('encoder')
        self.addCleanup(hh.close)
        hh.boot()
        for gate in (0, 1):
            hh.place_filament(gate, position=-100.)
        hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,2,2 ENABLE=1')
        hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1 test_force_in_print=1')
        hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=1 MATERIAL=PETG TEMP=240 SPOOLID=42 QUIET=1')
        hh.run_gcode('MMU_GATE_MAP GATE=1 AVAILABLE=1 QUIET=1')
        # The real motor move still runs, but the encoder emits no pulses.
        with patch.object(hh.filament(), 'travel_over', return_value=0.):
            hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertGreater(hh.filament().tip[0], 0.)
        self.assertEqual(hh.mmu.ttg_map[0], 0)
        self.assertEqual(hh.mmu.filament_pos, -1)
        self.assertEqual(hh.mmu.gate_status[0], -1)
        self.assertEqual(hh.mmu.gate_spool_id[0], 42)
        self.assertTrue(hh.errors)
        self.assertFalse(any(g == 1 for g, *_ in hh.filament().history))


class TestGateOccupancy(EndlessSpoolTestCase):
    """
    The three-way classification both MMU_CHECK_GATE and _home_to_gate decide on. EMPTY
    skips the pickup, UNKNOWN allows a clean homing miss to stand as empty, and PRESENT
    means only a pickup can say where the filament is.
    """

    def occupancy(self, gate=0):
        return self.hh.mmu.gate_occupancy(gate)

    def test_a_covered_entry_switch_is_present(self):
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)
        self.assertEqual(self.occupancy(), OCCUPANCY_PRESENT)

    def test_a_clear_entry_switch_is_empty(self):
        with self.hh.quiet_sensors():
            self.fil.remove(0)
        self.hh.settle()
        self.assertEqual(self.occupancy(), OCCUPANCY_EMPTY)

    def test_no_entry_switch_is_unknown(self):
        with self.hh.quiet_sensors():
            self.fil.remove(0)
        self.hh.settle()
        sm = self.hh.mmu.sensor_manager
        sm.all_sensors_map['mmu_entry_0'].runout_helper.sensor_enabled = False
        self.assertEqual(self.occupancy(), OCCUPANCY_UNKNOWN)

    def test_a_remnant_past_the_entry_switch_is_still_present(self):
        # Short remnant spanning the exit switch but clear of the entry switch. Calling
        # this empty would remap away from filament that is physically in the way.
        with self.hh.quiet_sensors():
            self.hh.place_filament(0, position=5.)
            self.fil.tail[0] = -120.
            self.fil.sync()
        self.assertFalse(self.hh.sensor('mmu_entry_0').present)
        self.assertTrue(self.hh.sensor('mmu_exit_0').present)
        self.assertEqual(self.occupancy(), OCCUPANCY_PRESENT)


class TestSharedPathTargetUnit(unittest.TestCase):
    """
    End-to-end: _check_path_unloaded must refuse a gate whose unit already has a shared
    path occupied, before selecting anything. The guard itself is unit-tested by
    test_mmu_nfc_scan.py::TestSharedGateOccupancyAcrossUnits; this is the command honouring
    it, which is what stops a selector moving across another gate's filament.
    """

    def test_check_gate_refuses_a_gate_on_an_occupied_unit(self):
        # clone_across_units is the supported way to get a multi-unit shape; building it by
        # hand renders unit1's sections with unit0's pins (see its docstring).
        profile = profiles.clone_across_units(
            'two_boxes', profiles.get('boxturtle_test'), ('unit0', 'unit1'))
        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot(calibrate=True)
        self.assertEqual(hh.errors, [])
        hh.mmu.select_gate(0)
        hh.place_filament(5, position=hh.filament().layout['mmu_shared_exit'] + 5.)
        hh.filament().history.clear()
        hh.run_gcode('MMU_CHECK_GATE TOOLS=4')
        self.assertEqual(hh.mmu.gate_selected, 0, 'must not have selected gate 4')
        self.assertEqual(hh.filament().history, [], 'must refuse before any motion')
        self.assertTrue(any('occupied' in e for e in hh.errors))


class TestSensorlessDiscovery(unittest.TestCase):
    def setUp(self):
        self.hh = session('chameleon')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.hh.calibrate()
        self.hh.run_gcode('MMU_HOME UNIT=0')
        for gate in (0, 3):
            self.hh.place_filament(gate, position=-40.)
        self.assertEqual(self.hh.errors, [])

    def test_empty_lanes_do_not_abort_discovery(self):
        self.hh.run_gcode('MMU_CHECK_GATE ALL=1')
        self.assertEqual(self.hh.mmu.gate_status, [1, 0, 0, 1])
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.errors, [])

    def test_print_reports_empty_lanes_after_finishing_discovery(self):
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        self.hh.run_gcode('MMU_CHECK_GATE ALL=1')
        self.assertEqual(self.hh.mmu.gate_status, [1, 0, 0, 1])
        self.assertTrue(self.hh.errors)
        self.assertTrue(self.hh.mmu.is_mmu_paused())

    def test_observed_encoder_motion_prevents_empty_classification(self):
        # Even sub-threshold measured filament motion contradicts an empty lane.
        mmu = self.hh.mmu
        with patch.object(mmu, 'get_encoder_distance', side_effect=itertools.count(step=1.)):
            with patch.object(mmu, 'handle_mmu_error') as handle:
                self.hh.run_gcode('MMU_CHECK_GATE GATES=1,3')
        self.assertEqual(mmu.gate_status[1], -1)
        self.assertEqual(mmu.filament_pos, -1)
        self.assertFalse(handle.call_args.kwargs['recover'])

    def test_driver_error_without_filament_evidence_continues_sweep(self):
        mmu = self.hh.mmu
        drive = mmu.drive(1)
        move = drive.move

        def fail_gate_one(*args, **kwargs):
            if mmu.gate_selected == 1:
                raise mmu.printer.command_error('Communication timeout')
            return move(*args, **kwargs)

        with patch.object(drive, 'move', side_effect=fail_gate_one):
            self.hh.run_gcode('MMU_CHECK_GATE GATES=1,3')
        self.assertEqual(mmu.gate_status[1], 0)
        self.assertEqual(mmu.gate_status[3], 1)
        self.assertEqual(mmu.filament_pos, 0)
        self.assertEqual(self.hh.errors, [])

class TestSensorlessEncoderDiscovery(unittest.TestCase):
    """
    The encoder branch of _home_to_gate on a machine with no per-gate switches. Distinct
    from TestSensorlessDiscovery, which runs on chameleon and therefore exercises the
    real-endstop branch: the encoder branch makes a plain move, not a homing move, so
    move_filament's tolerant command_error handler never covers it, and the encoder is the
    only thing that can report movement at all.
    """

    def setUp(self):
        self.hh = session(SENSORLESS_ENCODER)
        self.addCleanup(self.hh.close)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        sensors = self.hh.mmu.sensor_manager.all_sensors_map
        self.assertFalse([n for n in sensors if n.startswith(('mmu_entry', 'mmu_exit'))],
                         'precondition: no per-gate switches')
        for gate in range(self.hh.mmu.num_gates):
            self.hh.place_filament(gate, position=-100.)
        self.hh.settle()
        # Gate 1 emptied without Happy Hare seeing it - the state a gate check must discover.
        with self.hh.quiet_sensors():
            self.hh.filament().remove(1)
        self.hh.settle()

    def sub_threshold_encoder(self):
        """One rogue pulse on a lane that does not actually move."""
        real = self.hh.filament().travel_over
        step = self.hh.mmu.encoder(1).get_resolution()
        self.assertLess(step, self.hh.mmu.encoder(1).movement_min(),
                        'precondition: one pulse is below the noise floor')
        return patch.object(self.hh.filament(), 'travel_over',
                            side_effect=lambda *a, **k: real(*a, **k) or step)

    def test_a_rogue_encoder_pulse_is_still_an_empty_lane(self):
        with self.sub_threshold_encoder():
            self.hh.run_gcode('MMU_CHECK_GATE ALL=1')
        self.assertEqual(self.hh.mmu.gate_status, [1, 0, 1, 1])
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.errors, [])

    def test_real_encoder_motion_still_stops_the_sweep(self):
        # On this branch the encoder IS the detector, so anything above the pickup
        # threshold is a successful load. The window that matters is between the noise
        # floor and that threshold: filament demonstrably budged but never fed properly,
        # which is an uncertain position and must not be called empty.
        mmu = self.hh.mmu
        nudge = 3.0
        self.assertGreater(nudge, mmu.encoder(1).movement_min(), 'above the noise floor')
        # get_encoder_distance is read twice per move (start, end), so a counter stepping
        # by 'nudge' reports exactly that much measured movement.
        with patch.object(mmu, 'get_encoder_distance',
                          side_effect=itertools.count(step=nudge)):
            self.hh.run_gcode('MMU_CHECK_GATE GATES=1')
        self.assertEqual(mmu.gate_status[1], -1)
        self.assertEqual(mmu.filament_pos, -1)
        self.assertTrue(self.hh.errors)

    def test_a_driver_error_is_treated_like_a_homing_miss(self):
        mmu = self.hh.mmu
        drive = mmu.drive(1)
        move = drive.move

        def fail_gate_one(*args, **kwargs):
            if mmu.gate_selected == 1:
                raise mmu.printer.command_error('Communication timeout')
            return move(*args, **kwargs)

        with patch.object(drive, 'move', side_effect=fail_gate_one):
            self.hh.run_gcode('MMU_CHECK_GATE ALL=1')
        self.assertEqual(self.hh.mmu.gate_status, [1, 0, 1, 1],
                         'the sweep must finish, not abort at gate 1')
        self.assertEqual(self.hh.mmu.filament_pos, 0)
        self.assertEqual(self.hh.errors, [])

    def test_a_driver_error_on_an_ordinary_load_still_propagates(self):
        # The tolerance above is scoped to gate discovery. A normal load must not quietly
        # turn a comms fault into an empty gate.
        mmu = self.hh.mmu
        mmu.select_gate(0)
        with patch.object(mmu.drive(0), 'move',
                          side_effect=mmu.printer.command_error('Communication timeout')):
            with self.assertRaises(mmu.printer.command_error):
                mmu._load_gate(allow_retry=False)


if __name__ == '__main__':
    unittest.main()
