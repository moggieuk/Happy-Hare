# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Class to handle sync-feedback and adjustment of gear stepper rotation distance
#       to keep MMU in sync with extruder as well as some filament tension routines.
#       This will always exist even in the absense of a mmu_buffer - flowguard is
#       available with just encoder.
#
# FlowGuard: It also implements protection for all modes/sensor types that will trigger
#            on clog (at extruder) or tangle (at MMU) conditions.
#
# Autotune: An autotuning option can be enabled for dynamic tuning (and persistence) of
#           calibrated MMU gear rotation_distance.
#
# Implements commands:
#   MMU_SYNC_FEEDBACK
#   MMU_FLOWGUARD
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, math, time, os

# Happy Hare imports
from ..mmu_constants      import *
from ..mmu_utils          import MmuError
from .mmu_sync_controller import SyncControllerConfig, SyncController

SF_STATE_NEUTRAL     = 0
SF_STATE_COMPRESSION = 1
SF_STATE_TENSION     = -1


class MmuSyncFeedback:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()

        self.estimated_state = float(SF_STATE_NEUTRAL)
        self.active = False           # Sync-feedback actively operating?
        self.flowguard_active = False # FlowGuard armed in sync-feedback controller or just using encoder
        self.ctrl = None
        self.flow_rate = 100.         # Estimated % flowrate (calc only for proportional sensors)

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('mmu:initialized', self.handle_mmu_initialized)

        # Initial flowguard status (when using sync-feedback controller)
        self.flowguard_status = {'trigger': '', 'reason': '', 'level': 0.0, 'max_clog': 0.0, 'max_tangle': 0.0, 'active': False, 'enabled': False}


    def reinit(self):
        pass


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Shared MMU controller class

        if self.mmu_unit.has_buffer():
            # Setup events for managing motor synchronization
            self.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
            self.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
            self.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)


    def handle_mmu_initialized(self):
        if self.mmu_unit.has_buffer():
            self._init_controller()


    def is_enabled(self):
        """
        This is whether the user has enabled the sync-feedback feature (the "big" switch)
        """
        return self.p.sync_feedback_enabled


    def is_active(self):
        """
        Returns whether the sync-feedback is currently active (when synced)
        """
        return self.active


    def get_sync_feedback_string(self, state=None, detail=False):
        if not self.mmu_unit.has_buffer():
            return "unavailable"

        if state is None:
            state = self._get_sensor_state()
        if (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active) or detail:
            # Polarity varies slightly between modes on proportional sensor so ask controller
            polarity = self.ctrl.polarity(state)
            return 'compressed' if polarity > 0 else 'tension' if polarity < 0 else 'neutral'
        elif self.mmu.is_enabled and self.p.sync_feedback_enabled:
            return "inactive"
        return "disabled"


    def activate_flowguard(self, eventtime):
        u = self.mmu_unit
        msg = None

        if u.has_buffer() and self.p.flowguard_enabled and not self.flowguard_active:
            self.flowguard_active = True
            # This resets controller with last good autotuned RD, resets Flowguard then resumes Autotune
            self._reset_controller(eventtime, hard_reset=False)
            self.ctrl.autotune.resume()
            msg = "FlowGuard monitoring activated and Autotune resumed"

        # Enable encoder based Flowguard
        if u.has_encoder() and not u.encoder.is_flowguard_enabled():
            if not u.encoder.enable_flowguard(u):
                return # Must in in off mode
            self.flowguard_active = True
            msg = msg or "FlowGuard monitoring with encoder activated"

        if msg:
            self.mmu.log_info(msg)


    def deactivate_flowguard(self, eventtime):
        u = self.mmu_unit
        msg = None

        if u.has_buffer() and self.p.flowguard_enabled and self.flowguard_active:
            self.flowguard_active = False
            self.ctrl.autotune.pause()
            msg = "FlowGuard monitoring deactivated and Autotune paused"

        # Enable encoder based "flowguard"
        if u.has_encoder() and u.encoder.is_flowguard_enabled():
            if not u.encoder.disable_flowguard():
                return # Must in in off mode
            self.flowguard_active = False
            msg = msg or "FlowGuard monitoring with encoder deactivated"

        if msg:
            self.mmu.log_info(msg)


    def adjust_filament_tension(self, use_gear_motor=True, max_move=None):
        """
        Relax the filament tension, preferring proportional control if available else sync-feedback sensor switches.
        By default uses gear stepper to achive the result but optionally can use just extruder stepper for
        extruder entry check using compression sensor 'max_move' is advisory maximum travel distance
        Returns distance of the correction move and whether operation was successful (or None if not performed)
        """
        if not self.mmu_unit.has_buffer(): return 0.0, None

        has_tension, has_compression, has_proportional = self.get_active_sensors()
        max_move = max_move or self.mmu_unit.buffer.buffer_maxrange

        if has_proportional:
            return self._adjust_filament_tension_proportional() # Doesn't yet support extruder stepper or max_move parameter

        if has_tension or has_compression:
            return self._adjust_filament_tension_switch(use_gear_motor=use_gear_motor, max_move=max_move)

        # All sensors must be disabled...
        return 0.0, None


    def wipe_telemetry_logs(self):
        """
        Called to wipe any sync debug files on print start
        """
        if not self.mmu_unit.has_buffer(): return

        for gate in range(self.mmu.num_gates):
            log_path = self._telemetry_log_path(gate)

            # Can't wipe if already synced and active
            if gate != self.mmu.gate_selected or not self.active:
                if os.path.exists(log_path):
                    try:
                        os.remove(log_path)
                        self.mmu.log_error("REMOVED log_path=%s" % log_path)
                    except OSError as e:
                        self.mmu.log_debug("Unable to wipe sync feedback debug log: %s" % log_path)


    def get_active_sensors(self):
        """
        Returns tuple of active sync-feedback sensors
        """
        sm = self.mmu.sensor_manager
        has_tension      = sm.has_sensor(SENSOR_TENSION)
        has_compression  = sm.has_sensor(SENSOR_COMPRESSION)
        has_proportional = sm.has_sensor(SENSOR_PROPORTIONAL)
        return has_tension, has_compression, has_proportional


    def get_status(self, eventtime=None):

        # Buffer controlled sync feedback
        if self.mmu_unit.has_buffer() and self.ctrl:
            if self.mmu_unit.has_encoder():
                self.flowguard_status['encoder_mode'] = self.p.flowguard_encoder_mode # Ok to mutate status
            return {
                'sync_feedback_state': self.get_sync_feedback_string(),
                'sync_feedback_enabled': self.is_enabled(),
                'sync_feedback_bias_raw': self._get_sync_bias_raw(),
                'sync_feedback_bias_modelled': self._get_sync_bias_modelled(),
                'sync_feedback_flow_rate': self.flow_rate,
                'flowguard': self.flowguard_status,
            }

        # Encoder flowguard only
        if self.mmu_unit.has_encoder():
            return {
                'flowguard': {
                    'active': self.flowguard_active,
                    'enabled': self.p.flowguard_enabled,
                    'encoder_mode': self.p.flowguard_encoder_mode,
                }
            }

        return {}


    #
    # Internal implementation --------------------------------------------------
    #

    def _telemetry_log_path(self, gate=None):
        if gate is None: gate = self.mmu.gate_selected

        logfile_path = self.mmu.printer.start_args['log_file']
        dirname = os.path.dirname(logfile_path)

        if not dirname:
            dirname = "/tmp"

        return os.path.join(dirname, 'sync_%d.jsonl' % gate)


    def _handle_mmu_synced(self, eventtime=None):
        """
        Event indicating that gear stepper is now synced with extruder
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not self.mmu.is_enabled: return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Synced MMU to extruder%s" % (" (sync feedback activated)" if self.p.sync_feedback_enabled else "")
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if self.active: return

        # Enable sync feedback
        self.active = True
        self.new_autotuned_rd = None

        # Throw away current autotune info and reset rd
        self._reset_controller(eventtime)

        # Turn on extruder movement events
        self.mmu_unit.extruder_monitor().register_callback(self._handle_extruder_movement, self.p.sync_feedback_extrude_threshold)


    def _handle_mmu_unsynced(self, eventtime=None):
        """
        Event indicating that gear stepper has been unsynced from extruder
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.p.sync_feedback_enabled else "")
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if not self.active: return

        # Deactivate sync feedback
        self.active = False

        if self.new_autotuned_rd is not None:
            self.mmu_unit.calibrator.note_rd_telemetry(self.mmu.gate_selected, self.new_autotuned_rd)

        # Restore default (last tuned) rotation distance in case it wasn't "autotune-saved" above
        self.mmu_unit.calibrator.restore_gear_rd()

        # Optional but let's turn off extruder movement events
        self.mmu_unit.extruder_monitor().remove_callback(self._handle_extruder_movement)


    def _handle_extruder_movement(self, eventtime, move):
        """
        Event call when extruder has moved more than threshold. This also allows for
        periodic rotation_distance adjustment, autotune and flowguard checking
        """
        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        self.mmu.log_trace("MmuSyncFeedback: Extruder movement event, move=%.1f" % move)

        state = self._get_sensor_state()
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)


    def _handle_sync_feedback(self, eventtime, state):
        """
        Event call when sync-feedback discrete state changes.
        'state' should be -1 (tension), 0 (neutral), 1 (compressed)
        or can be a proportional float value between -1.0 and 1.0
        """
        # Ignore event if not for this unit
        if not self.mmu_unit.manages_gate(self.mmu.gate_selected): return

        if not (self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedback: Sync state changed to %s" % (self.get_sync_feedback_string(state))
        if self.mmu_unit.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        move = self.mmu_unit.extruder_monitor().get_and_reset_accumulated(self._handle_extruder_movement)
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)


    def _process_status(self, eventtime, status):
        """
        Common logic to process the rotation distance recommendations of the sync controller
        """
        output = status['output']

        # Handle estimated sensor position
        self.estimated_state = output['sensor_ui']

        # Handle flowguard trip
        self.flowguard_status = dict(output['flowguard'])
        self.flowguard_status['enabled'] = bool(self.p.flowguard_enabled)
        f_trigger = self.flowguard_status.get('trigger', None)
        f_reason = self.flowguard_status.get('reason', "")
        if f_trigger:
            if self.p.flowguard_enabled and self.flowguard_active:
                self.mmu.log_error("FlowGuard detected a %s.\nReason for trip: %s" % (f_trigger, f_reason))

                # Pick most appropriate sensor to assign event to (primariliy for optics)
                has_tension, has_compression, has_proportional = self.get_active_sensors()

                if has_proportional:
                    sensor_key = SENSOR_PROPORTIONAL
                elif has_compression and not has_tension:
                    sensor_key = SENSOR_COMPRESSION
                elif has_tension and not has_compression:
                    sensor_key = SENSOR_TENSION
                elif f_trigger == "clog":
                    sensor_key = SENSOR_COMPRESSION
                else: # "tangle"
                    sensor_key = SENSOR_TENSION
                sm = self.mmu.sensor_manager
                sensor = sm.get_sensor_obj(sensor_key)

                if sensor is not None:
                    sensor.runout_helper.note_clog_tangle(f_trigger)
                self.deactivate_flowguard(eventtime)
            else:
                self.mmu.log_debug("FlowGuard detected a %s, but handling is disabled.\nReason for trip: %s" % (f_trigger, f_reason))
                self.ctrl.flowguard.reset() # Prevent repetitive messages

        # Handle new autotune suggestions
        autotune = output['autotune']
        rd = autotune.get('rd', None)
        note = autotune.get('note', None)
        save = autotune.get('save', None)
        if rd is not None:
            msg = "MmuSyncFeedback: Autotune suggested new operational reference rd: %.4f\n%s" % (rd, note)
            self.new_autotuned_rd = rd
            self.mmu.log_debug(msg)

        # Always update instaneous gear stepper rotation_distance
        rd_current, rd_prev, rd_tuned = output['rd_current'], output['rd_prev'], output['rd_tuned']
        if rd_current != rd_prev:
            self.mmu.log_debug("MmuSyncFeedback: Altered rotation distance for gate %d from %.4f to %.4f" % (self.mmu.gate_selected, rd_prev, rd_current))
            self.mmu_unit.calibrator.apply_gear_rd(rd_current)

        # Proportional sensor (with autotune) allows for estimation of flow rate!
        if self.mmu.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            # if rd_current > rd_true then flowrate must be reduced
            self.flow_rate = round(min(1.0, (rd_tuned / rd_current)) * 100., 2)


    def _reset_controller(self, eventtime, hard_reset=True):
        """
        hard_reset: Completely reset sync-feedback: throw away autotune info, reset rd to
                    last calibrated value. Typically called when handling sync but also can
                    be explicitly called but MMU_SYNC_FEEDBACK command
        soft_reset: Rebase sync-feedback to last autotuned value. Typically called when
                    resuming flowguard (after some activity we want to exclude from tuning)
        """
        # Allow dynamic changing of effective "sensor type" based on currently enabled sensors
        self.ctrl.cfg.sensor_type = self._get_sensor_type()

        # Reset controller with initial rd and sensor reading (will also reset flowguard and autotune on hard_reset)
        starting_state = self._get_sensor_state()
        self.estimated_state = starting_state
        if hard_reset:
            rd_start = self.mmu_unit.calibrator.get_gear_rd()
        else:
            rd_start = self.ctrl.autotune.get_rec_rd()
        status = self.ctrl.reset(eventtime, rd_start, starting_state, log_file=self._telemetry_log_path(), hard_reset=hard_reset)
        self._process_status(eventtime, status) # May adjust rotation_distance


    def _init_controller(self):
        """
        The controller logic is in a completely standalone module for simulation
        and debugging purposes so instantiate it here with current config
        Returns: the SyncController object
        """
        rd_start = self.mmu_unit.calibrator.get_gear_rd(self.mmu_unit.first_gate) # Any RD is ok for startup
        cfg = SyncControllerConfig(
            log_sync = bool(self.p.sync_feedback_debug_log),
            buffer_range_mm = self.mmu_unit.buffer.buffer_range,
            buffer_max_range_mm = self.mmu_unit.buffer.buffer_maxrange,
            sensor_type = self._get_sensor_type(),
            use_twolevel_for_type_p = self.p.sync_feedback_force_twolevel,
            rd_start = rd_start,
            flowguard_relief_mm = self.p.flowguard_max_relief,
        )
        self.ctrl = SyncController(cfg)
        return self.ctrl


    def config_flowguard_feature(self, enable):
        if not self.mmu_unit.has_buffer(): return
        if enable:
            self.mmu.log_info("FlowGuard monitoring feature %senabled" % ("already " if self.p.flowguard_enabled else ""))
            if not self.p.flowguard_enabled:
                self.p.flowguard_enabled = True
                if self.ctrl:
                    self.ctrl.flowguard.reset()
        else:
            self.mmu.log_info("FlowGuard monitoring feature %sdisabled" % ("already " if not self.p.flowguard_enabled else ""))
            self.p.flowguard_enabled = False


    def _get_sync_bias_raw(self):
        return float(self._get_sensor_state())


    def _get_sync_bias_modelled(self):
        if self.mmu.is_enabled and self.p.sync_feedback_enabled and self.active and self.mmu.is_printing():
            # This is a better representation for UI when the controller is active
            return self.estimated_state
        else:
            # Otherwise return the real state
            return float(self._get_sensor_state())


    def _get_sensor_state(self):
        """
        Get current tension state based on current sensor feedback.
        Returns float in range [-1.0 .. 1.0] for proportional, {-1, 0, 1) for switch
        """
        sm = self.mmu.sensor_manager
        has_proportional   = sm.has_sensor(SENSOR_PROPORTIONAL)
        if has_proportional:
            sensor = sm.get_sensor_obj(SENSOR_PROPORTIONAL)
            return sensor.get_status(0).get('value', 0.)

        tension_active     = sm.check_sensor(SENSOR_TENSION)
        compression_active = sm.check_sensor(SENSOR_COMPRESSION)

        if tension_active == compression_active:
            ss = SF_STATE_NEUTRAL
        elif compression_active:
            ss = SF_STATE_COMPRESSION
        elif tension_active:
            ss = SF_STATE_TENSION
        else:
            ss = SF_STATE_NEUTRAL
        return ss


    def _get_sensor_type(self):
        """
        Return symbolic sensor type based on current active sensors
          "P" => proportional z ∈ [-1, +1]; enables EKF
          "D" => discrete dual-switch z ∈ {-1,0,+1}; Optional EKF
          "CO" => compression-only switch z ∈ {0,+1}
          "TO" => tension_only switch z ∈ {-1,0}
        """
        has_tension, has_compression, has_proportional = self.get_active_sensors()
        return (
            "P" if has_proportional
            else "D" if has_compression and has_tension
            else "CO" if has_compression
            else "TO" if has_tension
            else "Unknown"
        )


    def _adjust_filament_tension_switch(self, use_gear_motor=True, max_move=None):
        """
        Helper to relax filament tension using the sync-feedback buffer. This can be performed either with the
        gear motor (default) or extruder motor (which is also good as an extruder loading check)
        Returns distance moved and whether operation was successful and neutral was found (or None if not performed)
        """
        fhomed = None
        actual = 0.

        state = self._get_sensor_state()
        if state == SF_STATE_NEUTRAL:
            return actual, True

        has_tension, has_compression, _ = self.get_active_sensors()
        if not (has_tension or has_compression):
            self.mmu.log_debug("No active sync feedback sensors; cannot adjust filament tension")
            return actual, fhomed

        max_move = max_move or self.mmu_unit.buffer.buffer_maxrange
        self.mmu.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_move)

        motor = "gear" if use_gear_motor else "extruder"
        speed = min(self.mmu_unit.p.gear_homing_speed, self.mmu.p.extruder_homing_speed) # Keep this tension adjustment slow

        # Determine direction based on state and motor type
        # Note that if buffer_range is 0, it implies
        # special case where neutral point overlaps both sensors
        if state == SF_STATE_COMPRESSION:
            self.mmu.log_debug("Relaxing filament compression")
            direction = -1 if use_gear_motor else 1

            if self.mmu_unit.buffer.buffer_range == 0:
                msg = "Homing to tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = 1
            elif has_compression:
                msg = "Reverse homing off compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = -1
            else:
                msg = "Homing to tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = 1

        else:
            # Tension state
            self.mmu.log_debug("Relaxing filament tension")
            direction = 1 if use_gear_motor else -1

            if self.mmu_unit.buffer.buffer_range == 0:
                msg = "Homing to compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = 1
            elif has_tension:
                msg = "Reverse homing off tension sensor"
                sensor = SENSOR_TENSION
                homing_dir = -1
            else:
                msg = "Homing to compression sensor"
                sensor = SENSOR_COMPRESSION
                homing_dir = 1

        actual,fhomed,_,_ = self.mmu.move_filament(
            msg,
            max_move * direction,
            speed=speed,
            motor=motor,
            homing_move=homing_dir,
            endstop_name=sensor,
        )

        if fhomed and self.mmu_unit.buffer.buffer_range != 0:
            if use_gear_motor:
                # Move just a little more to find perfect neutral spot between sensors
                _,_,_,_ = self.mmu.move_filament("Centering sync feedback buffer", (self.mmu_unit.buffer.buffer_range * direction) / 2.)
        else:
            self.mmu.log_debug("Failed to reach neutral filament tension after moving %.1fmm" % max_move)

        return actual, fhomed


    def _adjust_filament_tension_proportional(self):
        """
        Helper to relax filament tension using the proportional sync-feedback buffer.
        Returns: actual distance moved (mm), success bool
        """

        # nudge_mm:     per-move adjustment distance in mm (small feed or retract)
        # neutral_band: absolute value of proportional sensor reading considered "neutral".
        #               This can be loosely interpreted as a % over the max range of detection of the sensor.
        #               For example for a sensor with 14mm range, a 0.15 tolerance is approx 1.4mm either side of centre.
        # settle_time:  delay between moves to allow sensor feedback to update
        # timeout_s:    hard stop to avoid hanging if the sensor never clears
        neutral_band = 0.1
        settle_time  = 0.1
        timeout_s    = 10.0

        # Wait for all moves to clear
        self.mmu.movequeue_wait()

        # sanity-check parameters before doing anything
        # neutral band needs to have a non zero and non trivial value. Enforce 5% (0.05)
        # as the lower limit of acceptable neutral band tolerance.
        if neutral_band < 0.05:
            neutral_band = 0.05

        # maxrange is full end-to-end sensor span; use half as the per-side budget from neutral to either end
        maxrange_span_mm = float(self.mmu_unit.buffer.buffer_maxrange)
        if maxrange_span_mm <= 0.0:
            self.mmu.log_debug("Proportional adjust skipped: buffer maxrange <= 0")
            return 0., False
        per_side_budget_mm = 0.5 * maxrange_span_mm
        nudge_mm = per_side_budget_mm * neutral_band

        # Cap total nudge iterations to stay within the overall sensor range
        max_steps = int(math.ceil(maxrange_span_mm / nudge_mm))

        moved_total_mm   = 0.0  # total net distance moved during this adjustment
        moved_nudges_mm  = 0.0  # sum of all nudge moves
        moved_initial_mm = 0.0  # size of the initial proportional move (if any)
        steps            = 0    # total moves performed
        t_start          = self.mmu.reactor.monotonic()

        # --- Initial proportional correction ---
        # Negative sensor state = tension -> feed filament. positive sensor state = compression -> retract filament
        prop_state = self._get_sensor_state() # [-1..+1], 0 ≈ neutral
        if abs(prop_state) > neutral_band:
            # Initial move distance as a proportion to how off centre we are based on the sensor readings.
            # this will get the sensor close but likely will need a few fine adjustments (nudges) to get it
            # within the centre range depending on how large the bowden tube slack is.
            initial_move_mm = -prop_state * per_side_budget_mm
            if abs(initial_move_mm) >= nudge_mm:
                self.mmu.move_filament(
                    "Proportional initial adjust - extruder load",
                    initial_move_mm, motor="gear", wait=True
                )
                moved_total_mm += initial_move_mm
                moved_initial_mm = initial_move_mm
                steps += 1
                try:
                    self.mmu.reactor.pause(settle_time)
                except Exception:
                    time.sleep(settle_time)

        # --- Check proportional sensor state after initial move and return if within neutral deadband ---
        prop_state = self._get_sensor_state()
        if abs(prop_state) <= neutral_band:
            self.mmu.log_info(
                "Proportional adjust: neutral after initial "
                "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=yes)" %
                (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
            )
            return moved_total_mm, True

        # --- Fine adjustment loop (nudges) ---
        while abs(moved_total_mm) < maxrange_span_mm and steps < max_steps:
            prop_state = self._get_sensor_state()
            # timeout safety: avoid hanging if the sensor never clears
            if (self.mmu.reactor.monotonic() - t_start) > timeout_s:
                self.mmu.log_info(
                    "Proportional adjust: timed out "
                    "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f)" %
                    (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
                )
                return moved_total_mm, False

            if abs(prop_state) <= neutral_band:
                # confirm neutral after a short wait
                try:
                    self.mmu.reactor.pause(settle_time)
                except Exception:
                    time.sleep(settle_time)
                prop_state = self._get_sensor_state()
                if abs(prop_state) <= neutral_band:
                    break

            # Direction: tension -> feed forward; compression -> retract
            nudge_move_mm = nudge_mm if prop_state < 0.0 else -nudge_mm
            # don't exceed the end to end sensor span (maxrange_span_mm). Serves as "ultimate" failsafe.
            if abs(moved_total_mm + nudge_move_mm) >= maxrange_span_mm:
                self.mmu.log_info(
                    "Proportional adjust: aborted (exceeded buffer) "
                    "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f)" %
                    (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
                )
                return moved_total_mm, False

            self.mmu.move_filament(
                "Proportional adjust - extruder load",
                nudge_move_mm, motor="gear", wait=True
            )
            moved_total_mm  += nudge_move_mm
            moved_nudges_mm += nudge_move_mm
            steps           += 1
            try:
                self.mmu.reactor.pause(settle_time)
            except Exception:
                time.sleep(settle_time)

        # Final check
        final_state = self._get_sensor_state()
        success = abs(final_state) <= neutral_band
        self.mmu.log_info(
            "Proportional adjust: complete "
            "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=%s)" %
            (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, final_state, "yes" if success else "no")
        )
        return moved_total_mm, success
