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
        """Boots one session, returns (session, gcode_output) - the output is every
        respond_info/respond_raw line emitted during the boot, i.e. the warnings."""
        hh = session(profile)
        if vars_file is not None:
            # Point [save_variables] at the previous session's file: build() (step 2) resolves
            # the filename through this seam (test/hh/bootstrap.py:_mmu_vars_copy)
            hh._mmu_vars_copy = lambda: vars_file
        hh.build()
        output = []
        hh.gcode.register_output_handler(output.append)
        if endstop_to_encoder is not None:
            hh.mmu.mmu_machine.units[0].p.gate_endstop_to_encoder = endstop_to_encoder
        hh.boot()
        self.assertEqual(hh.errors, [])
        self.addCleanup(hh.close)
        return hh, output

    def _seed_vars(self, variables):
        path = os.path.join(tempfile.mkdtemp(), 'mmu_vars.cfg')
        write_vars_file(path, {'mmu__revision': 7, **variables})
        return path

    def _calibrate_all(self, hh, length):
        unit = hh.mmu.mmu_unit()
        for gate in range(unit.first_gate, unit.first_gate + unit.num_gates):
            unit.calibrator.update_bowden_length(length, gate=gate)
        hh.reactor.advance(0.)
        return hh.save_variables.filename

    # -- the v3-era stale reference (the reported field bug) -----------------

    def _stale_home_vars(self, length):
        # A v3 -> v4 migration that carried 'mmu_gate' (the v3 gate endstop name)
        # into the v4 namespaced home variable. Previously: rejected at load, -1
        # rewritten every boot, home never repaired.
        return self._seed_vars({
            'mmu_unit0_bowden_lengths': [length] * 4,
            'mmu_unit0_bowden_home': 'mmu_gate',
        })

    def test_stale_v3_bowden_home_keeps_lengths_and_re_stamps(self):
        path = self._stale_home_vars(123.4)

        hh, _ = self._boot('boxturtle', vars_file=path)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * unit.num_gates)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [123.4] * unit.num_gates)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], unit.p.gate_homing_endstop)

        # And it sticks on the following boot
        hh2, _ = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths,
                         [123.4] * unit.num_gates)

    def test_stale_v3_bowden_home_with_endstop_offset_keeps_lengths(self):
        # The stored 'mmu_gate' must not be treated as a changed endstop: with a
        # nonzero gate_endstop_to_encoder the adjustment would add the offset to
        # the calibrated lengths on the recovery boot.
        path = self._stale_home_vars(123.4)

        hh, _ = self._boot('boxturtle', vars_file=path, endstop_to_encoder=47.0)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * unit.num_gates)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [123.4] * unit.num_gates)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], 'mmu_shared_exit')

    def test_stale_v3_bowden_home_encoder_restart_does_not_reset(self):
        # Encoder homing: treating the stale reference as a changed endstop
        # subtracts the offset, making a short bowden negative - which the next
        # boot's negative->UNCALIBRATED coercion turns into -1.
        path = self._stale_home_vars(30.0)

        hh, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)

        hh2, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)
        self.assertEqual(read_vars_file(path)['mmu_unit0_bowden_lengths'], [30.0] * 4)

    # -- calibration records its reference endstop ---------------------------

    def test_calibration_persists_bowden_home(self):
        hh, _ = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], 'mmu_shared_exit')

    # -- restarts are a no-op -------------------------------------------------

    def test_calibrated_lengths_survive_restart(self):
        hh, _ = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)

        hh2, _ = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_restart_with_endstop_offset_does_not_adjust(self):
        # gate_endstop_to_encoder is set (machine has both a gate endstop and an
        # encoder) but nothing changed between calibration and boot. Previously the
        # first boot applied +/-offset anyway because the home was never recorded.
        hh, _ = self._boot('boxturtle', endstop_to_encoder=47.0)
        path = self._calibrate_all(hh, 123.4)

        hh2, _ = self._boot('boxturtle', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_encoder_restart_short_bowden_does_not_reset(self):
        # Encoder homing: the spurious -offset made short bowdens negative, and the
        # next boot's negative->UNCALIBRATED coercion wrote the -1s the user saw.
        hh, _ = self._boot('encoder', endstop_to_encoder=47.0)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.p.gate_homing_endstop, 'encoder')
        path = self._calibrate_all(hh, 30.0)

        hh2, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [30.0] * 4)

    # -- the negative-length warning targets real corruption only -------------

    def test_partially_calibrated_restart_does_not_warn_negative(self):
        # UNCALIBRATED = -1 entries are the normal 'not calibrated yet' state of a
        # partially calibrated unit - the negative-length warning must not report
        # them as bad data on every restart.
        path = self._seed_vars({
            'mmu_unit0_bowden_lengths': [123.4, -1, -1, -1],
            'mmu_unit0_bowden_home': 'mmu_shared_exit',
        })

        hh, output = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [123.4, -1, -1, -1])
        self.assertNotIn('negative bowden lengths', '\n'.join(output))

    def test_genuinely_negative_stored_length_still_warns(self):
        # Guard: values below UNCALIBRATED are real corruption (the encoder-offset
        # chain) and must still be reported when coerced.
        path = self._seed_vars({
            'mmu_unit0_bowden_lengths': [-17.0, 123.4, -1, -1],
            'mmu_unit0_bowden_home': 'mmu_shared_exit',
        })

        hh, output = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [-1, 123.4, -1, -1])
        self.assertIn('negative bowden lengths', '\n'.join(output))

    # -- a genuine endstop change still adjusts -------------------------------

    def test_live_endstop_change_still_adjusts_lengths(self):
        # Guard: the fix must not neuter the legitimate adjustment when the endstop
        # actually changes after calibration.
        hh, _ = self._boot('boxturtle', endstop_to_encoder=47.0)
        self._calibrate_all(hh, 123.4)
        unit = hh.mmu.mmu_unit()

        unit.p.set_param('gate_homing_endstop', 'encoder')
        self.assertEqual(unit.calibrator._bowden_lengths, [76.4] * 4)

        unit.p.set_param('gate_homing_endstop', 'mmu_shared_exit')
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * 4)


if __name__ == '__main__':
    unittest.main()
