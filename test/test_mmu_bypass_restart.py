# Happy Hare MMU Software
#
# Goal: Bypass selection (and the filament in it) must survive a restart on a physical selector
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging
import unittest

from test.hh import session
from test.test_mmu_persisted_gate_lists import PersistedGateListsTestCase

logging.getLogger().setLevel(logging.CRITICAL)

TOOL_GATE_BYPASS = -2
FILAMENT_POS_LOADED = 10

# Vars as in issue #1517: tradrack restarted with bypass selected and filament loaded.
# last_pos was never written, and bypass offset is the -1 placeholder.
OFFSETS = [0.0, 17.0, 34.0, 51.0, 68.0, 85.0, 102.0, 119.0, 136.0, 153.0, 170.0, 187.0]


class TestBypassSurvivesRestart(PersistedGateListsTestCase):

    def boot_with_bypass(self, bypass_offset):
        self.boot('tradrack', {
            'mmu_state_gate_selected': TOOL_GATE_BYPASS,
            'mmu_state_tool_selected': TOOL_GATE_BYPASS,
            'mmu_state_filament_pos': FILAMENT_POS_LOADED,
            'mmu_unit0_selector_offsets': OFFSETS,
            'mmu_unit0_selector_bypass_offset': bypass_offset,
            'mmu_unit0_selector_last_pos': None,
        }, calibrate=True)

    def test_bypass_is_reselected_after_restart(self):
        self.boot_with_bypass(-1)
        self.assertEqual((self.mmu.gate_selected, self.mmu.tool_selected),
                         (TOOL_GATE_BYPASS, TOOL_GATE_BYPASS),
                         '\n'.join(self.hh.console[-40:]))

    def test_bypass_is_reselected_after_restart_with_calibrated_bypass(self):
        self.boot_with_bypass(204.0)
        self.assertEqual((self.mmu.gate_selected, self.mmu.tool_selected),
                         (TOOL_GATE_BYPASS, TOOL_GATE_BYPASS),
                         '\n'.join(self.hh.console[-40:]))


if __name__ == '__main__':
    unittest.main()
