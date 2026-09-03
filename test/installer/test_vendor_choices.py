# Keep installer-generated MMU vendors in sync with Klipper runtime validation.

import glob
import re
import unittest

from extras.mmu.mmu_constants import VENDORS
from test.hh import cfg


class TestVendorChoices(unittest.TestCase):

    def test_all_kconfig_mmu_vendors_are_runtime_choices(self):
        vendor_defaults = set()
        pattern = re.compile(
            r"config PARAM_VENDOR\s+default \"([^\"]+)\"",
            re.MULTILINE,
        )
        for path in glob.glob(cfg.REPO_ROOT + "/installer/mmu_types/Kconfig.*"):
            with open(path, encoding="utf-8") as stream:
                vendor_defaults.update(pattern.findall(stream.read()))

        self.assertTrue(vendor_defaults, "no MMU vendor defaults found in Kconfig")
        self.assertEqual(vendor_defaults - set(VENDORS), set())

    def test_ercf_defaults_to_two_gate_load_attempts_but_remains_editable(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            standard = cfg._kconfig(
                "ercf_gate_load_attempts",
                {
                    "MMU_FAMILY_ERCF": True,
                    "MMU_TYPE_ERCF_3_0": True,
                },
            )
            customized = cfg._kconfig(
                "ercf_gate_load_attempts_customized",
                {
                    "MMU_FAMILY_ERCF": True,
                    "MMU_TYPE_ERCF_3_0": True,
                    "PARAM_GATE_LOAD_ATTEMPTS": 1,
                },
            )

        attempts = standard.syms["PARAM_GATE_LOAD_ATTEMPTS"]
        self.assertEqual(attempts.str_value, "2")
        self.assertGreater(attempts.visibility, 0)
        self.assertEqual(customized.get("PARAM_GATE_LOAD_ATTEMPTS"), "1")


if __name__ == "__main__":
    unittest.main()
