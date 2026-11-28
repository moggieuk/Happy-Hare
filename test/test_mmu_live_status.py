# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify printer status fields used by live subscription clients.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import unittest
from unittest import mock
from types import SimpleNamespace
from test.hh import session


class TestLiveStatus(unittest.TestCase):
    def setUp(self):
        self.hh = session('emu')
        self.hh.boot(calibrate=True, selected_gate=0)

    def tearDown(self):
        self.hh.close()

    def test_machine_status_is_the_static_dict(self):
        machine = self.hh.mmu.mmu_machine
        self.assertIs(machine.get_status(0), machine.machine_status)

    def test_console_only_fields_are_not_in_mmu_status(self):
        status = self.hh.mmu.get_status(0)
        for key in ('filament_grip_state', 'sensor_states', 'active_sensors'):
            self.assertNotIn(key, status)
        self.assertIsNone(status['homing'])

    def test_filament_position_is_rounded_to_two_decimals(self):
        mmu = self.hh.mmu
        with mock.patch.object(mmu.drive(), 'get_filament_position', return_value=712.0900000000003):
            self.assertEqual(mmu.get_status(0)['filament_position'], 712.09)

    def test_filament_position_is_the_target_of_the_move_under_way(self):
        mmu = self.hh.mmu
        self.hh.place_filament(0)
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=1')
        seen = []
        def observe():
            status = mmu.get_status(self.hh.reactor.monotonic())
            seen.append((status['filament_position'], status['filament_position_live']))
        self.hh.printer.harness_pace_observer = observe
        self.addCleanup(setattr, self.hh.printer, 'harness_pace_observer', None)
        self.hh.set_pacing(1.)
        self.addCleanup(self.hh.set_pacing, 0.)
        self.hh.run_gcode('MMU_LOAD')
        furthest = max(live for _, live in seen)
        feeding = seen[:[live for _, live in seen].index(furthest) + 1]
        self.assertGreater(len(feeding), 50)
        self.assertFalse([s for s in feeding if s[0] < s[1] - 0.1][:5])

    def test_the_live_position_moves_through_a_homing_move(self):
        mmu = self.hh.mmu
        self.hh.place_filament(0)
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=1')
        homing = set()
        def observe():
            status = mmu.get_status(self.hh.reactor.monotonic())
            if status['homing']:
                homing.add(status['filament_position_live'])
        self.hh.printer.harness_pace_observer = observe
        self.addCleanup(setattr, self.hh.printer, 'harness_pace_observer', None)
        self.hh.set_pacing(1.)
        self.addCleanup(self.hh.set_pacing, 0.)
        self.hh.run_gcode('MMU_LOAD')
        self.assertGreater(len(homing), 10)

    def test_the_drive_sync_mode_is_reported_as_well_as_the_gear_following_the_extruder(self):
        from extras.mmu.mmu_constants import DRIVE_EXTRUDER_SYNCED_TO_GEAR, DRIVE_GEAR_SYNCED_TO_EXTRUDER
        mmu = self.hh.mmu
        mmu.drive().sync_mode(DRIVE_EXTRUDER_SYNCED_TO_GEAR)
        status = mmu.get_status(0)
        self.assertEqual((status['sync_mode'], status['sync_drive']), (DRIVE_EXTRUDER_SYNCED_TO_GEAR, False))
        mmu.drive().sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        status = mmu.get_status(0)
        self.assertEqual((status['sync_mode'], status['sync_drive']), (DRIVE_GEAR_SYNCED_TO_EXTRUDER, True))

    def test_runtime_edits_are_reported_against_their_config_section(self):
        mmu = self.hh.mmu
        self.assertNotIn('mmu_toolhead default', mmu.get_status(0)['config_changes'])
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=3')
        self.hh.run_gcode('MMU_TEST_CONFIG LOG_LEVEL=2')
        changes = mmu.get_status(0)['config_changes']
        self.assertEqual(changes['mmu_toolhead default'], {'toolhead_residual_filament': 3})
        self.assertEqual(changes['mmu_parameters']['log_level'], 2)

    def test_each_edit_reports_a_new_snapshot_so_status_subscribers_see_it(self):
        mmu = self.hh.mmu
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=3')
        earlier = mmu.get_status(0)['config_changes']
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=4')
        self.assertEqual(earlier['mmu_toolhead default'], {'toolhead_residual_filament': 3})
        self.assertIsNot(mmu.get_status(0)['config_changes'], earlier)
        self.assertEqual(mmu.get_status(0)['config_changes']['mmu_toolhead default'], {'toolhead_residual_filament': 4})

    def test_unchanged_value_is_not_reported(self):
        mmu = self.hh.mmu
        before = mmu.get_status(0)['config_changes'].get('mmu_parameters', {}).get('log_level')
        self.hh.run_gcode('MMU_TEST_CONFIG LOG_LEVEL=%d' % mmu.p.log_level)
        self.assertEqual(mmu.get_status(0)['config_changes'].get('mmu_parameters', {}).get('log_level'), before)

    def test_change_back_to_the_configfile_value_removes_the_entry(self):
        mmu = self.hh.mmu
        settings = self.hh.printer.lookup_object('configfile').get_status(0)['settings']
        original = settings['mmu_toolhead default']['toolhead_residual_filament']
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=%s' % (original + 1))
        self.assertIn('mmu_toolhead default', mmu.get_status(0)['config_changes'])
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=%s' % original)
        self.assertNotIn('mmu_toolhead default', mmu.get_status(0)['config_changes'])

    def test_startup_fixups_are_seeded_so_configfile_plus_changes_is_effective(self):
        mmu = self.hh.mmu
        settings = self.hh.printer.lookup_object('configfile').get_status(0)['settings']
        changes = mmu.get_status(0)['config_changes']
        plain = lambda v: list(v) if isinstance(v, (list, tuple)) else v
        sets = [mmu.p] + [p for u in mmu.mmu_machine.units for p in (u.p, u.selector.p, u.toolhead_wrapper.p)]
        skipped = lambda name: name.startswith('default_gate_') or name in ('default_ttg_map', 'default_endless_spool_groups')
        for params in sets:
            scope = params.scope()
            for spec in params._SPECS:
                if skipped(spec.name) or not hasattr(params, spec.name) or spec.name not in settings.get(scope, {}):
                    continue
                effective = changes.get(scope, {}).get(spec.name, settings[scope][spec.name])
                self.assertEqual(plain(effective), plain(getattr(params, spec.name)), (scope, spec.name))
        self.assertEqual(changes['mmu_parameters']['drying_data'], mmu.p.drying_data)
        self.assertFalse([n for n in changes['mmu_parameters'] if skipped(n)])

    def test_the_time_the_paused_extruder_heater_goes_off_is_reported_until_unlock(self):
        mmu = self.hh.mmu
        self.assertIsNone(mmu.get_status(0)['hotend_off_time'])
        before = self.hh.reactor.monotonic()
        self.hh.run_gcode('MMU_PAUSE FORCE_IN_PRINT=1')
        self.assertAlmostEqual(mmu.get_status(0)['hotend_off_time'], before + mmu.p.disable_heater, delta=1.)
        self.hh.run_gcode('MMU_UNLOCK')
        self.assertIsNone(mmu.get_status(0)['hotend_off_time'])

    def test_the_time_klippers_idle_timeout_expires_is_reported_while_it_is_counting(self):
        mmu = self.hh.mmu
        idle = self.hh.printer.lookup_object('idle_timeout')
        self.assertIsNone(mmu._idle_timeout_time(1000.))  # Idle: nothing is counting
        idle.set_state('Ready')
        idle.timeout_timer.waketime = 1001.  # Earlier check must still wait for the idle interval
        with mock.patch.object(mmu.toolhead, 'check_busy', return_value=(100., 130.4, True), create=True):
            self.assertEqual(mmu._idle_timeout_time(1000.), round(1000. + idle.idle_timeout - 30.4))
        with mock.patch.object(mmu.toolhead, 'check_busy', return_value=(100., 100., False), create=True):
            self.assertIsNone(mmu._idle_timeout_time(1000.))  # Moves are queued

    def test_the_idle_deadline_is_published_only_while_the_mmu_is_paused(self):
        mmu = self.hh.mmu
        self.hh.printer.lookup_object('idle_timeout').set_state('Ready')
        now = self.hh.reactor.monotonic()
        self.assertIsNone(mmu.get_status(now)['idle_timeout_time'])
        self.hh.run_gcode('MMU_PAUSE FORCE_IN_PRINT=1')
        now = self.hh.reactor.monotonic()
        self.assertGreater(mmu.get_status(now)['idle_timeout_time'], now)

    def test_idle_deadline_respects_timeout_reset_after_long_idle(self):
        mmu = self.hh.mmu
        idle = self.hh.printer.lookup_object('idle_timeout')
        idle.set_state('Ready')
        now = self.hh.reactor.monotonic()
        with mock.patch.object(mmu.toolhead, 'check_busy', return_value=(100., 400., True)):
            self.hh.run_gcode('SET_IDLE_TIMEOUT TIMEOUT=60')
            self.assertEqual(idle.timeout_timer.waketime, now + 60.)
            self.assertEqual(mmu._idle_timeout_time(now), round(now + 60.))
            # Polling later must not move the deadline or subtract the idle
            # interval that preceded SET_IDLE_TIMEOUT a second time.
            with mock.patch.object(mmu.toolhead, 'check_busy', return_value=(100., 410., True)):
                self.assertEqual(mmu._idle_timeout_time(now + 10.), round(now + 60.))

    def test_idle_deadline_respects_a_scheduled_retry(self):
        mmu = self.hh.mmu
        idle = self.hh.printer.lookup_object('idle_timeout')
        idle.set_state('Ready')
        idle.timeout_timer.waketime = 1001.
        # Klipper retries after one second if G-code is busy at expiry.
        with mock.patch.object(mmu.toolhead, 'check_busy', return_value=(100., 100. + idle.idle_timeout + 10., True)):
            self.assertEqual(mmu._idle_timeout_time(1000.), 1001.)

    def test_idle_deadline_is_absent_without_an_active_timer(self):
        mmu = self.hh.mmu
        idle = self.hh.printer.lookup_object('idle_timeout')
        idle.set_state('Ready')
        for timer in (None, SimpleNamespace(waketime=self.hh.reactor.NEVER)):
            with self.subTest(timer=timer), mock.patch.object(idle, 'timeout_timer', timer):
                self.assertIsNone(mmu._idle_timeout_time(self.hh.reactor.monotonic()))

    def test_idle_deadline_is_absent_when_kalico_disables_the_timeout(self):
        idle = self.hh.printer.lookup_object('idle_timeout')
        idle.set_state('Ready')
        idle.idle_timeout = 0.  # Kalico accepts timeout: 0 in its config
        self.assertIsNone(self.hh.mmu._idle_timeout_time(self.hh.reactor.monotonic()))

    def test_idle_timeout_parameter_stays_at_its_configfile_value(self):
        mmu = self.hh.mmu
        settings = self.hh.printer.lookup_object('configfile').get_status(0)['settings']
        self.assertEqual(mmu.p.default_idle_timeout, settings['mmu_parameters']['default_idle_timeout'])
        self.assertNotIn('default_idle_timeout', mmu.get_status(0)['config_changes']['mmu_parameters'])
        self.assertEqual(mmu.default_idle_timeout, mmu.klipper_idle_timeout)
        self.assertEqual(mmu.klipper_idle_timeout, self.hh.printer.lookup_object('idle_timeout').idle_timeout)

    def test_disable_restores_klippers_idle_timeout(self):
        idle = self.hh.printer.lookup_object('idle_timeout')
        original = idle.idle_timeout
        self.hh.run_gcode('SET_IDLE_TIMEOUT TIMEOUT=%d' % (original + 1000))
        self.hh.run_gcode('MMU ENABLE=0')
        self.assertEqual(idle.idle_timeout, original)

    def test_explicit_idle_timeout_overrides_klippers(self):
        mmu = self.hh.mmu
        self.hh.run_gcode('MMU_TEST_CONFIG DEFAULT_IDLE_TIMEOUT=300')
        self.assertEqual(mmu.default_idle_timeout, 300)
        self.assertEqual(mmu.get_status(0)['config_changes']['mmu_parameters']['default_idle_timeout'], 300)

    def test_calibrated_values_persist_through_save_variables_not_config_changes(self):
        before = dict(self.hh.mmu.get_status(0)['config_changes'])
        self.hh.run_gcode('MMU_TEST_CONFIG CALIBRATED_BOWDEN_LENGTH=650')
        self.assertEqual(self.hh.mmu.get_status(0)['config_changes'], before)

    def test_homing_status_lifetime_and_failure_cleanup(self):
        mmu = self.hh.mmu
        drive = mmu.drive()
        for direction in (1, -1):
            for fail in (False, True):
                with self.subTest(direction=direction, fail=fail):
                    def home(*args, **kwargs):
                        state = mmu.get_status(0)['homing']
                        self.assertEqual(state, dict(endstops=['unit0:filament_compression'], triggered=direction > 0))
                        state['endstops'].clear()
                        self.assertTrue(mmu.homing['endstops'])
                        if fail:
                            raise RuntimeError('homing failed')
                        return {'trig_pos': 0}
                    with mock.patch.object(drive, 'get_endstop', return_value=SimpleNamespace()), \
                         mock.patch.object(drive._driving_stepper, 'do_homing_move', side_effect=home):
                        if fail:
                            with self.assertRaises(RuntimeError):
                                drive.move(1, 10, 100, direction, 'filament_compression')
                        else:
                            drive.move(1, 10, 100, direction, 'filament_compression')
                    self.assertIsNone(mmu.get_status(0)['homing'])

    def test_compound_homing_reports_each_sensor(self):
        mmu = self.hh.mmu
        drive = mmu.drive()
        endstop = SimpleNamespace(endstop_names={object(): 'extruder', object(): 'filament_compression'})
        def home(*args, **kwargs):
            self.assertEqual(mmu.homing['endstops'], ['default:extruder', 'unit0:filament_compression'])
            return {'trig_pos': 0}
        with mock.patch.object(drive, 'get_endstop', return_value=endstop), \
             mock.patch.object(drive._driving_stepper, 'do_homing_move', side_effect=home):
            drive.move(1, 10, 100, 1, 'compound')
        self.assertIsNone(mmu.homing)

    def test_grip_is_already_in_selector_status(self):
        self.assertTrue({'servo', 'grip'} & set(self.hh.mmu.get_status(0)['selector']) or
                        self.hh.mmu.mmu_unit().filament_always_gripped)


class TestPositionFollowsTheExtruder(unittest.TestCase):
    def setUp(self):
        self.hh = session('emu')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu

    def position(self):
        return self.mmu.get_status(self.hh.reactor.monotonic())['filament_position']

    def test_extruder_moves_count_whether_or_not_the_gear_is_synced(self):
        from extras.mmu.mmu_constants import DRIVE_UNSYNCED, DRIVE_GEAR_SYNCED_TO_EXTRUDER
        drive = self.mmu.drive()
        start = self.position()
        self.hh.run_gcode('M83')
        self.hh.run_gcode('G1 E10 F600')
        self.assertAlmostEqual(self.position() - start, 10, places=2)
        drive.sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        self.hh.run_gcode('G1 E10 F600')
        self.assertAlmostEqual(self.position() - start, 20, places=2)
        drive.sync_mode(DRIVE_UNSYNCED)
        self.assertAlmostEqual(self.position() - start, 20, places=2)

    def live(self):
        return self.mmu.get_status(self.hh.reactor.monotonic())['filament_position_live']

    def test_the_live_position_follows_mmu_commands_and_holds_between_them(self):
        self.hh.run_gcode('M83')
        with self.mmu.wrap_command():
            self.hh.run_gcode('G1 E10 F600')
            self.assertEqual(self.live(), 10.)
        self.hh.run_gcode('G1 E10 F600')
        self.assertEqual(self.position(), 20.)
        self.assertEqual(self.live(), 10.)
        self.mmu.drive().set_filament_position(5.)
        self.assertEqual(self.live(), 5.)


class TestLivePositionBeforeClockSync(unittest.TestCase):
    """
    filament_position_live asks the MCU where the stepper was at 'eventtime'. Happy Hare's own
    get_status(0) calls (the filament position diagram, MMU_SELECT, MMU_STATUS...) pass 0, which
    converts to a negative MCU clock. Real Klipper hands that clock to an unsigned C argument and
    raises OverflowError; the harness's fake ignores print_time, so it is mimicked here.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot(calibrate=True, selected_gate=0)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        stepper = self.mmu.drive()._driving_stepper.stepper
        real = stepper.get_past_mcu_position

        def like_klipper(print_time):
            if stepper.get_mcu().print_time_to_clock(print_time) < 0:
                raise OverflowError("can't convert negative number to unsigned")
            return real(print_time)
        self.past = mock.patch.object(stepper, 'get_past_mcu_position', side_effect=like_klipper)
        self.past_mock = self.past.start()
        self.addCleanup(self.past.stop)

    def test_status_at_time_zero_does_not_raise(self):
        with self.mmu.wrap_command():
            status = self.mmu.get_status(0)
        self.assertEqual(status['filament_position_live'], round(self.mmu.drive().get_filament_position(), 1))

    def test_filament_position_diagram_does_not_raise(self):
        """The path MMU_SELECT and selecting a spool take"""
        self.assertTrue(self.mmu.get_filament_position_string())

    def test_a_real_time_still_asks_the_mcu(self):
        with self.mmu.wrap_command():
            self.mmu.get_status(self.hh.reactor.monotonic())
        self.assertTrue(self.past_mock.called)
