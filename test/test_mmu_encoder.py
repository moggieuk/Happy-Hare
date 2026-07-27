# Happy Hare test harness - encoder-based gate homing.
#
# WHY THIS FILE EXISTS SEPARATELY FROM THE OTHER LOAD TESTS
#
# _home_to_gate has two branches that share almost nothing
# (extras/mmu/mmu_filament_movement.py:206). The endstop branch homes: it drives the
# gear motor until a switch closes and stops there. The encoder branch does not home at
# all - it makes a FIXED-LENGTH move and asks whether the filament moved, deciding
# "filament picked up" from motion rather than from position:
#
#     _, _, m, _ = self.move_filament(msg, profile.homing_max)   # always 200mm
#     if m > 6.0:  ... gate is AVAILABLE
#
# Everything downstream differs as a result. The gate reference point becomes the
# encoder wheel rather than a switch, so parking is measured from there; a failure is
# "no movement seen" rather than "endstop never triggered"; and the overshoot the
# caller has to unwind is a measured distance rather than zero. None of that is
# exercised by the switch-homed profiles.
#
# WHY A DERIVED PROFILE. None of the three shipped machine profiles (boxturtle,
# tradrack, emu) has an encoder, so this branch was dead to the harness. profiles.py
# adds `encoder` = BoxTurtle + MMU_HAS_ENCODER + gate_homing_endstop=encoder. That is
# only legitimate because the rendered [mmu_encoder] section comes out COMPLETE - every
# dependent parameter has a real default - which test_config_is_complete below asserts
# rather than assumes. The same trick applied to a proportional buffer produced a
# section Happy Hare could not parse, and was reverted.
#
# WHAT THE MODEL HAD TO GROW. An encoder reports MOTION, not presence, so a switch
# could not express it. filament.py gained an `mmu_encoder` position (+20, just past the
# gate, where ERCF-style machines put the wheel) and travel_over(), which returns how
# much of a move happened while filament COVERED that point. bootstrap turns that into
# real pulses through MCU_counter's callback, so Happy Hare's own _counter_callback
# accumulates the distance and drives the derived sensor.
#
#   ./venv/bin/python -m unittest test.test_mmu_encoder
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import cfg, profiles, session
from test.hh.filament import TIP_ABSENT

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
GATE_EMPTY = 0
GATE_AVAILABLE = 1

ENCODER_PIN = 'unit0:PA6'
ENCODER_AT = 20.0               # filament.DEFAULT_LAYOUT['mmu_encoder']
TIP_AT_GATE = -40.0
# mmu_filament_movement.py:219 - the encoder must see more than this to call it a pickup
MOTION_THRESHOLD = 6.0


class EncoderTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('encoder')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        self.encoder = self.hh.mmu.mmu_unit(0).encoder

    def tearDown(self):
        self.hh.close()

    def preload(self, gate=0, present=True):
        """Offer filament to a gate (or not) and run a preload."""
        if present:
            self.hh.place_filament(gate, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)

    def trace(self):
        self.hh.mmu.p.log_level = 4
        return len(self.hh.console)


class TestEncoderProfile(EncoderTestCase):

    def test_gate_homing_uses_the_encoder(self):
        self.assertTrue(self.hh.mmu.has_encoder())
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'encoder')

    def test_the_encoder_pin_is_bound_as_a_counter(self):
        """
        Not incidental: pulse_counter.MCU_counter is what claims the pin, so a binding of
        any other type would mean the encoder was wired up through something else.
        """
        self.hh.pins.assert_bound(ENCODER_PIN, 'counter')

    def test_config_is_complete(self):
        """
        The check that makes this derived profile honest. Enabling a feature outside the
        starter that ships it can leave dependent parameters blank, and a half-rendered
        section is worse than no coverage - it produces a machine that boots but behaves
        unlike any real one. Every [mmu_encoder] key must carry a value.
        """
        parser = cfg.assemble(cfg.render(profiles.ENCODER))
        section = 'mmu_encoder unit0'
        self.assertIn(section, parser.sections())
        blank = [k for k, v in parser.items(section) if not str(v).strip()]
        self.assertEqual(blank, [], 'unpopulated keys in [%s]' % section)
        self.assertGreater(self.encoder.resolution, 0)


class TestEncoderMeasuresTravel(EncoderTestCase):
    """
    The measurement primitive everything else rests on. If these are wrong, the homing
    tests below pass or fail for reasons that have nothing to do with Happy Hare.
    """

    def moved(self, distance, gate=0):
        """Run a bare gear move and return how far the encoder thought it went."""
        before = self.encoder.get_distance()
        self.hh.run_gcode('MMU_TEST_MOVE MOVE=%.1f MOTOR=gear' % distance)
        return self.encoder.get_distance() - before

    def test_travel_is_only_counted_while_filament_covers_the_wheel(self):
        """
        A move that starts short of the encoder contributes only the part after the
        filament arrives. Counting the whole move instead would make the motion test
        pass for a gate with no filament in it, which is the check under test.
        """
        self.preload()
        start = self.fil.tip[0]
        self.assertLess(start, ENCODER_AT, 'precondition: parked short of the encoder')
        measured = self.moved(200.0)
        self.assertAlmostEqual(measured, 200.0 - (ENCODER_AT - start),
                               delta=self.encoder.resolution)

    def test_an_empty_gate_turns_the_wheel_not_at_all(self):
        self.hh.run_gcode('MMU_SELECT GATE=0')
        self.assertEqual(self.fil.tip[0], TIP_ABSENT)
        self.assertEqual(self.moved(200.0), 0.0)

    def test_counting_is_direction_blind(self):
        """
        A pulse counter has no quadrature, so a retraction produces counts exactly like
        an advance and get_distance() only ever grows. Happy Hare knows the commanded
        direction and signs the result itself; an encoder that decremented on retraction
        would silently halve every unload measurement.
        """
        self.preload()
        self.moved(200.0)                       # get the filament past the wheel
        self.assertGreater(self.moved(-30.0), 0.0)

    def test_movement_triggers_the_derived_encoder_sensor(self):
        """
        register_as_sensor makes the encoder visible as a filament switch, driven from
        _counter_callback rather than from any pin. The harness deliberately does not
        own this sensor - it has no position in the layout - so seeing it change proves
        Happy Hare's own callback ran.
        """
        self.assertFalse(self.hh.sensor('unit0:encoder').present)
        self.preload()
        self.assertTrue(self.hh.sensor('unit0:encoder').present)


class TestGateHomingByMotion(EncoderTestCase):

    def test_the_encoder_branch_is_the_one_that_runs(self):
        at = self.trace()
        self.preload()
        entry = [l for l in self.hh.console[at:] if '_home_to_gate(' in l]
        self.assertTrue(entry, 'no _home_to_gate trace')
        self.assertIn('endstop=encoder', entry[0])

    def test_pickup_marks_the_gate_available(self):
        self.preload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)

    def test_a_pickup_needs_more_than_the_motion_threshold(self):
        at = self.trace()
        self.preload()
        detected = [l for l in self.hh.console[at:] if 'load into encoder' in l]
        self.assertEqual(len(detected), 1, 'should have succeeded first time')
        self.assertGreater(self.encoder.get_distance(), MOTION_THRESHOLD)

    def test_parking_is_measured_from_the_encoder(self):
        """
        With no gate switch in the loop the encoder wheel becomes the reference point,
        so a parked filament sits gate_parking_distance BEHIND the encoder rather than
        behind the gate sensor. This is the visible consequence of the two branches
        using different references, and it is why the same machine parks at a different
        place depending on gate_homing_endstop.
        """
        self.preload()
        # gate_parking_distance is signed, and negative means "back toward the spool"
        parking = self.hh.mmu.mmu_unit(0).p.gate_parking_distance
        self.assertLess(parking, 0)
        self.assertAlmostEqual(self.fil.tip[0], ENCODER_AT + parking, delta=2.0)

    def test_an_empty_gate_fails_rather_than_reporting_a_pickup(self):
        self.preload(gate=1, present=False)
        self.assertTrue(self.hh.errors)
        self.assertIn('encoder', self.hh.errors[0].lower())
        self.assertEqual(self.hh.mmu.gate_status[1], GATE_EMPTY)

    def test_a_failed_pickup_is_retried(self):
        """
        The retry is not cosmetic - selector().filament_release() runs between attempts
        to let a mis-fed filament re-seat. Losing the loop would turn a recoverable
        mis-feed into a failed print.
        """
        at = self.trace()
        self.preload(gate=1, present=False)
        attempts = [l for l in self.hh.console[at:] if 'load into encoder' in l]
        self.assertGreater(len(attempts), 1, 'expected at least one retry')
        self.assertIn('Initial load into encoder', attempts[0])
        self.assertIn('Retry load into encoder', attempts[1])

    def test_a_failed_pickup_leaves_no_filament_behind(self):
        """
        Note the tip does not stay exactly at TIP_ABSENT: the gear motor really does
        turn for both attempts, so the model dutifully advances 400mm of nothing. The
        sentinel starts far enough away that this changes no sensor, which is the
        assertion that matters - a failed pickup must not leave the gate looking loaded.
        """
        self.preload(gate=1, present=False)
        self.assertFalse(self.hh.sensor('mmu_exit_1').present)
        self.assertLess(self.fil.tip[1], self.fil.layout['mmu_pre_gate'])


class TestLoadAndUnload(EncoderTestCase):
    """
    The whole sequence on an encoder machine, not just the gate step. Gate homing feeds
    its measured overshoot to the bowden load, so an error here compounds rather than
    cancelling out.
    """

    def setUp(self):
        super().setUp()
        self.preload()
        self.hh.heat_extruder(220)

    def test_tool_change_loads_to_the_toolhead(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertGreater(self.fil.tip[0], self.fil.layout['toolhead'])

    def test_unload_returns_the_filament_to_the_gate(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertLess(self.fil.tip[0], ENCODER_AT)

    def test_the_encoder_keeps_measuring_through_a_whole_tool_change(self):
        self.encoder.reset_counts()
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        # A full load is the best part of a metre of filament past the wheel; the exact
        # figure depends on bowden calibration, so assert the order of magnitude only.
        self.assertGreater(self.encoder.get_distance(),
                           self.fil.layout['extruder_entry'] / 2)

    def test_the_gate_stays_available_after_a_round_trip(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)


if __name__ == '__main__':
    unittest.main()
