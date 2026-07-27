# Happy Hare test harness - load, unload and tool change.
#
# The core product function, and until now completely untested. A tool change is the whole
# machine working together: unload the current tool through the extruder and bowden back
# to its gate, select another gate, load that one forward to the nozzle.
#
# WHAT MAKES A FULL LOAD POSSIBLE HERE
#
#  - filament_compression is modelled at the extruder entry. BoxTurtle's
#    extruder_homing_endstop is filament_compression: the MMU pushes filament until it
#    meets the stationary extruder gears and the buffer compresses. Without that sensor in
#    the layout a load dies with "Failed to reach extruder 'filament_compression' endstop".
#  - the shipped config/macros/*.cfg are loaded verbatim. An unload refuses to run without
#    _MMU_FORM_TIP ("Filament tip forming macro not found"). They are COPIED not rendered
#    by the installer (Makefile:148), so the harness reads them raw.
#  - the extruder is pre-heated. Otherwise HH auto-heats and reports it through log_error
#    (mmu_controller.py:2456), which lands in the error sentinel.
#
# GEOMETRY. park -100, entry -50, gate 0, extruder entry / compression +700, nozzle +740.
# A loaded filament sits at +768 - past the nozzle by toolhead_extruder_to_nozzle.
#
#   ./venv/bin/python -m unittest test.test_mmu_toolchange
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.filament import TIP_PARKED

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
GATE_EMPTY = 0
GATE_AVAILABLE = 1
TIP_AT_GATE = -40.0         # past the entry switch: where a user's push leaves it
TOOL_UNKNOWN = -2


class ToolchangeTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'
    PRELOAD_GATES = (0,)

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        for gate in self.PRELOAD_GATES:
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.hh.heat_extruder(220)

    def tearDown(self):
        self.hh.close()

    def nozzle(self):
        """Where a fully loaded filament tip sits."""
        return self.fil.layout['toolhead'] + 28.0


class TestLoad(ToolchangeTestCase):

    def test_load_reaches_the_nozzle(self):
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertGreater(self.fil.tip[0], self.fil.layout['extruder_entry'],
                           'filament should be past the extruder')
        self.assertEqual(self.hh.errors, [])

    def test_load_homes_to_the_extruder_via_compression(self):
        """
        The sequence, not just the endpoint: gate homing, then a bowden move that ends by
        homing onto the compression sensor at the extruder.
        """
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        reasons = [r for _g, _d, r in self.fil.history]
        self.assertTrue(any('mmu_exit_0' in r for r in reasons),
                        'never homed to the gate')
        self.assertTrue(any('filament_compression' in r for r in reasons),
                        'never homed to the extruder compression sensor')

    def test_bowden_distance_is_travelled(self):
        """Gate (0) to extruder (+700) really is covered, not skipped."""
        self.hh.mmu.select_gate(0)
        before = self.fil.tip[0]
        self.hh.run_gcode('MMU_LOAD')
        self.assertGreater(self.fil.tip[0] - before,
                           self.fil.layout['extruder_entry'] - before - 1.0)

    def test_load_is_tool_driven_not_gate_driven(self):
        """
        MMU_LOAD loads the CURRENT TOOL's gate, not whichever gate happens to be
        selected: selecting gate 3 and then loading re-selects gate 0, because tool 0
        maps there through the TTG map. Worth pinning - the opposite is the natural
        assumption, and it silently loads a different gate than you asked for.

        To refuse an empty gate, address it by TOOL - see
        TestToolChange.test_changing_to_an_empty_gate_is_refused.
        """
        self.hh.mmu.select_gate(3)
        self.assertEqual(self.hh.mmu.gate_selected, 3)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.mmu.gate_selected, 0,
                         'MMU_LOAD should follow the tool, not the selected gate')
        self.assertEqual(self.hh.errors, [])

    def test_bowden_length_is_auto_calibrated_on_first_load(self):
        """
        With no calibrated bowden length, the first load measures it from the gate
        sensor. Pinned because it means the first load takes a different path from
        every later one.
        """
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertIn('calibrat', ' '.join(self.hh.console).lower())

    def test_gate_stays_available_after_loading(self):
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)


class TestUnload(ToolchangeTestCase):

    def setUp(self):
        super().setUp()
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_unload_returns_filament_to_the_park_position(self):
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=1)
        self.assertEqual(self.hh.errors, [])

    def test_unload_clears_the_path_sensors(self):
        self.hh.run_gcode('MMU_UNLOAD')
        for name in ('mmu_exit_0', 'mmu_entry_0'):
            with self.subTest(sensor=name):
                self.assertFalse(self.hh.sensor(name).present)

    def test_load_unload_is_a_round_trip(self):
        """The whole point: a cycle must leave the machine where it started."""
        start = TIP_PARKED
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)
        self.assertEqual(self.hh.errors, [])

    def test_tip_forming_macro_is_invoked(self):
        """
        An unload forms a tip first. HH refuses outright if _MMU_FORM_TIP is missing, so
        assert the macro is actually present - it is only there because the harness loads
        the shipped macro files.
        """
        self.assertIn('gcode_macro _MMU_FORM_TIP', self.hh.printer.objects)


class TestToolChange(ToolchangeTestCase):
    PRELOAD_GATES = (0, 1, 2)

    def test_change_to_a_tool_loads_that_gate(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.mmu.tool_selected, 0)
        self.assertEqual(self.hh.mmu.gate_selected, 0)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(self.hh.errors, [])

    def test_swapping_tools_unloads_the_old_and_loads_the_new(self):
        """
        The complete cycle. Gate 0's filament must come all the way back to its park
        position while gate 2's goes to the nozzle - that is what a tool change IS.
        """
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertGreater(self.fil.tip[0], self.fil.layout['extruder_entry'])

        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=2')
        self.assertEqual(self.hh.mmu.tool_selected, 2)
        self.assertEqual(self.hh.mmu.gate_selected, 2)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertAlmostEqual(self.fil.tip[0], TIP_PARKED, places=1)
        self.assertGreater(self.fil.tip[2], self.fil.layout['extruder_entry'])
        self.assertEqual(self.hh.errors, [])

    def test_only_one_gate_is_loaded_at_a_time(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        loaded = [g for g in range(self.hh.mmu.num_gates)
                  if self.fil.tip[g] > self.fil.layout['extruder_entry']]
        self.assertEqual(loaded, [1])

    def test_changing_to_the_same_tool_is_a_no_op(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        moves = len(self.fil.history)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.hh.mmu.tool_selected, 1)
        self.assertEqual(len(self.fil.history), moves,
                         'reselecting the loaded tool should move no filament')
        self.assertEqual(self.hh.errors, [])

    def test_tx_macros_exist_but_their_bodies_do_not_run(self):
        """
        A HARNESS LIMITATION, pinned so nobody assumes T1 is covered.

        The shipped macros are loaded, so [gcode_macro T1] exists and HH's "macro not
        found" checks pass. But the fake gcode_macro records a call and does NOT render or
        execute the macro BODY - doing so would mean evaluating ~2000 lines of Jinja
        against live printer state. So T1 changes nothing.

        Consequence: anything driven purely by a macro body - Tx, the print start/end
        sequences, park/cut/purge - is NOT covered. Address the underlying command
        (MMU_CHANGE_TOOL) to test the real behaviour.
        """
        self.assertIn('gcode_macro T1', self.hh.printer.objects)
        before = self.hh.mmu.tool_selected
        self.hh.run_gcode('T1')
        self.assertEqual(self.hh.mmu.tool_selected, before,
                         'if this now changes, macro bodies execute and this test and '
                         'the README coverage map should be updated')

    def test_a_sequence_of_changes_stays_consistent(self):
        """
        Three changes in a row. Catches state that leaks between operations - the kind of
        thing that only shows up on the second or third toolchange of a print.
        """
        for tool in (0, 2, 1):
            with self.subTest(tool=tool):
                self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
                self.assertEqual(self.hh.mmu.tool_selected, tool)
                self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
                loaded = [g for g in range(self.hh.mmu.num_gates)
                          if self.fil.tip[g] > self.fil.layout['extruder_entry']]
                self.assertEqual(loaded, [tool])
        self.assertEqual(self.hh.errors, [])

    def test_changing_to_an_empty_gate_is_refused(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=3')     # gate 3 never preloaded
        self.assertTrue(self.hh.errors)
        self.assertNotEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)


class TestStateTracking(ToolchangeTestCase):
    PRELOAD_GATES = (0, 1)

    def test_filament_pos_progresses_through_the_state_machine(self):
        """
        filament_pos is HH's own belief about where the filament is. It must track the
        physical model, not drift from it.
        """
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_tool_and_gate_track_each_other(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.hh.mmu.tool_selected, 1)
        self.assertEqual(self.hh.mmu.gate_selected, 1)

    def test_status_reports_a_loaded_tool(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        status = self.hh.mmu.get_status(self.hh.reactor.monotonic())
        self.assertEqual(status['tool'], 0)
        self.assertEqual(status['gate'], 0)
        self.assertEqual(status['filament'], 'Loaded')


if __name__ == '__main__':
    unittest.main()
