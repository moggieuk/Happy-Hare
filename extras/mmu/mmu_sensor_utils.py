# Happy Hare MMU Software
#
# Utility sensor logic to allow easy creation of MMU filament sensors on a per mmu_machine
# or per mmu_unit basis.
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

# Happy Hare imports
from .mmu_constants import *


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

        if prev_filament_present == is_filament_present:
            return

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
                #logging.info("MMU: filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._insert_event_handler(eventtime))

        else: # Remove or Runout detected
            if is_printing and self.runout_suspended is False and runout_gcode:
                #logging.info("MMU: filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime, "runout"))
            elif remove_gcode and (not is_printing or self.insert_remove_in_print):
                # Just a "remove" event
                #logging.info("MMU: filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime))
                self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
                self.reactor.register_callback(lambda reh: self._remove_event_handler(eventtime))


    def note_clog_tangle(self, event_type):
        #logging.info("MMU: filament sensor %s: %s event detected, Eventtime %.2f" % (self.name, event_type, eventtime))
        now = self.reactor.monotonic()
        self.min_event_systime = self.reactor.NEVER # Prevent more callbacks until this one is complete
        self.reactor.register_callback(lambda reh: self._runout_event_handler(now, event_type))


    def enable_runout(self, restore):
        self.runout_suspended = not restore


    def enable_button_feedback(self, restore):
        self.button_handler_suspended = not restore


    def get_status(self, eventtime=None):
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



# -------------------------------------------------------------------------------------------------
# Analog Filament Tension Sensor used for proportional sync-feedback
# Maps sensor range to [-1,1]
class MmuProportionalSensor:

    def __init__(self, config, name):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = name
        self._last_extreme = None

        # Config
        self._pin           = config.get('sync_feedback_analog_pin')
        max_tension         = config.getfloat('sync_feedback_analog_max_tension', 1)
        max_compression     = config.getfloat('sync_feedback_analog_max_compression', 0)

        # Determine the actual raw min/max sensor values
        raw_min = min(max_tension, max_compression)
        raw_max = max(max_tension, max_compression)
        mid_point = (max_tension + max_compression) / 2.0

        self._neutral_point = config.getfloat('sync_feedback_analog_neutral_point', mid_point, minval=raw_min, maxval=raw_max)

        self._gamma         = config.getfloat('sync_feedback_analog_gamma', 1)           # Not exposed
        self._sample_time   = config.getfloat('sync_feedback_analog_sample_time', 0.005) # Not exposed
        self._sample_count  = config.getint('sync_feedback_analog_sample_count', 5)      # Not exposed
        self._report_time   = config.getfloat('sync_feedback_analog_report_time', 0.100) # Not exposed

        self._reversed = (max_compression < max_tension)
        eps = 1e-12
        if not self._reversed:
            # Tension low, Compression high value
            self._d_neg = max(self._neutral_point - max_tension, eps)
            self._d_pos = max(max_compression - self._neutral_point, eps)
        else:
            # Compression low, Tension high value
            self._d_pos = max(self._neutral_point - max_compression, eps)
            self._d_neg = max(max_tension - self._neutral_point, eps)

        # State
        self.value_raw = 0.0 # Raw ADC value
        self.value = 0.0     # In [-1.0, 1.0]

        # Setup ADC
        ppins = self.printer.lookup_object('pins')
        self.adc = ppins.setup_pin('adc', self._pin)

        self.adc.setup_adc_callback(self._report_time, self._adc_callback)
        if hasattr(self.adc, "setup_minmax"):
            # Kalico and older klipper
            self.adc.setup_minmax(self._sample_time, self._sample_count)
            self.adc.setup_adc_callback(self._report_time, self._adc_callback)
        else:
            try:
                # New klipper (>= v0.13.0-557)
                self.adc.setup_adc_sample(self._report_time, self._sample_time, self._sample_count)
                self.adc.setup_adc_callback(self._adc_callback)
            except TypeError:
                # A few versions of klipper had these signatures
                self.adc.setup_adc_sample(self._sample_time, self._sample_count)
                self.adc.setup_adc_callback(self._report_time, self._adc_callback)

        # Attach runout_helper (no gcode actions; just enable/disable plumbing to remove UI nag)
        clog_gcode   = ("%s SENSOR=%s" % (CLOG_GCODE,   name))
        tangle_gcode = ("%s SENSOR=%s" % (TANGLE_GCODE, name))
        self.runout_helper = MmuRunoutHelper(
            self.printer,
            self.name,                  # Name exposed to QUERY_/SET_FILAMENT_SENSOR
            0,                          # Event_delay (not used here)
            {
                "clog":   clog_gcode,
                "tangle": tangle_gcode,
            },
            insert_remove_in_print=False,
            button_handler=None,       # No button handler for analog
            switch_pin=self._pin
        )

        # Expose status
        self.printer.add_object(self.name, self)

    def _map_reading(self, v_raw):
        n = self._neutral_point

        v = float(v_raw)
        # Map around neutral_point into [-1, 1]
        if not self._reversed:
            if v >= n:
                y = (v - n) / self._d_pos
            else:
                y = -(n - v) / self._d_neg
        else:
            if v <= n:
                y = (n - v) / self._d_pos
            else:
                y = -(v - n) / self._d_neg

        # Optional shaping (gamma=1 => linear)
        if self._gamma != 1.0:
            y = (abs(y) ** self._gamma) * (1.0 if y >= 0 else -1.0)

        # Clamp
        if y < -1.0: y = -1.0
        if y >  1.0: y =  1.0
        return y


    def _adc_callback(self, *args):
        # Old klipper: _adc_callback(read_time, read_value)
        # New klipper: _adc_callback(samples) where samples is a list of (read_time, read_value)
        if len(args) == 1:
            samples = args[0]
            read_time, read_value = samples[-1]
        elif len(args) == 2:
            read_time, read_value = args
        else:
            raise TypeError("_adc_callback expected (read_time, read_value) or (samples), got %d args" % len(args))

        self.value_raw = float(read_value)
        self.value = self._map_reading(read_value) # Mapped & scaled value

        # Publish sync-feedback event immediately if extreme to match switch sensors
        # TODO really extreme should be determined by is_extreme() in mmu_sync_feedback manager (with hysteresis), but object hasn't been created yet
        # TODO so for now, use absolute extremes
        if abs(self.value) >= 1.0:
            extreme = 1 if self.value > 0 else -1
            if extreme != self._last_extreme: # Avoid repeated events
                self._last_extreme = extreme
                self.printer.send_event("mmu:sync_feedback", read_time, self.value)


    def get_status(self, eventtime):
        return {
            "enabled":          bool(self.runout_helper.sensor_enabled),
            "value":            self.value,             # in [-1.0, 1.0] (mapped)
            "value_raw":        self.value_raw,         # raw
        }



# -------------------------------------------------------------------------------------------------
# EXPERIMENTAL/HACK
# Support ViViD analog buffer "endstops"
# This class implments both the filament switch sensor and endstop. However:
#  * it will not display in UI because no filament_switch_sensor exists in config
#  * does not involve the mcu in the homing process so it can't be accurate
#  * suffers from inherent averaging lag for analog inputs
class MmuAdcSwitchSensor:

    def __init__(self, config, name_prefix, gate, switch_pin, event_delay,
                 a_range,
                 insert=False, remove=False, runout=False, clog=False, tangle=False,
                 insert_remove_in_print=False, button_handler=None,
                 a_pullup=4700.):

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
            {
                "insert": insert_gcode,
                "remove": remove_gcode,
                "runout": runout_gcode,
                "clog":   clog_gcode,
                "tangle": tangle_gcode,
            },
            insert_remove_in_print,
            button_handler,
            switch_pin,
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



# -------------------------------------------------------------------------------------------------
# EXPERIMENTAL
# Standalone Hall Filament Sensor Endstop using Multi-Use Pins
# Can coexists with standard Klipper hall_filament_width_sensor by sharing the ADC pins
class MmuHallEndstop:
    def __init__(self, config, name, pin1, pin2, cal_dia1, raw_dia1, cal_dia2, raw_dia2,
                 hall_runout_dia=1.,
                 insert=False, remove=False, runout=False, clog=False, tangle=False):

        self.printer = config.get_printer()
        self.name = name

        # Configurable sampling for fast endstop response
        # Defaults: 1ms sample, 8 samples = 8ms. Report every 10ms.
        self.sample_time = config.getfloat('hall_sample_time', 0.001, above=0.0)
        self.sample_count = config.getint('hall_sample_count', 8, minval=1)
        self.report_time = config.getfloat('hall_report_time', 0.010, above=0.0)

        # Sensor configuration for diameter calculation
        self.pin1_name = pin1
        self.pin2_name = pin2
        self.dia1 = cal_dia1
        self.rawdia1 = raw_dia1
        self.dia2 = cal_dia2
        self.rawdia2 = raw_dia2
        self.hall_min_diameter = hall_runout_dia

        # State
        self.lastFilamentWidthReading = 0
        self.lastFilamentWidthReading2 = 0
        self.diameter = 0
        self.is_active = True # Always active for endstop purposes? or should be toggleable?

        # Endstop state variables
        self._steppers = []
        self._trigger_completion = None
        self._last_trigger_time = None
        self._homing = False
        self._triggered = False

        # Setup Hardware (Multi-Use)
        ppins = self.printer.lookup_object('pins')

        _kalico = hasattr(self.adc, "setup_minmax") # Kalico and older klipper
        # ADC 1
        if self.pin1_name:
            ppins.allow_multi_use_pin(self.pin1_name)
            self.mcu_adc = ppins.setup_pin('adc', self.pin1_name)
            if _kalico:
                self.mcu_adc.setup_minmax(self.sample_time, self.sample_count)
            else:
                self.mcu_adc.setup_adc_sample(self.sample_time, self.sample_count)
            self.mcu_adc.setup_adc_callback(self.report_time, self.adc_callback)

        # ADC 2 (Optional)
        self.mcu_adc2 = None
        if self.pin2_name:
            ppins.allow_multi_use_pin(self.pin2_name)
            self.mcu_adc2 = ppins.setup_pin('adc', self.pin2_name)
            if _kalico:
                self.mcu_adc2.setup_minmax(self.sample_time, self.sample_count)
            else:
                self.mcu_adc2.setup_adc_sample(self.sample_time, self.sample_count)
            self.mcu_adc2.setup_adc_callback(self.report_time, self.adc2_callback)

        # Setup runout helper/virtual sensor for MMU integration
        event_delay = 0.5
        insert_gcode = ("%s SENSOR=%s" % (INSERT_GCODE, name)) if insert else None
        remove_gcode = ("%s SENSOR=%s" % (REMOVE_GCODE, name)) if remove else None
        runout_gcode = ("%s SENSOR=%s" % (RUNOUT_GCODE, name)) if runout else None

        # We pass "None" for switch_pin because we manage the pin state via ADC logic
        self.runout_helper = MmuRunoutHelper(
            self.printer,
            name,
            event_delay,
            {
                "insert": insert_gcode,
                "remove": remove_gcode,
                "runout": runout_gcode
            },
            insert_remove_in_print=False,
            button_handler=None,
            switch_pin=None
        )

        self.printer.add_object("mmu_hall_endstop %s" % name, self)

    def _calc_diameter(self):
        # Duplicate of Klipper hall_filament_width_sensor logic
        try:
            val_sum = self.lastFilamentWidthReading + self.lastFilamentWidthReading2
            slope = (self.dia2 - self.dia1) / (self.rawdia2 - self.rawdia1)
            diameter_new = round(slope * (val_sum - self.rawdia1) + self.dia1, 2)
            # Use same smoothing factor as Klipper? Or faster for endstop?
            # Klipper: self.diameter = (5.0 * self.diameter + diameter_new) / 6
            # For endstop we probably want instant reaction or less smoothing
            self.diameter = (2.0 * self.diameter + diameter_new) / 3 # Slightly faster smoothing
        except ZeroDivisionError:
            self.diameter = 1.75 # Default fallback

    def adc_callback(self, read_time, read_value):
        self.lastFilamentWidthReading = round(read_value * 10000)
        self._calc_diameter()
        self._check_trigger(read_time)

    def adc2_callback(self, read_time, read_value):
        self.lastFilamentWidthReading2 = round(read_value * 10000)
        self._calc_diameter()
        self._check_trigger(read_time)

    def _check_trigger(self, eventtime):
        is_present = self.diameter > self.hall_min_diameter
        self.runout_helper.note_filament_present(eventtime, is_present)

        if self._homing:
            if is_present == self._triggered:
                if self._trigger_completion is not None:
                    self._last_trigger_time = eventtime
                    self._trigger_completion.complete(True)
                    self._trigger_completion = None

    def get_status(self, eventtime):
        status = self.runout_helper.get_status(eventtime)
        status.update({
            "Diameter": self.diameter,
            "Raw": (self.lastFilamentWidthReading + self.lastFilamentWidthReading2)
        })
        return status

    # Required to implement a HH MMU endstop -------

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



class MmuSensorFactory:

    def __init__(self, printer):
        self.printer = printer

    def create_mmu_sensor(
        self, config, name_prefix, gate, switch_pin, event_delay,
        insert=False, remove=False, runout=False, clog=False, tangle=False,
        insert_remove_in_print=False, button_handler=None,
    ):
        fs = None
        if not self._is_empty_pin(switch_pin):
            name = "%s_%d" % (name_prefix, gate) if gate is not None else name_prefix
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
                {
                    "insert": insert_gcode,
                    "remove": remove_gcode,
                    "runout": runout_gcode,
                    "clog":   clog_gcode,
                    "tangle": tangle_gcode,
                },
                insert_remove_in_print,
                button_handler,
                switch_pin
            )
            fs.runout_helper = ro_helper
            fs.get_status = ro_helper.get_status
        return fs


    def _is_empty_pin(self, switch_pin):
        if switch_pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(switch_pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''


    def _sync_tension_callback(self, eventtime, t_sensor_name, tension_state, runout_helper):
        """
        Button event handler for sync-feedback tension switch
        """
        c_sensor_name = t_sensor_name.replace(SENSOR_TENSION, SENSOR_COMPRESSION)
        compression_sensor = self.printer.lookup_object("filament_switch_sensor %s" % c_sensor_name, None)
        compression_enabled = compression_sensor.runout_helper.sensor_enabled if compression_sensor else False
        compression_state = compression_sensor.runout_helper.filament_present if compression_enabled else False

        if compression_enabled:
            event_value = 0 if tension_state == compression_state else (-1 if tension_state else 1) # {-1,0,1}
        else:
            event_value = -tension_state # {0,-1}

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)


    def _sync_compression_callback(self, eventtime, c_sensor_name, compression_state, runout_helper):
        """
        Button event handler for sync-feedback compression switch
        """
        t_sensor_name = c_sensor_name.replace(SENSOR_COMPRESSION, SENSOR_TENSION)
        tension_sensor = self.printer.lookup_object("filament_switch_sensor %s" % t_sensor_name, None)
        tension_enabled = tension_sensor.runout_helper.sensor_enabled if tension_sensor else False
        tension_state = tension_sensor.runout_helper.filament_present if tension_enabled else False

        if tension_enabled:
            event_value = 0 if compression_state == tension_state else (1 if compression_state else -1) # {-1,0,1}
        else:
            event_value = compression_state # {1,0}

        # Send event now so it is processed as early as possible
        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)
