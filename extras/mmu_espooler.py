# Happy Hare MMU Software
#
# Implements h/w "eSpooler" control for a MMU unit that is powered by a DC motor
# (normally PWM speed controlled) that can be used to rewind a filament spool or be
# driven peridically in the forward direction to provide "forward assist" functionality.
# For simplicity of setup it is assumed that all pins are of the same type/config per mmu_unit.
# Control is via direct control or klipper events.
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

from . import output_pin

MAX_SCHEDULE_TIME = 5.0

class MmuESpooler:

    def __init__(self, config, first_gate=0, num_gates=23):
        self.config = config
        self.first_gate = first_gate
        self.num_gates = num_gates
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.respool_gates = []
        self.assist_gates = []
        self.mmu = None
        self.burst_trigger_enabled = {}
        self.burst_trigger_state = {}
        self.back_to_back_burst_count = {}
        self.burst_gates = set() # Gates with burst assist in operation

        # Get config
        self.motor_mcu_pins = {}
        self.last_value = {}
        self.operation = {}
        ppins = self.printer.lookup_object('pins')
        buttons = self.printer.load_object(config, 'buttons')

        # These params are assumed to be shared accross the espooler unit
        self.is_pwm = config.getboolean("pwm", True)
        self.hardware_pwm = config.getboolean("hardware_pwm", False)
        self.scale = config.getfloat('scale', 1., above=0.)
        self.cycle_time = config.getfloat("cycle_time", 0.100, above=0., maxval=MAX_SCHEDULE_TIME)
        self.shutdown_value = config.getfloat('shutdown_value', 0., minval=0., maxval=self.scale) / self.scale
        start_value = config.getfloat('value', 0., minval=0., maxval=self.scale) / self.scale

        for gate in range(self.first_gate, self.first_gate + self.num_gates + 1):
            self.respool_motor_pin = config.get('respool_motor_pin_%d' % gate, None)
            self.assist_motor_pin = config.get('assist_motor_pin_%d' % gate, None)
            self.enable_motor_pin = config.get('enable_motor_pin_%d' % gate, None)
            self.assist_trigger_pin = config.get('assist_trigger_pin_%d' % gate, None)

            # Setup pins
            if self.respool_motor_pin and not self._is_empty_pin(self.respool_motor_pin):
                if self.is_pwm:
                    mcu_pin = ppins.setup_pin("pwm", self.respool_motor_pin)
                    mcu_pin.setup_cycle_time(self.cycle_time, self.hardware_pwm)
                else:
                    mcu_pin = ppins.setup_pin("digital_out", self.respool_motor_pin)

                name = "respool_%d" % gate
                mcu_pin.setup_max_duration(0.)
                mcu_pin.setup_start_value(start_value, self.shutdown_value)
                self.motor_mcu_pins[name] = mcu_pin
                self.last_value[name] = start_value
                self.respool_gates.append(gate)

            if self.assist_motor_pin and not self._is_empty_pin(self.assist_motor_pin):
                if self.is_pwm:
                    mcu_pin = ppins.setup_pin("pwm", self.assist_motor_pin)
                    mcu_pin.setup_cycle_time(self.cycle_time, self.hardware_pwm)
                else:
                    mcu_pin = ppins.setup_pin("digital_out", self.assist_motor_pin)

                name = "assist_%d" % gate
                mcu_pin.setup_max_duration(0.)
                mcu_pin.setup_start_value(start_value, self.shutdown_value)
                self.motor_mcu_pins[name] = mcu_pin
                self.last_value[name] = start_value
                self.assist_gates.append(gate)

            if self.enable_motor_pin and not self._is_empty_pin(self.enable_motor_pin):
                mcu_pin = ppins.setup_pin("digital_out", self.enable_motor_pin)

                name = "enable_%d" % gate
                mcu_pin.setup_max_duration(0.)
                mcu_pin.setup_start_value(self.last_value, self.shutdown_value)
                self.motor_mcu_pins[name] = mcu_pin
                self.last_value[name] = start_value

            if self.assist_trigger_pin and not self._is_empty_pin(self.assist_trigger_pin):
                buttons.register_buttons(
                    [self.assist_trigger_pin],
                    lambda eventtime, state, gate=gate: self._handle_button_advance(eventtime, state, gate)
                )

            self.operation[self._key(gate)] = ('off', 0)
            self.back_to_back_burst_count[gate] = 0
            self.burst_trigger_state[gate] = 0

        # Setup minimum number of gcode request queues
        self.gcrqs = {}
        for mcu_pin in self.motor_mcu_pins.values():
            mcu = mcu_pin.get_mcu()
            # TODO Temporary workaround to allow Kalico to work since it lacks GCodeRequestQueue
            if hasattr(output_pin, 'GCodeRequestQueue'):
                self.gcrqs.setdefault(mcu, output_pin.GCodeRequestQueue(config, mcu, self._set_pin))
            else:
                self.gcrqs.setdefault(mcu, GCodeRequestQueue(config, mcu, self._set_pin))

        # Setup event handler for DC espooler motor operation
        self.printer.register_event_handler("mmu:espooler_advance", self._handle_espooler_advance)

        # Register event handlers
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.mmu = self.printer.lookup_object('mmu')

        # Setup extruder monitor
        try:
            self.extruder_monitor = self.ExtruderMonitor(self)
        except Exception as e:
            self.mmu.log_error(str(e))
            self.extruder_monitor = None

    def _key(self, gate):
        return '%s_gate_%d' % (self.name, gate)

    def _valid_gate(self, gate):
        return gate is not None and self.first_gate <= gate < self.first_gate + self.num_gates

    def _is_empty_pin(self, pin):
        if pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''

    # Callback from button sensor to initiate burst assist
    def _handle_button_advance(self, eventtime, state, gate):
        self.burst_trigger_state[gate] = state
        if self.mmu and self.mmu.espooler_assist_burst_trigger: # Don't handle if not ready or disabled
            if self.mmu.espooler_assist_burst_trigger and state and gate not in self.burst_gates:
                self.back_to_back_burst_count[gate] += 1
                self.advance(gate)
            elif not state and self.back_to_back_burst_count[gate] >= self.mmu.espooler_assist_burst_trigger_max:
                # Allow future triggers
                self.burst_gates.discard(gate)
                self.back_to_back_burst_count[gate] = 0

    def enable_burst_trigger(self, gate, enable):
        if self._valid_gate(gate):
            cur_enabled = self.burst_trigger_enabled.get(gate, False)
            if not cur_enabled and enable:
                # Turn on and if currently triggered immediately advance
                self.burst_trigger_enabled[gate] = True
                if self.burst_trigger_state.get(gate, 0):
                    self.advance(gate)
            elif cur_enabled and not enable:
                self.burst_trigger_enabled[gate] = False
                # If motor is running because of burst, disable it and prevent pending calls to _reset_burst_trigger turning off the motor unwantedly
                self._reset_burst_trigger(gate, True)


    # This resets burst trigger and repeats burst if sensor still triggered. It is used to cap the
    # back-to-back firing of burst triggers to prevent obvious overruns if sensor is defective
    def _reset_burst_trigger(self, gate, force=False):
        if gate in self.burst_gates:
            self._update(gate, 0, None)
        if not force and self.burst_trigger_state.get(gate, 0):
            # Still triggered
            if self.back_to_back_burst_count[gate] < self.mmu.espooler_assist_burst_trigger_max:
                self.back_to_back_burst_count[gate] += 1
                self.advance(gate)
            else:
                self.mmu.log_error("Espooler assist suspended bcause of suspected malfunction. Assist sensor may be stuck in triggered state")
        else:
            # Allow future triggers
            self.burst_gates.discard(gate)
            self.back_to_back_burst_count[gate] = 0

    # Direct call to initiate burst assist
    def advance(self, gate=None):
        # Advance by "mmu defined" parameters
        self._handle_espooler_advance(gate, self.mmu.espooler_assist_burst_power / 100, self.mmu.espooler_assist_burst_duration)

    # This event will advance the espooler by the power/duration if in "in-print assist" mode
    def _handle_espooler_advance(self, gate, value, duration):
        from .mmu import Mmu # For operation names

        # If gate not specifed, find the active gate (there should only be one)
        gate = (
            gate if gate is not None else
            next(
                (g for g in range(self.first_gate, self.first_gate + self.num_gates)
                 if self.get_operation(g)[0] == Mmu.ESPOOLER_PRINT),
                None
            )
        )

        if self._valid_gate(gate):
            cur_op, cur_value = self.get_operation(gate)
            msg = "Got espooler advance event for gate %d: value=%.2f duration=%.1f" % (gate, value, duration)
            if cur_op == Mmu.ESPOOLER_PRINT and cur_value == 0:
                self.mmu.log_debug(msg)
                self._update(gate, value, None) # On
                self.burst_gates.add(gate) # Should only be one gate at a time but this adds future flexibility
                waketime = self.reactor.monotonic() + duration
                self.reactor.register_callback(lambda pt: self._reset_burst_trigger(gate=gate), waketime) # Schedule off
            else:
                msg += " (Ignored because espooler state is %s, value: %.2f)" % (cur_op, cur_value)
                self.mmu.log_debug(msg)

    # Direct call to change the operation of the espooler
    def set_operation(self, gate, value, operation):
        from .mmu import Mmu # For operation names

        if self.mmu.log_enabled(Mmu.LOG_TRACE):
            self.mmu.log_trace("ESPOOLER: set_operation(gate=%s, value=%s, operation=%s)" % (gate, value, operation))

        # Turn off assist for all gates except specified gate if still wanted
        for g in range(self.first_gate, self.first_gate + self.num_gates):
            if (
                (self.get_operation(g)[0] == Mmu.ESPOOLER_PRINT and g != gate) or
                (g == gate and operation == Mmu.ESPOOLER_PRINT and value != 0)
            ):
                self._update(g, 0, Mmu.ESPOOLER_OFF)

                # Disable all triggers
                if self.mmu.espooler_assist_burst_trigger:
                    self.enable_burst_trigger(g, False)
                if self.extruder_monitor:
                    self.extruder_monitor.watch(False)
                if self.mmu.log_enabled(Mmu.LOG_TRACE):
                    self.mmu.log_trace("ESPOOLER: In-print assist for gate %d canceled" % g)

        if gate is not None and gate >= 0:
            self.mmu.log_debug("Espooler for gate %d set to %s (pwm: %.2f)" % (gate, operation, value))
            self._update(gate, value, operation)

            if operation == Mmu.ESPOOLER_PRINT and value == 0:
                if self.mmu.log_enabled(Mmu.LOG_TRACE):
                    self.mmu.log_trace("ESPOOLER: Entering in-print assist mode for gate %d" % gate)

                # Enable appropriate trigger
                if self.mmu.espooler_assist_burst_trigger:
                    self.enable_burst_trigger(gate, True)
                elif self.extruder_monitor:
                    self.extruder_monitor.watch(True)

    def get_operation(self, gate):
        return self.operation.get(self._key(gate), ('off', 0))

    # Set the PWM or digital signal
    def _update(self, gate, value, operation):
        from .mmu import Mmu # For operation names

        if self.mmu.log_enabled(Mmu.LOG_STEPPER):
            self.mmu.log_stepper("ESPOOLER: _update(%s, %s, %s)" % (gate, value, operation))

        def _schedule_set_pin(name, value):
            mcu_pin = self.motor_mcu_pins.get(name, None)
            if mcu_pin:
                estimated_print_time = mcu_pin.get_mcu().estimated_print_time(self.printer.reactor.monotonic())
                if self.mmu.log_enabled(Mmu.LOG_STEPPER):
                    self.mmu.log_stepper("ESPOOLER: --> _schedule_set_pin(name=%s, value=%s) @ print_time: %.8f" % (name, value, estimated_print_time))
                self.gcrqs[mcu_pin.get_mcu()].send_async_request((name, value))

        if operation == Mmu.ESPOOLER_OFF:
            value = 0

        # Clamp and scale value
        value = max(0, min(1, value)) / self.scale
        if not self.is_pwm:
            value = 1 if value > 0 else 0

        # None operation is special case of updating without changing operation (typically in-print assist burst)
        if operation == None or self.get_operation(gate) != (operation, value):
            if value == 0: # Stop motor
                _schedule_set_pin('enable_%d' % gate, 0)
                _schedule_set_pin('respool_%d' % gate, 0)
                _schedule_set_pin('assist_%d' % gate, 0)
            else:
                active_motor_name = 'respool_%d' % gate if operation == Mmu.ESPOOLER_REWIND else 'assist_%d' % gate
                inactive_motor_name = 'assist_%d' % gate if operation == Mmu.ESPOOLER_REWIND else 'respool_%d' % gate
                _schedule_set_pin(inactive_motor_name, 0)
                _schedule_set_pin(active_motor_name, value)
                _schedule_set_pin('enable_%d' % gate, 1)

            # Don't change the operation if it is just an in-print assist burst move.
            # If we would change operation, any MMU operation calling the get_operation(gate) method while the motor is performing a burst move
            # (e. g. a runout which is wrapped by wrap_sync_gear_to_extruder()) would get the burst pwm value instead of 0
            if operation != None:
                self.operation[self._key(gate)] = (operation, value)

    # This is the actual callback method to update pin signal (pwm or digital)
    def _set_pin(self, print_time, action):
        from .mmu import Mmu # For operation names

        name, value = action
        mcu_pin = self.motor_mcu_pins.get(name, None)
        if mcu_pin:
            if value == self.last_value.get(name, None):
                return
            if self.mmu.log_enabled(Mmu.LOG_STEPPER):
                self.mmu.log_stepper("ESPOOLER: -----> _set_pin(name=%s, value=%s) @ print_time: %.8f" % (name, value, print_time))
            if self.is_pwm and not name.startswith('enable_'):
                mcu_pin.set_pwm(print_time, value)
            else:
                mcu_pin.set_digital(print_time, value)
            self.last_value[name] = value

    def get_status(self, eventtime):
        return {
            'name': self.name,
            'first_gate': self.first_gate,
            'num_gates': self.num_gates,
            'respool_gates': self.respool_gates,
            'assist_gates': self.assist_gates
        }


    # Class to monitor extruder movement an generate espooler "advance" events
    class ExtruderMonitor:

        CHECK_MOVEMENT_PERIOD = 1. # How often to check extruder movement

        def __init__(self, espooler):
            self.espooler = espooler
            self.reactor = espooler.reactor
            self.estimated_print_time = espooler.printer.lookup_object('mcu').estimated_print_time
            self.extruder = espooler.printer.lookup_object(espooler.mmu.extruder_name, None)
            if not self.extruder:
                raise espooler.config.error("Extruder named `%s` not found. Espooler extruder monitor disabled" % espooler.mmu.extruder_name)

            self.enabled = False
            self.last_extruder_pos = None
            self._extruder_pos_update_timer = self.reactor.register_timer(self._extruder_pos_update_event)

        def watch(self, enable):
            if not self.enabled and enable:
                # Ensure first burst after initial extruder movement
                self.last_extruder_pos = self._get_extruder_pos() - self.espooler.mmu.espooler_assist_extruder_move_length + 1.
                self.enabled = True
                self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NOW) # Enabled
            elif not enable:
                self.last_extruder_pos = None
                self.enabled = False
                self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NEVER) # Disabled

        def _get_extruder_pos(self, eventtime=None):
            if eventtime is None:
                eventtime = self.reactor.monotonic()
            print_time = self.estimated_print_time(eventtime)
            if self.extruder:
                pos = self.extruder.find_past_position(print_time)
                return pos
            else:
                return 0.

        # Called periodically to check extruder movement
        def _extruder_pos_update_event(self, eventtime):
            extruder_pos = self._get_extruder_pos(eventtime)
            #self.espooler.mmu.log_trace("TEMP: current_extruder_pos: %s (last: %s)" % (extruder_pos, self.last_extruder_pos))
            if self.last_extruder_pos is not None and extruder_pos > self.last_extruder_pos + self.espooler.mmu.espooler_assist_extruder_move_length:
                self.espooler.advance() # Initiate burst
                self.last_extruder_pos = extruder_pos
            return eventtime + self.CHECK_MOVEMENT_PERIOD

def load_config_prefix(config):
    return MmuESpooler(config)


######################################################################
# G-Code request queuing helper
# This is included to allow Kalico to work since it has not yet picked
# up this klipper functionality 4/18/25
# Copyright (C) 2017-2024  Kevin O'Connor <kevin@koconnor.net>
######################################################################

PIN_MIN_TIME = 0.100

# Helper code to queue g-code requests
class GCodeRequestQueue:
    def __init__(self, config, mcu, callback):
        self.printer = printer = config.get_printer()
        self.mcu = mcu
        self.callback = callback
        self.rqueue = []
        self.next_min_flush_time = 0.
        self.toolhead = None
        mcu.register_flush_callback(self._flush_notification)
        printer.register_event_handler("klippy:connect", self._handle_connect)
    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
    def _flush_notification(self, print_time, clock):
        rqueue = self.rqueue
        while rqueue:
            next_time = max(rqueue[0][0], self.next_min_flush_time)
            if next_time > print_time:
                return
            # Skip requests that have been overridden with a following request
            pos = 0
            while pos + 1 < len(rqueue) and rqueue[pos + 1][0] <= next_time:
                pos += 1
            req_pt, req_val = rqueue[pos]
            # Invoke callback for the request
            min_wait = 0.
            ret = self.callback(next_time, req_val)
            if ret is not None:
                # Handle special cases
                action, min_wait = ret
                if action == "discard":
                    del rqueue[:pos+1]
                    continue
                if action == "delay":
                    pos -= 1
            del rqueue[:pos+1]
            self.next_min_flush_time = next_time + max(min_wait, PIN_MIN_TIME)
            # Ensure following queue items are flushed
            self.toolhead.note_mcu_movequeue_activity(self.next_min_flush_time)
    def _queue_request(self, print_time, value):
        self.rqueue.append((print_time, value))
        self.toolhead.note_mcu_movequeue_activity(print_time)
    def queue_gcode_request(self, value):
        self.toolhead.register_lookahead_callback(
            (lambda pt: self._queue_request(pt, value)))
    def send_async_request(self, value, print_time=None):
        if print_time is None:
            systime = self.printer.get_reactor().monotonic()
            print_time = self.mcu.estimated_print_time(systime + PIN_MIN_TIME)
        while 1:
            next_time = max(print_time, self.next_min_flush_time)
            # Invoke callback for the request
            action, min_wait = "normal", 0.
            ret = self.callback(next_time, value)
            if ret is not None:
                # Handle special cases
                action, min_wait = ret
                if action == "discard":
                    break
            self.next_min_flush_time = next_time + max(min_wait, PIN_MIN_TIME)
            if action != "delay":
                break
