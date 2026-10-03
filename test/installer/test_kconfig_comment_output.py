# Comments written to a saved config file.
#
# menuconfig shows [[VALUE:SYM]] markup as SYM's live value and hides a comment
# whose 'if' condition is false. The saved file should say the same, not carry
# the raw markup or comments for choices that weren't made.

import os
import tempfile
import unittest

import kconfiglib

from test.hh import cfg

KCONFIG = '''
config UNIT_NAME
  string "Name"
  default "unit0"

config MULTI
  bool "Multi"

comment "Unit: [[B]][[VALUE:UNIT_NAME:8]][[/B]]|"
comment "Shown when multi" if MULTI
comment "Shown when single" if !MULTI
'''


class TestCommentOutput(unittest.TestCase):

    def write(self, **values):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Kconfig")
            with open(path, "w") as f:
                f.write(KCONFIG)
            kconf = kconfiglib.Kconfig(path, warn=False)
            for name, value in values.items():
                kconf.syms[name].set_value(value)
            out = os.path.join(tmp, ".config")
            kconf.write_config(out, header="")
            with open(out) as f:
                return f.read()

    def test_value_markup_is_written_as_the_value(self):
        text = self.write(UNIT_NAME="box")
        self.assertNotIn("[[VALUE:", text)
        self.assertIn("Unit: [[B]]box     [[/B]]|", text)

    def test_hidden_comments_are_not_written(self):
        text = self.write(MULTI="n")
        self.assertIn("Shown when single", text)
        self.assertNotIn("Shown when multi", text)
        self.assertIn("Shown when multi", self.write(MULTI="y"))

    def test_the_unit_header_names_a_renamed_single_unit(self):
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, UNIT_NAME="box", MCU_NAME="box")), \
                cfg._chdir(cfg.INSTALLER):
            kconf = cfg._new_kconfig("comment-output")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, ".mmu_config")
            kconf.write_config(out)
            with open(out) as f:
                text = f.read()
        self.assertNotIn("[[VALUE:", text)
        self.assertIn("Unit: [[B]]box", text)
        self.assertNotIn("(set in the MMU units list)", text)


if __name__ == "__main__":
    unittest.main()
