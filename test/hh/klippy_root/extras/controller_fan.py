# Fake Klipper `klippy/extras/controller_fan.py` for the Happy Hare test harness.
#
# Without this module a [controller_fan] section is silently skipped (bootstrap.py passes
# None as load_object's default), so a misspelled stepper name would only fail on a real
# printer. Real Klipper checks every 'stepper' name against stepper_enable at
# klippy:connect and refuses to start otherwise; this does the same. It checks only the
# steppers that actually registered: the fake lookup_enable() creates an entry for any
# name it is asked about, which would otherwise mask a typo.
#
# The fan itself is not driven - there is no timer callback - so get_status() reports 0.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class ControllerFan:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.stepper_names = config.getlist("stepper", None)
        self.stepper_enable = self.printer.load_object(config, 'stepper_enable')
        self.printer.load_object(config, 'heaters')
        self.pin = config.get('pin')
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.kick_start_time = config.getfloat('kick_start_time', 0.1, minval=0.)
        self.shutdown_speed = config.getfloat('shutdown_speed', 0., minval=0., maxval=1.)
        self.fan_speed = config.getfloat('fan_speed', 1., minval=0., maxval=1.)
        self.idle_speed = config.getfloat('idle_speed', self.fan_speed, minval=0., maxval=1.)
        self.idle_timeout = config.getint('idle_timeout', 30, minval=0)
        self.heater_names = config.getlist("heater", ("extruder",))
        self.heaters = []

    def handle_connect(self):
        pheaters = self.printer.lookup_object('heaters')
        self.heaters = [pheaters.lookup_heater(n) for n in self.heater_names]
        all_steppers = [name for name, el in self.stepper_enable.enable_lines.items()
                        if el.stepper is not None]
        if self.stepper_names is None:
            self.stepper_names = all_steppers
            return
        if not all(x in all_steppers for x in self.stepper_names):
            raise self.printer.config_error(
                "One or more of these steppers are unknown: "
                "%s (valid steppers are: %s)"
                % (self.stepper_names, ", ".join(all_steppers)))

    def get_status(self, eventtime=None):
        return {'speed': 0., 'rpm': None}


def load_config_prefix(config):
    return ControllerFan(config)
