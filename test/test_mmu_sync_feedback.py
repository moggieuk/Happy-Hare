# Happy Hare: buffer status follows physical virtual-sensor hysteresis.
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
from unittest.mock import patch

from test.hh import session


class TestProportionalSyncFeedbackString(unittest.TestCase):
    def setUp(self):
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.prop = self.hh.sensor('filament_proportional')
        self.sf = self.hh.mmu.mmu_unit(0).sync_feedback

    def feed(self, value):
        sensor = self.prop.sensor
        span = sensor._d_pos if value >= 0 else sensor._d_neg
        self.prop.feed(sensor._neutral_point + value * span)
        self.assertAlmostEqual(self.prop.value, value)

    def state(self):
        return self.sf.get_sync_feedback_string(detail=True)

    def test_mid_range_is_neutral(self):
        for value in (-0.5, -0.001, 0.0, 0.084, 0.5):
            with self.subTest(value=value):
                self.feed(value)
                self.assertEqual(self.state(), 'neutral')

    def test_hysteresis_both_directions_and_direct_extreme_transitions(self):
        for value, expected in (
            (0.0, 'neutral'), (0.88, 'neutral'), (0.91, 'compressed'),
            (0.88, 'compressed'), (0.86, 'neutral'),
            (-0.88, 'neutral'), (-0.91, 'tension'),
            (-0.88, 'tension'), (-0.86, 'neutral'),
            (-0.95, 'tension'), (0.95, 'compressed'), (-0.95, 'tension'),
        ):
            with self.subTest(value=value, expected=expected):
                self.feed(value)
                self.assertEqual(self.state(), expected)
                sm = self.hh.mmu.sensor_manager
                self.assertEqual(sm.check_sensor('filament_tension'), expected == 'tension')
                self.assertEqual(sm.check_sensor('filament_compression'), expected == 'compressed')

    def test_disabled_virtual_switches_do_not_hide_physical_state(self):
        sm = self.hh.mmu.sensor_manager
        for generic in ('filament_compression', 'filament_tension'):
            name = sm.get_sensor_obj(generic).runout_helper.name
            self.hh.run_gcode('MMU_SENSORS SENSOR=%s ENABLE=0' % name)
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(sm.has_sensor('filament_proportional'))
        for value, expected in ((0.95, 'compressed'), (0.88, 'compressed'),
                                (0.5, 'neutral'), (-0.95, 'tension')):
            with self.subTest(value=value):
                self.feed(value)
                self.assertEqual(self.state(), expected)

    def test_disabled_analog_sensor_retains_discrete_fallback(self):
        self.hh.run_gcode('MMU_SENSORS SENSOR=unit0:filament_proportional ENABLE=0')
        self.assertEqual(self.hh.errors, [])
        self.feed(0.95)
        self.assertEqual(self.state(), 'compressed')
        self.feed(0.0)
        self.assertEqual(self.state(), 'neutral')

    def test_status_is_independent_of_controller_mode(self):
        ctrl = self.sf.ctrl
        self.sf.active = True
        self.sf.p.sync_feedback_enabled = True
        for twolevel in (False, True):
            ctrl.cfg.use_twolevel_for_type_p = twolevel
            ctrl._set_twolevel_active()
            self.feed(0.85)
            with patch.object(ctrl, 'polarity', side_effect=AssertionError('status queried controller')):
                before = ctrl._twolevel_hys_state
                for _ in range(3):
                    self.assertEqual(self.hh.mmu.get_status(0)['sync_feedback_state'], 'neutral')
                self.assertEqual(ctrl._twolevel_hys_state, before)

    def test_reporting_preserves_continuous_controller_input(self):
        self.sf.active = True
        self.sf.p.sync_feedback_enabled = True
        self.assertFalse(self.sf.ctrl.twolevel_active)
        self.feed(0.084)
        self.assertEqual(self.state(), 'neutral')
        with patch.object(self.sf.ctrl, 'update', wraps=self.sf.ctrl.update) as update, \
                patch.object(self.sf, '_process_status'):
            self.sf._handle_extruder_movement(self.hh.reactor.monotonic(), 1.0)
            self.hh.reactor.advance(0.0)
            update.assert_called_once()
            self.assertAlmostEqual(update.call_args.args[2], 0.084)
        self.assertAlmostEqual(self.sf._get_sensor_state(), 0.084)

    def test_inactive_disabled_and_detail_states(self):
        self.feed(0.084)
        self.sf.active = False
        self.sf.p.sync_feedback_enabled = True
        self.assertEqual(self.sf.get_sync_feedback_string(), 'inactive')
        self.assertEqual(self.state(), 'neutral')
        self.sf.p.sync_feedback_enabled = False
        self.assertEqual(self.sf.get_sync_feedback_string(), 'disabled')
        self.assertEqual(self.state(), 'neutral')

    def test_explicit_event_state_is_preserved(self):
        self.feed(0.0)
        for state, expected in ((-1, 'tension'), (0, 'neutral'), (1, 'compressed')):
            self.assertEqual(self.sf.get_sync_feedback_string(state, detail=True), expected)


class TestSwitchSyncFeedbackString(unittest.TestCase):
    def test_switch_combinations_are_unchanged(self):
        hh = session('kms')
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])
        sf = hh.mmu.mmu_unit(0).sync_feedback
        for tension, compression, expected in (
            (False, False, 'neutral'), (True, False, 'tension'),
            (False, True, 'compressed'), (True, True, 'neutral'),
        ):
            with self.subTest(tension=tension, compression=compression):
                hh.sensor('filament_tension').set(tension)
                hh.sensor('filament_compression').set(compression)
                self.assertEqual(sf.get_sync_feedback_string(detail=True), expected)


if __name__ == '__main__':
    unittest.main()
