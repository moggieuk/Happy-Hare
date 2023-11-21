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
    def __init__(self, config):
        super(MmuLedEffect, self).__init__(config)

        num_gates = 9 # PAUL
        for i in range(num_gates):
            new_section = self._add_config_section(config, config.get_name(), i)
            _ = ledEffect(new_section)

    def _add_config_section(self, config, section_from, index):
        section_to = section_from + "_%d" % index
        items = config.fileconfig.items(section_from)
        new_section = config.fileconfig.add_section(section_to)
        for item in items:
            if item[0] == "leds":
                new_leds = "%s (%d)" % (item[1], index + 1)
                config.fileconfig.set(section_to, item[0], new_leds)
            else:
                config.fileconfig.set(section_to, item[0], item[1])
        return config.getsection(section_to)

def load_config_prefix(config):
    return MmuLedEffect(config)
