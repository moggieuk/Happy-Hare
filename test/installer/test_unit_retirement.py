# Happy Hare installer unit-retirement test.
#
# Dropping a unit from the machine retires its generated files, but [mmu_machine] units is an
# ordinary parameter in mmu.cfg, and a refresh copies every existing parameter forward over the
# freshly rendered template. The retired unit therefore survived in the list, and Klipper
# refused to start with "Expected [mmu_unit unit1] section not found" - after an install that
# reported success. Only Replace mode escaped it, so the default path was the broken one.
#
# This drives the real build_config_file() in refresh mode against an installed mmu.cfg that
# names a unit the Kconfig no longer has.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import re
import shutil
import tempfile
import unittest

from installer.parser import ConfigBuilder
from test.hh import cfg, profiles


FIXTURE = os.path.join(os.path.dirname(__file__), "refresh", "4_00", "input")


class TestRetiredUnitLeavesTheUnitList(unittest.TestCase):
    """A unit the machine no longer has must not survive a refresh."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.installed = os.path.join(cls.tmpdir.name, "installed")
        cls.out_root = os.path.join(cls.tmpdir.name, "out")
        shutil.copytree(FIXTURE, cls.installed)

        # The machine used to have two units. Kconfig below knows about one.
        cls._set_units(os.path.join(cls.installed, "mmu.cfg"), "unit0,unit1")

        profile = profiles.get("boxturtle")
        dest_dir = os.path.join(cls.out_root, "mmu", "base")
        os.makedirs(dest_dir)
        env = dict(cfg._SINGLE_UNIT_ENV,
                   OUT=cls.out_root,
                   F_CFG_UPGRADE_MODE="refresh")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            from installer import build

            kconfig = cfg._kconfig("installer-retirement-4.00", profile.syms)
            cls.kconfig_units = kconfig.get("MMU_UNITS")
            build.build_config_file(
                "config/base/mmu.cfg",
                os.path.join(dest_dir, "mmu.cfg"),
                kconfig,
                sorted(
                    os.path.join(cls.installed, name)
                    for name in os.listdir(cls.installed)
                    if name.endswith(".cfg")
                ),
                {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")},
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    @staticmethod
    def _set_units(path, value):
        """Put a units line in [mmu_machine], the way an older install left it."""
        with open(path) as f:
            text = f.read()
        if re.search(r"^units\s*:", text, re.M):
            text = re.sub(r"^units\s*:.*$", "units: %s" % value, text, count=1, flags=re.M)
        else:
            text = re.sub(r"^\[mmu_machine\]\s*$",
                          "[mmu_machine]\nunits: %s" % value,
                          text, count=1, flags=re.M)
        with open(path, "w") as f:
            f.write(text)

    def rebuilt(self):
        return ConfigBuilder(os.path.join(self.out_root, "mmu", "base", "mmu.cfg"))

    def test_kconfig_knows_about_one_unit(self):
        """Guards the fixture: the test is meaningless if Kconfig also says two."""
        self.assertEqual(self.kconfig_units, "unit0")

    def test_retired_unit_is_dropped_from_the_unit_list(self):
        units = self.rebuilt().get("mmu_machine", "units")
        self.assertNotIn("unit1", units)
        self.assertEqual(units.strip(), "unit0")

    def test_ordinary_user_values_are_still_preserved(self):
        """The fix must not turn refresh into replace for everything else."""
        self.assertEqual(self.rebuilt().get("mmu_parameters", "log_level"), "4")


if __name__ == "__main__":
    unittest.main()
