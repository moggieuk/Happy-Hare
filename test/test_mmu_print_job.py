# Tests for playing a print job in the console (`/print job`, test/console.py) and for the
# fake Klipper G-code it relies on: KEY=VALUE parsing, gcode_move's movement modes, the
# extruder heater commands and the speed override HH saves and restores around a toolchange.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace

from test.hh import install, session
from test.hh import print_job
from test import console as console_mod

logging.getLogger().setLevel(logging.CRITICAL)

install()  # put the fake klippy tree on sys.path before importing gcode

import gcode as gcode_mod  # noqa: E402


class TestGcodeParser(unittest.TestCase):

    def parse(self, line):
        printer = SimpleNamespace(get_reactor=lambda: SimpleNamespace(mutex=lambda: None))
        return gcode_mod.GCodeDispatch(printer)._parse(line)[1]

    def test_an_empty_value_does_not_swallow_the_next_parameter(self):
        params = self.parse('MMU_SLICER_TOOL_MAP PURGE_VOLUMES= NUM_SLICER_TOOLS=4')
        self.assertEqual(params, {'PURGE_VOLUMES': '', 'NUM_SLICER_TOOLS': '4'})

    def test_a_trailing_comment_is_not_parsed(self):
        params = self.parse('MMU_CHANGE_TOOL TOOL=1 NEXT_POS="1,2" ; T1 X=9')
        self.assertEqual(params, {'TOOL': '1', 'NEXT_POS': '1,2'})
        self.assertEqual(self.parse('G1 X10 E1 ; Y20')['X'], '10')
        self.assertNotIn('Y', self.parse('G1 X10 E1 ; Y20'))


class TestFakeKlipperGcode(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.toolhead = self.hh.printer.lookup_object('toolhead')

    def e(self):
        return self.toolhead.get_position()[3]

    def extruded(self, script):
        before = self.e()
        self.hh.run_gcode(script)
        return round(self.e() - before, 6)

    def test_relative_and_absolute_extrusion(self):
        self.assertEqual(self.extruded('M83\nG1 E10 F300'), 10.)
        self.assertEqual(self.extruded('G1 E-2'), -2.)
        self.assertEqual(self.extruded('M82\nG92 E0\nG1 E5'), 5.)
        self.assertEqual(self.extruded('G1 E8'), 3.)

    def test_g91_makes_extrusion_relative_whatever_m82_says(self):
        self.assertEqual(self.extruded('M82\nG92 E0\nG91\nG1 E2\nG1 E2'), 4.)
        # And M83 keeps E relative while G90 makes XYZ absolute
        self.extruded('G90\nM83\nG1 X10 Y10 E1\nG1 X10 Y10 E1')
        self.assertEqual(self.toolhead.get_position()[:2], [10., 10.])
        self.assertEqual(self.extruded('G1 E1'), 1.)

    def test_extruder_heater_commands(self):
        heater = self.hh.printer.lookup_object('extruder').get_heater()
        self.hh.run_gcode('M104 S230')
        self.assertEqual(heater.get_temp()[1], 230.)
        self.hh.run_gcode('M109 S215')
        self.assertEqual(heater.get_temp(), (215., 215.))

    def test_speed_override_survives_hh_saving_and_restoring_it(self):
        gcode_move = self.hh.printer.lookup_object('gcode_move')
        self.assertEqual(gcode_move.get_status()['speed_factor'], 1.)
        self.hh.run_gcode('M220 S50\nG1 X10 F6000')
        self.assertEqual(gcode_move.get_status()['speed_factor'], .5)
        self.assertEqual(gcode_move.speed, 50.)


class TestMoldingTheBuiltinJob(unittest.TestCase):

    LINES = ['MMU_START_SETUP INITIAL_TOOL=2 TOOL_MATERIALS="!materials!"', 'T1', 'T2',
             'MMU_CHANGE_TOOL TOOL=3 NEXT_POS="1,2" ; T3', '; T2 in a comment', 'T0']

    def test_tools_fold_onto_the_gates_that_exist(self):
        for gates, tools in ((1, ['0', '0', '0', '0', '0']), (2, ['0', '1', '0', '1', '0']),
                             (4, ['2', '1', '2', '3', '0']), (13, ['2', '1', '2', '3', '0'])):
            molded = print_job.mold(self.LINES, gates)
            self.assertEqual(molded[0], 'MMU_START_SETUP INITIAL_TOOL=%s TOOL_MATERIALS='
                                        '"!materials!"' % tools[0])
            self.assertEqual([molded[1], molded[2], molded[3], molded[5]],
                             ['T' + tools[1], 'T' + tools[2],
                              'MMU_CHANGE_TOOL TOOL=%s NEXT_POS="1,2" ; T3' % tools[3],
                              'T' + tools[4]])
            self.assertEqual(molded[4], '; T2 in a comment')

    def test_metadata_follows_the_gate_map_and_the_tool_count(self):
        gate_maps = SimpleNamespace(ttg_map=[1, 0], gate_color=['ff0000', ''],
                                    gate_temperature=[200, 0], gate_material=['PLA', ''],
                                    gate_filament_name=['Red', ''])
        colors, temps, materials, names = print_job.gate_map_metadata(gate_maps, 3, 215)
        self.assertEqual(colors, ['000000', 'ff0000', '000000'])
        self.assertEqual(temps, ['215', '200', '215'])
        self.assertEqual(materials, ['PLA', 'PLA', 'PLA'])
        self.assertEqual(names, ['', 'Red', ''])


class TestPrintJob(unittest.TestCase):

    def console(self):
        console = console_mod.Console(console_mod.parse_args(
            ['--profile', 'boxturtle', '--plain', '--pace', '0', '--no-log', '--no-serve']))
        self.addCleanup(console.close)
        console.boot()
        return console

    def meta(self, console, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console.meta(line)
        return out.getvalue()

    def test_the_builtin_job_prints_end_to_end(self):
        console = self.console()
        hh = console.hh
        started = hh.reactor.monotonic()
        out = self.meta(console, '/print job')
        self.assertEqual(hh.errors, [], out[-3000:])
        self.assertEqual(console.failures, 0, out[-3000:])
        self.assertIsNone(console.job)
        self.assertEqual(hh.mmu.psm.print_state, 'complete')
        self.assertEqual(hh.printer.lookup_object('print_stats').state, 'complete')
        self.assertEqual(hh.mmu.num_toolchanges, 4)         # the initial load, then T1 T2 T0
        self.assertIn('Checking gate 2...', out)            # the gate check really ran
        self.assertIn('Print job print_job.gcode complete', out)
        self.assertGreater(hh.reactor.monotonic() - started, 30.)
        self.assertEqual(hh.printer.harness_macro_effects.get('T1'), None)

    def job_file(self, text):
        fd, path = tempfile.mkstemp(suffix='.gcode')
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_a_failing_command_errors_the_print(self):
        console = self.console()
        out = self.meta(console, '/print job %s' % self.job_file(
            'G90\nG1 X10 F600\nG1 X15 F0\nG1 X20 F600\n'))
        self.assertIn('failed at line 3', out)
        self.assertIsNone(console.job)
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'error')
        self.assertEqual(console.hh.printer.lookup_object('toolhead').get_position()[0], 10.)

    def test_cancel_print_in_the_job_stops_it(self):
        console = self.console()
        out = self.meta(console, '/print job %s' % self.job_file(
            'G90\nG1 X10 F600\nCANCEL_PRINT\nG1 X30 F600\n'))
        self.assertIn('cancelled at line 3', out)
        self.assertIsNone(console.job)
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'cancelled')
        self.assertEqual(console.hh.mmu.psm.print_state, 'cancelled')
        self.assertEqual(console.hh.printer.lookup_object('toolhead').get_position()[0], 10.)

    def test_a_job_needing_more_tools_than_the_mmu_has_fails_in_happy_hare(self):
        path = self.job_file(
            '; generated by PrusaSlicer 2.8.1\n'
            'MMU_START_SETUP INITIAL_TOOL=0 REFERENCED_TOOLS=!referenced_tools! '
            'TOOL_COLORS=!colors! TOOL_TEMPS=!temperatures! '
            'TOOL_MATERIALS=!materials! FILAMENT_NAMES=!filament_names! '
            'PURGE_VOLUMES=!purge_volumes! TOTAL_TOOLCHANGES=!total_toolchanges!\n'
            'MMU_START_CHECK\nMMU_START_LOAD_INITIAL_TOOL\n'
            'T4\nG1 X10 Y10 E1\nMMU_END\n')
        console = self.console()
        out = self.meta(console, '/print job %s' % path)
        self.assertIn('your MMU only has 4 gates', out)
        self.assertIsNone(console.job)
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'error')

    def test_an_unprocessed_job_using_a_tool_the_mmu_lacks_fails(self):
        console = self.console()
        out = self.meta(console, '/print job %s' % self.job_file(
            'G90\nG1 X10 F600\nT4\nG1 X20 F600\n'))
        self.assertIn('T4: no such tool, this MMU has 4 gates', out)
        self.assertIn('failed at line 3', out)
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'error')
        self.assertEqual(console.hh.printer.lookup_object('toolhead').get_position()[0], 10.)

    def test_a_job_the_mmu_pauses_is_held_until_cancelled(self):
        console = self.console()
        out = self.meta(console, '/print job %s' % self.job_file(
            'MMU_GATE_MAP GATE=1 AVAILABLE=0\nMMU_CHANGE_TOOL TOOL=1\nG1 X20 F600\n'))
        self.assertIn('Gate 1 is empty', out)
        self.assertIsNotNone(console.job, out[-3000:])
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'paused')
        self.meta(console, '/print cancel')
        self.assertIsNone(console.job)
        self.assertEqual(console.hh.printer.lookup_object('print_stats').state, 'cancelled')


if __name__ == '__main__':
    unittest.main()
