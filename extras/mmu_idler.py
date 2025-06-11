# Happy Hare MMU Software
# Idler control for Prusa MMU3
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import math, logging, stepper
import chelper

from .  import force_move
from .. import mmu_machine

BUZZ_DISTANCE = 1.
BUZZ_VELOCITY = BUZZ_DISTANCE / .250
BUZZ_RADIANS_DISTANCE = math.radians(1.)
BUZZ_RADIANS_VELOCITY = BUZZ_RADIANS_DISTANCE / .250
STALL_TIME = 0.100


# Based of 'force_move.py' and 'manual_stepper.py' in klipper
class MmuIdler:
    def __init__(self, config):
        self.printer = config.get_printer()
        
        # Setup stepper for idler
        self.rail = stepper.PrinterRail(config, need_position_minmax=False, default_position_endstop=0)
        self.steppers = self.rail.steppers

        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)

        self.next_cmd_time = None

        self.speed = config.getint("velocity", 100)
        self.accel = config.getint("accel", 80)
        self.mmu = self.printer.lookup_object('mmu')
        self._homing_accel = 10

        config.get('endstop_pin') # Ensure endstop is defined

    def sync_print_time(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time and self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time

    def do_enable(self, enable):
        self.sync_print_time()
        stepper_enable = self.printer.lookup_object('stepper_enable')
        if enable:
            for s in self.steppers:
                se = stepper_enable.lookup_enable(s.get_name())
                se.motor_enable(self.next_cmd_time)
        else:
            for s in self.steppers:
                se = stepper_enable.lookup_enable(s.get_name())
                se.motor_disable(self.next_cmd_time)

    def do_set_position(self, setpos):
        self.rail.set_position([setpos, 0., 0.])

    def _submit_move(self, movetime, movepos, speed, accel):
        cp = self.rail.get_commanded_position()
        dist = movepos - cp
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(
            dist, speed, accel)
        self.trapq_append(self.trapq, movetime,
                          accel_t, cruise_t, accel_t,
                          cp, 0., 0., axis_r, 0., 0.,
                          0., cruise_v, accel)
        return movetime + accel_t + cruise_t + accel_t
    def do_move(self, movepos, sync=True):
        self.sync_print_time()
        self.next_cmd_time = self._submit_move(self.next_cmd_time, movepos,
                                               self.speed, self.accel)
        self.rail.generate_steps(self.next_cmd_time)
        self.trapq_finalize_moves(self.trapq, self.next_cmd_time + 99999.9,
                                  self.next_cmd_time + 99999.9)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.note_mcu_movequeue_activity(self.next_cmd_time)
        if sync:
            self.sync_print_time()
    def do_homing_move(self, movepos, speed, accel, triggered, check_trigger):
        self.mmu.movequeues_wait()
        self.do_set_position(0)
        self._homing_accel = accel
        pos = [movepos, 0., 0., 0.]
        endstops = self.rail.get_endstops()
        logging.info('Endstops %s', endstops)

        homing_state = mmu_machine.MmuHoming(self.mmu.printer, self)
        homing_state.set_axes([0])

        phoming = self.printer.lookup_object('homing')
        phoming.manual_home(self, endstops, pos, speed,
                            triggered, check_trigger)

    def get_status(self, eventtime):
        return {'position': self.rail.get_commanded_position()}

    # Toolhead wrappers to support homing
    def flush_step_generation(self):
        self.sync_print_time()
    def get_position(self):
        return [self.rail.get_commanded_position(), 0., 0., 0.]
    def set_position(self, newpos, homing_axes=""):
        self.do_set_position(newpos[0])
    def get_last_move_time(self):
        self.sync_print_time()
        return self.next_cmd_time
    def dwell(self, delay):
        self.next_cmd_time += max(0., delay)
    def drip_move(self, newpos, speed, drip_completion):
        # Submit move to trapq
        self.sync_print_time()
        maxtime = self._submit_move(self.next_cmd_time, newpos[0],
                                    speed, self._homing_accel)
        # Drip updates to motors
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.drip_update_time(maxtime, drip_completion, self.steppers)
        # Clear trapq of any remaining parts of movement
        reactor = self.printer.get_reactor()
        self.trapq_finalize_moves(self.trapq, reactor.NEVER, 0)
        self.rail.set_position([newpos[0], 0., 0.])
        self.sync_print_time()
    def get_kinematics(self):
        return self
    def get_steppers(self):
        return self.steppers
    def calc_position(self, stepper_positions):
        return [stepper_positions[self.rail.get_name()], 0., 0.]

    #
    # def move_idler_to_position(self, position):
    #     cur_pos = self.trapq.get_position()
    #     distance = position - cur_pos
    #     if distance == 0:
    #         return
    #
    #     move = self.trapq.set_trapezoid_move(
    #         move_dist=distance,
    #         accel=500.0,
    #         cruise_velocity=20.0,
    #         exit_velocity=0.0
    #     )
    #     move.drain()
    #
    # def set_position(self, position):
    #     toolhead = self.printer.lookup_object('toolhead')
    #     toolhead.wait_moves()
    #     self.stepper.set_position([position, 0, 0])
    #
    #     # Move to the specified position
    #     move_speed = 50  # mm/s - adjust as needed
    #     accel = 1000     # mm/s^2 - adjust as needed
    #     toolhead.manual_move([position, 0, 0], move_speed, accel)
    #     toolhead.wait_moves()
    #
    # def manual_move(self, dist, speed, accel=0.):
    #     toolhead = self.printer.lookup_object('toolhead')
    #     toolhead.flush_step_generation()
    #     self.stepper.set_position((0., 0., 0.))
    #     axis_r, accel_t, cruise_t, cruise_v = calc_move_time(dist, speed, accel)
    #     print_time = toolhead.get_last_move_time()
    #     self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
    #                       0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
    #     print_time = print_time + accel_t + cruise_t + accel_t
    #     self.stepper.generate_steps(print_time)
    #     self.trapq_finalize_moves(self.trapq, print_time + 99999.9,
    #                               print_time + 99999.9)
    #     toolhead.note_mcu_movequeue_activity(print_time)
    #     toolhead.dwell(accel_t + cruise_t + accel_t)
    #     toolhead.flush_step_generation()
    #
    # def _force_enable(self, stepper):
    #     toolhead = self.printer.lookup_object('toolhead')
    #     print_time = toolhead.get_last_move_time()
    #     stepper_enable = self.printer.lookup_object('stepper_enable')
    #     enable = stepper_enable.lookup_enable(stepper.get_name())
    #     was_enable = enable.is_motor_enabled()
    #     if not was_enable:
    #         enable.motor_enable(print_time)
    #         toolhead.dwell(STALL_TIME)
    #     return was_enable
    #
    # def _restore_enable(self, stepper, was_enable):
    #     if not was_enable:
    #         toolhead = self.printer.lookup_object('toolhead')
    #         toolhead.dwell(STALL_TIME)
    #         print_time = toolhead.get_last_move_time()
    #         stepper_enable = self.printer.lookup_object('stepper_enable')
    #         enable = stepper_enable.lookup_enable(stepper.get_name())
    #         enable.motor_disable(print_time)
    #         toolhead.dwell(STALL_TIME)
    #
    # def home_stepper(self):
    #     # Clear any prior move
    #     self.trapq.flush()
    #
    #     # Enable stepper
    #     self.stepper.enable()
    #
    #     # Set a high backoff speed and accel
    #     accel = 500.0  # mm/s^2
    #     speed = 10.0  # mm/s
    #     distance = -10.0  # Move backwards
    #
    #     # Start the move
    #     self.trapq.set_positioning(0.0)  # Start from position 0 (assumed)
    #     move = self.trapq.set_trapezoid_move(
    #         move_dist=distance,
    #         accel=accel,
    #         cruise_velocity=speed,
    #         exit_velocity=0.0,
    #     )
    #
    #     # Wait until move completes or endstop triggers
    #     move.drain()
    #
    #     endstop_triggered = self.stepper.query_endstop()
    #     if endstop_triggered:
    #         self.stepper.get_kinematics().set_position(0.0)
    #     else:
    #         raise self.gcmd.error("Homing failed: Endstop not triggered")
    #
    #     # When the move stops due to virtual endstop (StallGuard)
    #     # Set current position as home
    #     self.stepper.get_kinematics().set_position(0.0)

def load_config(config):
    return MmuIdler(config)