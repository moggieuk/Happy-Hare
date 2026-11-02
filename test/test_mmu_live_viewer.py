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
from test.hh.live_viewer import SourceViewer
from test.hh.webui_bridge import WebUiBridge, RpcError
from test.console import Console, parse_args
from test.test_mmu_console import make_install_tree


class TestSourceViewer(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.viewer = SourceViewer(self.hh)

    def tearDown(self):
        self.viewer.close()
        self.hh.close()

    def test_operation_markers_do_not_invoke_toolchange_macro_hooks(self):
        for command in ('MMU_LOAD', 'MMU_UNLOAD', 'MMU_CHANGE_TOOL TOOL=0'):
            previous = self.viewer.read_telemetry()['records'][-1]['sequence']
            with mock.patch.object(self.hh.mmu, 'mmu_macro_event') as macro:
                self.hh.run_gcode(command)
            records = self.viewer.read_telemetry(after=previous)['records']
            markers = [r for r in records if r['event']=='toolchange']
            self.assertEqual(len(markers), 1, command)
            self.assertEqual(markers[0]['label'], command)
            self.assertFalse(any(c.args[0]=='toolchange' for c in macro.call_args_list))
            starts = [r for r in records if r['event']=='movement_start']
            if starts:
                self.assertLess(markers[0]['sequence'], starts[0]['sequence'])

    def test_real_macros_produce_source_moves_and_ordered_cut_hooks(self):
        for profile in ('ercf_vvd', 'encoder'):
            with self.subTest(profile=profile):
                console = Console(parse_args(['--profile',profile,'--no-serve','--no-log','--plain']))
                viewer = None
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        console.boot()
                        viewer = SourceViewer(console.hh)
                        console.run_command('MMU_LOAD')
                        previous = viewer.read_telemetry()['records'][-1]['sequence']
                        console.run_command('MMU_UNLOAD')
                    self.assertEqual(console.hh.errors, [])
                    records = viewer.read_telemetry(after=previous)['records']
                    moves = [r for r in records if r['event']=='movement_start']
                    self.assertTrue(any(r['source']=='mmu_gcode' for r in moves))
                    self.assertFalse(any(r['source'].startswith('harness_') for r in moves))
                    self.assertEqual(sum(r['event']=='movement_outcome' for r in records),len(moves))
                    hooks = [r['event'] for r in records if r['event'] in ('cut','release')]
                    self.assertEqual(hooks, ['cut','release'] if profile=='ercf_vvd' else [])
                finally:
                    if viewer: viewer.close()
                    console.close()

    def test_raw_telemetry_reads_do_not_expose_mutable_capture_records(self):
        self.hh.mmu.move_filament('Original label', 1)
        record = next(r for r in self.viewer.read_telemetry()['records'] if r['event'] == 'movement_start')
        record['label'] = 'Changed by reader'
        record['sensors'].clear()
        reread = next(r for r in self.viewer.read_telemetry()['records'] if r['event'] == 'movement_start')
        self.assertEqual(reread['label'], 'Original label')
        self.assertTrue(reread['sensors'])

    def test_failed_bind_releases_capture_hooks(self):
        bridge = WebUiBridge(self.hh, self.hh.run_gcode, port=0)
        with mock.patch('test.hh.webui_bridge._Server', side_effect=OSError('busy')):
            with self.assertRaises(OSError): bridge.start()
        self.assertNotIn(bridge.viewer.telemetry_capture.on_event, self.hh.mmu.movement_telemetry.listeners)
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
        url='http://127.0.0.1:%d/sim/viewer/telemetry'%self.bridge.port
        with urllib.request.urlopen(url,timeout=2) as response:
            self.assertEqual(json.load(response)['result']['records'][0]['event'], 'session')
        with urllib.request.urlopen(url.replace('/telemetry','/'),timeout=2) as response:
            self.assertIn(b'Live simulator',response.read())

    def configure_macro(self, macro, name, value):
        with contextlib.redirect_stdout(self.out):
            return self.bridge.rpc_sim_viewer_configure(None,
                dict(unit=0, macro=macro, name=name, value=value))

    def test_macro_geometry_is_recorded_and_reverts_without_activity(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_viewer_restart(None, {'profile': 'ercf_vvd'})
        viewer = self.bridge.viewer
        initial = viewer.read_telemetry()['records'][0]
        macro = '_MMU_CUT_TIP_VARS'
        old = initial['state']['macros'][macro]['blade_pos']
        with self.assertRaises(RpcError):
            self.configure_macro(macro, 'blade_pos', "'not a number'")
        self.configure_macro(macro, 'blade_pos', str(old + 5))
        pending = viewer.read_telemetry()['records'][-1]
        self.assertTrue(pending['pending'])
        self.assertIn(dict(path=['macros', macro, 'blade_pos'], value=old+5), pending['changes'])
        self.configure_macro(macro, 'blade_pos', str(old))
        self.assertEqual(viewer.read_telemetry()['records'], [initial])

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
        records = viewer.read_telemetry()['records']
        config = next(r for r in records if r['event'] == 'state_delta')
        self.assertGreaterEqual(len(config['changes']), 2)
        self.assertLess(config['sequence'], next(r['sequence'] for r in records if r['event'] == 'movement_start'))
        self.assertEqual(records[0]['state']['macros'][macro]['blade_pos'], old)
        self.assertFalse(any(r.get('pending') for r in records))

    def test_sensor_toggles_coalesce_but_survive_a_move(self):
        viewer = self.bridge.viewer
        initial = viewer.read_telemetry()['records'][0]
        name = next(n for n, sensor in initial['state']['printer']['mmu']['sensors'].items() if sensor['enabled'])
        def toggle(enabled):
            with contextlib.redirect_stdout(self.out):
                self.bridge.rpc_sim_viewer_sensor(None, dict(name=name, enabled=enabled))
        toggle(False)
        self.assertTrue(viewer.read_telemetry()['records'][-1]['pending'])
        toggle(True)
        self.assertEqual(viewer.read_telemetry()['records'], [initial])
        toggle(False)
        with contextlib.redirect_stdout(self.out):
            self.console.hh.mmu.move_filament('disabled sensor', 1, motor='gear')
        toggle(True)
        records = viewer.read_telemetry()['records']
        self.assertTrue(records[-1]['pending'])
        self.assertTrue(any(r['event']=='state_delta' and not r.get('pending') for r in records))
        self.assertTrue(initial['state']['printer']['mmu']['sensors'][name]['enabled'])

    def test_running_bridge_does_not_observe_simulator_geometry(self):
        with mock.patch.object(self.console.hh, 'filament', side_effect=AssertionError('simulator geometry')):
            self.bridge.viewer.observe_configuration()
            self.bridge.viewer.read_telemetry()
        self.assertIsNone(getattr(self.console.hh.printer, 'harness_stepper_observer', None))
        self.assertIsNone(getattr(self.console.hh.printer, 'harness_drive_observer', None))

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
