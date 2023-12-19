# Happy Hare MMU Software
# Wrapper around led_effect klipper module to replicate any effect on entire strip as well
# as on each individual LED for per-gate effects
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
from extras.led_effect import ledEffect

class MmuLedEffect(ledEffect, object):

    first_led_index = None # Starting LED index (must be the same and also used for configuring the LED macro)
    last_led_index = None

    def __init__(self, config):
        self.printer = config.get_printer()
        leds = config.get('leds').split(' ', 1)[0]
        strip = leds.replace(':', ' ')
        first, last = map(int, config.get('leds').split(' ', 1)[1].strip('()').split('-'))
        if not MmuLedEffect.first_led_index:
            MmuLedEffect.first_led_index = first
        elif first != MmuLedEffect.first_led_index:
            raise config.error("First led index '%d' differs from others in the config (%d)" % (first, MmuLedEffect.first_led_index))
        if not MmuLedEffect.last_led_index:
            MmuLedEffect.last_led_index = last
        elif last != MmuLedEffect.last_led_index:
            logging.warning("mmu_led_effect: last led index '%d' differs from others in the config (%d)" % (last, MmuLedEffect.last_led_index))

        pixels = self.printer.lookup_object(strip)

        # Reduce led range by one to separate control of gate effects from exit effect
        new_leds = "%s (%d-%d)" % (leds, first, last - 1)
        config.fileconfig.set(config.get_name(), 'leds', new_leds)

        # Create the combined gate_effects effect
        super(MmuLedEffect, self).__init__(config)

        # Create individual effects for gate and exit leds
        for i in range(first, last + 1):
            new_section = self._add_config_section(config, config.get_name(), i)
            _ = ledEffect(new_section)

    def _add_config_section(self, config, section_from, index):
        section_to = section_from + "_%d" % index
        items = config.fileconfig.items(section_from)
        new_section = config.fileconfig.add_section(section_to)
        for item in items:
            if item[0] == 'leds':
                new_leds = "%s (%d)" % (item[1].split(' ', 1)[0], index)
                config.fileconfig.set(section_to, item[0], new_leds)
            else:
                config.fileconfig.set(section_to, item[0], item[1])
        return config.getsection(section_to)

def load_config_prefix(config):
    return MmuLedEffect(config)
