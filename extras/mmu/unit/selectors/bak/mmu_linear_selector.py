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
#    MMU_SOAKTEST_SELECTOR
#
# LinearServoSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  - Supports type-A classic MMU's like ERCFv1.1, ERCFv2.0 and Tradrack
#
# Implements commands:
#    MMU_CALIBRATE_SELECTOR
#    MMU_SOAKTEST_SELECTOR
#    MMU_SERVO
#    MMU_GRIP
#    MMU_RELEASE
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
from ....homing          import Homing, HomingMove

# Happy Hare imports
from ...mmu_constants    import *
from ...mmu_utils        import MmuError
from ..mmu_calibrator    import CALIBRATED_SELECTOR
from .mmu_base_selectors import PhysicalSelector


################################################################################
# Linear Selector
#
# Implements Linear Selector for type-A MMU's that uses stepper conrolled
# rail[0] on mmu toolhead
#
# Implements commands:
#   MMU_CALIBRATE_SELECTOR
#   MMU_SOAKTEST_SELECTOR
################################################################################

class LinearSelector(PhysicalSelector):

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.bypass_offset = -1

        # Process config
        self.selector_move_speed = config.getfloat('selector_move_speed', 200, minval=1.)
        self.selector_homing_speed = config.getfloat('selector_homing_speed', 100, minval=1.)
        self.selector_touch_speed = config.getfloat('selector_touch_speed', 60, minval=1.)
        self.selector_touch_enabled = config.getint('selector_touch_enabled', 1, minval=0, maxval=1)
        self.selector_accel = config.getfloat('selector_accel', 1200, above=1.) # PAUL TODO implemenet

        # To simplfy config CAD related parameters are set based on vendor and version setting
        #
        # These are default for ERCFv1.1 - the first MMU supported by Happy Hare
        #  cad_gate0_pos          - approximate distance from endstop to first gate
        #  cad_gate_width         - width of each gate
        #  cad_bypass_offset      - distance from end of travel to the bypass
        #  cad_last_gate_offset   - distance from end of travel to last gate
        #  cad_block_width        - width of bearing block (ERCF v1.1)
        #  cad_bypass_block_width - width of bypass block (ERCF v1.1)
        #  cad_bypass_block_delta - distance from previous gate to bypass (ERCF v1.1)
        #  cad_selector_tolerance - extra movement allowed by selector
        #
        self.cad_gate0_pos = 4.2
        self.cad_gate_width = 21.
        self.cad_bypass_offset = 0
        self.cad_last_gate_offset = 2.
        self.cad_block_width = 5.
        self.cad_bypass_block_width = 6.
        self.cad_bypass_block_delta = 9.
        self.cad_selector_tolerance = 15.

        # Specific vendor build parameters / tuning.
        if self.mmu_unit.mmu_vendor.lower() == VENDOR_ERCF.lower():
            if self.mmu_unit.mmu_version >= 2.0: # V2 community edition
                self.cad_gate0_pos = 4.0
                self.cad_gate_width = 23.
                self.cad_bypass_offset = 0.72
                self.cad_last_gate_offset = 14.4

            else: # V1.1 original
                # Modifications:
                #  t = TripleDecky filament blocks
                #  s = Springy sprung servo selector
                #  b = Binky encoder upgrade
                if "t" in self.mmu_unit.mmu_version_string:
                    self.cad_gate_width = 23. # Triple Decky is wider filament block
                    self.cad_block_width = 0. # Bearing blocks are not used

                if "s" in self.mmu_unit.mmu_version_string:
                    self.cad_last_gate_offset = 1.2 # Springy has additional bump stops

        elif self.mmu_unit.mmu_vendor.lower() == VENDOR_TRADRACK.lower():
            self.cad_gate0_pos = 2.5
            self.cad_gate_width = 17.
            self.cad_bypass_offset = 0     # Doesn't have bypass
            self.cad_last_gate_offset = 0. # Doesn't have reliable hard stop at limit of travel

        # But still allow all CAD parameters to be customized
        self.cad_gate0_pos = config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_bypass_offset = config.getfloat('cad_bypass_offset', self.cad_bypass_offset, minval=0.)
        self.cad_last_gate_offset = config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)
        self.cad_block_width = config.getfloat('cad_block_width', self.cad_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_width = config.getfloat('cad_bypass_block_width', self.cad_bypass_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_delta = config.getfloat('cad_bypass_block_delta', self.cad_bypass_block_delta, above=0.) # ERCF v1.1 only
        self.cad_selector_tolerance = config.getfloat('cad_selector_tolerance', self.cad_selector_tolerance, above=0.) # Extra movement allowed by selector

        # Sub components
        self.servo = LinearSelectorServo(config, mmu_unit, self)

        # Register GCODE commands specific to this module
        self.register_mux_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc=self.cmd_MMU_CALIBRATE_SELECTOR_help)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        # Sub components
        if self.servo:
            self.servo.reinit()

    def handle_connect(self):
        super(LinearSelector, self).handle_connect()

        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]

        # Adjust selector rail limits now we know the config
        self.selector_rail.position_min = -1
        self.selector_rail.position_max = self._get_max_selector_movement()
        self.selector_rail.homing_speed = self.selector_homing_speed
        self.selector_rail.second_homing_speed = self.selector_homing_speed / 2.
        self.selector_rail.homing_retract_speed = self.selector_homing_speed
        self.selector_rail.homing_positive_dir = False

        # Load selector offsets (calibration set with MMU_CALIBRATE_SELECTOR) -------------------------------
        self.var_manager.upgrade(VARS_MMU_SELECTOR_OFFSETS, self.mmu_unit.name) # v3 upgrade
        self.var_manager.upgrade(VARS_MMU_SELECTOR_BYPASS, self.mmu_unit.name) # v3 upgrade

        self.selector_offsets = self.var_manager.get(VARS_MMU_SELECTOR_OFFSETS, None, namespace=self.mmu_unit.name)
        if self.selector_offsets:
            # Ensure list size
            if len(self.selector_offsets) == self.mmu_unit.num_gates:
                self.mmu.log_debug("Loaded saved selector offsets: %s" % self.selector_offsets)
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % VARS_MMU_SELECTOR_OFFSETS)
                self.selector_offsets = self._ensure_list_size(self.selector_offsets, self.mmu_unit.num_gates)

            if not any(x == -1 for x in self.selector_offsets):
                self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        else:
            self.mmu.log_always("Warning: Selector offsets not found in mmu_vars.cfg. Probably not calibrated")
            self.selector_offsets = [-1] * self.mmu_unit.num_gates
        self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, namespace=self.mmu_unit.name)

        self.bypass_offset = self.var_manager.get(VARS_MMU_SELECTOR_BYPASS, -1, namespace=self.mmu_unit.name)
        if self.bypass_offset > 0:
            self.mmu.log_debug("Loaded saved bypass offset: %s" % self.bypass_offset)
        else:
            self.bypass_offset = -1 # Ensure -1 value for uncalibrated / non-existent
        self.var_manager.set(VARS_MMU_SELECTOR_BYPASS, self.bypass_offset, namespace=self.mmu_unit.name)

        # See if we have a TMC controller setup with stallguard
        if not self.mmu_unit.selector_touch:
            self.mmu.log_debug("Selector 'touch' not setup. Cannot automatically recovery from gate blockage")
        else:
            self.mmu.log_debug("Selector 'touch' movement and recovery possible")

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    def handle_disconnect(self):
        super(LinearSelector, self).handle_disconnect()

    def handle_ready(self):
        super(LinearSelector, self).handle_ready()

    def home(self, force_unload = None):
        if self.mmu.check_if_bypass(): return
        with self.mmu.wrap_action(ACTION_HOMING):
            self.mmu.log_info("Homing MMU...")
            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))
            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)
            elif force_unload is None and self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self.mmu.unload_sequence()
            self._home_selector()

    # Physically move selector to correct gate position
    def select_gate(self, gate):
       super().select_gate(gate) # Important because LinearMultiGear*Selector inherits from this class

        if gate == self.mmu.gate_selected: return # PAUL if local_gate == self.gate_selected
        with self.mmu.wrap_action(ACTION_SELECTING):
            self.filament_hold_move()
            if gate == TOOL_GATE_BYPASS: # PAUL if local_gate == TOOL_GATE_BYPASS:
                self._position(self.bypass_offset)
            elif gate >= 0:
                self.mmu.log_error("PAUL: gate=%s, local_gate=%s, sel_offsets=%s" % (gate, self.local_gate(gate), self.selector_offsets))
                self._position(self.selector_offsets[self.local_gate(gate)])

    # Correct rail position for selector
    def restore_gate(self, gate):
       super().select_gate(gate) # Important because LinearMultiGear*Selector inherits from this class

        if gate == TOOL_GATE_BYPASS:
            self.set_position(self.bypass_offset)
        elif gate >= 0:
            self.set_position(self.selector_offsets[self.local_gate(gate)])

    def filament_drive(self, buzz_gear=True):
        if self.servo:
            self.servo.servo_down(buzz_gear=buzz_gear)

    def filament_release(self, measure=False):
        if self.servo:
            return self.servo.servo_up(measure=measure)

    def filament_hold_move(self): # AKA position for holding filament and moving selector
        if self.servo:
            self.servo.servo_move()

    def get_filament_grip_state(self):
        if self.servo:
            return self.servo.get_filament_grip_state()

    def disable_motors(self):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False
        if self.servo:
            self.servo.disable_motors()

    def enable_motors(self):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.mmu_toolhead.get_position()[0]
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        elif motor == "servo" and self.servo:
            self.servo.buzz_motor()
        else:
            return False
        return True

    def has_bypass(self):
        return self.mmu_unit.has_bypass and self.bypass_offset >= 0

    def get_status(self, eventtime):
        status = super(LinearSelector, self).get_status(eventtime)
        if self.servo:
            status.update(self.servo.get_status(eventtime))
        return status

    def get_mmu_status_config(self):
        msg = "\nSelector is NOT HOMED" if not self.is_homed else ""
        if self.servo:
            msg += self.servo.get_mmu_status_config()
        return msg

    def set_test_config(self, gcmd):
        self.selector_move_speed = gcmd.get_float('SELECTOR_MOVE_SPEED', self.selector_move_speed, minval=1.)
        self.selector_homing_speed = gcmd.get_float('SELECTOR_HOMING_SPEED', self.selector_homing_speed, minval=1.)
        self.selector_touch_speed = gcmd.get_float('SELECTOR_TOUCH_SPEED', self.selector_touch_speed, minval=1.)
        self.selector_touch_enabled = gcmd.get_int('SELECTOR_TOUCH_ENABLED', self.selector_touch_enabled, minval=0, maxval=1)

        # Sub components
        if self.servo:
            self.servo.set_test_config(gcmd)

    def get_test_config(self):
        msg = "\n\nSELECTOR:"
        msg += "\nselector_move_speed = %.1f" % self.selector_move_speed
        msg += "\nselector_homing_speed = %.1f" % self.selector_homing_speed
        msg += "\nselector_touch_speed = %.1f" % self.selector_touch_speed
        msg += "\nselector_touch_enabled = %d" % self.selector_touch_enabled

        # Sub components
        if self.servo:
            msg += self.servo.get_test_config()

        return msg

    def check_test_config(self, param):
        return (vars(self).get(param) is None) and (self.servo is None or self.servo.check_test_config(param))

    def get_uncalibrated_gates(self, check_gates):
        return [lgate + self.mmu_unit.first_gate for lgate, value in enumerate(self.selector_offsets) if value == -1 and lgate + self.mmu_unit.first_gate in check_gates]

    # Internal Implementation --------------------------------------------------

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector positions or postion of specified gate"
    cmd_MMU_CALIBRATE_SELECTOR_param_help = (
        "MMU_CALIBRATE_SELECTOR: %s\n" % cmd_MMU_CALIBRATE_SELECTOR_help
        + "UNIT         = #(int)\n"
        + "GATE         = #(int) Optional, default all gates on unit\n"
        + "SAVE         = [0|1]\n"
        + "BYPASS       = [0|1]\n"
        + "BYPASS_BLOCK = [0|1]  ERCFv1.1 only\n"
    )
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_unit.num_gates - 1)
        if gate == -1 and gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
            gate = TOOL_GATE_BYPASS

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_CALIBRATE_SELECTOR_param_help), color=True)
            return

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                self.mmu.calibrating = True
                self.mmu.reinit()
                self.filament_hold_move()
                successful = False
                if gate != -1:
                    successful = self._calibrate_selector(gate, extrapolate=not single, save=save)
                else:
                    successful = self._calibrate_selector_auto(save=save, v1_bypass_block=gcmd.get_int('BYPASS_BLOCK', -1, minval=1, maxval=3))

                if not any(x == -1 for x in self.selector_offsets):
                    self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

                # If not fully calibrated turn off the selector stepper to ease next step, else activate by homing
                if successful and self.calibrator.check_calibrated(CALIBRATED_SELECTOR):
                    self.mmu.log_always("Selector calibration complete")
                    self.mmu.select_tool(0)
                else:
                    self.mmu.motors_onoff(on=False, motor="selector")

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
        finally:
            self.mmu.calibrating = False

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu_unit.num_gates - 1

        if self.mmu_unit.mmu_vendor == VENDOR_ERCF:
            # ERCF Designs
            if self.mmu_unit.mmu_version >= 2.0 or "t" in self.mmu_unit.mmu_version_string:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)
            else:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width) + (n//3) * self.cad_block_width
        else:
            # Everything else
            max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)

        max_movement += self.cad_last_gate_offset if gate in [TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.cad_selector_tolerance
        return max_movement

    # Manual selector offset calibration
    def _calibrate_selector(self, gate, extrapolate=True, save=True):
        gate_str = lambda gate : ("gate %d" % gate) if gate >= 0 else "bypass"

        max_movement = self._get_max_selector_movement(gate)
        self.mmu.log_always("Measuring the selector position for %s..." % gate_str(gate))
        traveled, found_home = self.measure_to_home()

        # Test we actually homed
        if not found_home:
            self.mmu.log_error("Selector didn't find home position")
            return False

        # Warn and don't save if the measurement is unexpected
        if traveled > max_movement:
            self.mmu.log_always("Selector move measured %.1fmm. More than the anticipated maximum of %.1fmm. Save disabled\nIt is likely that your basic MMU dimensions are incorrect in mmu_parameters.cfg. Check vendor/version and optional 'cad_*' parameters" % (traveled, max_movement))
            save = 0
        else:
            self.mmu.log_always("Selector move measured %.1fmm" % traveled)

        if save:
            if gate >= 0:
                self.selector_offsets[gate] = round(traveled, 1)
                if (
                    extrapolate and gate == self.mmu_unit.num_gates - 1  and self.selector_offsets[0] > 0 or
                    extrapolate and gate == 0 and self.selector_offsets[-1] > 0
                ):
                    # Distribute selector spacing
                    spacing = (self.selector_offsets[-1] - self.selector_offsets[0]) / (self.mmu_unit.num_gates - 1)
                    self.selector_offsets = [round(self.selector_offsets[0] + i * spacing, 1) for i in range(self.mmu_unit.num_gates)]
                else:
                    extrapolate = False
                self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True, namespace=self.mmu_unit.name)
            else:
                self.bypass_offset = round(traveled, 1)
                extrapolate = False
                self.var_manager.set(VARS_MMU_SELECTOR_BYPASS, self.bypass_offset, write=True, namespace=self.mmu_unit.name)

            if extrapolate:
                self.mmu.log_always("All selector offsets have been extrapolated and saved:\n%s" % self.selector_offsets)
            else:
                self.mmu.log_always("Selector offset (%.1fmm) for %s has been saved" % (traveled, gate_str(gate)))
                if gate == 0:
                    self.mmu.log_always("Run MMU_CALIBRATE_SELECTOR again with GATE=%d to extrapolate all gate positions. Use SINGLE=1 to force calibration of only one gate" % (self.mmu_unit.num_gates - 1))
        return True

    # Fully automated selector offset calibration
    # Strategy is to find the two end gates, infer and set number of gates and distribute selector positions
    # Assumption: the user has manually positioned the selector aligned with gate 0 before calling.  Doesn't work
    # with as well with open ended designs like Tradrack. Use "manual" calibration routine above for that
    def _calibrate_selector_auto(self, save=True, v1_bypass_block=-1):
        self.mmu.log_always("Auto calibrating the selector. Excuse the whizz, bang, buzz, clicks...")

        # Step 1 - position of gate 0
        self.mmu.log_always("Measuring the selector position for gate 0...")
        traveled, found_home = self.measure_to_home()
        if not found_home or traveled > self.cad_gate0_pos + self.cad_selector_tolerance:
            self.mmu.log_error("Selector didn't find home position or distance moved (%.1fmm) was larger than expected.\nAre you sure you aligned selector with gate 0 and removed filament?" % traveled)
            return False
        gate0_pos = traveled

        # Step 2 - end of selector
        max_movement = self._get_max_selector_movement()
        self.mmu.log_always("Searching for end of selector... (up to %.1fmm)" % max_movement)
        if self.use_touch_move():
            _,found_home = self.homing_move("Detecting end of selector movement", max_movement, homing_move=1, endstop_name=self.mmu.SENSOR_SELECTOR_TOUCH)
        else:
            # This might not sound good!
            self.move("Ensure we are clear off the physical endstop", self.cad_gate0_pos)
            self.move("Forceably detecting end of selector movement", max_movement, speed=self.selector_homing_speed)
            found_home = True
        if not found_home:
            msg = "Didn't detect the end of the selector"
            if self.cad_last_gate_offset > 0:
                self.mmu.log_error(msg)
                return False
            else:
                self.mmu.log_always(msg)

        # Step 3a - selector length
        self.mmu.log_always("Measuring the full selector length...")
        traveled, found_home = self.measure_to_home()
        if not found_home:
            self.mmu.log_error("Selector didn't find home position after full length move")
            return False
        self.mmu.log_always("Maximum selector movement is %.1fmm" % traveled)

        # Step 3b - bypass and last gate position (measured back from limit of travel)
        if self.cad_bypass_offset > 0:
            bypass_pos = traveled - self.cad_bypass_offset
        else:
            bypass_pos = -1
        if self.cad_last_gate_offset > 0:
            # This allows the error to be averaged
            last_gate_pos = traveled - self.cad_last_gate_offset
        else:
            # This simply assumes theoretical distance
            last_gate_pos = gate0_pos + (self.mmu_unit.num_gates - 1) * self.cad_gate_width

        # Step 4 - the calcs
        length = last_gate_pos - gate0_pos
        self.mmu.log_debug("Results: gate0_pos=%.1f, last_gate_pos=%.1f, length=%.1f" % (gate0_pos, last_gate_pos, length))
        selector_offsets = []

        if self.mmu_unit.mmu_vendor.lower() == VENDOR_ERCF.lower() and self.mmu_unit.mmu_version == 1.1:
            # ERCF v1.1 special case
            num_gates = adj_gate_width = int(round(length / (self.cad_gate_width + self.cad_block_width / 3))) + 1
            num_blocks = (num_gates - 1) // 3
            bypass_offset = -1
            if num_gates > 1:
                if v1_bypass_block >= 0:
                    adj_gate_width = (length - (num_blocks - 1) * self.cad_block_width - self.cad_bypass_block_width) / (num_gates - 1)
                else:
                    adj_gate_width = (length - num_blocks * self.cad_block_width) / (num_gates - 1)
            self.mmu.log_debug("Adjusted gate width: %.1f" % adj_gate_width)
            for i in range(num_gates):
                bypass_adj = (self.cad_bypass_block_width - self.cad_block_width) if (i // 3) >= v1_bypass_block else 0.
                selector_offsets.append(round(gate0_pos + (i * adj_gate_width) + (i // 3) * self.cad_block_width + bypass_adj, 1))
                if ((i + 1) / 3) == v1_bypass_block:
                    bypass_offset = selector_offsets[i] + self.cad_bypass_block_delta

        else:
            # Generic Type-A MMU case
            num_gates = int(round(length / self.cad_gate_width)) + 1
            adj_gate_width = length / (num_gates - 1) if num_gates > 1 else length
            self.mmu.log_debug("Adjusted gate width: %.1f" % adj_gate_width)
            for i in range(num_gates):
                selector_offsets.append(round(gate0_pos + (i * adj_gate_width), 1))
            bypass_offset = bypass_pos

        if num_gates != self.mmu_unit.num_gates:
            self.mmu.log_error("You configued your MMU for %d gates but I counted %d! Please update 'num_gates'" % (self.mmu_unit.num_gates, num_gates))
            return False

        self.mmu.log_always("Offsets: %s%s" % (selector_offsets, (" (bypass: %.1f)" % bypass_offset) if bypass_offset > 0 else " (no bypass fitted)"))
        if save:
            self.selector_offsets = selector_offsets
            self.bypass_offset = bypass_offset
            self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, namespace=self.mmu_unit.name)
            self.var_manager.set(VARS_MMU_SELECTOR_BYPASS, self.bypass_offset, namespace=self.mmu_unit.name)
            self.var_manager.write()
            self.mmu.log_always("Selector calibration has been saved")
        return True

    def _home_selector(self):
        self.mmu.unselect_gate()
        self.filament_hold_move()
        self.mmu.movequeues_wait()
        try:
            homing_state = MmuUnit.MmuHoming(self.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_toolhead.get_kinematics().home(homing_state)
            self.is_homed = True
        except Exception as e: # Homing failed
            logging.error(traceback.format_exc())
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def _position(self, target):
        if not self.use_touch_move():
            self.move("Positioning selector", target)
        else:
            init_pos = self.mmu_toolhead.get_position()[0]
            halt_pos,homed = self.homing_move("Positioning selector with 'touch' move", target, homing_move=1, endstop_name=self.mmu.SENSOR_SELECTOR_TOUCH)
            if homed: # Positioning move was not successful
                with self.mmu.wrap_suppress_visual_log():
                    travel = abs(init_pos - halt_pos)
                    if travel < 4.0: # Filament stuck in the current gate (based on ERCF design)
                        self.mmu.log_info("Selector is blocked by filament inside gate, will try to recover...")
                        self.move("Realigning selector by a distance of: %.1fmm" % -travel, init_pos)
                        self.mmu_toolhead.flush_step_generation() # TTC mitigation when homing move + regular + get_last_move_time() in close succession

                        # See if we can detect filament in gate area
                        found = self.mmu.check_filament_in_gate()
                        if not found:
                            # Push filament into view of the gate endstop
                            self.filament_drive()
                            _,_,measured,_ = self.mmu.trace_filament_move("Locating filament", self.mmu.gate_parking_distance + self.mmu.gate_endstop_to_encoder + 10.)
                            if self.mmu.has_encoder() and measured < self.mmu.encoder_min:
                                raise MmuError("Unblocking selector failed bacause unable to move filament to clear")

                        # Try a full unload sequence
                        try:
                            self.mmu.unload_sequence(check_state=True)
                        except MmuError as ee:
                            raise MmuError("Unblocking selector failed because: %s" % (str(ee)))

                        # Check if selector can now reach proper target
                        self._home_selector()
                        halt_pos,homed = self.homing_move("Positioning selector with 'touch' move", target, homing_move=1, endstop_name=self.mmu.SENSOR_SELECTOR_TOUCH)
                        if homed: # Positioning move was not successful
                            self.is_homed = False
                            raise MmuError("Unblocking selector recovery failed. Path is probably internally blocked")

                    else: # Selector path is blocked, probably externally
                        self.is_homed = False
                        raise MmuError("Selector is externally blocked perhaps by filament in another gate")

    def move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, wait=wait)[0]

    def homing_move(self, trace_str, new_pos, speed=None, accel=None, homing_move=0, endstop_name=None):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, homing_move=homing_move, endstop_name=endstop_name)

    # Internal raw wrapper around all selector moves except rail homing
    # Returns position after move, if homed (homing moves)
    def _trace_selector_move(self, trace_str, new_pos, speed=None, accel=None, homing_move=0, endstop_name=None, wait=False):
        if trace_str:
            self.mmu.log_trace(trace_str)

        self.mmu_toolhead.quiesce()

        # Set appropriate speeds and accel if not supplied
        if homing_move != 0:
            speed = speed or (self.selector_touch_speed if self.selector_touch_enabled or endstop_name == self.mmu.SENSOR_SELECTOR_TOUCH else self.selector_homing_speed)
        else:
            speed = speed or self.selector_move_speed
        accel = accel or self.mmu_toolhead.get_selector_limits()[1]

        pos = self.mmu_toolhead.get_position()
        homed = False
        if homing_move != 0:
            # Check for valid endstop
            endstops = self.selector_rail.get_endstops() if endstop_name is None else self.selector_rail.get_extra_endstop(endstop_name)
            if endstops is None:
                self.mmu.log_error("Endstop '%s' not found" % endstop_name)
                return pos[0], homed

            hmove = HomingMove(self.printer, endstops, self.mmu_toolhead)
            try:
                trig_pos = [0., 0., 0., 0.]
                with self.mmu.wrap_accel(accel):
                    pos[0] = new_pos
                    trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                    if hmove.check_no_movement():
                        self.mmu.log_stepper("No movement detected")
                    if self.selector_rail.is_endstop_virtual(endstop_name):
                        # Try to infer move completion is using Stallguard. Note that too slow speed or accelaration
                        delta = abs(new_pos - trig_pos[0])
                        if delta < 1.0:
                            homed = False
                            self.mmu.log_trace("Truing selector %.4fmm to %.2fmm" % (delta, new_pos))
                            self.mmu_toolhead.move(pos, speed)
                        else:
                            homed = True
                    else:
                        homed = True
            except self.printer.command_error:
                homed = False
            finally:
                self.mmu_toolhead.flush_step_generation() # TTC mitigation when homing move + regular + get_last_move_time() in close succession
                pos = self.mmu_toolhead.get_position()
                if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                    self.mmu.log_stepper("SELECTOR HOMING MOVE: requested position=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s >> %s" % (new_pos, speed, accel, endstop_name, "%s actual pos=%.2f, trig_pos=%.2f" % ("HOMED" if homed else "DID NOT HOMED",  pos[0], trig_pos[0])))
        else:
            pos = self.mmu_toolhead.get_position()
            with self.mmu.wrap_accel(accel):
                pos[0] = new_pos
                self.mmu_toolhead.move(pos, speed)
            if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (new_pos, speed, accel))
            if wait:
                self.mmu.movequeues_wait(toolhead=False, mmu_toolhead=True)

        return pos[0], homed

    def set_position(self, position):
        pos = self.mmu_toolhead.get_position()
        pos[0] = position
        self.mmu_toolhead.set_position(pos, homing_axes=(0,))
        self.enable_motors()
        self.is_homed = True
        return position

    def measure_to_home(self):
        self.mmu.movequeues_wait()
        init_mcu_pos = self.selector_stepper.get_mcu_position()
        homed = False
        try:
            homing_state = MmuUnit.MmuHoming(self.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_toolhead.get_kinematics().home(homing_state)
            homed = True
        except Exception:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * self.selector_stepper.get_step_dist()
        return traveled, homed

    def use_touch_move(self):
        return self.mmu_unit.selector_touch and self.mmu.SENSOR_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enable



################################################################################
# LinearSelectorServo
# Implements servo control for typical linear selector
################################################################################

# Servo states
SERVO_MOVE_STATE      = FILAMENT_HOLD_STATE
SERVO_DOWN_STATE      = FILAMENT_DRIVE_STATE
SERVO_UP_STATE        = FILAMENT_RELEASE_STATE
SERVO_UNKNOWN_STATE   = FILAMENT_UNKNOWN_STATE

class LinearSelectorServo:


    def __init__(self, config, mmu_unit, selector):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = mmu_unit.p                     # mmu_unit_parameters
        self.selector = selector
        self.printer = config.get_printer()

        # Process config
        self.servo_angles = {}
        self.servo_angles['down'] = config.getint('servo_down_angle', 90)
        self.servo_angles['up'] = config.getint('servo_up_angle', 90)
        self.servo_angles['move'] = config.getint('servo_move_angle', self.servo_angles['up'])
        self.servo_duration = config.getfloat('servo_duration', 0.2, minval=0.1)
        self.servo_always_active = config.getint('servo_always_active', 0, minval=0, maxval=1)
        self.servo_active_down = config.getint('servo_active_down', 0, minval=0, maxval=1)
        self.servo_dwell = config.getfloat('servo_dwell', 0.4, minval=0.1)
        self.servo_buzz_gear_on_down = config.getint('servo_buzz_gear_on_down', 3, minval=0, maxval=10)

        # Get hardware
        self.servo = self.mmu_unit.selector_servo
        if not self.servo:
            raise self.config.error("Selector servo not found")

        # Register GCODE commands specific to this module
        selector.register_mux_command('MMU_SERVO', self.cmd_MMU_SERVO, desc=self.cmd_MMU_SERVO_help)

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)

        self.reinit()

    def reinit(self):
        self.servo_state = SERVO_UNKNOWN_STATE
        self.servo_angle = SERVO_UNKNOWN_STATE

    def handle_connect(self):
        logging.info("PAUL: handle_connect: LinearSelectorServo")
        self.mmu = self.mmu_unit.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager

        # Override defaults with saved/calibrated servo positions (set with MMU_SERVO)
        try:
            self.var_manager.upgrade(VARS_MMU_SERVO_ANGLES, self.mmu_unit.name) # v3 upgrade
            servo_angles = self.var_manager.get(VARS_MMU_SERVO_ANGLES, {}, namespace=self.mmu_unit.name)
            self.servo_angles.update(servo_angles)
        except Exception as e:
            raise self.config.error("Exception whilst parsing servo angles from 'mmu_vars.cfg': %s" % str(e))


    cmd_MMU_SERVO_help = "Move MMU servo to position specified position or angle"
    cmd_MMU_SERVO_param_help = (
        "MMU_SERVO: %s\n" % cmd_MMU_SERVO_help
        + "UNIT   = #(int) Optional, defaults to all units\n"
        + "RESET  = [0|1]  Clear saved calibration\n"
        + "SAVE   = [0|1]  Save current position against pos if calibrating\n"
        + "POS    = [off|up|move|down]\n"
    )
    def cmd_MMU_SERVO(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        reset = gcmd.get_int('RESET', 0)
        save = gcmd.get_int('SAVE', 0)
        pos = gcmd.get('POS', "").lower()

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SERVO_param_help), color=True)
            return

        if reset:
            self.mmu.delete_variable(VARS_MMU_SERVO_ANGLES, write=True)
            self.mmu.log_info("Calibrated servo angles have be reset to configured defaults")
        elif pos == "off":
            self.servo_off() # For 'servo_always_active' case
        elif pos == "up":
            if save:
                self._servo_save_pos(pos)
            else:
                self.servo_up()
        elif pos == "move":
            if save:
                self._servo_save_pos(pos)
            else:
                self.servo_move()
        elif pos == "down":
            if self.mmu.check_if_bypass(): return
            if save:
                self._servo_save_pos(pos)
            else:
                self.servo_down()
        elif save:
            self.mmu.log_error("Servo position not specified for save")
        elif pos == "":
            if self.mmu.check_if_bypass(): return
            angle = gcmd.get_int('ANGLE', None)
            if angle is not None:
                self.mmu.log_debug("Setting servo to angle: %d" % angle)
                self._set_servo_angle(angle)
            else:
                self.mmu.log_always("Current servo angle: %d, Positions: %s" % (self.servo_angle, self.servo_angles))
                self.mmu.log_info("Use POS= or ANGLE= to move position")
        else:
            self.mmu.log_error("Unknown servo position '%s'" % pos)

    def _set_servo_angle(self, angle):
        self.servo.set_position(angle=angle, duration=None if self.servo_always_active else self.servo_duration)
        self.servo_angle = angle
        self.servo_state = SERVO_UNKNOWN_STATE

    def _servo_save_pos(self, pos):
        if self.servo_angle != SERVO_UNKNOWN_STATE:
            self.servo_angles[pos] = self.servo_angle
            self.var_manager.set(VARS_MMU_SERVO_ANGLES, self.servo_angles, write=True, namespace=self.mmu_unit.name)
            self.mmu.log_info("Servo angle '%d' for position '%s' has been saved" % (self.servo_angle, pos))
        else:
            self.mmu.log_info("Servo angle unknown")

    def servo_down(self, buzz_gear=True):
        if self.mmu._is_running_test: return # Save servo while testing
        if self.mmu.gate_selected == TOOL_GATE_BYPASS: return
        if self.servo_state == SERVO_DOWN_STATE: return
        self.mmu.log_trace("Setting servo to down (filament drive) position at angle: %d" % self.servo_angles['down'])

        if buzz_gear and self.servo_buzz_gear_on_down > 0:
            self.mmu_unit.mmu_toolhead.sync(MmuToolHead.GEAR_ONLY) # Must be in correct sync mode before buzz to avoid delay

        self.mmu.movequeues_wait() # Probably not necessary
        initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
        self.servo.set_position(angle=self.servo_angles['down'], duration=None if self.servo_active_down or self.servo_always_active else self.servo_duration)

        if self.servo_angle != self.servo_angles['down'] and buzz_gear and self.servo_buzz_gear_on_down > 0:
            for _ in range(self.servo_buzz_gear_on_down):
                self.mmu.trace_filament_move(None, 0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None, speed_override=False)
                self.mmu.trace_filament_move(None, -0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None, speed_override=False)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))

        self.servo_angle = self.servo_angles['down']
        self.servo_state = SERVO_DOWN_STATE
        self.mmu.set_encoder_distance(initial_encoder_position)
        self.mmu.mmu_macro_event(self.mmu.MACRO_EVENT_FILAMENT_GRIPPED)

    def servo_move(self): # Position servo for selector movement
        if self.mmu._is_running_test: return # Save servo while testing
        if self.servo_state == SERVO_MOVE_STATE: return
        self.mmu.log_trace("Setting servo to move (filament hold) position at angle: %d" % self.servo_angles['move'])
        if self.servo_angle != self.servo_angles['move']:
            self.mmu.movequeues_wait()
            self.servo.set_position(angle=self.servo_angles['move'], duration=None if self.servo_always_active else self.servo_duration)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            self.servo_angle = self.servo_angles['move']
            self.servo_state = SERVO_MOVE_STATE

    def servo_up(self, measure=False):
        if self.mmu._is_running_test: return 0. # Save servo while testing
        if self.servo_state == SERVO_UP_STATE: return 0.
        self.mmu.log_trace("Setting servo to up (filament released) position at angle: %d" % self.servo_angles['up'])
        delta = 0.
        if self.servo_angle != self.servo_angles['up']:
            self.mmu.movequeues_wait()
            if measure:
                initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
            self.servo.set_position(angle=self.servo_angles['up'], duration=None if self.servo_always_active else self.servo_duration)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
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
        self.reinit() # Reset state

    def enable_motors(self):
        self.servo_move()

    def buzz_motor(self):
        self.mmu.movequeues_wait()
        old_state = self.servo_state
        low=min(self.servo_angles['down'], self.servo_angles['up'])
        high=max(self.servo_angles['down'], self.servo_angles['up'])
        mid = (low + high) / 2
        move = (high - low) / 4
        duration=None if self.servo_always_active else self.servo_duration
        self.servo.set_position(angle=mid, duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.servo.set_position(angle=(mid - move), duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.servo.set_position(angle=(mid + move), duration=duration)
        self.mmu.movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
        self.mmu.movequeues_wait()
        if old_state == SERVO_DOWN_STATE:
            self.servo_down(buzz_gear=False)
        elif old_state == SERVO_MOVE_STATE:
            self.servo_move()
        else:
            self.servo_up()

    def set_test_config(self, gcmd):
        self.servo_duration = gcmd.get_float('SERVO_DURATION', self.servo_duration, minval=0.1)
        self.servo_always_active = gcmd.get_int('SERVO_ALWAYS_ACTIVE', self.servo_always_active, minval=0, maxval=1)
        self.servo_active_down = gcmd.get_int('SERVO_ACTIVE_DOWN', self.servo_active_down, minval=0, maxval=1)
        self.servo_dwell = gcmd.get_float('SERVO_DWELL', self.servo_active_down, minval=0.1)
        self.servo_buzz_gear_on_down = gcmd.get_int('SERVO_BUZZ_GEAR_ON_DOWN', self.servo_buzz_gear_on_down, minval=0, maxval=10)

    def get_test_config(self):
        msg = "\n\nSERVO:"
        msg += "\nservo_duration = %.1f" % self.servo_duration
        msg += "\nservo_always_active = %d" % self.servo_always_active
        msg += "\nservo_active_down = %d" % self.servo_active_down
        msg += "\nservo_dwell = %.1f" % self.servo_dwell
        msg += "\nservo_buzz_gear_on_down = %d" % self.servo_buzz_gear_on_down

        return msg

    def check_test_config(self, param):
        return vars(self).get(param) is None

    def get_mmu_status_config(self):
        msg = ". Servo in %s position" % ("RELEASE" if self.servo_state == SERVO_UP_STATE else \
                "GRIP" if self.servo_state == SERVO_DOWN_STATE else "MOVE" if self.servo_state == SERVO_MOVE_STATE else "unknown")
        return msg

    def get_status(self, eventtime):
        return {
            'servo': "Up" if self.servo_state == SERVO_UP_STATE else
                     "Down" if self.servo_state == SERVO_DOWN_STATE else
                     "Move" if self.servo_state == SERVO_MOVE_STATE else
                     "Unknown",
        }



################################################################################
# LinearServoSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  - Supports type-A with combined selection and filament gripping line ERCFv3
#
class LinearServoSelector(LinearSelector):

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
