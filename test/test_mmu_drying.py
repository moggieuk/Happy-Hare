# Happy Hare filament drying cycle tests.
#
# Klipper's I2C environment drivers expose humidity on their own chip object
# ("aht10 <name>", "htu21d <name>", ...), not on the temperature_sensor, and the
# harness has no such drivers, so each test registers a small stand-in.

import unittest

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


if __name__ == '__main__':
    unittest.main()
