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


WAKE_TIMEOUT = 5.  # Seconds Moonraker has to subscribe after being asked to record


class TelemetryAPI:
    def __init__(self, mmu):
        self.mmu = mmu
        self.reactor = mmu.reactor
        self.wake_timer = None
        self.stream = TelemetryStream(mmu)
        self.timer = None
        self.webhooks = webhooks = mmu.printer.lookup_object('webhooks')
        for name in ('subscribe', 'ack', 'unsubscribe', 'status', 'record'):
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
            batch = self.stream.subscribe(key, send, conn.is_closed)
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

    def status(self, request):
        request.send(dict(record=self.stream.record_mode))

    def record(self, request):
        try:
            self.set_record_mode(request.get_str('mode'))
        except ValueError as exc:
            raise request.error(str(exc))
        request.send({})

    def set_record_mode(self, mode):
        """Moonraker persists records; ask it to subscribe if no viewer already has."""
        if self.stream.set_record_mode(mode):
            self.webhooks.call_remote_method('mmu_telemetry_wake')
            if self.wake_timer is None:
                self.wake_timer = self.reactor.register_timer(self._check_wake)
            self.reactor.update_timer(self.wake_timer, self.reactor.monotonic() + WAKE_TIMEOUT)

    def _check_wake(self, eventtime):
        if self.stream.capture is None and self.stream.record_mode != 'off':
            self.mmu.log_warning("Telemetry recording is requested but Moonraker has not started it. Check that Moonraker "
                                 "is running with [mmu_telemetry] loaded; recording starts when it connects")
        return self.reactor.NEVER

    def unsubscribe(self, request):
        request.send(self.stream.unsubscribe(self._key(request)))

    def _tick(self, eventtime):
        try:
            self.stream.pump()
        except Exception:
            logging.exception('MMU telemetry delivery stopped')
            self.stream.close(notify=True)
        if not self.stream.clients:
            return self.reactor.NEVER
        return eventtime + .25

    def close(self):
        self.stream.record_mode = 'off'
        if self.wake_timer is not None:
            self.reactor.update_timer(self.wake_timer, self.reactor.NEVER)
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
            self.timer = None
        self.stream.close()
