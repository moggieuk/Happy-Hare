# Happy Hare MMU Software
# Wrapper around led_effect klipper module to replicate any effect on entire strip as well
# as on each individual LED for per-gate effects. The implements the shared definition for
# intuitive configuration and minimising errors
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

class MmuLeds:

    SEGMENTS = ['exit', 'entry', 'status']

    # Shared by all [mmu_led_effect] definitions
    num_gates = None
    led_strip = None
    frame_rate = 24
    chains = {}
    led_effect_module = False

    def __init__(self, config):
        led_strip = config.get('led_strip')
        MmuLeds.num_gates = config.getint('num_gates')
        MmuLeds.frame_rate = config.getint('frame_rate', MmuLeds.frame_rate)

        if config.get_printer().lookup_object(led_strip.replace(':', ' '), None) is None:
            logging.warning("Happy Hare LED support cannot be loaded. Led strip '%s' not defined" % led_strip)
        else:
            try:
                pixels = config.get_printer().load_object(config, led_strip.replace(':', ' '))
                MmuLeds.led_strip = led_strip
            except Exception as e:
                raise config.error("Unable to load LED strip '%s': %s" % (led_strip, str(e)))

        indicies_used = set()
        try:
            for segment in self.SEGMENTS:
                MmuLeds.chains[segment] = None
                if segment == 'status':
                    sidx = config.getint("%s_index" % segment, None)
                    MmuLeds.chains[segment] = [sidx] if sidx else None
                else:
                    led_range = config.get("%s_range" % segment, None)
                    if led_range:
                        first, last = map(int, led_range.split('-'))
                        if abs(first - last) + 1 != MmuLeds.num_gates:
                            raise config.error("Range of '%s' LEDS doesn't match num_gates (%d)" % (segment, MmuLeds.num_gates))
                        MmuLeds.chains[segment] = list(range(first, last + 1) if first <= last else range(first, last - 1, -1))
                if MmuLeds.chains[segment]:
                    as_set = set(MmuLeds.chains[segment])
                    if indicies_used.isdisjoint(as_set):
                        indicies_used.update(as_set)
                    else:
                        raise config.error("Overlapping LED indicies")
        except Exception as e:
            raise config.error("Invalid 'mmu_leds' specification. Exception: %s" % str(e))

        # Lack of this module loading will make future mmu_led_effect definitions a no-op. This provides an easy way to disable
        if MmuLeds.led_strip is None:
            MmuLeds.chains = {}
        else:
            try:
                led_effects = config.get_printer().load_object(config, 'led_effect')
                MmuLeds.led_effect_module = True
            except Exception:
                MmuLeds.led_effect_module = False

def load_config(config):
    return MmuLeds(config)

