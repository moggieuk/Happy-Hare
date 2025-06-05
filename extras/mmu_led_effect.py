# Happy Hare MMU Software
# Wrapper around led_effect klipper module to replicate any effect on entire strip as well
# as on each individual LED for per-gate effects. This relies on the [mmu_leds] section
# for each mmu_unit
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
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

        mmu_machine = self.printer.lookup_object('mmu_machine', None)
        if mmu_machine is None:
            raise config.error("[mmu_led_effect] requires [mmu_machine] to be loaded first")

        define_on_str = config.get('define_on', "").strip()
        _ = config.get('layers')
        for unit_index, mmu_unit in enumerate(mmu_machine.units):
            mmu_leds = self.printer.lookup_object('mmu_leds %s' % mmu_unit.name, None)
            if mmu_leds:
#PAUL                has_led_effects = mmu_leds.get_status().get('led_effect_module')
#PAUL                frame_rate = mmu_leds.get_status().get('default_frame_rate')
                has_led_effects = mmu_leds.led_effect_module
                frame_rate = mmu_leds.frame_rate
                define_on = [segment.strip() for segment in define_on_str.split(',') if segment.strip()]
                if define_on and not all(e in MmuLeds.SEGMENTS for e in define_on):
                    raise config.error("Unknown LED segment name specified in '%s'" % define_on_str)
                config.fileconfig.set(config.get_name(), 'frame_rate', config.get('frame_rate', frame_rate))
#PAUL                led_effect_section = "unit%d_%s" % (unit_index, config.get_name().split()[1])
                led_effect_section = config.get_name().split()[1]
    
                # This condition makes it a no-op if [mmu_leds] is not present or led_effects not installed
                if has_led_effects:
                    for segment in MmuLeds.SEGMENTS:
                        led_segment_name = "unit%d_mmu_%s_leds" % (unit_index, segment)
                        led_chain = self.printer.lookup_object(led_segment_name)
                        num_leds = led_chain.led_helper.led_count # PAUL could infer modulo (leds per gate)
    
                        if num_leds > 0:
                            # Full segment effects
                            if not define_on or segment in define_on:
                                section_to = "led_effect unit%d_%s_%s" % (unit_index, led_effect_section, segment)
                                logging.info("PAUL: add_led_effect(%s, %s)" % (section_to, led_segment_name))
                                self._add_led_effect(config, section_to, led_segment_name)
    
                            # Per gate
                            if segment in MmuLeds.PER_GATE_SEGMENTS and not define_on and segment != 'status':
#PAUL                                for idx in range(num_leds): # PAUL would have to be num_leds / num_gates
                                for idx in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                                    section_to = "led_effect %s_%s_%d" % (led_effect_section, segment, idx)
                                    logging.info("PAUL: add_led_effect(%s, %s (%d))" % (section_to, led_segment_name, idx - mmu_unit.first_gate + 1))
                                    self._add_led_effect(config, section_to, "%s (%d)" % (led_segment_name, idx - mmu_unit.first_gate + 1))

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
