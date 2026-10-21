# Happy Hare test harness - the post-toolchange / post-resume "loaded" sanity check.
#
# While printing, _continue_after() asks the sensor manager to confirm that every sensor
# between the gate and the nozzle sees filament before handing back to the print. A
# toolchange that fails part way (here: an empty gate, after the old tool has already been
# unloaded) pauses the print with nothing loaded; RESUME must not carry on printing air.
#
# The check only applies where filament is expected to be loaded: after a load, toolchange
# or EndlessSpool reload, or on resume when HH believes it is loaded or a toolchange/load
# was interrupted. An unload during a print (MMU_UNLOAD, MMU_CHECK_GATE in PRINT_START) and
# a resume from a pause taken before the first tool was loaded are left alone.
#
#   ./venv/bin/python -m unittest test.test_mmu_confirm_loaded
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0


class ConfirmLoadedTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'
    PRELOAD_GATES = (0, 1)

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        for gate in self.PRELOAD_GATES:
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.hh.heat_extruder(220)

    def tearDown(self):
        self.hh.close()

    def gcode(self, script, settle=0.5):
        self.hh.run_gcode(script)
        self.hh.settle(settle)

    def start_print(self, tool=None):
        if tool is not None:
            self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.gcode('MMU_PRINT_START', settle=1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')
        self.assertTrue(self.mmu.is_printing())

    def clear_errors(self):
        del self.hh.gcode.errors[:]

    def pause(self):
        self.gcode('MMU_PAUSE')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.clear_errors()

    def loaded_check_failures(self):
        return [e for e in self.hh.errors if 'Loaded check failed' in e]

    def mmu_issues(self):
        return [e for e in self.hh.errors if 'MMU issue' in e]


class TestFailedToolchange(ConfirmLoadedTestCase):

    def fail_toolchange_to_empty_gate(self):
        self.start_print(0)
        self.gcode('MMU_CHANGE_TOOL TOOL=3')                # gate 3 never preloaded
        self.assertTrue(self.hh.errors, 'toolchange to an empty gate should fail')
        self.assertTrue(self.mmu.is_mmu_paused())
        self.assertEqual(self.mmu.tool_selected, 3)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.clear_errors()

    def test_resume_without_loading_is_refused(self):
        self.fail_toolchange_to_empty_gate()
        self.gcode('RESUME')
        self.assertTrue(self.loaded_check_failures(), self.hh.errors)
        self.assertTrue(self.mmu.is_mmu_paused(), 'print resumed with no filament loaded')

    def test_resume_after_loading_the_tool_continues(self):
        self.fail_toolchange_to_empty_gate()
        self.hh.place_filament(3, position=TIP_AT_GATE)
        self.gcode('MMU_PRELOAD GATE=3')
        self.gcode('MMU_CHANGE_TOOL TOOL=3')
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.gcode('RESUME')
        self.assertEqual(self.mmu_issues(), [])
        self.assertTrue(self.mmu.is_printing())


class TestSensorsDisagreeWithLoaded(ConfirmLoadedTestCase):

    def test_resume_with_filament_missing_from_a_sensor_is_refused(self):
        self.start_print(0)
        self.pause()
        self.hh.sensor('unit0:mmu_shared_exit').set(False)  # filament gone from the hub
        self.gcode('RESUME')
        self.assertEqual(len(self.loaded_check_failures()), 1, self.hh.errors)
        self.assertIn('mmu_shared_exit', self.loaded_check_failures()[0])
        self.assertTrue(self.mmu.is_mmu_paused())


class TestHealthyPrint(ConfirmLoadedTestCase):

    def test_toolchanges_in_a_print_do_not_pause(self):
        self.start_print(0)
        for tool in (1, 0, 1):
            self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
            self.assertEqual(self.hh.errors, [], 'toolchange to T%d paused' % tool)
            self.assertTrue(self.mmu.is_printing())

    def test_pause_and_resume_while_loaded_continues(self):
        self.start_print(0)
        self.pause()
        self.gcode('RESUME')
        self.assertEqual(self.mmu_issues(), [])
        self.assertTrue(self.mmu.is_printing())

    def test_unload_in_a_print_does_not_pause(self):
        self.start_print(0)
        self.gcode('MMU_UNLOAD')
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.mmu.is_printing())

    def test_check_gate_in_a_print_with_a_tool_loaded_does_not_pause(self):
        self.start_print(0)
        self.gcode('MMU_CHECK_GATE GATE=1', settle=2.0)
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.mmu.is_printing())


class TestResumeBeforeTheFirstTool(ConfirmLoadedTestCase):
    # A pause in PRINT_START (e.g. a failed gate check) legitimately resumes with nothing
    # loaded; the slicer's first toolchange does the load.

    def resume_unloaded(self):
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.pause()
        self.gcode('RESUME')
        self.assertEqual(self.mmu_issues(), [])
        self.assertTrue(self.mmu.is_printing())

    def test_resume_before_the_first_tool_is_loaded_continues(self):
        self.start_print()
        self.resume_unloaded()

    def test_a_failed_toolchange_before_the_print_is_forgotten(self):
        self.gcode('MMU_CHANGE_TOOL TOOL=0')
        self.gcode('MMU_CHANGE_TOOL TOOL=3')                # standalone, so no pause
        self.assertTrue(self.hh.errors)
        self.assertFalse(self.mmu.is_mmu_paused())
        self.clear_errors()
        self.start_print()
        self.resume_unloaded()


class TestEndlessSpoolInAPrint(ConfirmLoadedTestCase):
    PROFILE = 'boxturtle_test'
    PRELOAD_GATES = (0, 1, 2, 3)

    def test_runout_reload_in_a_print_does_not_pause(self):
        self.gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,1,1 ENABLE=1')
        self.start_print(0)
        checked = []
        confirm_loaded = self.mmu.sensor_manager.confirm_loaded
        def spy():
            checked.append(self.mmu.gate_selected)
            return confirm_loaded()
        self.mmu.sensor_manager.confirm_loaded = spy

        self.hh.filament().exhaust(0)               # the runout sensor drives the reload
        self.hh.settle(1.0)                         # announced through log_error, so filter
        self.assertEqual(self.loaded_check_failures(), [])
        self.assertEqual(self.mmu_issues(), [])
        self.assertNotEqual(self.mmu.gate_selected, 0)
        self.assertEqual(self.mmu.ttg_map[0], self.mmu.gate_selected)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(checked, [self.mmu.gate_selected], 'reload did not run the loaded check')


class TestHealthyPrintEncoder(TestHealthyPrint):
    PROFILE = 'encoder'


class TestHealthyPrintTradrack(TestHealthyPrint):
    PROFILE = 'tradrack'


class TestHealthyPrintMultiUnit(ConfirmLoadedTestCase):
    PROFILE = 'ercf_vvd'
    PRELOAD_GATES = (0, 1, 9, 10)

    def test_cross_unit_toolchanges_in_a_print_do_not_pause(self):
        self.start_print(0)
        for tool in (9, 1, 10, 0):
            self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
            self.assertEqual(self.hh.errors, [], 'toolchange to T%d paused' % tool)
            self.assertTrue(self.mmu.is_printing())

    def test_pause_and_resume_on_the_second_unit_continues(self):
        self.start_print(9)
        self.pause()
        self.gcode('RESUME')
        self.assertEqual(self.mmu_issues(), [])
        self.assertTrue(self.mmu.is_printing())


if __name__ == '__main__':
    unittest.main()
