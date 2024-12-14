# Happy Hare MMU Software
#
# Allows for flexible creation of virtual leds chains - one for each of the supported
# segments (exit, entry, status). Entry and exit are indexed by gate number.
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

from . import led as klipper_led

class VirtualMmuLedChain:
    def __init__(self, config, segment, config_chains):
        self.printer = printer = config.get_printer()
        self.name = "mmu_%s_leds" % segment
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
            #chain = printer.load_object(config, chain_name) # PAUL is trying to load now better for error feedback?
            chain = self.printer.lookup_object(chain_name, None)
            if chain:
                for led in leds:
                    self.leds.append((chain, led))
            else:
                raise config.error("MMU LED chain '%s' referenced in '%s' doesn't exist" % (chain_name, self.name))

    def update_leds(self, led_state, print_time):
        chains_to_update = set()
        for color, (chain, led) in zip(led_state, self.leds):
            chain.led_helper.led_state[led] = color
            chains_to_update.add(chain)
        for chain in chains_to_update:
            chain.led_helper.update_func(chain.led_helper.led_state, None)

    def get_status(self, eventtime=None):
        state = []
        chain_status = {}
        for chain, led in self.leds:
            if chain not in chain_status:
                status = chain.led_helper.get_status(eventtime)['color_data']
                chain_status[chain] = status
            state.append(chain_status[chain][led])
        return dict(color_data=state)


class MmuLeds:

    PER_GATE_SEGMENTS = ['exit', 'entry']
    SEGMENTS = PER_GATE_SEGMENTS + ['status']

    # Shared by all [mmu_led_effect] definitions
    num_gates = None
    frame_rate = 24
    leds_configured = False
    led_effect_module = False

    def __init__(self, config):
        self.printer = printer = config.get_printer()

        MmuLeds.num_gates = config.getsection("mmu_machine").getint("num_gates")
        MmuLeds.frame_rate = config.getint('frame_rate', MmuLeds.frame_rate)

        # Create virtual led chains
        self.virtual_chains = {}
        for segment in self.SEGMENTS:
            name = "%s_leds" % segment
            config_chains = [self.parse_chain(line) for line in config.get(name, '').split('\n') if line.strip()]
            self.virtual_chains[segment] = VirtualMmuLedChain(config, segment, config_chains)
            printer.add_object("mmu_%s" % name, self.virtual_chains[segment])

            num_leds = len(self.virtual_chains[segment].leds)
            if segment in self.PER_GATE_SEGMENTS and num_leds != MmuLeds.num_gates:
                raise config.error("Number of MMU '%s' LEDs (%d) doesn't match num_gates (%d)" % (segment, num_leds, MmuLeds.num_gates))

        # Check for LED chain overlap or unavailable LED
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

        MmuLeds.leds_configured = True

        # See if LED effects module is installed
        try:
           _ = config.get_printer().load_object(config, 'led_effect')
           MmuLeds.led_effect_module = True
        except Exception:
            pass

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

    def get_status(self, eventtime=None):
        return {segment: len(self.virtual_chains[segment].leds) for segment in self.SEGMENTS}

def load_config(config):
    return MmuLeds(config)
