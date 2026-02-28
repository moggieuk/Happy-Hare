# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle sync-feedback and adjustment of gear rotation distance
#       to keep MMU in sync with extruder. It also implements clog and tangle detection
#       if a proportional filament pressure sensor is installed.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, math, time, os

# Happy Hare imports

# MMU subcomponent clases
from .mmu_sync_controller  import SyncControllerConfig, SyncController
from .mmu_extruder_monitor import ExtruderMonitor
from .mmu_shared           import MmuError

class MmuSyncFeedbackManager:

    FEEDBACK_INTERVAL     = 0.25    # How often to check extruder movement
    SIGNIFICANT_MOVEMENT  = 5.      # Min extruder movement to trigger direction change (don't want small retracts to trigger)
    MOVEMENT_THRESHOLD    = 50      # Default extruder movement threshold trigger when stuck in one state
    MULTIPLIER_RUNAWAY    = 0.25    # Used to limit range in runaway conditions (25%)
    MULTIPLIER_WHEN_STUCK = 0.01    # Used to "widen" clamp if we are not getting to neutral soon enough (1%)
    MULTIPLIER_WHEN_GOOD  = 0.005   # Used to move off trigger when tuned rotation distance has been found (0.5%)
    AUTOTUNE_TOLERANCE    = 0.0025  # The desired accuracy of autotuned rotation distance (0.25% or 2.5mm per m)

    SYNC_STATE_NEUTRAL    = 0
    SYNC_STATE_COMPRESSED = 1
    SYNC_STATE_EXPANDED   = -1

    # proportional tension / compression control tunables
    RDD_THRESHOLD         = 1e-4     # Min Rotation Distance delta to trigger application of it.
    SIDE_THRESHOLD        = 1e-3     # Magnitude of side motion before considering state as tension/compression. Units are arbitrary
    								 # and loosely linked to the distance the magnet travels over the hall effect sensor.

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu.managers.append(self)

        self.state = 0.             # 0 = Neutral
        self.extruder_direction = 0 # 0 = Extruder not moving
        self.active = False         # Actively operating?
        self.last_recorded_extruder_position = None
        self._last_state_side = 0   # track sign of proportional state to detect transitions
        self._last_watchdog_reset = 0.0
        self._rd_applied = None     # track live applied RD so UI can show true adjustment
        self._proportional_seen = False  # true once we receive at least one proportional event

        # Process config
        self.sync_feedback_enabled = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range = self.mmu.config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange = self.mmu.config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_multiplier_high = self.mmu.config.getfloat('sync_multiplier_high', 1.05, minval=1., maxval=2.)
        self.sync_multiplier_low = self.mmu.config.getfloat('sync_multiplier_low', 0.95, minval=0.5, maxval=1.)
        # Make direction detection threshold configurable. This is the min extruder movement to trigger direction change
        self.sync_movement_threshold = self.mmu.config.getfloat('sync_movement_threshold',self.SIGNIFICANT_MOVEMENT,above=0.5)

        # EndGuard (proportional near-end watchdog)
        self.sync_endguard_enabled  = self.mmu.config.getint('sync_endguard_enabled', 0, minval=0, maxval=1)
        self.sync_endguard_band     = self.mmu.config.getfloat('sync_endguard_band', 0.80, minval=0.55, maxval=1.00)
        self.sync_endguard_distance_mm  = self.mmu.config.getfloat('sync_endguard_distance_mm', 6.0, minval=1.0)
        # G-code to run when EndGuard triggers.
        self._endguard_forward_mm   = 0.0
        self._endguard_triggered    = False
        self.endguard_active        = 0  # runtime latch to activate/deactivate endguard during the load/unload process

        # Setup events for managing motor synchronization
        self.mmu.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
        self.mmu.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
        self.mmu.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)

        # Initial flowguard status
        self.flowguard_status = {'trigger': '', 'reason': '', 'level': 0.0, 'max_clog': 0.0, 'max_tangle': 0.0, 'active': False, 'enabled': bool(self.flowguard_enabled)}

        # Register GCODE commands ---------------------------------------------------------------------------

        self.mmu.gcode.register_command('MMU_SYNC_FEEDBACK', self.cmd_MMU_SYNC_FEEDBACK, desc=self.cmd_MMU_SYNC_FEEDBACK_help)
        self.mmu.gcode.register_command('MMU_FLOWGUARD',  self.cmd_MMU_FLOWGUARD, desc=self.cmd_MMU_FLOWGUARD_help)

        self.extruder_monitor = ExtruderMonitor(mmu)


    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self.rd_clamps = {}         # Autotune - Array of [slow_rd, current_rd, fast_rd, tuned_rd, original_rd] indexed by gate
        self._reset_extruder_watchdog()
        self._reset_endguard()

    def set_test_config(self, gcmd):
        self.sync_feedback_enabled = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled)
        self.sync_feedback_buffer_range = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
        self.sync_feedback_buffer_maxrange = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)
        self.sync_movement_threshold = gcmd.get_float('SYNC_MOVEMENT_THRESHOLD', self.sync_movement_threshold, minval=0.5)
        self.sync_endguard_enabled = gcmd.get_int('SYNC_ENDGUARD_ENABLED', self.sync_endguard_enabled, minval=0, maxval=1)
        self.sync_endguard_band = gcmd.get_float('SYNC_ENDGUARD_BAND', self.sync_endguard_band, minval=0.55, maxval=1.00)
        self.sync_endguard_distance_mm = gcmd.get_float('SYNC_ENDGUARD_DISTANCE_MM', self.sync_endguard_distance_mm, minval=1.0)

    def get_test_config(self):
        msg = "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
        msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
        msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
        msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
        msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
        msg += "\nsync_movement_threshold = %.2f" % self.sync_movement_threshold
        msg += "\nsync_endguard_enabled = %d" % self.sync_endguard_enabled
        msg += "\nsync_endguard_band = %.2f" % self.sync_endguard_band
        msg += "\nsync_endguard_distance_mm = %.1f" % self.sync_endguard_distance_mm
        return msg


    def check_test_config(self, param):
        return vars(self).get(param) is None

    #
    # Sync feedback manager public access...
    #

    def set_default_rd(self):
        """
        Ensure correct starting rotation distance
        """
        gate = self.mmu.gate_selected
        if gate < 0: return

            self._reset_extruder_watchdog()
            self._reset_endguard()
            self._reset_current_sync_state()
            if self.sync_feedback_enabled:
                self.mmu.log_debug("MmuSyncFeedbackManager: Set initial sync feedback state to: %s" % self.get_sync_feedback_string(detail=True))

            # Always set initial rotation distance (may have been previously autotuned)
            if not self._adjust_gear_rotation_distance():
                rd = self.rd_clamps[gate][1]
                self._rd_applied = rd
                self.mmu.set_rotation_distance(rd)
        else:
            self._reset_gear_rotation_distance()

    def is_enabled(self):
        """
        This is whether the user has enabled the sync-feedback feature (the "big" switch)
        """
        return self.sync_feedback_enabled


    def is_active(self):
        """
        Returns whether the sync-feedback is currently active (when synced)
        """
        return self.active


    def get_sync_feedback_string(self, state=None, detail=False):
        if state is None:
            state = self._get_sensor_state()
        if (self.mmu.is_enabled and self.sync_feedback_enabled and self.active) or detail:
            # Polarity varies slightly between modes on proportional sensor so ask controller
            polarity = self.ctrl.polarity(state)
            return 'compressed' if polarity > 0 else 'tension' if polarity < 0 else 'neutral'
        elif self.mmu.is_enabled and self.sync_feedback_enabled:
            return "inactive"
        return "disabled"

    # End guard enable/disable/reset hooks
    def enable_endguard(self, reason=None):
        self.set_endguard_active(True, reason)

    def disable_endguard(self, reason=None):
        self.set_endguard_active(False, reason)

    #
    # Internal implementation --------------------------------------------------
    #

    def _telemetry_log_path(self, gate=None):
        if gate is None: gate = self.mmu.gate_selected

        logfile_path = self.mmu.printer.start_args['log_file']
        dirname = os.path.dirname(logfile_path)

        if not dirname:
            dirname = "/tmp"

            # Have we changed direction?
            if abs(pos - self.last_recorded_extruder_position) > self.sync_movement_threshold:
                prev_direction = self.extruder_direction
                self.extruder_direction = (
                    self.mmu.DIRECTION_LOAD if pos > self.last_recorded_extruder_position
                    else self.mmu.DIRECTION_UNLOAD if pos < self.last_recorded_extruder_position
                    else 0
                )
                if self.extruder_direction != prev_direction:
                    self._notify_direction_change(prev_direction, self.extruder_direction)
                    # Feed EndGuard with the positive chunk consumed by the direction-change path
                    if pos > self.last_recorded_extruder_position:
                        self._notify_endguard_forward_progress(pos - self.last_recorded_extruder_position)
                    self.last_recorded_extruder_position = pos

            if (pos - self.last_recorded_extruder_position) >= self.sync_movement_threshold:
                # Feed EndGuard on forward-only chunks as well
                self._notify_endguard_forward_progress(pos - self.last_recorded_extruder_position)
                # Ensure we are given periodic notifications to aid autotuning
                self._notify_hit_movement_marker(pos - self.last_recorded_extruder_position)
                self.last_recorded_extruder_position = pos # Move marker


        return eventtime + self.FEEDBACK_INTERVAL

    # Event indicating that gear stepper is now synced with extruder
    def _handle_mmu_synced(self):
        if not self.mmu.is_enabled: return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedbackManager: Synced MMU to extruder%s" % (" (sync feedback activated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if not self.active:
            # Enable sync feedback
            self.active = True
            self._reset_extruder_watchdog()
            self._reset_endguard()
            self._reset_current_sync_state()
            self._adjust_gear_rotation_distance()
            self.mmu.reactor.update_timer(self.extruder_watchdog_timer, self.mmu.reactor.NOW)

        msg = "MmuSyncFeedbackManager: Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if self.active:
            # Disable sync feedback
            self.active = False
            self.mmu.reactor.update_timer(self.extruder_watchdog_timer, self.mmu.reactor.NEVER)
            self.state = self.SYNC_STATE_NEUTRAL
            self._reset_endguard()
            self._reset_gear_rotation_distance()

    def _handle_sync_feedback(self, eventtime, state):
        if not self.mmu.is_enabled: return
        if abs(state) <= 1:
            old_state = self.state
            self.state = float(state)
            self._proportional_seen = True
            self.mmu.log_trace(
                "MmuSyncFeedbackManager(%s): Got sync force feedback update. State: %s (%s)" % (
                    "active" if self.sync_feedback_enabled and self.active else "inactive",
                    self.get_sync_feedback_string(detail=True),
                    float(state)
                )
            )
            # IMPORTANT: Do NOT reset the extruder watchdog every proportional tick.
            # Only reset on *side* transitions (tension<->compression) so ΔE can accumulate.
            def _side(v):
                return 0 if abs(v) < self.SIDE_THRESHOLD else (1 if v > 0.0 else -1)
            new_side = _side(self.state)
            if new_side != self._last_state_side:
                self._reset_extruder_watchdog()
                self._reset_endguard()
                self._last_state_side = new_side

            if self.sync_feedback_enabled and self.active:
                # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
                # Note that proportional feedback sensors do not have tension switch so clamp logic will be bypassed
                has_dual_sensors = (
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
                )
                if state != old_state and has_dual_sensors and self.mmu.autotune_rotation_distance:
                    self._adjust_clamps(state, old_state)
                self._adjust_gear_rotation_distance()
        else:
            self.mmu.log_info(msg)

        move = self.extruder_monitor.get_and_reset_accumulated(self._handle_extruder_movement)
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)

    # This signifies that the extruder has changed direction
    def _notify_direction_change(self, last_direction, new_direction):
        dir_str = lambda d: 'extrude' if d == self.mmu.DIRECTION_LOAD else 'retract' if d == self.mmu.DIRECTION_UNLOAD else 'static'
        self.mmu.log_debug(
            "MmuSyncFeedbackManager: Sync direction changed from %s to %s" % (
                dir_str(last_direction),
                dir_str(new_direction)
            )
        )
        self._adjust_gear_rotation_distance()

    def _process_status(self, eventtime, status):
        """
        Common logic to process the rotation distance recommendations of the sync controller
        """
        output = status['output']

        # Handle estimated sensor position
        self.estimated_state = output['sensor_ui']

        # Handle flowguard trip
        self.flowguard_status = dict(output['flowguard'])
        self.flowguard_status['enabled'] = bool(self.flowguard_enabled)
        f_trigger = self.flowguard_status.get('trigger', None)
        f_reason = self.flowguard_status.get('reason', "")
        if f_trigger:
            if self.flowguard_enabled and self.flowguard_active:
                self.mmu.log_error("FlowGuard detected a %s.\nReason for trip: %s" % (f_trigger, f_reason))

                # Pick most appropriate sensor to assign event to (primariliy for optics)
                has_tension, has_compression, has_proportional = self.get_active_sensors()

                if has_proportional:
                    sensor_key = self.mmu.SENSOR_PROPORTIONAL
                elif has_compression and not has_tension:
                    sensor_key = self.mmu.SENSOR_COMPRESSION
                elif has_tension and not has_compression:
                    sensor_key = self.mmu.SENSOR_TENSION
                elif f_trigger == "clog":
                    sensor_key = self.mmu.SENSOR_COMPRESSION
                else: # "tangle"
                    sensor_key = self.mmu.SENSOR_TENSION
                sm = self.mmu.sensor_manager
                sensor = sm.sensors.get(sensor_key)

        elif self.state == self.SYNC_STATE_NEUTRAL:
            self.mmu.log_trace("MmuSyncFeedbackManager: Ignoring extruder move marker trigger because in neutral state")
            return # Do nothing, we want to stay in this state

        # No need to update the same rd value
        if not math.isclose(rd_clamp[1], old_clamp[1]):
            self._adjust_gear_rotation_distance()


    # Called to use binary search algorithm to slowly reduce clamping range to minimize switching
    # Note that this will converge on new calibrated value and update if autotune options is set
    def _adjust_clamps(self, state, old_state):
        if state == old_state: return # Shouldn't happen
        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        old_clamp = rd_clamp.copy()
        tuned_rd = None

        def check_if_tuned(rd_clamp):
            if math.isclose(rd_clamp[0], rd_clamp[2], rel_tol=self.AUTOTUNE_TOLERANCE):
                tuned_rd = (rd_clamp[0] + rd_clamp[2]) / 2.
                if not rd_clamp[3] or not math.isclose(tuned_rd, rd_clamp[3]):
                    # New tuned setting
                    rd_clamp[3] = tuned_rd
                    self.mmu.log_always(
                        "MmuSyncFeedbackManager: New autotuned rotation_distance for gate %d: %.4f" % (
                            self.mmu.gate_selected,
                            rd_clamp[3]
                        )
                    )
                    if self.mmu.autotune_rotation_distance:
                        self.mmu.save_rotation_distance(self.mmu.gate_selected, tuned_rd)
                return tuned_rd
            return None

        if state == self.SYNC_STATE_COMPRESSED:  # Transition from neutral --> compressed
            # Use current rotation distance to clamp fast setting
            rd_clamp[2] = rd_clamp[1]
            self.mmu.log_trace(
                "MmuSyncFeedbackManager: Neutral -> Compressed. Going too fast. "
                "Adjusted fast clamp (%.4f -> %.4f)" % (
                    old_clamp[2],
                    rd_clamp[2]
                )
            )

            # If we have good calibration, adjust a little to make move off trigger
            tuned_rd = check_if_tuned(rd_clamp)
            if tuned_rd:
                rd_clamp[0] *= (1 + self.MULTIPLIER_WHEN_GOOD)
                self.mmu.log_trace(
                    "MmuSyncFeedbackManager: Have good rotation_distance, adjusting slow clamp slightly "
                    "(%.4f -> %.4f) to move off trigger" % (
                        old_clamp[0],
                        rd_clamp[0]
                    )
                )
            rd_clamp[1] = rd_clamp[0]  # Set current rd to slow setting

        elif state == self.SYNC_STATE_EXPANDED:  # Transition from neutral --> tension
            # Use current rotation distance to clamp slow setting
            rd_clamp[0] = rd_clamp[1]
            self.mmu.log_trace(
                "MmuSyncFeedbackManager: Neutral -> Tension. Going too slow. "
                "Adjusted slow clamp (%.4f -> %.4f)" % (
                    old_clamp[0],
                    rd_clamp[0]
                )
            )

            # If we have good calibration, adjust a little to make move off trigger
            tuned_rd = check_if_tuned(rd_clamp)
            if tuned_rd:
                rd_clamp[2] *= (1 - self.MULTIPLIER_WHEN_GOOD)
                self.mmu.log_trace(
                    "MmuSyncFeedbackManager: Have good rotation_distance, adjusting fast clamp slightly "
                    "(%.4f -> %.4f) to move off trigger" % (
                        old_clamp[2],
                        rd_clamp[2]
                    )
                )
            rd_clamp[1] = rd_clamp[2] # Set current rd to fast setting

        elif state == self.SYNC_STATE_NEUTRAL:
            # Test mid point of the clamping range
            rd_clamp[1] = (rd_clamp[0] + rd_clamp[2]) / 2.
            self.mmu.log_trace(
                "MmuSyncFeedbackManager: %s -> Neutral. Averaging default rotation_distance (%.4f -> %.4f)" % (
                    self.get_sync_feedback_string(old_state),
                    old_clamp[1],
                    rd_clamp[1]
                )
            )
            _ = check_if_tuned(rd_clamp)

        # Paranoia - handle unexpected inversion condition
        if rd_clamp[2] > rd_clamp[0]:
            self.mmu.log_warning("Inverted rotation_distance clamping range! Fixing...")
            rd_clamp[0], rd_clamp[2] = rd_clamp[2], rd_clamp[0]

        # Limit runaway conditions (perhaps could occur during a long clog?)
        rd_clamp[0] = min(rd_clamp[0], rd_clamp[4] * (1 + self.MULTIPLIER_RUNAWAY))
        rd_clamp[2] = max(rd_clamp[2], rd_clamp[4] * (1 - self.MULTIPLIER_RUNAWAY))

    # Update gear rotation_distance based on current state. This correctly handled
    # the direction of movement (although it will almost always be extruding)
    # Return True if rotation_distance set/reset
    def _adjust_gear_rotation_distance(self):
        self.mmu.log_trace( "MmuSyncFeedbackManager: adjust RD? enabled=%s active=%s state=%.3f dir=%d" % (self.sync_feedback_enabled, self.active, self.state, self.extruder_direction) )

        if not self.sync_feedback_enabled or not self.active: return False

        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        if self.state == self.SYNC_STATE_NEUTRAL or self.extruder_direction == 0:
            rd = rd_clamp[1]
        else:
            go_slower = lambda s, d: abs(s - d) < abs(s + d)
            if go_slower(self.state, self.extruder_direction):
                # Compressed when extruding or tension when retracting, so increase the rotation distance of gear stepper to slow it down
                rd = rd_clamp[0]
                self.mmu.log_trace("MmuSyncFeedbackManager: Slowing gear motor down")
            else:
                # Tension when extruding or compressed when retracting, so decrease the rotation distance of gear stepper to speed it up
                rd = rd_clamp[2]
                self.mmu.log_trace("MmuSyncFeedbackManager: Speeding gear motor up")

        if self._rd_applied is not None and abs(rd - self._rd_applied) < self.RDD_THRESHOLD:
            # No meaningful change; skip logging & write
            return False

        self.mmu.log_debug(
            "MmuSyncFeedbackManager: Gear rotation_distance: %.4f (slow:%.4f, default: %.4f, fast:%.4f)%s" % (
                rd,
                rd_clamp[0],
                rd_clamp[1],
                rd_clamp[2],
                (" tuned: %.4f" % rd_clamp[3]) if rd_clamp[3] else ""
            )
        )
        self._rd_applied = rd
        self.mmu.set_rotation_distance(rd)
        self.mmu.log_debug("Applied RD now: %.4f" % self._rd_applied)
        return True

    # Reset rotation_distance to calibrated value of current gate (not necessarily current value if autotuning)
    def _reset_gear_rotation_distance(self):
        rd = self.mmu.get_rotation_distance(self.mmu.gate_selected)
        self.mmu.log_trace("MmuSyncFeedbackManager: Reset rotation distance to calibrated value (%.4f)" % rd)
        self._rd_applied = rd
        self.mmu.set_rotation_distance(rd)

    def _config_flowguard_feature(self, enable):
        if enable:
            self.mmu.log_info("FlowGuard monitoring feature %senabled" % ("already " if self.flowguard_enabled else ""))
            if not self.flowguard_enabled:
                self.flowguard_enabled = True
                if self.ctrl:
                    self.ctrl.flowguard.reset()
        else:
            self.mmu.log_info("FlowGuard monitoring feature %sdisabled" % ("already " if not self.flowguard_enabled else ""))
            self.flowguard_enabled = False


    def _get_sync_bias_raw(self):
        return float(self._get_sensor_state())


    def _get_sync_bias_modelled(self):
        if self.mmu.is_enabled and self.sync_feedback_enabled and self.active and self.mmu.is_printing():
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
        has_proportional   = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        if has_proportional:
            sensor = sm.sensors.get(self.mmu.SENSOR_PROPORTIONAL)
            return sensor.get_status(0).get('value', 0.)

        tension_active     = sm.check_sensor(self.mmu.SENSOR_TENSION)
        compression_active = sm.check_sensor(self.mmu.SENSOR_COMPRESSION)

        if tension_active == compression_active:
            ss = self.SF_STATE_NEUTRAL
        elif compression_active:
            ss = self.SF_STATE_COMPRESSION
        elif tension_active:
            ss = self.SF_STATE_TENSION
        else:
            ss = self.SF_STATE_NEUTRAL
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
        if state == self.SF_STATE_NEUTRAL:
            return actual, True

        has_tension, has_compression, _ = self.get_active_sensors()
        if not (has_tension or has_compression):
            self.mmu.log_debug("No active sync feedback sensors; cannot adjust filament tension")
            return actual, fhomed

        max_move = max_move or self.sync_feedback_buffer_maxrange
        self.mmu.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_move)

        motor = "gear" if use_gear_motor else "extruder"
        speed = min(self.mmu.gear_homing_speed, self.mmu.extruder_homing_speed) # Keep this tension adjustment slow

        # Determine direction based on state and motor type
        # Note that if sync_feedback_buffer_range is 0, it implies
        # special case where neutral point overlaps both sensors
        if state == self.SF_STATE_COMPRESSION:
            self.mmu.log_debug("Relaxing filament compression")
            direction = -1 if use_gear_motor else 1

            if self.sync_feedback_buffer_range == 0:
                msg = "Homing to tension sensor"
                sensor = self.mmu.SENSOR_TENSION
                homing_dir = 1
            elif has_compression:
                msg = "Reverse homing off compression sensor"
                sensor = self.mmu.SENSOR_COMPRESSION
                homing_dir = -1
            else:
                ss = self.SYNC_STATE_COMPRESSED
        elif has_compression and not has_tension:
            ss = self.SYNC_STATE_COMPRESSED if compression_active else self.SYNC_STATE_EXPANDED
        elif has_tension and not has_compression:
            ss = self.SYNC_STATE_EXPANDED if tension_active else self.SYNC_STATE_COMPRESSED
        else:
            # No switches: fall back to proportional state if we have seen any
            if self._proportional_seen:
                ss = self.SYNC_STATE_NEUTRAL if abs(self.state) < 0.5 else (
                    self.SYNC_STATE_COMPRESSED if self.state > 0 else self.SYNC_STATE_EXPANDED
                )
            else:
                ss = self.SYNC_STATE_NEUTRAL
        self.state = ss
        # Update cached side for later transition detection
        self._last_state_side = 0 if ss == self.SYNC_STATE_NEUTRAL else (1 if ss == self.SYNC_STATE_COMPRESSED else -1)


    # EndGuard implementation (proportional filament pressure sensor clog and tangle detection)

    def set_endguard_active(self, enabled, reason=None):
        # Respect config: if EndGuard is disabled, ignore requests to change state.
        if not getattr(self, "sync_endguard_enabled", 0):
            # Make sure runtime latch reflects disabled state
            self.endguard_active = 0
            try:
                if hasattr(self, "_endguard_arm_timer"):
                    self.mmu.reactor.update_timer(self._endguard_arm_timer, self.mmu.reactor.NEVER)
            except Exception:
                pass
            self._clear_pending_endguard(reason="disabled in config")
            return

        # Reset EndGuard unconditionally. Will also attempt to cancel any pending scheduled
        # pauses (no guarantee that it will succeed before klipper schedules the pause.)
        self._clear_pending_endguard(reason=("arming" if enabled else "disabling"))

        # Cancel any previously scheduled deferred arm
        try:
            if hasattr(self, "_endguard_arm_timer"):
                self.mmu.reactor.update_timer(self._endguard_arm_timer, self.mmu.reactor.NEVER)
        except Exception:
            pass

        if enabled:
            # Rebaseline BEFORE arming; this guarantees the first watchdog tick is a no-op baseline seed.
            self._reset_extruder_watchdog()     # last_recorded_extruder_position = None
            self._reset_current_sync_state()

            # Defer arming by a short reactor delay to avoid same-cycle interleaving
            delay_s = 0.10  # ~100 ms; just enough to skip current event-loop tail

            # Arm endguard in the next reactor tick to ensure any inflight activities are complete (safeguard)
            def _arm(evt):
                self.endguard_active = 1
                try:
                    self.mmu.log_info("EndGuard: enabled (deferred)%s" % ((" (%s)" % reason) if reason else ""))
                except Exception:
                    pass
                return self.mmu.reactor.NEVER

            # Register and schedule the one-shot arm
            self._endguard_arm_timer = self.mmu.reactor.register_timer(_arm)
            now = self.mmu.reactor.monotonic()
            self.mmu.reactor.update_timer(self._endguard_arm_timer, now + delay_s)

            # Update logs
            try:
                self.mmu.log_debug("EndGuard: enable requested; arming in %.2fs%s" %(delay_s, (" (%s)" % reason) if reason else ""))
            except Exception:
                pass

            # Ensure latch remains inactive until the deferred arm fires
            self.endguard_active = 0
        else:
            self.endguard_active = 0

            # Update logs
            try:
                self.mmu.log_info("EndGuard: disabled%s" % ((" (%s)" % reason) if reason else ""))
            except Exception:
                pass


    def _clear_pending_endguard(self, reason=None):
        #  Cancel any scheduled EndGuard action (pause) and optionally reset accumulation.
        #  This does NOT change sync_endguard_enabled/active; it only clears pending/triggered state.
        #  Note that if a pause has been scheduled and executed, this function cannot undo it.
        #  Therefore, make sure endguard is disabled before any operations that may over-drive
        #  the sync feedback sensor!
        #  reason: Optional string appended to the log.
        #  Returns True if something was actually cleared/canceled; False otherwise.

        cleared = False

        # Cancel any scheduled one-shot pause
        try:
            if hasattr(self, "_endguard_timer_handle"):
                self.mmu.reactor.update_timer(self._endguard_timer_handle, self.mmu.reactor.NEVER)
                cleared = True
        except Exception:
            pass

        # Clear pending-action flag
        if getattr(self, "_endguard_action_pending", False):
            try:
                self._endguard_action_pending = False
                cleared = True
            except Exception:
                pass

        # Clear "triggered" latch
        if getattr(self, "_endguard_triggered", False):
            self._endguard_triggered = False
            cleared = True

        # Reset accumulation counter
        try:
            if getattr(self, "_endguard_forward_mm", 0.0) != 0.0:
                cleared = True
            self._endguard_forward_mm = 0.0
        except Exception:
            pass

        # Log outcome
        try:
            self.mmu.log_info("EndGuard pending actions cleared%s" % ((" (%s)" % reason) if reason else ""))
        except Exception:
            pass

        return cleared


    def _reset_endguard(self):
    # Reset endguard measuring state when initialising and also when switching sides in
    # the proportional filament sensor. This does not clear the endguard state and timers
    # For that use _clear_pending_endguard.
        self._endguard_forward_mm = 0.0
        self._endguard_triggered = False

    def _notify_endguard_forward_progress(self, movement):
        if not (getattr(self, "sync_endguard_enabled", 0) and getattr(self, "endguard_active", 0)):
            return
        if movement <= 0.0:
            return
        # If we already decided to act or have a pending action, stop accumulating/logging
        if getattr(self, "_endguard_triggered", False) or getattr(self, "_endguard_action_pending", False):
            return

        # accumulate only when hugging an end
        if abs(self.state) >= self.sync_endguard_band:
            self._endguard_forward_mm += movement
            self.mmu.log_info("EndGuard: +%.1fmm at |state|=%.3f -> total=%.1f/%.1f" % (movement, abs(self.state), self._endguard_forward_mm, self.sync_endguard_distance_mm))
            #IG ToDo: REVERT LOGGING to debug
            if self._endguard_forward_mm >= self.sync_endguard_distance_mm:
                self._trigger_endguard_pause()
        else:
            if self._endguard_forward_mm > 0.0:
                self.mmu.log_info("EndGuard: left band; resetting (total was %.1fmm)" % (self._endguard_forward_mm,)) #IG ToDo: REVERT LOGGING to debug
            self._reset_endguard()

    def _trigger_endguard_pause(self):
        if self._endguard_triggered:
            return
        self._endguard_triggered = True
        reason = ("Proportional sensor near end of travel during forward feed "
                  f"(accum {self._endguard_forward_mm:.1f}mm ≥ {self.sync_endguard_distance_mm:.1f}mm; "
                  f"|state|={abs(self.state):.3f})")
        self.mmu.log_always("MmuSyncFeedbackManager: EndGuard triggered: " + reason)

        # Defer the actual action to the reactor to avoid event-context races
        self._schedule_endguard_action(delay_s=0.15)


    # EndGuard print pause reactor scheduling (one-shot)

    def _schedule_endguard_action(self, delay_s=0.15):
        if not hasattr(self, "_endguard_timer_handle"):
            self._endguard_timer_handle = self.mmu.reactor.register_timer(self._endguard_timer)

        if getattr(self, "_endguard_action_pending", False):
            return  # already queued

        self._endguard_action_pending = True
        now = self.mmu.reactor.monotonic()
        # enforce a minimum delay to avoid same-cycle execution in flush context
        delay = max(float(delay_s), 0.05)
        self.mmu.reactor.update_timer(self._endguard_timer_handle, now + delay)
        self.mmu.log_debug("EndGuard: deferred pause scheduled in %.2fs" % (float(delay_s),))


    def _endguard_timer(self, eventtime):
        # One-shot; run the configured action outside the event callback context
        if not getattr(self, "_endguard_action_pending", False):
            return self.mmu.reactor.NEVER

        self._endguard_action_pending = False

        try:
            self.mmu.gcode.run_script('MMU_PAUSE MSG="Endguard detected clog or tangle"')
        except Exception:
            self.mmu.log_always("EndGuard: failed to invoke MMU_PAUSE")

        return self.mmu.reactor.NEVER

