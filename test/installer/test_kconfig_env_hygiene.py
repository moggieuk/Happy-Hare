# Hermeticity of a harness Kconfig parse.
#
# Every golden-file test in this suite is only as trustworthy as the parse that
# produced it, and two things can quietly contaminate one: a leftover generated
# Kconfig in /tmp, and an environment variable that outlives the parse that set
# it. Neither shows up as an error - both just change the answer.

import os
import unittest

from test.hh import cfg

# installer/Kconfig:226 osource's this, and installer/build.py:1027 writes it
# during a multi-unit build. It is outside the repo, so a stale one survives a
# clean checkout and silently joins every parse the harness makes.
GENERATED_KCONFIG = "/tmp/.Kconfig.generated"

_MULTI_UNIT_ENTRY_ENV = {
    "UNIT_NAME": "unit0,unit1",
    "MCU_NAME": "unit0,unit1",
    "UNIT_INDEX": "0",
    "F_MULTI_UNIT": "y",
    "F_MULTI_UNIT_ENTRY_POINT": "y",
}


class TestHarnessParseIsHermetic(unittest.TestCase):

    def test_no_stale_generated_kconfig_joins_the_parse(self):
        self.assertFalse(
            os.path.exists(GENERATED_KCONFIG),
            "%s exists and is osource'd by installer/Kconfig:226, so it is part "
            "of every parse this suite makes - including the ones that capture "
            "golden files. It is written by a multi-unit build (build.py:1027) "
            "and lives outside the repo, so a clean checkout does not remove it. "
            "Delete it and re-run." % GENERATED_KCONFIG)

    def test_env_is_restored_after_a_parse(self):
        """_env() must restore, not just assign.

        The existing render-level guard in test_mmu_config catches a leak that
        changes rendered output. This catches the leak itself, which is strictly
        earlier: a variable can outlive its parse for a while before it happens
        to land on a profile whose output differs.
        """
        before = dict(os.environ)

        with cfg._env(_MULTI_UNIT_ENTRY_ENV):
            cfg._kconfig("env_hygiene_entry", {})
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cfg._kconfig("env_hygiene_single", {})

        after = dict(os.environ)
        self.assertEqual(
            sorted(before), sorted(after),
            "a Kconfig parse added or removed an environment variable")
        for key in before:
            self.assertEqual(
                before[key], after[key],
                "%s changed value across a Kconfig parse - _env() assigned "
                "without restoring" % key)


if __name__ == "__main__":
    unittest.main()
