# Happy Hare MMU Software
#
# Goal: A selector on a non-critical (Kalico) mcu that is unplugged must not be touched at startup
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, os, tempfile
import unittest
from unittest import mock

from test.hh import session
from test.test_mmu_persisted_gate_lists import PersistedGateListsTestCase, write_vars_file

logging.getLogger().setLevel(logging.CRITICAL)

OFFSETS = [0.0, 17.0, 34.0, 51.0, 68.0, 85.0, 102.0, 119.0, 136.0, 153.0, 170.0, 187.0]


class TestDisconnectedNonCriticalMcu(PersistedGateListsTestCase):

    def boot_tradrack(self, disconnected):
        self.path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(self.path, {
            'mmu__revision': 7,
            'mmu_unit0_selector_offsets': OFFSETS,
            'mmu_unit0_selector_last_pos': 17.0,
        })
        self.hh = session('tradrack')
        self.hh._mmu_vars_copy = lambda: self.path
        self.hh.build()
        # Kalico sets this on an is_non_critical mcu that is unplugged
        patcher = mock.patch.object(type(self.hh.printer.lookup_object('mcu')),
                                    'non_critical_disconnected', disconnected, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.mmu = self.hh.mmu
        return self.mmu.mmu_machine.units[0].selector

    def test_position_not_restored_when_mcu_disconnected(self):
        selector = self.boot_tradrack(True)
        self.assertFalse(selector.is_homed, '\n'.join(self.hh.console[-40:]))

    def test_position_restored_when_mcu_connected(self):
        selector = self.boot_tradrack(False)
        self.assertTrue(selector.is_homed, '\n'.join(self.hh.console[-40:]))


if __name__ == '__main__':
    unittest.main()
