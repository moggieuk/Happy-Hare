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

    def test_stock_sensors_buffer_and_dryer_are_enabled(self):
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

        hardware = self.rendered["config/base/mmu_hardware.cfg"]
        self.assertNotIn("mmu_exit_switch_pin_0", hardware)
        self.assertIn("environment_sensor       : box1_env", hardware)
        self.assertIn("filament_heater          : box1_heater", hardware)
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
        self.assertFalse(self.other_kconfig.is_enabled("CHOICE_GEAR_TMC_NONE"))
        self.assertFalse(self.other_kconfig.is_enabled("BOARD_TYPE_QIDI_BOX_2_0"))
        hardware = self.rendered["config/base/mmu_hardware.cfg"]
        self.assertIsNone(re.search(
            r"^\[tmc\w+ mmu_stepper unit0_gear(?:_\d+)?\]$",
            hardware,
            re.MULTILINE,
        ))
        for suffix in ("", "_1", "_2", "_3"):
            self.assertIn("[mmu_stepper unit0_gear%s]" % suffix, hardware)

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

    def test_generic_qidi_q2_toolhead_dimensions_are_preserved(self):
        machine = self.rendered["config/base/mmu.cfg"]
        self.assertIn("toolhead_extruder_to_nozzle : 72", machine)
        self.assertIn("toolhead_sensor_to_nozzle   : 62", machine)
        self.assertIn("toolhead_entry_to_extruder  : 8", machine)


if __name__ == "__main__":
    unittest.main()
