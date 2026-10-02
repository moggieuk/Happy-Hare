# Hermeticity of a harness Kconfig parse.
#
# Every golden-file test in this suite is only as trustworthy as the parse that
# produced it. A Kconfig file sourced from outside the repo (the root Kconfig once
# osource'd a generated /tmp/.Kconfig.generated) survives a clean checkout and
# quietly changes the answer - it does not show up as an error.

import os
import re
import unittest

from test.hh import cfg

INSTALLER = os.path.realpath(cfg.INSTALLER)


def _source_paths():
    for root, _dirs, files in os.walk(INSTALLER):
        for name in files:
            if name.startswith("Kconfig"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    for line in f:
                        m = re.match(r'\s*o?r?source\s+"([^"]*)"', line)
                        if m:
                            yield os.path.join(root, name), m.group(1)


class TestHarnessParseIsHermetic(unittest.TestCase):

    def test_no_kconfig_sources_an_absolute_path(self):
        absolute = ["%s: %s" % (f, p) for f, p in _source_paths() if p.startswith("/")]
        self.assertEqual(absolute, [])

    def test_a_parse_reads_only_files_inside_the_installer_tree(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig("hermetic", {})
        self.assertGreater(len(kc.kconfig_filenames), 100)
        outside = [f for f in kc.kconfig_filenames
                   if not os.path.realpath(os.path.join(INSTALLER, f)).startswith(INSTALLER + os.sep)]
        self.assertEqual(outside, [])


if __name__ == "__main__":
    unittest.main()
