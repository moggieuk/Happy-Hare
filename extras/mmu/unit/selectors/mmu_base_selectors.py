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

        self.params = self.p = self.PARAMS_CLS(config, self)

        self.is_homed = False                   # Whether selector is home and knows current position
        self.requires_homing = True             # Whether selector requires homing

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)

    # Prevent overriding of methods with physical gate number as parameter
    # It is important and all selector logic works with local gates
    _final_methods = {"select_gate"}

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

    def home(self, force_unload = None):
        pass

    def select_gate(self, gate):
        """
        Select physical gate position. Maybe a no-op if already selected.
        Don't override this method, instead override _select_gate() after the local gate translation.
        """
        lgate = self._local_gate(gate)
        self._select_gate(lgate)

    def _select_gate(self, lgate):
        pass

    def filament_drive(self):
        pass

    def filament_release(self, measure=False):
        return 0. # Fake encoder movement

    def filament_hold_move(self):
        pass

    def get_filament_grip_state(self):
        return FILAMENT_DRIVE_STATE

    def disable_motors(self):
        pass

    def enable_motors(self):
        pass

    def buzz_motor(self, motor):
        return False

    def has_bypass(self):
        """
        Whether the selector has a selectable bypass gate (not whether to show on unit)
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

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()

    def handle_ready(self):
        super().handle_ready()

    def handle_disconnect(self):
        super().handle_disconnect()


    def home(self, force_unload = None):
        """
        Home the selector, optionally unloading filament first.

        If bypass is active, homing is skipped. When requested (or required by
        filament state), triggers an unload sequence before selector homing.
        """
        if not self.requires_homing: return
        if self.check_if_bypass(): return

        with self.mmu.wrap_action(ACTION_HOMING):
            self.mmu.log_info("Homing MMU %s..." % self.mmu_unit.name)

            if force_unload is not None:
                self.mmu.log_debug("(asked to %s)" % ("force unload" if force_unload else "not unload"))

            if force_unload is True:
                # Forced unload case for recovery
                self.mmu.unload_sequence(check_state=True)

            elif (
                force_unload is None and
                self.mmu_unit.manages_gate(self.mmu.gate_selected)
                and self.mmu.filament_pos != FILAMENT_POS_UNLOADED
            ):
                # Automatic unload case
                self.mmu.unload_sequence()

            self._home_selector()


    def _select_gate(self, lgate):
        if lgate == TOOL_GATE_UNKNOWN: return
        if self.requires_homing and not self.is_homed:
            raise MmuError("Selector is not homed on %s" % self.mmu_unit.name)
        super()._select_gate(lgate)


    def check_if_bypass(self):
        """
        Similar to MMU controller check but localized to specific selector
        """
        return self.mmu_unit.manages_gate(self.mmu.gate_selected) and self.mmu.check_if_bypass()


    def check_if_loaded(self):
        """
        Similar to MMU controller check but localized to specific selector
        """
        return self.mmu_unit.manages_gate(self.mmu.gate_selected) and self.mmu.check_if_loaded()


    def get_mmu_status_config(self):
        msg =  super().get_mmu_status_config()
        if self.requires_homing:
            msg += " Selector is %s." % ("HOMED" if self.is_homed else "NOT HOMED")
        return msg



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

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

    def handle_ready(self):
        super().handle_ready()

    def handle_disconnect(self):
        super().handle_disconnect()

    def _select_gate(self, lgate):
        super()._select_gate(lgate)
        self.mmu_unit.mmu_toolhead.select_gear_stepper(lgate)



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

        if mmu.check_if_disabled(): return
        if self.check_if_loaded(): return

        if not mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            mmu.log_error("Operation not possible. Selector not yet calibrated")
            return

        loops = gcmd.get_int('LOOP', 10)
        servo = gcmd.get_int('SERVO', 0) # Legacy option, replaced by generic "GRIP"
        grip = bool(gcmd.get_int('GRIP', servo))
        home = bool(gcmd.get_int('HOME', 0))

        # Test and report using logical system-wide gate numbering (by design user never sees local gate numbers)
        min_gate, max_gate = mmu_unit.gate_range()
        mmu.log_always("Soak testing selector on %s (gates %d-%d) for %s iterations..." % (mmu_unit.name, min_gate, max_gate, loops))

        # We test fully by going through the MMU controller and not to the selector directly
        try:
            with mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    gate = random.randint(min_gate, max_gate)

                    if random.randint(0, 10) == 0 and home:
                        mmu.home_unit(mmu_unit)
                  
                    if random.randint(0, 10) == 0 and mmu_unit.has_bypass:
                        mmu.log_always("Testing loop %d / %d. Selecting bypass..." % (l + 1, loops))
                        mmu.select_gate(TOOL_GATE_BYPASS)
                    else:
                        mmu.log_always("Testing loop %d / %d. Selecting gate %d..." % (l + 1, loops, gate))
                        mmu.select_gate(gate)

                    if grip:
                        mmu.selector().filament_drive()
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
