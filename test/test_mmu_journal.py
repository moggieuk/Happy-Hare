# Happy Hare MMU Software
# Tests for SaveVariableJournal (extras/mmu/mmu_utils.py)
#
# Klipper's SAVE_VARIABLE snapshots its variable dict and renders every repr() BEFORE it
# writes, then discards the live dict and re-reads the file. Anything set in between is
# missing from the snapshot and is then erased by the re-read - from disk and from memory,
# with no error. The journal exists so that cannot lose an update.
#
# mmu_utils.py is loaded by path because extras/ is a klipper extras directory, not an
# importable package. It only imports copy+math, so it loads fine standalone - which is the
# whole reason the journal lives there rather than in mmu.py.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), '..', 'extras', 'mmu', 'mmu_utils.py')
_spec = importlib.util.spec_from_file_location('mmu_utils_under_test', _PATH)
mmu_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mmu_utils)


class FakeSaveVariables:
    """Just the one attribute Happy Hare touches, plus a way to fake klipper's re-read."""

    def __init__(self, initial=None):
        self.allVariables = dict(initial or {})

    def reload_as(self, on_disk):
        """What loadVariables() does: replace the dict wholesale with the file contents."""
        self.allVariables = dict(on_disk)


class TestSaveVariableJournal(unittest.TestCase):

    def setUp(self):
        self.sv = FakeSaveVariables({'mmu__revision': 1})
        self.journal = mmu_utils.SaveVariableJournal(self.sv)

    # -- the core invariant ----------------------------------------------------------

    def test_entry_is_dropped_once_it_is_seen_on_disk(self):
        self.journal.record('mmu_a', 1)
        self.journal.apply()
        self.sv.reload_as({'mmu__revision': 2, 'mmu_a': 1})  # the write landed
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {})

    def test_entry_the_reload_dropped_is_kept_and_reapplied(self):
        """The whole point: a value that missed the snapshot survives in the journal."""
        self.journal.record('mmu_lost', 42)
        self.journal.apply()
        self.sv.reload_as({'mmu__revision': 2})             # 'mmu_lost' never made it
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {'mmu_lost': 42})
        self.assertEqual(self.sv.allVariables['mmu_lost'], 42, 'not restored to memory')

    def test_a_stale_value_on_disk_does_not_count_as_persisted(self):
        self.journal.record('mmu_a', 'new')
        self.sv.reload_as({'mmu_a': 'old'})
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {'mmu_a': 'new'})

    # -- deletes ---------------------------------------------------------------------

    def test_delete_is_dropped_once_the_key_is_gone_from_disk(self):
        self.journal.record_delete('mmu_gone')
        self.journal.apply()
        self.sv.reload_as({'mmu__revision': 2})
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {})

    def test_delete_is_retried_if_the_key_came_back(self):
        self.journal.record_delete('mmu_gone')
        self.journal.apply()
        self.assertNotIn('mmu_gone', self.sv.allVariables)
        self.sv.reload_as({'mmu_gone': 'resurrected'})       # the delete missed the snapshot
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {'mmu_gone': mmu_utils.DELETED})
        self.assertNotIn('mmu_gone', self.sv.allVariables, 'not re-removed from memory')

    # -- snapshotting ----------------------------------------------------------------

    def test_journal_is_not_disturbed_by_later_mutation_of_the_callers_object(self):
        """
        Callers pass live objects (gate maps, stats) and keep mutating them. Without a
        private copy the journal could never tell a dropped write from a later edit.
        """
        live = [1, 2, 3]
        self.journal.record('mmu_list', live)
        live.append(4)
        self.assertEqual(self.journal.pending['mmu_list'], [1, 2, 3])

        self.sv.reload_as({'mmu_list': [1, 2, 3]})           # what was actually written
        self.journal.reconcile()
        self.assertEqual(self.journal.pending, {}, 'should have reconciled against the copy')

    def test_nested_containers_are_deep_copied(self):
        live = {'gates': [{'id': 0}]}
        self.journal.record('mmu_nested', live)
        live['gates'][0]['id'] = 99
        self.assertEqual(self.journal.pending['mmu_nested'], {'gates': [{'id': 0}]})

    def test_scalars_pass_straight_through(self):
        for value in (1, 2.5, 'text', None, True):
            self.assertIs(mmu_utils.SaveVariableJournal.snapshot(value), value)

    def test_sets_are_not_treated_as_containers(self):
        """
        Deliberate: repr(set()) is "set()", which ast.literal_eval rejects, so klipper
        cannot round-trip one. Copying it here would imply support that does not exist.
        """
        value = {1, 2}
        self.assertIs(mmu_utils.SaveVariableJournal.snapshot(value), value)

    # -- the race detector -----------------------------------------------------------

    def test_mutations_counter_moves_on_every_change(self):
        start = self.journal.mutations
        self.journal.record('mmu_a', 1)
        self.journal.record_delete('mmu_b')
        self.assertEqual(self.journal.mutations, start + 2)

    # -- apply() ---------------------------------------------------------------------

    def test_apply_repairs_a_dict_a_foreign_reload_replaced(self):
        self.journal.record('mmu_a', 1)
        self.journal.record_delete('mmu_b')
        self.sv.reload_as({'blobifier': 'someone elses write', 'mmu_b': 'stale'})
        self.journal.apply()
        self.assertEqual(self.sv.allVariables['mmu_a'], 1)
        self.assertNotIn('mmu_b', self.sv.allVariables)
        self.assertEqual(self.sv.allVariables['blobifier'], 'someone elses write',
                         'must not disturb keys we do not own')


if __name__ == '__main__':
    unittest.main()
