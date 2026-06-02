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
import logging
from typing                 import Sequence

# Klipper imports

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
    Rotary selector for type-A MMUs that uses stepper-controlled selection

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

        self.selector_stepper_name = mmu_unit.config.get('selector_stepper') # Name of selector stepper
        stepper_section = f"mmu_stepper {self.selector_stepper_name}"

        # Force stepper loading now (TMC first)
        tmc_found = False
        for chip in TMC_CHIPS: 
            tmc_section = f"{chip} {stepper_section}"
            if config.has_section(tmc_section):
                _ = self.printer.load_object(config, tmc_section)
                logging.info("MMU: Loaded: [%s]" % tmc_section)
                tmc_found = True
                break
        if not tmc_found:
            raise config.error("Selector stepper TMC configuration not found for %s on mmu_unit %s" % (self.selector_stepper_name, self.name))

        # Inject sensible config if not supplied by user
        key = "homing_speed"
        if not config.fileconfig.has_option(stepper_section, key):
            config.fileconfig.set(stepper_section, key, self.p.selector_homing_speed)

        key = "second_homing_speed"
        if not config.fileconfig.has_option(stepper_section, key):
            config.fileconfig.set(stepper_section, "second_homing_speed", self.p.selector_homing_speed / 2.)

        # Force correct max movement based on cad dimensions
        key = "homing_move_dist"
        config.fileconfig.set(stepper_section, key, self._get_max_selector_movement())

        # Now we can load the mmu_stepper object
        self.selector_stepper = self.printer.load_object(config, stepper_section)
        logging.info("MMU: Loaded: [%s]" % stepper_section)

        # Have an endstop (most likely stallguard)?
        self.has_endstop = bool(self.selector_stepper.rail.get_endstops())

        # Register GCODE commands specific to this module
        try:
            register_command(MmuCalibrateRotarySelectorCommand)
        except KeyError:
            pass # Already registered

        self.grip_state = FILAMENT_DRIVE_STATE


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()


    def handle_ready(self):
        """
        Loads per-gate selector offsets and bypass offset from mmu_vars.cfg,
        ensures list sizing matches num_gates, and sets calibrated status when
        all offsets are known.
        """
        super().handle_ready()

        # Load selector offsets (calibration set with MMU_CALIBRATE_SELECTOR) -------------------------------

        def ensure_list_size(lst, size, default_value=-1):
            lst = lst[:size]
            lst.extend([default_value] * (size - len(lst)))
            return lst

        self.var_manager.upgrade(VARS_MMU_SELECTOR_OFFSETS, self.mmu_unit.name) # v3 upgrade
        self.selector_offsets = self.var_manager.get(VARS_MMU_SELECTOR_OFFSETS, None, namespace=self.mmu_unit.name)
        if self.selector_offsets:
            # Ensure list size
            if len(self.selector_offsets) == self.mmu_unit.num_gates:
                self.mmu.log_debug("Loaded saved selector offsets: %s" % self.selector_offsets)
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % VARS_MMU_SELECTOR_OFFSETS)
                self.selector_offsets = ensure_list_size(self.selector_offsets, self.mmu_unit.num_gates)

            if not any(x == -1 for x in self.selector_offsets):
                self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        else:
            self.mmu.log_always("Warning: Selector offsets not found in mmu_vars.cfg. Probably not calibrated")
            self.selector_offsets = [-1] * self.mmu_unit.num_gates
        self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, namespace=self.mmu_unit.name)

        # Finally restore the last known local gate position to avoid need to re-home
        last_pos = self.var_manager.get(VARS_MMU_SELECTOR_LAST_POS, None, namespace=self.mmu_unit.name)
        if last_pos is not None:
            self._restore_position(last_pos)
            self.is_homed = True


    # Actual gate selection can be delayed (if not forcing grip) until the
    # filament_drive/release to reduce selector movement
    def _select_gate(self, lgate):
        super()._select_gate(lgate)

        with self.mmu.wrap_action(ACTION_SELECTING):
            if self.mmu_unit.filament_always_gripped:
                self._grip(lgate)


    def filament_drive(self):
        self._grip(self.local_gate(self.mmu.gate_selected))


    def filament_release(self, measure=False):
        if not self.mmu_unit.filament_always_gripped:
            self._grip(self.local_gate(self.mmu.gate_selected), release=True)
        return 0. # Fake encoder movement


    # --------------------------------------------------------------------------

    # Note there is no separation of gate selection and grip/release with this type of selector
    def _grip(self, lgate, release=False):
        """
        Move to the grip or release position for a local gate.

        Persists VARS_MMU_SELECTOR_LAST_POS so the selector can restore an
        accurate gate/release position after a restart. Also sets filament drive
        direction based on configured gate directions.
        """
        if lgate >= 0:
            if release:
                release_pos = self.selector_offsets[self.selector_release_gates[lgate]]
                self.mmu.log_trace("Setting selector to filament release position at position: %.1f" % release_pos)
                self._position(release_pos)
                self.grip_state = FILAMENT_RELEASE_STATE

            else:
                grip_pos = self.selector_offsets[lgate]
                self.mmu.log_trace("Setting selector to filament grip position at position: %.1f" % grip_pos)
                self._position(grip_pos)
                self.grip_state = FILAMENT_DRIVE_STATE

            # Ensure gate filament drive is in the correct direction
            self.mmu_unit.drive_obj(lgate).set_gear_direction(self.p.selector_gate_directions[lgate])
            self.mmu.movequeue_wait()
        else:
            self.grip_state = FILAMENT_UNKNOWN_STATE


    def get_filament_grip_state(self):
        return self.grip_state


    def enable_motors(self):
        self.selector_stepper.do_enable(True)


    def disable_motors(self):
        self.selector_stepper.do_enable(False)

        # Assume that if disabling motor then the position will be modified
        self.is_homed = False
        self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, None, namespace=self.mmu_unit.name)


    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.selector_stepper.commanded_pos
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
        msg = super().get_mmu_status_config()
        msg += "Filament is %s." % ("GRIPPED" if self.grip_state == FILAMENT_DRIVE_STATE else "RELEASED")
        return msg


    def get_uncalibrated_gates(self, check_gates):
        return [
            lgate + self.mmu_unit.first_gate
            for lgate, value in enumerate(self.selector_offsets)
            if value == -1 and lgate + self.mmu_unit.first_gate in check_gates
        ]


    # Internal Implementation --------------------------------------------------

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu_unit.num_gates - 1

        max_movement = self.p.cad_gate0_pos + (n * self.p.cad_gate_width)
        max_movement += self.p.cad_last_gate_offset if gate in [TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.p.cad_selector_tolerance
        return max_movement


    def _get_max_selector_movement(self):
        max_movement = self.mmu.num_gates * self.p.cad_gate_width * self.p.cad_max_rotations
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
        """
        self.mmu.movequeue_wait()
        self.filament_hold_move()

        try:
            if self.has_endstop:
                self.selector_stepper.do_home_rail()
            else:
                self._home_hard_endstop()

            self.is_homed = True
            self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, 0, namespace=self.mmu_unit.name)

        except Exception as e:
            self.is_homed = False
            self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, None, namespace=self.mmu_unit.name)
            raise MmuError(f"Homing selector failed because of blockage or malfunction. Klipper reports: {e}") from e


    def _home_hard_endstop(self):
        self.mmu.log_always("Forcing selector homing to hard endstop. Excuse the noise!\n(Configure stallguard endstop on selector stepper to avoid)")
        self._restore_position(self._get_max_selector_movement()) # Worst case position to allow full movement
        self.move("Forceably homing to hard endstop", new_pos=0, speed=self.p.selector_homing_speed)
        self._restore_position(0) # Reset pos


    def _position(self, target):
        self.move("Positioning selector", target)
        self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, target, write=True, namespace=self.mmu_unit.name)


    def move(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        return self._move_selector(trace_str, new_pos, speed=speed, accel=accel, wait=wait)


    # Internal raw wrapper around all selector moves except rail homing
    # Returns position after move
    def _move_selector(self, trace_str, new_pos, speed=None, accel=None, wait=False):
        """
        Execute a selector move with consistent tracing and motion settings.

        Applies default selector speed/accel when not supplied, performs the
        toolhead move, optionally waits for queues, and returns the resulting
        selector position.
        """
        if trace_str:
            self.mmu.log_trace(trace_str)

        self.mmu.movequeue_wait()

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.p.selector_move_speed
        accel = accel or self.p.selector_accel

        pos = self.selector_stepper.commanded_pos
        self.selector_stepper.do_move(new_pos, speed, accel)
        self.mmu.log_stepper(
            f"SELECTOR MOVE: requested position={new_pos:.1f}, "
            f"speed={speed:.1f}, accel={accel:.1f}"
        )

        if wait:
            self.mmu.movequeue_wait()

        return new_pos


    def _restore_position(self, position):
        self.selector_stepper.do_set_position(position)
        self.enable_motors()


    def measure_to_home(self):
        """
        Home the selector axis and report travel distance.

        Returns (traveled_mm, homed_ok). Travel is computed from MCU step
        position delta multiplied by step distance.
        """
        self.mmu.movequeue_wait()

        traveled = 0.0
        homed = False

        try:
            traveled = self.selector_stepper.do_home_rail()
            homed = True

        except self.printer.command_error:
            pass # Expected: endstop not triggered

        return abs(traveled), homed


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
        + "SAVE   = [0|1]  Whether to persist the calibration results\n"
        + "SINGLE = [0|1]  Set to force the calibration of a single position only\n"
        + "QUICK  = [0|1]  Calibrate all offsets based on CAD geometry (good for initial setup)\n"
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
        mmu = mmu_unit.mmu
        selector = mmu_unit.selector

        if self.check_if_disabled(): return
        if not isinstance(selector, RotarySelector):
            self.mmu.log_error("Operation not possible on this selector type (RotarySelector only)")
            return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        quick = gcmd.get_int('QUICK', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', 0, minval=0, maxval=mmu_unit.num_gates - 1)

        try:
            mmu.calibrating = True
            successful = False

            if selector.has_endstop and not quick:
                successful = selector._calibrate_selector(gate, extrapolate=not single, save=save)
            else:
                mmu.log_always("%s - will calculate gate offsets from cad_gate0_offset and cad_gate_width" % ("Quick method" if quick else "No endstop configured"))
                selector.selector_offsets = [round(selector.p.cad_gate0_pos + i * selector.p.cad_gate_width, 1) for i in range(mmu_unit.num_gates)]
                mmu_unit.calibrator.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, selector.selector_offsets, write=True, namespace=mmu_unit.name)
                successful = True

            if not any(x == -1 for x in selector.selector_offsets):
                mmu_unit.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

            # If not fully calibrated turn off the selector stepper to ease next step, else activate by homing
            if successful and mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
                mmu.log_always("Selector calibration complete")
                selector._select_gate(0)
            else:
                selector.disable_motors()

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False
