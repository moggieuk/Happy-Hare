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
from ...mmu_constants import *
from ...mmu_utils     import MmuError
from ...commands      import register_command
from ..mmu_calibrator import CALIBRATED_SELECTOR


class BaseSelector:
    """
    Base class for all selectors.

    Provides the expected contract with the mmu_controller and basic
    plumbing used by selector implementations.
    """

    def __init__(self, config, mmu_unit, params):
        logging.info("PAUL: init() for BaseSelector")
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()

        self.is_homed = False                   # Whether selector is home and knows current position
        self.requires_homing = True             # Whether selector requires homing
        self.local_gate_selected = None		# Local gate selected # PAUL complete me on all selectors!
        self.mmu_toolhead = self.mmu_unit.mmu_toolhead # PAUL to be deprecated

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)

    # Prevent overriding of methods with physical gate number as parameter
    # It is important and all selector logic works with local gates
    _final_methods = {"select_gate", "restore_gate"}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        overridden = BaseSelector._final_methods.intersection(cls.__dict__.keys())
        if overridden:
            raise TypeError(
                f"{cls.__name__} is not allowed to override: {', '.join(sorted(overridden))}"
            )

    def handle_connect(self):
        logging.info("PAUL: =========== handle_connect: BaseSelector")
        self.mmu = self.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager
        self.calibrator = self.mmu_unit.calibrator

    def handle_ready(self):
        logging.info("PAUL: handle_ready: BaseSelector")
        pass

    def handle_disconnect(self):
        logging.info("PAUL: handle_disconnect: BaseSelector")
        pass

    def bootup(self): # PAUL why do we need this?
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
        self.local_gate_selected = lgate

    def _select_gate(self, lgate):
        pass

    def restore_gate(self, gate): # PAUL maybe we can remove this and leverage select_gate() in the future?
        """
        Correct gate position of selector without checks. Used in state restoration
        Don't override this method, instead override _restore_gate() after the local gate translation.
        """
        lgate = self._local_gate(gate)
        self._restore_gate(lgate)
        self.local_gate_selected = lgate

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

    def disable_motors(self):
        pass

    def enable_motors(self):
        pass

    def buzz_motor(self, motor):
        return False

    def has_bypass(self):
        return self.mmu_unit.has_bypass # PAUL shouldn't this also be on selector? like selects bypass?

    def get_status(self, eventtime):
        return {
            'has_bypass': self.has_bypass()
        }

    def get_mmu_status_config(self):
        return "Selector Type: %s" % self.__class__.__name__

    def set_test_config(self, gcmd):
        pass

    def get_test_config(self):
        return ""

    def check_test_config(self, param):
        return True

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
        super().handle_connect() # PAUL
        logging.info("PAUL: =========== handle_connect: PhysicalSelector")

    def handle_ready(self):
        super().handle_ready() # PAUL
        logging.info("PAUL: =========== handle_ready: PhysicalSelector")

    def handle_disconnect(self):
        super().handle_disconnect() # PAUL
        logging.info("PAUL: =========== handle_disconnect: PhysicalSelector")

    def _select_gate(self, lgate):
        if lgate == TOOL_GATE_UNKNOWN: return
        if not self.is_homed: # PAUL new clause
            raise MmuError("Selector is not homed on %s" % self.mmu_unit.name)
        super()._select_gate(lgate)

    def _restore_gate(self, lgate):
        super()._restore_gate(lgate)



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
        logging.info("PAUL: =========== handle_connect: VirtualSelector")
        self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

    def handle_ready(self):
        super().handle_ready() # PAUL
        logging.info("PAUL: =========== handle_ready: VirtualSelector")

    def handle_disconnect(self):
        super().handle_disconnect() # PAUL
        logging.info("PAUL: =========== handle_disconnect: VirtualSelector")

    def _select_gate(self, lgate):
        super()._select_gate(lgate)
        self.mmu_unit.mmu_toolhead.select_gear_stepper(lgate)

    def _restore_gate(self, lgate):
        super()._restore_gate(lgate)
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
        + "LOOP  = #(int) Test loops\n"
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

        if self.mmu.check_if_disabled(): return
        if self.mmu_unit.calibrator.manages_gate(self.mmu.gate_selected) and self.mmu.check_if_loaded(): return

        if not self.mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            self.mmu.log_error("Operation not possible. Selector not yet calibrated")
            return

        loops = gcmd.get_int('LOOP', 100)
        servo = gcmd.get_int('SERVO', 0) # Legacy option
        grip = bool(gcmd.get_int('GRIP', servo))
        home = bool(gcmd.get_int('HOME', 0))

        # Test and report using logical system-wide gate numbering (by design user never sees local gate numbers)
        min_gate, max_gate = mmu_unit.gate_range()
        self.mmu.log_always("Soak testing selector on %s (gates %d-%d) for %s iterations..." % (mmu_unit.name, min_gate, max_gate, loops))

        return # PAUL testing
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    gate = random.randint(min_gate, max_gate - 1)

                    if random.randint(0, 10) == 0 and home:
                        mmu_unit.selector.home()
                  
                    if random.randint(0, 10) == 0 and mmu_unit.has_bypass:
                        self.mmu.log_always("Testing loop %d / %d. Selecting bypass" % (l + 1, loops))
                        mmu_unit.selector.select_bypass()
                    else:
                        self.mmu.log_always("Testing loop %d / %d. Selecting gate %d" % (l + 1, loops, gate))
                        mmu_unit.selector.select_gate(gate)

                    if grip:
                        mmu_unit.selector.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))



# -----------------------------------------------------------------------------------------------------------
# MMU_GRIP command
#  This "registered command" will be conditionally registered in PhysicalSelector, then instantiated later
#  by the main mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

class MmuGripCommand(BaseCommand):

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

        gate = self.mmu.gate_selected
        mmu_unit = self.mmu.mmu_unit(gate)

        if gate >= 0:
            mmu_unit.selector.filament_drive()



# -----------------------------------------------------------------------------------------------------------
# MMU_RELEASE command
#  This "registered command" will be conditionally registered in PhysicalSelector, then instantiated later
#  by the main mmu_controller module when commands are loaded
# -----------------------------------------------------------------------------------------------------------

class MmuReleaseCommand(BaseCommand):

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

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        gate = self.mmu.gate_selected
        mmu_unit = self.mmu.mmu_unit(gate)

        if gate >= 0:
            if not mmu_unit.filament_always_gripped:
                mmu_unit.selector.filament_release()
            else:
                self.mmu.log_error("Selector configured to not allow filament release")
