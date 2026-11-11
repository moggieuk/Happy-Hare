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

    def test_viewer_only_fields_are_not_in_mmu_status(self):
        status = self.hh.mmu.get_status(0)
        for key in ('filament_grip_state', 'sensor_states', 'active_sensors'):
            self.assertNotIn(key, status)
        self.assertIsNone(status['homing'])

    def test_runtime_edits_are_reported_against_their_config_section(self):
        mmu = self.hh.mmu
        self.assertNotIn('mmu_toolhead default', mmu.get_status(0)['config_changes'])
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=3')
        self.hh.run_gcode('MMU_TEST_CONFIG LOG_LEVEL=2')
        changes = mmu.get_status(0)['config_changes']
        self.assertEqual(changes['mmu_toolhead default'], {'toolhead_residual_filament': 3})
        self.assertEqual(changes['mmu_parameters']['log_level'], 2)

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
