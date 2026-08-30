# Fake Klipper `klippy/extras/output_pin.py` for the Happy Hare test harness.
#
# extras/mmu/unit/mmu_espooler.py:27 imports the module and :134 hasattr-probes for
# GCodeRequestQueue (Kalico lacks it, so HH falls back to its own vendored copy).
# The probe means this class MUST exist here, otherwise the test only ever
# exercises HH's fallback path.
#
# Klipper's real GCodeRequestQueue defers the callback to a lookahead callback so
# pin changes land at the right print_time. Here it calls straight through: at this
# tier there is no move queue to order against, and the espooler's own
# _set_pin(print_time, value) is what we want to observe.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class GCodeRequestQueue:
    def __init__(self, config, mcu, callback):
        self.printer = config.get_printer()
        self.mcu = mcu
        self.callback = callback
        self.requests = []          # [(print_time, value)] test assertion surface

    def _get_print_time(self):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return 0.
        return toolhead.get_last_move_time()

    def send_async_request(self, value, pt=None):
        if pt is None:
            pt = self._get_print_time()
        self.requests.append((pt, value))
        self.callback(pt, value)

    def queue_gcode_request(self, value):
        self.send_async_request(value)
