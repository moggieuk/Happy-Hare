# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to implement MMU heater control and basic filament drying functionality
#
# Implements commands:
#   MMU_HEATER
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import ast, logging

# Happy Hare imports

# MMU subcomponent clases
from .mmu_shared           import *


class MmuEnvironmentManager:

    CHECK_INTERVAL = 30 # How often to check heater and environment sensors (seconds)

    # Environment sensor chips with humidity
    ENV_SENSOR_CHIPS = ["bme280", "htu21d", "sht3x", "lm75"]

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu.managers.append(self)

        # Process config
        self.heater_default_dry_temp = self.mmu.config.getfloat('heater_default_dry_temp', 45, above=0.)
        self.heater_default_dry_time = self.mmu.config.getfloat('heater_default_dry_time', 300, above=0.)
        self.heater_default_humidity = self.mmu.config.getfloat('heater_default_humidity', 10, above=0.)
        self.heater_vent_macro       = self.mmu.config.get(     'heater_vent_macro', '')
        self.heater_vent_interval    = self.mmu.config.getfloat('heater_vent_interval', 0, minval=0)

        # Build tuples of drying temp / drying time indexed by filament type
        drying_data_str = self.mmu.config.get('drying_data', {})
        try:
            drying_data = ast.literal_eval(drying_data_str)
            # Store as upper case keys (If there are duplicate keys differing only by case, the last one wins)
            self.drying_data = dict((str(k).upper(), v) for k, v in drying_data.items())
        except Exception as e:
            raise self.mmu.config.error("Unparsable 'drying_data' parameter: %s" % str(e))

        # Listen of important mmu events
        self.mmu.printer.register_event_handler("mmu:disabled", self._handle_mmu_disabled)

        # Register GCODE commands ---------------------------------------------------------------------------
        self.mmu.gcode.register_command('MMU_HEATER', self.cmd_MMU_HEATER, desc=self.cmd_MMU_HEATER_help)

        self._periodic_timer = self.mmu.reactor.register_timer(self._check_mmu_environment)
        self.reinit()


    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self._drying = False
        self._drying_temp = None
        self._drying_humidity_target = None
        self._drying_start_time = self._drying_end_time = None
        self._drying_gates = []
        self._drying_vent_interval = None

        # Per-gate drying state (multi-heater mode)
        # gate -> dict(state, start_time, end_time, temp, humidity_target, done_reason, last_temp, last_humidity)
        self._gate_drying = {}
        self._active_gates = []  # Currently heated gates (that have filament present)
        self._pending_gates = [] # Queued gates awaiting heater capacity
        self._vent_timer = None


    # No ready/connect/disconnect lifecycle hooks


    def set_test_config(self, gcmd):
        if self.has_heater():
            self.heater_default_dry_temp = gcmd.get_float('HEATER_DEFAULT_DRY_TEMP', self.heater_default_dry_temp, above=0.)
            self.heater_default_dry_time = gcmd.get_float('HEATER_DEFAULT_DRY_TIME', self.heater_default_dry_time, above=0.)
            self.heater_default_humidity = gcmd.get_float('HEATER_DEFAULT_HUMIDITY', self.heater_default_humidity, above=0.)
            self.heater_vent_macro       = gcmd.get(      'HEATER_VENT_MACRO', self.heater_vent_macro)
            self.heater_vent_interval    = gcmd.get_float('HEATER_VENT_INTERVAL', self.heater_vent_interval, minval=0)


    def get_test_config(self):
        if self.has_heater():
            msg = "\n\nHEATER:"
            msg += "\nheater_default_dry_temp = %.1f" % self.heater_default_dry_temp
            msg += "\nheater_default_dry_time = %.1f" % self.heater_default_dry_time
            msg += "\nheater_default_humidity = %.1f" % self.heater_default_humidity
            msg += "\nheater_vent_macro = %s" % self.heater_vent_macro
            msg += "\nheater_vent_interval = %.1f" % self.heater_vent_interval

        return msg


    def check_test_config(self, param):
        return vars(self).get(param) is None

    #
    # Mmu Heater manager public access...
    #

    def is_drying(self):
        """
        Returns whether the MMU heater is currently in drying cycle
        """
        return self._drying


    def _has_per_gate_heaters(self):
        """
        Returns whether this MMU configuration has a separate heater for each gate
        (and corresponding environment sensor per gate)
        """
        heaters = self.mmu.mmu_machine.filament_heaters
        sensors = self.mmu.mmu_machine.environment_sensors
        if not heaters or not sensors:
            return False
        if not isinstance(heaters, (list, tuple)) or not isinstance(sensors, (list, tuple)):
            return False
        # Consider it active only if there is at least one valid entry
        return True


    def has_heater(self):
        if self._has_per_gate_heaters():
            heaters = self.mmu.mmu_machine.filament_heaters
            return bool(heaters) # At least one heater configured
        return self.mmu.mmu_machine.filament_heater != ''


    def has_env_sensor(self):
        if self._has_per_gate_heaters():
            sensors = self.mmu.mmu_machine.environment_sensors
            return bool(sensors)
        return self.mmu.mmu_machine.environment_sensor != ''


    #
    # GCODE Commands -----------------------------------------------------------
    #

    cmd_MMU_HEATER_help = "Enable/disable MMU heater (filament dryer)"
    cmd_MMU_HEATER_param_help = (
        "MMU_HEATER: %s\n" % cmd_MMU_HEATER_help
        + "OFF = [0|1] Turn off heater and drying cycle\n"
        + "DRY = [0|1] Disable/enable filament heater for filament drying cycle\n"
        + "TIMER = #(mins) Force drying time\n"
        + "TEMP = #(degrees) Force temperature\n"
        + "HUMIDITY = % Terminate drying when humidty goal is reached\n"
        + "GATES = x,y Gates to dry ONLY IF MMU has individual spool heaters/dryers\n"
        + "DRYING_DATA = [0|1] Dump configured drying data for filament types\n"
        + "VENT_INTERVAL = #(mins) How often to call 'vent' macro\n"
        + "(no parameters for status report)"
    )
    def cmd_MMU_HEATER(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_HEATER_param_help), color=True)
            return

        drying_data = gcmd.get_int('DRYING_DATA', 0, minval=0, maxval=1)
        off = gcmd.get_int('OFF', None, minval=0, maxval=1)
        dry = gcmd.get_int('DRY', None, minval=0, maxval=1)
        timer = gcmd.get_int('TIMER', None, minval=0)
        temp = gcmd.get_float('TEMP', None, minval=0., maxval=100.)
        humidity = gcmd.get_float('HUMIDITY', self.heater_default_humidity, minval=0)
        vent_interval = gcmd.get_float('VENT_INTERVAL', self.heater_vent_interval, minval=0)
        gates = gcmd.get('GATES', "!")
        if gates != "!":
            gatelist = []
            # Supplied list of gates
            try:
                for gate in gates.split(','):
                    gate = int(gate)
                    if 0 <= gate < self.mmu.num_gates:
                        gatelist.append(gate)
                gates = gatelist
            except ValueError:
                raise gcmd.error("Invalid GATES parameter: %s" % gates)
        else:
            # Default to non empty gates
            gates = [
                i for i, status in enumerate(self.mmu.gate_status)
                if status != self.mmu.GATE_EMPTY
            ]

        def _format_minutes(minutes):
            hours, mins = divmod(int(minutes), 60)
            parts = []
            if hours:
                parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
            if mins:
                parts.append("%d minute%s" % (mins, "" if mins == 1 else "s"))
            if not (hours or mins):
                parts.append("0 minutes")
            return " ".join(parts)

        if drying_data:
            # Sort keys for stable, readable output
            msg = "Drying data:\n"
            for material in sorted(self.drying_data.keys()):
                t, minutes = self.drying_data[material]
                # Avoid format() on unicode with alignment in Py2 edge-cases; keep it simple
                msg += u"%s %s°C for %s\n" % (material + ":", int(t), _format_minutes(minutes))
            self.mmu.log_always(msg)

        # Cancel drying cycle / Heater off
        if off or temp == 0:
            if self._drying:
                self._stop_drying_cycle()
            else:
                self._heater_off()
            return

        # Raw heater control
        if not dry and temp is not None:
            # In per-gate mode, raw TEMP control is ambiguous; apply to the single heater only
            if self._has_per_gate_heaters():
                self.mmu.log_always("Raw TEMP control not supported in per-gate heater mode. Use DRY=1 with GATES=")
                return
            self._heater_on(temp)
            if self._drying:
                self._drying_temp = temp
            return

        if dry:
            if not self.has_env_sensor():
                self.mmu.log_warning("MMU environment sensor not found. Check `environment_sensor` configuration")
                return

            if self._drying:
                self.mmu.log_always("MMU already in filament drying cycle. Stop current cycle first")
                return

            # Per-gate recommended temps/times, plus overall notes
            per_gate_plan = self._get_drying_plan(gates)

            # If TEMP specified, override all selected gates to that temp (still warn if above recommendation)
            if temp is not None:
                for gate in gates:
                    if temp > per_gate_plan[gate]['temp']:
                        if per_gate_plan[gate]['material']:
                            self.mmu.log_warning(u"Warning: Gate %d drying temperature %.1f°C is greater than that recommended for %s (%.1f°C)"
                                                 % (gate, temp, per_gate_plan[gate]['material'], per_gate_plan[gate]['temp']))
                        else:
                            self.mmu.log_warning(u"Warning: Gate %d has unknown filament type. Cannot validate temperature %.1f°C" % (gate, temp))
                    per_gate_plan[gate]['temp'] = temp
            else:
                # Default to each gate's recommended temperature
                # Also log a summary: lowest of recommended temps and longest time # PAUL not sure about this?
                lowest = None
                longest = None
                for gate in gates:
                    t = per_gate_plan[gate]['temp']
                    d = per_gate_plan[gate]['timer']
                    if lowest is None or t < lowest: lowest = t
                    if longest is None or d > longest: longest = d
                if lowest is not None and longest is not None:
                    self.mmu.log_info(u"Defaulting to per-gate drying temperatures (lowest %.1f°C) for %s given filaments types in MMU"
                                      % (lowest, _format_minutes(longest)))

            # Initiate dryer, record state at start of cycle
            self._drying_time = timer or self.heater_default_dry_time # PAUL what about handling longest dry time
            self._drying_temp = temp or self.heater_default_dry_temp # PAUL what about handling lowest dry temp
            self._drying_humidity_target = humidity
            self._drying_start_time = self.mmu.reactor.monotonic()
            self._drying_end_time = self._drying_start_time + self._drying_time * 60 # NOTE: In multi-heater mode, each gate's end_time is tracked independently
            self._drying_gates = gates
            self._drying_vent_interval = vent_interval

            self.mmu.log_warning("PAUL: plan2=%s" % per_gate_plan)
            self.mmu.log_warning("PAUL: self._drying_time=%s" % self._drying_time)
            self.mmu.log_warning("PAUL: self._drying_temp=%s" % self._drying_temp)

            self._start_drying_cycle(per_gate_plan)
            self.mmu.log_warning("PAUL: self._gate_drying=%s" % self._gate_drying)
            self.mmu.log_warning("PAUL: self._active_gates=%s" % self._active_gates)

            msg = "MMU filament drying cycle started:"

        elif self._drying:
            msg = "MMU is in filament drying cycle:"
        else:
            cur_temp, cur_target = self._get_heater_status()
            if cur_target != 0:
                msg = u"Not in drying cycle but heater is on. Target: %.1f°C, Actual: %.1f°C" % (cur_target, cur_temp)
            else:
                msg = "Not in drying cycle and heater is off"

        if self._drying:
            # Display environment sensor data unless if is unavailable and then fallback to heater status
            now = self.mmu.reactor.monotonic()

            if self._drying_gates:
                msg += "\nDrying filaments in gates: %s" % ", ".join(str(g) for g in self._drying_gates)

            if not self._has_per_gate_heaters():
                remaining_mins = _format_minutes((self._drying_end_time - now) // 60)
                cur_temp, cur_humidity = self._get_environment_status()
                msg += "\nCycle time: %s (remaining: %s)" % (_format_minutes(self._drying_time), remaining_mins)
                if cur_temp is not None:
                    msg += "\nTarget humidity: %.1f%%" % self._drying_humidity_target
                    if cur_humidity is not None:
                        msg += " (current: %.1f%%)" % cur_humidity
                else:
                    cur_temp, cur_target = self._get_heater_status()
                    msg += "\nEnvironment sensor not available / misconfigured"
                msg += u"\nDrying temp: %.1f°C (current: %.1f°C)" % (self._drying_temp, cur_temp)

            else:
                # Per-gate status report
                msg += "\nPer-gate dryer mode (max concurrent heaters: %d). Humidty target %.1f%%" % (self.mmu.mmu_machine.max_concurrent_heaters, self._drying_humidity_target)
                for gate in self._drying_gates:
                    gd = self._gate_drying.get(gate, {})
                    state = gd.get('state', 'unknown')
                    material = gd.get('material', None)
                    t = gd.get('temp', None)
                    last_t = gd.get('last_temp', None)
                    last_h = gd.get('last_humidity', None)
                    end_t = gd.get('end_time', None)
                    if end_t is not None and state == 'active':
                        rem = max(0, int((end_t - now) // 60))
                        rem_txt = _format_minutes(rem)
                    else:
                        rem_txt = None

                    line = "\nGate %d: " % gate
                    if state == 'active':
                        if last_t is not None:
                            line += "Drying %s %.1f°C (target %.1f°C)" % (material, last_t, t)
                        if last_h is not None:
                            line += ", humidity %.1f%%" % last_h
                        if rem_txt is not None:
                            line += ", %s remaining" % rem_txt
                    elif state == 'queued':
                        line += "(queued waiting for heater slot)"
                    elif state == 'done':
                        reason = gd.get('done_reason', 'complete')
                        line += "done (%s" % reason
                        if last_h is not None:
                            line += ", final humidity: %.1f%%" % last_h
                        line += ")"
                    msg += line

            # Venting status
            if self._vent_timer is not None:
                msg += "\nVenting operational (runing macro %s every %s, next in %s)" % (
                    self.heater_vent_macro,
                    _format_minutes(self._drying_vent_interval),
                    _format_minutes(max(self.CHECK_INTERVAL, self._vent_timer) / 60),
                )
            else:
                if not self.heater_vent_macro:
                    vent_reason = "heater_vent_macro not set"
                else:
                    vent_reason = "heater_vent_interval is 0"
                msg += "\nVenting not operational (%s)" % vent_reason

        # Report status
        self.mmu.log_always(msg)


    def get_status(self, eventtime=None):
        """
        Structured status for UI consumption
        """
        st = {
            'drying_filament': self._drying,
        }
# PAUL ... TEMP this is too much data!
        if self._drying and self._has_per_gate_heaters():
            gates = {}
            for gate, gd in self._gate_drying.items():
                gates[str(gate)] = {
                    'state': gd.get('state'),
                    'material': gd.get('material'),
                    'temp': gd.get('temp'),
                    'humidity': gd.get('last_humidity'),
                    'temperature': gd.get('last_temp'),
                    'end_time': gd.get('end_time'),
                    'done_reason': gd.get('done_reason'),
                }
            st['gate_drying'] = gates
            st['max_heaters'] = self.mmu.mmu_machine.max_concurrent_heaters
            st['active_gates'] = list(self._active_gates)
            st['pending_gates'] = list(self._pending_gates)
        return st


    #
    # Internal implementation --------------------------------------------------
    #

    def _handle_mmu_disabled(self, eventtime=None):
        """
        Event indicating that the MMU unit was disabled
        """
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()
        self._stop_drying_cycle()
        self._heater_off()


    def _check_mmu_environment(self, eventtime):
        """
        Reactor callback to periodically check drying status and to rationalize state
        """
        if not self._drying:
            return self.mmu.reactor.NEVER

        now = self.mmu.reactor.monotonic()

        # Per-gate drying mode
        if self._has_per_gate_heaters():
            # Update active gates: check completion / humidity threshold
            completed_any = False
            for gate in list(self._active_gates):
                gd = self._gate_drying.get(gate)
                if not gd or gd.get('state') != 'active':
                    continue

                # Read environment sensor
                cur_temp, cur_humidity = self._get_environment_status(gate=gate)
                gd['last_temp'] = cur_temp
                gd['last_humidity'] = cur_humidity

                # Cycle complete (per gate)
                if gd.get('end_time') is not None and (gd['end_time'] - now) <= 0:
                    self._heater_off(gate=gate)
                    gd['state'] = 'done'
                    gd['done_reason'] = 'timer complete'
                    completed_any = True
                    try:
                        self._active_gates.remove(gate)
                    except Exception:
                        pass
                    continue

                # Humidity goal reached (per gate)
                if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                    self._heater_off(gate=gate)
                    gd['state'] = 'done'
                    gd['done_reason'] = 'humidity goal reached'
                    completed_any = True
                    try:
                        self._active_gates.remove(gate)
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
                if not gd or gd.get('state') != 'done':
                    all_done = False
                    break
            if all_done:
                self._stop_drying_cycle("Drying cycle complete (all gates)")
                return self.mmu.reactor.NEVER

        else:
            # Single heater mode

            # Cycle complete?
            if (self._drying_end_time - now) <= 0:
                cur_temp, cur_humidity = self._get_environment_status()
                self._stop_drying_cycle("Drying cycle complete\nFinal humidity: %.1f%%)" % cur_humidity)
                return self.mmu.reactor.NEVER

            # Humidity goal reached?
            cur_temp, cur_humidity = self._get_environment_status()
            if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                self._stop_drying_cycle("Drying cycle terminated because humidity goal %.1f%% reached" % self._drying_humidity_target)
                return self.mmu.reactor.NEVER

        # Run periodic venting (macro)
        if self._vent_timer is not None:
            self._vent_timer -= self.CHECK_INTERVAL

            if self._vent_timer < 0 and self.heater_vent_macro:
                cmd = "%s GATES=%s" % (self.heater_vent_macro, ",".join(map(str, self._active_gates)))
                self.mmu.log_info("MmuEnvironmentManager: Running heater vent macro '%s'" % cmd)
                self.mmu.wrap_gcode_command(cmd, exception=False) # Will report errors without exception

                # Reset countdown regardless (prevents hammering if undefined or failing)
                self._vent_timer = self._drying_vent_interval * 60.0 if self._drying_vent_interval else None

        # Reschedule
        return eventtime + self.CHECK_INTERVAL


    def _start_drying_cycle(self, per_gate_plan=None):
        if not self._drying:
            self.mmu.log_info("MmuEnvironmentManager: Filament drying started")
            self._drying = True

            # Vent timer countdown (seconds). 0/None disables venting.
            if self._drying_vent_interval:
                self._vent_timer = self._drying_vent_interval * 60.0 # To seconds
            else:
                self._vent_timer = None

            # Turn on heater or heaters depending on mode
            if not self._has_per_gate_heaters():
                # Single heater mode
                self._heater_on(self._drying_temp)

            else:
                # Multi heater mode: Initialize per-gate state and start as many as possible
                self._gate_drying = {}
                self._active_gates = []
                self._pending_gates = []

                if per_gate_plan is None:
                    per_gate_plan = self._get_drying_plan(self._drying_gates)

                # Queue all selected gates; we'll start up to max_concurrent_heaters
                for gate in self._drying_gates:
                    plan = per_gate_plan.get(gate, {})
                    gtemp = plan.get('temp', self.heater_default_dry_temp)
                    gtime = plan.get('timer', self.heater_default_dry_time)
                    material = plan.get('material', "unknown")

                    self._gate_drying[gate] = {
                        'state': 'queued',
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
                    self._pending_gates.append(gate)

                # Turn heater on if possible else queue
                self._start_next_queued_gates(self.mmu.reactor.monotonic())

            # Enable
            self.mmu.reactor.update_timer(self._periodic_timer, self.mmu.reactor.NOW)


    def _start_next_queued_gates(self, now):
        """
        Start queued gates up to max concurrent heaters.
        In this setup ensure the maximum drying time is applied per gate
        meaning the total drying time for all gates might be longer.
        """
        max_h = self.mmu.mmu_machine.max_concurrent_heaters
        if max_h <= 0:
            max_h = 1

        while len(self._active_gates) < max_h and self._pending_gates:
            gate = self._pending_gates.pop(0)
            gd = self._gate_drying.get(gate)
            if not gd or gd.get('state') != 'queued':
                continue

            # Use per-gate time (from material) else user forced timer or default time
            per_gate_minutes = gd.get('timer', None)
            if per_gate_minutes is None:
                per_gate_minutes = int(self._drying_time)

            gd['start_time'] = now
            gd['end_time'] = now + (int(per_gate_minutes) * 60)
            gd['state'] = 'active'
            gd['done_reason'] = None

            # Read environment sensor
            cur_temp, cur_humidity = self._get_environment_status(gate=gate)
            gd['last_temp'] = cur_temp
            gd['last_humidity'] = cur_humidity

            self._active_gates.append(gate)
            self._heater_on(gd.get('temp'), gate=gate)


    def _stop_drying_cycle(self, msg="Filament drying stopped"):
        if self._drying:
            self.mmu.log_info("MmuEnvironmentManager: %s" % msg)
            self.mmu.reactor.update_timer(self._periodic_timer, self.mmu.reactor.NEVER)

            # Turn off all heaters in either mode
            if self._has_per_gate_heaters():
                for gate in list(self._active_gates):
                    self._heater_off(gate=gate)
                # Best effort: also turn off any configured heaters for selected gates
                for gate in self._drying_gates:
                    self._heater_off(gate=gate)
            else:
                self._heater_off()

            self._drying = False
            self._active_gates = []
            self._pending_gates = []


    def _heater_on(self, temp, gate=None):
        if gate is None:
            self.mmu.log_info(u"MmuEnvironmentManager: Heater %s set to target temp of %.1f°C" % (self.mmu.mmu_machine.filament_heater, temp))
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (self.mmu.mmu_machine.filament_heater, temp))
            return

        heaters = self.mmu.mmu_machine.filament_heaters
        if gate < 0 or gate >= len(heaters) or not heaters[gate]:
            self.mmu.log_warning("MmuEnvironmentManager: No heater configured for gate %d" % gate)
            return

        heater_name = heaters[gate]
        self.mmu.log_info(u"MmuEnvironmentManager: Gate %d heater %s set to target temp of %.1f°C" % (gate, heater_name, temp))
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (heater_name, temp))


    def _heater_off(self, gate=None):
        if gate is None and not self._has_per_gate_heaters():
            self.mmu.log_info(u"MmuEnvironmentManager: Heater %s turned off" % self.mmu.mmu_machine.filament_heater)
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % self.mmu.mmu_machine.filament_heater)
            return

        if gate is None and self._has_per_gate_heaters():
            # Turn off all known heaters (best effort)
            heaters = self.mmu.mmu_machine.filament_heaters
            for i in range(len(heaters)):
                if heaters[i]:
                    self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % heaters[i])
            return

        heaters = self.mmu.mmu_machine.filament_heaters
        if gate < 0 or gate >= len(heaters) or not heaters[gate]:
            return
        heater_name = heaters[gate]
        self.mmu.log_info(u"MmuEnvironmentManager: Gate %d heater %s turned off" % (gate, heater_name))
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % heater_name)


    def _get_heater_status(self):
        """
        Single-heater status only
        """
        status = self.mmu.printer.lookup_object(self.mmu.mmu_machine.filament_heater).get_status(0)
        temperature = status.get('temperature')
        target = status.get('target')
        power = status.get('power')
        return (temperature, target)


    def _get_environment_status(self, gate=None):
        """
        Return tuple of temperature and humidity from environment sensor.
        Note that some configured sensors may only offer temperature
        """
        if gate is None:
            sensor = self.mmu.mmu_machine.environment_sensor
        else:
            sensors = self.mmu.mmu_machine.environment_sensors
            if gate < 0 or gate >= len(sensors) or not sensors[gate]:
                return None, None
            sensor = sensors[gate]

        obj = self.mmu.printer.lookup_object(sensor, None)
        if obj is None:
            return None, None

        status = obj.get_status(0)
        temperature = status.get('temperature')

        # See if chip supports humidity (we hope so)
        humidity = None
        p = sensor.split()
        s_name = p[1] if len(p) > 1 else None
        if s_name:
            for chip in self.ENV_SENSOR_CHIPS:
                obj = self.mmu.printer.lookup_object("%s %s" % (chip, s_name), None)
                if obj:
                    humidity = obj.get_status(0).get('humidity')
                    break

        return (temperature, humidity)


    def _get_max_drying_temp_time(self, gates):
        """
        For the given gates, look up each gate's material to find drying data (temp/time)
        Return (lowest_temp, longest_time) across the set.

        If a material is not found in self.drying_data, use:
          - self.heater_default_dry_temp
          - self.heater_default_dry_time
        """
        default_temp = self.heater_default_dry_temp
        default_time = self.heater_default_dry_time

        lowest_temp = None
        longest_time = None

        for gate in gates:
            material = self.mmu.gate_material[gate]
            key = str(material).upper()

            temp, duration = self.drying_data.get(key, (default_temp, default_time))

            # Track lowest temperature
            if lowest_temp is None or temp < lowest_temp:
                lowest_temp = temp

            # Track longest time
            if longest_time is None or duration > longest_time:
                longest_time = duration

        # If no matching materials return defaults
        if lowest_temp is None:
            lowest_temp = default_temp
        if longest_time is None:
            longest_time = default_time

        return (lowest_temp, longest_time)


    def _get_drying_plan(self, gates):
        """
        For the given gates, look up each gate's material to find drying data (temp/time).
        Returns dict indexed by gate:
          plan[gate] = { 'temp': recommended_temp, 'timer': recommended_time, ... }

        If a material is not found in self.drying_data, use defaults.
        """
        default_temp = self.heater_default_dry_temp
        default_time = self.heater_default_dry_time

        plan = {}
        for gate in gates:
            material = self.mmu.gate_material[gate]
            key = material.upper()
            temp, duration = self.drying_data.get(key, (default_temp, default_time))
            plan[gate] = {
                'material': material,
                'temp': float(temp),
                'timer': float(duration),
            }
        return plan

