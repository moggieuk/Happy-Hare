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
#   Named `mmu_gear`
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
import logging, time, math

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
                self.min_event_systime = self.reactor.NEVER
                #logging.info("MMU: filament sensor %s: insert event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(lambda reh: self._insert_event_handler(eventtime))

        else: # Remove or Runout detected
            self.min_event_systime = self.reactor.NEVER
            if is_printing and self.runout_suspended is False and self.runout_gcode:
                #logging.info("MMU: filament sensor %s: runout event detected, Eventtime %.2f" % (self.name, eventtime))
                self.reactor.register_callback(lambda reh: self._runout_event_handler(eventtime))
            elif self.remove_gcode and (not is_printing or self.insert_remove_in_print):
                # Just a "remove" event
                #logging.info("MMU: filament sensor %s: remove event detected, Eventtime %.2f" % (self.name, eventtime))
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


class MmuSensors:

    def __init__(self, config):
        from .mmu import Mmu # For sensor names

        self.INSERT_GCODE = "__MMU_SENSOR_INSERT"
        self.REMOVE_GCODE = "__MMU_SENSOR_REMOVE"
        self.RUNOUT_GCODE = "__MMU_SENSOR_RUNOUT"

        self.printer = config.get_printer()
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
            if len(switch_pins) not in [ 1, num_units]:
                raise config.error("Invalid number of pins specified with gate_switch_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_GATE, None, switch_pins, event_delay, runout=True)

        # Setup "mmu_gear" sensors...
        for gate in range(23):
            switch_pin = config.get('post_gear_switch_pin_%d' % gate, None)
            if switch_pin:
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
            if len(switch_pins) not in [ 1, num_units]:
                raise config.error("Invalid number of pins specified with sync_feedback_tension_pin. Expected 1 or %d but counted %d" % (num_units, len(switch_pins)))
            self._create_mmu_sensor(config, Mmu.SENSOR_TENSION, None, switch_pins, 0, button_handler=self._sync_tension_callback)
        switch_pins = list(config.getlist('sync_feedback_compression_pin', []))
        if switch_pins:
            if len(switch_pins) not in [ 1, num_units]:
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
                insert_gcode = ("%s SENSOR=%s%s" % (self.INSERT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if insert else None
                remove_gcode = ("%s SENSOR=%s%s" % (self.REMOVE_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if remove else None
                runout_gcode = ("%s SENSOR=%s%s" % (self.RUNOUT_GCODE, name, (" GATE=%d" % gate) if gate is not None else "")) if runout else None
                ro_helper = MmuRunoutHelper(self.printer, sensor, event_delay, insert_gcode, remove_gcode, runout_gcode, insert_remove_in_print, button_handler, switch_pin)
                fs.runout_helper = ro_helper
                fs.get_status = ro_helper.get_status

    def _is_empty_pin(self, switch_pin):
        if switch_pin == '': return True
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(switch_pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return real_pin == ''

    # Button event handlers for sync-feedback
    # Feedback state should be between -1 (expanded) and 1 (compressed)
    def _sync_tension_callback(self, eventtime, tension_state, runout_helper):
        from .mmu import Mmu # For sensor names
        tension_enabled = runout_helper.sensor_enabled
        compression_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_COMPRESSION, None)
        has_active_compression = compression_sensor.runout_helper.sensor_enabled if compression_sensor else False
        compression_state = compression_sensor.runout_helper.filament_present if has_active_compression else False

        if tension_enabled:
            if has_active_compression and compression_state:
                if tension_state and compression_state:
                    logging.info("Malfunction of sync-feedback unit: both tension and compression sensors are triggered at the same time!")
                    event_value = 0
                elif tension_state and not compression_state:
                    event_value = -1
                elif not tension_state and compression_state:
                    event_value = 1
            else:
                event_value = -tension_state # -1 or 0 (neutral)
        else:
            event_value = 0 # Neutral

        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)

    def _sync_compression_callback(self, eventtime, compression_state, runout_helper):
        from .mmu import Mmu
        compression_enabled = runout_helper.sensor_enabled
        tension_sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % Mmu.SENSOR_TENSION, None)
        has_active_tension = tension_sensor.runout_helper.sensor_enabled if tension_sensor else False
        tension_state = tension_sensor.runout_helper.filament_present if has_active_tension else False

        if compression_enabled:
            if has_active_tension and tension_state:
                if compression_state and tension_state:
                    logging.info("Malfunction of sync-feedback unit: both tension and compression sensors are triggered at the same time!")
                    event_value = 0
                elif compression_state and not tension_state:
                    event_value = 1
                elif not compression_state and tension_state:
                    event_value = -1
            else:
                event_value = compression_state # 1 or 0 (neutral)
        else:
            event_value = 0 # Neutral

        self.printer.send_event("mmu:sync_feedback", eventtime, event_value)

def load_config(config):
    return MmuSensors(config)







# filament sync tension handler, help to adjust rotation dist in real time automatically
# to search and use the perfect rotation_dist value.
# Theory of operation
# use binary search to get the perfect rotation dist value.
# the upper limit: can slowly trigger expanded sensor (not enough filament)
# the lower limit: can slowly trigger compressed sensor ( too much filament)
# the perfect rotation dist is always within the upper and lower limit.
# when in neutral, get an avg value from upper and lower set it as new rotation_dist, and test it,
# when it triggers another upper or lower limit, and use it as a new upper or lower limit value.
# As it goes, slowly and slowly we have smaller and smaller range from upper to lower value,
# and can have a better and better, more and more precise rota dist value.

# function get_rotation_dist_on_state_change()
#   when non sensor is triggered, we are in neutral position:
#      time to test a avg value from current upper and lower rotation_dist values.
#   When it's compressed, we have a confirmed compressed value, update the lower limit using current rotation_dist
#   When it's expanded,  we have a confirmed expanded value, update the upper limit using current rotation_dist


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
#   We further increase or reduce the confirmed upper/lower limit bad values to make sure we can move out of the tension state.

class SyncTensionSensorAdj:
    SYNC_STATE_NEUTRAL = 0
    SYNC_STATE_COMPRESSED = 1
    SYNC_STATE_EXPANDED = -1
    # output even more debug info
    DEBUG = False

    # larger the value, need a bit more time to get out of sync bad state.
    # smaller the value, easier to get out of sync bad state, but it adjust the good rota dist too often even it's good.
    # 40 or 50 or 60 should be good values, the better the rota dist value is , it takes longer to get out of the trigger state because
    # a good  rota dist doesn't change the sync tension state a lot
    ALLOW_STAY_IN_TENSION_STATE_MAX = 50
    # adjust the get out of tension rota dist even more (1%)
    # once we have counter reach above (40), we increase /decrease 0.01, when reach another 40, we increase /decrease another 0.01
    # so to make sure we can get out of the current tension state and go to neutral
    ADJUST_WHEN_STAY_TENSION_TOO_LONG = 0.01
    ADJUST_WHEN_ALREADY_PERFECT = 0.005


    # we can use 80% and 120% of rotation_dist as initial upper and lower limit, it's not important. just an initial value
    INITIAL_ADJ = 0.20
    DEFAULT_ROTA_DIST = 22.3 # not important at all, just have some value to start with.
    def __init__(self):

        # self.init_for_next_print(rota_dist) # cannot get rota_dist when create this object, not loaded yet
        self.log_debug = None
        self.log_always = None
        self.log_error = None
        self.lower_rotation_dist_compressed = 0
        self.upper_rotation_dist_expanded = 0
        self.rotation_dist = SyncTensionSensorAdj.DEFAULT_ROTA_DIST	 # doesn't really matter
        self.sync_state = SyncTensionSensorAdj.SYNC_STATE_NEUTRAL
    def init_for_next_print(self, state, rota_dist=DEFAULT_ROTA_DIST):
        self.rotation_dist = rota_dist
        self.set_lower_upper_limit_values(rota_dist)
        self.stayed_in_tension_count = 0
        self.sync_state = state

    def set_lower_upper_limit_values(self, rd) -> None:
        # this value will cause compressed
        self.lower_rotation_dist_compressed = rd * (1.0 - SyncTensionSensorAdj.INITIAL_ADJ)
        self.log_debug(f"using {self.lower_rotation_dist_compressed:.5f} as initial lower_rotation_dist_compressed value ({SyncTensionSensorAdj.INITIAL_ADJ*100}% smaller than {rd})")

        # this value will cause expanded
        self.upper_rotation_dist_expanded = self.rotation_dist * (1.0 + SyncTensionSensorAdj.INITIAL_ADJ)
        self.log_debug(f"using {self.upper_rotation_dist_expanded:.5f} as initial upper_rotation_dist_expanded value ({SyncTensionSensorAdj.INITIAL_ADJ*100}% larger than {rd})")
        return

    def set_logger(self, log_debug, log_always, log_error ):
        # reuse the mmu log functions
        self.log_debug = log_debug
        self.log_always = log_always
        self.log_error = log_error


    #  a helper function easily output tension state to log
    @staticmethod
    def tension_state_to_text(state:float) ->str:
        if math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_NEUTRAL):
            return "neutral"
        elif math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_COMPRESSED):
            return "compressed"
        elif math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_EXPANDED):
            return "expanded"
        else:
            return f"unknown ({state=})"

    @staticmethod
    def is_sync_tension_neutral(state:float) -> bool:
        return math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_NEUTRAL)

    @staticmethod
    def is_sync_tension_compressed(state:float) -> bool:
        return math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_COMPRESSED)

    @staticmethod
    def is_sync_tension_expanded(state:float) -> bool:
        return math.isclose(float(state), SyncTensionSensorAdj.SYNC_STATE_EXPANDED)


    # similar to _update_sync_multiplier, it deals with rotation_dist dcirectly instead of ratio and multiplier
    def update_sync_rotation_dist(self, state: float):

        new_rota_dist = 0

        # did it change to a new state ?
        if self.sync_state != state:
            new_rota_dist = self.get_rotation_dist_on_state_change(last_state=self.sync_state, state=state)
            # update the state , prepare for next state change
            self.sync_state = state
            return new_rota_dist

        # same state
        if self.is_sync_tension_compressed(state):
            # too much filament, need go slower, larger rota dist
            # use the other side of rotat_dist value
            # use this known value to go to the other direction

            self.stayed_in_tension_count += 1
            if self.stayed_in_tension_count > SyncTensionSensorAdj.ALLOW_STAY_IN_TENSION_STATE_MAX:
                self.log_debug(f"we are in trouble get out of compressed state? {self.stayed_in_tension_count=}")
                # are we in trouble to get out of the bad compressed state??? adjust (make % larger) the known bad expanded value to get out of this state quickly
                # ie make it larger , 105% of the current value
                new_val = self.upper_rotation_dist_expanded * (1 + SyncTensionSensorAdj.ADJUST_WHEN_STAY_TENSION_TOO_LONG)
                # just to make sure the expand value can make more filament (so to ensure to reach the expanded state)
                self.log_debug(f"stayed too long in compressed state! {self.stayed_in_tension_count=}(too long time), make known bad "
                f"expanded value larger {SyncTensionSensorAdj.ADJUST_WHEN_STAY_TENSION_TOO_LONG * 100}%, now {new_val:.5f}(was {self.upper_rotation_dist_expanded:.5f})")
                self.upper_rotation_dist_expanded = new_val
                self.stayed_in_tension_count = 0

            new_rota_dist = self.upper_rotation_dist_expanded

        elif self.is_sync_tension_expanded(state):
            # too little filament, need go faster, smaller rota dist
            # use this known value to go to the other direction

            self.stayed_in_tension_count += 1
            if self.stayed_in_tension_count > SyncTensionSensorAdj.ALLOW_STAY_IN_TENSION_STATE_MAX:

                # are we in trouble to get out of the bad compressed state??? adjust (make % smaller) the known bad expanded value
                # to get out of this state quickly
                # ie make it smaller , 95% of the current value
                new_val = self.lower_rotation_dist_compressed * (1 - SyncTensionSensorAdj.ADJUST_WHEN_STAY_TENSION_TOO_LONG)
                # just to make sure the expand value can make more filament (so to ensure to reach the expanded state)
                self.log_debug(f"stayed too long in expanded state? {self.stayed_in_tension_count=}(too long time), make known bad " 
                f"compressed value smaller {SyncTensionSensorAdj.ADJUST_WHEN_STAY_TENSION_TOO_LONG * 100:.5f}%, now {new_val:.5f}(was {self.lower_rotation_dist_compressed:.5f})")
                self.stayed_in_tension_count = 0
                self.lower_rotation_dist_compressed = new_val

            new_rota_dist = self.lower_rotation_dist_compressed

        elif self.is_sync_tension_neutral(state):
            self.stayed_in_tension_count = 0
            # do nothing don't change the current rotation dist, the current rota_dist should be correct since we are in neutral
            # until it hits the expanded or compressed state
            return None

        #if SyncTensionSensorAdj.DEBUG:
        #    self.log_debug(f"update_sync_rotation_dist have  {new_rota_dist=}")

        if math.isclose(new_rota_dist, self.rotation_dist) and not math.isclose(self.rotation_dist,0):
            # in case self.rota_dist is not inited properly
            # return None so it won't update again and again using the same value, wasting time and log
            return None
        else:
            # we have a new different value, return it to let it set to stepper motor
            self.rotation_dist = new_rota_dist
            self.log_debug(f"sync state:{self.tension_state_to_text(state)}, new rota_dist:{new_rota_dist:.5f}")

            return new_rota_dist

        return None


    def cal_rotation_dist_by_tension_value(self, compressed:float, expanded:float) -> float:
        rotation_dist = (self.lower_rotation_dist_compressed + self.upper_rotation_dist_expanded)/2.0
        self.log_always(
            f"using last compressed {compressed:.5f} and last expanded {expanded:.5f} to avg: have new better rotation_dist:{rotation_dist:.5f}")
        return rotation_dist

    def get_rotation_dist_on_state_change(self, last_state: float, state: float) -> float:
        if SyncTensionSensorAdj.DEBUG:
            self.log_debug(f"get_rotation_dist_on_state_change() working {last_state=}({SyncTensionSensorAdj.tension_state_to_text(last_state)}) {state=}({SyncTensionSensorAdj.tension_state_to_text(state)})")
        # same tension state?? impossible?? this function won't be called.
        if math.isclose(last_state, state):
            # impossible, state didn't change,
            return self.rotation_dist

        if SyncTensionSensorAdj.is_sync_tension_compressed(state):
            # tension state just turn to compressed from neutral
            # only record the confirmed bad compressed value and not to adjust it in here, let the other function update_sync_rotation_dist() to adjust it

            # record the current bad compressed value, this current rota dist value is confirmed bad compressed because it just triggered the compressed sensor
            self.lower_rotation_dist_compressed = self.rotation_dist
            self.log_debug(f"tension state changed to compressed(from neutral), record current rota_dist as new bad compressed val:{self.rotation_dist:.5f}(current rota dist) {self.lower_rotation_dist_compressed} {self.upper_rotation_dist_expanded=}")

            if math.isclose(self.rotation_dist, self.upper_rotation_dist_expanded):
                adjusted_bad_rotation_dist_expanded = self.upper_rotation_dist_expanded * ( 1.0 + SyncTensionSensorAdj.ADJUST_WHEN_ALREADY_PERFECT)
                self.log_debug(f"already have the perfect rota dist, adjust a little bit to make to move to expanded direction {self.upper_rotation_dist_expanded:.5f}->{adjusted_bad_rotation_dist_expanded:.5f}")
                self.upper_rotation_dist_expanded = adjusted_bad_rotation_dist_expanded
            # immediately return the bad value of other dir
            self.rotation_dist = self.upper_rotation_dist_expanded
            self.log_debug(f"for compressed state, return new rota_dist {self.rotation_dist:5f}")
            return self.rotation_dist

        elif SyncTensionSensorAdj.is_sync_tension_expanded(state):
            # tension state just turn to expanded from neutral
            # record the current bad expanded value, this current rota dist value is confirmed bad expanded because it just triggered the expanded sensor
            self.upper_rotation_dist_expanded = self.rotation_dist
            if SyncTensionSensorAdj.DEBUG:
               self.log_debug(f"tension state changed to expanded(from neutral), record current rota_dist as new bad expanded val:{self.rotation_dist:.5f}(current rota dist), {self.lower_rotation_dist_compressed=}, {self.upper_rotation_dist_expanded=}")

            if math.isclose(self.rotation_dist, self.lower_rotation_dist_compressed):
                adjusted_bad_rotation_dist_compressed = self.lower_rotation_dist_compressed * ( 1.0 - SyncTensionSensorAdj.ADJUST_WHEN_ALREADY_PERFECT)
                self.log_debug(f"already have the perfect rota dist, adjust a little bit to make to move to compressed direction {self.lower_rotation_dist_compressed:.5f}->{adjusted_bad_rotation_dist_compressed:.5f}")
                self.lower_rotation_dist_compressed = adjusted_bad_rotation_dist_compressed

            self.rotation_dist = self.lower_rotation_dist_compressed
            self.log_debug(f"for expanded state, return new rota_dist {self.rotation_dist:5f}")
            return self.rotation_dist

        # enhanced error handling, this shouldn't happen but just in case, if the two upper /lower limit values are messed up , fix them
        if self.lower_rotation_dist_compressed > self.upper_rotation_dist_expanded:
            self.log_error(f"impossible! why lower compressed rota_dist is larger than upper expanded rota ?? {self.lower_rotation_dist_compressed}>{self.upper_rotation_dist_expanded}, just swap them")
            temp = self.lower_rotation_dist_compressed
            self.lower_rotation_dist_compressed = self.upper_rotation_dist_expanded
            self.upper_rotation_dist_expanded = temp
            self.log_error(f"after swap, now compressed rota dist:{self.lower_rotation_dist_compressed} < expanded rota dist:{self.upper_rotation_dist_expanded}")

        if SyncTensionSensorAdj.is_sync_tension_neutral(state):
            # tension state just turn to neutral (because tension state is slowly moving from one end to other dir,
            # binary search, when it's neutral, test the middle point of the two sides limt values.

            self.log_debug(f"tension state changed to neutral (from {SyncTensionSensorAdj.tension_state_to_text(last_state)})")
            #if math.isclose(self.lower_rotation_dist_compressed, self.upper_rotation_dist_expanded):
            #    pass
            #else:
            rotation_dist =  self.cal_rotation_dist_by_tension_value(self.lower_rotation_dist_compressed, self.upper_rotation_dist_expanded)
            if math.isclose(rotation_dist, self.rotation_dist):
                # the new avg rota dist is just the same as the current rota dist. meaning we already have the "perfect" rota dist
                self.log_always(f"This rotation dist seems very accurate {rotation_dist:.5f}...if haven't, consider update mmu_gear_rotation_distances[] using this value for the selected gate.")

            self.rotation_dist = rotation_dist

        return self.rotation_dist


