# Happy Hare test harness - persisted per-gate lists that don't match the gate count.
#
# Adding gates (a second unit, or more gates on a unit) without the installer migrating
# mmu_vars.cfg leaves the saved lists short. They must be resized, not dropped: dropping
# them reset the gate map and, in Spoolman push mode, unassigned every spool.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

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


class TestMismatchedPersistedGateLists(unittest.TestCase):

    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(path, {
            'mmu__revision': 7,
            'mmu_state_gate_spool_id': [5, 6],
            'mmu_state_gate_material': ['PLA', 'PETG'],
            'mmu_state_gate_color': ['red', 'green', 'blue', 'white', 'black', 'grey'],
            'mmu_state_tool_to_gate_map': [1, 0],
            'mmu_state_endless_spool_groups': [0, 0],
        })
        self.hh = session('nfc_spoolman')
        self.hh._mmu_vars_copy = lambda: path
        # Filament is physically in the gates, so bootup's gate check keeps their spools
        self.hh.boot(gates_loaded_at=TIP_AT_GATE)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu

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


if __name__ == '__main__':
    unittest.main()
