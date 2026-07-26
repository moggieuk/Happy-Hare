# Fake Klipper `klippy/toolhead.py` for the Happy Hare test harness.
#
# THIN at this tier: enough state to satisfy callers plus a recorded move log. No
# move planning, no step generation - real motion belongs to the filament-path model
# (harness plan, phase C) and, for true kinematics, the optional real-Klipper tier.
#
# Most-used methods (counted across extras/): get_last_move_time x18,
# flush_step_generation x15, then get_position / wait_moves / dwell / move /
# note_mcu_movequeue_activity / register_lookahead_callback / get_extruder /
# get_kinematics / add_extra_axis / remove_extra_axis / get_extra_axes.
#
# print_time is kept DISTINCT from reactor eventtime (see mcu.HOST_OFFSET): HH
# carefully separates the two clock domains and unifying them would hide a whole
# class of bug (extras/mmu/mmu_sensor_utils.py:410-435).
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import mcu as mcu_mod


class DummyKinematics:
    def get_steppers(self):
        return []

    def calc_position(self, stepper_positions):
        return [0., 0., 0.]

    def check_move(self, move):
        pass

    def get_status(self, eventtime):
        return {'homed_axes': 'xyz', 'axis_minimum': (0., 0., 0., 0.),
                'axis_maximum': (200., 200., 200., 0.)}


class ToolHead:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [self.printer.lookup_object('mcu')]
        self.mcu = self.all_mcus[0]
        self.max_velocity = config.getfloat('max_velocity', 300., above=0.)
        self.max_accel = config.getfloat('max_accel', 3000., above=0.)
        self.commanded_pos = [0., 0., 0., 0.]
        self.extra_axes = []
        self.extruder = None
        self.kin = DummyKinematics()
        self.step_generators = []
        self.lookahead_callbacks = []
        # -- assertion surfaces -------------------------------------------
        self.moves = []             # [(newpos, speed)] every commanded move
        self.dwells = []
        self.flushes = 0
        self.waits = 0
        # Mirrors real Klipper's ToolHead.__init__ default-module preload
        # (klippy/toolhead.py:292). `homing` in particular is looked up rather than
        # loaded by HH (extras/mmu_stepper.py:883), so it has to exist by now. The
        # ones the harness does not fake are loaded with a None default and skipped.
        for module_name in ('gcode_move', 'homing', 'idle_timeout', 'statistics',
                            'manual_probe', 'tuning_tower', 'garbage_collection'):
            self.printer.load_object(config, module_name, None)

    # -- print time --------------------------------------------------------
    def _print_time(self):
        return self.mcu.estimated_print_time(self.reactor.monotonic())

    def get_last_move_time(self):
        return self._print_time()

    def flush_step_generation(self):
        self.flushes += 1

    def note_mcu_movequeue_activity(self, print_time, set_step_gen_time=False):
        pass

    def register_lookahead_callback(self, callback):
        # Real Klipper defers to the end of the move queue; with no queue we call
        # straight through, which is what the callers here expect to observe.
        self.lookahead_callbacks.append(callback)
        callback(self._print_time())

    def register_step_generator(self, handler):
        self.step_generators.append(handler)

    def note_step_generation_scan_time(self, delay, old_delay=0.):
        pass

    # -- position / motion -------------------------------------------------
    def get_position(self):
        return list(self.commanded_pos)

    def set_position(self, newpos, homing_axes=()):
        self.commanded_pos = list(newpos)

    def move(self, newpos, speed):
        self.moves.append((list(newpos), speed))
        self.commanded_pos = list(newpos)

    def manual_move(self, coord, speed):
        newpos = list(self.commanded_pos)
        for i, c in enumerate(coord):
            if c is not None:
                newpos[i] = c
        self.move(newpos, speed)

    def dwell(self, delay):
        self.dwells.append(delay)

    def wait_moves(self):
        self.waits += 1

    def drip_move(self, newpos, speed, drip_completion):
        self.move(newpos, speed)

    def get_max_velocity(self):
        return self.max_velocity, self.max_accel

    # -- objects -----------------------------------------------------------
    def get_kinematics(self):
        return self.kin

    def get_extruder(self):
        return self.extruder

    def set_extruder(self, extruder, extrude_pos):
        self.extruder = extruder

    def get_trapq(self):
        return None

    # -- extra axes (HH adds the MMU gear axis) ----------------------------
    def add_extra_axis(self, axis, pos=0.):
        if axis not in self.extra_axes:
            self.extra_axes.append(axis)
            self.commanded_pos.append(pos)

    def remove_extra_axis(self, axis):
        if axis in self.extra_axes:
            idx = self.extra_axes.index(axis)
            self.extra_axes.pop(idx)
            if len(self.commanded_pos) > 4 + idx:
                self.commanded_pos.pop(4 + idx)

    def get_extra_axes(self):
        return list(self.extra_axes)

    # -- status ------------------------------------------------------------
    def get_status(self, eventtime=None):
        return {
            'print_time': self._print_time(),
            'stalls': 0,
            'estimated_print_time': self._print_time(),
            'extruder': self.extruder.get_name() if self.extruder else '',
            'position': list(self.commanded_pos),
            'max_velocity': self.max_velocity,
            'max_accel': self.max_accel,
            'minimum_cruise_ratio': 0.5,
            'square_corner_velocity': 5.,
            'homed_axes': 'xyz',
        }

    def stats(self, eventtime):
        return False, ''


def add_printer_objects(config):
    printer = config.get_printer()
    printer.add_object('toolhead', ToolHead(config))
