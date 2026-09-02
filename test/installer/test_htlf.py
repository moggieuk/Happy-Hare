# HTLF Kconfig profile tests.

import unittest

from extras.mmu.mmu_constants import VENDORS
from test.hh import cfg, profiles


class TestHtlfProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.Profile(
            "htlf",
            syms={"MMU_TYPE_HTLF_1_0": True},
        )
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(cls.profile.name, cls.profile.syms)

    def test_machine_and_motion_defaults(self):
        expected = {
            "PARAM_VENDOR": "HTLF",
            "PARAM_VERSION": "1.0",
            "PARAM_NUM_GATES": "4",
            "PARAM_SELECTOR_TYPE": "RotarySelector",
            "PARAM_GEAR_GEAR_RATIO": "1:1",
            "PARAM_GEAR_ROTATION_DISTANCE": "4.65",
            "PARAM_GEAR_RUN_CURRENT": "0.8",
            "PARAM_GEAR_HOLD_CURRENT": "0.1",
            "PARAM_GEAR_MICROSTEPS": "16",
            "PARAM_SELECTOR_GEAR_RATIO": "1:1",
            "PARAM_SELECTOR_ROTATION_DISTANCE": "32",
            "PARAM_SELECTOR_RUN_CURRENT": "0.6",
            "PARAM_SELECTOR_HOLD_CURRENT": "0.3",
            "PARAM_SELECTOR_RELEASE_GATES": "1, 2, 3, 0",
            "PARAM_GEAR_LOAD_SPEED": "135",
            "PARAM_GEAR_SHORT_MOVE_SPEED": "80",
            "PARAM_SELECTOR_MOVE_SPEED": "50",
            "PARAM_SELECTOR_ACCEL": "50",
            "PARAM_EXTRUDER_LOAD_SPEED": "16",
            "PARAM_GATE_PARKING_DISTANCE": "-25",
            "PARAM_GATE_HOMING_MAX": "1000",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

        self.assertTrue(self.kconfig.is_enabled("PARAM_VARIABLE_ROTATION_DISTANCES"))
        self.assertTrue(self.kconfig.is_enabled("PARAM_FILAMENT_ALWAYS_GRIPPED"))

    def test_vendor_is_accepted_by_runtime_config(self):
        self.assertIn(self.kconfig.get("PARAM_VENDOR"), VENDORS)


if __name__ == "__main__":
    unittest.main()
