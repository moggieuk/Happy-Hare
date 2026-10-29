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
        motors = [e for e in events if e['kind']=='stepper']
        self.assertTrue(any('gear' in e['motor'] for e in motors), motors)
        self.assertTrue(any('extruder' in e['motor'] for e in motors), motors)
        self.assertEqual(len(motors), 2)
        self.assertEqual([e['distance'] for e in motors], [12, 12])
        self.assertTrue(any(e['kind']=='filament' for e in events))
        self.assertNotEqual(before['state']['tips'][0], events[-1]['state']['tips'][0])
        json.dumps(self.viewer.read(), allow_nan=False)

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
