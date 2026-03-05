# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ESPOOLER command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import BaseCommand


class MmuEspoolerCommand(BaseCommand):

    CMD = "MMU_ESPOOLER"

    HELP_BRIEF = "Direct control of espooler or display of current status"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "ALLOFF = [0|1] Quick way to turn all espoolers off\n"
        + "TRIGGER = [0|1] Fire in-print trigger for testing\n"
        + "BURST = [0|1] Jog in direction of OPERATION (assist|rewind) using configured burst duration and power\n"
        + "DURATION = [0-10] Override duration of PWM signal (seconds) for burst operations\n"
        + "GATE = g Specify gate to operate on (defaults to current gate)\n"
        + "LOOSEN = [0|1] Quick way to loosen filament on spool\n"
        + "OPERATION = [assist|off|print|rewind] Set espooler operation mode\n"
        + "POWER = [0-100] Override default % power to apply to espooler motor\n"
        + "QUIET = [0|1] Used to suppress console/log output\n"
        + "RESET = [0|1] Turn of in-print assist\n"
        + "TIGHTEN = [0|1] Quick way to tighten filament on spool\n"
        + "(no parameters for status report)"
    )
    HELP_SUPPLEMENT = (
        ""  # add additional examples here if/when desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.mmu.check_if_disabled(): return
        if self.mmu._check_has_espooler(): return

        operation = gcmd.get('OPERATION', None)
        burst = gcmd.get_int('BURST', 0, minval=0, maxval=1)
        tighten = gcmd.get_int('TIGHTEN', 0, minval=0, maxval=1)
        loosen = gcmd.get_int('LOOSEN', 0, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        alloff = bool(gcmd.get_int('ALLOFF', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        trigger = bool(gcmd.get_int('TRIGGER', 0, minval=0, maxval=1))
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu.num_gates - 1)

        if reset:
            # Turn off in-print assist mode
            self.mmu.espooler.reset_print_assist_mode()

        if trigger:
            # Mimick in-print assist trigger
            # No gate specified = similar to extruder movement
            # With gate specified = similar to filament tension trigger
            self.mmu.espooler.advance(gate)

        if alloff:
            for gate in range(self.mmu.num_gates):
                self.mmu.espooler.set_operation(gate, 0, self.mmu.ESPOOLER_OFF)

        elif tighten or loosen:
            if gate is None:
                gate = self.mmu.gate_selected
            if gate < 0:
                raise gcmd.error("Invalid gate")

            power = self.mmu.mmu_unit().p.espooler_assist_burst_power if loosen else self.mmu.mmu_unit().p.espooler_rewind_burst_power
            duration = self.mmu.mmu_unit().p.espooler_assist_burst_duration if loosen else self.mmu.mmu_unit().p.espooler_rewind_burst_duration
            operation = self.mmu.ESPOOLER_ASSIST if loosen else self.mmu.ESPOOLER_REWIND
            self.mmu.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, operation)

        elif operation is not None:
            operation = operation.lower()

            if gate is None:
                gate = self.mmu.gate_selected
            if gate < 0:
                raise gcmd.error("Invalid gate")

            # Determine power
            if burst:
                default_power = self.mmu.mmu_unit().p.espooler_assist_burst_power if operation == self.mmu.ESPOOLER_ASSIST else self.mmu.mmu_unit().p.espooler_rewind_burst_power
            else:
                default_power = self.mmu.mmu_unit().p.espooler_printing_power if operation == self.mmu.ESPOOLER_PRINT else 50
            power = gcmd.get_int('POWER', default_power, minval=0, maxval=100) if operation != self.mmu.ESPOOLER_OFF else 0

            if burst:
                default_duration = self.mmu.mmu_unit().p.espooler_assist_burst_duration if operation == self.mmu.ESPOOLER_ASSIST else self.mmu.mmu_unit().p.espooler_rewind_burst_duration
                duration = gcmd.get_float('DURATION', default_duration, above=0., maxval=10.)

                if operation in [self.mmu.ESPOOLER_ASSIST, self.mmu.ESPOOLER_REWIND]:
                    self.mmu.log_info("Espooler burst on gate %d for %.1fs at %d%% power in %s direction" % (gate, duration, power, operation))
                    self.mmu.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, operation)
                else:
                    self.mmu.log_error("Must specify 'assist' or 'rewind' operation for burst")

            elif operation not in self.mmu.ESPOOLER_OPERATIONS:
                raise gcmd.error("Invalid operation. Options are: %s" % ", ".join(self.mmu.ESPOOLER_OPERATIONS))

            elif operation == self.mmu.ESPOOLER_PRINT:
                if self.mmu.is_printing():
                    self.mmu.log_warning("Cannot set in-print assist mode for non selected gate while printing")
                else:
                    if gate != self.mmu.gate_selected:
                        self.mmu.log_warning("In-print assist mode set for non selected gate - for testing only")
                    self.mmu.espooler.set_operation(gate, power / 100, self.mmu.ESPOOLER_PRINT)

            elif operation != self.mmu.ESPOOLER_OFF:
                self.mmu.espooler.set_operation(gate, power / 100, operation)
            else:
                self.mmu.espooler.set_operation(gate, 0, self.mmu.ESPOOLER_OFF)

        if not quiet:
            msg = ""
            for gate in range(self.mmu.num_gates):
                if msg:
                    msg += "\n"
                msg += "{}".format(gate).ljust(2, self.mmu.UI_SPACE) + ": "
                if self.mmu.has_espooler():
                    operation, value = self.mmu.espooler.get_operation(gate)
                    burst = ""
                    if operation == self.mmu.ESPOOLER_PRINT and value == 0:
                        burst = " [assist for %.1fs at %d%% power " % (self.mmu.mmu_unit().p.espooler_assist_burst_duration, self.mmu.mmu_unit().p.espooler_assist_burst_power)
                        if self.mmu.mmu_unit().p.espooler_assist_burst_trigger:
                            burst += "on trigger, max %d bursts]" % self.mmu.mmu_unit().p.espooler_assist_burst_trigger_max
                        else:
                            burst += "every %.1fmm of extruder movement]" % self.mmu.mmu_unit().p.espooler_assist_extruder_move_length
                    msg += "{}".format(operation).ljust(7, self.mmu.UI_SPACE) + " (%d%%)%s" % (round(value * 100), burst)
                else:
                    msg += "not fitted"
            self.mmu.log_always(msg)
