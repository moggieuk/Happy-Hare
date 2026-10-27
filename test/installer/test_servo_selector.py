# Generic ServoSelector Kconfig default tests.

import unittest

from test.hh import cfg, profiles


class TestServoSelectorGateAngleDefaults(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.Profile(
            "custom_servo_selector",
            syms={
                "MMU_CUSTOM": True,
                "CHOICE_SELECTOR_TYPE_SERVO_SELECTOR": True,
            },
        )
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(cls.profile.name, cls.profile.syms)

    def assert_defaults(self, maximum, expected):
        maximum_sym = self.kconfig.syms["PARAM_SERVO_MAX_ANGLE"]
        gates_sym = self.kconfig.syms["PARAM_NUM_GATES"]
        angles_sym = self.kconfig.syms["PARAM_SERVO_GATE_ANGLES"]

        maximum_sym.set_value(str(maximum))
        for gates, angles in expected.items():
            with self.subTest(maximum=maximum, gates=gates):
                gates_sym.set_value(str(gates))
                self.assertEqual(angles_sym.str_value, angles)
                self.assertFalse(self.kconfig.syms["W20"].tri_value)

    def test_180_degree_selector_uses_adjusted_defaults(self):
        self.assert_defaults(180, {
            2: "0, 180",
            3: "0, 90, 180",
            4: "0, 60, 120, 180",
            5: "0, 45, 90, 135, 180",
            6: "0, 36, 72, 108, 144, 180",
        })

    def test_other_selector_maximum_keeps_historical_defaults(self):
        self.assert_defaults(360, {
            2: "0, 180",
            3: "0, 120, 240",
            4: "0, 90, 180, 270",
            5: "0, 72, 144, 216, 288",
            6: "0, 60, 120, 180, 240, 300",
        })

    def test_release_gates_default_to_the_next_gate(self):
        self.kconfig.syms["PARAM_NUM_GATES"].set_value("4")
        self.assertEqual(self.kconfig.syms["PARAM_SELECTOR_RELEASE_GATES"].str_value, "1, 2, 3, 0")


class TestPicoMmuSyncDefaults(unittest.TestCase):

    SYNC_SYMS = ("PARAM_SYNC_TO_EXTRUDER", "PARAM_SYNC_FORM_TIP", "PARAM_SYNC_PURGE")

    def kconfig(self, name, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig(name, {"MMU_TYPE_PICO_MMU_1_0": True, **syms})

    def test_sync_follows_filament_always_gripped(self):
        gripped = self.kconfig("pico_mmu_gripped", {})
        self.assertEqual(gripped.get("PARAM_FILAMENT_ALWAYS_GRIPPED"), "1")
        for sym in self.SYNC_SYMS:
            self.assertEqual(gripped.get(sym), "1", sym)

        released = self.kconfig("pico_mmu_released", {"PARAM_FILAMENT_ALWAYS_GRIPPED": False})
        for sym in self.SYNC_SYMS:
            self.assertEqual(released.get(sym), "0", sym)
            self.assertGreater(released.syms[sym].visibility, 0, sym)


if __name__ == "__main__":
    unittest.main()
