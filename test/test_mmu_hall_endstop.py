# Happy Hare test harness - the experimental hall-effect toolhead endstop.
#
# [mmu_toolhead] hall_sensor_endstop / hall_adc1 / hall_adc2 (Qidi-style
# hall_filament_width_sensor pins) replace the extruder or toolhead switch with an
# MmuHallEndstop that reads the two ADC pins through MmuAdcHelper. No shipped profile
# sets these options, so the tests add them to the assembled config before boot.
#
#   ./venv/bin/python -m unittest test.test_mmu_hall_endstop
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
from unittest.mock import patch

from test.hh import cfg, session

PIN1, PIN2 = 'PA2', 'PA3'


class _HallEndstopBoot(unittest.TestCase):
    target = None

    def setUp(self):
        assemble = cfg.assemble
        target = self.target

        def with_hall(*args, **kwargs):
            fileconfig = assemble(*args, **kwargs)
            section = next(s for s in fileconfig.sections() if s.startswith('mmu_toolhead'))
            fileconfig.set(section, 'hall_sensor_endstop', target)
            fileconfig.set(section, 'hall_adc1', PIN1)
            fileconfig.set(section, 'hall_adc2', PIN2)
            return fileconfig

        with patch.object(cfg, 'assemble', with_hall):
            self.hh = session('boxturtle')
            self.addCleanup(self.hh.close)
            self.hh.boot()
        self.sensor = self.hh.mmu.mmu_unit(0).toolhead_wrapper.sensors[target]

    def test_boots_with_a_hall_endstop(self):
        from extras.mmu.unit.mmu_toolhead_wrapper import MmuHallEndstop
        self.assertEqual(self.hh.errors, [])
        self.assertIsInstance(self.sensor, MmuHallEndstop)

    def test_both_pins_are_bound_as_adc(self):
        self.hh.pins.assert_bound(PIN1, 'adc')
        self.hh.pins.assert_bound(PIN2, 'adc')


class TestHallAsExtruderSensor(_HallEndstopBoot):
    target = 'extruder'

    def feed(self, raw):
        self.sensor.mcu_adc.feed(raw)
        self.sensor.mcu_adc2.feed(raw)
        for _ in range(5):
            self.hh.reactor.advance(0.6)

    def test_readings_drive_the_sensor_state(self):
        sm = self.hh.mmu.sensor_manager
        self.feed(1.0)      # 2 x 10000 raw -> well above hall_min_diameter
        self.assertTrue(sm.check_sensor('extruder'))
        self.feed(0.0)
        self.assertFalse(sm.check_sensor('extruder'))


class TestHallAsToolheadSensor(_HallEndstopBoot):
    target = 'toolhead'


del _HallEndstopBoot


if __name__ == '__main__':
    unittest.main()
