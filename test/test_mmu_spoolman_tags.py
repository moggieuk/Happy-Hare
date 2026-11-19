# Happy Hare MMU Software
#
# Spoolman >= 0.27 native tag links and the MIGRATE command. Below 0.27 no /tag
# endpoint may be called; startup never migrates; reads cover both stores.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import spoolman
from test.hh.moonraker import SPOOLMAN_URL, harness

logging.getLogger().setLevel(logging.CRITICAL)

LEGACY_UID = '04A1B2C3'
NATIVE_UID = '0A0B0C0D'
NEW_UID = '11223344'
TAG_METADATA = {
    'material': 'PETG',
    'brand': 'Overture',
    'color_hex': '00FF00',
    'material_detail': 'PETG Basic',
    'min_temp': 230,
    'max_temp': 250,
    'tag_format': 'openspool',
}


PROBE = ('GET', SPOOLMAN_URL + '/v1/tag/reader')


class TagTestCase(unittest.TestCase):
    PROBE_ONLY = [PROBE]
    VERSION = spoolman.TAG_API_VERSION
    SPOOLS = (dict(uid=LEGACY_UID, material='PLA'),
              dict(material='ABS', vendor='Polymaker'))

    def setUp(self):
        self.hh = harness(spools=list(self.SPOOLS), spoolman_version=self.VERSION)
        self.db = self.hh.db
        self.hh.component_init()

    def tearDown(self):
        self.hh.close()

    def make_legacy(self):
        """Put the database back to the pre-migration state (startup migrates it)."""
        self.db.tag_links.clear()
        self.db.tag_formats.clear()
        self.hh.mmu_server.native_uids.clear() # Or the missing links look like unlinks in the Spoolman UI
        self.hh.call_remote('spoolman_refresh', silent=True)
        self.hh.klippy.drain()

    def tag_requests(self):
        return [(m, u) for m, u, _ in self.db.requests if '/tag' in u]

    def lookup(self, uid, **kwargs):
        kwargs.setdefault('gate', None)
        kwargs.setdefault('silent', True)
        return self.hh.call_remote('spoolman_get_spool_by_uid', uid=uid, **kwargs)

    def last_gcode(self):
        matching = self.hh.gcode('MMU_GATE_MAP')
        return matching[-1] if matching else None


class TestLegacySpoolmanUntouched(TagTestCase):
    """Below 0.27 the tag API doesn't exist - only the startup probe may touch /tag."""
    VERSION = '0.26.9'

    def test_no_tag_endpoint_is_called(self):
        self.lookup(LEGACY_UID)
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=2, uid=NEW_UID, silent=True)
        self.lookup(NEW_UID)
        self.assertEqual(self.tag_requests(), self.PROBE_ONLY)
        self.assertEqual(self.db.spool_uid(2), NEW_UID)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_migrate_is_refused(self):
        self.assertFalse(self.hh.call_remote('spoolman_migrate_tags', direction='native', silent=True))
        self.assertEqual(self.tag_requests(), self.PROBE_ONLY)


class TestCapabilityProbe(TagTestCase):
    """Native tags are detected by probing GET /v1/tag/reader, not by version number."""

    def init(self, version, tag_api=None, fail=None):
        hh = harness(spools=list(self.SPOOLS), spoolman_version=version)
        hh.db.tag_api = tag_api
        if fail is not None:
            real = hh.db.handle
            def handle(method, url, body=None):
                if url.endswith('/v1/tag/reader'):
                    hh.db.requests.append((method, url, body))
                    return spoolman.Response(fail, error='injected')
                return real(method, url, body)
            hh.db.handle = handle
        hh.component_init()
        self.addCleanup(hh.close)
        return hh

    def test_probe_is_sent(self):
        self.assertIn(PROBE, self.tag_requests())

    def test_dev_build_with_old_number_is_native(self):
        """A fork or dev build reporting an older version but serving the API."""
        self.assertTrue(self.init('0.26.0', tag_api=True).mmu_server.spoolman_has_tags)

    def test_new_number_without_api_is_legacy(self):
        hh = self.init('0.27.0', tag_api=False)
        self.assertFalse(hh.mmu_server.spoolman_has_tags)
        self.assertTrue(hh.mmu_server.spoolman_has_extras, 'integration still on')

    def test_probe_error_is_legacy_and_retried(self):
        hh = self.init('0.27.0', fail=500)
        self.assertFalse(hh.mmu_server.spoolman_has_tags)
        self.assertTrue(hh.mmu_server.spoolman_has_extras)
        hh.db.handle = spoolman.InMemorySpoolman.handle.__get__(hh.db)   # server recovers
        hh.mmu_server.spoolman_has_extras = False                       # force a re-init
        hh.call_remote('spoolman_refresh', silent=True)
        self.assertTrue(hh.mmu_server.spoolman_has_tags, 'probed again on the next init')

    def test_old_spoolman_without_extras_is_not_probed(self):
        hh = self.init('0.17.0')
        self.assertEqual([u for _, u, _ in hh.db.requests if '/tag' in u], [])


class TestStartupMigration(TagTestCase):
    """Legacy extra-field UIDs are linked natively at startup, with progress on the console."""

    def test_legacy_uid_linked_at_startup(self):
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.hh.mmu_server.legacy_only_uid_count, 0)

    def test_extra_field_is_kept_for_older_clients(self):
        self.assertEqual(self.db.spool_uid(1), LEGACY_UID)

    def test_progress_on_the_console(self):
        msgs = ' '.join(self.hh.gcode('MMU_LOG'))
        self.assertIn('migrating 1 tag(s) on 1 spool(s) to native Spoolman tags', msgs)
        self.assertIn('[1/1] tag %s on spool 1 migrated to a native Spoolman tag' % LEGACY_UID, msgs)

    def test_second_startup_does_nothing(self):
        before = len(self.tag_requests())
        self.hh.mmu_server.spoolman_has_extras = False  # force a re-init
        self.hh.call_remote('spoolman_refresh', silent=True)
        self.hh.drain_tasks()
        posts = [r for r in self.tag_requests()[before:] if r[0] == 'POST']
        self.assertEqual(posts, [], 'already-migrated tags must not be re-posted')

    def test_conflict_is_reported_not_fatal(self):
        hh = harness(spools=[dict(uid=LEGACY_UID, material='PLA')], spoolman_version=self.VERSION)
        try:
            fid = hh.db.add_filament(name='Vendor tag product')
            hh.db.link_tag(LEGACY_UID, filament_id=fid)
            hh.component_init()
            self.assertTrue(hh.mmu_server.spoolman_has_tags)
            self.assertEqual(hh.db.tag_links[LEGACY_UID], ('filament', fid))
            self.assertIn('not migrated - already linked to filament %d' % fid, ' '.join(hh.gcode('MMU_LOG')))
        finally:
            hh.close()

    def test_nothing_to_migrate_says_nothing(self):
        hh = harness(spools=[dict(material='PLA')], spoolman_version=self.VERSION)
        try:
            hh.component_init()
            self.assertFalse([m for m in hh.gcode('MMU_LOG') if 'migrat' in m])
        finally:
            hh.close()

    def test_klipper_never_ready_logs_only_and_never_pauses(self):
        """Without a ready Klipper the console fallback (M118) would pause the print on an error."""
        self.make_legacy()
        del self.hh.mmu_server.mmu_backend_present
        self.hh.klippy.klippy_state = 'startup'
        self.hh.run(self.hh.mmu_server._auto_migrate_tags(wait=0, attempts=2))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID], 'still migrated')
        self.assertEqual(self.hh.klippy.drain(), [], 'nothing sent to Klipper')
        self.assertEqual(self.hh.klippy.pause_calls, 0)

    def test_waits_until_klipper_is_ready_not_just_connected(self):
        """Seen on a real printer: queries work while Klipper is starting, gcode doesn't."""
        self.make_legacy()
        server, klippy = self.hh.mmu_server, self.hh.klippy
        del server.mmu_backend_present
        klippy.klippy_state = 'startup'
        real_query, real_gcode, polls = klippy.query_objects, klippy.run_gcode, []
        async def query(objects, default=None):
            if 'webhooks' in objects:
                polls.append(1)
                if len(polls) >= 3:
                    klippy.klippy_state = 'ready'
            return await real_query(objects, default)
        async def gcode(script, default=None):
            if klippy.klippy_state != 'ready':
                raise RuntimeError('Printer is not ready')
            return await real_gcode(script, default)
        klippy.query_objects, klippy.run_gcode = query, gcode
        self.hh.run(server._auto_migrate_tags(wait=0, attempts=5))
        self.assertEqual(len(polls), 3)
        self.assertIn('migrated to a native Spoolman tag', ' '.join(self.hh.gcode('MMU_LOG')))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])

    def test_console_failure_mid_run_does_not_abort(self):
        """If Klipper drops while reporting, the migration still finishes (log only)."""
        self.make_legacy()
        self.db.spools[2]['extra'][spoolman.FIELD_RFID] = '"%s"' % NATIVE_UID
        self.hh.call_remote('spoolman_refresh', silent=True)
        klippy, sent = self.hh.klippy, []
        real_gcode = klippy.run_gcode
        async def flaky(script, default=None):
            sent.append(script)
            if len(sent) >= 2:
                raise RuntimeError('Printer is not ready')
            return await real_gcode(script, default)
        klippy.run_gcode = flaky
        self.hh.run(self.hh.mmu_server._auto_migrate_tags(wait=0, attempts=1))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.db.spool_native_uids(2), [NATIVE_UID])
        self.assertEqual(klippy.pause_calls, 0)

    def test_quiet_migration_never_touches_the_console(self):
        self.make_legacy()
        before = len(self.hh.gcode())
        self.hh.call_remote('spoolman_migrate_tags', direction='native', silent=True)
        self.assertEqual(self.hh.gcode()[before:], [], 'not even the summary')


class TestFreshNativeInstall(TagTestCase):
    """Spoolman >= 0.27 without rfid_tag: don't create it, don't fail without it."""
    SPOOLS = (dict(material='PLA'), dict(material='ABS', vendor='Polymaker'))

    def setUp(self):
        self.hh = harness(spools=list(self.SPOOLS), spoolman_version=self.VERSION,
                          with_extra_fields=False)
        self.db = self.hh.db
        self.hh.component_init()

    def test_rfid_field_not_created_but_integration_enabled(self):
        self.assertNotIn(spoolman.FIELD_RFID, self.db.fields['spool'])
        self.assertIn(spoolman.FIELD_GATE, self.db.fields['spool'])
        self.assertTrue(self.hh.mmu_server.spoolman_has_extras)
        self.assertTrue(self.hh.mmu_server.spoolman_has_tags)

    def test_set_uid_links_natively_without_touching_extra(self):
        self.assertTrue(self.hh.call_remote('spoolman_set_spool_uid', spool_id=2,
                                            uid=NEW_UID, silent=True))
        self.assertEqual(self.db.spool_native_uids(2), [NEW_UID])
        self.assertNotIn(spoolman.FIELD_RFID, self.db.spools[2]['extra'])
        self.assertTrue(self.lookup(NEW_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_move_between_spools_without_extra_field(self):
        self.db.link_tag(NATIVE_UID, spool_id=1)
        self.hh.call_remote('spoolman_refresh', silent=True)
        self.assertTrue(self.hh.call_remote('spoolman_set_spool_uid', spool_id=2,
                                            uid=NATIVE_UID, silent=True))
        self.assertEqual(self.db.tag_links[NATIVE_UID], ('spool', 2))

    def test_auto_create_works_without_extra_field(self):
        self.assertTrue(self.lookup('DEADBEEF', metadata=TAG_METADATA, save=True))
        sid = self.db.created_spools[0]
        self.assertEqual(self.db.spool_native_uids(sid), ['DEADBEEF'])

    def test_migrate_extra_creates_the_field_on_demand(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.hh.call_remote('spoolman_migrate_tags', direction='extra', silent=True))
        self.assertIn(spoolman.FIELD_RFID, self.db.fields['spool'])
        self.assertEqual(self.db.spool_uid(2), NATIVE_UID)
        self.assertEqual(self.db.tag_links, {})


class TestNativeLookup(TagTestCase):

    def test_native_only_tag_resolves(self):
        """A tag linked in the Spoolman UI (no extra field) must resolve."""
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.lookup(NATIVE_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_extra_only_tag_still_resolves(self):
        """E.g. written by an older Happy Hare client."""
        self.db.spools[2]['extra'][spoolman.FIELD_RFID] = '"%s"' % NEW_UID
        self.assertTrue(self.lookup(NEW_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')


class TestNativeSetSpoolUid(TagTestCase):

    def set_uid(self, spool_id, uid, **kwargs):
        return self.hh.call_remote('spoolman_set_spool_uid', spool_id=spool_id, uid=uid,
                                   silent=True, **kwargs)

    def test_links_natively_and_mirrors_extra_field(self):
        self.assertTrue(self.set_uid(2, '11:22:33:44'))
        self.assertEqual(self.db.spool_native_uids(2), [NEW_UID])
        self.assertEqual(self.db.spool_uid(2), NEW_UID)

    def test_replace_unlinks_the_old_native_tag(self):
        self.db.link_tag(LEGACY_UID, spool_id=1)
        self.assertTrue(self.set_uid(1, NEW_UID))
        self.assertEqual(self.db.spool_native_uids(1), [NEW_UID])
        self.assertNotIn(LEGACY_UID, self.db.tag_links)
        self.assertEqual(self.db.spool_uid(1), NEW_UID)

    def test_append_keeps_existing_tags(self):
        self.assertTrue(self.set_uid(1, NEW_UID, append=True))
        self.assertEqual(sorted(self.db.spool_native_uids(1)), sorted([LEGACY_UID, NEW_UID]))
        self.assertEqual(self.db.spool_uid(1), '%s,%s' % (LEGACY_UID, NEW_UID))

    def test_blank_clears_both_stores(self):
        self.assertTrue(self.set_uid(1, ''))
        self.assertEqual(self.db.spool_native_uids(1), [])
        self.assertEqual(self.db.spool_uid(1), '')
        self.assertIsNone(self.hh.mmu_server.uid_to_spool_id.get(LEGACY_UID))

    def test_tag_on_another_spool_is_moved(self):
        """409 with spool_id -> unlink from the holder and relink here."""
        self.assertTrue(self.set_uid(2, LEGACY_UID))
        self.assertEqual(self.db.tag_links[LEGACY_UID], ('spool', 2))
        self.assertEqual(self.db.spool_uid(1), '', 'stripped from the old spool extra field too')
        self.assertTrue(self.lookup(LEGACY_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_tag_held_by_a_filament_is_refused(self):
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.assertFalse(self.set_uid(2, NATIVE_UID))
        self.assertEqual(self.db.tag_links[NATIVE_UID], ('filament', fid))
        self.assertEqual(self.db.spool_uid(2), '', 'nothing written when the native link fails')


class TestNativeAutoCreate(TagTestCase):

    def test_created_spool_gets_a_native_tag_with_format(self):
        self.assertTrue(self.lookup('DEADBEEF', metadata=TAG_METADATA, save=True))
        sid = self.db.created_spools[0]
        self.assertEqual(self.db.spool_native_uids(sid), ['DEADBEEF'])
        self.assertEqual(self.db.tag_formats['DEADBEEF'], 'openspool')
        self.assertEqual(self.db.spool_uid(sid), 'DEADBEEF', 'extra field still written')


class TestMigrateCommand(TagTestCase):

    def setUp(self):
        super().setUp()
        self.make_legacy()
        self.log_start = len(self.hh.gcode('MMU_LOG'))

    def migrate(self, direction, **kwargs):
        return self.hh.call_remote('spoolman_migrate_tags', direction=direction,
                                   silent=True, **kwargs)

    def console(self):
        """NFC console messages sent since setUp (startup's own migration excluded)."""
        return [m for m in self.hh.gcode('MMU_LOG')[self.log_start:] if 'NFC:' in m]

    def test_progress_is_reported_tag_by_tag(self):
        """The console shows a start line, one line per tag, then the summary."""
        self.db.spools[2]['extra'][spoolman.FIELD_RFID] = '"%s"' % NATIVE_UID
        self.hh.call_remote('spoolman_migrate_tags', direction='native', silent=False)
        msgs = self.console()
        self.assertIn('migrating 2 tag(s) on 2 spool(s) to native Spoolman tags', msgs[0])
        self.assertIn('[1/2] tag %s on spool 1 migrated to a native Spoolman tag' % LEGACY_UID, msgs[1])
        self.assertIn('[2/2] tag %s on spool 2 migrated to a native Spoolman tag' % NATIVE_UID, msgs[2])
        self.assertIn('2 tag(s) moved on 2 spool(s)', msgs[3])

    def test_progress_back_to_extra_field(self):
        self.migrate('native')
        self.hh.klippy.drain()
        before = len(self.console())
        self.hh.call_remote('spoolman_migrate_tags', direction='extra', silent=False)
        msgs = self.console()[before:]
        self.assertIn('migrating 1 tag(s) on 1 spool(s) to the extra field', msgs[0])
        self.assertIn('[1/1] tag %s on spool 1 migrated to the extra field' % LEGACY_UID, msgs[1])

    def test_dry_run_lists_what_would_move(self):
        self.hh.call_remote('spoolman_migrate_tags', direction='native', dry_run=True, silent=False)
        msgs = self.console()
        self.assertIn('checking 1 tag(s)', msgs[0])
        self.assertIn('[1/1] tag %s on spool 1 would be migrated' % LEGACY_UID, msgs[1])
        self.assertEqual(self.db.tag_links, {})

    def test_conflict_is_reported_as_it_happens(self):
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(LEGACY_UID, filament_id=fid)
        self.hh.call_remote('spoolman_migrate_tags', direction='native', silent=False)
        self.assertTrue(any('[1/1] tag %s on spool 1 not migrated - already linked to filament %d' % (LEGACY_UID, fid) in m
                            for m in self.console()))

    def test_quiet_shows_nothing(self):
        self.migrate('native')
        self.assertEqual(self.console(), [])

    def test_nothing_to_do_has_no_start_line(self):
        self.migrate('native')
        self.hh.call_remote('spoolman_migrate_tags', direction='native', silent=False)
        msgs = self.console()
        self.assertFalse([m for m in msgs if 'migrating' in m])
        self.assertIn('0 tag(s) moved on 0 spool(s)', msgs[-1])

    def test_native_links_legacy_uids_and_keeps_extra(self):
        self.assertTrue(self.migrate('native'))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.db.spool_uid(1), LEGACY_UID)
        self.assertEqual(self.hh.mmu_server.legacy_only_uid_count, 0)

    def test_native_is_idempotent(self):
        self.assertTrue(self.migrate('native'))
        before = len(self.tag_requests())
        self.assertTrue(self.migrate('native'))
        posts = [r for r in self.tag_requests()[before:] if r[0] == 'POST']
        self.assertEqual(posts, [], 'an already-linked tag must not be re-posted')

    def test_native_conflict_is_reported_not_fatal(self):
        """A UID in the extra field that is natively held by a filament is left alone."""
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(LEGACY_UID, filament_id=fid)
        self.migrate('native')
        self.assertEqual(self.db.tag_links[LEGACY_UID], ('filament', fid))
        self.assertEqual(self.db.spool_native_uids(1), [])

    def test_extra_moves_native_tags_back_to_the_extra_field(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.migrate('extra'))
        self.assertEqual(self.db.tag_links, {})
        self.assertEqual(self.db.spool_uid(1), LEGACY_UID)
        self.assertEqual(self.db.spool_uid(2), NATIVE_UID)
        # Still resolvable while in the legacy state
        self.assertTrue(self.lookup(NATIVE_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_round_trip(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.migrate('extra'))
        self.assertTrue(self.migrate('native'))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.db.spool_native_uids(2), [NATIVE_UID])

    def test_dry_run_changes_nothing(self):
        before = dict(self.db.tag_links)
        patched = len(self.db.patched_spools)
        self.assertTrue(self.migrate('extra', dry_run=True))
        self.assertEqual(self.db.tag_links, before)
        self.assertEqual(len(self.db.patched_spools), patched)

    def test_spool_filter_limits_migration(self):
        """Moonraker side of MIGRATE ... GATE=."""
        self.assertTrue(self.migrate('native'))
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.migrate('extra', spool_id=2))
        self.assertEqual(self.db.tag_links, {LEGACY_UID: ('spool', 1)}, 'spool 1 untouched')
        self.assertEqual(self.db.spool_uid(2), NATIVE_UID)

    def test_unknown_direction_is_refused(self):
        self.assertFalse(self.migrate('sideways'))


class TestTagScan(TagTestCase):
    """POST /tag/scan first on >= 0.27; anything else falls through to the cached lookup."""

    def requests_since(self, n):
        return [(m, u) for m, u, _ in self.db.requests[n:]]

    def test_known_tag_is_one_request(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        n = len(self.db.requests)
        self.assertTrue(self.lookup(NATIVE_UID))
        self.assertEqual([u.split('/v1/')[1] for _, u in self.requests_since(n)], ['tag/scan'])
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_per_gate_reader(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.lookup(NATIVE_UID, gate=2, metadata={'tag_format': 'openspool'})
        scan = self.db.scans[-1]
        self.assertEqual(scan['reader_id'], 'testprinter-gate2')
        self.assertEqual(scan['name'], 'Happy Hare testprinter gate 2')
        self.assertEqual(scan['format'], 'openspool')
        self.assertTrue(self.last_gcode().startswith('MMU_GATE_MAP GATE=2 SPOOLID=2'))

    def test_shared_reader(self):
        """gate=None: one reader id for the MMU, NEXT_SPOOLID callback releases Klipper's guard."""
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.lookup(NATIVE_UID)
        self.assertEqual(self.db.scans[-1]['reader_id'], 'testprinter-mmu')
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_shared_reader_unknown_tag_releases_guard(self):
        self.assertFalse(self.lookup('DEADBEEF'))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')

    def test_filament_tag_without_auto_create_is_unknown(self):
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.assertFalse(self.lookup(NATIVE_UID, metadata=TAG_METADATA, save=False))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')
        self.assertEqual(self.db.tag_links[NATIVE_UID], ('filament', fid))

    def test_filament_tag_without_auto_create_warns(self):
        """Shown even for a silent lookup: the user needs to know where the tag went."""
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.lookup(NATIVE_UID, save=False, silent=True)
        msg = ' '.join(self.hh.gcode('MMU_LOG'))
        self.assertIn('assigned to filament "Vendor tag product" (id %d)' % fid, msg)
        self.assertIn('spoolman_nfc_auto_create', msg)

    def test_filament_warning_survives_apostrophe(self):
        fid = self.db.add_filament(name="Bob's PLA")
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.lookup(NATIVE_UID, save=False)
        self.assertIn('filament "Bobs PLA"', ' '.join(self.hh.gcode('MMU_LOG')))

    def test_filament_tag_with_auto_create_moves_tag_to_new_spool(self):
        fid = self.db.add_filament(name='Vendor tag product', material='PETG')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.assertTrue(self.lookup(NATIVE_UID, save=True))   # no metadata needed
        sid = self.db.created_spools[0]
        self.assertEqual(self.db.spools[sid]['filament']['id'], fid, 'spool of the tag\'s filament')
        self.assertEqual(self.db.tag_links[NATIVE_UID], ('spool', sid))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=%d CREATED=1 QUIET=1' % sid)

    def test_filament_tag_next_scan_resolves_without_creating(self):
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        self.lookup(NATIVE_UID, gate=1, save=True)
        sid = self.db.created_spools[0]
        self.lookup(NATIVE_UID, gate=1, save=True)
        self.assertEqual(self.db.created_spools, [sid], 'one spool only')
        self.assertTrue(self.last_gcode().startswith('MMU_GATE_MAP GATE=1 SPOOLID=%d' % sid))

    def test_filament_tag_create_failure_puts_tag_back(self):
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(NATIVE_UID, filament_id=fid)
        real = self.db._create_spool
        self.db._create_spool = lambda body: spoolman.Response(500, error='injected')
        try:
            self.assertFalse(self.lookup(NATIVE_UID, save=True))
        finally:
            self.db._create_spool = real
        self.assertEqual(self.db.tag_links[NATIVE_UID], ('filament', fid))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')

    def test_hostname_is_cleaned(self):
        hh = harness(spools=list(self.SPOOLS), spoolman_version=self.VERSION, hostname='my printer!')
        try:
            hh.component_init()
            hh.call_remote('spoolman_get_spool_by_uid', uid=NATIVE_UID, gate=None, silent=True)
            self.assertEqual(hh.db.scans[-1]['reader_id'], 'my-printer--mmu')
        finally:
            hh.close()

    def test_ui_link_resolves_despite_recent_miss(self):
        self.assertFalse(self.lookup(NATIVE_UID))
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.assertTrue(self.lookup(NATIVE_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=2 QUIET=1')

    def test_scan_refreshes_cached_spool(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        self.db.spools[2]['remaining_weight'] = 321
        self.lookup(NATIVE_UID)
        self.assertEqual(self.hh.mmu_server.spool_location[2][2]['remaining_weight'], 321)

    def test_scan_error_falls_back_to_cache(self):
        self.db.fail_next = 500
        self.assertTrue(self.lookup(LEGACY_UID))
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=1 QUIET=1')

    def test_report_only_sends_nothing_to_klipper(self):
        self.db.link_tag(NATIVE_UID, spool_id=2)
        before = len(self.hh.gcode('MMU_GATE_MAP'))
        self.assertTrue(self.lookup(NATIVE_UID, report_only=True))
        self.assertEqual(len(self.hh.gcode('MMU_GATE_MAP')), before)


class TestMigrateOnResolve(TagTestCase):
    """A tag the cached lookup resolves from the extra field is linked natively."""

    def setUp(self):
        super().setUp()
        self.make_legacy()

    def test_legacy_tag_is_linked_when_scanned(self):
        self.assertEqual(self.db.tag_links, {}, 'precondition: nothing migrated at startup')
        self.assertTrue(self.lookup(LEGACY_UID))
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.hh.mmu_server.legacy_only_uid_count, 0)

    def test_migration_is_reported_on_the_console(self):
        """Shown even for a quiet lookup: it changes the user's Spoolman data."""
        self.lookup(LEGACY_UID, silent=True)
        self.assertIn('NFC: tag %s migrated to a native Spoolman tag on spool 1' % LEGACY_UID,
                      ' '.join(self.hh.gcode('MMU_LOG')))

    def test_no_message_when_already_native(self):
        self.lookup(LEGACY_UID)
        before = len(self.hh.gcode('MMU_LOG'))
        self.lookup(LEGACY_UID)
        self.assertFalse([m for m in self.hh.gcode('MMU_LOG')[before:] if 'migrated' in m])

    def test_shared_reader_legacy_tag_is_linked(self):
        self.lookup(LEGACY_UID, gate=None)
        self.assertEqual(self.db.spool_native_uids(1), [LEGACY_UID])
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=1 QUIET=1')

    def test_next_scan_is_native(self):
        self.lookup(LEGACY_UID)
        n = len(self.db.requests)
        self.lookup(LEGACY_UID)
        self.assertEqual([u.split('/v1/')[1] for _, u, _ in self.db.requests[n:]], ['tag/scan'])

    def test_link_conflict_still_resolves(self):
        """Scan failed, tag is natively on a filament: legacy result stands, nothing moved."""
        fid = self.db.add_filament(name='Vendor tag product')
        self.db.link_tag(LEGACY_UID, filament_id=fid)
        self.db.fail_next = 500
        self.assertTrue(self.lookup(LEGACY_UID))
        self.assertEqual(self.db.tag_links[LEGACY_UID], ('filament', fid))

    def test_not_linked_below_0_27(self):
        hh = harness(spools=list(self.SPOOLS), spoolman_version='0.26.9')
        try:
            hh.component_init()
            hh.call_remote('spoolman_get_spool_by_uid', uid=LEGACY_UID, gate=None, silent=True)
            self.assertEqual([(m, u) for m, u, _ in hh.db.requests if '/tag' in u], self.PROBE_ONLY)
        finally:
            hh.close()


class TestUnlinkedInSpoolmanUi(TagTestCase):
    """
    Unlinking in the Spoolman UI leaves Happy Hare's extra-field mirror behind. The
    next scan must honour the unlink, not link the tag straight back from the mirror.
    """

    def setUp(self):
        super().setUp()
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=2, uid=NATIVE_UID, silent=True)
        self.assertEqual(self.db.spool_uid(2), NATIVE_UID, 'precondition: mirror written')
        del self.db.tag_links[NATIVE_UID]     # user unlinks in the Spoolman UI

    def test_not_relinked(self):
        self.assertFalse(self.lookup(NATIVE_UID))
        self.assertNotIn(NATIVE_UID, self.db.tag_links)
        self.assertEqual(self.last_gcode(), 'MMU_GATE_MAP NEXT_SPOOLID=-2 QUIET=1')

    def test_mirror_removed(self):
        self.lookup(NATIVE_UID)
        self.assertEqual(self.db.spool_uid(2), '')

    def test_not_relinked_after_cache_rebuild(self):
        self.hh.call_remote('spoolman_refresh', silent=True)
        self.assertEqual(self.db.spool_uid(2), '')
        self.assertFalse(self.lookup(NATIVE_UID))
        self.assertNotIn(NATIVE_UID, self.db.tag_links)


if __name__ == '__main__':
    unittest.main()
