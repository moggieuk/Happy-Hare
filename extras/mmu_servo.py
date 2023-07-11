# Happy Hare MMU Software
# Custom servo support that carefully synchronizes PWM changes to avoid "kickback".
# All existing servo funcationality is avialable with the addition of a `duration`
# parameter for setting PWM pulse train with auto off
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Based on original servo.py            Copyright (C) 2017-2020  Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

SERVO_SIGNAL_PERIOD = 0.02
PIN_MIN_TIME = 0.1

class MmuServo:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.min_width = config.getfloat('minimum_pulse_width', 0.001, above=0., below=SERVO_SIGNAL_PERIOD)
        self.max_width = config.getfloat('maximum_pulse_width', 0.002, above=self.min_width, below=SERVO_SIGNAL_PERIOD)
        self.max_angle = config.getfloat('maximum_servo_angle', 180.)
        self.angle_to_width = (self.max_width - self.min_width) / self.max_angle
        self.width_to_value = 1. / SERVO_SIGNAL_PERIOD
        self.not_before_time = initial_pwm = 0.
        iangle = config.getfloat('initial_angle', None, minval=0., maxval=360.)
        if iangle is not None:
            initial_pwm = self._get_pwm_from_angle(iangle)
        else:
            iwidth = config.getfloat('initial_pulse_width', 0., minval=0., maxval=self.max_width)
            initial_pwm = self._get_pwm_from_pulse_width(iwidth)
        self.last_value = initial_pwm
        self.angle_to_width = (self.max_width - self.min_width) / self.max_angle
        self.width_to_value = 1.0 / SERVO_SIGNAL_PERIOD
        self.pwm_period_safe_offset = SERVO_SIGNAL_PERIOD - (SERVO_SIGNAL_PERIOD - self.max_width) / 2
        ppins = self.printer.lookup_object('pins')
        self.mcu_pwm = ppins.setup_pin('pwm', config.get('pin'))
        self.mcu_pwm.setup_max_duration(0.)
        self.mcu_pwm.setup_cycle_time(SERVO_SIGNAL_PERIOD)
        self.mcu_pwm.setup_start_value(initial_pwm, 0.)
        servo_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('SET_SERVO', 'SERVO', servo_name, self.cmd_SET_SERVO, desc=self.cmd_SET_SERVO_help)

    def handle_connect(self):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        self.not_before_time = print_time + PIN_MIN_TIME

    def get_status(self, eventtime):
        return {'value': self.last_value}

    # Return a print_time that is a safe place to change PWM signal
    def _get_synced_print_time(self):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        if self.not_before_time == 0.:
            self.not_before_time = print_time
        if print_time > self.not_before_time:
            skew = (print_time - self.not_before_time) % SERVO_SIGNAL_PERIOD
            print_time -= skew # Align on SERVO_SIGNAL_PERIOD
            if skew > self.pwm_period_safe_offset:
                print_time += SERVO_SIGNAL_PERIOD
            return print_time + self.pwm_period_safe_offset
        elif self.last_value != 0.:
            return self.not_before_time + self.pwm_period_safe_offset
        else:
            return max(self.not_before_time, print_time) # Already off so sync is not necessary

    def _set_burst_pwm(self, print_time, value, duration):
        mcu = self.mcu_pwm.get_mcu()
        # Translate duration to ticks to avoid any secondary mcu clock skew
        cmd_clock = mcu.print_time_to_clock(print_time)
        burst = int(duration / SERVO_SIGNAL_PERIOD) * SERVO_SIGNAL_PERIOD
        cmd_clock += mcu.seconds_to_clock(max(PIN_MIN_TIME, burst) + self.pwm_period_safe_offset)
        end_time = mcu.clock_to_print_time(cmd_clock)
        # Schedule command followed by PWM disable
        self.mcu_pwm.set_pwm(print_time, value)
        self.mcu_pwm.set_pwm(end_time, 0.)
        # Update time tracking
        self.last_value = 0.
        self.not_before_time = end_time + PIN_MIN_TIME

    def _set_pwm(self, print_time, value):
        self.mcu_pwm.set_pwm(print_time, value)
        self.last_value = value
        self.not_before_time = print_time + PIN_MIN_TIME

    def _get_pwm_from_angle(self, angle):
        angle = max(0., min(self.max_angle, angle))
        width = self.min_width + angle * self.angle_to_width
        return width * self.width_to_value

    def _get_pwm_from_pulse_width(self, width):
        if width:
            width = max(self.min_width, min(self.max_width, width))
        return width * self.width_to_value

    def set_value(self, width=None, angle=None, duration=None):
        print_time = self._get_synced_print_time()
        if width is not None:
            value = self._get_pwm_from_pulse_width(width)
        else:
            value = self._get_pwm_from_angle(angle)
        if duration is not None:
            self._set_burst_pwm(print_time, value, duration)
        elif value != self.last_value:
            self._set_pwm(print_time, value)

    cmd_SET_SERVO_help = 'Set servo angle'
    def cmd_SET_SERVO(self, gcmd):
        width = gcmd.get_float('WIDTH', None, minval=0.)
        angle = gcmd.get_float('ANGLE', None)
        duration = gcmd.get_float('DURATION', None, minval=SERVO_SIGNAL_PERIOD)
        self.set_value(width, angle, duration)

def load_config_prefix(config):
    return MmuServo(config)
