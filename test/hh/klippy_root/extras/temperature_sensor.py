# Fake Klipper extras/temperature_sensor.py. The rendered BoxTurtle config ships
# [temperature_sensor unit0_mcu], and MmuUnit.resolve_object_name will load
# `temperature_sensor <name>` for a configured environment sensor
# (extras/mmu/mmu_unit.py:139-195), raising a wrapped config error on failure.


class PrinterSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.min_temp = config.getfloat('min_temp', -273.15)
        self.max_temp = config.getfloat('max_temp', 1000.)
        self.last_temp = 25.
        self.measured_min = self.measured_max = self.last_temp

    def feed(self, temp):
        """Test-facing: set the reported temperature."""
        self.last_temp = temp
        self.measured_min = min(self.measured_min, temp)
        self.measured_max = max(self.measured_max, temp)

    def get_temp(self, eventtime=None):
        return self.last_temp, 0.

    def get_status(self, eventtime=None):
        return {'temperature': round(self.last_temp, 2),
                'measured_min_temp': round(self.measured_min, 2),
                'measured_max_temp': round(self.measured_max, 2)}


def load_config_prefix(config):
    return PrinterSensor(config)
