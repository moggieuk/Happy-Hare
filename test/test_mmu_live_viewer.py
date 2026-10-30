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
        self.hh.mmu.move_filament('Mix', 10, motor='extruder')
        color = self.viewer.read()['events'][-1]['state']['nozzle_color']
        self.assertNotEqual(color, 'ff00ff')
        self.assertNotEqual(color, '00ffff')

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
        self.assertNotIn(bridge.viewer.on_filament, self.hh.filament().observers)
        self.assertFalse(pathlib.Path(bridge.uploads.name).exists())

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

    def test_apply_and_reject_hardware(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_configure(None,dict(unit=0,name='toolhead_ooze_reduction',value='1.5'))
        self.assertEqual(self.console.hh.mmu.mmu_unit().toolhead_wrapper.p.toolhead_ooze_reduction,1.5)
        with self.assertRaises(RpcError):
            self.bridge.rpc_sim_viewer_configure(None,dict(unit=0,name='extruder_switch_pin',value='PA1'))

    def test_restart_preserves_server_and_replaces_session(self):
        old=self.bridge.viewer.session_id
        port=self.bridge.port
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_restart(None,{'profile':'encoder'})
        self.assertIs(self.console.bridge,self.bridge)
        self.assertEqual(self.bridge.port,port)
        self.assertNotEqual(self.bridge.viewer.session_id,old)
        self.assertEqual(self.console.args.profile,'encoder')

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
