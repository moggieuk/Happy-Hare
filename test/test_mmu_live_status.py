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

    def test_configuration_reflects_runtime_changes(self):
        mmu = self.hh.mmu
        before = mmu.mmu_machine.get_status(0)['configuration']
        self.hh.run_gcode('MMU_TEST_CONFIG TOOLHEAD_RESIDUAL_FILAMENT=3')
        after = mmu.mmu_machine.get_status(0)['configuration']
        self.assertNotEqual(before['units']['unit0']['toolhead']['parameters']['toolhead_residual_filament'], 3)
        self.assertEqual(after['units']['unit0']['toolhead']['parameters']['toolhead_residual_filament'], 3)
        self.assertTrue(after['units']['unit0']['has_per_gate_heaters'])

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
        endstop = SimpleNamespace(endstop_names={object():'extruder', object():'filament_compression'})
        def home(*args, **kwargs):
            self.assertEqual(mmu.homing['endstops'], ['default:extruder', 'unit0:filament_compression'])
            return {'trig_pos': 0}
        with mock.patch.object(drive, 'get_endstop', return_value=endstop), \
             mock.patch.object(drive._driving_stepper, 'do_homing_move', side_effect=home):
            drive.move(1, 10, 100, 1, 'compound')
        self.assertIsNone(mmu.homing)

    def test_proportional_value_is_subscribable(self):
        buffer = self.hh.mmu.mmu_unit().buffer
        with mock.patch.object(buffer.proportional_sensor, 'get_status', return_value={'value': .6}):
            self.assertEqual(buffer.get_status(0), {'value': .6})

    def test_all_sensor_states_are_detached(self):
        mmu = self.hh.mmu
        state = mmu.get_status(0)
        self.assertTrue(state['sensor_states'])
        self.assertTrue(set(state['active_sensors']).issubset(state['sensor_states']))
        state['sensor_states'].clear()
        self.assertTrue(mmu.get_status(0)['sensor_states'])
