# Happy Hare MMU Software
# Support for a manual stepper than can be synced to the extruder or take over the extruder stepper for homing purposes
# Designed for "gear" stepper on MMU
#
# Copyright (C) 2023  Cambridge Yang <camyang@csail.mit.edu>
#                     moggieuk#6538 (discord) moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import stepper, chelper, logging, contextlib # PAUL need contextlib?
from kinematics import extruder as kinematics_extruder
from . import manual_stepper, manual_mmu_stepper


class ManualGearStepper(manual_mmu_stepper.ManualMmuStepper, kinematics_extruder.ExtruderStepper, object):
    """Extruder stepper that can be manually controlled when it is not synced to its motion queue"""

    def __init__(self, config):
        super(ManualGearStepper, self).__init__(config) # Will call ManualMmuStepper.__init__()

        # Extruder setup
        self.stepper = self.rail
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat('pressure_advance_smooth_time', 0.040, above=0., maxval=.200)

        # Setup extruder kinematics
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(), ffi_lib.free)

        # Get the kinematics for the steppers under manual mode
        # by temporarily setting the extruder kinematics to the extruder
        # kinematics then setting back. This avoid using private APIs
        self.alt_stepper_sks = [s.set_stepper_kinematics(self.sk_extruder)
                                for s in self.steppers]
        # Set back to the manual kinematics
        self._set_manual_kinematics()
        self.motion_queue = None

        # Setup kinematics that can be passed to extruder for use when homing
        self.toolhead_homing_sk = ffi_main.gc(ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        # Register variation of MANUL_STEPPER command for linked extruder control
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('MANUAL_LINKED_STEPPER',
                    self.cmd_MANUAL_LINKED_STEPPER,
                    desc = self.cmd_MANUAL_LINKED_STEPPER_help)

        self.printer.register_event_handler("klippy:connect", self._handle_connect)

    def do_enable(self, enable):
        assert self.motion_queue is None
        return super(ManualGearStepper, self).do_enable(enable)
    
    def do_set_position(self, setpos):
        assert self.motion_queue is None
        return super(ManualGearStepper, self).do_set_position(setpos)
    
    def do_move(self, movepos, speed, accel, sync=True):
        assert self.motion_queue is None
        return super(ManualGearStepper, self).do_move(movepos, speed, accel, sync)

    def do_homing_move(self, movepos, speed, accel, triggered, check_trigger):
        assert self.motion_queue is None
        return super(ManualGearStepper, self).do_homing_move(movepos, speed, accel, triggered, check_trigger)

    def cmd_MANUAL_STEPPER(self, gcmd):
        if self.motion_queue is not None:
            raise self.printer.command_error("Cannot manual move: stepper synced to motion queue")
        return super(ManualGearStepper, self).cmd_MANUAL_STEPPER(gcmd)

    cmd_MANUAL_LINKED_STEPPER_help = "Command a manually configured stepper with linked extruder"
    def cmd_MANUAL_LINKED_STEPPER(self, gcmd):
        extruder_name = gcmd.get('EXTRUDER', "extruder")
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            super(ManualGearStepper, self).do_enable(enable)
        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            super(ManualGearStepper, self).do_set_position(setpos)
        speed = gcmd.get_float('SPEED', self.velocity, above=0.)
        accel = gcmd.get_float('ACCEL', self.accel, minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('MOVE')
            self.do_linked_homing_move(extruder_name, movepos, speed, accel, homing_move > 0, abs(homing_move) == 1)
        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            sync = gcmd.get_int('SYNC', 1)
            self.do_linked_move(extruder_name, movepos, speed, accel, sync)
        elif gcmd.get_int('SYNC', 0):
            super(ManualGearStepper, self).sync_print_time()

    def _set_manual_kinematics(self):
        for s, sk in zip(self.steppers, self.alt_stepper_sks):
            s.set_stepper_kinematics(sk)
        self.rail.set_trapq(self.trapq)

    def sync_to_extruder(self, extruder_name):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        if not extruder_name:
            self._set_manual_kinematics()
            self.motion_queue = None
            return
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, kinematics_extruder.PrinterExtruder):
            raise self.printer.command_error("Extruder named '%s' is not found" % extruder_name)
        for s in self.steppers:
            s.set_stepper_kinematics(self.sk_extruder)
        self.rail.set_trapq(extruder.get_trapq())
        self.rail.set_position([extruder.last_position, 0., 0.])
        self.motion_queue = extruder_name

    def is_synced(self):
        return self.motion_queue != None

    @contextlib.contextmanager
    def _with_linked_extruder(self, extruder_name):
        extruder = self.printer.lookup_object(extruder_name, None)
        if not extruder:
            raise self.printer.command_error("Extruder named '%s' not found" % extruder_name)
        toolhead_stepper = extruder.extruder_stepper.stepper

        # Switch manual stepper to manual mode
        manual_stepper_mq = self.motion_queue
        manual_trapq = self.trapq
        self.sync_to_extruder(None)
        logging.info("PAUL: manual_stepper_mq = %s" % manual_stepper_mq)
        logging.info("PAUL: manual_trapq = %s" % manual_trapq)

        # Sync toolhead to manual stepper
        # We do this by injecting the toolhead stepper into the manual stepper's rail
        prev_manual_steppers = self.steppers
        prev_manual_rail_steppers = self.rail.steppers
        logging.info("PAUL: prev_manual_steppers = %s" % prev_manual_steppers)
        logging.info("PAUL: prev_manual_rail_steppers = %s" % prev_manual_rail_steppers)
        self.steppers = self.steppers + [toolhead_stepper]
        self.rail.steppers = self.rail.steppers + [toolhead_stepper]
        logging.info("PAUL: self.steppers = %s" % self.steppers)
        logging.info("PAUL: self.rail.steppers = %s" % self.rail.steppers)

        prev_toolhead_trapq = toolhead_stepper.set_trapq(manual_trapq)
        prev_toolhead_sk = toolhead_stepper.set_stepper_kinematics(self.toolhead_homing_sk)
        logging.info("PAUL: prev_toolhead_trapq = %s" % prev_toolhead_trapq)
        logging.info("PAUL: prev_toolhead_sk = %s" % prev_toolhead_sk)

        # Yield to caller
        yield self

        # Restore previous state
        logging.info("PAUL: restoring previous state")
        self.steppers = prev_manual_steppers
        self.rail.steppers = prev_manual_rail_steppers
        toolhead_stepper.set_trapq(prev_toolhead_trapq)
        toolhead_stepper.set_stepper_kinematics(prev_toolhead_sk)
        self.sync_to_extruder(manual_stepper_mq)

    # Will perform regular move bringing the extruder along for the ride
    def do_linked_move(self, extruder_name, movepos, speed, accel, sync=True):
        with self._with_linked_extruder(extruder_name):
            super(ManualGearStepper, self).do_move(movepos, speed, accel, sync)

    # Will perform homing move using active endstop bringing the extruder along for the ride
    def do_linked_homing_move(self, extruder_name, movepos, speed, accel, triggered, check_trigger):
        with self._with_linked_extruder(extruder_name):
            super(ManualGearStepper, self).do_homing_move(movepos, speed, accel, triggered=True, check_trigger=True)

def load_config_prefix(config):
    return ManualGearStepper(config)

