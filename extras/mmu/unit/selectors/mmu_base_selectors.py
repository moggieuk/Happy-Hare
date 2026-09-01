# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Base classes for all Happy Hare selectors
#
# BaseSelector:
#   - All selectors must extend this class
#   - Defines expected contract with mmu_controller
#
# PhysicalSelector:
#   - Base class for selectors that involve movement
#   - Implements selector soaktest
#       MMU_CALIBRATE_SELECTOR
#
# VirtualSelector:
#  Implements selector for type-B MMU's with gear driver per gate
#   - Uses gear driver stepper per-gate
#   - For type-B designs like BoxTurtle, KMS, QuattroBox
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import random, logging

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...commands            import register_command
from ...mmu_base_parameters import TunableParametersBase
from ..mmu_calibrator       import CALIBRATED_SELECTOR


class BaseSelector:
    """
    Base class for all selectors.

    Provides the expected contract with the mmu_controller and basic
    plumbing used by selector implementations.
    """
    PARAMS_CLS = TunableParametersBase # Empty parameters in case selector doesn't have parameters (like VirtualSelector)

    def __init__(self, config, mmu_unit, unit_params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.printer = config.get_printer()

        # Always create empty params class or one specific to selector type
        if self.PARAMS_CLS is TunableParametersBase:
            self.params = self.p = self.PARAMS_CLS(config)
        elif issubclass(self.PARAMS_CLS, TunableParametersBase):
            self.params = self.p = self.PARAMS_CLS(config, self)

        self.is_homed = False                   # Whether selector is home and knows current position
        self.requires_homing = True             # Whether selector requires homing

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)


    # Prevent overriding of methods with physical gate number as parameter
    # It is important and all selector logic works with local gates
    _final_methods = {"select_gate, restore_gate"}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        overridden = BaseSelector._final_methods.intersection(cls.__dict__.keys())
        if overridden:
            raise TypeError(
                f"{cls.__name__} is not allowed to override: {', '.join(sorted(overridden))}"
            )


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager
        self.calibrator = self.mmu_unit.calibrator


    def handle_ready(self):
        pass


    def handle_disconnect(self):
        pass


    def home(self):
        pass


    def select_gate(self, gate):
        """
        Select physical gate position. Maybe a no-op if already selected.
        Don't override this method, instead override _select_gate() after the local gate translation.
        """
        if not self.mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            raise MmuError(f"Selector is not calibrated on %s" % self.mmu_unit.name)

        lgate = self._local_gate(gate)
        self._select_gate(lgate)

    def _select_gate(self, lgate):
        pass


    def restore_gate(self, gate):
        """
        Marks selector as having designated gate selected. This is a no-op on many selectors.
        Don't override this method, instead override _restore_gate() after the local gate translation.
        """
        lgate = self._local_gate(gate)
        self._restore_gate(lgate)

    def _restore_gate(self, lgate):
        pass


    def filament_drive(self):
        pass


    def filament_release(self, measure=False):
        return 0. # Fake encoder movement


    def filament_hold_move(self):
        pass


    def get_filament_grip_state(self):
        return FILAMENT_DRIVE_STATE


    def set_filament_grip(self, state):
        """
        Convenient way to restore previous grip state
        """
        if state == FILAMENT_DRIVE_STATE:
            self.filament_drive()
        elif state == FILAMENT_RELEASE_STATE:
            self.filament_release()
        elif state == FILAMENT_HOLD_STATE:
            self.filament_hold_move()


    def disable_motors(self):
        pass


    def enable_motors(self):
        pass


    def buzz_motor(self, motor):
        return False


    def has_bypass(self):
        """
        Whether the selector has a selectable bypass gate
        """
        return False


    def get_status(self, eventtime):
        return {
            'has_bypass': self.has_bypass()
        }


    def get_mmu_status_config(self):
        return "Selector Type: %s." % self.__class__.__name__


    def get_uncalibrated_gates(self, check_gates):
        """
        Returns a list of absolute gate numbers (not local indices) that are still uncalibrated
        """
        return []


    def _local_gate(self, gate):
        """
        Convert an absolute gate number to a local gate index for this unit.
        """
        return self.mmu_unit.local_gate(gate)


    def _logical_gate(self, lgate):
        """
        Convert an local gate on this unit to absolute (logical) gate number.
        """
        return self.mmu_unit.logical_gate(lgate)


# -----------------------------------------------------------------------------------------------------------
# Base class for all selectors with moving parts
# -----------------------------------------------------------------------------------------------------------

class PhysicalSelector(BaseSelector, object):
    """
    Base class for selectors that involve movement.

    Provides common functionality used by physical selector implementations
    including a soak test command for exercising selector movement.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        # Register GCODE commands
        try:
            register_command(MmuSoaktestSelectorCommand)
            register_command(MmuGripCommand)
            register_command(MmuReleaseCommand)
        except KeyError:
            pass # Already registered


    def handle_connect(self):
        super().handle_connect()


    def handle_ready(self):
        super().handle_ready()


    def handle_disconnect(self):
        super().handle_disconnect()


    # -----------------------------------------------------------------------------------------------------------
    # Persisted position
    #
    # Two records describe where the carriage is, and they must never disagree:
    #   VARS_MMU_SELECTOR_LAST_POS  - this unit's raw carriage position (primary)
    #   VARS_MMU_GATE_SELECTED      - the gate that position corresponds to (secondary)
    # Everything that forgets the position goes through _invalidate_persisted_position(), which
    # clears both. That is what makes the gate safe to fall back on: it can only be trusted
    # when last_pos was never RECORDED, never when it was INVALIDATED.
    # -----------------------------------------------------------------------------------------------------------

    def _gate_position(self, lgate):
        """
        Carriage position for a local gate, or None if this selector cannot say.

        Override in selectors that map gates to positions.
        """
        return None


    def _restore_position_at_startup(self):
        """
        Re-establish the carriage position at klippy:ready so a reboot needs no re-home.

        This runs before MmuController.handle_ready (units are built before the controller in
        mmu_machine.py, and klipper dispatches in registration order), so is_homed is already
        settled by the time mmu_gate_maps.load_persisted_state() decides whether to keep the
        persisted gate.

        NOTE: a bare M84 / TURN_OFF_MOTORS de-energises the selector through klipper's
        stepper_enable without Happy Hare seeing it, so last_pos survives a de-energising we
        never observed. Trusting it is a pre-existing assumption of this scheme, not something
        the gate fallback adds.
        """
        last_pos = self.var_manager.get(VARS_MMU_SELECTOR_LAST_POS, None, namespace=self.mmu_unit.name)
        if last_pos is None and self.requires_homing:
            # No position on record. If the gate is still on record it was never invalidated
            # (an upgrade or a renamed unit that never wrote the namespaced var), so the gate's
            # calibrated offset is where we are.
            #
            # Only a physical selector that requires homing may infer a carriage position.
            # Always-homed selectors such as VirtualSelector have no carriage position to
            # reconstruct; their pre-existing explicit last_pos handling below is untouched.
            last_pos = self._persisted_gate_position()
            if last_pos is not None:
                self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, last_pos, namespace=self.mmu_unit.name)

        if last_pos is not None:
            self._restore_position(last_pos)
            self.is_homed = True


    def _persisted_gate_position(self):
        """
        Position implied by the persisted gate selection, or None if it can't be trusted.
        """
        if not self.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            return None # Offsets are -1 placeholders, so keep reporting unhomed

        gate = self.var_manager.get(VARS_MMU_GATE_SELECTED, TOOL_GATE_UNKNOWN)
        if not isinstance(gate, int) or gate == TOOL_GATE_UNKNOWN:
            return None # Explicit: manages_gate() answers True on unknown for EVERY unit

        if not self.mmu_unit.manages_gate(gate):
            return None # Another unit's gate

        return self._gate_position(self._local_gate(gate))


    def _invalidate_persisted_position(self):
        """
        Forget where we are, on disk as well as in memory.

        Clears the persisted gate/tool alongside last_pos: the gate alone is enough for
        _persisted_gate_position() to reconstruct a position, so leaving it behind would
        resurrect exactly the position this call is invalidating. Scoped to the unit that owns
        the selection a reboot would restore, so MMU_MOTORS_OFF UNIT=0 cannot wipe a good
        selection belonging to another unit.
        """
        self.is_homed = False
        self.var_manager.set(VARS_MMU_SELECTOR_LAST_POS, None, namespace=self.mmu_unit.name)

        # The persisted gate, not mmu.gate_selected: the question is literally what a reboot
        # would reconstruct from this file.
        gate = self.var_manager.get(VARS_MMU_GATE_SELECTED, TOOL_GATE_UNKNOWN)
        if isinstance(gate, int) and gate != TOOL_GATE_UNKNOWN and self.mmu_unit.manages_gate(gate):
            self.var_manager.set(VARS_MMU_GATE_SELECTED, TOOL_GATE_UNKNOWN)
            self.var_manager.set(VARS_MMU_TOOL_SELECTED, TOOL_GATE_UNKNOWN)

        self.var_manager.write() # One flush, so the pair can never land separately


    def home(self):
        """
        Home the physical selector mechanism.

        MmuController.home_unit() verifies that selector motion cannot be
        obstructed by filament before calling this mechanical operation.
        """
        if not self.requires_homing: return
        if self.check_if_unit_bypass(): return

        with self.mmu.wrap_action(ACTION_HOMING):
            self.mmu.log_info("Homing MMU %s..." % self.mmu_unit.name)

            self._home_selector()


    def _select_gate(self, lgate):
        if lgate == TOOL_GATE_UNKNOWN: return
        if self.requires_homing and not self.is_homed:
            raise MmuError(f"Selector is not homed on %s" % self.mmu_unit.name)
        super()._select_gate(lgate)


    def _restore_gate(self, lgate):
        if lgate == TOOL_GATE_UNKNOWN: return
        super()._restore_gate(lgate)


    def check_if_unit_bypass(self):
        """
        Similar to MMU controller check but localized to specific selector
        """
        if not self.mmu._unit_owns_selection(self.mmu_unit, self.mmu.gate_selected):
            return False
        if self.mmu.tool_selected == TOOL_GATE_BYPASS and self.mmu.filament_pos not in [FILAMENT_POS_UNLOADED]:
            self.mmu.log_error("Operation not possible. MMU is currently using bypass. Unload or select a different gate first")
            return True
        return False


    def check_if_unit_loaded(self):
        """
        Similar to MMU controller check but localized to specific selector
        """
        if self.mmu._unit_may_have_filament(self.mmu_unit):
            self.mmu.log_error("Operation not possible. Filament may be loaded or its state is unknown")
            return True
        return False


    def get_mmu_status_config(self):
        msg =  super().get_mmu_status_config()
        if self.requires_homing:
            msg += " Selector is %s." % ("HOMED" if self.is_homed else "NOT HOMED")
        return msg


# -----------------------------------------------------------------------------------------------------------
# Base class for type-B MMU's
# -----------------------------------------------------------------------------------------------------------

class VirtualSelector(BaseSelector):
    """
    Selector implementation for type-B MMUs which use a gear driver per gate.

    This virtual selector uses a gear stepper on the toolhead and does not
    require physical homing because it selects gears rather than moving a
    selector carriage.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.is_homed = True # Always "homed" since no selector movement
        self.requires_homing = False


    def handle_connect(self):
        super().handle_connect()
        self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)


    def handle_ready(self):
        super().handle_ready()


    def handle_disconnect(self):
        super().handle_disconnect()


    def _select_gate(self, lgate):
        super()._select_gate(lgate)




# -----------------------------------------------------------------------------------------------------------
# Calibration commands are defined here to keep close to helper logic
# -----------------------------------------------------------------------------------------------------------

from ...commands.mmu_base_command import *


# -----------------------------------------------------------------------------------------------------------
# MMU_SOAKTEST_SELECTOR command
#  This "registered command" will be conditionally registered in PhysicalSelector, then instantiated later
#  by the main mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

class MmuSoaktestSelectorCommand(BaseCommand):

    CMD = "MMU_SOAKTEST_SELECTOR"

    HELP_BRIEF = "Soak test of selector movement"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT  = #(int) Optional if only one unit fitted to printer\n"
        + "LOOP  = #(int) Test loops (default 10)\n"
        + "GRIP  = [0|1]  Force filament gripping after selection where optional\n"
        + "HOME  = [0|1]  Randomized homing\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s UNIT=1 LOOP=1000 ...make 1000 gate selections on unit 1\n" % CMD
        + "%s HOME=1           ...randomly home whilst testing selection on current unit\n" % CMD
        + "%s GRIP=1           ...force filament grip after selection (where servo/gripping available)\n" % CMD
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
        Run a soak test exercising selector movement across random gates.

        The command supports unit selection, loop count, optional gripping
        and randomized homing. Errors from the MMU are handled and cause
        the soak test to abort cleanly.
        """
        mmu = self.mmu

        if self.check_if_disabled(): return
        if mmu_unit.selector.check_if_unit_loaded(): return

        if not mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            mmu.log_error("Operation not possible. Selector not yet calibrated")
            return

        loops = gcmd.get_int('LOOP', 10)
        servo = gcmd.get_int('SERVO', 0) # Legacy option, replaced by generic "GRIP"
        grip = bool(gcmd.get_int('GRIP', servo))
        home = bool(gcmd.get_int('HOME', 0))

        # Test and report using logical system-wide gate numbering (by design user never sees local gate numbers)
        min_gate, max_gate = mmu_unit.gate_bounds()
        mmu.log_always("Soak testing selector on %s (gates %d-%d) for %s iterations..." % (mmu_unit.name, min_gate, max_gate, loops))

        # We test fully by going through the MMU controller and not to the selector directly
        try:
            with mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    gate = random.randint(min_gate, max_gate)

                    if random.randint(0, 10) == 0 and home:
                        mmu.home_unit(mmu_unit)

                    if random.randint(0, 10) == 0 and mmu_unit.selector.has_bypass():
                        mmu.log_always("Testing loop %d / %d. Selecting bypass..." % (l + 1, loops))
                        mmu_unit.selector._select_gate(TOOL_GATE_BYPASS) # Force local bypass gate
                    else:
                        mmu.log_always("Testing loop %d / %d. Selecting gate %d..." % (l + 1, loops, gate))
                        mmu_unit.selector.select_gate(gate)

                    if grip:
                        mmu_unit.selector.filament_drive()
        except MmuError as ee:
            mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))



# -----------------------------------------------------------------------------------------------------------
# MMU_GRIP command
#  This "registered command" will be conditionally registered in PhysicalSelector, then instantiated later
#  by the main mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

class MmuGripCommand(BaseCommand):
    """
    Note that because this command operates on the current gate selected it is not a per-unit command
    """

    CMD = "MMU_GRIP"

    HELP_BRIEF = "Grip filament in current gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            per_unit=False,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        gate = mmu.gate_selected
        mmu_unit = mmu.mmu_unit(gate)

        if gate >= 0:
            mmu_unit.selector.filament_drive()



# -----------------------------------------------------------------------------------------------------------
# MMU_RELEASE command
#  This "registered command" will be conditionally registered in PhysicalSelector, then instantiated later
#  by the main mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

class MmuReleaseCommand(BaseCommand):
    """
    Note that because this command operates on the current gate selected it is not a per-unit command
    """

    CMD = "MMU_RELEASE"

    HELP_BRIEF = "Ungrip filament in current gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
    )
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            per_unit=False,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        gate = mmu.gate_selected
        mmu_unit = mmu.mmu_unit(gate)

        if gate >= 0:
            if not mmu_unit.filament_always_gripped:
                mmu_unit.selector.filament_release()
            else:
                mmu.log_error("Selector doesn't allow or not configured to allow filament release")
