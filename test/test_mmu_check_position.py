# Happy Hare test harness - MMU_CHECK_POSITION, the on-demand check that an X,Y gcode
# position stays within the axis limits at every Z height once gcode offset and skew
# correction are applied.
#
#   ./venv/bin/python -m unittest test.test_mmu_check_position
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.bootstrap import PRINTER_STUB

logging.getLogger().setLevel(logging.CRITICAL)

X_MAX, Y_MAX, Z_MAX = 350., 356., 340.
YZ = (Y_MAX - 357.271) / 68.092     # Same skew as test_mmu_safe_g1 (from a reported error)
STUB = PRINTER_STUB + "\n[skew_correction]\n"


class CheckPositionTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle', printer_stub=STUB)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.toolhead.get_kinematics().set_limits((0., 0., 0.), (X_MAX, Y_MAX, Z_MAX))
        self.skew = self.hh.printer.lookup_object('skew_correction')
        self.skew.add_profile('my_skew', yz=YZ)
        self.hh.run_gcode("G90")

    def tearDown(self):
        self.hh.close()

    def check(self, params):
        start = len(self.hh.console)
        self.hh.run_gcode("MMU_CHECK_POSITION %s" % params)
        return "\n".join(self.hh.console[start:])

    def test_in_range(self):
        out = self.check("X=100 Y=300 SKEW=my_skew")
        self.assertIn("Checking X100.00 Y300.00 from Z=0.0 to Z=340.0:", out)
        self.assertIn("X: OK (machine 100.00..100.00, limits 0.00..350.00)", out)
        self.assertIn("Y: OK (machine 300.00..306.35, limits 0.00..356.00)", out)

    def test_over_maximum_above_a_height(self):
        out = self.check("X=19 Y=355 SKEW=my_skew")
        self.assertIn("Y: exceeds maximum 356.00 by 5.35 above Z=53.6. Use Y <= 349.64 for all heights", out)

    def test_below_minimum(self):
        self.skew.add_profile('xz', xz=0.002)
        out = self.check("X=0.5 Y=100 SKEW=xz")
        self.assertIn("X: below minimum 0.00 by 0.18 above Z=250.0. Use X >= 0.69 for all heights", out)

    def test_x_only_checks_both_ends_of_y(self):
        # Skewed X depends on Y, and Y is only out of range at the far end
        self.skew.add_profile('xy', xy=-0.005)
        out = self.check("X=349 SKEW=xy")
        self.assertNotIn("Y:", out)
        self.assertIn("X: exceeds maximum 350.00 by 0.78 at all heights", out)

    def test_y_only(self):
        out = self.check("Y=355 SKEW=my_skew")
        self.assertIn("Checking Y355.00 from", out)
        self.assertNotIn("X:", out)
        self.assertIn("Y: exceeds maximum", out)

    def test_requires_x_or_y(self):
        with self.assertRaises(self.hh.gcode.error):
            self.hh.run_gcode("MMU_CHECK_POSITION SKEW=my_skew")

    def test_supplied_profile_does_not_change_live_skew(self):
        self.skew.add_profile('other', yz=0.001)
        self.skew.set_factors(yz=0.001, profile='other')
        out = self.check("X=19 Y=355 SKEW=my_skew")
        self.assertIn("Skew: profile 'my_skew' (supplied) xy=0.000000 xz=0.000000 yz=-0.018666", out)
        self.assertEqual((self.skew.xy_factor, self.skew.xz_factor, self.skew.yz_factor), (0., 0., 0.001))
        self.assertEqual(self.skew.current_profile_name, 'other')

    def test_unknown_profile_lists_saved_profiles(self):
        self.skew.add_profile('another')
        with self.assertRaises(self.hh.gcode.error) as cm:
            self.hh.run_gcode("MMU_CHECK_POSITION Y=300 SKEW=nope")
        self.assertIn("Unknown skew profile 'nope'. Saved profiles: another, my_skew", str(cm.exception))

    def test_no_skew_loaded(self):
        out = self.check("X=19 Y=355")
        self.assertIn("Skew: no skew profile provided or loaded", out)
        self.assertIn("Y: OK", out)

    def test_loaded_profile(self):
        self.skew.set_factors(yz=YZ, profile='my_skew')
        out = self.check("X=19 Y=355")
        self.assertIn("Skew: profile 'my_skew' (currently loaded)", out)
        self.assertIn("Y: exceeds maximum", out)

    def test_manual_skew_is_not_reported_as_a_profile(self):
        self.skew.set_factors(xz=0.001, profile='')
        self.assertIn("Skew: current factors, not from a saved profile xy=0.000000 xz=0.001000", self.check("Y=300"))
        # Klipper keeps the last profile name after SET_SKEW
        self.skew.set_factors(yz=0.001, profile='my_skew')
        self.assertIn("Skew: current factors, not from a saved profile", self.check("Y=300"))

    def test_gcode_offset_applied_and_reported(self):
        self.hh.run_gcode("SET_GCODE_OFFSET X=5 Y=-2")
        out = self.check("X=346 Y=300")
        self.assertIn("Gcode offset: X5.00 Y-2.00", out)
        self.assertIn("X: exceeds maximum 350.00 by 1.00 at all heights. Use X <= 344.99 for all heights", out)
        self.assertIn("Y: OK (machine 298.00..298.00", out)

    def test_error_only_when_requested_and_out_of_range(self):
        self.check("X=19 Y=355 SKEW=my_skew")                 # Default reports only
        self.check("X=19 Y=300 SKEW=my_skew ERROR=1")         # In range doesn't raise
        with self.assertRaises(self.hh.gcode.error) as cm:
            self.hh.run_gcode("MMU_CHECK_POSITION X=19 Y=355 SKEW=my_skew ERROR=1")
        self.assertIn("position out of range: Y355.00 exceeds maximum 356.00 by 5.35 above Z=53.6", str(cm.exception))

    def test_fail_height_matches_klipper_range_check(self):
        self.skew.set_factors(yz=YZ, profile='my_skew')
        self.assertIn("above Z=53.6", self.check("X=19 Y=355"))
        self.hh.run_gcode("G1 X19 Y300 Z53.5")
        self.hh.run_gcode("G1 Y355")                          # Just below: in range
        self.hh.run_gcode("G1 Y300")
        self.hh.run_gcode("G1 Z53.7")
        with self.assertRaises(self.hh.printer.command_error):
            self.hh.run_gcode("G1 Y355")                      # Just above: out of range
        self.hh.run_gcode("G1 Y349.64 Z340")                  # Suggested value works at max Z


class CheckPositionWithoutSkewTestCase(unittest.TestCase):

    def test_skew_correction_not_configured(self):
        hh = session('boxturtle')
        try:
            hh.boot()
            hh.printer.lookup_object('toolhead').get_kinematics().set_limits((0., 0., 0.), (X_MAX, Y_MAX, Z_MAX))
            start = len(hh.console)
            hh.run_gcode("MMU_CHECK_POSITION X=10 Y=10")
            out = "\n".join(hh.console[start:])
            self.assertIn("Skew: skew_correction not configured", out)
            self.assertIn("X: OK", out)
            with self.assertRaises(hh.gcode.error):
                hh.run_gcode("MMU_CHECK_POSITION X=10 SKEW=my_skew")
        finally:
            hh.close()

    def test_kinematics_without_box_limits(self):
        hh = session('boxturtle', printer_stub=STUB)
        try:
            hh.boot()
            start = len(hh.console)
            hh.run_gcode("MMU_CHECK_POSITION X=10 Y=10")
            self.assertIn("only possible on kinematics with rectangular axis limits", "\n".join(hh.console[start:]))
        finally:
            hh.close()


if __name__ == '__main__':
    unittest.main()
