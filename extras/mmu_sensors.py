# Happy Hare MMU Software
# Easy setup of all sensors for MMU
#
# Pre-gate sensors:
#   Simplifed filament switch sensor easy configuration of pre-gate sensors used to detect runout and insertion of filament
#   and preload into gate and update gate_map when possible to do so based on MMU state, not printer state
#   Essentially this uses the default `filament_switch_sensor` but then replaces the runout_helper
#   Each has name `mmu_pre_gate_X` where X is gate number
#
# mmu_gear sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gear_X` where X is the gate number
#
# mmu_gate sensor(s):
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gate`
#
# extruder & toolhead sensor:
#   Wrapper around `filament_switch_sensor` disabling all functionality - just for visability
#   Named `extruder` & `toolhead`
#
# sync feedback sensor(s):
#   Creates buttons handlers (with filament_switch_sensor for visibility and control) and publishes events based on state change
#   Named `sync_feedback_compression` & `sync_feedback_tension`
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# RunoutHelper based on:
# Generic Filament Sensor Module Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, time

import configparser, configfile

INSERT_GCODE = "__MMU_SENSOR_INSERT"
REMOVE_GCODE = "__MMU_SENSOR_REMOVE"
RUNOUT_GCODE = "__MMU_SENSOR_RUNOUT"
CLOG_GCODE   = "__MMU_SENSOR_CLOG"
TANGLE_GCODE = "__MMU_SENSOR_TANGLE"


# -------------------------------------------------------------------------------------------------
# Enhanced "runout helper" that gives greater control of when filament sensor events are fired and
# direct access to button events in addition to creating a "remove" / "runout" distinction
class MmuRunoutHelper:

    def __init__(self, printer, name, event_delay, gcodes, insert_remove_in_print, button_handler, switch_pin):
        """
        gcodes: dict of gcode macros to call for each event type.
        Any key can be omitted or set to None/"" to disable that event.
        """

        self.printer, self.name = printer, name

        # Expecting a dict with keys like "insert", "remove", "runout", "clog", "tangle"
        self.gcodes = gcodes or {}

        self.insert_remove_in_print = insert_remove_in_print
        self.button_handler = button_handler
        self.switch_pin = switch_pin
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.event_delay = event_delay # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_suspended = None
        self.button_handler_suspended = False

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # Replace previous runout_helper mux commands with ours
        prev = self.gcode.mux_commands.get("QUERY_FILAMENT_SENSOR")
        if prev:
            _, prev_values = prev
            prev_values[self.name] = self.cmd_QUERY_FILAMENT_SENSOR

        prev = self.gcode.mux_commands.get("SET_FILAMENT_SENSOR")
        if prev:
            _, prev_values = prev
            prev_values[self.name] = self.cmd_SET_FILAMENT_SENSOR



    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2. # Time to wait before first events are processed


    def _insert_event_handler(self, eventtime):
        insert_gcode = self.gcodes.get("insert")
        self._exec_gcode("%s EVENTTIME=%s" % (insert_gcode, eventtime) if insert_gcode else None)


    def _remove_event_handler(self, eventtime):
        remove_gcode = self.gcodes.get("remove")
        self._exec_gcode("%s EVENTTIME=%s" % (remove_gcode, eventtime) if remove_gcode else None)


    def _runout_event_handler(self, eventtime, event_type):
        # Pausing from inside an event requires that the pause portion of pause_resume execute immediately.
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_pause_command()
        handler_gcode = self.gcodes.get(event_type)
        self._exec_gcode("%s EVENTTIME=%s" % (handler_gcode, eventtime) if handler_gcode else None)


    def _exec_gcode(self, command):
        if command:
            try:
                self.gcode.run_script(command)
            except Exception:
                logging.exception("MMU: Error running mmu sensor handler: `%s`" % command)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay


    # Latest klipper v0.12.0-462 added the passing of eventtime
    #     old: note_filament_present(self, is_filament_present):
    #     new: note_filament_present(self, eventtime, is_filament_present):
    def note_filament_present(self, *args):
        if len(args) == 1:
            eventtime = self.reactor.monotonic()
            is_filament_present = args[0]
        else:
            eventtime = args[0]
            is_filament_present = args[1]

        prev_filament_present = self.filament_present
        self.filament_present = bool(is_filament_present)

        # Button handlers are used for sync feedback state switches
        if self.button_handler and not self.button_handler_suspended:
            self.button_handler(eventtime, self.name, is_filament_present, self)

        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present

        # Don't handle too early or if disabled
        if eventtime >= self.min_event_systime and self.sensor_enabled:
            self._process_state_change(eventtime, is_filament_present)


    def _process_state_change(self, eventtime, is_filament_present):
        # Determine "printing" status
        now = self.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is not None:
            is_printing = print_stats.get_status(now)["state"] == "printing"
        else:
            is_printing = self.printer.lookup_object("idle_timeout").get_status(now)["state"] == "Printing"

        insert_gcode = self.gcodes.get("insert")
        remove_gcode = self.gcodes.get("remove")
        runout_gcode = self.gcodes.get("runout")

        if is_filament_present and insert_gcode: # Insert detected
            if not is_printing or (is_printing and self.insert_remove_in_print):
                logging.info("MMU: filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime)) # PAUL uncommented
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._insert_event_handler(eventtime))

        else: # Remove or Runout detected
            if is_printing and self.runout_suspended is False and runout_gcode:
                logging.info("MMU: filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime)) # PAUL uncommented
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime, "runout"))
            elif remove_gcode and (not is_printing or self.insert_remove_in_print):
                # Just a "remove" event
                logging.info("MMU: filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime)) # PAUL uncommented
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._remove_event_handler(eventtime))


    def note_clog_tangle(self, event_type):
        logging.info("MMU: filament sensor %s: %s event detected, Eventtime %.2f" % (self.name, event_type, eventtime)) # PAUL uncommented
        self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
        self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime, event_type))


    def enable_runout(self, restore):
        self.runout_suspended = not restore


    def enable_button_feedback(self, restore):
        self.button_handler_suspended = not restore


    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
            "runout_suspended": bool(self.runout_suspended),
        }


    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"
    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "MMU Sensor %s: filament detected" % (self.name)
        else:
            msg = "MMU Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)


    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"
    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = bool(gcmd.get_int("ENABLE", 1))


# EXPERIMENT/HACK to support ViViD analog buffer "endstops" -------------------------------
# This class implments both the filament switch sensor and endstop. However:
#  * it will not display in UI because no filament_switch_sensor exists in config
#  * does not involve the mcu in the homing process so it can't be accurate
#  * suffers from inherent averaging lag for analog inputs
class MmuAdcSwitchSensor:

    def __init__(self, config, name_prefix, gate, switch_pin, event_delay, a_range, insert=False, remove=False, runout=False, clog=False, tangle=False, insert_remove_in_print=False, button_handler=None, a_pullup=4700.):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self._pin = switch_pin
        self._steppers = []
        self._trigger_completion = None
        self._last_trigger_time = None

        buttons = self.printer.load_object(config, 'buttons')
        a_min, a_max = a_range
        buttons.register_adc_button(switch_pin, a_min, a_max, a_pullup, self._button_handler)
        self.name = name = "%s_%d" % (name_prefix, gate)
        insert_gcode = ("%s SENSOR=%s%s" % (INSERT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if insert else None
        remove_gcode = ("%s SENSOR=%s%s" % (REMOVE_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if remove else None
        runout_gcode = ("%s SENSOR=%s%s" % (RUNOUT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if runout else None
        clog_gcode   = ("%s SENSOR=%s%s" % (CLOG_GCODE,   name, (" GATE=%d" % gate) if gate is not None else "")) if clog else None
        tangle_gcode = ("%s SENSOR=%s%s" % (TANGLE_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if tangle else None
        self.runout_helper = MmuRunoutHelper(
            self.printer,
            name,
            event_delay,
            gcodes={
                "insert": insert_gcode,
                "remove": remove_gcode,
                "runout": runout_gcode,
                "clog":   clog_gcode,
                "tangle": tangle_gcode,
            },
            insert_remove_in_print=insert_remove_in_print,
            button_handler=button_handler,
            switch_pin=switch_pin
        )
        self.get_status = self.runout_helper.get_status


    def _button_handler(self, eventtime, state):
        self.runout_helper.note_filament_present(eventtime, state)
        if self._trigger_completion is not None:
            self._last_trigger_time = eventtime
            self._trigger_completion.complete(True)


    # Required to implement an endstop -------

    def query_endstop(self, print_time):
        return self.runout_helper.filament_present


    def setup_pin(self, pin_type, pin_name):
        return self


    def add_stepper(self, stepper):
        self._steppers.append(stepper)


    def get_steppers(self):
        return list(self._steppers)


    def home_start(self, print_time, sample_time, sample_count, rest_time, triggered):
        self._trigger_completion = self.reactor.completion()
        self._last_trigger_time = None
        self._homing = True
        self._triggered = triggered

        if self.runout_helper.filament_present == self._triggered:
            self._last_trigger_time = print_time
            self._trigger_completion.complete(True)

        return self._trigger_completion


    def home_wait(self, home_end_time):
        self._homing = False
        self._trigger_completion = None

        if self._last_trigger_time is None:
            raise self.printer.command_error("No trigger on %s after full movement" % self.name)

        return self._last_trigger_time


# Analog Filament Tension Sensor used for proportional sync-feedback
# Maps [0..set_point] -> [-1..0]  and  [set_point..1] -> [0..1]
# Range multiplier is applied to the ADC reading after set_point mapping.
# Copyright (C) 2023-2025 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
class MmuProportionalSensor:

    def __init__(self, config, name):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = name

        # Config
        self._pin = config.get('sync_feedback_analog_pin')
        self._reversed = config.getboolean('sync_feedback_analog_reversed', False)
        self._set_point = config.getfloat('sync_feedback_analog_set_point', 0.5)
        self._scale = config.getfloat('sync_feedback_analog_scale', 1.0)

        self._sample_time = config.getfloat('sync_feedback_analog_sample_time', 0.005) # Not exposed
        self._sample_count = config.getint('sync_feedback_analog_sample_count', 5)     # Not exposed
        self._report_time = config.getfloat('sync_feedback_analog_report_time', 0.100) # Not exposed
        self._debug = config.getboolean('sync_feedback_analog_debug', False)           # Not exposed

        # State
        self.value_raw = 0.0      # raw ADC value
        self.value_signed = 0.0   # correctly signed after optional inversion
        self.value_offset = 0.0   # after offset but before range multiplier
        self._last_value = 0.0
        self.value = 0.0          # in [-1.0, 1.0] (signed, offset and scaled)

        # Setup ADC
        ppins = self.printer.lookup_object('pins')
        self.adc = ppins.setup_pin('adc', self._pin)

        _kalico = bool(self.printer.lookup_object('danger_options', False))
        if _kalico:
            self.adc.setup_minmax(self._sample_time, self._sample_count)
        else:
            self.adc.setup_adc_sample(self._sample_time, self._sample_count)
        self.adc.setup_adc_callback(self._report_time, self._adc_callback)

        # Attach runout_helper (no gcode actions; just enable/disable plumbing to remove UI nag)
        self.runout_helper = MmuRunoutHelper(
            self.printer,
            self.name,                  # Name exposed to QUERY_/SET_FILAMENT_SENSOR
            0,                          # Event_delay (not used here)
            gcodes={
                "clog": self.CLOG_GCODE,
                "tangle": self.TANGLE_GCODE,
            },
            insert_remove_in_print=False,
            button_handler=None,       # No button handler for analog
            switch_pin=self._pin
        )

        # Expose status
        self.printer.add_object(self.name, self)


    def _remap(self, v_raw: float) -> float:
        # 1) Reverse if specified
        v = 1.0 - v_raw if self._reversed else v_raw

        # 2) clamp ADC to [0,1] for safety
        if v < 0.0: v = 0.0
        if v > 1.0: v = 1.0

        # 3) Map around set_point to [-1,1]
        # (guard extremes so we don't divide by ~0)
        sp = max(1e-6, min(1.0 - 1e-6, self._set_point))
        if v >= sp:
            out = (v - sp) / (1.0 - sp)
        else:
            out = (v - sp) / sp

        # Store pre-multiplier mapped value for display
        self.value_offset = out

        # 4) Apply range multiplier AFTER mapping; clamp to [-1,1]
        out = out * self._scale

        # 5) Clamp range
        return max(-1.0, min(1.0, out))


    def _adc_callback(self, read_time, read_value):
        self.value_raw = float(read_value)

        # Raw after optional reversal; keep unclamped here since _remap clamps
        self.value_signed = 1.0 - read_value if self._reversed else read_value

        # Mapped & scaled value
        self._last_value = self.value
        self.value = self._remap(read_value)  # _remap handles reverse + scaling + mapping

        # Publish proportional sync-feedback event if extreme
        # TODO really extreme should be determined by is_extreme() in mmu_sync_feedback manager (with hysteresis), but object doesn't exist yet in v3 codebase..
        if abs(self.value) >= 1.0 and self.value != self._last_value:
            self.printer.send_event("mmu:sync_feedback", read_time, event_val)


    def get_status(self, eventtime):
        s = {
            "enabled":          bool(self.runout_sensor.sensor_enabled),
            "value":            self.value,             # in [-1.0, 1.0] (mapped * multipler)
            "value_raw":        self.value_raw,         # raw
        }

        if self._debug:
            s.extend({
                "value_signed":     self.value_signed, # raw after reversal if set
                "value_offset":     self.value_offset, # after offset but before range multiplier
                "set_point":        self._set_point,
                "scale":            self._scale,
            })
        return s


class MmuSensors:

    def __init__(self, config):
        from .mmu import Mmu # For sensor names

        self.printer = config.get_printer()
        self.sensors = {}
        mmu_machine = self.printer.lookup_object("mmu_machine", None)
        num_units = mmu_machine.num_units if mmu_machine else 1
        event_delay = config.get('event_delay', 0.5)

        # Setup "mmu_pre_gate" sensors...
        for gate in range(23):
            switch_pin = config.get('pre_gate_switch_pin_%d' % gate, None)
            if switch_pin:
                self._create_mmu_sensor(config, Mmu.SENSOR_PRE_GATE_PREFIX, gate, switch_pin, event_delay, insert=True, remove=True, runout=True, insert_remove_in_print=True)

        # Setup single "mmu_gate" sensor(s)...
        switch_pins = list(config.getlist('gate_switch_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with gate_switch_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_GATE, None, switch_pins, event_delay, runout=True)

        # Setup "mmu_gear" sensors...
        for gate in range(23):
            switch_pin = config.get('post_gear_switch_pin_%d' % gate, None)
            if switch_pin:
                a_range = config.getfloatlist('post_gear_analog_range_%d' % gate, None, count=2)
                if a_range is not None:
                    a_pullup = config.getfloat('post_gear_analog_pullup_resister_%d' % gate, 4700.)
                    s = MmuAdcSwitchSensor(config, Mmu.SENSOR_GEAR_PREFIX, gate, switch_pin, event_delay, a_range, runout=True, a_pullup=a_pullup)
                    self.sensors["%s_%d" % (Mmu.SENSOR_GEAR_PREFIX, gate)] = s
                else:
                    self._create_mmu_sensor(config, Mmu.SENSOR_GEAR_PREFIX, gate, switch_pin, event_delay, runout=True)

        # Setup single extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        if switch_pin:
            self._create_mmu_sensor(config, Mmu.SENSOR_EXTRUDER_ENTRY, None, switch_pin, event_delay, insert=True, runout=True)

        # Setup single toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        if switch_pin:
            self._create_mmu_sensor(config, Mmu.SENSOR_TOOLHEAD, None, switch_pin, event_delay)

        # For Qidi printers or any other that use a hall_filament_width_sensor as an endstop
        hall_sensor_endstop = config.get('hall_sensor_endstop', None)
        if hall_sensor_endstop is not None:
            if hall_sensor_endstop == 'gate':
                target_name = Mmu.SENSOR_GATE
            elif hall_sensor_endstop == 'extruder':
                target_name = Mmu.SENSOR_EXTRUDER_ENTRY
            elif hall_sensor_endstop == 'toolhead':
                target_name = Mmu.SENSOR_TOOLHEAD
            else:
                target_name = hall_sensor_endstop

            self.hall_pin1 = config.get('hall_adc1')
            self.hall_pin2 = config.get('hall_adc2')
            self.hall_dia1 = config.getfloat('hall_cal_dia1', 1.5)
            self.hall_dia2 = config.getfloat('hall_cal_dia2', 2.0)
            self.hall_rawdia1 = config.getint('hall_raw_dia1', 9500)
            self.hall_rawdia2 = config.getint('hall_raw_dia2', 10500)
            self.hall_runout_dia = config.getfloat('hall_min_diameter', 1.0)
            # self.hall_runout_dia_max = config.getfloat('hall_max_diameter', 2.0) - Unused for trigger

            s = MmuHallEndstop(config, target_name, self.hall_pin1, self.hall_pin2,
                               self.hall_dia1, self.hall_rawdia1, self.hall_dia2, self.hall_rawdia2,
                               hall_runout_dia=self.hall_runout_dia,
                               insert=True, runout=True)
            self.sensors[target_name] = s

        # Setup motor syncing feedback sensors...
        switch_pins = list(config.getlist('sync_feedback_tension_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with sync_feedback_tension_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_TENSION, None, switch_pins, 0, clog=True, tangle=True, button_handler=self._sync_tension_callback)
        switch_pins = list(config.getlist('sync_feedback_compression_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with sync_feedback_compression_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_COMPRESSION, None, switch_pins, 0, clog=True, tangle=True, button_handler=self._sync_compression_callback)

        # Setup analog Filament Pressure Sensor for proportional sync feedback
        # Uses single analog input; value scaled in [-1, 1]
        analog_pin = config.get('sync_feedback_analog_pin', None)
        if analog_pin:
            self.sensors[Mmu.SENSOR_PROPORTIONAL] = MmuProportionalSensor(config, name=Mmu.SENSOR_PROPORTIONAL)


    def _create_mmu_sensor(self, config, name_prefix, gate, switch_pins, event_delay, insert=False, remove=False, runout=False, clog=False, tangle=False, insert_remove_in_print=False, button_handler=None):
        switch_pins = [switch_pins] if not isinstance(switch_pins, list) else switch_pins
        for unit, switch_pin in enumerate(switch_pins):
            if not self._is_empty_pin(switch_pin):
                # name must match mmu_sensor_manager
                if gate is not None:
                    name = "%s_%d" % (name_prefix, gate)
                elif len(switch_pins) > 1:
                    name = "unit_%d_%s" % (unit, name_prefix)
                else:
                    name = name_prefix
                sensor = name if gate is not None else "%s_sensor" % name
                section = "filament_switch_sensor %s" % sensor
                config.fileconfig.add_section(section)
                config.fileconfig.set(section, "switch_pin", switch_pin)
                config.fileconfig.set(section, "pause_on_runout", "False")
                fs = self.printer.load_object(config, section)

                # Replace with custom runout_helper because of state specific behavior
                insert_gcode = ("%s SENSOR=%s%s" % (INSERT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if insert else None
                remove_gcode = ("%s SENSOR=%s%s" % (REMOVE_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if remove else None
                runout_gcode = ("%s SENSOR=%s%s" % (RUNOUT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if runout else None
                clog_gcode   = ("%s SENSOR=%s%s" % (CLOG_GCODE,   name, (" GATE=%d" % gate) if gate is not None else "")) if clog else None
                tangle_gcode = ("%s SENSOR=%s%s" % (TANGLE_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if tangle else None
                ro_helper = MmuRunoutHelper(
                    self.printer,
                    sensor,
                    event_delay,
                    gcodes={
                        "insert": insert_gcode,
                        "remove": remove_gcode,
                        "runout": runout_gcode,
                        "clog":   clog_gcode,
                        "tangle": tangle_gcode,
                    },
                    insert_remove_in_print=insert_remove_in_print,
                    button_handler=button_handler,
                    switch_pin=switch_pin
                )
                fs.runout_helper = ro_helper
                fs.get_status = ro_helper.get_status
                self.sensors[name] = fs


    def _is_empty_pin(self, switch_pin):
        if switch_pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(switch_pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''


    def _sync_tension_callback(self, eventtime, tension_state, runout_helper):
        """
        Button event handler for sync-feedback tension switch
        """
        from .mmu import Mmu # For sensor names
        tension_enabled = runout_helper.sensor_enabled
        compression_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_COMPRESSION, None)
        has_active_compression = compression_sensor.runout_helper.sensor_enabled if compression_sensor else False
        compression_state = compression_sensor.runout_helper.filament_present if has_active_compression else False

        if tension_enabled and has_active_compression:
            event_value = 0 if tension_state == compression_state else (-1 if tension_state else 1)

        elif tension_enabled: # no compression active
            event_value = -1 if tension_state else 1

        elif has_active_compression: # tension disabled
            event_value = 1 if compression_state else -1

        else:
            event_value = 0

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)


    def _sync_compression_callback(self, eventtime, compression_state, runout_helper):
        """
        Button event handler for sync-feedback compression switch
        """
        from .mmu import Mmu
        compression_enabled = runout_helper.sensor_enabled
        tension_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_TENSION, None)
        has_active_tension = tension_sensor.runout_helper.sensor_enabled if tension_sensor else False
        tension_state = tension_sensor.runout_helper.filament_present if has_active_tension else False

        if compression_enabled and has_active_tension:
            event_value = 0 if tension_state == compression_state else (1 if compression_state else -1)

        elif compression_enabled: # no tension active
            event_value = 1 if compression_state else -1

        elif has_active_tension: # compression disabled
            event_value = -1 if tension_state else 1

        else:
            event_value = 0

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)


def load_config(config):
    return MmuSensors(config)
