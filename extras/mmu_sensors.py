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
#   Wrapper around `filament_switch_sensor` setting up insert/runout callbacks.
#   Named `mmu_gate`
#
# extruder & toolhead sensor:
#   Wrapper around `filament_switch_sensor` disabling all functionality - just for visability
#   Named `extruder` & `toolhead`
# 
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Based on:
# Generic Filament Sensor Module                 Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import itertools, logging, time

class PreGateRunoutHelper:

    def __init__(self, printer, name, gate):
        self.printer, self.name, self.gate = printer, name, gate
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.event_delay = 1. # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # We are going to replace previous runout_helper mux commands with ours
        prev = self.gcode.mux_commands.get("QUERY_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_QUERY_FILAMENT_SENSOR

        prev = self.gcode.mux_commands.get("SET_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_SET_FILAMENT_SENSOR

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2. # Time to wait until events are processed

    def _insert_event_handler(self, eventtime):
        self._exec_gcode("__MMU_PRE_GATE_INSERT GATE=%d" % self.gate)

    def _runout_event_handler(self, eventtime):
        self._exec_gcode("__MMU_PRE_GATE_RUNOUT GATE=%d" % self.gate)

    def _exec_gcode(self, command):
        try:
            self.gcode.run_script(command)
            #self.gcode.run_script("__MMU_M400\n" + command)
        except Exception:
            logging.exception("Error running pre-gate handler: `%s`" % command)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present: return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()

        # Don't handle too early or if disabled
        if eventtime < self.min_event_systime or not self.sensor_enabled: return

        # Let Happy Hare decide what processing is possible based on current state
        if is_filament_present: # Insert detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("MMU Pre-gate filament sensor %s: insert event detected, Time %.2f" % (self.name, eventtime))
            self.reactor.register_callback(self._insert_event_handler)
        else: # Runout detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("MMU Pre-gate filament sensor %s: runout event detected, Time %.2f" % (self.name, eventtime))
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
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
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)

class MmuSensors:

    ENDSTOP_PRE_GATE  = "mmu_pre_gate"
    ENDSTOP_GATE      = "mmu_gate"
    ENDSTOP_EXTRUDER  = "extruder"
    ENDSTOP_TOOLHEAD  = "toolhead"

    def __init__(self, config):
        printer = config.get_printer()

        # Setup and pre-gate sensors that are defined...
        for gate in itertools.count(0):
            switch_pin = config.get('pre_gate_switch_pin_%d' % gate, None)

            if switch_pin is None:
                break

            # Automatically create necessary filament_switch_sensors
            name = "%s_%d" % (self.ENDSTOP_PRE_GATE, gate)
            section = "filament_switch_sensor %s" % name
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            config.fileconfig.set(section, "insert_gcode", "__MMU_PRE_GATE_INSERT GATE=%d" % gate)
            config.fileconfig.set(section, "runout_gcode", "__MMU_PRE_GATE_RUNOUT GATE=%d" % gate)
            fs = printer.load_object(config, section)

            # Replace with custom runout_helper because limited operation is possible during print
            pre_gate_helper = PreGateRunoutHelper(printer, name, gate)
            fs.runout_helper = pre_gate_helper
            fs.get_status = pre_gate_helper.get_status

        # Setup gate sensor...
        switch_pin = config.get('gate_switch_pin', None)
        if switch_pin:
            # Automatically create necessary filament_switch_sensors
            section = "filament_switch_sensor %s_sensor" % self.ENDSTOP_GATE
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            config.fileconfig.set(section, "insert_gcode", "__MMU_GATE_INSERT")
            config.fileconfig.set(section, "runout_gcode", "__MMU_GATE_RUNOUT")
            fs = printer.load_object(config, section)

        # Setup extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        if switch_pin:
            # Automatically create necessary filament_switch_sensors
            section = "filament_switch_sensor %s_sensor" % self.ENDSTOP_EXTRUDER
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = printer.load_object(config, section)

        # Setup toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        if switch_pin:
            # Automatically create necessary filament_switch_sensors
            section = "filament_switch_sensor %s_sensor" % self.ENDSTOP_TOOLHEAD
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = printer.load_object(config, section)

def load_config(config):
    return MmuSensors(config)

