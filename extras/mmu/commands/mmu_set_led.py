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
from .mmu_base_command import *
from .mmu_misc_mixins  import LedMixin


class MmuSetLedCommand(LedMixin, BaseCommand):

    CMD = "MMU_SET_LED"

    HELP_BRIEF = "Raw direct control of MMU leds for temporary changes (normally you want to use MMU_LED)"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE          = #(int)\n"
        + "UNIT          = #(int)|_name_ Specify unit by name or number. OMIT if GATE supplied\n"
        + "EXIT_EFFECT   = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "ENTRY_EFFECT  = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "STATUS_EFFECT = [off|on|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "LOGO_EFFECT   = [off|r,g,b|_effect_]\n"
        + "DURATION      = #.#(float) seconds\n"
        + "FADETIME      = #.#(float) seconds\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} EXIT_EFFECT=mmu_ready_orange GATE=2 DURATION=5 ...Set the exit LED on gate 2 to orange effect for 5 seconds then revert\n"
        + f"{CMD} ENTRY_EFFECT=(1,1,1) GATE=4                    ...Set the entry LED on gate 4 to solid white until state change\n"
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

        gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
        if gate is not None:
            if gcmd.get("UNIT", None) is not None:
                raise gcmd.error("UNIT parameter is not required if GATE is specified")
            mmu_unit = mmu.mmu_unit(gate)
        else:
            mmu_unit = self.get_unit(gcmd, mode="optional")

        for u in ([mmu_unit] if mmu_unit is not None else mmu.mmu_machine.units):
        
            if not u.has_leds():
                mmu.log_error(f"No MMU LEDs configured on {u.name}")
                continue

            exit_effect = gcmd.get('EXIT_EFFECT', None)
            entry_effect = gcmd.get('ENTRY_EFFECT', None)
            status_effect = gcmd.get('STATUS_EFFECT', None)
            logo_effect = gcmd.get('LOGO_EFFECT', None)

            exit_effect = self._validate_effect(gcmd, "EXIT_EFFECT", u, exit_effect, self.EXIT_OPTIONS)
            entry_effect = self._validate_effect(gcmd, "ENTRY_EFFECT", u, entry_effect, self.ENTRY_OPTIONS)
            status_effect = self._validate_effect(gcmd, "STATUS_EFFECT", u, status_effect, self.STATUS_OPTIONS)
            logo_effect = self._validate_effect(gcmd, "LOGO_EFFECT", u, logo_effect, self.LOGO_OPTIONS)

            duration = gcmd.get_float('DURATION', None, minval=0)
            fadetime = gcmd.get_float('FADETIME', 1, minval=0)

            # Effect of None means no change
            mmu.led_manager._set_led(
                u.unit_index, gate,
                entry_effect=entry_effect,
                exit_effect=exit_effect,
                status_effect=status_effect,
                logo_effect=logo_effect,
                fadetime=fadetime,
                duration=duration
            )
