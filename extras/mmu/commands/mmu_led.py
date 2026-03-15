# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_LED command
#  - This is a "per-unit" command
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


class MmuLedCommand(BaseCommand):

    CMD = "MMU_LED"

    HELP_BRIEF = "Manage mode of operation of optional MMU LED's"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT          = #(int)|_name_|ALL Specify unit by name, number or all-units (optional if single unit)\n"
        + "ENABLE        = [0|1] Enable/disable\n"
        + "ANIMATION     = [0|1] Enable/disable animations\n"
        + "EXIT_EFFECT   = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "ENTRY_EFFECT  = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "STATUS_EFFECT = [off|on|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "LOGO_EFFECT   = [off|r,g,b|_effect_]\n"
        + "REFRESH       = [0|1] Force refresh of LED\n"
        + "QUIET         = [0|1] Don't report non-essential status\n"
        + "(no parameters for status report)\n"
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
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))

        if not mmu_unit.has_leds():
            mmu.log_error("No MMU LEDs configured on %s" % unit.name)
            return

        msg = ""
        led_manager = mmu.led_manager
        leds = mmu_unit.leds
        unit = mmu_unit.unit_index

        if leds:
            exit_effect = gcmd.get('EXIT_EFFECT', leds.exit_effect)
            entry_effect = gcmd.get('ENTRY_EFFECT', leds.entry_effect)
            status_effect = gcmd.get('STATUS_EFFECT', leds.status_effect)
            logo_effect = gcmd.get('LOGO_EFFECT', leds.logo_effect)
            enabled = bool(gcmd.get_int('ENABLE', leds.enabled, minval=0, maxval=1))
            animation = bool(gcmd.get_int('ANIMATION', leds.animation, minval=0, maxval=1))

            if leds.enabled and not enabled or refresh:
                # Enabled to disabled or refresh
                led_manager._set_led(
                    mmu_unit.unit_index, None,
                    exit_effect='off',
                    entry_effect='off',
                    status_effect='off',
                    logo_effect='off'
                )
            else:
                if leds.animation and not animation:
                    # Turning animation off so clear existing effects
                    led_manager._set_led(
                        mmu_unit.unit_index, None,
                        exit_effect='off',
                        entry_effect='off',
                        status_effect='off',
                        logo_effect='off',
                        fadetime=0
                    )

            if (leds.exit_effect != exit_effect or
                leds.entry_effect != entry_effect or
                leds.status_effect != status_effect or
                leds.logo_effect != logo_effect or
                leds.enabled != enabled or
                leds.animation != animation or
                refresh):

                leds.exit_effect = exit_effect
                leds.entry_effect = entry_effect
                leds.status_effect = status_effect
                leds.logo_effect = logo_effect
                leds.enabled = enabled
                leds.animation = animation

                if enabled:
                    led_manager._set_led(
                        mmu_unit.unit_index, None,
                        exit_effect='default',
                        entry_effect='default',
                        status_effect='default',
                        logo_effect='default'
                    )

            if not quiet:
                available = lambda effect, enabled : ("'%s'" % str(effect)) if enabled else "unavailable"
                msg += "\nUnit %s LEDs (%s)\n" % (mmu_unit.unit_index, ("enabled" if enabled else "disabled"))
                msg += "  Animation: %s\n" % ("enabled" if animation else "disabled")
                msg += "  Exit effect: %s\n" % available(exit_effect, leds.get_status()['exit'])
                msg += "  Entry effect: %s\n" % available(entry_effect, leds.get_status()['entry'])
                msg += "  Status effect: %s\n" % available(status_effect, leds.get_status()['status'])
                msg += "  Logo effect: %s\n" % available(logo_effect, leds.get_status()['logo'])

        mmu.log_always(msg)
