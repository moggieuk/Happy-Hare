# Happy Hare test harness - tangle prevention and the gear-current bookkeeping it rides on.
#
# Tangle prevention boosts the gear stepper to 100% when the buffer reports high tension (the gear
# is struggling to pull from the spool) and restores it to sync_gear_current once tension eases.
#
# WHY THIS NEEDS ITS OWN SUITE
#
# The current change is DEFERRED to a reactor callback, deliberately: issuing SET_TMC_CURRENT from
# the timer that detects the tension would flush the toolhead lookahead and risk a move stall. That
# deferral is also where it went wrong - the callback used to capture no gate, and the gear stepper
# is resolved from the CURRENT selection at apply time, so a boost decided for one lane could
# program another and leave the first stuck at 100% for the rest of the print.
#
# THE PROFILE MATTERS. 'emu' is the only one that is simultaneously multigear (5 distinct gear
# steppers, so a wrong target is observable) and fitted with a proportional buffer sensor (so the
# feature can arm at all). It also has sync_gear_current=52; on a machine where that equals the
# driver default, every SET_TMC_CURRENT is suppressed as a no-op and these tests prove nothing.
#
# Tension is driven by stubbing _get_sensor_state and the check is called directly rather than
# waiting on the 0.5s extruder-movement timer. The timer is what PRODUCES the race; it is not what
# needs asserting, and driving it would make these tests nondeterministic.
#
#   ./venv/bin/python -m unittest test.test_mmu_tangle_prevention
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class TanglePreventionTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit()
        self.sf = self.unit.sync_feedback
        self.sync_percent = self.unit.p.sync_gear_current
        self.assertNotEqual(self.sync_percent, 100,
                            'profile sync_gear_current == 100 makes every current change a no-op')

        # Stand in for the buffer: tension is the negative half of the sensor range
        self._tension = 0.0
        self.sf._get_sensor_state = lambda: -self._tension

        # Arm as a synced, monitored print would
        self.sf.active = True
        self.sf._tangle_prevention_active = True

    def tearDown(self):
        self.hh.close()

    # -- helpers ------------------------------------------------------------
    def currents(self):
        return [line for line in self.hh.gcode.executed if line.startswith('SET_TMC_CURRENT')]

    def stepper(self, gate):
        return self.unit.gear_name(gate)

    def tick(self, tension, drain=True):
        """Run one tension sample through the check, then let the deferred change land."""
        self._tension = tension
        self.sf._check_tangle_prevention(self.hh.reactor.monotonic())
        if drain:
            self.hh.settle(0.)

    def synced_baseline(self, gate=0):
        """Put a stepper at sync_gear_current, as syncing to the extruder does."""
        self.mmu._adjust_gear_current(gate=gate, percent=self.sync_percent, reason="test baseline")


class TestBoostAndRelease(TanglePreventionTestCase):

    def test_boost_and_release_reach_the_selected_stepper(self):
        """
        Positive control. Without it, a race test that passes because the callback never drained
        looks identical to one that passes because the targeting was fixed.
        """
        self.synced_baseline(0)
        mark = len(self.currents())

        self.tick(0.9)
        boost = self.currents()[mark:]
        self.assertEqual(len(boost), 1, 'expected one current change, got %s' % boost)
        self.assertIn('STEPPER=%s' % self.stepper(0), boost[0])
        self.assertTrue(self.sf._tangle_prevention_boosted)
        self.assertEqual(self.mmu.gear_run_current(0), 100)

        mark = len(self.currents())
        self.tick(0.0)
        release = self.currents()[mark:]
        self.assertEqual(len(release), 1, 'expected one current change, got %s' % release)
        self.assertIn('STEPPER=%s' % self.stepper(0), release[0])
        self.assertFalse(self.sf._tangle_prevention_boosted)
        self.assertEqual(self.mmu.gear_run_current(0), self.sync_percent)

    def test_re_arming_after_a_disarm_can_boost_again(self):
        """
        Guards the split between hysteresis intent and applied state: if intent leaks across a
        disarm, the check takes the release branch forever and the feature is silently dead.
        """
        self.synced_baseline(0)
        self.tick(0.9)
        self.assertTrue(self.sf._tangle_prevention_boosted)

        self.sf.deactivate_tangle_prevention(self.hh.reactor.monotonic())
        self.sf.activate_tangle_prevention(self.hh.reactor.monotonic())
        self.assertTrue(self.sf._tangle_prevention_active, 're-arm did not take')

        mark = len(self.currents())
        self.tick(0.9)
        self.assertTrue(self.sf._tangle_prevention_boosted, 'second boost never happened')
        self.assertTrue(self.currents()[mark:])


class TestDeferredTargeting(TanglePreventionTestCase):

    def test_boost_lands_on_the_gate_it_was_decided_for(self):
        """
        The reported bug. The selection moves between deciding to boost and the deferred change
        landing, which is routine because a toolchange blocks on toolhead waits and reactor
        callbacks interleave there.
        """
        self.synced_baseline(0)
        mark = len(self.currents())

        self.tick(0.9, drain=False)      # decided for gate 0
        self.mmu.gate_selected = 3       # selection moves underneath
        self.hh.settle(0.)               # now the deferred change lands

        applied = self.currents()[mark:]
        self.assertTrue(applied, 'boost never applied at all')
        for line in applied:
            self.assertNotIn('STEPPER=%s' % self.stepper(3), line,
                             'boost programmed the stepper that happened to be selected')
            self.assertIn('STEPPER=%s' % self.stepper(0), line)
        self.assertEqual(self.sf._tangle_prevention_gate, 0)

    def test_release_returns_the_boosted_stepper_not_the_selected_one(self):
        self.synced_baseline(0)
        self.tick(0.9)
        self.assertEqual(self.mmu.gear_run_current(0), 100)

        self.mmu.gate_selected = 3       # moved on while still boosted
        mark = len(self.currents())
        self.tick(0.0)

        released = self.currents()[mark:]
        self.assertTrue(released, 'release never applied')
        for line in released:
            self.assertIn('STEPPER=%s' % self.stepper(0), line)
        self.assertEqual(self.mmu.gear_run_current(0), self.sync_percent,
                         'boosted stepper left high')

    def test_a_refused_change_is_not_recorded_as_applied(self):
        """
        A blocking operation owns the gear current while it runs. A boost that lands then is
        dropped, and claiming it was applied would mean the matching release never fires.
        """
        self.synced_baseline(0)
        self.mmu._gear_run_current_depth = 1   # stand in for an operation holding the current
        mark = len(self.currents())

        self.tick(0.9)
        self.assertEqual(self.currents()[mark:], [])
        self.assertFalse(self.sf._tangle_prevention_boosted, 'claimed a boost that was refused')

        self.mmu._gear_run_current_depth = 0
        self.tick(0.9)
        self.assertTrue(self.sf._tangle_prevention_boosted, 'boost never retried once free')


class TestPerStepperAccounting(TanglePreventionTestCase):

    def test_a_change_to_one_stepper_does_not_suppress_another(self):
        """
        Current was tracked as one value for the whole machine, so a stepper already sitting at the
        wanted percentage suppressed the change to a different one - which is what left a boosted
        lane stuck high after the selection moved.
        """
        mark = len(self.currents())
        self.mmu._adjust_gear_current(gate=0, percent=60, reason="test")
        self.mmu._adjust_gear_current(gate=1, percent=60, reason="test")

        applied = self.currents()[mark:]
        self.assertEqual(len(applied), 2, 'second stepper was suppressed: %s' % applied)
        self.assertIn('STEPPER=%s' % self.stepper(0), applied[0])
        self.assertIn('STEPPER=%s' % self.stepper(1), applied[1])
        self.assertEqual(self.mmu.gear_run_current(0), 60)
        self.assertEqual(self.mmu.gear_run_current(1), 60)

    def test_reinit_forgets_stale_current_records(self):
        self.mmu._adjust_gear_current(gate=0, percent=60, reason="test")
        self.assertEqual(self.mmu.gear_run_current(0), 60)

        self.mmu.reinit()
        self.assertEqual(self.mmu.gear_run_current(0), 100)


class TestWrapNesting(TanglePreventionTestCase):

    def test_inner_wrap_exit_does_not_unlock_the_outer_block(self):
        """
        The lock says "an operation owns the gear current". Releasing it when a nested block exits
        leaves the rest of the outer operation exposed to changes it meant to shut out.
        """
        self.synced_baseline(0)
        with self.mmu.wrap_gear_current(percent=80, reason="outer"):
            with self.mmu.wrap_gear_current(percent=40, reason="inner"):
                pass

            mark = len(self.currents())
            self.mmu._adjust_gear_current(gate=0, percent=99, reason="should be locked out")
            self.assertEqual(self.currents()[mark:], [],
                             'outer block was left unlocked by the inner exit')

        self.assertEqual(self.mmu.gear_run_current(0), self.sync_percent)

    def test_a_wrap_restores_the_stepper_it_changed(self):
        self.synced_baseline(0)
        with self.mmu.wrap_gear_current(percent=80, reason="outer"):
            self.mmu.gate_selected = 3   # selection moves inside the block

        self.assertEqual(self.mmu.gear_run_current(0), self.sync_percent,
                         'wrap restored a different stepper than it changed')


class TestDisableWhileBoosted(TanglePreventionTestCase):

    def test_disabling_the_feature_while_boosted_still_restores(self):
        """
        The unsync handler bails early when sync feedback is switched off, which used to skip the
        restore and leave the stepper at 100% for the rest of the print.
        """
        self.synced_baseline(0)
        self.tick(0.9)
        self.assertEqual(self.mmu.gear_run_current(0), 100)

        self.unit.p.sync_feedback_enabled = 0
        mark = len(self.currents())
        self.sf._handle_mmu_unsynced()

        self.assertTrue(self.currents()[mark:], 'nothing restored the boosted stepper')
        self.assertFalse(self.sf._tangle_prevention_boosted)
        self.assertEqual(self.mmu.gear_run_current(0), self.sync_percent)


if __name__ == '__main__':
    unittest.main()
