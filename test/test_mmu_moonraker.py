# Happy Hare test harness - milestone B1: the Moonraker half.
#
# components/mmu_server.py is a Moonraker component - async, asyncio-based, and
# entirely separate from Klipper. It needs no Klipper to test, which is why this runs
# independently of the bootup milestones.
#
# Everything here had never executed before: the whole NFC -> Spoolman resolution and
# auto-create path is session-1..5 work that the dev handoffs describe as
# "static-verified only (ast.parse) - nothing run on hardware".
#
# Two traps this file is built around:
#   - MmuServer cannot even be CONSTRUCTED outside Moonraker
#     (setup_placeholder_processor -> from .file_manager import file_manager,
#     mmu_server.py:174,1691). test/hh/moonraker.py stubs that.
#   - almost every method silently no-ops unless _mmu_backend_enabled() is true
#     (:293-296), which needs klippy_apis to report an enabled 'mmu' object. Without
#     that, these tests would pass having exercised nothing.
#
#   ./venv/bin/python -m unittest test.test_mmu_moonraker
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import spoolman
from test.hh.moonraker import harness

logging.getLogger().setLevel(logging.CRITICAL)

KNOWN_UID = '04A1B2C3'
UNKNOWN_UID = 'DEADBEEF'
TAG_METADATA = {
    'material': 'PETG',
    'brand': 'Overture',
    'color_hex': '00FF00',
    'material_detail': 'PETG Basic',
    'min_temp': 230,
    'max_temp': 250,
}


class MoonrakerTestCase(unittest.TestCase):
    SPOOLS = ()
    KWARGS = {}

    def setUp(self):
        self.hh = harness(spools=list(self.SPOOLS), **self.KWARGS)
        self.hh.component_init()

    def tearDown(self):
        self.hh.close()

    def last_gcode(self, startswith='MMU_GATE_MAP'):
        matching = self.hh.gcode(startswith)
        return matching[-1] if matching else None


class TestInitialisation(MoonrakerTestCase):

    def test_all_remote_methods_registered(self):
        """
        The full Klipper-facing surface. Klipper calls these by name via
        webhooks.call_remote_method, so a rename here silently breaks the contract.
        """
        expected = {
            'spoolman_refresh', 'spoolman_get_filaments', 'spoolman_push_gate_map',
            'spoolman_pull_gate_map', 'spoolman_clear_spools_for_printer',
            'spoolman_set_spool_gate', 'spoolman_unset_spool_gate',
            'spoolman_get_spool_info', 'spoolman_display_spool_location',
            'spoolman_get_spool_by_uid', 'spoolman_set_spool_uid',
            'moonraker_push_lane_data', 'moonraker_cleanup_lane_data',
        }
        self.assertEqual(set(self.hh.server.remote_methods), expected)

    def test_set_active_spool_is_not_ours(self):
        """
        Klipper calls spoolman_set_active_spool (extras/mmu/mmu_controller.py:3030)
        but mmu_server deliberately does NOT register it - Moonraker's own built-in
        spoolman component serves it. Documented so nobody "fixes" it by adding one.
        """
        self.assertNotIn('spoolman_set_active_spool', self.hh.server.remote_methods)

    def test_version_negotiated_and_extras_confirmed(self):
        self.assertEqual(self.hh.mmu_server.spoolman_version, (0, 18, 1))
        self.assertTrue(self.hh.mmu_server.spoolman_has_extras)

    def test_extra_fields_created_when_absent(self):
        """
        A virgin Spoolman needs printer_name / mmu_gate / rfid adding
        (mmu_server.py:218-224). The `rfid` field is the authoritative UID<->spool
        mapping for the whole NFC feature.
        """
        hh = harness(with_extra_fields=False)
        try:
            hh.component_init()
            self.assertEqual(set(hh.db.fields['spool']),
                             {spoolman.FIELD_PRINTER, spoolman.FIELD_GATE,
                              spoolman.FIELD_RFID})
            self.assertTrue(hh.mmu_server.spoolman_has_extras)
        finally:
            hh.close()

    def test_too_old_spoolman_is_refused(self):
        """MIN_SM_VER is (0,18,1) - an older db must not be used."""
        hh = harness(spoolman_version='0.17.0')
        try:
            hh.component_init()
            self.assertFalse(hh.mmu_server.spoolman_has_extras)
        finally:
            hh.close()

    def test_unreachable_spoolman_does_not_raise(self):
        hh = harness()
        try:
            hh.db.offline = True
            hh.component_init()
            self.assertFalse(hh.mmu_server.spoolman_has_extras)
        finally:
            hh.close()


class TestFieldKeysMatchProduction(MoonrakerTestCase):
    """
    The harness store keys its `extra` dict with literals. If mmu_server ever renames
    a field, the store would go silently invisible to HH rather than failing - so pin
    them together. (The gate key is 'mmu_gate_map', not the 'mmu_gate' you would
    guess from the constant name MMU_GATE_FIELD.)
    """

    def test_keys_are_in_step(self):
        mod = self.hh.mmu_server_mod
        self.assertEqual(spoolman.FIELD_PRINTER, mod.MMU_NAME_FIELD)
        self.assertEqual(spoolman.FIELD_GATE, mod.MMU_GATE_FIELD)
        self.assertEqual(spoolman.FIELD_RFID, mod.MMU_RFID_FIELD)


class TestUidCache(MoonrakerTestCase):
    SPOOLS = (dict(uid='04:A1:B2:C3', material='PLA', vendor='Prusament'),)

    def test_uid_is_normalised_on_load(self):
        """
        _normalise_uid strips quotes/separators and uppercases (mmu_server.py:376-385),
        so a tag written '04:a1:b2:c3' resolves the same as '04A1B2C3'.
        """
        self.assertEqual(self.hh.mmu_server.uid_to_spool_id, {KNOWN_UID: 1})

    def test_lookup_is_separator_and_case_insensitive(self):
        for variant in ('04a1b2c3', '04:A1:B2:C3', '04-a1-b2-c3', '"04A1B2C3"'):
            with self.subTest(variant=variant):
                self.hh.call_remote('spoolman_get_spool_by_uid', uid=variant,
                                    gate=None, silent=True)
                self.assertEqual(self.last_gcode(),
                                 'MMU_GATE_MAP NEXT_SPOOLID=1 QUIET=1')


class TestSharedReaderLookup(MoonrakerTestCase):
    SPOOLS = (dict(uid=KNOWN_UID, material='PLA', vendor='Prusament'),)

    def test_known_tag_resolves_to_pending_spool_id(self):
        result = self.hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID,
                                     gate=None, silent=True)
        self.assertTrue(result)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=1 QUIET=1')

    def test_unknown_tag_sends_definitive_minus_two(self):
        """-2 means 'unknown tag': release Klipper's guard, do not re-read."""
        result = self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                                     gate=None, silent=True)
        self.assertFalse(result)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')

    def test_spoolman_outage_sends_recoverable_minus_one(self):
        """-1 means 'try again': Klipper releases the guard AND allows a re-read."""
        self.hh.db.offline = True
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-1 QUIET=1')

    def test_missing_uid_is_rejected(self):
        result = self.hh.call_remote('spoolman_get_spool_by_uid', uid=None,
                                     gate=None, silent=True)
        self.assertFalse(result)
        self.assertIsNone(self.last_gcode())


class TestPerGateLookup(MoonrakerTestCase):
    SPOOLS = (dict(uid=KNOWN_UID, material='PLA'),)

    def test_known_tag_assigns_the_gate_directly(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID, gate=2,
                            silent=True)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP GATE=2 SPOOLID=1 QUIET=1')

    def test_failure_reports_back_per_gate(self):
        """
        Session 5 added this: previously a per-gate lookup failure sent NOTHING, so
        the gate's LEDs and console never learned about it. Now all three failure
        sites emit GATE=x LOOKUP=-1|-2 and the gate map is left untouched.
        """
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=3,
                            silent=True)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP GATE=3 LOOKUP=-2 QUIET=1')

    def test_per_gate_outage_is_recoverable(self):
        self.hh.db.offline = True
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=1,
                            silent=True)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP GATE=1 LOOKUP=-1 QUIET=1')


class TestMissCache(MoonrakerTestCase):
    """
    NFC_UID_MISS_TTL is 10s (mmu_server.py:88). The cache exists to spare the
    Spoolman fetch on rapid re-scans of an unregistered tag - NOT to suppress the
    callback. Session 5 fixed exactly that: a cached miss must still send the
    terminal -2, or Klipper's in-flight guard orphans until its own timeout and a
    re-scan of the same unknown tag stalls.
    """
    SPOOLS = (dict(uid=KNOWN_UID, material='PLA'),)

    def _spool_list_fetches(self):
        return len([1 for m, u in self.hh.http.requests
                    if m == 'GET' and u.endswith('/v1/spool')])

    def test_cached_miss_still_sends_terminal_result(self):
        for i in range(3):
            self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                                gate=None, silent=True)
            self.assertEqual(self.last_gcode(),
                             'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1',
                             'scan %d got no terminal callback' % (i + 1))

    def test_cache_spares_the_fetch(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        after_first = self._spool_list_fetches()
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        self.assertEqual(self._spool_list_fetches(), after_first,
                         'a cached miss should not re-fetch the spool list')

    def test_cache_expires_after_ttl(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        before = self._spool_list_fetches()
        self.hh.advance(11.0)       # virtual clock, not a sleep
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        self.assertGreater(self._spool_list_fetches(), before)

    def test_report_only_bypasses_the_cache(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True)
        before = self._spool_list_fetches()
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                            gate=None, silent=True, report_only=True)
        self.assertGreater(self._spool_list_fetches(), before,
                          'an explicit REGISTER request must get a live answer')


class TestAutoCreate(MoonrakerTestCase):
    """
    Auto-create turns an unknown tag into a positive resolution. Gated on
    save=True + usable metadata['material'] (mmu_server.py:976).
    """

    def test_creates_vendor_filament_and_spool(self):
        result = self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                                     gate=None, metadata=TAG_METADATA, save=True,
                                     silent=True)
        self.assertTrue(result)
        self.assertEqual(len(self.hh.db.created_spools), 1)
        sid = self.hh.db.created_spools[0]
        self.assertEqual(self.last_gcode(),
                         'MMU_GATE_MAP NEXT_SPOOLID=%d CREATED=1 QUIET=1' % sid)

    def test_created_flag_tells_klipper_to_log_it(self):
        """CREATED=1 is what makes MMU_GATE_MAP log 'created new Spoolman spool N'."""
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=True)
        self.assertIn('CREATED=1', self.last_gcode())

    def test_uid_is_registered_against_the_new_spool(self):
        """Without this the next scan of the same tag would create ANOTHER spool."""
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=True)
        sid = self.hh.db.created_spools[0]
        self.assertEqual(self.hh.db.spool_uid(sid), UNKNOWN_UID)

    def test_rescanning_resolves_instead_of_recreating(self):
        for _ in range(2):
            self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID,
                                gate=None, metadata=TAG_METADATA, save=True,
                                silent=True)
        self.assertEqual(len(self.hh.db.created_spools), 1,
                         'second scan must resolve the existing spool')
        sid = self.hh.db.created_spools[0]
        self.assertEqual(self.last_gcode(),
                         'MMU_GATE_MAP NEXT_SPOOLID=%d QUIET=1' % sid,
                         'second scan must NOT carry CREATED=1')

    def test_temperature_is_the_median_of_min_and_max(self):
        """
        A session-1 decision: settings_extruder_temp is the median of the tag's
        min/max, not either endpoint. 230/250 -> 240.
        """
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=True)
        filament = self.hh.db.spools[self.hh.db.created_spools[0]]['filament']
        self.assertEqual(filament['settings_extruder_temp'], 240)

    def test_vendor_is_a_relation_not_part_of_the_name(self):
        """
        Session-1 "option (a)": the filament NAME is the SpoolmanDB name or
        material_detail; the brand lives only in the vendor relation.
        """
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=True)
        filament = self.hh.db.spools[self.hh.db.created_spools[0]]['filament']
        self.assertEqual(filament['vendor']['name'], 'Overture')
        self.assertEqual(filament['name'], 'PETG Basic')
        self.assertNotIn('Overture', filament['name'])

    def test_density_comes_from_spoolmandb(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=True)
        filament = self.hh.db.spools[self.hh.db.created_spools[0]]['filament']
        self.assertEqual(filament['density'], 1.27)     # PETG

    def test_density_falls_back_when_spoolmandb_offline(self):
        """
        DENSITY_FALLBACK must keep auto-create working with no internet
        (mmu_server.py:98-103). The two donkie.github.io URLs have no disable flag,
        so this path matters for anyone behind a firewall.
        """
        hh = harness(spoolmandb=False)
        try:
            hh.component_init()
            hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                           metadata=TAG_METADATA, save=True, silent=True)
            self.assertEqual(len(hh.db.created_spools), 1)
            filament = hh.db.spools[hh.db.created_spools[0]]['filament']
            self.assertEqual(filament['density'], 1.27)   # PETG in DENSITY_FALLBACK
        finally:
            hh.close()

    def test_no_autocreate_without_save(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=False, silent=True)
        self.assertEqual(self.hh.db.created_spools, [])
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')

    def test_no_autocreate_without_material(self):
        """Material is the minimum usable payload - a UID-only tag can't be created."""
        metadata = dict(TAG_METADATA, material='')
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=metadata, save=True, silent=True)
        self.assertEqual(self.hh.db.created_spools, [])

    def test_per_gate_autocreate_assigns_the_gate(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=1,
                            metadata=TAG_METADATA, save=True, silent=True)
        sid = self.hh.db.created_spools[0]
        self.assertEqual(self.last_gcode(),
                         'MMU_GATE_MAP GATE=1 SPOOLID=%d CREATED=1 QUIET=1' % sid)


class TestReportOnly(MoonrakerTestCase):
    """
    MMU_NFC REGISTER=1 on a shared reader. Resolves/auto-creates and updates the
    caches, but sends NO callback: no pending spool_id, no gate map change, no guard
    interplay - console output only.
    """
    SPOOLS = (dict(uid=KNOWN_UID, material='PLA'),)

    def test_no_gate_map_callback(self):
        result = self.hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID,
                                     gate=None, silent=False, report_only=True)
        self.assertTrue(result)
        self.assertEqual(self.hh.gcode('MMU_GATE_MAP'), [])

    def test_reports_to_the_console(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID, gate=None,
                            silent=False, report_only=True)
        logs = self.hh.gcode('MMU_LOG')
        self.assertTrue(logs)
        self.assertIn(KNOWN_UID, logs[-1])

    def test_report_only_can_still_autocreate(self):
        self.hh.call_remote('spoolman_get_spool_by_uid', uid=UNKNOWN_UID, gate=None,
                            metadata=TAG_METADATA, save=True, silent=False,
                            report_only=True)
        self.assertEqual(len(self.hh.db.created_spools), 1)
        self.assertEqual(self.hh.gcode('MMU_GATE_MAP'), [])


class TestBackendGate(unittest.TestCase):
    """
    The trap that would make every test above vacuous. If klippy_apis does not report
    an enabled 'mmu' object, _mmu_backend_enabled() is False and every
    MMU_GATE_MAP-emitting branch silently does nothing.
    """

    def test_disabled_backend_emits_no_gate_map(self):
        hh = harness(spools=[dict(uid=KNOWN_UID, material='PLA')], mmu_enabled=False)
        try:
            hh.component_init()
            hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID, gate=None,
                           silent=True)
            self.assertEqual(hh.gcode('MMU_GATE_MAP'), [],
                             'a disabled backend must not receive callbacks')
        finally:
            hh.close()

    def test_enabled_backend_does_emit(self):
        hh = harness(spools=[dict(uid=KNOWN_UID, material='PLA')], mmu_enabled=True)
        try:
            hh.component_init()
            hh.call_remote('spoolman_get_spool_by_uid', uid=KNOWN_UID, gate=None,
                           silent=True)
            self.assertTrue(hh.gcode('MMU_GATE_MAP'))
        finally:
            hh.close()


class TestGateAssignment(MoonrakerTestCase):
    SPOOLS = (dict(uid=KNOWN_UID, material='PLA'),
              dict(material='ABS', vendor='Polymaker'))

    def test_set_spool_gate_records_printer_and_gate(self):
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=2,
                            sync=False, silent=True)
        self.assertEqual(self.hh.db.spool_gate(1), 2)
        self.assertEqual(self.hh.db.spool_printer(1), 'testprinter')

    def test_unset_spool_gate_clears_it(self):
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=2,
                            sync=False, silent=True)
        self.hh.call_remote('spoolman_unset_spool_gate', spool_id=1, gate=None,
                            sync=False, silent=True)
        self.assertEqual(self.hh.db.spool_gate(1), -1)

    def test_set_spool_gate_emits_an_event(self):
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=0,
                            sync=False, silent=True)
        self.assertTrue(self.hh.server.events_named('spoolman:set_spool_gate'))

    def test_unset_rejects_both_spool_id_and_gate(self):
        """
        spool_id XOR gate - supplying both is ambiguous and HH refuses it
        (mmu_server.py:840-842). Caught by getting this wrong while writing the test
        above.
        """
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=2,
                            sync=False, silent=True)
        result = self.hh.call_remote('spoolman_unset_spool_gate', spool_id=1, gate=2,
                                     sync=False, silent=True)
        self.assertFalse(result)
        self.assertEqual(self.hh.db.spool_gate(1), 2, 'must be left untouched')

    def test_unset_by_gate_clears_whichever_spool_is_there(self):
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=3,
                            sync=False, silent=True)
        self.hh.call_remote('spoolman_unset_spool_gate', spool_id=None, gate=3,
                            sync=False, silent=True)
        self.assertEqual(self.hh.db.spool_gate(1), -1)

    def test_set_spool_uid_registers_a_tag(self):
        """
        Registered but, per its own docstring, not yet wired to any Klipper command
        on this branch - so this is the only exercise it gets.
        """
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=2, uid='11:22:33:44',
                            silent=True)
        self.assertEqual(self.hh.db.spool_uid(2), '11223344')


if __name__ == '__main__':
    unittest.main()
