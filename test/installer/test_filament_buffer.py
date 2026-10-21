"""Filament catchment defaults, menu placement, and hardware exclusions."""

import unittest

from test.hh import cfg, profiles, session

PARAMETERS = "config/base/mmu_parameters.cfg"


class TestFilamentBuffer(unittest.TestCase):

    def config(self, design):
        syms = {design: True}
        if design.startswith("MMU_TYPE_ERCF_"):
            syms = {"MMU_FAMILY_ERCF": True, **syms}
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig("filament_buffer", syms)

    def test_defaults_are_editable_in_design_attributes(self):
        for design, default in (
            ("MMU_TYPE_ERCF_1_1", "y"),
            ("MMU_TYPE_ERCF_2_0", "y"),
            ("MMU_TYPE_ERCF_3_0", "y"),
            ("MMU_TYPE_TRADRACK_1_0", "y"),
            ("MMU_CUSTOM", "n"),
            ("MMU_TYPE_LOW_RIDER_1_0", "n"),
        ):
            with self.subTest(design=design):
                kc = self.config(design)
                buffer = kc.syms["MMU_HAS_FILAMENT_BUFFER"]
                self.assertEqual(buffer.str_value, default)
                self.assertGreater(buffer.visibility, 0)
                from kconfiglib import expr_value
                visible_nodes = [node for node in buffer.nodes
                                 if node.prompt and expr_value(node.prompt[1])]
                self.assertEqual(len(visible_nodes), 1)
                self.assertEqual(visible_nodes[0].parent.prompt[0],
                                 "Design attributes")
                for value in (0, 2):
                    buffer.set_value(value)
                    self.assertEqual(buffer.tri_value, value)

                # Enabling an optional eSpooler must override even a saved yes.
                kc.syms["MMU_HAS_ESPOOLER"].set_value(2)
                self.assertTrue(kc.is_enabled("MMU_HAS_ESPOOLER"))
                self.assertTrue(kc.is_enabled("UNSELECT_MMU_HAS_FILAMENT_BUFFER"))
                self.assertEqual(buffer.visibility, 0)
                self.assertEqual(buffer.str_value, "n")
                kc.syms["MMU_HAS_ESPOOLER"].set_value(0)
                self.assertGreater(buffer.visibility, 0)
                self.assertEqual(buffer.str_value, "y")

    def test_excluded_designs_reject_saved_buffer_setting(self):
        for design in ("MMU_TYPE_EMU_1_0", "MMU_TYPE_VVD_1_0",
                       "MMU_TYPE_BOX_TURTLE_1_0", "MMU_TYPE_KMS_1_0"):
            with self.subTest(design=design):
                kc = self.config(design)
                buffer = kc.syms["MMU_HAS_FILAMENT_BUFFER"]
                buffer.set_value(2)
                self.assertTrue(kc.is_enabled("UNSELECT_MMU_HAS_FILAMENT_BUFFER"))
                self.assertEqual(buffer.visibility, 0)
                self.assertEqual(buffer.str_value, "n")


class TestFilamentBufferRender(unittest.TestCase):
    """The menuconfig choice must reach [mmu_unit], where the runtime reads it."""

    ERCF = {"MMU_FAMILY_ERCF": True, "MMU_TYPE_ERCF_2_0": True}

    def render(self, name, syms):
        return cfg.render(profiles.Profile("filament_buffer_" + name, syms=syms))

    def rendered(self, name, syms, unit="unit0"):
        out = self.render(name, syms)
        unit_section = dict(cfg.assemble(out).items("mmu_unit " + unit))
        return unit_section.get("filament_buffer"), out[PARAMETERS]

    def test_rendered_value_follows_the_menuconfig_choice(self):
        for name, syms, expected in (
            ("ercf_default", self.ERCF, "1"),
            ("tradrack_default", {"MMU_TYPE_TRADRACK_1_0": True}, "1"),
            ("ercf_unticked", {**self.ERCF, "MMU_HAS_FILAMENT_BUFFER": False}, "0"),
            ("custom_default", {"MMU_CUSTOM": True}, "0"),
            ("custom_ticked", {"MMU_CUSTOM": True, "MMU_HAS_FILAMENT_BUFFER": True}, "1"),
            ("box_turtle", {"MMU_TYPE_BOX_TURTLE_1_0": True}, "0"),
        ):
            with self.subTest(name):
                value, params = self.rendered(name, syms)
                self.assertEqual(value, expected)
                self.assertEqual("gear_from_filament_buffer_speed" in params, expected == "1")

    def test_espooler_forces_the_buffer_off(self):
        value, params = self.rendered("ercf_espooler", {**self.ERCF, "MMU_HAS_ESPOOLER": True})
        self.assertEqual(value, "0")
        self.assertNotIn("gear_from_filament_buffer_speed", params)

    def test_espooler_forces_the_buffer_off_over_a_stale_tick(self):
        value, _ = self.rendered("ercf_espooler_stale", {
            **self.ERCF, "MMU_HAS_ESPOOLER": True, "MMU_HAS_FILAMENT_BUFFER": True})
        self.assertEqual(value, "0")

    def test_multi_unit_renders_each_unit_its_own_value(self):
        parser = cfg.assemble(cfg.render(profiles.get("ercf_vvd")))
        self.assertEqual(parser.get("mmu_unit unit0", "filament_buffer"), "1")
        self.assertEqual(parser.get("mmu_unit unit1", "filament_buffer"), "0")


class TestFilamentBufferRuntime(unittest.TestCase):

    def boot(self, base, syms):
        hh = session(profiles.get(base).derive(base + "_filament_buffer", syms=syms))
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])
        return hh.mmu.mmu_unit(0)

    def test_unticking_turns_off_a_vendor_default_buffer(self):
        self.assertFalse(self.boot("tradrack", {"MMU_HAS_FILAMENT_BUFFER": False}).has_filament_buffer())

    def test_ticking_turns_on_a_buffer_the_vendor_lacks(self):
        unit = self.boot("mmx", {"MMU_HAS_FILAMENT_BUFFER": True,
                                 "PARAM_GEAR_FROM_FILAMENT_BUFFER_SPEED": "222"})
        self.assertTrue(unit.has_filament_buffer())
        self.assertEqual(unit.p.gear_from_filament_buffer_speed, 222.)


if __name__ == "__main__":
    unittest.main()
