# VVD filament_always_gripped 0 -> 1 upgrade.
#
# 4.0/4.1 rendered filament_always_gripped: 0 for the ViViD, which cannot release filament.
# A default (refresh) reinstall keeps the installed value, so the upgrade has to correct it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import tempfile
import unittest

from installer.build import HHConfig
from installer.upgrades import Upgrades


INSTALLED_4_1 = """
[mmu_unit unit0]
vendor                      : ERCF
version                     : 2.0
filament_always_gripped     : 0

[mmu_unit unit1]
vendor                      : VVD
version                     : 1.0
filament_always_gripped     : 0
"""


class TestVvdAlwaysGrippedUpgrade(unittest.TestCase):

    def test_vvd_unit_becomes_always_gripped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "mmu_hardware.cfg")
            with open(path, "w") as f:
                f.write(INSTALLED_4_1)
            cfg = HHConfig([path])
        with self.assertLogs(level="WARNING"):
            Upgrades().upgrade(cfg, (4, 1), (4, 2))
        self.assertEqual(cfg.get("mmu_unit unit1", "filament_always_gripped"), "1")
        self.assertEqual(cfg.get("mmu_unit unit0", "filament_always_gripped"), "0")


if __name__ == "__main__":
    unittest.main()
