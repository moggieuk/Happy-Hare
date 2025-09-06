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
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
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
#
import logging, time

import configparser, configfile

INSERT_GCODE = "__MMU_SENSOR_INSERT"
REMOVE_GCODE = "__MMU_SENSOR_REMOVE"
RUNOUT_GCODE = "__MMU_SENSOR_RUNOUT"

# Enhanced "runout helper" that gives greater control of when filament sensor events are fired and
# direct access to button events in addition to creating a "remove" / "runout" distinction
class MmuRunoutHelper:
    def __init__(self, printer, name, event_delay, insert_gcode, remove_gcode, runout_gcode, insert_remove_in_print, button_handler, switch_pin):

        self.printer, self.name = printer, name
        self.insert_gcode, self.remove_gcode, self.runout_gcode = insert_gcode, remove_gcode, runout_gcode
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

        # Button handlers are used for sync feedback state switches
        if self.button_handler and not self.button_handler_suspended:
            self.button_handler(eventtime, is_filament_present, self)

        if is_filament_present == self.filament_present: return
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

        if is_filament_present and self.insert_gcode: # Insert detected
            if not is_printing or (is_printing and self.insert_remove_in_print):
                #logging.info("MMU: filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._insert_event_handler(eventtime))

        else: # Remove or Runout detected
            if is_printing and self.runout_suspended is False and self.runout_gcode:
                #logging.info("MMU: filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime))
            elif self.remove_gcode and (not is_printing or self.insert_remove_in_print):
                # Just a "remove" event
                #logging.info("MMU: filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._remove_event_handler(eventtime))

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

# EXPERIMENT/HACK to support ViViD analog buffer "endstops"
# This class implments both the filament switch sensor and endstop. However:
#  * it will not display in UI because no filament_switch_sensor exists in config
#  * does not involve the mcu in the homing process so it can't be accurate
#  * suffers from inherent averaging lag for analog inputs
class MmuAdcSwitchSensor:
    def __init__(self, config, name_prefix, gate, switch_pin, event_delay, a_range, insert=False, remove=False, runout=False, insert_remove_in_print=False, button_handler=None, a_pullup=4700.):
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
        self.runout_helper = MmuRunoutHelper(self.printer, name, event_delay, insert_gcode, remove_gcode, runout_gcode, insert_remove_in_print, button_handler, switch_pin)
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
        # (possible to be multiplexed on type-B designs)
        switch_pins = list(config.getlist('gate_switch_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with gate_switch_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_GATE, None, switch_pins, event_delay, runout=True)

        # Setup "mmu_gear" sensors...
        for gate in range(23):
            switch_pin = config.get('post_gear_switch_pin_%d' % gate, None)
            if switch_pin:
                # EXPERIMENT/HACK to support ViViD analog buffer "endstops"
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

        # Setup motor syncing feedback sensors...
        # (possible to be multiplexed on type-B designs)
        switch_pins = list(config.getlist('sync_feedback_tension_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with sync_feedback_tension_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_TENSION, None, switch_pins, 0, button_handler=self._sync_tension_callback)
        switch_pins = list(config.getlist('sync_feedback_compression_pin', []))
        if switch_pins:
            if len(switch_pins) not in [1, num_units]:
                raise config.error("Invalid number of pins specified with sync_feedback_compression_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_COMPRESSION, None, switch_pins, 0, button_handler=self._sync_compression_callback)

    def _create_mmu_sensor(self, config, name_prefix, gate, switch_pins, event_delay, insert=False, remove=False, runout=False, insert_remove_in_print=False, button_handler=None):
        switch_pins = [switch_pins] if not isinstance(switch_pins, list) else switch_pins
        for unit, switch_pin in enumerate(switch_pins):
            if not self._is_empty_pin(switch_pin):
                name = "%s_%d" % (name_prefix, gate) if gate is not None else "unit_%d_%s" % (unit, name_prefix) if len(switch_pins) > 1 else name_prefix # Must match mmu_sensor_manager
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
                ro_helper = MmuRunoutHelper(self.printer, sensor, event_delay, insert_gcode, remove_gcode, runout_gcode, insert_remove_in_print, button_handler, switch_pin)
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

    # Button event handlers for sync-feedback
    # Feedback state should be between -1 (tension) and 1 (compressed)
    def _sync_tension_callback(self, eventtime, tension_state, runout_helper):
        from .mmu import Mmu # For sensor names
        tension_enabled = runout_helper.sensor_enabled
        compression_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_COMPRESSION, None)
        has_active_compression = compression_sensor.runout_helper.sensor_enabled if compression_sensor else False
        compression_state = compression_sensor.runout_helper.filament_present if has_active_compression else False

        if tension_enabled:
            if has_active_compression:
                if tension_state == compression_state:
                    event_value = 0
                elif tension_state and not compression_state:
                    event_value = -1
                else:
                    event_value = 1
            else:
                if tension_state :
                    event_value = -1
                else:
                    event_value = 1
        else:
            if has_active_compression:
                if compression_state:
                    event_value = 1
                else:
                    event_value = -1
            else:
                event_value = 0

        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)

    def _sync_compression_callback(self, eventtime, compression_state, runout_helper):
        from .mmu import Mmu
        compression_enabled = runout_helper.sensor_enabled
        tension_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_TENSION, None)
        has_active_tension = tension_sensor.runout_helper.sensor_enabled if tension_sensor else False
        tension_state = tension_sensor.runout_helper.filament_present if has_active_tension else False

        if compression_enabled:
            if has_active_tension:
                if tension_state == compression_state:
                    event_value = 0
                elif compression_state and not tension_state:
                    event_value = 1
                else:
                    event_value = -1
            else:
                if compression_state:
                    event_value = 1
                else:
                    event_value = -1
        else:
            if has_active_tension:
                if tension_state:
                    event_value = -1
                else:
                    event_value = 1
            else:
                event_value = 0

        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)

def load_config(config):
    return MmuSensors(config)
