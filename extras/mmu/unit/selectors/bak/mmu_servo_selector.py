# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Servo Selector
# - Servo based Selector for PicoMMU and clones
#
# Implements commands (selector dependent):
#    MMU_CALIBRATE_SELECTOR
#    MMU_SOAKTEST_SELECTOR (PhysicalSelector)
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
from ....homing        import Homing, HomingMove

# Happy Hare imports
from ...mmu_constants    import *
from ...mmu_utils        import MmuError
from ..mmu_calibrator    import CALIBRATED_SELECTOR
from .mmu_base_selectors import PhysicalSelector


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

class ServoSelector(PhysicalSelector):

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.is_homed = True

        self.servo_state = FILAMENT_UNKNOWN_STATE
        self.servo_bypass_angle = -1

        # Get hardware
        self.servo = self.mmu_unit.selector_servo
        if not self.servo:
            raise self.config.error("Selector servo not found")

        # Process config
        self.servo_duration = config.getfloat('servo_duration', 0.5, minval=0.1)
        self.servo_dwell = config.getfloat('servo_dwell', 0.6, minval=0.1)
        self.servo_always_active = config.getint('servo_always_active', 0, minval=0, maxval=1)
        self.servo_min_angle = config.getfloat('servo_min_angle', 0, above=0)                    # Not exposed
        self.servo_max_angle = config.getfloat('servo_max_angle', self.servo.max_angle, above=0) # Not exposed
        self.servo_angle = self.servo_min_angle + (self.servo_max_angle - self.servo_min_angle) / 2
        self.servo_release_angle = config.getfloat('servo_release_angle', -1, minval=-1, maxval=self.servo_max_angle)
        self.servo_bypass_angle = config.getfloat('servo_bypass_angle', -1, minval=-1, maxval=self.servo_max_angle)
        self.servo_gate_angles = list(config.getintlist('servo_gate_angles', []))

        # Register GCODE commands specific to this module
        self.register_mux_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc=self.cmd_MMU_CALIBRATE_SELECTOR_help)
        self.register_mux_command('MMU_GRIP', self.cmd_MMU_GRIP, desc=self.cmd_MMU_GRIP_help)
        self.register_mux_command('MMU_RELEASE', self.cmd_MMU_RELEASE, desc=self.cmd_MMU_RELEASE_help)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        self.servo_state = FILAMENT_UNKNOWN_STATE

    def handle_connect(self):
        super().handle_connect()

        self.var_manager.upgrade(VARS_MMU_SELECTOR_ANGLES, self.mmu_unit.name) # v3 upgrade
        self.var_manager.upgrade(VARS_MMU_SELECTOR_BYPASS_ANGLE, self.mmu_unit.name) # v3 upgrade

        # Load and merge calibrated selector angles (calibration set with MMU_CALIBRATE_SELECTOR) ------------
        self.servo_gate_angles = self._ensure_list_size(self.servo_gate_angles, self.mmu_unit.num_gates)

        cal_servo_gate_angles = self.var_manager.get(VARS_MMU_SELECTOR_ANGLES, [], namespace=self.mmu_unit.name)
        if cal_servo_gate_angles:
            self.mmu.log_debug("Loaded saved selector angles: %s" % cal_servo_gate_angles)
        else:
            self.mmu.log_always("Warning: Selector angles not found in mmu_vars.cfg. Using configured defaults")

        # Merge calibrated angles with conf angles
        for gate, angle in enumerate(zip(self.servo_gate_angles, cal_servo_gate_angles)):
            if angle[1] >= 0:
                self.servo_gate_angles[gate] = angle[1]

        if not any(x == -1 for x in self.servo_gate_angles):
            self.calibrator.mark_calibrated(self.calibrator.CALIBRATED_SELECTOR)

        servo_bypass_angle = self.var_manager.get(VARS_MMU_SELECTOR_BYPASS_ANGLE, -1, namespace=self.mmu_unit.name)
        if servo_bypass_angle >= 0:
            self.servo_bypass_angle = servo_bypass_angle
            self.mmu.log_debug("Loaded saved bypass angle: %s" % self.servo_bypass_angle)

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    # Actual gate selection (servo movement) can be delayed until the filament_drive/release instruction
    # to prevent unecessary flutter. Conrolled by `filament_always_gripped` setting
    def select_gate(self, gate):
        if gate != self.mmu.gate_selected:
            with self.mmu.wrap_action(ACTION_SELECTING):
                if self.mmu_unit.filament_always_gripped:
                    self._grip(self.local_gate(gate))

    def restore_gate(self, gate):
        if gate == TOOL_GATE_BYPASS:
            self.servo_state = FILAMENT_RELEASE_STATE
            self.mmu.log_trace("Setting servo to bypass angle: %.1f" % self.servo_bypass_angle)
            self._set_servo_angle(self.servo_bypass_angle)
        else:
            if self.mmu_unit.filament_always_gripped:
                self._grip(self.local_gate(gate))
            else:
                # Defer movement until filament_drive/release/hold call
                self.servo_state = FILAMENT_UNKNOWN_STATE

    def filament_drive(self):
        self._grip(self.local_gate(self.mmu.gate_selected))

    def filament_release(self, measure=False):
        if not self.mmu_unit.filament_always_gripped:
            self._grip(self.local_gate(self.mmu.gate_selected), release=True)
        return 0. # Fake encoder movement

    # Common logic for servo manipulation
    def _grip(self, gate, release=False):
        if gate == TOOL_GATE_BYPASS:
            self.mmu.log_trace("Setting servo to bypass angle: %.1f" % self.servo_bypass_angle)
            self._set_servo_angle(self.servo_bypass_angle)
            self.servo_state = FILAMENT_UNKNOWN_STATE
        elif gate >= 0:
            if release:
                release_angle = self._get_closest_released_angle()
                self.mmu.log_trace("Setting servo to filament released position at angle: %.1f" % release_angle)
                self._set_servo_angle(release_angle)
                self.servo_state = FILAMENT_RELEASE_STATE
            else:
                angle = self.servo_gate_angles[self.local_gate(gate)]
                self.mmu.log_trace("Setting servo to filament grip position at angle: %.1f" % angle)
                self._set_servo_angle(angle)
                self.servo_state = FILAMENT_DRIVE_STATE
        else:
            self.servo_state = FILAMENT_UNKNOWN_STATE

    def get_filament_grip_state(self):
        return self.servo_state

    def buzz_motor(self, motor):
        if motor == "selector":
            prev_servo_angle = self.servo_angle
            low = max(min(self.servo_gate_angles), self.servo_min_angle)
            high = min(max(self.servo_gate_angles), self.servo_max_angle)
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
        return self.mmu_unit.has_bypass and self.servo_bypass_angle >= 0

    def get_status(self, eventtime):
        status = super().get_status(eventtime)
        status.update({
            'grip': "Gripped" if self.servo_state == FILAMENT_DRIVE_STATE else "Released",
        })
        return status

    def get_mmu_status_config(self):
        msg = super().get_mmu_status_config()
        msg += ". Servo in %s position" % ("GRIP" if self.servo_state == FILAMENT_DRIVE_STATE else \
                "RELEASE" if self.servo_state == FILAMENT_RELEASE_STATE else "unknown")
        return msg

    def get_uncalibrated_gates(self, check_gates):
        return [lgate + self.mmu_unit.first_gate for lgate, value in enumerate(self.servo_gate_angles) if value == -1 and lgate + self.mmu_unit.first_gate in check_gates]

    # Internal Implementation --------------------------------------------------

    cmd_MMU_GRIP_help = "Grip filament in current gate"
    def cmd_MMU_GRIP(self, gcmd):
        if self.mmu.gate_selected >= 0:
            self.filament_drive()

    cmd_MMU_RELEASE_help = "Ungrip filament in current gate"
    def cmd_MMU_RELEASE(self, gcmd):
        if self.mmu.gate_selected >= 0:
            if not self.mmu_unit.filament_always_gripped:
                self.filament_release()
            else:
                self.mmu.log_error("Selector configured to not allow filament release")

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector servo angle for specifed gate(s)"
    cmd_MMU_CALIBRATE_SELECTOR_param_help = (
        "MMU_CALIBRATE_SELECTOR: %s\n" % cmd_MMU_CALIBRATE_SELECTOR_help
        + "UNIT    = #(int)\n"
        + "GATE    = #(int) Optional, default all gates on unit\n"
        + "SHOW    = [0,1]\n"
        + "ANGLE   = #(int)\n"
        + "SAVE    = [0|1]\n"
        + "SINGLE  = [0|1]\n"
        + "SPACING = #.#(float)\n"
        + "BYPASS  = [0|1]\n"
    )
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        usage = "\nUsage: MMU_CALIBRATE_SELECTOR [GATE=x] [BYPASS=0|1] [SPACING=x] [ANGLE=x] [SAVE=0|1] [SINGLE=0|1] [SHOW=0|1]"
        show = gcmd.get_int('SHOW', 0, minval=0, maxval=1)
        angle = gcmd.get_int('ANGLE', None)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        spacing = gcmd.get_float('SPACING', 25., above=0, below=180) # TiPicoMMU is 25 degrees between gates
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_unit.num_gates - 1)
        if gate == -1 and gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
            gate = TOOL_GATE_BYPASS

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_CALIBRATE_SELECTOR_param_help), color=True)
            return

        if show:
            msg = ""
            if not self.calibrator.check_calibrated(self.calibrator.CALIBRATED_SELECTOR):
                msg += "Calibration not complete\n"
            msg += "Current selector gate angle positions are: %s degrees" % self.servo_gate_angles
            if self.servo_release_angle >= 0:
                msg += "\nRelease angle is fixed at: %s degrees" % self.servo_release_angle
            else:
                msg += "\nRelease angles configured to be between each gate angle"
            if self.has_bypass():
                msg += "\nBypass angle: %s" % self.servo_bypass_angle
            else:
                msg += "\nBypass angle not configured"
            self.mmu.log_info(msg)

        elif angle is not None:
            self.mmu.log_debug("Setting selector servo to angle: %d" % angle)
            self._set_servo_angle(angle)
            self.servo_state = FILAMENT_UNKNOWN_STATE

        elif save:
            if gate == TOOL_GATE_BYPASS:
                self.servo_bypass_angle = self.servo_angle
                self.var_manager.set(VARS_MMU_SELECTOR_BYPASS_ANGLE, self.servo_bypass_angle, write=True, namespace=self.mmu_unit.name)
                self.mmu.log_info("Servo angle '%d' for bypass position has been saved" % self.servo_angle)
            elif gate >= 0:
                if single:
                    self.servo_gate_angles[gate] = self.servo_angle
                    self.var_manager.set(VARS_MMU_SELECTOR_ANGLES, self.servo_gate_angles, write=True, namespace=self.mmu_unit.name)
                    self.mmu.log_info("Servo angle '%d' for gate %d has been saved" % (self.servo_angle, gate))
                else:
                    # If possible evenly distribute based on spacing
                    angles = self._generate_gate_angles(self.servo_angle, gate, spacing)
                    if angles:
                        self.servo_gate_angles = angles
                        self.var_manager.set(VARS_MMU_SELECTOR_ANGLES, self.servo_gate_angles, write=True, namespace=self.mmu_unit.name)
                        self.mmu.log_info("Selector gate angle positions %s has been saved" % self.servo_gate_angles)
                    else:
                        self.mmu.log_error("Not possible to distribute angles with separation of %.1f degrees with gate %d at %.1f%s" % (spacing, gate, self.servo_angle, usage))
            else:
                self.mmu.log_error("No gate specified%s" % usage)
        else:
            self.mmu.log_always("Current selector servo angle: %d, Selector gate angle positions: %s" % (self.servo_angle, self.servo_gate_angles))

        if not any(x == -1 for x in self.servo_gate_angles):
            self.calibrator.mark_calibrated(self.calibrator.CALIBRATED_SELECTOR)

    def _set_servo_angle(self, angle):
        if angle >= 0 and angle != self.servo_angle:
            self.mmu.movequeues_wait()
            self.servo.set_position(angle=angle, duration=None if self.servo_always_active else self.servo_duration)
            self.servo_angle = angle
            self.mmu.movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))

    def _get_closest_released_angle(self):
        if self.servo_release_angle >= 0:
            return self.servo_release_angle
        neutral_angles = [(self.servo_gate_angles[i] + self.servo_gate_angles[i + 1]) / 2 for i in range(len(self.servo_gate_angles) - 1)]
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
        for i in range(self.mmu_unit.num_gates):
            a = start_angle + i * spacing
            if not (self.servo_min_angle <= a <= self.servo_max_angle):
                return None # Not possible
            angles.append(round(a))
        return angles
