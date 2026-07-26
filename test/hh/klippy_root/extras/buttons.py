# Fake Klipper `klippy/extras/buttons.py` for the Happy Hare test harness.
#
# This is the ONLY legitimate way for a test to drive a switch sensor, and that is
# deliberate. Poking runout_helper.filament_present directly would skip
# MmuRunoutHelper.note_filament_present entirely - and that is where event_delay,
# min_event_systime gating, and the insert/remove/runout/clog/tangle dispatch live
# (extras/mmu/mmu_sensor_utils.py:98-273). Driving the button callback is what real
# Klipper MCU button handling does, so the whole path runs.
#
# Call sites:
#   register_debounce_button  extras/mmu/mmu_sensor_utils.py:366  (switch sensors)
#   register_adc_button       extras/mmu/unit/mmu_sensors.py:139  (analog hall as switch)
#   register_buttons          extras/mmu/unit/mmu_espooler.py:120 (assist trigger)
#                             extras/mmu/unit/nfc/pn7160_driver.py:208 (IRQ pin)
#                             extras/mmu_led_effect.py:557
#
# Note the clock domain: real Klipper marshals the ADC-button callback through
# reactor.register_async_callback, so it arrives as a reactor EVENTTIME, not
# print_time - which is exactly why MmuAdcSwitchSensor overrides
# _endstop_trigger_time (extras/mmu/unit/mmu_sensors.py:144-147). We match that.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class _Registration:
    def __init__(self, kind, pins, callback, extra=None):
        self.kind = kind
        self.pins = list(pins)
        self.callback = callback
        self.extra = extra or {}
        self.state = 0


class PrinterButtons:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.registrations = []          # every registration, in order
        self.by_pin = {}                 # pin desc -> [_Registration, ...]

    def _add(self, kind, pins, callback, extra=None):
        reg = _Registration(kind, pins, callback, extra)
        self.registrations.append(reg)
        for pin in reg.pins:
            self.by_pin.setdefault(pin.strip(), []).append(reg)
        # Make sure the pin is bound so it shows up in the pin registry with a type
        ppins = self.printer.lookup_object('pins')
        for pin in reg.pins:
            pin_type = 'adc' if kind == 'adc' else 'endstop'
            try:
                ppins.setup_pin(pin_type, pin)
            except Exception:
                # Shared/multi-use pins may already be bound; the registry keeps
                # every binding so this is not fatal.
                pass
        return reg

    def register_buttons(self, pins, callback):
        return self._add('digital', pins, callback)

    def register_button_push(self, pin, callback):
        def helper(eventtime, state):
            if state:
                callback(eventtime)
        return self._add('digital', [pin], helper)

    def register_debounce_button(self, pin, callback, config):
        debounce = config.getfloat('debounce_delay', 0., minval=0.)
        return self._add('debounce', [pin], callback, {'debounce_delay': debounce})

    def register_adc_button(self, pin, min_val, max_val, pullup, callback):
        return self._add('adc', [pin], callback,
                         {'min': min_val, 'max': max_val, 'pullup': pullup})

    def register_adc_button_push(self, pin, min_val, max_val, pullup, callback):
        def helper(eventtime, state):
            if state:
                callback(eventtime)
        return self.register_adc_button(pin, min_val, max_val, pullup, helper)

    def register_rotary_encoder(self, pin1, pin2, cw_callback, ccw_callback,
                                steps_per_detent=2):
        return self._add('encoder', [pin1, pin2], cw_callback)

    # -- test-facing ---------------------------------------------------------
    def press(self, pin, state=True, eventtime=None):
        """
        Deliver a state change to every callback registered for `pin`, exactly as
        the MCU button handler would. Returns the number of callbacks invoked.
        """
        regs = self.by_pin.get(pin.strip())
        if not regs:
            raise AssertionError(
                "no button registered for pin %r; registered: %s"
                % (pin, ', '.join(sorted(self.by_pin))))
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        for reg in regs:
            reg.state = 1 if state else 0
            reg.callback(eventtime, 1 if state else 0)
        return len(regs)

    def get_status(self, eventtime=None):
        return {}


def load_config(config):
    return PrinterButtons(config)
