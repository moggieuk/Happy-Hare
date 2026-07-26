# Fake Klipper `klippy/stepper.py` for the Happy Hare test harness.
#
# Pure Python, no chelper. "Stepper kinematics" is an opaque token object rather
# than a C iterative solver - that is enough for MmuStepper._activate_manual_mode /
# _activate_extruder_mode_detached (extras/mmu_stepper.py:649-682) to be exercised
# for real, since all they do is swap trapq + sk.
#
# Surface HH uses (extras/mmu_stepper.py:157-158, 245-253, 550-700):
#   get_name, get_mcu, get_commanded_position, calc_position_from_coord,
#   setup_itersolve, set_trapq, get_trapq, set_position, get_stepper_kinematics,
#   set_stepper_kinematics, get_mcu_position, get_past_mcu_position, get_step_dist,
#   mcu_to_commanded_position, set_stepper_enable / is_motor_enabled helpers.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class StepperKinematics:
    """Opaque token standing in for the chelper stepper_kinematics struct."""

    def __init__(self, alloc_name, axis):
        self.alloc_name = alloc_name
        self.axis = axis
        self.trapq = None
        self.step_dist = 1.

    def __repr__(self):
        return 'StepperKinematics(%s, %r)' % (self.alloc_name, self.axis)


class MCU_stepper:
    def __init__(self, name, step_pin_params, dir_pin_params, rotation_dist,
                 steps_per_rotation, mcu=None):
        self._name = name
        self._mcu = mcu
        self._rotation_dist = rotation_dist
        self._steps_per_rotation = steps_per_rotation
        self._step_dist = rotation_dist / steps_per_rotation
        self._sk = None
        self._trapq = None
        self._commanded_pos = 0.
        self._mcu_position_offset = 0.
        self._invert_dir = self._orig_invert_dir = dir_pin_params.get('invert', False)

    # -- identity ----------------------------------------------------------
    def get_name(self, short=False):
        if short and self._name.startswith('stepper_'):
            return self._name[8:]
        return self._name

    def get_mcu(self):
        return self._mcu

    def get_step_dist(self):
        return self._step_dist

    def get_rotation_distance(self):
        return self._rotation_dist, self._steps_per_rotation

    def set_rotation_distance(self, rotation_dist):
        self._rotation_dist = rotation_dist
        self._step_dist = rotation_dist / self._steps_per_rotation

    def get_dir_inverted(self):
        return self._invert_dir, self._orig_invert_dir

    def is_dir_inverted(self):
        return self._invert_dir

    # -- kinematics --------------------------------------------------------
    def setup_itersolve(self, alloc_func, *params):
        self._sk = StepperKinematics(alloc_func, params[0] if params else None)
        self._sk.step_dist = self._step_dist
        return self._sk

    def get_stepper_kinematics(self):
        return self._sk

    def set_stepper_kinematics(self, sk):
        old = self._sk
        self._sk = sk
        if sk is not None:
            sk.step_dist = self._step_dist
        return old

    def set_trapq(self, trapq):
        old = self._trapq
        self._trapq = trapq
        if self._sk is not None:
            self._sk.trapq = trapq
        return old

    def get_trapq(self):
        return self._trapq

    # -- position ----------------------------------------------------------
    def calc_position_from_coord(self, coord):
        return coord[0]

    def set_position(self, coord):
        self._commanded_pos = self.calc_position_from_coord(coord)

    def get_commanded_position(self):
        return self._commanded_pos

    def get_mcu_position(self, cmd_pos=None):
        if cmd_pos is None:
            cmd_pos = self._commanded_pos
        return int((cmd_pos + self._mcu_position_offset) / self._step_dist + 0.5)

    def get_past_mcu_position(self, print_time):
        return self.get_mcu_position()

    def mcu_to_commanded_position(self, mcu_pos):
        return mcu_pos * self._step_dist - self._mcu_position_offset

    def dump_steps(self, count, start_clock, end_clock):
        return []

    def generate_steps(self, flush_time):
        pass

    def note_homing_end(self, did_trigger=False):
        pass

    def setup_dir_pin(self, pin_params):
        pass

    def add_active_callback(self, cb):
        pass


class PrinterStepper:
    """
    Wraps an MCU_stepper, parsing the standard stepper config keys. HH's
    MmuGenericRail / MmuStepper hold one of these as `.stepper`.
    """

    def __init__(self, config, units_in_radians=False):
        printer = config.get_printer()
        self.printer = printer
        self.name = config.get_name()
        ppins = printer.lookup_object('pins')
        step_pin = config.get('step_pin')
        dir_pin = config.get('dir_pin')
        step_params = ppins.parse_pin(step_pin, can_invert=True)
        dir_params = ppins.parse_pin(dir_pin, can_invert=True)
        mcu = ppins.setup_pin('stepper', step_pin).get_mcu()

        rotation_dist = config.getfloat('rotation_distance', 40., above=0.)
        microsteps = config.getint('microsteps', 16, minval=1)
        full_steps = config.getint('full_steps_per_rotation', 200, minval=1)
        gear_ratio_str = config.get('gear_ratio', None)
        gear_ratio = 1.
        if gear_ratio_str:
            for part in gear_ratio_str.split(','):
                part = part.strip()
                if not part:
                    continue
                if ':' in part:
                    num, den = part.split(':')
                    gear_ratio *= float(num) / float(den)
                else:
                    gear_ratio *= float(part)
        self.rotation_distance = rotation_dist / gear_ratio
        self.mcu_stepper = MCU_stepper(
            self.name, step_params, dir_params,
            self.rotation_distance, microsteps * full_steps, mcu=mcu)

        self.enable_pin = config.get('enable_pin', None)
        self._enabled = False
        # Klipper registers the stepper with stepper_enable; mirror it so
        # stepper_enable.lookup_enable(name) resolves.
        se = printer.load_object(config, 'stepper_enable')
        if se is not None:
            se.register_stepper(config, self.mcu_stepper)

    # PrinterStepper proxies the MCU_stepper API in real Klipper
    def __getattr__(self, name):
        return getattr(self.mcu_stepper, name)

    def get_name(self, short=False):
        return self.mcu_stepper.get_name(short)

    def get_step_dist(self):
        return self.mcu_stepper.get_step_dist()

    def setup_itersolve(self, alloc_func, *params):
        return self.mcu_stepper.setup_itersolve(alloc_func, *params)


def LookupMultiRail(config, need_position_minmax=True, default_position_endstop=None,
                    units_in_radians=False):
    raise NotImplementedError(
        "stepper.LookupMultiRail is not used by Happy Hare - it builds its own "
        "MmuGenericRail (extras/mmu_stepper.py:57). If this fired, HH has changed.")
