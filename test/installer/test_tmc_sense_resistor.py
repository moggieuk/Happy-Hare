# TMC current-sense resistor Kconfig and template rendering tests.

import unittest

from test.hh import cfg, profiles


class TestTmcSenseResistor(unittest.TestCase):

    @staticmethod
    def kconfig(name, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig(name, syms)

    @staticmethod
    def hardware(name, syms):
        profile = profiles.Profile(name, syms=syms)
        return cfg.render(profile)["config/base/mmu_hardware.cfg"]

    def test_standard_tmc_defaults(self):
        kconfig = self.kconfig(
            "standard_sense_resistors",
            {"MMU_TYPE_TRADRACK_1_0": True},
        )

        self.assertEqual(kconfig.get("PARAM_GEAR_SENSE_RESISTOR"), "0.110")
        self.assertEqual(
            kconfig.get("PARAM_SELECTOR_SENSE_RESISTOR"), "0.110")

    def test_tmc2226_defaults_and_klipper_driver_name(self):
        syms = {
            "MMU_TYPE_TRADRACK_1_0": True,
            "CHOICE_GEAR_TMC2226": True,
            "CHOICE_SELECTOR_TMC2226": True,
        }
        kconfig = self.kconfig("tmc2226_sense_resistors", syms)

        self.assertTrue(kconfig.is_enabled("CHOICE_GEAR_TMC2226"))
        self.assertTrue(kconfig.is_enabled("CHOICE_SELECTOR_TMC2226"))
        self.assertEqual(kconfig.get("PARAM_GEAR_TMC"), "tmc2209")
        self.assertEqual(kconfig.get("PARAM_SELECTOR_TMC"), "tmc2209")
        self.assertEqual(kconfig.get("PARAM_GEAR_SENSE_RESISTOR"), "0.150")
        self.assertEqual(
            kconfig.get("PARAM_SELECTOR_SENSE_RESISTOR"), "0.150")

        hardware = self.hardware("tmc2226_sense_resistors", syms)
        self.assertEqual(hardware.count("sense_resistor           : 0.150"), 2)
        self.assertIn("[tmc2209 mmu_stepper unit0_gear]", hardware)
        self.assertIn("[tmc2209 mmu_stepper unit0_selector]", hardware)

    def test_board_override_is_rendered(self):
        syms = {
            "MMU_TYPE_NIGHT_OWL_1_0": True,
            "MMU_HAS_SENSOR_SHARED_EXIT": True,
        }
        hardware = self.hardware("owlfc_sense_resistor", syms)

        self.assertIn("sense_resistor           : 0.180", hardware)


if __name__ == "__main__":
    unittest.main()
