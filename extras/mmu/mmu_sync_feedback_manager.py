# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle sync-feedback and adjustment of gear rotation distance
#       to keep MMU in sync with extruder as well as some filament tension routines.
#       It also implements clog and tangle detection if a proportional filament pressure sensor is installed.
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

    FEEDBACK_INTERVAL       = 0.25    # How often to check extruder movement (seconds)
    SIGNIFICANT_MOVEMENT    = 5.      # Min extruder movement to trigger direction change (don't want small retracts to trigger)
    MOVEMENT_THRESHOLD      = 50      # Default extruder movement threshold trigger when stuck in one state
    MULTIPLIER_RUNAWAY      = 0.25    # Used to limit range in runaway conditions (25%)
    MULTIPLIER_WHEN_STUCK   = 0.01    # Used to "widen" clamp if we are not getting to neutral soon enough (1%)
    MULTIPLIER_WHEN_GOOD    = 0.005   # Used to move off trigger when tuned rotation distance has been found (0.5%)
    AUTOTUNE_TOLERANCE      = 0.0025  # The desired accuracy of autotuned rotation distance (0.25% or 2.5mm per m)

    SYNC_STATE_NEUTRAL      = 0
    SYNC_STATE_COMPRESSION  = 1
    SYNC_STATE_TENSION      = -1

    # proportional tension / compression control tunables
    RDD_THRESHOLD           = 1e-4    # Min Rotation Distance delta to trigger application of it.
    PROP_DEADBAND_THRESHOLD = 0.20    # Magnitude of side motion before considering state as tension/compression.
                                      # A 0.20 side threshold means sensor values of ~2mm either side are considered neutral

    PROP_DEADBAND_AUTOTUNE_THRESHOLD = 0.30  # For the auto tune path, use a slightly larger triggering threshold
                                             # to reduce false triggering due to system oscillations.
    PROP_RELEASE_AUTOTUNE_THRESHOLD  = 0.20  # Sensor reading threshold where the trigger is released, emulating virtual switch
                                             # deadband. 0.3-0.2=0.1 for a 10mm sensor ~1mm of virtual switch hysteresis.

    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu.managers.append(self)

        self.state = 0.             # 0 = Neutral
        self.extruder_direction = 0 # 0 = Extruder not moving
        self.active = False         # Actively operating?
        self.last_recorded_extruder_position = None
        self._last_state_side = self.SYNC_STATE_NEUTRAL # track sign of proportional state to detect transitions
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-latched side in _last_state_side
        self._rd_applied = None     # Track live applied RD so UI can show true adjustment

        # Process config
        self.sync_feedback_enabled = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
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

        # Flowguard (proportional near-end watchdog)
        self.sync_flowguard_enabled = self.mmu.config.getint('sync_flowguard_enabled', 0, minval=0, maxval=1)
        self.sync_flowguard_band = self.mmu.config.getfloat('sync_flowguard_band', 0.90, minval=0.55, maxval=1.00)
        self.sync_flowguard_distance = self.mmu.config.getfloat('sync_flowguard_distance', 6.0, minval=1.0)

        # Control of relaxing filament tension when using proportional feedback
        # See _adjust_filament_tension_proportional() for meaning
        self.sync_proportional_neutral_band = self.mmu.config.getfloat('sync_proportional_neutral_band', 0.1, minval=0.05, maxval=0.45) # Not exposed
        self.sync_proportional_settle_time = self.mmu.config.getfloat('sync_proportional_settle_time', 0.3, above=0.1)                  # Not exposed
        self.sync_proportional_timeout = self.mmu.config.getfloat('sync_proportional_timeout', 10.0, above=1.)                          # Not exposed

        self.flowguard_active          = 0  # Runtime latch to activate/deactivate flowguard during the load/unload process
        self.flowguard_last_recorded_extruder_position  = None
        self._flowguard_forward_mm     = 0.0
        self._flowguard_triggered      = False
        self._flowguard_arm_timer      = None
        self._flowguard_timer_handle   = None
        self._flowguard_action_pending = None

        # Setup events for managing motor synchronization
        self.mmu.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
        self.mmu.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
        self.mmu.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)

        # Register GCODE commands ---------------------------------------------------------------------------

        self.mmu.gcode.register_command('MMU_SYNC_FEEDBACK', self.cmd_MMU_SYNC_FEEDBACK, desc=self.cmd_MMU_SYNC_FEEDBACK_help)
        self.mmu.gcode.register_command('MMU_FLOWGUARD',  self.cmd_MMU_FLOWGUARD, desc=self.cmd_MMU_FLOWGUARD_help)

        self.reinit()
        self._setup_extruder_watchdog_timer()


    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self.rd_clamps = {}         # Autotune - Array of [slow_rd, current_rd, fast_rd, tuned_rd, original_rd] indexed by gate
        self._reset_extruder_watchdog()
        self._reset_flowguard()

    def set_test_config(self, gcmd):
        self.sync_feedback_enabled = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled)
        self.sync_feedback_buffer_range = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
        self.sync_feedback_buffer_maxrange = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)
        self.sync_significant_movement_threshold = gcmd.get_float('SYNC_SIGNIFICANT_MOVEMENT_THRESHOLD', self.sync_significant_movement_threshold, minval=0.5, maxval=self.sync_movement_threshold)
        self.sync_flowguard_enabled = gcmd.get_int('SYNC_FLOWGUARD_ENABLED', self.sync_flowguard_enabled, minval=0, maxval=1)
        self.sync_flowguard_band = gcmd.get_float('SYNC_FLOWGUARD_BAND', self.sync_flowguard_band, minval=0.55, maxval=1.00)
        self.sync_flowguard_distance = gcmd.get_float('SYNC_FLOWGUARD_DISTANCE', self.sync_flowguard_distance, minval=1.0)

    def get_test_config(self):
        msg = "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
        msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
        msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
        msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
        msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
        msg += "\nsync_significant_movement_threshold = %.2f" % self.sync_significant_movement_threshold
        msg += "\nsync_flowguard_enabled = %d" % self.sync_flowguard_enabled
        msg += "\nsync_flowguard_band = %.2f" % self.sync_flowguard_band
        msg += "\nsync_flowguard_distance = %.1f" % self.sync_flowguard_distance
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
            self._reset_flowguard()
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

    # This is whether the user has enabled the sync-feedback (the "big" switch)
    def is_enabled(self):
        """
        This is whether the user has enabled the sync-feedback feature (the "big" switch)
        """
        return self.sync_feedback_enabled

    # This is whether the sync-feedback is currently active. It isn't if mmu is not synced to extruder for example
    def is_active(self):
        """
        Returns whether the sync-feedback is currently active (when synced)
        """
        return self.active


    def get_sync_feedback_string(self, state=None, detail=False):
        if state is None:
            state = self.state
        if self.mmu.is_enabled and self.sync_feedback_enabled and (self.active or detail):
            if self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL):
                # Show the latched side to match control behavior
                s = self._last_state_side
                return 'neutral' if s == 0 else ('compressed' if s > 0 else 'tension')
            else:
                # Dual-switch path
                return 'compressed' if float(state) > 0.0 else 'tension' if float(state) < 0.0 else 'neutral'
        return "disabled"

    # End guard enable/disable/reset hooks
    def enable_flowguard(self):
        if self._set_flowguard_active(True):
            self.log_info("MmuSyncFeedbackManager: Flowguard enabled")

    def disable_flowguard(self):
        if self._set_flowguard_active(False):
            self.log_info("MmuSyncFeedbackManager: Flowguard disabled")

    # Relax the filament tension, preferring proportional control if available else sync-feedback sensor switches
    # By default uses gear stepper to achive the result but optionally can use just extruder stepper for
    # extruder entry check using compression sensor
    #   'max_move' is advisory maximum travel distance
    # Return distance moved for correction and success flag
    def adjust_filament_tension(self, use_gear_motor=True, max_move=None):
        has_tension      = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        max_move = max_move or self.sync_feedback_buffer_maxrange

        if has_proportional:
            return self._adjust_filament_tension_proportional() # Doesn't support extruder stepper or max_move

        if has_tension or has_compression:
            return self._adjust_filament_tension_switch(use_gear_motor=use_gear_motor, max_move=max_move)

        return 0, False # Shouldn't get here


    #
    # GCODE Commands -----------------------------------------------------------
    #

    cmd_MMU_FLOWGUARD_help = "Enable/disable Flowguard (clog-tangle detection)"
    cmd_MMU_FLOWGUARD_param_help = (
        "MMU_FLOWGUARD: %s\n" % cmd_MMU_FLOWGUARD_help,
        "ENABLE = [1|0] enable/disable Flowguard clog/tangle detection"
    )
    def cmd_MMU_FLOWGUARD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        help = gcmd.get_int('HELP', 0, minval=0, maxval=1)
        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_FLOWGUARD_param_help), color=True)
            return

        has_tension      = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)

        if self.sync_feedback_enabled and (
            has_proportional
            or (has_compression and has_tension)
        ):
            try:
                if enable == 1:
                    self.enable_flowguard()
                elif enable == 0:
                    self.disable_flowguard()
                else:
                    self.mmu.log_always("Flowguard is %s" % "enabled" if self.flowguard_active else "disabled")
            except Exception as e:
                raise gcmd.error("MMU_ENABLE_FLOWGUARD failed: %s" % e)
        else:
            self.log_warning("Sync feedback manager is disabled or unavailable")

    cmd_MMU_SYNC_FEEDBACK_help = "Controls sync feedback and applies filament tension adjustments"
    cmd_MMU_SYNC_FEEDBACK_param_help = (
        "MMU_SYNC_FEEDBACK: %s\n" % cmd_MMU_SYNC_FEEDBACK_help,
        "ENABLE         = [1|0] enable/disable sync feedback control\n",
        "ADJUST_TENSION = [1|0] apply correction to neutralize filament tension\n",
        "AUTOTUNE       = [1|0] allow saving of autotuned rotation distance"
    )
    def cmd_MMU_SYNC_FEEDBACK(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_bypass(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_FLOWGUARD_param_help), color=True)
            return

        has_tension      = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)

        if not (has_proportional or has_tension or has_compression):
            self.mmu.log_always("No sync-feedback sensors are present/active")
            return

        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        autotune = gcmd.get_int('AUTOTUNE', None, minval=0, maxval=1)
        adjust_tension = gcmd.get_int('ADJUST_TENSION', 0, minval=0, maxval=1)

        if enable is not None:
            self.sync_feedback_enabled = enable

        if autotune is not None:
            self.mmu.autotune_rotation_distance = autotune

        if adjust_tension:
            try:
                with self.wrap_sync_gear_to_extruder(): # Cannot adjust sync feedback sensor if gears are not synced
                    with self.mmu._wrap_suspend_runout_clog_flowguard(): # Avoid spurious runout during tiny corrective moves (unlikely)
                        actual,success = self.adjust_filament_tension()
                        self.reset_sync_starting_state_for_gate(self.mmu.gate_selected) # Will always set rotation_distance
                        if success:
                            self.mmu.log_info("Neutralized tension after moving %.2fmm" % actual)
                        else:
                            self.mmu.log_warning("Moved %.2fmm without neutralizing tenstion")

            except MmuError as ee:
                self.mmu.log_error("Error in MMU_SYNC_FEEDBACK: %s" % str(ee))


    #
    # Internal implementation --------------------------------------------------
    #

    def _telemetry_log_path(self, gate=None):
        if gate is None: gate = self.mmu.gate_selected

    # Starting assumption is that extruder is not moving and measurement is 0mm
    def _reset_extruder_watchdog(self):
        self.extruder_direction = 0 # Extruder not moving to force neutral start position
        self.last_recorded_extruder_position = None
        self.flowguard_last_recorded_extruder_position = None

    # Called periodically to check extruder movement
    def _check_extruder_movement(self, eventtime):
        if self.mmu.is_enabled:
            estimated_print_time = self.mmu.printer.lookup_object('mcu').estimated_print_time(eventtime)
            extruder = self.mmu.toolhead.get_extruder()
            pos = extruder.find_past_position(estimated_print_time)

            if self.last_recorded_extruder_position is None:
                self.last_recorded_extruder_position = pos

            if self.flowguard_last_recorded_extruder_position is None:
                self.flowguard_last_recorded_extruder_position = pos

            # Have we changed direction?
            if abs(pos - self.last_recorded_extruder_position) > self.sync_significant_movement_threshold:
                prev_direction = self.extruder_direction
                self.extruder_direction = (
                    self.mmu.DIRECTION_LOAD if pos > self.last_recorded_extruder_position
                    else self.mmu.DIRECTION_UNLOAD if pos < self.last_recorded_extruder_position
                    else 0
                )
                delta = pos - self.last_recorded_extruder_position
                if self.extruder_direction != prev_direction:
                    self._notify_direction_change(prev_direction, self.extruder_direction)
                    # Feed Flowguard with the positive chunk consumed by the direction-change path
                    if delta > 0.0:
                        self._notify_flowguard_forward_progress(delta)
                        if delta >= self.sync_movement_threshold:
                            self._notify_hit_movement_marker(delta)
                    self.last_recorded_extruder_position = pos
                    self.flowguard_last_recorded_extruder_position = pos

            # Feed auto tuning on sync_movement_threshold intervals
            if (pos - self.last_recorded_extruder_position) >= self.sync_movement_threshold:
                # Ensure we are given periodic notifications to aid autotuning
                self._notify_hit_movement_marker(pos - self.last_recorded_extruder_position)
                self.last_recorded_extruder_position = pos # Move marker

            # Feed flowguard on sync_significant_movement_threshold intervals
            if (pos - self.flowguard_last_recorded_extruder_position) >= self.sync_significant_movement_threshold:
                # Feed Flowguard on forward-only chunks as well
                self._notify_flowguard_forward_progress(pos - self.flowguard_last_recorded_extruder_position)
                self.flowguard_last_recorded_extruder_position = pos # Move marker

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
            self._reset_flowguard()
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
            self._reset_flowguard()
            self._reset_gear_rotation_distance()

    def _handle_sync_feedback(self, eventtime, state):
        if not self.mmu.is_enabled: return

        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)

        if abs(state) <= 1:
            old_state = self.state
            self.state = float(state)
            log = self.mmu.log_trace
            if has_proportional:
                # If proportional sensor is fitted, events are published every 200ms. Demote logging state to stepper log level
                # to prevent spamming of trace level
                log = self.mmu.log_stepper
            log(
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
                if has_proportional and not self.mmu.autotune_rotation_distance: # PAUL ??
                    # Outside deadband: (re)latch to sign immediately
                    if abs(v) >= self.PROP_DEADBAND_THRESHOLD:
                        return self.SYNC_STATE_COMPRESSED if v > self.SYNC_STATE_NEUTRAL else self.SYNC_STATE_EXPANDED
                    # Inside deadband: keep previous side; only go NEUTRAL when crossing neutral (0)
                    if prev == self.SYNC_STATE_COMPRESSED and v <= self.SYNC_STATE_NEUTRAL:
                        return self.SYNC_STATE_NEUTRAL
                    if prev == self.SYNC_STATE_EXPANDED and v >= self.SYNC_STATE_NEUTRAL:
                        return self.SYNC_STATE_NEUTRAL
                    return prev  # hold previous (including NEUTRAL) while hovering in deadband
                else:
                    # Proportional sensor with autotune enabled: emulate switch with small release hysteresis
                    if has_proportional:
                        if prev == self.SYNC_STATE_COMPRESSION:
                            if v > self.PROP_RELEASE_AUTOTUNE_THRESHOLD:
                                return self.SYNC_STATE_COMPRESSION
                            if v <= -self.PROP_DEADBAND_AUTOTUNE_THRESHOLD:
                                return self.SYNC_STATE_TENSION
                            return self.SYNC_STATE_NEUTRAL

                        if prev == self.SYNC_STATE_TENSION:
                            if v < -self.PROP_RELEASE_AUTOTUNE_THRESHOLD:
                                return self.SYNC_STATE_TENSION
                            if v >= self.PROP_DEADBAND_AUTOTUNE_THRESHOLD:
                                return self.SYNC_STATE_COMPRESSION
                            return self.SYNC_STATE_NEUTRAL

                        if v >= self.PROP_DEADBAND_AUTOTUNE_THRESHOLD:
                            return self.SYNC_STATE_COMPRESSION
                        if v <= -self.PROP_DEADBAND_AUTOTUNE_THRESHOLD:
                            return self.SYNC_STATE_TENSION
                        return self.SYNC_STATE_NEUTRAL

                    # Non-proportional path
                    return self.SYNC_STATE_NEUTRAL if abs(v) < self.PROP_DEADBAND_THRESHOLD else (self.SYNC_STATE_COMPRESSION if v > self.SYNC_STATE_NEUTRAL else self.SYNC_STATE_TENSION)

            old_side = self._last_state_side
            new_side = _side(self.state, old_side)

            if new_side != old_side:
                self.last_recorded_extruder_position = None # Reset extruder watchdog position
                self._reset_flowguard()                      # reset flowguard accumulators as we are counting from a new side.
                self._last_state_side = new_side            # switch sides

            if self.sync_feedback_enabled and self.active:
                # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
                # Proportional sensor does not have any switches, hence will bypass the dual switch autotune path
                has_dual_sensors = (
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
                    self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
                )

                # Dual-switch RD autotune path. Use state transitions
                if state != old_state and has_dual_sensors and self.mmu.autotune_rotation_distance and not has_proportional: # PAUL fix
                    self._adjust_clamps(state, old_state)

                # Proportional RD autotune path. Use debounced side threshold transitions
                # Only active when proportional is selected, and autotune enabled.
                if new_side != old_side and has_proportional and self.mmu.autotune_rotation_distance:
                    self._adjust_clamps(new_side, old_side)

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

    # This signifies we have been sitting in same state for longer than the movement threshold so
    # rotation_distance may need an additional nudge. Also allows us to "clamp down" on perfect
    # calibration if we have dual sensors
    def _notify_hit_movement_marker(self, movement):
        # Dynamically inspect sensor availability so we can be reactive to user enable/disable mid print
        has_dual_sensors = (
            self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION) and
            self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        )
        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)

        # Allow nudges for:
        #   - dual sensors + autotune OR
        #   - proportional + autotune.
        # Currently we don't do anything if using fixed multipliers (single sensor case) TODO we could though!
        if not (self.mmu.autotune_rotation_distance and (has_dual_sensors or has_proportional)): # TODO could separate "save autotune" from autotune? # PAUL
            return

        # Handle flowguard trip
        self.flowguard_status = dict(output['flowguard'])
        self.flowguard_status['enabled'] = bool(self.flowguard_enabled)
        f_trigger = self.flowguard_status.get('trigger', None)
        f_reason = self.flowguard_status.get('reason', "")
        if f_trigger:
            if self.flowguard_enabled and self.flowguard_active:
                self.mmu.log_error("FlowGuard detected a %s.\nReason for trip: %s" % (f_trigger, f_reason))

        # Effective state for nudger:
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-latched side in _last_state_side
        effective_state = self.state if (has_dual_sensors and not has_proportional) else self._last_state_side # PAUL

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

        elif effective_state == self.SYNC_STATE_EXPANDED:
            # Tension state too long means filament feed too slow, need to go faster so smaller rotation distance
            # Increase compressed value by fixed % and set new_rd to compressed value
            rd_clamp[2] *= (1 - self.MULTIPLIER_WHEN_STUCK)
            self.mmu.log_debug(
                "MmuSyncFeedbackManager: Extruder moved too far in tension state (%.1fmm). Decreased fast clamp value by %d%% from %.4f to %.4f" % (
                    movement,
                    self.MULTIPLIER_WHEN_STUCK * 100,
                    old_clamp[2],
                    rd_clamp[2]
                )
            )
            # Adjust clamp and use new fast rd that is known to make sensor move towards compressed
            rd_clamp[1] = rd_clamp[2]

        elif effective_state == self.SYNC_STATE_NEUTRAL:
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
                        self.mmu.save_rotation_distance(self.mmu.gate_selected, round(_tuned_rd, 4)) # Round to 4 decimals for saving.
                    # We have found a tuned RD value - widen clamps to prevent oscillation of the tuned RD value
                    # due to the nudge off the trigger point.
                    rd_clamp[0] = _tuned_rd * self.sync_multiplier_high
                    rd_clamp[2] = _tuned_rd * self.sync_multiplier_low
                return _tuned_rd
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
        if not self.sync_feedback_enabled or not self.active: return False

        self.mmu.log_trace( "MmuSyncFeedbackManager: adjust RD? enabled=%s active=%s state=%.3f dir=%d" % (self.sync_feedback_enabled, self.active, self.state, self.extruder_direction) )
        rd_clamp = self.rd_clamps[self.mmu.gate_selected]
        effective_state = self.SYNC_STATE_NEUTRAL # Initialise effective state to neutral (unnecessary, here for code readability)


        # Choose the effective side the sensor is at to drive RD
        # Distinguish between proportional sensor and switch based sensor to derive effective state
        # - Dual switches: use self.state
        # - Proportional: use hysteresis-quantised side in _last_state_side
        if self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL):
            effective_state = self._last_state_side
        else:
            # If not using proportional feedback sensor -> use the hardware state.
            if self.state == self.SYNC_STATE_NEUTRAL:
                effective_state = self.SYNC_STATE_NEUTRAL
            elif self.state == self.SYNC_STATE_COMPRESSED:
                effective_state = self.SYNC_STATE_COMPRESSED
            else:
                effective_state = self.SYNC_STATE_TENSION

        if effective_state == self.SYNC_STATE_NEUTRAL:
            # Start with mid point of previous clamps
            selected_rd = (rd_clamp[0] + rd_clamp[2]) / 2.
        else:
            s = float(effective_state)      # +1 compressed, -1 expanded(tension)
            d = float(self.extruder_direction)        # +1 extrude, -1 retract (logical)
            go_slower = lambda ss, dd: abs(ss - dd) < abs(ss + dd)

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
                (" tuned: %.4f" % rd_clamp[3]) if rd_clamp[3] else ""
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
        has_tension        = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression    = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional   = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        tension_active     = sm.check_sensor(self.mmu.SENSOR_TENSION)
        compression_active = sm.check_sensor(self.mmu.SENSOR_COMPRESSION)

        ss = self.SYNC_STATE_NEUTRAL
        if has_tension and has_compression and not has_proportional:
            # Allow for sync-feedback sensor designs with minimal travel where both sensors can be triggered at same time
            if tension_active == compression_active:
                ss = self.SYNC_STATE_NEUTRAL
            elif tension_active and not compression_active:
                ss = self.SYNC_STATE_EXPANDED
            else:
                ss = self.SYNC_STATE_COMPRESSION
        elif has_compression and not has_tension and not has_proportional:
            ss = self.SYNC_STATE_COMPRESSION if compression_active else self.SYNC_STATE_TENSION
        elif has_tension and not has_compression and not has_proportional:
            ss = self.SYNC_STATE_TENSION if tension_active else self.SYNC_STATE_COMPRESSION
        else:
            # Proportional sensor
            if has_proportional:
                ss = self.SYNC_STATE_NEUTRAL if abs(self.state) < self.PROP_DEADBAND_THRESHOLD else (
                    self.SYNC_STATE_COMPRESSED if self.state > 0 else self.SYNC_STATE_EXPANDED
                )
            else: # No sensors at all
                ss = self.SYNC_STATE_NEUTRAL
        self.state = ss
        # Update quantised cached side for later transition detection for the proportional sensor
        # that uses hysteresis-latched side in _last_state_side
        self._last_state_side = ss


    #
    # Flowguard implementation (proportional filament pressure sensor clog and tangle detection)
    #

    # Set flowguard active/inactive
    # Return True is successful
    def _set_flowguard_active(self, enabled, reason=None):
        sm = self.mmu.sensor_manager
        has_tension      = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        sufficent_sensors = has_proportional or (has_compression and has_tension) # PAUL TODO not yet coded to support dual switch sensor setup but it seems like it should be possible??
        if not self.sync_feedback_enabled or not sufficent_sensors:
            return False

        # Respect config: if Flowguard is disabled, ignore requests to change state.
        if not self.sync_flowguard_enabled:
            # Make sure runtime latch reflects disabled state
            self.flowguard_active = 0
            if self._flowguard_arm_timer:
                self.mmu.reactor.update_timer(self._flowguard_arm_timer, self.mmu.reactor.NEVER)
            self._clear_pending_flowguard(reason="disabled in config")
            return False

        # Reset Flowguard unconditionally. Will also attempt to cancel any pending scheduled
        # pauses (no guarantee that it will succeed before klipper schedules the pause.)
        self._clear_pending_flowguard(reason=("arming" if enabled else "disabling"))

        # Cancel any previously scheduled deferred arm
        if self._flowguard_arm_timer:
            self.mmu.reactor.update_timer(self._flowguard_arm_timer, self.mmu.reactor.NEVER)

        if enabled:
            # Rebaseline BEFORE arming; this guarantees the first watchdog tick is a no-op baseline seed.
            self._reset_extruder_watchdog()     # last_recorded_extruder_position = None
            self._reset_current_sync_state()

            # Defer arming by a short reactor delay to avoid same-cycle interleaving
            delay_s = 1.00  # ~1s; stay well clear of any immediate resume print operations

            # Arm flowguard in a deferred manner to ensure any resume activities are complete (safeguard)
            def _arm(evt):
                self.flowguard_active = 1
                self.mmu.log_debug("Flowguard: enabled (deferred)%s" % ((" (%s)" % reason) if reason else ""))
                return self.mmu.reactor.NEVER

            # Register and schedule the one-shot arm
            self._flowguard_arm_timer = self.mmu.reactor.register_timer(_arm)
            now = self.mmu.reactor.monotonic()
            self.mmu.reactor.update_timer(self._flowguard_arm_timer, now + delay_s)

            self.mmu.log_debug("Flowguard: enable requested; arming in %.2fs%s" %(delay_s, (" (%s)" % reason) if reason else ""))

            # Ensure latch remains inactive until the deferred arm fires
            self.flowguard_active = 0

        else:
            self.flowguard_active = 0
            self.mmu.log_info("Flowguard: disabled%s" % ((" (%s)" % reason) if reason else ""))

        return True

    #  Cancel any scheduled Flowguard action (pause) and optionally reset accumulation.
    #  This does NOT change sync_flowguard_enabled/active; it only clears pending/triggered state.
    #  Note that if a pause has been scheduled and executed, this function cannot undo it.
    #  Therefore, make sure flowguard is disabled before any operations that may over-drive
    #  the sync feedback sensor!
    #  reason: Optional string appended to the log.
    #  Returns True if something was actually cleared/canceled; False otherwise.
    def _clear_pending_flowguard(self, reason=None):
        cleared = False

        # Cancel any scheduled one-shot pause
        if self._flowguard_timer_handle:
            self.mmu.reactor.update_timer(self._flowguard_timer_handle, self.mmu.reactor.NEVER)
            cleared = True

        # Clear pending-action flag
        if self._flowguard_action_pending:
            self._flowguard_action_pending = False
            cleared = True

        # Clear "triggered" latch
        if self._flowguard_triggered:
            self._flowguard_triggered = False
            cleared = True

        # Reset accumulation counter
        if self._flowguard_forward_mm != 0.0:
            cleared = True
        self._flowguard_forward_mm = 0.0

        # Log outcome
        try:
            self.mmu.log_info("Flowguard pending actions cleared%s" % ((" (%s)" % reason) if reason else ""))
        except Exception:
            pass

        return cleared

    # Reset flowguard measuring state when initialising and also when switching sides in
    # the proportional filament sensor. This does not clear the flowguard state and timers
    # For that use _clear_pending_flowguard.
    def _reset_flowguard(self):
        self._flowguard_forward_mm = 0.0
        self._flowguard_triggered = False
        self.flowguard_last_recorded_extruder_position = None

    def _notify_flowguard_forward_progress(self, movement):
        if not (self.sync_flowguard_enabled and self.flowguard_active):
            return
        if movement <= 0.0:
            return
        # If we already decided to act or have a pending action, stop accumulating/logging
        if self._flowguard_triggered or self._flowguard_action_pending:
            return

        # accumulate only when hugging an end
        if abs(self.state) >= self.sync_flowguard_band:
            self._flowguard_forward_mm += movement
            self.mmu.log_info("Flowguard: +%.1fmm at |state|=%.3f -> total=%.1f/%.1f" % (movement, abs(self.state), self._flowguard_forward_mm, self.sync_flowguard_distance))
            #IG ToDo: REVERT LOGGING to debug
            if self._flowguard_forward_mm >= self.sync_flowguard_distance:
                self._trigger_flowguard_pause()
        else:
            if self._flowguard_forward_mm > 0.0:
                self.mmu.log_info("Flowguard: left band; resetting (total was %.1fmm)" % (self._flowguard_forward_mm,)) #IG ToDo: REVERT LOGGING to debug
            self._reset_flowguard()

    def _trigger_flowguard_pause(self):
        if self._flowguard_triggered:
            return
        self._flowguard_triggered = True
        reason = (
            "Proportional sensor near end of travel during forward feed "
            "(accum %.1fmm ≥ %.1fmm; |state|=%.3f)" % (self._flowguard_forward_mm, self.sync_flowguard_distance, abs(self.state))
        )
        self.mmu.log_always("MmuSyncFeedbackManager: Flowguard triggered: " + reason)

        # Defer the actual action to the reactor to avoid event-context races
        self._schedule_flowguard_action(delay_s=0.15)

    # Flowguard print pause reactor scheduling (one-shot)
    def _schedule_flowguard_action(self, delay_s=0.15):
        if not self._flowguard_timer_handle:
            self._flowguard_timer_handle = self.mmu.reactor.register_timer(self._flowguard_timer)

        if self._flowguard_action_pending:
            return  # Already queued

        self._flowguard_action_pending = True
        now = self.mmu.reactor.monotonic()
        # Enforce a minimum delay to avoid same-cycle execution in flush context
        delay = max(float(delay_s), 0.05)
        self.mmu.reactor.update_timer(self._flowguard_timer_handle, now + delay)
        self.mmu.log_debug("Flowguard: deferred pause scheduled in %.2fs" % (float(delay),))


    def _flowguard_timer(self, eventtime):
        # One-shot; run the configured action outside the event callback context
        if not self._flowguard_action_pending:
            return self.mmu.reactor.NEVER

        self._flowguard_action_pending = False

        try:
            # Run the pause portion of pause_resume execute immediately.
            pause_resume = self.mmu.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            self.mmu.gcode.run_script('MMU_PAUSE MSG="Flowguard detected clog or tangle"')
        except Exception:
            self.mmu.log_always("Flowguard: failed to invoke MMU_PAUSE")

        return self.mmu.reactor.NEVER

    # Helper to relax filament tension using the sync-feedback buffer. This can be performed either with the
    # gear motor (default) or extruder motor (which is good as an extruder loading check)
    # Returns distance moved and whether operation was successful (or None if not performed)
    def _adjust_filament_tension_switch(self, use_gear_motor=True, max_move=None):
        fhomed = None
        actual = 0
        tension_active = self.mmu.sensor_manager.check_sensor(self.mmu.SENSOR_TENSION)
        compression_active = self.mmu.sensor_manager.check_sensor(self.mmu.SENSOR_COMPRESSION)

        max_move = max_move or self.sync_feedback_buffer_maxrange
        self.mmu.log_debug("Monitoring extruder entrance transistion for up to %.1fmm..." % max_move)
        if (compression_active is True) != (tension_active is True): # Equality means already neutral

            if use_gear_motor:
                motor = "gear"
                if compression_active:
                    self.mmu.log_debug("Relaxing filament compression")
                elif tension_active:
                    self.mmu.log_debug("Relaxing filament tension")
            else:
                motor = "extruder"
                self.mmu.log_debug("Monitoring extruder entry transistion...")
            speed = min(self.mmu.gear_homing_speed, self.mmu.extruder_homing_speed) # Keep this tension adjustment slow

            if self.sync_feedback_buffer_range == 0:
                # Special case for buffers whose neutral point overlaps both sensors. I.e. both sensors active
                # is the neutral point. This requires different homing logic
                if compression_active:
                    direction = -1 if use_gear_motor else 1
                    actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to tension sensor", max_move * direction, speed=speed, motor=motor, homing_move=1, endstop_name=self.mmu.SENSOR_TENSION)

                elif tension_active:
                    direction = 1 if use_gear_motor else -1
                    actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to compression sensor", max_move * direction, speed=speed, motor=motor, homing_move=1, endstop_name=self.mmu.SENSOR_COMPRESSION)
            else:
                # Normally configured buffer with neutral (no-trigger) gap
                direction = 0
                if compression_active:
                    direction = -1 if use_gear_motor else 1
                    actual,fhomed,_,_ = self.mmu.trace_filament_move("Reverse homing off compression sensor", max_move * direction, speed=speed, motor=motor, homing_move=-1, endstop_name=self.mmu.SENSOR_COMPRESSION)

                elif tension_active:
                    direction = 1 if use_gear_motor else -1
                    actual,fhomed,_,_ = self.mmu.trace_filament_move("Reverse homing off tension sensor", max_move * direction, speed=speed, motor=motor, homing_move=-1, endstop_name=self.mmu.SENSOR_TENSION)

            if fhomed:
                if use_gear_motor:
                    # Move just a little more to find perfect neutral spot between sensors
                    _,_,_,_ = self.mmu.trace_filament_move("Centering sync feedback buffer", (max_move * direction) / 2.)
            else:
                self.mmu.log_debug("Failed to reach neutral filament tension after moving %.1fmm" % max_move)

        return actual, fhomed


    # Helper to relax filament tension using the proportional sync-feedback buffer.
    # Only use when no tension/compression switches are present.
    # Do not mix with _adjust_filament_tension_switch() (switch-based) in the same sequence.
    #
    # Returns: actual distance moved (mm), success bool
    def _adjust_filament_tension_proportional(self):

        # nudge_mm:     per-move adjustment distance in mm (small feed or retract)
        # neutral_band: absolute value of proportional sensor reading considered "neutral".
        #               This can be loosely interpreted as a % over the max range of detection of the sensor.
        #               For example for a sensor with 14mm range, a 0.15 tolerance is approx 1.4mm either side of centre.
        # settle_time:  delay between moves to allow sensor feedback to update
        # timeout_s:    hard stop to avoid hanging if the sensor never clears
        neutral_band = self.sync_proportional_neutral_band
        settle_time = self.sync_proportional_settle_time
        timeout = self.sync_proportional_timeout

        # Wait for move queues to clear
        self.mmu.mmu_toolhead.quiesce()

        # sanity-check parameters before doing anything
        # neutral band needs to have a non zero and non trivial value. Enforce 5% (0.05)
        # as the lower limit of acceptable neutral band tolerance.
        if neutral_band < 0.05:
            neutral_band = 0.05

        # maxrange is full end-to-end sensor span; use half as the per-side budget from neutral to either end
        maxrange_span_mm = float(self.sync_feedback_buffer_maxrange)
        if maxrange_span_mm <= 0.0:
            self.mmu.log_debug("Proportional adjust skipped: buffer maxrange <= 0")
            return 0., False
        per_side_budget_mm = 0.5 * maxrange_span_mm
        nudge_mm = per_side_budget_mm * neutral_band

        # Cap total nudge iterations to stay within the overall sensor range
        max_steps = math.ceil(maxrange_span_mm / nudge_mm)

        moved_total_mm   = 0.0  # total net distance moved during this adjustment
        moved_nudges_mm  = 0.0  # sum of all nudge moves
        moved_initial_mm = 0.0  # size of the initial proportional move (if any)
        steps            = 0    # total moves performed
        t_start          = self.mmu.reactor.monotonic()

        # --- Initial proportional correction ---
        # Negative sensor state = tension -> feed filament. positive sensor state = compression -> retract filament
        prop_state = float(self.state)  # [-1..+1], 0 ≈ neutral
        if abs(prop_state) > neutral_band:
            # Iinitial move distance as a proportion to how off centre we are based on the sensor readings.
            # this will get the sensor close but likely will need a few fine adjustments (nudges) to get it
            # within the centre range depending on how large the bowden tube slack is.
            initial_move_mm = -prop_state * per_side_budget_mm
            if abs(initial_move_mm) >= nudge_mm:
                self.mmu.trace_filament_move(
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
        prop_state = float(self.state)
        if abs(prop_state) <= neutral_band:
            self.mmu.log_info(
                "Proportional adjust: neutral after initial "
                "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=yes)" %
                (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
            )
            return moved_total_mm, True

        # --- Fine adjustment loop (nudges) ---
        while abs(moved_total_mm) < maxrange_span_mm and steps < max_steps:
            prop_state = float(self.state)
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
                prop_state = float(self.state)
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

            self.mmu.trace_filament_move(
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
        final_state = float(self.state)
        success = abs(final_state) <= neutral_band
        self.mmu.log_info(
            "Proportional adjust: complete "
            "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=%s)" %
            (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, final_state, "yes" if success else "no")
        )
        return moved_total_mm, success
