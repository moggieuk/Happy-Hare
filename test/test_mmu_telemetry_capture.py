# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Replay capture must remain independent of simulator geometry and the viewer.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import copy
import json
import unittest
from unittest import mock
from test.hh import session


class TestTelemetryCapture(unittest.TestCase):
    def test_spool_weights_are_captured(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 INITIAL_WEIGHT=1000 WEIGHT=250')
        state = self.State(self.hh.mmu)()
        self.assertEqual(state['printer']['mmu']['gate_initial_weight'][0], 1000)
        self.assertEqual(state['printer']['mmu']['gate_remaining_weight'][0], 250)

    def setUp(self):
        self.hh = session('ercf_vvd_buffers')
        self.hh.boot(calibrate=True, selected_gate=0)
        from extras.mmu.telemetry.capture import TelemetryCapture, ReplayState, apply_changes, merge
        self.Capture, self.State, self.apply, self.merge = TelemetryCapture, ReplayState, apply_changes, merge
        self.captures = []

    def tearDown(self):
        for capture in self.captures:
            capture.close()
        self.hh.close()

    def capture(self, **kwargs):
        capture = self.Capture(self.hh.mmu, **kwargs)
        self.captures.append(capture)
        return capture

    def emit(self):
        self.hh.mmu.movement_telemetry.state('cut', cutter='toolhead')

    def test_baseline_uses_printer_facts_without_simulator_coordinates(self):
        with mock.patch.object(self.hh.mmu, 'get_status', side_effect=AssertionError('broad scan')):
            capture = self.capture()
            self.emit()
        records = capture.read()['records']
        baseline = records[0]
        self.assertEqual(baseline['event'], 'session')
        self.assertEqual(baseline['version'], 1)
        state = baseline['state']
        self.assertEqual(set(state['printer']), {'mmu', 'mmu_machine'})
        self.assertEqual(state['printer']['mmu']['gate'], 0)
        self.assertEqual(state['printer']['mmu']['gate_status'], self.hh.mmu.gate_status)
        self.assertEqual(state['printer']['mmu_machine']['purge_macro'],
                         self.hh.mmu._macro_name(self.hh.mmu.p.purge_macro))
        unit = state['printer']['mmu_machine']['units']['unit0']
        self.assertEqual(unit['selector_type'], self.hh.mmu.mmu_machine.units[0].selector_type)
        self.assertIn('buffer_maxrange', unit['buffer'])
        self.assertIn('0', unit['bowden_lengths'])
        self.assertNotIn('tips', state)
        self.assertNotIn('layout', state)
        for record in records[1:]:
            self.assertNotIn('schema', record)
            self.assertNotIn('session_id', record)
            self.assertNotIn('state', record)
        json.dumps(records, allow_nan=False)

    def test_per_gate_heater_capability(self):
        unit = self.hh.mmu.mmu_machine.units[0]
        for heaters, expected in [([], False), ([None, None], False),
                                  (['heater_generic gate0', None], True)]:
            with self.subTest(heaters=heaters), mock.patch.object(unit, 'filament_heaters', heaters):
                capture = self.capture()
                facts = capture.read()['records'][0]['state']
                self.assertEqual(facts['printer']['mmu_machine']['units'][unit.name]
                                 ['has_per_gate_heaters'], expected)

    def test_heater_changes_are_not_movement_telemetry(self):
        heater = self.hh.mmu.printer.lookup_object('extruder').get_heater()
        capture = self.capture()
        baseline = capture.read()['records'][0]['state']
        self.assertNotIn('target_temperature', baseline['printer']['mmu_machine']['units']['unit0']['extruder'])
        heater.set_temp(220)
        self.emit()
        changes = [change for record in capture.read()['records'] if record['event'] == 'state_delta'
                   for change in record['changes']]
        self.assertFalse(any('target_temperature' in change['path'] for change in changes))

    def test_grip_state_and_always_gripped_are_captured(self):
        selector = self.hh.mmu.selector()
        unit = self.hh.mmu.mmu_unit()
        with mock.patch.object(selector, 'get_filament_grip_state', return_value=1):
            capture = self.capture()
        baseline = capture.read()['records'][0]['state']['printer']
        self.assertEqual(baseline['mmu']['filament_grip_state'], 1)
        self.assertEqual(baseline['mmu_machine']['units'][unit.name]['filament_always_gripped'],
                         unit.filament_always_gripped)
        with mock.patch.object(selector, 'get_filament_grip_state', return_value=0):
            self.emit()
        delta = capture.read()['records'][1]
        self.assertIn(dict(path=['printer', 'mmu', 'filament_grip_state'], value=0), delta['changes'])

    def test_history_response_is_copied_outside_the_producer_lock(self):
        capture = self.capture()
        self.emit()
        original = copy.deepcopy
        copied = []
        def check(value, *args, **kwargs):
            if isinstance(value, dict) and 'records' in value:
                self.assertFalse(capture.lock.locked())
                copied.append(True)
            return original(value, *args, **kwargs)
        with mock.patch('extras.mmu.telemetry.capture.copy.deepcopy', side_effect=check):
            capture.read()
        self.assertTrue(copied)

    def test_encoder_runtime_parameters_are_captured_as_configuration_changes(self):
        capture = self.capture()
        encoder = self.hh.mmu.mmu_unit().encoder
        baseline = capture.read()['records'][0]['state']
        parameters = baseline['printer']['mmu_machine']['units']['unit0']['encoder']['parameters']
        self.assertEqual(parameters['resolution'], encoder.get_resolution())
        encoder.desired_headroom += 1
        self.emit()
        changes = capture.read()['records'][1]['changes']
        self.assertTrue(any(change['path'][-3:] == ['encoder', 'parameters', 'desired_headroom'] for change in changes))

    def test_only_changed_leaves_are_saved_before_the_next_event(self):
        capture = self.capture()
        old = capture.read()['records'][0]['state']
        unit = self.hh.mmu.mmu_unit()
        unit.buffer.buffer_range += 1
        self.emit()
        records = capture.read()['records']
        self.assertEqual([r['event'] for r in records], ['session', 'state_delta', 'cut'])
        delta = records[1]['changes']
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]['path'][-1], 'buffer_range')
        self.apply(old, delta)
        state = self.State(self.hh.mmu)
        self.assertEqual(old, self.merge(state(), state.static()))
        self.emit()
        self.assertEqual(len(capture.read()['records']), 4)

    def test_baseline_holds_full_parameter_sets_read_once(self):
        capture = self.capture()
        state = capture.read()['records'][0]['state']['printer']['mmu_machine']
        unit = state['units']['unit0']
        self.assertIn('log_level', state['parameters'])
        self.assertIn('gate_homing_endstop', unit['parameters'])
        self.assertIn('toolhead_residual_filament', unit['toolhead']['parameters'])

    def test_parameter_edit_is_one_delta_and_does_not_reread_parameter_sets(self):
        capture = self.capture()
        with mock.patch.object(self.State, 'parameter_sets', side_effect=AssertionError('re-read')):
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 TOOLHEAD_RESIDUAL_FILAMENT=3')
        records = capture.read()['records']
        deltas = [r for r in records if r['event'] == 'state_delta']
        self.assertEqual([r['event'] for r in records].count('config_changed'), 0)
        paths = [c['path'] for d in deltas for c in d['changes']]
        self.assertIn(['printer', 'mmu_machine', 'units', 'unit0', 'toolhead', 'parameters',
                       'toolhead_residual_filament'], paths)
        change = next(c for d in deltas for c in d['changes'] if c['path'][-1] == 'toolhead_residual_filament')
        self.assertEqual(change['value'], 3)

    def test_changing_a_parameter_back_still_records_each_change(self):
        capture = self.capture()
        original = self.hh.mmu.mmu_unit().toolhead_wrapper.p.toolhead_residual_filament
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 TOOLHEAD_RESIDUAL_FILAMENT=%s' % (original + 1))
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 TOOLHEAD_RESIDUAL_FILAMENT=%s' % original)
        # A shared toolhead is one record that updates every unit using it
        records = [r for r in capture.read()['records'] if r['event'] == 'state_delta']
        self.assertEqual([{c['value'] for c in r['changes']} for r in records], [{original + 1}, {original}])

    def test_unchanged_parameter_adds_no_record(self):
        capture = self.capture()
        before = len(capture.read()['records'])
        self.hh.run_gcode('MMU_TEST_CONFIG LOG_LEVEL=%d' % self.hh.mmu.p.log_level)
        self.assertEqual(len(capture.read()['records']), before)

    def test_edit_without_listener_emits_nothing(self):
        telemetry = self.hh.mmu.movement_telemetry
        sequence = telemetry.sequence
        self.hh.run_gcode('MMU_TEST_CONFIG LOG_LEVEL=2')
        self.assertEqual(telemetry.sequence, sequence)

    def test_cancelled_edits_are_not_saved(self):
        capture = self.capture()
        buffer = self.hh.mmu.mmu_unit().buffer
        initial = buffer.buffer_range
        buffer.buffer_range += 3
        buffer.buffer_range = initial
        self.emit()
        self.assertEqual([r['event'] for r in capture.read()['records']], ['session', 'cut'])

    def test_bounded_history_rolls_forward_its_baseline(self):
        current = {'printer': {'mmu': {'gate': 0}}}
        with mock.patch.object(self.State, '__call__', lambda _: copy.deepcopy(current)):
            capture = self.capture(limit=3)
            for gate in range(1, 10):
                current['printer']['mmu']['gate'] = gate
                self.emit()
        result = capture.read()
        records = result['records']
        self.assertEqual(len(records), 4)
        self.assertTrue(records[0]['checkpoint'])
        restored = records[0]['state']
        for record in records[1:]:
            if record['event'] == 'state_delta':
                self.apply(restored, record['changes'])
        self.assertEqual(restored['printer']['mmu'], current['printer']['mmu'])
        self.assertTrue(capture.read(after=1)['reset'])
        self.assertEqual(capture.read(after=records[-1]['sequence'])['records'], [])

    def test_sensor_enablement_and_macro_dimensions_are_recorded(self):
        mmu = self.hh.mmu
        mmu.p.form_tip_macro = '_MMU_CUT_TIP'
        macro = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        capture = self.capture()
        name, (_, sensor) = next(iter(mmu.sensor_manager.get_sensor_states(all_sensors=True).items()))
        sensor.runout_helper.sensor_enabled = not sensor.runout_helper.sensor_enabled
        macro.variables['blade_pos'] += 1
        self.emit()
        delta = capture.read()['records'][1]
        self.assertEqual(delta['event'], 'state_delta')
        paths = [change['path'] for change in delta['changes']]
        self.assertIn(['printer', 'mmu', 'sensors', name, 'enabled'], paths)
        self.assertIn(['macros', '_MMU_CUT_TIP_VARS', 'blade_pos'], paths)

    def test_idle_change_waits_for_the_next_event(self):
        capture = self.capture()
        buffer = self.hh.mmu.mmu_unit().buffer
        buffer.buffer_range += 1
        self.assertEqual(capture.read(after=0)['records'], [])
        self.assertEqual(capture.sequence, 0)
        self.emit()
        self.assertEqual([r['event'] for r in capture.read(after=0)['records']], ['state_delta', 'cut'])

    def test_removals_are_distinct_from_null(self):
        from extras.mmu.telemetry.capture import changes
        old, new = {'a': 1, 'b': 2}, {'b': None}
        self.apply(old, changes(old, new))
        self.assertEqual(old, new)

    def test_readers_cannot_mutate_recording(self):
        capture = self.capture()
        self.emit()
        result = capture.read()
        result['records'][0]['state'].clear()
        result['records'][1]['event'] = 'changed'
        self.assertTrue(capture.read()['records'][0]['state'])
        self.assertEqual(capture.read()['records'][1]['event'], 'cut')

    def test_closing_capture_removes_all_state_collection_work(self):
        with mock.patch.object(self.State, '__call__', return_value={}) as provider:
            capture = self.capture()
            capture.close()
            provider.reset_mock()
            self.emit()
            provider.assert_not_called()
        self.assertNotIn(capture.on_event, self.hh.mmu.movement_telemetry.listeners)

    def test_status_capture_does_not_reinstall_g1_wrapper(self):
        gcode = self.hh.printer.lookup_object('gcode')
        before = dict(gcode.ready_gcode_handlers)
        self.capture()
        self.assertEqual(gcode.ready_gcode_handlers['G1'], before['G1'])
