# Happy Hare test harness - espooler pin discovery on a multi-unit machine.
#
# Espooler pins are written per-unit and ALWAYS start at '_0' (mmu_hardware.cfg:778-785 renders
# `for i in range(0, PARAM_NUM_GATES)`, and the section comment above it says so outright), but
# everything downstream of the config read - the mcu pin names, respool_gates/assist_gates, the
# operation state - is keyed on the MACHINE-WIDE gate number. MmuESpooler has to translate
# between the two, and only when it reads the config.
#
# Getting it wrong does not raise. config.get() for a key that was never rendered returns the
# default, so the second unit simply finds no pins, registers nothing, and reports an empty
# respool_gates. The espooler is then silently inert on that unit for the life of the install:
# rewind and assist do nothing, no error is logged, and the only symptom is filament that never
# gets wound back in. That is why this asserts on the registered pin set rather than on a boot
# error - there is no boot error to catch.
#
# Only unit1+ can express the bug: on unit0 first_gate is 0, so the local and machine gate
# numbers are identical and a missing translation is invisible. Two BoxTurtles is the cheapest
# fixture that produces it - BoxTurtle is the profile that enables espoolers by default
# (Kconfig.box_turtle), and clone_across_units is the sanctioned way to get a multi-unit shape
# out of a single-unit machine (see its note on why deriving with extra_params is wrong).
#
#   ./venv/bin/python -m unittest test.test_mmu_espooler_pins
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session, profiles

logging.getLogger().setLevel(logging.CRITICAL)

# A test fixture, not a machine - nobody runs two identical BoxTurtles. It lives here rather
# than in the PROFILES registry for the same reason TWO_UNIT does in test_mmu_config.
TWO_BOXTURTLES = profiles.clone_across_units(
    'two_boxturtles_espooler', profiles.get('boxturtle'), ('unit0', 'unit1'),
    description='two BoxTurtles, for per-unit espooler pin numbering')


class TestMultiUnitEspoolerPins(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hh = session(TWO_BOXTURTLES)
        cls.hh.boot(calibrate=True)
        assert cls.hh.errors == [], 'bootup was not clean: %s' % (cls.hh.errors,)
        machine = cls.hh.mmu.mmu_machine
        cls.unit0 = machine.get_mmu_unit_by_index(0)
        cls.unit1 = machine.get_mmu_unit_by_index(1)

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_both_units_own_the_gates_the_fixture_promises(self):
        """
        Guards the fixture itself. If BoxTurtle ever changes gate count the assertions below
        would still pass while testing nothing, so pin the shape the rest of the file assumes.
        """
        self.assertEqual(self.unit0.gate_bounds(), (0, 3))
        self.assertEqual(self.unit1.gate_bounds(), (4, 7))
        self.assertNotEqual(self.unit1.first_gate, 0,
                            'unit1 must not start at gate 0 or the bug cannot be expressed')

    def test_second_unit_registers_its_espooler_pins(self):
        """
        The regression. Reading 'respool_motor_pin_4' - a key the per-unit render never
        writes - left unit1 with an entirely empty pin set and no complaint.
        """
        for unit, gates in ((self.unit0, [0, 1, 2, 3]), (self.unit1, [4, 5, 6, 7])):
            with self.subTest(unit=unit.name):
                espooler = unit.espooler
                self.assertIsNotNone(espooler, 'BoxTurtle is expected to have an espooler')
                self.assertEqual(espooler.respool_gates, gates)
                self.assertEqual(espooler.assist_gates, gates)

    def test_pins_are_named_by_machine_gate_not_local_gate(self):
        """
        The translation is confined to the config read. _update_pwm() builds 'respool_%d' from
        the machine gate, so naming the pins locally would fix this test and break the printer:
        both units would claim 'respool_0' and unit1's lookups would find unit0's hardware.
        """
        expected = set()
        for gate in range(4, 8):
            expected.update(('respool_%d' % gate, 'assist_%d' % gate))
        self.assertTrue(expected.issubset(set(self.unit1.espooler.motor_mcu_pins)),
                        'unit1 pins: %s' % sorted(self.unit1.espooler.motor_mcu_pins))
        self.assertFalse(set(self.unit0.espooler.motor_mcu_pins)
                         & set(self.unit1.espooler.motor_mcu_pins),
                         'the two units must not share pin names')


if __name__ == '__main__':
    unittest.main()
