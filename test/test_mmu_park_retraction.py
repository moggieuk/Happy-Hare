# Happy Hare test harness - the park retraction is tracked through load, restore and cut.
#
# A load stops short by the retraction _MMU_PARK made, which restoring the toolhead position then
# undoes, so the nozzle is full after any load. The cut fragment is worked out from the same
# retraction, so it is exact however the nozzle was left before the cut.
#
# The park, retract, restore and cut macros run for real. The fake gcode_move ignores E, so a
# ledger moves the extruder stepper by each G1 E and tracks how far the filament is short of a
# full nozzle: extruding past full oozes out, and a purge fills the nozzle before re-retracting
# what it un-retracted. The true fragment is blade_pos - residual - shortfall when the blade closes.
#
#   ./venv/bin/python -m unittest test.test_mmu_park_retraction
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

MACROS = ('_MMU_PARK', '_MMU_SAVE_POSITION', '_MMU_RESTORE_POSITION', '_MMU_RETRACT',
          '_MMU_UNRETRACT', '_MMU_CLEAR_POSITION', '_MMU_CUT_TIP', '_CUT_TIP_MOVE_TO_CUTTER_PIN',
          '_CUT_TIP_GANTRY_SERVO_DOWN', '_CUT_TIP_GANTRY_SERVO_UP')


class ParkRetractionTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session(profiles.get('3ms').derive('3ms_park_retraction', syms={'MMU_HAS_TOOLHEAD_CUTTER': True}))
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        self.toolhead = self.mmu.mmu_unit(0).toolhead_wrapper.p
        self.shortfall = 0.
        self.cut_shortfall = None
        self.recorded = None

        g1 = self.hh.gcode.register_command('G1', None)

        def extrude(gcmd):
            g1(gcmd)
            e = float(gcmd.get_command_parameters().get('E', 0))
            stepper = self.hh.printer.lookup_object('toolhead').get_extruder().extruder_stepper.stepper
            stepper.set_position([stepper.get_commanded_position() + e, 0., 0., 0.])
            stepper.harness_note_motion(e)
            self.shortfall = max(self.shortfall - e, 0.) if e > 0 else self.shortfall - e

        self.hh.gcode.register_command('G1', extrude)

        effects = self.hh.printer.harness_macro_effects
        for name in MACROS:
            effects[name] = lambda macro, gcmd: macro.run_body(gcmd)

        def cut(macro, gcmd):
            self.cut_shortfall = self.shortfall
            self.cut_retracted = self.retracted()
            macro.run_body(gcmd)

        effects['_CUT_TIP_DO_CUT_MOTION'] = cut
        effects[self.mmu.p.purge_macro] = lambda macro, gcmd: setattr(self, 'shortfall', self.retracted())

        move_filament = self.mmu.move_filament

        def record_load(trace, dist, *args, **kwargs):
            to_nozzle = (self.toolhead.toolhead_sensor_to_nozzle if self.mmu.sensor_manager.has_sensor('toolhead')
                         else self.toolhead.toolhead_extruder_to_nozzle)
            full = (to_nozzle - self.mmu.mmu_unit(0).extruder_wrapper.filament_remaining
                    - self.toolhead.toolhead_residual_filament - self.toolhead.toolhead_ooze_reduction)
            result = move_filament(trace, dist, *args, **kwargs)
            if trace == 'Loading filament to nozzle':
                self.shortfall = full - result[0]
            return result

        self.mmu.move_filament = record_load
        do_form_tip = self.mmu._do_form_tip

        def record_fragment(*args, **kwargs):
            result = do_form_tip(*args, **kwargs)
            self.recorded = result[1]
            return result

        self.mmu._do_form_tip = record_fragment

        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)

    def retracted(self):
        return self.hh.printer.lookup_object('gcode_macro _MMU_PARK').variables['retracted_length']

    def no_purge(self):
        self.mmu.p.purge_macro = ''

    def gcode(self, command):
        self.hh.run_gcode(command)
        self.assertEqual(self.hh.errors, [], command)

    def assert_nozzle_full(self):
        self.assertAlmostEqual(self.shortfall, 0., delta=0.05)
        self.assertEqual(self.retracted(), 0)

    def assert_fragment_exact(self):
        blade_pos = float(self.mmu.cut_tip_vars()['blade_pos'])
        true = blade_pos - self.toolhead.toolhead_residual_filament - self.cut_shortfall
        self.assertAlmostEqual(self.recorded, true, delta=0.05) # Extruder step rounding


class TestParkRetraction(ParkRetractionTestCase):

    def test_standalone_load_fills_the_nozzle(self):
        self.no_purge()
        self.gcode('MMU_LOAD')
        self.assert_nozzle_full()

    def test_unload_after_standalone_load_records_the_true_fragment(self):
        # The park retract before the cut is on top of whatever the load left
        self.no_purge()
        self.gcode('MMU_LOAD')
        self.gcode('MMU_UNLOAD')
        self.assert_fragment_exact()

    def test_test_form_tip_after_purged_load_records_the_true_fragment(self):
        # Nothing is retracted before MMU_TEST_FORM_TIP
        self.gcode('MMU_LOAD')
        self.gcode('MMU_TEST_FORM_TIP')
        self.assert_fragment_exact()

    def test_print_toolchange_is_exact(self):
        self.gcode('MMU_LOAD')
        self.mmu.psm.set_print_state('printing', call_macro=False)
        for tool in (1, 0):
            self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
            self.assertEqual(self.cut_retracted, self.mmu.toolchange_retract)
            self.assert_fragment_exact()
            self.assert_nozzle_full()


if __name__ == '__main__':
    unittest.main()
