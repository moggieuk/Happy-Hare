# Happy Hare test harness - persisted per-gate lists that don't match the gate count.
#
# Adding gates (a second unit, or more gates on a unit) without the installer migrating
# mmu_vars.cfg leaves the saved lists short. Dropping them reset the gate map and, in
# Spoolman push mode, unassigned every spool. With the unit layout the lists were saved
# with, gates are matched by unit name and local gate so one unit's data never lands on
# another unit's gate. Without it a gate's identity can't be established, so the lists
# fall back to defaults as before.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import logging
import os
import tempfile
import unittest

from test.hh import cfg, session

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

    def boot(self, profile, variables, **boot_options):
        self.path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(self.path, dict({'mmu__revision': 7}, **variables))
        self.hh = session(profile)
        self.hh._mmu_vars_copy = lambda: self.path
        # Filament is physically in the gates, so bootup's gate check keeps their spools
        self.hh.boot(gates_loaded_at=TIP_AT_GATE, **boot_options)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        return self.mmu

    def warnings(self):
        return '\n'.join(line for line in self.hh.console if 'Persisted gate lists' in line)


class TestMismatchedPersistedGateLists(PersistedGateListsTestCase):

    def setUp(self):
        self.boot('nfc_spoolman', {
            'mmu_state_gate_map_units': [['unit0', 2]],
            'mmu_state_gate_spool_id': [5, 6],
            'mmu_state_gate_material': ['PLA', 'PETG'],
            'mmu_state_tool_to_gate_map': [1, 0],
            'mmu_state_endless_spool_groups': [0, 0],
        })

    def test_short_lists_keep_their_gates(self):
        self.assertEqual(self.mmu.num_gates, 4)
        self.assertEqual(self.mmu.gate_spool_id, [5, 6, -1, -1])
        self.assertEqual(self.mmu.gate_material, ['PLA', 'PETG', '', ''])
        self.assertEqual(self.mmu.ttg_map, [1, 0, 2, 3])
        self.assertEqual(self.mmu.gate_maps.endless_spool_groups, [0, 0, 1, 2])

    def test_spoolman_is_not_sent_an_empty_gate_map(self):
        pushes = [kw['gate_ids'] for name, kw in self.hh.printer.lookup_object('webhooks').calls
                  if name == 'spoolman_push_gate_map']
        self.assertTrue(pushes)
        for gate_ids in pushes:
            self.assertIn((0, 5), gate_ids)
            self.assertIn((1, 6), gate_ids)


class TestSingleUnitWithoutAMatchingList(PersistedGateListsTestCase):

    def test_long_lists_are_trimmed(self):
        self.boot('nfc_spoolman', {
            'mmu_state_gate_map_units': [['unit0', 6]],
            'mmu_state_gate_color': ['red', 'green', 'blue', 'white', 'black', 'grey'],
        })
        self.assertEqual(self.mmu.gate_color, ['red', 'green', 'blue', 'white'])

    def test_without_a_layout_a_mismatched_list_uses_defaults(self):
        self.boot('nfc_spoolman', {'mmu_state_gate_spool_id': [5, 6]})
        self.assertEqual(self.mmu.gate_spool_id, [-1, -1, -1, -1])
        self.assertIn('mmu_state_gate_spool_id', self.warnings())


class TestInvalidPersistedValues(PersistedGateListsTestCase):

    def test_out_of_range_tool_to_gate_entries_use_the_default(self):
        self.boot('nfc_spoolman', {'mmu_state_tool_to_gate_map': [-10, 1, 2, 9]})
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

    def test_growing_the_first_unit_moves_sensor_disables_and_resets_the_selection(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 8], ['unit1', 4]],
            'mmu_state_gate_spool_id': self.UNIT0[:8] + self.UNIT1,
            'mmu_state_sensor_enabled': {'mmu_exit_9': False, 'unit1:filament_tension': False},
            'mmu_state_gate_selected': 11,
            'mmu_state_tool_selected': 11,
        })
        # unit1's second gate was 9 and is now 10
        self.assertEqual(self.mmu.var_manager.get('mmu_state_sensor_enabled', None),
                         {'mmu_exit_10': False, 'unit1:filament_tension': False})
        sensors = self.mmu.sensor_manager.all_sensors_map
        self.assertFalse(sensors['mmu_exit_10'].runout_helper.sensor_enabled)
        self.assertTrue(sensors['mmu_exit_9'].runout_helper.sensor_enabled)
        # Gate 11 was unit1's last gate; it is now its third, so the selection can't stand
        self.assertNotEqual(self.mmu.gate_selected, 11)
        self.assertNotEqual(self.mmu.tool_selected, 11)
        # ...and the drop is saved with the new layout, so a restart can't bring it back
        self.hh.reactor.advance(1.0)
        saved = read_vars_file(self.path)
        self.assertEqual(saved['mmu_state_gate_map_units'], [['unit0', 9], ['unit1', 4]])
        self.assertEqual((saved['mmu_state_gate_selected'], saved['mmu_state_tool_selected']), (-1, -1))

    def test_a_renumbered_gate_cant_position_another_units_selector(self):
        # Saved with unit1 first: gate 2 was unit1's, now it is unit0's. unit0 has no position of
        # its own on record, so it would otherwise infer one from the old global gate
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit1', 4], ['unit0', 9]],
            'mmu_state_gate_spool_id': self.UNIT1 + self.UNIT0,
            'mmu_state_gate_selected': 2,
            'mmu_state_tool_selected': 2,
        }, calibrate=True)
        unit0 = self.mmu.mmu_machine.units[0]
        self.assertFalse(unit0.selector.is_homed)
        self.assertIsNone(self.mmu.var_manager.get('mmu_selector_last_pos', None, namespace='unit0'))

    def test_a_unit_missing_from_the_saved_layout_gets_defaults(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 9], ['renamed', 3]],
            'mmu_state_gate_spool_id': self.UNIT0 + self.UNIT1[:3],
        })
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0 + [-1] * 4)

    def test_without_a_layout_a_length_matching_the_first_unit_uses_defaults(self):
        # Saved by a 5 + 4 gate machine: nine entries, the same as the new unit0
        self.boot('ercf_vvd', {'mmu_state_gate_spool_id': self.UNIT0[:5] + self.UNIT1})
        self.assertEqual(self.mmu.gate_spool_id, [-1] * 13)
        self.assertIn('mmu_state_gate_spool_id', self.warnings())

    def test_a_saved_layout_is_followed_even_when_the_length_already_matches(self):
        # Saved by an 8 + 5 gate machine: unit0's ninth gate is new, unit1's fifth is gone
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 8], ['unit1', 5]],
            'mmu_state_gate_spool_id': self.UNIT0[:8] + [18] + self.UNIT1,
        })
        self.assertEqual(self.mmu.gate_spool_id, self.UNIT0[:8] + [-1, 18] + self.UNIT1[:3])
        self.assertIn('mmu_state_gate_spool_id', self.warnings())

    def test_a_layout_matching_the_units_loads_as_is_without_a_warning(self):
        self.boot('ercf_vvd', {
            'mmu_state_gate_map_units': [['unit0', 9], ['unit1', 4]],
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



class TestInstallerThenStartup(PersistedGateListsTestCase):
    """
    Unit renames and reorders: the installer only renames per-unit keys and records the old
    layout under the new names, then Happy Hare maps and resizes the gate lists at startup.
    """

    def migrate(self, variables, frm, to, origin, old_gates, new_gates):
        cfg._prepare_imports() # The installer imports the vendored kconfiglib helpers
        from installer import unit_migration
        install = {"from": frm, "to": to, "origin": origin, "old_gates": old_gates,
                   "new_gates": new_gates, "encoders": {}}
        migrated, _ = unit_migration.migrate_vars(variables, install)
        for var in ('mmu_state_gate_spool_id', 'mmu_state_tool_to_gate_map'):
            self.assertEqual(migrated[var], variables[var], 'the installer leaves %s to startup' % var)
        return migrated

    def single_unit(self, old_gates):
        spools = list(range(1, old_gates + 1))
        migrated = self.migrate({
            'mmu_state_gate_spool_id': spools,
            'mmu_state_tool_to_gate_map': list(range(old_gates)),
            'mmu_old_bowden_lengths': [500.0] * old_gates,
        }, ['old'], ['unit0'], {'unit0': 'old'}, {'old': old_gates}, {'unit0': 4})
        self.assertIn('mmu_unit0_bowden_lengths', migrated)
        self.assertNotIn('mmu_old_bowden_lengths', migrated)
        self.boot('boxturtle', migrated)
        return spools

    def test_rename_only(self):
        spools = self.single_unit(4)
        self.assertEqual(self.mmu.gate_spool_id, spools)
        self.assertEqual(self.warnings(), '')
        self.hh.reactor.advance(1.0)
        self.assertEqual(read_vars_file(self.path)['mmu_state_gate_map_units'], [['unit0', 4]])

    def test_rename_and_one_more_gate(self):
        self.single_unit(3)
        self.assertEqual(self.mmu.gate_spool_id, [1, 2, 3, -1])

    def test_appending_a_gate_keeps_the_selection(self):
        self.boot('boxturtle', {
            'mmu_state_gate_map_units': [['unit0', 3]],
            'mmu_state_gate_spool_id': [1, 2, 3],
            'mmu_state_gate_selected': 1,
            'mmu_state_tool_selected': 1,
        })
        self.assertEqual(self.mmu.gate_spool_id, [1, 2, 3, -1])
        self.assertEqual(self.mmu.gate_selected, 1)

    def test_rename_and_one_gate_fewer(self):
        self.single_unit(5)
        self.assertEqual(self.mmu.gate_spool_id, [1, 2, 3, 4])

    def reversed_units(self, old_unit0_gates):
        unit1, unit0 = [20, 21, 22, 23], list(range(10, 10 + old_unit0_gates))
        migrated = self.migrate({
            'mmu_state_gate_spool_id': unit1 + unit0,
            'mmu_state_tool_to_gate_map': list(range(4 + old_unit0_gates)),
            'mmu_unit0_bowden_lengths': [600.0] * old_unit0_gates,
            'mmu_unit1_bowden_lengths': [700.0] * 4,
        }, ['unit1', 'unit0'], ['unit0', 'unit1'], {'unit0': 'unit0', 'unit1': 'unit1'},
            {'unit1': 4, 'unit0': old_unit0_gates}, {'unit0': 9, 'unit1': 4})
        self.assertEqual(migrated['mmu_unit1_bowden_lengths'], [700.0] * 4)
        self.boot('ercf_vvd', migrated)
        return unit0, unit1

    def test_units_reversed(self):
        unit0, unit1 = self.reversed_units(9)
        self.assertEqual(self.mmu.gate_spool_id, unit0 + unit1)
        # T0-T3 were unit1's gates, now 9-12
        self.assertEqual(self.mmu.ttg_map, [9, 10, 11, 12] + list(range(9)))

    def test_units_reversed_and_the_first_one_more_gate(self):
        unit0, unit1 = self.reversed_units(8)
        self.assertEqual(self.mmu.gate_spool_id, unit0 + [-1] + unit1)
        self.assertEqual(self.mmu.ttg_map, [9, 10, 11, 12] + list(range(8)) + [12])

    def test_units_reversed_then_renamed_before_klipper_starts(self):
        unit1, unit0 = [20, 21, 22, 23], list(range(10, 19))
        variables = {
            'mmu_state_gate_spool_id': unit1 + unit0,
            'mmu_state_tool_to_gate_map': list(range(13)),
        }
        reordered = self.migrate(variables, ['unit1', 'old'], ['old', 'unit1'], {'old': 'old', 'unit1': 'unit1'},
                                 {'unit1': 4, 'old': 9}, {'old': 9, 'unit1': 4})
        renamed = self.migrate(reordered, ['old', 'unit1'], ['unit0', 'unit1'], {'unit0': 'old', 'unit1': 'unit1'},
                               {'old': 9, 'unit1': 4}, {'unit0': 9, 'unit1': 4})
        self.assertEqual(renamed['mmu_state_gate_map_units'], [['unit1', 4], ['unit0', 9]])
        self.boot('ercf_vvd', renamed)
        self.assertEqual(self.mmu.gate_spool_id, unit0 + unit1)

    def test_unit_added_then_renamed_before_klipper_starts(self):
        unit0 = list(range(10, 19))
        variables = {
            'mmu_state_gate_spool_id': unit0,
            'mmu_state_tool_to_gate_map': list(range(9)),
        }
        added = self.migrate(variables, ['unit0'], ['unit0', 'new'], {'unit0': 'unit0', 'new': None},
                             {'unit0': 9}, {'unit0': 9, 'new': 4})
        renamed = self.migrate(added, ['unit0', 'new'], ['unit0', 'unit1'], {'unit0': 'unit0', 'unit1': 'new'},
                               {'unit0': 9, 'new': 4}, {'unit0': 9, 'unit1': 4})
        self.assertEqual(renamed['mmu_state_gate_map_units'], [['unit0', 9]])
        self.boot('ercf_vvd', renamed)
        self.assertEqual(self.mmu.gate_spool_id, unit0 + [-1] * 4)

if __name__ == '__main__':
    unittest.main()
