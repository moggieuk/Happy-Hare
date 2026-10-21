# Happy Hare test harness - heating the extruder a unit is configured for (#842, #872).
#
# On a toolchanger or IDEX the MMU can feed an extruder that is not the active one. A bare M104
# heats whichever extruder Klipper has active, while HH then waits on the unit's own extruder -
# so it heated the wrong hotend and waited forever.
#
#   ./venv/bin/python -m unittest test.test_mmu_extruder_heating
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


def _heater_commands(lines):
    return [l for l in lines if l.split()[0] in ('M104', 'M109', 'SET_HEATER_TEMPERATURE', 'TEMPERATURE_WAIT')]


class TestDualExtruderHeating(unittest.TestCase):

    def setUp(self):
        from test.hh.bootstrap import PRINTER_STUB
        from test.hh.profiles import EXTRA_EXTRUDER_STUB
        self.hh = session('ercf_vvd_dual_extruder',
                          printer_stub=PRINTER_STUB + EXTRA_EXTRUDER_STUB)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.extruder = self.hh.printer.lookup_object('extruder')
        self.extruder1 = self.hh.printer.lookup_object('extruder1')
        toolhead = self.hh.printer.lookup_object('toolhead')
        self.assertIs(toolhead.get_extruder(), self.extruder, 'expected [extruder] to be active')

        self.mmu.select_gate(9) # unit1, on extruder1
        self.assertEqual(self.mmu.mmu_unit().extruder_name(), 'extruder1')

    def tearDown(self):
        self.hh.close()

    def test_heating_targets_the_units_extruder(self):
        self.mmu._ensure_safe_extruder_temperature()

        self.assertGreater(self.extruder1.get_heater().target_temp, 0)
        self.assertEqual(self.extruder.get_heater().target_temp, 0)

    def test_hotend_off_turns_off_the_units_extruder(self):
        self.extruder.get_heater().set_temp(210.)
        self.extruder1.get_heater().set_temp(200.)

        self.mmu._hotend_off_handler(self.hh.reactor.monotonic())

        self.assertEqual(self.extruder1.get_heater().target_temp, 0)
        self.assertEqual(self.extruder.get_heater().target_temp, 210.)

    def test_cold_pull_uses_the_units_extruder(self):
        effects = getattr(self.hh.printer, 'harness_macro_effects', None)
        if effects is None:
            effects = self.hh.printer.harness_macro_effects = {}
        effects['MMU_COLD_PULL'] = lambda macro, gcmd: macro.run_body(gcmd)
        mark = len(self.hh.gcode.executed)

        self.hh.run_gcode('MMU_COLD_PULL MATERIAL=PLA HOT_TEMP=180 COLD_TEMP=170 '
                          'PULL_TEMP=175 MIN_EXTRUDE_TEMP=175 CLEAN_LENGTH=2')

        commands = _heater_commands(self.hh.gcode.executed[mark:])
        self.assertTrue(commands, 'cold pull issued no heater commands')
        for line in commands:
            self.assertIn('extruder1', line)
        self.assertEqual(self.extruder.get_heater().target_temp, 0)


class TestSingleExtruderHeating(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.extruder = self.hh.printer.lookup_object('extruder')
        self.mmu.select_gate(0)

    def tearDown(self):
        self.hh.close()

    def test_active_extruder_is_still_heated_with_m104(self):
        mark = len(self.hh.gcode.executed)

        self.mmu._ensure_safe_extruder_temperature()

        commands = _heater_commands(self.hh.gcode.executed[mark:])
        self.assertTrue(commands[0].startswith('M104 S'), commands)
        self.assertGreater(self.extruder.get_heater().target_temp, 0)

    def test_hotend_off_still_uses_m104(self):
        self.extruder.get_heater().set_temp(210.)
        mark = len(self.hh.gcode.executed)

        self.mmu._hotend_off_handler(self.hh.reactor.monotonic())

        self.assertEqual(_heater_commands(self.hh.gcode.executed[mark:]), ['M104 S0.0'])
        self.assertEqual(self.extruder.get_heater().target_temp, 0)


if __name__ == '__main__':
    unittest.main()
