# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Test installer intent and the complete startup state migration transaction
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import copy
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from test.hh import session, profiles
from test.hh.root import install

install()
from test.test_mmu_persisted_gate_lists import read_vars_file, write_vars_file
from extras.mmu import mmu_state_migration as sm

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
    def test_history_append_is_immutable_and_retry_is_idempotent(self):
        history = sm.append_history(None, rename_intent())
        before = copy.deepcopy(history)
        self.assertEqual(sm.append_history(history, rename_intent()), history)
        conflicting = rename_intent()
        conflicting['new_gates']['unit0'] = 5
        with self.assertRaises(ValueError):
            sm.append_history(history, conflicting)
        self.assertEqual(history, before)

    def test_malformed_or_discontinuous_history_is_rejected_without_changes(self):
        history = sm.append_history(None, rename_intent())
        before = copy.deepcopy(history)
        original = source_vars()
        bad = intent(['other'], ['unit1'], {'unit1': 'other'}, {'other': 4}, {'unit1': 4}, 'install-2')
        with self.assertRaises(ValueError):
            sm.append_history(history, bad)
        self.assertEqual(history, before)
        for change in (lambda p: p.update(version=99),
                       lambda p: p['steps'][0]['origin'].update(unit0='missing'),
                       lambda p: p['steps'][0]['old_gates'].update(old=-1)):
            invalid = copy.deepcopy(history)
            change(invalid)
            snapshot = copy.deepcopy(invalid)
            with self.assertRaises(ValueError):
                sm.migrate_state(original, [['unit0', 4]], {SPOOLS: [-1] * 4}, invalid)
            self.assertEqual(invalid, snapshot)
            self.assertEqual(original, source_vars())
        with self.assertRaises(ValueError):
            sm.migrate_state(original, [['wrong_config', 4]], {SPOOLS: [-1] * 4}, history)

    def test_deleted_unit_name_reused_for_new_unit_does_not_inherit_state(self):
        v = {LAYOUT: [['a', 2], ['b', 2]], SPOOLS: [10, 11, 20, 21],
             'mmu_a_bowden_lengths': [500, 500], 'mmu_b_bowden_lengths': [600, 600]}
        history = sm.append_history(None, intent(['a', 'b'], ['b'], {'b': 'b'},
                                           {'a': 2, 'b': 2}, {'b': 2}))
        history = sm.append_history(history, intent(['b'], ['a', 'b'], {'a': None, 'b': 'b'},
                                           {'b': 2}, {'a': 2, 'b': 2}, 'install-2'))
        result, _, _ = sm.migrate_state(v, [['a', 2], ['b', 2]], {SPOOLS: [-1] * 4}, history)
        self.assertEqual(result[SPOOLS], [-1, -1, 20, 21])
        self.assertNotIn('mmu_a_bowden_lengths', result)
        self.assertEqual(result['mmu_b_bowden_lengths'], [600, 600])

    def test_composed_installs_follow_identity_through_rename_reuse_and_resize(self):
        rng = random.Random(1453)
        for case in range(200):
            units = [('a', 2, 'original-a'), ('b', 3, 'original-b')]
            original = [(token, local) for _, count, token in units for local in range(count)]
            v = {LAYOUT: [[n, c] for n, c, _ in units], SPOOLS: list(range(5))}
            if case % 2:
                del v[LAYOUT]
            history = None
            surviving = set(original)
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
                surviving.intersection_update((token, local) for _, count, token in units for local in range(count))
                history = sm.append_history(history, intent(
                    [n for n, _, _ in before], [n for n, _, _ in units], origin,
                    {n: c for n, c, _ in before}, {n: c for n, c, _ in units}, str(step)))
            final = [(token, local) for _, count, token in units for local in range(count)]
            expected = [original.index(identity) if identity in surviving else -1 for identity in final]
            layout = [[n, c] for n, c, _ in units]
            actual, _, _ = sm.migrate_state(v, layout, {SPOOLS: [-1] * len(final)}, history)
            self.assertEqual(actual[SPOOLS], expected, 'sequence %d' % case)
            self.assertEqual(sm.migrate_state(actual, layout, {SPOOLS: [-1] * len(final)}, history)[0], actual)


class TestStartupTransaction(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'mmu_vars.cfg')
        self.history = sm.append_history(None, rename_intent())
        self.queued = source_vars()
        write_vars_file(self.path, self.queued)

    def machine(self, profile_name='boxturtle'):
        profile = profiles.get(profile_name).derive('config-history',
                    syms={'PARAM_STATE_MIGRATION': repr(self.history)})
        hh = session(profile)
        hh._mmu_vars_copy = lambda: self.path
        self.addCleanup(hh.close)
        return hh

    def assert_migrated(self, saved):
        self.assertEqual(saved[sm.C.VARS_MMU_MIGRATION_APPLIED], self.history['steps'][-1]['id'])
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

    def test_recorded_rename_preserves_spoolman_push_and_selection(self):
        self.history = sm.append_history(None, intent(['old'], ['unit0'], {'unit0': 'old'},
                                                      {'old': 4}, {'unit0': 4}))
        self.queued = {LAYOUT: [['old', 4]], SPOOLS: [5, 6, 7, 8], 'mmu__revision': 7,
                       sm.C.VARS_MMU_GATE_SELECTED: 3, sm.C.VARS_MMU_TOOL_SELECTED: 3,
                       sm.C.VARS_MMU_SENSOR_ENABLED: {'mmu_exit_3': False}}
        write_vars_file(self.path, self.queued)
        hh = self.machine('nfc_spoolman')
        hh.boot(gates_loaded_at=-40.0)
        saved = read_vars_file(self.path)
        self.assertEqual(saved[SPOOLS], [5, 6, 7, 8])
        self.assertEqual(saved[sm.C.VARS_MMU_GATE_SELECTED], 3)
        self.assertEqual(saved[sm.C.VARS_MMU_TOOL_SELECTED], 3)
        self.assertEqual(saved[sm.C.VARS_MMU_SENSOR_ENABLED], {'mmu_exit_3': False})
        pushes = [kw['gate_ids'] for name, kw in hh.printer.lookup_object('webhooks').calls
                  if name == 'spoolman_push_gate_map']
        self.assertTrue(pushes)
        for gate_ids in pushes:
            self.assertEqual(gate_ids, [(0, 5), (1, 6), (2, 7), (3, 8)])

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
        if sm.C.VARS_MMU_MIGRATION_APPLIED not in saved:
            self.assertEqual(saved, self.queued)
        else:
            self.assert_migrated(saved)

    def test_wrong_configuration_rejects_intent_without_touching_disk(self):
        self.history['steps'][-1]['to'] = ['wrong']
        self.history['steps'][-1]['origin'] = {'wrong': 'old'}
        self.history['steps'][-1]['new_gates'] = {'wrong': 4}
        write_vars_file(self.path, self.queued)
        hh = self.machine()
        with self.assertRaisesRegex(Exception, 'does not match configured units'):
            hh.build()
        hh.close()
        self.assertEqual(read_vars_file(self.path), self.queued)


class TestUnrecordedRename(unittest.TestCase):
    def test_unrecorded_renames_reject_before_changing_state(self):
        for layout, current in (([['old', 4]], [['unit0', 4]]),
                                ([['old', 9], ['unit1', 4]], [['unit0', 9], ['unit1', 4]])):
            with self.subTest(layout=layout):
                count = sum(n for _, n in layout)
                original = {LAYOUT: layout, SPOOLS: list(range(5, 5 + count)),
                            sm.C.VARS_MMU_SENSOR_ENABLED: {'mmu_exit_3': False},
                            sm.C.VARS_MMU_GATE_SELECTED: 3, sm.C.VARS_MMU_TOOL_SELECTED: 3}
                before = copy.deepcopy(original)
                with self.assertRaisesRegex(ValueError, 'Unrecorded.*old.*unit0'):
                    sm.migrate_state(original, current, {SPOOLS: [-1] * count})
                self.assertEqual(original, before)

    def test_ambiguous_startup_never_reaches_ready_or_writes_defaults(self):
        for profile, layout in (('nfc_spoolman', [['old', 4]]),
                                ('ercf_vvd', [['old', 9], ['unit1', 4]])):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'mmu_vars.cfg')
                original = {LAYOUT: layout, SPOOLS: list(range(5, 5 + sum(n for _, n in layout))),
                            'mmu__revision': 7}
                write_vars_file(path, original)
                hh = session(profile)
                hh._mmu_vars_copy = lambda: path
                try:
                    with self.assertRaisesRegex(Exception, 'Unrecorded'):
                        hh.boot(gates_loaded_at=-40.0)
                finally:
                    hh.close()
                self.assertEqual(read_vars_file(path), original)
                calls = hh.printer.lookup_object('webhooks').calls
                self.assertFalse(any(name == 'spoolman_push_gate_map' for name, _ in calls))

    def test_pending_append_cannot_hide_an_earlier_unrecorded_rename(self):
        original = {LAYOUT: [['old', 4]], SPOOLS: [5, 6, 7, 8]}
        history = sm.append_history(None, intent(
            ['unit0'], ['unit0', 'unit1'], {'unit0': 'unit0', 'unit1': None},
            {'unit0': 4}, {'unit0': 4, 'unit1': 4}))
        before = copy.deepcopy(original)
        with self.assertRaisesRegex(ValueError, 'Unrecorded'):
            sm.migrate_state(original, [['unit0', 4], ['unit1', 4]], {SPOOLS: [-1] * 8}, history)
        self.assertEqual(original, before)

    def test_explicit_replacement_and_tombstones_do_not_inherit_state(self):
        original = {LAYOUT: [['old', 4]], SPOOLS: [5, 6, 7, 8]}
        history = sm.append_history(None, intent(
            ['old'], ['unit0'], {'unit0': None}, {'old': 4}, {'unit0': 4}))
        result, _, _ = sm.migrate_state(original, [['unit0', 4]], {SPOOLS: [-1] * 4}, history)
        self.assertEqual(result[SPOOLS], [-1] * 4)
        original[LAYOUT] = [['', 4]]
        result, _, _ = sm.migrate_state(original, [['unit0', 4]], {SPOOLS: [-1] * 4})
        self.assertEqual(result[SPOOLS], [-1] * 4)


class TestConfigCheckpoint(unittest.TestCase):
    def setUp(self):
        self.first = sm.append_history(None, intent(['a'], ['b'], {'b': 'a'}, {'a': 2}, {'b': 2}))
        self.both = sm.append_history(self.first, intent(['b'], ['c'], {'c': 'b'}, {'b': 2}, {'c': 2}, 'install-2'))
        self.original = {LAYOUT: [['a', 2]], SPOOLS: [5, 6], 'mmu_a_bowden_lengths': [10, 20]}

    def migrate(self, values, history, name):
        return sm.migrate_state(values, [[name, 2]], {SPOOLS: [-1, -1]}, history)[0]

    def test_only_new_steps_apply_after_checkpoint(self):
        first = self.migrate(self.original, self.first, 'b')
        first['mmu_b_bowden_lengths'] = [30, 40]  # calibration changed since the first migration
        result = self.migrate(first, self.both, 'c')
        self.assertEqual(result['mmu_c_bowden_lengths'], [30, 40])
        self.assertEqual(result[SPOOLS], [5, 6])
        self.assertEqual(self.migrate(result, self.both, 'c'), result)
        self.assertEqual(result[sm.C.VARS_MMU_MIGRATION_APPLIED], 'install-2')

    def test_multiple_installs_before_startup_replay_all_steps(self):
        result = self.migrate(self.original, self.both, 'c')
        self.assertEqual(result['mmu_c_bowden_lengths'], [10, 20])
        self.assertEqual(result[SPOOLS], [5, 6])

    def test_missing_or_replaced_history_rejects_saved_checkpoint(self):
        migrated = self.migrate(self.original, self.both, 'c')
        snapshot = copy.deepcopy(migrated)
        for history in (None, {}, self.first):
            with self.subTest(history=history), self.assertRaisesRegex(ValueError, 'checkpoint is missing'):
                self.migrate(migrated, history, 'c')
        self.assertEqual(migrated, snapshot)

    def test_empty_variables_initialize_to_current_configuration(self):
        result = self.migrate({}, self.both, 'c')
        self.assertEqual(result[SPOOLS], [-1, -1])
        self.assertEqual(result[LAYOUT], [['c', 2]])
        self.assertEqual(result[sm.C.VARS_MMU_MIGRATION_APPLIED], 'install-2')

    def test_recorded_shrink_then_growth_does_not_restore_removed_gates(self):
        history = sm.append_history(None, intent(['a'], ['b'], {'b': 'a'}, {'a': 4}, {'b': 2}))
        history = sm.append_history(history, intent(['b'], ['c'], {'c': 'b'},
                                                    {'b': 2}, {'c': 4}, 'install-2'))
        values = {LAYOUT: [['a', 4]], SPOOLS: [5, 6, 7, 8],
                  sm.C.VARS_MMU_SENSOR_ENABLED: {'mmu_exit_3': False},
                  sm.C.VARS_MMU_TOOL_TO_GATE_MAP: [3, 1, 0, 2]}
        result = sm.migrate_state(values, [['c', 4]],
                                 {SPOOLS: [-1] * 4, sm.C.VARS_MMU_TOOL_TO_GATE_MAP: list(range(4))}, history)[0]
        self.assertEqual(result[SPOOLS], [5, 6, -1, -1])
        self.assertEqual(result[sm.C.VARS_MMU_SENSOR_ENABLED], {})
        self.assertEqual(result[sm.C.VARS_MMU_TOOL_TO_GATE_MAP], [0, 1, 0, 3])

    def test_invalid_saved_layout_during_rename_uses_installed_counts(self):
        for layout in ([[['unhashable'], 2]], [['a', -1]], [['a', True]]):
            with self.subTest(layout=layout):
                result = self.migrate(dict(self.original, **{LAYOUT: layout}), self.first, 'b')
                self.assertEqual(result[SPOOLS], [5, 6])

    def test_applied_history_does_not_block_manual_gate_growth(self):
        result = self.migrate(self.original, self.first, 'b')
        grown = sm.migrate_state(result, [['b', 3]], {SPOOLS: [-1] * 3}, self.first)[0]
        self.assertEqual(grown[SPOOLS], [5, 6, -1])

    def test_history_is_immutable_and_invalid_text_is_rejected(self):
        before = copy.deepcopy(self.both)
        self.migrate(self.original, repr(self.both), 'c')
        self.assertEqual(self.both, before)
        with self.assertRaisesRegex(ValueError, 'Invalid MMU state_migration'):
            self.migrate(self.original, 'not a literal', 'c')
