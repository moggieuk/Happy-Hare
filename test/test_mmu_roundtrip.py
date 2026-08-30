# Happy Hare test harness - milestones B2 and B3: the Klipper <-> Moonraker contract.
#
# THIS IS THE MILESTONE THE WHOLE HARNESS EXISTS FOR.
#
# The real MmuController and the real MmuServer run in one process, connected by
# test/hh/roundtrip.py's two-queue settle. Everything asserted below is
# session-1..5 work that, per the dev handoffs, had never executed: "static-verified
# only (ast.parse) - nothing run on hardware."
#
# Tags are injected via HH's own _MMU_TEST NFC_READ=1 hook, which hands off at
# nfc_manager._dispatch_lookup - exactly where a real reader would - so no reader
# hardware or driver is involved in the contract tests.
#
# Note the 12-second warm-up in setUp: effect_initialized is a unit-wide TIMED state
# effect lasting 8s from bootup, and per the LED design a transient flash requested
# while such an effect holds the unit is DROPPED, not deferred. Tests that care about
# LEDs or pending overlays must wait it out or they measure the rainbow.
#
#   ./venv/bin/python -m unittest test.test_mmu_roundtrip
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh.roundtrip import RoundTrip

logging.getLogger().setLevel(logging.CRITICAL)

TAG_A = 'AAAA1111'
TAG_B = 'BBBB2222'
TAG_C = 'CCCC3333'
UNKNOWN_TAG = 'DEADBEEF'
TAG_METADATA = dict(material='PETG', brand='Overture', color='00FF00',
                    detail='PETG_Basic', min_temp=230, max_temp=250)


class RoundTripTestCase(unittest.TestCase):
    PROFILE = 'nfc_spoolman'
    SPOOLS = ()
    WARMUP = 12.0       # past the 8s effect_initialized state flash

    def setUp(self):
        self.rt = RoundTrip(profile=self.PROFILE, spools=list(self.SPOOLS))
        self.rt.boot()
        self.assertEqual(self.rt.errors, [], 'bootup was not clean')
        if self.WARMUP:
            self.rt.advance(self.WARMUP)

    def tearDown(self):
        self.rt.close()

    def last_callback(self):
        commands = self.rt.gate_map_commands()
        return commands[-1] if commands else None


class TestContractIsWired(RoundTripTestCase):

    def test_bootup_closes_the_loop(self):
        """
        Bootup calls _spoolman_sync, Moonraker answers with a gate-map push, and
        Klipper applies it. Proves both directions work before anything else is tested.
        """
        self.assertIn('spoolman_push_gate_map',
                      [name for name, _ in self.rt.remote_calls()])
        self.assertTrue(self.rt.gate_map_commands(),
                        'Moonraker sent no gate map back to Klipper')

    def test_spoolman_is_actually_enabled(self):
        """
        Guards against the whole suite going vacuous: spoolman_support defaults to
        'off', in which case none of the flows below would run.
        """
        self.assertEqual(self.rt.mmu.p.spoolman_support, 'push')
        self.assertEqual(self.rt.mmu.p.spoolman_nfc_auto_create, 1)
        self.assertEqual(self.rt.mmu.mmu_unit().p.nfc_deep_read, 1)

    def test_unknown_remote_method_would_be_caught(self):
        """
        The dispatcher fails loudly rather than silently dropping a call, so a
        renamed remote method cannot pass unnoticed.
        """
        with self.assertRaises(AssertionError):
            self.rt._dispatch('spoolman_no_such_method', {})


class TestSharedReaderAutoCreate(RoundTripTestCase):
    """
    The headline flow, end to end across both processes:
      tag -> _dispatch_lookup -> spoolman_get_spool_by_uid -> miss -> auto-create
          -> NEXT_SPOOLID=n CREATED=1 -> pending spool_id -> overlay -> timeout
    """

    def test_unknown_tag_creates_a_spool_and_becomes_pending(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=True, **TAG_METADATA)
        self.assertEqual(len(self.rt.db.created_spools), 1)
        spool_id = self.rt.db.created_spools[0]
        self.assertEqual(self.last_callback(),
                         'MMU_GATE_MAP NEXT_SPOOLID=%d CREATED=1 QUIET=1' % spool_id)
        self.assertEqual(self.rt.mmu.pending_spool_id, spool_id)
        self.assertEqual(self.rt.mmu.pending_phase, 'pending')
        self.assertEqual(self.rt.errors, [])

    def test_created_spool_carries_the_tag_data(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=True, **TAG_METADATA)
        spool = self.rt.db.spools[self.rt.db.created_spools[0]]
        self.assertEqual(spool['filament']['material'], 'PETG')
        self.assertEqual(spool['filament']['vendor']['name'], 'Overture')
        self.assertEqual(spool['filament']['settings_extruder_temp'], 240)

    def test_uid_is_registered_so_a_rescan_resolves(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=True, **TAG_METADATA)
        spool_id = self.rt.db.created_spools[0]
        self.rt.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=0')     # clear the pending
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=True, **TAG_METADATA)
        self.assertEqual(len(self.rt.db.created_spools), 1,
                         'a re-scan must resolve, not create a duplicate')
        self.assertEqual(self.last_callback(),
                         'MMU_GATE_MAP NEXT_SPOOLID=%d QUIET=1' % spool_id)

    def test_pending_expires_through_warn_to_timeout(self):
        """
        The full pending lifecycle in virtual time: 'pending' -> 'expiring' at
        T-PENDING_LED_WARN_WINDOW -> cleared at spoolman_pending_id_timeout.
        """
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=True, **TAG_METADATA)
        timeout = self.rt.mmu.p.spoolman_pending_id_timeout
        self.assertEqual(self.rt.mmu.pending_phase, 'pending')
        self.rt.advance(timeout - 4.0)
        self.assertEqual(self.rt.mmu.pending_phase, 'expiring')
        self.rt.advance(6.0)
        self.assertIsNone(self.rt.mmu.pending_phase)
        self.assertEqual(self.rt.mmu.pending_spool_id, -1)

    def test_no_autocreate_without_metadata(self):
        """A UID-only read of an unregistered tag is a definitive miss on spool_id."""
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=False)
        self.assertEqual(self.rt.db.created_spools, [])
        self.assertEqual(self.last_callback(),
                         'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')
        self.assertEqual(self.rt.mmu.pending_spool_id, -1)

    def test_bare_uid_is_retained_as_a_weak_pending_after_the_miss(self):
        """
        The uid itself is not a dead end even though Spoolman doesn't know it - it stays
        staged (no spool_id LED overlay, since there is no spool_id) so it can still land
        on whichever gate loads next.
        """
        self.rt.present_tag(UNKNOWN_TAG, gate=None, deep=False)
        self.assertEqual(self.rt.mmu.pending_tag, (UNKNOWN_TAG, None))


class TestKnownTagResolution(RoundTripTestCase):
    SPOOLS = (dict(uid=TAG_A, material='PLA', vendor='Prusament',
                   name='PLA Galaxy Black'),)

    def test_shared_reader_sets_a_pending_spool(self):
        self.rt.present_tag(TAG_A, gate=None, deep=False)
        self.assertEqual(self.last_callback(), 'MMU_GATE_MAP NEXT_SPOOLID=1 QUIET=1')
        self.assertEqual(self.rt.mmu.pending_spool_id, 1)

    def test_per_gate_reader_updates_the_gate_map_directly(self):
        """
        A per-gate read knows its gate, so it assigns in place rather than pending.

        This is a TWO-step round trip, which is worth spelling out: Moonraker first
        sends GATE=n SPOOLID=id, and Klipper's handler then asks for that spool's
        attributes, which come back as a second FROM_SPOOLMAN=1 MAP callback carrying
        material/colour/name/temp/vendor. Only after both has the gate map converged.
        """
        self.rt.present_tag(TAG_A, gate=1, deep=False)
        callbacks = self.rt.gate_map_commands()
        self.assertIn('MMU_GATE_MAP GATE=1 SPOOLID=1 RFIDS=%s QUIET=1' % TAG_A,
                      callbacks)

        follow_ups = [c for c in callbacks if 'FROM_SPOOLMAN=1' in c and "'material'" in c]
        self.assertTrue(follow_ups,
                        'expected an attribute push after the spool assignment; got %r'
                        % (callbacks,))
        self.assertIn("'material': 'PLA'", follow_ups[-1])

        self.assertEqual(self.rt.mmu.gate_spool_id[1], 1)
        self.assertEqual(self.rt.mmu.gate_material[1], 'PLA')
        self.assertEqual(self.rt.mmu.gate_vendor[1], 'Prusament')
        self.assertEqual(self.rt.mmu.pending_spool_id, -1,
                         'a per-gate read must not also set a pending spool')

    def test_resolved_attributes_include_the_tag_alias(self):
        """
        The attribute push carries the complete Spoolman UID set separately from
        the one physical UID observed at the gate.
        """
        self.rt.present_tag(TAG_A, gate=1, deep=False)
        pushes = [c for c in self.rt.gate_map_commands() if 'FROM_SPOOLMAN=1' in c]
        self.assertTrue(any("'rfids': '%s'" % TAG_A in c for c in pushes))

    def test_only_the_addressed_gate_changes(self):
        self.rt.present_tag(TAG_A, gate=2, deep=False)
        self.assertEqual(list(self.rt.mmu.gate_spool_id), [-1, -1, 1, -1])

    def test_shared_resolution_uid_is_applied_with_the_spool_id(self):
        """
        A shared read that resolves to a spool_id must not lose the uid itself - once
        applied to a gate, gate_spool_rfid records which physical tag resolved there.
        """
        self.rt.present_tag(TAG_A, gate=None, deep=False)
        self.assertEqual(self.rt.mmu.pending_tag, (TAG_A, None),
                         'the uid must survive spool_id resolution, only metadata is superseded')
        self.rt.mmu._check_pending_filament(3)
        self.assertEqual(self.rt.mmu.gate_spool_id[3], 1)
        self.assertEqual(self.rt.mmu.gate_spool_rfid[3], TAG_A)


class TestMultiUidGateResolution(RoundTripTestCase):
    SPOOLS = (dict(uid=[TAG_A, TAG_B], material='PLA'),)

    def test_gate_keeps_observed_uid_and_caches_complete_alias_set(self):
        self.rt.present_tag(TAG_B, gate=1, deep=False)

        self.assertEqual(self.rt.mmu.gate_spool_rfid[1], TAG_B)
        self.assertNotIn(',', self.rt.mmu.gate_spool_rfid[1])
        self.assertEqual(self.rt.mmu.gate_maps.gate_spool_rfid_aliases[1],
                         (TAG_A, TAG_B))
        self.assertTrue(any('RFIDS=%s,%s' % (TAG_A, TAG_B) in command
                            for command in self.rt.gate_map_commands()))

    def test_alternate_known_uid_does_not_clear_existing_spool_assignment(self):
        self.rt.present_tag(TAG_A, gate=1, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[1], 1)

        self.rt.present_tag(TAG_B.lower(), gate=1, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[1], 1)
        self.assertEqual(self.rt.mmu.gate_spool_rfid[1], TAG_B)


class TestPerGateFailure(RoundTripTestCase):
    """
    Session 5: a per-gate lookup failure used to send NOTHING back. Now all three
    failure sites report GATE=x LOOKUP=-1|-2 so the console and the gate's LEDs
    learn about it. The gate map's *filament attributes* stay untouched by a
    failure, but a genuinely different physical tag (new uid) always lands its
    uid on the gate and clears whatever spool_id belonged to the old one - see
    TestStaleSpoolIdIsCleared below.
    """
    SPOOLS = (dict(uid=TAG_A, material='PLA'),)

    def test_unknown_tag_reports_lookup_failure(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=2, deep=False)
        self.assertEqual(self.last_callback(), 'MMU_GATE_MAP GATE=2 LOOKUP=-2 QUIET=1')

    def test_a_new_tags_failure_clears_the_old_spool_id(self):
        """
        Gate 2 was carrying spool 1's tag. A different, unregistered tag is scanned on
        the same gate - the old spool_id must not linger next to the new uid.
        """
        self.rt.present_tag(TAG_A, gate=2, deep=False)
        self.rt.present_tag(UNKNOWN_TAG, gate=2, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[2], -1,
                         'a new physical tag must clear the old spool_id, not carry it over')
        self.assertEqual(self.rt.mmu.gate_spool_rfid[2], UNKNOWN_TAG)

    def test_rescanning_the_same_tag_does_not_clear_its_own_assignment(self):
        """
        The clear is keyed on the uid changing, not on the lookup failing - re-presenting
        the SAME tag (e.g. a recoverable Spoolman outage) must leave the assignment alone.
        """
        self.rt.present_tag(TAG_A, gate=2, deep=False)
        self.rt.db.offline = True
        self.rt.present_tag(TAG_A, gate=2, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[2], 1,
                         'a recoverable failure on the SAME tag must not clear the assignment')

    def test_failure_is_surfaced_to_the_console(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=2, deep=False)
        joined = ' '.join(self.rt.errors + self.rt.klipper.console)
        self.assertIn('NFC', joined)

    def test_outage_is_reported_as_recoverable(self):
        self.rt.db.offline = True
        self.rt.present_tag(UNKNOWN_TAG, gate=3, deep=False)
        self.assertEqual(self.last_callback(), 'MMU_GATE_MAP GATE=3 LOOKUP=-1 QUIET=1')


class TestPendingCancellation(RoundTripTestCase):
    """
    Session 5 narrowed which actions invalidate a pending spool_id to filament-moving
    ones only, so the natural "scan tag -> select gate -> load" flow survives
    (extras/mmu/mmu_controller.py:2515).
    """
    SPOOLS = (dict(uid=TAG_A, material='PLA'),)

    def _pend(self):
        self.rt.present_tag(TAG_A, gate=None, deep=False)
        self.assertEqual(self.rt.mmu.pending_spool_id, 1)

    def test_pending_survives_gate_selection(self):
        self._pend()
        self.rt.run_gcode('MMU_SELECT GATE=2')
        self.assertEqual(self.rt.mmu.gate_selected, 2)
        self.assertEqual(self.rt.mmu.pending_spool_id, 1,
                         'ACTION_SELECTING is exempt from the pending cancel')

    def test_zero_cancels_quietly(self):
        """
        NEXT_SPOOLID=0 is a deliberate user cancel, not a failure: no error, and no
        fail flash. Spool ids are 1-based so 0 is unambiguous.
        """
        self._pend()
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=0')
        self.assertEqual(self.rt.mmu.pending_spool_id, -1)
        self.assertIsNone(self.rt.mmu.pending_phase)
        self.assertEqual(len(self.rt.errors), errors_before,
                         'a deliberate cancel must be quiet')

    def test_negative_with_no_lookup_in_flight_is_also_quiet(self):
        self._pend()
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_GATE_MAP NEXT_SPOOLID=-1')
        self.assertEqual(len(self.rt.errors), errors_before)


class TestGateMapMaintenance(RoundTripTestCase):
    SPOOLS = (dict(uid=TAG_A, material='ABS', vendor='Polymaker'),)

    def test_eject_fully_clears_a_gate(self):
        """
        Session 5 (7c0a0cb2): ejecting used to leave stale name/material/vendor/
        colour/temp/spool_id/rfid behind and only set availability to EMPTY.
        reset_gate now reverts attributes to configured defaults, forces EMPTY, and
        always clears the spool identity.
        """
        self.rt.present_tag(TAG_A, gate=3, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[3], 1)
        self.assertEqual(self.rt.mmu.gate_material[3], 'ABS')

        self.rt.mmu.gate_maps.reset_gate(3)
        self.rt.settle()

        self.assertEqual(self.rt.mmu.gate_spool_id[3], -1)
        self.assertEqual(self.rt.mmu.gate_material[3], '')
        self.assertEqual(self.rt.mmu.gate_vendor[3], '')
        self.assertEqual(self.rt.mmu.gate_spool_rfid[3], '')
        self.assertEqual(self.rt.mmu.gate_status[3], 0)     # GATE_EMPTY

    def test_eject_only_touches_the_named_gate(self):
        self.rt.present_tag(TAG_A, gate=0, deep=False)
        self.rt.mmu.gate_maps.reset_gate(2)
        self.rt.settle()
        self.assertEqual(self.rt.mmu.gate_spool_id[0], 1)


class TestSpoolmanOff(RoundTripTestCase):
    """
    Deep-read metadata must reach the gate map even with Spoolman disabled - that is
    the point of the _nfc_tag_read umbrella (session 1 §F). Uses the NFC profile
    WITHOUT spoolman so nothing is dispatched to Moonraker.
    """
    PROFILE = 'nfc_per_gate'

    def test_no_lookup_is_dispatched(self):
        self.assertEqual(self.rt.mmu.p.spoolman_support, 'off')
        self.rt.present_tag(UNKNOWN_TAG, gate=1, deep=True, **TAG_METADATA)
        self.assertEqual(self.rt.remote_calls('spoolman_get_spool_by_uid'), [])
        self.assertEqual(self.rt.db.created_spools, [])

    def test_metadata_still_reaches_the_gate_map(self):
        self.rt.present_tag(UNKNOWN_TAG, gate=1, deep=True, **TAG_METADATA)
        self.assertEqual(self.rt.mmu.gate_material[1], 'PETG')
        self.assertEqual(self.rt.mmu.gate_vendor[1], 'Overture')
        self.assertEqual(self.rt.mmu.gate_spool_id[1], -1,
                         'no Spoolman means no spool id')
        self.assertEqual(self.rt.errors, [])

    def test_bare_uid_still_reaches_the_gate_map(self):
        """Even with no metadata and no spool_id, the uid itself is recorded locally."""
        self.rt.present_tag(UNKNOWN_TAG, gate=1, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_rfid[1], UNKNOWN_TAG)
        self.assertEqual(self.rt.mmu.gate_spool_id[1], -1)
        self.assertEqual(self.rt.mmu.gate_material[1], '')
        self.assertEqual(self.rt.errors, [])

    def test_gate_map_changes_refresh_moonraker_lane_data(self):
        calls_before = len(self.rt.remote_calls('moonraker_push_lane_data'))
        self.rt.run_gcode(
            'MMU_GATE_MAP GATE=1 AVAILABLE=1 NAME="PLA Basic" '
            'MATERIAL=PLA VENDOR="Bambu Lab" COLOR=00AE42 TEMP=210'
        )

        calls = self.rt.remote_calls('moonraker_push_lane_data')
        self.assertEqual(len(calls), calls_before + 1)
        self.assertEqual(
            calls[-1]['gate_ids'],
            list(enumerate(self.rt.mmu.gate_spool_id))
        )

    def test_gate_status_changes_refresh_moonraker_lane_data(self):
        self.rt.run_gcode(
            'MMU_GATE_MAP GATE=1 AVAILABLE=1 MATERIAL=PLA COLOR=00AE42 TEMP=210'
        )
        calls_before = len(self.rt.remote_calls('moonraker_push_lane_data'))
        self.rt.mmu.gate_maps.set_gate_status(1, 0)
        self.rt.settle()

        calls = self.rt.remote_calls('moonraker_push_lane_data')
        self.assertEqual(len(calls), calls_before + 1)
        self.assertEqual(calls[-1]['gate_ids'], [(1, -1)])


class TestNfcCommandSurface(RoundTripTestCase):
    """
    MMU_NFC as expanded in session 5. The readers here are real driver objects
    running against a scripted RC522 (test/hh/nfc_fixtures.py), so this exercises the
    driver's init/is_alive path too - previously never run.
    """
    SPOOLS = (dict(uid=TAG_A, material='PLA'),)

    def test_status_report_shows_every_reader_alive(self):
        self.rt.run_gcode('MMU_NFC')
        report = ' '.join(self.rt.klipper.console)
        for gate in range(4):
            self.assertIn('gate %d:' % gate, report)
        self.assertNotIn('alive=0', report)

    def test_gates_addresses_multiple_readers(self):
        """GATES=g,g,g - session 5's multi-gate addressing."""
        self.rt.run_gcode('MMU_NFC GATES=0,1,2,3 ENABLE=0')
        manager = self.rt.mmu.mmu_unit().nfc_manager
        for gate in range(4):
            self.assertFalse(manager.is_enabled(gate=gate),
                             'gate %d reader should be disabled' % gate)

    def test_disabled_reader_is_reenabled(self):
        self.rt.run_gcode('MMU_NFC GATES=0,1,2,3 ENABLE=0')
        self.rt.run_gcode('MMU_NFC GATES=0,1,2,3 ENABLE=1')
        manager = self.rt.mmu.mmu_unit().nfc_manager
        for gate in range(4):
            self.assertTrue(manager.is_enabled(gate=gate))

    def test_scan_command_rejects_a_gate_with_no_reader(self):
        """
        MMU_NFC_SCAN fails fast before announcing or selecting (session 5,
        commit 9002588e). Disable the reader to hit that guard.
        """
        self.rt.run_gcode('MMU_NFC GATE=1 ENABLE=0')
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_NFC_SCAN GATE=1')
        self.assertGreater(len(self.rt.errors), errors_before,
                           'scanning a disabled reader must error, not proceed')
        self.assertIn('disabled', self.rt.errors[-1].lower())


class TestSpoolmanRfidCommand(RoundTripTestCase):
    """
    'MMU_SPOOLMAN_TAG ... RFID=' end to end: Klipper command -> _spoolman_set_spool_uid ->
    webhook -> MmuServer.set_spool_uid -> the spoolman db.

    Direction matters: this BINDS a tag onto an existing spool. The other direction -
    UID in, spool found or auto-created - is MMU_NFC REGISTER=1 / get_spool_by_uid, and
    is covered above. Neither substitutes for the other.
    """

    SPOOLS = (dict(uid=TAG_A, material='PLA'),
              dict(material='ABS', vendor='Polymaker'))

    def test_explicit_spoolid_registers_the_tag(self):
        self.assertEqual(self.rt.db.spool_uid(2), '', 'precondition: spool 2 has no tag')
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s' % TAG_B)
        self.assertEqual(self.rt.db.spool_uid(2), TAG_B)

    def test_gate_resolves_to_its_assigned_spool(self):
        self.rt.run_gcode('MMU_GATE_MAP GATE=3 SPOOLID=2')
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 RFID=%s' % TAG_B)
        self.assertEqual(self.rt.db.spool_uid(2), TAG_B,
                         "GATE= must resolve through the gate map to spool 2")

    def test_separators_are_stripped(self):
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=bb:bb:22:22')
        self.assertEqual(self.rt.db.spool_uid(2), TAG_B, 'normalised uppercase, no separators')

    def test_does_not_unset_the_gate_assignment(self):
        """
        REGRESSION GUARD. A bare SPOOLID= with no GATE= already means "unset that
        spool's gate", so RFID= has to be dispatched before that branch or registering
        a tag would silently clear the spool's gate as a side effect.
        """
        self.rt.run_gcode('MMU_GATE_MAP GATE=3 SPOOLID=2')
        self.assertEqual(self.rt.db.spool_gate(2), 3, 'precondition: spool 2 is on gate 3')
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s' % TAG_B)
        self.assertEqual(self.rt.db.spool_gate(2), 3, 'gate assignment must survive')
        self.assertEqual(self.rt.db.spool_uid(2), TAG_B)

    def test_gate_with_no_spool_errors(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=1 RFID=%s' % TAG_B)
        self.assertGreater(len(self.rt.errors), errors_before)
        self.assertIn('no spoolman spool assigned', self.rt.errors[-1].lower())

    def test_neither_spoolid_nor_gate_errors(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG RFID=%s' % TAG_B)
        self.assertGreater(len(self.rt.errors), errors_before)
        self.assertIn('spoolid', self.rt.errors[-1].lower())

    def test_blank_rfid_clears_the_registered_tag(self):
        """
        Blank RFID= (no APPEND=1) is the documented way to unregister all tags
        from a spool - it must succeed silently, not error (that used to be
        rejected outright; now RFID='' is how a spool's tag(s) get cleared).
        """
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s' % TAG_B)
        errors_before = len(self.rt.errors)
        self.rt.run_gcode("MMU_SPOOLMAN_TAG SPOOLID=2 RFID=''")
        self.assertEqual(len(self.rt.errors), errors_before)
        self.assertEqual(self.rt.db.spool_uid(2), '')

    def test_append_adds_a_second_tag_without_losing_the_first(self):
        """A spool can carry more than one physical tag (e.g. one on each side)."""
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s' % TAG_B)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s APPEND=1' % TAG_C)
        self.assertEqual(set(self.rt.db.spool_uids(2)), {TAG_B, TAG_C})

    def test_gate_targeted_update_refreshes_and_clears_alias_cache(self):
        self.rt.run_gcode('MMU_GATE_MAP GATE=3 SPOOLID=2')
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 RFID=%s' % TAG_B)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 RFID=%s APPEND=1' % TAG_C)
        self.assertEqual(self.rt.mmu.gate_maps.gate_spool_rfid_aliases[3],
                         (TAG_B, TAG_C))

        self.rt.run_gcode("MMU_SPOOLMAN_TAG GATE=3 RFID=''")
        self.assertEqual(self.rt.mmu.gate_maps.gate_spool_rfid_aliases[3], tuple())

    def test_the_registered_tag_then_resolves_on_a_scan(self):
        """The round trip: bind a tag, then scanning it must resolve to that spool."""
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 RFID=%s' % TAG_B)
        self.rt.present_tag(TAG_B, gate=0, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_id[0], 2,
                         'the freshly bound tag must resolve to spool 2')
        self.assertEqual(len(self.rt.db.created_spools), 0,
                         'it must resolve, not auto-create a duplicate')


class TestSpoolmanRegisterCommand(RoundTripTestCase):
    """
    'MMU_SPOOLMAN_TAG GATE= SPOOLID= REGISTER=1': a spool with no Spoolman entry at scan
    time leaves its uid sitting in the gate map with no spool_id. Once the matching
    spool exists, REGISTER=1 binds the gate's already-known uid onto it - without any
    new tag read. The gate map only updates once Spoolman confirms the write (via the
    same 'MMU_GATE_MAP GATE=<g> SPOOLID=<id>' callback a per-gate NFC lookup resolution
    uses), never optimistically.
    """
    SPOOLS = (dict(uid=TAG_A, material='PLA'),
              dict(material='ABS', vendor='Polymaker'))

    def _scan_bare_uid(self, gate=3, uid=UNKNOWN_TAG):
        self.rt.present_tag(uid, gate=gate, deep=False)
        self.assertEqual(self.rt.mmu.gate_spool_rfid[gate], uid, 'precondition: uid recorded, no spool')
        self.assertEqual(self.rt.mmu.gate_spool_id[gate], -1, 'precondition: no spool_id yet')

    def test_registers_the_gates_known_uid_and_assigns_locally(self):
        self._scan_bare_uid()
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 SPOOLID=2 REGISTER=1')
        self.assertEqual(self.rt.db.spool_uid(2), UNKNOWN_TAG)
        self.assertEqual(self.rt.mmu.gate_spool_id[3], 2)

    def test_failed_write_leaves_the_gate_map_untouched(self):
        self._scan_bare_uid()
        self.rt.db.offline = True
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 SPOOLID=2 REGISTER=1')
        self.assertEqual(self.rt.mmu.gate_spool_id[3], -1,
                         'a failed remote write must not commit the local assignment')

    def test_no_recorded_uid_errors(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 SPOOLID=2 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before)
        self.assertIn('no nfc/rfid tag uid recorded', self.rt.errors[-1].lower())

    def test_missing_gate_or_spoolid_errors(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before + 1)

    def test_bypass_gate_is_rejected(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=-1 SPOOLID=2 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before)

    def test_pull_mode_errors_instead_of_silently_finding_nothing(self):
        self._scan_bare_uid()
        self.rt.mmu.p.spoolman_support = 'pull'
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=3 SPOOLID=2 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before)
        self.assertIn('pull', self.rt.errors[-1].lower())

    def test_omitted_gate_defaults_to_the_selected_gate(self):
        self._scan_bare_uid()
        self.rt.run_gcode('MMU_SELECT GATE=3')
        self.rt.run_gcode('MMU_SPOOLMAN_TAG SPOOLID=2 REGISTER=1')
        self.assertEqual(self.rt.mmu.gate_spool_id[3], 2)

    def test_gate_last_resolves_to_the_most_recently_preloaded_gate(self):
        self._scan_bare_uid()
        self.rt.mmu.last_preloaded_gate = 3
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=LAST SPOOLID=2 REGISTER=1')
        self.assertEqual(self.rt.mmu.gate_spool_id[3], 2)

    def test_gate_last_errors_when_nothing_has_been_preloaded(self):
        errors_before = len(self.rt.errors)
        self.rt.run_gcode('MMU_SPOOLMAN_TAG GATE=LAST SPOOLID=2 REGISTER=1')
        self.assertGreater(len(self.rt.errors), errors_before)
        self.assertIn('preloaded', self.rt.errors[-1].lower())


if __name__ == '__main__':
    unittest.main()
