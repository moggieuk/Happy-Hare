# HTLF selector rotation_distance 32 -> 360 upgrade.
#
# HTLF's selector CAD geometry is now rendered in cam degrees with rotation_distance 360. A
# default (refresh) reinstall keeps the user's existing rotation_distance but takes the new
# CAD lines, so without this step gate 0 would sit 215mm (nearly seven cam turns) from home.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import tempfile
import unittest

from installer.build import HHConfig
from installer.upgrades import Upgrades


INSTALLED_4_0 = """
[mmu_unit unit0]
vendor                   : HTLF
version                  : 1.0
selector_stepper         : unit0_selector

[mmu_stepper unit0_selector]
rotation_distance        : 32
gear_ratio               : 1:1

[mmu_unit_parameters unit0]
selector_move_speed      : 50			# mm/s speed of selector movement
selector_homing_speed    : 60
selector_accel           : 50
cad_gate0_pos            : 15
cad_gate_width           : 15

[mmu_unit unit1]
vendor                   : 3DChameleon
version                  : 1.0
selector_stepper         : unit1_selector

[mmu_stepper unit1_selector]
rotation_distance        : 32

[mmu_unit_parameters unit1]
selector_move_speed      : 50
cad_gate_width           : 25
"""


class TestHtlfSelectorUpgrade(unittest.TestCase):

    def load(self, text):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "mmu_hardware.cfg")
        with open(path, "w") as f:
            f.write(text)
        return HHConfig([path])

    def upgraded(self, text=INSTALLED_4_0):
        cfg = self.load(text)
        with self.assertLogs(level="WARNING"):
            Upgrades().upgrade(cfg, (4, 0), (4, 1))
        return cfg

    def test_selector_moves_to_cam_degrees(self):
        cfg = self.upgraded()
        self.assertEqual(cfg.get("mmu_stepper unit0_selector", "rotation_distance"), "360")

    def test_old_default_speeds_are_dropped_for_the_new_defaults(self):
        cfg = self.upgraded()
        params = "mmu_unit_parameters unit0"
        for option in ("selector_move_speed", "selector_homing_speed", "selector_accel"):
            self.assertFalse(cfg.has_option(params, option), option)

    def test_tuned_speeds_keep_their_physical_speed(self):
        """Scaled by 360/32 then rounded to the nearest 50, as the fresh-install defaults are."""
        text = (INSTALLED_4_0
                .replace("selector_move_speed      : 50", "selector_move_speed      : 40")
                .replace("selector_homing_speed    : 60", "selector_homing_speed    : 30"))
        cfg = self.upgraded(text)
        params = "mmu_unit_parameters unit0"
        self.assertEqual(cfg.get(params, "selector_move_speed"), "450")
        self.assertEqual(cfg.get(params, "selector_homing_speed"), "350")
        self.assertFalse(cfg.has_option(params, "selector_accel"))

    def test_hand_set_mm_geometry_is_dropped(self):
        cfg = self.upgraded()
        self.assertFalse(cfg.has_option("mmu_unit_parameters unit0", "cad_gate0_pos"))
        self.assertFalse(cfg.has_option("mmu_unit_parameters unit0", "cad_gate_width"))

    def test_other_rotary_units_are_untouched(self):
        cfg = self.upgraded()
        self.assertEqual(cfg.get("mmu_stepper unit1_selector", "rotation_distance"), "32")
        self.assertEqual(cfg.get("mmu_unit_parameters unit1", "selector_move_speed"), "50")
        self.assertEqual(cfg.get("mmu_unit_parameters unit1", "cad_gate_width"), "25")

    def test_an_already_converted_unit_is_left_alone(self):
        text = INSTALLED_4_0.replace("rotation_distance        : 32\ngear_ratio",
                                     "rotation_distance        : 360\ngear_ratio")
        cfg = self.load(text)
        Upgrades().upgrade(cfg, (4, 0), (4, 1))
        self.assertEqual(cfg.get("mmu_unit_parameters unit0", "selector_move_speed"), "50")
        self.assertEqual(cfg.get("mmu_unit_parameters unit0", "cad_gate0_pos"), "15")


if __name__ == "__main__":
    unittest.main()
