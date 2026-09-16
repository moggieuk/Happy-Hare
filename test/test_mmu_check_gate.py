# Regression coverage for the gate-check UI wrapper parameter forwarding.

import logging
import unittest
from unittest.mock import patch

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class TestCheckGate(unittest.TestCase):

    def boot(self):
        hh = session('boxturtle')
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.errors, [])
        hh.printer.harness_macro_effects['MMU__CHECK_GATE'] = (
            lambda macro, gcmd: macro.run_body(gcmd)
        )
        return hh

    def test_wrapper_checks_requested_gates(self):
        for params, expected in (
            ('', [2]),
            ('GATE=1', [1]),
            ('TOOL=1', [1]),
            ('GATES=0,1', [0, 1]),
            ('TOOLS=0,1', [0, 1]),
            ('ALL=1 QUIET=1', [0, 1, 2, 3]),
        ):
            with self.subTest(params=params):
                hh = self.boot()
                for gate in range(hh.mmu.num_gates):
                    hh.place_filament(gate, position=-40.0)
                hh.mmu.select_gate(2)
                checked = []
                load_gate = hh.mmu._load_gate

                def record_load(*args, **kwargs):
                    checked.append(hh.mmu.gate_selected)
                    return load_gate(*args, **kwargs)

                with patch.object(hh.mmu, '_load_gate', side_effect=record_load):
                    hh.run_gcode('MMU__CHECK_GATE ' + params)
                self.assertEqual(hh.errors, [])
                self.assertEqual(checked, expected)
                self.assertTrue(all(hh.mmu.gate_status[g] >= 1 for g in expected))
                hh.close()

    def test_empty_tools_remains_a_noop(self):
        hh = self.boot()
        with patch.object(hh.mmu, '_load_gate') as load:
            hh.run_gcode('MMU__CHECK_GATE TOOLS=')
        load.assert_not_called()
        self.assertEqual(hh.errors, [])
