# Happy Hare test harness - MMU_CHECK_POSITIONS.
#
# The parking and cut tip macros each report the X,Y points they move to (via
# _MMU_CHECK_POSITION) and Happy Hare checks them against the axis limits after gcode
# offset and skew correction, from Z=0 up to the axis maximum. Park positions are
# adjusted at runtime by _SAFE_G1 so are only reported; the cut stroke can't be adjusted
# so pauses the print before the toolchange that would fail. Blobifier moves all go
# through _SAFE_G1 so aren't checked.
#
#   ./venv/bin/python -m unittest test.test_mmu_check_positions
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import sys
import unittest

from test.hh import profiles, session
from test.hh.bootstrap import PRINTER_STUB

logging.getLogger().setLevel(logging.CRITICAL)

Y_MAX = 356.
Z_MAX = 340.
YZ = (Y_MAX - 357.271) / 68.092
STUB = PRINTER_STUB + "\n[skew_correction]\n"
CHECK_MACROS = ('_MMU_PARK_CHECK_POSITIONS', '_MMU_CUT_TIP_CHECK_POSITIONS')


def _run_body(macro, gcmd):
    macro.run_body(gcmd)


def _mark_initialized(macro, gcmd):
    blobifier = macro.printer.lookup_object('gcode_macro BLOBIFIER')
    blobifier.variables = dict(blobifier.variables, initialized=1)


class CheckPositionsTestCase(unittest.TestCase):

    PROFILE = profiles.get('boxturtle').derive('boxturtle_cutter_blobifier', syms={
        'MMU_HAS_BLOBIFIER': True,
        'PIN_BLOBIFIER_SERVO': 'unit0:PA1',
        'MMU_HAS_TOOLHEAD_CUTTER': True,
    })

    def setUp(self):
        self.hh = session(self.PROFILE, printer_stub=STUB)
        self.hh.build()
        effects = {m: _run_body for m in CHECK_MACROS}
        effects['BLOBIFIER_INIT'] = _mark_initialized
        self.hh.printer.harness_macro_effects = effects
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.hh.printer.lookup_object('toolhead').get_kinematics().set_limits((0., 0., 0.), (350., Y_MAX, Z_MAX))
        self.skew = self.hh.printer.lookup_object('skew_correction')
        self.blobifier = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS', None)

    def tearDown(self):
        self.hh.close()

    def set_vars(self, macro, **values):
        obj = self.hh.printer.lookup_object('gcode_macro %s' % macro)
        obj.variables = dict(obj.variables, **values)

    def check(self):
        del self.hh.gcode.console[:]
        self.hh.run_gcode('MMU_CHECK_POSITIONS')
        return "\n".join(self.hh.gcode.console)

    def start_print(self):
        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.settle(1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')

    def pin_near_y_max(self):
        # With YZ skew pushing toward Y max this cut stroke is out of range above Z~54
        self.set_vars('_MMU_CUT_TIP_VARS', pin_loc_xy=[14., Y_MAX - 1.], pin_loc_compressed_xy=[0.5, Y_MAX - 1.])

    def test_profile_uses_cut_tip_and_blobifier(self):
        self.assertEqual(self.hh.mmu.p.form_tip_macro, '_MMU_CUT_TIP')
        self.assertEqual(self.hh.mmu.p.purge_macro, 'BLOBIFIER')
        self.assertIsNotNone(self.blobifier)

    def test_shipped_defaults_in_range_with_skew(self):
        self.skew.set_factors(yz=YZ)
        out = self.check()
        self.assertIn("All configured positions are within axis limits", out)
        for macro in CHECK_MACROS:
            self.assertEqual(len(self.hh.printer.lookup_object('gcode_macro %s' % macro).calls), 1, macro)

    def test_park_position_reported_with_safe_value(self):
        self.set_vars('_MMU_SEQUENCE_VARS', park_pause=[50., Y_MAX, 5., 0., 2.])
        self.skew.set_factors(yz=YZ)
        out = self.check()
        self.assertIn("park_pause (x:50.0, y:356.0): Y exceeds maximum by %.2fmm above Z=0.0" % (-Z_MAX * YZ), out)
        self.assertIn("use y <= %.2f" % (Y_MAX + Z_MAX * YZ - 0.01), out)

    def test_x_only_park_position(self):
        self.set_vars('_MMU_SEQUENCE_VARS', park_pause=[-5., -999., 5., 0., 2.])
        out = self.check()
        self.assertIn("park_pause (x:-5.0, y:n/a): X below minimum by 5.00mm, use x >= 0.01", out)

    def test_gcode_offset_reported(self):
        self.set_vars('_MMU_SEQUENCE_VARS', park_pause=[50., 350., 5., 0., 2.])
        self.hh.run_gcode('SET_GCODE_OFFSET Y=10')
        out = self.check()
        self.assertIn("park_pause (x:50.0, y:350.0): Y exceeds maximum by 4.00mm, use y <= 345.99", out)

    def test_cut_stroke_with_skew_is_fatal(self):
        self.pin_near_y_max()
        self.skew.set_factors(yz=YZ)
        out = self.check()
        self.assertIn("cut_tip_pin_park (x:19.0, y:355.0): Y exceeds maximum by", out)
        self.assertIn("above Z=53.6", out)
        self.assertIn("will pause print", out)

    def test_cut_stroke_failure_pauses_print_before_toolchange(self):
        self.pin_near_y_max()
        self.start_print()
        self.skew.set_factors(yz=YZ)      # Typically loaded in PRINT_START after MMU_PRINT_START
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.hh.settle(0.5)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertTrue(any("would cause a 'Move out of range' error" in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.hh.mmu.tool_selected, 0, 'toolchange should not have started')

    def park_checks(self):
        return len(self.hh.printer.lookup_object('gcode_macro _MMU_PARK_CHECK_POSITIONS').calls)

    def change_tool(self, tool):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.hh.settle(0.5)

    def test_resume_requires_the_interrupted_tool_change(self):
        self.pin_near_y_max()
        self.start_print()
        self.skew.set_factors(yz=YZ)
        self.change_tool(1)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertEqual(self.hh.mmu.tool_selected, 0)
        self.assertTrue(any("Run T1 to complete it" in e for e in self.hh.errors), self.hh.errors)

        # Resuming would print the rest of the T1 section with T0 loaded
        del self.hh.gcode.errors[:]
        self.hh.run_gcode('RESUME')
        self.hh.settle(0.5)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertTrue(any("Cannot resume because the change to T1" in e for e in self.hh.errors), self.hh.errors)

        # Completing the change (config left as is) then resuming continues without re-checking
        del self.hh.gcode.errors[:]
        self.change_tool(1)
        self.assertEqual(self.hh.mmu.tool_selected, 1)
        self.hh.run_gcode('RESUME')
        self.hh.settle(0.5)
        self.assertEqual(self.hh.errors, [])
        self.assertFalse(self.hh.mmu.is_mmu_paused())
        self.change_tool(0)
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.tool_selected, 0)

    def test_resume_requires_the_interrupted_tool_to_be_loaded(self):
        # A failed retry of the tool change can select T1 without loading it
        self.pin_near_y_max()
        self.start_print()
        self.skew.set_factors(yz=YZ)
        self.change_tool(1)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.hh.mmu.select_tool(1)
        unloaded = sys.modules[type(self.hh.mmu).__module__].FILAMENT_POS_UNLOADED
        self.hh.mmu.set_filament_pos_state(unloaded, silent=True)
        del self.hh.gcode.errors[:]
        self.hh.run_gcode('RESUME')
        self.hh.settle(0.5)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertTrue(any("T1 is not loaded" in e for e in self.hh.errors), self.hh.errors)

    def test_runout_does_not_pause_but_next_toolchange_does(self):
        self.pin_near_y_max()
        self.start_print()
        self.skew.set_factors(yz=YZ)
        self.hh.mmu.position_checker.before_toolhead_operation('runout')     # Check runs here...
        self.assertFalse(self.hh.mmu.is_mmu_paused())
        self.change_tool(1)                                                 # ...and pauses here
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertEqual(self.hh.mmu.tool_selected, 0)

    def test_checked_once_per_print_then_only_on_change(self):
        self.start_print()
        start = self.park_checks()
        self.change_tool(1)
        self.assertEqual(self.park_checks(), start + 1)
        self.change_tool(0)
        self.hh.run_gcode('SET_GCODE_OFFSET Z=-0.1')     # Babystep: ignored
        self.change_tool(1)
        self.assertEqual(self.park_checks(), start + 1)
        self.skew.set_factors(xy=-0.0001)
        self.change_tool(0)
        self.assertEqual(self.park_checks(), start + 2)
        self.hh.run_gcode('SET_GCODE_OFFSET X=1')
        self.change_tool(1)
        self.assertEqual(self.park_checks(), start + 3)
        self.assertEqual(self.hh.errors, [])

    def test_skew_loaded_mid_print_pauses_at_next_toolchange(self):
        self.pin_near_y_max()
        self.start_print()
        self.change_tool(1)
        self.assertFalse(self.hh.mmu.is_mmu_paused())
        self.skew.set_factors(yz=YZ)
        self.change_tool(0)
        self.assertTrue(self.hh.mmu.is_mmu_paused())
        self.assertEqual(self.hh.mmu.tool_selected, 1)

    def test_next_print_checks_again_with_same_skew(self):
        self.skew.set_factors(yz=YZ)
        self.start_print()
        self.change_tool(1)
        first = self.park_checks()
        self.hh.run_gcode('MMU_PRINT_END')
        self.hh.settle(0.5)
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.settle(1.0)
        self.assertEqual(self.park_checks(), first, 'print start itself does not check')
        self.change_tool(0)
        self.assertEqual(self.park_checks(), first + 1)
        self.assertEqual(self.hh.errors, [])

    def test_no_pause_outside_print(self):
        self.skew.set_factors(yz=YZ)
        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.errors, [])
        self.assertFalse(self.hh.mmu.is_mmu_paused())

    def test_park_failure_does_not_pause(self):
        self.set_vars('_MMU_SEQUENCE_VARS', park_toolchange=[50., Y_MAX, 1., 0., 2.])
        self.start_print()
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.hh.settle(0.5)
        self.assertEqual(self.hh.errors, [])
        self.assertFalse(self.hh.mmu.is_mmu_paused())
        self.assertEqual(self.hh.mmu.tool_selected, 1)
        self.assertIn("park_toolchange", "\n".join(self.hh.gcode.console))


class CheckPositionsBootupTestCase(unittest.TestCase):

    def test_bootup_reports_out_of_range_positions(self):
        hh = session(CheckPositionsTestCase.PROFILE, printer_stub=STUB)
        try:
            hh.build()
            hh.printer.harness_macro_effects = {m: _run_body for m in CHECK_MACROS}
            hh.printer.harness_macro_effects['BLOBIFIER_INIT'] = _mark_initialized
            seq = hh.printer.lookup_object('gcode_macro _MMU_SEQUENCE_VARS')
            seq.variables = dict(seq.variables, park_pause=[50., Y_MAX + 1, 5., 0., 2.])
            hh.boot(pre_bootup=lambda: hh.printer.lookup_object('toolhead').get_kinematics().set_limits(
                (0., 0., 0.), (350., Y_MAX, Z_MAX)))
            self.assertEqual(hh.errors, [])
            self.assertIn("park_pause (x:50.0, y:357.0): Y exceeds maximum by 1.00mm", "\n".join(hh.console))
        finally:
            hh.close()


class CheckPositionsNoLimitsTestCase(unittest.TestCase):

    def test_kinematics_without_box_limits(self):
        hh = session('boxturtle')
        try:
            hh.boot()
            del hh.gcode.console[:]
            hh.run_gcode('MMU_CHECK_POSITIONS')
            self.assertIn("only possible on kinematics with rectangular axis limits", "\n".join(hh.gcode.console))
            self.assertEqual(hh.errors, [])
        finally:
            hh.close()


if __name__ == '__main__':
    unittest.main()
