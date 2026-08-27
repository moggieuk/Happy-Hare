# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Driver for encoder that supports movement measurement, runout/clog detection and flow rate calc
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Klipper imports
from ... import pulse_counter

# Happy Hare imports
from ..mmu_constants    import *
from ..mmu_sensor_utils import MmuVirtualEndstopSensor

CHECK_MOVEMENT_TIMEOUT = 0.250


class MmuEncoder:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.active_mmu_unit = None
        self.extruder = None
        self.connected_units = [mmu_unit]       # mmu_unit is just the first to load, not necessarily all
        encoder_pin = config.get('encoder_pin')

        # For measurement/counter functionality...
        sample_time = config.getfloat('sample_time', 0.1, above=0.)  # How often Klippy receives accumulated counts
        poll_time = config.getfloat('poll_time', 0.001, above=0.)    # How often the MCU samples the GPIO for edge changes
        encoder_resolution = config.getfloat('encoder_resolution', 1., above=0.)        # Expect to be calibrated by user in Happy Hare
        register_as_sensor = config.getint('register_as_sensor', 1, minval=0, maxval=1) # Make visible as filament switch sensor?
        self.no_movement_samples = config.getint('no_movement_samples', 10, minval=1)   # How many no-movement samples to un-trigger sensor
        self.no_movement_count = 0
        self.sensor_triggered = False
        self.set_resolution(encoder_resolution)
        self._last_count_time = None          # Last time counts were received from MCU
        self._counts = self._last_count = 0
        self._movement = False
        counter = pulse_counter.MCU_counter(self.printer, encoder_pin, sample_time, poll_time)
        counter.setup_callback(self._counter_callback)

        # For FlowGuard runout/clog/tangle functionality...
        self.active = False                   # Active only while in a print
        self._flowguard_enabled = False       # FlowGuard Runout/Clog/Tangle functionality
        self._flowguard_generation = 0        # Invalidates callbacks queued before a disable/re-enable

        # The runout headroom that MMU will attempt to maintain (closest MMU comes to triggering runout)
        self.desired_headroom = config.getfloat('desired_headroom', 6., above=0.)
        # The "damping" effect of last measurement. Higher value means clog_length will be reduced more slowly
        self.average_samples = config.getint('average_samples', 4, minval=1)
        # The extrusion interval where new detection_length is calculated (also done on toolchange)
        self.next_calibration_point = self.calibration_length = config.getfloat('calibration_length', 10000., minval=50.) # 10m

        # Detection length will be set by MMU calibration
        self.detection_length = self.min_headroom = config.getfloat('detection_length', 10., above=2.) # Note: this is now in flowguard!

        self.event_delay = config.getfloat('event_delay', 2., above=0.)
        self.pause_delay = config.getfloat('pause_delay', 0, above=0.)
        self.runout_gcode = '__MMU_ENCODER_RUNOUT'
        self.insert_gcode = '__MMU_ENCODER_INSERT'
        self.min_event_systime = self.reactor.NEVER
        self.filament_detected = False
        self.detection_mode = ENCODER_RUNOUT_STATIC
        self.last_extruder_pos = self.filament_runout_pos = 0.
        self.filament_runout_pos = self.min_headroom = self.detection_length

        # For flowrate functionality
        self.flowrate_last_encoder_pos = 0.
        self.extrusion_flowrate = 0.
        self.samples = []
        self.flowrate_samples = config.getint('flowrate_samples', 20, minval=5)

        # Create virtual endstop (for giggles and experimental use)
        endstop_sensor_name = f"{self.name}:{SENSOR_ENCODER}"
        self.endstop_sensor = MmuVirtualEndstopSensor(config, endstop_sensor_name, None, register=register_as_sensor)

        # Register event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)

        self.printer.register_event_handler('mmu:printing', self._handle_printing)
        self.printer.register_event_handler('mmu:not_printing', self._handle_not_printing)


    def add_unit(self, mmu_unit):
        self.connected_units.append(mmu_unit)


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def handle_ready(self):
        # Read calibrated encoder resolution if available
        cal_res = self.mmu_machine.var_manager.get(VARS_MMU_ENCODER_RESOLUTION, None, namespace=self.name)
        if cal_res:
            self.set_resolution(cal_res)
            self.mmu.log_debug(f"Loaded saved resolution for encoder {self.name}: {cal_res:.4f}")

            for unit in self.connected_units:
                unit.calibrator.mark_calibrated(CALIBRATED_ENCODER)
        else:
            self.mmu.log_warning(f"Warning: Encoder resolution for {self.name} was not found in mmu_vars.cfg. Probably not calibrated")

        self.min_event_systime = self.reactor.monotonic() + 2. # Don't process events too early
        self._extruder_pos_update_timer = self.reactor.register_timer(self._extruder_pos_update_event)


    def _handle_printing(self, eventtime):
        self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NOW) # Enabled
        self.active = True


    def _handle_not_printing(self, eventtime):
        self.reactor.update_timer(self._extruder_pos_update_timer, self.reactor.NEVER) # Disabled
        self.active = False


# -----------------------------------------------------------------------------
# Flowguard runout/clog/tangle functionality
# -----------------------------------------------------------------------------

    def get_clog_detection_length(self):
        return self.detection_length


    def get_effective_clog_detection_length(self, mmu_unit):
        """
        The detection length in force for a unit, for callers outside the flowguard loop.
        While flowguard is enabled the live length is the answer because autotuning owns it.
        While it isn't, the live length is just whatever was last in use and can lag a fresh
        calibration, so answer with what the next enable_flowguard() will settle on instead
        """
        if self._flowguard_enabled and self.active_mmu_unit is mmu_unit:
            return self.detection_length
        return self._resolve_detection_length(mmu_unit)


    # The detection length a unit's mode calls for. Auto mode follows the calibrated length
    # until there is one, everything else uses the configured maximum motion
    def _resolve_detection_length(self, mmu_unit):
        cdl = mmu_unit.p.flowguard_encoder_max_motion
        if mmu_unit.p.flowguard_encoder_mode == ENCODER_RUNOUT_AUTOMATIC:
            cal_cdl = mmu_unit.calibrator.get_clog_detection_length()
            if cal_cdl is not None:
                cdl = cal_cdl
        return max(cdl, 2.)


    # Suggest that a new automatic detection length is calculated
    def note_clog_detection_length(self):
        self._update_detection_length()


    def enable_flowguard(self, mmu_unit):
        if mmu_unit not in self.connected_units:
            return

        self.active_mmu_unit = mmu_unit

        # Make sure we are watching the correct extruder
        self.extruder = self.printer.lookup_object(mmu_unit.extruder_name())

        # Mode of operation for particular mmu_unit
        mode = mmu_unit.p.flowguard_encoder_mode
        self.detection_mode = mode

        self.detection_length = self._resolve_detection_length(mmu_unit)

        self._reset_filament_runout_params()
        self._flowguard_enabled = (mode != 0)
        self._flowguard_generation += 1
        return self._flowguard_enabled # Success if mode is not "off"


    def disable_flowguard(self):
        self._flowguard_enabled = False
        self._flowguard_generation += 1
        return (self.detection_mode != 0) # Success if mode is not "off"


    def is_flowguard_enabled(self):
        return self._flowguard_enabled


    def get_flowguard_generation(self):
        return self._flowguard_generation


    def _get_extruder_pos(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()

        print_time = self.printer.lookup_object('mcu').estimated_print_time(eventtime)

        if not self.extruder:
            return 0.

        return self.extruder.find_past_position(print_time)


    # Called periodically to check filament movement
    def _extruder_pos_update_event(self, eventtime):
        if not self._flowguard_enabled or not self.active:
            return eventtime + CHECK_MOVEMENT_TIMEOUT

        extruder_pos = self._get_extruder_pos(eventtime)

        # First lets see if we got encoder movement since last invocation
        if self._movement:
            self._movement = False
            self.filament_runout_pos = max(extruder_pos + self.detection_length, self.filament_runout_pos)

        if extruder_pos >= self.next_calibration_point:
            if self.next_calibration_point > 0:
                self._update_detection_length()
            self.next_calibration_point = extruder_pos + self.calibration_length

        headroom = max(0, self.filament_runout_pos - extruder_pos)
        if headroom < self.min_headroom:
            self.min_headroom = headroom
            if self.min_headroom < self.desired_headroom:
                if self.detection_mode == ENCODER_RUNOUT_AUTOMATIC:
                    self.mmu.log_debug(
                        f"Automatic clog detection: new min_headroom "
                        f"(< {self.desired_headroom:.1f}mm desired): "
                        f"{self.min_headroom:.1f}mm"
                    )
                elif self.detection_mode == ENCODER_RUNOUT_STATIC:
                    self.mmu.log_debug(
                        f"Warning: Only {self.min_headroom:.1f}mm of headroom to clog/runout"
                    )

        self._handle_filament_event(extruder_pos < self.filament_runout_pos)

        # Flowrate calc. Depends of calibration accuracy of encoder
        encoder_pos = self.get_distance()
        # If encoder has moved, record the extruder and encoder movement for flow rate calcs
        if encoder_pos > self.flowrate_last_encoder_pos:
            self._record(encoder_pos, extruder_pos)
            self.flowrate_last_encoder_pos = encoder_pos

        self.last_extruder_pos = extruder_pos

        return eventtime + CHECK_MOVEMENT_TIMEOUT


    def _reset_min_headroom(self):
        self.min_headroom = self.detection_length


    def _reset_filament_runout_params(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()

        self.last_extruder_pos = self._get_extruder_pos(eventtime)
        self.flowrate_last_encoder_pos = self.get_distance()
        self.extrusion_flowrate = 0.
        self.samples = []
        self.filament_runout_pos = self.last_extruder_pos + self.detection_length + self.desired_headroom # Add some headroom to decrease sensitivity on startup
        self.next_calibration_point = self.last_extruder_pos + self.calibration_length
        self._reset_min_headroom()


    # Called periodically to tune the automatic clog detection length
    def _update_detection_length(self, increase_only=False):
        # Match v3: suspended monitoring must not consume or alter the measurement
        # accumulated during the preceding print interval.
        if not self._flowguard_enabled:
            return
        if self.detection_mode != ENCODER_RUNOUT_AUTOMATIC:
            return

        old_detection_length = self.detection_length
        headroom_error = self.desired_headroom - self.min_headroom

        if headroom_error > 0:
            # Maintain headroom
            extra_length = min(headroom_error, self.desired_headroom)
            self.detection_length += extra_length
            self.mmu.log_debug(f"Automatic clog detection: maintaining headroom by adding {extra_length:.1f}mm to detection_length")

        elif headroom_error < 0 and not increase_only:
            # Average down
            sample = self.detection_length + headroom_error
            self.detection_length = (((self.average_samples - 1) * self.detection_length) + sample) / self.average_samples
            self.detection_length = max(self.detection_length, 2.)
            self.mmu.log_debug(f"Automatic clog detection: averaging down detection_length with new {sample:.1f}mm measurement")

        else:
            return

        self._reset_min_headroom()
        self.filament_runout_pos = self.last_extruder_pos + self.detection_length

        if round(self.detection_length, 1) != round(old_detection_length, 1): # Persist if significant
            self.mmu.log_debug(
                f"Automatic clog detection: reset detection_length to "
                f"{self.detection_length:.1f}mm"
            )

            # Tell the calibrator for this unit of the change
            if self.active_mmu_unit:
                self.active_mmu_unit.calibrator.update_clog_detection_length(round(self.detection_length, 1))

            # Match v3 set_clog_detection_length(): after a material autotune change,
            # rebase at the live extruder position and grant startup headroom.
            self._reset_filament_runout_params()


    # Called to see if state update requires callback notification
    def _handle_filament_event(self, filament_detected):
        if self.filament_detected == filament_detected:
            return

        self.filament_detected = filament_detected
        eventtime = self.reactor.monotonic()
        if eventtime < self.min_event_systime or self.detection_mode == ENCODER_RUNOUT_DISABLED or not self._flowguard_enabled:
            return
        is_printing = self.printer.lookup_object("idle_timeout").get_status(eventtime)["state"] == "Printing"

        if filament_detected:
            if not is_printing and self.insert_gcode is not None:
                # Insert detected
                self.min_event_systime = self.reactor.NEVER
                self.mmu.log_info(f"MMU: Encoder Sensor {self.name}: insert event detected, Time {eventtime:.2f}")
                self.reactor.register_callback(self._insert_event_handler)

        else:
            if is_printing and self.runout_gcode is not None:
                # Runout detected
                self.min_event_systime = self.reactor.NEVER
                self.mmu.log_info(f"MMU: Encoder Sensor {self.name}: runout event detected, Time {eventtime:.2f}")
                generation = self._flowguard_generation
                self.reactor.register_callback(
                    lambda reh, et=eventtime, gen=generation: self._runout_event_handler(reh, et, gen))


    def _runout_event_handler(self, reactor_eventtime, eventtime, generation):
        # Avoid issuing even a transient PAUSE when the callback was invalidated before
        # it began. The command repeats this check after acquiring the gcode mutex.
        if not self.active or not self._flowguard_enabled or generation != self._flowguard_generation:
            self.min_event_systime = self.reactor.monotonic() + self.event_delay
            return

        # Pausing from inside an event requires that the pause portion of pause_resume execute immediately.
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_pause_command()
        if self.pause_delay:
            self.printer.get_reactor().pause(reactor_eventtime + self.pause_delay)
        self._exec_gcode(f"{self.runout_gcode} EVENTTIME={eventtime:.6f} GENERATION={generation}")


    def _insert_event_handler(self, eventtime):
        self._exec_gcode(self.insert_gcode)


    def _exec_gcode(self, command):
        try:
            self.gcode.run_script(command)
        except Exception:
            self.mmu.log_error(f"MMU: Error running mmu encoder handler: `{command}`")
        self.min_event_systime = self.reactor.monotonic() + self.event_delay


    def _record(self, encoder_pos, extruder_pos):
        self.samples.append((encoder_pos, extruder_pos))
        if len(self.samples) > self.flowrate_samples:
            self.samples = self.samples[-self.flowrate_samples:]
        encoder_movement = encoder_pos - self.samples[0][0]
        extruder_movement = extruder_pos - self.samples[0][1]
        new_extrusion_flowrate = (encoder_movement / extruder_movement) if extruder_movement > 0. else 1.
        self.extrusion_flowrate = (self.extrusion_flowrate + new_extrusion_flowrate) / 2.


# -----------------------------------------------------------------------------
# Encoder measurement/counter functionality
# -----------------------------------------------------------------------------

    def set_resolution(self, resolution):
        self.resolution = resolution


    def get_resolution(self):
        return self.resolution


    # The threshold (mm) that determines real encoder movement
    # (set to 1.5 pulses of encoder. i.e. to allow one rogue pulse)
    def movement_min(self):
        return 1.5 * self.resolution


    def reset_counts(self):
        self._counts = 0


    def get_counts(self):
        return self._counts


    def set_distance(self, new_distance):
        self._counts = int(round(new_distance / self.resolution))


    def get_distance(self):
        return self._counts * self.resolution


    # Callback for MCU_counter
    def _counter_callback(self, print_time, count, count_time):
        if self._last_count_time is None:
            self._last_count_time = print_time
            self._last_count = count
            self._movement = False
            self._endstop_triggered = False
            self._no_movement_count = 0
            self.endstop_sensor.trigger_handler(print_time, False)
            return

        delta_time = count_time - self._last_count_time

        if delta_time > 0:
            self._last_count_time = count_time

            new_counts = count - self._last_count
            self._counts += new_counts
            movement = new_counts > 0

            # FlowGuard samples less often than the MCU counter. Latch movement until
            # _extruder_pos_update_event() consumes it; an intervening empty counter
            # sample must not erase a real pulse.
            self._movement = self._movement or movement

            if movement:
                self._no_movement_count = 0

                if not self._endstop_triggered:
                    self._endstop_triggered = True
                    self.endstop_sensor.trigger_handler(print_time, True)

            else:
                self._no_movement_count += 1

        else:
            # No counts since last sample
            self._last_count_time = print_time
            self._no_movement_count += 1

        if (self._endstop_triggered and self._no_movement_count >= self.no_movement_samples):
            self._endstop_triggered = False
            self.endstop_sensor.trigger_handler(print_time, False)

        self._last_count = count


    def get_status(self, eventtime):
        return {
                'encoder_pos': round(self.get_distance(), 1),
                'detection_length': round(self.detection_length, 1),
                'min_headroom': round(self.min_headroom, 1),
                'headroom': round(min(max(0, self.filament_runout_pos - self.last_extruder_pos), self.detection_length), 1),
                'desired_headroom': round(self.desired_headroom, 1),
                'detection_mode': self.detection_mode,
                'enabled': self._flowguard_enabled,
                'flow_rate': int(round(min(self.extrusion_flowrate, 1.) * 100))
        }
