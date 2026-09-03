# NightOwl and OwlFC-Mini Kconfig defaults.

import unittest

from test.hh import cfg


class TestNightOwlProfile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._kconfig(
                "night_owl",
                {"MMU_TYPE_NIGHT_OWL_1_0": True},
            )

    def test_owlfc_mini_is_the_default_board(self):
        self.assertEqual(self.kconfig.get("PARAM_NUM_GATES"), "2")
        self.assertTrue(
            self.kconfig.is_enabled("BOARD_TYPE_OWLFC_MINI_1_0"))
        self.assertFalse(self.kconfig.is_enabled("BOARD_TYPE_OTHER"))
        self.assertEqual(
            self.kconfig.get("PARAM_BOARD_TYPE"), "OwlFC-Mini v1.0")

    def test_owlfc_mini_pinout(self):
        expected = {
            "PIN_GEAR_STEP": "unit0:PB15",
            "PIN_GEAR_DIR": "unit0:PB14",
            "PIN_GEAR_ENABLE": "!unit0:PA8",
            "PIN_GEAR_UART": "unit0:PB13",
            "PIN_GEAR_STEP_1": "unit0:PC6",
            "PIN_GEAR_DIR_1": "unit0:PA9",
            "PIN_GEAR_ENABLE_1": "!unit0:PA10",
            "PIN_GEAR_UART_1": "unit0:PC7",
            "PIN_ENTRY_SENSOR_0": "^unit0:PB1",
            "PIN_EXIT_SENSOR_0": "^unit0:PB11",
            "PIN_ENTRY_SENSOR_1": "^unit0:PB12",
            "PIN_EXIT_SENSOR_1": "^unit0:PB4",
            "PIN_SHARED_EXIT_SENSOR": "^unit0:PB5",
            "PIN_BUFFER_COMPRESSION": "^unit0:PA2",
            "PIN_BUFFER_TENSION": "^unit0:PA3",
            "PIN_NEOPIXEL": "unit0:PB3",
            "PARAM_GEAR_SENSE_RESISTOR": "0.180",
        }
        for symbol, value in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(self.kconfig.get(symbol), value)

    def test_board_is_not_available_above_two_lanes(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            three_lane = cfg._kconfig(
                "night_owl_three_lane",
                {
                    "MMU_TYPE_NIGHT_OWL_1_0": True,
                    "PARAM_NUM_GATES": 3,
                },
            )

        self.assertFalse(
            three_lane.is_enabled("BOARD_TYPE_OWLFC_MINI_1_0"))
        self.assertTrue(three_lane.is_enabled("BOARD_TYPE_OTHER"))


if __name__ == "__main__":
    unittest.main()
