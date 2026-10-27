# Happy Hare test harness - repeated cutter strokes must not multiply retraction.
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import profiles, session


class TestCutTipIterations(unittest.TestCase):
    def setUp(self):
        self.hh = session(profiles.get('boxturtle').derive(
            'cut_tip_iterations', syms={'MMU_HAS_TOOLHEAD_CUTTER': True}))
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        for name in ('_MMU_CUT_TIP', '_CUT_TIP_MOVE_TO_CUTTER_PIN',
                     '_CUT_TIP_DO_CUT_MOTION'):
            self.hh.printer.harness_macro_effects[name] = (
                lambda macro, gcmd: macro.run_body(gcmd))
        self.vars = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        self.vars.variables.update(
            gantry_servo_enabled=True, cutting_axis='x', pin_park_dist=0.,
            pin_loc_xy=[110., 100.], pin_loc_compressed_xy=[100., 100.],
            cut_fast_move_fraction=0.5, cut_dwell_time=123,
            blade_pos=37.5, retract_length=20., pushback_length=10.,
            pushback_dwell_time=234, simple_tip_forming=False,
            rip_length=2., restore_position=False)
        self.hh.mmu.mmu_unit().extruder_wrapper.set_filament_remaining(0.)
        self.hh.mmu.mmu_unit().toolhead_wrapper.p.toolhead_residual_filament = 2.
        self.hh.printer.lookup_object('gcode_macro _MMU_PARK').variables['retracted_length'] = 0.
        self.hh.run_gcode('G90\nG1 X120 Y100 Z10\nM83')

    def run_cut(self, iterations, rip):
        self.vars.variables.update(cut_iterations=iterations, rip_length=rip)
        first_command = len(self.hh.gcode.executed)
        self.hh.run_gcode('_MMU_CUT_TIP')
        commands = [line.split('#')[0].strip()
                    for line in self.hh.gcode.executed[first_command:]]
        # The harness executes XYZ but records E commands without physical extrusion.
        e_moves = [float(line.split()[1][1:]) for line in commands
                   if line.startswith('G1 E')]
        return e_moves, commands

    def test_repeated_cuts_rip_once_after_last_stroke(self):
        for iterations in (1, 2, 3, 0):
            with self.subTest(iterations=iterations):
                e_moves, commands = self.run_cut(iterations, 2.)
                # Initial retract, one rip, then balanced pushback/retract.
                self.assertEqual(e_moves, [-18., -2., 10., -10.])
                strokes = [i for i, line in enumerate(commands)
                           if line.startswith('G1 X100.0 Y100.0 ')]
                self.assertEqual(len(strokes), max(1, iterations))
                rip = next(i for i, line in enumerate(commands)
                           if line.startswith('G1 E-2.0 '))
                self.assertGreater(rip, strokes[-1])
                # Rip while the final stroke is still depressed, before returning.
                self.assertTrue(commands[rip + 1].startswith('_SAFE_G1 X=110.0 '))
                macro = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP')
                self.assertEqual(macro.variables['output_park_pos'], 39.5)
                self.assertEqual(self.hh.errors, [])

    def test_zero_rip_and_pauses_only_between_cuts(self):
        for iterations in (1, 3):
            with self.subTest(iterations=iterations):
                e_moves, commands = self.run_cut(iterations, 0.)
                self.assertEqual(e_moves, [-18., 10., -10.])
                self.assertEqual(commands.count('G4 P500'), iterations - 1)


if __name__ == '__main__':
    unittest.main()
