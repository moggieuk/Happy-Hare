# Happy Hare test harness - gcode offsets in the toolhead positions Happy Hare restores.
#
# Klipper's saved gcode state holds last_position, which already includes the gcode
# offset (gcode position + SET_GCODE_OFFSET/G92), and RESTORE_GCODE_STATE MOVE=1 moves
# straight to it. Any position Happy Hare writes into that state must therefore be the
# offset one, not the gcode coordinate, or the restore move lands off by the offset.
#
#   ./venv/bin/python -m unittest test.test_mmu_toolhead_offsets
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0
OFFSET = (2., 1., -0.2)


class ToolheadOffsetTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        for gate in (0, 1):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.settle(1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.hh.run_gcode("SET_GCODE_OFFSET X=%s Y=%s Z=%s" % OFFSET)
        self.hh.run_gcode("G90\nG1 X100 Y100 Z5")
        self.printing_at = [100. + OFFSET[0], 100. + OFFSET[1], 5. + OFFSET[2]]
        self.assertEqual(self.toolhead.get_position()[:3], self.printing_at)

    def tearDown(self):
        self.hh.close()

    def moves_during(self, script):
        start = len(self.toolhead.moves)
        self.hh.run_gcode(script)
        self.hh.settle(0.5)
        self.assertEqual(self.hh.errors, [])
        return [pos[:3] for pos, _ in self.toolhead.moves[start:]]

    def assert_xyz(self, actual, expected):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, places=6)

    def set_restore_xy_pos(self, value):
        seq = self.hh.printer.lookup_object('gcode_macro _MMU_SEQUENCE_VARS')
        variables = dict(seq.variables)
        variables['restore_xy_pos'] = value
        seq.variables = variables

    def test_next_pos_restore_includes_offset(self):
        self.set_restore_xy_pos('next')
        moves = self.moves_during('MMU_CHANGE_TOOL TOOL=1 NEXT_POS=50,60')
        self.assertTrue(moves, 'expected the toolchange to restore position')
        self.assert_xyz(moves[-1], [50. + OFFSET[0], 60. + OFFSET[1], self.printing_at[2]])

    def test_restore_xy_none_does_not_move_xy(self):
        self.set_restore_xy_pos('none')
        moves = self.moves_during('MMU_CHANGE_TOOL TOOL=1')
        self.assertTrue(moves, 'expected the toolchange to restore position')
        self.assert_xyz(moves[-1], self.printing_at)

    def test_resume_does_not_move_toolhead_by_offset(self):
        self.hh.run_gcode('MMU_PAUSE')
        self.hh.settle(0.5)
        self.assertTrue(self.hh.mmu.is_paused())
        del self.hh.gcode.errors[:]     # The pause itself is reported as an MMU issue
        for move in self.moves_during('RESUME'):
            self.assert_xyz(move, self.printing_at)


if __name__ == '__main__':
    unittest.main()
