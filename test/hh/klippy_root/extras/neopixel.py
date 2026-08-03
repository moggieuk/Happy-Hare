# Fake Klipper extras/neopixel.py for the Happy Hare test harness.
#
# VirtualMmuLedChain does printer.load_object(config, chain_name)
# (extras/mmu/unit/mmu_leds.py:41) for each configured chain and then reaches into
# `chain.led_helper` (:52-62) to write led_state and force a transmit. The rendered
# BoxTurtle config ships [neopixel _unit0_leds].
from . import led as led_mod

# HH monkeypatches this constant as a Klipper workaround
# (extras/mmu/mmu_controller.py:120-124, gated on update_bit_max_time). It logs an
# error if the attribute is absent, so it must exist. Value from real Klipper
# extras/neopixel.py:11.
BIT_MAX_TIME = .000004


class PrinterNeoPixel:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        chain_count = config.getint('chain_count', 1, minval=1)
        color_order = config.get('color_order', 'GRB')
        self.color_order = color_order
        self.led_helper = led_mod.LEDHelper(config, self.update_leds, chain_count)
        self.updates = []       # test assertion surface

    def update_leds(self, led_state, print_time):
        self.updates.append((print_time, list(led_state)))

    def get_status(self, eventtime=None):
        return self.led_helper.get_status(eventtime)


def load_config_prefix(config):
    return PrinterNeoPixel(config)
