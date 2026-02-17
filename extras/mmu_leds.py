# Happy Hare MMU Software
#
# Allows for flexible creation of virtual leds chains - one for each of the supported
# segments (exit, entry, status). Entry and exit are indexed by gate number.
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, re

# Klipper imports
from . import led as klipper_led

class VirtualMmuLedChain:
    def __init__(self, config, unit_name, segment, config_chains):
        self.printer = config.get_printer()
        self.name = "%s_mmu_%s_leds" % (unit_name, segment)
        self.config_chains = config_chains

        # Create temporary config section just to access led helper
        led_section = "led %s" % self.name
        config.fileconfig.add_section(led_section)
        led_config = config.getsection(led_section)
        self.led_helper = klipper_led.LEDHelper(led_config, self.update_leds, sum(len(leds) for chain_name, leds in config_chains))
        config.fileconfig.remove_section(led_section)

        # We need to configure the chain now so we can validate
        self.leds = []
        for chain_name, leds in self.config_chains:
            try:
                chain = self.printer.load_object(config, chain_name)
                if chain:
                    for led in leds:
                        self.leds.append((chain, led))
            except Exception as e:
                raise config.error("MMU LED chain '%s' referenced in '%s' cannot be loaded:\n%s" % (chain_name, self.name, str(e)))

        # Register led object with klipper
        logging.info("MMU: Created: %s" % led_section)
        self.printer.add_object(self.name, self)

    def update_leds(self, led_state, print_time):
        chains_to_update = set()
        for color, (chain, led) in zip(led_state, self.leds):
            chain.led_helper.led_state[led] = color
            chains_to_update.add(chain)
        for chain in chains_to_update:
            chain.led_helper.need_transmit = True
            if hasattr(chain.led_helper, '_check_transmit'):
                chain.led_helper._check_transmit() # New klipper
            else:
                chain.led_helper.check_transmit(None)  # Older klipper / Kalico

    def get_status(self, eventtime=None):
        state = []
        chain_status = {}
        for chain, led in self.leds:
            if chain not in chain_status:
                status = chain.led_helper.get_status(eventtime)['color_data']
                chain_status[chain] = status
            state.append(chain_status[chain][led])
        return {"color_data": state}


class MmuLeds:

    PER_GATE_SEGMENTS = ['exit', 'entry']
    SEGMENTS = PER_GATE_SEGMENTS + ['status', 'logo']

    def __init__(self, config, *args):
        if len(args) < 2:
            raise config.error("[%s] cannot be instantiated directly. It must be loaded by [mmu_unit]" % config.get_name())
        self.mmu_machine, self.mmu_unit, self.first_gate, self.num_gates = args
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.frame_rate = config.getint('frame_rate', 24)

        # Create virtual led chains
        self.virtual_chains = {}
        for segment in self.SEGMENTS:
            name = "%s_leds" % segment
            config_chains = [self.parse_chain(line) for line in config.get(name, '').split('\n') if line.strip()]
            self.virtual_chains[segment] = VirtualMmuLedChain(config, "unit%d" % self.mmu_unit.unit_index, segment, config_chains)

            num_leds = len(self.virtual_chains[segment].leds)
            if segment in self.PER_GATE_SEGMENTS and num_leds > 0 and num_leds % self.num_gates:
                raise config.error("Number of MMU '%s' LEDs (%d) cannot be spread over num_gates (%d)" % (segment, num_leds, self.num_gates))

        # Check for LED chain overlap or unavailable LEDs
        used = {}
        for segment in self.SEGMENTS:
            for led in self.virtual_chains[segment].leds:
                obj, index = led
                if index >= obj.led_helper.led_count:
                    raise config.error("MMU LED (with index %d) on segment %s isn't available" % (index + 1, segment))
                if led in used:
                    raise config.error("Same MMU LED (with index %d) used more than one segment: %s and %s" % (index + 1, used[led], segment))
                else:
                    used[led] = segment

        # Read default effects for each segment and other options
        self.enabled = config.get('enabled', True)
        self.animation = config.get('animation', True)
        self.exit_effect = config.get('exit_effect', 'gate_status')
        self.entry_effect = config.get('entry_effect', 'filament_color')
        self.status_effect = config.get('status_effect', 'filament_color')
        self.logo_effect = MmuLeds.string_to_rgb(config.get('logo_effect', '(0,0,0.3)'))
        self.white_light = MmuLeds.string_to_rgb(config.get('white_light', '(1,1,1)'))
        self.black_light = MmuLeds.string_to_rgb(config.get('black_light', '(0.01,0,0.02)'))
        self.empty_light = MmuLeds.string_to_rgb(config.get('empty_light', '(0,0,0)'))

        # Read operation to effect mappings
        self.effects = {}
        self.effect_rgb = {}
        effect_keys = [
            'effect_loading',
            'effect_loading_extruder',
            'effect_unloading',
            'effect_unloading_extruder',
            'effect_heating',
            'effect_selecting',
            'effect_checking',
            'effect_initialized',
            'effect_error',
            'effect_complete',
            'effect_gate_selected',
            'effect_gate_available',
            'effect_gate_unknown',
            'effect_gate_empty',
            'effect_gate_available_sel',
            'effect_gate_unknown_sel',
            'effect_gate_empty_sel'
        ]
        for key in effect_keys:
            parts = [part.strip() for part in config.get(key, '').split(",", 1)]
            effect = parts[0]
            rgb_string = parts[1] if len(parts) == 2 else config.get('empty_light', '(0,0,0)')
            operation = key[len('effect_'):]
            self.effects[operation] = effect
            self.effect_rgb[effect] = MmuLeds.string_to_rgb(rgb_string)
        self.effect_rgb[''] = (0,0,0)

    def parse_chain(self, chain):
        chain = chain.strip()
        leds=[]
        parms = [parameter.strip() for parameter in chain.split() if parameter.strip()]
        if parms:
            chain_name = parms[0].replace(':',' ')
            led_indices = ''.join(parms[1:]).strip('()').split(',')
            for led in led_indices:
                if led:
                    if '-' in led:
                        start, stop = map(int,led.split('-'))
                        if stop == start:
                            ledList = [start-1]
                        elif stop > start:
                            ledList = list(range(start-1, stop))
                        else:
                            ledList = list(reversed(range(stop-1, start)))
                        for i in ledList:
                            leds.append(int(i))
                    else:
                        for i in led.split(','):
                            leds.append(int(i)-1)
            return chain_name, leds
        else:
            return None, None

    def get_effect(self, operation):
        return self.effects.get(operation, '')

    def get_rgb_for_effect(self, effect):
        return self.effect_rgb[effect]

    def get_status(self, eventtime=None):
        status = {segment: len(self.virtual_chains[segment].leds) for segment in self.SEGMENTS}
        status.update({
            'enabled': self.enabled,
            'animation': self.animation,
            'exit_effect': self.exit_effect,
            'entry_effect': self.entry_effect,
            'status_effect': self.status_effect,
            'logo_effect': self.logo_effect,
            'num_gates': self.num_gates,
        })
        return status

    @staticmethod
    def string_to_rgb(rgb_string):
        if not isinstance(rgb_string, tuple):
            rgb = re.sub(r"[\"'()]", '', rgb_string) # Clean up strings
            rgb = tuple(float(x) for x in rgb.split(','))
        else:
           rgb = rgb_string
        if len(rgb) != 3:
            raise ValueError("%s is not a valid rgb tuple" % str(rgb_string))
        return rgb

def load_config_prefix(config):
    return MmuLeds(config)
