# MMX and MMX6 Kconfig design-attribute tests.

import unittest

from test.hh import cfg


class TestMmxDesignAttributes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.mmx = cfg._kconfig(
                "mmx_design_attributes",
                {"MMU_TYPE_MMX_1_0": True},
            )
            cls.mmx6 = cfg._kconfig(
                "mmx6_design_attributes",
                {"MMU_TYPE_MMX6_1_0": True},
            )

    def assert_variable_rotation_distances_recommended(
            self, kconfig, mmu_type):
        self.assertFalse(kconfig.is_enabled(
            "UNSELECT_VARIABLE_ROTATION_DISTANCES"))
        variable = kconfig.syms["PARAM_VARIABLE_ROTATION_DISTANCES"]
        self.assertEqual(variable.str_value, "1")
        self.assertGreater(variable.visibility, 0)

        with cfg._env(cfg._SINGLE_UNIT_ENV):
            fixed = cfg._kconfig(
                "%s_fixed_rotation_distance" % mmu_type.lower(),
                {
                    mmu_type: True,
                    "PARAM_VARIABLE_ROTATION_DISTANCES": False,
                },
            )
        self.assertEqual(fixed.get("PARAM_VARIABLE_ROTATION_DISTANCES"), "0")
        self.assertGreater(
            fixed.syms["PARAM_VARIABLE_ROTATION_DISTANCES"].visibility, 0)

    def test_mmx_recommends_variable_rotation_distances(self):
        self.assert_variable_rotation_distances_recommended(
            self.mmx, "MMU_TYPE_MMX_1_0")

    def test_mmx6_recommends_variable_rotation_distances(self):
        self.assert_variable_rotation_distances_recommended(
            self.mmx6, "MMU_TYPE_MMX6_1_0")

    def test_mmx6_recommends_filament_always_gripped_without_forcing_it(self):
        self.assertEqual(
            self.mmx6.get("PARAM_FILAMENT_ALWAYS_GRIPPED"), "1")

        with cfg._env(cfg._SINGLE_UNIT_ENV):
            released = cfg._kconfig(
                "mmx6_releasable_filament",
                {
                    "MMU_TYPE_MMX6_1_0": True,
                    "PARAM_FILAMENT_ALWAYS_GRIPPED": False,
                },
            )
        self.assertEqual(released.get("PARAM_FILAMENT_ALWAYS_GRIPPED"), "0")
        self.assertGreater(
            released.syms["PARAM_FILAMENT_ALWAYS_GRIPPED"].visibility, 0)

    def test_mmx_does_not_force_bowden_move(self):
        bowden = self.mmx.syms["PARAM_REQUIRE_BOWDEN_MOVE"]
        self.assertEqual(bowden.str_value, "0")
        self.assertGreater(bowden.visibility, 0)

        bowden.set_value("1")
        self.assertEqual(bowden.str_value, "1")


if __name__ == "__main__":
    unittest.main()
