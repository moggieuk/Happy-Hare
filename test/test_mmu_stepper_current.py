# Happy Hare test harness - stepper run current, modelled end to end.
#
# HH changes gear and extruder current by emitting SET_TMC_CURRENT gcode, which real Klipper
# handles in its TMC module. The harness used to have no handler, so the command fell through the
# ignore-unknown path: HH's own log line appeared, the driver never moved, and any assertion about
# the resulting current was really asserting the config default.
#
# So these tests are about the SIMULATION being faithful, not about HH's logic. They pin the three
# things that make a current change observable: the modelled driver tracks it, the change is
# recorded, and Klipper's own acknowledgment reaches the console alongside HH's line.
#
# 'emu' because its sync_gear_current is 52 rather than 100 - on a machine where it equals the
# driver default every change is suppressed as a no-op and nothing is observable at all.
#
#   ./venv/bin/python -m unittest test.test_mmu_stepper_current
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class StepperCurrentTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit()
        self.tmc = self.unit.gear_tmc_obj(0)
        self.default = self.unit.gear_default_current(0)
        self.assertTrue(self.default, 'profile has no TMC on gate 0 - nothing to model')

    def tearDown(self):
        self.hh.close()


class TestModelledDriver(StepperCurrentTestCase):

    def test_a_current_change_moves_the_modelled_driver(self):
        self.assertAlmostEqual(self.tmc.get_status()['run_current'], self.default, places=3)

        self.mmu._adjust_gear_current(gate=0, percent=50, reason="test")

        self.assertAlmostEqual(self.tmc.get_status()['run_current'], self.default * 0.5, places=3)

    def test_the_change_is_recorded_for_assertion(self):
        mark = len(self.tmc.current_changes)
        self.mmu._adjust_gear_current(gate=0, percent=50, reason="test")

        recorded = self.tmc.current_changes[mark:]
        self.assertEqual(len(recorded), 1)
        _print_time, run_current, _hold = recorded[0]
        self.assertAlmostEqual(run_current, self.default * 0.5, places=3)

    def test_each_stepper_is_modelled_independently(self):
        other = self.unit.gear_tmc_obj(1)
        self.assertIsNot(other, self.tmc, 'profile is not multigear - test is vacuous')

        self.mmu._adjust_gear_current(gate=0, percent=50, reason="test")

        self.assertAlmostEqual(self.tmc.get_status()['run_current'], self.default * 0.5, places=3)
        self.assertAlmostEqual(other.get_status()['run_current'],
                               self.unit.gear_default_current(1), places=3)

    def test_the_command_is_no_longer_unhandled(self):
        self.mmu._adjust_gear_current(gate=0, percent=50, reason="test")
        self.assertEqual([c for c in self.hh.gcode.unhandled if 'SET_TMC_CURRENT' in c], [])

    def test_a_bare_call_is_a_query(self):
        self.mmu._adjust_gear_current(gate=0, percent=50, reason="test")
        before = self.tmc.get_status()['run_current']

        self.hh.run_gcode('SET_TMC_CURRENT STEPPER=%s' % self.unit.gear_name(0))

        self.assertAlmostEqual(self.tmc.get_status()['run_current'], before, places=3)


class TestConsoleOutput(StepperCurrentTestCase):

    def test_both_the_intent_and_the_acknowledgment_are_reported(self):
        """
        HH's line says what it asked for; Klipper's says what the driver ended up at. Seeing
        only the first is how a change that never reached the driver used to look correct.
        """
        mark = len(self.hh.console)
        self.mmu._adjust_gear_current(gate=0, percent=50, reason="for testing")

        emitted = self.hh.console[mark:]
        self.assertTrue(any('run current to 50%' in line for line in emitted),
                        'HH did not report its intent: %s' % emitted)
        self.assertTrue(any(line.startswith('Run Current:') for line in emitted),
                        'driver did not acknowledge the change: %s' % emitted)


class TestPerExtruderAccounting(unittest.TestCase):
    """
    Two units on DIFFERENT physical extruders. Every other profile leaves both on the default one,
    so they share a wrapper and nothing distinguishes per-extruder state from machine-wide state.
    """

    def setUp(self):
        from test.hh.bootstrap import PRINTER_STUB
        from test.hh.profiles import EXTRA_EXTRUDER_STUB
        self.hh = session('ercf_vvd_dual_extruder',
                          printer_stub=PRINTER_STUB + EXTRA_EXTRUDER_STUB)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit0 = self.mmu.mmu_machine.get_mmu_unit_by_index(0)
        self.unit1 = self.mmu.mmu_machine.get_mmu_unit_by_index(1)
        self.assertIsNot(self.unit0.extruder_wrapper, self.unit1.extruder_wrapper,
                         'units share a wrapper - fixture is not doing its job')

    def tearDown(self):
        self.hh.close()

    def currents(self):
        return [line for line in self.hh.gcode.executed if line.startswith('SET_TMC_CURRENT')]

    def test_one_extruder_does_not_suppress_the_other(self):
        """
        The record used to be one machine-wide value, so setting extruder A then asking for the
        same percentage on B was refused as a no-op - and A was never restored either.
        """
        self.mmu.select_gate(0)
        self.mmu._adjust_extruder_current(60, "test")
        mark = len(self.currents())

        self.mmu.select_gate(9) # unit1, a different extruder
        self.mmu._adjust_extruder_current(60, "test")

        applied = self.currents()[mark:]
        self.assertEqual(len(applied), 1, 'second extruder was suppressed: %s' % applied)
        self.assertIn('STEPPER=%s' % self.unit1.extruder_name(), applied[0])
        self.assertEqual(self.unit0.extruder_wrapper.run_current_percent(), 60)
        self.assertEqual(self.unit1.extruder_wrapper.run_current_percent(), 60)

    def test_the_record_is_forgotten_on_reinit(self):
        self.mmu.select_gate(0)
        self.mmu._adjust_extruder_current(60, "test")
        self.assertEqual(self.mmu.extruder_run_current(), 60)

        self.mmu.reinit()
        self.assertEqual(self.mmu.extruder_run_current(), 100)


if __name__ == '__main__':
    unittest.main()
