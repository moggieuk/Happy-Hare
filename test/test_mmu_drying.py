# Happy Hare filament drying cycle tests.
#
# Klipper's I2C environment drivers expose humidity on their own chip object
# ("aht10 <name>", "htu21d <name>", ...), not on the temperature_sensor, and the
# harness has no such drivers, so each test registers a small stand-in.

import unittest
from unittest import mock

from test.hh import session

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
        # Klipper's print time is never negative but the harness starts it at -HOST_OFFSET.
        # Idle gears have next_cmd_time 0, so let print time pass that as on a running printer
        mcu = self.hh.printer.lookup_object('mcu')
        self.hh.reactor.advance(1. - mcu.estimated_print_time(self.hh.reactor.monotonic()))
        for gate in (1, 2):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=0 QUIET=1' % gate)

    def _gear(self, gate):
        return self.unit.drive_obj(gate).mmu_gear_stepper

    def _no_background_moves(self):
        return mock.patch.object(type(self._gear(1)), 'can_background_move', return_value=False)

    def test_idle_gears_rotate_without_stalling_the_toolhead(self):
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        for gate in (1, 2):
            # Gears are auto-enabled by their steps on real Klipper
            self.stepper_enable.lookup_enable(self._gear(gate).stepper.get_name()).motor_enable(0.)
        dwells = len(self.toolhead.dwells)
        moves = {gate: len(self._gear(gate).manual_trapq.moves) for gate in (0, 1, 2)}

        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 ROTATE_INTERVAL=1 TEMP=45 TIMER=60')
        # Rotation runs on the third check, the gears are disabled once both moves are done
        self.hh.reactor.advance(3 * ENV_CHECK_INTERVAL + 20)

        self.assertTrue(self.manager.is_drying())
        self.assertEqual(len(self.toolhead.dwells), dwells)
        self.assertEqual(len(self._gear(0).manual_trapq.moves), moves[0])
        end_times = []
        for gate in (1, 2):
            new = self._gear(gate).manual_trapq.moves[moves[gate]:]
            self.assertEqual(len(new), 1)
            self.assertLess(new[0]['axes_r'][0], 0.)
            end_times.append(new[0]['print_time'] + new[0]['accel_t'] + new[0]['cruise_t'] + new[0]['decel_t'])
            el = self.stepper_enable.lookup_enable(self._gear(gate).stepper.get_name())
            self.assertFalse(el.is_motor_enabled())
            self.assertGreaterEqual(el.transitions[-1][0], end_times[-1])
        # One spool after the other
        first = self._gear(2).manual_trapq.moves[moves[2]]
        self.assertGreaterEqual(first['print_time'], end_times[0])
        self.assertEqual(self.hh.errors, [])

    def test_rotation_is_refused_while_printing_when_not_possible(self):
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        with self._no_background_moves():
            self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 TEMP=45 TIMER=60')
        self.assertFalse(self.manager.is_drying())
        self.assertIn('not possible while printing', '\n'.join(self.hh.errors))

    def test_cycle_with_rotation_stops_when_a_print_starts(self):
        self.hh.run_gcode('MMU_HEATER DRY=1 GATES=1,2 ROTATE=1 ROTATE_INTERVAL=1 TEMP=45 TIMER=60')
        self.assertTrue(self.manager.is_drying())

        at = len(self.hh.console)
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        with self._no_background_moves():
            self.hh.reactor.advance(ENV_CHECK_INTERVAL)
        self.assertFalse(self.manager.is_drying())
        self.assertIn('Drying cycle stopped', '\n'.join(self.hh.console[at:]))


if __name__ == '__main__':
    unittest.main()
