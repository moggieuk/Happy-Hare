# Fake Klipper `klippy/extras/led.py` for the Happy Hare test harness.
#
# extras/mmu/unit/mmu_leds.py:21 imports this module and :34 builds a REAL
# LEDHelper from a config section it synthesises on the fly (add_section ->
# getsection -> remove_section, :32-36). So LEDHelper must be constructible from a
# bare, optionless `[led <name>]` section.
#
# Surface HH touches (mmu_leds.py:52-70): led_state, led_count, need_transmit,
# _check_transmit() (new Klipper) / check_transmit(pt) (older Klipper + Kalico -
# HH hasattr-probes for both at :60-62, so BOTH must exist here or that compat
# branch is dead code), get_status()['color_data'], set_color.
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class LEDHelper:
    def __init__(self, config, update_func, led_count=1):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.update_func = update_func
        self.led_count = led_count
        self.need_transmit = False
        # Klipper seeds from initial_RED/GREEN/BLUE/WHITE; bare synthesised
        # sections have none, so default to off.
        red = config.getfloat('initial_RED', 0., minval=0., maxval=1.)
        green = config.getfloat('initial_GREEN', 0., minval=0., maxval=1.)
        blue = config.getfloat('initial_BLUE', 0., minval=0., maxval=1.)
        white = config.getfloat('initial_WHITE', 0., minval=0., maxval=1.)
        self.led_state = [(red, green, blue, white)] * led_count
        self.transmits = 0          # test assertion surface

    def get_led_count(self):
        return self.led_count

    def set_color(self, index, color):
        if index is None:
            self.led_state = [color] * self.led_count
        else:
            self.led_state[index - 1] = color
        self.need_transmit = True

    def check_transmit(self, print_time):
        """Older Klipper / Kalico name - HH probes for _check_transmit first."""
        if not self.need_transmit:
            return
        self.need_transmit = False
        self.transmits += 1
        if self.update_func is not None:
            self.update_func(self.led_state, print_time)

    def _check_transmit(self, print_time=None):
        """Newer Klipper name."""
        self.check_transmit(print_time)

    def get_status(self, eventtime=None):
        return {'color_data': list(self.led_state)}
