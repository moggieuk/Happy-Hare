# Happy Hare test harness - extra purge volume when the material family changes.
#
# purge_material_change_volume is added to the suggested toolchange purge volume when the
# material last loaded and purged into the extruder and the incoming gate's material are
# different families ("PLA Silk" is still PLA), or only one of them is known.
# purge_change_min_volume is a floor on the whole suggested purge volume.
#
#   ./venv/bin/python -m unittest test.test_mmu_material_purge
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0
MIN_VOLUME_LENGTH = 100 / (0.875 ** 2 * 3.1415) # purge_change_min_volume=100 of 1.75mm filament


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

    def material_volume_during_purge(self, from_material, to_material, fail_first=None,
                                     key='toolchange_material_purge_volume'):
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL="%s" COLOR=000000' % from_material)
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 MATERIAL="%s" COLOR=000000' % to_material)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')

        seen = []
        effects = self.hh.printer.harness_macro_effects
        purge_macro = self.hh.mmu.p.purge_macro
        fail_macro = fail_first and getattr(self.hh.mmu.p, fail_first)

        def wrap(macro, record):
            original = effects.get(macro)
            calls = []

            def effect(m, gcmd):
                calls.append(1)
                if record:
                    seen.append(self.hh.mmu.get_status(0)[key])
                if macro == fail_macro and len(calls) == 1:
                    raise gcmd.error("simulated failure")
                if original is not None:
                    original(m, gcmd)
            effects[macro] = effect

        wrap(purge_macro, True)
        if fail_macro and fail_macro != purge_macro:
            wrap(fail_macro, False)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=1%s' % (' STANDALONE=1' if fail_first else ''))
        if not fail_first:
            self.assertEqual(self.hh.errors, [])
        return seen

    def test_material_change_adds_extra_volume(self):
        self.assertEqual(self.material_volume_during_purge('ABS', 'PLA'), [150.0])

    def test_same_family_adds_nothing(self):
        self.assertEqual(self.material_volume_during_purge('PLA', 'PLA Silk'), [0.0])

    def test_change_to_unknown_material_adds_extra_volume(self):
        self.assertEqual(self.material_volume_during_purge('PLA', ''), [150.0])

    def test_minimum_purge_applies_to_an_identical_filament(self):
        self.hh.run_gcode('MMU_TEST_CONFIG purge_change_min_volume=100')
        self.assertEqual(self.material_volume_during_purge('PLA', 'PLA', key='toolchange_purge_volume'), [100.0])

    def test_retry_after_failed_purge_keeps_extra_volume(self):
        self.hh.run_gcode('MMU_TEST_CONFIG retry_tool_change_on_error=1')
        self.assertEqual(self.material_volume_during_purge('ABS', 'PLA', fail_first='purge_macro'), [150.0, 150.0])

    def test_retry_after_failed_post_load_keeps_extra_volume(self):
        self.hh.run_gcode('MMU_TEST_CONFIG retry_tool_change_on_error=1')
        self.assertEqual(self.material_volume_during_purge('ABS', 'PLA', fail_first='post_load_macro'), [150.0, 150.0])


class ReferencePurgeMacroTestCase(unittest.TestCase):
    """The shipped _MMU_PURGE body extrudes the suggested volume whatever the gcode state"""

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.addCleanup(self.hh.close)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_TEST_CONFIG purge_macro=_MMU_PURGE purge_change_min_volume=100')
        # The harness publishes no raw config and the macro reads the diameter from it
        self.hh.printer.lookup_object('configfile').status_raw_config = {'extruder': {'filament_diameter': '1.75'}}

    def purged_on_load(self, setup=''):
        toolhead = self.hh.printer.lookup_object('toolhead')
        gcode_move = self.hh.printer.lookup_object('gcode_move')
        effects = self.hh.printer.harness_macro_effects
        purged = []

        def purge(macro, gcmd):
            before = toolhead.get_position()[3]
            macro.run_body(gcmd)
            purged.append(toolhead.get_position()[3] - before)

        effects['_MMU_PURGE'] = purge
        effects['__MMU_PURGE_SEGMENT'] = lambda macro, gcmd: macro.run_body(gcmd)
        if setup:
            self.hh.run_gcode(setup)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(len(purged), 1)
        return purged[0]

    def test_purge_ignores_absolute_extrusion_and_extrude_factor(self):
        gcode_move = self.hh.printer.lookup_object('gcode_move')
        self.assertAlmostEqual(self.purged_on_load('M82\nM221 S150'), MIN_VOLUME_LENGTH, places=3)
        status = gcode_move.get_status(0)
        self.assertTrue(status['absolute_extrude'])
        self.assertAlmostEqual(status['extrude_factor'], 1.5)

    def test_extra_purge_length_is_added(self):
        self.assertEqual(self.hh.printer.lookup_object('gcode_macro _MMU_PURGE_VARS').variables['extra_purge_length'], 0)
        setup = 'SET_GCODE_VARIABLE MACRO=_MMU_PURGE_VARS VARIABLE=extra_purge_length VALUE=7.5'
        self.assertAlmostEqual(self.purged_on_load(setup), MIN_VOLUME_LENGTH + 7.5, places=3)


if __name__ == '__main__':
    unittest.main()
