# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: One Klipper subscription, bounded Moonraker history and optional disk recording.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import asyncio
import logging
from pathlib import Path
import uuid
from ..common import WebRequest, RequestType, TransportType
from .mmu_telemetry_store import TelemetryHistory, TelemetryRecorder, TelemetryWriter

MAX_SUBSCRIBERS = 32
RESUBSCRIBE_DELAY = 5.  # Seconds before subscribing again after Klipper closes the stream


class MmuTelemetry:
    def __init__(self, config):
        self.server = config.get_server()
        self.clients = {}
        self.history_buffer = config.getint('history_buffer', 0, minval=0)
        # With history disabled, retain only one delivery batch for streaming.
        self.history = TelemetryHistory(self.history_buffer or 128)
        self.record = config.getboolean('record', False)
        max_log_size = config.getint('max_log_size', 10485760, minval=1024)
        log_backups = config.getint('log_backups', 5, minval=1)
        logdir = Path(self.server.get_app_args()['data_path']) / 'logs'
        self.recorder = TelemetryRecorder(logdir / 'mmu-telemetry.jsonl',
            max_log_size, log_backups) if self.record else None
        self.writer = None
        self.upstream = None
        self.stream_id = uuid.uuid4().hex
        self.delivery = 0
        self.lock = asyncio.Lock()
        self.server.register_remote_method('process_mmu_telemetry', self.process_batch)
        for name in ('subscribe', 'ack', 'unsubscribe'):
            self.server.register_endpoint('/server/mmu/telemetry/' + name, RequestType.POST,
                getattr(self, name), transports=TransportType.WEBSOCKET)
        self.server.register_event_handler('websockets:client_removed', self.client_removed)
        self.server.register_event_handler('server:klippy_ready', self.ready)
        self.server.register_event_handler('server:klippy_disconnect', self.disconnected)
        self.server.register_event_handler('server:klippy_shutdown', self.disconnected)

    async def request(self, method, args):
        return await self.server.lookup_component('klippy_connection').request(
            WebRequest('mmu/telemetry/' + method, args))

    async def ready(self):
        if self.record or self.history_buffer:
            async with self.lock:
                await self._start()

    async def _start(self):
        if self.upstream is not None:
            return
        token = self.upstream = uuid.uuid4().hex
        try:
            batch = await self.request('subscribe', dict(subscription_id=token,
                response_template={'method': 'process_mmu_telemetry'}))
            if self.upstream == token:
                await self.process_batch(token, batch, initial=True)
        except Exception:
            if self.upstream == token:
                self.upstream = None
            try:
                await self.request('unsubscribe', dict(subscription_id=token))
            except Exception:
                pass
            raise

    def _batch(self, client):
        batch = self.history.read(client['after'], history=client['history'])
        if not batch['records']:
            return None
        self.delivery += 1
        cursor = batch['records'][-1]['sequence']
        batch.update(stream_id=self.stream_id, delivery_id=self.delivery, cursor=cursor)
        client['inflight'] = (self.delivery, cursor)
        return batch

    async def subscribe(self, request):
        conn = request.get_client_connection()
        if conn not in self.clients and len(self.clients) >= MAX_SUBSCRIBERS:
            raise self.server.error('Too many telemetry subscribers')
        history = request.get('history', 'start')
        if history not in ('start', 'current'):
            raise self.server.error('history must be start or current')
        client = dict(after=None, history=history, inflight=None, ready=False)
        self.clients[conn] = client
        async with self.lock:
            try:
                await self._start()
            except Exception:
                if self.clients.get(conn) is client:
                    self.clients.pop(conn, None)
                raise
        if conn.is_closed:
            await self.client_removed(conn)
            return {}
        if self.clients.get(conn) is not client:
            return {}
        client['ready'] = True
        return self._batch(client)

    async def ack(self, request):
        client = self.clients.get(request.get_client_connection())
        if client and request.get_str('stream_id') == self.stream_id and client['inflight']:
            delivery, cursor = client['inflight']
            if delivery == request.get_int('delivery_id'):
                client.update(after=cursor, inflight=None)
                self._deliver(request.get_client_connection(), client)
        return {}

    def _deliver(self, conn, client):
        if not conn.is_closed and client['inflight'] is None:
            batch = self._batch(client)
            if batch:
                conn.call_method('notify_mmu_telemetry', [batch])

    async def unsubscribe(self, request):
        await self.client_removed(request.get_client_connection())
        return {}

    async def client_removed(self, conn):
        self.clients.pop(conn, None)
        await self._release_if_idle()

    async def _release_if_idle(self):
        async with self.lock:
            if not self.clients and not self.record and not self.history_buffer and self.upstream:
                token, self.upstream = self.upstream, None
                try:
                    await self.request('unsubscribe', dict(subscription_id=token))
                except Exception:
                    pass  # Klipper disconnect releases its subscription.

    def disconnected(self):
        self.upstream = None
        self.clients.clear()
        self.history = TelemetryHistory(self.history.limit)
        self.stream_id = uuid.uuid4().hex

    async def process_batch(self, subscription_id, batch, initial=False):
        if subscription_id != self.upstream:
            return
        if batch.get('closed'):
            # Klipper dropped the subscription after an error: subscribe again shortly.
            self.upstream = None
            if self.clients or self.record or self.history_buffer:
                self.server.get_event_loop().delay_callback(RESUBSCRIBE_DELAY, self._resubscribe)
            return
        if batch.get('reset'):
            self.stream_id = uuid.uuid4().hex
            for client in self.clients.values():
                client.update(after=None, inflight=None)
        committed = self.history.append(batch)
        if self.record and committed:
            if self.writer is None:
                self.writer = TelemetryWriter(self.recorder, self._recording_failed)
            self.writer.submit(committed)
        # Acknowledgment means accepted into Moonraker history, not rendered or flushed to disk.
        try:
            await self.request('ack', dict(subscription_id=subscription_id,
                stream_id=batch['stream_id'], delivery_id=batch['delivery_id']))
        except Exception:
            if initial:
                raise  # The subscription is not established
            logging.exception('MMU telemetry acknowledgement failed; Klipper will resend')
        for conn, client in list(self.clients.items()):
            # Initial subscription returns its baseline through the request response.
            if client['ready']:
                self._deliver(conn, client)

    async def _resubscribe(self):
        async with self.lock:
            try:
                await self._start()
            except Exception:
                logging.exception('MMU telemetry could not subscribe again')

    def _recording_failed(self, reason):
        self.record = False
        self.server.add_warning('MMU telemetry recording stopped: %s.' % reason)
        asyncio.create_task(self._release_if_idle())  # Nothing else needs the Klipper subscription

    async def close(self):
        token, self.upstream = self.upstream, None
        self.clients.clear()
        if token:
            try:
                await self.request('unsubscribe', dict(subscription_id=token))
            except Exception:
                pass
        if self.writer:
            await self.writer.close()


def load_component(config):
    return MmuTelemetry(config)
