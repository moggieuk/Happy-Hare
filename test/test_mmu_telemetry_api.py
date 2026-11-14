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

    async def test_recording_subscribes_at_readiness_without_a_browser(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            self.component.record = True
            self.component.recorder = TelemetryRecorder(path, 1000000, 2)
            await self.component.ready()
            self.assertEqual(len(self.api.stream.clients), 1)
            self.assertEqual(self.component.clients, {})
            self.hh.mmu.movement_telemetry.state('toolchange', command='MMU_LOAD')
            self.api.stream.pump()
            await asyncio.sleep(0)
            await self.component.writer.queue.join()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]['event'], 'session')
            self.assertEqual(records[-1]['command'], 'MMU_LOAD')
            browser = Browser()
            batch = await self.component.subscribe(Request(connection=browser))
            self.assertEqual(batch['records'][1]['command'], 'MMU_LOAD')
            await self.component.client_removed(browser)
            self.assertEqual(len(self.api.stream.clients), 1)
            await self.component.close()

    async def test_writer_failure_stops_recording_and_warns(self):
        from components.mmu_telemetry_store import TelemetryRecorder
        warnings = []
        self.add_warning = warnings.append
        with tempfile.TemporaryDirectory() as directory:
            self.component.record = True
            self.component.recorder = TelemetryRecorder(Path(directory)/'missing'/'motion.jsonl', 10000, 2)
            await self.component.ready()
            await self.component.writer.queue.join()
            self.assertFalse(self.component.record)
            self.assertEqual(len(warnings), 1)
            for _ in range(3): await asyncio.sleep(0)
            self.assertIsNone(self.api.stream.capture)   # No client or history needs the Klipper subscription now

    async def test_history_buffer_zero_is_on_demand_with_small_delivery_window(self):
        await self.component.ready()
        self.assertEqual(self.component.history_buffer, 0)
        self.assertEqual(self.component.history.limit, 128)
        self.assertIsNone(self.api.stream.capture)
        browser = Browser()
        await self.component.subscribe(Request(connection=browser))
        await self.component.client_removed(browser)
        self.assertIsNone(self.api.stream.capture)

    async def test_positive_history_buffer_enables_background_capture_without_files(self):
        self.component = self.module.load_component(types.SimpleNamespace(
            get_server=lambda: self,
            getint=lambda key, default, **kw: 300 if key == 'history_buffer' else default,
            getboolean=lambda key, default: default))
        await self.component.ready()
        self.assertEqual(self.component.history.limit, 300)
        self.assertIsNotNone(self.api.stream.capture)
        self.assertIsNone(self.component.recorder)
        browser = Browser()
        await self.component.subscribe(Request(connection=browser))
        await self.component.client_removed(browser)
        self.assertIsNotNone(self.api.stream.capture)
        self.hh.mmu.movement_telemetry.state('toolchange', command='MMU_LOAD')
        self.api.stream.pump()
        await asyncio.sleep(0)
        batch = await self.component.subscribe(Request(connection=Browser()))
        self.assertEqual(batch['records'][-1]['command'], 'MMU_LOAD')

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
