# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to implement MMU heater control and basic filament drying functionality
#
# Two setups are supported:
#  1. The more normal shared enclosure with single heater and environment sensor. In this case
#     'filament_heater' and 'environment_sensor' properties should be set. Direct heater or
#     drying lifecycle control is possible. An optional venting macro will periodically be called
#     with no arguments.
#  2. Where each MMU gate has a separate heater/environment sensor (e.g. EMU design). Here it
#     is possible to specify which gates to dry. The list of heaters and environment sensors
#     should be set with the 'filament_heaters' and 'environment_sensors' properties.
#     Further, in this mode a basic "power management" is implemented which limits the number
#     of simulateous heaters to that defined by the 'max_concurrent_heaters' property.
#     Individual control of per-gate heaters and lifecycle is possible by specifying gates of
#     interest. The periodic venting macro will be called with a GATE parameter listing the
#     currently heated gates.
#
# The manager will support automatic spool rotation if equiped with eSpooler and the dry cycle
# is initiated with this option. IMPORTANT: filament must be removed from the MMU inlet and
# fastened to the spool and the GATES parameter must be supplied.
#
# Implements printer variables:
#   drying_state   [{string} : list indexed by gate with values:
#                                DRYING_STATE_ACTIVE    'active'    actively drying
#                                DRYING_STATE_QUEUED    'queued'    waiting to start
#                                DRYING_STATE_COMPLETE  'complete'  completed drying
#                                DRYING_STATE_CANCELLED 'cancelled' cycle was canceled prematurely
#                                DRYING_STATUS_NONE     ''          not part of the current cycle
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Happy Hare imports
from ..mmu_constants import *
from ..mmu_utils     import MmuError

ENV_CHECK_INTERVAL = 30 # How often to check heater and environment sensors (seconds)

# Environment sensor chips with humidity
ENV_SENSOR_CHIPS = ["bme280", "htu21d", "sht3x", "lm75", "aht10"]


class MmuEnvironmentManager:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        # Listen of important mmu events
        self.printer.register_event_handler("mmu:enabled", self._handle_mmu_enabled)
        self.printer.register_event_handler("mmu:disabled", self._handle_mmu_disabled)
        self.printer.register_event_handler("mmu:espooler_burst_done", self._handle_espooler_burst_done)

        self._periodic_timer = self.reactor.register_timer(self._check_mmu_environment)
        self.reinit()

        # Register event handlers
        self.printer.register_event_handler('klippy:connect', self._handle_connect)


    def reinit(self):
        self._drying_temp = None
        self._drying_humidity_target = None
        self._drying_start_time = self._drying_end_time = None
        self._drying_gates = []
        self._drying_vent_interval = None

        # Per-gate drying state (multi-heater mode)
        # gate -> dict(state, start_time, end_time, temp, humidity_target, done_reason, last_temp, last_humidity)
        self._gate_drying = {}    # Contains details required for managing drying for scheduled gates

        # Drying state indexed by local gate
        self._drying_state = [DRYING_STATE_NONE] * self.mmu_unit.num_gates

        self._drying_queue = []   # Queued gates awaiting heater capacity (FIFO)
        self._vent_timer = None

        # Optional auto spool rotation (eSpooler)
        self._rotate_timer = None
        self._rotate_enabled = False
        self.spools_to_rotate = [] # Queue of spools that we are rotating (one at a time)


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    #
    # Mmu Heater manager public access...
    #


    def prepare_drying_cycle(self, gates, timer=None, temp=None, humidity=None,
                             vent_interval=None, rotate=False, rotate_interval=None):
        """
        Configure manager state for a new drying cycle.

        Returns:
          per_gate_plan, warnings, info
        """
        warnings = []
        info = []

        if self.is_drying():
            raise MmuError("MMU already in filament drying cycle. Stop current cycle first")

        per_gate_plan = self._get_drying_plan(gates)

        if timer is not None and self.has_per_gate_heaters():
            for gate in gates:
                per_gate_plan[gate]['timer'] = timer

        if temp is not None:
            for gate in gates:
                rec_temp = per_gate_plan[gate]['temp']
                material = per_gate_plan[gate]['material']
                if temp > rec_temp:
                    if material:
                        warnings.append(
                            "Warning: Gate %d drying temperature %.1f°C is greater than that recommended for %s (%.1f°C)"
                            % (gate, temp, material, rec_temp)
                        )
                    else:
                        warnings.append(
                            "Warning: Gate %d has unknown filament type. Cannot validate temperature %.1f°C"
                            % (gate, temp)
                        )
                per_gate_plan[gate]['temp'] = temp

        else:
            lowest = self.mmu_unit.p.heater_default_dry_temp
            longest = self.mmu_unit.p.heater_default_dry_time

            for gate in gates:
                lowest = min(lowest, per_gate_plan[gate]['temp'])
                longest = max(longest, per_gate_plan[gate]['timer'])

            if not self.has_per_gate_heaters():
                temp = lowest
                if timer is None:
                    timer = longest
                    info_word = "longest"
                else:
                    info_word = "specified"

                info.append(
                    "Defaulting to lowest drying temperature of %.1f°C for %s drying time given filaments types currently in MMU"
                    % (temp, info_word)
                )

        self._drying_time = timer or self.mmu_unit.p.heater_default_dry_time
        self._drying_temp = temp or self.mmu_unit.p.heater_default_dry_temp
        self._drying_humidity_target = humidity
        self._drying_start_time = self.reactor.monotonic()
        self._drying_end_time = self._drying_start_time + self._drying_time * 60
        self._drying_gates = list(gates)
        self._drying_vent_interval = vent_interval
        self._drying_rotate_interval = rotate_interval or self.mmu_unit.p.heater_rotate_interval

        self.spools_to_rotate = []
        self._rotate_enabled = bool(rotate)
        self._rotate_timer = self._drying_rotate_interval * 60.0 if self._rotate_enabled else None

        return per_gate_plan, warnings, info


    def start_drying_cycle(self, gates, timer=None, temp=None, humidity=None,
                           vent_interval=None, rotate=False, rotate_interval=None):
        per_gate_plan, warnings, info = self.prepare_drying_cycle(
            gates, timer=timer, temp=temp, humidity=humidity,
            vent_interval=vent_interval, rotate=rotate,
            rotate_interval=rotate_interval,
        )
        self._start_drying_cycle(per_gate_plan)
        return warnings, info


    def stop_or_cancel(self, gates=None):
        """
        Stop heater/drying. In per-gate mode with gates, cancels only those gates.
        Returns a short result string for the command to log.
        """
        if self.has_per_gate_heaters():
            gates = list(gates or self.mmu_unit.gate_range())

            if self.is_drying():
                cancelled = self._cancel_gates(gates, reason="cancelled")

                if self._all_selected_gates_done():
                    self._stop_drying_cycle(
                        "Drying cycle stopped (all selected gates cancelled)",
                        reset_state=True,
                    )

                if cancelled:
                    return "Cancelled drying for gates: %s" % ",".join(map(str, gates))
                return "No matching active/queued gates to cancel"

            for gate in gates:
                self._heater_off(gate=gate)
            return "Selected gate heaters turned off"

        if self.is_drying():
            self._stop_drying_cycle(reset_state=True)
            return "Cancelled drying cycle"

        self._heater_off()
        return "Heater turned off"


    def _all_selected_gates_done(self):
        for gate in self._drying_gates:
            gd = self._gate_drying.get(gate)
            if not gd or gd.get('state') not in [DRYING_STATE_COMPLETE, DRYING_STATE_CANCELLED]:
                return False
        return True


    def has_per_gate_heaters(self):
        """
        Returns whether this MMU configuration has a separate heater for each gate
        and corresponding environment sensor per gate.
        """
        return bool(self.mmu_unit.filament_heaters and self.mmu_unit.environment_sensors)


    def get_heater_snapshot(self):
        """
        Return current heater status for command/status reporting.
        """
        if self.has_per_gate_heaters():
            heaters_on = []
            heaters_off = []

            for gate in self.mmu_unit.gate_range():
                cur_temp, cur_target = self._get_heater_status(gate)
                if cur_target is None:
                    continue

                if cur_target != 0:
                    heaters_on.append({
                        'gate': gate,
                        'temperature': cur_temp,
                        'target': cur_target,
                    })
                else:
                    heaters_off.append(gate)

            return {
                'per_gate': True,
                'heaters_on': heaters_on,
                'heaters_off': heaters_off,
            }

        cur_temp, cur_target = self._get_heater_status()
        return {
            'per_gate': False,
            'temperature': cur_temp,
            'target': cur_target,
        }


    def get_drying_snapshot(self):
        """
        Return current drying status for command/status reporting.
        """
        now = self.reactor.monotonic()

        snapshot = {
            'is_drying': self.is_drying(),
            'per_gate': self.has_per_gate_heaters(),
            'gates': list(self._drying_gates),
            'time': self._drying_time,
            'temp': self._drying_temp,
            'humidity_target': self._drying_humidity_target,
            'remaining': max(0, self._drying_end_time - now) if self._drying_end_time is not None else None,
            'vent_timer': self._vent_timer,
            'vent_interval': self._drying_vent_interval,
            'rotate_enabled': self._rotate_enabled,
            'rotate_timer': self._rotate_timer,
            'rotate_interval': self._drying_rotate_interval,
        }

        if self.has_per_gate_heaters():
            gate_drying = []
            for gate in self._drying_gates:
                gd = self._gate_drying.get(gate, {})
                item = dict(gd)
                item['gate'] = gate

                end_time = gd.get('end_time')
                if end_time is not None and gd.get('state') == DRYING_STATE_ACTIVE:
                    item['remaining'] = max(0, end_time - now)
                else:
                    item['remaining'] = None

                gate_drying.append(item)

            snapshot['gate_drying'] = gate_drying

        else:
            cur_temp, cur_humidity = self._get_environment_status()
            heater_temp, heater_target = self._get_heater_status()

            snapshot['environment_temp'] = cur_temp
            snapshot['environment_humidity'] = cur_humidity
            snapshot['heater_temp'] = heater_temp
            snapshot['heater_target'] = heater_target

        return snapshot


    def set_heater_target(self, temp, gates=None):
        """
        Raw heater control. Updates drying state if selected gates are part of
        an active drying cycle.
        """
        if not self.has_per_gate_heaters():
            self._heater_on(temp)
            if self.is_drying():
                self._drying_temp = temp
            return

        gates = list(gates or [])

        if len(gates) > self.mmu_unit.max_concurrent_heaters:
            raise MmuError("Exceeded max concurrent heaters")

        for gate in gates:
            gd = self._gate_drying.get(gate)

            if self.is_drying() and gd is not None:
                gd['temp'] = temp
                if gd.get('state') == DRYING_STATE_QUEUED:
                    continue

            self._heater_on(temp, gate=gate)


    def is_drying(self):
        """
        Returns whether the MMU heater is currently in drying cycle
        """
        return any(s in [DRYING_STATE_ACTIVE, DRYING_STATE_QUEUED]
           for s in self._drying_state)


    def has_heater(self):
        if self.has_per_gate_heaters():
            heaters = self.mmu_unit.filament_heaters
            return bool(heaters) # At least one heater configured
        return self.mmu_unit.filament_heater != ''


    def has_env_sensor(self):
        if self.has_per_gate_heaters():
            sensors = self.mmu_unit.environment_sensors
            return bool(sensors)
        return self.mmu_unit.environment_sensor != ''


    def _get_active_gates(self):
        """
        Return list of active gates from per-gate drying states
        """
        return [self.mmu_unit.logical_gate(i) for i, s in enumerate(self._drying_state) if s == DRYING_STATE_ACTIVE]


    def get_status(self, eventtime=None):
        """
        Structured status for client consumption.
        We don't duplicate temperature or humidity data here but expect the client to read configuration
        and look up appropriate heator and environemnt sensor objects directly
        """
        return {
            'drying_state': self._drying_state
        }


    #
    # Internal implementation --------------------------------------------------
    #

    def _handle_mmu_disabled(self):
        """
        Event indicating that the MMU unit was disabled
        """
        self._stop_drying_cycle(reset_state=True)
        self._heater_off()
        self.spools_to_rotate = []


    def _handle_mmu_enabled(self):
        """
        Event indicating that the MMU unit was enabled
        """
        self.reactor.update_timer(self._periodic_timer, self.reactor.NEVER)
        self.reinit()


    def _state_get(self, gate):
        """
        Get drying state for a global (logical) gate.
        """
        return self._drying_state[self.mmu_unit.local_gate(gate)]


    def _state_set(self, gate, state):
        """
        Set drying state for a global (logical) gate.
        """
        self._drying_state[self.mmu_unit.local_gate(gate)] = state


    def _check_mmu_environment(self, eventtime):
        """
        Reactor callback to periodically check drying status and to rationalize state
        """
        if not self.is_drying():
            return self.reactor.NEVER

        now = self.reactor.monotonic()

        # Per-gate drying mode
        if self.has_per_gate_heaters():
            # Update active gates: check completion / humidity threshold
            completed_any = False
            for gate in list(self._get_active_gates()):
                gd = self._gate_drying.get(gate)
                if not gd or gd.get('state') != DRYING_STATE_ACTIVE:
                    continue

                # Read environment sensor
                cur_temp, cur_humidity = self._get_environment_status(gate=gate)
                gd['last_temp'] = cur_temp
                gd['last_humidity'] = cur_humidity

                # Cycle complete (per gate)
                if gd.get('end_time') is not None and (gd['end_time'] - now) <= 0:
                    self._heater_off(gate=gate)
                    gd['state'] = DRYING_STATE_COMPLETE
                    gd['done_reason'] = 'timer complete'
                    completed_any = True
                    try:
                        self._state_set(gate, DRYING_STATE_COMPLETE)
                    except Exception:
                        pass
                    continue

                # Humidity goal reached (per gate)
                if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                    self._heater_off(gate=gate)
                    gd['state'] = DRYING_STATE_COMPLETE
                    gd['done_reason'] = 'humidity goal reached'
                    completed_any = True
                    try:
                        self._state_set(gate, DRYING_STATE_COMPLETE)
                    except Exception:
                        pass
                    continue

            # If any heater slots freed, start next queued gates
            if completed_any:
                self._start_next_queued_gates(now)

            # If all gates done, stop overall drying cycle
            all_done = True
            for gate in self._drying_gates:
                gd = self._gate_drying.get(gate)
                if not gd or gd.get('state') not in [DRYING_STATE_COMPLETE, DRYING_STATE_CANCELLED]:
                    all_done = False
                    break
            if all_done:
                self._stop_drying_cycle("Drying cycle complete (all gates)", reset_state=False)
                return self.reactor.NEVER

        else: # Single heater mode

            # Cycle complete?
            if (self._drying_end_time - now) <= 0:
                cur_temp, cur_humidity = self._get_environment_status()
                for gate in self.mmu_unit.gate_range():
                    try:
                        if self._state_get(gate) == DRYING_STATE_ACTIVE:
                            self._state_set(gate, DRYING_STATE_COMPLETE)
                    except Exception:
                        pass

                if cur_humidity is not None:
                    msg = "Drying cycle complete. Final humidity: %.1f%%" % cur_humidity
                else:
                    msg = "Drying cycle complete. Final humidity: unknown"
                self._stop_drying_cycle(msg, reset_state=False)

                return self.reactor.NEVER

            # Humidity goal reached?
            cur_temp, cur_humidity = self._get_environment_status()
            if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                for gate in self.mmu_unit.gate_range():
                    try:
                        if self._state_get(gate) == DRYING_STATE_ACTIVE:
                            self._state_set(gate, DRYING_STATE_COMPLETE)
                    except Exception:
                        pass
                self._stop_drying_cycle("Drying cycle terminated because humidity goal %.1f%% reached" % self._drying_humidity_target, reset_state=False)
                return self.reactor.NEVER

        # Run periodic venting (macro)
        if self._vent_timer is not None:
            self._vent_timer -= ENV_CHECK_INTERVAL

            if self._vent_timer < 0 and self.mmu_unit.p.heater_vent_macro:
                cmd = self.mmu_unit.p.heater_vent_macro
                if self.has_per_gate_heaters():
                    cmd += " GATES=%s" % ",".join(map(str, self._get_active_gates()))
                self.mmu.log_debug("MmuEnvironmentManager: Running heater vent macro '%s'" % cmd)
                self.mmu.wrap_gcode_command(cmd, exception=False) # Will report errors without exception

                # Reset countdown regardless (prevents hammering if undefined or failing)
                self._vent_timer = self._drying_vent_interval * 60.0 if self._drying_vent_interval else None

        # Run periodic spool rotation (eSpooler)
        if self._rotate_timer is not None and self._rotate_enabled:
            self._rotate_timer -= ENV_CHECK_INTERVAL

            if self._rotate_timer < 0:
                # Re-check EMPTY status at time of rotation (supports dynamic state changes)
                if self.has_per_gate_heaters():
                    candidates = list(self._get_active_gates())
                else:
                    candidates = self.mmu_unit.gate_range()

                gates_to_rotate = []
                for gate in candidates:
                    try:
                        if self.mmu.gate_status[gate] == GATE_EMPTY:
                            gates_to_rotate.append(gate)
                    except Exception:
                        pass

                if gates_to_rotate:
                    self._rotate_spools_in_gates(gates_to_rotate)

                self._rotate_timer = self._drying_rotate_interval * 60.0 # To seconds

        # Reschedule
        return eventtime + ENV_CHECK_INTERVAL


    def _start_drying_cycle(self, per_gate_plan=None):
        if self.is_drying():
            return

        self.mmu.log_debug("MmuEnvironmentManager: Filament drying started")

        # Reset state at the beginning of a new cycle
        self._drying_state = [DRYING_STATE_NONE] * self.mmu_unit.num_gates

        # Vent timer countdown (seconds). 0/None disables venting.
        if self._drying_vent_interval:
            self._vent_timer = self._drying_vent_interval * 60.0 # To seconds
        else:
            self._vent_timer = None

        # Turn on heater or heaters depending on mode
        if not self.has_per_gate_heaters():
            # Single heater mode
            for gate in self.mmu_unit.gate_range():
                try:
                    self._state_set(gate, DRYING_STATE_ACTIVE)
                except Exception:
                    pass
            self._heater_on(self._drying_temp)

        else:
            # Multi heater mode: Initialize per-gate state and start as many as possible
            self._gate_drying = {}
            self._drying_queue = []
            self._drying_state = [DRYING_STATE_NONE] * self.mmu_unit.num_gates

            if per_gate_plan is None:
                per_gate_plan = self._get_drying_plan(self._drying_gates)

            # Queue all selected gates; we'll start up to max_concurrent_heaters
            for gate in self._drying_gates:
                plan = per_gate_plan.get(gate, {})
                gtemp = plan.get('temp', self.mmu_unit.p.heater_default_dry_temp)
                gtime = plan.get('timer', self.mmu_unit.p.heater_default_dry_time)
                material = plan.get('material', "unknown")

                self._gate_drying[gate] = {
                    'state': DRYING_STATE_QUEUED,
                    'start_time': None,
                    'end_time': None,
                    'material': material,
                    'temp': gtemp,
                    'timer': gtime,
                    'humidity_target': self._drying_humidity_target,
                    'done_reason': None,
                    'last_temp': None,
                    'last_humidity': None,
                }
                self._drying_queue.append(gate)
                try:
                    self._state_set(gate, DRYING_STATE_QUEUED)
                except Exception:
                    pass

            # Turn heater on if possible else queue
            self._start_next_queued_gates(self.reactor.monotonic())

        # Enable
        self.reactor.update_timer(self._periodic_timer, self.reactor.NOW)


    def _start_next_queued_gates(self, now):
        """
        Start queued gates up to max concurrent heaters.
        In this setup ensure the maximum drying time is applied per gate
        meaning the total drying time for all gates might be longer.
        """
        max_h = self.mmu_unit.max_concurrent_heaters
        if max_h <= 0:
            max_h = 1

        while len(self._get_active_gates()) < max_h and self._drying_queue:
            gate = self._drying_queue.pop(0)
            gd = self._gate_drying.get(gate)
            if not gd or gd.get('state') != DRYING_STATE_QUEUED:
                continue

            # Use per-gate time (from material) else user forced timer or default time
            per_gate_minutes = gd.get('timer', None)
            if per_gate_minutes is None:
                per_gate_minutes = int(self._drying_time)

            gd['start_time'] = now
            gd['end_time'] = now + (int(per_gate_minutes) * 60)
            gd['state'] = DRYING_STATE_ACTIVE
            gd['done_reason'] = None

            try:
                self._state_set(gate, DRYING_STATE_ACTIVE)
            except Exception:
                pass

            # Read environment sensor
            cur_temp, cur_humidity = self._get_environment_status(gate=gate)
            gd['last_temp'] = cur_temp
            gd['last_humidity'] = cur_humidity

            self._heater_on(gd.get('temp'), gate=gate)


    def _stop_drying_cycle(self, msg="Filament drying stopped", reset_state=True):
        if self.is_drying() or self._drying_end_time is not None:
            self.mmu.log_info(msg)
            self.reactor.update_timer(self._periodic_timer, self.reactor.NEVER)

            # Turn off all heaters in either mode
            if self.has_per_gate_heaters():
                for gate in list(self._get_active_gates()):
                    self._heater_off(gate=gate)
                # Best effort: also turn off any configured heaters for selected gates
                for gate in self._drying_gates:
                    self._heater_off(gate=gate)
            else:
                self._heater_off()

            self._drying_queue = []

            if reset_state:
                self._drying_gates = []
                self._gate_drying = {}
                self._drying_state = [DRYING_STATE_NONE] * self.mmu_unit.num_gates

            # Stop rotation
            self._rotate_timer = None
            self._rotate_enabled = False


    def _cancel_gates(self, gates, reason="cancelled"):
        """
        Cancel drying for the given gates in multi-heater mode.
        - If gate is active: turn off its heater, mark done, remove from active list.
        - If gate is queued: remove from pending queue, mark done.
        - If gate is unknown: ignore.
        Returns number of gates actually cancelled.
        """
        cancelled = 0
        now = self.reactor.monotonic()

        for gate in list(gates):
            gd = self._gate_drying.get(gate)

            # If we don't have state for it, it might not be part of this cycle
            if gd is None:
                continue

            state = gd.get('state')

            if state == DRYING_STATE_ACTIVE:
                # Turn off heater and mark done
                self._heater_off(gate=gate)
                gd['state'] = DRYING_STATE_CANCELLED
                gd['done_reason'] = reason
                gd['end_time'] = now
                self._state_set(gate, DRYING_STATE_CANCELLED)
                cancelled += 1

            elif state == DRYING_STATE_QUEUED:
                # Remove from pending queue and mark done
                try:
                    while gate in self._drying_queue:
                        self._drying_queue.remove(gate)
                except Exception:
                    pass
                gd['state'] = DRYING_STATE_CANCELLED
                gd['done_reason'] = reason
                gd['end_time'] = now
                self._state_set(gate, DRYING_STATE_CANCELLED)
                cancelled += 1

            elif state == DRYING_STATE_COMPLETE:
                # Already done; no-op
                pass

        # After cancellations, if we freed heater slots start next queued gates
        if self.has_per_gate_heaters():
            self._start_next_queued_gates(now)

        return cancelled


    def _heater_on(self, temp, gate=None):
        """
        Turn MMU heater on.
        """
        if not self.has_per_gate_heaters():
            self.mmu.log_debug("MmuEnvironmentManager: Heater %s set to target temp of %.1f°C" % (self.mmu_unit.filament_heater, temp))
            hname = self._heater_name(self.mmu_unit.filament_heater)
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (hname, temp))
            return

        heaters = self.mmu_unit.filament_heaters
        lgate = self.mmu_unit.local_gate(gate)
        if lgate < 0 or lgate >= len(heaters) or not heaters[lgate]:
            self.mmu.log_warning("MmuEnvironmentManager: No heater configured for gate %d" % gate)
            return

        hname = self._heater_name(heaters[lgate])
        self.mmu.log_debug("MmuEnvironmentManager: Gate %d heater %s set to target temp of %.1f°C" % (gate, hname, temp))
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (hname, temp))


    def _heater_off(self, gate=None):
        """
        Turn MMU heater off. If gate=None then turn off all heaters
        """
        if not self.has_per_gate_heaters() and self.mmu_unit.filament_heater:
            self.mmu.log_debug("MmuEnvironmentManager: Heater %s turned off" % self.mmu_unit.filament_heater)
            hname = self._heater_name(self.mmu_unit.filament_heater)
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % hname)
            return

        if gate is None:
            # Turn off all known heaters (best effort)
            self.mmu.log_debug("MmuEnvironmentManager: All gate heaters turned off")
            heaters = self.mmu_unit.filament_heaters
            for i in range(len(heaters)):
                if heaters[i]:
                    hname = self._heater_name(heaters[i])
                    self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % hname)
            return

        heaters = self.mmu_unit.filament_heaters
        lgate = self.mmu_unit.local_gate(gate)
        if lgate < 0 or lgate >= len(heaters) or not heaters[lgate]:
            return
        _,target = self._get_heater_status(gate)
        if target:
            hname = self._heater_name(heaters[lgate])
            self.mmu.log_debug("MmuEnvironmentManager: Gate %d heater %s turned off" % (gate, hname))
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % hname)


    def _heater_name(self, heater_obj_name):
        """
        Return just the simple heater name from the heater object name
        """
        return heater_obj_name.split(None, 1)[1].strip()


    def _get_heater_status(self, gate=None):
        """
        Return tuple of temperature and target temperature from heater
        either the single heater or the per-gate heater
        Returns (None, None) if heater is not configured / not found.
        """
        if gate is None:
            heater_name = self.mmu_unit.filament_heater
        else:
            heaters = self.mmu_unit.filament_heaters
            lgate = self.mmu_unit.local_gate(gate)
            if lgate < 0 or lgate >= len(heaters) or not heaters[lgate]:
                return (None, None)
            heater_name = heaters[lgate]

        obj = self.printer.lookup_object(heater_name, None)
        if obj is None:
            return (None, None)

        status = obj.get_status(0)
        return (status.get('temperature'), status.get('target'))


    def _get_environment_status(self, gate=None):
        """
        Return tuple of temperature and humidity from environment sensor.
        Note that some configured sensors may only offer temperature
        """
        if gate is None:
            sensor = self.mmu_unit.environment_sensor
        else:
            sensors = self.mmu_unit.environment_sensors
            lgate = self.mmu_unit.local_gate(gate)
            if lgate < 0 or lgate >= len(sensors) or not sensors[lgate]:
                return None, None
            sensor = sensors[lgate]

        obj = self.printer.lookup_object(sensor, None)
        if obj is None:
            return None, None

        status = obj.get_status(0)
        temperature = status.get('temperature')

        # See if chip supports humidity (we hope so)
        humidity = None
        p = sensor.split()
        s_name = p[1] if len(p) > 1 else None
        if s_name:
            for chip in ENV_SENSOR_CHIPS:
                obj = self.printer.lookup_object("%s %s" % (chip, s_name), None)
                if obj:
                    humidity = obj.get_status(0).get('humidity')
                    break

        return (temperature, humidity)


    def _rotate_spools_in_gates(self, gates):
        """
        eSpooler-driven spool rotation.
        Move the spools in the retract direction a small distance, 90 degrees is perfect
        """
        self.mmu.log_info("Rotating spools in gates: %s..." % ",".join(map(str, gates)))
        if self.mmu_unit.mmu_vendor != VENDOR_VVD:
            self.spools_to_rotate = list(gates)
            # Initiate rotation of first spool -- they are moved in sequence for asetics and to avoid possiblity of overload
            self._rotate_spool(self.spools_to_rotate[0])
            return

        # Special case VVD design because of unique spool rotation using shared gear stepper coupled to gate selection
        if not self.mmu.is_in_print():
            prev_gate_selected = self.mmu.gate_selected
            for gate in gates:
                self.mmu.select_gate(gate)
                _,_,_,_ = self.mmu.move_filament("Rotating spool for drying", -100, motor="gear", wait=True)
            self.mmu.select_gate(prev_gate_selected)


    def _rotate_spool(self, gate):
        """
        Send event to  cause a small rewind action to rotate the spool in gate
        """
        power = self.mmu_unit.p.espooler_rewind_burst_power
        duration = self.mmu_unit.p.espooler_rewind_burst_duration
        self.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, ESPOOLER_REWIND)


    def _handle_espooler_burst_done(self, gate):
        """
        Event indicating that a spool rotation completed
        """
        if gate in self.spools_to_rotate:
            self.spools_to_rotate.remove(gate)
            if self.spools_to_rotate:
                self._rotate_spool(self.spools_to_rotate[0])


    def _get_drying_plan(self, gates):
        """
        For the given gates, look up each gate's material to find drying data (temp/time).
        Returns dict indexed by gate:
          plan[gate] = { 'temp': recommended_temp, 'timer': recommended_time, ... }

        If a material is not found in self.mmu.p.drying_data, use defaults.
        """
        max_temp = self.mmu_unit.p.heater_max_temp
        default_temp = self.mmu_unit.p.heater_default_dry_temp
        default_time = self.mmu_unit.p.heater_default_dry_time

        plan = {}
        for gate in gates:
            material = self.mmu.gate_material[gate]
            key = str(material).upper()
            temp, duration = self.mmu.p.drying_data.get(key, (default_temp, default_time))
            plan[gate] = {
                'material': material,
                'temp': float(min(max_temp, temp)),
                'timer': int(duration),
            }
        return plan
