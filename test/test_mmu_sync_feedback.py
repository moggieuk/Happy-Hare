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


class TestBufferStatus(unittest.TestCase):
    def test_status_reports_only_configured_buffers_on_either_selected_unit(self):
        hh = session('ercf_vvd')
        self.addCleanup(hh.close)
        hh.boot(calibrate=True, selected_gate=0, selector_last_pos=True)
        for gate in (0, 9):
            hh.run_gcode('MMU_SELECT GATE=%d' % gate)
            for compressed, expected in ((True, 'compressed'), (False, 'neutral')):
                hh.sensor('unit1:filament_tension').set(False)
                hh.sensor('unit1:filament_compression').set(compressed)
                for command in ('MMU_STATUS', 'MMU_STATUS SHOWCONFIG=1'):
                    with self.subTest(gate=gate, state=expected, command=command):
                        start = len(hh.console)
                        hh.run_gcode(command)
                        shown = '\n'.join(hh.console[start:])
                        ercf, vvd = shown.split('● ')[1:3]
                        self.assertNotIn('Buffer reads', ercf)
                        self.assertIn('└ Buffer reads %s\n' % expected, vvd)
        self.assertEqual(hh.errors, [])

    def test_status_reads_each_buffers_own_sensors(self):
        hh = session('ercf_vvd_buffers')
        self.addCleanup(hh.close)
        hh.boot(calibrate=True, selected_gate=9, selector_last_pos=True)
        prop = hh.sensor('unit0:filament_proportional')
        sensor = prop.sensor
        raw = sensor._neutral_point + (-0.95 * sensor._d_neg) * (-1 if sensor._reversed else 1)
        prop.feed(raw)
        hh.sensor('unit1:filament_tension').set(False)
        hh.sensor('unit1:filament_compression').set(True)
        start = len(hh.console)
        hh.run_gcode('MMU_STATUS SHOWCONFIG=1')
        ercf, vvd = '\n'.join(hh.console[start:]).split('● ')[1:3]
        self.assertIn('└ Buffer reads tension\n', ercf)
        self.assertIn('└ Buffer reads compressed\n', vvd)
        self.assertEqual(hh.errors, [])


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
        """Regression: the existing behavior must be untouched by the new target."""
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


class TestAdjustTensionRestoresSync(unittest.TestCase):
    """
    #1445: the Blobifier calls ADJUST_TENSION between blobs. Out of print the gear must be
    re-synced afterwards, or the next blob is extruded against a gripped, stationary gear.
    """

    def test_nested_adjust_tension_resyncs_a_synced_gear(self):
        # Type-A ERCF: not always gripped, so nothing else forces sync back on
        hh = session('ercf_vvd_buffers')
        self.addCleanup(hh.close)
        hh.boot(calibrate=True)
        hh.run_gcode('MMU_SELECT GATE=0')
        self.assertEqual(hh.errors, [])

        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        mmu = hh.mmu
        mmu.filament_pos = FILAMENT_POS_LOADED
        self.assertFalse(mmu.mmu_unit().filament_always_gripped)

        # As set by the purge step with sync_purge: 1
        mmu.reset_sync_gear_to_extruder(True, force_grip=True)
        self.assertTrue(mmu.drive().is_synced_to_extruder())

        sf = mmu.mmu_unit(0).sync_feedback
        readings = [-0.5, -0.05]
        with patch.object(sf, '_get_sensor_state',
                          side_effect=lambda *a, **k: readings.pop(0) if len(readings) > 1 else readings[0]), \
                patch.object(mmu, 'move_filament', side_effect=lambda reason, dist, *a, **k: dist) as move:
            # Nested, as when the purge macro runs inside MMU_CHANGE_TOOL
            with mmu.wrap_sync_gear_to_extruder():
                hh.run_gcode('MMU_SYNC_FEEDBACK ADJUST_TENSION=1 UNIT=0')
                self.assertTrue(move.called)
                self.assertTrue(mmu.drive().is_synced_to_extruder(),
                                'gear left gripped but unsynced after ADJUST_TENSION')
        self.assertEqual(hh.errors, [])


class TestPrintEndReleaseGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test.hh import install
        install()

    def boot(self, profile='emu'):
        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED
        hh.mmu.gate_selected = 0
        hh.mmu.filament_pos = FILAMENT_POS_LOADED
        hh.mmu.psm.set_print_state('printing')
        hh.printer.lookup_object('gcode_macro _MMU_SOFTWARE_VARS').variables.update(
            unload_tool=False, reset_ttg=False, dump_stats=False)
        hh.printer.harness_macro_effects['MMU_END'] = lambda macro, gcmd: macro.run_body(gcmd)
        hh.gcode.executed.clear()
        return hh

    def run_end(self, hh, expected_call, expected_adjust=False):
        with patch.object(hh.mmu.mmu_unit(0).sync_feedback, 'adjust_filament_tension',
                          return_value=(0., True)) as adjust, \
                patch.object(hh.mmu, 'log_warning') as warning:
            hh.run_gcode('MMU_END')
        self.assertEqual('MMU_SYNC_FEEDBACK RELEASE=1' in hh.gcode.executed, expected_call)
        self.assertEqual(adjust.called, expected_adjust)
        warning.assert_not_called()
        self.assertEqual(hh.errors, [])
        self.assertIn('MMU_PRINT_END STATE=complete', hh.gcode.executed)
        self.assertEqual(hh.mmu.psm.print_state, 'complete')

    def test_loaded_buffer_releases_even_with_feedback_control_disabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                hh = self.boot()
                hh.mmu.mmu_unit(0).sync_feedback.p.sync_feedback_enabled = enabled
                self.run_end(hh, True, True)
                hh.close()

    def test_macro_skips_unit_without_buffer(self):
        hh = self.boot('ercf_vvd')
        self.assertFalse(hh.mmu.mmu_unit().has_buffer())
        self.run_end(hh, False)

    def test_macro_skips_invalid_gate_or_unloaded_filament(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED
        for gate, position in ((-1, FILAMENT_POS_LOADED), (-2, FILAMENT_POS_LOADED),
                               (0, FILAMENT_POS_UNLOADED)):
            with self.subTest(gate=gate, position=position):
                hh = self.boot()
                hh.mmu.gate_selected = gate
                hh.mmu.filament_pos = position
                self.run_end(hh, False)
                hh.close()

    def test_extension_can_invalidate_release_after_macro_rendering(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_UNLOADED
        for attribute, value in (('gate_selected', -1), ('filament_pos', FILAMENT_POS_UNLOADED)):
            with self.subTest(attribute=attribute):
                hh = self.boot('ercf_vvd_buffers')
                hh.printer.lookup_object('gcode_macro _MMU_SOFTWARE_VARS').variables[
                    'user_print_end_extension'] = 'TEST_CHANGE_STATE'
                hh.gcode.register_command('TEST_CHANGE_STATE',
                    lambda gcmd: setattr(hh.mmu, attribute, value))
                self.run_end(hh, True)
                hh.close()

    def test_release_only_quietly_skips_ineligible_states(self):
        from extras.mmu.mmu_constants import FILAMENT_POS_UNLOADED
        for case in ('unknown_gate', 'bypass', 'unloaded', 'disabled', 'no_buffer',
                     'disabled_sensors', 'no_rest_state', 'switch_buffer'):
            with self.subTest(case=case):
                profile = 'ercf_vvd' if case in ('unknown_gate', 'no_buffer') else 'emu'
                if case == 'switch_buffer': profile = 'boxturtle'
                hh = self.boot(profile)
                if case == 'unknown_gate': hh.mmu.gate_selected = -1
                if case == 'bypass': hh.mmu.gate_selected = -2
                if case == 'unloaded': hh.mmu.filament_pos = FILAMENT_POS_UNLOADED
                if case == 'disabled': hh.mmu.is_enabled = False
                if case == 'disabled_sensors':
                    for name in ('filament_proportional', 'filament_tension', 'filament_compression'):
                        sensor = hh.mmu.sensor_manager.get_sensor_obj(name)
                        hh.run_gcode('MMU_SENSORS SENSOR=%s ENABLE=0' % sensor.runout_helper.name)
                if case == 'no_rest_state':
                    hh.mmu.mmu_unit(0).buffer.buffer_spring_state_num = None
                with patch.object(hh.mmu, 'move_filament') as move, \
                        patch.object(hh.mmu, 'log_warning') as warning, \
                        patch.object(hh.mmu, 'log_always') as status:
                    hh.run_gcode('MMU_SYNC_FEEDBACK RELEASE=1')
                move.assert_not_called()
                warning.assert_not_called()
                status.assert_not_called()
                self.assertEqual(hh.errors, [])
                hh.close()

    def test_combined_command_preserves_other_actions(self):
        hh = self.boot('boxturtle')
        with patch.object(hh.mmu, 'move_filament') as move:
            hh.run_gcode('MMU_SYNC_FEEDBACK RELEASE=1 ENABLE=0')
        self.assertFalse(hh.mmu.mmu_unit(0).sync_feedback.p.sync_feedback_enabled)
        move.assert_not_called()
        self.assertEqual(hh.errors, [])


class TestTwolevelSpeedMultipliers(unittest.TestCase):
    """sync_feedback_speed/boost_multiplier must reach the controller (were stuck at 5%)."""

    def setUp(self):
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.sf = self.hh.mmu.mmu_unit(0).sync_feedback

    def force_twolevel(self):
        ctrl = self.sf.ctrl
        ctrl.cfg.use_twolevel_for_type_p = True
        ctrl._set_twolevel_active()
        return ctrl

    def test_configured_multipliers_build_the_controller_config(self):
        self.sf.p.sync_feedback_speed_multiplier = 8.0
        self.sf.p.sync_feedback_boost_multiplier = 3.0
        self.sf._init_controller()
        self.assertAlmostEqual(self.sf.ctrl.cfg.rd_twolevel_speed_multiplier, 0.08)
        self.assertAlmostEqual(self.sf.ctrl.cfg.rd_twolevel_boost_multiplier, 0.03)

    def test_live_speed_change_retunes_the_low_high_rd(self):
        ctrl = self.force_twolevel()
        ctrl._twolevel_boost_active = False
        self.hh.run_gcode('MMU_TEST_CONFIG sync_feedback_speed_multiplier=8')
        self.assertEqual(self.hh.errors, [])
        self.assertAlmostEqual(ctrl.cfg.rd_twolevel_speed_multiplier, 0.08)
        self.assertAlmostEqual(ctrl.rd_low, ctrl.rd_ref / 1.08)
        self.assertAlmostEqual(ctrl.rd_high, ctrl.rd_ref / 0.92)

    def test_live_boost_change_applies_while_boost_is_active(self):
        ctrl = self.force_twolevel()
        ctrl._twolevel_boost_active = True
        self.hh.run_gcode('MMU_TEST_CONFIG sync_feedback_speed_multiplier=5 sync_feedback_boost_multiplier=2')
        self.assertEqual(self.hh.errors, [])
        self.assertAlmostEqual(ctrl.cfg.rd_twolevel_boost_multiplier, 0.02)
        self.assertAlmostEqual(ctrl.rd_low, ctrl.rd_ref / 1.07)
        self.assertAlmostEqual(ctrl.rd_high, ctrl.rd_ref / 0.93)


class TestTwolevelTuningEvidence(unittest.TestCase):
    def test_live_speed_change_discards_old_duty_measurements(self):
        # Duty cycles measured at the old speeds cannot calibrate the new band.
        with session('emu') as hh:
            hh.boot()
            sf = hh.mmu.mmu_unit(0).sync_feedback
            ctrl = sf.ctrl
            ctrl.cfg.use_twolevel_for_type_p = True
            ctrl._set_twolevel_active()
            ctrl._twolevel_boost_active = False
            ctrl._set_low_high_rd(ctrl.rd_ref)
            auto = ctrl.autotune
            auto.note_twolevel_tick('low', True, 0)
            for _ in range(6):
                auto.note_twolevel_tick('low', False, 30)
                auto.note_twolevel_tick('high', True, 0)
                auto.note_twolevel_tick('high', False, 10)
                auto.note_twolevel_tick('low', True, 0)
            before, _ = auto._recommend_rd_from_twolevel()
            self.assertIsNotNone(before)
            auto._rd_cert_fifo.append(before)
            auto.pause()
            preserved = (ctrl.rd_ref, ctrl.rd_current, auto._autotune_current,
                         auto._autotune_baseline, auto._total_motion_mm,
                         auto._total_time_s, auto._autotune_last_motion_mm,
                         auto._autotune_last_time_s)
            hh.run_gcode('MMU_TEST_CONFIG sync_feedback_speed_multiplier=20')
            self.assertTrue(auto._paused)
            self.assertEqual(preserved, (ctrl.rd_ref, ctrl.rd_current,
                auto._autotune_current, auto._autotune_baseline,
                auto._total_motion_mm, auto._total_time_s,
                auto._autotune_last_motion_mm, auto._autotune_last_time_s))
            self.assertEqual(list(auto._rd_cert_fifo), [])
            self.assertEqual(hh.errors, [])
            after, note = auto._recommend_rd_from_twolevel()
            self.assertIsNone(after, 'Old measurements must not yield a new-speed calibration: %s' % note)

    def test_inactive_boost_change_preserves_evidence(self):
        with session('emu') as hh:
            hh.boot()
            ctrl = hh.mmu.mmu_unit(0).sync_feedback.ctrl
            ctrl.cfg.use_twolevel_for_type_p = True
            ctrl._set_twolevel_active()
            ctrl._twolevel_boost_active = False
            ctrl._set_low_high_rd(ctrl.rd_ref)
            ctrl.autotune._tl_cycles = [(30., 10.)]
            hh.run_gcode('MMU_TEST_CONFIG sync_feedback_boost_multiplier=9')
            self.assertEqual(hh.errors, [])
            self.assertEqual(ctrl.autotune._tl_cycles, [(30., 10.)])

    def test_active_boost_change_discards_evidence(self):
        with session('emu') as hh:
            hh.boot()
            ctrl = hh.mmu.mmu_unit(0).sync_feedback.ctrl
            ctrl.cfg.use_twolevel_for_type_p = True
            ctrl._set_twolevel_active()
            ctrl._set_low_high_rd(ctrl.rd_ref)
            ctrl.autotune._tl_cycles = [(30., 10.)]
            ctrl.autotune._tl_seg_mm = 12.
            ctrl.autotune._tl_last_unpaired_low = 30.
            hh.run_gcode('MMU_TEST_CONFIG sync_feedback_boost_multiplier=9')
            self.assertEqual(hh.errors, [])
            self.assertEqual(ctrl.autotune._tl_cycles, [])
            self.assertEqual(ctrl.autotune._tl_seg_mm, 0.)
            self.assertIsNone(ctrl.autotune._tl_last_unpaired_low)


class TestAutotunedRd(unittest.TestCase):
    """Tuned rd is per gate, only confident suggestions persist, and correction % is reported."""

    def setUp(self):
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [])
        self.unit = self.hh.mmu.mmu_unit(0)
        self.sf = self.unit.sync_feedback
        self.now = self.hh.reactor.monotonic()

    def process(self, autotune=None, rd_tuned=None):
        status = self.sf.ctrl.update(self.now, 0.0, 0.0)
        if autotune is not None:
            status['output']['autotune'] = autotune
        if rd_tuned is not None:
            status['output']['rd_tuned'] = rd_tuned
        self.sf._process_status(self.now, status)

    def test_hard_reset_rebases_tuned_rd_for_new_gate(self):
        self.sf.ctrl.reset(self.now, 21.70, 0.0, hard_reset=True)
        self.sf.ctrl.reset(self.now, 22.50, 0.0, hard_reset=True)
        self.assertEqual(self.sf.ctrl.autotune.get_tuned_rd(), 22.50)

    def test_only_confident_suggestion_is_kept_for_saving(self):
        self.sf.new_autotuned_rd = None
        self.process(autotune={'rd': 22.61, 'note': '', 'save': False})
        self.assertIsNone(self.sf.new_autotuned_rd)
        self.process(autotune={'rd': 22.62, 'note': '', 'save': True})
        self.assertEqual(self.sf.new_autotuned_rd, 22.62)

    def test_status_reports_correction_from_starting_rd(self):
        self.sf._reset_controller(self.now)
        rd = self.unit.calibrator.get_gear_rd()
        self.process(rd_tuned=rd * 1.02)
        status = self.hh.mmu.get_status(self.now)
        self.assertEqual(status['sync_feedback_rd_correction'], 2.0)
        self.sf._handle_gate_selected(1, 0)
        self.assertEqual(self.hh.mmu.get_status(self.now)['sync_feedback_rd_correction'], 0.)

    def test_print_end_summarises_autotuned_rd(self):
        cal = self.unit.calibrator
        rd = cal.get_gear_rd(0)
        self.unit.p.autotune_rotation_distance = 1
        self.hh.run_gcode('MMU_PRINT_START')
        cal.note_rd_telemetry(0, rd * 1.01)
        self.assertTrue(any('Revised: %.4fmm (+1.00%%) -> Updated' % (rd * 1.01) in line for line in self.hh.console))
        cal.note_rd_telemetry(0, rd * 1.02)
        self.hh.run_gcode('MMU_PRINT_END')
        self.assertEqual(self.hh.errors, [])
        summary = [line for line in self.hh.console if 'Autotune summary' in line]
        self.assertEqual(len(summary), 1)
        self.assertIn('Gate 0: rotation_distance %.4f -> %.4f (+2.00%%)' % (rd, round(rd * 1.02, 4)), summary[0])

    def test_no_print_end_summary_without_autotune(self):
        self.unit.p.autotune_rotation_distance = 0
        self.hh.run_gcode('MMU_PRINT_START')
        self.unit.calibrator.note_rd_telemetry(0, self.unit.calibrator.get_gear_rd(0) * 1.02)
        self.hh.run_gcode('MMU_PRINT_END')
        self.assertFalse(any('Autotune summary' in line for line in self.hh.console))


    def start_loaded_print(self):
        self.hh.heat_extruder(220)
        self.hh.place_filament(0, position=-40.)
        self.hh.run_gcode('MMU_LOAD')
        self.hh.run_gcode('MMU_PRINT_START')
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.sf.active)

    def test_stable_twolevel_recommendation_is_saved_when_confident(self):
        self.start_loaded_print()
        self.unit.p.autotune_rotation_distance = 1
        rd = self.unit.calibrator.get_gear_rd(0)
        target = rd * 1.02
        self.sf.ctrl.cfg.use_twolevel_for_type_p = True
        self.sf._reset_controller(self.now)
        suggestions = []
        with patch.object(self.sf.ctrl.autotune, '_recommend_rd_from_twolevel',
                          return_value=(target, 'stable estimate')):
            for i in range(1, 17):
                status = self.sf.ctrl.update(self.now + i * 20, 110., 0.)
                self.sf._process_status(self.now + i * 20, status)
                suggestions.append(status['output']['autotune'])
        self.assertFalse(next(s for s in suggestions if s['rd'] is not None)['save'])
        self.assertGreaterEqual(self.sf.ctrl.autotune._rd_cert_last_score, .5)
        self.sf._handle_mmu_unsynced()
        self.assertAlmostEqual(self.unit.calibrator.get_gear_rd(0), target, places=4)
        self.assertEqual(sum(s['save'] for s in suggestions), 1)
        self.assertEqual(self.sf.rd_correction, 2.)

    def test_print_end_saves_final_gate_without_unsyncing(self):
        self.start_loaded_print()
        self.unit.p.autotune_rotation_distance = 1
        cal = self.unit.calibrator
        rd = cal.get_gear_rd(0)
        target = rd * 1.02
        self.assertTrue(self.unit.filament_always_gripped)
        self.process(autotune={'rd': target, 'note': '', 'save': True})
        with patch.object(cal, 'note_rd_telemetry', wraps=cal.note_rd_telemetry) as save:
            self.hh.run_gcode('MMU_PRINT_END')
            self.assertEqual(self.hh.errors, [])
            summary = [line for line in self.hh.console if 'Autotune summary' in line]
            self.assertEqual(len(summary), 1)
            self.assertIn('Gate 0: rotation_distance %.4f -> %.4f (+2.00%%)' % (rd, round(target, 4)), summary[0])
            self.assertTrue(self.sf.active)
            self.assertAlmostEqual(cal.get_gear_rd(0), target, places=4)
            self.hh.run_gcode('MMU_PRINT_START')
            self.hh.run_gcode('MMU_PRINT_END')
            self.sf._handle_mmu_unsynced()
            save.assert_called_once_with(0, target)
        self.assertEqual(sum('Autotune summary' in line for line in self.hh.console), 1)

    def test_next_print_rebases_correction_on_saved_rd(self):
        self.start_loaded_print()
        self.unit.p.autotune_rotation_distance = 1
        target = self.unit.calibrator.get_gear_rd(0) * 1.02
        self.sf.ctrl.autotune._autotune_baseline = target
        self.process(autotune={'rd': target, 'note': '', 'save': True})
        self.assertEqual(self.sf.rd_correction, 2.)
        self.hh.run_gcode('MMU_PRINT_END')
        self.hh.run_gcode('MMU_PRINT_START')
        self.assertTrue(self.sf.active)
        self.assertEqual(self.sf.rd_correction, 0.)

    def test_print_end_keeps_autotune_disabled_on_loaded_gate(self):
        self.start_loaded_print()
        self.unit.p.autotune_rotation_distance = 0
        rd = self.unit.calibrator.get_gear_rd(0)
        self.process(autotune={'rd': rd * 1.02, 'note': '', 'save': True})
        self.hh.run_gcode('MMU_PRINT_END')
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.sf.active)
        self.assertEqual(self.unit.calibrator.get_gear_rd(0), rd)
        self.assertFalse(any('Autotune summary' in line for line in self.hh.console))


if __name__ == '__main__':
    unittest.main()
