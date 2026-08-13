# Happy Hare MMU Software
#
# Utility sensor logic to allow easy creation of MMU filament sensors on a per mmu_machine
# or per mmu_unit basis.
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# MmuSensor utils including:
#   - ADC helper for klipper compatibility
#   - Enhanced runout helper for sensors
#   - Switch based sensor
#   - Virtual sensor (that also support endstop homing)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

# Klipper imports
import mcu

# Happy Hare imports
from .mmu_constants import *


INSERT_GCODE = "__MMU_SENSOR_INSERT"
REMOVE_GCODE = "__MMU_SENSOR_REMOVE"
RUNOUT_GCODE = "__MMU_SENSOR_RUNOUT"
CLOG_GCODE   = "__MMU_SENSOR_CLOG"
TANGLE_GCODE = "__MMU_SENSOR_TANGLE"

EVENT_GCODES = {
    "insert": INSERT_GCODE,
    "remove": REMOVE_GCODE,
    "runout": RUNOUT_GCODE,
    "clog":   CLOG_GCODE,
    "tangle": TANGLE_GCODE,
}


# -----------------------------------------------------------------------------------------------------------
# Adc helper class
# -----------------------------------------------------------------------------------------------------------

class MmuAdcHelper:

    @staticmethod
    def setup_adc_compat(mcu_adc, report_time, sample_time, sample_count, callback):
        if hasattr(mcu_adc, 'setup_adc_sample'):
            try:
                mcu_adc.setup_adc_sample(report_time, sample_time, sample_count)
                mcu_adc.setup_adc_callback(callback)
            except TypeError:
                mcu_adc.setup_adc_sample(sample_time, sample_count)
                mcu_adc.setup_adc_callback(report_time, callback)

        elif hasattr(mcu_adc, 'setup_minmax'):
            mcu_adc.setup_minmax(sample_time, sample_count)
            mcu_adc.setup_adc_callback(report_time, callback)

        else:
            raise RuntimeError(
                "Klipper version not compatible: mcu_adc missing "
                "'setup_adc_sample' and 'setup_minmax'"
            )

    @staticmethod
    def unpack_adc_callback(*args):
        """
        Old klipper: callback(read_time, read_value)
        New klipper: callback(samples) where samples is a list of
          (read_time, read_value)
        """
        if len(args) == 1:
            samples = args[0]
            return samples[-1]

        if len(args) == 2:
            return args

        raise TypeError(
            "ADC callback expected (read_time, read_value) or (samples), got %d args"
            % len(args)
        )



# -----------------------------------------------------------------------------------------------------------
# Enhanced "runout helper" that gives greater control of when filament sensor events are fired and
# direct access to button events in addition to creating a "remove" / "runout" distinction
# This class is also used to create virtual sensors when analog sensors can emulate them
# -----------------------------------------------------------------------------------------------------------

class MmuRunoutHelper:

    def __init__(self, printer, name,
            event_delay=0,
            gcodes=None,
            insert_remove_in_print=False,
            button_handler=None,
            register=True,
        ):
        """
        gcodes: dict of gcode macros to call for each event type.
        Any key can be omitted or set to None/"" to disable that event.
        """

        self.printer, self.name = printer, name

        # Expecting a dict with keys like "insert", "remove", "runout", "clog", "tangle"
        self.gcodes = gcodes or {}

        self.insert_remove_in_print = insert_remove_in_print
        self.button_handler = button_handler
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.min_event_systime = self.reactor.NEVER
        self.event_delay = event_delay # Time between generated events
        self.filament_present = False
        self.sensor_enabled = True
        self.runout_suspended = False
        self.button_handler_suspended = False
        self.events_suspended = False
        self._events_suspended_from = None

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        if register:
            self.gcode.register_mux_command(
                "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
                self.cmd_QUERY_FILAMENT_SENSOR,
                desc=self.cmd_QUERY_FILAMENT_SENSOR_help)

            self.gcode.register_mux_command(
                "SET_FILAMENT_SENSOR", "SENSOR", self.name,
                self.cmd_SET_FILAMENT_SENSOR,
                desc=self.cmd_SET_FILAMENT_SENSOR_help)


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
        if not self.gcodes:
            return # No actions to take

        # Determine "printing" status
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is not None:
            is_printing = print_stats.get_status(eventtime)["state"] == "printing"
        else:
            is_printing = self.printer.lookup_object("idle_timeout").get_status(eventtime)["state"] == "Printing"

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


    def suspend_events(self, suspend):
        """
        Suppress insert/remove/runout gcode events without disabling the sensor.

        For an operation that deliberately drives filament across a sensor - MMU_NFC_SCAN
        homes through the gate to establish a datum, crossing the entry sensor - where an
        insert event would start an MMU_PRELOAD inside the operation that caused it.

        Works the min_event_systime gate, which is exactly what _process_state_change
        already uses to stop an event re-entering while its handler runs. Deliberately NOT
        sensor_enabled: that would also make check_sensor() report the sensor as absent,
        and callers still need to query it (the scan reads the gate switch to pick its
        homing direction). Note button_handler_suspended is no help here either - that
        gates the sync-feedback callback, not these gcode events.
        """
        if suspend:
            if not self.events_suspended:
                self.events_suspended = True
                self._events_suspended_from = self.min_event_systime # Save once, so nesting is safe
            self.min_event_systime = self.reactor.NEVER

        elif self.events_suspended:
            self.events_suspended = False
            # NEVER means a handler was in flight when we suspended. Restoring it would
            # silence the sensor for the rest of the session, so re-open the gate instead
            restore = self._events_suspended_from
            self.min_event_systime = (
                self.reactor.monotonic() if restore == self.reactor.NEVER else restore)


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
        # Make a Mainsail/SET_FILAMENT_SENSOR toggle just as sticky as MMU_SENSORS ENABLE=
        mmu = self.printer.lookup_object('mmu', None)
        if mmu is not None:
            mmu.sensor_manager.persist_sensor_enabled_change(self)



# -----------------------------------------------------------------------------------------------------------
# Factory class for setting up standard MMU (switch) sensors
# -----------------------------------------------------------------------------------------------------------

class MmuSensorFactory:

    def __init__(self, printer):
        self.printer = printer

    def create_mmu_sensor(self, config, name_prefix, gate, switch_pin, **kwargs):

        if self._is_empty_pin(switch_pin):
            return None

        return MmuSwitchSensor(
            config=config,
            name_prefix=name_prefix,
            gate=gate,
            switch_pin=switch_pin,
            **kwargs,
        )


    def _is_empty_pin(self, switch_pin):
        if switch_pin is None or switch_pin == '':
            return True

        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(switch_pin, can_invert=True, can_pullup=True)
        pin_resolver = ppins.get_pin_resolver(pin_params['chip_name'])
        real_pin = pin_resolver.aliases.get(pin_params['pin'], '_real_')
        return (real_pin == '')



# -----------------------------------------------------------------------------------------------------------
# Set up a MMU sensor. Generally these are enhanced filament_switch_sensors but can also be virtual
# -----------------------------------------------------------------------------------------------------------

class MmuSensor:

    def __init__(
        self, config, name_prefix, gate,
        event_delay=0,
        events=(),
        insert_remove_in_print=False,
        button_handler=None,
        register=True,
    ):
        self.printer = config.get_printer()
        name = self.name = "%s_%d" % (name_prefix, gate) if gate is not None else name_prefix

        gate_arg = (" GATE=%d" % gate) if gate is not None else ""

        events = set(events or ())
        gcodes = {
            event: "%s SENSOR=%s%s" % (macro, name, gate_arg)
            for event, macro in EVENT_GCODES.items()
            if event in events
        }

        ro_helper = MmuRunoutHelper(
            self.printer,
            name,
            event_delay=event_delay,
            gcodes=gcodes,
            insert_remove_in_print=insert_remove_in_print,
            button_handler=button_handler,
            register=register,
        )

        self.runout_helper = ro_helper
        self.get_status = ro_helper.get_status

        # This will make sensor visible in UI's like Mainsail/Fluidd and allow it to be disabled
        if register:
            self.printer.add_object(f"filament_switch_sensor {name}", self)

        logging.info(f"MMU: Created MmuSensor({name})")



# -----------------------------------------------------------------------------------------------------------
# Set up a regular switch based MMU sensor
# -----------------------------------------------------------------------------------------------------------

class MmuSwitchSensor(MmuSensor):

    def __init__(self, config, name_prefix, gate, switch_pin, **kwargs):
        super().__init__(config, name_prefix, gate, **kwargs)

        self.switch_pin = switch_pin
        if switch_pin is not None:
            buttons = self.printer.load_object(config, 'buttons')
            # TODO debounce_delay will be read for supplied config, could this solve mmu_entry flutter issue?
            buttons.register_debounce_button(switch_pin, self._button_handler, config)


    # Handler for digital switch sensors to update state
    def _button_handler(self, eventtime, state):
        self.runout_helper.note_filament_present(eventtime, state)



# -----------------------------------------------------------------------------------------------------------
# Set up a virtual sensor. This is typically supported by an analog pin where the wrapper
# calls trigger_handler() similar to a button callback.
# Also, this sensor object is used for "software" endstops so implements the endstop interface
# -----------------------------------------------------------------------------------------------------------

class MmuVirtualEndstopSensor(MmuSensor):

    def __init__(self, config, name_prefix, gate, **kwargs):
        super().__init__(config, name_prefix, gate, **kwargs)

        # For "software" endstop support
        self._steppers = []
        self._trigger_completion = None
        self._last_trigger_time = None
        self._homing = False
        self._triggered = False


    def trigger_handler(self, eventtime, state):
        # Sensor gating is on the host reactor clock but 'eventtime' may be print_time
        # (see the table below), so take reactor time from the reactor rather than trust it.
        # Callbacks reach us on the host, so this is within a report interval of the event.
        self.runout_helper.note_filament_present(self.runout_helper.reactor.monotonic(), state)

        # Process endstop if homing. The recorded trigger time is returned by
        # home_wait() and fed to stepper.get_past_mcu_position(), which needs MCU
        # print_time - so it goes through _endstop_trigger_time() (see below).
        if self._homing and state == self._triggered:
            if self._trigger_completion is not None:
                self._last_trigger_time = self._endstop_trigger_time(eventtime)
                self._trigger_completion.complete(True)
                self._trigger_completion = None


    # Trigger-time clock reference, by endstop source
    # -----------------------------------------------
    # home_wait() returns _last_trigger_time, which Klipper feeds to
    # stepper.get_past_mcu_position() to compute the stopped/trigger position - so
    # it MUST be MCU print_time. Trigger callbacks arrive on different clocks
    # depending on the source, so each records its time via _endstop_trigger_time()
    # (default identity; host-timed sources override to convert):
    #
    #   endstop / trigger source     incoming clock          -> _last_trigger_time
    #   ---------------------------  ----------------------  ----------------------
    #   encoder (MCU counter cb)     print_time              identity -> print_time
    #   compression/tension (ADC cb) print_time (read_time)  identity -> print_time
    #   MmuHallEndstop (ADC cb)      print_time (read_time)  identity -> print_time
    #   MmuAdcSwitchSensor           reactor eventtime       convert  -> print_time
    #   MmuNfcEndstop (host poll)    reactor eventtime       convert  -> print_time
    #
    # note_filament_present() (above) never sees this value at all - it is given
    # reactor.monotonic() directly, because its event gating and the EVENTTIME it
    # passes to the runout handler are both on the reactor clock.
    def _endstop_trigger_time(self, eventtime):
        """
        Time recorded for a homing trigger. Default identity; host-timed
        sources override to convert reactor eventtime -> MCU print_time.
        """
        return eventtime


    def estimated_print_time(self, eventtime):
        """
        Convert a host reactor 'eventtime' to MCU print_time on the homed
        stepper's MCU. Falls back to eventtime when no stepper is bound yet.
        """
        steppers = self.get_steppers()
        if steppers:
            try:
                return steppers[0].get_mcu().estimated_print_time(eventtime)
            except Exception:
                pass
        return eventtime


    # Interface required to implement an endstop ----------------------------------

    def query_endstop(self, print_time):
        return self.runout_helper.filament_present


    def setup_pin(self, pin_type, pin_name):
        return self


    def add_stepper(self, stepper):
        if stepper not in self._steppers:
            self._steppers.append(stepper)


    def get_steppers(self):
        return list(self._steppers)


    def home_start(self, print_time, sample_time, sample_count, rest_time, triggered):
        self._trigger_completion = self.printer.get_reactor().completion()
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



# -----------------------------------------------------------------------------------------------------------
# Compound endstop wrapper. Allows homing against multiple virtual endstops and an optional
# MCU endstop, triggering on the first to activate while recording the source of the trigger.
# Presents a standard Klipper endstop interface for transparent use during homing.
# -----------------------------------------------------------------------------------------------------------

class MmuCompoundEndstop:
    def __init__(self, printer, name, endstops):
        self._printer = printer
        self.name = name

        self.endstops = []
        self.endstop_names = {}
        self.virtual_endstops = []
        self.mcu_endstop = None

        if not endstops:
            raise self._printer.command_error(
                "No endstops specified for %s" % self.name
            )

        for endstop_tuple in endstops:
            endstop, endstop_name = endstop_tuple

            self.endstops.append(endstop)
            self.endstop_names[endstop] = endstop_name

            if isinstance(endstop, mcu.MCU_endstop):
                if self.mcu_endstop is not None:
                    raise self._printer.command_error("Only one MCU endstop may be specified for %s" % self.name)
                self.mcu_endstop = endstop
            else:
                self.virtual_endstops.append(endstop)

        self._steppers = []
        self._trigger_completion = None
        self._triggered_endstop = None
        self._last_trigger_time = None
        self._homing = False
        self._pending = 0  # Number of children not yet resolved
        self._resolved = False # Has any child already resolved our completion?


    def get_triggered_endstop_name(self):
        if self._triggered_endstop is None:
            return None
        return self.endstop_names.get(self._triggered_endstop)


    # Interface required to implement an endstop ----------------------------------

    def setup_pin(self, pin_type, pin_name):
        # Important: real MCU endstops need Klipper's normal setup path.
        if self.mcu_endstop is not None:
            self.mcu_endstop.setup_pin(pin_type, pin_name)
        return self


    def add_stepper(self, stepper):
        if stepper not in self._steppers:
            self._steppers.append(stepper)


    def get_steppers(self):
        if self.mcu_endstop is not None:
            return self.mcu_endstop.get_steppers()
        return list(self._steppers)


    def query_endstop(self, print_time):
        return any(es.query_endstop(print_time) for es in self.endstops)


    def home_start(self, print_time, sample_time, sample_count, rest_time, triggered):
        reactor = self._printer.get_reactor()

        self._trigger_completion = reactor.completion()
        self._triggered_endstop = None
        self._last_trigger_time = None
        self._homing = True

        self._resolved = False
        self._pending = len(self.endstops)
        for es in self.endstops:
            child_completion = es.home_start(
                print_time, sample_time, sample_count, rest_time, triggered
            )
            reactor.register_callback(
                lambda eventtime, es=es, c=child_completion:
                    self._wait_for_child_endstop(es, c)
            )

        return self._trigger_completion


    def _wait_for_child_endstop(self, endstop, child_completion):
        """
        Forward the first child resolution to our own completion so the homing move
        stops. That is this callback's ONLY job - it deliberately does not decide which
        child won; home_wait() does that from the children's return values.

        Why the completion can't identify the winner: its VALUE means opposite things
        depending on the child. Klipper's MCU_endstop resolves via
        async_complete(tc, is_failure), so a real hit completes with False and a comms
        timeout with True, and TriggerDispatch.wait_end() also completes True at
        end-of-move - i.e. for a MISS. MmuVirtualEndstopSensor is the other way round:
        it completes True on a genuine trigger. So `bool(child_completion.wait())` is
        exactly inverted for one of the two, whichever way you write it. (And note
        ReactorCompletion.wait() RETURNS the value - it never raises - so there is no
        exception to catch here either.)
        """
        child_completion.wait()

        if not self._homing:
            return # home_wait() already ran; nothing left to resolve

        self._pending -= 1
        if not self._resolved:
            self._resolved = True
            self._trigger_completion.complete(True)
        elif self._pending == 0:
            # Every child resolved without anyone stopping the move - complete so
            # home_wait()'s "no trigger" path can fire instead of hanging forever
            self._trigger_completion.complete(False)


    @staticmethod
    def _child_triggered(trigger_time, error):
        """
        Did a child actually stop the move, judged from its home_wait() result?

        Uniform across every child type we support:
          - Klipper MCU_endstop:      hit -> print_time,  no trigger -> exactly 0.,
                                      comms timeout -> raises
          - MmuVirtualEndstopSensor:  hit -> _last_trigger_time,  no trigger -> raises
          - the test harness's MCU_endstop: hit -> print_time, no trigger -> raises

        Deliberately `!= 0.` and NOT Klipper's own `> 0.` (extras/homing.py). Print times
        are not necessarily positive: the test harness offsets them by HOST_OFFSET=1234.5
        against a reactor starting at 1000.0, so a perfectly good harness trigger time is
        around -234.5. A `> 0.` test would call every one of those a miss. Only Klipper's
        documented 0. sentinel means "did not trigger".

        Residual ambiguity: a genuine hit at exactly print_time 0. reads as a miss. That
        is the same corner Klipper itself tolerates (and it rejects all negatives too).
        """
        return error is None and trigger_time is not None and trigger_time != 0.


    def home_wait(self, home_end_time):
        self._homing = False
        self._trigger_completion = None

        # Close out EVERY child exactly once, in insertion order, and keep each result.
        # Children are not re-callable: Klipper's MCU_endstop.home_wait disarms through
        # _dispatch.stop(), and MmuNfcEndstop.home_wait stops the manager's presence poll.
        results = []
        for es in self.endstops:
            try:
                results.append((es, es.home_wait(home_end_time), None))
            except Exception as e:
                results.append((es, None, e))

        # Winner = the earliest trigger time among the children that actually triggered.
        # min() over the enumerate index breaks ties by insertion order, which for the NFC
        # compound is [gate switch, reader] - and gate-first is the safe direction, since
        # callers read a gate win as "we are on the datum".
        #
        # A child that was ALREADY in its sought state when armed completes immediately and
        # reports the arm-time print_time, which is earlier than any later real trip, so it
        # wins. That is correct rather than an off-by-one: a pre-triggered endstop is
        # exactly why the move could not go anywhere, and the halt is at 0mm.
        candidates = [(t, i, es) for i, (es, t, err) in enumerate(results)
                      if self._child_triggered(t, err)]
        if not candidates:
            # Nobody triggered, so surface the most informative failure we have.
            #
            # Prefer an exception from a real MCU child: MCU_endstop signals a plain
            # no-trigger by RETURNING 0., so if it raised at all it is a genuine fault
            # ("Communication timeout during homing"). A virtual child, by contrast, raises
            # as its ordinary no-trigger signal, so its error must not be allowed to mask
            # a hardware fault - which is exactly what re-raising the last error did.
            errors = [(es, err) for es, _t, err in results if err is not None]
            mcu_errors = [err for es, err in errors if isinstance(es, mcu.MCU_endstop)]
            if mcu_errors:
                raise mcu_errors[0]
            if errors:
                # A virtual child's own message names the endstop, which beats ours
                raise errors[0][1]
            raise self._printer.command_error("No trigger on %s after full movement" % self.name)

        self._last_trigger_time, _idx, self._triggered_endstop = min(candidates)
        return self._last_trigger_time
