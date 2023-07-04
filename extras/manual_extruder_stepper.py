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
import stepper, chelper, logging, contextlib
from kinematics import extruder as kinematics_extruder
from . import manual_stepper, manual_mh_stepper


class ManualExtruderStepper(manual_mh_stepper.ManualMhStepper, kinematics_extruder.ExtruderStepper, object):
    """Extruder stepper that can be manually controlled when it is not synced to its motion queue"""

    def __init__(self, config):
        super(ManualExtruderStepper, self).__init__(config) # Will call ManualMhStepper.__init__()

        # Register variation of MANUAL_STEPPER command for linked extruder control
        gcode = self.printer.lookup_object('gcode')
        stepper_name = config.get_name().split()[1]
        gcode.register_mux_command('MANUAL_EXTRUDER_STEPPER', "STEPPER",
                                   stepper_name, self.cmd_MANUAL_EXTRUDER_STEPPER,
                                   desc=self.cmd_MANUAL_EXTRUDER_STEPPER_help)

        # Extruder setup
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat('pressure_advance_smooth_time', 0.040, above=0., maxval=.200)

        # Setup extruder kinematics
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(), ffi_lib.free)

        # Get the kinematics for the steppers under manual mode
        # by temporarily setting to the extruder kinematics then setting back.
        # This avoids using private APIs
        self.alt_stepper_sks = [s.set_stepper_kinematics(self.sk_extruder) for s in self.steppers]
        # Set back to the manual kinematics
        self._set_manual_kinematics()
        self.motion_queue = self.synced_extruder_name = None

        # Setup kinematics that can be passed to extruder for use when homing
        self.linked_move_sk = ffi_main.gc(ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        # Register extruder commands
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        if self.name == 'extruder':
            gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER", None,
                                       self.cmd_default_SET_PRESSURE_ADVANCE,
                                       desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_PRESSURE_ADVANCE,
                                   desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_EXTRUDER_ROTATION_DISTANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_E_ROTATION_DISTANCE,
                                   desc=self.cmd_SET_E_ROTATION_DISTANCE_help)
        gcode.register_mux_command("SYNC_EXTRUDER_MOTION", "EXTRUDER",
                                   self.name, self.cmd_SYNC_EXTRUDER_MOTION,
                                   desc=self.cmd_SYNC_EXTRUDER_MOTION_help)
        gcode.register_mux_command("SET_EXTRUDER_STEP_DISTANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_E_STEP_DISTANCE,
                                   desc=self.cmd_SET_E_STEP_DISTANCE_help)
        gcode.register_mux_command("SYNC_STEPPER_TO_EXTRUDER", "STEPPER",
                                   self.name, self.cmd_SYNC_STEPPER_TO_EXTRUDER,
                                   desc=self.cmd_SYNC_STEPPER_TO_EXTRUDER_help)

    def do_enable(self, enable):
        assert self.motion_queue is None
        return super(ManualExtruderStepper, self).do_enable(enable)
    
    def do_set_position(self, setpos):
        assert self.motion_queue is None
        return super(ManualExtruderStepper, self).do_set_position(setpos)
    
    def do_move(self, movepos, speed, accel, sync=True):
        assert self.motion_queue is None
        return super(ManualExtruderStepper, self).do_move(movepos, speed, accel, sync)

    def do_homing_move(self, movepos, speed, accel, triggered, check_trigger):
        assert self.motion_queue is None
        return super(ManualExtruderStepper, self).do_homing_move(movepos, speed, accel, triggered, check_trigger)

    def cmd_MANUAL_STEPPER(self, gcmd):
        if self.motion_queue is not None:
            raise self.printer.command_error("Cannot manual move: stepper synced to motion queue")
        return super(ManualExtruderStepper, self).cmd_MANUAL_STEPPER(gcmd)

    cmd_MANUAL_EXTRUDER_STEPPER_help = "Command a manually configured stepper with linked extruder"
    def cmd_MANUAL_EXTRUDER_STEPPER(self, gcmd):
        if self.motion_queue is not None:
            raise self.printer.command_error("Cannot manual move: stepper synced to motion queue")
        extruder_name = gcmd.get('EXTRUDER', "extruder") # Added
        endstop_name = gcmd.get('ENDSTOP', "default") # Added by ManualMhStepper
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            super(ManualExtruderStepper, self).do_enable(enable)
        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            super(ManualExtruderStepper, self).do_set_position(setpos)
        speed = gcmd.get_float('SPEED', self.velocity, above=0.)
        accel = gcmd.get_float('ACCEL', self.accel, minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('MOVE')
            self.do_linked_homing_move(movepos, speed, accel, homing_move > 0, abs(homing_move) == 1, extruder_name, endstop_name)
        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            sync = gcmd.get_int('SYNC', 1)
            self.do_linked_move(movepos, speed, accel, sync, extruder_name)
        elif gcmd.get_int('SYNC', 0):
            super(ManualExtruderStepper, self).sync_print_time()

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
            self.synced_extruder_name = None
            return
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, kinematics_extruder.PrinterExtruder):
            raise self.printer.command_error("Extruder named '%s' is not found" % extruder_name)
        for s in self.steppers:
            s.set_stepper_kinematics(self.sk_extruder)
        self.rail.set_position([extruder.last_position, 0., 0.])
        self.rail.set_trapq(extruder.get_trapq())
        self.motion_queue = extruder_name
        self.synced_extruder_name = extruder_name

    def is_synced(self):
        return self.motion_queue != None

    @contextlib.contextmanager
    def _with_linked_extruder(self, extruder_name):
        manual_extruder_stepper = self.printer.lookup_object("manual_extruder_stepper %s" % extruder_name, None)
        if not manual_extruder_stepper:
            raise self.printer.command_error("ManualExtruderStepper named '%s' not found" % extruder_name)
        extruder_stepper = manual_extruder_stepper.stepper

        # Switch manual stepper to manual mode
        manual_stepper_mq = self.motion_queue
        manual_trapq = self.trapq
        manual_steppers = self.steppers
        self.sync_to_extruder(None)

        # Sync extruder to manual stepper
        # We do this by injecting the extruder stepper into the manual stepper's rail
        prev_manual_steppers = self.steppers
        prev_manual_rail_steppers = self.rail.steppers
        self.steppers = self.steppers + [extruder_stepper]
        self.rail.steppers = self.rail.steppers + [extruder_stepper]

        # Extruder must look like it has always been part of the rail (position important!)
        prev_extruder_sk = extruder_stepper.set_stepper_kinematics(self.linked_move_sk)
        prev_extruder_trapq = extruder_stepper.set_trapq(manual_trapq)
        pos = manual_steppers[0].get_commanded_position()
        extruder_stepper.set_position([pos, 0., 0.])

        # Yield to caller
        try:
            yield self

        finally:
            # Restore previous state
            self.steppers = prev_manual_steppers
            self.rail.steppers = prev_manual_rail_steppers
            extruder_stepper.set_stepper_kinematics(prev_extruder_sk)
            extruder_stepper.set_trapq(prev_extruder_trapq)
            self.sync_to_extruder(manual_stepper_mq)

    # Perform regular move bringing the extruder along for the ride
    def do_linked_move(self, movepos, speed, accel, sync=True, extruder_name="extruder"):
        assert self.motion_queue is None
        with self._with_linked_extruder(extruder_name):
            super(ManualExtruderStepper, self).do_move(movepos, speed, accel, sync)

    # Perform homing move using specified endstop bringing the extruder along for the ride
    def do_linked_homing_move(self, movepos, speed, accel, triggered=True, check_trigger=True, extruder_name="extruder", endstop_name=None):
        assert self.motion_queue is None
        with self._with_linked_extruder(extruder_name):
            super(ManualExtruderStepper, self).do_mh_homing_move(movepos, speed, accel, triggered, check_trigger, endstop_name)

def load_config_prefix(config):
    return ManualExtruderStepper(config)

