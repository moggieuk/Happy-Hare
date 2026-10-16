# Happy Hare installer: the summary install.sh prints of what an install changed in the
# Happy Hare .cfg files, against the backup the install made.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import contextlib
import io
import os
import tempfile
import unittest

from test.hh import cfg

cfg._prepare_imports()

from installer import cfg_diff  # noqa: E402 (needs the installer on the path)

OLD = """\
[mmu_unit_parameters unit0]
gear_load_speed: 100   # a comment
gear_unload_speed: 80
old_option: 1

[mmu_removed_section]
x: 1
"""

NEW = """\
[mmu_unit_parameters unit0]
gear_load_speed: 100   # a reworded comment
gear_unload_speed: 77
new_option: 2

[mmu_added_section]
y: 2
z: 3
"""


class TestCfgDiff(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.backup = self.files("mmu.old-20261016-120005", OLD)
        self.install = self.files("mmu", NEW)

    def files(self, name, text):
        path = os.path.join(self.home, name)
        os.makedirs(os.path.join(path, "base"))
        with open(os.path.join(path, "base", "mmu_parameters_unit0.cfg"), "w") as f:
            f.write(text)
        return path

    def summary(self, **kwargs):
        out = io.StringIO()
        shown = cfg_diff.summary(self.backup, self.install, out=out, **kwargs)
        return shown, out.getvalue()

    def test_values_changed_removed_and_added_are_summarised(self):
        shown, out = self.summary()
        self.assertTrue(shown)
        self.assertEqual(out, (
            "Changes to your Happy Hare .cfg files (previous files are in 'mmu.old-20261016-120005'):\n"
            "From mmu_parameters_unit0.cfg:\n"
            "Changed:\n"
            "[mmu_unit_parameters unit0]\n"
            "gear_unload_speed: 80 -> 77\n"
            "Removed:\n"
            "[mmu_unit_parameters unit0]\n"
            "old_option: 1\n"
            "[mmu_removed_section]\n"
            "Added: 3 new option(s) and 1 new section(s)\n"
            "\n"))

    def test_verbose_lists_what_was_added(self):
        with mock_word_diff():
            _, out = self.summary(verbose=True)
        self.assertIn("Added:\n[mmu_unit_parameters unit0]\nnew_option: 2\n[mmu_added_section]\ny: 2\nz: 3\n", out)

    def test_replaced_hand_edits_are_marked(self):
        _, out = self.summary(replaced={("mmu_parameters_unit0.cfg", "mmu_unit_parameters unit0", "gear_unload_speed")})
        self.assertIn("gear_unload_speed: 80 -> 77 (your .cfg edit, replaced by a menuconfig change)\n", out)

    def test_comment_only_changes_are_no_change(self):
        with open(os.path.join(self.install, "base", "mmu_parameters_unit0.cfg"), "w") as f:
            f.write(OLD.replace("# a comment", "# changed wording"))
        shown, out = self.summary()
        self.assertFalse(shown)
        self.assertEqual(out, "No changes to the values in your Happy Hare .cfg files\n")

    def test_backup_must_be_from_this_install(self):
        self.files("mmu.old-20261016-120005-1", OLD)
        self.files("mmu.old-20250101-000000", OLD)
        self.assertEqual(cfg_diff.backup_since(self.home, "20261016-120000"),
                         os.path.join(self.home, "mmu.old-20261016-120005-1"))
        self.assertIsNone(cfg_diff.backup_since(self.home, "20261016-130000"))

    def test_first_install_prints_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cfg_diff.main([self.home, "--since", "20261016-130000"]), 0)
        self.assertEqual(out.getvalue(), "")


@contextlib.contextmanager
def mock_word_diff():
    from unittest import mock
    with mock.patch.object(cfg_diff, "word_diff"):
        yield


if __name__ == "__main__":
    unittest.main()
