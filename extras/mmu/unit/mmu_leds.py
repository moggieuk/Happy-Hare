# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal:
# Allows for flexible creation of virtual leds chains for each mmmu_unit
#  - One for each of the supported segments (exit, entry, status, logo).
#  - Entry and exit are indexed by gate number.
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, re

# Klipper imports
from ... import led as klipper_led


class VirtualMmuLedChain:

    def __init__(self, config, unit_name, segment, config_chains):
        self.printer = config.get_printer()
        self.name = "%s_mmu_%s_leds" % (unit_name, segment)
        self.config_chains = config_chains

        # Create temporary config section just to access led helper
        led_section = "led %s" % self.name
        config.fileconfig.add_section(led_section)
        led_config = config.getsection(led_section)
        self.led_helper = klipper_led.LEDHelper(led_config, self.update_leds, sum(len(leds) for gate, chain_name, leds in config_chains))
        config.fileconfig.remove_section(led_section)

        # We need to configure the chain now so we can validate
        self.leds = []
        self.fragments = [] # (gate or None, led count) per config line, in order
        for gate, chain_name, leds in self.config_chains:
            before = len(self.leds)
            try:
                chain = self.printer.load_object(config, chain_name)
                if chain:
                    for led in leds:
                        self.leds.append((chain, led))
            except Exception as e:
                raise config.error("MMU LED chain '%s' referenced in '%s' cannot be loaded:\n%s" % (chain_name, self.name, str(e)))
            self.fragments.append((gate, len(self.leds) - before))

        # Register led object with klipper
        logging.info("MMU: Created virtual led chain: [%s]" % led_section)
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

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.name = config.get_name().split()[-1]
        self.first_gate = mmu_unit.first_gate
        self.num_gates = mmu_unit.num_gates
        self.printer = config.get_printer()

        self.frame_rate = config.getint('frame_rate', 24)

        # Create virtual led chains
        self.virtual_chains = {}
        for segment in self.SEGMENTS:
            name = "%s_leds" % segment
            config_chains = [self.parse_chain(line) for line in config.get(name, '').split('\n') if line.strip()]
            self.virtual_chains[segment] = VirtualMmuLedChain(config, self.mmu_unit.name, segment, config_chains)

        # Work out which LEDs of the per-gate segments belong to which gate. This is the single
        # place that mapping is decided - everything else (mmu_led_effect, mmu_led_manager) asks
        # here rather than deriving it, precisely because the split need not be even
        self.gate_leds = {segment: self._map_leds_to_gates(config, segment) for segment in self.PER_GATE_SEGMENTS}

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
        self.filament_color_intensity = config.getfloat('filament_color_intensity', 0.5, minval=0.0, maxval=1.0)

        # Read operation to effect mappings
        self.effects = {}
        self.effect_rgb = {}
        self.effect_duration = {}
        effect_keys = [
            'effect_loading',
            'effect_loading_extruder',
            'effect_unloading',
            'effect_unloading_extruder',
            'effect_heating',
            'effect_selecting',
            'effect_checking',
            'effect_preloading',
            'effect_initialized',
            'effect_error',
            'effect_complete',
            'effect_gate_selected',
            'effect_gate_available',
            'effect_gate_unknown',
            'effect_gate_empty',
            'effect_gate_available_sel',
            'effect_gate_unknown_sel',
            'effect_gate_empty_sel',
            'effect_pending_spoolid',
            'effect_pending_spoolid_expiring',
            'effect_nfc_read',      # Transient effect
            'effect_nfc_deep_read', # Transient effect
            'effect_nfc_fail',      # Transient effect
        ]
        for key in effect_keys:
            operation = key[len('effect_'):]
            try:
                effect, rgb_string, duration = MmuLeds.parse_effect_spec(
                    config.get(key, ''), config.get('empty_light', '(0,0,0)'))
                self.effect_rgb[effect] = MmuLeds.string_to_rgb(rgb_string)
            except ValueError as e:
                raise ValueError("Invalid value for '%s' in [mmu_leds]: %s" % (key, e))
            self.effects[operation] = effect
            self.effect_duration[operation] = duration
        self.effect_rgb[''] = (0,0,0)

    # Split a per-gate segment's LEDs between the gates of this unit. Returns a list of
    # 'num_gates' lists, each holding the 1-based LED indexes (into the segment's virtual
    # chain) that belong to that gate.
    #
    # THE LINES ARE NOT GATES. A line is a fragment of physical wiring - a strip, a single
    # LED, a chain on another pin - and is written that way to spread current over pins or
    # simply because that is how the machine is built. The shipped 8-gates-over-5-strips
    # example in mmu_hardware.cfg has five lines and eight gates. So by default the LEDs are
    # concatenated into one logical chain and shared out EVENLY, which requires the count to
    # divide by num_gates. That is the historical behaviour and stays the default.
    #
    # A gate that owns a different number of LEDs from its neighbours has to say so, and it
    # says so in the same declaration, by prefixing lines with the gate they belong to:
    #
    #   exit_leds: 0: neopixel:bt_1 (1-3)
    #              1: neopixel:bt_1 (4)
    #
    # Deliberately all-or-nothing, and every gate must appear: a definition half in one
    # notation and half in the other has no obvious reading, and a gate with no LEDs at all
    # would need paint/stop paths that do not exist. More than one line may name the same
    # gate, which is how a gate whose LEDs straddle two strips is expressed.
    def _map_leds_to_gates(self, config, segment):
        fragments = self.virtual_chains[segment].fragments
        num_leds = len(self.virtual_chains[segment].leds)
        assigned = [gate for gate, _ in fragments if gate is not None]

        if not assigned:
            if num_leds % self.num_gates:
                raise config.error(
                    "Number of MMU '%s' LEDs (%d) cannot be spread over num_gates (%d). Either use a count that "
                    "divides evenly or prefix each line of '%s_leds' with the gate it belongs to (e.g. \"0: ...\")" % (
                        segment, num_leds, self.num_gates, segment)
                )
            per_gate = num_leds // self.num_gates
            return [list(range(gate * per_gate + 1, (gate + 1) * per_gate + 1)) for gate in range(self.num_gates)]

        if len(assigned) != len(fragments):
            raise config.error(
                "Only %d of the %d lines of MMU '%s_leds' name a gate. Either prefix all of them or none of "
                "them - a partly prefixed definition is ambiguous" % (len(assigned), len(fragments), segment))

        gate_leds = [[] for _ in range(self.num_gates)]
        index = 1 # LED indexes are 1-based within the segment
        for gate, count in fragments:
            if not 0 <= gate < self.num_gates:
                raise config.error(
                    "MMU '%s_leds' assigns LEDs to gate %d but this unit only has gates 0..%d" % (
                        segment, gate, self.num_gates - 1))
            gate_leds[gate].extend(range(index, index + count))
            index += count

        unlit = [gate for gate, leds in enumerate(gate_leds) if not leds]
        if unlit:
            raise config.error(
                "MMU '%s_leds' gives no LEDs to gate(s) %s. Every gate needs at least one" % (
                    segment, ', '.join(map(str, unlit))))
        return gate_leds

    # The 1-based LED indexes on 'segment' belonging to 'gate' (absolute gate number). Empty
    # if the segment isn't indexed by gate or the gate isn't on this unit
    def gate_led_indexes(self, segment, gate):
        gate_leds = self.gate_leds.get(segment)
        if gate_leds is None:
            return []
        index = gate - self.first_gate
        if not 0 <= index < len(gate_leds):
            return []
        return gate_leds[index]

    # Number of LEDs on 'segment' for each gate of this unit (list of length num_gates)
    def gate_led_counts(self, segment):
        return [len(leds) for leds in self.gate_leds.get(segment, [])]

    # An optional "<gate>:" prefix on a line assigns that fragment to a gate of this unit
    # (0-based within the unit), e.g. "2: neopixel:mmu_leds (5-8)". Nothing else can start a
    # line with digits followed by a colon - a chain reference always leads with its section
    # type ("neopixel:...") - so this cannot collide with an existing config.
    GATE_PREFIX_RE = re.compile(r'^\s*(\d+)\s*:\s*')

    def parse_chain(self, chain):
        chain = chain.strip()
        leds=[]
        gate = None
        prefix = self.GATE_PREFIX_RE.match(chain)
        if prefix:
            gate = int(prefix.group(1))
            chain = chain[prefix.end():]
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
            return gate, chain_name, leds
        else:
            return None, None, None

    def get_effect(self, operation):
        return self.effects.get(operation, '')

    def get_duration(self, operation):
        # Optional per-operation default duration (3rd config field), or None if not specified
        return self.effect_duration.get(operation)

    def get_rgb_for_effect(self, effect):
        return self.effect_rgb.get(effect)

    def get_effect_names(self):
        return set(effect for effect in self.effect_rgb if effect)

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
    def parse_effect_spec(raw, empty_rgb='(0,0,0)'):
        # Parse an "effect_<op>" config value of the form "<effect>, (r,g,b)[, <duration>]".
        # Returns (effect_name, rgb_string, duration_or_None). The rgb tuple and the optional
        # trailing duration (seconds) are split on the closing paren so the commas inside
        # (r,g,b) don't confuse a naive split. A bare effect name (no rgb) falls back to
        # empty_rgb; a missing/empty value yields ('', empty_rgb, None).
        raw = (raw or '').strip()
        effect, _, rest = raw.partition(',')
        effect = effect.strip()
        rest = rest.strip()
        duration = None
        if ')' in rest:
            close = rest.index(')')
            rgb_string = rest[:close + 1]
            tail = rest[close + 1:].strip().lstrip(',').strip()
            if tail:
                duration = float(tail)
        else:
            rgb_string = rest if rest else empty_rgb
        return effect, rgb_string, duration

    @staticmethod
    def string_to_rgb(rgb_string):
        if not isinstance(rgb_string, tuple):
            rgb = re.sub(r"[\"'()]", '', rgb_string)
            rgb = tuple(float(x) for x in rgb.split(','))
        else:
            rgb = rgb_string

        if len(rgb) != 3:
            raise ValueError(f"{rgb_string} is not a valid rgb tuple")

        for value in rgb:
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"{rgb_string} is not a valid rgb tuple. "
                    f"RGB value {value} is out of range. Values must be between 0.0 and 1.0"
                )

        return rgb

    @staticmethod
    def apply_intensity(rgb, intensity):
        return tuple(min(1.0, value * intensity) for value in rgb)
