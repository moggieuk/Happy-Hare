# Happy Hare test harness - the two stepper-current nesting policies.
#
# wrap_gear_current and wrap_extruder_current look identical at the call site and behave
# differently on purpose:
#
#   gear      outermost wins. The depth counter also acts as a global lockout on ALL gear current
#             traffic, which is what stops move_filament - it re-asserts current on every move -
#             and the async sync-feedback timer from clobbering a wrapped block.
#   extruder  fully re-entrant. Nothing contends for it, so an inner wrap exits back to the
#             outer's percentage rather than being ignored.
#
# Nothing pinned that until now: the only test touching either wrapper asserts "did not throw".
# This file is a characterisation pin, written ahead of a refactor that moves the current record
# onto the per-stepper objects. Its job is to fail if that refactor quietly unifies the policies.
#
# A characterisation test passes on current code by construction, so its value rests entirely on
# having been mutation-checked: giving the gear wrapper the extruder's policy must take the gear
# count from 2 to 4.
#
# 'emu' because the wrappers only emit when a gate is selected (a negative gate short-circuits
# before the stepper is named) and because its sync_gear_current differs from the driver default.
#
# Known-uncovered adjacency, recorded here because this is where someone will look: the selector
# calibration command nests wrap_gear_current INSIDE wrap_sync_gear_to_extruder. Exits run
# innermost-first, so the gear wrap clears the depth before the sync re-apply runs. Reverse that
# nesting and the sync re-apply is silently swallowed by the lockout.
#
#   ./venv/bin/python -m unittest test.test_mmu_current_nesting
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class CurrentNestingTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit()
        # A negative gate short-circuits before a stepper is named, so a pin written without
        # this counts zero emissions against zero and proves nothing
        self.assertEqual(self.mmu.gate_selected, 0, 'no gate selected - the gear wrap cannot emit')

    def tearDown(self):
        self.hh.close()

    def currents(self):
        return [line for line in self.hh.gcode.executed if line.startswith('SET_TMC_CURRENT')]


class TestGearIsOutermostWins(CurrentNestingTestCase):

    def test_a_nested_gear_wrap_emits_only_the_outer_pair(self):
        default = self.unit.gear_default_current(0)
        tmc = self.unit.gear_tmc_obj(0)
        mark, changes = len(self.currents()), len(tmc.current_changes)

        with self.mmu.wrap_gear_current(percent=80, reason="outer"):
            with self.mmu.wrap_gear_current(percent=40, reason="inner"):
                pass

        emitted = self.currents()[mark:]
        self.assertEqual(len(emitted), 2, 'expected apply+restore only, got %s' % emitted)
        for line in emitted:
            self.assertIn('STEPPER=%s' % self.unit.gear_name(0), line)

        applied = [run for _t, run, _h in tmc.current_changes[changes:]]
        self.assertEqual(len(applied), 2)
        self.assertAlmostEqual(applied[0], default * 0.80, places=3)
        self.assertAlmostEqual(applied[1], default, places=3)
        self.assertEqual(self.mmu.gear_run_current(0), 100)


class TestExtruderNestsFully(CurrentNestingTestCase):

    def test_a_nested_extruder_wrap_emits_every_level(self):
        default = self.unit.extruder_default_current()
        tmc = self.unit.extruder_tmc_obj()
        mark, changes = len(self.currents()), len(tmc.current_changes)

        with self.mmu.wrap_extruder_current(percent=80, reason="outer"):
            with self.mmu.wrap_extruder_current(percent=40, reason="inner"):
                pass

        emitted = self.currents()[mark:]
        self.assertEqual(len(emitted), 4, 'expected both levels to apply and restore, got %s' % emitted)
        for line in emitted:
            self.assertIn('STEPPER=%s' % self.unit.extruder_name(), line)

        applied = [run for _t, run, _h in tmc.current_changes[changes:]]
        self.assertEqual([round(a / default, 2) for a in applied], [0.80, 0.40, 0.80, 1.00],
                         'inner wrap must restore to the outer percentage, not to the default')
        self.assertEqual(self.mmu.extruder_run_current(), 100)


if __name__ == '__main__':
    unittest.main()
