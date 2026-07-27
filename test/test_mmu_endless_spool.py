# Happy Hare test harness - runout detection and EndlessSpool.
#
# When a spool runs out mid-print, EndlessSpool remaps the active TOOL to another gate in
# the same group and continues. High user impact when it misbehaves: the failure mode is a
# ruined print, and the code only runs at the moment you least want to debug it.
#
# WHAT MAKES A RUNOUT DISTINGUISHABLE FROM A CLOG
#
# A runout means the END of the filament has passed the gate - the gate sensor releases
# while filament is still gripped downstream. A clog means filament stopped moving but is
# still present. Happy Hare decides between them by looking at the gate sensor, so the
# model needs a filament TAIL: filament occupies [tail, tip], normally with tail at
# -infinity because a spool is attached. fil.exhaust(gate) gives it a finite tail.
#
# Without that, every simulated runout reads as "a clog/tangle has been detected and
# requires manual intervention" - which is Happy Hare being right about an impossible
# machine, not a bug.
#
# NOTE the runout announcement goes through log_error ("A runout has been detected.
# Checking for alternative gates...") even though it is informational, so these tests
# assert on outcomes rather than on an empty error list.
#
#   ./venv/bin/python -m unittest test.test_mmu_endless_spool
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.filament import TIP_PARKED

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_LOADED = 10
GATE_EMPTY = 0
GATE_AVAILABLE = 1
TIP_AT_GATE = -40.0


class EndlessSpoolTestCase(unittest.TestCase):
    GATES = (0, 1, 2, 3)
    GROUPS = '1,1,1,1'          # every gate substitutable for every other
    ENABLE = 1

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        for gate in self.GATES:
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=%s ENABLE=%d'
                          % (self.GROUPS, self.ENABLE))
        self.gate_maps = self.hh.mmu.gate_maps

    def tearDown(self):
        self.hh.close()

    def load_and_run_out(self, tool=0):
        """Load a tool, exhaust its spool, then trigger runout detection."""
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        gate = self.hh.mmu.gate_selected
        self.fil.exhaust(gate)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        return gate

    def loaded_gates(self):
        return [g for g in range(self.hh.mmu.num_gates)
                if self.fil.tip[g] > self.fil.layout['extruder_entry']]


class TestConfiguration(EndlessSpoolTestCase):

    def test_groups_and_enable_are_applied(self):
        self.assertTrue(self.gate_maps.endless_spool_enabled)
        self.assertEqual(list(self.gate_maps.endless_spool_groups), [1, 1, 1, 1])

    def test_state_lives_on_the_gate_map_not_the_machine_param(self):
        """
        MMU_ENDLESS_SPOOL writes gate_maps.endless_spool_enabled; the machine param
        endless_spool_enabled is only the configured default. Reading the wrong one makes
        a test look enabled when it is not.
        """
        self.assertTrue(self.gate_maps.endless_spool_enabled)
        self.assertEqual(self.hh.mmu.p.endless_spool_enabled, 0)

    def test_reset_restores_per_gate_groups(self):
        self.hh.run_gcode('MMU_ENDLESS_SPOOL RESET=1')
        groups = list(self.gate_maps.endless_spool_groups)
        self.assertEqual(len(set(groups)), len(groups),
                         'reset should put each gate in its own group')

    def test_wrong_length_group_list_is_rejected(self):
        before = list(self.gate_maps.endless_spool_groups)
        errors_before = len(self.hh.errors)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1')      # 4 gates, 2 groups
        self.assertGreater(len(self.hh.errors), errors_before)
        self.assertEqual(list(self.gate_maps.endless_spool_groups), before)


class TestRunoutSwapsGate(EndlessSpoolTestCase):

    def test_runout_remaps_the_tool_to_another_gate(self):
        """
        The headline behaviour. The TOOL stays T0 - the slicer keeps asking for T0 - but
        it now resolves to a different gate.
        """
        original = self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.tool_selected, 0, 'the tool must not change')
        self.assertNotEqual(self.hh.mmu.gate_selected, original)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_the_new_gate_is_actually_loaded(self):
        original = self.load_and_run_out(tool=0)
        new_gate = self.hh.mmu.gate_selected
        self.assertEqual(self.loaded_gates(), [new_gate])
        self.assertAlmostEqual(self.fil.tip[original], TIP_PARKED, places=1)

    def test_the_exhausted_gate_is_marked_empty(self):
        original = self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.gate_status[original], GATE_EMPTY)

    def test_the_replacement_gate_stays_available(self):
        self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.gate_status[self.hh.mmu.gate_selected],
                         GATE_AVAILABLE)

    def test_the_runout_is_announced(self):
        self.load_and_run_out(tool=0)
        announced = ' '.join(self.hh.errors + self.hh.console).lower()
        self.assertIn('runout', announced)
        self.assertIn('endlessspool', announced.replace(' ', ''))

    def test_successive_runouts_walk_through_the_group(self):
        """
        Three spools in a row. Each runout must find the next available gate rather than
        re-trying an exhausted one - the failure mode being an infinite loop.
        """
        used = []
        for _ in range(3):
            used.append(self.load_and_run_out(tool=0))
        self.assertEqual(len(set(used)), 3, 'each runout should consume a distinct gate')
        for gate in used:
            self.assertEqual(self.hh.mmu.gate_status[gate], GATE_EMPTY)


class TestRunoutWithoutAlternatives(EndlessSpoolTestCase):
    GROUPS = '1,2,3,4'          # every gate in its own group: no substitutes

    def test_runout_with_no_group_partner_needs_intervention(self):
        """
        Gate 0 is alone in group 1, so there is nowhere to go. HH must stop and say so
        rather than silently picking an unrelated gate.
        """
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'must not remap outside the group')
        # HH leaves the filament where it is rather than unloading: there is nothing
        # behind it to pull back, so the run-out remainder stays in the extruder for the
        # user to deal with.
        self.assertEqual(self.loaded_gates(), [0],
                         'no gate from another group should have been loaded')

    def test_gates_in_other_groups_are_untouched(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        for gate in (1, 2, 3):
            with self.subTest(gate=gate):
                self.assertAlmostEqual(self.fil.tip[gate], TIP_PARKED, places=1)


class TestEndlessSpoolDisabled(EndlessSpoolTestCase):
    ENABLE = 0

    def test_runout_without_endless_spool_needs_intervention(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'no remap should have happened')


class TestClogVersusRunout(EndlessSpoolTestCase):
    """
    The distinction HH has to make, and the reason the model needs a tail. Filament still
    present at the gate means a clog - a jam needing a human. Filament gone means a runout
    - swap and carry on. Getting this backwards either strands a print or feeds a jam.
    """

    def test_filament_still_present_is_treated_as_a_clog(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertTrue(self.hh.sensor('mmu_exit_0').present,
                        'precondition: filament still spans the gate sensor')
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        reported = ' '.join(self.hh.errors).lower()
        self.assertIn('clog', reported)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'a clog must not remap')

    def test_exhausting_the_spool_clears_the_gate_sensors(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        self.assertFalse(self.hh.sensor('mmu_entry_0').present)

    def test_filament_downstream_is_still_gripped_after_exhaust(self):
        """A runout is not "filament vanished" - the tip is still in the extruder."""
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.assertGreater(self.fil.tip[0], self.fil.layout['extruder_entry'])

    def test_refill_restores_an_attached_spool(self):
        self.fil.exhaust(0)
        self.hh.settle()
        self.fil.refill(0)
        self.hh.settle()
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)


if __name__ == '__main__':
    unittest.main()
