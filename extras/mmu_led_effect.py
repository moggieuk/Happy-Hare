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

class MmuLedEffect:

    first_led_index = None # Starting LED index (must be the same and also used for configuring the LED macro)
    last_led_index = None

    def __init__(self, config):
        self.printer = config.get_printer()
        try:
            leds = config.get('leds').split(' ', 1)[0]
            strip = leds.replace(':', ' ')
            first, last = map(int, config.get('leds').split(' ', 1)[1].strip('()').split('-'))
        except Exception as e:
            raise config.error("Invalid 'leds' specification: %s. Exception: %s" % (config.get('leds'), str(e)))
        
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
        section_to = config.get_name()[4:]
        config.fileconfig.add_section(section_to)
        new_section = config.getsection(section_to)
        items = config.fileconfig.items(config.get_name())
        for item in items:
            _ = config.get(item[0])
            config.fileconfig.set(section_to, item[0], item[1])
        try:
            _ = self.printer.load_object(config, new_section.get_name())
        except Exception as e:
            raise config.error("Unable to create led effect`. It is likely you don't have the 'led_effect' klipper module installed. Exception: %s" % str(e))

        # Create individual effects for gate and exit leds
        for i in range(first, last + 1):
            new_section = self._add_config_section(config, config.get_name(), i)
            _ = self.printer.load_object(config, new_section.get_name())

    def _add_config_section(self, config, section_from, index):
        section_to = section_from[4:] + "_%d" % index
        items = config.fileconfig.items(section_from)
        config.fileconfig.add_section(section_to)
        for item in items:
            if item[0] == 'leds':
                new_leds = "%s (%d)" % (item[1].split(' ', 1)[0], index)
                config.fileconfig.set(section_to, item[0], new_leds)
            else:
                config.fileconfig.set(section_to, item[0], item[1])
        return config.getsection(section_to)

def load_config_prefix(config):
    return MmuLedEffect(config)
