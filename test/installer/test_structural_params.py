# Happy Hare installer structural-parameter tests.
#
# A refresh rebuilds each .cfg from today's template and then copies every parameter the
# installed file already had back over it, so user edits survive an upgrade. A few parameters
# are not user preferences though - they describe the shape of the machine and Kconfig derives
# them on every build. Copying those forward lets a value outlive the thing it describes:
#
#   [mmu_machine] units            a retired unit stayed in the list, and Klipper refused to
#                                  start with "Expected [mmu_unit unit1] section not found"
#                                  after an install that reported success
#   [mmu_machine] bare_unit_names  toggling "Single Unit" in menuconfig had no effect on an
#                                  existing install, so the naming never changed and the
#                                  saved data that follows it never moved
#
# Only Replace mode escaped either one, which made the default path the broken one. These
# drive the real build_config_file() in refresh mode against an installed mmu.cfg that
# disagrees with Kconfig.
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


def set_machine_param(path, option, value):
    """Put an option in [mmu_machine], the way an earlier install left it."""
    with open(path) as f:
        text = f.read()
    pattern = r"^%s\s*:.*$" % re.escape(option)
    if re.search(pattern, text, re.M):
        text = re.sub(pattern, "%s: %s" % (option, value), text, count=1, flags=re.M)
    else:
        text = re.sub(r"^\[mmu_machine\]\s*$",
                      "[mmu_machine]\n%s: %s" % (option, value),
                      text, count=1, flags=re.M)
    with open(path, "w") as f:
        f.write(text)


class StructuralParamCase:
    """Refresh an installed mmu.cfg whose machine description is out of date.

    A mixin rather than a TestCase, so unittest does not collect the base itself.
    """

    INSTALLED_VALUES = {}       # what the old install left in [mmu_machine]
    SYMS = {}                   # what Kconfig says now

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        installed = os.path.join(cls.tmpdir.name, "installed")
        cls.out_root = os.path.join(cls.tmpdir.name, "out")
        shutil.copytree(FIXTURE, installed)

        for option, value in cls.INSTALLED_VALUES.items():
            set_machine_param(os.path.join(installed, "mmu.cfg"), option, value)

        profile = profiles.get("boxturtle")
        dest_dir = os.path.join(cls.out_root, "mmu", "base")
        os.makedirs(dest_dir)
        env = dict(cfg._SINGLE_UNIT_ENV,
                   OUT=cls.out_root,
                   F_CFG_UPGRADE_MODE="refresh")
        with cfg._env(env), cfg._chdir(cfg.REPO_ROOT):
            from installer import build

            kconfig = cfg._kconfig("installer-structural-4.00",
                                   dict(profile.syms, **cls.SYMS))
            cls.kconfig = kconfig
            build.build_config_file(
                "config/base/mmu.cfg",
                os.path.join(dest_dir, "mmu.cfg"),
                kconfig,
                sorted(os.path.join(installed, name)
                       for name in os.listdir(installed)
                       if name.endswith(".cfg")),
                {"PARAM_TOTAL_NUM_GATES": kconfig.getint("PARAM_NUM_GATES")},
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def rebuilt(self):
        return ConfigBuilder(os.path.join(self.out_root, "mmu", "base", "mmu.cfg"))

    def machine(self, option):
        return self.rebuilt().get("mmu_machine", option).strip()

    def test_ordinary_user_values_are_still_preserved(self):
        """The fix must not turn refresh into replace for everything else."""
        self.assertEqual(self.rebuilt().get("mmu_parameters", "log_level"), "4")


class TestRetiredUnitLeavesTheUnitList(StructuralParamCase, unittest.TestCase):
    """A unit the machine no longer has must not survive a refresh."""

    INSTALLED_VALUES = {"units": "unit0,unit1"}

    def test_kconfig_knows_about_one_unit(self):
        """Guards the fixture: the test is meaningless if Kconfig also says two."""
        self.assertEqual(self.kconfig.get("MMU_UNITS"), "unit0")

    def test_retired_unit_is_dropped_from_the_unit_list(self):
        units = self.machine("units")
        self.assertNotIn("unit1", units)
        self.assertEqual(units, "unit0")


class TestSingleUnitToggleReachesTheConfig(StructuralParamCase, unittest.TestCase):
    """Turning "Single Unit" off in menuconfig must reach an existing install."""

    INSTALLED_VALUES = {"bare_unit_names": "1"}
    SYMS = {"PARAM_BARE_UNIT_NAMES": False}

    def test_kconfig_has_the_option_off(self):
        """Guards the fixture: the test is meaningless if Kconfig also says on."""
        self.assertFalse(self.kconfig.is_enabled("PARAM_BARE_UNIT_NAMES"))

    def test_toggle_is_not_overwritten_by_the_previous_install(self):
        self.assertEqual(self.machine("bare_unit_names"), "0")


if __name__ == "__main__":
    unittest.main()
