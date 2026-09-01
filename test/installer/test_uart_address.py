# Board-specific TMC UART address Kconfig and rendering tests.

import unittest

from test.hh import cfg, profiles


class TestUartAddressConfiguration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile_syms = {
            "unknown": {"MMU_TYPE_HTLF_1_0": True},
            "easy_brd": {
                "MMU_TYPE_HTLF_1_0": True,
                "BOARD_TYPE_EASY_BRD": True,
            },
            "skr_pico": {
                "MMU_TYPE_HTLF_1_0": True,
                "BOARD_TYPE_SKR_PICO_1": True,
            },
            "mmb": {
                "MMU_TYPE_HTLF_1_0": True,
                "BOARD_TYPE_MMB_2_0": True,
            },
            "virtual_selector": {
                "MMU_TYPE_BOX_TURTLE_1_0": True,
                "BOARD_TYPE_SKR_PICO_1": True,
            },
            "custom": {
                "MMU_TYPE_HTLF_1_0": True,
                "BOARD_TYPE_SKR_PICO_1": True,
                "PARAM_GEAR_UART_ADDRESS": 2,
                "PARAM_SELECTOR_UART_ADDRESS": 3,
            },
        }
        cls.kconfigs = {}
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            for name, syms in cls.profile_syms.items():
                cls.kconfigs[name] = cfg._kconfig(name, syms)

    @staticmethod
    def render_hardware(name, syms):
        profile = profiles.Profile(name, syms=syms)
        return cfg.render(profile)["config/base/mmu_hardware.cfg"]

    def test_addresses_are_visible_only_for_supported_boards(self):
        for board in ("unknown", "easy_brd", "skr_pico"):
            with self.subTest(board=board):
                kconfig = self.kconfigs[board]
                self.assertGreater(
                    kconfig.syms["PARAM_GEAR_UART_ADDRESS"].visibility, 0)
                self.assertGreater(
                    kconfig.syms["PARAM_SELECTOR_UART_ADDRESS"].visibility, 0)

        kconfig = self.kconfigs["mmb"]
        self.assertEqual(kconfig.syms["PARAM_GEAR_UART_ADDRESS"].visibility, 0)
        self.assertEqual(kconfig.syms["PARAM_SELECTOR_UART_ADDRESS"].visibility, 0)

        kconfig = self.kconfigs["virtual_selector"]
        self.assertGreater(kconfig.syms["PARAM_GEAR_UART_ADDRESS"].visibility, 0)
        self.assertEqual(kconfig.syms["PARAM_SELECTOR_UART_ADDRESS"].visibility, 0)

    def test_addresses_have_expected_defaults_and_range(self):
        for board in ("unknown", "easy_brd", "skr_pico"):
            with self.subTest(board=board):
                kconfig = self.kconfigs[board]
                self.assertEqual(kconfig.get("PARAM_GEAR_UART_ADDRESS"), "0")
                self.assertEqual(kconfig.get("PARAM_SELECTOR_UART_ADDRESS"), "1")

        for symbol in (
            "PARAM_GEAR_UART_ADDRESS",
            "PARAM_SELECTOR_UART_ADDRESS",
        ):
            low, high, _condition = self.kconfigs["unknown"].syms[symbol].ranges[0]
            self.assertEqual((low.str_value, high.str_value), ("0", "3"))

    def test_supported_boards_render_uart_addresses(self):
        for board in ("unknown", "easy_brd", "skr_pico"):
            with self.subTest(board=board):
                hardware = self.render_hardware(board, self.profile_syms[board])
                self.assertIn("uart_address             : 0", hardware)
                self.assertIn("uart_address             : 1", hardware)

    def test_custom_addresses_are_rendered(self):
        kconfig = self.kconfigs["custom"]
        self.assertEqual(kconfig.get("PARAM_GEAR_UART_ADDRESS"), "2")
        self.assertEqual(kconfig.get("PARAM_SELECTOR_UART_ADDRESS"), "3")

        hardware = self.render_hardware("custom", self.profile_syms["custom"])
        self.assertIn("uart_address             : 2", hardware)
        self.assertIn("uart_address             : 3", hardware)

    def test_other_boards_do_not_render_uart_addresses(self):
        hardware = self.render_hardware("mmb", self.profile_syms["mmb"])
        self.assertNotIn("uart_address", hardware)
        self.assertNotIn("Only for old EASY-BRD mcu", hardware)


if __name__ == "__main__":
    unittest.main()
