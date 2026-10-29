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
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from test.hh import profiles, session
from test.hh.bootstrap import PRINTER_STUB

logging.getLogger().setLevel(logging.CRITICAL)

# A custom park_macro wrapping the shipped one, which still holds the retraction. HH already sets
# next_pos and next_xy on park_macro, so a wrapper has to define them
PARK_WRAPPER = """
[gcode_macro MY_PARK]
variable_next_pos: False
variable_next_xy: 0, 0
gcode:
    _MMU_PARK {rawparams}
"""

# A replacement for the shipped park macros that holds its own retraction: renamed copies of them
REPLACED = ('_MMU_PARK', '_MMU_SAVE_POSITION', '_MMU_RESTORE_POSITION', '_MMU_RETRACT', '_MMU_UNRETRACT',
            '_MMU_CLEAR_POSITION')


def replacement_park_macros():
    with open(os.path.join(os.path.dirname(__file__), '..', 'config', 'macros', 'mmu_sequence.cfg')) as f:
        text = f.read()
    blocks = [m.group(0) for m in re.finditer(r'^\[gcode_macro (\w+)\]\n.*?(?=^\[|^#{5,}|\Z)', text, flags=re.M | re.S)
              if m.group(1) in REPLACED]
    assert len(blocks) == len(REPLACED)
    out = '\n'.join(blocks)
    for name in REPLACED:
        out = re.sub(r'\b%s\b' % name, 'MY' + name, out)
    return '\n' + out


MACROS = ('_MMU_PARK', '_MMU_SAVE_POSITION', '_MMU_RESTORE_POSITION', '_MMU_RETRACT',
          '_MMU_UNRETRACT', '_MMU_CLEAR_POSITION', '_MMU_CUT_TIP', '_CUT_TIP_MOVE_TO_CUTTER_PIN',
          '_CUT_TIP_GANTRY_SERVO_DOWN', '_CUT_TIP_GANTRY_SERVO_UP')


class ParkRetractionTestCase(unittest.TestCase):

    def setUp(self):
        self.shortfall = 0.
        self.start()
        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)

    def start(self, vars_file=None):
        self.hh = session(profiles.get('3ms').derive('3ms_park_retraction', syms={'MMU_HAS_TOOLHEAD_CUTTER': True}),
                          printer_stub=PRINTER_STUB + PARK_WRAPPER + replacement_park_macros())
        if vars_file is not None:
            self.hh._mmu_vars_copy = lambda: vars_file
        self.hh.build()
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        self.toolhead = self.mmu.mmu_unit(0).toolhead_wrapper.p
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
        for name in MACROS + ('MY_PARK',) + tuple('MY' + name for name in REPLACED):
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

    def restart(self):
        self.hh.reactor.advance(1.) # Flush mmu_vars
        vars_file = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        shutil.copy(self.hh.save_variables.filename, vars_file)
        self.hh.close()
        self.start(vars_file)

    def retracted(self):
        return self.hh.printer.lookup_object('gcode_macro _MMU_PARK').variables['retracted_length']

    def no_purge(self):
        self.mmu.p.purge_macro = ''

    def gcode(self, command):
        self.hh.run_gcode(command)
        self.assertEqual(self.hh.errors, [], command)

    def assert_shortfall(self, shortfall):
        self.assertAlmostEqual(self.shortfall, shortfall, delta=0.05)

    def assert_nozzle_full(self):
        self.assert_shortfall(0.)
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

    def test_print_end_retraction_is_kept_for_the_next_cut(self):
        self.gcode('MMU_LOAD')
        self.gcode('MMU_PRINT_START')
        self.gcode('MMU_PRINT_END')
        self.assertGreater(self.retracted(), 0)
        self.gcode('MMU_UNLOAD')
        self.assert_fragment_exact()

    def test_retraction_survives_a_restart(self):
        self.gcode('MMU_LOAD')
        self.gcode('MMU_PRINT_START')
        self.gcode('MMU_PRINT_END')
        retracted = self.retracted()
        self.restart()
        self.assertEqual(self.retracted(), retracted)
        self.assertEqual(self.mmu.mmu_unit().extruder_wrapper.retracted_length, retracted)

    def test_print_start_resets_the_retraction(self):
        # The start gcode is assumed to prime the nozzle
        self.gcode('MMU_LOAD')
        self.gcode('MMU_PRINT_START')
        self.gcode('MMU_PRINT_END')
        self.gcode('MMU_PRINT_START')
        self.assertEqual(self.retracted(), 0)
        self.assertEqual(self.mmu.mmu_unit().extruder_wrapper.retracted_length, 0)

    def test_load_without_a_saved_position_fills_the_nozzle(self):
        # Nothing parks or restores while XYZ isn't homed, so nothing would un-retract
        self.gcode('MMU_LOAD')
        self.gcode('MMU_UNLOAD')
        self.assertGreater(self.retracted(), 0)
        get_status = self.mmu.toolhead.get_status
        with mock.patch.object(self.mmu.toolhead, 'get_status',
                               side_effect=lambda *a: dict(get_status(*a), homed_axes='')):
            self.gcode('MMU_LOAD')
        self.assert_nozzle_full()

    def test_park_macro_wrapper_toolchange_is_exact(self):
        self.mmu.p.park_macro = 'MY_PARK'
        self.gcode('MMU_LOAD')
        self.mmu.psm.set_print_state('printing', call_macro=False)
        self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.cut_retracted, self.mmu.toolchange_retract)
        self.assert_fragment_exact()
        self.assert_nozzle_full()

    def test_replacement_park_macros_toolchange_is_exact(self):
        for param, name in (('park_macro', '_MMU_PARK'), ('save_position_macro', '_MMU_SAVE_POSITION'),
                            ('restore_position_macro', '_MMU_RESTORE_POSITION'),
                            ('clear_position_macro', '_MMU_CLEAR_POSITION')):
            setattr(self.mmu.p, param, 'MY' + name)
        self.gcode('MMU_LOAD')
        self.mmu.psm.set_print_state('printing', call_macro=False)
        self.gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.retracted(), 0) # The shipped macro is unused
        self.assert_fragment_exact()
        self.assert_shortfall(0.)

    def test_extruder_only_load_records_the_retraction_it_takes_up(self):
        # No park or restore, so the load fills the nozzle
        self.gcode('MMU_LOAD')
        self.gcode('MMU_UNLOAD')
        self.assertGreater(self.retracted(), 0)
        self.hh.place_filament(0, position=self.hh.filament().layout['extruder_entry'] - 5.)
        self.gcode('MMU_LOAD EXTRUDER_ONLY=1')
        self.assert_nozzle_full()
        self.assertEqual(self.mmu.mmu_unit().extruder_wrapper.retracted_length, 0)

    def test_print_toolchange_is_exact(self):
        self.gcode('MMU_LOAD')
        self.mmu.psm.set_print_state('printing', call_macro=False)
        for tool in (1, 0):
            self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
            self.assertEqual(self.cut_retracted, self.mmu.toolchange_retract)
            self.assert_fragment_exact()
            self.assert_nozzle_full()


class TestRetractionPerExtruder(unittest.TestCase):

    def setUp(self):
        from test.hh.bootstrap import PRINTER_STUB
        from test.hh.profiles import EXTRA_EXTRUDER_STUB
        self.hh = session('ercf_vvd_dual_extruder', printer_stub=PRINTER_STUB + EXTRA_EXTRUDER_STUB)
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        self.park = self.hh.printer.lookup_object('gcode_macro _MMU_PARK')

    def test_each_extruder_keeps_its_own_retraction(self):
        self.mmu.select_gate(0)
        self.hh.run_gcode('SET_GCODE_VARIABLE MACRO=_MMU_PARK VARIABLE=retracted_length VALUE=3')
        self.mmu.select_gate(9) # unit1, on extruder1
        self.assertEqual(self.park.variables['retracted_length'], 0)
        self.mmu.select_gate(1)
        self.assertEqual(self.park.variables['retracted_length'], 3)
        self.assertEqual(self.hh.errors, [])

    def test_print_start_keeps_inactive_extruder_retraction(self):
        # Only the active extruder is primed by the start gcode
        self.mmu.select_gate(9)
        self.hh.run_gcode('SET_GCODE_VARIABLE MACRO=_MMU_PARK VARIABLE=retracted_length VALUE=3')
        self.mmu.select_gate(0)
        self.hh.run_gcode('MMU_PRINT_START')
        self.assertEqual(self.park.variables['retracted_length'], 0)
        self.mmu.select_gate(9)
        self.assertEqual(self.park.variables['retracted_length'], 3)
        self.assertEqual(self.hh.errors, [])


if __name__ == '__main__':
    unittest.main()
