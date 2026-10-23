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

    def pause_on_unit0_then_select_unit1(self, paused_temp=210.):
        self.mmu.select_gate(0) # unit0, on the active extruder
        self.extruder.get_heater().set_temp(paused_temp)
        self.extruder1.get_heater().set_temp(200.)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.mmu.handle_mmu_error("test pause", force_in_print=True)
        self.assertTrue(self.mmu.is_mmu_paused())
        self.mmu.select_gate(9)

    def let_hotend_off_timer_expire(self):
        for _ in range(int(self.mmu.p.disable_heater) // 60 + 1): # advance() caps iterations, so slice it
            self.hh.reactor.advance(60.)

    def test_hotend_off_targets_the_extruder_paused_on(self):
        self.pause_on_unit0_then_select_unit1()
        self.let_hotend_off_timer_expire()

        self.assertEqual(self.extruder.get_heater().target_temp, 0)
        self.assertEqual(self.extruder1.get_heater().target_temp, 200.)

    def test_unlock_restores_the_extruder_paused_on(self):
        self.pause_on_unit0_then_select_unit1()
        self.let_hotend_off_timer_expire()
        mark = len(self.hh.gcode.executed)

        self.hh.run_gcode('MMU_UNLOCK')

        self.assertEqual(self.extruder.get_heater().target_temp, 210.)
        self.assertEqual(self.extruder1.get_heater().target_temp, 200.)
        for line in _heater_commands(self.hh.gcode.executed[mark:]):
            self.assertNotIn('extruder1', line)

    def test_resume_restores_the_extruder_paused_on(self):
        self.pause_on_unit0_then_select_unit1()
        self.let_hotend_off_timer_expire()

        self.mmu._continue_after('resume')

        self.assertEqual(self.extruder.get_heater().target_temp, 210.)
        self.assertEqual(self.extruder1.get_heater().target_temp, 200.)

    def test_heating_another_unit_while_paused_keeps_its_target(self):
        self.pause_on_unit0_then_select_unit1(paused_temp=230.)
        self.extruder1.get_heater().set_temp(245.)
        self.extruder1.get_heater().smoothed_temp = 180.
        mark = len(self.hh.gcode.executed)

        self.mmu._ensure_safe_extruder_temperature(wait=True) # as the load paths call it

        self.assertEqual(self.extruder1.get_heater().target_temp, 245.)
        self.assertEqual(self.extruder.get_heater().target_temp, 230.)
        waits = [l for l in self.hh.gcode.executed[mark:] if l.startswith('TEMPERATURE_WAIT')]
        self.assertEqual(len(waits), 1, waits)
        self.assertIn('SENSOR=extruder1', waits[0])

    def test_heating_another_cold_unit_while_paused_uses_its_gate_temperature(self):
        self.pause_on_unit0_then_select_unit1(paused_temp=230.)
        self.extruder1.get_heater().set_temp(0.)
        self.mmu.gate_temperature[9] = 245
        self.assertNotEqual(self.mmu.p.default_extruder_temp, 245)

        self.mmu._ensure_safe_extruder_temperature()

        self.assertEqual(self.extruder1.get_heater().target_temp, 245.)
        self.assertEqual(self.extruder.get_heater().target_temp, 230.)

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


class TestResidualFilamentHeating(unittest.TestCase):
    """Loading heats hot enough to expel the previous filament, then drops after the purge (#973)."""

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.extruder = self.hh.printer.lookup_object('extruder')
        self.wrapper = self.mmu.mmu_unit().extruder_wrapper
        self.wrapper.filament_remaining_temp = 270

    def tearDown(self):
        self.hh.close()

    def test_initial_load_heats_to_residual_then_drops_to_slicer_temp(self):
        self.hh.heat_extruder(220)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        mark = len(self.hh.gcode.executed)

        self.hh.run_gcode('MMU_CHANGE_TOOL STANDALONE=1 RESTORE=0 TOOL=1')

        self.assertFalse(self.mmu.is_mmu_paused())
        commands = _heater_commands(self.hh.gcode.executed[mark:])
        self.assertIn('M104 S270.0', commands)
        self.assertEqual(self.extruder.get_heater().target_temp, 220.)
        self.assertTrue(commands[-1].startswith('TEMPERATURE_WAIT') and 'MAXIMUM=222.0' in commands[-1], commands)
        self.assertEqual(self.wrapper.filament_remaining_temp, 220)
        self.assertTrue(any('to clear previous filament' in l for l in self.hh.console + self.hh.errors))
        self.assertTrue(any('Reducing extruder to 220' in l for l in self.hh.console))

    def test_cold_standalone_load_drops_to_default_after_purge(self):
        self.hh.run_gcode('MMU_SELECT GATE=1')
        mark = len(self.hh.gcode.executed)

        self.hh.run_gcode('MMU_LOAD')

        commands = _heater_commands(self.hh.gcode.executed[mark:])
        self.assertIn('M104 S270.0', commands)
        self.assertEqual(commands[-1], 'M104 S%.1f' % self.mmu.p.default_extruder_temp)
        self.assertEqual(self.extruder.get_heater().target_temp, self.mmu.p.default_extruder_temp)


if __name__ == '__main__':
    unittest.main()
