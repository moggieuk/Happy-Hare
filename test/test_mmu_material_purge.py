# Happy Hare test harness - extra purge volume when the material family changes.
#
# purge_material_change_volume is added to the suggested toolchange purge volume when the
# material recorded for the filament in the extruder and the incoming gate's material are
# different families ("PLA Silk" is still PLA).
#
#   ./venv/bin/python -m unittest test.test_mmu_material_purge
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0


class MaterialPurgeTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        for gate in (0, 1):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_TEST_CONFIG purge_material_change_volume=150')

    def tearDown(self):
        self.hh.close()

    def material_volume_during_purge(self, from_material, to_material):
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL="%s" COLOR=000000' % from_material)
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 MATERIAL="%s" COLOR=000000' % to_material)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')

        seen = []
        effects = self.hh.printer.harness_macro_effects
        macro = self.hh.mmu.p.purge_macro
        original = effects.get(macro)

        def effect(m, gcmd):
            seen.append(self.hh.mmu.get_status(0)['toolchange_material_purge_volume'])
            if original is not None:
                original(m, gcmd)

        effects[macro] = effect
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1')
        self.assertEqual(self.hh.errors, [])
        return seen

    def test_material_change_adds_extra_volume(self):
        self.assertEqual(self.material_volume_during_purge('ABS', 'PLA'), [150.0])

    def test_same_family_adds_nothing(self):
        self.assertEqual(self.material_volume_during_purge('PLA', 'PLA Silk'), [0.0])


if __name__ == '__main__':
    unittest.main()
