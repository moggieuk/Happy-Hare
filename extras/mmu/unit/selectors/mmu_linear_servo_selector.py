# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of LinearSelector:
#  Implements Linear Selector for type-A MMU's without servo
#  - Stepper controlled linear movement with endstop
#
# Implements commands:
#    MMU_CALIBRATE_SELECTOR
#    MMU_SOAKTEST_SELECTOR (PhysicalSelector)
#
# LinearServoSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  - Supports type-A classic MMU's like ERCFv1.1, ERCFv2.0 and Tradrack
#
# Implements commands:
#    MMU_CALIBRATE_SELECTOR (LinearSelector)
#    MMU_SOAKTEST_SELECTOR (LinearSelector)
#    MMU_SERVO (LinearSelectorServo)
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, traceback

# Klipper imports
from ....homing             import Homing, HomingMove

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...commands            import register_command
from ...mmu_base_parameters import ParamSpec
from ...mmu_unit            import DRIVE_GEAR_ONLY
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_linear_selector   import LinearSelector, LinearSelectorParameters


# -----------------------------------------------------------------------------------------------------------
# Additional parameters for linear servo selector
# -----------------------------------------------------------------------------------------------------------

class LinearServoSelectorParameters(LinearSelectorParameters):

    _SPECS = (*LinearSelectorParameters._SPECS,
        ParamSpec('servo_down_angle',         'int',    90, section="SERVO", hidden=True),
        ParamSpec('servo_up_angle',           'int',    90, section="SERVO", hidden=True),
        ParamSpec('servo_move_angle',         'int', lambda self: self.servo_up_angle, section="SERVO", hidden=True),

        ParamSpec('servo_duration',           'float', 0.2, section="SERVO", limits=dict(minval=0.1)),
        ParamSpec('servo_always_active',      'int',   0,   section="SERVO", limits=dict(minval=0, maxval=1)),
        ParamSpec('servo_active_down',        'int',   0,   section="SERVO", limits=dict(minval=0, maxval=1)),
        ParamSpec('servo_dwell',              'float', 0.4, section="SERVO", limits=dict(minval=0.1)),
        ParamSpec('servo_buzz_gear_on_down',  'int',   3,   section="SERVO", limits=dict(minval=0, maxval=10)),
    )



# -----------------------------------------------------------------------------------------------------------
# LinearServoSelector implementation
# -----------------------------------------------------------------------------------------------------------

class LinearServoSelector(LinearSelector):
    """
    Linear selector variant that enables servo-controlled filament gripping.

    Extends LinearSelector by constructing the selector with its servo component
    and supporting the additional servo-related commands and behaviors.
    """
    PARAMS_CLS = LinearServoSelectorParameters

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        self.servo = LinearSelectorServo(config, mmu_unit, self)


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.servo.handle_connect()

    def handle_ready(self):
        super().handle_ready()

    def handle_disconnect(self):
        super().handle_disconnect()

    def filament_drive(self, buzz_gear=True):
        return self.servo.servo_down(buzz_gear=buzz_gear)

    def filament_release(self, measure=False):
        return self.servo.servo_up(measure=measure)

    def filament_hold_move(self): # AKA position for holding filament and moving selector
        return self.servo.servo_move()

    def get_filament_grip_state(self):
        return self.servo.get_filament_grip_state()

    def disable_motors(self):
        super().disable_motors()
        self.servo.disable_motors()

    def buzz_motor(self, motor):
        if motor == "servo":
            self.servo.buzz_motor()
            return True
        return super().buzz_motor(motor)

    def get_status(self, eventtime):
        status = super().get_status(eventtime)
        status.update(self.servo.get_status(eventtime))
        return status

    def get_mmu_status_config(self):
        return super().get_mmu_status_config() + self.servo.get_mmu_status_config()



# -----------------------------------------------------------------------------------------------------------
# Servo controller for LinearSelector with grip/release/hold positions
# -----------------------------------------------------------------------------------------------------------

# Servo states for 3-position grip implementation (allows for separate "move" position)
SERVO_MOVE_STATE      = FILAMENT_HOLD_STATE
SERVO_DOWN_STATE      = FILAMENT_DRIVE_STATE
SERVO_UP_STATE        = FILAMENT_RELEASE_STATE
SERVO_UNKNOWN_STATE   = FILAMENT_UNKNOWN_STATE

class LinearSelectorServo:
    """
    Servo controller for LinearSelector with grip/release/hold positions.

    Provides servo position management (up/move/down), optional gear buzzing on
    grip, calibration persistence via mmu_vars.cfg, and a MMU_SERVO command for
    manual movement and saving of positions.
    """

    def __init__(self, config, mmu_unit, selector):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.selector = selector
        self.printer = config.get_printer()

        self.params = self.p = selector.p

        # Default to config angles. This will be overrided by calibration in connect()
        self.servo_angles = {
            'down': self.p.servo_down_angle,
            'up': self.p.servo_up_angle,
            'move': self.p.servo_move_angle,
        }

        # Get hardware
        self.servo = self.mmu_unit.selector_servo
        if not self.servo:
            raise self.config.error("Selector servo not found")

        # Register GCODE commands specific to this module
        try:
            register_command(MmuServoCommand)
        except KeyError:
            pass # Already registered

        self._reinit()

    def _reinit(self):
        self.servo_state = SERVO_UNKNOWN_STATE
        self.servo_angle = SERVO_UNKNOWN_STATE

    def handle_connect(self):
        """
        Initialize shared MMU references and load saved servo angle calibrations.

        Merges any persisted VARS_MMU_SELECTOR_SERVO_ANGLES values into the configured
        servo angle map.
        """
        self.mmu = self.mmu_unit.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager

        # Override defaults with saved/calibrated servo positions (set with MMU_SERVO)
        try:
            self.var_manager.upgrade(VARS_MMU_SELECTOR_SERVO_ANGLES, self.mmu_unit.name) # v3 upgrade
            servo_angles = self.var_manager.get(VARS_MMU_SELECTOR_SERVO_ANGLES, {}, namespace=self.mmu_unit.name)
            self.servo_angles.update(servo_angles)
        except Exception as e:
            raise self.config.error("Exception whilst parsing servo angles from 'mmu_vars.cfg': %s" % str(e))

    def _set_servo_angle(self, angle):
        self.servo.set_position(angle=angle, duration=None if self.p.servo_always_active else self.p.servo_duration)
        self.servo_angle = angle
        self.servo_state = SERVO_UNKNOWN_STATE

    def _servo_save_pos(self, pos):
        if self.servo_angle != SERVO_UNKNOWN_STATE:
            self.servo_angles[pos] = self.servo_angle
            self.var_manager.set(VARS_MMU_SELECTOR_SERVO_ANGLES, self.servo_angles, write=True, namespace=self.mmu_unit.name)
            self.mmu.log_info("Servo angle '%d' for position '%s' has been saved" % (self.servo_angle, pos))
        else:
            self.mmu.log_info("Servo angle unknown")

    def servo_down(self, buzz_gear=True):
        """
        Move servo to the filament-drive position, optionally buzzing the gear.

        When configured, performs small gear oscillations after moving down to
        ensure filament is seated, preserving encoder distance across the buzz.
        """
        if self.mmu._is_running_test: return # Save servo while testing
        if self.mmu.gate_selected == TOOL_GATE_BYPASS: return
        if self.servo_state == SERVO_DOWN_STATE: return
        self.mmu.log_trace("Setting servo to down (filament drive) position at angle: %d" % self.servo_angles['down'])

        if buzz_gear and self.p.servo_buzz_gear_on_down > 0:
            self.mmu_unit.mmu_toolhead.sync(DRIVE_GEAR_ONLY) # Must be in correct sync mode before buzz to avoid delay

        self.mmu.movequeues_wait() # Probably not necessary
        initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
        self.servo.set_position(angle=self.servo_angles['down'], duration=None if self.p.servo_active_down or self.p.servo_always_active else self.p.servo_duration)

        if self.servo_angle != self.servo_angles['down'] and buzz_gear and self.p.servo_buzz_gear_on_down > 0:
            for _ in range(self.p.servo_buzz_gear_on_down):
                self.mmu.trace_filament_move(None, 0.8, speed=25, accel=self.mmu_unit.p.gear_buzz_accel, encoder_dwell=None, speed_override=False)
                self.mmu.trace_filament_move(None, -0.8, speed=25, accel=self.mmu_unit.p.gear_buzz_accel, encoder_dwell=None, speed_override=False)
            self.mmu.movequeues_dwell(max(self.p.servo_dwell, self.p.servo_duration, 0))

        self.servo_angle = self.servo_angles['down']
        self.servo_state = SERVO_DOWN_STATE
        self.mmu.set_encoder_distance(initial_encoder_position)
        self.mmu.mmu_macro_event(MACRO_EVENT_FILAMENT_GRIPPED)

    def servo_move(self): # Position servo for selector movement
        if self.mmu._is_running_test: return # Save servo while testing
        if self.servo_state == SERVO_MOVE_STATE: return
        self.mmu.log_trace("Setting servo to move (filament hold) position at angle: %d" % self.servo_angles['move'])
        if self.servo_angle != self.servo_angles['move']:
            self.mmu.movequeues_wait()
            self.servo.set_position(angle=self.servo_angles['move'], duration=None if self.p.servo_always_active else self.p.servo_duration)
            self.mmu.movequeues_dwell(max(self.p.servo_dwell, self.p.servo_duration, 0))
            self.servo_angle = self.servo_angles['move']
            self.servo_state = SERVO_MOVE_STATE

    def servo_up(self, measure=False):
        """
        Move servo to the filament-release position, optionally measuring springback.

        When measure=True, reports encoder delta after releasing and reverts the
        encoder position to avoid double-counting springback.
        """
        if self.mmu._is_running_test: return 0. # Save servo while testing
        if self.servo_state == SERVO_UP_STATE: return 0.
        self.mmu.log_trace("Setting servo to up (filament released) position at angle: %d" % self.servo_angles['up'])
        delta = 0.
        if self.servo_angle != self.servo_angles['up']:
            self.mmu.movequeues_wait()
            if measure:
                initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
            self.servo.set_position(angle=self.servo_angles['up'], duration=None if self.p.servo_always_active else self.p.servo_duration)
            self.mmu.movequeues_dwell(max(self.p.servo_dwell, self.p.servo_duration, 0))
            if measure:
                # Report on spring back in filament then revert counter
                delta = self.mmu.get_encoder_distance() - initial_encoder_position
                if delta > 0.:
                    self.mmu.log_debug("Spring in filament measured  %.1fmm - adjusting encoder" % delta)
                    self.mmu.set_encoder_distance(initial_encoder_position, dwell=None)
        self.servo_angle = self.servo_angles['up']
        self.servo_state = SERVO_UP_STATE
        return delta

    # De-energize servo if 'servo_always_active' or 'servo_active_down' are being used
    def servo_off(self):
        self.servo.set_position(width=0, duration=None)

    def get_filament_grip_state(self):
        return self.servo_state

    def disable_motors(self):
        self.servo_move()
        self.servo_off()
        self._reinit() # Reset state

    def enable_motors(self):
        self.servo_move()

    def buzz_motor(self):
        self.mmu.movequeues_wait()
        old_state = self.servo_state
        low=min(self.servo_angles['down'], self.servo_angles['up'])
        high=max(self.servo_angles['down'], self.servo_angles['up'])
        mid = (low + high) / 2
        move = (high - low) / 4
        duration=None if self.p.servo_always_active else self.p.servo_duration

        self.servo.set_position(angle=mid, duration=duration)
        self.mmu.movequeues_dwell(max(self.p.servo_duration, 0.5), mmu_toolhead=False)
        self.servo.set_position(angle=(mid - move), duration=duration)
        self.mmu.movequeues_dwell(max(self.p.servo_duration, 0.5), mmu_toolhead=False)
        self.servo.set_position(angle=(mid + move), duration=duration)
        self.mmu.movequeues_dwell(max(self.p.servo_duration, 0.5), mmu_toolhead=False)
        self.mmu.movequeues_wait()

        if old_state == SERVO_DOWN_STATE:
            self.servo_down(buzz_gear=False)
        elif old_state == SERVO_MOVE_STATE:
            self.servo_move()
        else:
            self.servo_up()

    def get_mmu_status_config(self):
        msg = " Servo in %s position." % ("RELEASE" if self.servo_state == SERVO_UP_STATE else \
                "GRIP" if self.servo_state == SERVO_DOWN_STATE else "MOVE" if self.servo_state == SERVO_MOVE_STATE else "unknown")
        return msg

    def get_status(self, eventtime):
        return {
            'servo': "Up"   if self.servo_state == SERVO_UP_STATE else
                     "Down" if self.servo_state == SERVO_DOWN_STATE else
                     "Move" if self.servo_state == SERVO_MOVE_STATE else
                     "Unknown",
        }



# -----------------------------------------------------------------------------------------------------------
# MMU_SERVO command
#  This "registered command" will be conditionally registered, then instantiated later by the main
#  mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

from ...commands.mmu_base_command import *

class MmuServoCommand(BaseCommand):

    CMD = "MMU_SERVO"

    HELP_BRIEF = "Move MMU servo to position specified position or angle"
    HELP_PARAMS = (
        "MMU_SERVO: %s\n" % HELP_BRIEF
        + "UNIT   = #(int) Optional, defaults to all units\n"
        + "RESET  = [0|1]  Clear saved calibration\n"
        + "SAVE   = [0|1]  Save current position against pos if calibrating\n"
        + "POS    = [off|up|move|down]\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        """
        Handle MMU_SERVO command for moving/calibrating the selector servo.

        Supports RESET of saved angles, POS-based movement (off/up/move/down),
        optional SAVE to persist the current angle for a named position, and
        direct ANGLE=<n> movement when POS is omitted.
        """
        mmu = mmu_unit.mmu

        if self.check_if_disabled(): return

        if not hasattr(mmu_unit.selector, "servo"):
            raise gmcd.error("No servo fitted to selector on MMU %s" % mmu_unit.name)
        servo = mmu_unit.selector.servo

        reset = gcmd.get_int('RESET', 0)
        save = gcmd.get_int('SAVE', 0)
        pos = gcmd.get('POS', "").lower()

        if reset:
            mmu_unit.mmu_machine.var_manager.delete(VARS_MMU_SELECTOR_SERVO_ANGLES, namespace=mmu_unit.name, write=True)
            mmu.log_info("Calibrated servo angles have be reset to configured defaults")
        elif pos == "off":
            servo.servo_off() # For 'servo_always_active' case
        elif pos == "up":
            if save:
                servo._servo_save_pos(pos)
            else:
                servo.servo_up()
        elif pos == "move":
            if save:
                servo._servo_save_pos(pos)
            else:
                servo.servo_move()
        elif pos == "down":
            if mmu_unit.selector.check_if_unit_bypass(): return
            if save:
                servo._servo_save_pos(pos)
            else:
                servo.servo_down()
        elif save:
            mmu.log_error("Servo position not specified for save")
        elif pos == "":
            if mmu_unit.selector.check_if_unit_bypass(): return
            angle = gcmd.get_int('ANGLE', None)
            if angle is not None:
                mmu.log_debug("Setting servo to angle: %d" % angle)
                servo._set_servo_angle(angle)
            else:
                mmu.log_always("Current servo angle: %d, Positions: %s" % (servo.servo_angle, servo.servo_angles))
                mmu.log_info("Use POS= or ANGLE= to move position")
        else:
            mmu.log_error("Unknown servo position '%s'" % pos)
