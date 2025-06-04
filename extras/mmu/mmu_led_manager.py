# Happy Hare MMU Software
#
# Manager to centralize mmu_led operations accross all mmu_units
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

class MmuLedManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine
        self.inside_timer = False

        # Event handlers
        self.mmu.printer.register_event_handler("klippy:ready", self.handle_ready)

    def handle_ready(self):
        self.setup_led_timer()

    def setup_led_timer(self):
        self.led_timer = self.mmu.reactor.register_timer(self.led_timer_handler, self.mmu.reactor.NEVER)

    def led_timer_handler(self, eventtime):
        self.inside_timer = True
        try:
            self._set_led(None, None, exit_effect='default', entry_effect='default', status_effect='default', logo_effect='default')
        finally:
            self.inside_timer = False
        return self.mmu.reactor.NEVER

    def schedule_led_command(self, duration, unit):
        if self.inside_timer:
           logging.info("PAUL: WARNING SCHEDULED DURATION inside of timer!")
        self.mmu.reactor.update_timer(self.led_timer, self.mmu.reactor.monotonic() + duration)

    # Called when an action has changed to update LEDs
    # (this could be changed to klipper event)
    def action_changed(self, action, old_action):
        gate = self.mmu.gate_selected
        unit = self.mmu.unit_selected

        if action == "Loading":
            self._set_led(
                None, gate,
                exit_effect='mmu_blue_slow_exit',
                status_effect='mmu_blue_slow_status'
            )
        elif action == "Loading Ext":
            self._set_led(
                None, gate,
                exit_effect='mmu_blue_fast_exit',
                status_effect='mmu_blue_fast_status'
            )
        elif old_action == "Exiting Ext":
            self._set_led(
                None, gate,
                exit_effect='mmu_blue_slow_exit',
                status_effect='mmu_blue_slow_status'
            )
        elif action == "Unloading":
            self._set_led(
                None, gate,
                exit_effect='mmu_blue_fast_exit',
                status_effect='mmu_blue_fast_status'
            )
        elif action == "Heating":
            self._set_led(
                None, gate,
                exit_effect='mmu_breathing_red_exit',
                status_effect='mmu_breathing_red_status'
            )
        elif action == "Idle":
            self._set_led(
                None, None,
                exit_effect='default',
                status_effect='default'
            )
        elif action in ("Homing", "Selecting"):
            if old_action not in ("Homing", "Checking"):
                self._set_led(
                    unit, None,
                    exit_effect='mmu_white_fast_exit',
                    status_effect='off',
                    fadetime=0
                )
        elif action == "Checking":
            self._set_led(
                unit, None,
                exit_effect='default',
                status_effect='mmu_white_fast_status'
            )

    # Called when print state changes to update LEDs
    # (this could be changed to klipper event)
    def print_state_changed(self, state, old_state):
        gate = self.mmu.gate_selected
        unit = self.mmu.unit_selected

        if state == "initialized":
            self._set_led(
                None, None,
                exit_effect='mmu_rainbow_exit',
                entry_effect='mmu_rainbow_entry',
                duration=8
            )
        elif state == "printing":
            self._set_led(
                None, None,
                exit_effect='default',
                entry_effect='default',
                status_effect='default'
            )
        elif state == "pause_locked":
            self._set_led(
                unit, None,
                exit_effect='mmu_strobe_exit',
                status_effect='mmu_strobe_status'
            )
        elif state == "paused":
            self._set_led(
                None, gate,
                exit_effect='mmu_strobe_exit',
                status_effect='mmu_strobe_status'
            )
        elif state == "ready":
            self._set_led(
                None, None,
                exit_effect='default',
                entry_effect='default',
                status_effect='default'
            )
        elif state == "complete":
            self._set_led(
                unit, None,
                exit_effect='mmu_sparkle_exit',
                status_effect='default',
                duration=20
            )
        elif state == "error":
            self._set_led(
                unit, None,
                exit_effect='mmu_strobe_exit',
                status_effect='default',
                duration=20
            )
        elif state == "cancelled":
            self._set_led(
                None, None,
                exit_effect='default',
                entry_effect='default',
                status_effect='default'
            )
        elif state == "standby":
            self._set_led(
                None, None,
                exit_effect='off',
                entry_effect='off',
                status_effect='off',
                logo_effect='off'
            )

    # Called when gate map is updated to update LEDs
    # (this could be changed to klipper event)
    def gate_map_changed(self, gate):
        return # PAUL TODO

        #current = mmu_unit.leds.get_status()['exit_effect']

        set_led_vars = printer['gcode_macro _MMU_SET_LED']

        exit_effect = ""
        current = set_led_vars.get('current_exit_effect')
        if current in ["gate_status", "filament_color", "slicer_color"]:
            exit_effect = current

        entry_effect = ""
        current = set_led_vars.get('current_entry_effect')
        if current in ["gate_status", "filament_color", "slicer_color"]:
            entry_effect = current

        status_effect = ""
        current = set_led_vars.get('current_status_effect')
        if current in ["filament_color", "slicer_color"]:
            status_effect = current

        if exit_effect or entry_effect or status_effect:
            print(f"_MMU_SET_LED exit_effect={exit_effect} entry_effect={entry_effect} status_effect={status_effect}")  # PAUL ALL UNITS

    # Make the necessary changes to LED accross all mmu_units
    #
    # Effects for LED segments when not providing "action status feedback" can be:
    # any effect name, "r,g,b" color, or built-in functional effects:
    #   "off"             - LED's off
    #   "on"              - LED's white
    #   "gate_status"     - indicate gate availability
    #   "filament_color"  - indicate filament color
    #   "slicer_color"    - display slicer defined color for each gate
    def _set_led(self, unit, gate, duration=None, fadetime=1, exit_effect=None, entry_effect=None, status_effect=None, logo_effect=None):
        logging.info("PAUL: _set_led(unit=%s, gate=%s, duration=%s, fadetime=%s, exit_effect=%s, entry_effect=%s, status_effect=%s, logo_effect=%s)" % (unit, gate, duration, fadetime, exit_effect, entry_effect, status_effect, logo_effect))

        # Helper functions...

        # List of led indexes (1-based on led_chain_str) for iteration
        def led_indexes(unit, segment, gate):
            mmu_unit = self.mmu_machine.get_mmu_unit_by_index(unit)
            num_leds = mmu_unit.leds.get_status()[segment]
            if gate is None or gate < 0:
                return list(range(1, num_leds))
            leds_per_gate = num_leds // mmu_unit.num_gates
            index0 = (gate - mmu_unit.first_gate) * leds_per_gate + 1
            return list(range(index0, index0 + leds_per_gate))

        # Raw virtual led chain for given segment
        def led_chain_str(unit, segment):
            return 'unit%d_mmu_%s_leds' % (unit, segment)

        # Used for selectively stopping effects
        def effect_leds_str(unit, segment, gate):
            if gate is not None and gate >= 0:
                led_index_str = ','.join(led_indexes(unit, segment, gate))
                return "%s (%s)" % (led_chain_str(unit, segment), led_index_str) # All leds for gate
            return led_chain_str(unit, segment) # All leds in segment

        # Used for applying effects
        def effect_str(unit, effect, gate):
            if gate is not None and gate >= 0:
                return "%s_%d" % (effect, gate)
            return "unit%d_%s" % (unit, effect)

        # Helper function to easily detect the last loop
        def with_last(iterable):
            it = iter(iterable)
            prev = next(it)
            for item in it:
                yield prev, False
                prev = item
            yield prev, True

        def get_effect(mmu_unit, segment, suggested):
            if mmu_unit.leds is None or mmu_unit.leds.get_status()[segment] == 0 or not mmu_unit.leds.enabled:
                return None # Not available
            elif suggested == 'default':
                return mmu_unit.leds.get_status()['%s_effect' % segment]
            return suggested

        def stop_and_set_rgb(rgb, unit, segment, gate):
            #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds ({index})"
            self.mmu.wrap_gcode_command("STOP_LED_EFFECTS LEDS='%s'" % (effect_leds_str(unit, segment, gate))
            #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={index} RED={rgb[0]} GREEN={rgb[1]} BLUE={rgb[2]} TRANSMIT=1
            for index, last in with_last(led_indexes(unit, segment, gate)):
                self.mmu.wrap_gcode_command(
                    "SET_LED LED=%s INDEX=%d RED=%d GREEN=%d BLUE=%d TRANSMIT=%d" % (
                        led_chain_str(unit, segment), index, rgb[0], rgb[1], rgb[2], 1 if is_last else 0
                    )
                )


        # Process LED update...

        if gate is not None and gate < 0:
            logging.info("PAUL: FIXME saftey .. gate <0")
            return

        if duration is not None:
            self.schedule_led_command(duration, unit)

        # Important: unit is redefined in this loop and will always be non-None
        for mmu_unit in [self.mmu_machine.get_mmu_unit_by_index(unit)] if unit is not None else self.mmu_machine.units:
            unit = mmu_unit.unit_index

            for segment in ['exit', 'entry']:
                effect = get_effect(mmu_unit, 'exit', exit_effect)
                logging.info("PAUL: effect for %s is %s" % (segment, effect))

                if not effect: # None or empty
                    continue

                elif effect == "off" or effect == "gate_status" or effect == "filament_color":
                    if gate is not None:
                        #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds ({index})" FADETIME={fadetime}
                        self.mmu.wrap_gcode_command("STOP_LED_EFFECTS LEDS='%s' FADETIME=%d" % (effect_leds_str(unit, segment, gate), fadetime))
                        #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={index} RED=0 GREEN=0 BLUE=0 TRANSMIT=1
                        for index, last in with_last(led_indexes(unit, segment, gate)):
                            self.mmu.wrap_gcode_command("SET_LED LED=%s INDEX=%d RED=0 GREEN=0 BLUE=0 TRANSMIT=%d" % (led_chain_str(unit, segment), index, 1 if is_last else 0))
                    else:
                        #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds" FADETIME={fadetime}
                        self.mmu.wrap_gcode_command("STOP_LED_EFFECTS LEDS='%s' FADETIME=%d" % (effect_leds_str(unit, segment, gate), fadetime))
                        #{% for i in range(1, num_gates + 1) %}
                            #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={i} RED=0 GREEN=0 BLUE=0 TRANSMIT={1 if loop.last else 0}
                        #{% endfor %}
                        for index, is_last in with_last(led_indexes(unit, segment, gate)):
                            self.mmu.wrap_gcode_command("SET_LED LED=%s INDEX=%d RED=0 GREEN=0 BLUE=0 TRANSMIT=%d" % (led_chain_str(unit, segment), index, 1 if is_last else 0))

                elif effect == "gate_status": # Filament availability (gate_map)
                    if gate is not None:
                        if gate == self.mmu.gate_selected and self.mmu.filament_pos == self.mmu.FILAMENT_POS_LOADED:
                            #_SET_LED_EFFECT EFFECT={unitstr}_mmu_blue_{segment}_{index} FADETIME={fadetime} REPLACE=1
                        elif self.mmu.gate_status[gate] == self.mmu.GATE_UNKNOWN:
                            #_SET_LED_EFFECT EFFECT={unitstr}_mmu_orange_{segment}_{index} FADETIME={fadetime} REPLACE=1
                        elif self.mmu.gate_status[gate] > self.mmu.GATE_EMPTY:
                            #_SET_LED_EFFECT EFFECT={unitstr}_mmu_green_{segment}_{index} FADETIME={fadetime} REPLACE=1
                        else:
                            #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds ({index})" FADETIME={fadetime}
                    else:
                        for g, status in enumerate(self.mmu.gate_status):
                            if g == self.mmu.gate_selected and self.mmu.filament_pos == self.mmu.FILAMENT_POS_LOADED:
                                #_SET_LED_EFFECT EFFECT={unitstr}_mmu_blue_{segment}_{loop.index} FADETIME={fadetime} REPLACE=1
                            elif status == self.mmu.GATE_UNKNOWN:
                                #_SET_LED_EFFECT EFFECT={unitstr}_mmu_orange_{segment}_{loop.index} FADETIME={fadetime} REPLACE=1
                            elif status > self.mmu.GATE_EMPTY:
                                #_SET_LED_EFFECT EFFECT={unitstr}_mmu_green_{segment}_{loop.index} FADETIME={fadetime} REPLACE=1
                            else:
                                #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds ({loop.index})" FADETIME={fadetime}
                                #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={loop.index} RED=0 GREEN=0 BLUE=0 TRANSMIT=1

                elif effect == "filament_color":
                    if gate is not None:
                        rgb = self.mmu.gate_color_rgb[gate]
                        stop_and_set_rgb(rgb, unit, segment, gate)
                    else:
                        #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds"
                        {% for rgb in gate_color_rgb %}
                            {% set current_gate = loop.index0 %}
                            {% if gate_status[current_gate] != 0 %}
                                {% if gate_color[current_gate] == "" %}
                                    {% set rgb = vars['white_light'] %}
                                {% elif rgb == (0,0,0) %}
                                    {% set rgb = vars['black_light'] %}
                                {% endif %}
                            {% else %}
                                {% set rgb = vars['empty_light'] %}
                            {% endif %}
                            #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={loop.index} RED={rgb[0]} GREEN={rgb[1]} BLUE={rgb[2]} TRANSMIT={1 if loop.last else 0}
                        {% endfor %}

                elif effect == "slicer_color":
                    if gate is not None:
                        rgb = self.mmu.slicer_color_rgb[gate]
                        stop_and_set_rgb(rgb, unit, segment, gate)
                    else:
                        #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds"
                        self.mmu.wrap_gcode_command("STOP_LED_EFFECTS LEDS='%s'" % (effect_leds_str(unit, segment, gate))
                        #{% for rgb in slicer_color_rgb %}
                            #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={loop.index} RED={rgb[0]} GREEN={rgb[1]} BLUE={rgb[2]} TRANSMIT={1 if loop.last else 0}
                        #{% endfor %}
                        # PAUL TODO

                elif "," in effect: # Not effect, just simple RGB color
                    set rgb = effect.split(",") # PAUL this is a tuple?
                    if gate is not None:
                        stop_and_set_rgb(rgb, unit, segment, gate)
                    else:
                        #_STOP_LED_EFFECTS LEDS="{unitstr}_mmu_{segment}_leds"
                        {% for i in range(1, num_gates + 1) %}
                            #SET_LED LED={unitstr}_mmu_{segment}_leds INDEX={i} RED={rgb[0]} GREEN={rgb[1]} BLUE={rgb[2]} TRANSMIT={1 if loop.last else 0}
                        {% endfor %}

                else: # Named effect
                    if gate is not None:
                        self.mmu.wrap_gcode_command("SET_LED_EFFECT EFFECT=%s_%d FADETIME=%d REPLACE=1" % (effect, gate, fadetime))
                    else:
                        self.mmu.wrap_gcode_command("SET_LED_EFFECT EFFECT=unit%d_%s FADETIME=%d REPLACE=1" % (mmu_unit.unit_index, effect, fadetime))
