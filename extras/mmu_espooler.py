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

    def __init__(self, config, first_gate=0):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.first_gate = first_gate

        # Get config
        self.mcu_motor_pin = {}
        self.last_value = {}
        self.operation = {}
        ppins = self.printer.lookup_object('pins')

        # These params are assumed to be shared accross the MMU unit
        self.is_pwm = config.getboolean("pwm", True)
        self.hardware_pwm = config.getboolean("hardware_pwm", False)
        self.scale = config.getfloat('scale', 1., above=0.)
        self.cycle_time = config.getfloat("cycle_time", 0.100, above=0., maxval=MAX_SCHEDULE_TIME)

        for i in range(23):
            gate = first_gate + i
            self.respool_motor_pin = config.get('respool_motor_pin_%d' % gate, None)
            self.assist_motor_pin = config.get('assist_motor_pin_%d' % gate, None)
            self.enable_motor_pin = config.get('enable_motor_pin_%d' % gate, None) # AFC MCU only

            # Setup pins
            if self.is_pwm:
                if self.respool_motor_pin:
                    logging.info("PAUL respool_pin")
                    mcu_pin = ppins.setup_pin("pwm", self.respool_motor_pin)
                    mcu_pin.setup_cycle_time(cycle_time, hardware_pwm)
                    mcu_pin.setup_max_duration(0.)
                    self.motor_mcu_pins['respool_%d' % gate] = mcu_pin

                if self.assist_motor_pin:
                    logging.info("PAUL assist_pin")
                    mcu_pin = ppins.setup_pin("pwm", self.assist_motor_pin)
                    mcu_pin.setup_cycle_time(cycle_time, hardware_pwm)
                    mcu_pin.setup_max_duration(0.)
                    self.motor_mcu_pins['assist_%d' % gate] = mcu_pin
            else:
                if self.respool_motor_pin:
                    mcu_pin = ppins.setup_pin("digital_out", self.respool_motor_pin)
                    mcu_pin.setup_max_duration(0.)
                    self.motor_mcu_pins['respool_%d' % gate] = mcu_pin

                if self.assist_motor_pin:
                    mcu_pin = ppins.setup_pin("digital_out", self.assist_motor_pin)
                    mcu_pin.setup_max_duration(0.)
                    self.motor_mcu_pins['assist_%d' % gate] = mcu_pin

            if self.enable_motor_pin:
                logging.info("PAUL enable_pin")
                mcu_pin = ppins.setup_pin("digital_out", self.enable_motor_pin)
                mcu_pin.setup_max_duration(0.)
                self.motor_mcu_pins['enable_%d' % gate] = mcu_pin
 
            self.operation['%s_gate_%d' % (self.name, gate)] = ('stop', 0)

        # Setup event handler for DC espooler motor operation
        self.printer.register_event_handler("mmu:espooler", self._handle_espooler_request)

    def _handle_espooler_request(self, eventtime, gate, value, operation):
         logging.info("Got espooler '%s' event for gate %d: value=%.2f" % (operation, gate, value))
         self.operation['%s_gate_%d' % (self.name, gate)] = (operation, value)
         self.operation = list(self.operation) # For moonraker visibility
         self._update_espooler(eventtime, gate, value)

    # Set the PWM or digital signal (-ve value is assume to be respool, +ve is assist, 0 is stop)
    def _update_espooler(eventtime, gate, value):
        toolhead = self.printer.lookup_object('toolhead') # PAUL is the correct toolhead should it be mmu_toolhead?

        def _schedule_set_pin(name, value):
            mcu_pin = mcu_motor_pin.get(name, None)
            if mcu_pin:
                #toolhead.register_lookahead_callback(lambda print_time: self._set_pin(print_time, name, value))
                toolhead.register_callback(lambda print_time: self._set_pin(print_time, name, value))

        value /= self.scale
        if not self.is_pwm:
            value = 1 if value > 0 else -1 if value < 0 else 0
       
        if value == 0: # Stop motor
            _schedule_set_pin('respool_%d' % gate, 0)
            _schedule_set_pin('assist_%d' % gate, 0)
            _schedule_set_pin('enable_%d' % gate, 0)
        else:
            active_motor_name = 'assist_%d' % gate if value > 0 else 'respool_%d' % gate
            inactive_motor_name = 'respool_%d' % gate if value > 0 else 'assist_%d' % gate
            _schedule_set_pin(inactive_motor_name, 0)
            _schedule_set_pin(active_motor_name, value)
            _schedule_set_pin('enable_%d' % gate, 1)

    # This is the actual callback method to update pin signal
    def _set_pin(self, print_time, name, value):
        mcu_pin = mcu_motor_pin.get(name, None)
        if mcu_pin:
            if value == self.last_value.get(name, None):
                return
        if self.is_pwm:
            mcu_pin.set_pwm(print_time, value)
        else:
            mcu_pin.set_digital(print_time, value)
        self.last_value[name] = value

    def get_operation(self, gate):
        return self.operation.get('%s_gate_%d' % (self.name, gate), ('', 0))

    def get_status(self, eventtime):
        return self.operation

def load_config_prefix(config):
    return MmuESpooler(config)
