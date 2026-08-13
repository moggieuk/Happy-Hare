# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of LinearIdlerSelector:
#  Implements Linear Selector for type-A MMU's with stepper-driven idler
#  filament grip (Prusa MMU3)
#  - Stepper controlled linear selector movement with endstop (from LinearSelector)
#  - Stepper driven idler barrel to grip/release filament at each gate
#
# Implements commands:
#    MMU_CALIBRATE_SELECTOR (LinearSelector)
#    MMU_SOAKTEST_SELECTOR (LinearSelector)
#    MMU_IDLER              (LinearSelectorIdler)
#    MMU_CALIBRATE_IDLER    (LinearSelectorIdler)
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

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...commands            import register_command
from ...mmu_base_parameters import TunableParametersBase, ParamSpec
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_linear_selector   import LinearSelector, LinearSelectorParameters


# -----------------------------------------------------------------------------------------------------------
# Additional parameters for linear idler selector
# -----------------------------------------------------------------------------------------------------------

class LinearIdlerSelectorParameters(LinearSelectorParameters):

    _SPECS = (*LinearSelectorParameters._SPECS,
        ParamSpec('idler_dwell',            'float', 0.4, section="IDLER", limits=dict(minval=0.1)),
        ParamSpec('idler_buzz_gear_on_down', 'int',    3, section="IDLER", limits=dict(minval=0, maxval=10)),
    )

    def __init__(self, config, selector):
        self._selector = selector
        super().__init__(config, selector)


# -----------------------------------------------------------------------------------------------------------
# LinearIdlerSelector implementation
# -----------------------------------------------------------------------------------------------------------

class LinearIdlerSelector(LinearSelector):
    """
    Linear selector variant with a stepper-driven idler barrel for filament
    gripping (Prusa MMU3).

    Extends LinearSelector by constructing the idler component and delegating
    the filament grip interface to it, exactly as LinearServoSelector
    delegates to its servo component.
    """
    PARAMS_CLS = LinearIdlerSelectorParameters

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        self.idler = LinearSelectorIdler(config, mmu_unit, self)


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.idler.handle_connect()

    def handle_ready(self):
        super().handle_ready()
        self.idler.handle_ready()

    def handle_disconnect(self):
        super().handle_disconnect()
        self.idler.handle_disconnect()

    def home(self, force_unload=None):
        # Home selector, then idler (MMU3 requires both before operation)
        super().home(force_unload)
        self.idler.home()

    def filament_drive(self, buzz_gear=True):
        return self.idler.idler_down(buzz_gear=buzz_gear)

    def filament_release(self, measure=False):
        return self.idler.idler_up(measure=measure)

    def filament_hold_move(self): # AKA position for holding filament and moving selector
        return self.idler.idler_move()

    def get_filament_grip_state(self):
        return self.idler.get_filament_grip_state()

    def enable_motors(self):
        super().enable_motors()
        self.idler.enable_motors()

    def disable_motors(self):
        super().disable_motors()
        self.idler.disable_motors()

    def buzz_motor(self, motor):
        if motor == "idler":
            self.idler.buzz_motor()
            return True
        return super().buzz_motor(motor)

    def get_status(self, eventtime):
        status = super().get_status(eventtime)
        status.update(self.idler.get_status())
        return status


# -----------------------------------------------------------------------------------------------------------
# LinearSelectorIdler
#  Stepper-driven idler barrel (Prusa MMU3) mapped onto the servo interface of
#  LinearServoSelector:
#     idler_down()  ~ servo_down()   -> grip filament at selected gate
#     idler_move()  ~ servo_move()   -> disengage (idler at num_gates position)
#     idler_up()    ~ servo_up()     -> release filament (pre-positioned at gate)
#
# The idler rail is a regular [mmu_stepper X_idler] with stallguard "touch"
# homing on the TMC2130 virtual endstop (SENSOR_IDLER_TOUCH). Per-gate barrel
# offsets (plus one disengaged position) are calibrated with MMU_CALIBRATE_IDLER
# and persisted in mmu_vars.cfg as VARS_MMU_IDLER_OFFSETS.
# -----------------------------------------------------------------------------------------------------------

class LinearSelectorIdler:

    def __init__(self, config, mmu_unit, selector):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.selector = selector
        self.printer = config.get_printer()

        self.params = self.p = selector.p

        # Grip states (reuse filament state constants)
        self.idler_state = FILAMENT_UNKNOWN_STATE

        # Disengaged position is one beyond the last gate
        self._disengaged_gate = self.mmu_unit.num_gates
        self.active_gate = -1
        self.is_homed = False

        # Load the idler stepper + its TMC (mirrors LinearSelector.__init__)
        idler_stepper_name = self.mmu_unit.config.get('idler_stepper')
        stepper_section = f"mmu_stepper {idler_stepper_name}"

        tmc_found = False
        for chip in TMC_CHIPS:
            tmc_section = f"{chip} {stepper_section}"
            if config.has_section(tmc_section):
                _ = self.printer.load_object(config, tmc_section)
                logging.info("MMU: Loaded: [%s]" % tmc_section)
                tmc_found = True
                break
        if not tmc_found:
            raise config.error("Idler stepper TMC configuration not found for %s on mmu_unit %s" % (idler_stepper_name, self.mmu_unit.name))

        # Inject sensible config if not supplied by user
        key = "homing_speed"
        if not config.fileconfig.has_option(stepper_section, key):
            config.fileconfig.set(stepper_section, key, "20")

        # Now we can load the mmu_stepper object
        self.idler_stepper = self.printer.load_object(config, stepper_section)
        logging.info("MMU: Loaded: [%s]" % stepper_section)

        # Does idler have stallguard "touch" homing (default endstop must be
        # the TMC virtual endstop -- the MMU3 has no physical idler endstop)?
        self.idler_touch = (self.idler_stepper.can_home and self.idler_stepper.rail.endstop_is_virtual)

        # Register GCODE commands specific to this module
        try:
            register_command(MmuCalibrateIdlerCommand)
            register_command(MmuIdlerCommand)
        except KeyError:
            pass # Already registered


    def handle_connect(self):
        self.mmu = self.mmu_unit.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager
        self.calibrator = self.mmu_unit.calibrator

        if not self.idler_touch:
            self.mmu.log_error("No idler 'touch' endstop defined. Idler cannot be homed!")


    def handle_ready(self):
        """
        Loads per-gate idler offsets from mmu_vars.cfg, ensures list sizing
        matches num_gates + 1 (disengaged), and sets calibrated status when
        all offsets are known.
        """
        def ensure_list_size(lst, size, default_value=-1):
            lst = lst[:size]
            lst.extend([default_value] * (size - len(lst)))
            return lst

        self.idler_offsets = self.var_manager.get(VARS_MMU_IDLER_OFFSETS, None, namespace=self.mmu_unit.name)
        if self.idler_offsets:
            if len(self.idler_offsets) == self.mmu_unit.num_gates + 1:
                self.mmu.log_debug("Loaded saved idler offsets: %s" % self.idler_offsets)
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % VARS_MMU_IDLER_OFFSETS)
                self.idler_offsets = ensure_list_size(self.idler_offsets, self.mmu_unit.num_gates + 1)

            if not any(x == -1 for x in self.idler_offsets):
                self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        else:
            # No saved offsets: fall back to the vendor CAD defaults (derived
            # from Prusa's factory idler geometry) so a fresh install has
            # working gate positions out of the box.
            defaults = list(self.p.cad_idler_offsets)
            defaults = ensure_list_size(defaults, self.mmu_unit.num_gates + 1)
            if not any(x == -1 for x in defaults):
                self.mmu.log_always("Idler offsets not found in mmu_vars.cfg. Using CAD defaults: %s" % defaults)
                self.idler_offsets = defaults
                self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
            else:
                self.mmu.log_always("Warning: Idler offsets not found in mmu_vars.cfg. Probably not calibrated")
                self.idler_offsets = [-1] * (self.mmu_unit.num_gates + 1)
        self.var_manager.set(VARS_MMU_IDLER_OFFSETS, self.idler_offsets, namespace=self.mmu_unit.name)


    def handle_disconnect(self):
        pass


    # Filament grip interface (servo replacement) ------------------------------

    def idler_down(self, buzz_gear=True):
        """Grip filament at the currently selected gate (idler pressed down)."""
        if self.mmu._is_running_test: return # Save idler while testing
        if self.mmu.gate_selected == TOOL_GATE_BYPASS: return
        if self.mmu_unit.manages_gate(self.mmu.gate_selected):
            lgate = self.mmu_unit.local_gate(self.mmu.gate_selected)
            if self.active_gate == lgate: return
            self.mmu.log_trace("Setting idler to grip position for gate %d" % self.mmu.gate_selected)

            if buzz_gear and self.p.idler_buzz_gear_on_down > 0:
                self.mmu.drive().sync_mode(DRIVE_UNSYNCED) # Must be in correct sync mode before buzz to avoid delay

            self.mmu.movequeue_wait()
            initial_encoder_position = self.mmu.get_encoder_distance(dwell=None)
            self._set_idler_to_gate(lgate)

            if self.active_gate == lgate and buzz_gear and self.p.idler_buzz_gear_on_down > 0:
                # Very important that suppress_grip_change=True to avoid infinite recursion
                for _ in range(self.p.idler_buzz_gear_on_down):
                    self.mmu.move_filament(None, 0.8, speed=25, accel=self.mmu_unit.p.gear_buzz_accel, encoder_dwell=None, speed_override=False, suppress_grip_change=True)
                    self.mmu.move_filament(None, -0.8, speed=25, accel=self.mmu_unit.p.gear_buzz_accel, encoder_dwell=None, speed_override=False, suppress_grip_change=True)
                self.mmu.movequeue_dwell(max(self.p.idler_dwell, 0))

            self.mmu.set_encoder_distance(initial_encoder_position)
            self.mmu.mmu_macro_event(MACRO_EVENT_FILAMENT_GRIPPED)


    def idler_move(self):
        """Disengage idler so the selector can move freely."""
        if self.mmu._is_running_test: return # Save idler while testing
        if self.active_gate == self._disengaged_gate: return
        self.mmu.log_trace("Setting idler to disengaged (move) position")
        # Selector and idler share the same MCU -- wait for selector to finish
        # before starting idler move to avoid MCU move queue overflow
        self.mmu.movequeue_wait()
        self._set_idler_to_gate(self._disengaged_gate)


    def idler_up(self, measure=False):
        """Release filament; pre-position at selected gate (MMU3 idler)."""
        if self.mmu._is_running_test: return 0. # Save idler while testing
        gate = self.mmu.gate_selected
        if self.mmu_unit.manages_gate(gate) and gate >= 0:
            lgate = self.mmu_unit.local_gate(gate)
        else:
            lgate = self._disengaged_gate
        if self.active_gate == lgate:
            return 0. # Already at correct position
        self.mmu.log_trace("Setting idler to release position for gate %d" % gate)
        # Selector and idler share the same MCU -- wait for selector to finish
        # before starting idler move to avoid MCU move queue overflow
        self.mmu.movequeue_wait()
        self._set_idler_to_gate(lgate)
        return 0. # Fake encoder movement


    def get_filament_grip_state(self):
        return self.idler_state


    def enable_motors(self):
        self.idler_stepper.do_enable(True)


    def disable_motors(self):
        self.idler_stepper.do_enable(False)
        self.is_homed = False
        self.reinit() # Reset state


    def buzz_motor(self):
        self.mmu.movequeue_wait()
        old_state = self.idler_state
        cur_pos = self.idler_stepper.commanded_pos
        dist = 2.0 # mm
        speed = self.p.selector_move_speed
        accel = self.p.selector_accel
        for p in (cur_pos + dist, cur_pos - dist, cur_pos):
            self.idler_stepper.do_move(p, speed, accel)
        self.mmu.movequeue_wait()
        if old_state == FILAMENT_DRIVE_STATE:
            self.idler_down(buzz_gear=False)
        elif old_state == FILAMENT_HOLD_STATE:
            self.idler_move()
        else:
            self.idler_up()


    def home(self):
        """Home the idler rail using stallguard 'touch' endstop."""
        if not self.idler_touch:
            raise MmuError("Idler cannot be homed because no idler 'touch' endstop is defined")
        with self.mmu.wrap_action(ACTION_HOMING):
            self.mmu.log_info("Homing MMU Idler...")
            self.mmu.movequeue_wait()
            try:
                self.idler_stepper.do_home_rail()
                self.is_homed = True
                self.mmu.log_info("Homed MMU Idler...")
            except Exception as e:  # Homing failed
                raise MmuError("Homing idler failed. Klipper reports: %s" % str(e))


    def reinit(self):
        self.idler_state = FILAMENT_UNKNOWN_STATE
        self.active_gate = -1


    def get_status(self):
        return {
            'idler': "Up" if self.idler_state == FILAMENT_RELEASE_STATE else
                     "Down" if self.idler_state == FILAMENT_DRIVE_STATE else
                     "Move" if self.idler_state == FILAMENT_HOLD_STATE else
                     "Unknown",
            'idler_gate': self.active_gate,
        }


    def get_uncalibrated_gates(self, check_gates):
        """Return a list of gates that are not calibrated"""
        return [
            lgate + self.mmu_unit.first_gate
            for lgate, value in enumerate(self.idler_offsets)
            if value == -1 and lgate < self.mmu_unit.num_gates and lgate + self.mmu_unit.first_gate in check_gates
        ]


    # Internal Implementation --------------------------------------------------

    def _set_idler_to_gate(self, lgate):
        """
        Move the idler barrel to the position for a local gate
        (or the disengaged position when lgate == num_gates).
        """
        if not self.is_homed:
            return # Position unknown until the idler has been homed
        if lgate == TOOL_GATE_BYPASS: return
        if lgate >= len(self.idler_offsets):
            self.mmu.log_error("Gate number does not exist")
            return
        if self.idler_offsets[lgate] < 0:
            self.mmu.log_error("Idler position for gate %d is not calibrated" % lgate)
            return

        target_pos = self.idler_offsets[lgate]
        speed = self.p.selector_move_speed
        accel = self.p.selector_accel

        self.mmu.log_trace("IDLER MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (target_pos, speed, accel))
        self.idler_stepper.do_move(target_pos, speed, accel)
        self.mmu.movequeue_wait()

        self.active_gate = lgate
        if lgate == self._disengaged_gate:
            self.idler_state = FILAMENT_RELEASE_STATE
        else:
            self.idler_state = FILAMENT_DRIVE_STATE


    def _check_calibrated(self):
        if not any(x == -1 for x in self.idler_offsets):
            self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        else:
            self.calibrator.mark_not_calibrated(CALIBRATED_SELECTOR)


# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_IDLER command
#  This "registered command" will be conditionally registered, then instantiated later by the main
#  mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

from ...commands.mmu_base_command import *

class MmuCalibrateIdlerCommand(BaseCommand):

    CMD = "MMU_CALIBRATE_IDLER"

    HELP_BRIEF = "Calibration of the idler positions for specified gate(s)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT   = #(int) Optional if only one unit fitted to printer\n"
        + "GATE   = #(int) Specify the gate by it's global logical index\n"
        + "LGATE  = #(int) Speficy gate by the local mmu unit index (same as GATE with single MMU unit, LGATE=num_gates for the disengaged position)\n"
        + "POSITION = #(float) Current idler position to save for the gate\n"
        + "SAVE   = 1      To persist the calibration results else they will just be reported\n"
        + "RESET  = 1      To remove calibrated settings and default to configured starting values\n"
        + "(no options to show the current calibration)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                          ...Report on current calibration\n"
        + f"{CMD} LGATE=0 POSITION=10.5    ...Save current idler position for local gate 0\n"
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
        mmu = mmu_unit.mmu
        selector = mmu_unit.selector

        if self.check_if_disabled(): return
        if not isinstance(selector, LinearIdlerSelector):
            self.mmu.log_error("Operation not possible on this selector type (LinearIdlerSelector only)")
            return
        idler = selector.idler

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        position = gcmd.get_float('POSITION', None)
        gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
        lgate = gcmd.get_int('LGATE', None, minval=0, maxval=mmu_unit.num_gates) # num_gates == disengaged position

        if reset:
            defaults = list(selector.p.cad_idler_offsets)
            size = mmu_unit.num_gates + 1
            idler.idler_offsets = (defaults[:size] + [-1] * (size - len(defaults))) if defaults else [-1] * size
            idler.var_manager.set(VARS_MMU_IDLER_OFFSETS, idler.idler_offsets, write=True, namespace=mmu_unit.name)
            idler._check_calibrated()
            mmu.log_always(f"Reset idler calibration on {mmu_unit.name} to: {idler.idler_offsets}")
            return

        if gate is not None and not mmu_unit.manages_gate(gate):
            raise gcmd.error(f"Gate {gate} is not managed by {mmu_unit.name}")

        if gate is not None and lgate is not None:
            raise gcmd.error("Specify either GATE or LGATE, not both")

        lgate = lgate if lgate is not None else mmu_unit.local_gate(gate) if gate is not None else None

        if lgate is None:
            # Report current calibration
            mmu.log_always(f"Idler offsets: {idler.idler_offsets}")
            return

        if position is None:
            raise gcmd.error("POSITION must be specified when calibrating a gate")

        idler.idler_offsets[lgate] = position
        if save:
            idler.var_manager.set(VARS_MMU_IDLER_OFFSETS, idler.idler_offsets, write=True, namespace=mmu_unit.name)
            mmu.log_always(f"Idler position for gate {lgate} saved: {position}")

        idler._check_calibrated()


# -----------------------------------------------------------------------------------------------------------
# MMU_IDLER command
# -----------------------------------------------------------------------------------------------------------

class MmuIdlerCommand(BaseCommand):

    CMD = "MMU_IDLER"

    HELP_BRIEF = "Move MMU idler to specified position"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT   = #(int) Optional if only one unit fitted to printer\n"
        + "HOME   = 1      Home the idler\n"
        + "GATE   = #(int) Move idler to the specified gate (global logical index)\n"
        + "POSITION = #(float) Move idler to absolute position\n"
        + "(no options to report current idler position)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} HOME=1               ...Home the idler\n"
        + f"{CMD} GATE=2               ...Position idler for gate 2\n"
        + f"{CMD} POSITION=20.0        ...Move idler to 20.0mm\n"
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
        mmu = mmu_unit.mmu
        selector = mmu_unit.selector

        if self.check_if_disabled(): return
        if not isinstance(selector, LinearIdlerSelector):
            self.mmu.log_error("Operation not possible on this selector type (LinearIdlerSelector only)")
            return
        idler = selector.idler

        home = gcmd.get_int('HOME', None)
        gate = gcmd.get_int('GATE', None)
        position = gcmd.get_float('POSITION', None)

        if home:
            idler.home()
        elif gate is not None:
            if not mmu_unit.manages_gate(gate):
                raise gcmd.error(f"Gate {gate} is not managed by {mmu_unit.name}")
            if not idler.is_homed:
                self.mmu.log_error("Idler not homed")
                return
            idler._set_idler_to_gate(mmu_unit.local_gate(gate))
        elif position is not None:
            if not idler.is_homed:
                self.mmu.log_error("Idler not homed")
                return
            idler._set_idler_to_gate(idler._disengaged_gate)
            speed = idler.p.selector_move_speed
            accel = idler.p.selector_accel
            idler.idler_stepper.do_move(position, speed, accel)
            idler.mmu.movequeue_wait()
            mmu.log_always(f"Idler moved to {position:.1f}mm")
        else:
            mmu.log_always(f"Current idler position: {idler.idler_stepper.commanded_pos:.1f}mm")
            mmu.log_info("Use HOME=1, GATE=, or POSITION= to move position")
