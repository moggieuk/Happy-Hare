"""Filament catchment defaults, menu placement, and hardware exclusions."""

import unittest

from test.hh import cfg


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


if __name__ == "__main__":
    unittest.main()
