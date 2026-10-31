# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import urllib.request
from unittest import mock

from test.hh import session
from test.hh.live_viewer import LiveViewer
from test.hh.webui_bridge import WebUiBridge, RpcError
from test.console import Console, parse_args
from test.test_mmu_console import make_install_tree


class TestLiveViewer(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.viewer = LiveViewer(self.hh)

    def tearDown(self):
        self.viewer.close()
        self.hh.close()

    def test_simulated_cut_produces_a_dedicated_playback_event(self):
        macro = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP')
        self.hh._effect_cut_tip(macro, None)
        cuts = [e for e in self.viewer.read()['events'] if e['kind'] == 'cut']
        self.assertEqual(len(cuts), 1)
        self.assertEqual(cuts[0]['cutter'], 'toolhead')
        self.assertEqual(cuts[0]['telemetry']['event'], 'filament_cut')
        self.assertEqual(cuts[0]['origin'], 'harness_cut_tip')
        self.assertFalse(any(e['kind'] in ('filament', 'stepper')
                             for e in self.viewer.read()['events']))

    def test_model_samples_update_state_without_creating_trace_entries(self):
        self.hh.place_filament(0, 200)
        before = self.viewer.snapshot()
        events = self.viewer.read()['events']
        records = self.viewer.read_telemetry()['records']
        for _ in range(10):
            self.viewer.model.advance(0, .1, 'tip cutting')
            self.viewer.on_stepper('extruder', .1)
        after = self.viewer.snapshot()
        self.assertAlmostEqual(after['tips'][0] - before['tips'][0], 1)
        self.assertAlmostEqual(after['motors']['extruder'] - before['motors'].get('extruder', 0), 1)
        self.assertEqual(self.viewer.read()['events'], events)
        self.assertEqual(self.viewer.read_telemetry()['records'], records)

    def test_cut_fragment_stays_until_new_end_pushes_it(self):
        mmu = self.hh.mmu
        mmu.p.form_tip_macro = '_MMU_CUT_TIP'
        macro = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP')
        variables = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS', macro)
        variables.variables.update(blade_pos=52., rip_length=2., retract_length=10., pushback_length=0.)
        nozzle = self.viewer.snapshot()['layout']['nozzle']
        self.hh.place_filament(0, nozzle)
        self.viewer.latest = self.viewer.latest_raw = None
        self.viewer.capture('state', 'Loaded')
        self.hh._effect_cut_tip(macro, None)
        events = self.viewer.read()['events']
        cut = next(e for e in events if e['kind'] == 'cut')
        segment = cut['state']['retained_segment']
        self.assertEqual(segment['start'], nozzle-52)
        self.assertAlmostEqual(segment['end'], cut['before']['tips'][0])
        self.assertEqual(segment['end'], nozzle)
        self.assertEqual(segment['tip_shape'], 'square')
        self.assertEqual(cut['state']['tip_shapes'][0], 'square')
        self.assertEqual(cut['state']['tips'][0], nozzle-52)
        self.assertEqual(events[-1]['state']['retained_segment'], segment)
        self.assertAlmostEqual(events[-1]['state']['tips'][0], nozzle-54)
        mmu.move_filament('Retract away from fragment', -10, motor='extruder')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['retained_segment'], segment)
        mmu.move_filament('New end pushes fragment', 15, motor='extruder')
        state = self.viewer.read()['events'][-1]['state']
        self.assertAlmostEqual(state['retained_segment']['start'], segment['start']+3)
        self.assertAlmostEqual(state['extruded_mm'], 3)

    def test_console_runs_real_cut_and_form_macros(self):
        for profile in ('ercf_vvd', 'encoder'):
            with self.subTest(profile=profile):
                console = Console(parse_args(['--profile',profile,'--no-serve','--no-log','--plain']))
                self.addCleanup(console.close)
                with contextlib.redirect_stdout(io.StringIO()):
                    console.boot()
                    viewer = LiveViewer(console.hh)
                    self.addCleanup(viewer.close)
                    console.run_command('MMU_LOAD')
                    if profile == 'ercf_vvd':
                        console.run_command('SET_GCODE_VARIABLE MACRO=_MMU_CUT_TIP_VARS VARIABLE=cut_iterations VALUE=2')
                    start = viewer.sequence
                    console.run_command('MMU_UNLOAD')
                self.assertEqual(console.hh.errors, [])
                events = [e for e in viewer.read()['events'] if e['seq'] > start]
                moves = [e for e in events if e.get('source') == 'mmu_gcode']
                self.assertGreater(len(moves), 5)
                self.assertTrue(all(e.get('source') != 'harness_cut_tip' for e in events))
                if profile == 'ercf_vvd':
                    cuts = [e for e in events if e['kind'] == 'cut']
                    releases = [e for e in events if e['kind'] == 'release']
                    self.assertEqual(len(cuts), 2)
                    self.assertEqual(len(releases), 2)
                    cut, release = cuts[-1], releases[-1]
                    self.assertEqual(cut['telemetry']['event'], 'cut')
                    self.assertEqual(release['telemetry']['event'], 'release')
                    self.assertEqual(cut['telemetry']['origin'], 'macro')
                    self.assertEqual(cuts[0]['state']['retained_segment'], cut['state']['retained_segment'])
                    self.assertEqual(cut['state']['tip_shapes'][0], 'square')
                    self.assertEqual(cut['state']['retained_segment']['tip_shape'], 'cone')
                    self.assertLess(moves[0]['seq'], cut['seq'])
                    for name, device in cut['before']['devices'].items():
                        if 'encoder_pos' in device:
                            self.assertEqual(device['encoder_pos'],cut['state']['devices'][name]['encoder_pos'])
                    rip = next(e for e in moves if cut['seq'] < e['seq'] < release['seq'])
                    self.assertLess(rip['distance'], 0)
                    self.assertTrue(rip['state']['cutter_pressed'])
                    self.assertFalse(release['state']['cutter_pressed'])
                    after = [e for e in moves if e['seq'] > release['seq']]
                    self.assertGreater(after[0]['distance'], 0)
                    self.assertLess(after[1]['distance'], 0)
                    self.assertEqual(after[0]['state']['retained_segment'],after[1]['state']['retained_segment'])
                    fragment = after[1]['state']['retained_segment']
                    remaining = [e for e in events if e['seq'] > after[1]['seq']]
                    self.assertTrue(any(e['label'] == 'IN EXTRUDER' for e in remaining))
                    self.assertTrue(any(e['kind'] == 'movement' for e in remaining))
                    for event in remaining:
                        self.assertEqual(event['state']['retained_segment'], fragment)
                        self.assertLess(event['state']['tips'][0], fragment['start'])
                    reload_start = viewer.sequence
                    with contextlib.redirect_stdout(io.StringIO()):
                        console.run_command('MMU_LOAD')
                    self.assertEqual(console.hh.errors, [])
                    reload = [e for e in viewer.read()['events'] if e['seq'] > reload_start]
                    contact = next(e for e in reload if e['state']['retained_segment'] != fragment)
                    for event in reload:
                        if event['seq'] < contact['seq']:
                            self.assertEqual(event['state']['retained_segment'], fragment)
                    self.assertEqual(contact['kind'], 'movement')
                    self.assertGreater(contact['distance'], 0)
                    self.assertIsNone(reload[-1]['state']['retained_segment'])

                else:
                    self.assertFalse(any(e['kind'] == 'cut' for e in events))
                    self.assertEqual(moves[-1]['state']['tip_shapes'][0], 'cone')

    def test_console_purge_executes_atomic_g1_moves_and_reaches_nozzle(self):
        console = Console(parse_args(['--profile','boxturtle','--no-serve','--no-log','--plain']))
        self.addCleanup(console.close)
        console.boot()
        viewer = LiveViewer(console.hh)
        self.addCleanup(viewer.close)
        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command('MMU_LOAD')
        self.assertEqual(console.hh.errors, [])
        moves = [e for e in viewer.read()['events'] if e.get('source') == 'mmu_gcode']
        self.assertTrue(moves)
        for event in moves:
            commanded = float(event['label'].split()[1][1:])
            self.assertAlmostEqual(event['distance'], commanded, places=6)
        self.assertGreater(max(e['state']['extruded_mm'] for e in moves), 1)
        self.assertEqual(max(e['state']['tips'][0] for e in moves), moves[-1]['state']['layout']['nozzle'])
        self.assertFalse(any(e['kind'] in ('filament','stepper') for e in viewer.read()['events']))

    def test_toolhead_sensor_drawing_uses_configured_nozzle_distance(self):
        console = Console(parse_args(['--profile','ercf_vvd','--no-serve','--no-log','--plain']))
        self.addCleanup(console.close)
        console.boot()
        viewer = LiveViewer(console.hh)
        self.addCleanup(viewer.close)
        snapshot = viewer.snapshot()
        params = console.hh.mmu.mmu_unit().toolhead_wrapper.p
        for name, sensor in snapshot['sensors'].items():
            if name.split(':')[-1] == 'toolhead':
                self.assertAlmostEqual(snapshot['layout']['nozzle']-sensor['position'],
                                       params.toolhead_sensor_to_nozzle)
        with contextlib.redirect_stdout(io.StringIO()):
            console.run_command('MMU_LOAD')
        self.assertEqual(console.hh.errors, [])
        load = next(e for e in viewer.read()['events'] if e['label'] == 'Loading filament to nozzle')
        self.assertAlmostEqual(load['state']['extruded_mm'], 0)
        self.assertAlmostEqual(load['state']['tips'][0], snapshot['layout']['nozzle']-params.toolhead_residual_filament)

    def test_configuration_calibration_is_not_selected_gate_dependent(self):
        unit = self.hh.mmu.mmu_unit()
        unit.calibrator.rotation_distances[1] = unit.calibrator.rotation_distances[0] + 1
        original = self.viewer.configuration()
        self.hh.mmu.gate_selected = 1
        self.assertEqual(self.viewer.configuration(), original)

    def test_live_capture_has_motor_and_filament_and_immutable_history(self):
        before = self.viewer.read()['events'][0]
        self.hh.place_filament(0, 300)
        self.hh.mmu.move_filament('viewer test', 12, motor='gear+extruder')
        events = self.viewer.read()['events']
        moves = [e for e in events if e['kind']=='movement']
        self.assertEqual(len(moves), 1)
        move = moves[0]
        self.assertEqual(move['origin'], 'happy_hare.motion')
        self.assertEqual(move['telemetry']['start']['move_id'], move['telemetry']['outcome']['move_id'])
        self.assertEqual(move['speed'], move['telemetry']['start']['speed_mm_s'])
        self.assertEqual(move['distance'], 12)
        self.assertEqual(move['before']['tips'][0], 300)
        self.assertEqual(move['state']['tips'][0], 312)
        self.assertEqual(sorted(move['motors'].values()), [12, 12])
        self.assertEqual({e['kind'] for e in events}, {'session', 'movement'})
        self.assertNotEqual(before['state']['tips'][0], events[-1]['state']['tips'][0])
        json.dumps(self.viewer.read(), allow_nan=False)

    def test_gear_only_collision_stops_visual_tip_without_losing_raw_travel(self):
        gears = self.viewer.snapshot()['layout']['extruder_gears']
        self.hh.place_filament(0, gears-10)
        self.hh.mmu.move_filament('Overshoot', 100, motor='gear')
        event = self.viewer.read()['events'][-1]
        self.assertAlmostEqual(event['state']['tips'][0], gears)
        self.assertAlmostEqual(event['raw_state']['tips'][0], gears+90)
        self.assertEqual(event['distance'], 100)
        self.hh.mmu.move_filament('Push again', 10, motor='gear')
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['tips'][0], gears)
        self.hh.mmu.move_filament('Engage extruder', 5, motor='gear+extruder')
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['tips'][0], gears+5)

    def test_untriggered_sensor_bounds_visual_tip_and_disabled_sensor_does_not(self):
        snapshot = self.viewer.snapshot()
        snapshot['tips'][0] = 200
        snapshot['unit_sensors'] = ['unit0:mmu_shared_exit']
        snapshot['active_sensors'] = ['unit0:mmu_shared_exit']
        snapshot['sensors'] = {'unit0:mmu_shared_exit': {'state': False, 'position': 10}}
        with mock.patch.object(self.viewer, 'snapshot', return_value=snapshot):
            self.viewer.capture('test', 'Contradictory sensor')
        self.assertLess(self.viewer.read()['events'][-1]['state']['tips'][0], 10)
        self.assertEqual(self.viewer.read()['events'][-1]['raw_state']['tips'][0], 200)
        snapshot['tips'][0] = 200
        snapshot['sensors']['unit0:mmu_shared_exit']['state'] = None
        self.viewer.latest = self.viewer.latest_raw = None
        with mock.patch.object(self.viewer, 'snapshot', return_value=snapshot):
            self.viewer.capture('test', 'Disabled sensor')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['tips'][0], 200)

    def test_filament_state_change_is_visible_without_a_move(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_IN_BOWDEN
        self.hh.mmu.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=True)
        state = self.viewer.read()['events'][-1]['state']
        self.assertEqual(state['filament_pos'], FILAMENT_POS_IN_BOWDEN)
        self.assertEqual(state['filament_pos_name'], 'IN BOWDEN')

    def test_purge_accumulates_only_after_nozzle_and_retract_does_not_remove_it(self):
        nozzle = self.viewer.snapshot()['layout']['nozzle']
        self.hh.place_filament(0, nozzle-5)
        self.hh.mmu.move_filament('Purge', 15, motor='extruder')
        state = self.viewer.read()['events'][-1]['state']
        self.assertAlmostEqual(state['extruded_mm'], 10)
        self.assertAlmostEqual(state['tips'][0], nozzle)
        self.hh.mmu.move_filament('Retract', -2, motor='extruder')
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['extruded_mm'], 10)
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['purge_base_mm'], 0)
        self.hh.mmu.move_filament('Recover', 2, motor='extruder')
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['extruded_mm'], 10)
        self.hh.mmu.move_filament('More purge', 3, motor='extruder')
        self.assertAlmostEqual(self.viewer.read()['events'][-1]['state']['extruded_mm'], 13)
        self.hh.mmu._set_park_retracted_length(2)
        self.hh.mmu._report_standalone_load_retraction(-2)
        event = self.viewer.read()['events'][-1]
        self.assertEqual(event['kind'], 'purge_release')
        self.assertEqual(event['state']['purge_base_mm'], 13)
        self.assertEqual(event['before']['purge_base_mm'], 0)

    def test_retained_color_and_lengths_seed_the_viewer(self):
        unit = self.hh.mmu.mmu_unit()
        unit.extruder_wrapper.filament_remaining = 7
        unit.extruder_wrapper.filament_remaining_color = '#ff00ff'
        unit.toolhead_wrapper.p.toolhead_residual_filament = 3
        self.hh.mmu.gate_color[0] = '00ffff'
        self.viewer.nozzle_color = None
        state = self.viewer.snapshot()
        self.assertEqual(state['filament']['fragment_mm'], 7)
        self.assertEqual(state['filament']['residual_mm'], 3)
        self.assertEqual(state['filament']['retained_color'], 'ff00ff')
        self.hh.place_filament(0, state['layout']['nozzle'])
        self.hh.mmu.move_filament('Mix', 7, motor='extruder')
        color = self.viewer.read()['events'][-1]['state']['nozzle_color']
        self.assertEqual(color, 'ff00ff')
        self.hh.mmu.move_filament('Mix transition', 1.5, motor='extruder')
        color = self.viewer.read()['events'][-1]['state']['nozzle_color']
        self.assertNotEqual(color, 'ff00ff')
        self.assertNotEqual(color, '00ffff')
        self.hh.mmu.move_filament('Complete transition', 1.5, motor='extruder')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['nozzle_color'], '00ffff')

    def test_real_gate_change_purge_replaces_old_residual_without_extra_purge(self):
        console = Console(parse_args(['--profile','ercf_vvd','--no-serve','--no-log','--plain']))
        self.addCleanup(console.close)
        with contextlib.redirect_stdout(io.StringIO()):
            console.boot()
            viewer = LiveViewer(console.hh)
            self.addCleanup(viewer.close)
            console.run_command('MMU_LOAD')
            console.run_command('MMU_UNLOAD')
            previous = viewer.latest['nozzle_color']
            console.run_command('MMU_SELECT GATE=1')
            start = viewer.sequence
            console.run_command('MMU_LOAD')
        self.assertEqual(console.hh.errors, [])
        events = [e for e in viewer.read()['events'] if e['seq'] > start]
        final = events[-1]['state']
        self.assertNotEqual(previous, final['filament']['incoming_color'])
        self.assertEqual(final['nozzle_color'], final['filament']['incoming_color'])
        self.assertIsNone(final['retained_segment'])
        plan = final['purge_transition']
        self.assertGreaterEqual(final['extruded_mm']-plan['start_mm'],plan['retained_mm']-.01)

    def test_residual_is_replaced_by_one_residual_volume_of_purge(self):
        unit = self.hh.mmu.mmu_unit()
        unit.extruder_wrapper.filament_remaining = 0
        unit.extruder_wrapper.filament_remaining_color = '00ff00'
        unit.toolhead_wrapper.p.toolhead_residual_filament = 2
        self.hh.mmu.gate_color[0] = 'ff0000'
        self.hh.place_filament(0, self.viewer.snapshot()['layout']['nozzle'])
        self.hh.mmu.move_filament('Replace half residual', 1, motor='extruder')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['nozzle_color'], '808000')
        self.hh.mmu.move_filament('Replace remaining residual', 1, motor='extruder')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['nozzle_color'], 'ff0000')
        self.hh.mmu.move_filament('Retract', -2, motor='extruder')
        self.assertEqual(self.viewer.read()['events'][-1]['state']['nozzle_color'], 'ff0000')

    def test_cutter_configuration_and_sync_are_reported(self):
        from extras.mmu.mmu_constants import DRIVE_EXTRUDER_SYNCED_TO_GEAR
        self.assertEqual(self.viewer.snapshot()['cutters'], [])
        with mock.patch.object(self.hh.mmu, 'cut_tip_vars', return_value={'blade_pos': 20}), mock.patch.object(self.hh.mmu, 'cutter_before_extruder', return_value=False):
            state = self.viewer.snapshot()
            self.assertEqual(state['cutters'][0]['position'], state['layout']['nozzle']-20)
            self.assertFalse(state['cutters'][0]['before_extruder'])
        with mock.patch.object(self.hh.mmu, 'cut_tip_vars', return_value={'blade_pos': 100}), mock.patch.object(self.hh.mmu, 'cutter_before_extruder', return_value=True):
            self.assertTrue(self.viewer.snapshot()['cutters'][0]['before_extruder'])
        self.hh.mmu.p.post_unload_macro = 'SERVO_CUTTER_ACTION'
        self.assertEqual(self.viewer.snapshot()['cutters'][0]['kind'], 'mmu')
        self.hh.mmu.drive().sync_mode(DRIVE_EXTRUDER_SYNCED_TO_GEAR)
        self.assertTrue(self.viewer.snapshot()['synced'])

    def test_active_sensor_list_excludes_other_gates_and_retains_disabled_sensors(self):
        state = self.viewer.snapshot()
        self.assertIn('mmu_entry_0', state['active_sensors'])
        self.assertNotIn('mmu_entry_1', state['active_sensors'])
        sensor = self.hh.mmu.sensor_manager.all_sensors_map['mmu_entry_0']
        sensor.runout_helper.sensor_enabled = False
        state = self.viewer.snapshot()
        self.assertIn('mmu_entry_0', state['active_sensors'])
        self.assertIsNone(state['sensors']['mmu_entry_0']['state'])
        self.hh.mmu._set_gate_selected(1)
        state = self.viewer.snapshot()
        self.assertIn('mmu_entry_1', state['active_sensors'])
        self.assertNotIn('mmu_entry_0', state['active_sensors'])

    def test_reverse_home_reanchors_visual_offset_at_sensor(self):
        import copy
        before = self.viewer.snapshot()
        name = 'unit0:mmu_shared_exit'
        position = before['sensors'][name]['position']
        before['tips'][0] = position+30
        for sensor in before['sensors'].values():
            sensor['state'] = True
        self.viewer.latest_raw = copy.deepcopy(before)
        self.viewer.latest = copy.deepcopy(before)
        self.viewer.latest['tips'][0] -= 12.1
        after = copy.deepcopy(before)
        after['tips'][0] = position-0.000001
        after['sensors'][name]['state'] = False
        with mock.patch.object(self.viewer, 'snapshot', return_value=after):
            self.viewer.capture('movement', 'Reverse home', before=before,
                homing=True, homed=True, requested=-100, distance=-30.000001, endstop=name)
        event = self.viewer.read()['events'][-1]
        self.assertAlmostEqual(event['state']['tips'][0], position, places=2)
        self.assertAlmostEqual(event['raw_state']['tips'][0], position, places=2)
        self.assertTrue(event['homing_anchor'])

    def test_forward_home_reanchors_visual_offset_at_sensor(self):
        import copy
        before = self.viewer.snapshot()
        name = 'unit0:mmu_shared_exit'
        position = before['sensors'][name]['position']
        before['tips'][0] = position-30
        for sensor in before['sensors'].values():
            sensor['state'] = True
        before['sensors'][name]['state'] = False
        self.viewer.latest_raw = copy.deepcopy(before)
        self.viewer.latest = copy.deepcopy(before)
        self.viewer.latest['tips'][0] -= 12.1
        after = copy.deepcopy(before)
        after['tips'][0] = position
        after['sensors'][name]['state'] = True
        with mock.patch.object(self.viewer, 'snapshot', return_value=after):
            self.viewer.capture('movement', 'Forward home', before=before,
                homing=True, homed=True, requested=100, distance=30, endstop=name)
        event = self.viewer.read()['events'][-1]
        self.assertAlmostEqual(event['state']['tips'][0], position)
        self.assertTrue(event['homing_anchor'])

    def test_default_speed_is_resolved_from_executed_move(self):
        self.hh.place_filament(0, 200)
        self.hh.mmu.move_filament('Default speed', 12, motor='gear')
        event = self.viewer.read()['events'][-1]
        self.assertGreater(event['speed'], 0)

    def test_paced_move_is_one_record(self):
        self.hh.set_pacing(0.5, wall=False)
        self.hh.place_filament(0, 200)
        self.hh.mmu.move_filament('Bowden move', 300, speed=100, motor='gear')
        moves = [e for e in self.viewer.read()['events'] if e['kind']=='movement']
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]['distance'], 300)
        self.assertEqual(moves[0]['speed'], 100)
        self.assertGreater(moves[0]['duration'], 0)

    def test_homing_record_preserves_search_limit_and_actual_stop(self):
        name = self.hh.mmu.mmu_unit().name + ':mmu_shared_exit'
        position = self.hh.filament().position(name)
        self.hh.place_filament(0, position-20)
        self.hh.mmu.move_filament('Home to exit', 100, homing_move=1, endstop_name=name)
        event = self.viewer.read()['events'][-1]
        self.assertEqual(event['kind'], 'movement')
        self.assertEqual(event['requested'], 100)
        self.assertAlmostEqual(event['distance'], 20)
        self.assertTrue(event['homed'])
        self.assertFalse(event['before']['sensors'][name]['state'])
        self.assertTrue(event['state']['sensors'][name]['state'])

    def test_bounded_capture_and_after_cursor(self):
        for i in range(self.viewer.LIMIT+3): self.viewer.capture('test', str(i))
        data=self.viewer.read(after=self.viewer.sequence-2)
        self.assertEqual(len(data['events']),2)
        self.assertGreater(data['first'],1)

    def test_failed_bind_releases_capture_hooks(self):
        bridge = WebUiBridge(self.hh, self.hh.run_gcode, port=0)
        with mock.patch('test.hh.webui_bridge._Server', side_effect=OSError('busy')):
            with self.assertRaises(OSError): bridge.start()
        self.assertNotIn(bridge.viewer.on_telemetry, self.hh.mmu.movement_telemetry.listeners)
        self.assertIsNone(self.hh.printer.harness_stepper_observer)
        self.assertIsNone(self.hh.printer.harness_drive_observer)
        self.assertFalse(pathlib.Path(bridge.uploads.name).exists())

    def test_parameter_groups_match_test_config_owners(self):
        parameters = {p['name']: p for p in self.viewer.parameters(0)}
        self.assertEqual(parameters['timeout_pause']['group'], 'Machine Wide')
        self.assertEqual(parameters['gate_homing_max']['group'], 'Unit 0')
        self.assertEqual(parameters['calibrated_bowden_length']['group'], 'Other · Calibration (unit0 / gate 0)')
        self.assertTrue(all(p['section'] and p['group'] for p in parameters.values()))

    def test_parameters_use_runtime_guards(self):
        names={p['name'] for p in self.viewer.parameters(0)}
        self.assertIn('toolhead_ooze_reduction',names)
        self.assertNotIn('extruder_switch_pin',names)


class TestViewerConsole(unittest.TestCase):
    def setUp(self):
        self.out=io.StringIO()
        with contextlib.redirect_stdout(self.out):
            self.console=Console(parse_args(['--profile','boxturtle','--serve','0','--no-log','--no-live','--pace','0','--plain'])).boot()
        self.bridge=self.console.bridge

    def tearDown(self):
        self.console.close()

    def test_http_capture_does_not_need_console_service(self):
        url='http://127.0.0.1:%d/sim/viewer/events'%self.bridge.port
        with urllib.request.urlopen(url,timeout=2) as response:
            self.assertIn('events',json.load(response)['result'])
        with urllib.request.urlopen(url.replace('/events','/'),timeout=2) as response:
            self.assertIn(b'Live simulator',response.read())

    def configure_macro(self, macro, name, value):
        with contextlib.redirect_stdout(self.out):
            return self.bridge.rpc_sim_viewer_configure(None,
                dict(unit=0, macro=macro, name=name, value=value))

    def test_macro_geometry_is_recorded_and_reverts_without_activity(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_restart(None, {'profile': 'ercf_vvd'})
        viewer = self.bridge.viewer
        initial = viewer.read()['events'][0]
        macro = '_MMU_CUT_TIP_VARS'
        item = next(p for p in viewer.macro_parameters() if p['macro'] == macro and p['name'] == 'blade_pos')
        old = item['value']
        with self.assertRaises(RpcError):
            self.configure_macro(macro, 'blade_pos', "'not a number'")
        self.configure_macro(macro, 'blade_pos', str(old + 5))
        pending = viewer.read()['pending_config']
        self.assertEqual(pending['kind'], 'config')
        self.assertEqual(pending['state']['cutters'][0]['position'], initial['state']['cutters'][0]['position'] - 5)
        self.assertEqual(initial['state']['configuration']['macros'][item['id']], old)
        viewer.capture('state', 'State changed')
        self.assertIsNotNone(viewer.read()['pending_config'])
        self.configure_macro(macro, 'blade_pos', str(old))
        viewer.capture('state', 'State changed')
        self.assertIsNone(viewer.read()['pending_config'])
        self.assertEqual(len(viewer.read()['events']), 1)

    def test_grouped_configuration_precedes_move_with_historical_geometry(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_restart(None, {'profile': 'ercf_vvd'})
        viewer = self.bridge.viewer
        macro = '_MMU_CUT_TIP_VARS'
        old = next(p['value'] for p in viewer.macro_parameters() if p['macro'] == macro and p['name'] == 'blade_pos')
        self.configure_macro(macro, 'blade_pos', str(old + 3))
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_configure(None,dict(unit=0,name='toolhead_ooze_reduction',value='1.5'))
            self.console.hh.mmu.move_filament('after settings', 1, motor='gear')
        events = viewer.read()['events']
        configs = [e for e in events if e['kind'] == 'config']
        self.assertEqual(len(configs), 1)
        self.assertGreaterEqual(len(configs[0]['changes']), 2)
        move = next(e for e in events if e['kind'] == 'movement')
        self.assertLess(configs[0]['seq'], move['seq'])
        self.assertEqual(move['before']['cutters'], configs[0]['state']['cutters'])
        self.assertEqual(move['state']['configuration'], configs[0]['state']['configuration'])
        self.assertNotEqual(events[0]['state']['cutters'], move['state']['cutters'])

    def test_sensor_toggles_coalesce_but_survive_a_move(self):
        viewer = self.bridge.viewer
        name = next(n for n, sensor in viewer.snapshot()['sensors'].items() if sensor['enabled'])
        def toggle(enabled):
            with contextlib.redirect_stdout(self.out):
                self.bridge.rpc_sim_viewer_sensor(None, dict(name=name, enabled=enabled))
                viewer.capture('state', 'State changed')
        toggle(False)
        self.assertFalse(viewer.read()['pending_config']['state']['sensors'][name]['enabled'])
        toggle(True)
        self.assertIsNone(viewer.read()['pending_config'])
        toggle(False)
        with contextlib.redirect_stdout(self.out):
            self.console.hh.mmu.move_filament('disabled sensor', 1, motor='gear')
        toggle(True)
        self.assertEqual(len([e for e in viewer.read()['events'] if e['kind']=='config']), 1)
        self.assertIsNotNone(viewer.read()['pending_config'])
        self.assertTrue(viewer.read()['events'][0]['state']['sensors'][name]['enabled'])

    def test_internal_macro_variables_are_rejected_and_strings_are_quoted(self):
        with self.assertRaises(RpcError):
            self.configure_macro('_MMU_CUT_TIP', 'output_park_pos', '99')
        viewer = self.bridge.viewer
        item = next(p for p in viewer.macro_parameters() if isinstance(p['value'], str))
        text = 'both \" and \' quotes; M112\nnext line'
        self.configure_macro(item['macro'], item['name'], repr(text))
        obj = self.console.hh.printer.lookup_object('gcode_macro ' + item['macro'])
        self.assertEqual(obj.variables[item['name']], text)

    def test_apply_and_reject_hardware(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_configure(None,dict(unit=0,name='toolhead_ooze_reduction',value='1.5'))
        self.assertEqual(self.console.hh.mmu.mmu_unit().toolhead_wrapper.p.toolhead_ooze_reduction,1.5)
        with self.assertRaises(RpcError):
            self.bridge.rpc_sim_viewer_configure(None,dict(unit=0,name='extruder_switch_pin',value='PA1'))

    def test_restart_preserves_server_and_replaces_session(self):
        old=self.bridge.viewer.session_id
        port=self.bridge.port
        with contextlib.redirect_stdout(self.out), mock.patch.object(self.console, 'clear_log') as clear_log:
            self.bridge.rpc_sim_viewer_restart(None,{'profile':'encoder'})
        clear_log.assert_called_once_with()
        self.assertIs(self.console.bridge,self.bridge)
        self.assertEqual(self.bridge.port,port)
        self.assertNotEqual(self.bridge.viewer.session_id,old)
        self.assertEqual(self.console.args.profile,'encoder')
        self.assertIn('Happy Hare console  profile=encoder', self.out.getvalue())
        self.assertIn('Live viewer: http://127.0.0.1:%d/sim/viewer/' % port, self.out.getvalue())

    def test_uploaded_config_and_failure_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            make_install_tree(root)
            files=[dict(name=str(p.relative_to(root)),text=p.read_text()) for p in pathlib.Path(root).rglob('*.cfg')]
            with contextlib.redirect_stdout(self.out):
                self.bridge.rpc_sim_viewer_restart(None,{'files':files})
            self.assertEqual(self.console.hh.mmu.num_gates,4)
        previous=self.console.args.profile
        with contextlib.redirect_stdout(self.out), self.assertRaises(Exception):
            self.bridge.rpc_sim_viewer_restart(None,{'files':[{'name':'printer.cfg','text':'[include mmu/base/*.cfg]'}]})
        self.assertEqual(self.console.args.profile,previous)
        self.assertIsNotNone(self.console.hh.mmu)

    def test_upload_cannot_escape(self):
        for name,text in [('../bad.cfg',''),('printer.cfg','[include /etc/*.cfg]')]:
            with self.assertRaises(RpcError):
                self.bridge.rpc_sim_viewer_restart(None,{'files':[dict(name=name,text=text)]})
