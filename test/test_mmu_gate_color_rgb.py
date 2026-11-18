# Happy Hare test harness - gate_color_rgb follows gate_color.
#
# gate_color_rgb is the parallel map the LEDs and UIs read. A gate that sensor
# validation finds empty has its color cleared, so the RGB map must clear with it.
#
#   ./venv/bin/python -m unittest test.test_mmu_gate_color_rgb
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class TestValidateGateStatusUpdatesRgb(unittest.TestCase):

    GATE = 1

    def test_gate_validated_empty_clears_rgb(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        gm = hh.mmu.gate_maps
        entry = hh.sensor('mmu_entry_%d' % self.GATE).sensor.runout_helper
        entry.filament_present = True
        hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=1 COLOR=abcdef QUIET=1' % self.GATE)
        self.assertNotEqual(gm.gate_color_rgb[self.GATE], (0., 0., 0.))

        entry.filament_present = False
        gm.validate_gate_status([self.GATE])

        self.assertEqual(gm.gate_color[self.GATE], '')
        self.assertEqual(gm.gate_color_rgb[self.GATE], (0., 0., 0.))


if __name__ == '__main__':
    unittest.main()
