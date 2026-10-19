# Happy Hare test harness - encoder validation of the final load to the nozzle.
#
# The last extruder load move is synced (gear+extruder), so a sync-feedback buffer's travel
# can absorb gear push when the extruder fails to grip and the encoder still reads movement.
# When the buffer's own grip check (toolhead_entry_tension_test) has confirmed the extruder
# is pulling, the encoder test is skipped; otherwise it still runs.
#
#   ./venv/bin/python -m unittest test.test_mmu_extruder_load
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest.mock import patch

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0
NOZZLE_MOVE = 'Loading filament to nozzle'
SKIPPED = 'Skipping encoder load check'


class ExtruderLoadTestCase(unittest.TestCase):
    PROFILE = None
    SESSION_KWARGS = {}
    GRIP_CHECK = None   # Console text proving the buffer grip check ran

    def setUp(self):
        self.hh = session(self.PROFILE, **self.SESSION_KWARGS)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.errors, [], 'preload was not clean')
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)
        self.hh.run_gcode('MMU_TEST_CONFIG log_level=2')

    def tearDown(self):
        self.hh.close()

    def load_with_encoder_under_read(self):
        """MMU_LOAD with the encoder seeing only 10% of the final move to the nozzle"""
        move_filament = self.mmu.move_filament
        traces = []
        start = len(self.hh.console)

        def under_read(trace_str, dist, *args, **kwargs):
            traces.append(trace_str)
            actual, homed, measured, delta = move_filament(trace_str, dist, *args, **kwargs)
            if trace_str == NOZZLE_MOVE:
                measured = abs(actual) * 0.1
                delta = abs(actual) - measured
            return actual, homed, measured, delta

        with patch.object(self.mmu, 'move_filament', side_effect=under_read):
            self.hh.run_gcode('MMU_LOAD')
        self.assertIn(NOZZLE_MOVE, traces, 'never made the final move to the nozzle')
        return '\n'.join(self.hh.console[start:])

    def nozzle_failures(self):
        return [e for e in self.hh.errors if 'Move to nozzle failed' in e]


class GripCheckCases:

    def test_grip_confirmed_by_buffer_skips_the_encoder_check(self):
        console = self.load_with_encoder_under_read()
        self.assertIn(self.GRIP_CHECK, console)
        self.assertIn(SKIPPED, console)
        self.assertEqual(self.nozzle_failures(), [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_encoder_check_still_runs_without_the_grip_check(self):
        self.hh.run_gcode('MMU_TEST_CONFIG toolhead_entry_tension_test=0')
        console = self.load_with_encoder_under_read()
        self.assertNotIn(self.GRIP_CHECK, console)
        self.assertNotIn(SKIPPED, console)
        self.assertTrue(self.nozzle_failures(), self.hh.errors)
        self.assertNotEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)


class TestCompressionBuffer(GripCheckCases, ExtruderLoadTestCase):
    # BoxTurtle two-switch buffer + encoder, homing to the extruder via compression
    PROFILE = 'encoder'
    GRIP_CHECK = 'Monitoring extruder entrance transition'


class TestProportionalBuffer(GripCheckCases, ExtruderLoadTestCase):
    # QuattroBox v2: Binky encoder + PSF-style proportional buffer
    PROFILE = 'quattrobox_v2'
    SESSION_KWARGS = {'virtual_nfc': True}
    GRIP_CHECK = 'extruder grip confirmed'


if __name__ == '__main__':
    unittest.main()
