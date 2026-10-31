# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
"""MMU-owned extrusion commands, independent of physical queue completion."""
import unittest
import greenlet
from test.hh import session
from test.hh.live_viewer import LiveViewer


class TestGcodeTelemetry(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.hh.heat_extruder(220)
        self.records = []
        self.unsubscribe = self.hh.mmu.movement_telemetry.subscribe(self.records.append)
        self.scope = self.hh.mmu.gcode_telemetry.scope

    def tearDown(self):
        self.unsubscribe()
        self.hh.close()

    def run_gcode(self, text):
        self.hh.gcode.run_script_from_command(text)

    def outcomes(self):
        return [r for r in self.records if r['event'] == 'movement_outcome']

    def handler(self):
        original = self.hh.gcode.register_command('G1', None)
        self.hh.gcode.register_command('G1', original)
        return original

    def test_scope_restores_exact_handler_and_excludes_print_moves(self):
        original = self.handler()
        self.run_gcode('M83\nG1 E1 F300')
        with self.scope('MMU_TEST'):
            with self.scope('NESTED_MACRO'):
                self.run_gcode('G1 E2 F300\nG1 E-1')
            self.assertNotEqual(self.handler(), original)
        self.assertIs(self.handler(), original)
        self.run_gcode('G1 E1')
        self.assertEqual([r['actual_mm'] for r in self.outcomes()], [2, -1])

    def test_registered_mmu_command_owns_nested_gcode(self):
        from extras.mmu.commands.mmu_base_command import BaseCommand
        BaseCommand(self.hh.mmu).register('MMU_CAPTURE_TEST',
            lambda gcmd: self.run_gcode('M83\nG1 E2 F300'),
            'Test extrusion', '', log=False)
        self.run_gcode('MMU_CAPTURE_TEST')
        self.assertEqual([r['actual_mm'] for r in self.outcomes()], [2])

    def test_coordinate_resets_and_flow_override(self):
        with self.scope('MMU_TEST'):
            self.run_gcode('G90\nM82\nG92 E100\nG1 E102 F300\nM83\nG1 E-1\nM221 S200\nG1 E3')
        self.assertEqual([r['actual_mm'] for r in self.outcomes()], [2, -1, 6])

    def test_unowned_greenlet_and_non_extrusion_commands_are_excluded(self):
        self.run_gcode('M83')
        with self.scope('MMU_TEST'):
            greenlet.greenlet(lambda: self.run_gcode('G1 E1 F300')).switch()
            self.run_gcode('G0 E1\nG1 X1\nG1 E0')
        self.assertEqual(self.outcomes(), [])

    def test_existing_extension_handler_is_called_and_restored(self):
        original = self.hh.gcode.register_command('G1', None)
        calls = []
        def extension(gcmd):
            calls.append(gcmd.get_commandline())
            return original(gcmd)
        self.hh.gcode.register_command('G1', extension)
        with self.scope('MMU_TEST'):
            self.run_gcode('M83\nG1 E1 F300')
        self.assertIs(self.handler(), extension)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.outcomes()), 1)

    def test_existing_movement_operation_prevents_duplicate_g1_events(self):
        telemetry = self.hh.mmu.movement_telemetry
        with self.scope('MMU_TEST'):
            with telemetry.operation():
                self.run_gcode('M83\nG1 E1 F300')
        self.assertEqual(self.outcomes(), [])

    def test_error_restores_handler(self):
        original = self.handler()
        self.run_gcode('M83')
        with self.assertRaises(Exception):
            with self.scope('MMU_TEST'):
                self.run_gcode('G1 E1 F0')
        self.assertIs(self.handler(), original)
        self.assertEqual(self.outcomes()[0]['status'], 'error')

    def test_feed_override_reports_multiplier_and_resolves_command_speed(self):
        with self.scope('MMU_TEST'):
            self.run_gcode('M220 S50\nM83\nG1 E2 F600')
        move = self.hh.printer.lookup_object('gcode_move')
        self.assertEqual(move.get_status()['speed_factor'], .5)
        self.assertEqual(move.get_status()['speed'], 600)
        self.assertEqual(self.outcomes()[0]['speed_mm_s'], 5)

    def test_no_listener_leaves_handler_untouched(self):
        self.unsubscribe()
        original = self.handler()
        with self.scope('MMU_TEST'):
            self.assertIs(self.handler(), original)

    def test_coordinated_move_uses_path_feed_rate(self):
        with self.scope('MMU_TEST'):
            self.run_gcode('G91\nM83\nG1 X3 Y4 E10 F300')
        outcome = self.outcomes()[0]
        self.assertEqual(outcome['actual_mm'], 10)
        self.assertEqual(outcome['estimated_duration_s'], 1)
        self.assertEqual(outcome['extrusion_speed_mm_s'], 10)

    def test_macro_scope_and_viewer_emit_one_row_per_command(self):
        viewer = LiveViewer(self.hh)
        try:
            self.hh.gcode.register_command('TEST_EXTRUSION_MACRO',
                lambda gcmd: self.run_gcode('M83\nG1 E2 F300\nG1 E-1'))
            self.hh.mmu.wrap_gcode_command('TEST_EXTRUSION_MACRO', exception=None)
            self.run_gcode('G1 E4')
            with self.scope('MMU_TEST'):
                self.run_gcode('G0 E1\nG1 X1')
            events = viewer.read()['events']
            moves = [e for e in events if e['kind'] == 'movement']
            self.assertEqual([e['distance'] for e in moves], [2, -1])
            self.assertFalse(any(e['kind'] == 'filament' for e in events))
        finally:
            viewer.close()


if __name__ == '__main__':
    unittest.main()
