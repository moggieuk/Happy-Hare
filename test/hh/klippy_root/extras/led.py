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
# SET_LED IS NOT OPTIONAL, and it is registered from HERE because that is where real
# Klipper registers it (klippy/extras/led.py:38-40, a mux command keyed on the LED
# name, inside LEDHelper.__init__). Without it every STATIC colour Happy Hare paints
# is silently lost: mmu/mmu_led_manager.py:635-642 drives all of them through
# `SET_LED LED=<virtual chain> INDEX=n RED=..`, and an unregistered command just
# lands in gcode.unhandled. That is the whole `animation: False` path plus, at the
# shipped defaults, entry_effect/status_effect (filament_color) and logo_effect
# (an r,g,b tuple) - i.e. three of the four segments would render permanently black.
#
# SET_LED_TEMPLATE is deliberately absent: it needs output_pin.lookup_template_eval
# and display_template, and HH never issues it.
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
        self.printer.lookup_object('gcode').register_mux_command(
            "SET_LED", "LED", self.name, self.cmd_SET_LED,
            desc=self.cmd_SET_LED_help)

    def get_led_count(self):
        return self.led_count

    def set_color(self, index, color):
        # NO "colour unchanged -> return early" short-circuit, unlike real Klipper
        # (_set_color, klippy/extras/led.py:45-49). There the skip is free because
        # led_state is already correct; here the transmit is what drives
        # VirtualMmuLedChain.update_leds (mmu/unit/mmu_leds.py:53-63), i.e. the
        # virtual -> physical copy. Repainting an unchanged colour is routine
        # (filament_color over an unchanged gate map), and skipping it would leave
        # the physical chain stale after any partial write.
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

    cmd_SET_LED_help = "Set the color of an LED"

    def cmd_SET_LED(self, gcmd):
        """Mirrors klippy/extras/led.py:110-133, minus SET_LED_TEMPLATE."""
        red = gcmd.get_float('RED', 0., minval=0., maxval=1.)
        green = gcmd.get_float('GREEN', 0., minval=0., maxval=1.)
        blue = gcmd.get_float('BLUE', 0., minval=0., maxval=1.)
        white = gcmd.get_float('WHITE', 0., minval=0., maxval=1.)
        index = gcmd.get_int('INDEX', None, minval=1, maxval=self.led_count)
        # TRANSMIT=0 on every index but the last is how mmu_led_manager.py:635-642
        # paints a whole segment in one flush, so honouring it is not cosmetic.
        transmit = gcmd.get_int('TRANSMIT', 1)
        sync = gcmd.get_int('SYNC', 1)
        color = (red, green, blue, white)

        def lookahead_bgfunc(print_time):
            self.set_color(index, color)
            if transmit:
                self._check_transmit(print_time)

        if sync:
            # Klipper defers to the end of the move queue. The fake toolhead has no
            # queue and calls straight through (klippy_root/toolhead.py:76-80), so
            # this stays synchronous - taking the same route rather than
            # special-casing keeps the harness honest about which path HH uses.
            self.printer.lookup_object('toolhead').register_lookahead_callback(
                lookahead_bgfunc)
        else:
            lookahead_bgfunc(None)
