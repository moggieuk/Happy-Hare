# Happy Hare test harness - 'active_filament' describes the selected gate's filament.
#
# It used to be a snapshot taken at selection: gate-map edits never reached it, and
# selecting the bypass wiped it, so bypass metadata (which has no gate-map row) was lost
# whenever the bypass was re-selected - including on every restart (#1192).
#
#   ./venv/bin/python -m unittest test.test_mmu_active_filament
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class ActiveFilamentTestCase(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu

    def tearDown(self):
        self.hh.close()

    def test_a_selected_gate_follows_gate_map_edits(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=2 MATERIAL=PETG QUIET=1')
        self.mmu.select_gate(2)
        self.assertEqual(self.mmu.active_filament['material'], 'PETG')
        self.hh.run_gcode('MMU_GATE_MAP GATE=2 MATERIAL=ABS QUIET=1')
        self.assertEqual(self.mmu.active_filament['material'], 'ABS')

    def test_bypass_filament_survives_a_restart(self):
        self.mmu.select_bypass()
        self.hh.run_gcode('MMU_GATE_MAP BYPASS=1 SPOOLID=21 NAME="ASA Carbon" '
                          'MATERIAL=ASA-CF VENDOR=Acme COLOR=000000 QUIET=1')
        self.hh.run_gcode('MMU ENABLE=0')
        self.hh.run_gcode('MMU ENABLE=1')
        self.hh.reactor.advance(5.)
        active = self.mmu.get_status(0.)['active_filament']
        self.assertEqual(active['spool_id'], 21)
        self.assertEqual(active['material'], 'ASA-CF')


if __name__ == '__main__':
    unittest.main()
