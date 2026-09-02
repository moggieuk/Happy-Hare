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
            "custom_multigear": {
                "MMU_TYPE_BOX_TURTLE_1_0": True,
                "BOARD_TYPE_OTHER": True,
                "PARAM_GEAR_UART_ADDRESS": 0,
                "PARAM_GEAR_UART_ADDRESS_1": 1,
                "PARAM_GEAR_UART_ADDRESS_2": 2,
                "PARAM_GEAR_UART_ADDRESS_3": 3,
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
                selector_address = "2" if board == "skr_pico" else "1"
                self.assertEqual(
                    kconfig.get("PARAM_SELECTOR_UART_ADDRESS"),
                    selector_address,
                )

        for symbol in (
            "PARAM_GEAR_UART_ADDRESS",
            "PARAM_SELECTOR_UART_ADDRESS",
        ):
            low, high, _condition = self.kconfigs["unknown"].syms[symbol].ranges[0]
            self.assertEqual((low.str_value, high.str_value), ("0", "3"))

    def test_skr_pico_multigear_uses_shared_uart_pin_and_indexed_addresses(self):
        kconfig = self.kconfigs["virtual_selector"]
        uart_pin = kconfig.get("PIN_GEAR_UART")
        self.assertEqual(uart_pin, "unit0:gpio9")
        for gear, expected in enumerate((0, 1, 2, 3)):
            suffix = "" if gear == 0 else "_%d" % gear
            address_symbol = "PARAM_GEAR_UART_ADDRESS%s" % suffix
            pin_symbol = "PIN_GEAR_UART%s" % suffix
            with self.subTest(gear=gear):
                self.assertEqual(kconfig.get(pin_symbol), uart_pin)
                self.assertEqual(kconfig.get(address_symbol), str(expected))

        hardware = self.render_hardware(
            "virtual_selector", self.profile_syms["virtual_selector"])
        sections = hardware.split("[tmc2209 mmu_stepper unit0_gear")
        self.assertEqual(len(sections), 5)
        for gear, expected in enumerate(range(4)):
            with self.subTest(rendered_gear=gear):
                self.assertIn(
                    "uart_pin                 : unit0:gpio9",
                    sections[gear + 1],
                )
                self.assertIn(
                    "uart_address             : %d" % expected,
                    sections[gear + 1],
                )

    def test_non_skr_indexed_gear_addresses_still_default_to_zero(self):
        kconfig = self.kconfigs["easy_brd"]
        for gear in range(1, 4):
            symbol = "PARAM_GEAR_UART_ADDRESS_%d" % gear
            with self.subTest(symbol=symbol):
                self.assertEqual(kconfig.get(symbol), "0")

    def test_skr_pico_selector_shares_uart_pin_and_uses_driver_address(self):
        kconfig = self.kconfigs["skr_pico"]
        self.assertEqual(
            kconfig.get("PIN_SELECTOR_UART"),
            kconfig.get("PIN_GEAR_UART"),
        )
        self.assertEqual(kconfig.get("PARAM_SELECTOR_UART_ADDRESS"), "2")

    def test_supported_boards_render_uart_addresses(self):
        for board in ("unknown", "easy_brd", "skr_pico"):
            with self.subTest(board=board):
                hardware = self.render_hardware(board, self.profile_syms[board])
                self.assertIn("uart_address             : 0", hardware)
                selector_address = 2 if board == "skr_pico" else 1
                self.assertIn(
                    "uart_address             : %d" % selector_address,
                    hardware,
                )

    def test_custom_addresses_are_rendered(self):
        kconfig = self.kconfigs["custom"]
        self.assertEqual(kconfig.get("PARAM_GEAR_UART_ADDRESS"), "2")
        self.assertEqual(kconfig.get("PARAM_SELECTOR_UART_ADDRESS"), "3")

        hardware = self.render_hardware("custom", self.profile_syms["custom"])
        self.assertIn("uart_address             : 2", hardware)
        self.assertIn("uart_address             : 3", hardware)

    def test_multigear_addresses_are_configurable_and_rendered_per_gear(self):
        kconfig = self.kconfigs["custom_multigear"]
        for gear, expected in enumerate((0, 1, 2, 3)):
            suffix = "" if gear == 0 else "_%d" % gear
            symbol = "PARAM_GEAR_UART_ADDRESS%s" % suffix
            with self.subTest(symbol=symbol):
                self.assertEqual(kconfig.get(symbol), str(expected))
                self.assertGreater(kconfig.syms[symbol].visibility, 0)

        hardware = self.render_hardware(
            "custom_multigear", self.profile_syms["custom_multigear"])
        sections = hardware.split("[tmc2209 mmu_stepper unit0_gear")
        self.assertEqual(len(sections), 5)
        for gear, expected in enumerate((0, 1, 2, 3)):
            with self.subTest(gear=gear):
                self.assertIn(
                    "uart_address             : %d" % expected,
                    sections[gear + 1],
                )

    def test_other_boards_do_not_render_uart_addresses(self):
        hardware = self.render_hardware("mmb", self.profile_syms["mmb"])
        self.assertNotIn("uart_address", hardware)
        self.assertNotIn("Only for old EASY-BRD mcu", hardware)


if __name__ == "__main__":
    unittest.main()
