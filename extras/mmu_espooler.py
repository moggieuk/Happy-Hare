# Happy Hare MMU Software
#
# Implements h/w "eSpooler" control for a MMU unit that is powered by a DC motor
# (normally PWM speed controlled) that can be used to rewind a filament spool or be
# driven peridically in the forward direction to provide "forward assist" functionality.
# For simplicity of setup it is assumed that all pins are of the same type/config per mmu_unit.
# Control is via direct control or klipper events.
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
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
        self.mmu = None
        self.respool_gates = []       # List of gates that can perform respool operation
        self.assist_gates = []        # List of gates that can perform assist operation
        self.burst_gates = {}         # Key:Gate, Value:(operation, callback_timer) for gates currently executing a "burst"

        # The following implement the "burst assist". Currently only the print_assist_gate has burst_trigger_enabled
        # but the orthogonal indicators would allow for future change in behavior
        self.print_assist_gate = None      # Current gate in "print assist" mode (should only be one or None)
        self.burst_trigger_enabled = {}    # Key: Gate, Value: True|False representing if trigger is enabled for each gate
        self.burst_trigger_state = {}      # Key: Gate, Value: 0|1 trigger button state for each gate trigger
        self.back_to_back_burst_count = {} # Key: Gate, Value: Count of back-to-back bursts for each gate

        # Get config
        self.motor_mcu_pins = {} # Key: pin_name, Value: mcu_pin
        self.last_value = {}     # Key: pin_name, Value: Last pwm value
        self.operation = {}      # Key: Gate, Value: (operation, pwm_value) tuple
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

            valid_gate = False # This is hack to support HHv3 (remove in HHv4)
            # Setup pins
            if self.respool_motor_pin and not self._is_empty_pin(self.respool_motor_pin):
                if self.is_pwm:
                    mcu_pin = ppins.setup_pin("pwm", self.respool_motor_pin)
                    mcu_pin.setup_cycle_time(self.cycle_time, self.hardware_pwm)
                else:
                    mcu_pin = ppins.setup_pin("digital_out", self.respool_motor_pin)

                valid_gate = True
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

                valid_gate = True
                name = "assist_%d" % gate
                mcu_pin.setup_max_duration(0.)
                mcu_pin.setup_start_value(start_value, self.shutdown_value)
                self.motor_mcu_pins[name] = mcu_pin
                self.last_value[name] = start_value
                self.assist_gates.append(gate)

            if self.enable_motor_pin and not self._is_empty_pin(self.enable_motor_pin):
                mcu_pin = ppins.setup_pin("digital_out", self.enable_motor_pin)

                valid_gate = True
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

            if valid_gate:
                from .mmu import Mmu  # For operation names
                self.operation[gate] = (Mmu.ESPOOLER_OFF, 0)
                self.back_to_back_burst_count[gate] = 0
                self.burst_trigger_state[gate] = 0
            else:
                # Hack to support on HH v3
                self.num_gates = gate + 1
                break

        # Setup minimum number of gcode request queues
        self.gcrqs = {}
        for mcu_pin in self.motor_mcu_pins.values():
            mcu = mcu_pin.get_mcu()
            # TODO Temporary workaround to allow Kalico to work since it lacks GCodeRequestQueue
            if hasattr(output_pin, 'GCodeRequestQueue'):
                self.gcrqs.setdefault(mcu, output_pin.GCodeRequestQueue(config, mcu, self._set_pin))
            else:
                self.gcrqs.setdefault(mcu, GCodeRequestQueue(config, mcu, self._set_pin))

        # Setup event handler for DC espooler motor burst operation
        self.printer.register_event_handler("mmu:espooler_burst", self._handle_espooler_burst)
        self.printer.register_event_handler("mmu:disabled", self._handle_mmu_disabled)

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


    def _handle_mmu_disabled(self):
        """
        Event indicating that the MMU unit was disabled. Make sure the espooler triggers are disabled
        """
        self.reset_print_assist_mode()


    def _valid_gate(self, gate):
        return gate is not None and self.first_gate <= gate < self.first_gate + self.num_gates


    def _is_empty_pin(self, pin):
        if pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''


    def _handle_button_advance(self, eventtime, state, gate):
        """
        Callback from button sensor to initiate burst assist
        """
        self.mmu.log_trace("ESPOOLER: Trigger fired for gate %d, state=%s" % (gate, state))
        self.burst_trigger_state[gate] = state
        if self.mmu and self.mmu.espooler_assist_burst_trigger and self.burst_trigger_enabled.get(gate, False): # Don't handle if not ready or disabled
            if self.mmu.espooler_assist_burst_trigger and state:
                self.back_to_back_burst_count[gate] += 1
                self.advance(gate)
            else:
                # Allow future triggers
                self.back_to_back_burst_count[gate] = 0


    def _set_burst_trigger_enable(self, gate, enable):
        from .mmu import Mmu  # For operation names

        if self._valid_gate(gate):
            cur_enabled = self.burst_trigger_enabled.get(gate, False)
            if not cur_enabled and enable:
                # Turn on and if currently triggered immediately advance
                self.burst_trigger_enabled[gate] = True
                if self.burst_trigger_state.get(gate, 0):
                    self.advance(gate)
            elif cur_enabled and not enable:
                self.burst_trigger_enabled[gate] = False
                self.back_to_back_burst_count[gate] = 0
                # Turn off espooler can cancel any burst timer
                self.set_operation(gate, 0, Mmu.ESPOOLER_OFF)


    # Logic to handle a short "jog" rotation in assist or rewind direction ----------------------------------------------

    def advance(self, gate=None):
        """
        Direct call to initiate in print burst assist.
        If called with gate=None then it is likely from the extruder monitor so apply to the
        gate in "print assist" mode
        """
        from .mmu import Mmu  # For operation names

        if gate is None:
            gate = self.print_assist_gate

        if gate is None:
            self.mmu.log_trace("ESPOOLER: In print assist advance() called but no gate in 'print' mode (ignored)")
            return

        self.burst(gate, Mmu.ESPOOLER_ASSIST)

    def burst(self, gate, operation):
        """
        Direct call to rotate spool in a burst defined by operation (ESPOOLER_ASSIST or ESPOOLER_REWIND).
        The burst will automatically be terminated after configured rotate parameters
        (used in spool drying rotation, filament tightening and manual jogging)
        """
        from .mmu import Mmu  # For operation names

        if operation == Mmu.ESPOOLER_ASSIST:
            power = self.mmu.espooler_assist_burst_power
            duration = self.mmu.espooler_assist_burst_duration
        elif operation == Mmu.ESPOOLER_REWIND:
            power = self.mmu.espooler_rewind_burst_power
            duration = self.mmu.espooler_rewind_burst_duration
        else:
            return
   
        self._handle_espooler_burst(gate, power / 100, duration, operation)


    def _handle_espooler_burst(self, gate, value, duration, operation):
        """
        Rotate burst: short jog movement of spool in selected direction
        - Only allowed when gate is ESPOOLER_OFF or ESPOOLER_PRINT.
        - While active for a gate, no other operations for that gate are allowed.
        - Stops via scheduled callback at end of duration or manual ESPOOLER_OFF.
        """
        from .mmu import Mmu  # For operation names

        if not self._valid_gate(gate):
            return

        # Per-gate lock: ignore if this gate is already in a rotation burst
        if gate in self.burst_gates:
            self.mmu.log_debug("Got espooler burst event for gate %d but burst already active (ignored)" % gate)
            return

        if duration <= 0 or value == 0:
            self.mmu.log_debug("Got bad espooler burst event for gate %d: duration=%.1f, value=%.1f (ignored)" % (gate, duration, value))
            return

        # Only allowed if not moving (OFF or PRINT) but always allow interuption of in-print assist gate
        cur_op, cur_value = self.get_operation(gate)
        msg = "ESPOOLER: Got espooler rotate event for gate %d: value=%.2f duration=%.1f" % (gate, value, duration)
        if cur_op in [Mmu.ESPOOLER_OFF, Mmu.ESPOOLER_PRINT] or gate == self.print_assist_gate:
            self.mmu.log_trace(msg)

            # Schedule future return to ESPOOLER_OFF / ESPOOLER_PRINT state
            waketime = self.reactor.monotonic() + duration
            timer = self.reactor.register_timer(lambda pt: self._stop_espooler_burst(gate=gate), waketime)

            # Take per-gate lock and start rewind motor
            self.set_operation(gate, value, operation)
            self.burst_gates[gate] = (operation, timer)
        else:
            msg += " (Ignored because espooler state is %s, value: %.2f)" % (cur_op, cur_value)
            self.mmu.log_trace(msg)


    def _stop_espooler_burst(self, gate):
        """
        Scheduled (timer event) callback to terminate an espooler burst
        """
        from .mmu import Mmu  # For operation names
        operation = Mmu.ESPOOLER_PRINT if gate == self.print_assist_gate else Mmu.ESPOOLER_OFF
        if gate in self.burst_gates:
            self.set_operation(gate, 0, operation) # This will call _dequeue_espooler_burst()

        # Monitor triggers for print assist gate
        if gate == self.print_assist_gate:
            if self.burst_trigger_state.get(gate, 0):
                # Still triggered
                if self.back_to_back_burst_count[gate] < self.mmu.espooler_assist_burst_trigger_max:
                    self.back_to_back_burst_count[gate] += 1
                    self.advance(gate)
                else:
                    self.mmu.log_error("Espooler assist temporarily suspended because of suspected malfunction. Assist trigger sensor may be stuck in triggered state")
            else:
                # Trigger has cleared, allow future triggers
                self.back_to_back_burst_count[gate] = 0

        return self.reactor.NEVER # This is setup as a one-shot timer (so early cancellation is possible)


    def _dequeue_espooler_burst(self, gate):
        """
        Cancel "espooler off" callback and remove from burst list. Send completion event
        """
        # Cancel callback and remove from burst list
        if gate in self.burst_gates:
            timer = self.burst_gates[gate][1]
            try:
                self.reactor.unregister_timer(timer)
            except Exception as e:
                self.mmu.log_debug("Error cancelling burst callback: Exception: %s" % str(e))
            del self.burst_gates[gate]

            # Notify listeners
            self.printer.send_event("mmu:espooler_burst_done", gate)


    def set_print_assist_mode(self, gate):
        """
        Efficient method to turn on in-print assist
        """
        from .mmu import Mmu # For operation names
        if self.print_assist_gate != gate:
           self.set_operation(gate, 0, Mmu.ESPOOLER_PRINT)


    def reset_print_assist_mode(self):
        """
        Reset any gate in the sticky "in-print assist" mode. This is called a lot so should be efficient
        """
        from .mmu import Mmu # For operation names
        pg = self.print_assist_gate
        if pg is not None:
            self.print_assist_gate = None
            self.mmu.log_trace("ESPOOLER: Cancelling in-print assist for gate %d" % pg)
            self._update_pwm(pg, 0, Mmu.ESPOOLER_OFF)
            self.operation[pg] = (Mmu.ESPOOLER_OFF, 0)

            # Disable all triggers
            if self.mmu.espooler_assist_burst_trigger:
                self._set_burst_trigger_enable(pg, False)
            if self.extruder_monitor:
                self.extruder_monitor.watch(False)

            self._dequeue_espooler_burst(pg)


    # Change operation in progress and DC motor PWM control -------------------------------------------------------------

    def set_operation(self, gate, value, operation):
        """
        Direct call to change the operation of the espooler and adjust DC motor
        Operations are:
          ESPOOLER_OFF    = Force motor off
          ESPOOLER_REWIND = Set motor in rewind (retract) direction
          ESPOOLER_ASSIST = Set motor in forward (assist) direction
          ESPOOLER_PRINT  = Set stick "in-print assist" mode and clear former gate in that mode
        """
        from .mmu import Mmu # For operation names

        # To aid debugging...
        if self.mmu.log_enabled(Mmu.LOG_TRACE):
            self.mmu.log_trace("ESPOOLER: set_operation(gate=%s, value=%s, operation=%s)" % (gate, value, operation))

        gates = [gate]
        if gate is None:
            gates = range(self.first_gate, self.first_gate + self.num_gates)

        for g in gates:
            if not self._valid_gate(g):
                self.mmu.log_trace("ESPOOLER: Trying to set Espooler operation of illegal gate %d (ignored)" % g)
                continue

            cur_op, cur_value = self.get_operation(g)

            # OFF ----------------------------------------
            if operation == Mmu.ESPOOLER_OFF:
                # Always update PWM as safety precaution
                self._update_pwm(g, 0, Mmu.ESPOOLER_OFF)

                if g != self.print_assist_gate:
                    self.operation[g] = (Mmu.ESPOOLER_OFF, 0)
                else:
                    self.operation[g] = (Mmu.ESPOOLER_PRINT, 0)

                if cur_op != Mmu.ESPOOLER_OFF:
                    self.mmu.log_debug("Espooler for gate %d turned off" % g)

                # Ensure any existing burst is canceled
                self._dequeue_espooler_burst(g)

            # ASSIST or REWIND ---------------------------
            elif operation in [Mmu.ESPOOLER_ASSIST, Mmu.ESPOOLER_REWIND]:
                if cur_op not in [Mmu.ESPOOLER_OFF, Mmu.ESPOOLER_PRINT]:
                    # Stop PWM before sending new
                    self._update_pwm(g, 0, operation)

                conf_gates = self.assist_gates if operation == Mmu.ESPOOLER_ASSIST else self.respool_gates
                if g in conf_gates:
                    self._update_pwm(g, value, operation)
                    self.operation[g] = (operation, value)
                    self.mmu.log_debug("Espooler for gate %d set to %s (pwm: %.2f)" % (g, operation, value))
                else:
                    self.mmu.log_debug("Espooler for gate %d not configured to perform %s operation" % (g, operation))

                # Ensure any existing burst is canceled
                self._dequeue_espooler_burst(g)

            # SPECIAL PRINT ASSIST MODE ------------------
            elif operation == Mmu.ESPOOLER_PRINT:
                # Practically this will only be called on a single gate at a time
                self.mmu.log_trace("ESPOOLER: Entering in-print assist mode for gate %d" % g)

                # Only one gate can be in print assist mode at a time so clear previous
                if g != self.print_assist_gate and self.print_assist_gate is not None:
                    self.reset_print_assist_mode()

                # Only set sticky in-print trigger mode if pwm value is 0, else ignore
                if value == 0:
                    self.print_assist_gate = g

                    self._update_pwm(g, 0, operation)
                    self.operation[g] = (operation, 0)

                    # Enable appropriate triggers
                    if self.mmu.espooler_assist_burst_trigger:
                        self._set_burst_trigger_enable(g, True)
                    elif self.extruder_monitor:
                        self.extruder_monitor.watch(True)

                    self.mmu.log_debug("Espooler for gate %d set to %s (pwm: %.2f)" % (g, operation, value))

                    # Ensure any existing burst is canceled
                    self._dequeue_espooler_burst(g)


    def get_operation(self, gate):
        """
        Return tuple of current operation and pwm value for gate
        """
        from .mmu import Mmu # For operation names
        return self.operation.get(gate, (Mmu.ESPOOLER_OFF, 0))


    def _update_pwm(self, gate, value, operation):
        """
        Set the PWM or digital signal for espooler on gate
        The operation is used to assertain motor direction
        """
        from .mmu import Mmu # For operation names

        if self.mmu.log_enabled(Mmu.LOG_STEPPER):
            self.mmu.log_stepper("ESPOOLER: _update_pwm(%s, %s, %s)" % (gate, value, operation))

        def _schedule_set_pin(name, value):
            mcu_pin = self.motor_mcu_pins.get(name, None)
            if mcu_pin:
                estimated_print_time = mcu_pin.get_mcu().estimated_print_time(self.printer.reactor.monotonic())
                if self.mmu.log_enabled(Mmu.LOG_STEPPER):
                    self.mmu.log_stepper("ESPOOLER: --> _schedule_set_pin(name=%s, value=%s) @ print_time: %.8f" % (name, value, estimated_print_time))
                self.gcrqs[mcu_pin.get_mcu()].send_async_request((name, value))

        # Sanity check
        if operation == Mmu.ESPOOLER_OFF:
            value = 0

        # Clamp and scale value
        value = max(0, min(1, value)) / self.scale
        if not self.is_pwm:
            value = 1 if value > 0 else 0

        # Update PWM signal
        if self.get_operation(gate) != (operation, value):
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


    # -------------------------------------------------------------------------------------------------------------------

    def get_status(self, eventtime):
        return {
            'espooler': [v[0] for v in self.operation.values()]
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
            #self.espooler.mmu.log_trace("ESPOOLER: current_extruder_pos: %s (last: %s)" % (extruder_pos, self.last_extruder_pos))
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
