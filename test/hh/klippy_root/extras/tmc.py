# Fake Klipper extras/tmc.py (+ per-chip shims) for the Happy Hare test harness.
#
# Three jobs:
#
# 1. get_status() must expose 'run_current'. HH snapshots it once at connect as each
#    stepper's default (extras/mmu/mmu_unit.py, extras/mmu/unit/mmu_extruder_wrapper.py).
#
# 2. It must model SET_TMC_CURRENT, so run_current actually tracks what HH asked for.
#    Without the handler the command falls through gcode.py's ignore-unknown path and the
#    modelled current never moves - the console shows HH's intent with nothing behind it.
#
# 3. It must register a pin chip named "<chip>_<stepper>" exposing 'virtual_endstop',
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
        self.max_current = config.getfloat('max_current', 2.0, above=0.)
        self.current_changes = []       # test assertion surface

        # Keyed on STEPPER= exactly as real Klipper's TMCCommandHelper does. HH changes gear
        # and extruder current by emitting this gcode, so without the handler the modelled
        # driver never moves and any test or panel reading it is reading the config default.
        self.printer.lookup_object('gcode').register_mux_command(
            'SET_TMC_CURRENT', 'STEPPER', self.stepper_name,
            self._cmd_SET_TMC_CURRENT,
            desc='Set the current of a TMC driver')

    def get_current(self):
        return self.run_current, self.hold_current, self.hold_current, self.max_current

    def set_current(self, run_current, hold_current, print_time):
        self.run_current = run_current
        self.hold_current = hold_current
        self.current_changes.append((print_time, run_current, hold_current))

    def _cmd_SET_TMC_CURRENT(self, gcmd):
        # Mirrors real Klipper: both args optional, a bare call is a query, and the reported
        # value is re-read after applying so it is the applied current, not the requested one.
        run_current = gcmd.get_float('CURRENT', None, minval=0., maxval=self.max_current)
        hold_current = gcmd.get_float('HOLDCURRENT', None, above=0., maxval=self.max_current)

        if run_current is not None or hold_current is not None:
            prev_run, _prev_hold, req_hold, _max = self.get_current()
            toolhead = self.printer.lookup_object('toolhead')
            self.set_current(prev_run if run_current is None else run_current,
                             req_hold if hold_current is None else hold_current,
                             toolhead.get_last_move_time())

        gcmd.respond_info("Run Current: %0.2fA Hold Current: %0.2fA"
                          % (self.run_current, self.hold_current))

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
        # The chip MUST carry an mcu. A TMC virtual endstop belongs to the mcu driving the
        # stepper, and anything that reports on endstops walks es.get_mcu().get_name() -
        # _MMU_TEST DUMP_MCU_ENDSTOPS and MmuStepper's own diagnostic dump both do. Left as
        # None (the default) they raise "'NoneType' object has no attribute 'get_name'".
        # Every fake stepper resolves to the single [mcu], so that is the faithful owner.
        ppins.register_virtual_endstop_chip(chip_name, self.printer.lookup_object('mcu', None))
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
        self.get_current = self.cmd_helper.get_current
        self.current_changes = self.cmd_helper.current_changes

    def get_phase_offset(self):
        return None, 256


def make_load_config_prefix(chip_name):
    def load_config_prefix(config):
        return TMC(config, chip_name)
    return load_config_prefix


load_config_prefix = make_load_config_prefix('tmc')
