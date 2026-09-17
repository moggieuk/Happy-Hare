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


class TestBufferSpringRelease(unittest.TestCase):
    """
    MMU_SYNC_FEEDBACK RELEASE=1 parks the buffer where its spring rests, so a sprung
    buffer is not left holding its spring loaded between prints.

    These tests script the sensor readings rather than letting filament travel drive
    them. That is not laziness: the harness only models buffer travel dynamically for
    two-switch tension-sprung buffers (test/hh/filament.py:167-171), so a PROPORTIONAL
    buffer's analog reading does not respond to gear moves here at all. What is being
    changed is which target the routine aims at and which way it therefore moves, and
    that is exactly what these assert on.
    """

    NEUTRAL_START = 0.0

    # EMU already ships buffer_spring_state: tension, so every case states the spring
    # it wants rather than leaning on the profile default. Drive the CHOICE symbols
    # and not PARAM_BUFFER_SPRING_STATE: the PARAM has no prompt, so setting it
    # directly is ignored by Kconfig (it warns) and only works by accident.
    SPRING_CHOICE = {
        'tension':     'CHOICE_BUFFER_SPRING_STATE_TENSION',
        'neutral':     'CHOICE_BUFFER_SPRING_STATE_NEUTRAL',
        'compression': 'CHOICE_BUFFER_SPRING_STATE_COMPRESSION',
        'none':        'CHOICE_BUFFER_SPRING_STATE_NONE',
    }

    def _session(self, spring=None, profile_name='emu'):
        from test.hh import profiles as profiles_mod
        profile = profiles_mod.get(profile_name)
        if spring is not None:
            syms = dict.fromkeys(self.SPRING_CHOICE.values(), False)
            syms[self.SPRING_CHOICE[spring]] = True
            profile = profile.derive('%s_spring_%s' % (profile_name, spring), syms=syms)
        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])

        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        hh.mmu.filament_pos = FILAMENT_POS_LOADED
        return hh

    def _run(self, hh, gcode, readings):
        """
        Run gcode with a scripted sequence of sensor readings.
        Returns (gear moves commanded, info/debug lines logged).
        """
        moves, logged = [], []
        sf = hh.mmu.mmu_unit(0).sync_feedback
        seq = list(readings)

        def fake_state(*args, **kwargs):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        def fake_move(reason, dist, *args, **kwargs):
            moves.append(dist)
            return dist

        with patch.object(sf, '_get_sensor_state', side_effect=fake_state), \
                patch.object(hh.mmu, 'move_filament', side_effect=fake_move), \
                patch.object(hh.mmu, 'log_info', side_effect=logged.append), \
                patch.object(hh.mmu, 'log_debug', side_effect=logged.append), \
                patch.object(hh.mmu, 'log_warning', side_effect=logged.append):
            hh.run_gcode(gcode)
        return moves, logged

    def test_spring_state_is_the_release_target_and_sets_the_direction(self):
        """
        A tension-sprung buffer rests at -1, so releasing it RETRACTS. This is the
        whole feature: before the change there was no way to ask for anything but 0.
        """
        hh = self._session(spring='tension')
        self.assertEqual(hh.mmu.mmu_unit(0).buffer.buffer_spring_state_num, -1)

        moves, logged = self._run(
            hh, 'MMU_SYNC_FEEDBACK RELEASE=1', [self.NEUTRAL_START, -0.99])

        self.assertTrue(moves, 'RELEASE=1 commanded no movement at all')
        self.assertLess(moves[0], 0.0,
                        'releasing a tension-sprung buffer must retract, got %r' % moves)
        self.assertTrue(any('Released buffer spring' in m for m in logged),
                        'expected a release confirmation, got %r' % logged)

    def test_direction_is_taken_from_config_not_hardcoded(self):
        """A compression-sprung buffer rests at +1, so it releases the other way."""
        hh = self._session(spring='compression')
        self.assertEqual(hh.mmu.mmu_unit(0).buffer.buffer_spring_state_num, 1)

        moves, _ = self._run(
            hh, 'MMU_SYNC_FEEDBACK RELEASE=1', [self.NEUTRAL_START, 0.99])

        self.assertTrue(moves, 'RELEASE=1 commanded no movement at all')
        self.assertGreater(moves[0], 0.0,
                           'releasing a compression-sprung buffer must feed, got %r' % moves)

    def test_release_is_a_noop_when_no_resting_position_is_configured(self):
        """
        'none' is the shipped default and means nobody has said where this buffer
        rests. Aiming at a guess would be worse than doing nothing.
        """
        hh = self._session(spring='none')
        self.assertIsNone(hh.mmu.mmu_unit(0).buffer.buffer_spring_state_num)

        moves, logged = self._run(
            hh, 'MMU_SYNC_FEEDBACK RELEASE=1', [self.NEUTRAL_START])

        self.assertEqual(moves, [], 'RELEASE=1 moved filament with no spring state set')
        self.assertTrue(any('Nothing to release' in m for m in logged),
                        'expected a skip explanation, got %r' % logged)

    def test_adjust_tension_still_targets_neutral(self):
        """Regression: the existing behaviour must be untouched by the new target."""
        hh = self._session(spring='tension')

        moves, logged = self._run(
            hh, 'MMU_SYNC_FEEDBACK ADJUST_TENSION=1', [-0.5, -0.05])

        self.assertTrue(moves, 'ADJUST_TENSION=1 commanded no movement')
        self.assertGreater(moves[0], 0.0,
                           'correcting tension toward neutral must feed, got %r' % moves)
        self.assertTrue(any('Neutralized tension' in m for m in logged),
                        'expected the original neutralize message, got %r' % logged)
        self.assertFalse(any('Released buffer spring' in m for m in logged),
                         'ADJUST_TENSION must not report a spring release')

    def test_rail_target_accepts_a_saturated_reading_as_arrival(self):
        """
        A sensor pinned at its rail never reports exactly -1.0, so a symmetric band
        around the target would never be satisfied and the routine would keep nudging
        into the buffer's own hard stop. Reaching the rail has to count as arrival.
        """
        hh = self._session(spring='tension')
        sf = hh.mmu.mmu_unit(0).sync_feedback

        moves = []
        with patch.object(sf, '_get_sensor_state', side_effect=lambda *a, **k: -0.98), \
                patch.object(hh.mmu, 'move_filament',
                             side_effect=lambda reason, dist, *a, **k: moves.append(dist)):
            actual, success = sf.adjust_filament_tension(target=-1.0)

        self.assertTrue(success, 'a buffer sitting on its rail should report success')
        self.assertEqual(moves, [], 'already at the rail: nothing should move')


class TestSwitchBufferSpringRelease(unittest.TestCase):
    """A switch buffer can only home to neutral, so it must decline rather than guess."""

    def test_switch_buffer_declines_release(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])

        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        hh.mmu.filament_pos = FILAMENT_POS_LOADED

        unit = hh.mmu.mmu_unit(0)
        self.assertFalse(hh.mmu.sensor_manager.has_sensor('filament_proportional'))

        moves, logged = [], []
        with patch.object(hh.mmu, 'move_filament',
                          side_effect=lambda reason, dist, *a, **k: moves.append(dist)), \
                patch.object(hh.mmu, 'log_debug', side_effect=logged.append), \
                patch.object(hh.mmu, 'log_info', side_effect=logged.append), \
                patch.object(hh.mmu, 'log_warning', side_effect=logged.append):
            hh.run_gcode('MMU_SYNC_FEEDBACK RELEASE=1')

        self.assertEqual(moves, [], 'a switch buffer must not be driven to a rail')
        self.assertTrue(any('Nothing to release' in m for m in logged),
                        'expected an explicit decline, got %r' % logged)
