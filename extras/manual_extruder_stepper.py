# Happy Hare MMU Software
# Support for an extruder stepper that can be manually controlled when it is not synced to its motion queue
#
# Copyright (C) 2023  Cambridge Yang <camyang@csail.mit.edu>
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import stepper, chelper, logging
from kinematics import extruder as kinematics_extruder
from . import manual_stepper

class ManualExtruderStepper(kinematics_extruder.ExtruderStepper, manual_stepper.ManualStepper, object):
    """Extruder stepper that can be manually controlled when it is not synced to its motion queue"""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat('pressure_advance_smooth_time', 0.040, above=0., maxval=.200)

        # Setup stepper
        if config.get('endstop_pin', None) is not None:
            self.can_home = True
            self.stepper = stepper.PrinterRail(config, need_position_minmax=False, default_position_endstop=0.)
            self.steppers = self.stepper.get_steppers()
        else:
            self.can_home = False
            self.stepper = stepper.PrinterStepper(config)
            self.steppers = [self.stepper]
        self.rail = self.stepper # For forwarding manual_stepper...

        self.velocity = config.getfloat('velocity', 5., above=0.)
        self.accel = self.homing_accel = config.getfloat('accel', 0., minval=0.)
        self.next_cmd_time = 0.

        ffi_main, ffi_lib = chelper.get_ffi()

        # Setup iterative solver for manual movement
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)

        # Setup extruder kinematics
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(), ffi_lib.free)

        # Get the kinematics for the steppers under manual mode
        # by temporarily setting the extruder kinematics to the extruder
        # kinematics then setting back. This avoid using private APIs
        self.alt_stepper_sks = [s.set_stepper_kinematics(self.sk_extruder)
                                for s in self.steppers]
        # Set back to the manual kinematics
        self._set_manual_kinematics()
        self.motion_queue = None

        # Register commands
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        gcode = self.printer.lookup_object('gcode')

        # Extruder commands
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

        # Manual Stepper commands
        gcode.register_mux_command('MANUAL_STEPPER', "STEPPER",
                                   self.name, self.cmd_MANUAL_STEPPER,
                                   desc=self.cmd_MANUAL_STEPPER_help)


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
        return manual_stepper.ManualStepper.cmd_MANUAL_STEPPER(self, gcmd)

    def sync_to_extruder(self, extruder_name):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        if not extruder_name:
            self._set_manual_kinematics()
            self.motion_queue = None
            return
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, kinematics_extruder.PrinterExtruder):
            raise self.printer.command_error("'%s' is not a valid extruder." % (extruder_name,))
        for s in self.steppers:
            s.set_stepper_kinematics(self.sk_extruder)
        self.rail.set_trapq(extruder.get_trapq())
        self.rail.set_position([extruder.last_position, 0., 0.])
        self.motion_queue = extruder_name

    def _set_manual_kinematics(self):
        for s, sk in zip(self.steppers, self.alt_stepper_sks):
            s.set_stepper_kinematics(sk)
        self.rail.set_trapq(self.trapq)

    def is_synced(self):
        return self.motion_queue != None

def load_config_prefix(config):
    return ManualExtruderStepper(config)

