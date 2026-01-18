# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle sync-feedback and adjustment of gear rotation distance
#       to keep MMU in sync with extruder
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

    FEEDBACK_INTERVAL      = 0.5     # How often to check extruder movement (seconds)
    SIGNIFICANT_MOVEMENT   = 5.      # Min extruder movement to trigger direction change (don't want small retracts to trigger)
    MOVEMENT_THRESHOLD     = 50      # Default extruder movement threshold trigger when stuck in one state
    MULTIPLIER_RUNAWAY     = 0.30    # Used to limit range in runaway conditions (30%)
    MULTIPLIER_WHEN_STUCK  = 0.01    # Used to "widen" rd clamp if we are not getting to neutral soon enough (1%)
    MULTIPLIER_WHEN_GOOD   = 0.005   # Used to move off trigger when tuned rotation distance has been found (0.5%)
    AUTOTUNE_TOLERANCE     = 0.0025  # The desired accuracy of autotuned rotation distance (0.25% or 2.5mm per m)

    SYNC_STATE_NEUTRAL     = 0
    SYNC_STATE_COMPRESSION = 1
    SYNC_STATE_TENSION     = -1

    def __init__(self, mmu):
        self.mmu = mmu

        self.state = float(self.SYNC_STATE_NEUTRAL) # 0 = Neutral (but a float to allow proportional support)
        self.extruder_direction = 0 # 0 = Extruder not moving
        self.active = False         # Actively operating?
        self.last_recorded_extruder_position = None

        # Process config
        self.sync_feedback_enabled = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range = self.mmu.config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange = self.mmu.config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_multiplier_high = self.mmu.config.getfloat('sync_multiplier_high', 1.05, minval=1., maxval=2.)
        self.sync_multiplier_low = self.mmu.config.getfloat('sync_multiplier_low', 0.95, minval=0.5, maxval=1.)
        self.sync_movement_threshold = self.mmu.config.getfloat('sync_movement_threshold', self.MOVEMENT_THRESHOLD, above=self.SIGNIFICANT_MOVEMENT) # Not yet exposed

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

    def set_test_config(self, gcmd):
        self.sync_feedback_enabled = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled)
        self.sync_feedback_buffer_range = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
        self.sync_feedback_buffer_maxrange = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)

    def get_test_config(self):
        msg = "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
        msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
        msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
        msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
        msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
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
            self._reset_current_sync_state()
            if self.sync_feedback_enabled:
                self.mmu.log_debug("MmuSyncFeedbackManager: Set initial sync feedback state to: %s" % self.get_sync_feedback_string(detail=True))

            # Set initial rotation distance (may have been previously autotuned)
            if not self._adjust_gear_rotation_distance():
                # Must not be available/enabled/active so use last or initial value
                self.mmu.set_rotation_distance(self.rd_clamps[gate][1])
        else:
            self._reset_gear_rotation_distance()

    def is_enabled(self):
        return self.sync_feedback_enabled

    def is_active(self):
        return self.active

    def has_sensor(self):
        return self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) or self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)

    def get_sync_bias_raw(self):
        return float(self.state) # TODO separate sensor_value(float) from state(int)

    def get_sync_bias_modelled(self): # TODO to allow prediction for UI
        return self.get_sync_bias_raw()

    def get_sync_feedback_string(self, state=None, detail=False):
        if state is None:
            state = self.state
        if self.mmu.is_enabled and self.sync_feedback_enabled and (self.active or detail):
            return 'compressed' if state > 0.5 else 'tension' if state < -0.5 else 'neutral'
        return "disabled"

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

    # Called periodically to check extruder movement
    def _check_extruder_movement(self, eventtime):
        if self.mmu.is_enabled:
            estimated_print_time = self.mmu.printer.lookup_object('mcu').estimated_print_time(eventtime)
            extruder = self.mmu.toolhead.get_extruder()
            pos = extruder.find_past_position(estimated_print_time)
            if self.last_recorded_extruder_position is None:
                self.last_recorded_extruder_position = pos

            # Have we changed direction?
            if abs(pos - self.last_recorded_extruder_position) >= self.SIGNIFICANT_MOVEMENT:
                prev_direction = self.extruder_direction
                self.extruder_direction = (
                    self.mmu.DIRECTION_LOAD if pos > self.last_recorded_extruder_position
                    else self.mmu.DIRECTION_UNLOAD if pos < self.last_recorded_extruder_position
                    else 0
                )
                if self.extruder_direction != prev_direction:
                    self._notify_direction_change(prev_direction, self.extruder_direction)
                    self.last_recorded_extruder_position = pos

            if abs(pos - self.last_recorded_extruder_position) >= self.sync_movement_threshold:
                # Ensure we are given periodic notifications to aid autotuning
                self._notify_hit_movement_marker(abs(pos - self.last_recorded_extruder_position))
                self.last_recorded_extruder_position = pos # Move marker

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
            self.last_recorded_extruder_position = None # Reset extruder watchdog position

            if self.sync_feedback_enabled and self.active:
                # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
                # Note that proportional feedback sensors do not have tension switch so clamp logic will be bypassed
                has_dual_sensors = (
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
                )
                if state != old_state and has_dual_sensors and self.mmu.autotune_rotation_distance: # TODO could separate "autotune and save" from just autotune?
                    self._adjust_clamps(state, old_state)
                self._adjust_gear_rotation_distance()
        else:
            self.mmu.log_error("MmuSyncFeedbackManager: Invalid sync feedback state: %s" % state)

        if self.mmu._is_running_test:
            self.mmu.printer.send_event("mmu:sync_feedback_finished", state)

    # This signifies that the extruder has changed direction
    def _notify_direction_change(self, last_direction, new_direction):
        dir_str = lambda d: 'extrude' if d == self.mmu.DIRECTION_LOAD else 'retract' if d == self.mmu.DIRECTION_UNLOAD else 'static'
        self.mmu.log_trace(
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

        # Currently we don't do anything if using fixed multipliers (single sensor case) TODO we could though!
        if not (has_dual_sensors and self.mmu.autotune_rotation_distance): return # TODO could separate "save autotune" from autotune?

        # Report and limit runaway conditions which could occur with bad configuration
        # (like tension/compression sensor reversal or perhaps during a long clog)
        def check_clamp_runaway(rd_clamp):
            max_slow_clamp = rd_clamp[4] * (1 + self.MULTIPLIER_RUNAWAY)
            max_fast_clamp = rd_clamp[4] * (1 - self.MULTIPLIER_RUNAWAY)
            exceeded = rd_clamp[0] > max_slow_clamp or rd_clamp[2] < max_fast_clamp
            if exceeded:
                self.mmu.log_warning(
                    "MmuSyncFeedbackManager: Exceeded rotation_distance autotune range)\n"
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

        if self.state == self.SYNC_STATE_COMPRESSION:
            # Compression state too long means filament feed is too fast, need to go slower so larger rotation distance
            # If we are at the previous slow clamp value (we expect to be) we need to increase its value (make even slower)

            # Widen clamp range by increasing slow clamp value by fixed % (make it even slower)
            if rd_clamp[1] >= rd_clamp[0]:
                rd_clamp[0] *= (1 + self.MULTIPLIER_WHEN_STUCK)

                if not check_clamp_runaway(rd_clamp):
                    self.mmu.log_trace(
                        "MmuSyncFeedbackManager: Extruder moved too far in compressed state (%.1fmm). Increased slow_rd clamp value by %.1f%% from %.4f to %.4f" % (
                            movement,
                            self.MULTIPLIER_WHEN_STUCK * 100,
                            old_clamp[0],
                            rd_clamp[0]
                        )
                    )

            # Switch to the new slow clamp value (to hopefully move towards tension state)
            rd_clamp[1] = rd_clamp[0]

        elif self.state == self.SYNC_STATE_TENSION:
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

        elif self.state == self.SYNC_STATE_NEUTRAL:
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
                        self.mmu.save_rotation_distance(self.mmu.gate_selected, _tuned_rd)
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
        if not self.sync_feedback_enabled or not self.active: return False

        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        if self.state == self.SYNC_STATE_NEUTRAL:
            # Start with mid point of previous clamps
            start_rd = (rd_clamp[0] + rd_clamp[2]) / 2.
        else:
            if self.extruder_direction == 0:
                # Start based simply on sensor state (assuming extrude direction)
                go_slower = self.state == self.SYNC_STATE_COMPRESSION
            else:
                # Start based simply on sensor state and extruder direction
                calc = lambda s, d: abs(s - d) < abs(s + d)
                go_slower = calc(self.state, self.extruder_direction)

            if go_slower:
                # Compressed when extruding or tension when retracting, so increase the rotation distance of gear stepper to slow it down
                start_rd = rd_clamp[0]
                self.mmu.log_debug("MmuSyncFeedbackManager: Slowing gear motor down")
            else:
                # Tension when extruding or compressed when retracting, so decrease the rotation distance of gear stepper to speed it up
                start_rd = rd_clamp[2]
                self.mmu.log_debug("MmuSyncFeedbackManager: Speeding gear motor up")

        rd_clamp[1] = start_rd
        self.mmu.log_trace(
            "MmuSyncFeedbackManager: Adjusted gear rotation_distance: %.4f (slow:%.4f, current: %.4f, fast:%.4f, tuned:%s, initial:%.4f)" % (
                start_rd,
                rd_clamp[0],
                rd_clamp[1],
                rd_clamp[2],
                ("%.4f" % rd_clamp[3]) if rd_clamp[3] else "None",
                rd_clamp[4]
            )
        )
        self.mmu.set_rotation_distance(start_rd)
        return True

    # Reset rotation_distance to calibrated value of current gate (not necessarily current value if autotuning)
    def _reset_gear_rotation_distance(self):
        rd = self.mmu.get_rotation_distance(self.mmu.gate_selected)
        self.mmu.log_trace("MmuSyncFeedbackManager: Reset rotation distance to last calibrated value (%.4f)" % rd)
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

        ss = self.SYNC_STATE_NEUTRAL
        if has_tension and has_compression:
            # Allow for sync-feedback sensor designs with minimal travel where both sensors can be triggered at same time
            if tension_active == compression_active:
                ss = self.SYNC_STATE_NEUTRAL
            elif tension_active and not compression_active:
                ss = self.SYNC_STATE_TENSION
            else:
                ss = self.SYNC_STATE_COMPRESSION
        elif has_compression and not has_tension:
            ss = self.SYNC_STATE_COMPRESSION if compression_active else self.SYNC_STATE_TENSION
        elif has_tension and not has_compression:
            ss = self.SYNC_STATE_TENSION if tension_active else self.SYNC_STATE_COMPRESSION
        self.state = ss
