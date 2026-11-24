# Happy Hare test harness - opt-in stall-safe gear homing (gear_homing_stall_retries).
#
# A gear homing move drives the gear for up to the whole homing distance whatever the
# filament does. Filament that has snagged keeps getting driven, and the gear grinds a notch
# in it. With gear_homing_stall_retries set, gear homing moves go in encoder-checked steps:
# a step the encoder barely saw is a stall, so back off, halve the speed and retry, then give
# up after that many retries.
#
# WHY THIS PROFILE. The option needs an encoder that sees the filament while it homes. The
# `encoder` fixture is the neutral BoxTurtle plus an encoder 20mm past the gate. Here its
# gates home to their exit sensor (the neutral fixture's own choice) instead of to the
# encoder, so both directions make gear homing moves:
#   load    homes to the extruder entry sensor, with the encoder already on the filament
#   unload  reverse-homes off the gate exit sensor, the encoder on the filament until the
#           last 20mm
# The load's first move, homing to the gate exit sensor from the park, happens before the
# filament reaches the encoder, so the encoder sees nothing then. That must not read as a stall.
#
# The jam is the filament model's (FilamentPath.jam): the tip can't move past a point and the
# gear slips, so the encoder stops counting while the motor keeps turning.
#
#   ./venv/bin/python -m unittest test.test_mmu_stall_safe_homing
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10

RETRIES = 3
STEP = 30.                  # gear_homing_stall_step's default
EXTRUDER_HOMING_MAX = 400   # long enough that grinding all of it is clearly more than the retries
ENCODER_TOLERANCE = 10.     # the encoder reads each step to within a mm or two

LOAD_HOMING = 'Homing filament to extruder'
UNLOAD_HOMING = 'Reverse homing off mmu_exit'
BACK_OFF = 'Stall back off'


class TestStallSafeHoming(unittest.TestCase):

    def boot(self, retries=None, encoder_at=None):
        profile = profiles.get('encoder')
        syms = {
            'CHOICE_GATE_HOMING_ENDSTOP_ENCODER': False,    # back to the fixture's exit sensor
            'PARAM_EXTRUDER_HOMING_MAX': EXTRUDER_HOMING_MAX,
        }
        layout = None
        if retries is not None:
            syms['PARAM_GEAR_HOMING_STALL_RETRIES'] = retries
        if encoder_at is not None:
            # The exit sensor is at 0, so the encoder is this far past the gate endstop
            syms['PARAM_GATE_ENDSTOP_TO_ENCODER'] = encoder_at
            layout = dict(profile.filament_layout, mmu_encoder=float(encoder_at))
        hh = session(profile.derive(
            'encoder_exit_homing_stall_%s_%s' % (retries, encoder_at), syms=syms,
            filament_layout=layout))
        self.addCleanup(hh.close)
        hh.boot(calibrate=True)
        self.assertEqual(hh.errors, [])
        self.assertEqual(hh.mmu.mmu_unit().p.gear_homing_stall_retries, retries or 0)
        hh.heat_extruder(220)
        hh.place_filament(0)
        hh.run_gcode('MMU_PRELOAD GATE=0')
        hh.run_gcode('MMU_SELECT GATE=0')
        self.fil = hh.filament()
        self.moves = []
        move_filament = hh.mmu.move_filament

        def record(trace, dist, *args, **kwargs):
            result = move_filament(trace, dist, *args, **kwargs)
            self.moves.append((trace, dist, kwargs.get('speed'), result))
            return result

        hh.mmu.move_filament = record
        return hh

    def enabled(self):
        return self.boot(retries=RETRIES)

    def homing_moves(self, prefix):
        """
        (commanded, gear travel, encoder) for each move with this trace. The stall-safe move
        records each step and then their total under the same trace, so with the option on,
        only the step-sized moves are the gear's own.
        """
        return [(dist, abs(result[0]), abs(result[2])) for trace, dist, _, result in self.moves
                if trace.startswith(prefix)]

    def grinding(self, prefix):
        """Gear travel in the homing steps with this trace that the filament didn't follow."""
        return sum(travel - seen for dist, travel, seen in self.homing_moves(prefix)
                   if abs(dist) <= STEP + 1e-6)

    def stalled_steps(self, prefix):
        """Homing steps with this trace the encoder saw too little of (the 40% default ratio)."""
        return [m for m in self.homing_moves(prefix)
                if abs(m[0]) <= STEP + 1e-6 and m[1] > 0 and m[2] < 0.4 * m[1]]

    def back_offs(self):
        return [m for m in self.moves if m[0] == BACK_OFF]

    def test_off_by_default(self):
        hh = self.boot()
        self.assertEqual(hh.mmu.mmu_unit().p.gear_homing_stall_retries, 0)
        hh.run_gcode('MMU_LOAD')
        hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(hh.errors, [])
        # One move each, for the whole homing distance, as before the option existed
        [(dist, _, _)] = self.homing_moves(LOAD_HOMING)
        self.assertGreaterEqual(dist, EXTRUDER_HOMING_MAX)
        self.assertEqual(len(self.homing_moves(UNLOAD_HOMING)), 1)
        self.assertEqual(self.back_offs(), [])

    def test_off_by_default_a_jam_grinds_the_whole_homing_move(self):
        hh = self.boot()
        tip = self.fil.layout['extruder_entry'] - 10.
        self.fil.jam(0, ahead=tip)
        hh.run_gcode('MMU_LOAD')
        self.assertTrue(hh.errors)
        [(dist, travel, seen)] = self.homing_moves(LOAD_HOMING)
        self.assertGreaterEqual(dist, EXTRUDER_HOMING_MAX)
        self.assertEqual(travel, dist)
        self.assertLess(seen, 20.)

    def test_load_stall_stops_after_the_retries(self):
        hh = self.enabled()
        self.fil.jam(0, ahead=self.fil.layout['extruder_entry'] - 10.)
        hh.run_gcode('MMU_LOAD')
        self.assertTrue(hh.errors)
        self.assertNotEqual(hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(len(self.back_offs()), RETRIES)
        # Each attempt grinds at most one step before the stall is seen, against the whole
        # homing distance with the option off
        self.assertEqual(len(self.stalled_steps(LOAD_HOMING)), RETRIES + 1)
        grinding = self.grinding(LOAD_HOMING)
        self.assertGreater(grinding, 0.)
        self.assertLess(grinding, (RETRIES + 1) * STEP + ENCODER_TOLERANCE)
        self.assertTrue(any('Gear stalled %d times' % (RETRIES + 1) in line for line in hh.console))

    def test_unload_stall_stops_after_the_retries(self):
        hh = self.enabled()
        hh.run_gcode('MMU_LOAD')
        self.assertEqual(hh.errors, [])
        # The fast bowden unload stops 80mm or so short of the exit sensor; jam it just behind that
        self.fil.jam(0, behind=60.)
        hh.run_gcode('MMU_UNLOAD')
        self.assertTrue(hh.errors)
        self.assertNotEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertEqual(len(self.back_offs()), RETRIES)
        self.assertEqual(len(self.stalled_steps(UNLOAD_HOMING)), RETRIES + 1)
        grinding = self.grinding(UNLOAD_HOMING)
        self.assertGreater(grinding, 0.)
        self.assertLess(grinding, (RETRIES + 1) * STEP + ENCODER_TOLERANCE)

    def test_a_stall_that_clears_is_retried_slower_and_completes(self):
        hh = self.enabled()
        self.fil.jam(0, ahead=self.fil.layout['extruder_entry'] - 10.)
        fil = self.fil

        def unjam_on_back_off(gate, moved, *args):
            if moved < 0 and fil.tip[gate] > 600.:      # the back-off after the first stall
                fil.unjam(gate)

        fil.observers.append(unjam_on_back_off)
        hh.run_gcode('MMU_LOAD')
        self.assertEqual(hh.errors, [])
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(len(self.back_offs()), 1)
        back_off = self.moves.index(self.back_offs()[0])
        before = [speed for trace, dist, speed, _ in self.moves[:back_off]
                  if trace.startswith(LOAD_HOMING) and abs(dist) <= STEP + 1e-6]
        retried = [speed for trace, dist, speed, _ in self.moves[back_off:]
                   if trace.startswith(LOAD_HOMING) and abs(dist) <= STEP + 1e-6]
        # Before the stall, the steps go at the homing speed; after it, at half that (no
        # slower than 10mm/s)
        self.assertEqual(set(before), {None})
        self.assertTrue(retried)
        expected = max(hh.mmu.mmu_unit().p.gear_homing_speed / 2., 10.)
        self.assertEqual(set(retried), {expected})

    def test_moves_that_dont_stall_are_unaffected(self):
        hh = self.enabled()
        hh.run_gcode('MMU_LOAD')
        self.assertEqual(hh.errors, [])
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        # Homing to the gate exit sensor from the park, the encoder saw none of it: the filament
        # hadn't reached the encoder yet, which is not a stall
        [(dist, travel, seen)] = self.homing_moves('Initial homing to mmu_exit')
        self.assertGreater(travel, STEP)
        self.assertEqual(seen, 0.)
        hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(hh.errors, [])
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.back_offs(), [])
        # Both homing moves went in steps
        self.assertGreater(len(self.homing_moves(LOAD_HOMING)), 1)
        self.assertGreater(len(self.homing_moves(UNLOAD_HOMING)), 1)

    def test_unload_past_the_encoder_is_not_a_stall(self):
        # The last gate_endstop_to_encoder of an unload is between the encoder and the gate
        # endstop, where the encoder can't see it. A step that ends there saw little, but the
        # filament moved. Here the encoder is 50mm past the gate exit sensor: the fast bowden
        # unload stops 80mm or so short of the sensor, so the second 30mm step ends 20mm or so
        # short of it, having seen only its first few mm.
        hh = self.boot(retries=RETRIES, encoder_at=50)
        hh.run_gcode('MMU_LOAD')
        self.assertEqual(hh.errors, [])
        self.moves = []
        hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(hh.errors, [])
        self.assertEqual(hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        steps = [m for m in self.homing_moves(UNLOAD_HOMING) if abs(m[0]) <= STEP + 1e-6]
        self.assertTrue(any(seen < 0.4 * travel for _, travel, seen in steps), steps)
        self.assertEqual(self.back_offs(), [])


if __name__ == '__main__':
    unittest.main()
