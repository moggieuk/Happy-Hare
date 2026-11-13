# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify selector telemetry coverage, error outcomes and logging limits.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import unittest
from unittest import mock
from test.hh import session


class TestSelectorTelemetry(unittest.TestCase):
    def test_logging_limits(self):
        hh = session('chameleon')
        try:
            hh.boot(calibrate=True)
            for parameter in ('LOG_LEVEL', 'LOG_FILE_LEVEL'):
                hh.run_gcode('MMU_TEST_CONFIG %s=3' % parameter)
                self.assertEqual(getattr(hh.mmu.p, parameter.lower()), 3)
                with self.assertRaisesRegex(hh.gcode.error, 'maximum of 3'):
                    hh.run_gcode('MMU_TEST_CONFIG %s=4' % parameter)
                self.assertEqual(getattr(hh.mmu.p, parameter.lower()), 3)
        finally:
            hh.close()

    def test_grip_state_that_settles_after_the_last_event_is_committed_when_idle(self):
        from extras.mmu.telemetry.capture import TelemetryCapture
        hh = session('ercf_vvd')
        try:
            hh.boot(calibrate=True)
            hh.run_gcode('MMU_SELECT GATE=2')
            capture = TelemetryCapture(hh.mmu)
            hh.run_gcode('MMU_SERVO UNIT=0 POS=down')
            # The grip state is only set after the servo's own events, so nothing has recorded it yet
            self.assertEqual(capture.state['printer']['mmu']['filament_grip_state'], 0)
            capture.sync()
            self.assertEqual(capture.state['printer']['mmu']['filament_grip_state'], 1)
            delta = [r for r in capture.read()['records'] if r['event'] == 'state_delta'][-1]
            self.assertIn(['printer', 'mmu', 'filament_grip_state'], [c['path'] for c in delta['changes']])
            capture.close()
        finally:
            hh.close()

    def test_unsubscribed_selector_does_not_allocate_records(self):
        hh = session('chameleon')
        try:
            hh.boot(calibrate=True)
            telemetry = hh.mmu.movement_telemetry
            self.assertFalse(telemetry.listeners)
            before = (telemetry.move_id, telemetry.sequence)
            hh.mmu.selector()._move_selector('Unobserved selector', 1)
            self.assertEqual((telemetry.move_id, telemetry.sequence), before)
        finally:
            hh.close()

    def test_physical_selectors_emit_paired_motion_and_homing(self):
        for profile in ('tradrack', 'chameleon', 'ercf_vvd'):
            with self.subTest(profile=profile):
                hh = session(profile)
                try:
                    hh.boot(calibrate=True)
                    records = []
                    hh.mmu.movement_telemetry.subscribe(records.append)
                    for unit in hh.mmu.mmu_machine.units:
                        hh.run_gcode('MMU_HOME UNIT=%d' % unit.unit_index)
                        selector = unit.selector
                        selector._move_selector('Telemetry test', selector.selector_stepper.commanded_pos+1, speed=20, accel=100)
                    self.assertEqual(hh.errors, [])
                    starts = [r for r in records if r['event']=='selector_movement_start']
                    outcomes = {r['move_id']:r for r in records if r['event']=='selector_movement_outcome'}
                    self.assertTrue(any(r.get('homing') for r in starts))
                    self.assertEqual(len(starts), len(outcomes))
                    for start in starts:
                        self.assertIn(start['move_id'], outcomes)
                        self.assertEqual(start['unit'], outcomes[start['move_id']]['unit'])
                    moves = [r for r in starts if r.get('label')=='Telemetry test']
                    self.assertEqual(len(moves), len(hh.mmu.mmu_machine.units))
                    for start in moves:
                        self.assertEqual(start['requested_mm'], 1)
                        self.assertEqual(start['speed_mm_s'], 20)
                        self.assertEqual(start['acceleration_mm_s2'], 100)
                        self.assertAlmostEqual(outcomes[start['move_id']]['actual_mm'], 1)
                finally:
                    hh.close()

    def test_soaktest_selects_through_the_controller_so_the_gate_follows_the_selector(self):
        hh = session('tradrack')
        try:
            hh.boot(calibrate=True)
            records = []
            hh.mmu.movement_telemetry.subscribe(records.append)
            with mock.patch('random.randint', side_effect=lambda low, high: high), \
                 mock.patch('random.choice', side_effect=lambda gates: gates[-1]):
                hh.run_gcode('MMU_SOAKTEST_SELECTOR LOOP=1 GRIP=1')
            self.assertEqual(hh.errors, [])
            changed = [r for r in records if r.get('event') == 'gate_changed']
            self.assertTrue(changed)
            self.assertEqual(hh.mmu.gate_selected, changed[-1]['gate'])
            self.assertEqual(hh.mmu.gate_selected, hh.mmu.mmu_machine.units[0].gate_bounds()[1])
        finally:
            hh.close()

    def test_servo_selection_grip_and_off_preserve_command_arguments(self):
        for profile in ('mmx', 'tradrack'):
            with self.subTest(profile=profile):
                hh = session(profile)
                try:
                    hh.boot(calibrate=True)
                    selector = hh.mmu.selector()
                    owner = selector if profile=='mmx' else selector.servo
                    records=[]
                    hh.mmu.movement_telemetry.subscribe(records.append)
                    owner._set_servo_angle(30)
                    owner._set_servo_angle(60)
                    starts=[r for r in records if r['event']=='selector_movement_start' and r['actuator']=='servo']
                    self.assertEqual([r['target_angle_deg'] for r in starts], [30,60])
                    self.assertEqual(starts[1]['position_angle_deg'], 30)
                    if profile == 'mmx':
                        owner._servo_off()
                    else:
                        owner.servo_off()
                    self.assertEqual(records[-2]['pulse_width_s'], 0)
                    self.assertFalse(records[-1]['enabled'])
                    self.assertEqual(records[-1]['status'], 'submitted')
                    with mock.patch.object(owner.servo, 'set_position', side_effect=RuntimeError('servo failure')):
                        with self.assertRaisesRegex(RuntimeError, 'servo failure'):
                            owner._set_servo_angle(90)
                    self.assertEqual(records[-1]['event'], 'selector_movement_outcome')
                    self.assertEqual(records[-1]['status'], 'error')
                finally:
                    hh.close()

    def test_failed_stepper_move_has_terminal_outcome(self):
        hh=session('chameleon')
        try:
            hh.boot(calibrate=True)
            selector=hh.mmu.selector()
            records=[]
            hh.mmu.movement_telemetry.subscribe(records.append)
            with mock.patch.object(selector.selector_stepper, 'do_move', side_effect=RuntimeError('move failure')):
                with self.assertRaisesRegex(RuntimeError, 'move failure'):
                    selector._move_selector('Failing selector', 10)
            self.assertEqual([r['event'] for r in records], ['selector_movement_start','selector_movement_outcome'])
            self.assertEqual(records[-1]['status'], 'error')
        finally:
            hh.close()
