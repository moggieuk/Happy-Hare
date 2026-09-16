# Happy Hare test harness - calibrated bowden length persistence across restarts.
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
    """Each restart reads the previous session's mmu_vars.cfg."""

    def _boot(self, profile, vars_file=None, endstop_to_encoder=None):
        """Return the booted session and captured G-code output."""
        hh = session(profile)
        if vars_file is not None:
            # Reuse the saved file instead of copying the profile defaults.
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

    def _stale_home_vars(self, length):
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

        hh2, _ = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths,
                         [123.4] * unit.num_gates)

    def test_stale_v3_bowden_home_with_endstop_offset_keeps_lengths(self):
        path = self._stale_home_vars(123.4)

        hh, _ = self._boot('boxturtle', vars_file=path, endstop_to_encoder=47.0)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * unit.num_gates)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [123.4] * unit.num_gates)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], 'mmu_shared_exit')

    def test_stale_v3_bowden_home_encoder_restart_does_not_reset(self):
        path = self._stale_home_vars(30.0)

        hh, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)

        hh2, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)
        self.assertEqual(read_vars_file(path)['mmu_unit0_bowden_lengths'], [30.0] * 4)

    def test_calibration_persists_bowden_home(self):
        hh, _ = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_home'], 'mmu_shared_exit')

    def test_calibrated_lengths_survive_restart(self):
        hh, _ = self._boot('boxturtle')
        path = self._calibrate_all(hh, 123.4)

        hh2, _ = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_restart_with_endstop_offset_does_not_adjust(self):
        hh, _ = self._boot('boxturtle', endstop_to_encoder=47.0)
        path = self._calibrate_all(hh, 123.4)

        hh2, _ = self._boot('boxturtle', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [123.4] * 4)

    def test_encoder_restart_short_bowden_does_not_reset(self):
        hh, _ = self._boot('encoder', endstop_to_encoder=47.0)
        unit = hh.mmu.mmu_unit()
        self.assertEqual(unit.p.gate_homing_endstop, 'encoder')
        path = self._calibrate_all(hh, 30.0)

        hh2, _ = self._boot('encoder', vars_file=path, endstop_to_encoder=47.0)
        self.assertEqual(hh2.mmu.mmu_unit().calibrator._bowden_lengths, [30.0] * 4)
        on_disk = read_vars_file(path)
        self.assertEqual(on_disk['mmu_unit0_bowden_lengths'], [30.0] * 4)

    def test_partially_calibrated_restart_does_not_warn_negative(self):
        path = self._seed_vars({
            'mmu_unit0_bowden_lengths': [123.4, -1, -1, -1],
            'mmu_unit0_bowden_home': 'mmu_shared_exit',
        })

        hh, output = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [123.4, -1, -1, -1])
        self.assertNotIn('negative bowden lengths', '\n'.join(output))

    def test_genuinely_negative_stored_length_still_warns(self):
        path = self._seed_vars({
            'mmu_unit0_bowden_lengths': [-17.0, 123.4, -1, -1],
            'mmu_unit0_bowden_home': 'mmu_shared_exit',
        })

        hh, output = self._boot('boxturtle', vars_file=path)
        self.assertEqual(hh.mmu.mmu_unit().calibrator._bowden_lengths, [-1, 123.4, -1, -1])
        self.assertIn('negative bowden lengths', '\n'.join(output))

    def test_live_endstop_change_still_adjusts_lengths(self):
        hh, _ = self._boot('boxturtle', endstop_to_encoder=47.0)
        self._calibrate_all(hh, 123.4)
        unit = hh.mmu.mmu_unit()

        unit.p.set_param('gate_homing_endstop', 'encoder')
        self.assertEqual(unit.calibrator._bowden_lengths, [76.4] * 4)

        unit.p.set_param('gate_homing_endstop', 'mmu_shared_exit')
        self.assertEqual(unit.calibrator._bowden_lengths, [123.4] * 4)


if __name__ == '__main__':
    unittest.main()
