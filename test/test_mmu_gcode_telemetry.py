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

    def test_real_g1_extrusion_updates_physical_step_count(self):
        # _do_form_tip must measure actual G1 travel when calculating fragments.
        stepper = self.hh.printer.lookup_object('toolhead').get_extruder().extruder_stepper.stepper
        before = stepper.get_mcu_position()
        self.run_gcode('M83\nG1 E-29.5 F1500')
        measured = (before-stepper.get_mcu_position())*stepper.get_step_dist()
        self.assertAlmostEqual(measured, 29.5, places=2)

    def test_restore_keeps_extrusion_and_preserves_absolute_e_origin(self):
        stepper = self.hh.printer.lookup_object('toolhead').get_extruder().extruder_stepper.stepper
        before = stepper.get_mcu_position()
        self.run_gcode('G90\nM82\nG92 E0\nG1 E10 F300\nSAVE_GCODE_STATE NAME=a\nM83\nG1 E-4\nRESTORE_GCODE_STATE NAME=a\nG1 E12')
        measured = (stepper.get_mcu_position()-before)*stepper.get_step_dist()
        self.assertAlmostEqual(measured, 8, places=2)
        self.assertEqual(self.hh.errors, [])

    def test_coordinate_resets_and_restore_do_not_create_physical_extrusion(self):
        stepper = self.hh.printer.lookup_object('toolhead').get_extruder().extruder_stepper.stepper
        self.run_gcode('M83\nG1 E3 F300')
        before = stepper.get_mcu_position()
        self.run_gcode('G92 E200\nSAVE_GCODE_STATE NAME=a\nG92 E0\nRESTORE_GCODE_STATE NAME=a MOVE=1')
        self.assertEqual(stepper.get_mcu_position(), before)

    def test_manual_and_synced_moves_update_each_motor_once(self):
        drive = self.hh.mmu.drive()
        gear = drive.mmu_gear_stepper.get_steppers()[0]
        extruder = drive.mmu_extruder_stepper.get_steppers()[0]
        for motor, expected in [('gear', (4,0)), ('extruder', (0,4)), ('gear+extruder', (4,4))]:
            with self.subTest(motor=motor):
                before = [s.get_mcu_position() for s in (gear,extruder)]
                self.hh.mmu.move_filament('Accounting audit',4,motor=motor)
                for stepper, start, distance in zip((gear,extruder),before,expected):
                    self.assertAlmostEqual((stepper.get_mcu_position()-start)*stepper.get_step_dist(),distance,places=2)

    def test_synced_g1_updates_motor_counts_without_doubling_viewer_totals(self):
        from extras.mmu.mmu_constants import DRIVE_GEAR_SYNCED_TO_EXTRUDER
        drive = self.hh.mmu.drive()
        drive.sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        motors = [drive.mmu_gear_stepper.get_steppers()[0],drive.mmu_extruder_stepper.get_steppers()[0]]
        before = [s.get_mcu_position() for s in motors]
        viewer = LiveViewer(self.hh)
        self.addCleanup(viewer.close)
        with self.scope('MMU_TEST'):
            self.run_gcode('M83\nG1 E3 F300')
        for stepper, start in zip(motors,before):
            self.assertAlmostEqual((stepper.get_mcu_position()-start)*stepper.get_step_dist(),3,places=2)
        event = viewer.read()['events'][-1]
        self.assertEqual(sorted(event['motors'].values()), [3,3])

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

    def test_unsynced_g1_moves_filament_only_when_gripped_by_extruder(self):
        from extras.mmu.mmu_constants import DRIVE_EXTRUDER_ONLY
        self.hh.mmu.drive().sync_mode(DRIVE_EXTRUDER_ONLY)
        model = self.hh.filament()
        self.hh.place_filament(0, 750)
        with self.scope('MMU_TEST'):
            self.run_gcode('M83\nG1 E2 F300')
        self.assertAlmostEqual(model.tip[0], 752)
        self.hh.place_filament(0, 100)
        with self.scope('MMU_TEST'):
            self.run_gcode('G1 E2')
        self.assertAlmostEqual(model.tip[0], 100)

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
            self.assertFalse(any(e['kind'] in ('filament', 'stepper') for e in events))
            records = viewer.read_telemetry()['records']
            self.assertEqual(sum(r['event'] == 'movement_start' for r in records), 2)
            self.assertEqual(sum(r['event'] == 'movement_outcome' for r in records), 2)
        finally:
            viewer.close()


if __name__ == '__main__':
    unittest.main()
