# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Rotary Selector
# - Rotary Selector for 3D Chamelon using stepper selection
#   without servo
#
# Implements commands:
#    MMU_CALIBRATE_ROTARY_SELECTOR
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
from ....homing             import HomingMove

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...commands            import register_command
from ...mmu_base_parameters import TunableParametersBase, ParamSpec
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_base_selectors    import PhysicalSelector


# -----------------------------------------------------------------------------------------------------------
# Parameters for rotary selector
# -----------------------------------------------------------------------------------------------------------

class RotarySelectorParameters(TunableParametersBase):

    _SPECS: Sequence[ParamSpec] = (
        ParamSpec('selector_move_speed',     'float',  200.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_homing_speed',   'float',  100.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_accel',          'float', 1200.0, section="SELECTOR", limits=dict(above=1.0)),

        # Gate direction and "release" position if 'filament_always_gripped: 0'
        ParamSpec('selector_gate_directions','intlist', [1, 1, 0, 0], section="SELECTOR", hidden=True),
        ParamSpec('selector_release_gates',  'intlist', [2, 3, 0, 1], section="SELECTOR", hidden=True),

        ParamSpec('cad_gate0_pos',           'float',   4.0,  section="CAD", limits=dict(minval=0.0), hidden=True),
        ParamSpec('cad_gate_width',          'float',   25.0, section="CAD", limits=dict(above=0.0),  hidden=True),
        ParamSpec('cad_bypass_offset',       'float',   2.0,  section="CAD", limits=dict(minval=0.0), hidden=True),
        ParamSpec('cad_selector_tolerance',  'float',   15.0, section="CAD", limits=dict(minval=0.0), hidden=True),
    )

    def __init__(self, config, selector):
        self._selector = selector
        super().__init__(config)


# -----------------------------------------------------------------------------------------------------------
# RotarySelector implementation
# -----------------------------------------------------------------------------------------------------------

class RotarySelector(PhysicalSelector):
    """
    Rotary selector for type-A MMUs that uses stepper-controlled rail[0] on the
    MMU toolhead (e.g. 3D Chameleon).

    `filament_always_gripped` alters operation:
      0 (default) - Lazy gate selection; occurs when asked to grip filament
      1           - Grip immediately on selection and will not release

    Implements commands:
      MMU_CALIBRATE_ROTARY_SELECTOR
      MMU_SOAKTEST_SELECTOR (PyhsicalSelector)
      MMU_GRIP (PyhsicalSelector)
      MMU_RELEASE (PyhsicalSelector)
    """
    PARAMS_CLS = RotarySelectorParameters

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        # Register GCODE commands specific to this module
        try:
            register_command(MmuCalibrateRotarySelectorCommand)
        except KeyError:
            pass # Already registered

        self._reinit() # PAUL do we need a separate method?


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        """
        Bind selector rail/stepper, configure rail limits, and load calibration.

        Determines whether an actual endstop is present, loads per-gate selector
        offsets from mmu_vars.cfg, and marks the selector calibrated when all
        offsets are known.
        """
        super().handle_connect()

        self.selector_rail = self.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]

        # Adjust selector rail limits now we know the config
        self.selector_rail.position_min = -1
        self.selector_rail.position_max = self._get_max_selector_movement()
        self.selector_rail.homing_speed = self.p.selector_homing_speed
        self.selector_rail.second_homing_speed = self.p.selector_homing_speed / 2.
        self.selector_rail.homing_retract_speed = self.p.selector_homing_speed
        self.selector_rail.homing_positive_dir = False

        # Have an endstop (most likely stallguard)?
        endstops = self.selector_rail.get_endstops()
        self.has_endstop = bool(endstops) and endstops[0][0].__class__.__name__ != "MockEndstop"

        # Load selector offsets (calibration set with MMU_CALIBRATE_SELECTOR) -------------------------------
        self.var_manager.upgrade(VARS_MMU_SELECTOR_OFFSETS, self.mmu_unit.name) # v3 upgrade
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

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    def home(self, force_unload = None):
        """
        Home the selector, optionally unloading filament first.

        If bypass is active, homing is skipped. When requested (or required by
        filament state), triggers an unload sequence before performing selector
        homing via endstop or hard-endstop fallback.
        """
        if self.mmu.check_if_bypass(): return
        with self.mmu.wrap_action(ACTION_HOMING):
            self.mmu.log_info("Homing MMU %s..." % self.mmu_unit.name)
            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))
            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)
            elif force_unload is False and self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self.mmu.unload_sequence()
            self._home_selector()

    # Actual gate selection can be delayed (if not forcing grip) until the
    # filament_drive/release to reduce selector movement
    def _select_gate(self, lgate):
        super()._select_gate(lgate)

        if gate != self.mmu.gate_selected:
            with self.mmu.wrap_action(ACTION_SELECTING):
                if self.mmu_unit.filament_always_gripped:
                    self._grip(self.local_gate(gate))

    def _restore_gate(self, lgate):
        """
        Restore selector position/grip state based on last saved gate position.

        Uses VARS_MMU_SELECTOR_GATE_POS to set position from calibrated offsets,
        and infers grip vs release based on whether the restored gate matches the
        selected gate.
        """
        super()._restore_gate(lgate)

        gate_pos = self.var_manager.get(VARS_MMU_SELECTOR_GATE_POS, None, namespace=self.mmu_unit.name)
        if gate_pos is not None:
            self.set_position(self.selector_offsets[gate_pos])
            if self.local_gate(gate) == gate_pos:
                self.grip_state = FILAMENT_DRIVE_STATE
            else:
                self.grip_state = FILAMENT_RELEASE_STATE
        else:
            self.grip_state = FILAMENT_UNKNOWN_STATE

    def filament_drive(self):
        self._grip(self.local_gate(self.mmu.gate_selected))

    def filament_release(self, measure=False):
        if not self.mmu_unit.filament_always_gripped:
            self._grip(self.local_gate(self.mmu.gate_selected), release=True)
        return 0. # Fake encoder movement

    # --------------------------------------------------------------------------

    def _reinit(self):
        self.grip_state = FILAMENT_DRIVE_STATE

    # Note there is no separation of gate selection and grip/release with this type of selector
    def _grip(self, gate, release=False):
        """
        Move to the grip or release position for a local gate.

        Persists VARS_MMU_SELECTOR_GATE_POS so the selector can restore an
        accurate gate/release position after a restart. Also sets filament drive
        direction based on configured gate directions.
        """
        lgate = self.local_gate(gate)
        if lgate >= 0:
            if release:
                release_pos = self.selector_offsets[self.selector_release_gates[lgate]]
                self.mmu.log_trace("Setting selector to filament release position at position: %.1f" % release_pos)
                self._position(release_pos)
                self.grip_state = FILAMENT_RELEASE_STATE

                # Precaution to ensure correct postion/gate restoration on restart
                self.var_manager.set(VARS_MMU_SELECTOR_GATE_POS, self.selector_release_gates[lgate], write=True, namespace=self.mmu_unit.name)
            else:
                grip_pos = self.selector_offsets[lgate]
                self.mmu.log_trace("Setting selector to filament grip position at position: %.1f" % grip_pos)
                self._position(grip_pos)
                self.grip_state = FILAMENT_DRIVE_STATE

                # Precaution to ensure correct postion/gate restoration on restart
                self.var_manager.set(VARS_MMU_SELECTOR_GATE_POS, lgate, write=True, namespace=self.mmu_unit.name)

            # Ensure gate filament drive is in the correct direction
            self.mmu_toolhead.get_kinematics().rails[1].set_direction(self.p.selector_gate_directions[lgate])
            self.mmu.movequeues_wait()
        else:
            self.grip_state = FILAMENT_UNKNOWN_STATE

    def get_filament_grip_state(self):
        return self.grip_state

    def disable_motors(self):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_toolhead.get_last_move_time())
        self.is_homed = False

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
        else:
            return False
        return True

    def get_status(self, eventtime):
        status = super().get_status(eventtime)
        status.update({
            'grip': "Gripped" if self.grip_state == FILAMENT_DRIVE_STATE else "Released",
        })
        return status

    def get_mmu_status_config(self):
        msg = "\nSelector is NOT HOMED. " if not self.is_homed else ""
        msg += "Filament is %s" % ("GRIPPED" if self.grip_state == FILAMENT_DRIVE_STATE else "RELEASED")
        return msg

    def get_uncalibrated_gates(self, check_gates):
        return [lgate + self.mmu_unit.first_gate for lgate, value in enumerate(self.selector_offsets) if value == -1 and lgate + self.mmu_unit.first_gate in check_gates]


    # Internal Implementation --------------------------------------------------

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu_unit.num_gates - 1

        max_movement = self.p.cad_gate0_pos + (n * self.p.cad_gate_width)
        max_movement += self.p.cad_last_gate_offset if gate in [TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.p.cad_selector_tolerance
        return max_movement

    # Manual selector offset calibration
    def _calibrate_selector(self, gate, extrapolate=True, save=True):
        """
        Measure selector travel to home to establish a gate offset.

        Validates the measured travel against CAD-derived maximums and, when
        saving, either extrapolates offsets across all gates or writes only the
        requested gate depending on extrapolate/SINGLE.
        """
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
            if extrapolate and gate == self.mmu_unit.num_gates - 1 and self.selector_offsets[0] > 0:
                # Distribute selector spacing based on measurements of first and last gate
                spacing = (self.selector_offsets[-1] - self.selector_offsets[0]) / (self.mmu_unit.num_gates - 1)
                self.selector_offsets = [round(self.selector_offsets[0] + i * spacing, 1) for i in range(self.mmu_unit.num_gates)]
            elif extrapolate:
                # Distribute using cad spacing
                self.selector_offsets = [round(self.selector_offsets[0] + i * self.p.cad_gate_width, 1) for i in range(self.mmu_unit.num_gates)]
            else:
                extrapolate = False
            self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True, namespace=self.mmu_unit.name)

            if extrapolate:
                self.mmu.log_always("All selector offsets have been extrapolated and saved:\n%s" % self.selector_offsets)
            else:
                self.mmu.log_always("Selector offset (%.1fmm) for gate %d has been saved" % (traveled, gate))
                if gate == 0:
                    self.mmu.log_always("Run MMU_CALIBRATE_SELECTOR again with GATE=%d to extrapolate all gate positions. Use SINGLE=1 to force calibration of only one gate" % (self.mmu_unit.num_gates - 1))
        return True

    def _home_selector(self):
        """
        Home the selector rail using the configured endstop or a hard endstop.

        Uses Klipper kinematics homing when an endstop is present, otherwise
        forces a hard-endstop home. Raises MmuError with Klipper context on
        failure (blockage/malfunction).
        """
        from ...mmu_unit import MmuHoming

        self.mmu.unselect_gate()
        self.mmu.movequeues_wait()
        try:
            if self.has_endstop:
                homing_state = MmuHoming(self.printer, self.mmu_toolhead)
                homing_state.set_axes([0])
                self.mmu_toolhead.get_kinematics().home(homing_state)
            else:
                self._home_hard_endstop()
            self.is_homed = True
        except Exception as e: # Homing failed
            logging.error(traceback.format_exc())
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def _home_hard_endstop(self):
        self.mmu.log_always("Forcing selector homing to hard endstop. Excuse the noise!\n(Configure stallguard endstop on selector stepper to avoid)")
        self.set_position(self._get_max_selector_movement()) # Worst case position to allow full movement
        self.move("Forceably homing to hard endstop", new_pos=0, speed=self.p.selector_homing_speed)
        self.set_position(0) # Reset pos

    def _position(self, target):
        self.move("Positioning selector", target)

    def move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        return self._trace_selector_move(trace_str, new_pos, speed=speed, accel=accel, wait=wait)

    # Internal raw wrapper around all selector moves except rail homing
    # Returns position after move, if homed (homing moves)
    def _trace_selector_move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        """
        Execute a selector move with consistent tracing and motion settings.

        Applies default selector speed/accel when not supplied, performs the
        toolhead move, optionally waits for queues, and returns the resulting
        selector position.
        """
        if trace_str:
            self.mmu.log_trace(trace_str)

        self.mmu_toolhead.quiesce()

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.p.selector_move_speed
        accel = accel or self.p.selector_accel

        pos = self.mmu_toolhead.get_position()
        with self.mmu.wrap_accel(accel):
            pos[0] = new_pos
            self.mmu_toolhead.move(pos, speed)
        if self.mmu.log_enabled(LOG_STEPPER):
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
        """
        Home the selector axis and report travel distance.

        Returns (traveled_mm, homed_ok). Travel is computed from MCU step
        position delta multiplied by step distance.
        """
        from ...mmu_unit import MmuHoming

        self.mmu.movequeues_wait()
        init_mcu_pos = self.selector_stepper.get_mcu_position()
        homed = False
        try:
            homing_state = MmuHoming(self.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_toolhead.get_kinematics().home(homing_state)
            homed = True
        except Exception:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * self.selector_stepper.get_step_dist()
        return traveled, homed



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_ROTARY_SELECTOR command
#  This "registered command" will be conditionally registered, then instantiated later by the main
#  mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

from ...commands.mmu_base_command import *

class MmuCalibrateRotarySelectorCommand(BaseCommand):

    CMD = "MMU_CALIBRATE_ROTARY_SELECTOR"

    HELP_BRIEF = "Calibration of the selector positions or postion of specified gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT   = #(int) Optional if only one unit fitted to printer\n"
        + "GATE   = #(int) Optional, default all gates on unit\n"
        + "SAVE   = [0|1]\n"
        + "SINGLE = [0|1]\n"
        + "QUICK  = [0|1]\n"
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
            category=CATEGORY_TESTING,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        """
        Calibrate and persist selector offsets for gates.

        Uses an endstop-based measurement when available (and QUICK=0), otherwise
        derives offsets from CAD parameters. Optionally extrapolates remaining
        gates unless SINGLE=1, and writes results to mmu_vars.cfg.
        """

        if self.mmu.check_if_disabled(): return

        sel = mmu_unit.selector

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        quick = gcmd.get_int('QUICK', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', 0, minval=0, maxval=self.mmu_unit.num_gates - 1) # PAUL need gate and lgate

        try:
            self.mmu.calibrating = True
#            self.mmu.reinit() # PAUL why?
            successful = False

            if sel.has_endstop and not quick:
                successful = sel._calibrate_selector(gate, extrapolate=not single, save=save)
            else:
                self.mmu.log_always("%s - will calculate gate offsets from cad_gate0_offset and cad_gate_width" % ("Quick method" if quick else "No endstop configured"))
                sel.selector_offsets = [round(sel.p.cad_gate0_pos + i * sel.p.cad_gate_width, 1) for i in range(mmu_unit.num_gates)]
                mmu_unit.calibrator.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, sel.selector_offsets, write=True, namespace=mmu_unit.name)
                successful = True

            if not any(x == -1 for x in sel.selector_offsets):
                mmu_unit.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

            # If not fully calibrated turn off the selector stepper to ease next step, else activate by homing
            if successful and mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
                self.mmu.log_always("Selector calibration complete")
                sel._select_gate(0)
            else:
                sel.disable_motors()

        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))
        finally:
            self.mmu.calibrating = False
