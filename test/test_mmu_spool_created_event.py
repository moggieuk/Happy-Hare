# Happy Hare test harness - the "spool_created" macro event.
#
# When Moonraker created a Spoolman spool from a scanned tag, it calls back MMU_GATE_MAP with
# CREATED=1. Happy Hare then calls _MMU_EVENT EVENT=spool_created with the new spool and its
# gate (-1 for a shared reader, whose gate is only known once the spool loads), so a user
# macro can act on a new spool (for example offer a calibration).
#
#   ./venv/bin/python -m unittest test.test_mmu_spool_created_event
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest import mock

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class SpoolCreatedEventTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu

    def tearDown(self):
        self.hh.close()

    def events(self, gcode):
        with mock.patch.object(self.mmu, 'mmu_macro_event') as event:
            self.hh.run_gcode(gcode)
        return [c.args for c in event.call_args_list if c.args[0] == 'spool_created']

    def test_per_gate_lookup_that_created_a_spool(self):
        self.assertEqual(self.events('MMU_GATE_MAP GATE=1 SPOOLID=7 CREATED=1 QUIET=1'),
                         [('spool_created', 'GATE=1 SPOOLID=7')])

    def test_per_gate_lookup_of_a_known_spool(self):
        self.assertEqual(self.events('MMU_GATE_MAP GATE=1 SPOOLID=7 QUIET=1'), [])

    def test_shared_reader_lookup_that_created_a_spool(self):
        self.mmu.p.spoolman_support = 'push'
        self.assertEqual(self.events('MMU_GATE_MAP NEXT_SPOOLID=9 CREATED=1 QUIET=1'),
                         [('spool_created', 'GATE=-1 SPOOLID=9')])

    def test_shared_reader_lookup_of_a_known_spool(self):
        self.mmu.p.spoolman_support = 'push'
        self.assertEqual(self.events('MMU_GATE_MAP NEXT_SPOOLID=9 QUIET=1'), [])


if __name__ == '__main__':
    unittest.main()
