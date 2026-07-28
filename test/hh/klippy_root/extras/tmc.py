# Fake Klipper extras/tmc.py (+ per-chip shims) for the Happy Hare test harness.
#
# Two jobs:
#
# 1. get_status() must expose 'run_current'. That is the ONLY field HH reads
#    (extras/mmu/mmu_unit.py:593, extras/mmu/unit/mmu_extruder_wrapper.py:109).
#    Current CHANGES go out as SET_TMC_CURRENT gcode
#    (extras/mmu/mmu_filament_movement.py:3489), so no setter is needed.
#
# 2. It must register a pin chip named "<chip>_<stepper>" exposing 'virtual_endstop',
#    mirroring Klipper's TMCVirtualPinHelper. The rendered BoxTurtle config relies on
#    this: [mmu_stepper unit0_gear] has
#        extra_endstops: mmu_gear_touch=tmc2209_unit0_gear:virtual_endstop
#    and extras/mmu_stepper.py:311 calls setup_pin('endstop', ...) on it. Without the
#    chip, config load fails on an unknown pin chip.
#
# MmuExtruderWrapper REQUIRES a `<chip> extruder` section to exist and raises
# otherwise (extras/mmu/unit/mmu_extruder_wrapper.py:44-55), which is why the harness
# printer stub ships [tmc2209 extruder].


class TMCCommandHelper:
    def __init__(self, config, mcu_tmc=None):
        self.printer = config.get_printer()
        self.stepper_name = config.get_name().split()[-1]
        self.name = config.get_name()
        self.run_current = config.getfloat('run_current', 0.5, above=0.)
        self.hold_current = config.getfloat('hold_current', self.run_current,
                                            above=0.)
        self.sense_resistor = config.getfloat('sense_resistor', 0.110, above=0.)
        self.interpolate = config.getboolean('interpolate', True)
        self.stealthchop_threshold = config.getfloat('stealthchop_threshold', 0.,
                                                     minval=0.)
        self.diag_pin = config.get('diag_pin', None)
        self.mcu_tmc = mcu_tmc
        self.echeck_helper = None
        self.current_changes = []       # test assertion surface

    def set_current(self, run_current, hold_current, print_time):
        self.run_current = run_current
        self.hold_current = hold_current
        self.current_changes.append((print_time, run_current, hold_current))

    def get_status(self, eventtime=None):
        return {
            'mcu_phase_offset': None,
            'phase_offset_position': None,
            'drv_status': None,
            'temperature': None,
            'run_current': self.run_current,
            'hold_current': self.hold_current,
        }


class TMCVirtualPinHelper:
    """Registers the `<chip>_<stepper>` pin chip whose only pin is virtual_endstop."""

    def __init__(self, config, mcu_tmc=None):
        self.printer = config.get_printer()
        name_parts = config.get_name().split()
        self.diag_pin = config.get('diag_pin', None)
        ppins = self.printer.lookup_object('pins')
        chip_name = "%s_%s" % (name_parts[0], name_parts[-1])
        ppins.register_virtual_endstop_chip(chip_name)
        self.chip_name = chip_name

    def setup_pin(self, pin_type, pin_params):
        # The chip delegates back through PrinterPins._make_pin, so nothing to do.
        raise NotImplementedError


class TMC:
    def __init__(self, config, chip_name):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.chip_name = chip_name
        self.stepper_name = config.get_name().split()[-1]
        self.fields = {}
        self.cmd_helper = TMCCommandHelper(config)
        self.virtual_pin_helper = TMCVirtualPinHelper(config)
        # HH reads run_current straight off get_status()
        self.get_status = self.cmd_helper.get_status
        self.set_current = self.cmd_helper.set_current

    def get_phase_offset(self):
        return None, 256


def make_load_config_prefix(chip_name):
    def load_config_prefix(config):
        return TMC(config, chip_name)
    return load_config_prefix


load_config_prefix = make_load_config_prefix('tmc')
