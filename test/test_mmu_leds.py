# Happy Hare test harness - milestone D: LED effect state.
#
# Covers the mechanism sessions 3-5 kept reworking: the segment-scoped transient flash
# with a CAS-style checked restore, its one-slot deferral queue, config-driven durations,
# and the pending-spool_id overlay. All of it was "static-verified only" per the handoffs.
#
# TWO THINGS THAT MAKE NAIVE LED ASSERTIONS WRONG, both learned the hard way:
#
#  1. effect_state records the UNDERLYING configured effect, not the overlay name. The
#     pending overlay is baked into the render (_pending_overlay_effect is consulted by
#     the gate_status and status branches of _set_led), so effect_state showing
#     'gate_status' during a pending is CORRECT - the overlay is applied at paint time.
#     Assert the overlay through _pending_overlay_effect, not effect_state.
#
#  2. effect_initialized is a unit-wide TIMED state effect lasting 8s from bootup, and a
#     transient flash requested while such an effect holds the unit is DROPPED, not
#     deferred. Anything LED-related must wait it out first, or it measures the rainbow.
#
#   ./venv/bin/python -m unittest test.test_mmu_leds
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

WARMUP = 12.0       # past the 8s effect_initialized state flash


class LedTestCase(unittest.TestCase):
    PROFILE = 'boxturtle'

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.hh.reactor.advance(WARMUP)
        self.leds = self.hh.mmu.led_manager
        self.unit = self.hh.mmu.mmu_unit(0)

    def tearDown(self):
        self.hh.close()

    def state(self, segment='exit', unit=0):
        return self.leds.effect_state.get(unit, {}).get(segment)


class TestEffectConfiguration(LedTestCase):
    """The [mmu_leds] operation->effect mapping, as rendered from the real templates."""

    def test_operations_resolve_to_configured_effects(self):
        expected = {
            'gate_selected': 'mmu_static_blue',
            'checking': 'mmu_breathing_cyan_fast',
            'preloading': 'mmu_breathing_cyan_fast',
            'pending_spoolid': 'mmu_breathing_purple_slow',
            'pending_spoolid_expiring': 'mmu_breathing_purple_fast',
            'nfc_read': 'mmu_green_strobe_fast',
            'nfc_deep_read': 'mmu_green_strobe_fast',
            'nfc_fail': 'mmu_red_strobe',
        }
        for operation, effect in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(self.leds.effect_name(0, operation), effect)

    def test_preloading_has_its_own_mapping(self):
        """
        Session 5 added effect_preloading. It ships pointing at the same visual as
        effect_checking, so the only way to tell it was really added is that it resolves
        as a distinct, independently remappable operation.
        """
        self.assertTrue(self.leds.effect_name(0, 'preloading'))

    def test_durations_come_from_the_third_config_field(self):
        """
        Durations are the optional 3rd field of an effect_* mapping. The shipped values
        are what actually apply - the NFC_LED_*_FLASH constants are only fallbacks, and
        they disagree (1.5/3.0 vs 0.8/1.6/3), so reading the config matters.
        """
        self.assertAlmostEqual(self.leds.effect_duration(0, 'nfc_read', 99), 0.8)
        self.assertAlmostEqual(self.leds.effect_duration(0, 'nfc_deep_read', 99), 1.6)
        self.assertAlmostEqual(self.leds.effect_duration(0, 'nfc_fail', 99), 3.0)
        self.assertAlmostEqual(self.leds.effect_duration(0, 'initialized', 0), 8.0)

    def test_unmapped_operation_falls_back(self):
        self.assertEqual(self.leds.effect_duration(0, 'gate_selected', 42), 42)
        self.assertEqual(self.leds.effect_name(0, 'no_such_operation'), '')


class TestTransientFlash(LedTestCase):
    """set_transient_effect: paint a segment, then put back what was there."""

    def test_flash_paints_then_restores(self):
        before = self.state()
        ok = self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                            segment='exit', duration=1.0)
        self.assertTrue(ok)
        self.assertEqual(self.state(), 'mmu_green_strobe_fast')
        self.hh.reactor.advance(1.5)
        self.assertEqual(self.state(), before, 'the pre-flash effect was not restored')

    def test_persistent_flash_stays_until_repainted(self):
        """duration=None paints and holds - no bookkeeping, no timer."""
        self.leds.set_transient_effect(self.unit, 'mmu_red_strobe', segment='exit')
        self.hh.reactor.advance(30.0)
        self.assertEqual(self.state(), 'mmu_red_strobe')

    def test_restore_self_cancels_if_something_repainted(self):
        """
        The CAS check: at expiry the segment is only restored if it still shows the
        flash. Anything painted over it wins. This is what makes snapshot-restore safe -
        staleness is why the earlier restore machinery was removed.
        """
        self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                       segment='exit', duration=1.0)
        self.leds.set_transient_effect(self.unit, 'mmu_static_green', segment='exit')
        self.hh.reactor.advance(2.0)
        self.assertEqual(self.state(), 'mmu_static_green',
                         'the expiring flash stomped a newer effect')

    def test_first_snapshot_wins_across_a_chain(self):
        """
        A chained flash keeps the ORIGINAL pre-flash baseline, so read -> fail -> baseline
        restores what was there before the sequence rather than the first flash.
        """
        baseline = self.state()
        self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                       segment='exit', duration=1.0)
        self.leds.set_transient_effect(self.unit, 'mmu_red_strobe',
                                       segment='exit', duration=1.0, defer=True)
        self.hh.reactor.advance(5.0)
        self.assertEqual(self.state(), baseline)

    def test_deferred_flash_waits_for_the_running_one(self):
        """
        defer=True queues behind the active flash instead of cutting it short, which is
        how the NFC fail flash follows the read acknowledgement.
        """
        self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                       segment='exit', duration=2.0)
        self.leds.set_transient_effect(self.unit, 'mmu_red_strobe',
                                       segment='exit', duration=2.0, defer=True)
        self.assertEqual(self.state(), 'mmu_green_strobe_fast',
                         'the deferred flash cut the running one short')
        self.hh.reactor.advance(2.5)
        self.assertEqual(self.state(), 'mmu_red_strobe', 'the queued flash never ran')

    def test_immediate_flash_discards_a_stale_deferral(self):
        self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                       segment='exit', duration=2.0)
        self.leds.set_transient_effect(self.unit, 'mmu_red_strobe',
                                       segment='exit', duration=2.0, defer=True)
        self.leds.set_transient_effect(self.unit, 'mmu_static_blue',
                                       segment='exit', duration=1.0)
        self.hh.reactor.advance(5.0)
        self.assertNotEqual(self.state(), 'mmu_red_strobe',
                            'a superseded deferral should have been dropped')

    def test_other_segments_are_untouched(self):
        """A segment-scoped flash must not reset the whole unit."""
        status_before = self.state('status')
        self.leds.set_transient_effect(self.unit, 'mmu_green_strobe_fast',
                                       segment='exit', duration=1.0)
        self.assertEqual(self.state('status'), status_before)


class TestFlashDroppedUnderStateEffect(unittest.TestCase):
    """
    A flash requested while a unit-wide TIMED state effect holds the unit is DROPPED and
    returns False - not deferred. Deferring would replay it at the state flash's end with
    nothing scheduled to clear it.

    Deliberately does NOT warm up: effect_initialized still holds the unit right after
    bootup, which is precisely the condition under test.
    """

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()

    def tearDown(self):
        self.hh.close()

    def test_flash_is_refused_while_initialized_holds_the_unit(self):
        leds = self.hh.mmu.led_manager
        unit = self.hh.mmu.mmu_unit(0)
        self.assertEqual(leds.effect_state.get(0, {}).get('exit'), 'mmu_rainbow',
                         'precondition: the initialized state effect should be showing')
        self.assertFalse(
            leds.set_transient_effect(unit, 'mmu_green_strobe_fast',
                                      segment='exit', duration=1.0),
            'a flash under a timed state effect must be dropped, not queued')

    def test_flash_is_accepted_once_the_state_effect_expires(self):
        leds = self.hh.mmu.led_manager
        unit = self.hh.mmu.mmu_unit(0)
        self.hh.reactor.advance(WARMUP)
        self.assertTrue(leds.set_transient_effect(unit, 'mmu_green_strobe_fast',
                                                  segment='exit', duration=1.0))


class TestPendingOverlay(LedTestCase):
    """
    The pending-spool_id overlay is BASE spoolman functionality, not NFC: a manual
    MMU_GATE_MAP NEXT_SPOOLID=n sets the identical state, so it must light up without any
    reader involved. It is baked into the render rather than painted as a transient.

    NAMING TRAP, worth stating because it is easy to get backwards: the machine param
    spoolman_led_segment takes 'gate_status' | 'status' | 'both', but 'gate_status' is NOT
    a segment name - it means "the segments that render per-gate availability", i.e.
    'exit' and 'entry'. _pending_overlay_effect() takes a REAL segment name and returns
    None for anything that is not exit/entry/status (mmu_led_manager.py:379-386).
    """

    def overlay(self, segment='exit'):
        return self.leds._pending_overlay_effect(self.unit, segment)

    def test_no_overlay_when_nothing_is_pending(self):
        self.assertIsNone(self.hh.mmu.pending_phase)
        self.assertIsNone(self.overlay('exit'))

    def test_manual_next_spoolid_lights_the_overlay(self):
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertEqual(self.hh.mmu.pending_spool_id, 7)
        self.assertEqual(self.hh.mmu.pending_phase, 'pending')
        self.assertEqual(self.overlay('exit'), 'mmu_breathing_purple_slow')
        self.assertEqual(self.overlay('entry'), 'mmu_breathing_purple_slow')

    def test_overlay_switches_to_expiring_then_clears(self):
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        timeout = self.hh.mmu.p.spoolman_pending_id_timeout
        self.hh.reactor.advance(timeout - 4.0)
        self.assertEqual(self.hh.mmu.pending_phase, 'expiring')
        self.assertEqual(self.overlay('exit'), 'mmu_breathing_purple_fast')
        self.hh.reactor.advance(6.0)
        self.assertIsNone(self.hh.mmu.pending_phase)
        self.assertIsNone(self.overlay('exit'))

    def test_overlay_segment_is_configurable(self):
        """
        spoolman_led_segment picks which segments carry it: gate_status (default),
        status, or both.
        """
        self.assertEqual(self.hh.mmu.p.spoolman_led_segment, 'gate_status')
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertIsNotNone(self.overlay('exit'))
        self.assertIsNone(self.overlay('status'),
                          "mode 'gate_status' covers exit/entry only, not the status ring")

    def test_status_mode_moves_the_overlay(self):
        self.hh.mmu.p.spoolman_led_segment = 'status'
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertIsNotNone(self.overlay('status'))
        self.assertIsNone(self.overlay('exit'))

    def test_both_mode_covers_everything(self):
        self.hh.mmu.p.spoolman_led_segment = 'both'
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertIsNotNone(self.overlay('exit'))
        self.assertIsNotNone(self.overlay('status'))

    def test_a_non_segment_name_returns_none(self):
        """'gate_status' is a MODE, not a segment - passing it as one yields nothing."""
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertIsNone(self.overlay('gate_status'))

    def test_effect_state_records_the_underlying_effect_not_the_overlay(self):
        """
        Pins the trap in this module's header, and a deliberate session-5 review fix: the
        exit branch records 'gate_status', so the overlay is invisible in effect_state by
        design. A test asserting the purple here would be asserting a bug.
        """
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.assertEqual(self.overlay('exit'), 'mmu_breathing_purple_slow',
                         'precondition: the overlay is active')
        self.assertNotEqual(self.state(), 'mmu_breathing_purple_slow',
                            'effect_state must record the underlying effect')

    def test_cancel_clears_the_overlay(self):
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=7')
        self.hh.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=0')
        self.assertIsNone(self.hh.mmu.pending_phase)
        self.assertIsNone(self.overlay('exit'))


if __name__ == '__main__':
    unittest.main()
