"""Calibration must read sensors without dispatching filament event callbacks."""
import logging
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class CalibrationEventsTestCase(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu
        self.mmu.select_gate(0)
        self.hh.settle(3.)

    def tearDown(self):
        self.hh.close()

    def exercise_edges(self):
        for sensor in self.mmu.sensor_manager.all_sensors_map.values():
            helper = sensor.runout_helper
            before_enabled = helper.sensor_enabled
            # A previously queued handler finishing must not reopen event dispatch.
            helper._exec_gcode(None)
            with patch.object(helper, '_process_state_change') as dispatch:
                for present in (True, False):
                    helper.note_filament_present(self.hh.reactor.monotonic(), present)
                    self.assertEqual(helper.filament_present, present)
                dispatch.assert_not_called()
            self.assertEqual(helper.sensor_enabled, before_enabled)

    def test_all_sensors_suppressed_across_gate_changes_and_nested_operations(self):
        self.mmu._enable_filament_monitoring()
        with self.mmu.wrap_suspend_calibration_events():
            self.exercise_edges()
            self.mmu.select_gate(1)
            with self.mmu.wrap_suspend_insert_events():
                with self.mmu.wrap_suspend_calibration_events():
                    self.exercise_edges()
            self.exercise_edges()
        self.assertTrue(self.mmu.filament_monitoring_enabled)
        for sensor in self.mmu.sensor_manager.all_sensors_map.values():
            helper = sensor.runout_helper
            self.assertFalse(helper.events_suspended)
            with patch.object(helper, '_process_state_change') as dispatch:
                helper.note_filament_present(self.hh.reactor.monotonic(), True)
                dispatch.assert_called_once()

    def test_exception_restores_existing_suspension_and_monitoring(self):
        self.mmu._disable_filament_monitoring()
        with self.mmu.wrap_suspend_insert_events():
            with self.assertRaisesRegex(RuntimeError, 'calibration failed'):
                with self.mmu.wrap_suspend_calibration_events():
                    raise RuntimeError('calibration failed')
            self.assertTrue(all(s.runout_helper.events_suspended
                                for s in self.mmu.sensor_manager.active_sensors_map.values()))
        self.assertFalse(self.mmu.filament_monitoring_enabled)
        self.assertFalse(any(s.runout_helper.events_suspended
                             for s in self.mmu.sensor_manager.all_sensors_map.values()))

    def test_toolhead_clean_command_suppresses_edges_during_probe(self):
        from extras.mmu.commands.mmu_calibration_commands import MmuCalibrateToolheadCommand
        # Supply the toolhead hardware/motion boundaries missing from this profile;
        # run the real registered command and its sensor helpers.
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.mmu.sensor_manager, 'has_sensor', return_value=True))
            for method in ('initialize_filament_position', '_load_gate', '_load_bowden',
                           '_home_to_extruder', '_unload_bowden', '_unload_gate'):
                stack.enter_context(patch.object(self.mmu, method, return_value=0.))
            probe = stack.enter_context(patch.object(
                MmuCalibrateToolheadCommand, '_probe_toolhead', side_effect=self.probe))
            self.hh.run_gcode('MMU_CALIBRATE_TOOLHEAD CLEAN=1 SAVE=0')
            probe.assert_called_once()
        self.assertEqual(self.hh.errors, [])

    def probe(self):
        self.exercise_edges()
        return 50., 40., 10.


if __name__ == '__main__':
    unittest.main()
