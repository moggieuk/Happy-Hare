# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
"""Source telemetry tests: these intentionally do not instantiate the live viewer."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from test.hh import session
from test.console import Console, parse_args


class TestMovementTelemetry(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.hh.place_filament(0, 200)
        self.records = []
        self.unsubscribe = self.hh.mmu.movement_telemetry.subscribe(self.records.append)

    def tearDown(self):
        self.unsubscribe()
        self.hh.close()

    def pair(self):
        moves = [r for r in self.records if r['event'].startswith('movement_')]
        self.assertEqual(len(moves), 2)
        start, end = moves
        self.assertEqual(start['event'], 'movement_start')
        self.assertEqual(end['event'], 'movement_outcome')
        self.assertEqual(start['move_id'], end['move_id'])
        self.assertEqual(start['session_id'], end['session_id'])
        self.assertLess(start['sequence'], end['sequence'])
        json.dumps(self.records, allow_nan=False)
        return start, end

    def test_idle_disable_uses_trace_not_stepper(self):
        with mock.patch.object(self.hh.mmu, 'log_stepper') as stepper, mock.patch.object(self.hh.mmu, 'log_trace') as trace:
            self.hh.mmu.disable_idle_gear_stepper(0)
            self.hh.mmu.disable_all_idle_gear_steppers()
        stepper.assert_not_called()
        self.assertEqual(trace.call_count, 2)

    def test_sync_state_reports_successful_changes_only(self):
        from extras.mmu.mmu_constants import DRIVE_EXTRUDER_ONLY
        drive = self.hh.mmu.drive()
        drive.sync_mode(DRIVE_EXTRUDER_ONLY)
        drive.sync_mode(DRIVE_EXTRUDER_ONLY)
        events = [r for r in self.records if r['event'] == 'drive_sync_changed']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['mode'], DRIVE_EXTRUDER_ONLY)
        self.assertEqual(events[0]['drive'], drive.name)
        self.assertIsNone(events[0]['move_id'])

    def test_espooler_operation_changes_are_semantic_and_deduplicated(self):
        from extras.mmu.mmu_constants import ESPOOLER_ASSIST, ESPOOLER_OFF, ESPOOLER_REWIND
        espooler = self.hh.mmu.espooler()
        espooler.assist_gates = sorted(set(espooler.assist_gates + [0]))
        espooler.respool_gates = sorted(set(espooler.respool_gates + [0]))
        for op, power in [(ESPOOLER_ASSIST, .5), (ESPOOLER_ASSIST, .5),
                          (ESPOOLER_ASSIST, .8), (ESPOOLER_REWIND, .8), (ESPOOLER_OFF, 0)]:
            espooler.set_operation(0, power, op)
        events = [r for r in self.records if r['event'].startswith('espooler_')]
        self.assertEqual([r['event'] for r in events], ['espooler_operation_changed'] * 4)
        self.assertEqual([r['operation'] for r in events], ['assist', 'assist', 'rewind', 'off'])
        self.assertEqual(events[-1]['direction'], 'stopped')
        self.assertEqual(events[-1]['output_power'], 0)
        self.assertEqual(events[0]['requested_power'], .5)

    def test_espooler_print_assist_burst_and_reset(self):
        from extras.mmu.mmu_constants import ESPOOLER_ASSIST
        espooler = self.hh.mmu.espooler()
        espooler.assist_gates = sorted(set(espooler.assist_gates + [0]))
        espooler.set_print_assist_mode(0)
        espooler._handle_espooler_burst(0, .5, 1., ESPOOLER_ASSIST)
        espooler._stop_espooler_burst(0)
        espooler.reset_print_assist_mode()
        events = [r for r in self.records if r['event'].startswith('espooler_')]
        changes = [r for r in events if r['event'] == 'espooler_operation_changed']
        self.assertEqual([r['operation'] for r in changes], ['print', 'assist', 'print', 'off'])
        self.assertTrue(changes[2]['print_assist_armed'])
        self.assertFalse(changes[3]['print_assist_armed'])
        self.assertEqual(len([r for r in events if r['event'] == 'espooler_burst_started']), 1)
        self.assertEqual(len([r for r in events if r['event'] == 'espooler_burst_ended']), 1)

    def test_resolved_speed_matches_stepper_log_after_override(self):
        self.hh.mmu.gate_speed_override[0] = 50
        with mock.patch.object(self.hh.mmu, 'log_stepper') as log:
            self.hh.mmu.move_filament('capture', 30, speed=80, accel=200, wait=True)
        start, end = self.pair()
        self.assertEqual(start['speed_mm_s'], 40)
        self.assertTrue(any('speed=40.0' in str(call) for call in log.call_args_list))
        self.assertEqual(start['requested_mm'], 30)
        self.assertEqual(end['actual_mm'], 30)
        self.assertEqual(end['status'], 'completed')
        self.assertIsNone(end['homed'])
        self.assertTrue(start['motors'])

    def test_source_stream_works_without_stepper_logging_or_viewer(self):
        with mock.patch.object(self.hh.mmu, 'log_enabled', return_value=False), mock.patch.object(self.hh.mmu, 'log_stepper'):
            self.hh.mmu.move_filament('capture', 10)
        start, end = self.pair()
        self.assertGreater(start['speed_mm_s'], 0)
        self.assertEqual(end['status'], 'submitted')
        self.assertNotIn('tips', start)  # No simulator-only geometry in the source schema.

    def test_homing_reports_actual_stop_and_sensor_endpoints(self):
        name = self.hh.mmu.mmu_unit().name + ':mmu_shared_exit'
        self.hh.place_filament(0, self.hh.filament().position(name)-20)
        self.hh.mmu.move_filament('home', 100, speed=30, homing_move=1, endstop_name=name)
        start, end = self.pair()
        self.assertTrue(start['trigger_state'])
        self.assertEqual(start['endstop'], name)
        self.assertEqual(start['requested_mm'], 100)
        self.assertAlmostEqual(end['actual_mm'], 20)
        self.assertTrue(end['homed'])
        self.assertFalse(start['sensors'][name])
        self.assertTrue(end['sensors'][name])

    def test_missed_home_has_terminal_outcome(self):
        name = self.hh.mmu.mmu_unit().name + ':mmu_shared_exit'
        self.hh.place_filament(0, self.hh.filament().position(name)-100)
        self.hh.mmu.move_filament('miss', 5, homing_move=1, endstop_name=name)
        _, end = self.pair()
        self.assertFalse(end['homed'])
        self.assertEqual(end['status'], 'not_homed')
        self.assertAlmostEqual(end['actual_mm'], 5)

    def test_exception_emits_one_outcome_and_propagates(self):
        with mock.patch.object(self.hh.mmu.drive(), 'move', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.hh.mmu.move_filament('failure', 30)
        _, end = self.pair()
        self.assertEqual(end['status'], 'error')
        self.assertEqual(end['error'], 'injected')
        self.assertIsNone(end['actual_mm'])
        self.assertEqual(self.hh.mmu.movement_telemetry.operations, [])

    def test_failed_subscriber_does_not_affect_motion(self):
        self.hh.mmu.movement_telemetry.subscribe(lambda record: (_ for _ in ()).throw(RuntimeError('sink failed')))
        with self.assertLogs(level='ERROR'):
            result = self.hh.mmu.move_filament('capture', 10)
        self.assertEqual(result[0], 10)
        self.pair()

    def test_subscriber_cannot_mutate_another_consumers_record(self):
        self.hh.mmu.movement_telemetry.subscribe(lambda r: r.update(event='corrupted'))
        other = []
        self.hh.mmu.movement_telemetry.subscribe(other.append)
        self.hh.mmu.move_filament('capture', 10)
        self.assertEqual(other[0]['event'], 'movement_start')

    def test_jsonl_capture_is_independent_of_web_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)/'motion.jsonl'
            with contextlib.redirect_stdout(io.StringIO()):
                console = Console(parse_args(['--profile','boxturtle','--no-serve','--no-log','--no-live','--pace','0','--telemetry',str(path)])).boot()
                try:
                    console.hh.mmu.move_filament('file capture', 10)
                finally:
                    console.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            start = next(r for r in records if r.get('label') == 'file capture')
            ends = [r for r in records if r['move_id']==start['move_id'] and r['event']=='movement_outcome']
            self.assertEqual(len(ends), 1)
            self.assertEqual(ends[0]['actual_mm'], 10)
