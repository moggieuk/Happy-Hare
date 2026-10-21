# installer/lib/kconfiglib/shared_components.py: what a unit parse reads from the other
# units' saved configs. The Kconfig behavior built on it is in test_shared_buffer.py and
# test_shared_encoder.py; these pin the reader itself.

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from test.hh import cfg

sys.path.insert(0, cfg.KCONFIGLIB)
import kconfigfunctions  # noqa: E402
import shared_components as sc  # noqa: E402

OWNER = "CONFIG_MMU_HAS_SYNC_FEEDBACK_BUFFER=y\n"
SHARER = OWNER + 'CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y\n'


def named(text, name, default=False):
    return text + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="%s"%s\n' % (
        name, sc.HH_DEFAULT_TOKEN if default else "")


class Scratch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = os.path.join(self.tmp.name, ".mmu_config")

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        return path

    def install(self, units, **files):
        self.write(self.parent, 'CONFIG_MULTI_UNIT=y\nCONFIG_MMU_UNITS="%s"\n' % ",".join(units))
        for name, text in files.items():
            self.write(sc.unit_file(self.parent, name), text)

    def env(self, unit, **extra):
        values = dict(KCONFIG_PARENT=self.parent, UNIT_NAME=unit,
                      KCONFIG_CONFIG=sc.unit_file(self.parent, unit))
        values.update(extra)
        return mock.patch.dict(os.environ, values)


class TestOwners(Scratch):

    def test_nothing_is_read_without_a_parent(self):
        self.install(["unit0", "unit1"], unit0=OWNER)
        with self.env("unit1", KCONFIG_PARENT=""):
            self.assertEqual(sc.owners("buffer"), [])
            self.assertEqual(sc.owner_name(None, None, "buffer", "0"), "")
            self.assertEqual(sc.owner_max(None, None, "buffer"), "0")

    def test_owners_are_the_other_units_with_their_own_in_unit_order(self):
        self.install(["unit0", "unit1", "unit2", "unit3", "unit4"],
                     unit0=named(OWNER, "box_buf"), unit1="", unit2=SHARER,
                     unit3=OWNER, unit4=OWNER)
        with self.env("unit4"):
            # unit1 has no buffer, unit2 shares one, unit4 is the unit being configured
            self.assertEqual([(o.unit, o.name) for o in sc.owners("buffer")],
                             [("unit0", "box_buf"), ("unit3", "unit3")])
            self.assertEqual(sc.owner_max(None, None, "buffer"), "1")
            self.assertEqual(sc.owner_name(None, None, "buffer", "1"), "unit3")
            self.assertEqual(sc.owner_name(None, None, "buffer", "2"), "")

    def test_owner_names_lists_each_named_owner_once(self):
        self.install(["unit0", "unit1", "unit2", "unit3", "unit4"],
                     unit0=named(OWNER, "box_buf"), unit1=named(OWNER, ""),
                     unit2=named(OWNER, "box_buf"), unit3=SHARER)
        with self.env("unit4"):
            self.assertEqual(sc.owner_names(None, None, "buffer"), "box_buf")
        with self.env("unit0"):
            self.assertEqual(sc.owner_names(None, None, "buffer"), "box_buf")
        self.install(["unit0", "unit1"], unit0=SHARER)
        with self.env("unit1"):
            self.assertEqual(sc.owner_names(None, None, "buffer"), "")

    def test_an_owners_name_is_its_saved_name_default_or_not(self):
        self.install(["unit0", "unit1", "unit2"],
                     unit0=named(OWNER, "unit0", default=True), unit1=named(OWNER, ""))
        with self.env("unit2"):
            self.assertEqual([o.name for o in sc.owners("buffer")], ["unit0", ""])

    def test_exports_are_read_from_an_owner_and_absent_past_the_end(self):
        self.install(["unit0", "unit1"],
                     unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y #~DEFAULT~#\n"
                     'CONFIG_PARAM_BUFFER_SPRING_STATE="tension"\n')
        with self.env("unit1"):
            export = lambda i, sym: sc.owner_export(None, None, "buffer", i, sym)
            self.assertEqual(export("0", "MMU_HAS_SENSOR_BUFFER_TENSION"), "y")
            self.assertEqual(export("0", "MMU_HAS_SENSOR_BUFFER_COMPRESSION"), "n")
            self.assertEqual(export("0", "PARAM_BUFFER_SPRING_STATE"), "tension")
            self.assertEqual(export("1", "PARAM_BUFFER_SPRING_STATE"), "none")

    def test_a_name_is_escaped_for_a_kconfig_string(self):
        self.install(["unit0", "unit1"], unit0=named(OWNER, 'a\\"b'))
        with self.env("unit1"):
            self.assertEqual(sc.owners("buffer")[0].name, 'a"b')
            self.assertEqual(sc.owner_name(None, None, "buffer", "0"), 'a\\"b')

    def test_a_rewritten_file_is_read_again(self):
        self.install(["unit0", "unit1"], unit0=OWNER)
        path = sc.unit_file(self.parent, "unit0")
        with self.env("unit1"):
            self.assertEqual(sc.owners("buffer")[0].name, "unit0")
            self.write(path, named(OWNER, "box_buf"))
            later = time.time() + 10
            os.utime(path, (later, later))
            self.assertEqual(sc.owners("buffer")[0].name, "box_buf")

    def test_the_functions_are_registered_with_kconfig(self):
        for name in sc.FUNCTIONS:
            self.assertIn(name, kconfigfunctions.functions)


class TestRegistryMatchesKconfig(unittest.TestCase):
    """A registry entry naming a symbol Kconfig doesn't define would just find no owners."""

    @classmethod
    def setUpClass(cls):
        from test.hh import profiles
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kc = cfg._kconfig("shared_components", dict(
                profiles.get("encoder").syms,
                **{kind.shared: True for kind in sc.KINDS.values()}))

    def test_every_kind_names_symbols_kconfig_defines(self):
        import kconfiglib
        for name, kind in sc.KINDS.items():
            with self.subTest(kind=name):
                for sym in (kind.has, kind.shared):
                    self.assertEqual(self.kc.syms[sym].orig_type, kconfiglib.BOOL, sym)
                self.assertEqual(self.kc.syms[kind.name].orig_type, kconfiglib.STRING)
                for export in kind.exports:
                    self.assertEqual(self.kc.syms[export.symbol].orig_type,
                                     kconfiglib.BOOL if export.absent == "n" else kconfiglib.STRING,
                                     export.symbol)


class TestContextKey(Scratch):

    def test_an_owners_name_and_exports_are_part_of_the_key(self):
        self.install(["unit0", "unit1"], unit0=OWNER)
        with self.env("unit1"):
            before = sc.context_key()
        self.install(["unit0", "unit1"], unit0=named(OWNER, "box_buf"))
        with self.env("unit1"):
            renamed = sc.context_key()
        self.install(["unit0", "unit1"],
                     unit0=named(OWNER, "box_buf") + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n")
        with self.env("unit1"):
            changed = sc.context_key()
        self.assertEqual(len({before, renamed, changed}), 3)

    def test_the_units_saved_share_is_part_of_the_key(self):
        self.install(["unit0", "unit1"], unit0=OWNER, unit1=named(SHARER, "unit0"))
        with self.env("unit1"):
            before = sc.context_key()
        self.install(["unit0", "unit1"], unit1=named(SHARER, "box_buf"))
        with self.env("unit1"):
            self.assertNotEqual(sc.context_key(), before)

    def test_nothing_without_a_parent_or_a_saved_config(self):
        with mock.patch.dict(os.environ, dict(KCONFIG_PARENT="", KCONFIG_CONFIG="")):
            self.assertEqual(sc.context_key(), ())


class TestStale(Scratch):

    def stale(self, unit):
        return sc.stale(sc.unit_file(self.parent, unit), self.parent)

    def test_a_sharer_is_stale_until_it_matches_the_owner_it_names(self):
        sharer = named(SHARER, "box_buf")
        self.install(["unit0", "unit1"],
                     unit0=named(OWNER, "box_buf") + "CONFIG_MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=y\n",
                     unit1=sharer)
        self.assertTrue(self.stale("unit1"))
        self.install(["unit0", "unit1"],
                     unit1=sharer + "CONFIG_MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=y #~DEFAULT~#\n")
        self.assertFalse(self.stale("unit1"))

    def test_an_owner_is_found_by_its_name_not_its_unit(self):
        self.install(["unit0", "unit1", "unit2"],
                     unit0=named(OWNER, "unit2") + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=named(SHARER, "unit2"), unit2=OWNER)
        self.assertTrue(self.stale("unit1"))

    def test_without_an_owner_of_that_name_there_is_nothing_to_refresh_to(self):
        self.install(["unit0", "unit1"],
                     unit0=named(OWNER, "box_buf") + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=named(SHARER, "unit0"))
        self.assertFalse(self.stale("unit1"))
        self.install(["unit0", "unit1"], unit1=named(SHARER, ""))
        self.assertFalse(self.stale("unit1"))

    def test_a_unit_that_shares_nothing_is_never_stale(self):
        self.install(["unit0", "unit1"], unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=OWNER)
        self.assertFalse(self.stale("unit1"))
        self.assertFalse(self.stale("unit0"))

    def test_the_command_line_prints_y_or_n(self):
        self.install(["unit0", "unit1"], unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=named(SHARER, "unit0"))
        with mock.patch("builtins.print") as out:
            self.assertEqual(sc.main(["stale", sc.unit_file(self.parent, "unit1"), self.parent]), 0)
        out.assert_called_once_with("y")


if __name__ == "__main__":
    unittest.main()
