# Fake Klipper `klippy/webhooks.py` for the Happy Hare test harness.
#
# HH only ever uses call_remote_method (16 sites, all Spoolman/Moonraker -
# extras/mmu/mmu_controller.py:2957-3408). It never registers an endpoint.
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
        if self.sink is not None:
            self.sink(method, kwargs)
        else:
            self.inbox.append((method, kwargs))

    # -- test-facing --------------------------------------------------------
    def calls_to(self, method):
        return [kw for name, kw in self.calls if name == method]

    def drain(self):
        pending, self.inbox = self.inbox, []
        return pending


def add_early_printer_objects(printer):
    printer.add_object('webhooks', WebHooks(printer))
