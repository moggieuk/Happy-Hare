# LowRider Kconfig profile tests.

import unittest

from test.hh import cfg, profiles


class TestLowRiderProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.Profile(
            "low_rider",
            syms={"MMU_TYPE_LOW_RIDER_1_0": True},
        )
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(cls.profile.name, cls.profile.syms)

    def test_machine_and_motion_defaults(self):
        expected = {
            "PARAM_VENDOR": "LowRider",
            "PARAM_VERSION": "1.0",
            "PARAM_NUM_GATES": "6",
            "PARAM_SELECTOR_TYPE": "ServoSelector",
            "PARAM_VARIABLE_ROTATION_DISTANCES": "1",
            "PARAM_REQUIRE_BOWDEN_MOVE": "0",
            "PARAM_FILAMENT_ALWAYS_GRIPPED": "1",
            "PARAM_GATE_HOMING_ENDSTOP": "extruder",
            "PARAM_GEAR_ROTATION_DISTANCE": "23",
            "PARAM_GEAR_GEAR_RATIO": "80:20",
            "PARAM_GEAR_FULL_STEPS_PER_ROTATION": "200",
            "PARAM_SERVO_DURATION": "0.6",
            "PARAM_SERVO_DWELL": "1.0",
            "PARAM_SERVO_ALWAYS_ACTIVE": "0",
            "PARAM_SERVO_GATE_ANGLES": "30, 60, 90, 120, 150, 180",
            "PARAM_SERVO_BYPASS_ANGLE": "-1",
            "PARAM_SERVO_RELEASE_ANGLE": "0",
            "PARAM_SERVO_MAX_ANGLE": "180",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

        self.assertTrue(self.kconfig.is_enabled("SERVO_TYPE_DM996"))
        self.assertTrue(self.kconfig.is_enabled("MMU_HAS_SENSOR_EXTRUDER"))
        self.assertFalse(self.kconfig.is_enabled("UNSELECT_VARIABLE_ROTATION_DISTANCES"))
        self.assertGreater(
            self.kconfig.syms["PARAM_VARIABLE_ROTATION_DISTANCES"].visibility, 0)
        angles = self.kconfig.syms["PARAM_SERVO_GATE_ANGLES"]
        self.assertEqual(angles.array_editor, ",")
        self.assertIs(angles.array_size_sym, self.kconfig.syms["PARAM_NUM_GATES"])
        self.assertFalse(self.kconfig.is_enabled("W20"))

    def test_gate_count_is_visible_and_can_be_overridden(self):
        self.assertGreater(self.kconfig.syms["PARAM_NUM_GATES"].visibility, 0)

        with cfg._env(cfg._SINGLE_UNIT_ENV):
            customized = cfg._kconfig(
                "low_rider_four_gate",
                {
                    "MMU_TYPE_LOW_RIDER_1_0": True,
                    "PARAM_NUM_GATES": 4,
                },
            )
        self.assertEqual(customized.get("PARAM_NUM_GATES"), "4")
        self.assertEqual(
            customized.get("PARAM_SERVO_GATE_ANGLES"), "45, 90, 135, 180")
        self.assertFalse(customized.is_enabled("W20"))

        customized.syms["PARAM_SERVO_GATE_ANGLES"].set_value(
            "26,58,90,118,147,180")
        self.assertTrue(customized.is_enabled("W20"))

        customized.syms["PARAM_SERVO_GATE_ANGLES"].set_value("26,58,90,118")
        self.assertFalse(customized.is_enabled("W20"))

    def test_gate_defaults_reserve_zero_for_release(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig(
                "low_rider_gate_angle_defaults",
                {"MMU_TYPE_LOW_RIDER_1_0": True},
            )

        expected = {
            2: "90, 180",
            3: "60, 120, 180",
            4: "45, 90, 135, 180",
            5: "36, 72, 108, 144, 180",
            6: "30, 60, 90, 120, 150, 180",
        }
        gates = kconfig.syms["PARAM_NUM_GATES"]
        for count, angles in expected.items():
            with self.subTest(gates=count):
                gates.set_value(str(count))
                self.assertEqual(
                    kconfig.get("PARAM_SERVO_GATE_ANGLES"), angles)
                self.assertEqual(kconfig.get("PARAM_SERVO_RELEASE_ANGLE"), "0")
                self.assertFalse(kconfig.is_enabled("W20"))

    def test_empty_gate_angles_remain_a_valid_calibration_sentinel(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            empty = cfg._kconfig(
                "low_rider_empty_angles",
                {
                    "MMU_TYPE_LOW_RIDER_1_0": True,
                    "PARAM_SERVO_GATE_ANGLES": "",
                },
            )
        self.assertFalse(empty.is_enabled("W20"))

    def test_filament_always_gripped_is_recommended_not_forced(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            released = cfg._kconfig(
                "low_rider_releasable_filament",
                {
                    "MMU_TYPE_LOW_RIDER_1_0": True,
                    "PARAM_FILAMENT_ALWAYS_GRIPPED": False,
                },
            )
        self.assertEqual(released.get("PARAM_FILAMENT_ALWAYS_GRIPPED"), "0")
        self.assertGreater(
            released.syms["PARAM_FILAMENT_ALWAYS_GRIPPED"].visibility, 0)

    def test_variable_rotation_distances_is_recommended_not_forced(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            fixed = cfg._kconfig(
                "low_rider_fixed_rotation_distance",
                {
                    "MMU_TYPE_LOW_RIDER_1_0": True,
                    "PARAM_VARIABLE_ROTATION_DISTANCES": False,
                },
            )
        self.assertEqual(fixed.get("PARAM_VARIABLE_ROTATION_DISTANCES"), "0")
        self.assertGreater(
            fixed.syms["PARAM_VARIABLE_ROTATION_DISTANCES"].visibility, 0)


if __name__ == "__main__":
    unittest.main()
