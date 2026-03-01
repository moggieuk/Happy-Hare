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

    # Ensure that shared commands are only registered once
    def register_command(self, cmd, func, desc=None):
        gcode = self.printer.lookup_object('gcode')
        if cmd not in gcode.ready_gcode_handlers:
            gcode.register_command(cmd, func, desc=desc)

    # Turn mux commands into simple commands if the MMU has a single mmu_unit
    def register_mux_command(self, cmd, func, desc=None):
        gcode = self.printer.lookup_object('gcode')
        if self.mmu_unit.mmu_machine.num_units > 1:
            gcode.register_mux_command(cmd, 'UNIT', str(self.mmu_unit.unit_index), func, desc=desc)
        else:
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
        self.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc=self.cmd_MMU_SOAKTEST_SELECTOR_help)

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    cmd_MMU_SOAKTEST_SELECTOR_param_help = (
        "MMU_SOAKTEST_SELECTOR: %s\n" % cmd_MMU_SOAKTEST_SELECTOR_help
        + "UNIT  = #(int) Optional, defaults to all units\n"
        + "LOOP  = #(int) Test loops\n"
        + "GRIP  = [0|1]  Force filament gripping after selection where optional\n"
        + "HOME  = [0|1]  Randomized homing\n"
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
        if self.mmu.check_if_loaded(): return

        if self.calibrator.check_if_not_calibrated(CALIBRATED_SELECTOR): return

        show_help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        unit = gcmd.get_int('UNIT', None, minval=0, maxval=self.mmu.mmu_machine.num_units - 1) # PAUL unit!
        loops = gcmd.get_int('LOOP', 100)
        servo = bool(gcmd.get_int('SERVO', 0)) # Legacy option
        grip = bool(gcmd.get_int('GRIP', servo))
        home = bool(gcmd.get_int('HOME', 0))

        if show_help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SOAKTEST_SELECTOR_param_help), color=True)
            return

        try:
            min_gate = 0
            max_gate = self.mmu.num_gates
            if unit is not None:
                min_gate = self.mmu.mmu_machine.units[unit].first_gate
                max_gate = min_gate + self.mmu_unit.num_gates

            with self.mmu.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    gate = random.randint(min_gate, max_gate - 1)
                    mmu_unit = self.mmu.mmu_machine.getmmu_unit_by_gate(gate)

                    if random.randint(0, 10) == 0 and home:
                        self.mmu.home()
                  
                    if random.randint(0, 10) == 0 and mmu_unit.has_bypass:
                        self.mmu.log_always("Testing loop %d / %d. Selecting bypass..." % (l + 1, loops))
                        self.mmu.select_bypass()
                    else:
                        self.mmu.log_always("Testing loop %d / %d. Selecting gate %d..." % (l + 1, loops, gate))
                        self.mmu.select_gate(gate)
                    if grip:
                        self.filament_drive()
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
        self.mmu_toolhead.select_gear_stepper(self.local_gate(gate))

    def restore_gate(self, gate):
        super().restore_gate(gate)
        self.mmu_toolhead.select_gear_stepper(self.local_gate(gate))
