# Happy Hare test harness - pressure advance around tip forming and purging.
#
# HH zeroes pressure advance while the tip forming and purge macros run and restores
# whatever value was live beforehand (_wrap_pressure_advance). If PA is already 0 when the
# wrap starts, something upstream (typically slicer wipe-tower gcode) zeroed it without
# restoring, and HH notes that at DEBUG level so it shows in mmu.log.
#
# The [extruder] stub gets a non-zero pressure_advance so a lost restore is visible.
#
#   ./venv/bin/python -m unittest test.test_mmu_pressure_advance
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest import mock

from test.hh import session
from test.hh.bootstrap import PRINTER_STUB

logging.getLogger().setLevel(logging.CRITICAL)

CONFIG_PA = 0.045
TIP_AT_GATE = -40.0
ALREADY_ZERO = "Pressure advance was already 0"


def _stub(pa):
    if pa is None:
        return PRINTER_STUB
    return PRINTER_STUB.replace("[extruder]\n", "[extruder]\npressure_advance: %s\n" % pa, 1)


class PressureAdvanceTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'
    PA = CONFIG_PA

    def setUp(self):
        self.hh = session(self.PROFILE, printer_stub=_stub(self.PA))
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        for gate in (0, 1):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.errors, [], 'initial load was not clean')

    def tearDown(self):
        self.hh.close()

    def pa(self):
        return self.hh.printer.lookup_object('extruder').get_status(0)['pressure_advance']

    def record_pa_in(self, macro_name, raise_error=False):
        """Wrap the harness effect for macro_name so it records PA while the macro runs."""
        seen = []
        effects = self.hh.printer.harness_macro_effects
        original = effects.get(macro_name)

        def effect(macro, gcmd):
            seen.append(self.pa())
            if raise_error:
                raise gcmd.error("simulated macro failure")
            if original is not None:
                original(macro, gcmd)

        effects[macro_name] = effect
        return seen


class TestPurge(PressureAdvanceTestCase):

    def purge_macro(self):
        macro = self.hh.mmu.p.purge_macro
        self.assertTrue(macro, 'profile has no purge_macro')
        return macro

    def test_purge_runs_with_pa_zeroed_and_restores_it(self):
        seen = self.record_pa_in(self.purge_macro())
        self.hh.run_gcode('MMU_TEST_PURGE')
        self.assertEqual(seen, [0.0])
        self.assertEqual(self.pa(), CONFIG_PA)
        self.assertEqual(self.hh.errors, [])

    def test_purge_restores_the_live_value_not_the_config_one(self):
        self.hh.run_gcode('SET_PRESSURE_ADVANCE ADVANCE=0.06')
        self.record_pa_in(self.purge_macro())
        self.hh.run_gcode('MMU_TEST_PURGE')
        self.assertEqual(self.pa(), 0.06)

    def test_purge_failure_still_restores_pa(self):
        seen = self.record_pa_in(self.purge_macro(), raise_error=True)
        self.hh.run_gcode('MMU_TEST_PURGE')
        self.assertEqual(seen, [0.0])
        self.assertNotEqual(self.hh.errors, [], 'the macro failure should be reported')
        self.assertEqual(self.pa(), CONFIG_PA)

    def test_no_already_zero_note_when_pa_is_set(self):
        with mock.patch.object(self.hh.mmu, 'log_debug') as debug:
            self.hh.run_gcode('MMU_TEST_PURGE')
        self.assertFalse([c for c in debug.call_args_list if ALREADY_ZERO in c.args[0]])


class TestTipForming(PressureAdvanceTestCase):

    def test_tip_forming_runs_with_pa_zeroed_and_restores_it(self):
        macro = self.hh.mmu._macro_name(self.hh.mmu.p.form_tip_macro)
        seen = self.record_pa_in(macro)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(seen, [0.0])
        self.assertEqual(self.pa(), CONFIG_PA)


class TestAlreadyZero(PressureAdvanceTestCase):
    # As if slicer gcode zeroed PA for the wipe tower and never restored it
    PA = None

    def test_purge_notes_pa_already_zero_at_debug_level(self):
        with mock.patch.object(self.hh.mmu, 'log_debug') as debug, \
                mock.patch.object(self.hh.mmu, 'log_info') as info, \
                mock.patch.object(self.hh.mmu, 'log_warning') as warning:
            self.hh.run_gcode('MMU_TEST_PURGE')
        notes = [c.args[0] for c in debug.call_args_list if ALREADY_ZERO in c.args[0]]
        self.assertEqual(len(notes), 1)
        self.assertIn('for purging', notes[0])
        for console in (info, warning):
            self.assertFalse([c for c in console.call_args_list if ALREADY_ZERO in c.args[0]])
        self.assertEqual(self.pa(), 0.0)

    def test_tip_forming_notes_pa_already_zero(self):
        with mock.patch.object(self.hh.mmu, 'log_debug') as debug:
            self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        notes = [c.args[0] for c in debug.call_args_list if ALREADY_ZERO in c.args[0]]
        self.assertTrue(any('for tip forming' in n for n in notes), notes)


if __name__ == '__main__':
    unittest.main()
