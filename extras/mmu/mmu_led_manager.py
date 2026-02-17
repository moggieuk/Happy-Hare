# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to centralize mmu_led operations accross all mmu_units
#
# Implements commands:
#   MMU_SET_LED
#   MMU_LED
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ..mmu_leds  import MmuLeds

# MMU subcomponent clases
from .mmu_shared import *

class MmuLedManager:
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine
        self.inside_timer = False
        self.pending_update = [False] * self.mmu_machine.num_units
        self.effect_state = {} # Current state used to minimise updates {unit: {segment: effect}}

        # Event handlers
        self.mmu.printer.register_event_handler("klippy:ready", self.handle_ready)

        # Register commands
        self.mmu.gcode.register_command('MMU_SET_LED', self.cmd_MMU_SET_LED, desc = self.cmd_MMU_SET_LED_help)
        self.mmu.gcode.register_command('MMU_LED', self.cmd_MMU_LED, desc = self.cmd_MMU_LED_help)

    def handle_ready(self):
        self.setup_led_timer()

    def setup_led_timer(self):
        self.led_timer = self.mmu.reactor.register_timer(self.led_timer_handler, self.mmu.reactor.NEVER)

    def led_timer_handler(self, eventtime):
        self.inside_timer = True
        try:
            for unit in range(self.mmu_machine.num_units):
                self.pending_update[unit] = False
                self._set_led(unit, None, exit_effect='default', entry_effect='default', status_effect='default', logo_effect='default')
        finally:
            self.inside_timer = False
        return self.mmu.reactor.NEVER

    def schedule_led_command(self, duration, unit):
        if not self.inside_timer:
            self.pending_update[unit] = True
            self.mmu.reactor.update_timer(self.led_timer, self.mmu.reactor.monotonic() + duration)

    cmd_MMU_SET_LED_help = "Directly control MMU leds"
    cmd_MMU_SET_LED_param_help = (
        "MMU_SET_LED: %s\n" % cmd_MMU_SET_LED_help
        + "UNIT          = #(int)\n"
        + "GATE          = #(int)\n"
        + "EXIT_EFFECT   = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "ENTRY_EFFECT  = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "STATUS_EFFECT = [off|on|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "LOGO_EFFECT   = [off|r,g,b|_effect_]\n"
        + "DURATION      = #.#(float) seconds\n"
        + "FADETIME      = #.#(float) seconds"
    )
    def cmd_MMU_SET_LED(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())

        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        unit = gcmd.get_int('UNIT', 0, minval=0, maxval=self.mmu_machine.num_units - 1)
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu_machine.num_gates - 1)
        exit_effect = gcmd.get('EXIT_EFFECT', None)
        entry_effect = gcmd.get('ENTRY_EFFECT', None)
        status_effect = gcmd.get('STATUS_EFFECT', None)
        logo_effect = gcmd.get('LOGO_EFFECT', None)
        duration = gcmd.get_float('DURATION', None, minval=0)
        fadetime = gcmd.get_float('FADETIME', 1, minval=0)

        if self.mmu_machine.get_mmu_unit_by_index(unit).leds is None:
            self.mmu.log_error("No LEDs configured on MMU unit %d" % unit)
            return

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SET_LED_param_help), color=True)
            return

        self._set_led(
            unit, gate,
            entry_effect=entry_effect,
            exit_effect=exit_effect,
            status_effect=status_effect,
            logo_effect=logo_effect,
            fadetime=fadetime,
            duration=duration
        )

    cmd_MMU_LED_help = "Manage mode of operation of optional MMU LED's"
    cmd_MMU_LED_param_help = (
        "MMU_LED: %s\n" % cmd_MMU_LED_help
        + "UNIT          = #(int) default all units\n"
        + "ENABLE        = [0|1]\n"
        + "ANIMATION     = [0|1]\n"
        + "EXIT_EFFECT   = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "ENTRY_EFFECT  = [off|gate_status|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "STATUS_EFFECT = [off|on|filament_color|slicer_color|r,g,b|_effect_]\n"
        + "LOGO_EFFECT   = [off|r,g,b|_effect_]\n"
        + "REFRESH       = [0|1]\n"
        + "QUIET         = [0|1]"
    )
    def cmd_MMU_LED(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))
        unit = gcmd.get_int('UNIT', None, minval=0, maxval=self.mmu_machine.num_units - 1)

        mmu_units = [self.mmu_machine.get_mmu_unit_by_index(unit)] if unit is not None else self.mmu_machine.units
        if all(mmu_unit.leds is None for mmu_unit in mmu_units):
            self.mmu.log_error("No LEDs configured on MMU")
            return

        if help:
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_LED_param_help), color=True)
            return

        msg = ""
        for mmu_unit in mmu_units:
            leds = mmu_unit.leds
            unit = mmu_unit.unit_index

            if leds:
                exit_effect = gcmd.get('EXIT_EFFECT', leds.exit_effect)
                entry_effect = gcmd.get('ENTRY_EFFECT', leds.entry_effect)
                status_effect = gcmd.get('STATUS_EFFECT', leds.status_effect)
                logo_effect = gcmd.get('LOGO_EFFECT', leds.logo_effect)
                enabled = bool(gcmd.get_int('ENABLE', leds.enabled, minval=0, maxval=1))
                animation = bool(gcmd.get_int('ANIMATION', leds.animation, minval=0, maxval=1))

                if leds.enabled and not enabled or refresh:
                    # Enabled to disabled or refresh
                    self._set_led(
                        unit, None,
                        exit_effect='off',
                        entry_effect='off',
                        status_effect='off',
                        logo_effect='off'
                    )
                else:
                    if leds.animation and not animation:
                        # Turning animation off so clear existing effects
                        self._set_led(
                            unit, None,
                            exit_effect='off',
                            entry_effect='off',
                            status_effect='off',
                            logo_effect='off',
                            fadetime=0
                        )

                if (leds.exit_effect != exit_effect or
                    leds.entry_effect != entry_effect or
                    leds.status_effect != status_effect or
                    leds.logo_effect != logo_effect or
                    leds.enabled != enabled or
                    leds.animation != animation or
                    refresh):

                    leds.exit_effect = exit_effect
                    leds.entry_effect = entry_effect
                    leds.status_effect = status_effect
                    leds.logo_effect = logo_effect
                    leds.enabled = enabled
                    leds.animation = animation

                    if enabled:
                        self._set_led(
                            unit, None,
                            exit_effect='default',
                            entry_effect='default',
                            status_effect='default',
                            logo_effect='default'
                        )

                if not quiet:
                    available = lambda effect, enabled : ("'%s'" % str(effect)) if enabled else "unavailable"
                    msg += "\nUnit %s LEDs (%s)\n" % (unit, ("enabled" if enabled else "disabled"))
                    msg += "  Animation: %s\n" % ("enabled" if animation else "disabled")
                    msg += "  Default exit effect: %s\n" % available(exit_effect, leds.get_status()['exit'])
                    msg += "  Default entry effect: %s\n" % available(entry_effect, leds.get_status()['entry'])
                    msg += "  Default status effect: %s\n" % available(status_effect, leds.get_status()['status'])
                    msg += "  Default logo effect: %s\n" % available(logo_effect, leds.get_status()['logo'])
            else:
                msg += "No LEDs configured on MMU unit %d" % unit
        self.mmu.log_always(msg)

    # Called when an action has changed to update LEDs
    # (this could be changed to klipper event)
    def action_changed(self, action, old_action):
        gate = self.mmu.gate_selected

        # Check for unit specific actions
        if action in [self.mmu.ACTION_HOMING, self.mmu.ACTION_SELECTING]:
            units_to_update = [self.mmu.unit_selected]
        else:
            units_to_update = range(self.mmu_machine.num_units)

        for unit in units_to_update:

            # Load sequence...
            # idle -> loading -> load_ext -> [heat -> load_ext] -> loading* -> purging* -> loading* -> idle (*=excluded)

            if action == self.mmu.ACTION_LOADING:
                if old_action not in [self.mmu.ACTION_LOADING_EXTRUDER, self.mmu.ACTION_PURGING]:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'loading'),
                        status_effect=self.effect_name(unit, 'loading'),
                        fadetime=0.5
                    )

            elif action == self.mmu.ACTION_LOADING_EXTRUDER:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'loading_extruder'),
                    status_effect=self.effect_name(unit, 'loading_extruder'),
                    fadetime=0.5
                )

            elif action == self.mmu.ACTION_PURGING:
                pass

            # Unload sequence...
            # idle -> unloading -> form_tip/cut -> [heat -> form_tip/cut] -> unloading* -> unload_ext -> unloading
            # [cutting* -> unloading*] -> idle (*=excluded)
            elif action == self.mmu.ACTION_UNLOADING:
                if old_action == self.mmu.ACTION_IDLE:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'unloading_extruder'),
                        status_effect=self.effect_name(unit, 'unloading_extruder'),
                        fadetime=0.5
                    )

                elif old_action not in [
                    self.mmu.ACTION_FORMING_TIP,
                    self.mmu.ACTION_CUTTING_FILAMENT
                ]:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'unloading'),
                        status_effect=self.effect_name(unit, 'unloading'),
                        fadetime=0.5
                    )

            elif action == self.mmu.ACTION_UNLOADING_EXTRUDER:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'unloading_extruder'),
                    status_effect=self.effect_name(unit, 'unloading_extruder'),
                    fadetime=0.5
                )

            elif action in [self.mmu.ACTION_FORMING_TIP, self.mmu.ACTION_FORMING_TIP]:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'unloading_extruder'),
                    status_effect=self.effect_name(unit, 'unloading_extruder'),
                    fadetime=0.5
                )

            elif action == self.mmu.ACTION_CUTTING_FILAMENT:
                pass

            # Other actions...

            elif action == self.mmu.ACTION_HEATING:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'heating'),
                    status_effect=self.effect_name(unit, 'heating')
                )

            elif action == self.mmu.ACTION_IDLE:
                self._set_led(
                    unit, None,
                    exit_effect='default',
                    status_effect='default'
                )

            # Type-A MMU actions involving selector (unit specific)...

            # idle -> home -> select -> home* -> idle (*=excluded)
            elif action == self.mmu.ACTION_HOMING:
                if old_action == self.mmu.ACTION_IDLE:
                    self._set_led(
                        unit, None,
                        exit_effect=self.effect_name(unit, 'selecting'),
                        status_effect=self.effect_name(unit, 'selecting'),
                        fadetime=0
                    )

            # idle -> select -> idle
            elif action == self.mmu.ACTION_SELECTING:
                if old_action not in [self.mmu.ACTION_CHECKING]:
                    self._set_led(
                        unit, None,
                        exit_effect='default',
                        status_effect=self.effect_name(unit, 'selecting'),
                        fadetime=0
                    )

            # idle -> check -> select* -> check* -> select* -> check* -> idle
            elif action == self.mmu.ACTION_CHECKING:
                if old_action == self.mmu.ACTION_IDLE:
                    self._set_led(
                        unit, None,
                        exit_effect='default',
                        status_effect=self.effect_name(unit, 'checking')
                    )

    # Called when print state changes to update LEDs
    # (this could be changed to klipper event)
    def print_state_changed(self, state, old_state):
        gate = self.mmu.gate_selected
        if state in ['initialized', 'printing', 'ready', 'cancelled', 'standby']:
            units_to_update = range(self.mmu_machine.num_units)
        else:
            units_to_update = [self.mmu.unit_selected]

        for unit in units_to_update:
            if state == "initialized":
                self._set_led(
                    unit, None,
                    exit_effect=self.effect_name(unit, 'initialized'),
                    entry_effect=self.effect_name(unit, 'initialized'),
                    status_effect=self.effect_name(unit, 'initialized'),
                    duration=8
                )

            elif state == "printing":
                self._set_led(
                    unit, None,
                    exit_effect='default',
                    entry_effect='default',
                    status_effect='default'
                )

            elif state == "pause_locked":
                self._set_led(
                    unit, None,
                    exit_effect=self.effect_name(unit, 'error'),
                    status_effect=self.effect_name(unit, 'error')
                )

            elif state == "paused":
                self._set_led(
                    unit, gate, # Focus to specific gate
                    exit_effect=self.effect_name(unit, 'error'),
                    status_effect=self.effect_name(unit, 'error')
                )

            elif state == "ready":
                self._set_led(
                    unit, None,
                    exit_effect='default',
                    entry_effect='default',
                    status_effect='default'
                )

            elif state == "complete":
                self._set_led(
                    unit, None,
                    exit_effect=self.effect_name(unit, 'complete'),
                    status_effect='default',
                    duration=10
                )

            elif state == "error":
                self._set_led(
                    unit, None,
                    exit_effect=self.effect_name(unit, 'error'),
                    status_effect='default',
                    duration=10
                )

            elif state == "cancelled":
                self._set_led(
                    unit, None,
                    exit_effect='default',
                    entry_effect='default',
                    status_effect='default'
                )

            elif state == "standby":
                self._set_led(
                    unit, None,
                    exit_effect='off',
                    entry_effect='off',
                    status_effect='off',
                    logo_effect='off'
                )

    # Called when gate map is updated to update LEDs
    # (this could be changed to klipper event)
    def gate_map_changed(self, gate):
        if gate is not None and gate < 0:
            gate = None
        gate_effects = {'gate_status', 'filament_color', 'slicer_color'}
        units = [self.mmu_machine.get_mmu_unit_by_gate(gate)] if gate is not None else self.mmu_machine.units
        for mmu_unit in units:
            leds = mmu_unit.leds
            if not leds:
                continue

            entry_effect = leds.entry_effect if leds.entry_effect in gate_effects else None
            exit_effect = leds.exit_effect if leds.exit_effect in gate_effects else None
            status_effect = leds.status_effect if leds.status_effect in gate_effects - {'gate_status'} else None

            if exit_effect or entry_effect or status_effect:
                self._set_led(
                    mmu_unit.unit_index,
                    gate,
                    exit_effect=exit_effect,
                    entry_effect=entry_effect,
                    status_effect=status_effect
                )

    def effect_name(self, unit, operation):
        leds = self.mmu_machine.get_mmu_unit_by_index(unit).leds
        if leds:
            return leds.get_effect(operation)
        return ''

    # Make the necessary configuration changes to LED accross all mmu_units
    #
    # Effects for LED segments when not providing "action status feedback" can be:
    # any effect name, "r,g,b" color, or built-in functional effects:
    #   "off"             - LED's off
    #   "on"              - LED's white
    #   "gate_status"     - indicate gate availability
    #   "filament_color"  - indicate filament color
    #   "slicer_color"    - display slicer defined color for each gate
    def _set_led(self, unit, gate, duration=None, fadetime=1, exit_effect=None, entry_effect=None, status_effect=None, logo_effect=None):
        effects = {
            'entry': entry_effect,
            'exit': exit_effect,
            'status': status_effect,
            'logo': logo_effect,
        }

        # Helper functions to make core logic simplier...

        # Iteration wrapper to easily detect the last loop
        def with_last(iterable):
            it = iter(iterable)
            try:
                prev = next(it)
            except StopIteration:
                return  # Empty iterable
            for item in it:
                yield prev, False
                prev = item
            yield prev, True

        # List of led indexes (1-based on led_chain_spec) for iteration
        def led_indexes(unit, segment, gate):
            mmu_unit = self.mmu_machine.get_mmu_unit_by_index(unit)
            num_leds = mmu_unit.leds.get_status()[segment]
            if gate is None or gate < 0:
                return list(range(1, num_leds + 1))
            leds_per_gate = num_leds // mmu_unit.num_gates
            index0 = (gate - mmu_unit.first_gate) * leds_per_gate + 1
            return list(range(index0, index0 + leds_per_gate))

        # Get raw "LEDS=" spec to stop an effect on virtual chain for given segment
        def led_chain_spec(unit, segment):
            return 'unit%d_mmu_%s_leds' % (unit, segment)

        # Get specific LEDS=" spec to stop an effect on whole segment or gate part of segment
        def effect_leds_spec(unit, segment, gate):
            if gate is not None and gate >= 0:
                led_index_str = ','.join(map(str, led_indexes(unit, segment, gate)))
                return "%s (%s)" % (led_chain_spec(unit, segment), led_index_str) # All leds for gate
            return led_chain_spec(unit, segment) # All leds in segment

        # Get "EFFECT=" spec Used for applying effects
        def effect_spec(unit, gate, effect):
            if gate is not None and gate >= 0:
                return "%s_%d" % (effect, gate)
            return "unit%d_%s" % (unit, effect)

        # Translate desired effect into specific one based on context
        def get_effective_effect(mmu_unit, segment, suggested):
            if not mmu_unit.leds or not mmu_unit.leds.enabled or mmu_unit.leds.get_status()[segment] == 0:
                return '' # Not available
            elif suggested == 'default':
                return mmu_unit.leds.get_status()['%s_effect' % segment]
            return suggested

        # Stop the current effect on the gate led(s)
        def stop_gate_effect(unit, segment, gate, fadetime=None):
            if self.mmu_machine.get_mmu_unit_by_index(unit).leds.animation:
                self.mmu.gcode.run_script_from_command(
                    "_MMU_STOP_LED_EFFECTS LEDS='%s' %s" % (
                        effect_leds_spec(unit, segment, gate),
                        ('FADETIME=%d' % fadetime) if fadetime is not None else ''
                    )
                )

        # Sets or replaces effect on the gate led(s)
        def set_gate_effect(base_effect, unit, segment, gate, fadetime=None):
            leds = self.mmu_machine.get_mmu_unit_by_index(unit).leds
            if leds.animation:
                self.mmu.gcode.run_script_from_command(
                    "_MMU_SET_LED_EFFECT EFFECT='%s' REPLACE=1 %s" % (
                        effect_spec(unit, gate, "%s_%s" % (base_effect, segment)),
                        ('FADETIME=%d' % fadetime) if fadetime is not None else ''
                    )
                )
            else:
                # Set all leds for effect to static rbg
                rgb = leds.get_rgb_for_effect(base_effect)
                set_gate_rgb(rgb, unit, segment, gate)

        # Sets rgb value of gate led(s)
        def set_gate_rgb(rgb, unit, segment, gate, transmit=True):
            # Normally there is only a single led per gate but some designs have many
            for index, is_last in with_last(led_indexes(unit, segment, gate)):
                self.mmu.gcode.run_script_from_command(
                    "SET_LED LED=%s INDEX=%d RED=%s GREEN=%s BLUE=%s TRANSMIT=%d" % (
                        led_chain_spec(unit, segment), index, rgb[0], rgb[1], rgb[2], 1 if transmit and is_last else 0
                    )
                )

        # Stop any previous effect before setting rgb else it won't have an effect
        def stop_effect_and_set_gate_rgb(rgb, unit, segment, gate, fadetime=None):
            if fadetime:
                set_gate_rgb(rgb, unit, segment, gate)
                stop_gate_effect(unit, segment, gate, fadetime=fadetime)
            else:
                stop_gate_effect(unit, segment, gate)
                set_gate_rgb(rgb, unit, segment, gate)


        #
        # Process LED update...
        #
        try:
            mmu_unit = self.mmu_machine.get_mmu_unit_by_index(unit)
            if (
                not mmu_unit.leds or
                not mmu_unit.leds.enabled or
                (gate is not None and not mmu_unit.manages_gate(gate))
            ):
                # Ignore if unit doesn't have leds, is disabled for doesn't manage the specific gate
                # (saves callers from checking)
                return

            if gate is not None and gate < 0:
                return

            # Don't allow changes to shortcut animations - important changes will be seen when update timer fires
            if self.pending_update[unit]:
                return

            # Schedule a return to defaults after duration
            if duration is not None:
                self.schedule_led_command(duration, unit)

            #
            # Entry and Exit
            #
            for segment in ['exit', 'entry']:
                effect = get_effective_effect(mmu_unit, segment, effects[segment])

                # effect will be None if leds not configured for no led chain for that segment
                #if not effect or self.effect_state.get(unit, {}).get(segment) == effect:
                if not effect:
                    continue

                elif effect == "off":

                    stop_effect_and_set_gate_rgb((0,0,0), unit, segment, gate, fadetime=fadetime)

                elif effect == "gate_status":  # Filament availability (gate_map)

                    def _effect_for_gate(g):
                        # Selected gate, with filament past extruder entry: force 'gate_selected'
                        if g == self.mmu.gate_selected and self.mmu.filament_pos > self.mmu.FILAMENT_POS_EXTRUDER_ENTRY:
                            return self.effect_name(unit, 'gate_selected')

                        suffix = '_sel' if g == self.mmu.gate_selected else ''
                        status = self.mmu.gate_status[g]

                        if status == self.mmu.GATE_UNKNOWN:
                            key = 'gate_unknown'
                        elif status > self.mmu.GATE_EMPTY:
                            key = 'gate_available'
                        else:
                            key = 'gate_empty'

                        return self.effect_name(unit, '%s%s' % (key, suffix))

                    if gate is not None:
                        set_gate_effect(_effect_for_gate(gate), unit, segment, gate, fadetime=fadetime)
                    else:
                        for g in range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates):
                            set_gate_effect(_effect_for_gate(g), unit, segment, g, fadetime=fadetime)

                elif effect == "filament_color":

                    def _resolve_filament_rgb(g):
                        rgb = self.mmu.gate_color_rgb[g]
                        if self.mmu.gate_status[g] == self.mmu.GATE_EMPTY:
                            return mmu_unit.leds.empty_light
                        if self.mmu.gate_color[g] == "":
                            return mmu_unit.leds.white_light
                        if rgb == (0, 0, 0):
                            return mmu_unit.leds.black_light
                        return rgb

                    if gate is not None:
                        rgb = _resolve_filament_rgb(gate)
                        stop_effect_and_set_gate_rgb(rgb, unit, segment, gate)
                    else:
                        stop_gate_effect(unit, segment, None)
                        for g, is_last in with_last(range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates)):
                            rgb = _resolve_filament_rgb(g)
                            set_gate_rgb(rgb, unit, segment, g, transmit=is_last)

                elif effect == "slicer_color":

                    def _resolve_slicer_rgb(g):
                        rgb = self.mmu.slicer_color_rgb[g]
                        if self.mmu.gate_status[g] == self.mmu.GATE_EMPTY:
                            return mmu_unit.leds.empty_light
                        if rgb == (0, 0, 0):
                            return mmu_unit.leds.black_light
                        return rgb

                    if gate is not None:
                        rgb = _resolve_slicer_rgb(gate)
                        stop_effect_and_set_gate_rgb(rgb, unit, segment, gate)
                    else:
                        stop_gate_effect(unit, segment, None) # Stop all gates
                        for g, is_last in with_last(range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates)):
                            rgb = _resolve_slicer_rgb(g)
                            set_gate_rgb(rgb, unit, segment, g, transmit=is_last)

                elif isinstance(effect, tuple) or ',' in effect: # RGB color
                    rgb = MmuLeds.string_to_rgb(effect)
                    if gate is not None:
                        stop_effect_and_set_gate_rgb(rgb, unit, segment, gate)
                    else:
                        stop_gate_effect(unit, segment, None) # Stop all gates
                        for g, is_last in with_last(range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates)):
                            set_gate_rgb(rgb, unit, segment, g, transmit=is_last)

                elif effect != "": # Named effect
                    set_gate_effect(effect, unit, segment, gate, fadetime=fadetime)

                self.effect_state.setdefault(unit, {})[segment] = effect


            #
            # Status
            #
            segment = "status"
            effect = get_effective_effect(mmu_unit, segment, effects[segment])

            #if not effect or self.effect_state.get(unit, {}).get(segment) == effect:
            if not effect:
                pass

            elif effect == "off":

                stop_effect_and_set_gate_rgb((0,0,0), unit, segment, gate, fadetime=fadetime)
    
            elif effect in ["filament_color", "on"]:

                stop_gate_effect(unit, segment, None)
                rgb = mmu_unit.leds.white_light
                if self.mmu.gate_selected >= 0 and self.mmu.filament_pos > self.mmu.FILAMENT_POS_UNLOADED:
                    if effects[segment] != "on" and self.mmu.gate_color[self.mmu.gate_selected] != "":
                        rgb = self.mmu.gate_color_rgb[self.mmu.gate_selected]
                        if rgb == (0,0,0):
                            rgb = mmu_unit.leds.black_light
                else:
                    rgb = mmu_unit.leds.black_light
                set_gate_rgb(rgb, unit, segment, None)
    
            elif effect == "slicer_color":

                stop_gate_effect(unit, segment, None)
                rgb = (0,0,0)
                if self.mmu.gate_selected >= 0 and self.mmu.filament_pos > self.mmu.FILAMENT_POS_UNLOADED:
                    rgb = self.mmu.slicer_color_rgb[self.mmu.gate_selected]
                set_gate_rgb(rgb, unit, segment, None)
    
            elif isinstance(effect, tuple) or ',' in effect: # RGB color
                rgb = MmuLeds.string_to_rgb(effect)
                stop_effect_and_set_gate_rgb(rgb, unit, segment, None)
    
            elif effect != "": # Named effect
                set_gate_effect(effect, unit, segment, None, fadetime=fadetime)

            self.effect_state.setdefault(unit, {})[segment] = effect
    
            #
            # Logo
            #
            segment = "logo"
            effect = get_effective_effect(mmu_unit, segment, effects[segment])

            #if not effect or self.effect_state.get(unit, {}).get(segment) == effect:
            if not effect:
                pass

            elif effect == "off":

                stop_effect_and_set_gate_rgb((0,0,0), unit, segment, None, fadetime=fadetime)

            elif isinstance(effect, tuple) or ',' in effect: # RGB color
                rgb = MmuLeds.string_to_rgb(effect)
                stop_effect_and_set_gate_rgb(rgb, unit, segment, None)

            elif effect != "": # Named effect

                set_gate_effect(effect, unit, segment, None, fadetime=fadetime)

            self.effect_state.setdefault(unit, {})[segment] = effect

        except Exception as e:
            # Don't let a misconfiguration ruin a print!
            self.mmu.log_error("Error updating leds: %s" % str(e))
