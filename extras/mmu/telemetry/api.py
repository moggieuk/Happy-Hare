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
        self.error = mmu.printer.command_error
        self.webhooks = webhooks = mmu.printer.lookup_object('webhooks')
        for name in ('subscribe', 'ack', 'unsubscribe', 'record'):
            webhooks.register_endpoint('mmu/telemetry/' + name, self._guarded(getattr(self, name)))
        mmu.printer.register_event_handler('klippy:disconnect', self.close)
        mmu.printer.register_event_handler('klippy:shutdown', self.close)

    def _guarded(self, endpoint):
        """Klipper shuts down on any other exception from an endpoint, so report a failure to the caller instead."""
        def run(request):
            try:
                endpoint(request)
            except self.error:
                raise
            except Exception as exc:
                logging.exception('MMU telemetry request failed')
                raise request.error(str(exc))
        return run

    def _key(self, request):
        return request.get_client_connection(), request.get_str('subscription_id')

    def subscribe(self, request):
        conn, token = key = self._key(request)
        template = request.get_dict('response_template')
        def send(batch):
            conn.send(dict(template, params=dict(subscription_id=token, batch=batch)))
        request.send(self.stream.subscribe(key, send, conn.is_closed))
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._tick, self.reactor.monotonic() + .25)
        else:
            self.reactor.update_timer(self.timer, self.reactor.monotonic() + .25)

    def ack(self, request):
        request.send(self.stream.ack(self._key(request), request.get_str('stream_id'),
                                    request.get_int('delivery_id')))

    def record(self, request):
        self.set_record_mode(request.get_str('mode'))
        request.send({})

    def set_record_mode(self, mode):
        """Moonraker persists records; ask it to subscribe if no viewer already has."""
        try:
            wake = self.stream.set_record_mode(mode)
        except Exception as exc:
            if not isinstance(exc, ValueError):
                logging.exception('MMU telemetry could not change the recording mode')
            raise self.error("Telemetry recording mode not changed: %s" % exc)  # Anything else would shut Klipper down
        if wake:
            try:
                self.webhooks.call_remote_method('mmu_telemetry_wake')
            except self.error:
                self._check_wake()  # Moonraker has not registered the method: it is not running or lacks [mmu_telemetry]
                return
            if self.wake_timer is None:
                self.wake_timer = self.reactor.register_timer(self._check_wake)
            self.reactor.update_timer(self.wake_timer, self.reactor.monotonic() + WAKE_TIMEOUT)

    def _check_wake(self, eventtime=None):
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
