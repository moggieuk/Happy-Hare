# Happy Hare filament drying cycle tests.
#
# Klipper's I2C environment drivers expose humidity on their own chip object
# ("aht10 <name>", "htu21d <name>", ...), not on the temperature_sensor, and the
# harness has no such drivers, so each test registers a small stand-in.

import sys
import unittest
from unittest import mock

from test.hh import profiles, session
from test.hh.bootstrap import PRINTER_STUB

ENV_CHECK_INTERVAL = 30


class _FakeHumidityChip:
    """Stand-in for a Klipper aht10/htu21d/sht3x/bme280 chip object."""

    def __init__(self, temperature, humidity):
        self.temperature = temperature
        self.humidity = humidity

    def get_status(self, eventtime=None):
        return {'temperature': round(self.temperature, 2), 'humidity': self.humidity}


class TestDryingHumidity(unittest.TestCase):

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        self.sensor_name = self.unit.environment_sensor.split()[-1]

    def _add_chip(self, temperature, humidity):
        chip = _FakeHumidityChip(temperature, humidity)
        self.hh.printer.add_object('aht10 %s' % self.sensor_name, chip)
        return chip

    def _dry(self, humidity=20):
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER DRY=1 TEMP=45 TIMER=60 HUMIDITY=%d' % humidity)
        self.hh.settle()
        return '\n'.join(self.hh.console[at:])

    def test_failed_read_does_not_end_drying_early(self):
        chip = self._add_chip(45., 40)
        self._dry()
        self.assertTrue(self.manager.is_drying())

        # Klipper zeroes temperature and humidity on a failed read and stops sampling
        chip.temperature, chip.humidity = 0., 0
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertTrue(self.manager.is_drying())
        self.hh.run_gcode('MMU_HEATER')
        self.assertEqual(self.hh.errors, [])

        at = len(self.hh.console)
        chip.temperature, chip.humidity = 45., 15
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertFalse(self.manager.is_drying())
        self.assertIn('humidity goal 20.0% reached', '\n'.join(self.hh.console[at:]))

    def test_a_real_zero_humidity_reading_still_meets_the_goal(self):
        chip = self._add_chip(45., 40)
        self._dry()

        at = len(self.hh.console)
        chip.humidity = 0
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertFalse(self.manager.is_drying())
        self.assertIn('humidity goal 20.0% reached', '\n'.join(self.hh.console[at:]))

    def test_humidity_goal_without_a_humidity_reading_is_warned(self):
        output = self._dry()
        self.assertTrue(self.manager.is_drying())
        self.assertIn('reports no humidity reading', output)
        self.assertIn('20.0% humidity goal is ignored', output)

    def test_no_warning_when_the_sensor_reports_humidity(self):
        self._add_chip(45., 40)
        output = self._dry()
        self.assertTrue(self.manager.is_drying())
        self.assertNotIn('reports no humidity reading', output)
        self.assertEqual(self.hh.errors, [])



class TestDryingVent(unittest.TestCase):

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.vent = self.hh.printer.lookup_object('gcode_macro _MMU_VENT')

    def test_vent_macro_is_told_which_unit_is_drying(self):
        self.hh.run_gcode('MMU_HEATER DRY=1 TEMP=45 TIMER=60 VENT_INTERVAL=1')
        self.hh.settle()
        self.assertEqual(self.vent.calls, [])
        self.hh.reactor.advance(60 + ENV_CHECK_INTERVAL)
        self.assertEqual(self.vent.calls[0], '_MMU_VENT UNIT=%s OPEN=1' % self.unit.name)


class TestDryingRotationWhilePrinting(unittest.TestCase):
    """The QIDI Box turns its spools with a gear per gate (gear_rotates_spool)."""

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.stepper_enable = self.hh.printer.lookup_object('stepper_enable')
        self.motion_queuing = self.hh.printer.lookup_object('motion_queuing')
        self.mcu = self.hh.printer.lookup_object('mcu')
        # Klipper's print time is never negative but the harness starts it at -HOST_OFFSET.
        # Idle gears have next_cmd_time 0, so let print time pass that as on a running printer
        self.hh.reactor.advance(1. - self._est())
        for gate in (1, 2):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=0 QUIET=1' % gate)
            # Gears are auto-enabled by their steps on real Klipper
            self._enable(gate).motor_enable(0.)

    def _est(self):
        return self.mcu.estimated_print_time(self.hh.reactor.monotonic())

    def _gear(self, gate):
        return self.unit.drive_obj(gate).mmu_gear_stepper

    def _enable(self, gate):
        return self.stepper_enable.lookup_enable(self._gear(gate).stepper.get_name())

    def _moves(self, gate, since=0):
        return self._gear(gate).manual_trapq.moves[since:]

    def _record_queueing(self, step_gen_ahead=0.):
        """Records (est_print_time, restart point, queued end time) for every background move"""
        queued = []
        restart = self.motion_queuing.calc_step_gen_restart
        note = self.motion_queuing.note_mcu_movequeue_activity

        def calc_step_gen_restart(est_print_time):
            if step_gen_ahead:
                self.motion_queuing.last_step_gen_time = est_print_time + step_gen_ahead
            return restart(est_print_time)

        def note_mcu_movequeue_activity(mq_time, *args, **kwargs):
            est = self._est()
            point = max(est + self._gear(1).BACKGROUND_MOVE_START_TIME, calc_step_gen_restart(est))
            queued.append((est, point, mq_time))
            return note(mq_time, *args, **kwargs)

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.motion_queuing, 'calc_step_gen_restart', side_effect=calc_step_gen_restart).start()
        mock.patch.object(self.motion_queuing, 'note_mcu_movequeue_activity', side_effect=note_mcu_movequeue_activity).start()
        return queued

    def _dry_and_rotate(self, in_print=True, rotate_in_print=1, gates='GATES=1,2 '):
        self.unit.p.heater_rotate_in_print = rotate_in_print
        if in_print:
            self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER DRY=1 %sROTATE=1 ROTATE_INTERVAL=1 TEMP=45 TIMER=60' % gates)
        # Rotation runs on the third check
        self.hh.reactor.advance(3 * ENV_CHECK_INTERVAL + 20)
        return '\n'.join(self.hh.console[at:])

    def _no_background_moves(self):
        return mock.patch.object(type(self._gear(1)), 'can_background_move', return_value=False)

    def test_idle_gears_rotate_without_stalling_the_toolhead(self):
        dwells = len(self.toolhead.dwells)
        before = {gate: len(self._moves(gate)) for gate in (0, 1, 2)}
        queued = self._record_queueing()

        self._dry_and_rotate()

        self.assertTrue(self.manager.is_drying())
        self.assertEqual(len(self.toolhead.dwells), dwells)
        self.assertEqual(self._moves(0, before[0]), [])
        ends = {}
        for gate in (1, 2):
            moves = self._moves(gate, before[gate])
            self.assertGreater(len(moves), 1) # Split into short moves
            self.assertAlmostEqual(sum(m['axes_r'][0] * self._distance(m) for m in moves), -100., places=3)
            ends[gate] = max(m['print_time'] + m['accel_t'] + m['cruise_t'] + m['decel_t'] for m in moves)
            el = self._enable(gate)
            self.assertFalse(el.is_motor_enabled())
            self.assertGreaterEqual(el.transitions[-1][0], ends[gate])
        # One spool after the other
        self.assertGreaterEqual(self._moves(2, before[2])[0]['print_time'], ends[1])
        self.assertTrue(queued)
        self.assertEqual(self.hh.errors, [])

    @staticmethod
    def _distance(move):
        accel_d = .5 * move['cruise_v'] * move['accel_t']
        return 2. * accel_d + move['cruise_v'] * move['cruise_t']

    def _assert_bounded(self, queued):
        segment_time = sys.modules[type(self.manager).__module__].ROTATE_SEGMENT_TIME
        self.assertTrue(queued)
        for _, point, mq_time in queued:
            self.assertLessEqual(mq_time, point + segment_time + 1e-6)

    def test_little_is_queued_ahead_so_a_step_flush_never_waits_long(self):
        queued = self._record_queueing()
        self._dry_and_rotate()
        self._assert_bounded(queued)

    def test_moves_start_after_steps_already_generated(self):
        queued = self._record_queueing(step_gen_ahead=0.7)
        before = len(self._moves(1))
        self._dry_and_rotate()
        self._assert_bounded(queued)
        for (est, _, _), move in zip(queued, self._moves(1, before)):
            self.assertGreaterEqual(move['print_time'], est + 0.7)

    def test_skipped_while_printing_unless_enabled(self):
        before = len(self._moves(1))
        at = len(self.hh.console)
        self._dry_and_rotate(rotate_in_print=0)
        self.hh.reactor.advance(2 * 60) # Two more rotation intervals
        output = self.hh.console[at:]
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(self._moves(1, before), [])
        self.assertFalse(any('Rotating spools' in line for line in output))
        skipped = [line for line in output if 'rotation is skipped while printing' in line]
        self.assertEqual(len(skipped), 1)
        self.assertIn('set heater_rotate_in_print', skipped[0])

    def test_skipped_while_printing_when_not_possible(self):
        before = len(self._moves(1))
        with self._no_background_moves():
            output = self._dry_and_rotate()
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(self._moves(1, before), [])
        self.assertIn('needs a gear per gate', output)
        self.assertEqual(self.hh.errors, [])

    def test_rotation_requested_while_printing_is_not_refused(self):
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        with self._no_background_moves():
            self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 TEMP=45 TIMER=60')
            self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(self.hh.errors, [])

    def test_a_shared_heater_rotates_without_explicit_gates(self):
        before = {gate: len(self._moves(gate)) for gate in (1, 2)}
        output = self._dry_and_rotate(gates='')
        self.assertTrue(self.manager.is_drying())
        for gate in (1, 2):
            self.assertGreater(len(self._moves(gate, before[gate])), 0)
        self.assertNotIn('cannot rotate', output)
        self.assertEqual(self.hh.errors, [])

    def test_nothing_rotates_while_paused(self):
        before = len(self._moves(1))
        with mock.patch.object(type(self.hh.mmu), 'is_paused', return_value=True):
            self._dry_and_rotate()
        self.assertEqual(self._moves(1, before), [])
        self.assertTrue(self.manager.is_drying())

    def test_stopping_the_cycle_finishes_the_move_and_turns_the_gear_off(self):
        before = len(self._moves(1))
        self.unit.p.heater_rotate_in_print = 1
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 ROTATE_INTERVAL=1 TEMP=45 TIMER=60')
        # The third check (60s) starts the rotation
        self.hh.reactor.advance(2 * ENV_CHECK_INTERVAL + 0.01)
        moved = len(self._moves(1, before))
        self.assertGreater(moved, 0)
        self.hh.run_gcode('MMU_HEATER STOP=1')
        self.hh.reactor.advance(10)
        self.assertEqual(len(self._moves(1, before)), moved)
        self.assertFalse(self._enable(1).is_motor_enabled())
        self.assertEqual(len(self._moves(2)), 0)

    def test_an_error_while_rotating_does_not_stop_drying(self):
        with mock.patch.object(type(self._gear(1)), 'do_background_move', side_effect=Exception('boom')):
            output = self._dry_and_rotate()
        self.assertTrue(self.manager.is_drying())
        self.assertIn('Spool rotation while printing stopped: boom', output)

    def test_outside_a_print_rotation_is_unchanged(self):
        before = len(self._moves(1))
        output = self._dry_and_rotate(in_print=False, rotate_in_print=0)
        self.assertIn('Rotating spools in gates: 0,1,2,3...', output)
        self.assertGreater(len(self._moves(1, before)), 0)


class TestDryingRotationOnKalico(unittest.TestCase):
    """Kalico releases up to v2026.10.00 have no motion_queuing, so rotation is skipped in a print."""

    SESSION_KWARGS = {'kalico': True}

    def setUp(self):
        self.hh = session('qidi', **self.SESSION_KWARGS)
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        for gate in (1, 2):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=0 QUIET=1' % gate)

    def _gear(self, gate):
        return self.unit.drive_obj(gate).mmu_gear_stepper

    def _dry_and_rotate(self):
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 ROTATE_INTERVAL=1 TEMP=45 TIMER=60')
        self.hh.reactor.advance(3 * ENV_CHECK_INTERVAL + 20)
        return '\n'.join(self.hh.console[at:])

    def test_background_moves_are_not_available(self):
        self.assertFalse(self._gear(1).can_background_move())
        self.assertFalse(self.manager.can_rotate_while_printing())

    def test_drying_continues_without_rotation_while_printing(self):
        self.unit.p.heater_rotate_in_print = 1
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        moves = len(self._gear(1).manual_trapq.moves)
        output = self._dry_and_rotate()
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(len(self._gear(1).manual_trapq.moves), moves)
        self.assertIn('needs a gear per gate and a recent Klipper or Kalico', output)
        self.assertEqual(self.hh.errors, [])

    def test_outside_a_print_rotation_is_unchanged(self):
        moves = len(self._gear(1).manual_trapq.moves)
        output = self._dry_and_rotate()
        self.assertIn('Rotating spools in gates: 0,1,2,3...', output)
        self.assertGreater(len(self._gear(1).manual_trapq.moves), moves)
        self.assertEqual(self.hh.errors, [])

    def test_shared_heater_limit_applies(self):
        heater = self.hh.printer.lookup_object('heaters').heaters[self.unit.filament_heater.split()[-1]]
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL=PLA AVAILABLE=1 QUIET=1')
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 TEMP=60 TIMER=60')
        self.assertEqual(heater.target_temp, 45.)


class TestDryingRotationOnOldKlipper(TestDryingRotationOnKalico):
    """Klipper before motion_queuing uses the same emulator."""

    SESSION_KWARGS = {'old_klipper': True}


class TestDryingEnclosureLimit(unittest.TestCase):
    """The QIDI Box has one heater, so every spool in it is heated, selected for drying or not."""

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        heater_name = self.unit.filament_heater.split()[-1]
        self.heater = self.hh.printer.lookup_object('heaters').heaters[heater_name]
        for gate in range(4):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=0 QUIET=1' % gate)

    def _load(self, gate, material='PLA'):
        self.hh.run_gcode('MMU_GATE_MAP GATE=%d MATERIAL=%s AVAILABLE=1 QUIET=1' % (gate, material))

    def _dry(self, temp=60):
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 TEMP=%d TIMER=60' % temp)
        return '\n'.join(self.hh.console[at:])

    def test_filament_in_an_unselected_gate_limits_the_temperature(self):
        self._load(0)
        output = self._dry()
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(self.heater.target_temp, 45.)
        self.assertIn('limited to 45.0°C, the maximum for PLA in gate 0', output)
        self.assertIn('Drying temp: 45.0', output)

    def test_unknown_material_counts_as_the_default_drying_temperature(self):
        self.unit.p.heater_default_dry_temp = 50.
        self._load(0, material='MYSTERY')
        self._dry()
        self.assertEqual(self.heater.target_temp, 50.)

    def test_no_limit_with_all_gates_empty(self):
        output = self._dry()
        self.assertEqual(self.heater.target_temp, 60.)
        self.assertNotIn('limited', output)

    def test_the_limit_never_raises_the_temperature(self):
        self._load(0, material='PETG')
        output = self._dry(temp=50)
        self.assertEqual(self.heater.target_temp, 50.)
        self.assertNotIn('limited', output)

    def test_limit_applies_when_filament_is_loaded_during_drying(self):
        self._dry()
        self._load(0)
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertEqual(self.heater.target_temp, 45.)

    def test_limit_is_kept_for_the_rest_of_the_cycle(self):
        self._load(0)
        self._dry()
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=0 QUIET=1')
        self.hh.reactor.advance(2 * ENV_CHECK_INTERVAL)
        self.assertTrue(self.manager.is_drying())
        self.assertEqual(self.heater.target_temp, 45.)

    def test_a_manual_temperature_is_limited_too(self):
        self._load(0)
        self._dry()
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER TEMP=60')
        self.assertEqual(self.heater.target_temp, 45.)
        self.assertIn('limited to 45.0°C', '\n'.join(self.hh.console[at:]))
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertEqual(self.heater.target_temp, 45.)

        # Still limited after the filament is unloaded
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=0 QUIET=1')
        self.hh.run_gcode('MMU_HEATER TEMP=60')
        self.hh.reactor.advance(2 * ENV_CHECK_INTERVAL)
        self.assertEqual(self.heater.target_temp, 45.)

    def test_a_lower_manual_temperature_is_used(self):
        self._load(0)
        self._dry()
        self.hh.run_gcode('MMU_HEATER TEMP=40')
        self.assertEqual(self.heater.target_temp, 40.)

    def test_raw_heater_control_outside_a_cycle_is_unchanged(self):
        self._load(0)
        self.hh.run_gcode('MMU_HEATER TEMP=60')
        self.assertFalse(self.manager.is_drying())
        self.assertEqual(self.heater.target_temp, 60.)


class TestDryingDuringPause(unittest.TestCase):
    """heater_pause_temp turns the drying heater off or lowers it while the print is paused."""

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        heater_name = self.unit.filament_heater.split()[-1]
        self.heater = self.hh.printer.lookup_object('heaters').heaters[heater_name]
        for gate in range(4):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=0 QUIET=1' % gate)
        self.paused = False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(type(self.hh.mmu), 'is_paused', side_effect=lambda: self.paused).start()

    def _dry(self, temp=60, timer=60):
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 TEMP=%d TIMER=%d' % (temp, timer))

    def _pause(self, paused=True):
        at = len(self.hh.console)
        self.paused = paused
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        return '\n'.join(self.hh.console[at:])

    def test_drying_continues_through_a_pause_by_default(self):
        self.assertEqual(self.unit.p.heater_pause_temp, -1.)
        self._dry()
        output = self._pause()
        self.assertEqual(self.heater.target_temp, 60.)
        self.assertNotIn('Print paused', output)

    def test_heater_off_while_paused_and_back_on_resume(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry()
        output = self._pause()
        self.assertEqual(self.heater.target_temp, 0.)
        self.assertIn('Print paused: drying heater turned off until resume', output)
        self.assertTrue(self.manager.is_drying())
        output = self._pause(False)
        self.assertEqual(self.heater.target_temp, 60.)
        self.assertIn('Print resumed: drying heater restored', output)

    def test_heater_lowered_while_paused(self):
        self.unit.p.heater_pause_temp = 30.
        self._dry()
        output = self._pause()
        self.assertEqual(self.heater.target_temp, 30.)
        self.assertIn('lowered to 30.0°C until resume', output)
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 60.)

    def test_pause_temperature_never_raises_the_heater(self):
        self.unit.p.heater_pause_temp = 55.
        self._dry(temp=50)
        self._pause()
        self.assertEqual(self.heater.target_temp, 50.)

    def test_drying_started_while_paused_starts_lowered(self):
        self.unit.p.heater_pause_temp = 0.
        self.paused = True
        self._dry()
        self.assertEqual(self.heater.target_temp, 0.)
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 60.)

    def test_manual_temperature_while_paused_is_used_on_resume(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry()
        self._pause()
        self.hh.run_gcode('MMU_HEATER TEMP=50')
        self.assertEqual(self.heater.target_temp, 0.)
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 50.)

    def test_filament_loaded_while_paused_limits_the_restored_temperature(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry()
        self._pause()
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL=PLA AVAILABLE=1 QUIET=1')
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 45.)

    def test_the_drying_timer_keeps_running_while_paused(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry(timer=1)
        self._pause()
        self.hh.reactor.advance(ENV_CHECK_INTERVAL + 1)
        self.assertFalse(self.manager.is_drying())
        self.assertEqual(self.heater.target_temp, 0.)
        # Nothing comes back on resume once the cycle is over
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 0.)

    def test_raw_heater_control_outside_a_cycle_is_unchanged(self):
        self.unit.p.heater_pause_temp = 0.
        self.paused = True
        self.hh.run_gcode('MMU_HEATER TEMP=50')
        self.assertEqual(self.heater.target_temp, 50.)

    def test_manual_temperature_before_the_pause_is_noticed_is_restored(self):
        # The command applies the pause before the 30 s check sees it
        self.unit.p.heater_pause_temp = 0.
        self._dry()
        self.paused = True
        self.hh.run_gcode('MMU_HEATER TEMP=50')
        self.assertEqual(self.heater.target_temp, 0.)
        self._pause(False)
        self.assertEqual(self.heater.target_temp, 50.)

    def test_disabling_the_option_while_paused_says_why(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry()
        self._pause()
        self.unit.p.heater_pause_temp = -1.
        at = len(self.hh.console)
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        output = '\n'.join(self.hh.console[at:])
        self.assertEqual(self.heater.target_temp, 60.)
        self.assertIn('heater_pause_temp disabled: drying heater restored', output)
        self.assertNotIn('Print resumed', output)

    def test_status_shows_the_pause(self):
        self.unit.p.heater_pause_temp = 30.
        self._dry()
        self._pause()
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER')
        output = '\n'.join(self.hh.console[at:])
        self.assertIn('Paused: drying heater lowered to 30.0°C until resume', output)
        self._pause(False)
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_HEATER')
        self.assertNotIn('Paused:', '\n'.join(self.hh.console[at:]))

    def test_resume_on_the_last_check_does_not_heat_again(self):
        self.unit.p.heater_pause_temp = 0.
        self._dry(timer=1)
        self._pause()
        with mock.patch.object(self.manager, '_heater_on', wraps=self.manager._heater_on) as heater_on:
            self._pause(False)
            self.assertFalse(self.manager.is_drying())
        heater_on.assert_not_called()
        self.assertEqual(self.heater.target_temp, 0.)


# The EMU's per-gate heaters are the user's own [heater_generic] sections
EMU_HEATERS_STUB = PRINTER_STUB + ''.join('''
[heater_generic unit0_heater%d]
heater_pin: unit0_gate%d:PA1
sensor_type: Generic 3950
sensor_pin: unit0_gate%d:PA2
control: watermark
min_temp: 0
max_temp: 100
''' % (g, g, g) for g in range(5))


class TestDryingDuringPausePerGate(unittest.TestCase):
    """With a heater per gate (EMU) every active gate heater follows heater_pause_temp."""

    def setUp(self):
        profile = profiles.get('emu').derive('emu_heaters', syms={
            'MMU_HAS_HEATER': True, 'PARAM_MAX_CONCURRENT_HEATERS': 2})
        self.hh = session(profile, printer_stub=EMU_HEATERS_STUB)
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.environment_manager
        heaters = self.hh.printer.lookup_object('heaters').heaters
        self.heaters = [heaters['unit0_heater%d' % g] for g in range(4)]
        self.paused = False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(type(self.hh.mmu), 'is_paused', side_effect=lambda: self.paused).start()

    def _targets(self):
        return [h.target_temp for h in self.heaters]

    def test_active_gate_heaters_follow_the_pause(self):
        self.assertTrue(self.manager.has_per_gate_heaters())
        self.unit.p.heater_pause_temp = 30.
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 TEMP=50 TIMER=60')
        self.assertEqual(self._targets(), [0., 50., 50., 0.])
        self.paused = True
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertEqual(self._targets(), [0., 30., 30., 0.])
        self.paused = False
        self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertEqual(self._targets(), [0., 50., 50., 0.])


if __name__ == '__main__':
    unittest.main()
