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

        self.is_homed = False
        self.mmu_toolhead = self.mmu_unit.mmu_toolhead # PAUL to be deprecated

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)

    def register_mux_command(self, cmd, func, desc=None):
        """
        Turn mux commands into simple commands if the printer has a single mmu_unit
        so the user doesn't have to supply the UNIT=x parameter to command
        """
        gcode = self.printer.lookup_object('gcode')
        if self.mmu_unit.mmu_machine.num_units > 1:
            gcode.register_mux_command(cmd, 'UNIT', str(self.mmu_unit.unit_index), func, desc=desc)
        else:
            self.register_command(cmd, func, desc=desc)

    def register_command(self, cmd, func, desc=None):
        """
        Safey to ensure that common commands are only registered once
        """
        gcode = self.printer.lookup_object('gcode')
        if cmd not in gcode.ready_gcode_handlers:
            gcode.register_command(cmd, func, desc=desc)

    def reinit(self):
        pass

    def handle_connect(self):
        logging.info("PAUL: handle_connect: BaseSelector")
        self.mmu = self.mmu_machine.mmu_controller # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager
        self.calibrator = self.mmu_unit.calibrator

    def handle_ready(self):
        logging.info("PAUL: handle_ready: BaseSelector")
        pass

    def handle_disconnect(self):
        logging.info("PAUL: handle_disconnect: BaseSelector")
        pass

    def bootup(self):
        pass

    def home(self, force_unload = None):
        pass

    def select_gate(self, gate):
        pass

    def restore_gate(self, gate):
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
        return self.mmu_unit.has_bypass

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
        return []

    # Convert gate number to relative gate on mmu_unit
    def local_gate(self, gate):
        """
        Convert an absolute gate number to a local gate index for this unit.

        Returns the gate index relative to the unit's first_gate and logs
        an informational message for debugging.
        """
        if gate < 0: return gate

        local_gate = gate - self.mmu_unit.first_gate
        self.mmu.log_error("PAUL: local_gate(%s) on unit %s=%s" % (gate, self.mmu_unit.name, local_gate))
        return local_gate



class PhysicalSelector(BaseSelector, object):
    """
    Base class for selectors that involve movement.

    Provides common functionality used by physical selector implementations
    including a soak test command for exercising selector movement.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)

        # Register GCODE commands
        self.register_mux_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc=self.cmd_MMU_SOAKTEST_SELECTOR_help)

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    cmd_MMU_SOAKTEST_SELECTOR_param_help = (
        "MMU_SOAKTEST_SELECTOR: %s\n" % cmd_MMU_SOAKTEST_SELECTOR_help
        + "UNIT  = #(int) Optional if only one unit fitter to printer\n"
        + "LOOP  = #(int) Test loops\n"
        + "GRIP  = [0|1]  Force filament gripping after selection where optional\n"
        + "HOME  = [0|1]  Randomized homing\n"
    )
    cmd_MMU_SOAKTEST_SELECTOR_supplement_help = (
        "Examples:\n"
        + "MMU_SOAKTEST_SELECTOR UNIT=1 LOOP=1000 ...make 1000 gate selections on unit 1\n"
        + "MMU_SOAKTEST_SELECTOR HOME=1 ...randomly home whilst testing selection on current unit\n"
        + "MMU_SOAKTEST_SELECTOR GRIP=1 ...force filament grip after selection (where servo/gripping available)"
    )

    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        """
        Run a soak test exercising selector movement across random gates.

        The command supports unit selection, loop count, optional gripping
        and randomized homing. Errors from the MMU are handled and cause
        the soak test to abort cleanly.
        """
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu_unit.manages_gate(self.mmu.current_gate) and self.mmu.check_if_loaded(): return

        if not self.mmu_unit.calibrator.check_calibrated(CALIBRATED_SELECTOR):
            self.mmu.log_error("Operation not possible. Selector not yet calibrated")
            return

        unit = gcmd.get_int('UNIT', None, minval=0, maxval=self.mmu.mmu_machine.num_units - 1)
        loops = gcmd.get_int('LOOP', 100)
        servo = gcmd.get_int('SERVO', 0) # Legacy option
        grip = bool(gcmd.get_int('GRIP', servo))
        home = bool(gcmd.get_int('HOME', 0))

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SOAKTEST_SELECTOR_param_help, self.cmd_MMU_SOAKTEST_SELECTOR_supplement_help), color=True)
            return

        self.mmu.log_info("PAUL: unit=%s" % unit)
        return # PAUL

        try:
            mmu_unit = self.mmu_machine.get_mmu_unit_by_index(unit) if unit is not None else self.mmu_unit
            min_gate, max_gate = mmu_unit.gate_range()

            with self.mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    gate = random.randint(min_gate, max_gate - 1)

                    if random.randint(0, 10) == 0 and home:
                        mmu_unit.selector.home()
                  
                    if random.randint(0, 10) == 0 and mmu_unit.has_bypass:
                        self.mmu.log_always("Testing loop %d / %d. Selecting bypass..." % (l + 1, loops))
                        mmu_unit.selector.select_bypass()
                    else:
                        self.mmu.log_always("Testing loop %d / %d. Selecting gate %d..." % (l + 1, loops, gate))
                        mmu_unit.selector.select_gate()

                    if grip:
                        mmu_unit.selector.filament_drive()
        except MmuError as ee:
            self.mmu.handle_mmu_error("Soaktest abandoned because of error: %s" % str(ee))



class VirtualSelector(BaseSelector):
    """
    Selector implementation for type-B MMUs which use a gear driver per gate.

    This virtual selector uses a gear stepper on the toolhead and does not
    require physical homing because it selects gears rather than moving a
    selector carriage.
    """

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.is_homed = True

    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)

    def select_gate(self, gate):
        super().select_gate(gate)
        if gate == self.mmu.gate_selected: return
        self.mmu_unit.mmu_toolhead.select_gear_stepper(self.mmu_unit.local_gate(gate))

    def restore_gate(self, gate):
        super().restore_gate(gate)
        self.mmu_unit.mmu_toolhead.select_gear_stepper(self.mmu_unit.local_gate(gate))
