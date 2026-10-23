# Happy Hare test harness - persisted per-gate lists that don't match the gate count.
#
# Adding gates (a second unit, or more gates on a unit) without the installer migrating
# mmu_vars.cfg leaves the saved lists short. They must be resized, not dropped: dropping
# them reset the gate map and, in Spoolman push mode, unassigned every spool. Where the
# saved unit layout is known, gates are matched by unit name and local gate so one unit's
# data never lands on another unit's gate.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import logging
import os
import tempfile
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TIP_AT_GATE = -40.0


def write_vars_file(path, variables):
    parser = configparser.ConfigParser()
    parser.add_section('Variables')
    for name, value in variables.items():
        parser.set('Variables', name, repr(value))
    with open(path, 'w') as f:
        parser.write(f)


def read_vars_file(path):
    parser = configparser.ConfigParser()
    parser.read(path)
    return {name: ast.literal_eval(raw) for name, raw in parser.items('Variables')}


class PersistedGateListsTestCase(unittest.TestCase):

    def boot(self, profile, variables):
        self.path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(self.path, dict({'mmu__revision': 7}, **variables))
        self.hh = session(profile)
        self.hh._mmu_vars_copy = lambda: self.path
        # Filament is physically in the gates, so bootup's gate check keeps their spools
        self.hh.boot(gates_loaded_at=TIP_AT_GATE)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        return self.mmu

    def warnings(self):
        return '\n'.join(line for line in self.hh.console if 'Persisted gate lists' in line)


class TestMismatchedPersistedGateLists(PersistedGateListsTestCase):

    def setUp(self):
        self.boot('nfc_spoolman', {
            'mmu_state_gate_spool_id': [5, 6],
            'mmu_state_gate_material': ['PLA', 'PETG'],
            'mmu_state_gate_color': ['red', 'green', 'blue', 'white', 'black', 'grey'],
            'mmu_state_tool_to_gate_map': [1, 0],
            'mmu_state_endless_spool_groups': [0, 0],
        })

    def test_short_lists_keep_their_gates(self):
        self.assertEqual(self.mmu.num_gates, 4)
        self.assertEqual(self.mmu.gate_spool_id, [5, 6, -1, -1])
        self.assertEqual(self.mmu.gate_material, ['PLA', 'PETG', '', ''])
        self.assertEqual(self.mmu.ttg_map, [1, 0, 2, 3])
        self.assertEqual(self.mmu.gate_maps.endless_spool_groups, [0, 0, 1, 2])

    def test_long_lists_are_trimmed(self):
        self.assertEqual(self.mmu.gate_color, ['red', 'green', 'blue', 'white'])

    def test_spoolman_is_not_sent_an_empty_gate_map(self):
        pushes = [kw['gate_ids'] for name, kw in self.hh.printer.lookup_object('webhooks').calls
                  if name == 'spoolman_push_gate_map']
        self.assertTrue(pushes)
        for gate_ids in pushes:
            self.assertIn((0, 5), gate_ids)
            self.assertIn((1, 6), gate_ids)


class TestInvalidPersistedValues(PersistedGateListsTestCase):

    def test_out_of_range_tool_to_gate_entries_use_the_default(self):
        self.boot('nfc_spoolman', {'mmu_state_tool_to_gate_map': [-10, 1]})
        self.assertEqual(self.mmu.ttg_map, [0, 1, 2, 3])
        self.hh.run_gcode('MMU_TTG_MAP')
        self.assertEqual(self.hh.errors, [])

    def test_invalid_endless_spool_groups_use_the_configured_default(self):
        self.boot('nfc_spoolman', {'mmu_state_endless_spool_groups': 'invalid'})
        self.assertEqual(self.mmu.gate_maps.endless_spool_groups, [0, 1, 2, 3])


class TestUnitAwareResize(PersistedGateListsTestCase):
    """ercf_vvd: unit0 has 9 gates, unit1 has 4."""

    UNIT0 = list(range(10, 19))
    UNIT1 = [20, 21, 22, 23]

    def test_growing_the_first_unit_keeps_the_second_units_data_on_its_gates(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 8], ['unit1', 4]],
            'mmu_state_gate_spool_id': self.UNIT0[:8] + self.UNIT1,
            'mmu_state_gate_material': ['A'] * 8 + ['B'] * 4,
            'mmu_state_tool_to_gate_map': list(range(12)),
            'mmu_state_endless_spool_groups': list(range(12)),
        })
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0[:8] + [-1] + self.UNIT1)
        self.assertEqual(self.mmu.gate_material, ['A'] * 8 + [''] + ['B'] * 4)
        self.assertEqual(self.mmu.ttg_map, list(range(8)) + [9, 10, 11, 12, 12])
        self.assertEqual(self.mmu.gate_maps.endless_spool_groups, list(range(8)) + [12, 8, 9, 10, 11])

    def test_a_unit_missing_from_the_saved_layout_gets_defaults(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 9], ['renamed', 3]],
            'mmu_state_gate_spool_id': self.UNIT0 + self.UNIT1[:3],
        })
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0 + [-1] * 4)

    def test_without_a_layout_an_appended_unit_is_resized_by_position(self):
        self.boot('ercf_vvd', {'mmu_state_gate_spool_id': self.UNIT0})
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0 + [-1] * 4)

    def test_without_a_layout_an_unprovable_change_uses_defaults(self):
        self.boot('ercf_vvd', {'mmu_state_gate_spool_id': self.UNIT0[:8] + self.UNIT1})
        self.assertEqual(self.mmu.gate_spool_id, [-1] * 13)
        self.assertIn('mmu_state_gate_spool_id', self.warnings())

    def test_lists_already_the_right_length_ignore_a_stale_layout(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 8], ['unit1', 5]],
            'mmu_state_gate_spool_id': self.UNIT0 + self.UNIT1,
        })
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0 + self.UNIT1)
        self.assertEqual(self.warnings(), '')

    def test_corrected_lists_and_layout_are_written_at_startup(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 8], ['unit1', 4]],
            'mmu_state_gate_spool_id': self.UNIT0[:8] + self.UNIT1,
        })
        self.hh.reactor.advance(1.0)
        saved = read_vars_file(self.path)
        self.assertEqual(saved['mmu_state_gate_map_units'], [['unit0', 9], ['unit1', 4]])
        self.assertEqual(saved['mmu_state_gate_spool_id'], self.UNIT0[:8] + [-1] + self.UNIT1)


if __name__ == '__main__':
    unittest.main()
