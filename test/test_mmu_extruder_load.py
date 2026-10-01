# Happy Hare test harness - encoder validation of the final load to the nozzle.
#
# The last extruder load move is synced (gear+extruder), so a sync-feedback buffer's travel
# can absorb gear push when the extruder fails to grip and the encoder still reads movement.
# When the buffer's own grip check (toolhead_entry_tension_test) has confirmed the extruder
# is pulling, only the shortfall test is skipped: no movement at all still fails the load.
#
#   ./venv/bin/python -m unittest test.test_mmu_extruder_load
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest.mock import patch

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0
NOZZLE_MOVE = 'Loading filament to nozzle'
SKIPPED = 'Skipping encoder shortfall check'


class ExtruderLoadTestCase(unittest.TestCase):
    PROFILE = None
    SESSION_KWARGS = {}
    GRIP_CHECK = None   # Console text proving the buffer grip check ran

    def setUp(self):
        self.start_session(self.PROFILE)

    def start_session(self, profile):
        self.hh = session(profile, **self.SESSION_KWARGS)
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

    def load_with_encoder_reading(self, fraction):
        """MMU_LOAD with the encoder seeing only `fraction` of the final move to the nozzle"""
        move_filament = self.mmu.move_filament
        traces = []
        self.nozzle_move_lengths = []
        start = len(self.hh.console)

        def under_read(trace_str, dist, *args, **kwargs):
            traces.append(trace_str)
            actual, homed, measured, delta = move_filament(trace_str, dist, *args, **kwargs)
            self.after_move(trace_str)
            if trace_str == NOZZLE_MOVE:
                self.nozzle_move_lengths.append(abs(actual))
                measured = abs(actual) * fraction
                delta = abs(actual) - measured
            return actual, homed, measured, delta

        with patch.object(self.mmu, 'move_filament', side_effect=under_read):
            self.hh.run_gcode('MMU_LOAD')
        self.assertIn(NOZZLE_MOVE, traces, 'never made the final move to the nozzle')
        return '\n'.join(self.hh.console[start:])

    def after_move(self, trace_str):
        """Hook for buffer response the harness doesn't model"""

    def nozzle_failures(self, reason='Move to nozzle failed'):
        return [e for e in self.hh.errors if reason in e]


class GripCheckCases:

    def test_grip_confirmed_by_buffer_skips_the_shortfall_check(self):
        console = self.load_with_encoder_reading(0.1)
        self.assertIn(self.GRIP_CHECK, console)
        self.assertIn(SKIPPED, console)
        self.assertEqual(self.nozzle_failures(), [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_encoder_check_still_runs_without_the_grip_check(self):
        self.hh.run_gcode('MMU_TEST_CONFIG toolhead_entry_tension_test=0')
        console = self.load_with_encoder_reading(0.1)
        self.assertNotIn(self.GRIP_CHECK, console)
        self.assertNotIn(SKIPPED, console)
        self.assertTrue(self.nozzle_failures('sufficient movement'), self.hh.errors)
        self.assertNotEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_no_movement_still_fails_after_grip_confirmed(self):
        self.hh.run_gcode('MMU_TEST_CONFIG toolhead_move_error_tolerance=100')
        console = self.load_with_encoder_reading(0.0)
        self.assertIn(self.GRIP_CHECK, console)
        self.assertTrue(self.nozzle_failures('any movement'), self.hh.errors)
        self.assertNotEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_no_movement_ignored_on_a_short_move_after_grip_confirmed(self):
        # A compact buffer and short hotend leave a genuinely short final move
        # after the real grip check. Keep the normal 15 mm encoder threshold.
        self.hh.close()
        profile = profiles.get(self.PROFILE).derive(
            self.PROFILE + "_short_extruder",
            syms={
                "PARAM_BUFFER_MAXRANGE": 6,
                "PARAM_BUFFER_RANGE": 4,
                "PARAM_TOOLHEAD_EXTRUDER_TO_NOZZLE": 18,
            },
        )
        self.start_session(profile)
        self.assertEqual(self.mmu.mmu_unit().p.encoder_move_step_size, 15.)
        console = self.load_with_encoder_reading(0.0)
        self.assertEqual(len(self.nozzle_move_lengths), 1)
        self.assertGreater(self.nozzle_move_lengths[0], 0.)
        self.assertLessEqual(self.nozzle_move_lengths[0], 15.)
        self.assertIn(self.GRIP_CHECK, console)
        self.assertIn(SKIPPED, console)
        self.assertEqual(self.nozzle_failures(), [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)


class TestCompressionBuffer(GripCheckCases, ExtruderLoadTestCase):
    # BoxTurtle two-switch buffer + encoder, homing to the extruder via compression
    PROFILE = 'encoder'
    GRIP_CHECK = 'Monitoring extruder entrance transition'


class TestProportionalBuffer(GripCheckCases, ExtruderLoadTestCase):
    # QuattroBox v2: Binky encoder + PSF-style proportional buffer
    PROFILE = 'quattrobox_v2'
    SESSION_KWARGS = {'virtual_nfc': True}
    GRIP_CHECK = 'extruder grip confirmed'
    extruder_grips = True

    def after_move(self, trace_str):
        # Compression homing leaves the buffer compressed; a gripping extruder then
        # draws it back toward tension
        prop = self.hh.sensor('filament_proportional')
        if trace_str == 'Synced feed to establish extruder grip':
            prop.feed(prop._extreme_value())
        elif trace_str.startswith('Proportional extruder entry validation step') and self.extruder_grips:
            prop.feed(prop.neutral_value())

    def test_load_fails_when_buffer_never_relaxes(self):
        self.extruder_grips = False
        with self.assertRaises(AssertionError): # Never reaches the move to the nozzle
            self.load_with_encoder_reading(1.0)
        self.assertTrue(self.nozzle_failures('proportional buffer did not relax'), self.hh.errors)
        self.assertNotEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)


if __name__ == '__main__':
    unittest.main()
