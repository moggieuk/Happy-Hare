# Happy Hare MMU Software
# Custom servo support that carefully synchronizes PWM changes to avoid "kickback" caused
# by a truncated final pulse with digital servos.
# All existing servo functionality is available with the addition of a 'DURATION'
# parameter for setting PWM pulse train with auto off
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Based on original servo.py Copyright (C) 2017-2020  Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

SERVO_SIGNAL_PERIOD = 0.020
PIN_MIN_TIME = 0.100

class MmuServo:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.min_width = config.getfloat('minimum_pulse_width', .001, above=0., below=SERVO_SIGNAL_PERIOD)
        self.max_width = config.getfloat('maximum_pulse_width', .002, above=self.min_width, below=SERVO_SIGNAL_PERIOD)
        self.max_angle = config.getfloat('maximum_servo_angle', 180.)
        self.angle_to_width = (self.max_width - self.min_width) / self.max_angle
        self.width_to_value = 1. / SERVO_SIGNAL_PERIOD
        self.last_value = self.last_value_time = 0.
        initial_pwm = 0.
        iangle = config.getfloat('initial_angle', None, minval=0., maxval=360.)
        if iangle is not None:
            initial_pwm = self._get_pwm_from_angle(iangle)
        else:
            iwidth = config.getfloat('initial_pulse_width', 0., minval=0., maxval=self.max_width)
            initial_pwm = self._get_pwm_from_pulse_width(iwidth)
        self.last_value = initial_pwm

        # 50% of the "off" period is the best place to change PWM signal
        self.pwm_period_safe_offset = SERVO_SIGNAL_PERIOD - (SERVO_SIGNAL_PERIOD - self.max_width) / 2

        # Setup mcu_servo pin
        ppins = self.printer.lookup_object('pins')
        self.mcu_servo = ppins.setup_pin('pwm', config.get('pin'))
        self.mcu_servo.setup_max_duration(0.)
        self.mcu_servo.setup_cycle_time(SERVO_SIGNAL_PERIOD)
        self.mcu_servo.setup_start_value(initial_pwm, 0.)

        # Register command
        servo_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("SET_SERVO", "SERVO", servo_name, self.cmd_SET_SERVO, desc=self.cmd_SET_SERVO_help)

    def get_status(self, eventtime):
        return {'value': self.last_value}

    def _set_pwm(self, print_time, value, duration):
        if value == self.last_value:
            return

        print_time = max(print_time, self.last_value_time + PIN_MIN_TIME)
        pwm_start_time = self._get_synced_print_time(print_time)
        if duration is None:
            self.mcu_servo.set_pwm(pwm_start_time, value)
            self.last_value = value
            self.last_value_time = pwm_start_time
        else:
            # Translate duration to ticks to avoid any secondary mcu clock skew
            mcu = self.mcu_servo.get_mcu()
            cmd_clock = mcu.print_time_to_clock(pwm_start_time)
            burst = int(duration / SERVO_SIGNAL_PERIOD) * SERVO_SIGNAL_PERIOD
            cmd_clock += mcu.seconds_to_clock(max(SERVO_SIGNAL_PERIOD, burst) + self.pwm_period_safe_offset)
            pwm_end_time = mcu.clock_to_print_time(cmd_clock)
            # Schedule PWM burst
            self.mcu_servo.set_pwm(pwm_start_time, value)
            self.mcu_servo.set_pwm(pwm_end_time, 0.)
            # Update time tracking
            self.last_value = 0.
            self.last_value_time = pwm_end_time

    # Return a print_time that is a safe place to change PWM signal
    def _get_synced_print_time(self, print_time):
        if self.last_value != 0.: # If servo already off time syncing is not necessary
            skew = (print_time - self.last_value_time) % SERVO_SIGNAL_PERIOD
            print_time -= skew # Align on previous SERVO_SIGNAL_PERIOD boundary
            print_time += self.pwm_period_safe_offset
        return print_time

    def _get_pwm_from_angle(self, angle):
        angle = max(0., min(self.max_angle, angle))
        width = self.min_width + angle * self.angle_to_width
        return width * self.width_to_value

    def _get_pwm_from_pulse_width(self, width):
        width = max(self.min_width, min(self.max_width, width)) if width else width
        return width * self.width_to_value

    cmd_SET_SERVO_help = "Set servo angle"
    def cmd_SET_SERVO(self, gcmd):
        duration = gcmd.get_float('DURATION', None, minval=PIN_MIN_TIME)
        width = gcmd.get_float('WIDTH', None)
        angle = gcmd.get_float('ANGLE', None)
        self.set_position(width, angle, duration)

    def set_position(self, width=None, angle=None, duration=None):
        duration = max(duration, SERVO_SIGNAL_PERIOD) if duration else None
        if width is not None or angle is not None:
            value = self._get_pwm_from_pulse_width(width) if width is not None else self._get_pwm_from_angle(angle)
            pt = self.printer.lookup_object('toolhead').get_last_move_time()
            self._set_pwm(pt, value, duration)

def load_config_prefix(config):
    return MmuServo(config)
