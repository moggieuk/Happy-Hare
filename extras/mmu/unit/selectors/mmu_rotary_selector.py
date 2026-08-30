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

    def _validate_int_list(self, value, *, minval=None, maxval=None):
        expected = self._selector.mmu_unit.num_gates
        if len(value) != expected:
            raise ValueError(f"Expected {expected} gate values, got {len(value)}")

        for i, v in enumerate(value):
            if minval is not None and v < minval:
                raise ValueError(f"Gate {i}: value {v} is less than minimum {minval}")
            if maxval is not None and v > maxval:
                raise ValueError(f"Gate {i}: value {v} is greater than maximum {maxval}")


    _SPECS: Sequence[ParamSpec] = (
        ParamSpec('selector_move_speed',     'float',  200.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_homing_speed',   'float',  100.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_accel',          'float', 1200.0, section="SELECTOR", limits=dict(above=1.0)),

        # Gate direction and "release" position if 'filament_always_gripped: 0'
        ParamSpec('selector_gate_directions','intlist', [1, 1, 0, 0], section="SELECTOR", hidden=True,
            validator=lambda self, v: self._validate_int_list(v, minval=0, maxval=1)),
        ParamSpec('selector_release_gates',  'intlist', [2, 3, 0, 1], section="SELECTOR", hidden=True,
            validator=lambda self, v: self._validate_int_list(v, minval=0, maxval=self._selector.mmu_unit.num_gates - 1)),

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

        self._reinit()


    def _reinit(self):
        self.grip_state = FILAMENT_UNKNOWN_STATE


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

        # Finally restore the last known local gate position to avoid need to re-home. Must come
        # after the offsets above: the gate fallback needs them to turn a gate into a position.
        self._restore_position_at_startup()


    # Actual gate selection can be delayed (if not forcing grip) until the
    # filament_drive/release to reduce selector movement
    def _select_gate(self, lgate):
        super()._select_gate(lgate)

        with self.mmu.wrap_action(ACTION_SELECTING):
            if self.mmu_unit.filament_always_gripped:
                self._grip_release(lgate)


    def filament_drive(self):
        gate = self.mmu.gate_selected
        if self.mmu_unit.manages_gate(gate) and gate >= 0:
            lgate = self.mmu_unit.local_gate(gate)
            self._grip_release(lgate)


    def filament_release(self, measure=False):
        gate = self.mmu.gate_selected
        if self.mmu_unit.manages_gate(gate) and gate >= 0:
            lgate = self.mmu_unit.local_gate(gate)
            if not self.mmu_unit.filament_always_gripped:
                self._grip_release(lgate, release=True)
        return 0. # Fake encoder movement


    # --------------------------------------------------------------------------

    # Common logic for stepper movement
    def _grip_release(self, lgate, release=False):
        """
        Move to the grip or release position for a local gate.

        Persists VARS_MMU_SELECTOR_LAST_POS so the selector can restore an
        accurate gate/release position after a restart. Also sets filament drive
        direction based on configured gate directions.
        """
        if lgate >= 0:
            if release:
                pos = self.selector_offsets[self.p.selector_release_gates[lgate]]
                state = FILAMENT_RELEASE_STATE
                action = "filament released"

                if pos < 0:
                    self.mmu.log_error("Operation not possible because release gate position is not calibrated")
                    return

            else:
                pos = self.selector_offsets[lgate]
                state = FILAMENT_DRIVE_STATE
                action = "filament grip"

                if pos < 0:
                    self.mmu.log_error("Operation not possible because gate position is not calibrated")
                    return

        else:
            self.grip_state = FILAMENT_UNKNOWN_STATE
            return

        self.mmu.log_trace("Setting selector to %s position at: %.1f" % (action, pos))
        self._position(pos)
        self.grip_state = state

        # Ensure gate filament drive is in the correct direction. drive_obj() takes a machine gate
        # but everything here is unit-local, so convert rather than hand it a local index
        self.mmu_unit.drive_obj(self.mmu_unit.logical_gate(lgate)).set_gear_direction(
            self.p.selector_gate_directions[lgate])
        self.mmu.movequeue_wait()


    def get_filament_grip_state(self):
        return self.grip_state


    def _gate_position(self, lgate):
        """
        Note this is the GRIP position for the gate. Gate -> position is not a bijection on a
        rotary selector: the lazy-grip park sits at another gate's offset (see _grip_release), so
        a gate on its own cannot say whether we were parked gripped or released. Harmless for the
        startup restore because _reinit() resets grip_state to unknown on every boot anyway.
        """
        if 0 <= lgate < len(self.selector_offsets) and self.selector_offsets[lgate] >= 0:
            return self.selector_offsets[lgate]
        return None # No bypass on a rotary selector


    def enable_motors(self):
        self.selector_stepper.do_enable(True)


    def disable_motors(self):
        self.selector_stepper.do_enable(False)
        self._reinit()

        # Assume that if disabling motor then the position will be modified. Clears the persisted
        # gate too so the pair stays correlated -- see _invalidate_persisted_position()
        self._invalidate_persisted_position()


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

    def _get_max_selector_movement(self, lgate=TOOL_GATE_UNKNOWN):
        n = lgate if lgate >= 0 else self.mmu_unit.num_gates - 1
        max_movement = self.p.cad_gate0_pos + (n * self.p.cad_gate_width)
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
        lgate = self.mmu_unit.local_gate(gate)
        gate_str = lambda gate : ("gate %d" % gate) if gate >= 0 else "bypass"

        max_movement = self._get_max_selector_movement(lgate)
        self.mmu.log_always("Measuring the selector position for %s..." % gate_str(gate))
        traveled, found_home = self.measure_to_home()

        # Test we actually homed
        if not found_home:
            self.mmu.log_error("Selector didn't find home position")
            return False

        # Warn and don't save if the measurement is unexpected
        if traveled > max_movement:
            self.mmu.log_always(
                f"Selector move measured {traveled:.1f}mm. "
                f"More than the anticipated maximum of {max_movement:.1f}mm. "
                f"Save disabled\n"
                f"It is likely that your basic MMU dimensions are incorrect in "
                f"mmu_parameters.cfg. Check vendor/version and optional 'cad_*' parameters"
            )
            save = 0
        else:
            self.mmu.log_always("Selector move measured %.1fmm" % traveled)

        if save:
            self.selector_offsets[lgate] = round(traveled, 1)

            if (
                extrapolate
                and self.mmu_unit.num_gates > 1
                and (
                    (lgate == self.mmu_unit.num_gates - 1 and self.selector_offsets[0] > 0)
                    or
                    (lgate == 0 and self.selector_offsets[-1] > 0)
                )
            ):
                # Distribute selector spacing based on measurements of first and last gate
                spacing = (self.selector_offsets[-1] - self.selector_offsets[0]) / (self.mmu_unit.num_gates - 1)
                self.selector_offsets = [round(self.selector_offsets[0] + i * spacing, 1) for i in range(self.mmu_unit.num_gates)]

            elif extrapolate and lgate == 0 and self.selector_offsets[-1] == -1:
                # Distribute using cad spacing
                self.selector_offsets = [
                    round(self.selector_offsets[0] + i * self.p.cad_gate_width, 1)
                    for i in range(self.mmu_unit.num_gates)
                ]
            else:
                extrapolate = False

            self.var_manager.set(VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets, write=True, namespace=self.mmu_unit.name)

            if extrapolate:
                self.mmu.log_always(f"All selector offsets have been extrapolated and saved:\n{self.selector_offsets}")
            else:
                self.mmu.log_always(f"Selector offset ({traveled:.1f}mm) for {gate_str(gate)} has been saved")
                if gate == 0:
                    self.mmu.log_always(
                        f"Run MMU_CALIBRATE_SELECTOR again with GATE={self.mmu_unit.num_gates - 1} "
                        "to extrapolate all gate positions. Use SINGLE=1 to force calibration "
                        "of only one gate"
                    )
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
            self._invalidate_persisted_position() # Where the carriage stopped is anyone's guess
            raise MmuError(f"Homing selector failed because of blockage or malfunction. Klipper reports: {e}") from e


    def _home_hard_endstop(self):
        self.mmu.log_always("Forcing selector homing to hard endstop. Excuse the noise!\n(Configure stallguard endstop on selector stepper to avoid)")
        self._restore_position(self._get_max_selector_movement()) # Worst case position to allow full movement
        self.move("Forcibly homing to hard endstop", new_pos=0, speed=self.p.selector_homing_speed)
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

    HELP_BRIEF = "Calibration of the selector positions or position of specified gate"
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

        if selector.check_if_unit_loaded(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        single = gcmd.get_int('SINGLE', 0, minval=0, maxval=1)
        quick = gcmd.get_int('QUICK', 0, minval=0, maxval=1)
        # GATE is a machine-wide number, so range it against the machine and check ownership -
        # a unit-local range silently addresses the wrong gate on any unit that isn't first
        min_gate, max_gate = mmu_unit.gate_bounds()
        gate = gcmd.get_int('GATE', min_gate, minval=0, maxval=mmu.num_gates - 1)
        if not mmu_unit.manages_gate(gate):
            raise gcmd.error("Gate %d is not managed by %s (range=%d-%d)"
                             % (gate, mmu_unit.name, min_gate, max_gate))

        try:
            mmu.calibrating = True
            successful = False

            if selector.has_endstop and not quick:
                successful = selector._calibrate_selector(gate, extrapolate=not single, save=save)
            else:
                mmu.log_always("%s - will calculate gate offsets from cad_gate0_pos and cad_gate_width" % ("Quick method" if quick else "No endstop configured"))
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
