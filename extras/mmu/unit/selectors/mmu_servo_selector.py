# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Servo Selector
# - Servo based Selector for PicoMMU and clones
#
# Implements commands (selector dependent):
#    MMU_CALIBRATE_SERVO_SELECTOR
#    MMU_SOAKTEST_SELECTOR (PhysicalSelector)
#    MMU_GRIP              (PhysicalSelector)
#    MMU_RELEASE           (PhysicalSelector)
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, traceback
from typing                 import Sequence

# Klipper imports
from ....homing             import Homing, HomingMove

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...commands            import register_command
from ...mmu_base_parameters import TunableParametersBase, ParamSpec
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_base_selectors    import PhysicalSelector


# -----------------------------------------------------------------------------------------------------------
# Parameters for servo selector
# -----------------------------------------------------------------------------------------------------------

class ServoSelectorParameters(TunableParametersBase):

    _SPECS: Sequence[ParamSpec] = (
        ParamSpec('servo_min_angle',         'int',      0, section="SERVO", limits=dict(minval=0), hidden=True),
        ParamSpec('servo_max_angle',         'int',     90, section="SERVO", limits=dict(minval=0), hidden=True),
        ParamSpec('servo_release_angle',     'int',     -1, section="SERVO", limits=dict(minval=-1, maxval=lambda self: self.servo_max_angle)),
        ParamSpec('servo_bypass_angle',      'int',     -1, section="SERVO", limits=dict(minval=-1, maxval=lambda self: self.servo_max_angle)),
        ParamSpec('servo_gate_angles',       'intlist', [], section="SERVO",                       hidden=True),
        ParamSpec('servo_dwell',             'float',  0.6, section="SERVO", limits=dict(minval=0.1)),
        ParamSpec('servo_duration',          'float',  0.5, section="SERVO", limits=dict(minval=0.1)),
        ParamSpec('servo_always_active',     'int',      0, section="SERVO", limits=dict(minval=0, maxval=1)),
    )

    def __init__(self, config, selector):
        self._selector = selector
        super().__init__(config)


# -----------------------------------------------------------------------------------------------------------
# ServoSelector implementation
# -----------------------------------------------------------------------------------------------------------

class ServoSelector(PhysicalSelector):
    """
    Servo-based selector for type-A MMUs (e.g. PicoMMU and clones).

    Filament is gripped when a gate is selected, with a release position assumed
    between gate positions (or an explicit release angle, often 0 degrees).

    `filament_always_gripped` alters operation:
      0 (default) - Lazy selection; servo moves when asked to grip filament
      1           - Grip immediately on selection and do not release

    Implements commands:
      MMU_CALIBRATE_SERVO_SELECTOR
      MMU_SOAKTEST_SELECTOR (PhysicalSelector)
      MMU_GRIP              (PhysicalSelector)
      MMU_RELEASE           (PhysicalSelector)
    """
    PARAMS_CLS = ServoSelectorParameters

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        self.servo_bypass_angle = -1 # Required for get_status() success during init()

        self.is_homed = True # No homing necessary
        self.requires_homing = False

        # Load selector servo hardware
        servo_name = mmu_unit.config.get('selector_servo', self.mmu_unit.name)
        section = 'mmu_servo %s' % servo_name
        if config.has_section(section):
            self.servo = self.printer.load_object(config, section)
            logging.info("MMU: Loaded: [%s]" % section)
        else:
            raise config.error("Selector servo not found. Perhaps missing '[mmu_servo %s]' definition" % servo_name)

        # Initial defaults from config but will be overriden by calibrated values
        self.servo_bypass_angle  = self.p.servo_bypass_angle
        self.servo_release_angle = self.p.servo_release_angle
        self.servo_gate_angles   = self.p.servo_gate_angles

        # Start servo in safe place
        self.servo_angle = self.p.servo_min_angle + (self.p.servo_max_angle - self.p.servo_min_angle) / 2

        # Register GCODE commands specific to this module
        try:
            register_command(MmuCalibrateServoSelectorCommand)
        except KeyError:
            pass # Already registered

        self._reinit()


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()
    

    def handle_ready(self):
        """
        Load calibrated selector angles and merge with configured defaults.

        Ensures the per-gate angle list matches the unit's gate count, then
        applies any saved calibration angles and bypass angle from mmu_vars.cfg.
        Marks the selector calibrated when all gate angles are known.
        """
        logging.info("PAUL: handle_ready 1")
        super().handle_ready()
        logging.info("PAUL: handle_ready 2")

        # Load and merge calibrated selector angles (calibration set with MMU_CALIBRATE_SERVO_SELECTOR) ------------

        def ensure_list_size(lst, size, default_value=-1):
            lst = lst[:size]
            lst.extend([default_value] * (size - len(lst)))
            return lst

        self.servo_gate_angles = ensure_list_size(self.servo_gate_angles, self.mmu_unit.num_gates)

        calibrator = self.mmu_unit.calibrator
        var_manager = calibrator.var_manager

        cal_servo_gate_angles = var_manager.get(VARS_MMU_SELECTOR_ANGLES, [], namespace=self.mmu_unit.name)
        if cal_servo_gate_angles:
            self.mmu.log_debug("Loaded saved selector angles: %s" % cal_servo_gate_angles)
        else:
            self.mmu.log_always("Warning: Selector angles not found in mmu_vars.cfg. Using configured defaults")

        # Merge calibrated angles with conf angles
        for gate, angle in enumerate(zip(self.servo_gate_angles, cal_servo_gate_angles)):
            if angle[1] >= 0:
                self.servo_gate_angles[gate] = angle[1]

        servo_bypass_angle = var_manager.get(VARS_MMU_SELECTOR_BYPASS_ANGLE, -1, namespace=self.mmu_unit.name)
        if servo_bypass_angle >= 0:
            self.servo_bypass_angle = servo_bypass_angle
            self.mmu.log_debug("Loaded saved bypass angle: %s" % self.servo_bypass_angle)

        servo_release_angle = var_manager.get(VARS_MMU_SELECTOR_RELEASE_ANGLE, -1, namespace=self.mmu_unit.name)
        if servo_release_angle >= 0:
            self.servo_release_angle = servo_release_angle
            self.mmu.log_debug("Loaded saved release angle: %s" % self.servo_release_angle)

        self._check_calibrated()


    # Actual gate selection (servo movement) can be delayed until the filament_drive/release instruction
    # to prevent unecessary flutter. Conrolled by `filament_always_gripped` setting
    def _select_gate(self, lgate):
        super()._select_gate(lgate)

        with self.mmu.wrap_action(ACTION_SELECTING):
            if self.mmu_unit.filament_always_gripped:
                self._grip_release(lgate)


    def filament_drive(self):
        self.mmu.log_warning("PAUL: FILAMENT DRIVE")
        gate = self.mmu.gate_selected
        if self.mmu_unit.manages_gate(gate) and gate >= 0:
            lgate = self.mmu_unit.local_gate(gate)
            self._grip_release(lgate)


    def filament_release(self, measure=False):
        self.mmu.log_warning("PAUL: FILAMENT RELEASE")
        gate = self.mmu.gate_selected
        if self.mmu_unit.manages_gate(gate) and gate >= 0:
            lgate = self.mmu_unit.local_gate(gate)
            if not self.mmu_unit.filament_always_gripped:
                self._grip_release(lgate, release=True)
        return 0. # Fake encoder movement


    def get_filament_grip_state(self):
        return self.servo_state


    def buzz_motor(self, motor):
        if motor == "selector":
            prev_servo_angle = self.servo_angle
            low = max(min(self.servo_gate_angles), self.p.servo_min_angle)
            high = min(max(self.servo_gate_angles), self.p.servo_max_angle)
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
        return (self.servo_bypass_angle >= 0)


    def get_status(self, eventtime):
        status = super().get_status(eventtime)
        status.update({
            'grip': "Gripped" if self.servo_state == FILAMENT_DRIVE_STATE else "Released",
        })
        return status


    def get_mmu_status_config(self):
        msg = super().get_mmu_status_config()
        msg += " Servo in %s position." % ("GRIP" if self.servo_state == FILAMENT_DRIVE_STATE else \
                "RELEASE" if self.servo_state == FILAMENT_RELEASE_STATE else "unknown")
        return msg


    def get_uncalibrated_gates(self, check_gates):
        return [
            lgate + self.mmu_unit.first_gate
            for lgate, value in enumerate(self.servo_gate_angles)
            if value == -1 and lgate + self.mmu_unit.first_gate in check_gates
        ]


    # Internal Implementation --------------------------------------------------

    def _reinit(self):
        self.servo_state = FILAMENT_UNKNOWN_STATE


    # Common logic for servo manipulation
    def _grip_release(self, lgate, release=False):
        """
        Move the servo to grip or release filament for a gate.

        In bypass mode, moves to the configured bypass angle. For normal gates,
        sets the gate grip angle or a computed/explicit release angle, updating
        the cached servo state accordingly.
        """
        self.mmu.log_warning(f"PAUL: _GRIP(lgate={lgate}, release={release}")
        if lgate == TOOL_GATE_BYPASS:
            angle = self.servo_bypass_angle
            state = FILAMENT_UNKNOWN_STATE
            action = "bypass"

            if angle < 0:
                self.mmu.log_error("Operation not possible because bypass angle is not configured")
                return

        elif lgate >= 0:
            if release:
                angle = self._get_best_release_angle()
                state = FILAMENT_RELEASE_STATE
                action = "filament released"

                if angle < 0:
                    self.mmu.log_error("Operation not possible because neighboring selector gate angle is not calibrated")
                    return
            else:
                angle = self.servo_gate_angles[lgate]
                state = FILAMENT_DRIVE_STATE
                action = "filament grip"

                if angle < 0:
                    self.mmu.log_error("Operation not possible because selector gate angle is not calibrated")
                    return

        else:
            self.servo_state = FILAMENT_UNKNOWN_STATE
            return

        self.mmu.log_trace("Setting servo to %s position at angle: %.1f" % (action, angle))
        self._set_servo_angle(angle)
        self.servo_state = state


    def _set_servo_angle(self, angle):
        """
        Move the selector servo to a new angle and wait for motion to settle.

        Honors `servo_always_active` by controlling whether the move uses a
        duration, then dwells for at least the configured dwell/duration.
        """
        if angle >= 0 and angle != self.servo_angle:
            self.mmu.log_warning(f"PAUL: _set_servo_angle({angle})")
            self.mmu.movequeue_wait()
            self.servo.set_position(angle=angle, duration=None if self.p.servo_always_active else self.p.servo_duration)
            self.servo_angle = angle
            self.mmu.movequeue_dwell(max(self.p.servo_dwell, self.p.servo_duration, 0))


    def _get_best_release_angle(self):
        """
        Determine a release angle near the current servo position.

        If a fixed release angle is configured, returns it. Otherwise computes
        midpoints between neighboring gate angles and picks the closest.

        Returns -1 if a needed neighboring gate angle is uncalibrated.
        """
        if self.servo_release_angle >= 0:
            return self.servo_release_angle

        if len(self.servo_gate_angles) < 2:
            return -1

        neutral_angles = []
        for i in range(len(self.servo_gate_angles) - 1):
            a1 = self.servo_gate_angles[i]
            a2 = self.servo_gate_angles[i + 1]

            if a1 == -1 or a2 == -1:
                return -1

            neutral_angles.append((a1 + a2) / 2)

        closest_angle = min(
            neutral_angles,
            key=lambda angle: abs(angle - self.servo_angle)
        )

        return int(round(closest_angle))


    def _generate_gate_angles(self, known_angle, known_gate, spacing):
        """
        Generate evenly spaced gate angles anchored at a known gate.

        Returns a full per-gate angle list, or an error string if any computed
        angle would fall outside the configured servo min/max limits.
        """

        num_gates = self.mmu_unit.num_gates
        if not (0 <= known_gate < num_gates):
            return f"Invalid gate index: {known_gate}"

        # Special handling for 2-gate systems
        if num_gates == 2:
            if known_gate == 0:
                angles = [known_angle, known_angle + spacing]
            elif known_gate == 1:
                angles = [known_angle - spacing, known_angle]
            else:
                return f"Invalid gate index: {known_gate}"
        else:
            start_angle = known_angle - known_gate * spacing
            angles = [start_angle + i * spacing for i in range(num_gates)]

        for lgate, a in enumerate(angles):
            if not (self.p.servo_min_angle <= a <= self.p.servo_max_angle):
                return (
                    f"Computed angle {a} for lgate {lgate} falls outside of servo min/max range "
                    f"({self.p.servo_min_angle}{UI_DEGREE}-"
                    f"{self.p.servo_max_angle}{UI_DEGREE})"
                )

        return [int(round(a)) for a in angles]


    def _check_calibrated(self):
        if not any(x == -1 for x in self.servo_gate_angles):
            self.mmu_unit.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        else:
            self.mmu_unit.calibrator.mark_not_calibrated(CALIBRATED_SELECTOR)


# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_SERVO_SELECTOR command
#  This "registered command" will be conditionally registered, then instantiated later by the main
#  mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

from ...commands.mmu_base_command import *

class MmuCalibrateServoSelectorCommand(BaseCommand):

    CMD = "MMU_CALIBRATE_SERVO_SELECTOR"

    HELP_BRIEF = "Calibration of the selector servo angle for specifed gate(s)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT    = #(int) Optional if only one unit fitted to printer\n"
        + "ANGLE   = #(int) Move servo to designated angle\n"
        + "GATE    = #(int) Specify the gate by it's global logical index\n"
        + "LGATE   = #(int) Speficy gate by the local mmu unit index (same as GATE with single MMU unit)\n"
        + "SAVE    = 1      To persist the calibration results else they will just be reported\n"
        + "SINGLE  = 1      To force the calibration of a single gate only\n"
        + "SPACING = #(int) Angle between gates for quick setting all gates\n"
        + "BYPASS  = 1      To specify intention to define the bypass gate angle (if fitted)\n"
        + "RELEASE = 1      To specify intention to define a fixed release angle\n"
        + "RESET   = 1      To remove calibrated settings and default to configured starting values\n"
        + "(no options to show the current calibration)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                           ...Report on current calibration\n"
        + f"{CMD} ANGLE=83                  ...Set servo to angle of 83{UI_DEGREE}\n"
        + f"{CMD} GATE=5 SINGLE=1           ...Save current servo angle as position for gate 2\n"
        + f"{CMD} LGATE=0 SPACING=25 SAVE=0 ...Use current angle for local gate 0, space othes at 25{UI_DEGREE} intervals. Report but don't save results\n"
        + f"{CMD} RELEASE=1                 ...Save the current angle for a fixed release position\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        """
        Calibrate and persist selector servo angles for gates and bypass.
        Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        """
        mmu = mmu_unit.mmu
        selector = mmu_unit.selector
        calibrator = mmu_unit.calibrator
        var_manager = calibrator.var_manager

        if self.check_if_disabled(): return
        if not isinstance(selector, ServoSelector):
            self.mmu.log_error("Operation not possible on this selector type (ServoSelector only)")
            return

        angle = gcmd.get_int('ANGLE', None, minval=selector.p.servo_min_angle, maxval=selector.p.servo_max_angle)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        spacing = gcmd.get_float('SPACING', 25., above=0, below=180) # TiPicoMMU is 25 degrees between gates
        gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
        lgate = gcmd.get_int('LGATE', None, minval=0, maxval=mmu_unit.num_gates - 1)
        bypass = bool(gcmd.get_int('BYPASS', 0.,  minval=0, maxval=1))
        release = bool(gcmd.get_int('RELEASE', 0.,  minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0.,  minval=0, maxval=1))

        def show():
            lines = []
            lines.append("Servo selector summary")
            if not mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
                lines.append("Calibration not completed. Using configuration values")

            lines.append(f"Current selector gate angle positions are: {selector.servo_gate_angles} degrees")

            if selector.servo_release_angle >= 0:
                lines.append(f"Release angle is fixed at: {selector.servo_release_angle}{UI_DEGREES}")
            else:
                lines.append("Release angles configured to be between each gate angle")

            if selector.has_bypass():
                lines.append(f"Bypass angle: {selector.servo_bypass_angle}{UI_DEGREES}")
            else:
                lines.append("Bypass angle not configured")

            lines.append(
                f"Current selector servo angle is {selector.servo_angle}{UI_DEGREE}, "
                f"Allowed range: {selector.p.servo_min_angle}{UI_DEGREE}-{selector.p.servo_max_angle}{UI_DEGREE}"
            )

            mmu.log_info("\n".join(lines))

        if reset:
            selector.servo_bypass_angle = selector.p.servo_bypass_angle
            selector.servo_release_angle = selector.p.servo_release_angle
            selector.servo_gate_angles = selector.p.servo_gate_angles

            var_manager.delete(VARS_MMU_SELECTOR_RELEASE_ANGLE, namespace=mmu_unit.name)
            var_manager.delete(VARS_MMU_SELECTOR_BYPASS_ANGLE, namespace=mmu_unit.name)
            var_manager.delete(VARS_MMU_SELECTOR_ANGLES, namespace=mmu_unit.name, write=True)

            mmu.log_always(f"Reset servo selector calibration on {mmu_unit.name}")
            show()
            return

        if angle is not None:
            mmu.log_debug("Setting selector servo to angle: %d" % angle)
            selector._set_servo_angle(angle)
            selector.servo_state = FILAMENT_UNKNOWN_STATE
            return

        # Gate can be logic or local
        if gate is not None and not mmu_unit.manages_gate(gate):
            min_gate, max_gate = mmu_unit.gate_bounds()
            raise gcmd.error("Gate %d is not managed by %s (range=%d-%d)" % (gate, mmu_unit.name, min_gate, max_gate))
        lgate = lgate if lgate is not None else mmu_unit.local_gate(gate) if gate is not None else None
        have_gate = lgate is not None

        terms = sum((have_gate, bypass, release)) # Mutual exclusive test
        if terms == 0:
            show()
            return

        if terms != 1:
            raise gcmd.error("Must specify one of GATE=, LGATE=, BYPASS=1, or RELEASE=1")

        if release:
            msg = f"Servo angle for release position is {selector.servo_angle}{UI_DEGREE}"
            if save:
                selector.servo_release_angle = selector.servo_angle
                var_manager.set(VARS_MMU_SELECTOR_RELEASE_ANGLE, selector.servo_release_angle, write=True, namespace=mmu_unit.name)

        elif bypass:
            msg = f"Servo angle for bypass position is {selector.servo_angle}{UI_DEGREE}"
            if save:
                selector.servo_bypass_angle = selector.servo_angle
                var_manager.set(VARS_MMU_SELECTOR_BYPASS_ANGLE, selector.servo_bypass_angle, write=True, namespace=mmu_unit.name)

        else:
            if single:
                msg = f"Servo angle for local gate {lgate} is {selector.servo_angle}{UI_DEGREE}"
                if save:
                    selector.servo_gate_angles[lgate] = selector.servo_angle
                    var_manager.set(VARS_MMU_SELECTOR_ANGLES, selector.servo_gate_angles, write=True, namespace=mmu_unit.name)

            else:
                # If possible evenly distribute based on spacing
                angles = selector._generate_gate_angles(selector.servo_angle, lgate, spacing)
                if isinstance(angles, str):
                    raise gcmd.error(angles)

                msg = f"Calculated gate angle positions are {angles}"
                if save:
                    selector.servo_gate_angles = angles
                    var_manager.set(VARS_MMU_SELECTOR_ANGLES, selector.servo_gate_angles, write=True, namespace=mmu_unit.name)

        if save:
            msg += " and has been saved"
        else:
            msg += " (not saved)"

        mmu.log_always(msg)
        selector._check_calibrated()
