# Fake Klipper `klippy/mcu.py` for the Happy Hare test harness.
#
# CRITICAL: MCU_endstop is the isinstance anchor for two Happy Hare sites, and
# getting it wrong makes tests pass while testing nothing:
#
#   extras/mmu/mmu_sensor_utils.py:520    MmuCompoundEndstop picks the single
#                                         real MCU endstop out of its children.
#   extras/mmu/mmu_filament_movement.py:329  if the gate endstop is NOT an
#                                         mcu.MCU_endstop, _build_gate_nfc_compound
#                                         returns (None, None, None) and the caller
#                                         SILENTLY FALLS BACK to a plain load -
#                                         disabling NFC-compound preload entirely.
#
# So the concrete virtual switch endstop is defined in THIS file, subclassing
# MCU_endstop, making class identity structural rather than accidental (there is
# no way to import a second `mcu` module through a different path).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Mutated by extras/mmu/mmu_controller.py:110-114 (Klipper "timer too close"
# mitigation). Must exist at module level.
TRSYNC_TIMEOUT = 0.025

# Host clock -> MCU print_time offset. Deliberately NON-ZERO: Happy Hare carefully
# distinguishes reactor eventtime from MCU print_time (see the five-source clock
# table at extras/mmu/mmu_sensor_utils.py:410-435 and the _endstop_trigger_time
# overrides in MmuAdcSwitchSensor and MmuNfcEndstop). If the two clocks were
# numerically identical, every clock-domain bug in that code would be invisible.
HOST_OFFSET = 1234.5


class error(Exception):
    pass


class MCU:
    """
    Minimum surface used by HH: extras/mmu_servo.py:77-80,
    extras/mmu/unit/mmu_espooler.py:545, extras/mmu/unit/mmu_extruder_monitor.py:152,
    extras/mmu/unit/nfc/pn7160_driver.py:283.
    """

    def __init__(self, name='mcu', reactor=None):
        self._name = name
        self._reactor = reactor
        self._freq = 64000000.

    def get_name(self):
        return self._name

    def estimated_print_time(self, eventtime):
        return eventtime - HOST_OFFSET

    def print_time_to_clock(self, print_time):
        return int(print_time * self._freq)

    def clock_to_print_time(self, clock):
        return clock / self._freq

    def seconds_to_clock(self, seconds):
        return int(seconds * self._freq)

    def register_flush_callback(self, callback):
        pass

    def register_response(self, cb, msg, oid=None):
        pass

    def get_printer(self):
        return None


class _PinBase:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._pin_params = pin_params
        self._pin = pin_params.get('pin')

    def get_mcu(self):
        return self._mcu


class MCU_digital_out(_PinBase):
    """setup_pin('digital_out', ...) - espooler motors, PN7160 ven_pin."""

    def __init__(self, mcu, pin_params):
        _PinBase.__init__(self, mcu, pin_params)
        self.timeline = []          # [(print_time, value)] for test assertions
        self._start_value = 0.
        self._max_duration = 2.

    def setup_max_duration(self, max_duration):
        self._max_duration = max_duration

    def setup_start_value(self, start_value, shutdown_value, is_static=False):
        self._start_value = start_value

    def set_digital(self, print_time, value):
        self.timeline.append((print_time, int(value)))

    # Some Klipper versions/callers use the generic name
    def set_value(self, print_time, value):
        self.set_digital(print_time, value)


class MCU_pwm(MCU_digital_out):
    """setup_pin('pwm', ...) - espooler motors when pwm:true, mmu_servo, led_effect."""

    def __init__(self, mcu, pin_params):
        MCU_digital_out.__init__(self, mcu, pin_params)
        self._cycle_time = 0.100
        self._hardware_pwm = False

    def setup_cycle_time(self, cycle_time, hardware_pwm=False):
        self._cycle_time = cycle_time
        self._hardware_pwm = hardware_pwm

    def set_pwm(self, print_time, value, cycle_time=None):
        self.timeline.append((print_time, float(value)))


class MCU_adc(_PinBase):
    """
    setup_pin('adc', ...) - hall filament width sensors, proportional buffer
    sensor, led_effect analog trigger.

    Happy Hare probes THREE Klipper ADC API shapes with a TypeError fallback
    (MmuAdcHelper.setup_adc_compat, extras/mmu/mmu_sensor_utils.py:51-68) and
    accepts TWO callback payload shapes (unpack_adc_callback, :70-88). The `api`
    and `payload` knobs let a test select which shape this fake presents, so both
    compat branches are actually covered instead of one being dead code.

      api='new'    : setup_adc_sample(report_time, sample_time, sample_count)
                     + setup_adc_callback(callback)
      api='old'    : setup_adc_sample(sample_time, sample_count)
                     + setup_adc_callback(report_time, callback)
      api='oldest' : setup_minmax(sample_time, sample_count)
                     + setup_adc_callback(report_time, callback)

    'old' must genuinely raise TypeError from the 3-arg call, which is why it is
    implemented as a distinct two-positional-parameter method rather than one
    permissive signature.
    """

    def __init__(self, mcu, pin_params, api='new', payload='samples'):
        _PinBase.__init__(self, mcu, pin_params)
        self.api = api
        self.payload = payload
        self._callback = None
        self._report_time = 0.
        self.last_value = 0.
        if api == 'new':
            self.setup_adc_sample = self._sample_new
            self.setup_adc_callback = self._callback_new
        elif api == 'old':
            self.setup_adc_sample = self._sample_old
            self.setup_adc_callback = self._callback_old
        elif api == 'oldest':
            self.setup_minmax = self._sample_old
            self.setup_adc_callback = self._callback_old
        else:
            raise ValueError("unknown adc api %r" % (api,))

    def _sample_new(self, report_time, sample_time, sample_count):
        self._report_time = report_time

    def _sample_old(self, sample_time, sample_count):
        pass

    def _callback_new(self, callback):
        self._callback = callback

    def _callback_old(self, report_time, callback):
        self._report_time = report_time
        self._callback = callback

    def setup_minmax(self, sample_time, sample_count, minval=None, maxval=None,
                     range_check_count=0):
        pass

    def feed(self, value, read_time=None):
        """Test-facing: deliver one ADC reading in whichever payload shape is selected."""
        self.last_value = value
        if self._callback is None:
            return
        if read_time is None:
            read_time = self._mcu.estimated_print_time(0.) if self._mcu else 0.
        if self.payload == 'pair':
            self._callback(read_time, value)
        else:
            self._callback([(read_time, value)])


class MCU_endstop(_PinBase):
    """
    setup_pin('endstop', ...) - rail endstops and named extra_endstops.

    This is BOTH the isinstance anchor (see module docstring) and the working
    virtual switch. The contract mirrors what HH's own
    MmuVirtualEndstopSensor presents (extras/mmu/mmu_sensor_utils.py:478-486), so
    MmuCompoundEndstop can treat real and virtual children uniformly.
    """

    def __init__(self, mcu, pin_params, reactor=None, printer=None):
        _PinBase.__init__(self, mcu, pin_params)
        self._reactor = reactor
        self._printer = printer
        self._steppers = []
        self._triggered = False
        self._invert = pin_params.get('invert', False)
        self._home_completion = None
        self._home_triggered_state = True
        self._trigger_print_time = None

    # -- Klipper endstop interface -------------------------------------------
    def add_stepper(self, stepper):
        if stepper not in self._steppers:
            self._steppers.append(stepper)

    def get_steppers(self):
        return list(self._steppers)

    def setup_pin(self, pin_type, pin_params):
        return self

    def query_endstop(self, print_time):
        return 1 if self._triggered else 0

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        self._home_triggered_state = triggered
        self._trigger_print_time = None
        self._home_completion = self._reactor.completion()
        # Already in the sought state -> complete immediately, as a real MCU would.
        if bool(self._triggered) == bool(triggered):
            self._trigger_print_time = print_time
            self._home_completion.complete(True)
        return self._home_completion

    def home_wait(self, home_end_time):
        if self._trigger_print_time is None:
            raise self._printer.command_error(
                "No trigger on %s after full movement" % (self._pin,))
        return self._trigger_print_time

    # -- Test-facing ---------------------------------------------------------
    def trigger(self, print_time=None, state=True):
        """Set the switch state; completes an in-flight home_start if it matches."""
        self._triggered = bool(state)
        c = self._home_completion
        if c is not None and not c.test() and bool(state) == bool(self._home_triggered_state):
            self._trigger_print_time = print_time if print_time is not None else 0.
            c.complete(True)


class MCU_trsync:
    REASON_ENDSTOP_HIT = 1
    REASON_COMMS_TIMEOUT = 2


def add_printer_objects(config):
    """
    Mirrors Klipper's mcu.add_printer_objects: one MCU per [mcu] / [mcu name]
    section, the unnamed one registered as 'mcu'.
    """
    printer = config.get_printer()
    reactor = printer.get_reactor()
    printer.add_object('mcu', MCU('mcu', reactor))
    for s in config.get_prefix_sections('mcu '):
        name = s.get_name().split()[-1]
        printer.add_object(s.get_name(), MCU(name, reactor))
