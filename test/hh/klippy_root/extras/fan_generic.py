# Fake Klipper extras/fan_generic.py used by the Happy Hare harness.


class Fan:
    def __init__(self):
        self.speed = 0.

    def set_speed(self, print_time, value):
        self.speed = float(value)

    def set_speed_from_command(self, value):
        self.set_speed(None, value)

    def get_status(self, eventtime=None):
        return {'speed': self.speed, 'rpm': None}


class PrinterFanGeneric:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.fan_name = config.get_name().split()[-1]
        self.fan = Fan()
        self.printer.lookup_object('gcode').register_mux_command(
            'SET_FAN_SPEED', 'FAN', self.fan_name,
            self.cmd_SET_FAN_SPEED, desc='Sets the speed of a fan')

    def cmd_SET_FAN_SPEED(self, gcmd):
        self.fan.set_speed_from_command(gcmd.get_float('SPEED', minval=0., maxval=1.))

    def get_status(self, eventtime=None):
        return self.fan.get_status(eventtime)


def load_config_prefix(config):
    return PrinterFanGeneric(config)
