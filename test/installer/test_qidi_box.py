# QIDI Box Kconfig profile and rendering tests.

import re
import unittest

from test.hh import cfg, profiles


class TestQidiBoxProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.Profile(
            "qidi_box",
            syms={"MMU_TYPE_QIDI_BOX_1_0": True},
        )
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(cls.profile.name, cls.profile.syms)
            cls.other_kconfig = cfg._kconfig(
                "non-qidi-driver-check",
                {"MMU_TYPE_BOX_TURTLE_1_0": True},
            )
        cls.rendered = cfg.render(cls.profile)

    def test_machine_and_motion_defaults(self):
        expected = {
            "PARAM_VENDOR": "QIDI",
            "PARAM_VERSION": "1.0",
            "PARAM_NUM_GATES": "4",
            "PARAM_BOARD_TYPE": "QIDI Box v2 (STM32F401xC)",
            "PARAM_SELECTOR_TYPE": "VirtualSelector",
            "PARAM_VARIABLE_BOWDEN_LENGTHS": "0",
            "PARAM_VARIABLE_ROTATION_DISTANCES": "0",
            "PARAM_REQUIRE_BOWDEN_MOVE": "1",
            "PARAM_FILAMENT_ALWAYS_GRIPPED": "1",
            "PARAM_HAS_BYPASS": "0",
            "PARAM_GEAR_ROTATION_DISTANCE": "13.6",
            "PARAM_GEAR_MICROSTEPS": "16",
            "PARAM_GEAR_GEAR_RATIO": "1:1",
            "PARAM_TOOLHEAD_TYPE": "QIDI Q2 Extruder",
            "PARAM_GATE_HOMING_ENDSTOP": "mmu_shared_exit",
            "PARAM_GATE_HOMING_MAX": "1500",
            "PARAM_GATE_PRELOAD_ENDSTOP": "none",
            "PARAM_GATE_PRELOAD_HOMING_MAX": "200",
            "PARAM_GATE_PRELOAD_ATTEMPTS": "1",
            "PARAM_GATE_PARKING_DISTANCE": "-80",
            "PARAM_GATE_FINAL_EJECT_DISTANCE": "1500",
            "PARAM_EXTRUDER_HOMING_ENDSTOP": "extruder",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

        self.assertTrue(self.kconfig.is_enabled("BOARD_TYPE_QIDI_BOX_2_0"))
        self.assertFalse(self.kconfig.is_enabled("BOARD_TYPE_OTHER"))
        self.assertTrue(
            self.kconfig.is_enabled("TOOLHEAD_TYPE_QIDI_Q2"))
        self.assertFalse(self.kconfig.is_enabled("CHOICE_TOOLHEAD_TYPE_OTHER"))

    def test_path_variation_and_bypass_defaults_can_be_overridden(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            customized = cfg._kconfig(
                "qidi_box_customized",
                {
                    "MMU_TYPE_QIDI_BOX_1_0": True,
                    "PARAM_VARIABLE_BOWDEN_LENGTHS": True,
                    "PARAM_VARIABLE_ROTATION_DISTANCES": True,
                    "PARAM_HAS_BYPASS": True,
                },
            )
        self.assertEqual(customized.get("PARAM_VARIABLE_BOWDEN_LENGTHS"), "1")
        self.assertEqual(customized.get("PARAM_VARIABLE_ROTATION_DISTANCES"), "1")
        self.assertEqual(customized.get("PARAM_HAS_BYPASS"), "1")

    def test_dryer_capabilities_are_recommended_but_can_be_disabled(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            without_dryer = cfg._kconfig(
                "qidi_box_without_dryer",
                {
                    "MMU_TYPE_QIDI_BOX_1_0": True,
                    "MMU_HAS_ENVIRONMENT_SENSOR": False,
                    "MMU_HAS_HEATER": False,
                },
            )
        self.assertFalse(without_dryer.is_enabled("MMU_HAS_ENVIRONMENT_SENSOR"))
        self.assertFalse(without_dryer.is_enabled("MMU_HAS_HEATER"))

    def test_stock_sensors_buffer_and_standard_dryer_setup_are_enabled(self):
        for symbol in (
            "MMU_HAS_SENSOR_ENTRY",
            "MMU_HAS_SENSOR_SHARED_EXIT",
            "MMU_HAS_SENSOR_EXTRUDER",
            "MMU_HAS_SYNC_FEEDBACK_BUFFER",
            "MMU_HAS_SENSOR_BUFFER_TENSION",
            "MMU_HAS_ENVIRONMENT_SENSOR",
            "MMU_HAS_HEATER",
        ):
            with self.subTest(symbol=symbol):
                self.assertTrue(self.kconfig.is_enabled(symbol))
        self.assertFalse(self.kconfig.is_enabled("MMU_HAS_SENSOR_EXIT"))
        self.assertFalse(self.kconfig.is_enabled("CUSTOM_ENVIRONMENT_SENSOR_SETUP"))
        self.assertFalse(self.kconfig.is_enabled("CUSTOM_HEATER_SETUP"))
        self.assertGreater(self.kconfig.syms["PARAM_ENVIRONMENT_SENSOR"].visibility, 0)
        self.assertGreater(self.kconfig.syms["PARAM_FILAMENT_HEATER"].visibility, 0)

        hardware = self.rendered["config/base/mmu_hardware.cfg"]
        self.assertNotIn("mmu_exit_switch_pin_0", hardware)
        self.assertIn("environment_sensor       : unit0_Env", hardware)
        self.assertIn("filament_heater          : \n", hardware)
        self.assertIn("[temperature_sensor unit0_Env]", hardware)
        self.assertNotIn("max_concurrent_heaters", hardware)
        self.assertIn("buffer_range            : 8", hardware)
        self.assertIn("buffer_maxrange         : 12", hardware)

        parameters = self.rendered["config/base/mmu_parameters.cfg"]
        self.assertIn("gate_preload_endstop          : none", parameters)
        self.assertIn("gate_preload_homing_max       : 200", parameters)
        self.assertIn("sync_feedback_enabled           : 1", parameters)
        self.assertIn("sync_feedback_speed_multiplier  : 5", parameters)
        self.assertIn("gear_load_speed             : 120", parameters)

    def test_hardware_controlled_drivers_do_not_render_tmc_sections(self):
        self.assertEqual(self.kconfig.get("PARAM_GEAR_TMC"), "")
        self.assertEqual(self.other_kconfig.get("PARAM_GEAR_TMC"), "tmc2209")
        self.assertTrue(self.kconfig.is_enabled("CHOICE_GEAR_TMC_NONE"))
        self.assertFalse(self.other_kconfig.is_enabled("CHOICE_GEAR_TMC_NONE"))
        self.assertGreater(
            self.kconfig.named_choices["CHOICE_GEAR_TMC"].visibility, 0)
        for symbol in ("PIN_GEAR_UART", "PIN_GEAR_DIAG",
                       "PIN_GEAR_UART_1", "PIN_GEAR_DIAG_1"):
            with self.subTest(hidden_prompt=symbol):
                self.assertEqual(self.kconfig.syms[symbol].visibility, 0)
        self.assertFalse(self.other_kconfig.is_enabled("BOARD_TYPE_QIDI_BOX_2_0"))
        hardware = self.rendered["config/base/mmu_hardware.cfg"]
        self.assertIsNone(re.search(
            r"^\[tmc\w+ mmu_stepper unit0_gear(?:_\d+)?\]$",
            hardware,
            re.MULTILINE,
        ))
        for suffix in ("", "_1", "_2", "_3"):
            self.assertIn("[mmu_stepper unit0_gear%s]" % suffix, hardware)

    def test_hardware_controlled_driver_option_is_available_to_other_mmus(self):
        profile = profiles.Profile(
            "boxturtle_hardware_drivers",
            syms={
                "MMU_TYPE_BOX_TURTLE_1_0": True,
                "CHOICE_GEAR_TMC_NONE": True,
            },
        )
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig(profile.name, profile.syms)
        hardware = cfg.render(profile)["config/base/mmu_hardware.cfg"]

        self.assertTrue(kconfig.is_enabled("CHOICE_GEAR_TMC_NONE"))
        self.assertEqual(kconfig.get("PARAM_GEAR_TMC"), "")
        self.assertEqual(kconfig.syms["PIN_GEAR_UART"].visibility, 0)
        self.assertEqual(kconfig.syms["PIN_GEAR_DIAG"].visibility, 0)
        self.assertIsNone(re.search(
            r"^\[tmc\w+ mmu_stepper unit0_gear(?:_\d+)?\]$",
            hardware,
            re.MULTILINE,
        ))

    def test_fixed_board_pinout(self):
        hardware = self.rendered["config/base/mmu_hardware.cfg"]
        for expected in (
            "step_pin                 : unit0:PC14",
            "dir_pin                  : unit0:PC13",
            "enable_pin               : !unit0:PC15",
            "step_pin                 : unit0:PB9",
            "dir_pin                  : unit0:PB8",
            "enable_pin               : !unit0:PC0",
            "step_pin                 : unit0:PC12",
            "dir_pin                  : unit0:PC11",
            "enable_pin               : !unit0:PD2",
            "step_pin                 : unit0:PC8",
            "dir_pin                  : unit0:PB2",
            "enable_pin               : !unit0:PC10",
            "mmu_entry_switch_pin_0     : !unit0:PA0",
            "mmu_entry_switch_pin_1     : !unit0:PB3",
            "mmu_entry_switch_pin_2     : !unit0:PA13",
            "mmu_entry_switch_pin_3     : !unit0:PA7",
            "mmu_shared_exit_switch_pin : ^!unit0:PB1",
            "tension_pin             : ^unit0:PB0",
        ):
            with self.subTest(config=expected):
                self.assertIn(expected, hardware)

        machine = self.rendered["config/base/mmu.cfg"]
        self.assertIn("extruder_switch_pin         : !THR:PA1", machine)

    def test_qidi_q2_toolhead_dimensions_and_sensor(self):
        machine = self.rendered["config/base/mmu.cfg"]
        self.assertIn("toolhead_extruder_to_nozzle : 72", machine)
        self.assertIn("toolhead_sensor_to_nozzle   : 62", machine)
        self.assertIn("toolhead_entry_to_extruder  : 8", machine)
        self.assertIn("extruder_switch_pin         : !THR:PA1", machine)

    def test_qidi_q2_implies_cutter_with_tuned_defaults(self):
        self.assertTrue(self.kconfig.is_enabled("MMU_HAS_TOOLHEAD_CUTTER"))
        expected = {
            "VAR_CUT_TIP_PIN_LOC_XY": "15, 0.5",
            "VAR_CUT_TIP_PIN_LOC_COMPRESSED_XY": "-0.5, 0.5",
            "VAR_CUT_TIP_RETRACT_LENGTH": "15",
            "VAR_CUT_TIP_SIMPLE_TIP_FORMING": "False",
            "VAR_CUT_TIP_PUSHBACK_LENGTH": "0",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

    def test_qidi_q2_cutter_can_be_disabled(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            without_cutter = cfg._kconfig(
                "qidi_box_without_cutter",
                {
                    "MMU_TYPE_QIDI_BOX_1_0": True,
                    "MMU_HAS_TOOLHEAD_CUTTER": False,
                },
            )
        self.assertFalse(without_cutter.is_enabled("MMU_HAS_TOOLHEAD_CUTTER"))

    def test_qidi_q2_cutter_defaults_do_not_apply_to_q1(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            q1_with_cutter = cfg._kconfig(
                "qidi_box_q1_with_cutter",
                {
                    "MMU_TYPE_QIDI_BOX_1_0": True,
                    "TOOLHEAD_TYPE_QIDI_Q1_PRO": True,
                    "MMU_HAS_TOOLHEAD_CUTTER": True,
                },
            )
        self.assertEqual(q1_with_cutter.get("VAR_CUT_TIP_PIN_LOC_XY"),
                         "14, 250")

    def test_qidi_q2_cutter_defaults_can_be_overridden(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            customized = cfg._kconfig(
                "qidi_box_custom_cutter",
                {
                    "MMU_TYPE_QIDI_BOX_1_0": True,
                    "VAR_CUT_TIP_PIN_LOC_XY": "20, 1",
                    "VAR_CUT_TIP_PIN_LOC_COMPRESSED_XY": "1, 1",
                    "VAR_CUT_TIP_RETRACT_LENGTH": 12,
                    "BOOL_CUT_TIP_SIMPLE_TIP_FORMING": True,
                    "VAR_CUT_TIP_PUSHBACK_LENGTH": 2,
                },
            )

        expected = {
            "VAR_CUT_TIP_PIN_LOC_XY": "20, 1",
            "VAR_CUT_TIP_PIN_LOC_COMPRESSED_XY": "1, 1",
            "VAR_CUT_TIP_RETRACT_LENGTH": "12",
            "VAR_CUT_TIP_SIMPLE_TIP_FORMING": "True",
            "VAR_CUT_TIP_PUSHBACK_LENGTH": "2",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(customized.get(symbol), value)


if __name__ == "__main__":
    unittest.main()
