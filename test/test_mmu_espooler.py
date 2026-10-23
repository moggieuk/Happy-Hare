# Happy Hare test harness - eSpooler power scaling by spool weight (#856).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import session


class TestEspoolerSpoolWeight(unittest.TestCase):
    """
    espooler_full_spool_power scales rewind/assist power from that % on a full spool
    up to 100% on an empty one, using the gate map's spool weights.
    """

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu
        self.mmu._set_gate_selected(0)
        self.unit = self.mmu.mmu_unit()
        self.espooler = self.mmu.espooler()
        self.calls = []
        original = self.espooler.set_operation

        def record(gate, value, operation):
            self.calls.append((gate, value, operation))
            return original(gate, value, operation)
        self.espooler.set_operation = record

    def tearDown(self):
        self.hh.close()

    def rewind_power(self):
        self.calls.clear()
        speed = self.unit.p.espooler_max_stepper_speed / 2
        with self.mmu._wrap_espooler('gear', -200, speed, 100, 0):
            pass
        return self.calls[0][1]

    def test_half_full_spool_scales_rewind_power(self):
        unscaled = self.rewind_power()
        self.unit.p.espooler_full_spool_power = 50
        self.mmu.gate_remaining_weight[0] = 500
        self.mmu.gate_initial_weight[0] = 1000
        self.assertAlmostEqual(self.rewind_power(), unscaled * 0.75)

    def test_unknown_weight_leaves_power_unchanged(self):
        unscaled = self.rewind_power()
        self.unit.p.espooler_full_spool_power = 50
        self.assertAlmostEqual(self.rewind_power(), unscaled)

    def test_spoolman_gate_map_update_sets_and_keeps_weights(self):
        self.hh.run_gcode('MMU_GATE_MAP MAP="{0: {\'spool_id\': 5, \'remaining_weight\': 400, \'initial_weight\': 1000}}" FROM_SPOOLMAN=1 QUIET=1')
        self.assertEqual((self.mmu.gate_remaining_weight[0], self.mmu.gate_initial_weight[0]), (400, 1000))
        # A later update without weights (e.g. from the UI) must not wipe them
        self.hh.run_gcode('MMU_GATE_MAP MAP="{0: {\'spool_id\': 5, \'material\': \'PLA\'}}" QUIET=1')
        self.assertEqual((self.mmu.gate_remaining_weight[0], self.mmu.gate_initial_weight[0]), (400, 1000))

    def test_clearing_another_gates_spool_keeps_manual_weights(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 WEIGHT=400 INITIAL_WEIGHT=1000 QUIET=1')
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 SPOOLID=7 QUIET=1')
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 SPOOLID=-1 QUIET=1')
        self.assertEqual((self.mmu.gate_remaining_weight[1], self.mmu.gate_initial_weight[1]), (400, 1000))

    def test_gate_map_details_reports_weight_and_percent_remaining(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 WEIGHT=400 INITIAL_WEIGHT=1000 QUIET=1')
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_GATE_MAP DETAILS=1')
        self.assertIn('[WEIGHT: 400/1000g (40%)]', '\n'.join(self.hh.console[at:]))


if __name__ == '__main__':
    unittest.main()
