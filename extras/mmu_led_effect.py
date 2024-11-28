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
from extras.mmu_leds import MmuLeds

class MmuLedEffect:

    def __init__(self, config):
        self.printer = config.get_printer()
        chains = MmuLeds.chains
        led_strip = MmuLeds.led_strip
        define_on_str = config.get('define_on', "").strip()
        define_on = [segment.strip() for segment in define_on_str.split(',') if segment.strip()]
        if define_on and not all(e in MmuLeds.SEGMENTS for e in define_on):
            raise config.error("Unknown LED segment name specified in '%s'" % define_on_str)
        config.fileconfig.set(config.get_name(), 'frame_rate', config.get('frame_rate', MmuLeds.frame_rate))
        _ = config.get('layers')
        led_effect_section = config.get_name()[4:]

        # This condition makes it a no-op if [mmu_leds] is not present or led_effects not installed
        if chains and MmuLeds.led_effect_module:
            for segment in MmuLeds.SEGMENTS:
                if chains[segment] and (not define_on or segment in define_on):
                    section_to = "%s_%s" % (led_effect_section, segment)
                    leds = "%s (%s)" % (led_strip, ",".join(map(str, chains[segment])))
                    self._add_led_effect(config, section_to, leds)
                if chains[segment] and not define_on and len(chains[segment]) > 1:
                    for idx in range(len(chains[segment])):
                        section_to = "%s_%s_%d" % (led_effect_section, segment, chains[segment][idx])
                        leds = "%s (%s)" % (led_strip, chains[segment][idx])
                        self._add_led_effect(config, section_to, leds)

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
