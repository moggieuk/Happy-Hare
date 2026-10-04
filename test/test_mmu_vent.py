# Happy Hare enclosure venting macro (_MMU_VENT) tests.
#
# The drying cycle calls heater_vent_macro; these run the shipped macro body.

import unittest

from test.hh import session


class TestVentMacro(unittest.TestCase):

    def setUp(self):
        self.hh = session('qidi')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.hh.printer.harness_macro_effects['_MMU_VENT'] = lambda macro, gcmd: macro.run_body(gcmd)

    def _vent(self, params=''):
        at = len(self.hh.gcode.executed)
        self.hh.run_gcode(('_MMU_VENT ' + params).strip())
        return self.hh.gcode.executed[at:]

    def test_per_gate_vent_names_the_gates(self):
        executed = self._vent('GATES=0,1')
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(any('dry filaments in gates: 0, 1' in line for line in executed), executed)


if __name__ == '__main__':
    unittest.main()
