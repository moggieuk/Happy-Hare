# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to centralize mmu_led operations across all mmu_units
# 
# One per-machine manager reacts to action / print-state / gate-map changes and drives each
# unit's LED segments (exit, entry, status, logo). All rendering funnels through _set_led(),
# which resolves an "effect" — a named [mmu_led_effect] animation, an "r,g,b" color, or a
# functional effect (off/on/gate_status/filament_color/slicer_color) — and, in static mode,
# falls back to a plain RGB. A timed effect (duration=) auto-returns that unit to its default
# via a per-unit timer (per-unit so a flash on one unit never disturbs another); an update
# arriving while a timed effect holds the unit is deferred (last one wins) and replayed when
# the timer fires. Two overlays are baked into the render: the selected-gate emphasis and the
# base spoolman "pending spool_id" phase (see _pending_overlay_effect / pending_changed).
#
# set_transient_effect() additionally lets a feature (e.g. the NFC reader indicators) flash a
# caller-owned effect on ONE segment through the same pipeline: the segment's prior effect is
# snapshotted and restored when the flash expires - unless something newer painted over the
# flash, in which case the restore self-cancels (newest wins). Flashes never block or reset
# other segments. This keeps feature-specific LED policy in the caller, not in this module.
#
# Supports commands:
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
import functools
import logging

# Happy Hare imports
from .mmu_constants import *
from .unit.mmu_leds import MmuLeds


class MmuLedManager:

    # Functional effects _set_led renders directly (no [mmu_led_effect] / RGB fallback needed)
    FUNCTIONAL_EFFECTS = ('off', 'on', 'gate_status', 'filament_color', 'slicer_color')

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu_machine = mmu.mmu_machine

        num_units = self.mmu_machine.num_units
        self.inside_timer = [False] * num_units    # Per-unit re-entrancy guard for its led timer
        self.pending_update = [False] * num_units
        self.deferred = [None] * num_units         # Per-unit last _set_led request blocked by a timed effect, replayed when it ends
        self.led_timers = [None] * num_units       # Per-unit "return to default" timers (registered at ready)
        self.effect_state = {} # Current state used to minimise updates {unit: {segment: effect}}
        self.transient_flash = {}  # Active transient flash per (unit, segment): {'prior', 'flash', 'gate'}
        self.transient_timers = {} # Lazily registered restore timer per (unit, segment)
        self.transient_pending = {} # Optional queued flash per (unit, segment), promoted when the active flash ends
        self._initialized = False # Used to prevent very early calls before leds are fully initialized

        # Event handlers
        self.mmu.printer.register_event_handler("klippy:ready", self.handle_ready)


    def handle_ready(self):
        self.setup_led_timer()


    def setup_led_timer(self):
        # A separate timer per unit so a timed effect on one unit cannot reset or cut short
        # the LEDs on another - each unit owns its own "return to default" schedule.
        for unit in range(self.mmu_machine.num_units):
            self.led_timers[unit] = self.mmu.reactor.register_timer(
                functools.partial(self.led_timer_handler, unit), self.mmu.reactor.NEVER)


    def led_timer_handler(self, unit, eventtime):
        self.inside_timer[unit] = True
        next_wake = self.mmu.reactor.NEVER
        try:
            self.pending_update[unit] = False
            self._set_led(unit, None, exit_effect='default', entry_effect='default', status_effect='default', logo_effect='default')

            # Replay the last update that arrived while the timed effect was held (over the
            # reset, so its segments win and the rest stay at default). If that update itself
            # carried a duration, re-arm this timer via the return value (schedule_led_command
            # is a no-op while inside_timer, and update_timer here would be clobbered by the
            # returned waketime anyway).
            deferred, self.deferred[unit] = self.deferred[unit], None
            if deferred is not None:
                self._set_led(unit, deferred['gate'], fadetime=deferred['fadetime'], **deferred['effects'])
                if deferred['duration'] is not None:
                    self.pending_update[unit] = True
                    next_wake = self.mmu.reactor.monotonic() + deferred['duration']
        finally:
            self.inside_timer[unit] = False
        return next_wake


    def schedule_led_command(self, duration, unit):
        if not self.inside_timer[unit]:
            self.pending_update[unit] = True
            self.mmu.reactor.update_timer(self.led_timers[unit], self.mmu.reactor.monotonic() + duration)


    # Called when an action has changed to update LEDs
    # (this could be changed to klipper event)
    def action_changed(self, action, old_action):
        gate = self.mmu.gate_selected

        # Check for unit specific actions
        if action in [ACTION_HOMING, ACTION_SELECTING]:
            units_to_update = [self.mmu.unit_selected]
        else:
            units_to_update = range(self.mmu_machine.num_units)

        for unit in units_to_update:

            # Load sequence...
            # idle -> loading -> load_ext -> [heat -> load_ext] -> loading* -> purging* -> loading* -> idle (*=excluded)

            if action == ACTION_LOADING:
                if old_action not in [ACTION_LOADING_EXTRUDER, ACTION_PURGING]:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'loading'),
                        status_effect=self.effect_name(unit, 'loading'),
                        fadetime=0.5
                    )

            elif action == ACTION_LOADING_EXTRUDER:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'loading_extruder'),
                    status_effect=self.effect_name(unit, 'loading_extruder'),
                    fadetime=0.5
                )

            elif action == ACTION_PURGING:
                pass

            # Unload sequence...
            # idle -> unloading -> form_tip/cut -> [heat -> form_tip/cut] -> unloading* -> unload_ext -> unloading
            # [cutting* -> unloading*] -> idle (*=excluded)
            elif action == ACTION_UNLOADING:
                if old_action == ACTION_IDLE:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'unloading_extruder'),
                        status_effect=self.effect_name(unit, 'unloading_extruder'),
                        fadetime=0.5
                    )

                elif old_action not in [
                    ACTION_FORMING_TIP,
                    ACTION_CUTTING_FILAMENT
                ]:
                    self._set_led(
                        unit, gate,
                        exit_effect=self.effect_name(unit, 'unloading'),
                        status_effect=self.effect_name(unit, 'unloading'),
                        fadetime=0.5
                    )

            elif action == ACTION_UNLOADING_EXTRUDER:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'unloading_extruder'),
                    status_effect=self.effect_name(unit, 'unloading_extruder'),
                    fadetime=0.5
                )

            elif action in [ACTION_FORMING_TIP, ACTION_CUTTING_TIP]:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'unloading_extruder'),
                    status_effect=self.effect_name(unit, 'unloading_extruder'),
                    fadetime=0.5
                )

            elif action == ACTION_CUTTING_FILAMENT:
                pass

            # Other actions...

            elif action == ACTION_HEATING:
                self._set_led(
                    unit, gate,
                    exit_effect=self.effect_name(unit, 'heating'),
                    status_effect=self.effect_name(unit, 'heating')
                )

            elif action == ACTION_IDLE:
                self._set_led(
                    unit, None,
                    exit_effect='default',
                    status_effect='default'
                )

            # Type-A MMU actions involving selector (unit specific)...

            # idle -> home -> select -> home* -> idle (*=excluded)
            elif action == ACTION_HOMING:
                if old_action == ACTION_IDLE:
                    self._set_led(
                        unit, None,
                        exit_effect=self.effect_name(unit, 'selecting'),
                        status_effect=self.effect_name(unit, 'selecting'),
                        fadetime=0
                    )

            # idle -> select -> idle
            elif action == ACTION_SELECTING:
                if old_action not in [ACTION_CHECKING, ACTION_PRELOAD]:
                    self._set_led(
                        unit, None,
                        exit_effect='default',
                        status_effect=self.effect_name(unit, 'selecting'),
                        fadetime=0
                    )

            # idle -> check/preload -> select* -> check* -> select* -> check* -> idle
            # (preload has its own effect_preloading; checking uses effect_checking)
            elif action in [ACTION_CHECKING, ACTION_PRELOAD]:
                if old_action == ACTION_IDLE:
                    operation = 'preloading' if action == ACTION_PRELOAD else 'checking'
                    self._set_led(
                        unit, None,
                        exit_effect='default',
                        status_effect=self.effect_name(unit, operation)
                    )


    # Called when print state changes to update LEDs
    # (this could be changed to klipper event)
    def print_state_changed(self, state, old_state):
        self._initialized = True # First call is in MMU_BOOTUP
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
                    duration=self.effect_duration(unit, 'initialized', 8)
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
                    duration=self.effect_duration(unit, 'complete', 10)
                )

            elif state == "error":
                self._set_led(
                    unit, None,
                    exit_effect=self.effect_name(unit, 'error'),
                    status_effect='default',
                    duration=self.effect_duration(unit, 'error', 10)
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
    def gate_map_changed(self, gate):
        if not self._initialized: return

        gate = gate if (gate is None or gate >= 0) else None

        gate_effects = {'gate_status', 'filament_color', 'slicer_color'}
        units = [self.mmu.mmu_unit(gate)] if gate is not None else self.mmu_machine.units
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


    def effect_duration(self, unit, operation, default=None):
        """
        Return the config-specified default duration (3rd field of effect_<operation> in [mmu_leds]) for
        'operation', or 'default' when the config omits it
        """
        leds = self.mmu_machine.get_mmu_unit_by_index(unit).leds
        if leds:
            duration = leds.get_duration(operation)
            if duration is not None:
                return duration
        return default


    def _pending_overlay_effect(self, mmu_unit, segment):
        """
        Base spoolman pending-spool_id overlay for 'segment', or None. Baked into the
        render (not a transient), so any pending spool_id - from an NFC lookup OR a manual
        MMU_GATE_MAP NEXT_SPOOLID - shows on the segments chosen by the machine param
        spoolman_led_segment (gate_status | status | both). Returns '' -> None so it's opt-in
        (map effect_pending_spoolid[_expiring] to enable it).
        """
        phase = self.mmu.pending_phase
        if not phase:
            return None
        mode = self.mmu.p.spoolman_led_segment
        if segment in ('exit', 'entry'):
            if mode not in ('gate_status', 'both'):
                return None
        elif segment == 'status':
            if mode not in ('status', 'both'):
                return None
        else:
            return None
        op = 'pending_spoolid_expiring' if phase == 'expiring' else 'pending_spoolid'
        return self.effect_name(mmu_unit.unit_index, op) or None


    def pending_changed(self):
        """
        Re-render each unit's overlay-carrying segments when the pending spool_id phase
        changes (set/expiring/cleared). A full default refresh picks up pending_phase in the
        render branches; the overlay reverts automatically once the phase returns to None.
        """
        if not self._initialized:
            return
        for unit in range(self.mmu_machine.num_units):
            self._set_led(unit, None, exit_effect='default', entry_effect='default', status_effect='default')


    def set_transient_effect(self, mmu_unit, effect, segment='exit', gate=None, duration=None, fadetime=0, defer=False):
        """
        Apply a caller-owned effect to one segment via the normal LED pipeline - used for
        short-lived feature indicators (e.g. the NFC read/fail flashes) without this module
        knowing anything about the feature.

        'effect' is a resolved value understood by _set_led: a named [mmu_led_effect], an
        "r,g,b" colour, or a functional effect (off/on/gate_status/filament_color/
        slicer_color). Segment availability, gate ownership and animation-vs-static handling
        are all handled by _set_led.

        With a 'duration' this is a segment-scoped flash: what the segment was showing is
        snapshotted first and restored when the duration expires - but ONLY if the flash is
        still what's showing (anything painted over it in the meantime wins and the restore
        self-cancels). Other segments are never touched and updates are never blocked.
        Back-to-back flashes on the same segment keep the original pre-flash snapshot.
        With defer=True a flash requested while another is still running on the segment is
        queued and painted when that one ends (last queued wins) rather than cutting it short
        - e.g. so a fast NFC fail result doesn't stomp the read-acknowledge flash.
        With duration=None the effect simply persists until the next repaint of that
        segment (no restore bookkeeping).

        Returns True if dispatched, else False (no/disabled leds, bad segment, a named
        effect with no static RGB fallback while animation is off, or a flash requested
        while a unit-wide timed state effect holds the unit - a cosmetic flash is dropped
        rather than deferred, which would leave it with no way to clear).
        """
        if mmu_unit is None or not effect:
            return False
        segment = (segment or 'exit').strip().lower()
        if segment not in MmuLeds.SEGMENTS:
            return False
        leds = mmu_unit.leds
        if leds is None or not leds.enabled:
            return False

        # In static (non-animation) mode a bare named effect needs an RGB fallback to
        # render; RGB literals and functional effects are always renderable by _set_led,
        # so only reject an unmapped *named* effect.
        is_rgb = isinstance(effect, tuple) or (isinstance(effect, str) and ',' in effect)
        if (not leds.animation and not is_rgb
                and effect not in self.FUNCTIONAL_EFFECTS
                and effect not in leds.get_effect_names()):
            logging.warning("MMU: LED effect '%s' has no static RGB mapping for unit %s" % (effect, mmu_unit.name))
            return False

        unit = mmu_unit.unit_index
        key = (unit, segment)
        if duration is None:
            # Persistent: paint and walk away (ends at the next repaint of the segment). A
            # fresh paint abandons any flash queued behind a now-superseded sequence.
            self.transient_pending.pop(key, None)
            self._set_led(unit, gate, fadetime=fadetime, **{'%s_effect' % segment: effect})
            return True

        # Flash: drop (don't defer) while a unit-wide timed state effect (initialized/
        # complete/error) holds the unit - a deferred flash would replay at its end with
        # nothing scheduled to clear it, and a cosmetic indicator isn't worth extending
        if self.pending_update[unit]:
            self.mmu.log_trace("LED: transient '%s' flash dropped - unit %d held by a timed state effect" % (effect, unit))
            return False

        entry = self.transient_flash.get(key)
        if entry is not None and defer:
            # A flash is still running on this segment; queue this one to paint when it ends
            # (last queued wins) instead of cutting the running flash short.
            self.transient_pending[key] = {'effect': effect, 'gate': gate, 'duration': duration, 'fadetime': fadetime}
            self.mmu.log_trace("LED: transient '%s' flash queued behind active '%s' on unit %d/%s" % (effect, entry['flash'], unit, segment))
            return True

        # Immediate paint. A fresh (non-deferred) flash abandons any queued deferral - it
        # belongs to a superseded sequence.
        self.transient_pending.pop(key, None)
        if entry is None:
            # Snapshot what the segment is showing BEFORE the flash paints (first-wins so
            # chained flashes, e.g. read -> fail, restore the true pre-flash baseline)
            prior = self.effect_state.get(unit, {}).get(segment, 'default')
        else:
            prior = entry['prior']

        # Paint through the normal pipeline WITHOUT a _set_led duration: no unit-wide
        # reset, no pending_update lock - other segments and later updates stay live
        self._set_led(unit, gate, fadetime=fadetime, **{'%s_effect' % segment: effect})

        self.transient_flash[key] = {'prior': prior, 'flash': effect, 'gate': gate}
        if key not in self.transient_timers:
            self.transient_timers[key] = self.mmu.reactor.register_timer(
                functools.partial(self._transient_flash_handler, unit, segment), self.mmu.reactor.NEVER)
        self.mmu.reactor.update_timer(self.transient_timers[key], self.mmu.reactor.monotonic() + duration)
        return True


    def _transient_flash_handler(self, unit, segment, eventtime):
        """End a segment-scoped transient flash: restore the pre-flash effect, but only if
        the flash is still what the segment is showing. If anything painted over it (action
        transition, pending overlay, state change - none of which are blocked by a flash),
        the newer effect wins and the restore self-cancels (and any queued flash is dropped).
        If a flash was queued behind this one (defer=True) and this flash is still showing,
        promote it: paint it over this flash and re-arm for its duration, keeping the ORIGINAL
        pre-flash baseline so the chain (e.g. read -> fail -> baseline) restores correctly."""
        key = (unit, segment)
        entry = self.transient_flash.pop(key, None)
        pending = self.transient_pending.pop(key, None)
        if entry is None or self.effect_state.get(unit, {}).get(segment) != entry['flash']:
            self.mmu.log_trace("LED: transient flash end on unit %d/%s self-cancelled (overpainted: showing '%s')%s" % (
                unit, segment, self.effect_state.get(unit, {}).get(segment),
                (" - dropping queued '%s'" % pending['effect']) if pending else ""))
            return self.mmu.reactor.NEVER # Something newer won - self-cancel
        if pending is not None:
            self.mmu.log_trace("LED: promoting queued '%s' flash on unit %d/%s for %.1fs" % (pending['effect'], unit, segment, pending['duration']))
            self._set_led(unit, pending['gate'], fadetime=pending['fadetime'], **{'%s_effect' % segment: pending['effect']})
            self.transient_flash[key] = {'prior': entry['prior'], 'flash': pending['effect'], 'gate': pending['gate']}
            return self.mmu.reactor.monotonic() + pending['duration'] # Re-arm for the promoted flash
        self.mmu.log_trace("LED: transient flash end on unit %d/%s - restoring '%s'" % (unit, segment, entry['prior']))
        self._set_led(unit, entry['gate'], fadetime=0, **{'%s_effect' % segment: entry['prior']})
        return self.mmu.reactor.NEVER


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
                        (f'FADETIME={fadetime}') if fadetime is not None else ''
                    )
                )

        # Sets or replaces effect on the gate led(s)
        def set_gate_effect(base_effect, unit, segment, gate, fadetime=None, raw=False):
            leds = self.mmu_machine.get_mmu_unit_by_index(unit).leds
            if leds.animation:
                e_str = effect if raw else effect_spec(unit, gate, "%s_%s" % (base_effect, segment))
                self.mmu.gcode.run_script_from_command(
                    "_MMU_SET_LED_EFFECT EFFECT='%s' REPLACE=1 %s" % (
                        e_str,
                        (f'FADETIME={fadetime}') if fadetime is not None else ''
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

            # A timed effect is currently held on this unit. Don't paint over it now; instead
            # remember this request (last one wins) and replay it when the timed effect's timer
            # fires - honoring "important changes will be seen when the update timer fires".
            if self.pending_update[unit]:
                self.deferred[unit] = {
                    'gate': gate,
                    'fadetime': fadetime,
                    'duration': duration,
                    'effects': {
                        'exit_effect': exit_effect,
                        'entry_effect': entry_effect,
                        'status_effect': status_effect,
                        'logo_effect': logo_effect,
                    },
                }
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
                    pending_overlay = self._pending_overlay_effect(mmu_unit, segment)
                    def _effect_for_gate(g, pending_overlay=pending_overlay):
                        # Base spoolman pending overlay (if active) supersedes per-gate availability
                        if pending_overlay:
                            return pending_overlay
                        # Selected gate, with filament past extruder entry: force 'gate_selected'
                        if g == self.mmu.gate_selected and self.mmu.filament_pos > FILAMENT_POS_EXTRUDER_ENTRY:
                            return self.effect_name(unit, 'gate_selected')

                        suffix = '_sel' if g == self.mmu.gate_selected else ''
                        status = self.mmu.gate_status[g]

                        if status == GATE_UNKNOWN:
                            key = 'gate_unknown'
                        elif status > GATE_EMPTY:
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
                        if self.mmu.gate_status[g] == GATE_EMPTY:
                            return mmu_unit.leds.empty_light
                        if self.mmu.gate_color[g] == "":
                            return mmu_unit.leds.white_light
                        if rgb == (0, 0, 0):
                            return mmu_unit.leds.black_light
                        return MmuLeds.apply_intensity(rgb, mmu_unit.leds.filament_color_intensity)

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
                        if self.mmu.gate_status[g] == GATE_EMPTY:
                            return mmu_unit.leds.empty_light
                        if rgb == (0, 0, 0):
                            return mmu_unit.leds.black_light
                        return MmuLeds.apply_intensity(rgb, mmu_unit.leds.filament_color_intensity)

                    if gate is not None:
                        rgb = _resolve_slicer_rgb(gate)
                        stop_effect_and_set_gate_rgb(rgb, unit, segment, gate)
                    else:
                        stop_gate_effect(unit, segment, None) # Stop all gates
                        for g, is_last in with_last(range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates)):
                            rgb = _resolve_slicer_rgb(g)
                            set_gate_rgb(rgb, unit, segment, g, transmit=is_last)

                elif isinstance(effect, tuple) or (isinstance(effect, str) and ',' in effect): # RGB color
                    rgb = MmuLeds.string_to_rgb(effect)
                    if gate is not None:
                        stop_effect_and_set_gate_rgb(rgb, unit, segment, gate)
                    else:
                        stop_gate_effect(unit, segment, None) # Stop all gates
                        for g, is_last in with_last(range(mmu_unit.first_gate, mmu_unit.first_gate + mmu_unit.num_gates)):
                            set_gate_rgb(rgb, unit, segment, g, transmit=is_last)

                elif effect != "": # Named effect
                    set_gate_effect(effect, unit, segment, gate, fadetime=fadetime, raw=False)

                self.effect_state.setdefault(unit, {})[segment] = effect


            #
            # Status
            #
            segment = "status"
            effect = get_effective_effect(mmu_unit, segment, effects[segment])
            # Base spoolman pending overlay supersedes the configured status effect (only when
            # the status segment is available, i.e. effect resolved to non-empty)
            pending_overlay = self._pending_overlay_effect(mmu_unit, segment) if effect else None

            #if not effect or self.effect_state.get(unit, {}).get(segment) == effect:
            if pending_overlay:
                # Note: effect_state still records the underlying configured effect (like the
                # exit branch does for gate_status) - the overlay is transient render state
                set_gate_effect(pending_overlay, unit, segment, None, fadetime=fadetime)

            elif not effect:
                pass

            elif effect == "off":
                stop_effect_and_set_gate_rgb((0,0,0), unit, segment, gate, fadetime=fadetime)
    
            elif effect in ["filament_color", "on"]:
                stop_gate_effect(unit, segment, None)
                rgb = mmu_unit.leds.white_light
                if self.mmu.gate_selected >= 0 and self.mmu.filament_pos > FILAMENT_POS_UNLOADED:
                    if effects[segment] != "on" and self.mmu.gate_color[self.mmu.gate_selected] != "":
                        rgb = self.mmu.gate_color_rgb[self.mmu.gate_selected]
                        if rgb == (0,0,0):
                            rgb = mmu_unit.leds.black_light
                        elif effect == "filament_color":
                            rgb = MmuLeds.apply_intensity(rgb, mmu_unit.leds.filament_color_intensity)
                else:
                    rgb = mmu_unit.leds.black_light
                set_gate_rgb(rgb, unit, segment, None)
    
            elif effect == "slicer_color":
                stop_gate_effect(unit, segment, None)
                rgb = (0,0,0)
                if self.mmu.gate_selected >= 0 and self.mmu.filament_pos > FILAMENT_POS_UNLOADED:
                    rgb = self.mmu.slicer_color_rgb[self.mmu.gate_selected]
                    rgb = MmuLeds.apply_intensity(rgb, mmu_unit.leds.filament_color_intensity)
                set_gate_rgb(rgb, unit, segment, None)
    
            elif isinstance(effect, tuple) or (isinstance(effect, str) and ',' in effect): # RGB color
                rgb = MmuLeds.string_to_rgb(effect)
                stop_effect_and_set_gate_rgb(rgb, unit, segment, None)
    
            elif effect != "": # Named effect
                set_gate_effect(effect, unit, segment, None, fadetime=fadetime, raw=False)

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

            elif isinstance(effect, tuple) or (isinstance(effect, str) and ',' in effect): # RGB color
                rgb = MmuLeds.string_to_rgb(effect)
                stop_effect_and_set_gate_rgb(rgb, unit, segment, None)

            elif effect != "": # Named effect
                set_gate_effect(effect, unit, segment, None, fadetime=fadetime, raw=False)

            self.effect_state.setdefault(unit, {})[segment] = effect

        except Exception as e:
            # Don't let a misconfiguration ruin a print!
            self.mmu.log_error("Error updating leds: %s" % str(e))
