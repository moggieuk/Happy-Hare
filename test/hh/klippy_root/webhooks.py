# Fake Klipper `klippy/webhooks.py` for the Happy Hare test harness.
#
# HH uses call_remote_method (16 sites, all Spoolman/Moonraker -
# extras/mmu/mmu_controller.py:2957-3408) and, for TD-1 only, ONE endpoint: the
# 'mmu/td1' reply channel. deliver() is its inbound counterpart.
#
# In production this is FIRE-AND-FORGET: Klipper hands the call to Moonraker and
# returns immediately; any result comes back later as a separate gcode command. The
# harness preserves that exactly - calls are appended to an inbox and returned from
# at once. The round-trip driver (test/hh/moonraker.py) drains the inbox into the
# real MmuServer, so ordering and re-entrancy stay faithful without nested loops.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


class WebRequestError(Exception):
    pass


class WebRequest:
    """
    What an endpoint callback is handed. Klipper's real one carries a client
    connection; here the response is simply kept, since nothing reads it back.
    """

    _SENTINEL = object()

    def __init__(self, method, params):
        self.method = method
        self.params = dict(params)
        self.response = None

    def get_dict(self, key, default=_SENTINEL):
        return self.get(key, default)

    def get(self, key, default=_SENTINEL):
        if key not in self.params:
            if default is self._SENTINEL:
                raise WebRequestError("Missing Argument [%s]" % key)
            return default
        return self.params[key]

    def get_int(self, key, default=_SENTINEL):
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise WebRequestError("Argument [%s] is not an integer" % key) from exc

    def get_str(self, key, default=_SENTINEL):
        return self.get(key, default)

    def send(self, data):
        self.response = data


class WebHooks:
    error = WebRequestError

    def __init__(self, printer):
        self.printer = printer
        self._endpoints = {}
        self._remote_methods = {}
        # -- assertion surfaces / round-trip plumbing ---------------------
        self.calls = []        # [(name, kwargs)] every call_remote_method, in order
        self.inbox = []        # undrained calls, for the Moonraker pump
        self.sink = None       # optional callable(name, kwargs); set by the pump
        # Per-method overrides, checked before `sink` - see
        # MoonrakerLink.enable_td1_bridge()
        self.sinks = {}

    def register_endpoint(self, path, callback, request_methods=None):
        self._endpoints[path] = callback

    def register_mux_endpoint(self, path, key, value, callback):
        self._endpoints[(path, key, value)] = callback

    def get_connection(self):
        return None

    def get_status(self, eventtime=None):
        return {'state': 'ready', 'state_message': 'Printer is ready'}

    def call_remote_method(self, method, **kwargs):
        self.calls.append((method, kwargs))
        logging.debug('call_remote_method %s(%r)', method, kwargs)
        handler = self.sinks.get(method, self.sink)
        if handler is not None:
            handler(method, kwargs)
        else:
            self.inbox.append((method, kwargs))

    def deliver(self, path, **params):
        """
        Call a registered endpoint the way Moonraker's internal transport would.

        Inbound and synchronous, unlike call_remote_method: nothing is queued here
        because the endpoint callback IS the delivery. Whoever calls this is
        responsible for not doing so from inside a Moonraker coroutine - see
        MoonrakerLink, which queues the hop and drains it from settle().
        """
        callback = self._endpoints.get(path)
        if callback is None:
            raise WebRequestError("Unknown endpoint %s" % path)
        request = WebRequest(path, params)
        callback(request)
        return request.response

    def endpoints(self):
        return sorted(k for k in self._endpoints if isinstance(k, str))

    # -- test-facing --------------------------------------------------------
    def calls_to(self, method):
        return [kw for name, kw in self.calls if name == method]

    def drain(self):
        pending, self.inbox = self.inbox, []
        return pending


def add_early_printer_objects(printer):
    printer.add_object('webhooks', WebHooks(printer))
