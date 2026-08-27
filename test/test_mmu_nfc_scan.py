# Happy Hare test harness - milestone C2: NFC scanning against a virtual reader.
#
# The filament path model drives a VirtualNfcChip standing in for the real chip driver
# (test/hh/nfc_fixtures.py), so a reader returns a UID exactly when the model says the
# tag is inside the read window. That closes the last gap in the motion work: until now
# the model could trip the NFC *endstop* but read_gate() talked to a chip that had no tag,
# so MMU_NFC_SCAN and the preload compound could not be exercised at all.
#
# Everything here is first-execution code. Per the dev handoffs the whole NFC feature was
# "static-verified only (ast.parse) - nothing run on hardware", and the backward jog in
# particular depends on MmuNfcEndstop.home_start pinning triggered=True (tag detection is
# the trigger regardless of direction) which had never been executed.
#
# GEOMETRY. Model defaults: park -100, entry -50, gate/exit 0, NFC reader -80, read window
# +/-15mm. A tag rides `offset` mm behind the filament tip, since physically the tag is on
# the spool rather than the tip.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_scan
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.bootstrap import install

install()   # Put the fake klippy root on sys.path before importing MMU modules

from extras.mmu.unit import mmu_nfc_manager
from test.hh import profiles as hh_profiles

# nfc_per_gate has no encoder object at all (mmu_unit().encoder is None) - fine for every
# other test here, but the encoder-occupancy guard needs a profile where 'encoder' is a real,
# usable gate_homing_endstop rather than one that would blow up on first use regardless of
# the guard. Derived rather than added to profiles.py: nothing else needs this combination.
NFC_PER_GATE_ENCODER = hh_profiles.NFC_PER_GATE.derive(
    'nfc_per_gate_encoder',
    syms={'MMU_HAS_ENCODER': True, 'PIN_ENCODER': 'unit0:PA6', 'CHOICE_GATE_HOMING_ENDSTOP_ENCODER': True},
    description='BoxTurtle + per-gate NFC + encoder, for shared-endstop occupancy tests')

logging.getLogger().setLevel(logging.CRITICAL)

GATE_AVAILABLE = 1
FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
TAG = '04A1B2C3'


class NfcScanTestCase(unittest.TestCase):
    PROFILE = 'nfc_per_gate'

    def setUp(self):
        self.hh = session(self.PROFILE, virtual_nfc=True)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()

    def tearDown(self):
        self.hh.close()

    def window_edge(self):
        """Path position at which a tag first becomes readable approaching forward."""
        return self.fil.layout['mmu_nfc'] - self.fil.tag_window

    def preload(self, gate=0, position=None):
        """
        Place a gate's filament AND mark the gate available.

        MMU_NFC_SCAN refuses a gate the map calls EMPTY, because the scan jogs filament
        about and homes it back - there has to be filament there. place_filament() is
        deliberately physical-only (it does not touch HH's gate map), and the harness boots
        every gate EMPTY, so a scan test has to say the gate is preloaded. Which is also
        what a real machine looks like: _preload_gate sets GATE_AVAILABLE on success.
        """
        if position is None:
            self.hh.place_filament(gate)
        else:
            self.hh.place_filament(gate, position=position)
        self.hh.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
        return self.fil


class TestVirtualReader(NfcScanTestCase):
    """The chip must answer from the model, or nothing below means anything."""

    def test_readers_come_up_alive(self):
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        self.assertEqual(len(self.hh.nfc_chips), 4)
        self.assertTrue(all(r.alive for r in manager.gate_readers))

    def test_read_returns_the_uid_when_the_tag_is_in_the_window(self):
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.run_gcode('MMU_NFC GATE=0 READ=1')
        self.assertIn(TAG, ' '.join(self.hh.console))

    def test_read_returns_nothing_when_the_tag_is_away(self):
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'] - 200.0)
        self.hh.run_gcode('MMU_NFC GATE=0 READ=1')
        self.assertNotIn(TAG, ' '.join(self.hh.console))

    def test_a_gate_only_sees_its_own_tag(self):
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.run_gcode('MMU_NFC GATE=1 READ=1')
        self.assertNotIn(TAG, ' '.join(self.hh.console),
                         "gate 1's reader must not see gate 0's tag")

    def test_reader_is_polled_not_faked_at_a_higher_level(self):
        """Guards against the chip being bypassed - the driver must actually be asked."""
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        before = self.hh.chip(0).reads
        self.hh.run_gcode('MMU_NFC GATE=0 READ=1')
        self.assertGreater(self.hh.chip(0).reads, before)


class TestJogScanFindsTag(NfcScanTestCase):
    """MMU_NFC_SCAN: jog the filament past the reader until the tag is seen."""

    def test_forward_jog_finds_the_tag_at_the_window_edge(self):
        self.fil.attach_tag(0, TAG)
        self.preload(0)                                 # parked at -100
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and d > 0]
        self.assertTrue(trips, 'forward jog never tripped the NFC endstop')
        self.assertAlmostEqual(trips[0], self.window_edge() - start, places=3)
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

    def test_scan_refuses_the_current_gate_when_its_filament_is_loaded(self):
        """Crossload capability must not permit jogging the gate owning the active load."""
        self.preload(0)
        self.hh.mmu.select_gate(0)
        self.hh.mmu.filament_pos = FILAMENT_POS_LOADED
        self.fil.history.clear()

        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        self.assertEqual(self.fil.history, [], 'the currently loaded gate must not move')
        self.assertTrue(any('filament is loaded' in e.lower() for e in self.hh.errors),
                        self.hh.errors)

    def test_preload_refuses_the_current_gate_when_its_filament_is_loaded(self):
        """MMU_PRELOAD has the same invariant, including on a crossload-capable MMU."""
        self.preload(0)
        self.hh.mmu.select_gate(0)
        self.hh.mmu.filament_pos = FILAMENT_POS_LOADED
        self.fil.history.clear()

        self.hh.run_gcode('MMU_PRELOAD GATE=0')

        self.assertEqual(self.fil.history, [], 'preload must not move the currently loaded gate')
        self.assertTrue(any('filament is loaded' in e.lower() for e in self.hh.errors),
                        self.hh.errors)

    def test_backward_jog_finds_the_tag(self):
        """
        THE BACKWARD PATH. MmuNfcEndstop.home_start pins triggered=True because tag
        detection is the trigger whatever the direction; without it a backward scan can
        never fire. This is the first execution of that code.
        """
        self.fil.attach_tag(0, TAG)
        # Window longer backwards so the sweep goes that way first, and a tag position
        # the backward sweep will carry into the read window.
        self.hh.mmu.mmu_unit(0).p.nfc_gate_jog_scan_window = [-80.0, 20.0]
        self.preload(0, position=-60.0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and d < 0]
        self.assertTrue(trips, 'backward jog never tripped the NFC endstop - has the '
                               'triggered=True pin in MmuNfcEndstop.home_start gone?')
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

    def test_scan_with_no_tag_reports_nothing_found(self):
        self.preload(0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertNotIn('tag read', ' '.join(self.hh.console).lower())

    def test_scan_uses_the_gate_window_even_when_preload_window_differs(self):
        """
        Regression test: MMU_NFC_SCAN always drives _gate_profile(), so it must keep
        sweeping nfc_gate_jog_scan_window even when nfc_preload_jog_scan_window is set
        to something else entirely.
        """
        u = self.hh.mmu.mmu_unit(0)
        u.p.nfc_gate_jog_scan_window = [-50.0, 30.0]
        u.p.nfc_preload_jog_scan_window = [-20.0, 12.0]
        self.preload(0)  # no tag attached: sweep runs its full length, finds nothing
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and abs(d) in (30.0, 12.0)]
        self.assertIn(30.0, [abs(d) for d in trips], 'MMU_NFC_SCAN never swept the 30mm gate window')
        self.assertNotIn(12.0, [abs(d) for d in trips], 'MMU_NFC_SCAN swept the preload window instead')

    def test_tag_already_on_the_reader_short_circuits_the_jog(self):
        """
        _jog_scan pre-reads before moving, so a tag already sitting on the reader is
        resolved with zero motion.
        """
        self.fil.attach_tag(0, TAG)
        self.preload(0, position=self.fil.layout['mmu_nfc'])
        self.hh.mmu.select_gate(0)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.fil.history, [],
                         'a tag already under the reader must need no jogging')
        self.assertIn('tag read', ' '.join(self.hh.console).lower())


class TestPreloadNfcCompound(NfcScanTestCase):
    """
    _home_to_gate_with_nfc: preload homes to a FIRST-WINS MmuCompoundEndstop over
    [gate switch, NFC reader], so a tag is identified as the filament loads and no
    separate MMU_NFC_SCAN is needed.

    Requires the gate endstop to be a real mcu.MCU_endstop - otherwise
    _build_gate_nfc_compound returns None and preload silently degrades to a plain load
    (extras/mmu/mmu_filament_movement.py:329). test_mmu_bootup asserts that invariant.
    """

    def preload_with_tag_before_the_gate(self):
        """Tag 40mm behind the tip, so it crosses the reader before the gate switch."""
        self.fil.attach_tag(0, TAG, {'material': 'PLA'}, offset=40.0)
        self.hh.place_filament(0, position=-100.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')

    def test_compound_endstop_is_used(self):
        self.hh.mmu.p.log_level = 4
        self.preload_with_tag_before_the_gate()
        trace = ' '.join(self.hh.console)
        self.assertIn('preload_compound', trace,
                      'preload did not home to a compound endstop - it silently fell '
                      'back to a plain load')

    def test_nfc_wins_when_the_tag_comes_first(self):
        """
        First-wins: from -100 the tag reaches the window edge after 45mm while the gate
        switch is 100mm away, so the NFC endstop must stop the move.
        """
        self.fil.attach_tag(0, TAG, offset=40.0)
        self.hh.place_filament(0, position=-100.0)
        nfc = self.fil.nfc_trip_distance(0, 300.)
        switch = self.fil.trip_distance(0, 300., ['mmu_exit_0'])
        self.assertLess(nfc, switch[1], 'geometry precondition for this test')

        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        first = self.fil.history[0]
        self.assertIn('mmu_nfc_0', first[2])
        self.assertAlmostEqual(first[1], nfc, places=3)

    def test_preload_still_succeeds_and_reads_the_tag(self):
        self.preload_with_tag_before_the_gate()
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)
        self.assertEqual(self.hh.errors, [])

    def test_preload_without_a_tag_uses_the_gate_switch(self):
        self.hh.place_filament(0, position=-100.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        trips = [r for _g, _d, r in self.fil.history if 'mmu_exit_0' in r]
        self.assertTrue(trips, 'with no tag the gate switch must be what stops the move')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)

    def test_the_banner_says_the_scan_is_happening(self):
        """
        One banner, and it has to be truthful: _preload_gate decides whether the compound
        is really available BEFORE it logs, so "with NFC scan" is never a promise the
        move cannot keep.
        """
        at = len(self.hh.console)
        self.preload_with_tag_before_the_gate()
        banners = [l for l in self.hh.console[at:] if l.startswith('Preloading')]
        self.assertEqual(banners, ['Preloading gate 0 with NFC scan...'])

    def test_a_tag_that_was_read_is_reported(self):
        """
        The banner promised a scan, so the outcome has to be stated. Reported at info
        level, in the same words MMU_NFC_SCAN uses, rather than tacked onto the
        always-visible "Filament detected and loaded" line.
        """
        at = len(self.hh.console)
        self.preload_with_tag_before_the_gate()
        said = ' '.join(self.hh.console[at:]).lower()
        self.assertIn('tag read for gate 0', said)

    def test_a_tag_that_was_not_found_is_also_reported(self):
        """
        The case that used to be silent: a scan ran, found nothing, and said so only by
        omission - leaving no way to tell a spool with no tag from a broken reader.
        """
        self.hh.place_filament(0, position=-100.0)      # no tag attached
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        said = ' '.join(self.hh.console[at:]).lower()
        self.assertIn('no tag found for gate 0', said)
        self.assertNotIn('tag read', said)

    def test_a_plain_preload_says_nothing_about_nfc(self):
        """Only the scan path reports a scan result - gate 0's reader is disabled here."""
        self.hh.run_gcode('MMU_NFC GATE=0 ENABLE=0')
        self.hh.place_filament(0, position=-100.0)
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        said = ' '.join(self.hh.console[at:]).lower()
        self.assertIn('preloading gate 0...', said)
        self.assertNotIn('nfc:', said)

    def test_last_preloaded_gate_is_recorded_on_success(self):
        """MMU_SPOOLMAN_TAG GATE=LAST relies on this being set after a normal successful preload."""
        self.hh.place_filament(0, position=-100.0)
        self.assertEqual(self.hh.mmu.last_preloaded_gate, -1, 'precondition: nothing preloaded yet')
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.mmu.last_preloaded_gate, 0)

    def test_last_preloaded_gate_is_recorded_when_already_preloaded(self):
        """The early-return 'already preloaded' path counts too - the user did run MMU_PRELOAD."""
        self.hh.place_filament(0, position=0.0)  # filament already at the gate/exit datum
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertIn('already preloaded', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.mmu.last_preloaded_gate, 0)

    def test_pending_shared_uid_bypasses_the_per_gate_reader(self):
        """
        A shared-reader pending takes precedence over this gate's own NFC reader: the
        autoload preload it triggers must skip the per-gate scan entirely and apply the
        pending spool_id, rather than re-scan for (and not find) a tag of its own.

        Regression test: the entry-insert handler used to consume the pending before
        MMU_PRELOAD's _grab_pending() ever saw it (mmu_sensor_insert.py), so have_pending
        was always False on the autoload path and the per-gate scan always ran.
        """
        self.hh.mmu.mmu_unit(0).p.gate_autoload = 1
        self.hh.mmu.pending_spool_id = 7  # No tag attached to gate 0 - a scan would find none
        at = len(self.hh.console)
        self.hh.place_filament(0, position=self.fil.layout['mmu_entry'] + 10.0, quiet=False)
        self.hh.settle()
        banners = [l for l in self.hh.console[at:] if l.startswith('Preloading')]
        self.assertEqual(banners, ['Preloading gate 0...'],
                         'per-gate NFC scan ran despite a pending shared-reader UID')
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_id[0], 7)

    def test_weak_pending_does_not_bypass_the_per_gate_reader(self):
        """
        A bare-uid ('weak') pending is not trusted enough to skip the gate's own NFC
        reader - only a resolved spool_id or a tag with usable metadata ('strong') does.
        A fresh per-gate read must win over the stale weak pending, not be clobbered by it.
        """
        self.hh.mmu.pending_tag = ('DEADBEEF', None)
        self.fil.attach_tag(0, TAG, offset=40.0)
        at = len(self.hh.console)
        self.hh.place_filament(0, position=-100.0)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        banners = [l for l in self.hh.console[at:] if l.startswith('Preloading')]
        self.assertEqual(banners, ['Preloading gate 0 with NFC scan...'],
                         'a weak (bare-uid) pending must not skip the per-gate NFC scan')
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], TAG,
                         "the gate's own fresher read must win over the stale weak pending")

    def test_weak_pending_is_applied_when_the_gates_reader_finds_nothing(self):
        """The weak pending is not wasted - it is the fallback when the per-gate scan finds no tag."""
        self.hh.mmu.pending_tag = ('DEADBEEF', None)
        self.hh.place_filament(0, position=-100.0)   # no tag attached
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.assertEqual(self.hh.mmu.gate_maps.gate_spool_rfid[0], 'DEADBEEF')

    def test_nfc_first_finishes_on_the_gate_and_parks_without_reverse_homing(self):
        """
        Reader stops the move, tag is read, homing CONTINUES to the gate switch - so the
        filament ends standing on the datum and the park is a plain retraction. Two
        forward homing legs, no backward one.
        """
        at = len(self.fil.history)
        self.preload_with_tag_before_the_gate()
        homing = [(d, r) for _g, d, r in self.fil.history[at:] if 'homing' in r]
        self.assertEqual([d > 0 for d, _r in homing], [True, True],
                         'expected forward-to-reader then forward-to-gate, got %r' % (homing,))
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])

    def test_gate_first_scans_forward_then_reverse_homes_exactly_once(self):
        """
        Gate switch wins, so preload hands over to the scan logic: sweep forward through
        the positive half of the window, then ONE reverse-home back to the datum before
        parking. It used to do that reverse-home twice - once inline and once again in the
        borrowed _unload_gate - and the second one was budgeted for a normal load, not for
        however far the scan had chased.
        """
        # No tag at all: the forward scan runs the full window and finds nothing, which is
        # the case that strays furthest from the gate and so needs the biggest park budget.
        self.hh.place_filament(0, position=-100.0)
        at = len(self.fil.history)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        back = [(d, r) for _g, d, r in self.fil.history[at:] if 'homing' in r and d < 0]
        self.assertEqual(len(back), 1,
                         'expected exactly one reverse-home back to the gate, got %r' % (back,))
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present,
                         'the park must end behind the gate switch')
        self.assertEqual(self.hh.errors, [])

    def test_gate_first_scan_uses_the_preload_window_not_the_gate_window(self):
        """
        Regression test: _home_to_gate_with_nfc must sweep whatever window its caller's
        profile carries. MMU_PRELOAD drives _preload_profile(), so its forward sweep leg
        must be bounded by nfc_preload_jog_scan_window, not nfc_gate_jog_scan_window -
        the two are given distinct positive halves here so a fix that reads the wrong one
        is caught immediately.
        """
        u = self.hh.mmu.mmu_unit(0)
        u.p.nfc_gate_jog_scan_window = [-50.0, 30.0]
        u.p.nfc_preload_jog_scan_window = [-20.0, 12.0]
        self.hh.place_filament(0, position=-100.0)  # no tag: gate switch always wins first
        self.hh.run_gcode('MMU_PRELOAD GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and d > 0]
        self.assertEqual(trips, [12.0],
                         'preload swept %r - expected a single 12mm leg from '
                         'nfc_preload_jog_scan_window' % (trips,))
        self.assertEqual(self.hh.errors, [])


class NfcProbeTestCase(NfcScanTestCase):
    """Base for the presence-probe tests: deep reads on, probe path selectable."""

    PROBE_SUPPORT = False

    def setUp(self):
        super().setUp()
        # nfc_deep_read is the setting that used to make homing slow: it turned the
        # homing poll's read into a full metadata read. Every test below runs with it
        # ON, because that is the configuration the split has to make safe.
        self.hh.mmu.mmu_unit(0).p.nfc_deep_read = 1
        for chip in self.hh.nfc_chips.values():
            chip.probe_support = self.PROBE_SUPPORT

    def scan_gate_0(self, tag=TAG, position=None):
        self.fil.attach_tag(0, tag)
        self.preload(0, position=position)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')


class TestHomingUsesPresenceProbeOnly(NfcProbeTestCase):
    """
    The core invariant: a homing move must never read the tag.

    A deep read is slow enough to wreck homing accuracy and risk a Klipper "Timer too
    close", so homing gets a presence probe and the tag is read once afterwards, with
    the machine stationary.
    """

    PROBE_SUPPORT = True

    def test_homing_probes_and_does_not_read(self):
        chip = self.hh.chip(0)
        self.scan_gate_0()

        self.assertGreater(chip.probe_starts, 0, 'homing never started a presence probe')
        self.assertGreater(chip.probe_polls, 0, 'homing never ticked the probe')
        # The invariant that matters: reads either side of the probe window are fine
        # (_jog_scan pre-reads, read_gate_after_home reads once stopped) but never inside.
        self.assertEqual(chip.reads_during_probe(), 0,
                         'the tag was read while the homing probe was running: %r'
                         % (chip.events,))

    def test_the_tag_is_still_read_after_the_move(self):
        """The probe reports presence only, so the read has to happen somewhere."""
        chip = self.hh.chip(0)
        self.scan_gate_0()
        self.assertIn('probe_stop', chip.events)
        after = chip.events[chip.events.index('probe_stop'):]
        self.assertIn('read', after,
                      'nothing read the tag after the move - the gate map would never '
                      'learn the UID')

    def test_probe_is_stopped_after_a_hit(self):
        chip = self.hh.chip(0)
        self.scan_gate_0()
        self.assertGreater(chip.probe_stops, 0,
                           'a detected probe was never drained - a held target would '
                           'leak into the next operation')

    def test_probe_is_stopped_after_a_miss(self):
        """No tag anywhere: the probe still has to be torn down."""
        chip = self.hh.chip(0)
        self.preload(0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertGreater(chip.probe_starts, 0)
        self.assertGreater(chip.probe_stops, 0, 'a missed probe was never drained')

    def test_scan_still_finds_the_tag_with_deep_read_on(self):
        self.scan_gate_0()
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

    def test_probe_may_straddle_several_ticks(self):
        """
        The point of the non-blocking contract: a scan that isn't finished yet answers
        None and is picked up by a later tick, rather than being abandoned and restarted.
        """
        chip = self.hh.chip(0)
        chip.probe_latency_ticks = 3
        self.scan_gate_0()
        self.assertIn('tag read', ' '.join(self.hh.console).lower(),
                      'a probe spanning several ticks failed to detect the tag')
        self.assertGreater(chip.probe_polls, chip.probe_starts,
                           'a straddling scan must be polled more often than it is started')
        self.assertEqual(self.hh.errors, [])

    def test_a_probe_never_reports_a_uid_without_a_read(self):
        """
        get_status() publishes 'present' and 'uid' together, so a probe must not set
        either - only a real read may. present=True with uid=None is a pair no consumer
        has ever seen.
        """
        reader = self.hh.mmu.mmu_unit(0).nfc_manager.gate_readers[0]
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        reader.clear_uid()
        self.assertTrue(reader.probe_start())
        self.assertTrue(reader.probe_poll(), 'probe should see the tag here')
        self.assertIsNone(reader.last_uid, 'a probe must not set last_uid')
        self.assertFalse(reader.present, 'a probe must not set present')
        reader.probe_stop()

    def test_repeated_probes_keep_seeing_a_stationary_tag(self):
        """
        Regression guard for the RC522 REQA/READY trap: only IDLE tags answer REQA, so a
        probe built on it detects a tag once and then goes silent. Every tick against a
        tag that is still sitting there must report present.
        """
        reader = self.hh.mmu.mmu_unit(0).nfc_manager.gate_readers[0]
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        for tick in range(5):
            reader.probe_start()
            self.assertTrue(reader.probe_poll(),
                            'probe tick %d stopped seeing a stationary tag' % tick)
        reader.probe_stop()

    def test_tight_poll_interval_is_used(self):
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        reader = manager.gate_readers[0]
        self.assertTrue(reader.has_probe_support())
        self.assertEqual(manager._homing_poll_interval(reader),
                         mmu_nfc_manager.NFC_HOMING_POLL_INTERVAL)


class TestHomingProbeShimFallback(NfcProbeTestCase):
    """
    A driver without the probe contract must still home. MmuNfcReader falls back to one
    bounded read_target() per tick, and the manager slows the cadence to match.
    """

    PROBE_SUPPORT = False

    def test_shim_still_finds_the_tag(self):
        self.scan_gate_0()
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

    def test_shim_reader_reports_no_probe_support(self):
        reader = self.hh.mmu.mmu_unit(0).nfc_manager.gate_readers[0]
        self.assertFalse(reader.has_probe_support(),
                         'probe_supported() must be honoured, so a declining driver '
                         'takes the shim')

    def test_shim_uses_the_slower_poll_interval(self):
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        reader = manager.gate_readers[0]
        self.assertEqual(manager._homing_poll_interval(reader),
                         mmu_nfc_manager.NFC_HOMING_POLL_INTERVAL_SHIM)
        self.assertGreater(mmu_nfc_manager.NFC_HOMING_POLL_INTERVAL_SHIM,
                           mmu_nfc_manager.NFC_HOMING_POLL_INTERVAL,
                           'the shim cadence must be the slower of the two - a blocking '
                           'tick at the tight interval is worse than not tightening')


class TestDeepReadIsKeptOutOfMoves(NfcProbeTestCase):
    """The guards that make "no tag read while moving" structural, not incidental."""

    PROBE_SUPPORT = True

    def test_read_gate_refuses_a_deep_read_while_a_probe_is_armed(self):
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        endstop = manager.get_gate_endstop(0)
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])

        manager.start_homing_poll(endstop)
        self.assertIsNotNone(manager._homing_endstop, 'poll should be armed')
        chip = self.hh.chip(0)
        chip.reads = 0
        manager.read_gate(0)
        manager.stop_homing_poll()

        self.assertTrue(any('refusing a deep read' in e for e in self.hh.errors),
                        'read_gate() must refuse to deep read while a probe is armed')

    def test_post_move_read_is_allowed(self):
        """
        The mirror image: read_gate_after_home() runs once the probe is disarmed, so the
        guard must NOT fire for it or the deep read is silently disabled.
        """
        self.scan_gate_0()
        self.assertFalse(any('refusing a deep read' in e for e in self.hh.errors),
                         'the guard fired on the legitimate post-move read')
        self.assertEqual(self.hh.errors, [])

    def test_shared_poll_stands_down_while_a_probe_is_armed(self):
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        endstop = manager.get_gate_endstop(0)
        self.assertFalse(manager._movement_active(),
                         'nothing is moving yet')
        manager.start_homing_poll(endstop)
        self.assertTrue(manager._movement_active(),
                        'an armed probe must suppress the shared-reader poll')
        manager.stop_homing_poll()
        self.assertFalse(manager._movement_active(),
                         'suppression must lift once the probe is drained')

    def test_suppression_spans_the_deceleration_ramp(self):
        """
        A detection disarms the poll immediately (so the deep-read guard clears) but the
        chip may still have a scan in flight through the deceleration ramp, so
        suppression has to outlive _homing_endstop.
        """
        manager = self.hh.mmu.mmu_unit(0).nfc_manager
        endstop = manager.get_gate_endstop(0)
        manager.start_homing_poll(endstop)
        manager._disarm_homing_poll()
        self.assertIsNone(manager._homing_endstop)
        self.assertTrue(manager._movement_active(),
                        'an undrained probe must keep the shared poll suppressed')
        manager._drain_probe()
        self.assertFalse(manager._movement_active())


class TestReparkDrift(NfcScanTestCase):
    """
    A FIXED DEFECT - these are the regression guards.

    The scan used to walk the filament backward by roughly gate_parking_distance on every
    invocation. _unload_gate applies park_dist from wherever its reverse home ended, and
    that reverse home is 'home until the switch RELEASES' - so with the filament already
    behind the switch it completed in 0mm, reported success, and the park was applied from
    an arbitrary position. On BoxTurtle (park_dist -100, window +/-50) that was -95mm per
    scan; repeat it and the filament left the gate entirely.

    _jog_scan now homes to the gate for a DATUM before sweeping and re-parks exactly once,
    off that datum, so the park is always measured from the same reference. The two tests
    below were @unittest.expectedFailure while the bug stood.
    """

    def test_forward_scan_returns_to_the_park_position(self):
        """A scan inspects the tag and puts the filament back where it found it."""
        self.fil.attach_tag(0, TAG)
        self.preload(0)
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertIn('tag read', ' '.join(self.hh.console).lower(),
                      'precondition: the scan should have found the tag')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)
        self.assertEqual(self.hh.errors, [])

    def test_repeated_scans_do_not_walk_the_filament_out(self):
        """Three scans should leave the filament where it started, not ~300mm back."""
        self.fil.attach_tag(0, TAG)
        self.preload(0)
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        for _ in range(3):
            self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)

    def test_an_empty_gate_is_refused_before_anything_moves(self):
        """
        The scan jogs filament and homes it back, so there has to be filament there.
        Without this guard the datum home just runs its full length and fails with a
        homing error that says nothing about the real cause.
        """
        self.hh.place_filament(0)                       # physically there...
        self.hh.mmu.gate_maps.set_gate_status(0, 0)     # ...but the map says EMPTY
        self.fil.history.clear()
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.fil.history, [], 'an empty gate must not move the filament')
        self.assertTrue(any('is empty' in e for e in self.hh.errors),
                        'expected an explicit "gate is empty" error, got %r' % (self.hh.errors,))

    def scan_move_count(self, autoload):
        """Run one scan on a fresh session and return how many moves it made."""
        hh = session(self.PROFILE, virtual_nfc=True)
        try:
            hh.boot()
            fil = hh.filament()
            hh.mmu.mmu_unit(0).p.gate_autoload = autoload
            fil.attach_tag(0, TAG)
            hh.place_filament(0)
            hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
            hh.mmu.select_gate(0)
            fil.history.clear()
            hh.run_gcode('MMU_NFC_SCAN GATE=0')
            return len(fil.history), fil.tip[0], hh.errors
        finally:
            hh.close()

    def test_autoload_does_not_change_the_scan(self):
        """
        The datum leg homes THROUGH the gate, so it crosses the entry sensor. With
        gate_autoload set that raises an insert event and starts an MMU_PRELOAD inside the
        scan - a whole nested operation. wrap_suspend_insert_events covers it;
        wrap_suspend_filament_monitoring alone does not, because runout and insert are
        separate paths in MmuRunoutHelper.

        Compared against autoload off rather than a hard-coded move count, so this states
        the actual invariant - autoload must not affect a scan - and cannot drift as the
        leg sequence is tuned. Note the nested preload is NOT visible in history reasons
        (those are only 'homing -> <endstop>' / 'move'), so the move count is the signal.
        """
        off_moves, off_tip, off_errors = self.scan_move_count(0)
        on_moves, on_tip, on_errors = self.scan_move_count(1)
        self.assertEqual(off_errors, [])
        self.assertEqual(on_errors, [])
        self.assertEqual(on_moves, off_moves,
                         'gate_autoload changed the scan: %d moves with it on vs %d with it '
                         'off - an insert event started a nested preload'
                         % (on_moves, off_moves))
        self.assertAlmostEqual(on_tip, off_tip, places=1)

    def test_the_reverse_home_finds_the_switch_already_released(self):
        """
        Pins the MECHANISM that made the drift possible, one level below the symptom: a
        reverse home cannot establish a datum when the filament is already behind the
        switch. Still true - it is why _jog_scan now homes FORWARD to the gate first
        rather than trusting _unload_gate to find the datum on its own.
        """
        self.hh.place_filament(0)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        # sought=False is "home until the switch releases"; already released -> 0 travel
        trip = self.fil.trip_distance(0, -300., ['mmu_exit_0'], sought=False)
        self.assertEqual(trip, ('mmu_exit_0', 0.0))


class TestSharedGateOccupancy(NfcScanTestCase):
    """
    PR #1028/#1032 fixed _park_after_scan's re-park for a SHARED gate endstop (mmu_shared_exit
    / extruder_entry) but, by its own added comment in _validate_nfc_gate_jog_scan_window,
    left the actual cross-gate hazard unguarded: nothing stopped a scan from sweeping into a
    shared path another gate on the same unit was already occupying. can_crossload cannot
    cover this on its own - it says the SELECTOR mechanism won't jam moving between gates, not
    that a downstream SHARED sensor/encoder is clear. These pin the guard added on top:
    _shared_gate_path_occupied(), called from the command-level can_continue check and again
    (for the switch-based endstops) from _jog_scan/_preload_gate itself.
    """

    def use_shared_endstop(self, endstop, gate=0):
        self.hh.mmu.mmu_unit(gate).p.gate_homing_endstop = endstop

    def test_shared_exit_rewind_settles_and_does_not_drift_on_repeated_scans(self):
        """
        The open-loop rewind _park_after_scan now uses for mmu_shared_exit/extruder_entry
        must settle at ONE park position and stay there - the same invariant TestReparkDrift
        pins for the per-gate mmu_exit case, just off a different datum (mmu_shared_exit sits
        +10mm from the gate, not 0, so the settled park differs from mmu_exit's -100).
        """
        self.use_shared_endstop('mmu_shared_exit')
        self.preload(0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        settled = self.fil.tip[0]
        for _ in range(3):
            self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.hh.errors, [])
        self.assertAlmostEqual(self.fil.tip[0], settled, places=1,
                               msg='repeated scans drifted away from the settled shared-exit park')

    def test_shared_exit_scan_is_refused_when_a_sibling_gate_occupies_it(self):
        """
        mmu_shared_exit is ONE physical switch shared by every gate on the unit. If gate 1's
        filament is currently sitting on it, scanning gate 0 must not sweep into that
        occupied territory - refuse before any motion, the same way _preload_gate already did
        for this endstop (now widened and reused here).
        """
        self.use_shared_endstop('mmu_shared_exit')
        self.preload(0)
        self.hh.place_filament(1, position=self.fil.layout['mmu_shared_exit'] + 5.0)
        self.hh.mmu.gate_maps.set_gate_status(1, GATE_AVAILABLE)
        self.hh.mmu.select_gate(0)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.fil.history, [], 'an occupied shared path must not move the filament')
        self.assertTrue(any('occupied' in e for e in self.hh.errors), self.hh.errors)

    def test_encoder_scan_is_refused_when_the_active_filament_is_on_a_sibling_gate(self):
        """
        The encoder has no presence sensor, so the only signal equivalent to "is the shared
        path occupied" is whether the unit's actively fed-forward filament belongs to a
        different gate - measured before this command's own gate selection moves it. The
        command-level can_continue check is the only place this is checkable (see
        _shared_gate_path_occupied's docstring), so it must refuse here even though the unit
        is crossload-capable.

        Needs a profile with a real encoder object (nfc_per_gate has none), so this boots its
        own session rather than using self.hh.
        """
        hh = session(NFC_PER_GATE_ENCODER, virtual_nfc=True)
        try:
            hh.boot()
            self.assertEqual(hh.errors, [], 'bootup was not clean')
            fil = hh.filament()
            self.assertEqual(hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'encoder')
            self.assertTrue(hh.mmu.mmu_unit(0).can_crossload, 'precondition: boxturtle is crossload-capable')
            hh.place_filament(0)
            hh.mmu.gate_maps.set_gate_status(0, GATE_AVAILABLE)
            hh.place_filament(1)
            hh.mmu.gate_maps.set_gate_status(1, GATE_AVAILABLE)
            hh.mmu.select_gate(1)
            hh.mmu.filament_pos = FILAMENT_POS_LOADED
            fil.history.clear()
            hh.run_gcode('MMU_NFC_SCAN GATE=0')
            self.assertEqual(fil.history, [], 'encoder-shared occupancy must not move the filament')
            self.assertTrue(hh.errors, 'expected the scan to be refused')
        finally:
            hh.close()

    def test_switching_to_a_shared_endstop_rechecks_a_stale_parking_distance(self):
        """
        gate_parking_distance's legal sign depends on gate_homing_endstop (see
        _validate_gate_parking_distance). MMU_TEST_CONFIG already re-validates correctly
        within ONE combined command (alphabetical field order applies gate_homing_endstop
        first), but two SEPARATE commands used to leave a hole: set a positive parking
        distance while on mmu_exit (legal), then switch to a shared endstop in a later
        command, and the stale now-unsafe value was never rechecked.
        """
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_parking_distance=10')
        self.assertEqual(self.hh.errors, [])
        with self.assertRaises(Exception) as cm:
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 gate_homing_endstop=encoder')
        self.assertIn('gate_parking_distance', str(cm.exception))
        # The switch must not have landed half-applied with a stale positive parking distance
        self.assertEqual(self.hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'encoder')


if __name__ == '__main__':
    unittest.main()
