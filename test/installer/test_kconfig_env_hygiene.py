# Hermeticity of a harness Kconfig parse.
#
# Every golden-file test in this suite is only as trustworthy as the parse that
# produced it, and a leftover generated Kconfig in /tmp quietly contaminates
# one. It does not show up as an error - it just changes the answer.

import os
import unittest

# installer/Kconfig:226 osource's this, and installer/build.py:1027 writes it
# during a multi-unit build. It is outside the repo, so a stale one survives a
# clean checkout and silently joins every parse the harness makes.
GENERATED_KCONFIG = "/tmp/.Kconfig.generated"


class TestHarnessParseIsHermetic(unittest.TestCase):

    def test_no_stale_generated_kconfig_joins_the_parse(self):
        self.assertFalse(
            os.path.exists(GENERATED_KCONFIG),
            "%s exists and is osource'd by installer/Kconfig:226, so it is part "
            "of every parse this suite makes - including the ones that capture "
            "golden files. It is written by a multi-unit build (build.py:1027) "
            "and lives outside the repo, so a clean checkout does not remove it. "
            "Delete it and re-run." % GENERATED_KCONFIG)


if __name__ == "__main__":
    unittest.main()
