# Happy Hare slicer tool map tests: MMU_SLICER_TOOL_MAP argument checking and the
# slicer/gate count sanity check in MMU_START_SETUP (whose body runs for real here).
#
#   ./venv/bin/python -m unittest test.test_mmu_slicer_tool_map
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class SlicerToolMapTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.hh.printer.harness_macro_effects['MMU_START_SETUP'] = (
            lambda macro, gcmd: macro.run_body(gcmd))
        sw_vars = self.hh.printer.lookup_object('gcode_macro _MMU_SOFTWARE_VARS')
        sw_vars.variables = dict(sw_vars.variables, home_mmu=False, automap_strategy='none')

    def tearDown(self):
        self.hh.close()

    def test_purge_volumes_count_error_reports_slicer_tool_counts(self):
        # 3 slicer tools on 4 gates accepts 1, 3, 6 or 9 values
        with self.assertRaises(self.hh.gcode.error) as cm:
            self.hh.run_gcode('MMU_SLICER_TOOL_MAP NUM_SLICER_TOOLS=3 PURGE_VOLUMES=1,2,3,4,5')
        self.assertIn('Expected 1, 3, 6, or 9, got 5', str(cm.exception))

    def start_setup(self, num_tools):
        tools = range(num_tools)
        self.hh.run_gcode(
            'MMU_START_SETUP INITIAL_TOOL=0 REFERENCED_TOOLS=0 PURGE_VOLUMES=0'
            ' TOOL_COLORS=%s TOOL_TEMPS=%s TOOL_MATERIALS=%s FILAMENT_NAMES=%s' % (
                ','.join('ff0000' for _ in tools), ','.join('210' for _ in tools),
                ','.join('PLA' for _ in tools), ','.join('n%d' % t for t in tools)))

    def test_more_slicer_tools_than_gates_fails_clearly(self):
        with self.assertRaises(self.hh.printer.command_error) as cm:
            self.start_setup(5)
        self.assertIn('5 extruders but your MMU only has 4 gates', str(cm.exception))

    def test_fewer_slicer_tools_than_gates_warns_and_continues(self):
        self.start_setup(3)
        self.assertTrue(any('setup with 3 extruders but your MMU has 4 gates! These should match'
                            ' but will attempt to continue' in m.replace('\xa0', ' ')
                            for m in self.hh.console),
                        self.hh.console)
        self.assertEqual(self.hh.mmu.gate_maps.slicer_tool_map['tools']['2']['material'], 'PLA')


class ColorAutomapTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

    def tearDown(self):
        self.hh.close()

    def load_gates(self, *gates):
        for gate, (color, material) in enumerate(gates):
            self.hh.run_gcode('MMU_GATE_MAP GATE=%d COLOR=%s MATERIAL=%s QUIET=1' % (gate, color, material or "''"))

    def automap_t0(self, color, material, strategy='color'):
        self.hh.run_gcode("MMU_SLICER_TOOL_MAP TOOL=0 COLOR=%s MATERIAL='%s' AUTOMAP=%s" % (color, material, strategy))

    def test_color_skips_same_color_gate_with_other_material(self):
        # Every match is mapped in turn, so PETG on the later gate would win
        self.load_gates(('ff0000', 'PLA'), ('000000', 'PLA'), ('000000', 'PETG'), ('ffffff', 'PLA'))
        self.automap_t0('000000', 'pla')
        self.assertEqual(self.hh.mmu.ttg_map[0], 1)
        self.assertEqual(self.hh.errors, [])

    def test_color_with_no_matching_material_errors_and_keeps_map(self):
        self.load_gates(('ff0000', 'PLA'), ('000000', 'PETG'), ('000000', 'PETG'), ('ffffff', 'PLA'))
        self.automap_t0('000000', 'PLA')
        self.assertEqual(self.hh.mmu.ttg_map[0], 0)
        self.assertTrue(any('No gates found for tool 0 with Color 000000 (material PLA)' in e
                            for e in self.hh.errors), self.hh.errors)

    def test_color_without_tool_material_matches_on_color_alone(self):
        self.load_gates(('ff0000', 'PLA'), ('ffffff', 'PLA'), ('000000', 'PETG'), ('ffffff', 'PLA'))
        self.automap_t0('000000', 'unknown')
        self.assertEqual(self.hh.mmu.ttg_map[0], 2)
        self.assertEqual(self.hh.errors, [])

    def test_color_matches_gate_without_material(self):
        self.load_gates(('ff0000', 'PLA'), ('ffffff', 'PLA'), ('000000', ''), ('ffffff', 'PLA'))
        self.automap_t0('000000', 'PLA')
        self.assertEqual(self.hh.mmu.ttg_map[0], 2)
        self.assertEqual(self.hh.errors, [])


if __name__ == '__main__':
    unittest.main()
