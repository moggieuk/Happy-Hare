# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Klipper webhook adapter for the optional motion telemetry stream.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging
from .stream import TelemetryStream


class TelemetryAPI:
    def __init__(self, mmu):
        self.reactor = mmu.reactor
        self.stream = TelemetryStream(mmu)
        self.timer = None
        webhooks = mmu.printer.lookup_object('webhooks')
        for name in ('subscribe', 'ack', 'unsubscribe'):
            webhooks.register_endpoint('mmu/telemetry/' + name, getattr(self, name))
        mmu.printer.register_event_handler('klippy:disconnect', self.close)
        mmu.printer.register_event_handler('klippy:shutdown', self.close)

    def _key(self, request):
        return request.get_client_connection(), request.get_str('subscription_id')

    def subscribe(self, request):
        conn, token = key = self._key(request)
        template = request.get_dict('response_template')
        def send(batch):
            conn.send(dict(template, params=dict(subscription_id=token, batch=batch)))
        try:
            batch = self.stream.subscribe(key, send, conn.is_closed,
                request.get_int('after', None), request.get_str('stream_id', None))
        except ValueError as exc:
            raise request.error(str(exc))
        request.send(batch)
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._tick, self.reactor.monotonic() + .25)
        else:
            self.reactor.update_timer(self.timer, self.reactor.monotonic() + .25)

    def ack(self, request):
        request.send(self.stream.ack(self._key(request), request.get_str('stream_id'),
                                    request.get_int('delivery_id')))

    def unsubscribe(self, request):
        request.send(self.stream.unsubscribe(self._key(request)))

    def _tick(self, eventtime):
        try:
            self.stream.pump()
        except Exception:
            logging.exception('MMU telemetry delivery stopped')
            self.stream.close()
        if not self.stream.clients:
            return self.reactor.NEVER
        return eventtime + .25

    def close(self):
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
            self.timer = None
        self.stream.close()
