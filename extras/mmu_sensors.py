# Happy Hare MMU Software
# Easy setup of all sensors for MMU
#
# Pre-gate sensors:
#   Simplifed filament switch sensor easy configuration of pre-gate sensors used to detect runout and insertion of filament
#   and preload into gate and update gate_map when possible to do so based on MMU state, not printer state
#   Essentially this uses the default `filament_switch_sensor` but then replaces the runout_helper
#   Each has name `mmu_pre_gate_X` where X is gate number
#
# mmu_gate sensor:
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks with modified runout event handling
#   Named `mmu_gate`
#
# extruder & toolhead sensor:
#   Wrapper around `filament_switch_sensor` disabling all functionality - just for visability
#   Named `extruder` & `toolhead`
#
# sync feedback sensor:
#   Creates simple button and publishes events based on state change
#
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# RunoutHelper based on:
# Generic Filament Sensor Module                 Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time


class MmuRunoutHelper:
    def __init__(self, printer, name, event_delay, insert_gcode, remove_gcode, runout_gcode, allow_in_print):

        self.printer, self.name = printer, name
        self.insert_gcode, self.remove_gcode, self.runout_gcode = insert_gcode, remove_gcode, runout_gcode
        self.insert_remove_in_print = allow_in_print
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.event_delay = event_delay # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_suspended = False

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # Replace previous runout_helper mux commands with ours
        prev = self.gcode.mux_commands.get("QUERY_FILAMENT_SENSOR")
        _, prev_values = prev
        prev_values[self.name] = self.cmd_QUERY_FILAMENT_SENSOR

        prev = self.gcode.mux_commands.get("SET_FILAMENT_SENSOR")
        _, prev_values = prev
        prev_values[self.name] = self.cmd_SET_FILAMENT_SENSOR

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2. # Time to wait before first events are processed

    def _insert_event_handler(self, eventtime):
        self._exec_gcode("%s EVENTTIME=%s" % (self.insert_gcode, eventtime))

    def _remove_event_handler(self, eventtime):
        self._exec_gcode("%s EVENTTIME=%s" % (self.remove_gcode, eventtime))

    def _runout_event_handler(self, eventtime):
        # Pausing from inside an event requires that the pause portion of pause_resume execute immediately.
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_pause_command()
        self._exec_gcode("%s EVENTTIME=%s" % (self.runout_gcode, eventtime))

    def _exec_gcode(self, command):
        if command:
            try:
                self.gcode.run_script(command)
            except Exception:
                logging.exception("Error running mmu sensor handler: `%s`" % command)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present: return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()

        # Don't handle too early or if disabled
        if eventtime < self.min_event_systime or not self.sensor_enabled: return
        self._process_state_change(eventtime, is_filament_present)

    def _process_state_change(self, eventtime, is_filament_present):
        # Determine "printing" status
        is_printing = self.printer.lookup_object("idle_timeout").get_status(eventtime)["state"] == "Printing"

        if is_filament_present and self.insert_gcode: # Insert detected
            if not is_printing or (is_printing and self.insert_remove_in_print):
                self.min_event_systime = self.reactor.NEVER
                #logging.info("MMU filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(lambda reh: self._insert_event_handler(eventtime))

        else: # Remove or Runout detected
            if is_printing and not self.runout_suspended and self.runout_gcode:
                self.min_event_systime = self.reactor.NEVER
                #logging.info("MMU filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime))
            elif self.remove_gcode and (not is_printing or self.insert_remove_in_print):
                # Just a "remove" event
                self.min_event_systime = self.reactor.NEVER
                #logging.info("MMU filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(lambda reh: self._remove_event_handler(eventtime))

    def enable_runout(self, restore):
        self.runout_suspended = not restore

    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
            "runout_suspended": bool(self.runout_suspended),
        }

    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"
    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Pre-gate MMU Sensor %s: filament detected" % (self.name)
        else:
            msg = "Pre-gate MMU Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)

    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"
    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = bool(gcmd.get_int("ENABLE", 1))


class MmuSensors:

    def __init__(self, config):
        from extras.mmu import Mmu # For sensor names

        self.INSERT_GCODE = "__MMU_SENSOR_INSERT"
        self.REMOVE_GCODE = "__MMU_SENSOR_REMOVE"
        self.RUNOUT_GCODE = "__MMU_SENSOR_RUNOUT"

        self.printer = config.get_printer()
        event_delay = config.get('event_delay', 0.5)

        # Setup "mmu_pre_gate" sensors...
        for gate in range(23):
            switch_pin = config.get('pre_gate_switch_pin_%d' % gate, None)
            if switch_pin is not None and not self._is_empty_pin(switch_pin):
                self._create_mmu_sensor(config, Mmu.PRE_GATE_SENSOR_PREFIX, gate, switch_pin, event_delay, insert=True, remove=True, runout=True, allow_in_print=True)

        # Setup "mmu_gate" sensor...
        switch_pin = config.get('gate_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            self._create_mmu_sensor(config, Mmu.ENDSTOP_GATE, None, switch_pin, event_delay, runout=True)

        # Setup "mmu_post_gate" sensors...
        for gate in range(23):
            switch_pin = config.get('post_gate_switch_pin_%d' % gate, None)
            if switch_pin is not None and not self._is_empty_pin(switch_pin):
                self._create_mmu_sensor(config, Mmu.ENDSTOP_POST_GATE_PREFIX, gate, switch_pin, event_delay, runout=True)

        # Setup extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            self._create_mmu_sensor(config, Mmu.ENDSTOP_EXTRUDER_ENTRY, None, switch_pin, event_delay, insert=True, runout=True)

        # Setup toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            self._create_mmu_sensor(config, Mmu.ENDSTOP_TOOLHEAD, None, switch_pin, event_delay)

        # Setup motor syncing feedback buttons...
        self.has_tension_switch = self.has_compression_switch = False
        self.tension_switch_state = self.compression_switch_state = -1

        switch_pin = config.get('sync_feedback_tension_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            buttons = self.printer.load_object(config, "buttons")
            buttons.register_buttons([switch_pin], self._sync_tension_callback)
            self.has_tension_switch = True
            self.tension_switch_state = 0

        switch_pin = config.get('sync_feedback_compression_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            buttons = self.printer.load_object(config, "buttons")
            buttons.register_buttons([switch_pin], self._sync_compression_callback)
            self.has_compression_switch = True
            self.compression_switch_state = 0

    def _create_mmu_sensor(self, config, name_prefix, gate, switch_pin, event_delay, insert=False, remove=False, runout=False, allow_in_print=False):
        name = "%s_%d" % (name_prefix, gate) if gate is not None else "%s" % name_prefix
        sensor = name if gate is not None else "%s_sensor" % name
        section = "filament_switch_sensor %s" % sensor
        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", "False")
        fs = self.printer.load_object(config, section)

        # Replace with custom runout_helper because limited operation at all times
        insert_gcode = ("%s SENSOR=%s%s" % (self.INSERT_GCODE, name, (" GATE=%d" % gate) if gate else "")) if insert else None
        remove_gcode = ("%s SENSOR=%s%s" % (self.REMOVE_GCODE, name, (" GATE=%d" % gate) if gate else "")) if remove else None
        runout_gcode = ("%s SENSOR=%s%s" % (self.RUNOUT_GCODE, name, (" GATE=%d" % gate) if gate else "")) if runout else None
        gate_helper = MmuRunoutHelper(self.printer, sensor, event_delay, insert_gcode, remove_gcode, runout_gcode, allow_in_print)
        fs.runout_helper = gate_helper
        fs.get_status = gate_helper.get_status

    def _is_empty_pin(self, switch_pin):
        if switch_pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(switch_pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''

    # Feedback state should be between -1 (expanded) and 1 (compressed)
    def _sync_tension_callback(self, eventtime, state):
        self.tension_switch_state = state
        if not self.has_compression_switch:
            self.printer.send_event("mmu:sync_feedback", eventtime, -(state * 2 - 1)) # -1 or 1
        else:
            self.printer.send_event("mmu:sync_feedback", eventtime, -state) # -1 or 0 (neutral)

    def _sync_compression_callback(self, eventtime, state):
        self.compression_switch_state = state
        if not self.has_tension_switch:
            self.printer.send_event("mmu:sync_feedback", eventtime, state * 2 - 1) # 1 or -1
        else:
            self.printer.send_event("mmu:sync_feedback", eventtime, state) # 1 or 0 (neutral)

    def get_status(self, eventtime):
        return {
            "sync_feedback_tension": self.tension_switch_state,
            "sync_feedback_compression": self.compression_switch_state,
        }

def load_config(config):
    return MmuSensors(config)
