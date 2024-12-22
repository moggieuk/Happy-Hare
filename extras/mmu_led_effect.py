# Happy Hare MMU Software
# Wrapper around led_effect klipper module to replicate any effect on entire strip as well
# as on each individual LED for per-gate effects. This relies on a previous shared
# [mmu_leds] section for the shared part of the config
#
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Klipper imports
from .mmu_leds import MmuLeds

class MmuLedEffect:

    def __init__(self, config):
        self.printer = config.get_printer()
        mmu_leds = self.printer.lookup_object('mmu_leds', None)
        leds_configured = mmu_leds.get_status().get('leds_configured') if mmu_leds else False
        has_led_effects = mmu_leds.get_status().get('led_effect_module') if mmu_leds else False
        frame_rate = mmu_leds.get_status().get('default_frame_rate') if mmu_leds else 24

        if leds_configured:
            define_on_str = config.get('define_on', "").strip()
            define_on = [segment.strip() for segment in define_on_str.split(',') if segment.strip()]
            if define_on and not all(e in MmuLeds.SEGMENTS for e in define_on):
                raise config.error("Unknown LED segment name specified in '%s'" % define_on_str)
            config.fileconfig.set(config.get_name(), 'frame_rate', config.get('frame_rate', frame_rate))
            _ = config.get('layers')
            led_effect_section = config.get_name()[4:] # Remove "mmu_"

            # This condition makes it a no-op if [mmu_leds] is not present or led_effects not installed
            if has_led_effects:
                for segment in MmuLeds.SEGMENTS:
                    led_segment_name = "mmu_%s_leds" % segment
                    led_chain = self.printer.lookup_object("mmu_%s_leds" % segment)
                    num_leds = led_chain.led_helper.led_count

                    if num_leds > 0:
                        # Full segment effects
                        if not define_on or segment in define_on:
                            section_to = "%s_%s" % (led_effect_section, segment)
                            self._add_led_effect(config, section_to, led_segment_name)

                        # Per gate
                        if segment in MmuLeds.PER_GATE_SEGMENTS and not define_on and segment != 'status':
                            for idx in range(num_leds):
                                section_to = "%s_%s_%d" % (led_effect_section, segment, idx + 1)
                                self._add_led_effect(config, section_to, "%s (%d)" % (led_segment_name, idx + 1))

    def _add_led_effect(self, config, section_to, leds):
        config.fileconfig.add_section(section_to)
        config.fileconfig.set(section_to, 'leds', leds)
        items = config.fileconfig.items(config.get_name())
        for item in (i for i in items if i[0] != 'define_on'):
            config.fileconfig.set(section_to, item[0], item[1])
        new_object = config.getsection(section_to)
        try:
            _ = self.printer.load_object(config, new_object.get_name())
        except Exception as e:
            raise config.error("Unable to create led effect`. It is likely you don't have the 'led_effect' klipper module installed. Exception: %s" % str(e))

def load_config_prefix(config):
    return MmuLedEffect(config)
