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
#     is possible to supplied which gates to dry. The list of heaters and environment sensors
#     should be set with the 'filament_heaters' and 'environment_sensors' properties.
#     Further, in this mode a basic "power management" is implemented which limits the number
#     of simulateous heaters to that defined by the 'max_concurrent_heaters' property.
#     Individual control of per-gate heaters and lifecycle is possible by specifying gates of
#     interest. The periodic venting macro will be called with a GATE parameter listing the
#     currently heated gates.
# The manager will support automatic spool rotation if equiped with eSpooler and the dry cycle
# is initiated with this option. IMPORTANT: filament must be removed from the MMU inlet and
# fastened to the spool. Also, the GATES parameter must be supplied.
#
# TODO For HHv4 this needs to operate per unit (gate range)
#
# Implements commands:
#   MMU_HEATER
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
import ast, logging

# Happy Hare imports
from ..mmu_machine         import VENDOR_VVD

# MMU subcomponent clases
from .mmu_shared           import *


class MmuEnvironmentManager:

    CHECK_INTERVAL = 30 # How often to check heater and environment sensors (seconds)

    # Environment sensor chips with humidity
    ENV_SENSOR_CHIPS = ["bme280", "htu21d", "sht3x", "lm75"]

    # Drying states (mostly relevant for per-gate heaters)
    DRYING_STATE_NONE      = ''
    DRYING_STATE_QUEUED    = 'queued'
    DRYING_STATE_ACTIVE    = 'active'
    DRYING_STATE_COMPLETE  = 'complete'
    DRYING_STATE_CANCELLED = 'canceled'

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu.managers.append(self)

        # Process config
        self.heater_max_temp         = self.mmu.config.getfloat('heater_max_temp', 65, above=0.) # Never to exceed temp to avoid melting enclosure
        self.heater_default_dry_temp = self.mmu.config.getfloat('heater_default_dry_temp', 45, above=0.)
        self.heater_default_dry_time = self.mmu.config.getfloat('heater_default_dry_time', 300, above=0.)
        self.heater_default_humidity = self.mmu.config.getfloat('heater_default_humidity', 10, above=0.)
        self.heater_vent_macro       = self.mmu.config.get(     'heater_vent_macro', '')
        self.heater_vent_interval    = self.mmu.config.getfloat('heater_vent_interval', 0, minval=0)
        self.heater_rotate_interval  = self.mmu.config.getfloat('heater_rotate_interval', 5, minval=1)

        # Build tuples of drying temp / drying time indexed by filament type
        drying_data_str = self.mmu.config.get('drying_data', {})
        try:
            drying_data = ast.literal_eval(drying_data_str)
            # Store as upper case keys (If there are duplicate keys differing only by case, the last one wins)
            self.drying_data = dict((str(k).upper(), v) for k, v in drying_data.items())
        except Exception as e:
            raise self.mmu.config.error("Unparsable 'drying_data' parameter: %s" % str(e))

        # Listen of important mmu events
        self.mmu.printer.register_event_handler("mmu:enabled", self._handle_mmu_enabled)
        self.mmu.printer.register_event_handler("mmu:disabled", self._handle_mmu_disabled)
        self.mmu.printer.register_event_handler("mmu:espooler_burst_done", self._handle_espooler_burst_done)

        # Register GCODE commands ---------------------------------------------------------------------------
        self.mmu.gcode.register_command('MMU_HEATER', self.cmd_MMU_HEATER, desc=self.cmd_MMU_HEATER_help)

        self._periodic_timer = self.mmu.reactor.register_timer(self._check_mmu_environment)
        self.reinit()


    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self._drying_temp = None
        self._drying_humidity_target = None
        self._drying_start_time = self._drying_end_time = None
        self._drying_gates = []
        self._drying_vent_interval = None

        # Per-gate drying state (multi-heater mode)
        # gate -> dict(state, start_time, end_time, temp, humidity_target, done_reason, last_temp, last_humidity)
        self._gate_drying = {}    # Contains details required for managing drying for scheduled gates

        # Drying state indexed by gate
        self._drying_state = [self.DRYING_STATE_NONE] * self.mmu.num_gates

        self._drying_queue = []   # Queued gates awaiting heater capacity (FIFO)
        self._vent_timer = None

        # Optional auto spool rotation (eSpooler)
        self._rotate_timer = None
        self._rotate_enabled = False
        self.spools_to_rotate = [] # Queue of spools that we are rotating (one at a time)


    # Module has no ready/connect/disconnect lifecycle hooks


    def set_test_config(self, gcmd):
        if self.has_heater():
            self.heater_default_dry_temp = gcmd.get_float('HEATER_DEFAULT_DRY_TEMP', self.heater_default_dry_temp, above=0.)
            self.heater_default_dry_time = gcmd.get_float('HEATER_DEFAULT_DRY_TIME', self.heater_default_dry_time, above=0.)
            self.heater_default_humidity = gcmd.get_float('HEATER_DEFAULT_HUMIDITY', self.heater_default_humidity, above=0.)
            self.heater_vent_macro       = gcmd.get(      'HEATER_VENT_MACRO', self.heater_vent_macro)
            self.heater_vent_interval    = gcmd.get_float('HEATER_VENT_INTERVAL', self.heater_vent_interval, minval=0)
            self.heater_rotate_interval  = gcmd.get_float('HEATER_ROTATE_INTERVAL', self.heater_rotate_interval, minval=0)


    def get_test_config(self):
        msg = ""
        if self.has_heater():
            msg = "\n\nHEATER:"
            msg += "\nheater_default_dry_temp = %.1f" % self.heater_default_dry_temp
            msg += "\nheater_default_dry_time = %.1f" % self.heater_default_dry_time
            msg += "\nheater_default_humidity = %.1f" % self.heater_default_humidity
            msg += "\nheater_vent_macro = %s" % self.heater_vent_macro
            msg += "\nheater_vent_interval = %.1f" % self.heater_vent_interval
            msg += "\nheater_rotate_interval = %.1f" % self.heater_rotate_interval

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
        for s in self._drying_state:
            if s == self.DRYING_STATE_ACTIVE or s == self.DRYING_STATE_QUEUED:
                return True
        return False


    def _has_per_gate_heaters(self):
        """
        Returns whether this MMU configuration has a separate heater for each gate
        (and corresponding environment sensor per gate)
        """
        heaters = self.mmu.mmu_machine.filament_heaters
        sensors = self.mmu.mmu_machine.environment_sensors
        if not heaters or not sensors:
            return False
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


    def _get_active_gates(self):
        """
        Return list of active gates from per-gate drying states
        """
        return [i for i, s in enumerate(self._drying_state) if s == self.DRYING_STATE_ACTIVE]


    #
    # GCODE Commands -----------------------------------------------------------
    #

    cmd_MMU_HEATER_help = "Control MMU heater(s) and filament drying cycle"
    cmd_MMU_HEATER_param_help = (
        "MMU_HEATER: %s\n" % cmd_MMU_HEATER_help
        + "STOP = [0|1] Turn off heater and drying cycle\n"
        + "DRYING_DATA = [0|1] Dump configured drying data for filament types\n"
        + "DRY = [0|1] Disable/enable filament heater for filament drying cycle\n"
        + "TIMER = #(mins) Force drying time\n"
        + "TEMP = #(degrees) Force temperature\n"
        + "HUMIDITY = % Terminate drying when humidty goal is reached\n"
        + "GATES = g1,g2 Gates to control ONLY IF MMU has per-gate heaters/dryers\n"
        + "ROTATE = [0|1] Rotate spool (requires eSpooler and explicit GATES)\n"
        + "ROTATE_INTERVAL = #(mins) How often to rotate spools when drying (requires eSpooler)\n"
        + "VENT_INTERVAL = #(mins) How often to call 'vent' macro in drying cycle\n"
        + "(no parameters for status report)"
    )
    def cmd_MMU_HEATER(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_HEATER_param_help), color=True)
            return

        if not self.has_heater():
            raise gcmd.error("No MMU heater configured")

        drying_data = gcmd.get_int('DRYING_DATA', 0, minval=0, maxval=1)
        stop = gcmd.get_int('STOP', None, minval=0, maxval=1)
        dry = gcmd.get_int('DRY', None, minval=0, maxval=1)
        timer = gcmd.get_float('TIMER', None, minval=0.)
        temp = gcmd.get_float('TEMP', None, minval=0., maxval=self.heater_max_temp)
        humidity = gcmd.get_float('HUMIDITY', self.heater_default_humidity, minval=0.)
        vent_interval = gcmd.get_float('VENT_INTERVAL', self.heater_vent_interval, minval=0.)
        rotate = gcmd.get_int('ROTATE', 0, minval=0, maxval=1)
        rotate_interval = gcmd.get_float('ROTATE_INTERVAL', self.heater_rotate_interval, minval=1.)

        # GATE is a common user mistake so interpret as GATES of one element
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu.num_gates - 1)
        if gate is not None:
            gates_str = str(gate)
        else:
            gates_str = gcmd.get('GATES', "!")

        gates = []
        if gates_str != "!":
            # Supplied list of gates
            gates_param = True
            try:
                for gate in gates_str.split(','):
                    gate = int(gate)
                    if 0 <= gate < self.mmu.num_gates:
                        gates.append(gate)
            except ValueError:
                raise gcmd.error("Invalid GATES parameter: %s" % gates_str)
        else:
            gates_param = False
            all_gates = list(range(self.mmu.num_gates))
            empty_gates = [
                i for i, status in enumerate(self.mmu.gate_status)
                if status == self.mmu.GATE_EMPTY
            ]
            full_gates = [
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
                parts.append("<1 minute")
            return " ".join(parts)

        # Display drying data table ---------------------------------------------
        if drying_data:
            msg = u"Drying data:\n"
            for material in sorted(self.drying_data.keys()):
                t, minutes = self.drying_data[material]
                # Avoid format() on unicode with alignment in Py2 edge-cases; keep it simple
                msg += u"%s %s°C for %s\n" % (material + ":", int(t), _format_minutes(minutes))
            self.mmu.log_always(msg)
            return

        # Cancel drying cycle / Heater off --------------------------------------
        if stop or temp == 0:
            if self._has_per_gate_heaters():
                if not gates_param:
                    gates = all_gates

                if self.is_drying():
                    # STOP=1 with explicit GATES=... cancels only those gates in multi-heater mode
                    cancelled = self._cancel_gates(gates, reason="cancelled")
                    if cancelled:
                        self.mmu.log_info("Cancelled drying for gates: %s" % ",".join(map(str, gates)))
                    else:
                        self.mmu.log_info("No matching active/queued gates to cancel")

                    # If all gates are now done, stop overall cycle
                    all_done = True
                    for g in self._drying_gates:
                        gd = self._gate_drying.get(g)
                        if not gd or gd.get('state') not in [self.DRYING_STATE_COMPLETE, self.DRYING_STATE_CANCELLED]:
                            all_done = False
                            break
                    if all_done:
                        self._stop_drying_cycle("Drying cycle stopped (all selected gates cancelled)", reset_state=True)

                else:
                    # Always make sure heater is turned off
                    for gate in gates:
                        self._heater_off(gate=gate)

            else:
                # Otherwise stop whole cycle / single heater off
                if self.is_drying():
                    self.mmu.log_info("Cancelled drying cycle")
                    self._stop_drying_cycle(reset_state=True)

                else:
                    # Always make sure heater is turned off
                    self._heater_off()

            return

        # Raw heater control ----------------------------------------------------
        if not dry and temp is not None:
            if not gates_param:
                gates = full_gates # Default to all non empty gates

            # In per-gate mode, apply TEMP to the selected gate heaters
            if self._has_per_gate_heaters():
                if not gates:
                    self.mmu.log_always("No gates selected for raw heater control")
                    return

                if len(gates) > self.mmu.mmu_machine.max_concurrent_heaters:
                    self.mmu.log_error("Exceeded max concurrent heaters")
                    return

                # Best-effort: set each selected gate heater to TEMP
                #  - If gate is queued in a drying cycle: only update _gate_drying target (do not turn on yet)
                #  - If gate is active: update _gate_drying and apply immediately
                #  - If not in drying cycle OR gate not in current drying gates: apply immediately
                for gate in gates:
                    gd = self._gate_drying.get(gate)

                    if self.is_drying() and gd is not None:
                        state = gd.get('state')

                        # Update per-gate target in all cases when part of cycle
                        gd['temp'] = temp

                        if state == self.DRYING_STATE_QUEUED:
                            # Don't power on yet; it will be applied when the gate becomes active
                            continue

                        # Active (or any unexpected state): apply immediately
                        self._heater_on(temp, gate=gate)
                        continue

                    # Not in drying cycle, or gate not part of current cycle: apply immediately
                    self._heater_on(temp, gate=gate)

                return

            # Single heater mode
            self._heater_on(temp)
            if self.is_drying():
                self._drying_temp = temp
            return

        # Initiate drying cycle -------------------------------------------------
        if dry:
            if not self.has_env_sensor():
                self.mmu.log_warning("MMU environment sensor not found. Check 'environment_sensor' configuration")
                return

            if self.is_drying():
                self.mmu.log_always("MMU already in filament drying cycle. Stop current cycle first")
                return

            # Optional spool rotation (requires eSpooler and explicit gates)
            # (BTT ViViD is allowed if not in print)
            if rotate and not (self.mmu.has_espooler() or self.mmu.mmu_machine.mmu_vendor == VENDOR_VVD):
                self.mmu.log_warning("Rotation requested but eSpooler not fitted - ignoring")
                rotate = 0

            if rotate and not gates_param:
                raise gcmd.error("ROTATE requires explicit GATES parameter")

            if not rotate and not gates_param:
                gates = full_gates # Default to all non empty gates

            if rotate:
                for gate in gates:
                    if self.mmu.gate_status[gate] != self.mmu.GATE_EMPTY:
                        self.mmu.log_warning("Gate %d is not empty so cannot rotate (filament end must be removed from the gate and secured to the spool for rotation)" % gate)

            # Per-gate recommended temps/times, plus overall notes
            per_gate_plan = self._get_drying_plan(gates)
            # If TIMER specified, override all selected gates to that time (multi-heater mode uses per-gate timers)
            if timer is not None and self._has_per_gate_heaters():
                for gate in gates:
                    per_gate_plan[gate]['timer'] = timer

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
                # Default to each filament type recommended temperature and dry time
                lowest = self.heater_default_dry_temp
                longest = self.heater_default_dry_time
                for gate in gates:
                    t = per_gate_plan[gate]['temp']
                    d = per_gate_plan[gate]['timer']
                    if t < lowest: lowest = t
                    if d > longest: longest = d

                # If we only have a single heater apply the lowest temp for longest time
                if not self._has_per_gate_heaters():
                    temp = lowest
                    info = "specified"
                    if timer is None:
                        timer = longest
                        info = "longest"
                    self.mmu.log_info(u"Defaulting to lowest drying temperature of %.1f°C for %s %s given filaments types currently in MMU"
                                      % (temp, info, _format_minutes(timer)))

            # Note that in multi-heater mode, each gate's temp and end_time is tracked independently
            self._drying_time = timer or self.heater_default_dry_time
            self._drying_temp = temp or self.heater_default_dry_temp
            self._drying_humidity_target = humidity
            self._drying_start_time = self.mmu.reactor.monotonic()
            self._drying_end_time = self._drying_start_time + self._drying_time * 60
            self._drying_gates = gates
            self._drying_vent_interval = vent_interval
            self._drying_rotate_interval = rotate_interval

            # Optional spool rotation state
            self.spools_to_rotate = []
            self._rotate_enabled = bool(rotate)
            if self._rotate_enabled:
                self._rotate_timer = rotate_interval * 60.0
            else:
                self._rotate_timer = None

            # Initiate drying cycle
            self._start_drying_cycle(per_gate_plan)
            msg = u"MMU filament drying cycle started:"

        elif self.is_drying():
            msg = u"MMU is in filament drying cycle:"

        else: # Not in drying cycle, but let's check heaters
            if self._has_per_gate_heaters():
                # Per-gate heaters
                heaters_on = []
                heaters_off = []
                # Report all gates (0..num_gates-1)
                for gate in range(self.mmu.num_gates):
                    cur_temp, cur_target = self._get_heater_status(gate)
                    if cur_target is None:
                        # Heater missing / not configured; skip silently
                        continue

                    if cur_target != 0:
                        heaters_on.append((gate, cur_target, cur_temp))
                    else:
                        heaters_off.append(gate)

                if heaters_on:
                    msg = u"Not in drying cycle but one or more gate heaters are on:"
                    for gate, target, actual in heaters_on:
                        msg += u"\nGate %d: Target temperature %.1f°C (current: %.1f°C)" % (gate, target, actual)
                    if heaters_off:
                        msg += u"\nGate heaters off: %s" % u",".join([str(g) for g in heaters_off])
                else:
                    msg = u"Not in drying cycle and all gate heaters are off"

            else:
                # Single shared heater
                cur_temp, cur_target = self._get_heater_status()
                if cur_target is None:
                    msg = u"Heater if not found / misconfigured"
                elif cur_target != 0:
                    msg = u"Not in drying cycle but heater is on. Target temperature: %.1f°C (current: %.1f°C)" % (cur_target, cur_temp)
                else:
                    msg = u"Not in drying cycle and heater is off"

        # Display status report of drying cycle ---------------------------------
        if self.is_drying():
            now = self.mmu.reactor.monotonic()

            if self._drying_gates:
                msg += u"\nDrying filaments in gates: %s" % u",".join(str(g) for g in self._drying_gates)

            if not self._has_per_gate_heaters():
                # Single heater status report
                remaining_mins = _format_minutes((self._drying_end_time - now) // 60)
                cur_temp, cur_humidity = self._get_environment_status()
                msg += u"\nCycle time: %s (remaining: %s)" % (_format_minutes(self._drying_time), remaining_mins)
                if cur_temp is not None:
                    msg += u"\nTarget humidity: %.1f%%" % self._drying_humidity_target
                    if cur_humidity is not None:
                        msg += u" (current: %.1f%%)" % cur_humidity
                else:
                    msg += u"\nEnvironment sensor not available / misconfigured"
                    # Use heater's temp instead
                    cur_temp, _ = self._get_heater_status()
                    if cur_temp is None: cur_temp = -1 # Saftey, should not be possible to get here
                msg += u"\nDrying temp: %.1f°C (current: %.1f°C)" % (self._drying_temp, cur_temp)

            else:
                # Per-gate status report
                msg += u"\nPer-gate dryer mode (max concurrent heaters: %d). Humidty target %.1f%%" % (self.mmu.mmu_machine.max_concurrent_heaters, self._drying_humidity_target)
                for gate in self._drying_gates:
                    gd = self._gate_drying.get(gate, {})
                    state = gd.get('state', self.DRYING_STATE_NONE)
                    material = gd.get('material', None)
                    t = gd.get('temp', None)
                    last_t = gd.get('last_temp', None)
                    last_h = gd.get('last_humidity', None)
                    end_t = gd.get('end_time', None)
                    if end_t is not None and state == self.DRYING_STATE_ACTIVE:
                        rem = max(0, int((end_t - now) // 60))
                        rem_txt = _format_minutes(rem)
                    else:
                        rem_txt = None

                    line = u"\nGate %d: " % gate
                    if state == self.DRYING_STATE_ACTIVE:
                        if last_t is not None:
                            line += u"Drying %s %.1f°C (target %.1f°C)" % (material, last_t, t)
                        if last_h is not None:
                            line += u", humidity %.1f%%" % last_h
                        if rem_txt is not None:
                            line += u", %s remaining" % rem_txt

                    elif state == self.DRYING_STATE_QUEUED:
                        line += u"(queued waiting for heater slot, target %.1f°C)" % t

                    elif state in [self.DRYING_STATE_COMPLETE, self.DRYING_STATE_CANCELLED]:
                        reason = gd.get('done_reason', 'complete' if state == self.DRYING_STATE_COMPLETE else 'cancelled')
                        line += u"(%s" % reason
                        if last_h is not None:
                            line += u", final humidity: %.1f%%" % last_h
                        line += u")"

                    msg += line

            # Venting status
            if self._vent_timer is not None:
                msg += u"\nVenting operational (running macro %s every %s, next in %s)" % (
                    self.heater_vent_macro,
                    _format_minutes(self._drying_vent_interval),
                    _format_minutes(max(self.CHECK_INTERVAL, self._vent_timer) / 60),
                )
            else:
                if not self.heater_vent_macro:
                    vent_reason = "heater_vent_macro not set"
                else:
                    vent_reason = "heater_vent_interval is 0"
                msg += u"\nVenting not operational (%s)" % vent_reason

            # Rotation status (eSpooler)
            if self._rotate_enabled:
                msg += u"\nSpool rotation enabled (running every %s, next in %s)" % (
                    _format_minutes(self._drying_rotate_interval),
                    _format_minutes(max(self.CHECK_INTERVAL, self._rotate_timer) / 60),
                )
            elif self.mmu.has_espooler():
                msg += u"\nSpool rotation not enabled"

        # Report status
        self.mmu.log_always(msg)


    def get_status(self, eventtime=None):
        """
        Structured status for client consumption.
        We don't duplicate temperature or humidity data here but expect the client to read configuration
        and look up appropriate heator and environemnt sensor objects directly
        """
        status = {
            'drying_state': list(self._drying_state),
        }
        return status


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
        self.reinit()


    def _check_mmu_environment(self, eventtime):
        """
        Reactor callback to periodically check drying status and to rationalize state
        """
        if not self.is_drying():
            return self.mmu.reactor.NEVER

        now = self.mmu.reactor.monotonic()

        # Per-gate drying mode
        if self._has_per_gate_heaters():
            # Update active gates: check completion / humidity threshold
            completed_any = False
            for gate in list(self._get_active_gates()):
                gd = self._gate_drying.get(gate)
                if not gd or gd.get('state') != self.DRYING_STATE_ACTIVE:
                    continue

                # Read environment sensor
                cur_temp, cur_humidity = self._get_environment_status(gate=gate)
                gd['last_temp'] = cur_temp
                gd['last_humidity'] = cur_humidity

                # Cycle complete (per gate)
                if gd.get('end_time') is not None and (gd['end_time'] - now) <= 0:
                    self._heater_off(gate=gate)
                    gd['state'] = self.DRYING_STATE_COMPLETE
                    gd['done_reason'] = 'timer complete'
                    completed_any = True
                    try:
                        self._drying_state[gate] = self.DRYING_STATE_COMPLETE
                    except Exception:
                        pass
                    continue

                # Humidity goal reached (per gate)
                if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                    self._heater_off(gate=gate)
                    gd['state'] = self.DRYING_STATE_COMPLETE
                    gd['done_reason'] = 'humidity goal reached'
                    completed_any = True
                    try:
                        self._drying_state[gate] = self.DRYING_STATE_COMPLETE
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
                if not gd or gd.get('state') not in [self.DRYING_STATE_COMPLETE, self.DRYING_STATE_CANCELLED]:
                    all_done = False
                    break
            if all_done:
                self._stop_drying_cycle("Drying cycle complete (all gates)", reset_state=False)
                return self.mmu.reactor.NEVER

        else: # Single heater mode

            # Cycle complete?
            if (self._drying_end_time - now) <= 0:
                cur_temp, cur_humidity = self._get_environment_status()
                for gate in range(self.mmu.num_gates):
                    try:
                        if self._drying_state[gate] == self.DRYING_STATE_ACTIVE:
                            self._drying_state[gate] = self.DRYING_STATE_COMPLETE
                    except Exception:
                        pass
                self._stop_drying_cycle("Drying cycle complete. Final humidity: %.1f%%" % cur_humidity, reset_state=False)
                return self.mmu.reactor.NEVER

            # Humidity goal reached?
            cur_temp, cur_humidity = self._get_environment_status()
            if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
                for gate in range(self.mmu.num_gates):
                    try:
                        if self._drying_state[gate] == self.DRYING_STATE_ACTIVE:
                            self._drying_state[gate] = self.DRYING_STATE_COMPLETE
                    except Exception:
                        pass
                self._stop_drying_cycle("Drying cycle terminated because humidity goal %.1f%% reached" % self._drying_humidity_target, reset_state=False)
                return self.mmu.reactor.NEVER

        # Run periodic venting (macro)
        if self._vent_timer is not None:
            self._vent_timer -= self.CHECK_INTERVAL

            if self._vent_timer < 0 and self.heater_vent_macro:
                cmd = self.heater_vent_macro
                if self._has_per_gate_heaters():
                    cmd += " GATES=%s" % ",".join(map(str, self._get_active_gates()))
                self.mmu.log_debug("MmuEnvironmentManager: Running heater vent macro '%s'" % cmd)
                self.mmu.wrap_gcode_command(cmd, exception=False) # Will report errors without exception

                # Reset countdown regardless (prevents hammering if undefined or failing)
                self._vent_timer = self._drying_vent_interval * 60.0 if self._drying_vent_interval else None

        # Run periodic spool rotation (eSpooler)
        if self._rotate_timer is not None and self._rotate_enabled:
            self._rotate_timer -= self.CHECK_INTERVAL

            if self._rotate_timer < 0:
                # Re-check EMPTY status at time of rotation (supports dynamic state changes)
                if self._has_per_gate_heaters():
                    candidates = list(self._get_active_gates())
                else:
                    candidates = list(range(self.mmu.num_gates))

                gates_to_rotate = []
                for gate in candidates:
                    try:
                        if self.mmu.gate_status[gate] == self.mmu.GATE_EMPTY:
                            gates_to_rotate.append(gate)
                    except Exception:
                        pass

                if gates_to_rotate:
                    self._rotate_spools_in_gates(gates_to_rotate)

                self._rotate_timer = self._drying_rotate_interval * 60.0 # To seconds

        # Reschedule
        return eventtime + self.CHECK_INTERVAL


    def _start_drying_cycle(self, per_gate_plan=None):
        if self.is_drying():
            return

        self.mmu.log_debug("MmuEnvironmentManager: Filament drying started")

        # Reset state at the beginning of a new cycle
        self._drying_state = [self.DRYING_STATE_NONE] * self.mmu.num_gates

        # Vent timer countdown (seconds). 0/None disables venting.
        if self._drying_vent_interval:
            self._vent_timer = self._drying_vent_interval * 60.0 # To seconds
        else:
            self._vent_timer = None

        # Turn on heater or heaters depending on mode
        if not self._has_per_gate_heaters():
            # Single heater mode
            for gate in range(self.mmu.num_gates):
                try:
                    self._drying_state[gate] = self.DRYING_STATE_ACTIVE
                except Exception:
                    pass
            self._heater_on(self._drying_temp)

        else:
            # Multi heater mode: Initialize per-gate state and start as many as possible
            self._gate_drying = {}
            self._drying_queue = []
            self._drying_state = [self.DRYING_STATE_NONE] * self.mmu.num_gates

            if per_gate_plan is None:
                per_gate_plan = self._get_drying_plan(self._drying_gates)

            # Queue all selected gates; we'll start up to max_concurrent_heaters
            for gate in self._drying_gates:
                plan = per_gate_plan.get(gate, {})
                gtemp = plan.get('temp', self.heater_default_dry_temp)
                gtime = plan.get('timer', self.heater_default_dry_time)
                material = plan.get('material', "unknown")

                self._gate_drying[gate] = {
                    'state': self.DRYING_STATE_QUEUED,
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
                    self._drying_state[gate] = self.DRYING_STATE_QUEUED
                except Exception:
                    pass

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

        while len(self._get_active_gates()) < max_h and self._drying_queue:
            gate = self._drying_queue.pop(0)
            gd = self._gate_drying.get(gate)
            if not gd or gd.get('state') != self.DRYING_STATE_QUEUED:
                continue

            # Use per-gate time (from material) else user forced timer or default time
            per_gate_minutes = gd.get('timer', None)
            if per_gate_minutes is None:
                per_gate_minutes = int(self._drying_time)

            gd['start_time'] = now
            gd['end_time'] = now + (int(per_gate_minutes) * 60)
            gd['state'] = self.DRYING_STATE_ACTIVE
            gd['done_reason'] = None

            try:
                self._drying_state[gate] = self.DRYING_STATE_ACTIVE
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
            self.mmu.reactor.update_timer(self._periodic_timer, self.mmu.reactor.NEVER)

            # Turn off all heaters in either mode
            if self._has_per_gate_heaters():
                for gate in list(self._get_active_gates()):
                    self._heater_off(gate=gate)
                # Best effort: also turn off any configured heaters for selected gates
                for gate in self._drying_gates:
                    self._heater_off(gate=gate)
            else:
                self._heater_off()

            self._drying_queue = []
            self._drying_gates = []

            if reset_state:
                self._drying_state = [self.DRYING_STATE_NONE] * self.mmu.num_gates

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
        now = self.mmu.reactor.monotonic()

        for gate in list(gates):
            gd = self._gate_drying.get(gate)

            # If we don't have state for it, it might not be part of this cycle
            if gd is None:
                continue

            state = gd.get('state')

            if state == self.DRYING_STATE_ACTIVE:
                # Turn off heater and mark done
                self._heater_off(gate=gate)
                gd['state'] = self.DRYING_STATE_COMPLETE
                gd['done_reason'] = reason
                gd['end_time'] = now
                self._drying_state[gate] = self.DRYING_STATE_CANCELLED
                cancelled += 1

            elif state == self.DRYING_STATE_QUEUED:
                # Remove from pending queue and mark done
                try:
                    while gate in self._drying_queue:
                        self._drying_queue.remove(gate)
                except Exception:
                    pass
                gd['state'] = self.DRYING_STATE_COMPLETE
                gd['done_reason'] = reason
                gd['end_time'] = now
                self._drying_state[gate] = self.DRYING_STATE_CANCELLED
                cancelled += 1

            elif state == self.DRYING_STATE_COMPLETE:
                # Already done; no-op
                pass

        # After cancellations, if we freed heater slots start next queued gates
        if self._has_per_gate_heaters():
            self._start_next_queued_gates(now)

        return cancelled


    def _heater_on(self, temp, gate=None):
        """
        Turn MMU heater on.
        """
        if not self._has_per_gate_heaters():
            self.mmu.log_debug(u"MmuEnvironmentManager: Heater %s set to target temp of %.1f°C" % (self.mmu.mmu_machine.filament_heater, temp))
            hname = self._heater_name(self.mmu.mmu_machine.filament_heater)
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (hname, temp))
            return

        heaters = self.mmu.mmu_machine.filament_heaters
        if gate < 0 or gate >= len(heaters) or not heaters[gate]:
            self.mmu.log_warning("MmuEnvironmentManager: No heater configured for gate %d" % gate)
            return

        hname = self._heater_name(heaters[gate])
        self.mmu.log_debug(u"MmuEnvironmentManager: Gate %d heater %s set to target temp of %.1f°C" % (gate, hname, temp))
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (hname, temp))


    def _heater_off(self, gate=None):
        """
        Turn MMU heater off. If gate=None then turn off all heaters
        """
        if not self._has_per_gate_heaters() and self.mmu.mmu_machine.filament_heater:
            self.mmu.log_debug("MmuEnvironmentManager: Heater %s turned off" % self.mmu.mmu_machine.filament_heater)
            hname = self._heater_name(self.mmu.mmu_machine.filament_heater)
            self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % hname)
            return

        if gate is None:
            # Turn off all known heaters (best effort)
            self.mmu.log_debug("MmuEnvironmentManager: All gate heaters turned off")
            heaters = self.mmu.mmu_machine.filament_heaters
            for i in range(len(heaters)):
                if heaters[i]:
                    hname = self._heater_name(heaters[i])
                    self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % hname)
            return

        heaters = self.mmu.mmu_machine.filament_heaters
        if gate < 0 or gate >= len(heaters) or not heaters[gate]:
            return
        _,target = self._get_heater_status(gate)
        if target:
            hname = self._heater_name(heaters[gate])
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
            heater_name = self.mmu.mmu_machine.filament_heater
        else:
            heaters = self.mmu.mmu_machine.filament_heaters
            if gate < 0 or gate >= len(heaters) or not heaters[gate]:
                return (None, None)
            heater_name = heaters[gate]

        obj = self.mmu.printer.lookup_object(heater_name, None)
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


    def _rotate_spools_in_gates(self, gates):
        """
        eSpooler-driven spool rotation.
        Move the spools in the retract direction a small distance, 90 degrees is perfect
        """
        self.mmu.log_info("Rotating spools in gates: %s..." % ",".join(map(str, gates)))
        if self.mmu.mmu_machine.mmu_vendor != VENDOR_VVD:
            self.spools_to_rotate = list(gates)
            # Initiate rotation of first spool -- they are moved in sequence for asetics and to avoid possiblity of overload
            self._rotate_spool(self.spools_to_rotate[0])
            return

        # Special case VVD design because of unique spool rotation using shared gear stepper coupled to gate selection
        if not self.mmu.is_in_print():
            gate_selected = self.mmu.gate_selected
            for gate in gates:
                self.mmu.select_gate(gate)
                _,_,_,_ = self.mmu.trace_filament_move("Rotating spool for drying", -100, motor="gear", wait=True)
            self.mmu.select_gate(gate_selected)


    def _rotate_spool(self, gate):
        """
        Send event to  cause a small rewind action to rotate the spool in gate
        """
        power = self.mmu.espooler_rewind_burst_power
        duration = self.mmu.espooler_rewind_burst_duration
        self.mmu.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, self.mmu.ESPOOLER_REWIND)


    def _handle_espooler_burst_done(self, gate):
        """
        Event indicating that a spool rotation completed
        """
        if gate in self.spools_to_rotate:
            self.spools_to_rotate.remove(gate)
            if self.spools_to_rotate:
                self._rotate_spool(self.spools_to_rotate[0])


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
        max_temp = self.heater_max_temp
        default_temp = self.heater_default_dry_temp
        default_time = self.heater_default_dry_time

        plan = {}
        for gate in gates:
            material = self.mmu.gate_material[gate]
            key = str(material).upper()
            temp, duration = self.drying_data.get(key, (default_temp, default_time))
            plan[gate] = {
                'material': material,
                'temp': float(min(max_temp, temp)),
                'timer': int(duration),
            }
        return plan

