# installer/lib/kconfiglib/shared_components.py: what a unit parse reads from the other
# units' saved configs. The Kconfig behavior built on it is in test_shared_buffer.py
# (TestSharedSyncFeedbackBuffer); these pin the reader itself.

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

# A real parse, for the shared_slots variable the functions read
with cfg._env(cfg._SINGLE_UNIT_ENV):
    KCONFIG = cfg._kconfig("shared_components", {})

OWNER = "CONFIG_MMU_HAS_SYNC_FEEDBACK_BUFFER=y\n"
SHARER = OWNER + 'CONFIG_MMU_SHARED_SYNC_FEEDBACK_BUFFER=y\n'


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


class TestSlots(Scratch):

    def test_nothing_is_read_without_a_parent(self):
        self.install(["unit0", "unit1"], unit0=OWNER)
        with self.env("unit1", KCONFIG_PARENT=""):
            self.assertEqual(sc.slots("buffer"), [])
            self.assertEqual(sc.context_key(), ())
            self.assertFalse(sc.unresolved("buffer"))

    def test_owners_and_unconfigured_units_are_offered_in_unit_order(self):
        self.install(["unit0", "unit1", "unit2", "unit3"],
                     unit0="CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n" + OWNER,
                     unit1="", unit2=SHARER)
        with self.env("unit2"):
            found = [(s.unit, s.member, s.owner) for s in sc.slots("buffer")]
            self.assertEqual(found, [("unit0", "CHOICE_SHARED_BUFFER_UNIT0", True),
                                     ("unit3", "CHOICE_SHARED_BUFFER_UNIT3", False)])
            self.assertEqual([sc.label(s) for s in sc.slots("buffer")],
                             ["unit0", "unit3 (not configured yet)"])

    def test_the_unit_being_configured_is_never_offered(self):
        self.install(["unit0", "unit1"], unit0=OWNER, unit1=OWNER)
        with self.env("unit1"):
            self.assertEqual([s.unit for s in sc.slots("buffer")], ["unit0"])

    def test_names_that_map_to_one_symbol_get_distinct_members(self):
        self.install(["box-1", "box_1", "me"], **{"box-1": OWNER, "box_1": OWNER})
        with self.env("me"):
            self.assertEqual([s.member for s in sc.slots("buffer")],
                             ["CHOICE_SHARED_BUFFER_BOX_1", "CHOICE_SHARED_BUFFER_BOX_1_2"])

    def test_a_rewritten_file_is_read_again(self):
        self.install(["unit0", "unit1"], unit0=OWNER)
        path = sc.unit_file(self.parent, "unit0")
        with self.env("unit1"):
            self.assertEqual(sc.slots("buffer")[0].values.get("MMU_HAS_SENSOR_BUFFER_TENSION"), None)
            self.write(path, OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y #~DEFAULT~#\n")
            later = time.time() + 10
            os.utime(path, (later, later))
            self.assertEqual(sc.slots("buffer")[0].values["MMU_HAS_SENSOR_BUFFER_TENSION"], "y")

    def test_the_pick_list_length_comes_from_the_root_kconfig(self):
        self.assertEqual(sc._limit(KCONFIG), 4)

    def test_the_functions_are_registered_with_kconfig(self):
        for name in sc.FUNCTIONS:
            self.assertIn(name, kconfigfunctions.functions)


class TestUnresolved(Scratch):

    def test_a_saved_name_that_is_not_offered_is_unresolved(self):
        self.install(["unit0", "unit1"], unit0="",
                     unit1=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="gone"\n')
        with self.env("unit1"):
            self.assertTrue(sc.unresolved("buffer"))
            self.assertEqual(sc.shared_saved_member(KCONFIG, None, "buffer"),
                             "CHOICE_SHARED_BUFFER_UNRESOLVED")

    def test_a_saved_name_that_is_offered_selects_its_member(self):
        self.install(["unit0", "unit1"], unit0=OWNER,
                     unit1=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit0" #~DEFAULT~#\n')
        with self.env("unit1"):
            self.assertFalse(sc.unresolved("buffer"))
            self.assertEqual(sc.shared_saved_member(KCONFIG, None, "buffer"),
                             "CHOICE_SHARED_BUFFER_UNIT0")

    def test_an_owner_beyond_the_last_slot_is_unresolved_not_re_pointed(self):
        # First install of a big machine: units not configured yet fill the slots first
        limit = sc._limit(KCONFIG)
        units = ["unit%d" % i for i in range(limit + 2)]
        self.install(units, unit0=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="%s"\n'
                     % units[-1], **{units[-1]: OWNER})
        with self.env("unit0"):
            self.assertNotIn(units[-1], [s.unit for s in sc.slots("buffer", limit=limit)])
            self.assertTrue(sc.unresolved("buffer", limit))
            self.assertEqual(sc.shared_saved_member(KCONFIG, None, "buffer"),
                             "CHOICE_SHARED_BUFFER_UNRESOLVED")

    def test_an_owner_is_never_unresolved(self):
        self.install(["unit0", "unit1"], unit0=OWNER,
                     unit1=OWNER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit1"\n')
        with self.env("unit1"):
            self.assertFalse(sc.unresolved("buffer"))


class TestStale(Scratch):

    def stale(self, unit):
        return sc.stale(sc.unit_file(self.parent, unit), self.parent)

    def test_a_sharer_is_stale_until_it_matches_its_owner(self):
        named = SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit0"\n'
        self.install(["unit0", "unit1"],
                     unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=y\n", unit1=named)
        self.assertTrue(self.stale("unit1"))
        self.install(["unit0", "unit1"],
                     unit1=named + "CONFIG_MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=y #~DEFAULT~#\n")
        self.assertFalse(self.stale("unit1"))

    def test_without_a_readable_owner_there_is_nothing_to_refresh_to(self):
        self.install(["unit0", "unit1"], unit0="CONFIG_MMU_TYPE_TRADRACK_1_0=y\n",
                     unit1=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit0"\n')
        self.assertFalse(self.stale("unit1"))
        self.install(["unit0", "unit1"],
                     unit1=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit9"\n')
        self.assertFalse(self.stale("unit1"))

    def test_a_unit_that_shares_nothing_is_never_stale(self):
        self.install(["unit0", "unit1"], unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=OWNER)
        self.assertFalse(self.stale("unit1"))
        self.assertFalse(self.stale("unit0"))

    def test_the_command_line_prints_y_or_n(self):
        self.install(["unit0", "unit1"], unit0=OWNER + "CONFIG_MMU_HAS_SENSOR_BUFFER_TENSION=y\n",
                     unit1=SHARER + 'CONFIG_PARAM_SYNC_FEEDBACK_BUFFER_NAME="unit0"\n')
        with mock.patch("builtins.print") as out:
            self.assertEqual(sc.main(["stale", sc.unit_file(self.parent, "unit1"), self.parent]), 0)
        out.assert_called_once_with("y")


if __name__ == "__main__":
    unittest.main()
