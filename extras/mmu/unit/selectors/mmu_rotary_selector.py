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


class RotarySelector(PhysicalSelector):
    """
    Rotary selector for type-A MMUs that uses stepper-controlled rail[0] on the
    MMU toolhead (e.g. 3D Chameleon).

    `filament_always_gripped` alters operation:
      0 (default) - Lazy gate selection; occurs when asked to grip filament
      1           - Grip immediately on selection and will not release

    Implements commands:
      MMU_CALIBRATE_SELECTOR
      MMU_SOAKTEST_SELECTOR
      MMU_GRIP
      MMU_RELEASE
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        # Process config
        self.selector_move_speed = config.getfloat('selector_move_speed', 200, minval=1.)
        self.selector_homing_speed = config.getfloat('selector_homing_speed', 100, minval=1.)

        # Gate direction and "release" position if 'filament_always_gripped: 0'
        self.selector_gate_directions = list(config.getintlist('selector_cad_directions', [1, 1, 0, 0]))
        self.selector_release_gates = list(config.getintlist('selector_release_gates', [2, 3, 0, 1]))

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

        # But still allow all CAD parameters to be customized
        self.cad_gate0_pos = config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_last_gate_offset = config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)
        self.cad_selector_tolerance = config.getfloat('cad_selector_tolerance', self.cad_selector_tolerance, above=0.)

        # Register GCODE commands specific to this module
        self.register_mux_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc=self.cmd_MMU_CALIBRATE_SELECTOR_help)
        self.register_mux_command('MMU_GRIP', self.cmd_MMU_GRIP, desc=self.cmd_MMU_GRIP_help)
        self.register_mux_command('MMU_RELEASE', self.cmd_MMU_RELEASE, desc=self.cmd_MMU_RELEASE_help)

    # Selector "Interface" methods ---------------------------------------------

    def reinit(self):
        self.grip_state = FILAMENT_DRIVE_STATE

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
        self.selector_rail.homing_speed = self.selector_homing_speed
        self.selector_rail.second_homing_speed = self.selector_homing_speed / 2.
        self.selector_rail.homing_retract_speed = self.selector_homing_speed
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
            self.mmu.log_info("Homing MMU...")
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
    def select_gate(self, gate):
        super().select_gate(gate)

        if gate != self.mmu.gate_selected:
            with self.mmu.wrap_action(ACTION_SELECTING):
                if self.mmu_unit.filament_always_gripped:
                    self._grip(self.local_gate(gate))

    def restore_gate(self, gate):
        """
        Restore selector position/grip state based on last saved gate position.

        Uses VARS_MMU_SELECTOR_GATE_POS to set position from calibrated offsets,
        and infers grip vs release based on whether the restored gate matches the
        selected gate.
        """
        super().select_gate(gate)

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
            self.mmu_toolhead.get_kinematics().rails[1].set_direction(self.selector_cad_directions[lgate])
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

    def set_test_config(self, gcmd):
        self.selector_move_speed = gcmd.get_float('SELECTOR_MOVE_SPEED', self.selector_move_speed, minval=1.)
        self.selector_homing_speed = gcmd.get_float('SELECTOR_HOMING_SPEED', self.selector_homing_speed, minval=1.)

    def get_test_config(self):
        msg = "\n\nSELECTOR:"
        msg += "\nselector_move_speed = %.1f" % self.selector_move_speed
        msg += "\nselector_homing_speed = %.1f" % self.selector_homing_speed
        return msg

    def get_uncalibrated_gates(self, check_gates):
        return [lgate + self.mmu_unit.first_gate for lgate, value in enumerate(self.selector_offsets) if value == -1 and lgate + self.mmu_unit.first_gate in check_gates]

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

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector positions or postion of specified gate"
    cmd_MMU_CALIBRATE_SELECTOR_param_help = (
        "MMU_CALIBRATE_SELECTOR: %s\n" % cmd_MMU_CALIBRATE_SELECTOR_help
        + "UNIT   = #(int)\n"
        + "GATE   = #(int) Optional, default all gates on unit\n"
        + "SAVE   = [0|1]\n"
        + "SINGLE = [0|1]\n"
        + "QUICK  = [0|1]\n"
    )
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        """
        Calibrate and persist selector offsets for gates.

        Uses an endstop-based measurement when available (and QUICK=0), otherwise
        derives offsets from CAD parameters. Optionally extrapolates remaining
        gates unless SINGLE=1, and writes results to mmu_vars.cfg.
        """
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        quick = gcmd.get_int('QUICK', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', 0, minval=0, maxval=self.mmu_unit.num_gates - 1)

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_CALIBRATE_SELECTOR_param_help), color=True)
            return

        try:
            self.mmu.calibrating = True
            self.mmu.reinit()
            successful = False

            if self.has_endstop and not quick:
                successful = self._calibrate_selector(gate, extrapolate=not single, save=save)
            else:
                self.mmu.log_always("%s - will calculate gate offsets from cad_gate0_offset and cad_gate_width" % ("Quick method" if quick else "No endstop configured"))
                self.selector_offsets = [round(self.cad_gate0_pos + i * self.cad_gate_width, 1) for i in range(self.mmu_unit.num_gates)]
                self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True, namespace=self.mmu_unit.name)
                successful = True

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

        max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)
        max_movement += self.cad_last_gate_offset if gate in [TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.cad_selector_tolerance
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
                self.selector_offsets = [round(self.selector_offsets[0] + i * self.cad_gate_width, 1) for i in range(self.mmu_unit.num_gates)]
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
        self.mmu.unselect_gate()
        self.mmu.movequeues_wait()
        try:
            if self.has_endstop:
                homing_state = MmuUnit.MmuHoming(self.printer, self.mmu_toolhead)
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
        self.move("Forceably homing to hard endstop", new_pos=0, speed=self.selector_homing_speed)
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
        """
        Home the selector axis and report travel distance.

        Returns (traveled_mm, homed_ok). Travel is computed from MCU step
        position delta multiplied by step distance.
        """
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
