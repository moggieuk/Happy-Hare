# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Bounded, acknowledged telemetry delivery, independent of socket transport.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import uuid
from .capture import TelemetryCapture


class TelemetryStream:
    def __init__(self, mmu, capture=None):
        self.mmu = mmu
        self.capture = capture
        self.owns_capture = capture is None
        self.stream_id = uuid.uuid4().hex
        self.clients = {}
        self.delivery = 0

    def reset_capture(self, mmu, capture):
        """Keep connected consumers, but replace a restarted simulator's source."""
        if self.capture is not None and self.owns_capture:
            self.capture.close()
        self.mmu, self.capture = mmu, capture
        self.owns_capture = False
        self.stream_id = uuid.uuid4().hex
        for client in self.clients.values():
            client.update(after=None, inflight=None)

    def subscribe(self, key, send, closed, after=None, stream_id=None, history="start"):
        if key not in self.clients and len(self.clients) >= 32:
            raise ValueError('Too many telemetry subscribers')
        if self.capture is None:
            self.capture = TelemetryCapture(self.mmu, limit=512)
            self.stream_id = uuid.uuid4().hex
        client = dict(send=send, closed=closed, after=after if stream_id == self.stream_id else None,
                      inflight=None, history=history)
        self.clients[key] = client
        return self._batch(client)

    def _batch(self, client):
        batch = self.capture.read(client['after'], limit=128, history=client['history'])
        if not batch['records']:
            return None
        self.delivery += 1
        cursor = batch['records'][-1]['sequence']
        batch.update(stream_id=self.stream_id, delivery_id=self.delivery, cursor=cursor)
        client['inflight'] = (self.delivery, cursor)
        return batch

    def ack(self, key, stream_id, delivery_id):
        client = self.clients.get(key)
        if client and stream_id == self.stream_id and client['inflight']:
            delivery, cursor = client['inflight']
            if delivery == delivery_id:
                client.update(after=cursor, inflight=None)
        return {}

    def unsubscribe(self, key):
        self.clients.pop(key, None)
        if not self.clients and self.capture is not None and self.owns_capture:
            self.capture.close()
            self.capture = None
        return {}

    def pump(self):
        for key, client in list(self.clients.items()):
            if client['closed']():
                self.unsubscribe(key)
        if not self.clients:
            return
        self.capture.sync()
        for key, client in list(self.clients.items()):
            if client['inflight'] is None:
                batch = self._batch(client)
                if batch is not None:
                    try:
                        client['send'](batch)
                    except Exception:
                        self.unsubscribe(key)

    def close(self):
        for key in list(self.clients):
            self.unsubscribe(key)
