# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SET_LED command
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
from .mmu_base_command import *


class MmuSetLedCommand(BaseCommand):

    CMD = "MMU_SET_LED"

    HELP_BRIEF = "Directly control MMU leds"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "GATE          = #(int)\n"
        + "EXIT_EFFECT   = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "ENTRY_EFFECT  = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "STATUS_EFFECT = [off|on|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "LOGO_EFFECT   = [off|r,g,b|_effect_]\n"
        + "DURATION      = #.#(float) seconds\n"
        + "FADETIME      = #.#(float) seconds\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
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
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.mmu_machine.num_gates - 1)
        exit_effect = gcmd.get('EXIT_EFFECT', None)
        entry_effect = gcmd.get('ENTRY_EFFECT', None)
        status_effect = gcmd.get('STATUS_EFFECT', None)
        logo_effect = gcmd.get('LOGO_EFFECT', None)
        duration = gcmd.get_float('DURATION', None, minval=0)
        fadetime = gcmd.get_float('FADETIME', 1, minval=0)

        # Led manager works with unit index so derive from gate number
        mmu_unit = mmu.mmu_machine.get_mmu_unit_by_gate(gate)
        led_manager = mmu.led_manager

        if not mmu_unit.has_leds():
            mmu.log_error("No MMU LEDs configured on %d" % mmu_unit.name)
            return

        led_manager._set_led(
            mmu_unit.unit_index, gate,
            entry_effect=entry_effect,
            exit_effect=exit_effect,
            status_effect=status_effect,
            logo_effect=logo_effect,
            fadetime=fadetime,
            duration=duration
        )
