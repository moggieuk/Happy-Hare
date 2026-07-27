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
        self._last_time = 0.
        ppins = printer.lookup_object('pins')
        self._pin_obj = ppins.setup_pin('counter', pin)
        # mmu_encoder.py:53 keeps the counter in a local, so there is no way to reach
        # it from the MmuEncoder object. Publish it here instead, keyed by pin, so the
        # Session can drive real pulses from filament travel.
        if not hasattr(printer, 'harness_counters'):
            printer.harness_counters = {}
        printer.harness_counters[pin] = self
        # Deliver a baseline sample once the machine is up. Real hardware streams counts
        # every poll_time from connect onwards, so by the time anything moves the
        # consumer has long since seen a first sample. Here the counter is only driven
        # by filament travel, so without this the FIRST move of the session is silently
        # eaten: mmu_encoder._counter_callback (:386) uses its first sample as a
        # baseline and returns before accumulating. The symptom was an encoder
        # gate-home whose opening 200mm move measured 0.0mm and retried.
        #
        # It has to be connect, not here: mmu_encoder.py:53 constructs the counter
        # part-way through its own __init__, before self.endstop_sensor exists.
        printer.register_event_handler('klippy:connect', lambda: self.pulse(0))

    def setup_callback(self, cb):
        self._callback = cb

    def get_mcu(self):
        return getattr(self._pin_obj, 'get_mcu', lambda: None)()

    # -- Test-facing -------------------------------------------------------
    def pulse(self, count, read_time=None, count_time=None):
        """
        Deliver `count` additional pulses to the registered callback.

        Sample times must STRICTLY increase: mmu_encoder._counter_callback (:396)
        only accumulates when `count_time - self._last_count_time > 0` and otherwise
        treats the sample as no-movement. A whole gear move completes inside one
        virtual-reactor instant, so falling back to reactor.monotonic() alone would
        report every sample at the same timestamp and the encoder would measure zero.
        """
        self._count += count
        if self._callback is None:
            return
        reactor = self.printer.get_reactor()
        if read_time is None:
            read_time = max(reactor.monotonic(), self._last_time + self.sample_time)
        if count_time is None:
            count_time = read_time
        self._last_time = max(self._last_time, read_time, count_time)
        self._callback(read_time, self._count, count_time)
