# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify the simulator's Happy Hare Console controls and replay capture.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test.hh import session
from test.hh.console_support import ConsoleSupport
from test.hh.webui_bridge import WebUiBridge, RpcError
from test.console import Console, parse_args
from test.test_mmu_console import make_install_tree


class TestConsoleSupport(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.support = ConsoleSupport(self.hh)

    def tearDown(self):
        self.support.close()
        self.hh.close()

    def test_failed_bind_removes_the_upload_folder(self):
        bridge = WebUiBridge(self.hh, self.hh.run_gcode, port=0)
        with mock.patch('test.hh.webui_bridge._Server', side_effect=OSError('busy')):
            with self.assertRaises(OSError): bridge.start()
        self.assertFalse(pathlib.Path(bridge.uploads.name).exists())

    def test_parameter_groups_match_test_config_owners(self):
        parameters = {p['name']: p for p in self.support.parameters(0)}
        self.assertEqual(parameters['timeout_pause']['group'], 'Machine Wide')
        self.assertEqual(parameters['gate_homing_max']['group'], 'Unit 0')
        self.assertEqual(parameters['calibrated_bowden_length']['group'], 'Other · Calibration (unit0 / gate 0)')
        self.assertTrue(all(p['section'] and p['group'] for p in parameters.values()))

    def test_parameters_use_runtime_guards(self):
        names={p['name'] for p in self.support.parameters(0)}
        self.assertIn('toolhead_ooze_reduction',names)
        self.assertNotIn('extruder_switch_pin',names)


class TestConsoleHosting(unittest.TestCase):
    def setUp(self):
        self.out=io.StringIO()
        with contextlib.redirect_stdout(self.out):
            self.console=Console(parse_args(['--profile','boxturtle','--serve','0','--no-log','--no-live','--pace','0','--plain'])).boot()
        self.bridge=self.console.bridge

    def tearDown(self):
        self.console.close()

    def test_the_unit_footer_can_read_each_unit_from_the_bridge(self):
        self.assertIn('mmu_machine', self.bridge.rpc_printer_objects_list(None, {})['objects'])
        unit = self.bridge.rpc_printer_objects_query(None, {'objects': {'mmu_machine': None}})['status']['mmu_machine']['unit_0']
        self.assertEqual((unit['vendor'], unit['display_name']), ('BoxTurtle', 'BoxTurtle-0'))

    def test_the_console_is_served_from_its_own_checkout(self):
        url = 'http://127.0.0.1:%d/sim/console/' % self.bridge.port
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {'HH_CONSOLE_DIR': folder}):
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(url, timeout=2)
            self.assertIn('git clone', missing.exception.read().decode())
            for name in ('index.html', 'app.js'):
                with open(os.path.join(folder, name), 'w') as f:
                    f.write(name)
            for name in ('', 'app.js'):
                with urllib.request.urlopen(url + name, timeout=2) as response:
                    self.assertEqual(response.read().decode(), name or 'index.html')

    def run_script(self, script):
        with contextlib.redirect_stdout(self.out):
            return self.bridge.rpc_printer_gcode_script(None, {'script': script})

    def configure_macro(self, macro, name, value):
        self.run_script('SET_GCODE_VARIABLE MACRO=%s VARIABLE=%s VALUE="%s"' % (macro, name, value))

    def test_macro_edit_rides_on_the_next_event_and_cancelled_edits_leave_nothing(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_console_restart(None, {'profile': 'ercf_vvd'})
        support = self.bridge.console_support
        initial = support.read_telemetry()['records'][0]
        macro = '_MMU_CUT_TIP_VARS'
        old = initial['state']['macros'][macro]['blade_pos']
        self.configure_macro(macro, 'blade_pos', str(old + 5))
        self.assertEqual(support.read_telemetry()['records'], [initial])
        self.configure_macro(macro, 'blade_pos', str(old))
        self.console.hh.mmu.movement_telemetry.state('cut', cutter='toolhead')
        self.assertEqual([r['event'] for r in support.read_telemetry()['records']], ['session', 'cut'])
        self.configure_macro(macro, 'blade_pos', str(old + 5))
        self.console.hh.mmu.movement_telemetry.state('cut', cutter='toolhead')
        delta = support.read_telemetry()['records'][-2]
        self.assertEqual(delta['event'], 'state_delta')
        self.assertIn(dict(path=['macros', macro, 'blade_pos'], value=old+5), delta['changes'])

    def test_grouped_configuration_precedes_move_with_historical_geometry(self):
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_console_restart(None, {'profile': 'ercf_vvd'})
        support = self.bridge.console_support
        macro = '_MMU_CUT_TIP_VARS'
        old = next(p['value'] for p in support.macro_parameters() if p['macro'] == macro and p['name'] == 'blade_pos')
        self.configure_macro(macro, 'blade_pos', str(old + 3))
        self.run_script('MMU_TEST_CONFIG UNIT=0 TOOLHEAD_OOZE_REDUCTION=1.5')
        with contextlib.redirect_stdout(self.out):
            self.console.hh.mmu.move_filament('after settings', 1, motor='gear')
        records = support.read_telemetry()['records']
        config = next(r for r in records if r['event'] == 'state_delta')
        self.assertGreaterEqual(len(config['changes']), 2)
        self.assertLess(config['sequence'], next(r['sequence'] for r in records if r['event'] == 'movement_start'))
        self.assertEqual(records[0]['state']['macros'][macro]['blade_pos'], old)

    def test_sensor_toggles_ride_on_the_next_event(self):
        support = self.bridge.console_support
        initial = support.read_telemetry()['records'][0]
        name = next(n for n, sensor in initial['state']['printer']['mmu']['sensors'].items() if sensor['enabled'])
        def toggle(enabled):
            with contextlib.redirect_stdout(self.out):
                self.bridge.rpc_sim_console_sensor(None, dict(name=name, enabled=enabled))
        toggle(False)
        toggle(True)
        self.assertEqual(support.read_telemetry()['records'], [initial])
        toggle(False)
        self.assertEqual(support.read_telemetry()['records'], [initial])
        with contextlib.redirect_stdout(self.out):
            self.console.hh.mmu.move_filament('disabled sensor', 1, motor='gear')
        records = support.read_telemetry()['records']
        self.assertTrue(any(r['event'] == 'state_delta' for r in records))
        self.assertTrue(initial['state']['printer']['mmu']['sensors'][name]['enabled'])

    def test_escaped_string_literals_reach_a_macro_variable_unchanged(self):
        # The Happy Hare Console escapes every character (editor.js); the printer must decode it back exactly.
        item = next(p for p in self.bridge.console_support.macro_parameters() if isinstance(p['value'], str))
        text = 'both " and \' quotes; M112\nnext line'
        literal = "'" + ''.join('\\U%08x' % ord(c) for c in text) + "'"
        self.configure_macro(item['macro'], item['name'], literal)
        obj = self.console.hh.printer.lookup_object('gcode_macro ' + item['macro'])
        self.assertEqual(obj.variables[item['name']], text)

    def test_apply_and_reject_hardware(self):
        self.run_script('MMU_TEST_CONFIG UNIT=0 TOOLHEAD_OOZE_REDUCTION=1.5')
        self.assertEqual(self.console.hh.mmu.mmu_unit().toolhead_wrapper.p.toolhead_ooze_reduction,1.5)
        with self.assertRaises(RpcError):
            self.run_script('MMU_TEST_CONFIG UNIT=0 EXTRUDER_SWITCH_PIN=PA1')

    def test_restart_preserves_server_and_replaces_session(self):
        old=self.console.hh.mmu.movement_telemetry.session_id
        port=self.bridge.port
        with contextlib.redirect_stdout(self.out), mock.patch.object(self.console, 'clear_log') as clear_log:
            self.bridge.rpc_sim_console_restart(None,{'profile':'encoder'})
        clear_log.assert_called_once_with()
        self.assertIs(self.console.bridge,self.bridge)
        self.assertEqual(self.bridge.port,port)
        self.assertNotEqual(self.console.hh.mmu.movement_telemetry.session_id,old)
        self.assertEqual(self.console.args.profile,'encoder')
        self.assertIn('Happy Hare console  profile=encoder', self.out.getvalue())
        self.assertIn('Happy Hare Console: http://127.0.0.1:%d/sim/console/' % port, self.out.getvalue())

    def test_restart_tells_connected_pages_the_printer_is_ready_again(self):
        sent=[]
        self.bridge.clients.add(mock.Mock(send_text=sent.append))
        with contextlib.redirect_stdout(self.out):
            self.bridge.rpc_sim_console_restart(None,{'profile':'encoder'})
        self.assertTrue(any('notify_klippy_ready' in text for text in sent))

    def test_uploaded_config_and_failure_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            make_install_tree(root)
            files=[dict(name=str(p.relative_to(root)),text=p.read_text()) for p in pathlib.Path(root).rglob('*.cfg')]
            with contextlib.redirect_stdout(self.out):
                self.bridge.rpc_sim_console_restart(None,{'files':files})
            self.assertEqual(self.console.hh.mmu.num_gates,4)
        previous=self.console.args.profile
        with contextlib.redirect_stdout(self.out), self.assertRaises(Exception):
            self.bridge.rpc_sim_console_restart(None,{'files':[{'name':'printer.cfg','text':'[include mmu/base/*.cfg]'}]})
        self.assertEqual(self.console.args.profile,previous)
        self.assertIsNotNone(self.console.hh.mmu)

    def test_uploaded_mmu_folder_boots_without_a_printer_cfg(self):
        with tempfile.TemporaryDirectory() as root:
            make_install_tree(root)
            files=[dict(name=str(p.relative_to(root)),text=p.read_text()) for p in (pathlib.Path(root)/'mmu').rglob('*.cfg')]
            with contextlib.redirect_stdout(self.out):
                self.assertEqual(self.bridge.rpc_sim_console_restart(None,{'files':files}),{'profile':'mmu'})
            self.assertEqual(self.console.hh.mmu.num_gates,4)

    def test_upload_boots_from_the_outermost_printer_cfg_and_ignores_includes_it_never_reads(self):
        root=pathlib.Path(self.bridge._store_upload([
            dict(name='config/printer.cfg',text='[include /home/pi/extra.cfg]\n[include mmu/base/*.cfg]'),
            dict(name='config/backup/printer.cfg',text=''),dict(name='config/mmu/base/mmu.cfg',text='')]))
        self.assertEqual(self.bridge._upload_config_dir(root),root/'config')
        for files in ([dict(name='base/mmu.cfg',text='')],[dict(name='printer.cfg',text=''),dict(name='mmu.cfg',text='')]):
            with self.assertRaises(RpcError):
                self.bridge._upload_config_dir(pathlib.Path(self.bridge._store_upload(files)))

    def test_upload_cannot_escape(self):
        for name,text in [('../bad.cfg',''),('printer.cfg','[include mmu/../../etc/*.cfg]'),('mmu/base/mmu.cfg','[include /etc/*.cfg]'),(3,'x'),('printer.cfg',None)]:
            with self.assertRaises(RpcError):
                self.bridge.rpc_sim_console_restart(None,{'files':[dict(name=name,text=text)]})
