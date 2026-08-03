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


class TestSetLedReachesTheChain(LedTestCase):
    """
    The harness implements Klipper's core SET_LED (klippy_root/extras/led.py), and every
    STATIC colour Happy Hare paints depends on it: mmu_led_manager.py:635-642 drives the
    'off', filament_color, slicer_color and explicit-(r,g,b) branches - plus everything
    under 'animation: False' - through it. It was missing, so all of those silently landed
    in gcode.unhandled and the LEDs stayed black. These assert the wiring, not HH's paint
    logic, so they drive the command directly.
    """

    def chain(self, segment='exit'):
        return self.unit.leds.virtual_chains[segment]

    def test_a_single_index_reaches_the_physical_chain(self):
        self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds INDEX=2 '
                          'RED=1 GREEN=0.5 BLUE=0 TRANSMIT=1')
        self.assertEqual(self.chain().get_status()['color_data'][1],
                         (1., 0.5, 0., 0.))

    def test_no_index_paints_the_whole_segment(self):
        self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds RED=0 GREEN=0 BLUE=1')
        self.assertEqual(self.chain().get_status()['color_data'],
                         [(0., 0., 1., 0.)] * 4)

    def test_transmit_zero_defers_until_the_last_write(self):
        """
        set_gate_rgb sends TRANSMIT=0 for every index but the last, so honouring it is what
        makes a whole-segment repaint one flush instead of N. Ignoring the flag would still
        LOOK right - the colours land either way - so assert the transmit count.
        """
        physical = self.hh.printer.lookup_object('neopixel _unit0_leds')
        before = len(physical.updates)
        for index in range(1, 4):
            self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds INDEX=%d RED=1 TRANSMIT=0'
                              % index)
        self.assertEqual(len(physical.updates), before, 'TRANSMIT=0 flushed anyway')
        self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds INDEX=4 RED=1 TRANSMIT=1')
        self.assertEqual(len(physical.updates), before + 1)
        self.assertEqual(self.chain().get_status()['color_data'],
                         [(1., 0., 0., 0.)] * 4)

    def test_an_unchanged_colour_still_propagates(self):
        """
        Deliberately NOT Klipper's `_set_color` short-circuit. Here the transmit is what
        runs VirtualMmuLedChain.update_leds, i.e. the virtual -> physical copy, so skipping
        a repaint of the same colour would leave a physically stale chain.
        """
        physical = self.hh.printer.lookup_object('neopixel _unit0_leds')
        self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds INDEX=1 RED=1')
        before = len(physical.updates)
        physical.led_helper.led_state[0] = (0., 0., 0., 0.)     # something else clobbered it
        self.hh.run_gcode('SET_LED LED=unit0_mmu_exit_leds INDEX=1 RED=1')
        self.assertEqual(len(physical.updates), before + 1)
        self.assertEqual(self.chain().get_status()['color_data'][0], (1., 0., 0., 0.))


class TestStopReleasesTheLedsImmediately(LedTestCase):
    """
    Stopping an effect used to hand its LEDs back one frame LATE, and anything painted in
    that window was lost.

    set_enabled(False) only arms the frame timer; the blanking is the one-shot zero frame
    getFrame emits on the pass after that, and _getFrames zeroes every LED of an updating
    effect before it sums. So "_MMU_STOP_LED_EFFECTS then SET_LED" - which is exactly what
    mmu_led_manager's filament_color, slicer_color and (r,g,b) branches emit - landed the
    colours and then had them wiped by the timer. It came right only from the SECOND repaint,
    because by then the effect had latched nextEventTime = NEVER.

    cmd_STOP_LED_EFFECTS now flushes the pass before returning (ledFrameHandler.flush_frames).

    The commands go through ONE run_script on purpose. Happy Hare emits them from inside a
    single _set_led dispatch via run_script_from_command, and splitting them across calls
    would let the reactor turn in between, which is precisely the window being closed.
    """

    CHAIN = 'unit0_mmu_exit_leds'
    EFFECT = 'unit0_mmu_rainbow_exit'

    def colors(self):
        return self.unit.leds.virtual_chains['exit'].get_status()['color_data']

    def run_script(self, *lines):
        self.hh.gcode.run_script('\n'.join(lines))

    def start_effect(self):
        self.run_script("_MMU_SET_LED_EFFECT EFFECT='%s' REPLACE=1" % self.EFFECT)
        self.hh.reactor.advance(0.5)                # let it render a few frames

    def test_a_colour_set_right_after_a_stop_survives(self):
        """
        THE regression. Both halves matter: the write always LANDED, so a same-pass
        assertion passes even against the bug - it is the advance that catches it, because
        the blank was deferred to the frame timer.
        """
        self.start_effect()
        self.run_script("_MMU_STOP_LED_EFFECTS LEDS='%s'" % self.CHAIN,
                        'SET_LED LED=%s RED=1 GREEN=1 BLUE=1' % self.CHAIN)
        white = [(1., 1., 1., 0.)] * len(self.colors())
        self.assertEqual(self.colors(), white, 'the write did not even land')
        self.hh.reactor.advance(1.0)
        self.assertEqual(self.colors(), white,
                         'the stopped effect blanked a colour set after it')

    def test_a_stop_on_its_own_still_turns_the_leds_off(self):
        """
        The blank is not removed, only brought forward - a bare stop must still black out,
        and now does so before the command returns rather than on the next tick.
        """
        self.start_effect()
        self.assertNotEqual(self.colors(), [(0., 0., 0., 0.)] * len(self.colors()),
                            'precondition: the effect is lighting something')
        self.run_script("_MMU_STOP_LED_EFFECTS LEDS='%s'" % self.CHAIN)
        self.assertEqual(self.colors(), [(0., 0., 0., 0.)] * len(self.colors()))

    def test_a_fading_stop_is_left_to_fade(self):
        """
        A fade legitimately keeps rendering, and keeps ownership, until it runs out - so it
        must NOT be flushed to black on the spot. blanks_on_next_frame() is what excludes it.
        """
        self.start_effect()
        self.run_script("_MMU_STOP_LED_EFFECTS LEDS='%s' FADETIME=2" % self.CHAIN)
        self.assertNotEqual(self.colors(), [(0., 0., 0., 0.)] * len(self.colors()),
                            'the fade was cut short')
        self.hh.reactor.advance(3.0)
        self.assertEqual(self.colors(), [(0., 0., 0., 0.)] * len(self.colors()),
                         'the fade never finished')

    def test_replacing_an_effect_does_not_flash_the_leds_off(self):
        """
        The displaced effects hand their LEDs straight to the new one, and the pass that
        blanks them is the pass that draws it. Flushing on REPLACE would blank them a frame
        before the replacement had anything to draw, so that branch deliberately does not.
        """
        self.start_effect()
        self.run_script("_MMU_SET_LED_EFFECT EFFECT='mmu_static_green_exit_0' REPLACE=1")
        self.hh.reactor.advance(0.1)
        self.assertEqual(self.colors()[0], (0., 0.5, 0., 0.))


class TestAllFourSegments(unittest.TestCase):
    """
    ercf_vvd's unit0 configures every segment - 9 exit, 9 entry, 4 status, 3 logo - so the
    console can exercise each one. exit is on the external 'cabinet_leds' chain from
    PRINTER_STUB; the other three sit on '_unit0_leds', which the template emits anyway.
    """

    @classmethod
    def setUpClass(cls):
        cls.hh = session('ercf_vvd')
        cls.hh.boot()
        cls.hh.reactor.advance(WARMUP)

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def unit(self, index=0):
        return self.hh.mmu.mmu_machine.units[index]

    def test_every_segment_has_the_expected_led_count(self):
        leds = self.unit().leds
        self.assertEqual({s: len(leds.virtual_chains[s].leds)
                          for s in ('exit', 'entry', 'status', 'logo')},
                         {'exit': 9, 'entry': 9, 'status': 4, 'logo': 3})

    def test_the_second_unit_still_has_exit_only(self):
        """ViViD's 28-over-4-gates chain is the multi-LED-per-gate case; leave it alone."""
        leds = self.unit(1).leds
        self.assertEqual(len(leds.virtual_chains['exit'].leds), 28)
        self.assertEqual(len(leds.virtual_chains['entry'].leds), 0)

    def test_the_logo_tuple_effect_actually_lights_the_leds(self):
        """
        logo_effect is the r,g,b tuple (0,0,0.3), i.e. a pure SET_LED path with no
        animation behind it. Black here means the static path is broken again.
        """
        data = self.unit().leds.virtual_chains['logo'].get_status()['color_data']
        self.assertEqual(data, [(0., 0., 0.3, 0.)] * 3)


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

    def test_settle_leds_advances_out_of_the_hold(self):
        """
        The harness counterpart. boot() stops the clock 2.5s in, inside effect_initialized's
        8s window, so an interactive session would drop EVERY transient flash for good - which
        is how the NFC read acknowledgment came to look broken. settle_leds() walks out of it,
        the way a printer does by itself.
        """
        leds = self.hh.mmu.led_manager
        unit = self.hh.mmu.mmu_unit(0)
        self.assertTrue(any(leds.pending_update), 'precondition: a unit should still be held')
        advanced = self.hh.settle_leds()
        self.assertGreater(advanced, 0.)
        self.assertFalse(any(leds.pending_update), 'settle_leds did not clear the hold')
        self.assertTrue(
            leds.set_transient_effect(unit, 'mmu_green_strobe_fast',
                                      segment='exit', duration=1.0),
            'a flash should be accepted once nothing holds the unit')

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
