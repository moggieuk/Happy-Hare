# Fake Klipper extras/stepper_enable.py. HH: extras/mmu_stepper.py:827-828,
# extras/mmu/mmu_filament_movement.py:1169-1171,
# extras/mmu/commands/mmu_calibration_mixins.py:401.


class EnableTracking:
    def __init__(self, name, stepper=None):
        self.name = name
        self.stepper = stepper
        self.is_enabled = False
        self.transitions = []       # [(print_time, bool)] assertion surface

    def motor_enable(self, print_time):
        self.is_enabled = True
        self.transitions.append((print_time, True))

    def motor_disable(self, print_time):
        self.is_enabled = False
        self.transitions.append((print_time, False))

    def is_motor_enabled(self):
        return self.is_enabled

    def has_dedicated_enable(self):
        return True


class PrinterStepperEnable:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.enable_lines = {}

    def register_stepper(self, config, mcu_stepper):
        name = mcu_stepper.get_name()
        self.enable_lines[name] = EnableTracking(name, mcu_stepper)

    def lookup_enable(self, name):
        if name not in self.enable_lines:
            # HH looks up steppers it created through other paths too
            self.enable_lines[name] = EnableTracking(name)
        return self.enable_lines[name]

    def get_steppers(self):
        return list(self.enable_lines)

    def motor_off(self):
        toolhead = self.printer.lookup_object('toolhead', None)
        pt = toolhead.get_last_move_time() if toolhead else 0.
        for el in self.enable_lines.values():
            el.motor_disable(pt)

    def set_motors_enable(self, names, enable):
        toolhead = self.printer.lookup_object('toolhead', None)
        pt = toolhead.get_last_move_time() if toolhead else 0.
        for name in names:
            el = self.lookup_enable(name)
            el.motor_enable(pt) if enable else el.motor_disable(pt)

    def get_status(self, eventtime=None):
        return {'steppers': {n: el.is_enabled for n, el in self.enable_lines.items()}}


def load_config(config):
    return PrinterStepperEnable(config)
