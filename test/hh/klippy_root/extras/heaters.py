# Fake Klipper extras/heaters.py. Needed by kinematics.extruder (setup_heater) and
# read by HH at extras/mmu/mmu_controller.py:1967,2390-2392 (target_temp,
# min_extrude_temp, can_extrude).


class Heater:
    def __init__(self, name, config=None):
        self.name = name
        self.target_temp = 0.
        self.smoothed_temp = 20.
        self.min_temp = 0.
        self.max_temp = 300.
        self.min_extrude_temp = 170.
        if config is not None:
            self.min_temp = config.getfloat('min_temp', 0.)
            self.max_temp = config.getfloat('max_temp', 300.)
            self.min_extrude_temp = config.getfloat('min_extrude_temp', 170.)
        # Harness default: hot enough to extrude, so a bootup/load test is not
        # blocked on heating. A test wanting the cold path sets this False.
        self.can_extrude = True

    def set_temp(self, degrees):
        self.target_temp = degrees

    def get_temp(self, eventtime=None):
        return self.smoothed_temp, self.target_temp

    def check_busy(self, eventtime=None):
        return False

    def get_max_power(self):
        return 1.

    def get_status(self, eventtime=None):
        return {'temperature': round(self.smoothed_temp, 2),
                'target': self.target_temp, 'power': 0.,
                'can_extrude': self.can_extrude}

    def get_name(self):
        return self.name


class PrinterHeaters:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.heaters = {}
        self.gcode_id_to_sensor = {}
        self.available_heaters = []
        self.available_sensors = []
        self.available_monitors = []

    def setup_heater(self, config, gcode_id=None):
        name = config.get_name().split()[-1]
        if name in self.heaters:
            raise config.error("Heater %s already registered" % (name,))
        heater = Heater(name, config)
        self.heaters[name] = heater
        self.available_heaters.append(config.get_name())
        if gcode_id:
            self.gcode_id_to_sensor[gcode_id] = heater
        return heater

    def lookup_heater(self, heater_name):
        if heater_name not in self.heaters:
            raise self.printer.config_error("Unknown heater '%s'" % (heater_name,))
        return self.heaters[heater_name]

    def get_all_heaters(self):
        return self.available_heaters

    def keys(self):
        return self.heaters.keys()

    def get_status(self, eventtime=None):
        return {'available_heaters': self.available_heaters,
                'available_sensors': self.available_sensors,
                'available_monitors': self.available_monitors}


def load_config(config):
    return PrinterHeaters(config)
