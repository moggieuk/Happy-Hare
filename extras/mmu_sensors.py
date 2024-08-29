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
import logging, time, math

class MmuRunoutHelper:
    def __init__(self, printer, name, insert_gcode, runout_gcode, event_delay, pause_delay):
        self.printer, self.name = printer, name
        self.insert_gcode, self.runout_gcode = insert_gcode, runout_gcode
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.pause_delay = pause_delay # Time to wait after pause
        self.event_delay = event_delay # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_suspended = False

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # We are going to replace previous runout_helper mux commands with ours
        prev = self.gcode.mux_commands.get("QUERY_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_QUERY_FILAMENT_SENSOR

        prev = self.gcode.mux_commands.get("SET_FILAMENT_SENSOR")
        prev_key, prev_values = prev
        prev_values[self.name] = self.cmd_SET_FILAMENT_SENSOR

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2. # Time to wait before first events are processed

    def _insert_event_handler(self, eventtime):
        self._exec_gcode(self.insert_gcode)

    def _remove_event_handler(self, eventtime):
        self._exec_gcode(self.runout_gcode)

    def _runout_event_handler(self, eventtime):
        # Pausing from inside an event requires that the pause portion of pause_resume execute immediately.
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_pause_command()
        self.printer.get_reactor().pause(eventtime + self.pause_delay)
        self._exec_gcode(self.runout_gcode + " DO_RUNOUT=1")

    def _exec_gcode(self, command):
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
        # Let Happy Hare decide what processing is possible based on printing state
        if is_filament_present: # Insert detected
            self.min_event_systime = self.reactor.NEVER
            logging.info("MMU filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime))
            self.reactor.register_callback(self._insert_event_handler)
        else: # Runout detected
            self.min_event_systime = self.reactor.NEVER
            if self.runout_suspended: # Just a remove event
                logging.info("MMU filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(self._remove_event_handler)
            else: # True runout
                logging.info("MMU filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(self._runout_event_handler)

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

    ENDSTOP_PRE_GATE  = 'mmu_pre_gate'
    ENDSTOP_GATE      = 'mmu_gate'
    ENDSTOP_EXTRUDER  = 'extruder'
    ENDSTOP_TOOLHEAD  = 'toolhead'
    SWITCH_SYNC_FEEDBACK_TENSION     = 'sync_feedback_tension'
    SWITCH_SYNC_FEEDBACK_COMPRESSION = 'sync_feedback_compression'

    def __init__(self, config):
        self.printer = config.get_printer()

        event_delay = config.get('event_delay', 1.)
        pause_delay = config.get('pause_delay', 0.1)

        # Setup and pre-gate sensors that are defined...
        for gate in range(23):
            switch_pin = config.get('pre_gate_switch_pin_%d' % gate, None)

            if switch_pin is None or self._is_empty_pin(switch_pin):
                continue

            # Automatically create necessary filament_switch_sensors
            name = "%s_%d" % (self.ENDSTOP_PRE_GATE, gate)
            section = "filament_switch_sensor %s" % name
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = self.printer.load_object(config, section)

            # Replace with custom runout_helper because limited operation is possible during print
            insert_gcode = "__MMU_GATE_INSERT GATE=%d" % gate
            runout_gcode = "__MMU_GATE_RUNOUT GATE=%d" % gate
            gate_helper = MmuRunoutHelper(self.printer, name, insert_gcode, runout_gcode, event_delay, pause_delay)
            fs.runout_helper = gate_helper
            fs.get_status = gate_helper.get_status

        # Setup gate sensor...
        switch_pin = config.get('gate_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            # Automatically create necessary filament_switch_sensors
            name = "%s_sensor" % self.ENDSTOP_GATE
            section = "filament_switch_sensor %s" % name
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = self.printer.load_object(config, section)

            # Replace with custom runout_helper because limited operation is possible during print
            insert_gcode = "__MMU_GATE_INSERT"
            runout_gcode = "__MMU_GATE_RUNOUT"
            gate_helper = MmuRunoutHelper(self.printer, name, insert_gcode, runout_gcode, event_delay, pause_delay)
            fs.runout_helper = gate_helper
            fs.get_status = gate_helper.get_status

        # Setup extruder (entrance) sensor...
        switch_pin = config.get('extruder_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            # Automatically create necessary filament_switch_sensors
            section = "filament_switch_sensor %s_sensor" % self.ENDSTOP_EXTRUDER
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = self.printer.load_object(config, section)

        # Setup toolhead sensor...
        switch_pin = config.get('toolhead_switch_pin', None)
        if switch_pin is not None and not self._is_empty_pin(switch_pin):
            # Automatically create necessary filament_switch_sensors
            section = "filament_switch_sensor %s_sensor" % self.ENDSTOP_TOOLHEAD
            config.fileconfig.add_section(section)
            config.fileconfig.set(section, "switch_pin", switch_pin)
            config.fileconfig.set(section, "pause_on_runout", "False")
            fs = self.printer.load_object(config, section)

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
            self.SWITCH_SYNC_FEEDBACK_TENSION: self.tension_switch_state,
            self.SWITCH_SYNC_FEEDBACK_COMPRESSION: self.compression_switch_state,
        }

def load_config(config):
    return MmuSensors(config)






# filament sync tension handler, help to adjust rotation dist in real time automatically
# to search and use the perfect rotation_dist value.
# Theory of operation
# use binary search to get the perfect rotation dist value.
# the upper limit: can slowly trigger expanded sensor (not enough filament)
# the lower limit: can slowly trigger compressed sensor ( too much filament)
# the perfect rotation dist is always within the upper and lower limit.
# when in neutral, get a avg value from upper and lower set it as new rotation_dist, and test it,
# when it triggers another upper or lower limit, and use it as a new upper or lower limit value.
# As it goes, slowly and slowly we have smaller and smaller range from upper to lower value,
# a better and better , more and more precise rota dist value.

# function get_rotation_dist_on_state_change()
#   when non sensor is triggered, we are in neutral positioin:
#      time to test a avg value from current upper and lower rotation_dist values.
#   When it's compressed, we have a confirmed compressed value, update the lower limit using current rotation_dist
#   When it's expanded,  we have a confirmed expanded value, update the upper limit using current rotation_dist
#   this function doesn't change the rotation dist when it's compressed or expanded, only record the confirmed bad value for getting the avg
#
# update_sync_rotation_dist()
#  similar to _update_sync_multiplier
#   1
#   it will be called again and again frequently.
#   it's responsible to get the sensor out of the tension, by using a known upper/lower rotation_dist to go to the other direction
#   the initial adjustment is 50% more or 50% less of the initial rotation_dist.
#   as filament tension state changes, the upper and lower limit will change, we will have smaller range of upper lower limit.
#   the perfect rotation_dist must be in between upper and lower limit.
#   2
#   to add a safety measure, count the times (counter, not time/sec/min) the sensor remains in the same compressed or expanded state,
#   if with the known/confirmed other direction of the rotation_dist we still cannot get out of the current state within certain count (ALLOW_STAY_IN_TENSION_STATE_MAX)
#   We further increase or reduce the confirmed bad value to make sure we can move out of the tension state.

# in the beginning,
class FilaSyncTensionHandler:
    SYNC_NEUTRAL = 0
    SYNC_COMPRESSED = 1
    SYNC_EXPANDED = -1

    # MIN_ROTATION_DIST_DIFF = 0.02  # if upper and lower limit value are very close, no need to do more(avg).

    # larger the value, less safe to get out of tension bad state if something goes wrong
    # smaller the value, easier to get out of tension, but harder to find the perfect rotation_dist value, because the rota dist is adjusted more often
    # 20 or 30 or 40 should be good values, the better the rota dist value is , it takes longer to get out of the trigger state because
    # a good  rota dist doesn't change the tension state a lot
    ALLOW_STAY_IN_TENSION_STATE_MAX = 40
    # adjust the get out of tension rota dist even more (1%)
    # once we have counter reach above (40), we increase /decrease 0.01, when reach another 40, we increase /decrease another 0.01
    # so to make sure we can get out of the current tension state and go to neutral
    ADJUST_WHEN_STAY_TENSION_TOO_LONG = 0.01

    sync_auto_adjust_rotation_dist_enabled = False

    # the initial adjustment (50% more and 50% less) for upper and lower limit for rotation_dist 0.5 VS 1.5
    MAX_ADJ = 0.5

    def __init__(self):

        # self.init_for_next_print(rota_dist)
        self._log_debug = None
        self._log_always = None
        self._log_error = None

    def init_for_next_print(self, rota_dist:float):
        # the upper limit of the rotation_dist, larger rotation_dist, less filament, it's expanded
        self.bad_rotation_dist_expanded = None
        # the lower limit of the rotation_dist, smaller rotation_dist, more filament, it's compressed
        self.bad_rotation_dist_compressed = None
        self.rotation_dist = rota_dist
        self.delta = 0
        self.set_init_bad_values()
        self.stayed_in_tension_count = 0

    @staticmethod
    def is_auto_adjust_enabled() -> bool:
        return FilaSyncTensionHandler.sync_auto_adjust_rotation_dist_enabled

    @staticmethod
    def set_auto_adjust_feature_enable(enable:bool=True):
        FilaSyncTensionHandler.sync_auto_adjust_rotation_dist_enabled = enable

    def set_logger(self, log_debug, log_always, log_error ):
        # reuse the mmu log functions
        self._log_debug = log_debug
        self._log_always = log_always
        self._log_error = log_error


    #  a helper function easily output tension state to log
    @staticmethod
    def tension_state_to_text(state:float) ->str:
        if math.isclose(float(state), FilaSyncTensionHandler.SYNC_NEUTRAL):
            return "neutral"
        elif math.isclose(float(state), FilaSyncTensionHandler.SYNC_COMPRESSED):
            return "compressed"
        elif math.isclose(float(state), FilaSyncTensionHandler.SYNC_EXPANDED):
            return "expanded"
        else:
            return f"unknown ({state=})"

    @staticmethod
    def is_sync_tension_neutral(state:float) -> bool:
        return math.isclose(float(state), FilaSyncTensionHandler.SYNC_NEUTRAL)

    @staticmethod
    def is_sync_tension_compressed(state:float) -> bool:
        return math.isclose(float(state), FilaSyncTensionHandler.SYNC_COMPRESSED)

    @staticmethod
    def is_sync_tension_expanded(state:float) -> bool:
        return math.isclose(float(state), FilaSyncTensionHandler.SYNC_EXPANDED)


    # similar to _update_sync_multiplier, it deals with rotation_dist dcirectly instead of ratio and multiplier
    def update_sync_rotation_dist(self, state: float) -> float:

        if self.is_sync_tension_compressed(state):
            # too much filament, need go slower, larger rota dist
            # use the other side of rotat_dist value
            # use this known value to go to the other direction
            self.stayed_in_tension_count += 1
            if self.stayed_in_tension_count > FilaSyncTensionHandler.ALLOW_STAY_IN_TENSION_STATE_MAX:
                self._log_debug(f"we are in trouble get out of compressed state? {self.stayed_in_tension_count=}")
                # are we in trouble to get out of the bad compressed state??? adjust (make % larger) the known bad expanded value to get out of this state quickly
                # ie make it larger , 105% of the current value
                new_val = self.bad_rotation_dist_expanded * ( 1+ FilaSyncTensionHandler.ADJUST_WHEN_STAY_TENSION_TOO_LONG)
                # just to make sure the expand value can make more filament (so to ensure to reach the expanded state)
                self._log_debug(f"in trouble get out of compressed state? {self.stayed_in_tension_count=}(too long time), make known bad "
                f"expanded value larger {FilaSyncTensionHandler.ADJUST_WHEN_STAY_TENSION_TOO_LONG*100}%, now {new_val}(was {self.bad_rotation_dist_expanded})")
                self.bad_rotation_dist_expanded = new_val
                self.stayed_in_tension_count = 0
            if not math.isclose(self.rotation_dist, self.bad_rotation_dist_expanded):
                # reduce meaningless logs, only output when values are different
                self._log_debug(f"update_sync_rotation_dist() compressed {state=}({FilaSyncTensionHandler.tension_state_to_text(state)})"
                         f" to return bad expanded val(the other dir) {self.bad_rotation_dist_expanded} (to give less filament), tension counter={self.stayed_in_tension_count}")

            self.rotation_dist = self.bad_rotation_dist_expanded

        elif self.is_sync_tension_expanded(state):
            # too little filament, need go faster, smaller rota dist
            # use this known value to go to the other direction
            self.stayed_in_tension_count += 1
            if self.stayed_in_tension_count > FilaSyncTensionHandler.ALLOW_STAY_IN_TENSION_STATE_MAX:

                # are we in trouble to get out of the bad compressed state??? adjust (make % smaller) the known bad expanded value
                # to get out of this state quickly
                # ie make it smaller , 95% of the current value
                new_val = self.bad_rotation_dist_compressed * (1 - FilaSyncTensionHandler.ADJUST_WHEN_STAY_TENSION_TOO_LONG)
                # just to make sure the expand value can make more filament (so to ensure to reach the expanded state)
                self._log_debug(f"in trouble get out of expanded state? {self.stayed_in_tension_count=}(too long time), make known bad " 
                f"compressed value smaller {FilaSyncTensionHandler.ADJUST_WHEN_STAY_TENSION_TOO_LONG*100}%, now {new_val}(was {self.bad_rotation_dist_compressed})")
                self.stayed_in_tension_count = 0
                self.bad_rotation_dist_compressed = new_val
            if not math.isclose(self.rotation_dist, self.bad_rotation_dist_compressed):
                self._log_debug(f"update_sync_rotation_dist() expanded {state=}({FilaSyncTensionHandler.tension_state_to_text(state)})"
                   f" to return bad compressed(the other dir) val {self.bad_rotation_dist_compressed}(to give more filament), tension counter={self.stayed_in_tension_count}")

            self.rotation_dist = self.bad_rotation_dist_compressed


        elif self.is_sync_tension_neutral(state):
            self.stayed_in_tension_count = 0
            # do nothing don't change the current rotation dist
            pass

        return self.rotation_dist

    def set_init_bad_values(self) -> None:

        if not self.bad_rotation_dist_compressed:
            # this value will cause compressed
            self.bad_rotation_dist_compressed = self.rotation_dist * ( 1.0 - FilaSyncTensionHandler.MAX_ADJ )

        if not self.bad_rotation_dist_expanded:
            # this value will cause expanded
            self.bad_rotation_dist_expanded = self.rotation_dist * ( 1.0 + FilaSyncTensionHandler.MAX_ADJ )
        return

    def cal_rotation_dist_by_tension_value(self, compressed:float, expanded:float) -> float:

        rotation_dist = (self.bad_rotation_dist_compressed + self.bad_rotation_dist_expanded)/2.0
        self._log_always(
            f"using bad compressed {compressed} and bad expanded {expanded} to avg: new better rotation_dist:{rotation_dist}")
        return rotation_dist

    def get_rotation_dist_on_state_change(self, last_state: float, state: float) -> float:

        self._log_debug(f"get_rotation_dist_on_state_change() working {last_state=}({FilaSyncTensionHandler.tension_state_to_text(last_state)}) {state=}({FilaSyncTensionHandler.tension_state_to_text(state)})")
        # same tension state?? impossible?? this function won't be called.
        if math.isclose(last_state, state):
            # impossible, state didn't change,
            return self.rotation_dist

        if FilaSyncTensionHandler.is_sync_tension_compressed(state):
            # tension state just turn to compressed from neutral
            # only record the confirmed bad compressed value and not to adjust it in here, let the other function update_sync_rotation_dist() to adjust it

            # record the current bad compressed value, this current rota dist value is confirmed bad compressed because it just triggered the compressed sensor
            self.bad_rotation_dist_compressed = self.rotation_dist
            self._log_debug(f"tension state changed to compressed (from neutral), record current rota_dist as new bad compressed val:{self.rotation_dist}(current rota dist)")

        elif FilaSyncTensionHandler.is_sync_tension_expanded(state):
            # tension state just turn to expanded from neutral
            # record the current bad expanded value, this current rota dist value is confirmed bad expanded because it just triggered the expanded sensor
            self.bad_rotation_dist_expanded = self.rotation_dist
            self._log_debug(f"tension state changed to expanded (from neutral), record current rota_dist as new bad expanded val:{self.rotation_dist}(current rota dist)")


        # enhanced error handling, this shouldn't happen but just in case, if the two upper /lower limit values are messed up , fix them
        if self.bad_rotation_dist_compressed > self.bad_rotation_dist_expanded:
            self._log_error(f"impossible! why bad compressed rota_dist is larger than bad expanded rota ?? {self.bad_rotation_dist_compressed}>{self.bad_rotation_dist_expanded}, just swap them")
            temp = self.bad_rotation_dist_compressed
            self.bad_rotation_dist_compressed = self.bad_rotation_dist_expanded
            self.bad_rotation_dist_expanded = temp
            self._log_error(f"after swap, now compressed rota dist:{self.bad_rotation_dist_compressed} < expanded rota dist:{self.bad_rotation_dist_expanded}")

        if FilaSyncTensionHandler.is_sync_tension_neutral(state):
            # tension state just turn to neutral (because tension state is slowly moving from one end to other dir,
            # binary search, when it's neutral, test the middle point of the two side bad value.
            rotation_dist =  self.cal_rotation_dist_by_tension_value(self.bad_rotation_dist_compressed, self.bad_rotation_dist_expanded)

            self.rotation_dist = rotation_dist

        return self.rotation_dist


