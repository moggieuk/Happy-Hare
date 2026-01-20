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

    CHECK_INTERVAL = 60 # How often to check heater and environment sensors (seconds)

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
            self.drying_data = {str(k).upper(): v for k, v in drying_data.items()}
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


    def has_heater(self):
        return True # TODO (move to mmu.py?)


    def has_env_sensor(self):
        return True # TODO (move to mmu.py?)


    #
    # GCODE Commands -----------------------------------------------------------
    #

    cmd_MMU_HEATER_help = "Enable/disable MMU heater (filament dryer)"
    cmd_MMU_HEATER_param_help = (
        "MMU_HEATER: %s\n" % cmd_MMU_HEATER_help
        + "OFF = [0|1] Turn off heater and drying cycle\n"
        + "DRY = [0|1] Disable/enable filament heater for filament drying cycle\n"
        + "TIME = #(mins) Force drying time\n"
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
        time = gcmd.get_int('TIME', self.heater_default_dry_time, minval=0)
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
            hours, mins = divmod(minutes, 60)
            parts = []
            if hours:
                parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
            if mins:
                parts.append("%d minute%s" % (mins, "" if mins == 1 else "s"))
            return " ".join(parts)

        if drying_data:
            # Sort keys for stable, readable output
            msg = "Drying data:\n"
            for material in sorted(self.drying_data.keys()):
                temp, minutes = self.drying_data[material]

                msg += u"{:<6} {:>3}°C for {}\n".format(
                    material + ":",
                    temp,
                    _format_minutes(minutes)
                )

            self.mmu.log_always(msg)

        # Heater off / Cancel drying cycle
        if off or temp == 0:
            self._stop_drying_cycle()
            self._heater_off()
            return

        # Raw heater control
        if not dry and temp is not None:
            self._heater_on(temp)
            if self._drying:
                self._drying_temp = temp
            return

        if dry:
            if self._drying:
                self.mmu.log_always("MMU already in filament drying cycle. Stop current cycle first")
                return

            def_temp, def_time = self._get_max_drying_temp_time(gates)

            if temp is not None:
                if temp > def_temp:
                    self.mmu.log_warning(u"Drying temperature %.1f°C is greater than recommended (%.1f°C) given filaments types in MMU" % (temp, def_temp))
            else:
                # Reduce heat to lowest filament temp in non-empty gates unless overriden
                self.mmu.log_info(u"Defaulting to drying temperature %.1f°C for %s given filaments types in MMU" % (def_temp, _format_minutes(def_time)))
                temp = def_temp

            # Initiate dryer, record state at start of cycle
            self._drying_time = time
            self._drying_temp = temp
            self._drying_humidity_target = humidity
            self._drying_start_time = self.mmu.reactor.monotonic()
            self._drying_end_time = self._drying_start_time + self._drying_time * 60
            self._drying_gates = gates
            self._drying_vent_interval = vent_interval

            self._start_drying_cycle()

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
            remaining_mins = _format_minutes((self._drying_end_time - self.mmu.reactor.monotonic()) // 60)
            if self._drying_gates:
                msg += "\nDrying filaments in gates: %s" % ", ".join(str(g) for g in self._drying_gates)
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

            if self._vent_timer is not None and self._vent_timer > 0:
                msg += "\nVenting operational (runing macro %s every %s, next in %s)" % (
                    self.heater_vent_macro,
                    _format_minutes(self._drying_vent_interval),
                    _format_minutes(int(self._vent_timer / 60)),
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
        return {
            'drying_filament': self._drying
        }


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
        self.mmu.log_warning("PAUL TEMP DEBUG: mmu_environment_manager: _check_mmu_environment()")
        if not self._drying:
            return self.mmu.reactor.NEVER

        cur_temp, cur_humidity = self._get_environment_status()
        if cur_humidity is not None and cur_humidity <= self._drying_humidity_target:
            self.mmu.log_info("MmuEnvironmentManager: Drying cycle terminated because humidity goal %.1f%% reached" % self._drying_humidity_target)
            self._stop_drying_cycle()
            return self.mmu.reactor.NEVER

        # Run periodic venting (macro)
        if self._vent_timer is not None and self._vent_timer > 0:
            self._vent_timer -= self.CHECK_INTERVAL

            if self._vent_timer <= 0 and self.heater_vent_macro:
                self.mmu.log_info("MmuEnvironmentManager: Running heater vent macro '%s'" % self.heater_vent_macro)
                self.mmu.wrap_gcode_command(self.heater_vent_macro, exception=False) # Will report errors without exception

                # Reset countdown regardless (prevents hammering if undefined or failing)
                self._vent_timer = self._drying_vent_interval * 60.0 if self._drying_vent_interval else None

        # Reschedule
        return eventtime + self.CHECK_INTERVAL


    def _start_drying_cycle(self):
        if not self._drying:
            self.mmu.log_info("MmuEnvironmentManager: Filament drying started")
            self._drying = True

            # Vent timer countdown (seconds). 0/None disables venting.
            if self._drying_vent_interval and self._drying_vent_interval > 0:
                self._vent_timer = self._drying_vent_interval * 60.0 # To seconds
            else:
                self._vent_timer = None

            self._heater_on(self._drying_temp)
            self.mmu.reactor.update_timer(self._periodic_timer, self.mmu.reactor.NOW)


    def _stop_drying_cycle(self):
        if self._drying:
            self.mmu.log_info("MmuEnvironmentManager: Filament drying stopped")
            self.mmu.reactor.update_timer(self._periodic_timer, self.mmu.reactor.NEVER)
            self._heater_off()
            self._drying = False


    def _heater_on(self, temp):
        self.mmu.log_info(u"MmuEnvironmentManager: Heater %s set to target temp of %.1f°C" % (self.mmu.mmu_machine.filament_heater, temp))
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f" % (self.mmu.mmu_machine.filament_heater, temp))


    def _heater_off(self):
        self.mmu.log_info(u"MmuEnvironmentManager: Heater %s turned off" % self.mmu.mmu_machine.filament_heater)
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % self.mmu.mmu_machine.filament_heater)


    def _get_heater_status(self):
        status = self.mmu.printer.lookup_object(self.mmu.mmu_machine.filament_heater).get_status(0)
        temperature = status.get('temperature')
        target = status.get('target')
        power = status.get('power')
        return (temperature, target)


    def _get_environment_status(self):
        """
        Return tuple of temperature and humidity from environment sensor.
        Note that some configured sensors may only offer temperature
        """
        sensor = self.mmu.mmu_machine.environment_sensor
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

