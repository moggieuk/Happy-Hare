# Happy Hare test harness - clearing a gate must reach Moonraker clients.
#
# Klipper's webhooks only sends a status field when it differs from the value it sent last
# (webhooks.py _do_query: `rd != lres.get(ri)`), and it keeps that last value BY REFERENCE.
# A gate map list that is changed in place therefore compares equal to itself and is never
# sent: Mainsail/Fluidd keep showing the material, color and spool of a gate that is now
# empty, while a fresh query shows it cleared.
#
#   ./venv/bin/python -m unittest test.test_mmu_gate_map_status
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import copy
import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

GATE_EMPTY = 0
GATE_AVAILABLE = 1

# Attributes set up below that clear_gate_attributes() resets when a gate becomes empty
CLEARED_FIELDS = ('gate_material', 'gate_color', 'gate_filament_name', 'gate_temperature',
                  'gate_spool_id')


class TestClearedGateReachesWebhooks(unittest.TestCase):

    GATE = 1    # Not the selected gate, so nothing else touches it

    def setUp(self):
        self.hh = session('boxturtle')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.gm = self.mmu.gate_maps
        # The entry sensor has to read "present", otherwise a gate map update that
        # validates the gate would clear it before the test does
        self.set_entry_sensor(True)
        self.hh.run_gcode('MMU_GATE_MAP GATE=%d AVAILABLE=1 MATERIAL=PETG COLOR=abcdef '
                          'NAME=Galaxy TEMP=240 SPOOLID=42 QUIET=1' % self.GATE)
        self.assertEqual(self.gm.gate_status[self.GATE], GATE_AVAILABLE)
        self.assertEqual(self.gm.gate_material[self.GATE], 'PETG')
        self.assertEqual(self.gm.gate_spool_id[self.GATE], 42)

    def set_entry_sensor(self, present):
        self.hh.sensor('mmu_entry_%d' % self.GATE).sensor.runout_helper.filament_present = present

    def fields_webhooks_would_send(self, change):
        """
        Run change() between two get_status() calls. Returns (changed, sent): the fields
        whose value really changed, and the fields Klipper's webhooks would push - those
        that differ from the last value kept the way webhooks keeps it, as the very
        object get_status() returned.
        """
        last = self.mmu.get_status(self.hh.reactor.monotonic())
        snapshot = copy.deepcopy(last)
        change()
        now = self.mmu.get_status(self.hh.reactor.monotonic())
        changed = {field for field, value in now.items() if value != snapshot.get(field)}
        sent = {field for field, value in now.items() if value != last.get(field)}
        return changed, sent

    def assert_cleared_and_sent(self, result):
        changed, sent = result
        self.assertEqual(self.gm.gate_status[self.GATE], GATE_EMPTY)
        for field in CLEARED_FIELDS:
            self.assertIn(field, changed)
        self.assertEqual(sorted(changed - sent), [], 'changed fields never reach Moonraker clients')

    def test_set_gate_status_empty_sends_cleared_attributes(self):
        result = self.fields_webhooks_would_send(
            lambda: self.gm.set_gate_status(self.GATE, GATE_EMPTY))
        self.assert_cleared_and_sent(result)

    def test_validate_gate_status_empty_sends_cleared_attributes(self):
        self.set_entry_sensor(False)
        result = self.fields_webhooks_would_send(
            lambda: self.gm.validate_gate_status([self.GATE]))
        self.assert_cleared_and_sent(result)

    def test_sensor_remove_sends_cleared_attributes(self):
        """The path from the report: the entry sensor clears and marks the gate empty."""
        self.set_entry_sensor(False)
        result = self.fields_webhooks_would_send(
            lambda: self.hh.run_gcode('__MMU_SENSOR_REMOVE SENSOR=mmu_entry_%d GATE=%d'
                                      % (self.GATE, self.GATE)))
        self.assert_cleared_and_sent(result)


if __name__ == '__main__':
    unittest.main()
