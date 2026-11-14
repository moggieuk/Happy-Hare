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
import logging
import uuid
from ..mmu_constants import TELEMETRY_MODES
from .capture import TelemetryCapture

MAX_SUBSCRIBERS = 32
ACK_TIMEOUT = 5.  # A batch not acknowledged by now is sent again


class TelemetryStream:
    def __init__(self, mmu, capture=None):
        self.mmu = mmu
        self.capture = capture
        self.owns_capture = capture is None
        self.stream_id = uuid.uuid4().hex
        self.clients = {}
        self.delivery = 0
        self.record_mode = 'off'  # off, on or toolchange (one command, then off); Moonraker persists

    def reset_capture(self, mmu, capture):
        """Keep connected consumers, but replace a restarted simulator's source."""
        if self.capture is not None and self.owns_capture:
            self.capture.close()
        self.mmu, self.capture = mmu, capture
        self.owns_capture = False
        self.stream_id = uuid.uuid4().hex
        for client in self.clients.values():
            client.update(after=None, inflight=None)

    def subscribe(self, key, send, closed, history="start"):
        if key not in self.clients and len(self.clients) >= MAX_SUBSCRIBERS:
            raise ValueError('Too many telemetry subscribers')
        if self.capture is None:
            self._new_capture()
            self.stream_id = uuid.uuid4().hex
        client = dict(send=send, closed=closed, after=None, inflight=None, history=history)
        self.clients[key] = client
        return self._batch(client)

    def _batch(self, client):
        batch = self.capture.read(client['after'], limit=128, history=client['history'], detach=False)
        if not batch['records']:
            return None
        self.delivery += 1
        cursor = batch['records'][-1]['sequence']
        batch.update(stream_id=self.stream_id, delivery_id=self.delivery, cursor=cursor)
        client['inflight'] = (self.delivery, cursor)
        client['sent_at'] = self.mmu.reactor.monotonic()
        return batch

    def ack(self, key, stream_id, delivery_id):
        client = self.clients.get(key)
        if client and stream_id == self.stream_id and client['inflight']:
            delivery, cursor = client['inflight']
            if delivery == delivery_id:
                client.update(after=cursor, inflight=None)
                self._fold()
        return {}

    def _fold(self):
        # Records every consumer has acknowledged need not be kept: the baseline already includes them
        cursors = [client['after'] for client in self.clients.values()]
        if None not in cursors:
            self.capture.fold_through(min(cursors))

    def unsubscribe(self, key):
        self.clients.pop(key, None)
        if not self.clients and self.capture is not None and self.owns_capture:
            self.capture.close()
            self.capture = None
        return {}

    def _new_capture(self):
        self.capture = TelemetryCapture(self.mmu, limit=256, record_mode=lambda: self.record_mode)
        self.capture.on_toolchange_end = self._toolchange_ended
        if self.record_mode != 'off':
            self.capture.mark_record_mode(self.record_mode)  # A new consumer learns that recording is on

    def _toolchange_ended(self):
        if self.record_mode == 'toolchange':
            self.record_mode = 'off'

    def set_record_mode(self, mode):
        """Returns True when nobody is subscribed yet, so Moonraker must be asked to subscribe."""
        if mode not in TELEMETRY_MODES:
            raise ValueError('Unknown recording mode: %s' % mode)
        self.record_mode = mode
        if self.capture is not None:
            self.capture.mark_record_mode(mode)
        return self.capture is None and mode != 'off'

    def _renew_capture(self):
        """The source detached a capture that failed: start a new one and send every consumer a fresh baseline."""
        self.capture.close()
        self._new_capture()
        self.stream_id = uuid.uuid4().hex
        for client in self.clients.values():
            client.update(after=None, inflight=None)

    def pump(self):
        for key, client in list(self.clients.items()):
            if client['closed']():
                self.unsubscribe(key)
        if not self.clients:
            return
        if self.capture.failed and self.owns_capture:
            logging.error('MMU telemetry capture failed; restarting it')
            self._renew_capture()
        now = self.mmu.reactor.monotonic()
        for client in self.clients.values():
            if client['inflight'] is not None and now - client['sent_at'] > ACK_TIMEOUT:
                client['inflight'] = None  # The acknowledgement was lost: resend from the last acknowledged record
        for key, client in list(self.clients.items()):
            if client['inflight'] is None:
                batch = self._batch(client)
                if batch is not None:
                    try:
                        client['send'](batch)
                    except Exception:
                        logging.exception('MMU telemetry subscriber dropped')
                        self.unsubscribe(key)

    def close(self, notify=False):
        """Drop every consumer. With `notify` each is first told the stream is closed, so it can subscribe again."""
        for key, client in list(self.clients.items()):
            if notify:
                try:
                    client['send'](dict(schema='happy_hare.motion', version=1, closed=True, records=[]))
                except Exception:
                    logging.exception('MMU telemetry close notice failed')
            self.unsubscribe(key)
