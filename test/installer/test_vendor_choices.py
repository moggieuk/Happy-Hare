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


if __name__ == "__main__":
    unittest.main()
