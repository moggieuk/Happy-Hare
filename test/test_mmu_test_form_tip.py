# Happy Hare test harness - MMU_TEST_FORM_TIP measures the tip macro as a toolchange would run it.
#
# The macro is run without FINAL_EJECT and Happy Hare ejects afterwards, so the extruder movement
# it measures, and the fragment it records, are the macro's alone.
#
# Real G1 moves update physical extruder steps. The harness's simplified _MMU_FORM_TIP
# effect also reports physical travel when its macro body is not executed.
#
#   ./venv/bin/python -m unittest test.test_mmu_test_form_tip
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

CUT_MACROS = ('_CUT_TIP_MOVE_TO_CUTTER_PIN', '_CUT_TIP_DO_CUT_MOTION',
              '_CUT_TIP_GANTRY_SERVO_DOWN', '_CUT_TIP_GANTRY_SERVO_UP')
EJECT = 'Ejecting filament for inspection'


class TestFormTipCase(unittest.TestCase):
    SYMS = {}

    def setUp(self):
        self.hh = session(profiles.get('3ms').derive('3ms_test_form_tip_%d' % len(self.SYMS), syms=self.SYMS))
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        effects = self.hh.printer.harness_macro_effects
        for name in CUT_MACROS:
            effects[name] = lambda macro, gcmd: macro.run_body(gcmd)

        self.hh.place_filament(0, position=-40.)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [], 'load was not clean')
        self.moves = []
        move_filament = self.mmu.move_filament

        def record(trace, dist, *args, **kwargs):
            self.moves.append((trace, round(dist, 2), kwargs.get('motor')))
            return move_filament(trace, dist, *args, **kwargs)

        self.mmu.move_filament = record

    def run_test_form_tip(self):
        start = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_TEST_FORM_TIP')
        self.assertEqual(self.hh.errors, [])
        executed = self.hh.gcode.executed[start:]
        self.assertFalse(any('FINAL_EJECT' in line for line in executed), executed)
        ejects = [m for m in self.moves if m[0] == EJECT]
        self.assertEqual(len(ejects), 1, self.moves)
        self.assertEqual(ejects[0][1], -80.)
        self.assertIn(ejects[0][2], ('extruder', 'gear+extruder'))
        return self.mmu.mmu_unit(0).extruder_wrapper


class TestTipForming(TestFormTipCase):

    def test_ejects_after_the_macro_and_records_no_fragment(self):
        extruder = self.run_test_form_tip()
        self.assertEqual(extruder.filament_remaining, 0.)


class TestCutterAfterExtruderGears(TestFormTipCase):
    SYMS = {'MMU_HAS_TOOLHEAD_CUTTER': True}

    def test_records_the_fragment(self):
        extruder = self.run_test_form_tip()
        # blade_pos 37.5 - retract_length 32.5: the load filled the nozzle and nothing is retracted
        self.assertAlmostEqual(extruder.filament_remaining, 5.0, places=1)


del TestFormTipCase


if __name__ == '__main__':
    unittest.main()
