# Happy Hare: installer intent and the complete startup migration transaction.
# This file may be distributed under the terms of the GNU GPLv3 license.
import copy
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from test.hh import session
from test.hh.root import install

install()
from test.test_mmu_persisted_gate_lists import read_vars_file, write_vars_file
from extras.mmu import mmu_state_migration as sm

PENDING = sm.C.VARS_MMU_PENDING_MIGRATION
SPOOLS = sm.C.VARS_MMU_GATE_SPOOL_ID
LAYOUT = sm.C.VARS_MMU_GATE_MAP_UNITS


def intent(frm, to, origin, old, new, ident='install-1', encoders=None):
    return dict(zip(('from', 'to', 'origin', 'old_gates', 'new_gates', 'migration_id', 'encoders'),
                    (frm, to, origin, old, new, ident, encoders or {})))


def rename_intent():
    return intent(['old'], ['unit0'], {'unit0': 'old'}, {'old': 3}, {'unit0': 4},
                  encoders={'old': 'unit0'})


def source_vars():
    return {'mmu__revision': 7, LAYOUT: [['old', 3]], SPOOLS: [10, 11, 12],
            'mmu_old_gear_rotation_distances': [22.0, 23.0, 24.0],
            'mmu_old_encoder_resolution': 0.7,
            'mmu_state_sensor_enabled': {'old:mmu_shared_exit': False, 'mmu_exit_1': False},
            'other_macro_value': {'preserve': 42}}


class TestIntentJournal(unittest.TestCase):
    def test_installer_only_appends_intent_and_retry_is_idempotent(self):
        original = source_vars()
        queued, _ = sm.queue_migration(original, rename_intent())
        self.assertEqual({k: v for k, v in queued.items() if k != PENDING}, original)
        self.assertNotIn(PENDING, original)
        self.assertEqual(sm.queue_migration(queued, rename_intent())[0], queued)
        conflicting = rename_intent()
        conflicting['new_gates']['unit0'] = 5
        with self.assertRaises(ValueError):
            sm.queue_migration(queued, conflicting)

    def test_malformed_or_discontinuous_history_is_rejected_without_changes(self):
        queued, _ = sm.queue_migration(source_vars(), rename_intent())
        before = copy.deepcopy(queued)
        bad = intent(['other'], ['unit1'], {'unit1': 'other'}, {'other': 4}, {'unit1': 4}, 'install-2')
        with self.assertRaises(ValueError):
            sm.queue_migration(queued, bad)
        self.assertEqual(queued, before)
        for change in (lambda p: p.update(version=99),
                       lambda p: p['steps'][0]['origin'].update(unit0='missing'),
                       lambda p: p['steps'][0]['old_gates'].update(old=-1)):
            invalid = copy.deepcopy(queued)
            change(invalid[PENDING])
            snapshot = copy.deepcopy(invalid)
            with self.assertRaises(ValueError):
                sm.migrate_state(invalid, [['unit0', 4]], {SPOOLS: [-1] * 4})
            self.assertEqual(invalid, snapshot)
        with self.assertRaises(ValueError):
            sm.migrate_state(queued, [['wrong_config', 4]], {SPOOLS: [-1] * 4})

    def test_deleted_unit_name_reused_for_new_unit_does_not_inherit_state(self):
        v = {LAYOUT: [['a', 2], ['b', 2]], SPOOLS: [10, 11, 20, 21],
             'mmu_a_bowden_lengths': [500, 500], 'mmu_b_bowden_lengths': [600, 600]}
        v, _ = sm.queue_migration(v, intent(['a', 'b'], ['b'], {'b': 'b'},
                                           {'a': 2, 'b': 2}, {'b': 2}))
        v, _ = sm.queue_migration(v, intent(['b'], ['a', 'b'], {'a': None, 'b': 'b'},
                                           {'b': 2}, {'a': 2, 'b': 2}, 'install-2'))
        result, _, _ = sm.migrate_state(v, [['a', 2], ['b', 2]], {SPOOLS: [-1] * 4})
        self.assertEqual(result[SPOOLS], [-1, -1, 20, 21])
        self.assertNotIn('mmu_a_bowden_lengths', result)
        self.assertEqual(result['mmu_b_bowden_lengths'], [600, 600])
        self.assertNotIn(PENDING, result)

    def test_composed_installs_follow_identity_through_rename_reuse_and_resize(self):
        rng = random.Random(1453)
        for case in range(200):
            units = [('a', 2, 'original-a'), ('b', 3, 'original-b')]
            original = [(token, local) for _, count, token in units for local in range(count)]
            v = {LAYOUT: [[n, c] for n, c, _ in units], SPOOLS: list(range(5))}
            if case % 2:
                del v[LAYOUT]
            for step in range(5):
                before = list(units)
                origin = {n: n for n, _, _ in units}
                op = rng.choice(['rename', 'reorder', 'append', 'remove', 'resize'])
                i = rng.randrange(len(units))
                names = [n for n in 'abcdef' if n not in origin]
                if op == 'rename' and names:
                    name, count, token = units[i]
                    new = rng.choice(names)
                    units[i] = (new, count, token)
                    del origin[name]
                    origin[new] = name
                elif op == 'reorder':
                    rng.shuffle(units)
                elif op == 'append' and names:
                    new = rng.choice(names)
                    units.append((new, rng.randint(1, 5), 'new-%d' % step))
                    origin[new] = None
                elif op == 'remove' and len(units) > 1:
                    name, _, _ = units.pop(i)
                    del origin[name]
                elif op == 'resize':
                    name, _, token = units[i]
                    units[i] = (name, rng.randint(1, 5), token)
                v, _ = sm.queue_migration(v, intent(
                    [n for n, _, _ in before], [n for n, _, _ in units], origin,
                    {n: c for n, c, _ in before}, {n: c for n, c, _ in units}, str(step)))
            final = [(token, local) for _, count, token in units for local in range(count)]
            expected = [original.index(identity) if identity in original else -1 for identity in final]
            layout = [[n, c] for n, c, _ in units]
            actual, _, _ = sm.migrate_state(v, layout, {SPOOLS: [-1] * len(final)})
            self.assertEqual(actual[SPOOLS], expected, 'sequence %d' % case)
            self.assertEqual(sm.migrate_state(actual, layout, {SPOOLS: [-1] * len(final)})[0], actual)


class TestStartupTransaction(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'mmu_vars.cfg')
        self.queued, _ = sm.queue_migration(source_vars(), rename_intent())
        write_vars_file(self.path, self.queued)

    def machine(self):
        hh = session('boxturtle')
        hh._mmu_vars_copy = lambda: self.path
        self.addCleanup(hh.close)
        return hh

    def assert_migrated(self, saved):
        self.assertNotIn(PENDING, saved)
        self.assertNotIn('mmu_old_gear_rotation_distances', saved)
        self.assertEqual(saved['mmu_unit0_gear_rotation_distances'][:3], [22.0, 23.0, 24.0])
        self.assertEqual(saved['mmu_unit0_encoder_resolution'], 0.7)
        self.assertEqual(saved[SPOOLS], [10, 11, 12, -1])
        self.assertEqual(saved[LAYOUT], [['unit0', 4]])
        self.assertEqual(saved['mmu_state_sensor_enabled'],
                         {'unit0:mmu_shared_exit': False, 'mmu_exit_1': False})
        self.assertEqual(saved['other_macro_value'], {'preserve': 42})

    def test_all_changes_are_staged_before_ready_then_saved_with_receipt(self):
        hh = self.machine()
        hh.build()
        self.assert_migrated(hh.save_variables.allVariables)
        self.assertEqual(read_vars_file(self.path), self.queued)
        hh.boot(gates_loaded_at=-40.0)
        hh.reactor.advance(1.0)
        saved = read_vars_file(self.path)
        self.assert_migrated(saved)
        hh.close()
        again = self.machine()
        again.boot(gates_loaded_at=-40.0)
        self.assert_migrated(read_vars_file(self.path))

    def test_loading_gate_maps_does_not_alias_the_saved_lists(self):
        hh = self.machine()
        hh.boot(gates_loaded_at=-40.0)
        hh.reactor.advance(1.0)
        maps = hh.mmu.gate_maps
        maps.load_persisted_state()
        for key, attr in ((SPOOLS, 'gate_spool_id'),
                          (sm.C.VARS_MMU_TOOL_TO_GATE_MAP, 'ttg_map'),
                          (sm.C.VARS_MMU_ENDLESS_SPOOL_GROUPS, 'endless_spool_groups')):
            before = list(hh.save_variables.allVariables[key])
            getattr(maps, attr)[0] = 999
            self.assertEqual(hh.save_variables.allVariables[key], before, key)

    def test_failed_early_flush_leaves_intent_for_next_startup(self):
        hh = self.machine()
        hh.build()
        with patch.object(hh.printer.lookup_object('mmu_machine').var_manager, '_write_now', side_effect=OSError('disk unavailable')):
            hh.close()
        self.assertEqual(read_vars_file(self.path), self.queued)
        retry = self.machine()
        retry.boot(gates_loaded_at=-40.0)
        self.assert_migrated(read_vars_file(self.path))

    def test_disconnect_before_ready_can_only_write_a_complete_migration(self):
        hh = self.machine()
        hh.build()
        hh.close()
        saved = read_vars_file(self.path)
        # Klipper variants may reject SAVE_VARIABLE before ready. Either outcome must
        # be retryable, never new namespace keys with the old intent/layout.
        if PENDING in saved:
            self.assertEqual(saved, self.queued)
        else:
            self.assert_migrated(saved)

    def test_wrong_configuration_rejects_intent_without_touching_disk(self):
        self.queued[PENDING]['steps'][-1]['to'] = ['wrong']
        self.queued[PENDING]['steps'][-1]['origin'] = {'wrong': 'old'}
        self.queued[PENDING]['steps'][-1]['new_gates'] = {'wrong': 4}
        write_vars_file(self.path, self.queued)
        hh = self.machine()
        with self.assertRaisesRegex(Exception, 'does not match configured units'):
            hh.build()
        hh.close()
        self.assertEqual(read_vars_file(self.path), self.queued)
