# Happy Hare MMU Software
# Implementation of various selector variations:
#
# VirtualSelector:
#  Implements selector for type-B MMU's with gear driver per gate
#   - Uses gear driver stepper per-gate
#   - For type-B designs like BoxTurtle, KMS, QuattroBox
#
# LinearSelector:
#  Implements Linear Selector for type-A MMU's without servo
#  - Stepper controlled linear movement with endstop
#  - Supports type-A with combined selection and filament gripping line ERCFv3
#
# LinearServoSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  - Supports type-A classic MMU's like ERCFv1.1, ERCFv2.0 and Tradrack
#
# LinearMultiGearSelector:
#  Implements Linear Selector for type-C MMU's with multiple gear steppers:
#   - Uses gear driver stepper per-gate
#   - Uses selector stepper for gate selection with endstop
#   - Supports type-A classic MMU's like ERCF and Tradrack
#
# Rotary Selector
# - Rotary Selector for 3D Chamelon using stepper selection
#   without servo
#
# Macro Selector
#  - Universal selector control via macros
#  - Great for experimention
#
# Servo Selector
# - Servo based Selector for PicoMMU and clones
#
# Indexed Selector
# - Stepper based Selector for ViViD with per-gate index sensors
#
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
import random, logging, math, re

# Klipper imports
from ..homing          import Homing, HomingMove

# Happy Hare imports
from ..                import mmu_machine
from ..mmu_machine     import MmuToolHead

# MMU subcomponent clases
from .mmu_shared       import MmuError


################################################################################
# Base Selector Class
################################################################################

class BaseSelector:

    def __init__(self, mmu):
        self.mmu = mmu
        self.is_homed = False
        self.mmu_unit = 0

    def reinit(self):
        pass

    def handle_connect(self):
        pass

    def handle_ready(self):
        pass

    def handle_disconnect(self):
        pass

    def bootup(self):
        pass

    def home(self, force_unload = None):
        pass

    def select_gate(self, gate):
        pass

    def restore_gate(self, gate):
        pass

    def filament_drive(self):
        pass

    def filament_release(self, measure=False):
        return 0. # Fake encoder movement

    def filament_hold_move(self):
        pass

    def get_filament_grip_state(self):
        return self.mmu.FILAMENT_DRIVE_STATE

    def disable_motors(self):
        pass

    def enable_motors(self):
        pass

    def buzz_motor(self, motor):
        return False

    def has_bypass(self):
        return self.mmu.mmu_machine.has_bypass

    def get_status(self, eventtime):
        return {
            'has_bypass': self.has_bypass()
        }

    def get_mmu_status_config(self):
        return "\nSelector Type: %s" % self.__class__.__name__

    def set_test_config(self, gcmd):
        pass

    def get_test_config(self):
        return ""

    def check_test_config(self, param):
        return True

    def get_uncalibrated_gates(self, check_gates):
        return []



################################################################################
# VirtualSelector:
#  Implements selector for type-B MMU's with gear driver per gate
#   - Uses gear driver stepper per-gate
#   - For type-B designs like BoxTurtle, KMS, QuattroBox
################################################################################

class VirtualSelector(BaseSelector, object):

    def __init__(self, mmu):
        super(VirtualSelector, self).__init__(mmu)
        self.is_homed = True

        # Read all controller parameters related to selector or servo to stop klipper complaining. This
        # is done to allow for uniform and shared mmu_parameters.cfg file regardless of configuration.
        for option in ['selector_', 'servo_', 'cad_']:
            for key in mmu.config.get_prefix_options(option):
                _ = mmu.config.get(key)

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead
        self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR # No calibration necessary

    def select_gate(self, gate):
        if gate == self.mmu.gate_selected: return
        self.mmu_toolhead.select_gear_stepper(gate) # Select correct drive stepper or none if bypass

    def restore_gate(self, gate):
        self.mmu.mmu_toolhead.select_gear_stepper(gate) # Select correct drive stepper or none if bypass

    def get_mmu_status_config(self):
        msg = "\nVirtual selector"
        return msg



################################################################################
# LinearSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Optional servo controlled filament gripping
#  - Supports type-A classic MMU's like ERCF and Tradrack
################################################################################

class LinearSelector(BaseSelector, object):

    # mmu_vars.cfg variables
    VARS_MMU_SELECTOR_OFFSETS = "mmu_selector_offsets"
    VARS_MMU_SELECTOR_BYPASS  = "mmu_selector_bypass"

    def __init__(self, mmu):
        super(LinearSelector, self).__init__(mmu)
        self.bypass_offset = -1

        # Process config
        self.selector_move_speed = mmu.config.getfloat('selector_move_speed', 200, minval=1.)
        self.selector_homing_speed = mmu.config.getfloat('selector_homing_speed', 100, minval=1.)
        self.selector_touch_speed = mmu.config.getfloat('selector_touch_speed', 60, minval=1.)
        self.selector_touch_enabled = mmu.config.getint('selector_touch_enabled', 1, minval=0, maxval=1)

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
        if self.mmu.mmu_machine.mmu_vendor.lower() == mmu_machine.VENDOR_ERCF.lower():
            if self.mmu.mmu_machine.mmu_version >= 2.0: # V2 community edition
                self.cad_gate0_pos = 4.0
                self.cad_gate_width = 23.
                self.cad_bypass_offset = 0.72
                self.cad_last_gate_offset = 14.4

            else: # V1.1 original
                # Modifications:
                #  t = TripleDecky filament blocks
                #  s = Springy sprung servo selector
                #  b = Binky encoder upgrade
                if "t" in self.mmu.mmu_machine.mmu_version_string:
                    self.cad_gate_width = 23. # Triple Decky is wider filament block
                    self.cad_block_width = 0. # Bearing blocks are not used

                if "s" in self.mmu.mmu_machine.mmu_version_string:
                    self.cad_last_gate_offset = 1.2 # Springy has additional bump stops

        elif self.mmu.mmu_machine.mmu_vendor.lower() == mmu_machine.VENDOR_TRADRACK.lower():
            self.cad_gate0_pos = 2.5
            self.cad_gate_width = 17.
            self.cad_bypass_offset = 0     # Doesn't have bypass
            self.cad_last_gate_offset = 0. # Doesn't have reliable hard stop at limit of travel

        # But still allow all CAD parameters to be customized
        self.cad_gate0_pos = mmu.config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = mmu.config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_bypass_offset = mmu.config.getfloat('cad_bypass_offset', self.cad_bypass_offset, minval=0.)
        self.cad_last_gate_offset = mmu.config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)
        self.cad_block_width = mmu.config.getfloat('cad_block_width', self.cad_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_width = mmu.config.getfloat('cad_bypass_block_width', self.cad_bypass_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_delta = mmu.config.getfloat('cad_bypass_block_delta', self.cad_bypass_block_delta, above=0.) # ERCF v1.1 only
        self.cad_selector_tolerance = mmu.config.getfloat('cad_selector_tolerance', self.cad_selector_tolerance, above=0.) # Extra movement allowed by selector

        # Sub components (optional servo)
        if isinstance(self, LinearServoSelector):
            self.servo = LinearSelectorServo(mmu) if isinstance(self, LinearServoSelector) else None
        else:
            self.servo = None
            # Read all controller parameters related to to stop klipper complaining. This is done to allow
            # for uniform and shared mmu_parameters.cfg file regardless of configuration.
            for option in ['servo_']:
                for key in mmu.config.get_prefix_options(option):
                    _ = mmu.config.get(key)

        # Register GCODE commands specific to this module
        gcode = mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc = self.cmd_MMU_CALIBRATE_SELECTOR_help)
        gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc = self.cmd_MMU_SOAKTEST_SELECTOR_help)

        # Selector stepper setup before MMU toolhead is instantiated
        section = mmu_machine.SELECTOR_STEPPER_CONFIG
        if mmu.config.has_section(section):
            # Inject options into selector stepper config regardless or what user sets
            mmu.config.fileconfig.set(section, 'position_min', -1.)
            mmu.config.fileconfig.set(section, 'position_max', self._get_max_selector_movement())
            mmu.config.fileconfig.set(section, 'homing_speed', self.selector_homing_speed)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        # Sub components
        if self.servo:
            self.servo.reinit()

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead
        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]

        # Load selector offsets (calibration set with MMU_CALIBRATE_SELECTOR) -------------------------------
        self.selector_offsets = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_OFFSETS, None)
        if self.selector_offsets:
            # Ensure list size
            if len(self.selector_offsets) == self.mmu.num_gates:
                self.mmu.log_debug("Loaded saved selector offsets: %s" % self.selector_offsets)
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.VARS_MMU_SELECTOR_OFFSETS)
                self.selector_offsets = self._ensure_list_size(self.selector_offsets, self.mmu.num_gates)

            if not any(x == -1 for x in self.selector_offsets):
                self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR
        else:
            self.mmu.log_always("Warning: Selector offsets not found in mmu_vars.cfg. Probably not calibrated")
            self.selector_offsets = [-1] * self.mmu.num_gates
        self.mmu.save_variables.allVariables[self.VARS_MMU_SELECTOR_OFFSETS] = self.selector_offsets

        self.bypass_offset = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_BYPASS, -1)
        if self.bypass_offset > 0:
            self.mmu.log_debug("Loaded saved bypass offset: %s" % self.bypass_offset)
        else:
            self.bypass_offset = -1 # Ensure -1 value for uncalibrated / non-existent
        self.mmu.save_variables.allVariables[self.VARS_MMU_SELECTOR_BYPASS] = self.bypass_offset

        # See if we have a TMC controller setup with stallguard
        self.selector_tmc = None
        for chip in mmu_machine.TMC_CHIPS:
            if self.selector_tmc is None:
                self.selector_tmc = self.mmu.printer.lookup_object('%s %s' % (chip, mmu_machine.SELECTOR_STEPPER_CONFIG), None)
                if self.selector_tmc is not None:
                    self.mmu.log_debug("Found %s on selector_stepper. Stallguard 'touch' movement and recovery possible." % chip)
        if self.selector_tmc is None:
            self.mmu.log_debug("TMC driver not found for selector_stepper, cannot use 'touch' movement and recovery")

        # Sub components
        if self.servo:
            self.servo.handle_connect()

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    def handle_disconnect(self):
        # Sub components
        if self.servo:
            self.servo.handle_disconnect()

    def handle_ready(self):
        # Sub components
        if self.servo:
            self.servo.handle_ready()

    def home(self, force_unload = None):
        if self.mmu.check_if_bypass(): return
        with self.mmu.wrap_action(self.mmu.ACTION_HOMING):
            self.mmu.log_info("Homing MMU...")
            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))
            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)
            elif force_unload is None and self.mmu.filament_pos != self.mmu.FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self.mmu.unload_sequence()
            self._home_selector()

    # Physically move selector to correct gate position
    def select_gate(self, gate):
        if gate == self.mmu.gate_selected: return
        with self.mmu.wrap_action(self.mmu.ACTION_SELECTING):
            self.filament_hold_move()
            if gate == self.mmu.TOOL_GATE_BYPASS:
                self._position(self.bypass_offset)
            elif gate >= 0:
                self._position(self.selector_offsets[gate])

    # Correct rail position for selector
    def restore_gate(self, gate):
        if gate == self.mmu.TOOL_GATE_BYPASS:
            self.set_position(self.bypass_offset)
        elif gate >= 0:
            self.set_position(self.selector_offsets[gate])

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
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False
        if self.servo:
            self.servo.disable_motors()

    def enable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
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
        return self.mmu.mmu_machine.has_bypass and self.bypass_offset >= 0

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
        return [gate for gate, value in enumerate(self.selector_offsets) if value == -1 and gate in check_gates]

    # Internal Implementation --------------------------------------------------

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector positions or postion of specified gate"
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu.mmu_machine.num_gates - 1)
        if gate == -1 and gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
            gate = self.mmu.TOOL_GATE_BYPASS

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
                    self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR

                # If not fully calibrated turn off the selector stepper to ease next step, else activate by homing
                if successful and self.mmu.calibration_status & self.mmu.CALIBRATED_SELECTOR:
                    self.mmu.log_always("Selector calibration complete")
                    self.mmu.select_tool(0)
                else:
                    self.mmu.motors_onoff(on=False, motor="selector")

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
        finally:
            self.mmu.calibrating = False

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_loaded(): return
        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 100)
        servo = bool(gcmd.get_int('SERVO', 0))
        home = bool(gcmd.get_int('HOME', 0))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                if home:
                    self.home()
                for l in range(loops):
                    self.mmu.log_always("Testing loop %d / %d" % (l + 1, loops))
                    tool = random.randint(0, self.mmu.num_gates)
                    if tool == self.mmu.num_gates:
                        self.mmu.select_bypass()
                    else:
                        if random.randint(0, 10) == 0 and home:
                            self.mmu.home(tool=tool)
                        else:
                            self.mmu.select_tool(tool)
                    if servo:
                        self.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu.num_gates - 1

        if self.mmu.mmu_machine.mmu_vendor == mmu_machine.VENDOR_ERCF:
            # ERCF Designs
            if self.mmu.mmu_machine.mmu_version >= 2.0 or "t" in self.mmu.mmu_machine.mmu_version_string:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)
            else:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width) + (n//3) * self.cad_block_width
        else:
            # Everything else
            max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)

        max_movement += self.cad_last_gate_offset if gate in [self.mmu.TOOL_GATE_UNKNOWN] else 0.
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
                    extrapolate and gate == self.mmu.num_gates - 1  and self.selector_offsets[0] > 0 or
                    extrapolate and gate == 0 and self.selector_offsets[-1] > 0
                ):
                    # Distribute selector spacing
                    spacing = (self.selector_offsets[-1] - self.selector_offsets[0]) / (self.mmu.num_gates - 1)
                    self.selector_offsets = [round(self.selector_offsets[0] + i * spacing, 1) for i in range(self.mmu.num_gates)]
                else:
                    extrapolate = False
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True)
            else:
                self.bypass_offset = round(traveled, 1)
                extrapolate = False
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_BYPASS, self.bypass_offset, write=True)

            if extrapolate:
                self.mmu.log_always("All selector offsets have been extrapolated and saved:\n%s" % self.selector_offsets)
            else:
                self.mmu.log_always("Selector offset (%.1fmm) for %s has been saved" % (traveled, gate_str(gate)))
                if gate == 0:
                    self.mmu.log_always("Run MMU_CALIBRATE_SELECTOR again with GATE=%d to extrapolate all gate positions. Use SINGLE=1 to force calibration of only one gate" % (self.mmu.num_gates - 1))
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
            last_gate_pos = gate0_pos + (self.mmu.num_gates - 1) * self.cad_gate_width

        # Step 4 - the calcs
        length = last_gate_pos - gate0_pos
        self.mmu.log_debug("Results: gate0_pos=%.1f, last_gate_pos=%.1f, length=%.1f" % (gate0_pos, last_gate_pos, length))
        selector_offsets = []

        if self.mmu.mmu_machine.mmu_vendor.lower() == mmu_machine.VENDOR_ERCF.lower() and self.mmu.mmu_machine.mmu_version == 1.1:
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

        if num_gates != self.mmu.num_gates:
            self.mmu.log_error("You configued your MMU for %d gates but I counted %d! Please update 'num_gates'" % (self.mmu.num_gates, num_gates))
            return False

        self.mmu.log_always("Offsets: %s%s" % (selector_offsets, (" (bypass: %.1f)" % bypass_offset) if bypass_offset > 0 else " (no bypass fitted)"))
        if save:
            self.selector_offsets = selector_offsets
            self.bypass_offset = bypass_offset
            self.mmu.save_variable(self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets)
            self.mmu.save_variable(self.VARS_MMU_SELECTOR_BYPASS, self.bypass_offset)
            self.mmu.write_variables()
            self.mmu.log_always("Selector calibration has been saved")
        return True

    def _home_selector(self):
        self.mmu.unselect_gate()
        self.filament_hold_move()
        self.mmu.movequeues_wait()
        try:
            homing_state = mmu_machine.MmuHoming(self.mmu.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu.mmu_toolhead.get_kinematics().home(homing_state)
            self.is_homed = True
        except Exception as e: # Homing failed
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

            hmove = HomingMove(self.mmu.printer, endstops, self.mmu_toolhead)
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
            except self.mmu.printer.command_error:
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
            homing_state = mmu_machine.MmuHoming(self.mmu.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_toolhead.get_kinematics().home(homing_state)
            homed = True
        except Exception:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * self.selector_stepper.get_step_dist()
        return traveled, homed

    def use_touch_move(self):
        return self.selector_tmc and self.mmu.SENSOR_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enabled



################################################################################
# LinearSelectorServo
# Implements servo control for typical linear selector
################################################################################

class LinearSelectorServo:

    # mmu_vars.cfg variables
    VARS_MMU_SERVO_ANGLES = "mmu_servo_angles"

    def __init__(self, mmu):
        self.mmu = mmu

        # Servo states
        self.SERVO_MOVE_STATE      = mmu.FILAMENT_HOLD_STATE
        self.SERVO_DOWN_STATE      = mmu.FILAMENT_DRIVE_STATE
        self.SERVO_UP_STATE        = mmu.FILAMENT_RELEASE_STATE
        self.SERVO_UNKNOWN_STATE   = mmu.FILAMENT_UNKNOWN_STATE

        # Process config
        self.servo_angles = {}
        self.servo_angles['down'] = mmu.config.getint('servo_down_angle', 90)
        self.servo_angles['up'] = mmu.config.getint('servo_up_angle', 90)
        self.servo_angles['move'] = mmu.config.getint('servo_move_angle', self.servo_angles['up'])
        self.servo_duration = mmu.config.getfloat('servo_duration', 0.2, minval=0.1)
        self.servo_always_active = mmu.config.getint('servo_always_active', 0, minval=0, maxval=1)
        self.servo_active_down = mmu.config.getint('servo_active_down', 0, minval=0, maxval=1)
        self.servo_dwell = mmu.config.getfloat('servo_dwell', 0.4, minval=0.1)
        self.servo_buzz_gear_on_down = mmu.config.getint('servo_buzz_gear_on_down', 3, minval=0, maxval=10)

        self.servo = mmu.printer.lookup_object('mmu_servo selector_servo', None)
        if not self.servo:
            raise mmu.config.error("No [mmu_servo selector_servo] definition found in mmu_hardware.cfg")

        # Register GCODE commands specific to this module
        gcode = self.mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_SERVO', self.cmd_MMU_SERVO, desc = self.cmd_MMU_SERVO_help)

        self.reinit()

    def reinit(self):
        self.servo_state = self.SERVO_UNKNOWN_STATE
        self.servo_angle = self.SERVO_UNKNOWN_STATE

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead

        # Override with saved/calibrated servo positions (set with MMU_SERVO)
        try:
            servo_angles = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SERVO_ANGLES, {})
            self.servo_angles.update(servo_angles)
        except Exception as e:
            raise self.mmu.config.error("Exception whilst parsing servo angles from 'mmu_vars.cfg': %s" % str(e))

    def handle_disconnect(self):
        pass

    def handle_ready(self):
        pass

    cmd_MMU_SERVO_help = "Move MMU servo to position specified position or angle"
    def cmd_MMU_SERVO(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        reset = gcmd.get_int('RESET', 0)
        save = gcmd.get_int('SAVE', 0)
        pos = gcmd.get('POS', "").lower()
        if reset:
            self.mmu.delete_variable(self.VARS_MMU_SERVO_ANGLES, write=True)
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
        self.servo_state = self.SERVO_UNKNOWN_STATE

    def _servo_save_pos(self, pos):
        if self.servo_angle != self.SERVO_UNKNOWN_STATE:
            self.servo_angles[pos] = self.servo_angle
            self.mmu.save_variable(self.VARS_MMU_SERVO_ANGLES, self.servo_angles, write=True)
            self.mmu.log_info("Servo angle '%d' for position '%s' has been saved" % (self.servo_angle, pos))
        else:
            self.mmu.log_info("Servo angle unknown")

    def servo_down(self, buzz_gear=True):
        if self.mmu._is_running_test: return # Save servo while testing
        if self.mmu.gate_selected == self.mmu.TOOL_GATE_BYPASS: return
        if self.servo_state == self.SERVO_DOWN_STATE: return
        self.mmu.log_trace("Setting servo to down (filament drive) position at angle: %d" % self.servo_angles['down'])

        if buzz_gear and self.servo_buzz_gear_on_down > 0:
            self.mmu_toolhead.sync(MmuToolHead.GEAR_ONLY) # Must be in correct sync mode before buzz to avoid delay

        self.mmu.movequeues_wait() # Probably not necessary
        initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
        self.servo.set_position(angle=self.servo_angles['down'], duration=None if self.servo_active_down or self.servo_always_active else self.servo_duration)

        if self.servo_angle != self.servo_angles['down'] and buzz_gear and self.servo_buzz_gear_on_down > 0:
            for _ in range(self.servo_buzz_gear_on_down):
                self.mmu.trace_filament_move(None, 0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None, speed_override=False)
                self.mmu.trace_filament_move(None, -0.8, speed=25, accel=self.mmu.gear_buzz_accel, encoder_dwell=None, speed_override=False)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))

        self.servo_angle = self.servo_angles['down']
        self.servo_state = self.SERVO_DOWN_STATE
        self.mmu.set_encoder_distance(initial_encoder_position)
        self.mmu.mmu_macro_event(self.mmu.MACRO_EVENT_FILAMENT_GRIPPED)

    def servo_move(self): # Position servo for selector movement
        if self.mmu._is_running_test: return # Save servo while testing
        if self.servo_state == self.SERVO_MOVE_STATE: return
        self.mmu.log_trace("Setting servo to move (filament hold) position at angle: %d" % self.servo_angles['move'])
        if self.servo_angle != self.servo_angles['move']:
            self.mmu.movequeues_wait()
            self.servo.set_position(angle=self.servo_angles['move'], duration=None if self.servo_always_active else self.servo_duration)
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            self.servo_angle = self.servo_angles['move']
            self.servo_state = self.SERVO_MOVE_STATE

    def servo_up(self, measure=False):
        if self.mmu._is_running_test: return 0. # Save servo while testing
        if self.servo_state == self.SERVO_UP_STATE: return 0.
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
        self.servo_state = self.SERVO_UP_STATE
        return delta

    def _servo_auto(self):
        if self.mmu.is_printing() and self.mmu_toolhead.is_gear_synced_to_extruder():
            self.servo_down()
        elif not self.mmu.selector.is_homed or self.mmu.tool_selected < 0 or self.mmu.gate_selected < 0:
            self.servo_move()
        else:
            self.servo_up()

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
        if old_state == self.SERVO_DOWN_STATE:
            self.servo_down(buzz_gear=False)
        elif old_state == self.SERVO_MOVE_STATE:
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
        msg = ". Servo in %s position" % ("UP" if self.servo_state == self.SERVO_UP_STATE else \
                "DOWN" if self.servo_state == self.SERVO_DOWN_STATE else "MOVE" if self.servo_state == self.SERVO_MOVE_STATE else "unknown")
        return msg

    def get_status(self, eventtime):
        return {
            'servo': "Up" if self.servo_state == self.SERVO_UP_STATE else
                     "Down" if self.servo_state == self.SERVO_DOWN_STATE else
                     "Move" if self.servo_state == self.SERVO_MOVE_STATE else
                     "Unknown",
        }



################################################################################
# LinearServoSelector:
#  Implements Linear Selector for type-A MMU's with servo
#  - Stepper controlled linear movement with endstop
#  - Servo controlled filament gripping
#  - Supports type-A with combined selection and filament gripping line ERCFv3
#
class LinearServoSelector(LinearSelector, object):

    def __init__(self, mmu):
        super(LinearServoSelector, self).__init__(mmu)



################################################################################
# LinearMultiGearSelector:
#  Implements Linear Selector for type-C MMU's that:
#   - Uses gear driver stepper gate
#   - Uses selector stepper for gate selection with endstop
#   - Supports type-A classic MMU's like ERCF and Tradrack
# 
################################################################################

class LinearMultiGearSelector(LinearSelector, object):

    def __init__(self, mmu):
        super(LinearMultiGearSelector, self).__init__(mmu)

    # Selector "Interface" methods ---------------------------------------------

    def select_gate(self, gate):
        self.mmu_toolhead.select_gear_stepper(gate) # Select correct gear drive stepper or none if bypass
        super(LinearMultiGearSelector, self).select_gate(gate)

    def restore_gate(self, gate):
        self.mmu.mmu_toolhead.select_gear_stepper(gate) # Select correct gear drive stepper or none if bypass
        super(LinearMultiGearSelector, self).restore_gate(gate)



################################################################################
# Rotary Selector
# Implements Rotary Selector for type-A MMU's that uses stepper controlled
# rail[0] on mmu toolhead (3D Chameleon)
#
# 'filament_always_gripped' alters operation:
#   0 (default) - Lazy gate selection, occurs when asked to grip filament
#   1           - Gripped immediately on selection and will not release
#
# Implements commands:
#   MMU_CALIBRATE_SELECTOR
#   MMU_SOAKTEST_SELECTOR
#   MMU_GRIP    - realign with selected gate
#   MMU_RELEASE - move between gates to release filament
################################################################################

class RotarySelector(BaseSelector, object):

    # mmu_vars.cfg variables
    VARS_MMU_SELECTOR_OFFSETS  = "mmu_selector_offsets"
    VARS_MMU_SELECTOR_GATE_POS = "mmu_selector_gate_pos"

    def __init__(self, mmu):
        super(RotarySelector, self).__init__(mmu)

        # Process config
        self.selector_move_speed = mmu.config.getfloat('selector_move_speed', 200, minval=1.)
        self.selector_homing_speed = mmu.config.getfloat('selector_homing_speed', 100, minval=1.)
        self.selector_touch_speed = mmu.config.getfloat('selector_touch_speed', 60, minval=1.) # Not used with 3DChameleon but allows for param in config
        self.selector_touch_enabled = mmu.config.getint('selector_touch_enabled', 1, minval=0, maxval=1) # Not used with 3DChameleon but allows for param in config

        # To simplfy config CAD related parameters are set based on vendor and version setting
        #
        #  cad_gate0_pos          - approximate distance from endstop to first gate
        #  cad_gate_width         - width of each gate
        #  cad_last_gate_offset   - distance from end of travel to last gate
        #
        # Chameleon defaults
        self.cad_gate0_pos = 4.0
        self.cad_gate_width = 25.
        self.cad_last_gate_offset = 2.
        self.cad_bypass_offset = 0 # Doesn't have bypass
        self.cad_selector_tolerance = 15.

        self.cad_gate_directions = [1, 1, 0, 0]
        self.cad_release_gates = [2, 3, 0, 1]

        # But still allow all CAD parameters to be customized
        self.cad_gate0_pos = mmu.config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = mmu.config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_last_gate_offset = mmu.config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)
        self.cad_selector_tolerance = mmu.config.getfloat('cad_selector_tolerance', self.cad_selector_tolerance, above=0.) # Extra movement allowed by selector

        self.cad_gate_directions = list(mmu.config.getintlist('cad_gate_directions',self.cad_gate_directions))
        self.cad_release_gates = list(mmu.config.getintlist('cad_release_gates', self.cad_release_gates))

        # Register GCODE commands specific to this module
        gcode = mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc=self.cmd_MMU_CALIBRATE_SELECTOR_help)
        gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc=self.cmd_MMU_SOAKTEST_SELECTOR_help)
        gcode.register_command('MMU_GRIP', self.cmd_MMU_GRIP, desc=self.cmd_MMU_GRIP_help)
        gcode.register_command('MMU_RELEASE', self.cmd_MMU_RELEASE, desc=self.cmd_MMU_RELEASE_help)

        # Selector stepper setup before MMU toolhead is instantiated
        section = mmu_machine.SELECTOR_STEPPER_CONFIG
        if mmu.config.has_section(section):
            # Inject options into selector stepper config regardless or what user sets
            mmu.config.fileconfig.set(section, 'position_min', -1.)
            mmu.config.fileconfig.set(section, 'position_max', self._get_max_selector_movement())
            mmu.config.fileconfig.set(section, 'homing_speed', self.selector_homing_speed)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        self.grip_state = self.mmu.FILAMENT_DRIVE_STATE

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead
        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]

        # Have an endstop (most likely stallguard)?
        endstops = self.selector_rail.get_endstops()
        self.has_endstop = bool(endstops) and endstops[0][0].__class__.__name__ != "MockEndstop"

        # Load selector offsets (calibration set with MMU_CALIBRATE_SELECTOR) -------------------------------
        self.selector_offsets = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_OFFSETS, None)
        if self.selector_offsets:
            # Ensure list size
            if len(self.selector_offsets) == self.mmu.num_gates:
                self.mmu.log_debug("Loaded saved selector offsets: %s" % self.selector_offsets)
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.VARS_MMU_SELECTOR_OFFSETS)
                self.selector_offsets = self._ensure_list_size(self.selector_offsets, self.mmu.num_gates)

            if not any(x == -1 for x in self.selector_offsets):
                self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR
        else:
            self.mmu.log_always("Warning: Selector offsets not found in mmu_vars.cfg. Probably not calibrated")
            self.selector_offsets = [-1] * self.mmu.num_gates
        self.mmu.save_variables.allVariables[self.VARS_MMU_SELECTOR_OFFSETS] = self.selector_offsets

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    def home(self, force_unload = None):
        if self.mmu.check_if_bypass(): return
        with self.mmu.wrap_action(self.mmu.ACTION_HOMING):
            self.mmu.log_info("Homing MMU...")
            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))
            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)
            elif force_unload is False and self.mmu.filament_pos != self.mmu.FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self.mmu.unload_sequence()
            self._home_selector()

    # Actual gate selection can be delayed (if not forcing grip) until the
    # filament_drive/release to reduce selector movement
    def select_gate(self, gate):
        if gate != self.mmu.gate_selected:
            with self.mmu.wrap_action(self.mmu.ACTION_SELECTING):
                if self.mmu.mmu_machine.filament_always_gripped:
                    self._grip(gate)

    def restore_gate(self, gate):
        gate_pos = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_GATE_POS, None)
        if gate_pos is not None:
            self.set_position(self.selector_offsets[gate_pos])
            if gate == gate_pos:
                self.grip_state = self.mmu.FILAMENT_DRIVE_STATE
            else:
                self.grip_state = self.mmu.FILAMENT_RELEASE_STATE
        else:
            self.grip_state = self.mmu.FILAMENT_UNKNOWN_STATE

    def filament_drive(self):
        self._grip(self.mmu.gate_selected)

    def filament_release(self, measure=False):
        if not self.mmu.mmu_machine.filament_always_gripped:
            self._grip(self.mmu.gate_selected, release=True)
        return 0. # Fake encoder movement

    # Note there is no separation of gate selection and grip/release with this type of selector
    def _grip(self, gate, release=False):
        if gate >= 0:
            if release:
                release_pos = self.selector_offsets[self.cad_release_gates[gate]]
                self.mmu.log_trace("Setting selector to filament release position at position: %.1f" % release_pos)
                self._position(release_pos)
                self.grip_state = self.mmu.FILAMENT_RELEASE_STATE

                # Precaution to ensure correct postion/gate restoration on restart
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_GATE_POS, self.cad_release_gates[gate], write=True)
            else:
                grip_pos = self.selector_offsets[gate]
                self.mmu.log_trace("Setting selector to filament grip position at position: %.1f" % grip_pos)
                self._position(grip_pos)
                self.grip_state = self.mmu.FILAMENT_DRIVE_STATE

                # Precaution to ensure correct postion/gate restoration on restart
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_GATE_POS, gate, write=True)

            # Ensure gate filament drive is in the correct direction
            self.mmu_toolhead.get_kinematics().rails[1].set_direction(self.cad_gate_directions[gate])
            self.mmu.movequeues_wait()
        else:
            self.grip_state = self.mmu.FILAMENT_UNKNOWN_STATE

    def get_filament_grip_state(self):
        return self.grip_state

    def disable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False

    def enable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.mmu_toolhead.get_position()[0]
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        else:
            return False
        return True

    def get_status(self, eventtime):
        status = super(RotarySelector, self).get_status(eventtime)
        status.update({
            'grip': "Gripped" if self.grip_state == self.mmu.FILAMENT_DRIVE_STATE else "Released",
        })
        return status

    def get_mmu_status_config(self):
        msg = "\nSelector is NOT HOMED. " if not self.is_homed else ""
        msg += "Filament is %s" % ("GRIPPED" if self.grip_state == self.mmu.FILAMENT_DRIVE_STATE else "RELEASED")
        return msg

    def set_test_config(self, gcmd):
        self.selector_move_speed = gcmd.get_float('SELECTOR_MOVE_SPEED', self.selector_move_speed, minval=1.)
        self.selector_homing_speed = gcmd.get_float('SELECTOR_HOMING_SPEED', self.selector_homing_speed, minval=1.)

    def get_test_config(self):
        msg = "\n\nSELECTOR:"
        msg += "\nselector_move_speed = %.1f" % self.selector_move_speed
        msg += "\nselector_homing_speed = %.1f" % self.selector_homing_speed
        return msg

    def get_uncalibrated_gates(self, check_gates):
        return [gate for gate, value in enumerate(self.selector_offsets) if value == -1 and gate in check_gates]

    # Internal Implementation --------------------------------------------------

    cmd_MMU_GRIP_help = "Grip filament in current gate"
    def cmd_MMU_GRIP(self, gcmd):
        if self.mmu.gate_selected >= 0:
            self.filament_drive()

    cmd_MMU_RELEASE_help = "Ungrip filament in current gate"
    def cmd_MMU_RELEASE(self, gcmd):
        if self.mmu.gate_selected >= 0:
            if not self.mmu.mmu_machine.filament_always_gripped:
                self.filament_release()
            else:
                self.mmu.log_error("Selector configured to not allow filament release")

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector positions or postion of specified gate"
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        quick = gcmd.get_int('QUICK', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', 0, minval=0, maxval=self.mmu.mmu_machine.num_gates - 1)

        try:
            self.mmu.calibrating = True
            self.mmu.reinit()
            successful = False

            if self.has_endstop and not quick:
                successful = self._calibrate_selector(gate, extrapolate=not single, save=save)
            else:
                self.mmu.log_always("%s - will calculate gate offsets from cad_gate0_offset and cad_gate_width" % ("Quick method" if quick else "No endstop configured"))
                self.selector_offsets = [round(self.cad_gate0_pos + i * self.cad_gate_width, 1) for i in range(self.mmu.num_gates)]
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True)
                successful = True

            if not any(x == -1 for x in self.selector_offsets):
                self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR

            # If not fully calibrated turn off the selector stepper to ease next step, else activate by homing
            if successful and self.mmu.calibration_status & self.mmu.CALIBRATED_SELECTOR:
                self.mmu.log_always("Selector calibration complete")
                self.mmu.select_tool(0)
            else:
                self.mmu.motors_onoff(on=False, motor="selector")

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
        finally:
            self.mmu.calibrating = False

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_loaded(): return
        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 100)
        home = bool(gcmd.get_int('HOME', 0))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                if home:
                    self.home()
                for l in range(loops):
                    self.mmu.log_always("Testing loop %d / %d" % (l + 1, loops))
                    tool = random.randint(0, self.mmu.num_gates - 1)
                    if random.randint(0, 10) == 0 and home:
                        self.mmu.home(tool=tool)
                    else:
                        if random.randint(0, 10) == 0 and home:
                            self.mmu.home(tool=tool)
                        else:
                            self.mmu.select_tool(tool)
                        if not self.mmu.mmu_machine.filament_always_gripped:
                            self.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu.num_gates - 1

        max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)
        max_movement += self.cad_last_gate_offset if gate in [self.mmu.TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.cad_selector_tolerance
        return max_movement

    # Manual selector offset calibration
    def _calibrate_selector(self, gate, extrapolate=True, save=True):
        max_movement = self._get_max_selector_movement(gate)
        self.mmu.log_always("Measuring the selector position for gate %d..." % gate)
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
            self.selector_offsets[gate] = round(traveled, 1)
            if extrapolate and gate == self.mmu.num_gates - 1 and self.selector_offsets[0] > 0:
                # Distribute selector spacing based on measurements of first and last gate
                spacing = (self.selector_offsets[-1] - self.selector_offsets[0]) / (self.mmu.num_gates - 1)
                self.selector_offsets = [round(self.selector_offsets[0] + i * spacing, 1) for i in range(self.mmu.num_gates)]
            elif extrapolate:
                # Distribute using cad spacing
                self.selector_offsets = [round(self.selector_offsets[0] + i * self.cad_gate_width, 1) for i in range(self.mmu.num_gates)]
            else:
                extrapolate = False
            self.mmu.save_variable(self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True)

            if extrapolate:
                self.mmu.log_always("All selector offsets have been extrapolated and saved:\n%s" % self.selector_offsets)
            else:
                self.mmu.log_always("Selector offset (%.1fmm) for gate %d has been saved" % (traveled, gate))
                if gate == 0:
                    self.mmu.log_always("Run MMU_CALIBRATE_SELECTOR again with GATE=%d to extrapolate all gate positions. Use SINGLE=1 to force calibration of only one gate" % (self.mmu.num_gates - 1))
        return True

    def _home_selector(self):
        self.mmu.unselect_gate()
        self.mmu.movequeues_wait()
        try:
            if self.has_endstop:
                homing_state = mmu_machine.MmuHoming(self.mmu.printer, self.mmu_toolhead)
                homing_state.set_axes([0])
                self.mmu.mmu_toolhead.get_kinematics().home(homing_state)
            else:
                self._home_hard_endstop()
            self.is_homed = True
        except Exception as e: # Homing failed
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def _home_hard_endstop(self):
        self.mmu.log_always("Forcing selector homing to hard endstop. Excuse the noise!\n(Configure stallguard endstop on selector stepper to avoid)")
        self.set_position(self._get_max_selector_movement()) # Worst case position to allow full movement
        self.move("Forceably homing to hard endstop", new_pos=0, speed=self.selector_homing_speed)
        self.set_position(0) # Reset pos

    def _position(self, target):
        self.move("Positioning selector", target)

    def move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, wait=wait)

    # Internal raw wrapper around all selector moves except rail homing
    # Returns position after move, if homed (homing moves)
    def _trace_selector_move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        if trace_str:
            self.mmu.log_trace(trace_str)

        self.mmu_toolhead.quiesce()

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.selector_move_speed
        accel = accel or self.mmu_toolhead.get_selector_limits()[1]

        pos = self.mmu_toolhead.get_position()
        with self.mmu.wrap_accel(accel):
            pos[0] = new_pos
            self.mmu_toolhead.move(pos, speed)
        if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
            self.mmu.log_stepper("SELECTOR MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (new_pos, speed, accel))
        if wait:
            self.mmu.movequeues_wait(toolhead=False, mmu_toolhead=True)
        return pos[0]

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
            homing_state = mmu_machine.MmuHoming(self.mmu.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_toolhead.get_kinematics().home(homing_state)
            homed = True
        except Exception:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * self.selector_stepper.get_step_dist()
        return traveled, homed



################################################################################
# Macro Selector
# Implements macro-based selector for MMU's
#
# Example demultiplexer-style SELECT_TOOL macro:
# [gcode_macro SELECT_TOOL]
# gcode:
#     SET_PIN PIN=d0 VALUE={params.S0}
#     SET_PIN PIN=d1 VALUE={params.S1}
#     SET_PIN PIN=d2 VALUE={params.S2}
# 
# Example optocoupler-style SELECT_TOOL macro:
# [gcode_macro SELECT_TOOL]
# gcode:
#     SET_PIN PIN=o{printer.mmu.gate} VALUE=0
#     SET_PIN PIN=o{params.GATE} VALUE=1
################################################################################

class MacroSelector(BaseSelector, object):

    def __init__(self, mmu):
        super(MacroSelector, self).__init__(mmu)
        self.is_homed = True

        self.printer = mmu.printer
        self.gcode = self.printer.lookup_object('gcode')

        self.select_tool_macro = mmu.config.get('select_tool_macro')
        self.select_tool_num_switches = mmu.config.getint('select_tool_num_switches', default=0, minval=1)

        # Check if using a demultiplexer-style setup
        if self.select_tool_num_switches > 0:
            self.binary_mode = True
            max_num_tools = 2**self.select_tool_num_switches
            # Verify that there aren't too many tools for the demultiplexer
            if mmu.num_gates > max_num_tools:
                raise mmu.config.error('Maximum number of allowed tools is %d, but %d are present.' % (max_num_tools, mmu.num_gates))
        else:
            self.binary_mode = False

        # Read all controller parameters related to selector or servo to stop klipper complaining. This
        # is done to allow for uniform and shared mmu_parameters.cfg file regardless of configuration.
        for option in ['selector_', 'servo_', 'cad_']:
            for key in mmu.config.get_prefix_options(option):
                _ = mmu.config.get(key)

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead
        self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR # No calibration necessary

    def handle_ready(self):
        logging.info("Happy Hare MacroSelector: Gate %d" % self.mmu.gate_selected)
        self.select_gate(self.mmu.gate_selected)

    def select_gate(self, gate):
        # Store parameters as list
        params = ['GATE=' + str(gate)]
        if self.binary_mode: # If demultiplexer, pass binary parameters to the macro in the form of S0=, S1=, S2=, etc.
            binary = list(reversed('{0:b}'.format(gate).zfill(self.select_tool_num_switches)))
            for i in range(self.select_tool_num_switches):
                char = binary[i]
                params.append('S' + str(i) + '=' + str(char))
        params = ' '.join(params)

        # Call selector macro
        self.mmu.wrap_gcode_command('%s %s' % (self.select_tool_macro, params))

    def restore_gate(self, gate):
        pass



################################################################################
# Servo Selector
# Implements Servo based Selector for type-A MMU's like PicoMMU. Filament is
# always gripped when gate selected but a release position is assumed between
# each gate position (or specified release position, often 0 degrees)
#
# 'filament_always_gripped' alters operation:
#   0 (default) - Lazy gate selection, occurs when asked to grip filament
#   1           - Gripped immediately on selection and will not release
#
# Implements commands:
#   MMU_CALIBRATE_SELECTOR
#   MMU_SOAKTEST_SELECTOR
#   MMU_GRIP    - realign with selected gate
#   MMU_RELEASE - move between gates to release filament
################################################################################

class ServoSelector(BaseSelector, object):

    # mmu_vars.cfg variables
    VARS_MMU_SELECTOR_ANGLES       = "mmu_selector_angles"
    VARS_MMU_SELECTOR_BYPASS_ANGLE = "mmu_selector_bypass_angle"

    def __init__(self, mmu):

        super(ServoSelector, self).__init__(mmu)
        self.is_homed = True
        self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE
        self.selector_bypass_angle = -1

        # Get hardware
        servo_name = mmu.config.get('selector_servo_name', "selector_servo")
        self.servo = mmu.printer.lookup_object("mmu_servo %s" % servo_name, None)
        if not self.servo:
            raise self.mmu.config.error("Selector servo not found. Perhaps missing '[mmu_servo %s]' definition" % servo_name)

        # Process config
        self.servo_duration = mmu.config.getfloat('servo_duration', 0.5, minval=0.1)
        self.servo_dwell = mmu.config.getfloat('servo_dwell', 0.6, minval=0.1)
        self.servo_always_active = mmu.config.getint('servo_always_active', 0, minval=0, maxval=1)
        self.servo_min_angle = mmu.config.getfloat('servo_min_angle', 0, above=0)                    # Not exposed
        self.servo_max_angle = mmu.config.getfloat('servo_max_angle', self.servo.max_angle, above=0) # Not exposed
        self.servo_angle = self.servo_min_angle + (self.servo_max_angle - self.servo_min_angle) / 2
        self.selector_release_angle = mmu.config.getfloat('selector_release_angle', -1, minval=-1, maxval=self.servo_max_angle)
        self.selector_bypass_angle = mmu.config.getfloat('selector_bypass_angle', -1, minval=-1, maxval=self.servo_max_angle)
        self.selector_angles = list(mmu.config.getintlist('selector_gate_angles', []))

        # Register GCODE commands specific to this module
        gcode = mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc = self.cmd_MMU_CALIBRATE_SELECTOR_help)
        gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc=self.cmd_MMU_SOAKTEST_SELECTOR_help)
        gcode.register_command('MMU_GRIP', self.cmd_MMU_GRIP, desc=self.cmd_MMU_GRIP_help)
        gcode.register_command('MMU_RELEASE', self.cmd_MMU_RELEASE, desc=self.cmd_MMU_RELEASE_help)

        # Read all controller parameters related to selector or servo to stop klipper complaining. This
        # is done to allow for uniform and shared mmu_parameters.cfg file regardless of configuration.
        for option in ['selector_', 'servo_', 'cad_']:
            for key in mmu.config.get_prefix_options(option):
                _ = mmu.config.get(key)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE

    def handle_connect(self):
        # Load and merge calibrated selector angles (calibration set with MMU_CALIBRATE_SELECTOR) -----------
        self.selector_angles = self._ensure_list_size(self.selector_angles, self.mmu.num_gates)

        cal_selector_angles = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_ANGLES, [])
        if cal_selector_angles:
            self.mmu.log_debug("Loaded saved selector angles: %s" % cal_selector_angles)
        else:
            self.mmu.log_always("Warning: Selector angles not found in mmu_vars.cfg. Using configured defaults")

        # Merge calibrated angles with conf angles
        for gate, angle in enumerate(zip(self.selector_angles, cal_selector_angles)):
            if angle[1] >= 0:
                self.selector_angles[gate] = angle[1]

        if not any(x == -1 for x in self.selector_angles):
            self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR

        selector_bypass_angle = self.mmu.save_variables.allVariables.get(self.VARS_MMU_SELECTOR_BYPASS_ANGLE, -1)
        if selector_bypass_angle >= 0:
            self.selector_bypass_angle = selector_bypass_angle
            self.mmu.log_debug("Loaded saved bypass angle: %s" % self.selector_bypass_angle)

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    # Actual gate selection (servo movement) can be delayed until the filament_drive/release instruction
    # to prevent unecessary flutter. Conrolled by `filament_always_gripped` setting
    def select_gate(self, gate):
        if gate != self.mmu.gate_selected:
            with self.mmu.wrap_action(self.mmu.ACTION_SELECTING):
                if self.mmu.mmu_machine.filament_always_gripped:
                    self._grip(gate)

    def restore_gate(self, gate):
        if gate == self.mmu.TOOL_GATE_BYPASS:
            self.servo_state = self.mmu.FILAMENT_RELEASE_STATE
            self.mmu.log_trace("Setting servo to bypass angle: %.1f" % self.selector_bypass_angle)
            self._set_servo_angle(self.selector_bypass_angle)
        else:
            if self.mmu.mmu_machine.filament_always_gripped:
                self._grip(gate)
            else:
                # Defer movement until filament_drive/release/hold call
                self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE

    def filament_drive(self):
        self._grip(self.mmu.gate_selected)

    def filament_release(self, measure=False):
        if not self.mmu.mmu_machine.filament_always_gripped:
            self._grip(self.mmu.gate_selected, release=True)
        return 0. # Fake encoder movement

    # Common logic for servo manipulation
    def _grip(self, gate, release=False):
        if gate == self.mmu.TOOL_GATE_BYPASS:
            self.mmu.log_trace("Setting servo to bypass angle: %.1f" % self.selector_bypass_angle)
            self._set_servo_angle(self.selector_bypass_angle)
            self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE
        elif gate >= 0:
            if release:
                release_angle = self._get_closest_released_angle()
                self.mmu.log_trace("Setting servo to filament released position at angle: %.1f" % release_angle)
                self._set_servo_angle(release_angle)
                self.servo_state = self.mmu.FILAMENT_RELEASE_STATE
            else:
                angle = self.selector_angles[gate]
                self.mmu.log_trace("Setting servo to filament grip position at angle: %.1f" % angle)
                self._set_servo_angle(angle)
                self.servo_state = self.mmu.FILAMENT_DRIVE_STATE
        else:
            self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE

    def get_filament_grip_state(self):
        return self.servo_state

    def buzz_motor(self, motor):
        if motor == "selector":
            prev_servo_angle = self.servo_angle
            low = max(min(self.selector_angles), self.servo_min_angle)
            high = min(max(self.selector_angles), self.servo_max_angle)
            mid = (low + high) / 2
            move = (high - low) / 4
            self._set_servo_angle(angle=mid)
            self._set_servo_angle(angle=mid - move)
            self._set_servo_angle(angle=mid + move)
            self._set_servo_angle(angle=prev_servo_angle)
        else:
            return False
        return True

    def has_bypass(self):
        return self.mmu.mmu_machine.has_bypass and self.selector_bypass_angle >= 0

    def get_status(self, eventtime):
        status = super(ServoSelector, self).get_status(eventtime)
        status.update({
            'grip': "Gripped" if self.servo_state == self.mmu.FILAMENT_DRIVE_STATE else "Released",
        })
        return status

    def get_mmu_status_config(self):
        msg = super(ServoSelector, self).get_mmu_status_config()
        msg += ". Servo in %s position" % ("GRIP" if self.servo_state == self.mmu.FILAMENT_DRIVE_STATE else \
                "RELEASE" if self.servo_state == self.mmu.FILAMENT_RELEASE_STATE else "unknown")
        return msg

    def get_uncalibrated_gates(self, check_gates):
        return [gate for gate, value in enumerate(self.selector_angles) if value == -1 and gate in check_gates]

    # Internal Implementation --------------------------------------------------

    cmd_MMU_GRIP_help = "Grip filament in current gate"
    def cmd_MMU_GRIP(self, gcmd):
        if self.mmu.gate_selected >= 0:
            self.filament_drive()

    cmd_MMU_RELEASE_help = "Ungrip filament in current gate"
    def cmd_MMU_RELEASE(self, gcmd):
        if self.mmu.gate_selected >= 0:
            if not self.mmu.mmu_machine.filament_always_gripped:
                self.filament_release()
            else:
                self.mmu.log_error("Selector configured to not allow filament release")

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector servo angle for specifed gate(s)"
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        usage = "\nUsage: MMU_CALIBRATE_SELECTOR [GATE=x] [BYPASS=0|1] [SPACING=x] [ANGLE=x] [SAVE=0|1] [SINGLE=0|1] [SHOW=0|1]"
        show = gcmd.get_int('SHOW', 0)
        angle = gcmd.get_int('ANGLE', None)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        spacing = gcmd.get_float('SPACING', 25., above=0, below=180) # TiPicoMMU is 25 degrees between gates
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu.mmu_machine.num_gates - 1)
        if gate == -1 and gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
            gate = self.mmu.TOOL_GATE_BYPASS

        if show:
            msg = ""
            if not self.mmu.calibration_status & self.mmu.CALIBRATED_SELECTOR:
                msg += "Calibration not complete\n"
            msg += "Current selector gate angle positions are: %s degrees" % self.selector_angles
            if self.selector_release_angle >= 0:
                msg += "\nRelease angle is fixed at: %s degrees" % self.selector_release_angle
            else:
                msg += "\nRelease angles configured to be between each gate angle"
            if self.has_bypass():
                msg += "\nBypass angle: %s" % self.selector_bypass_angle
            else:
                msg += "\nBypass angle not configured"
            self.mmu.log_info(msg)

        elif angle is not None:
            self.mmu.log_debug("Setting selector servo to angle: %d" % angle)
            self._set_servo_angle(angle)
            self.servo_state = self.mmu.FILAMENT_UNKNOWN_STATE

        elif save:
            if gate == self.mmu.TOOL_GATE_BYPASS:
                self.selector_bypass_angle = self.servo_angle
                self.mmu.save_variable(self.VARS_MMU_SELECTOR_BYPASS_ANGLE, self.selector_bypass_angle, write=True)
                self.mmu.log_info("Servo angle '%d' for bypass position has been saved" % self.servo_angle)
            elif gate >= 0:
                if single:
                    self.selector_angles[gate] = self.servo_angle
                    self.mmu.save_variable(self.VARS_MMU_SELECTOR_ANGLES, self.selector_angles, write=True)
                    self.mmu.log_info("Servo angle '%d' for gate %d has been saved" % (self.servo_angle, gate))
                else:
                    # If possible evenly distribute based on spacing
                    angles = self._generate_gate_angles(self.servo_angle, gate, spacing)
                    if angles:
                        self.selector_angles = angles
                        self.mmu.save_variable(self.VARS_MMU_SELECTOR_ANGLES, self.selector_angles, write=True)
                        self.mmu.log_info("Selector gate angle positions %s has been saved" % self.selector_angles)
                    else:
                        self.mmu.log_error("Not possible to distribute angles with separation of %.1f degrees with gate %d at %.1f%s" % (spacing, gate, self.servo_angle, usage))
            else:
                self.mmu.log_error("No gate specified%s" % usage)
        else:
            self.mmu.log_always("Current selector servo angle: %d, Selector gate angle positions: %s" % (self.servo_angle, self.selector_angles))

        if not any(x == -1 for x in self.selector_angles):
            self.mmu.calibration_status |= self.mmu.CALIBRATED_SELECTOR

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_loaded(): return
        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 10)
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    self.mmu.log_always("Testing loop %d / %d" % (l + 1, loops))
                    tool = random.randint(0, self.mmu.num_gates - 1)
                    self.mmu.select_tool(tool)
                    if not self.mmu.mmu_machine.filament_always_gripped:
                        self.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))

    def _set_servo_angle(self, angle):
        if angle >= 0 and angle != self.servo_angle:
            self.mmu.movequeues_wait()
            self.servo.set_position(angle=angle, duration=None if self.servo_always_active else self.servo_duration)
            self.servo_angle = angle
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))

    def _get_closest_released_angle(self):
        if self.selector_release_angle >= 0:
            return self.selector_release_angle
        neutral_angles = [(self.selector_angles[i] + self.selector_angles[i + 1]) / 2 for i in range(len(self.selector_angles) - 1)]
        closest_angle = 0
        min_difference = float('inf')
        for angle in neutral_angles:
            difference = abs(angle - self.servo_angle)
            if difference < min_difference:
                min_difference = difference
                closest_angle = max(0, angle)
        return closest_angle

    def _generate_gate_angles(self, known_angle, known_gate, spacing):
        angles = []
        start_angle = known_angle - known_gate * spacing
        for i in range(self.mmu.num_gates):
            a = start_angle + i * spacing
            if not (self.servo_min_angle <= a <= self.servo_max_angle):
                return None # Not possible
            angles.append(round(a))
        return angles



################################################################################
# Indexed Selector
# Implements simple Indexed Selector for type-A MMU's that uses a stepper for
# gate selection but has an indexing sensor for each gate.
# E.g. As fitted to BTT ViViD
#
# Implements commands:
#   MMU_SOAKTEST_SELECTOR
################################################################################

class IndexedSelector(BaseSelector, object):

    def __init__(self, mmu):
        super(IndexedSelector, self).__init__(mmu)
        self.is_homed = True

        # Process config
        self.selector_move_speed = mmu.config.getfloat('selector_move_speed', 100, minval=1.)
        self.selector_homing_speed = mmu.config.getfloat('selector_homing_speed', self.selector_move_speed, minval=1.)
        self.selector_touch_speed = mmu.config.getfloat('selector_touch_speed', 60, minval=1.) # Not used with ViViD but allows for param in config
        self.selector_touch_enabled = mmu.config.getint('selector_touch_enabled', 1, minval=0, maxval=1) # Not used with ViViD but allows for param in config
        self.selector_index_distance = mmu.config.getfloat('selector_index_distance', 5, minval=0.)

        # To simplfy config CAD related parameters are set based on vendor and version setting
        self.cad_gate_width = 90. # Rotation distance set to make this equivalent to degrees
        self.cad_max_rotations = 2

        # But still allow all CAD parameters to be customized
        self.cad_gate_width = mmu.config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_max_rotations = mmu.config.getfloat('cad_max_rotations', self.cad_max_rotations, above=0.)

        # Register GCODE commands
        gcode = mmu.printer.lookup_object('gcode')
        gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc=self.cmd_MMU_SOAKTEST_SELECTOR_help)

        # Selector stepper setup before MMU toolhead is instantiated
        section = mmu_machine.SELECTOR_STEPPER_CONFIG
        if mmu.config.has_section(section):
            # Inject options into selector stepper config regardless or what user sets
            mmu.config.fileconfig.set(section, 'homing_speed', self.selector_homing_speed)

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        self.mmu_toolhead = self.mmu.mmu_toolhead
        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]
        self._set_position(0) # Reset pos

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_loaded(): return
        if self.mmu.check_if_not_calibrated(self.mmu.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 100)
        home = bool(gcmd.get_int('HOME', 0))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                if home:
                    self.home()
                for l in range(loops):
                    self.mmu.log_always("Testing loop %d / %d" % (l + 1, loops))
                    tool = random.randint(0, self.mmu.num_gates)
                    if tool == self.mmu.num_gates:
                        self.mmu.select_bypass()
                    else:
                        self.mmu.select_tool(tool)
                    if not self.mmu.mmu_machine.filament_always_gripped:
                        self.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))

    def bootup(self):
        self.select_gate(self.mmu.gate_selected)

    def home(self, force_unload = None):
        if self.mmu.check_if_bypass(): return
        with self.mmu.wrap_action(self.mmu.ACTION_HOMING):
            self.mmu.log_info("Homing MMU...")
            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))
            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)
            elif force_unload is None and self.mmu.filament_pos != self.mmu.FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self.mmu.unload_sequence()
            self._home_selector()

    def select_gate(self, gate):
        if gate >= 0:
            endstop = self.selector_rail.get_extra_endstop(self._get_gate_endstop(gate))
            if not endstop:
                raise MmuError("Extra endstop %s not defined on the selector stepper" % self._get_gate_endstop(gate))
            mcu_endstop = endstop[0][0]
            if not mcu_endstop.query_endstop(self.mmu_toolhead.get_last_move_time()):
                with self.mmu.wrap_action(self.mmu.ACTION_SELECTING):
                    self._find_gate(gate)

    def disable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False

    def enable_motors(self):
        stepper_enable = self.mmu.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.mmu_toolhead.get_position()[0]
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        else:
            return False
        return True

    def get_mmu_status_config(self):
        msg = "\nSelector is NOT HOMED" if not self.is_homed else ""
        return msg

    def set_test_config(self, gcmd):
        self.selector_move_speed = gcmd.get_float('SELECTOR_MOVE_SPEED', self.selector_move_speed, minval=1.)
        self.selector_homing_speed = gcmd.get_float('SELECTOR_HOMING_SPEED', self.selector_homing_speed, minval=1.)

    def get_test_config(self):
        msg = "\n\nSELECTOR:"
        msg += "\nselector_move_speed = %.1f" % self.selector_move_speed
        msg += "\nselector_homing_speed = %.1f" % self.selector_homing_speed
        return msg

    # Internal Implementation --------------------------------------------------

    def _get_max_selector_movement(self):
        max_movement = self.mmu.num_gates * self.cad_gate_width * self.cad_max_rotations
        return max_movement

    def _home_selector(self):
        self.mmu.unselect_gate()
        self.mmu.movequeues_wait()
        try:
            self._find_gate(0)
            self.is_homed = True
        except Exception as e: # Homing failed
            logging.error(traceback.format_exc())
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def _get_gate_endstop(self, gate):
        return "unit0_gate%d" % gate

    def _find_gate(self, gate):
        rotation_dir = self._best_rotation_direction(self.mmu.gate_selected, gate)
        max_move = self._get_max_selector_movement() * rotation_dir
        self.mmu.movequeues_wait()
        actual,homed = self._trace_selector_move("Indexing selector", max_move, speed=self.selector_move_speed, homing_move=1, endstop_name=self._get_gate_endstop(gate))
        if abs(actual) > 0 and homed:
            # If we actually moved to home make sure we are centered on index endstop
            center_move = (self.selector_index_distance / 2) * rotation_dir
            self._trace_selector_move("Centering selector", center_move, speed=self.selector_move_speed)

    # TODO automate the setup of the sequence through homing move on startup
    def _best_rotation_direction(self, start_gate, end_gate):
        if start_gate < 0:
            return 1 # Forward direction

        sequence = [0, 2, 1, 3] # Forward order of gates
        n = len(sequence)
        forward_distance = reverse_distance = 0

        # Find distance in forward direction
        start_idx = sequence.index(start_gate)
        for i in range(1, n):
            if sequence[(start_idx + i) % n] == end_gate:
                forward_distance = i
                break

        # Find distance in reverse direction
        rev_seq = sequence[::-1]
        start_idx = rev_seq.index(start_gate)
        for i in range(1, n):
            if rev_seq[(start_idx + i) % n] == end_gate:
                reverse_distance = i
                break

        return 1 if forward_distance <= reverse_distance else -1

    # Internal raw wrapper around all selector moves
    # Returns position after move, and if homed (homing moves)
    def _trace_selector_move(self, trace_str, dist, speed=None, accel=None, homing_move=0, endstop_name="default", wait=False):
        null_rtn = (0., False)
        homed = False
        actual = dist

        self.mmu_toolhead.quiesce()

        if homing_move != 0:
            # Check for valid endstop
            endstops = self.selector_rail.get_endstops() if endstop_name is None else self.selector_rail.get_extra_endstop(endstop_name)
            if endstops is None:
                self.mmu.log_error("Endstop '%s' not found" % endstop_name)
                return null_rtn

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.selector_homing_speed if homing_move != 0 else self.selector_move_speed
        accel = accel or self.mmu_toolhead.get_selector_limits()[1]

        pos = self.mmu_toolhead.get_position()
        if homing_move != 0:
            try:
                with self.mmu.wrap_accel(accel):
                    init_pos = pos[0]
                    pos[0] += dist
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.mmu.printer, endstops, self.mmu_toolhead)
                    trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                    homed = True
            except self.mmu.printer.command_error as e:
                homed = False

            halt_pos = self.mmu_toolhead.get_position()
            actual = halt_pos[0] - init_pos
            if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s >> %s" % (dist, speed, accel, endstop_name, wait, "%s halt_pos=%.1f (rail moved=%.1f), trig_pos=%.1f" % ("HOMED" if homed else "DID NOT HOMED",  halt_pos[0], actual, trig_pos[0])))

        else:
            with self.mmu.wrap_accel(accel):
                pos[0] += dist
                self.mmu_toolhead.move(pos, speed)
            if self.mmu.log_enabled(self.mmu.LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (dist, speed, accel))

        self.mmu_toolhead.flush_step_generation() # TTC mitigation (TODO: still required?)
        self.mmu.toolhead.flush_step_generation() # TTC mitigation (TODO: still required?)
        if wait:
            self.mmu.movequeues_wait(toolhead=False, mmu_toolhead=True)

        if trace_str:
            if homing_move != 0:
                trace_str += ". Stepper: selector %s after moving %.1fmm (of max %.1fmm)"
                trace_str = trace_str % (("homed" if homed else "did not home"), actual, dist)
                trace_str += ". Pos: @%.1f" % self.mmu_toolhead.get_position()[0]
            else:
                trace_str += ". Stepper: selector moved %.1fmm" % dist
            trace_str += ". Pos: @%.1f" % self.mmu_toolhead.get_position()[0]
            self.mmu.log_trace(trace_str)

        return actual, homed

    def _set_position(self, position):
        pos = self.mmu_toolhead.get_position()
        pos[0] = position
        self.mmu_toolhead.set_position(pos)
        self.enable_motors()
        self.is_homed = True
        return position
