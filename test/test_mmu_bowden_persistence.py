# Happy Hare test harness - calibrated bowden length persistence across restarts.
#
# The regression: MMU_CALIBRATE_BOWDEN persisted mmu_<unit>_bowden_lengths but never
# mmu_<unit>_bowden_home. On the next boot adjust_bowden_lengths_on_homing_change()
# saw current_home=None, treated it as "the gate homing endstop changed", and applied
# a spurious +/-gate_endstop_to_encoder to every length - and with encoder homing the
# result could go negative, which the following boot's x<0->UNCALIBRATED coercion
# turned into -1 (the calibration "reset" on restart). A stale v3-era reference value
# (e.g. mmu_gate) had the same effect via the load-time validity check: it rejected
# the whole load and rewrote -1, forever, because the only code path that re-stamps
# the home is gated behind a successful load.
#
# A "restart" here is a second Session pointed at the first session's mmu_vars.cfg -
# the same file the printer would re-read after a power cycle.
#
# Run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_bowden_persistence
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


def read_vars_file(path):
    """Parse mmu_vars.cfg off disk - the only assertion that proves durability."""
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section('Variables'):
        return {}
    out = {}
    for name, raw in parser.items('Variables'):
        try:
            out[name] = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            out[name] = raw
    return out


def write_vars_file(path, variables):
    parser = configparser.ConfigParser()
    parser.add_section('Variables')
    for name, value in variables.items():
        parser.set('Variables', name, repr(value))
    with open(path, 'w') as f:
        parser.write(f)


class BowdenRestartTestCase(unittest.TestCase):
    """
    Boots share one mmu_vars.cfg: boot #n+1 reads what boot #n left on disk.
    """

    def _boot(self, profile, vars_file=None, endstop_to_encoder=None):
        hh = session(profile)
        if vars_file is not None:
            # Point [save_variables] at the previous session's file: build() (step 2) resolves
            # the filename through this seam (test/hh/bootstrap.py:_mmu_vars_copy)
            hh._mmu_vars_copy = lambda: vars_file
        hh.build()
        if endstop_to_encoder is not None:
            hh.mmu.mmu_machine.units[0].p.gate_endstop_to_encoder = endstop_to_encoder
        hh.boot()
        self.assertEqual(hh.errors, [])
        self.addCleanup(hh.close)
        return hh

    def _calibrate_all(self, hh, length):
        unit = hh.mmu.mmu_unit()
        for gate in range(unit.first_gate, unit.first_gate + unit.num_gates):
            unit.calibrator.update_bowden_length(length, gate=gate)
        hh.reactor.advance(0.)
        return hh.save_variables.filename

    # -- the v3-era stale reference (the reported field bug) -----------------

    def test_stale_v3_bowden_home_keeps_lengths_and_re_stamps(self):
        # A v3 -> v4 migration that carried 'mmu_gate' (the v3 gate endstop name)
        # into the v4 namespaced home variable. Previously: rejected at load, -1
        # rewritten every boot, home never repaired.
        path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(path, {
            'mmu__revision': 7,
            'mmu_unit0_bowden_lengths': [123.4, 123.4, 123.4, 123.4],
            'mmu_unit0_bowden_home': 'mmu_gate',
        })

        hh = self._boot('boxturtle', vars_file=path)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * unit.num_gates)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [123.4] * unit.num_gates)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], unit.p.gate_homing_endstop)

        # And it sticks on the following boot
        hh2 = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths,
                         [123.4] * unit.num_gates)

    # -- calibration records its reference endstop ---------------------------

    def test_calibration_persists_bowden_home(self):
        hh = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], 'mmu_shared_exit')

    # -- restarts are a no-op -------------------------------------------------

    def test_calibrated_lengths_survive_restart(self):
        hh = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)

        hh2 = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_restart_with_endstop_offset_does_not_adjust(self):
        # gate_endstop_to_encoder is set (machine has both a gate endstop and an
        # encoder) but nothing changed between calibration and boot. Previously the
        # first boot applied +/-offset anyway because the home was never recorded.
        hh = self._boot('boxturtle', endstop_to_encoder=47.0)
        path = self._calibrate_all(hh, 123.4)

        hh2 = self._boot('boxturtle', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_encoder_restart_short_bowden_does_not_reset(self):
        # Encoder homing: the spurious -offset made short bowdens negative, and the
        # next boot's negative->UNCALIBRATED coercion wrote the -1s the user saw.
        hh = self._boot('encoder', endstop_to_encoder=47.0)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.p.gate_homing_endstop, 'encoder')
        path = self._calibrate_all(hh, 30.0)

        hh2 = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [30.0] * 4)

    # -- a genuine endstop change still adjusts -------------------------------

    def test_live_endstop_change_still_adjusts_lengths(self):
        # Guard: the fix must not neuter the legitimate adjustment when the endstop
        # actually changes after calibration.
        hh = self._boot('boxturtle', endstop_to_encoder=47.0)
        self._calibrate_all(hh, 123.4)
        unit = hh.mmu.mmu_unit()

        unit.p.set_param('gate_homing_endstop', 'encoder')
        self.assertEqual(unit.calibrator._bowden_lengths, [76.4] * 4)

        unit.p.set_param('gate_homing_endstop', 'mmu_shared_exit')
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * 4)


if __name__ == '__main__':
    unittest.main()
