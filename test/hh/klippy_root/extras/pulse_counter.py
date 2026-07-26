# Fake Klipper `klippy/extras/pulse_counter.py` for the Happy Hare test harness.
#
# extras/mmu/unit/mmu_encoder.py:17 imports it; :53-54 builds
# MCU_counter(printer, pin, sample_time, poll_time) and setup_callback(cb).
#
# Real Klipper's MCU_counter internally does pins.setup_pin('counter', pin), so we
# do too - that keeps the pin-binding registry honest (a test can assert the
# encoder pin really was bound as a counter).
#
# Callback shape matches Klipper: cb(read_time, count, count_time).
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class MCU_counter:
    def __init__(self, printer, pin, sample_time=0.001, poll_time=0.0002):
        self.printer = printer
        self._callback = None
        self._count = 0
        self.sample_time = sample_time
        self.poll_time = poll_time
        ppins = printer.lookup_object('pins')
        self._pin_obj = ppins.setup_pin('counter', pin)

    def setup_callback(self, cb):
        self._callback = cb

    def get_mcu(self):
        return getattr(self._pin_obj, 'get_mcu', lambda: None)()

    # -- Test-facing -------------------------------------------------------
    def pulse(self, count, read_time=None, count_time=None):
        """Deliver `count` additional pulses to the registered callback."""
        self._count += count
        if self._callback is None:
            return
        reactor = self.printer.get_reactor()
        if read_time is None:
            read_time = reactor.monotonic()
        if count_time is None:
            count_time = read_time
        self._callback(read_time, self._count, count_time)
