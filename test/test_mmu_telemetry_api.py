# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Exercise both adapters together against the real capture and Klipper harness.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import asyncio
import importlib.util
from pathlib import Path
import sys
import json
import tempfile
import types
import unittest
from unittest.mock import patch
from test.hh import session


class Request:
    error = ValueError
    def __init__(self, endpoint='', args=None, connection=None):
        self.endpoint, self.args, self.connection = endpoint, args or {}, connection
    def get_client_connection(self): return self.connection
    def get(self, key, default=None): return self.args.get(key, default)
    get_str = get_int = get_dict = get
    def send(self, response): self.response = response


class Browser:
    is_closed = False
    def __init__(self): self.messages = []
    def call_method(self, method, params): self.messages.append((method, params))


class TestTelemetryAdapters(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hh = session('boxturtle'); self.hh.boot(calibrate=True)
        self.api = self.hh.mmu.telemetry_api
        self.remote = {}; self.endpoints = {}; self.handlers = {}; self.delayed = []
        self.closed = False
        common = types.ModuleType('telemetry_test.common')
        common.WebRequest = Request
        common.RequestType = types.SimpleNamespace(POST=1)
        common.TransportType = types.SimpleNamespace(WEBSOCKET=2)
        path = Path(__file__).resolve().parents[1] / 'components/mmu_telemetry.py'
        spec = importlib.util.spec_from_file_location('telemetry_test.components.mmu_telemetry', path)
        module = importlib.util.module_from_spec(spec)
        from components import mmu_telemetry_store
        with patch.dict(sys.modules, {'telemetry_test.common': common,
                'telemetry_test.components.mmu_telemetry_store': mmu_telemetry_store}):
            spec.loader.exec_module(module)
        self.module = module
        self.component = module.load_component(types.SimpleNamespace(get_server=lambda: self, getint=lambda key, default, **kw: default,
            getboolean=lambda key, default: default))

    async def asyncTearDown(self):
        await self.component.close()
        self.api.close(); self.hh.close()

    def get_app_args(self): return {'data_path': '/tmp'}
    def get_event_loop(self): return types.SimpleNamespace(delay_callback=lambda delay, callback: self.delayed.append((delay, callback)))
    def add_warning(self, message): self.fail(message)
    def register_remote_method(self, name, callback): self.remote[name] = callback
    def register_endpoint(self, name, method, callback, **kwargs):
        self.endpoints[name] = callback
        self.assertEqual(kwargs['transports'], 2)
    def register_event_handler(self, name, callback): self.handlers[name] = callback
    def lookup_component(self, name):
        self.assertEqual(name, 'klippy_connection'); return self
    def is_closed(self): return self.closed
    def send(self, message):
        asyncio.create_task(self.remote[message['method']](**message['params']))
    async def request(self, web_request):
        request = Request(args=web_request.args, connection=self)
        getattr(self.api, web_request.endpoint.rsplit('/', 1)[-1])(request)
        return request.response

    async def test_routing_ownership_ack_and_disconnect(self):
        a, b = Browser(), Browser()
        first = await self.component.subscribe(Request(connection=a))
        second = await self.component.subscribe(Request(connection=b))
        self.assertEqual(len(self.api.stream.clients), 1)
        self.hh.mmu.movement_telemetry.state('cut')
        self.api.stream.pump()
        await asyncio.sleep(0)
        self.assertEqual(a.messages, []); self.assertEqual(b.messages, [])
        await self.component.ack(Request(args=first, connection=b)) # Wrong subscriber's receipt.
        self.api.stream.pump(); self.assertEqual(b.messages, [])
        await self.component.ack(Request(args=first, connection=a))
        self.api.stream.pump()
        await asyncio.sleep(0)
        self.assertEqual(a.messages[-1][0], 'notify_mmu_telemetry')
        self.assertEqual(b.messages, [])
        await self.component.client_removed(a)
        await self.component.client_removed(b)
        self.assertEqual(self.api.stream.clients, {})
        self.assertIsNone(self.api.stream.capture)

    async def test_a_closed_stream_is_subscribed_again_and_a_failed_ack_is_not_fatal(self):
        conn = Browser()
        await self.component.subscribe(Request(connection=conn))
        token = self.component.upstream
        await self.component.process_batch(token, dict(closed=True, records=[]))
        self.assertIsNone(self.component.upstream)
        self.assertEqual(len(self.delayed), 1)
        await self.delayed[0][1]()
        self.assertIsNotNone(self.component.upstream)
        batch = self.hh.mmu.telemetry_api.stream.capture.read()
        batch.update(stream_id=self.hh.mmu.telemetry_api.stream.stream_id, delivery_id=99, cursor=0)
        with patch.object(self.component, 'request', side_effect=OSError('busy')):
            await self.component.process_batch(self.component.upstream, batch)   # Klipper resends later

    async def test_klipper_disconnect_releases_capture(self):
        await self.component.subscribe(Request(connection=Browser()))
        self.closed = True
        self.api.stream.pump()
        await asyncio.sleep(0)
        self.component.disconnected()
        self.assertEqual(self.component.clients, {})
        self.assertIsNone(self.api.stream.capture)

    async def test_repeated_subscription_reuses_dormant_timer(self):
        timer = None
        for i in range(5):
            conn = Browser()
            await self.component.subscribe(Request(connection=conn))
            timer = self.api.timer if i == 0 else timer
            self.assertIs(self.api.timer, timer)
            await self.component.client_removed(conn)
            self.assertEqual(self.api._tick(self.hh.reactor.monotonic()), self.hh.reactor.NEVER)

    async def test_unsubscribe_while_baseline_is_in_flight_does_not_leak(self):
        conn = Browser()
        real_request = self.component.request
        ready, release = asyncio.Event(), asyncio.Event()
        async def delayed(method, args):
            result = await real_request(method, args)
            if method == 'subscribe':
                ready.set()
                await release.wait()
            return result
        with patch.object(self.component, 'request', side_effect=delayed):
            pending = asyncio.create_task(self.component.subscribe(Request(connection=conn)))
            await ready.wait()
            removing = asyncio.create_task(self.component.client_removed(conn))
            await asyncio.sleep(0)
            release.set()
            await pending
            await removing
        self.assertEqual(self.api.stream.clients, {})
        self.assertEqual(self.component.clients, {})

    def wakes(self):
        return [call for call in self.hh.printer.lookup_object('webhooks').calls if call[0] == 'mmu_telemetry_wake']

    async def settle(self, recorded=True):
        self.api.stream.pump()
        for _ in range(3): await asyncio.sleep(0)
        if recorded and self.component.writer is not None:
            await self.component.writer.queue.join()

    def logged(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_klipper_starts_and_stops_an_on_recording_with_the_final_state(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            self.component.recorder = TelemetryRecorder(path, 1000000, 2)
            await self.component.ready()
            self.assertIsNone(self.api.stream.capture)             # Recording is off: nothing subscribes
            self.api.set_record_mode('on')
            self.assertEqual(len(self.wakes()), 1)                  # Nobody was subscribed, so Moonraker is asked to
            await self.remote['mmu_telemetry_wake']()
            self.assertEqual(self.component.mode, 'on')
            self.hh.mmu.movement_telemetry.state('toolchange', command='MMU_LOAD')
            await self.settle()
            self.hh.mmu.gate_selected = 1                           # Changes after the last event
            self.api.set_record_mode('off')
            await self.settle()
            records = self.logged(path)
            self.assertEqual(records[0]['event'], 'session')
            self.assertEqual(records[-1]['event'], 'state_delta')
            self.assertIn(dict(path=['printer', 'mmu', 'gate'], value=1), records[-1]['changes'])
            self.assertNotIn('record_mode', [r['event'] for r in records])
            self.assertEqual((self.component.mode, self.component.recorder.mode), ('off', 'off'))
            self.assertIsNone(self.api.stream.capture)              # Nothing needs the Klipper subscription now
            await self.component.close()

    async def test_one_toolchange_is_recorded_between_its_markers_then_recording_stops(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        telemetry = self.hh.mmu.movement_telemetry
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            self.component.recorder = TelemetryRecorder(path, 1000000, 2)
            self.api.set_record_mode('toolchange')
            await self.remote['mmu_telemetry_wake']()
            telemetry.state('cut')                                  # Before the command: not recorded
            telemetry.toolchange_start(command='MMU_LOAD')
            telemetry.state('cut', cutter='toolhead')
            self.hh.mmu.gate_selected = 1
            telemetry.toolchange_end(command='MMU_LOAD', status='completed')
            await self.settle()
            records = self.logged(path)
            self.assertEqual([r['event'] for r in records],
                             ['session', 'toolchange', 'cut', 'state_delta', 'toolchange_end'])
            self.assertEqual(self.api.stream.record_mode, 'off')
            self.assertEqual(self.component.mode, 'off')
            self.assertIsNone(self.api.stream.capture)
            await self.component.close()

    async def test_arming_while_a_command_is_running_waits_for_the_next_one(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        telemetry = self.hh.mmu.movement_telemetry
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            self.component.recorder = TelemetryRecorder(path, 1000000, 2)
            telemetry.toolchange_start(command='MMU_LOAD')          # Already running when recording is armed
            self.api.set_record_mode('toolchange')
            await self.remote['mmu_telemetry_wake']()
            telemetry.toolchange_end(command='MMU_LOAD', status='completed')
            await self.settle(recorded=False)
            self.assertEqual((self.api.stream.record_mode, self.component.mode), ('toolchange', 'toolchange'))
            telemetry.toolchange_start(command='MMU_UNLOAD')
            telemetry.toolchange_end(command='MMU_UNLOAD', status='completed')   # A refused command: nothing recorded
            await self.settle(recorded=False)
            self.assertFalse(path.exists())
            self.assertEqual((self.api.stream.record_mode, self.component.mode), ('toolchange', 'toolchange'))
            telemetry.toolchange_start(command='MMU_UNLOAD')
            telemetry.state('cut', cutter='toolhead')
            telemetry.toolchange_end(command='MMU_UNLOAD', status='completed')
            await self.settle()
            self.assertEqual([r['event'] for r in self.logged(path)], ['session', 'toolchange', 'cut', 'toolchange_end'])
            await self.component.close()

    async def test_switching_from_on_to_one_toolchange_closes_the_log_and_rotates(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        telemetry = self.hh.mmu.movement_telemetry
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            self.component.recorder = TelemetryRecorder(path, 1000000, 2)
            self.api.set_record_mode('on')
            await self.remote['mmu_telemetry_wake']()
            telemetry.state('cut', cutter='toolhead')
            await self.settle()
            self.api.set_record_mode('toolchange')
            telemetry.toolchange_start(command='MMU_LOAD')
            telemetry.state('cut', cutter='toolhead')
            telemetry.toolchange_end(command='MMU_LOAD', status='completed')
            await self.settle()
            self.assertEqual([r['event'] for r in self.logged(path)], ['session', 'toolchange', 'cut', 'toolchange_end'])
            earlier = self.logged(Path(str(path) + '.1'))
            self.assertIn('cut', [r['event'] for r in earlier])
            await self.component.close()

    async def test_a_recording_requested_before_moonraker_restarted_resumes(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        with tempfile.TemporaryDirectory() as directory:
            self.component.recorder = TelemetryRecorder(Path(directory)/'motion.jsonl', 1000000, 2)
            self.api.stream.record_mode = 'on'
            await self.component.ready()
            self.assertIsNotNone(self.component.upstream)
            self.assertEqual(self.component.mode, 'on')
            await self.component.close()

    async def test_writer_failure_stops_recording_warns_and_tells_klipper(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        warnings = []
        self.add_warning = warnings.append
        with tempfile.TemporaryDirectory() as directory:
            self.component.recorder = TelemetryRecorder(Path(directory)/'missing'/'motion.jsonl', 10000, 2)
            self.api.set_record_mode('on')
            await self.remote['mmu_telemetry_wake']()
            await self.component.writer.queue.join()
            for _ in range(5): await asyncio.sleep(0)
            self.assertEqual(self.component.mode, 'off')
            self.assertEqual(len(warnings), 1)
            self.assertEqual(self.api.stream.record_mode, 'off')
            self.assertIsNone(self.api.stream.capture)   # No client or recording needs the Klipper subscription now

    async def test_a_wake_for_a_recording_already_switched_off_releases_the_subscription(self):
        self.api.set_record_mode('on')
        self.api.set_record_mode('off')                         # Before Moonraker handled the wake
        await self.remote['mmu_telemetry_wake']()
        self.assertIsNone(self.component.upstream)
        self.assertIsNone(self.api.stream.capture)

    async def test_disconnecting_forgets_a_segment_in_progress(self):
        self.component.mode, self.component.segment = 'toolchange', (1, 1)
        self.component.disconnected()
        self.assertEqual((self.component.mode, self.component.segment), ('off', None))

    async def test_a_recording_moonraker_never_starts_warns_the_user_once(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        with patch.object(self.hh.mmu, 'log_warning') as warn, tempfile.TemporaryDirectory() as directory:
            self.component.recorder = TelemetryRecorder(Path(directory)/'motion.jsonl', 1000000, 2)
            self.api.set_record_mode('on')
            self.hh.reactor.advance(6.)
            self.assertEqual(warn.call_count, 1)
            self.assertIn('Moonraker', warn.call_args[0][0])
            warn.reset_mock()
            await self.remote['mmu_telemetry_wake']()            # It did subscribe after all
            self.api.set_record_mode('off')
            self.api.set_record_mode('on')
            self.hh.reactor.advance(6.)
            warn.assert_not_called()
            await self.component.close()

    async def test_nothing_subscribes_until_a_browser_or_a_recording_needs_it(self):
        await self.component.ready()
        self.assertEqual(self.component.history.limit, 128)
        self.assertIsNone(self.api.stream.capture)
        browser = Browser()
        await self.component.subscribe(Request(connection=browser))
        await self.component.client_removed(browser)
        self.assertIsNone(self.api.stream.capture)

    async def test_failed_initial_ack_releases_upstream_capture(self):
        request = self.component.request
        async def failing(method, args):
            if method == 'ack':
                raise OSError('connection failed')
            return await request(method, args)
        with patch.object(self.component, 'request', side_effect=failing):
            with self.assertRaises(OSError):
                await self.component.subscribe(Request(connection=Browser()))
        self.assertIsNone(self.component.upstream)
        self.assertEqual(self.component.clients, {})
        self.assertIsNone(self.api.stream.capture)
