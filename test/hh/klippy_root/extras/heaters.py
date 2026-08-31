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
        self.printer.lookup_object('gcode').register_command(
            'TEMPERATURE_WAIT', self._cmd_TEMPERATURE_WAIT,
            desc='Wait for a temperature sensor')

    def setup_heater(self, config, gcode_id=None):
        name = config.get_name().split()[-1]
        if name in self.heaters:
            raise config.error("Heater %s already registered" % (name,))
        heater = Heater(name, config)
        self.heaters[name] = heater
        self.available_heaters.append(config.get_name())
        if gcode_id:
            self.gcode_id_to_sensor[gcode_id] = heater
        # Registered per heater, keyed on HEATER=, exactly as real Klipper's
        # Heater.__init__ does. Without it MmuEnvironmentManager's
        # SET_HEATER_TEMPERATURE (mmu_environment_manager.py:800) would fall through to
        # gcode.py's ignore-unknown path and the target would silently never change - a
        # heater test that passes while testing nothing.
        self.printer.lookup_object('gcode').register_mux_command(
            'SET_HEATER_TEMPERATURE', 'HEATER', name,
            self._cmd_SET_HEATER_TEMPERATURE,
            desc='Sets a heater temperature')
        return heater

    def _cmd_SET_HEATER_TEMPERATURE(self, gcmd):
        heater = self.heaters[gcmd.get('HEATER')]
        self.set_temperature(heater, gcmd.get_float('TARGET', 0.))

    def set_temperature(self, heater, temp, wait=False):
        heater.set_temp(temp)

    def _cmd_TEMPERATURE_WAIT(self, gcmd):
        """Complete a thermal wait instantly while preserving its observable stages."""
        sensor = gcmd.get('SENSOR')
        heater = self.lookup_heater(sensor)
        minimum = gcmd.get_float('MINIMUM', None)
        maximum = gcmd.get_float('MAXIMUM', None)
        if minimum is None and maximum is None:
            raise gcmd.error("TEMPERATURE_WAIT requires MINIMUM and/or MAXIMUM")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise gcmd.error("TEMPERATURE_WAIT MINIMUM exceeds MAXIMUM")

        # There is deliberately no thermal time model in the harness. Move the reading to
        # the first boundary this wait would encounter so a cooling ramp still exposes every
        # intermediate temperature instead of teleporting straight to its final target.
        current = heater.smoothed_temp
        target = heater.target_temp
        if minimum is not None and current < minimum:
            current = min(target, maximum) if maximum is not None else max(minimum, target)
            current = max(current, minimum)
        if maximum is not None and current > maximum:
            current = max(target, minimum) if minimum is not None else min(maximum, target)
            current = min(current, maximum)
        heater.smoothed_temp = current
        heater.can_extrude = current >= heater.min_extrude_temp

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
