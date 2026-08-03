# Fake Klipper `klippy/kinematics/extruder.py` for the Happy Hare test harness.
#
# extras/mmu_stepper.py:48 imports both classes and MmuStepper SUBCLASSES
# ExtruderStepper (:478, calling ExtruderStepper.__init__ at :508), relying on it to
# set: printer, name, stepper, sk_extruder, motion_queue and to register the stock
# SET_PRESSURE_ADVANCE / SET_EXTRUDER_ROTATION_DISTANCE / SYNC_EXTRUDER_MOTION
# mux commands.
#
# CRITICAL: PrinterExtruder must only build an ExtruderStepper when the section
# actually has a step_pin, exactly as real Klipper does. That is what makes
# MmuExtruderWrapper's option-stripping trick work - it removes [extruder]'s
# stepper options at config time (extras/mmu/unit/mmu_extruder_wrapper.py:63-66) so
# no duplicate stepper is built, then restores them and swaps in its own homing
# stepper at klippy:connect (:89-96). If this fake unconditionally built a stepper,
# that whole design would go untested.
#
# sk_extruder is a plain token, not a chelper struct (see stepper.StepperKinematics).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math, logging

import stepper


class ExtruderStepper:
    cmd_SET_PRESSURE_ADVANCE_help = "Set pressure advance parameters"
    cmd_SET_E_ROTATION_DISTANCE_help = "Set extruder rotation distance"
    cmd_SYNC_EXTRUDER_MOTION_help = "Set extruder stepper motion queue"

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat(
            'pressure_advance_smooth_time', 0.040, above=0., maxval=.200)
        self.stepper = stepper.PrinterStepper(config)
        self.sk_extruder = stepper.StepperKinematics('extruder_stepper_alloc', None)
        self.stepper.set_stepper_kinematics(self.sk_extruder)
        self.motion_queue = None
        self.motion_mode = None      # HH reads/sets this (extras/mmu_stepper.py)
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        gcode = self.printer.lookup_object('gcode')
        if self.name == 'extruder':
            gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER", None,
                                       self.cmd_default_SET_PRESSURE_ADVANCE,
                                       desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER", self.name,
                                   self.cmd_SET_PRESSURE_ADVANCE,
                                   desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_EXTRUDER_ROTATION_DISTANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_E_ROTATION_DISTANCE,
                                   desc=self.cmd_SET_E_ROTATION_DISTANCE_help)
        gcode.register_mux_command("SYNC_EXTRUDER_MOTION", "EXTRUDER", self.name,
                                   self.cmd_SYNC_EXTRUDER_MOTION,
                                   desc=self.cmd_SYNC_EXTRUDER_MOTION_help)

    def _handle_connect(self):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_step_generator(self.stepper.generate_steps)
        self._set_pressure_advance(self.config_pa, self.config_smooth_time)

    def get_status(self, eventtime):
        return {'pressure_advance': self.pressure_advance,
                'smooth_time': self.pressure_advance_smooth_time,
                'motion_queue': self.motion_queue}

    def find_past_position(self, print_time):
        return self.stepper.get_past_mcu_position(print_time) * self.stepper.get_step_dist()

    def sync_to_extruder(self, extruder_name):
        self.motion_queue = extruder_name

    def _set_pressure_advance(self, pressure_advance, smooth_time):
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time

    def cmd_default_SET_PRESSURE_ADVANCE(self, gcmd):
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        if extruder is None or extruder.extruder_stepper is None:
            raise gcmd.error("No extruder stepper to configure")
        extruder.extruder_stepper.cmd_SET_PRESSURE_ADVANCE(gcmd)

    def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
        pa = gcmd.get_float('ADVANCE', self.pressure_advance, minval=0.)
        st = gcmd.get_float('SMOOTH_TIME', self.pressure_advance_smooth_time,
                            minval=0., maxval=.200)
        self._set_pressure_advance(pa, st)
        gcmd.respond_info("pressure_advance: %.6f smooth_time: %.6f" % (pa, st))

    def cmd_SET_E_ROTATION_DISTANCE(self, gcmd):
        rd = gcmd.get_float('DISTANCE', None, above=0.)
        if rd is not None:
            self.stepper.set_rotation_distance(rd)
        gcmd.respond_info("stepper '%s' rotation_distance set" % (self.name,))

    def cmd_SYNC_EXTRUDER_MOTION(self, gcmd):
        self.motion_queue = gcmd.get('MOTION_QUEUE', None)


class DummyExtruder:
    def __init__(self, printer):
        self.printer = printer
        self.extruder_stepper = None

    def update_move_time(self, flush_time, clear_history_time):
        pass

    def check_move(self, move):
        raise move.move_error("Extrude when no extruder present")

    def find_past_position(self, print_time):
        return 0.

    def calc_junction(self, prev_move, move):
        return move.max_cruise_v2

    def get_name(self):
        return ""

    def get_heater(self):
        raise self.printer.command_error("Extruder not configured")

    def get_trapq(self):
        return None


class PrinterExtruder:
    def __init__(self, config, extruder_num):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.last_position = 0.
        pheaters = self.printer.load_object(config, 'heaters')
        self.heater = pheaters.setup_heater(config, 'T%d' % (extruder_num,))
        self.nozzle_diameter = config.getfloat('nozzle_diameter', 0.4, above=0.)
        filament_diameter = config.getfloat('filament_diameter', 1.75,
                                            minval=self.nozzle_diameter)
        self.filament_area = math.pi * (filament_diameter * .5) ** 2
        def_max_cross_section = 4. * self.nozzle_diameter ** 2
        max_cross_section = config.getfloat('max_extrude_cross_section',
                                            def_max_cross_section, above=0.)
        self.max_extrude_ratio = max_cross_section / self.filament_area
        self.max_e_dist = config.getfloat('max_extrude_only_distance', 50., minval=0.)
        self.max_e_velocity = config.getfloat('max_extrude_only_velocity', 100., above=0.)
        self.max_e_accel = config.getfloat('max_extrude_only_accel', 1000., above=0.)
        self.instant_corner_v = config.getfloat('instantaneous_corner_velocity', 1.,
                                                minval=0.)
        mq = self.printer.load_object(config, 'motion_queuing')
        self.trapq = mq.allocate_trapq()
        self.trapq_append = mq.lookup_trapq_append()

        # Only build a stepper when one is configured - see module docstring.
        self.extruder_stepper = None
        if config.get('step_pin', None) is not None:
            self.extruder_stepper = ExtruderStepper(config)
            self.extruder_stepper.stepper.set_trapq(self.trapq)

        self.printer.add_object(self.name, self)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.set_extruder(self, 0.)
        logging.info("Fake PrinterExtruder '%s' created (stepper=%s)",
                     self.name, self.extruder_stepper is not None)

    def update_move_time(self, flush_time, clear_history_time):
        pass

    def get_status(self, eventtime):
        s = dict(self.heater.get_status(eventtime))
        s['can_extrude'] = self.heater.can_extrude
        if self.extruder_stepper is not None:
            s.update(self.extruder_stepper.get_status(eventtime))
        return s

    def get_name(self):
        return self.name

    def get_heater(self):
        return self.heater

    def get_trapq(self):
        return self.trapq

    def stats(self, eventtime):
        return False, ''

    def find_past_position(self, print_time):
        if self.extruder_stepper is None:
            return 0.
        return self.extruder_stepper.find_past_position(print_time)

    def check_move(self, move):
        pass

    def calc_junction(self, prev_move, move):
        return move.max_cruise_v2

    def move(self, print_time, move):
        self.last_position = move.end_pos[3]


def add_printer_objects(config):
    """Mirrors Klipper: build [extruder], [extruder1].. and any [extruder_stepper N]."""
    printer = config.get_printer()
    for i in range(99):
        section = 'extruder' if i == 0 else 'extruder%d' % (i,)
        if not config.has_section(section):
            if i:
                break
            continue
        PrinterExtruder(config.getsection(section), i)
    for s in config.get_prefix_sections('extruder_stepper '):
        printer.load_object(config, s.get_name())
