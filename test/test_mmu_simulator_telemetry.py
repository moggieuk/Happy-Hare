# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify the simulator's telemetry capture and its Moonraker-compatible bridge.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import contextlib
import io
import unittest
from unittest import mock

from test.hh import session
from test.hh.webui_bridge import WebUiBridge
from test.console import Console, parse_args


class TestSimulatorCapture(unittest.TestCase):
    def setUp(self):
        from extras.mmu.telemetry.capture import TelemetryCapture
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.capture = TelemetryCapture(self.hh.mmu, limit=6000)

    def tearDown(self):
        self.capture.close()
        self.hh.close()

    def test_operation_markers_do_not_invoke_toolchange_macro_hooks(self):
        for command in ('MMU_LOAD', 'MMU_UNLOAD', 'MMU_CHANGE_TOOL TOOL=0'):
            previous = self.capture.read()['records'][-1]['sequence']
            with mock.patch.object(self.hh.mmu, 'mmu_macro_event') as macro:
                self.hh.run_gcode(command)
            records = self.capture.read(after=previous)['records']
            markers = [r for r in records if r['event']=='toolchange']
            self.assertEqual(len(markers), 1, command)
            self.assertEqual(markers[0]['label'], command)
            self.assertFalse(any(c.args[0]=='toolchange' for c in macro.call_args_list))
            starts = [r for r in records if r['event']=='movement_start']
            if starts:
                self.assertLess(markers[0]['sequence'], starts[0]['sequence'])

    def test_raw_telemetry_reads_do_not_expose_mutable_capture_records(self):
        self.hh.mmu.move_filament('Original label', 1)
        record = next(r for r in self.capture.read()['records'] if r['event'] == 'movement_start')
        record['label'] = 'Changed by reader'
        record['sensors'].clear()
        reread = next(r for r in self.capture.read()['records'] if r['event'] == 'movement_start')
        self.assertEqual(reread['label'], 'Original label')
        self.assertTrue(reread['sensors'])


class TestSimulatorMacros(unittest.TestCase):
    def test_real_macros_produce_source_moves_and_ordered_cut_hooks(self):
        from extras.mmu.telemetry.capture import TelemetryCapture
        for profile in ('ercf_vvd', 'encoder'):
            with self.subTest(profile=profile):
                console = Console(parse_args(['--profile',profile,'--no-serve','--no-log','--plain']))
                capture = None
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        console.boot()
                        capture = TelemetryCapture(console.hh.mmu, limit=6000)
                        console.run_command('MMU_LOAD')
                        previous = capture.read()['records'][-1]['sequence']
                        console.run_command('MMU_UNLOAD')
                    self.assertEqual(console.hh.errors, [])
                    records = capture.read(after=previous)['records']
                    moves = [r for r in records if r['event']=='movement_start']
                    self.assertTrue(any(r['source']=='mmu_gcode' for r in moves))
                    self.assertFalse(any(r['source'].startswith('harness_') for r in moves))
                    self.assertEqual(sum(r['event']=='movement_outcome' for r in records),len(moves))
                    hooks = [r['event'] for r in records if r['event'] in ('cut','release')]
                    self.assertEqual(hooks, ['cut','release'] if profile=='ercf_vvd' else [])
                finally:
                    if capture: capture.close()
                    console.close()

    def test_real_purge_macro_restores_absolute_extrusion_mode(self):
        console = Console(parse_args(['--profile','ercf_vvd','--no-serve','--no-log','--plain']))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                console.boot()
                gcode_move = console.hh.printer.lookup_object('gcode_move')
                gcode_move.absolute_extrude = True
                console.run_command('MMU_TEST_PURGE')
            self.assertEqual(console.hh.errors, [])
            self.assertTrue(gcode_move.absolute_extrude)
        finally:
            console.close()


class TestSimulatorBridge(unittest.TestCase):
    def test_failed_bind_releases_capture_hooks(self):
        hh = session('boxturtle')
        hh.boot(calibrate=True, selected_gate=0)
        try:
            bridge = WebUiBridge(hh, hh.run_gcode, port=0)
            with mock.patch('test.hh.webui_bridge._Server', side_effect=OSError('busy')):
                with self.assertRaises(OSError): bridge.start()
            self.assertNotIn(bridge.telemetry_capture.on_event, hh.mmu.movement_telemetry.listeners)
        finally:
            hh.close()

    def test_the_bridge_keeps_capturing_without_a_browser_and_ends_a_one_toolchange_recording(self):
        hh = session('boxturtle')
        hh.boot(calibrate=True, selected_gate=0)
        bridge = WebUiBridge(hh, hh.run_gcode, port=0)
        try:
            stream, telemetry = bridge.telemetry_stream, hh.mmu.movement_telemetry
            stream.subscribe('browser', lambda batch: None, lambda: False)
            stream.unsubscribe('browser')
            self.assertIs(stream.capture, bridge.telemetry_capture)   # Still recording for whoever connects next
            self.assertIn(bridge.telemetry_capture.on_event, telemetry.listeners)
            # A browser that acknowledged everything and left (a reload) does not take the history with it
            telemetry.state('cut')
            first = stream.subscribe('browser', lambda batch: None, lambda: False)
            stream.ack('browser', first['stream_id'], first['delivery_id'])
            stream.unsubscribe('browser')
            again = stream.subscribe('reloaded', lambda batch: None, lambda: False)
            self.assertFalse(again['records'][0].get('checkpoint'))
            self.assertEqual([r['sequence'] for r in again['records']], [r['sequence'] for r in first['records']])
            stream.unsubscribe('reloaded')
            stream.set_record_mode('toolchange')
            telemetry.toolchange_start(command='MMU_LOAD'); telemetry.state('cut'); telemetry.toolchange_end(command='MMU_LOAD')
            self.assertEqual(stream.record_mode, 'off')
        finally:
            bridge.close()
            hh.close()

    def test_console_runs_tool_selection_macro(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console = Console(parse_args(['--profile','boxturtle','--serve','0','--no-log','--no-live','--pace','0','--plain'])).boot()
        try:
            with contextlib.redirect_stdout(out):
                console.bridge.rpc_printer_gcode_script(None, {'script':'T1'})
            self.assertEqual(console.hh.errors,[])
            self.assertEqual(console.hh.mmu.tool_selected,1)
            self.assertEqual(console.hh.mmu.filament_pos,10)
        finally:
            console.close()


if __name__ == '__main__':
    unittest.main()
