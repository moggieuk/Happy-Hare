# Happy Hare test harness - repeated cutter strokes rip once, after the last stroke.
#
#   ./venv/bin/python -m unittest test.test_mmu_cut_tip
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import profiles, session


class CutTipIterationsTestCase(unittest.TestCase):

    def test_repeated_cuts_rip_once(self):
        hh = session(profiles.get('boxturtle').derive(
            'cut_tip_iterations', syms={'MMU_HAS_TOOLHEAD_CUTTER': True}))
        self.addCleanup(hh.close)
        hh.boot()
        hh.printer.harness_macro_effects['_CUT_TIP_DO_CUT_MOTION'] = (
            lambda macro, gcmd: macro.run_body(gcmd))
        vars = hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        vars.variables.update(gantry_servo_enabled=True, pin_loc_compressed_xy=[100., 100.],
                              cut_fast_move_fraction=0.5, cut_iterations=3, rip_length=2.)
        hh.run_gcode("G90\nG1 X120 Y100 Z10")
        first = len(hh.gcode.executed)
        hh.run_gcode("_CUT_TIP_DO_CUT_MOTION PIN_PARK_X_LOC=110 PIN_PARK_Y_LOC=100")
        commands = [line.split('#')[0].strip() for line in hh.gcode.executed[first:]]
        self.assertEqual(len([c for c in commands if c.startswith('G1 E-2.0 ')]), 1)
        self.assertEqual(commands.count('G4 P500'), 2)
        self.assertEqual(hh.errors, [])


if __name__ == '__main__':
    unittest.main()
