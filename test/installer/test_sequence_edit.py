# Happy Hare installer tests: the model behind menuconfig's sequence_editor.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import re
import tempfile
import unittest

import sequence_edit
from sequence_edit import SequenceModel

VALIDATOR = re.compile(r"[a-z][a-z0-9_-]*")


def model(baseline="a,b,c", current=None, **kwargs):
    base = sequence_edit.split_sequence(baseline, ",")
    cur = base if current is None else sequence_edit.split_sequence(current, ",")
    kwargs.setdefault("validator", VALIDATOR)
    return SequenceModel(base, cur, **kwargs)


class TestOperations(unittest.TestCase):

    def test_unchanged(self):
        m = model()
        self.assertFalse(m.is_changed())
        self.assertFalse(m.structural())
        self.assertEqual(m.summary(), [])

    def test_append_is_not_structural(self):
        m = model()
        self.assertIsNone(m.add("d"))
        self.assertEqual(m.names(), ["a", "b", "c", "d"])
        self.assertEqual(m.origins()["d"], None)
        self.assertTrue(m.is_changed())
        self.assertFalse(m.structural())
        self.assertEqual(m.summary(), ["add d"])

    def test_insert_mid_list_is_structural(self):
        m = model()
        self.assertIsNone(m.add("x", 1))
        self.assertEqual(m.names(), ["a", "x", "b", "c"])
        self.assertTrue(m.structural())

    def test_rename_keeps_origin(self):
        m = model()
        self.assertIsNone(m.rename(1, "box"))
        self.assertEqual(m.origins(), {"a": "a", "box": "b", "c": "c"})
        self.assertEqual(m.summary(), ["rename b -> box"])
        self.assertTrue(m.structural())

    def test_remove_and_restore(self):
        m = model()
        self.assertIsNone(m.remove(1))
        self.assertEqual(m.names(), ["a", "c"])
        self.assertEqual(m.removed, ["b"])
        self.assertEqual(m.summary(), ["remove b"])
        self.assertIsNone(m.restore("b"))
        self.assertEqual(m.names(), ["a", "b", "c"])
        self.assertFalse(m.is_changed())

    def test_adding_a_removed_name_restores_it(self):
        m = model()
        m.remove(0)
        self.assertIsNone(m.add("a", 0))
        self.assertFalse(m.is_changed())

    def test_removing_a_new_entry_drops_it(self):
        m = model()
        m.add("d")
        m.remove(3)
        self.assertFalse(m.is_changed())

    def test_move(self):
        m = model()
        self.assertIsNone(m.move(0, 1))
        self.assertEqual(m.names(), ["b", "a", "c"])
        self.assertEqual(m.summary(), ["reorder"])
        self.assertTrue(m.structural())
        self.assertIsNone(m.move(0, -1))  # Already first: no-op
        self.assertEqual(m.names(), ["b", "a", "c"])

    def test_swap_by_rename_differs_from_swap_by_move(self):
        renamed = model("a,b")
        renamed.rename(0, "tmp")
        renamed.rename(1, "a")
        renamed.rename(0, "b")
        moved = model("a,b")
        moved.move(0, 1)
        self.assertEqual(renamed.names(), moved.names())
        self.assertEqual(renamed.origins(), {"b": "a", "a": "b"})
        self.assertEqual(moved.origins(), {"b": "b", "a": "a"})

    def test_reset(self):
        m = model()
        m.rename(0, "x")
        m.remove(1)
        m.reset()
        self.assertFalse(m.is_changed())


class TestValidation(unittest.TestCase):

    def test_duplicates_rejected(self):
        m = model()
        self.assertIn("already", m.add("b"))
        self.assertIn("already", m.rename(0, "c"))
        self.assertEqual(m.names(), ["a", "b", "c"])

    def test_empty_rejected(self):
        self.assertIsNotNone(model().add(""))

    def test_last_entry_cannot_be_removed(self):
        m = model("a,b")
        self.assertIsNone(m.remove(0))
        self.assertIn("at least one", m.remove(0))
        self.assertEqual(m.names(), ["b"])

    def test_validator_applies_to_new_and_renamed_names(self):
        m = model()
        self.assertIn("not valid", m.add("Box"))
        self.assertIn("not valid", m.rename(0, "1st"))

    def test_existing_names_are_not_revalidated(self):
        m = model("Legacy,b")
        self.assertIsNone(m.move(0, 1))
        self.assertEqual(m.names(), ["b", "Legacy"])


class TestAppendOnly(unittest.TestCase):

    def test_structural_changes_refused(self):
        m = model(append_only=True)
        for error in (m.rename(0, "x"), m.remove(0), m.move(0, 1), m.add("x", 0)):
            self.assertIn("appending", error)
        self.assertFalse(m.is_changed())

    def test_append_allowed_after_a_pending_change(self):
        m = model(current="a,x,c", append_only=True)
        self.assertTrue(m.structural())
        self.assertIsNone(m.add("d"))
        self.assertIn("appending", m.rename(1, "y"))
        self.assertEqual(m.names(), ["a", "x", "c", "d"])

    def test_append_allowed(self):
        m = model(append_only=True)
        self.assertIsNone(m.add("d"))
        self.assertIsNone(m.rename(3, "e"))
        self.assertIsNone(m.remove(3))
        self.assertFalse(m.is_changed())


class TestChangesSidecar(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = sequence_edit.changes_path(os.path.join(self.tmp.name, ".mmu_config"), "MMU_UNITS")

    def test_path_is_derived_from_symbol(self):
        self.assertTrue(self.path.endswith(".mmu_config.MMU_UNITS.changes"))

    def test_round_trip(self):
        m = model()
        m.rename(0, "x")
        m.remove(1)
        sequence_edit.write_changes(self.path, m)
        loaded = SequenceModel.load(["a", "b", "c"], ["x", "c"], sequence_edit.read_changes(self.path))
        self.assertEqual(loaded.origins(), {"x": "a", "c": "c"})
        self.assertEqual(loaded.removed, ["b"])

    def test_unchanged_removes_sidecar(self):
        m = model()
        m.rename(0, "x")
        sequence_edit.write_changes(self.path, m)
        m.reset()
        sequence_edit.write_changes(self.path, m)
        self.assertFalse(os.path.exists(self.path))

    def test_sidecar_ignored_when_value_does_not_match(self):
        m = model()
        m.rename(0, "x")
        sequence_edit.write_changes(self.path, m)
        # Edited by hand after the sidecar was written
        loaded = SequenceModel.load(["a", "b", "c"], ["y", "b", "c"], sequence_edit.read_changes(self.path))
        self.assertEqual(loaded.origins(), {"y": None, "b": "b", "c": "c"})
        self.assertEqual(loaded.removed, ["a"])

    def test_sidecar_ignored_when_baseline_does_not_match(self):
        m = model()
        m.rename(0, "x")
        sequence_edit.write_changes(self.path, m)
        self.assertIsNone(sequence_edit.origins_from_changes(
            sequence_edit.read_changes(self.path), ["q", "b", "c"], ["x", "b", "c"]))

    def test_corrupt_sidecar_ignored(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(sequence_edit.read_changes(self.path))


if __name__ == "__main__":
    unittest.main()
