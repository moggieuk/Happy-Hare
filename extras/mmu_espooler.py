# Happy Hare MMU Software
#
# Implements h/w "eSpooler" control for a MMU unit that is powered by a DC motor
# (normally PWM speed controlled) that can be used to rewind a filament spool or be
# driven peridically in the forward direction to provide "forward assist" functionality.
# For simplicity of setup it is assumed that all pins are of the same type/config per mmu unit.
# Control is via klipper events.
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

MAX_SCHEDULE_TIME = 5.0

class MmuESpooler:

    def __init__(self, config, *args):
        if len(args) < 2:
            raise config.error("[%s] cannot be instantiated directly. It must be laoded by [mmu_unit]" % config.get_name())
        self.first_gate, self.num_gates = args
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.respool_gates = []
        self.assist_gates = []

        # Get config
        self.motor_mcu_pins = {}
        self.last_value = {}
        self.operation = {}
        ppins = self.printer.lookup_object('pins')

        # These params are assumed to be shared accross the MMU unit
        self.is_pwm = config.getboolean("pwm", True)
        self.hardware_pwm = config.getboolean("hardware_pwm", False)
        self.scale = config.getfloat('scale', 1., above=0.)
        self.cycle_time = config.getfloat("cycle_time", 0.100, above=0., maxval=MAX_SCHEDULE_TIME)
        self.shutdown_value = config.getfloat('shutdown_value', 0., minval=0., maxval=self.scale) / self.scale
        start_value = config.getfloat('value', 0., minval=0., maxval=self.scale) / self.scale # Starting value

        for gate in range(self.first_gate, self.first_gate + self.num_gates):
            self.respool_motor_pin = config.get('respool_motor_pin_%d' % gate, None)
            self.assist_motor_pin = config.get('assist_motor_pin_%d' % gate, None)
            self.enable_motor_pin = config.get('enable_motor_pin_%d' % gate, None) # AFC MCU only

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

            self.operation[self._key(gate)] = ('off', 0)


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

    def _is_empty_pin(self, pin):
        if pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''

    # This event will advance the espooler by the power/duration if in "in-print assist" mode
    def _handle_espooler_advance(self, gate, value, duration):
        from .mmu import Mmu # For operation names

        # If gate not specifed, find the (first) active gate
        gate = (
            gate if gate is not None else 
            next(
                (g for g in range(self.first_gate, self.first_gate + self.num_gates) 
                 if self.operation[self._key(g)][0] == Mmu.ESPOOLER_PRINT), 
                None
            )
        )

        if gate is not None:
            cur_op, cur_value = self.operation[self._key(gate)]
            msg = "Got espooler advance event for gate %d: value=%.2f duration=%.1f" % (gate, value, duration)
            if cur_op == Mmu.ESPOOLER_PRINT and cur_value == 0:
                print_time = self.toolhead.get_last_move_time()
                self.update(gate, value, None, print_time=print_time) # On
                self.update(gate, 0, None, print_time=print_time + duration)        # Off
            else:
                msg += " (Ignored because espooler state is %s, value: %.2f)" % (cur_op, cur_value)
            self.mmu.log_debug(msg)

    def advance(self):
        # Advance by "mmu defined" parameters
        self._handle_espooler_advance(None, self.mmu.espooler_assist_burst_power / 100, self.mmu.espooler_assist_burst_duration)

    # Set the PWM or digital signal
    def update(self, gate, value, operation, print_time=None):
        from .mmu import Mmu # For operation names

        # None operation is special case of updating without changing operation (typically end of in-print assist burst)
        if operation is None:
            operation = self.operation[self._key(gate)][0]
        else:
            if operation == Mmu.ESPOOLER_OFF:
                value = 0
            if operation == Mmu.ESPOOLER_PRINT and value == 0.:
                # Only allow bursts if default "in-print assist" power is 0
                if self.extruder_monitor:
                    self.extruder_monitor.watch(True)
            else:
                if self.extruder_monitor:
                    self.extruder_monitor.watch(False)

        self.operation[self._key(gate)] = (operation, value)

        def _schedule_set_pin(name, value, print_time=None):
            mcu_pin = self.motor_mcu_pins.get(name, None)
            if mcu_pin:
                if print_time:
                    self._set_pin(print_time, name, value)
                else:
                    self.toolhead.register_lookahead_callback(lambda print_time: self._set_pin(print_time, name, value))

        value /= self.scale
        if not self.is_pwm:
            value = 1 if value > 0 else 0
       
        if value == 0: # Stop motor
            _schedule_set_pin('respool_%d' % gate, 0, print_time)
            _schedule_set_pin('assist_%d' % gate, 0, print_time)
            _schedule_set_pin('enable_%d' % gate, 0, print_time)
        else:
            active_motor_name = 'respool_%d' % gate if operation == Mmu.ESPOOLER_REWIND else 'assist_%d' % gate
            inactive_motor_name = 'assist_%d' % gate if operation == Mmu.ESPOOLER_REWIND else 'respool_%d' % gate
            _schedule_set_pin(inactive_motor_name, 0, print_time)
            _schedule_set_pin(active_motor_name, value, print_time)
            _schedule_set_pin('enable_%d' % gate, 1, print_time)

    # This is the actual callback method to update pin signal (pwm or digital)
    def _set_pin(self, print_time, name, value):
        mcu_pin = self.motor_mcu_pins.get(name, None)
        if mcu_pin:
            if value == self.last_value.get(name, None):
                return
        if self.is_pwm and not name.startswith('enable_'):
            mcu_pin.set_pwm(print_time, value)
        else:
            mcu_pin.set_digital(print_time, value)
        self.last_value[name] = value

    def get_operation(self, gate):
        return self.operation.get(self._key(gate), ('', 0))

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

        CHECK_MOVEMENT_TIMEOUT = 1. # How often to check extruder movement

        def __init__(self, espooler):
            self.espooler = espooler
            self.reactor = espooler.printer.get_reactor()
            self.estimated_print_time = espooler.printer.lookup_object('mcu').estimated_print_time
            self.extruder = espooler.printer.lookup_object(espooler.mmu.extruder_name, None)
            if not self.extruder:
                raise espooler.config.error("Extruder named `%s` not found. Espooler extruder monitor disabled" % espooler.mmu.extruder_name)

            self.enabled = False
            self.last_extruder_pos = None
            self._extruder_pos_update_timer = self.reactor.register_timer(self._extruder_pos_update_event)

        def watch(self, enable):
            if not self.enabled and enable:
                self.last_extruder_pos = None
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
                return self.extruder.find_past_position(print_time)
            else:
                return 0.

        # Called periodically to check extruder movement
        def _extruder_pos_update_event(self, eventtime):
            extruder_pos = self._get_extruder_pos(eventtime)
            if self.last_extruder_pos is None or extruder_pos > self.last_extruder_pos + self.espooler.mmu.espooler_assist_extruder_move_length:
                self.espooler.advance() # Initiate burst
                self.last_extruder_pos = extruder_pos
            return eventtime + self.CHECK_MOVEMENT_TIMEOUT

def load_config_prefix(config):
    return MmuESpooler(config)
