# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
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
import logging

# Happy Hare imports
#from ..                  import mmu_machine
#from ..mmu_machine       import MmuToolHead
#from ..mmu_sensors       import MmuRunoutHelper
#
## MMU subcomponent clases
#from .mmu_shared         import *
#from .mmu_logger         import MmuLogger
#from .mmu_selector       import *
#from .mmu_test           import MmuTest
#from .mmu_utils          import DebugStepperMovement, PurgeVolCalculator
#from .mmu_sensor_manager import MmuSensorManager


class MmuSyncFeedbackManager:

    FEEDBACK_INTERVAL     = 0.25    # How often to check extruder movement
    SIGNIFICANT_MOVEMENT  = 5.      # Min extruder movement to trigger direction change (don't want small retracts to trigger)
    MOVEMENT_THRESHOLD    = 50      # Default extruder movement threshold trigger when stuck in one state
    MULTIPLIER_RUNAWAY    = 0.25    # Used to limit range in runaway conditions (25%)
    MULTIPLIER_WHEN_STUCK = 0.01    # Used to "widen" clamp if we are not getting to neutral soon enough (1%)
    MULTIPLIER_WHEN_GOOD  = 0.005   # Used to move off trigger when tuned rotation distance has been found (0.5%)
    AUTOTUNE_TOLERANCE    = 0.0025  # The desired accuracy of autotuned rotation distance (0.25% or 2.5mm per m)

    SYNC_STATE_NEUTRAL    = 0
    SYNC_STATE_COMPRESSION = 1
    SYNC_STATE_TENSION   = -1
    
    # proportional tension / compression control tunables
    RDD_THRESHOLD         = 1e-4     # Min Rotation Distance delta to trigger application of it.
    PROP_DEADBAND_THRESHOLD = 0.30  # Magnitude of side motion before considering state as tension/compression. 
    								 # a 0.30 side threshold means sensor values of ~3mm either side are considered neutral
    PROP_RELEASE_THRESHOLD  = 0.20  # Magnitude of side motion to consider the virtual switch as released.

    def __init__(self, mmu):
        self.mmu = mmu

        self.state = float(self.SYNC_STATE_NEUTRAL) # 0 = Neutral (but a float to allow proportional support)
        self.extruder_direction = 0 # 0 = Extruder not moving
        self.active = False         # Actively operating?
        self.last_recorded_extruder_position = None
        self._last_state_side = self.SYNC_STATE_NEUTRAL # track sign of proportional state to detect transitions
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-latched side in _last_state_side
        self._rd_applied = None     # track live applied RD so UI can show true adjustment

        # Process config
        self.sync_feedback_enabled = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_proportional_sensor = self.mmu.config.getint('sync_feedback_proportional_sensor', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range = self.mmu.config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange = self.mmu.config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_multiplier_high = self.mmu.config.getfloat('sync_multiplier_high', 1.05, minval=1., maxval=2.)
        self.sync_multiplier_low = self.mmu.config.getfloat('sync_multiplier_low', 0.95, minval=0.5, maxval=1.)
        self.sync_movement_threshold = self.mmu.config.getfloat('sync_movement_threshold', self.MOVEMENT_THRESHOLD, above=self.SIGNIFICANT_MOVEMENT) # Not yet exposed
        # Make direction detection threshold configurable. This is the min extruder movement to trigger direction change
        self.sync_significant_movement_threshold = self.mmu.config.getfloat('sync_significant_movement_threshold',self.SIGNIFICANT_MOVEMENT,above=0.5, below=self.sync_movement_threshold)
        # Log error if sync_significant_movement_threshold is not less than or equal to the sync_movement_threshold (user config guard)
        if self.sync_significant_movement_threshold > self.sync_movement_threshold:
            self.mmu.log_error( "Significant movement threshold higher than movement threshold")
        
        # EndGuard (proportional near-end watchdog)
        self.sync_endguard_enabled  = self.mmu.config.getint('sync_endguard_enabled', 0, minval=0, maxval=1)
        self.sync_endguard_band     = self.mmu.config.getfloat('sync_endguard_band', 0.90, minval=0.55, maxval=1.00)
        self.sync_endguard_distance_mm  = self.mmu.config.getfloat('sync_endguard_distance_mm', 6.0, minval=1.0)
        self.endguard_last_recorded_extruder_position  = None
        self._endguard_forward_mm   = 0.0
        self._endguard_triggered    = False
        self.endguard_active        = 0  # runtime latch to activate/deactivate endguard during the load/unload process

        # Setup events for managing motor synchronization
        self.mmu.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
        self.mmu.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
        self.mmu.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)

        self.reinit()
        self._setup_extruder_watchdog_timer()

    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self.rd_clamps = {}         # Autotune - Array of [slow_rd, current_rd, fast_rd, tuned_rd, original_rd] indexed by gate
        self._reset_extruder_watchdog()
        self._reset_endguard()

    def set_test_config(self, gcmd):
        self.sync_feedback_enabled = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled)
        self.sync_feedback_proportional_sensor = gcmd.get_int('SYNC_FEEDBACK_PROPORTIONAL_SENSOR', self.sync_feedback_proportional_sensor, minval=0, maxval=1)
        self.sync_feedback_buffer_range = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
        self.sync_feedback_buffer_maxrange = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)
        self.sync_significant_movement_threshold = gcmd.get_float('SYNC_SIGNIFICANT_MOVEMENT_THRESHOLD', self.sync_significant_movement_threshold, minval=0.5, maxval=self.sync_movement_threshold)
        self.sync_endguard_enabled = gcmd.get_int('SYNC_ENDGUARD_ENABLED', self.sync_endguard_enabled, minval=0, maxval=1)
        self.sync_endguard_band = gcmd.get_float('SYNC_ENDGUARD_BAND', self.sync_endguard_band, minval=0.55, maxval=1.00)
        self.sync_endguard_distance_mm = gcmd.get_float('SYNC_ENDGUARD_DISTANCE_MM', self.sync_endguard_distance_mm, minval=1.0)

    def get_test_config(self):
        msg = "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
        msg += "\nsync_feedback_proportional_sensor = %d" % self.sync_feedback_proportional_sensor
        msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
        msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
        msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
        msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
        msg += "\nsync_significant_movement_threshold = %.2f" % self.sync_significant_movement_threshold
        msg += "\nsync_endguard_enabled = %d" % self.sync_endguard_enabled
        msg += "\nsync_endguard_band = %.2f" % self.sync_endguard_band
        msg += "\nsync_endguard_distance_mm = %.1f" % self.sync_endguard_distance_mm
        return msg

    def check_test_config(self, param):
        return vars(self).get(param) is None

    #
    # Sync feedback manager public access...
    #

    # Ensure correct sync / rotation distance starting state based on current sensor input
    # Regardless of state or gate this will set a sensible rotation distance
    def reset_sync_starting_state_for_gate(self, gate):
        if gate >= 0:
            # Initialize rotation distance clamping range for gate
            if not self.rd_clamps.get(gate):
                rd = self.mmu.get_rotation_distance(gate)
                self.rd_clamps[gate] = [rd * self.sync_multiplier_high, rd, rd * self.sync_multiplier_low, None, rd]

            self._reset_extruder_watchdog()
            self._reset_endguard()
            self._reset_current_sync_state()
            if self.sync_feedback_enabled:
                self.mmu.log_debug("MmuSyncFeedbackManager: Set initial sync feedback state to: %s" % self.get_sync_feedback_string(detail=True))

            # Set initial rotation distance (may have been previously autotuned)
            if not self._adjust_gear_rotation_distance():
                rd = self.rd_clamps[gate][1]
                self._rd_applied = rd
                self.mmu.set_rotation_distance(rd)
        else:
            self._reset_gear_rotation_distance()

    def is_enabled(self):
        return self.sync_feedback_enabled

    def is_active(self):
        return self.active

    def has_sensor(self):
        return self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) or self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)

    def get_sync_feedback_string(self, state=None, detail=False):
        if state is None:
            state = self.state
        if self.mmu.is_enabled and self.sync_feedback_enabled and (self.active or detail):
            if self.sync_feedback_proportional_sensor:
                # Show the latched side to match control behavior
                s = self._last_state_side
                return 'neutral' if s == 0 else ('compressed' if s > 0 else 'tension')
            else:
                # Dual-switch path
                return 'compressed' if float(state) > 0.0 else 'tension' if float(state) < 0.0 else 'neutral'
        return "disabled"

    # End guard enable/disable/reset hooks
    def enable_endguard(self, reason=None):
        self.set_endguard_active(True, reason)

    def disable_endguard(self, reason=None):
        self.set_endguard_active(False, reason)

    #
    # Internal implementation --------------------------------------------------
    #

    # Python 2 doesn't support math.isclose() so simple emulation
    def _math_isclose(self, a, b, rel_tol=1e-9, abs_tol=0.0):
        return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

    def _setup_extruder_watchdog_timer(self):
        self.extruder_watchdog_timer = self.mmu.reactor.register_timer(self._check_extruder_movement)

    # Starting assumption is that extruder is not moving and measurement is 0mm
    def _reset_extruder_watchdog(self):
        self.extruder_direction = 0 # Extruder not moving
        self.last_recorded_extruder_position = None
        self.endguard_last_recorded_extruder_position = None

    # Called periodically to check extruder movement
    def _check_extruder_movement(self, eventtime):
        if self.mmu.is_enabled:
            estimated_print_time = self.mmu.printer.lookup_object('mcu').estimated_print_time(eventtime)
            extruder = self.mmu.toolhead.get_extruder()
            pos = extruder.find_past_position(estimated_print_time)
            
            if self.last_recorded_extruder_position is None:
                self.last_recorded_extruder_position = pos
            
            if self.endguard_last_recorded_extruder_position is None:
                self.endguard_last_recorded_extruder_position = pos

            # Have we changed direction?
            if abs(pos - self.last_recorded_extruder_position) >= self.sync_significant_movement_threshold:
                prev_direction = self.extruder_direction
                self.extruder_direction = (
                    self.mmu.DIRECTION_LOAD if pos > self.last_recorded_extruder_position
                    else self.mmu.DIRECTION_UNLOAD if pos < self.last_recorded_extruder_position
                    else 0
                )
                delta = pos - self.last_recorded_extruder_position
                if self.extruder_direction != prev_direction:
                    self._notify_direction_change(prev_direction, self.extruder_direction)
                    # Feed EndGuard with the positive chunk consumed by the direction-change path
                    if delta > 0.0:
                        self._notify_endguard_forward_progress(delta)
                        if delta >= self.sync_movement_threshold:
                            self._notify_hit_movement_marker(delta)
                    self.last_recorded_extruder_position = pos
                    self.endguard_last_recorded_extruder_position = pos

            # Feed auto tuning on sync_movement_threshold intervals
            if abs(pos - self.last_recorded_extruder_position) >= self.sync_movement_threshold:
                # Ensure we are given periodic notifications to aid autotuning
                self._notify_hit_movement_marker(abs(pos - self.last_recorded_extruder_position))
                self.last_recorded_extruder_position = pos # Move marker
            
            # Feed endguard on sync_significant_movement_threshold intervals
            if (pos - self.endguard_last_recorded_extruder_position) >= self.sync_significant_movement_threshold:
                # Feed EndGuard on forward-only chunks as well
                self._notify_endguard_forward_progress(pos - self.endguard_last_recorded_extruder_position)
                self.endguard_last_recorded_extruder_position = pos # Move marker

        return eventtime + self.FEEDBACK_INTERVAL

    # Event indicating that gear stepper is now synced with extruder
    def _handle_mmu_synced(self):
        if not self.mmu.is_enabled: return
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

    # Event indicating that gear stepper has been unsynced from extruder
    def _handle_mmu_unsynced(self):
        if not self.mmu.is_enabled: return
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

    # Gear/Extruder sync feedback event. State should be -1 (tension) and 1 (compressed)
    # or can be a proportional float value between -1.0 and 1.0
    def _handle_sync_feedback(self, eventtime, state):
        if not self.mmu.is_enabled: return
        if abs(state) <= 1:
            old_state = self.state
            self.state = float(state)
            self.mmu.log_trace(
                "MmuSyncFeedbackManager(%s): Got sync force feedback update. State: %s (%s)" % (
                    "active" if self.sync_feedback_enabled and self.active else "inactive",
                    self.get_sync_feedback_string(detail=True),
                    float(state)
                )
            )
            # IMPORTANT: Do NOT reset the last_recorded_extruder_position every proportional tick.
            # Only reset on deadband transitions (tension<->neutral<->compression) so ΔE can accumulate.
            # Hysteresis: hold side while in deadband; release to NEUTRAL only on true zero-crossing.
            def _side(v, prev):
                # Use latched when proportional sensor present and we are not auto tuning RD.
                if (bool(self.sync_feedback_proportional_sensor) and not getattr(self.mmu, "autotune_rotation_distance", False)):
                    # Outside deadband: (re)latch to sign immediately
                    if abs(v) >= self.PROP_DEADBAND_THRESHOLD:
                        return self.SYNC_STATE_COMPRESSION if v > self.SYNC_STATE_NEUTRAL else self.SYNC_STATE_TENSION
                    # Inside deadband: keep previous side; only go NEUTRAL when crossing neutral (0)
                    if prev == self.SYNC_STATE_COMPRESSION and v <= self.SYNC_STATE_NEUTRAL:
                        return self.SYNC_STATE_NEUTRAL
                    if prev == self.SYNC_STATE_TENSION and v >= self.SYNC_STATE_NEUTRAL:
                        return self.SYNC_STATE_NEUTRAL
                    return prev  # hold previous (including NEUTRAL) while hovering in deadband
                else:
                    # Proportional sensor with autotune enabled: emulate switch with small release hysteresis
                    if bool(self.sync_feedback_proportional_sensor):
                        if prev == self.SYNC_STATE_COMPRESSION:
                            if v > self.PROP_RELEASE_THRESHOLD:
                                return self.SYNC_STATE_COMPRESSION
                            if v <= -self.PROP_DEADBAND_THRESHOLD:
                                return self.SYNC_STATE_TENSION
                            return self.SYNC_STATE_NEUTRAL

                        if prev == self.SYNC_STATE_TENSION:
                            if v < -self.PROP_RELEASE_THRESHOLD:
                                return self.SYNC_STATE_TENSION
                            if v >= self.PROP_DEADBAND_THRESHOLD:
                                return self.SYNC_STATE_COMPRESSION
                            return self.SYNC_STATE_NEUTRAL

                        if v >= self.PROP_DEADBAND_THRESHOLD:
                            return self.SYNC_STATE_COMPRESSION
                        if v <= -self.PROP_DEADBAND_THRESHOLD:
                            return self.SYNC_STATE_TENSION
                        return self.SYNC_STATE_NEUTRAL

                    # Non-proportional path
                    return self.SYNC_STATE_NEUTRAL if abs(v) < self.PROP_DEADBAND_THRESHOLD else (self.SYNC_STATE_COMPRESSION if v > self.SYNC_STATE_NEUTRAL else self.SYNC_STATE_TENSION)

            old_side = self._last_state_side
            new_side = _side(self.state, old_side)
            
            if new_side != old_side:
                self.last_recorded_extruder_position = None # Reset extruder watchdog position
                self._reset_endguard()                      # reset endguard accumulators as we are counting from a new side.
                self._last_state_side = new_side            # switch sides

            if self.sync_feedback_enabled and self.active:
                # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
                # Proportional sensor does not have any switches, hence will bypass the dual switch autotune path
                has_dual_sensors = (
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
                )
                # Proportional sensor is in use
                use_proportional = bool(self.sync_feedback_proportional_sensor)
                
                # Dual-switch RD autotune path. Use state transitions
                if state != old_state and has_dual_sensors and self.mmu.autotune_rotation_distance and not use_proportional: # TODO could separate "autotune and save" from just autotune?
                    self._adjust_clamps(state, old_state)
                    
                # Proportional RD autotune path. Use debounced side threshold transitions
                # Only active when proportional is selected, and autotune enabled.
                if new_side != old_side and use_proportional and self.mmu.autotune_rotation_distance:
                    self._adjust_clamps(new_side, old_side)
                        
                self._adjust_gear_rotation_distance()
        else:
            self.mmu.log_error("MmuSyncFeedbackManager: Invalid sync feedback state: %s" % state)

        if self.mmu._is_running_test:
            self.mmu.printer.send_event("mmu:sync_feedback_finished", state)

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

    # This signifies we have been sitting in same state for longer than the movement threshold so
    # rotation_distance may need an additional nudge
    def _notify_hit_movement_marker(self, movement):
        # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
        has_dual_sensors = (
            self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
            self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        )
        use_proportional = bool(self.sync_feedback_proportional_sensor)

        # Allow nudges for:
        #   - dual sensors + autotune OR
        #   - proportional + autotune.
        # Currently we don't do anything if using fixed multipliers (single sensor case) TODO we could though!
        if not (self.mmu.autotune_rotation_distance and (has_dual_sensors or use_proportional)): # TODO could separate "save autotune" from autotune?
            return

        # Report and limit runaway conditions which could occur with bad configuration
        # (like tension/compression sensor reversal or perhaps during a long clog)
        def check_clamp_runaway(rd_clamp):
            max_slow_clamp = rd_clamp[4] * (1 + self.MULTIPLIER_RUNAWAY)
            max_fast_clamp = rd_clamp[4] * (1 - self.MULTIPLIER_RUNAWAY)
            exceeded = rd_clamp[0] > max_slow_clamp or rd_clamp[2] < max_fast_clamp
            if exceeded:
                self.mmu.log_warning(
                    "MmuSyncFeedbackManager: Exceeded rotation_distance autotune range\n"
                    "It is likely that the sync-feedback sensor is malfunctioning or misconfigured\n"
                    "(perhaps tension and compression sensors are reversed) or the extruder is clogged.\n"
                    "Continuing, but limiting rotation_distance to range (%.4f to %.4f)"
                    % (max_fast_clamp, max_slow_clamp)
                )
            rd_clamp[0] = min(rd_clamp[0], max_slow_clamp)
            rd_clamp[2] = max(rd_clamp[2], max_fast_clamp)
            return exceeded

        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        old_clamp = rd_clamp.copy()
        
        # Effective state for nudger:
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-latched side in _last_state_side
        effective_state = self.state if (has_dual_sensors and not use_proportional) else self._last_state_side

        if effective_state == self.SYNC_STATE_COMPRESSION:
            # Compression state too long means filament feed is too fast, need to go slower so larger rotation distance
            # If we are at the previous slow clamp value (we expect to be) we need to increase its value (make even slower)

            # Widen clamp range by increasing slow clamp value by fixed % (make it even slower)
            if rd_clamp[1] >= rd_clamp[0]:
                rd_clamp[0] *= (1 + self.MULTIPLIER_WHEN_STUCK)

                if not check_clamp_runaway(rd_clamp):
                    self.mmu.log_debug(
                        "MmuSyncFeedbackManager: Extruder moved too far in compressed state (%.1fmm). Increased slow_rd clamp value by %.1f%% from %.4f to %.4f" % (
                            movement,
                            self.MULTIPLIER_WHEN_STUCK * 100,
                            old_clamp[0],
                            rd_clamp[0]
                        )
                    )

            # Switch to the new slow clamp value (to hopefully move towards tension state)
            rd_clamp[1] = rd_clamp[0]

        elif effective_state == self.SYNC_STATE_TENSION:
            # Tension state too long means filament feed is too slow, need to go faster so smaller rotation distance
            # If we are at the previous fast clamp value (we expect to be) we need to decrease its value (make even faster)

            # Widen clamp range by decreasing fast clamp value by fixed % (make it even faster)
            if rd_clamp[1] <= rd_clamp[2]:
                rd_clamp[2] *= (1 - self.MULTIPLIER_WHEN_STUCK)

                if not check_clamp_runaway(rd_clamp):
                    self.mmu.log_trace(
                        "MmuSyncFeedbackManager: Extruder moved too far in tension state (%.1fmm). Decreased fast_rd clamp value by %.1f%% from %.4f to %.4f" % (
                            movement,
                            self.MULTIPLIER_WHEN_STUCK * 100,
                            old_clamp[2],
                            rd_clamp[2]
                        )
                    )

            # Switch to the new fast clamp value (to hopefully move towards compressed state)
            rd_clamp[1] = rd_clamp[2]

        elif effective_state == self.SYNC_STATE_NEUTRAL:
            self.mmu.log_trace("MmuSyncFeedbackManager: Ignoring extruder move marker trigger because in neutral state")
            return # Do nothing, we want to stay in this state

        # Update if current rd has changed
        if not self._math_isclose(rd_clamp[1], old_clamp[1]):
            self._adjust_gear_rotation_distance()


    # Called to use binary search algorithm to slowly reduce clamping range to minimize switching
    # Note that this will converge on new calibrated rd value and update it if autotune options are set
    def _adjust_clamps(self, state, old_state):
        if state == old_state: return # Shouldn't happen
        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        old_clamp = rd_clamp.copy()
        tuned_rd = None

        def check_if_tuned(rd_clamp):
            if self._math_isclose(rd_clamp[0], rd_clamp[2], rel_tol=self.AUTOTUNE_TOLERANCE):
                _tuned_rd = (rd_clamp[0] + rd_clamp[2]) / 2.
                if not rd_clamp[3] or not self._math_isclose(_tuned_rd, rd_clamp[3]):
                    # New tuned setting
                    rd_clamp[3] = _tuned_rd
                    self.mmu.log_always(
                        "MmuSyncFeedbackManager: New autotuned rotation_distance for gate %d: %.4f" % (
                            self.mmu.gate_selected,
                            rd_clamp[3]
                        )
                    )
                    if self.mmu.autotune_rotation_distance:
                        self.mmu.save_rotation_distance(self.mmu.gate_selected, round(_tuned_rd, 4)) # Round to 4 decimals for saving.
                    # We have found a tuned RD value - widen clamps to prevent oscillation of the tuned RD value
                    # due to the nudge off the trigger point.
                    rd_clamp[0] = _tuned_rd * self.sync_multiplier_high
                    rd_clamp[2] = _tuned_rd * self.sync_multiplier_low
                return _tuned_rd
            return None

        if state == self.SYNC_STATE_COMPRESSION:  # Transition from neutral --> compressed
            # MMU is driving filament too quickly, need to go slower so larger rotation distance (the "slow" clamp)
            # Also use current rotation distance to clamp fast setting because we know it is too fast
            rd_clamp[2] = rd_clamp[1]
            self.mmu.log_trace(
                "MmuSyncFeedbackManager: Neutral -> Compressed. Going too fast. "
                "Adjusted fast_rd clamp (%.4f -> %.4f)" % (
                    old_clamp[2],
                    rd_clamp[2]
                )
            )

            # If we have good calibration, adjust a little to move off trigger
            tuned_rd = check_if_tuned(rd_clamp)
            if tuned_rd:
                rd_clamp[0] *= (1 + self.MULTIPLIER_WHEN_GOOD)
                self.mmu.log_trace(
                    "MmuSyncFeedbackManager: Have good rotation_distance, adjusting current_rd and slow_rd clamp "
                    "slightly (%.4f -> %.4f) to move off trigger" % (
                        old_clamp[0],
                        rd_clamp[0]
                    )
                )
            rd_clamp[1] = rd_clamp[0]  # Set current rd to slow setting

        elif state == self.SYNC_STATE_TENSION:  # Transition from neutral --> tension
            # MMU is driving filament too slowly, need to go faster so smaller rotation distance (the "fast" clamp)
            # Also use current rotation distance to clamp slow setting because we know it is too slow
            rd_clamp[0] = rd_clamp[1]
            self.mmu.log_trace(
                "MmuSyncFeedbackManager: Neutral -> Tension. Going too slow. "
                "Adjusted slow_rd clamp (%.4f -> %.4f)" % (
                    old_clamp[0],
                    rd_clamp[0]
                )
            )

            # If we have good calibration, adjust a little to move off trigger
            tuned_rd = check_if_tuned(rd_clamp)
            if tuned_rd:
                rd_clamp[2] *= (1 - self.MULTIPLIER_WHEN_GOOD)
                self.mmu.log_trace(
                    "MmuSyncFeedbackManager: Have good rotation_distance, adjusting current_rd and fast_rd clamp "
                    "slightly (%.4f -> %.4f) to move off trigger" % (
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

    # Update gear rotation_distance based on current state. This correctly handles
    # the direction of movement (although it will almost always be neutral or extruding)
    # Return True if rotation_distance set/reset
    def _adjust_gear_rotation_distance(self):
        self.mmu.log_trace( "MmuSyncFeedbackManager: adjust RD? enabled=%s active=%s state=%.3f dir=%d" % (self.sync_feedback_enabled, self.active, self.state, self.extruder_direction) )
        
        if not self.sync_feedback_enabled or not self.active: return False

        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        
        effective_state = self.SYNC_STATE_NEUTRAL # Initialise effective state to neutral (unnecessary, here for code readability)

        # If using proportional feedback, consume hysteresis-quantised side (_last_state_side).
        use_proportional_hysteresis = bool(self.sync_feedback_proportional_sensor)

        # Choose the effective side the sensor is at to drive RD
        # Distinguish between proportional sensor and switch based sensor to derive effective state
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-latched side in _last_state_side
        if use_proportional_hysteresis:
            effective_state = self._last_state_side
        else:
        # If not using proportional feedback sensor -> use the hardware state.
            if self.state == self.SYNC_STATE_NEUTRAL:
                effective_state = self.SYNC_STATE_NEUTRAL
            elif self.state == self.SYNC_STATE_COMPRESSION:
                effective_state = self.SYNC_STATE_COMPRESSION
            else:
                effective_state = self.SYNC_STATE_TENSION   
        
        if effective_state == self.SYNC_STATE_NEUTRAL:
            # Start with mid point of previous clamps
            selected_rd = (rd_clamp[0] + rd_clamp[2]) / 2.
        else:
            if self.extruder_direction == 0:
                # Start based simply on sensor state (assuming extrude direction)
                go_slower = effective_state == self.SYNC_STATE_COMPRESSION
            else:
                # Start based simply on sensor state and extruder direction
                calc = lambda s, d: abs(s - d) < abs(s + d)
                go_slower = calc(effective_state, self.extruder_direction)

            if go_slower:
                # Compressed when extruding or tension when retracting, so increase the rotation distance of gear stepper to slow it down
                selected_rd = rd_clamp[0]
                decision = "Slowing down" # string for logging purposes. 
            else:
                # Tension when extruding or compressed when retracting, so decrease the rotation distance of gear stepper to speed it up
                selected_rd = rd_clamp[2]
                decision = "Speeding up" # string for logging purposes. 

        
        if self._rd_applied is not None and abs(selected_rd - self._rd_applied) < self.RDD_THRESHOLD:
            # No meaningful change; skip logging & RD adjustment.
            # This is critical for the proportional pressure sensor as it emits events every 200ms
            # hence without a gate, it would apply RD on every cycle.
            # Switch based should always result to false, hence apply the change immediately.
            return False
        
        rd_clamp[1] = selected_rd
        if 'decision' in locals():  # only present when not neutral
            self.mmu.log_debug("MmuSyncFeedbackManager: %s gear motor" % (decision))

        
        self.mmu.log_debug(
            "MmuSyncFeedbackManager: Adjusted gear rotation_distance: %.4f (slow:%.4f, current: %.4f, fast:%.4f, tuned:%s, initial:%.4f)" % (
                selected_rd,
                rd_clamp[0],
                rd_clamp[1],
                rd_clamp[2],
                ("%.4f" % rd_clamp[3]) if rd_clamp[3] else "None",
                rd_clamp[4]
            )
        )
        self._rd_applied = selected_rd
        self.mmu.set_rotation_distance(selected_rd)
        return True

    # Reset rotation_distance to calibrated value of current gate (not necessarily current value if autotuning)
    def _reset_gear_rotation_distance(self):
        rd = self.mmu.get_rotation_distance(self.mmu.gate_selected)
        self.mmu.log_debug("MmuSyncFeedbackManager: Reset rotation distance to last calibrated value (%.4f)" % rd)
        self._rd_applied = rd
        self.mmu.set_rotation_distance(rd)

        # This is mostly for optics because value will be reset on next sync() event
        if self.rd_clamps.get(self.mmu.gate_selected):
            rd_clamp = self.rd_clamps[self.mmu.gate_selected]
            rd_clamp[1] = rd

    # Reset current sync state based on current sensor feedback
    def _reset_current_sync_state(self):
        sm = self.mmu.sensor_manager
        has_tension = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        tension_active = sm.check_sensor(self.mmu.SENSOR_TENSION)
        compression_active = sm.check_sensor(self.mmu.SENSOR_COMPRESSION)
        use_proportional = bool(self.sync_feedback_proportional_sensor)

        ss = self.SYNC_STATE_NEUTRAL
        if has_tension and has_compression and not use_proportional:
            # Allow for sync-feedback sensor designs with minimal travel where both sensors can be triggered at same time
            if tension_active == compression_active:
                ss = self.SYNC_STATE_NEUTRAL
            elif tension_active and not compression_active:
                ss = self.SYNC_STATE_TENSION
            else:
                ss = self.SYNC_STATE_COMPRESSION
        elif has_compression and not has_tension and not use_proportional:
            ss = self.SYNC_STATE_COMPRESSION if compression_active else self.SYNC_STATE_TENSION
        elif has_tension and not has_compression and not use_proportional:
            ss = self.SYNC_STATE_TENSION if tension_active else self.SYNC_STATE_COMPRESSION
        else:
            # Proportional sensor
            if use_proportional:
                ss = self.SYNC_STATE_NEUTRAL if abs(self.state) < self.PROP_DEADBAND_THRESHOLD else (
                    self.SYNC_STATE_COMPRESSION if self.state > 0 else self.SYNC_STATE_TENSION
                )
            else: # No sensors at all
                ss = self.SYNC_STATE_NEUTRAL
        self.state = ss
        # Update quantised cached side for later transition detection for the proportional sensor
        # that uses hysteresis-latched side in _last_state_side
        self._last_state_side = ss

    
    # EndGuard implementation (proportional filament pressure sensor clog and tangle detection)

    def set_endguard_active(self, enabled, reason=None):
        use_proportional = bool(self.sync_feedback_proportional_sensor)
        if not use_proportional:
            return
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
            delay_s = 1.00  # ~1s; stay well clear of any immediate resume print operations
            
            # Arm endguard in a deferred manner to ensure any resume activities are complete (safeguard)
            def _arm(evt):
                self.endguard_active = 1
                try:
                    self.mmu.log_debug("EndGuard: enabled (deferred)%s" % ((" (%s)" % reason) if reason else ""))
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
        self.endguard_last_recorded_extruder_position = None

    def _notify_endguard_forward_progress(self, movement):
        if not (getattr(self, "sync_endguard_enabled", 0) and getattr(self, "endguard_active", 0)):
            return
        use_proportional = bool(self.sync_feedback_proportional_sensor)
        if not use_proportional:
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
        self.mmu.log_debug("EndGuard: deferred pause scheduled in %.2fs" % (float(delay),))


    def _endguard_timer(self, eventtime):
        # One-shot; run the configured action outside the event callback context
        if not getattr(self, "_endguard_action_pending", False):
            return self.mmu.reactor.NEVER
          
        self._endguard_action_pending = False
            
        try:
            # Run the pause portion of pause_resume execute immediately.
            pause_resume = self.mmu.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            self.mmu.gcode.run_script('MMU_PAUSE MSG="Endguard detected clog or tangle"')
        except Exception:
            self.mmu.log_always("EndGuard: failed to invoke MMU_PAUSE")
        
        return self.mmu.reactor.NEVER

