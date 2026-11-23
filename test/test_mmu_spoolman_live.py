# Happy Hare MMU Software
#
# Spoolman live updates: the websocket keeps Moonraker's cache current in every
# mode. Klipper only hears what it doesn't own - filament details in any mode,
# gate assignments only in pull mode - and never weight-only changes. Happy
# Hare's own writes come back as events and must be ignored.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import asyncio
import json
import logging
import unittest

from test.hh import spoolman
from test.hh.moonraker import harness

logging.getLogger().setLevel(logging.CRITICAL)

HOST = 'testprinter'
UID = '0A0B0C0D'


class LiveTestCase(unittest.TestCase):
    MODE = 'push'
    VERSION = spoolman.TAG_API_VERSION

    def setUp(self):
        self.hh = harness(spoolman_version=self.VERSION, spools=[
            dict(material='PLA', color_hex='FF0000', printer=HOST, gate=0),   # spool 1
            dict(material='ABS', color_hex='00FF00', printer=HOST, gate=1),   # spool 2
            dict(material='PETG', color_hex='0000FF'),                        # spool 3, unassigned
        ])
        self.db = self.hh.db
        self.server = self.hh.mmu_server
        self.hh.component_init()
        self.hh.klippy.extra_status = {'spoolman_support': self.MODE, 'gate_spool_id': [1, 2, -1, -1]}
        self.db.take_events()
        self.hh.klippy.drain()

    def tearDown(self):
        self.hh.close()

    def send(self, kind, resource, payload):
        self.hh.run(self.server._on_spoolman_event(
            {'type': kind, 'resource': resource, 'payload': json.loads(json.dumps(payload))}))

    def edit_spool(self, sid, **changes):
        """An edit made in the Spoolman UI: change the store, broadcast the event."""
        spool = self.db.spools[sid]
        for key, value in changes.items():
            if key == 'filament':
                spool['filament'] = dict(spool['filament'], **value)
            elif key == 'extra':
                spool['extra'].update(value)
            else:
                spool[key] = value
        self.send('updated', 'spool', self.db._render_spool(sid))

    def gate_map_commands(self):
        return [c for c in self.hh.klippy.drain() if c.startswith('MMU_GATE_MAP')]


class TestCacheKeptCurrent(LiveTestCase):

    def test_weight_only_change_updates_cache_but_not_klipper(self):
        """Moonraker reports usage every few seconds while printing - never forwarded."""
        self.edit_spool(1, remaining_weight=432)
        self.assertEqual(self.server.spool_location[1][2]['remaining_weight'], 432)
        self.assertEqual(self.gate_map_commands(), [])

    def test_new_tag_resolves_without_rebuild(self):
        self.db.link_tag(UID, spool_id=3)
        self.send('updated', 'spool', self.db._render_spool(3))
        self.assertEqual(self.server.uid_to_spool_id.get(UID), 3)

    def test_new_tag_clears_miss_cache(self):
        self.server.uid_miss_cache[UID] = 10 ** 9
        self.db.link_tag(UID, spool_id=3)
        self.send('updated', 'spool', self.db._render_spool(3))
        self.assertNotIn(UID, self.server.uid_miss_cache)

    def test_deleted_spool_leaves_cache(self):
        self.send('deleted', 'spool', {'id': 3})
        self.assertNotIn(3, self.server.spool_location)

    def test_added_spool_enters_cache(self):
        sid = self.db.add_spool(material='TPU')
        self.send('added', 'spool', self.db._render_spool(sid))
        self.assertEqual(self.server.spool_location[sid][2]['material'], 'TPU')

    def test_bad_event_is_ignored(self):
        self.hh.run(self.server._on_spoolman_event({'type': 'updated', 'resource': 'spool'}))
        self.hh.run(self.server._on_spoolman_event({'type': 'updated', 'resource': 'vendor', 'payload': {'id': 1}}))


class TestUnlinkedInSpoolmanUi(LiveTestCase):

    def test_mirror_removed_when_the_unlink_arrives(self):
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=3, uid=UID, silent=True)
        self.db.take_events()
        self.hh.advance(spoolman_live_window() + 1)
        del self.db.tag_links[UID]     # user unlinks in the Spoolman UI
        self.send('updated', 'spool', self.db._render_spool(3))
        self.assertEqual(self.db.spool_uid(3), '')
        self.assertNotIn(UID, self.server.uid_to_spool_id)


class TestFilamentDetails(LiveTestCase):
    """Spoolman owns filament details in every mode, so they reach Klipper in every mode but off."""

    def test_colour_change_on_a_gate_is_forwarded(self):
        self.edit_spool(1, filament={'color_hex': 'ABCDEF'})
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("0: {", cmds[0])
        self.assertIn("'color': 'abcdef'", cmds[0])
        self.assertNotIn('REPLACE=1', cmds[0])

    def test_tag_added_in_spoolman_on_a_gate_is_forwarded(self):
        """Klipper's NFC neighbour check uses the gate's RFIDS set, so tag edits must reach it."""
        self.db.link_tag(UID, spool_id=1)
        self.send('updated', 'spool', self.db._render_spool(1))
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("0: {", cmds[0])
        self.assertIn(UID, cmds[0])

    def test_tag_on_a_spool_not_on_a_gate_is_not_forwarded(self):
        self.db.link_tag(UID, spool_id=3)
        self.send('updated', 'spool', self.db._render_spool(3))
        self.assertEqual(self.gate_map_commands(), [])
        self.assertEqual(self.server.uid_to_spool_id.get(UID), 3, 'cache still updated')

    def test_spool_not_on_a_gate_is_not_forwarded(self):
        self.edit_spool(3, filament={'color_hex': 'ABCDEF'})
        self.assertEqual(self.gate_map_commands(), [])
        self.assertEqual(self.server.spool_location[3][2]['color'], 'abcdef')

    def test_filament_event_updates_its_spools(self):
        fid = self.db.spools[2]['filament']['id']
        filament = self.db.spools[2]['filament']
        filament['name'] = 'Renamed ABS'
        self.send('updated', 'filament', filament)
        self.assertEqual(self.server.spool_location[2][2]['name'], 'Renamed ABS')
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("1: {", cmds[0])
        self.assertEqual(fid, self.server.spool_location[2][2]['filament_id'])


class TestFilamentDetailsReadonly(TestFilamentDetails):
    MODE = 'readonly'


class TestFilamentDetailsPull(TestFilamentDetails):
    MODE = 'pull'


class TestOffMode(LiveTestCase):
    MODE = 'off'

    def test_nothing_forwarded(self):
        self.edit_spool(1, filament={'color_hex': 'ABCDEF'})
        self.edit_spool(3, extra={spoolman.FIELD_PRINTER: json.dumps(HOST), spoolman.FIELD_GATE: '2'})
        self.assertEqual(self.gate_map_commands(), [])


class TestAssignmentsPush(LiveTestCase):
    """Happy Hare owns gate assignments in push mode: changes made in Spoolman are ignored."""

    def test_assignment_in_spoolman_is_not_applied(self):
        self.edit_spool(3, extra={spoolman.FIELD_PRINTER: json.dumps(HOST), spoolman.FIELD_GATE: '2'})
        self.assertEqual(self.gate_map_commands(), [])
        self.assertEqual(self.server.spool_location[3][:2], (HOST, 2), 'cache still current')


class TestAssignmentsReadonly(TestAssignmentsPush):
    MODE = 'readonly'


class TestAssignmentsPull(LiveTestCase):
    """Spoolman owns gate assignments in pull mode: the gate map is refreshed."""
    MODE = 'pull'

    def test_assignment_in_spoolman_refreshes_gate_map(self):
        self.edit_spool(3, extra={spoolman.FIELD_PRINTER: json.dumps(HOST), spoolman.FIELD_GATE: '2'})
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn('REPLACE=1', cmds[0])
        self.assertIn("2: {", cmds[0])

    def test_unassignment_refreshes_gate_map(self):
        self.edit_spool(1, extra={spoolman.FIELD_PRINTER: json.dumps(''), spoolman.FIELD_GATE: '-1'})
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("0: {'spool_id': -1}", cmds[0])

    def test_deleted_spool_on_a_gate_refreshes_gate_map(self):
        self.send('deleted', 'spool', {'id': 1})
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("0: {'spool_id': -1}", cmds[0])

    def test_other_printer_is_ignored(self):
        self.edit_spool(3, extra={spoolman.FIELD_PRINTER: json.dumps('otherprinter'), spoolman.FIELD_GATE: '0'})
        self.assertEqual(self.gate_map_commands(), [])


class TestOwnWritesIgnored(LiveTestCase):
    """Happy Hare's writes come back as events; replaying them must not undo anything."""

    def replay(self):
        for event in self.db.take_events():
            self.hh.run(self.server._on_spoolman_event(event))

    def test_tag_registration_echo(self):
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=3, uid=UID, silent=True)
        self.hh.klippy.drain()
        self.assertTrue(self.db.events, 'precondition: the write produced events')
        self.replay()
        self.assertEqual(self.gate_map_commands(), [])
        self.assertEqual(self.server.uid_to_spool_id.get(UID), 3)

    def test_migrate_extra_echo_does_not_undo_it(self):
        self.db.link_tag(UID, spool_id=3)
        self.hh.call_remote('spoolman_refresh', silent=True)
        self.hh.call_remote('spoolman_migrate_tags', direction='extra', silent=True)
        self.replay()
        self.assertNotIn(UID, self.server.native_uids,
                         'stale "still linked" events must not mark the UID native again')

    def test_external_edit_after_the_window_is_applied(self):
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=1, uid=UID, silent=True)
        self.db.take_events()
        self.hh.klippy.drain()
        self.hh.advance(spoolman_live_window() + 1)
        self.edit_spool(1, filament={'color_hex': 'ABCDEF'})
        self.assertEqual(len(self.gate_map_commands()), 1)


def spoolman_live_window():
    from components import mmu_server
    return mmu_server.LIVE_OWN_WRITE_WINDOW


class TestStaleRecords(LiveTestCase):
    MODE = 'pull'

    def test_filament_edit_keeps_gate_assigned_by_happy_hare(self):
        """Assignment writes only touch spool_location; the cached record must not restore the old gate."""
        self.server.spool_records = json.loads(json.dumps(self.server.spool_records)) # Detach from the fake store, as over HTTP
        self.hh.call_remote('spoolman_set_spool_gate', spool_id=1, gate=2, silent=True)
        self.hh.klippy.extra_status['gate_spool_id'] = [-1, 2, 1, -1]
        self.db.take_events()
        self.hh.klippy.drain()
        self.hh.advance(spoolman_live_window() + 1)
        self.db.spools[1]['filament']['name'] = 'Renamed'
        self.send('updated', 'filament', self.db.spools[1]['filament'])
        self.assertEqual(self.server.spool_location[1][1], 2)
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn("2: {", cmds[0])
        self.assertNotIn('REPLACE=1', cmds[0])


class TestExternalEditInOwnWriteWindow(LiveTestCase):

    def test_edit_right_after_our_write_is_applied(self):
        self.hh.call_remote('spoolman_set_spool_uid', spool_id=1, uid=UID, silent=True)
        self.db.take_events()
        self.hh.klippy.drain()
        self.edit_spool(1, filament={'color_hex': 'ABCDEF'}) # No clock advance: still inside the window
        self.assertEqual(self.server.spool_location[1][2]['color'], 'abcdef')
        self.assertEqual(len(self.gate_map_commands()), 1)


class TestReconnect(LiveTestCase):
    """Events sent while disconnected are lost: the rebuild on reconnect must also reach Klipper."""
    MODE = 'pull'

    def reconnect(self):
        """First connection drops at once; the second one triggers the rebuild."""
        connections = []

        async def connect(url):
            connections.append(FakeConnection([None] if not connections else []))
            return connections[-1]

        async def until_reconnected():
            self.server._start_live_updates()
            for _ in range(200):
                await asyncio.sleep(0)
                if len(connections) == 2 and not self.server.cache_lock.locked():
                    return

        mod = self.hh.mmu_server_mod
        self.addCleanup(setattr, mod, 'asyncio', mod.asyncio)
        mod.asyncio = NoWaitAsyncio()
        self.server._ws_connect = connect
        self.server.live_updates = True
        self.hh.run(until_reconnected())
        self.hh.run(self.server.close())

    def test_assignment_made_while_disconnected_reaches_klipper(self):
        self.db.spools[3]['extra'].update({spoolman.FIELD_PRINTER: json.dumps(HOST), spoolman.FIELD_GATE: '2'})
        self.reconnect()
        self.assertEqual(self.server.spool_location[3][:2], (HOST, 2))
        cmds = self.gate_map_commands()
        self.assertEqual(len(cmds), 1)
        self.assertIn('REPLACE=1', cmds[0])
        self.assertIn("2: {'spool_id': 3", cmds[0])


class NoWaitAsyncio:
    """asyncio for mmu_server whose sleep() only yields (reconnect backoff in tests)."""
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(delay, result=None):
        return await asyncio.sleep(0, result)


class FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.forever = asyncio.get_running_loop().create_future()

    async def read_message(self):
        if self.messages:
            return self.messages.pop(0)
        return await self.forever

    def close(self):
        self.closed = True


class TestConnection(unittest.TestCase):

    def test_on_by_default(self):
        hh = harness(options={'spoolman_live_updates': True})
        self.addCleanup(hh.close)
        self.assertTrue(hh.mmu_server.live_updates)

    def test_off_when_configured(self):
        hh = harness(options={'spoolman_live_updates': False})
        self.addCleanup(hh.close)
        self.assertFalse(hh.mmu_server.live_updates)

    def test_websocket_url(self):
        hh = harness()
        self.addCleanup(hh.close)
        self.assertEqual(hh.mmu_server._live_updates_url(), 'ws://spoolman.test/api/v1/')

    def test_loop_applies_messages(self):
        hh = harness(spools=[dict(material='PLA')])
        self.addCleanup(hh.close)
        hh.component_init()
        server = hh.mmu_server
        spool = hh.db._render_spool(1)
        spool['remaining_weight'] = 123
        messages = ['not json', json.dumps({'type': 'updated', 'resource': 'spool', 'payload': spool})]
        urls = []

        async def connect(url):
            urls.append(url)
            return FakeConnection(messages)

        server._ws_connect = connect
        server.live_updates = True

        async def until_applied():
            server._start_live_updates()
            for _ in range(200):
                if server.spool_location[1][2]['remaining_weight'] == 123:
                    return True
                await asyncio.sleep(0)
            return False

        self.assertTrue(hh.run(until_applied()))
        self.assertEqual(urls, ['ws://spoolman.test/api/v1/'])
        hh.run(server.close())

    def test_start_is_idempotent(self):
        hh = harness()
        self.addCleanup(hh.close)
        server = hh.mmu_server

        async def connect(url):
            return FakeConnection([])

        server._ws_connect = connect
        server.live_updates = True

        async def start_twice():
            server._start_live_updates()
            first = server._live_task
            server._start_live_updates()
            return first is server._live_task

        self.assertTrue(hh.run(start_twice()))
        hh.run(server.close())

    def test_bad_event_keeps_the_connection(self):
        """An event that raises (e.g. Klipper not up yet) is skipped, not a reconnect"""
        hh = harness(spools=[dict(material='PLA')])
        self.addCleanup(hh.close)
        hh.component_init()
        server = hh.mmu_server
        event = json.dumps({'type': 'updated', 'resource': 'spool', 'payload': hh.db._render_spool(1)})
        connections, handled = [], []

        async def connect(url):
            connections.append(FakeConnection([event, event]))
            return connections[-1]

        async def on_event(ev):
            handled.append(ev)
            if len(handled) == 1:
                raise RuntimeError("Klipper not ready")

        server._ws_connect = connect
        server._on_spoolman_event = on_event
        server.live_updates = True

        async def until_handled():
            server._start_live_updates()
            for _ in range(200):
                if len(handled) == 2:
                    return True
                await asyncio.sleep(0)
            return False

        self.assertTrue(hh.run(until_handled()))
        self.assertEqual(len(connections), 1)
        hh.run(server.close())
        self.assertTrue(getattr(connections[0], 'closed', False))


if __name__ == '__main__':
    unittest.main()
