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

logging.getLogger().setLevel(logging.CRITICAL)

GATE_AVAILABLE = 1
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
        self.hh.place_filament(0)                       # parked at -100
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and d > 0]
        self.assertTrue(trips, 'forward jog never tripped the NFC endstop')
        self.assertAlmostEqual(trips[0], self.window_edge() - start, places=3)
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

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
        self.hh.place_filament(0, position=-60.0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')

        trips = [d for _g, d, r in self.fil.history if 'mmu_nfc_0' in r and d < 0]
        self.assertTrue(trips, 'backward jog never tripped the NFC endstop - has the '
                               'triggered=True pin in MmuNfcEndstop.home_start gone?')
        self.assertIn('tag read', ' '.join(self.hh.console).lower())
        self.assertEqual(self.hh.errors, [])

    def test_scan_with_no_tag_reports_nothing_found(self):
        self.hh.place_filament(0)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertNotIn('tag read', ' '.join(self.hh.console).lower())

    def test_tag_already_on_the_reader_short_circuits_the_jog(self):
        """
        _jog_scan pre-reads before moving, so a tag already sitting on the reader is
        resolved with zero motion.
        """
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0, position=self.fil.layout['mmu_nfc'])
        self.hh.mmu.select_gate(0)
        self.fil.history.clear()
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertEqual(self.fil.history, [],
                         'a tag already under the reader must need no jogging')
        self.assertIn('tag read', ' '.join(self.hh.console).lower())


class TestPreloadNfcCompound(NfcScanTestCase):
    """
    _home_gate_with_nfc: preload homes to a FIRST-WINS MmuCompoundEndstop over
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


class TestReparkDrift(NfcScanTestCase):
    """
    A REAL DEFECT, found by running the scan for the first time.

    _unload_gate applies gate_parking_distance from wherever its reverse-home ended. That
    reverse-home is supposed to establish a datum at the gate endstop - but when the
    filament is BEHIND that endstop the home completes having moved 0mm (the switch is
    already released), so the park is applied from an arbitrary position and the filament
    ends up a further ~gate_parking_distance back than it started.

    Happy Hare's own trace agrees with the model:
        Reverse homing off mmu_exit_0 ... homed after moving 0.0mm (of max -305.0mm)
        Final parking. Stepper: 'gear' moved -100.0mm ... --> Pos: @-95.0
    i.e. 95mm behind where the scan began, for a 5mm jog.

    So each MMU_NFC_SCAN walks the filament backwards by roughly the parking distance.
    Repeat it and the filament leaves the gate entirely.

    The precondition for the forward path to be sound is that the jog can actually reach
    the gate datum, i.e. nfc_gate_jog_scan_window[1] >= abs(gate_parking_distance).
    BoxTurtle ships window +/-50 with gate_parking_distance -100, so it never can. Nothing
    validates this today - _validate_nfc_gate_jog_scan_window only checks
    abs(neg) <= gate_homing_max.
    """

    def test_forward_scan_over_retracts(self):
        """Documents the CURRENT behaviour so a fix is visible as a change here."""
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0)
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        drift = self.fil.tip[0] - start
        parking = self.hh.mmu.mmu_unit(0).p.gate_parking_distance
        self.assertLess(drift, 0.0, 'expected a net retraction')
        self.assertAlmostEqual(drift, parking + (self.window_edge() - start), places=2)

    @unittest.expectedFailure
    def test_forward_scan_should_return_to_the_park_position(self):
        """
        What SHOULD happen: a scan inspects the tag and puts the filament back. Flips
        green when the re-park is fixed; delete this and fold the assertion into
        test_forward_scan_over_retracts then.
        """
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0)
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)

    @unittest.expectedFailure
    def test_repeated_scans_should_not_walk_the_filament_out(self):
        """Three scans should leave the filament where it started, not ~300mm back."""
        self.fil.attach_tag(0, TAG)
        self.hh.place_filament(0)
        start = self.fil.tip[0]
        self.hh.mmu.select_gate(0)
        for _ in range(3):
            self.hh.run_gcode('MMU_NFC_SCAN GATE=0')
        self.assertAlmostEqual(self.fil.tip[0], start, places=1)

    def test_the_reverse_home_finds_the_switch_already_released(self):
        """
        Pins the mechanism rather than the symptom: with the filament behind the gate
        switch the datum-establishing reverse home cannot move.
        """
        self.hh.place_filament(0)
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        # sought=False is "home until the switch releases"; already released -> 0 travel
        trip = self.fil.trip_distance(0, -300., ['mmu_exit_0'], sought=False)
        self.assertEqual(trip, ('mmu_exit_0', 0.0))


if __name__ == '__main__':
    unittest.main()
