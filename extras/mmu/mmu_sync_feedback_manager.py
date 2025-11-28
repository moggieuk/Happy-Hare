# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle sync-feedback and adjustment of gear stepper rotation distance
#       to keep MMU in sync with extruder as well as some filament tension routines.
#
# Flowguard: It also implements protection for all modes/sensor types that will trigger
#            on clog (at extruder) or tangle (at MMU) conditions.
#
# Autotune: An autotuning option can be enabled for dynamic tuning (and persistence) of
#           calibrated MMU gear rotation_distance.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ..                   import mmu_machine
from ..mmu_machine        import MmuToolHead
from ..mmu_sensors        import MmuRunoutHelper

# MMU subcomponent clases
from .mmu_shared          import *
from .mmu_logger          import MmuLogger
from .mmu_selector        import *
from .mmu_test            import MmuTest
from .mmu_utils           import DebugStepperMovement, PurgeVolCalculator
from .mmu_sensor_manager  import MmuSensorManager
from .mmu_sync_controller import SyncControllerConfig, SyncController


class MmuSyncFeedbackManager:

    SF_STATE_NEUTRAL      = 0
    SF_STATE_COMPRESSION  = 1
    SF_STATE_TENSION      = -1
    
    def __init__(self, mmu):
        self.mmu = mmu

        self.state = float(self.SF_STATE_NEUTRAL) # 0 = Neutral (but a float to allow proportional support)
        self.active  = False                          # Actively operating?

        # Process config
        self.sync_feedback_enabled           = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range      = self.mmu.config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange   = self.mmu.config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_feedback_speed_multiplier  = self.mmu.config.getfloat('sync_feedback_speed_multipler', 5, minval=1, maxval=50)
        self.sync_feedback_boost_multiplier  = self.mmu.config.getfloat('sync_feedback_boost_multipler', 5, minval=1, maxval=50)
        self.sync_feedback_extrude_threshold = self.mmu.config.getfloat('sync_feedback_extrude_threshold', 5, above=1.)
        self.sync_feedback_debug_log         = self.mmu.config.get('SF_FEEDBACK_DEBUG_LOG', self.sync_feedback_debug_log, "")

        # Flowguard
        self.flowguard_enabled    = self.mmu.config.getint('flowguard_enabled', 1, minval=0, maxval=1)
        self.flowguard_max_relief = self.mmu.config.getfloat('flowguard_max_relief', 8, above=1.)
        self.flowguard_max_motion = self.mmu.config.getfloat('flowguard_max_motion', 80, above=10.)

        # Setup events for managing motor synchronization
        self.mmu.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
        self.mmu.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
        self.mmu.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)

        # Register GCODE commands ---------------------------------------------------------------------------

        self.mmu.gcode.register_command('MMU_SF_FEEDBACK', self.cmd_MMU_SF_FEEDBACK, desc=self.cmd_MMU_SF_FEEDBACK_help)
        self.mmu.gcode.register_command('MMU_FLOWGUARD',  self.cmd_MMU_FLOWGUARD, desc=self.cmd_MMU_FLOWGUARD_help)

        self.extruder_monitor = ExtruderMonitor(mmu)


    #
    # Standard mmu manager hooks...
    #

    def reinit(self):
        self.extruder_monitor.enable()
        

    def set_test_config(self, gcmd):
        self.sync_feedback_enabled           = gcmd.get_int('SF_FEEDBACK_ENABLED', self.sync_feedback_enabled, minval=0, maxval=1)
        self.sync_feedback_buffer_range      = gcmd.get_float('SF_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
        self.sync_feedback_buffer_maxrange   = gcmd.get_float('SF_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
        self.sync_feedback_speed_multiplier  = gcmd.get_float('SF_FEEDBACK_SPEED_MULTIPLER', self.sync_feedback_speed_multiplier, minval=1., maxval=50)
        self.sync_feedback_boost_multiplier  = gcmd.get_float('SF_FEEDBACK_BOOST_MULTIPLER', self.sync_feedback_boost_multiplier, minval=1., maxval=50)
        self.sync_feedback_extrude_threshold = gcmd.get_float('SF_FEEDBACK_EXTRUDE_THRESHOLD', self.sync_feedback_extrude_threshold, above=1.)
        self.sync_feedback_debug_log         = gcmd.get('SF_FEEDBACK_DEBUG_LOG', self.sync_feedback_debug_log, "")

        flowguard_enabled = gcmd.get_int('FLOWGUARD_ENABLED', self.flowguard_enabled, minval=0, maxval=1)
        if flowguard_enabled != self.flowguard_enabled:
            if flowguard_enabled:
                self.enable_flowguard()
            else:
                self.disable_flowguard()
        self.flowguard_max_relief = gcmd.get_float('FLOWGUARD_MAX_RELIEF', self.flowguard_max_relief, above=1.)
        self.flowguard_max_motion = gcmd.get_float('FLOWGUARD_MAX_MOTION', self.flowguard_max_motion, above=10.)


    def get_test_config(self):
        msg  = "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
        msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
        msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
        msg += "\nsync_feedback_speed_multiplier = %.1f" % self.sync_feedback_speed_multiplier
        msg += "\nsync_feedback_boost_multiplier = %.1f" % self.sync_feedback_boost_multiplier
        msg += "\nsync_feedback_extrude_threshold = %.1f" % self.sync_feedback_extrude_threshold
        msg += "\nsync_feedback_debug_log = %s" % (gcmd.get('SF_FEEDBACK_DEBUG_LOG', self.sync_feedback_debug_log)

        msg += "\nflowguard_enabled = %d" % self.sync_flowguard_enabled
        msg += "\nflowguard_max_relief = %.1f" % self.flowguard_max_relief
        msg += "\nflowguard_max_motion = %.1f" % self.flowguard_max_motion
        return msg


    def check_test_config(self, param):
        return vars(self).get(param) is None

    #
    # Sync feedback manager public access...
    #

    def set_default_rd(self, gate):
        """
        Ensure correct starting rotation distance
        """
        rd = self.mmu.get_rotation_distance(self.mmu.gate_selected)
        if gate >= 0:
            self.mmu.log_debug("MmuSyncFeedbackManager: Setting default rotation distance for gate %d to %.4f" % (gate, rd))
        self.mmu.set_rotation_distance(rd)


    def is_enabled(self):
        """
        This is whether the user has enabled the sync-feedback (the "big" switch)
        """
        return self.sync_feedback_enabled


    def is_active(self):
        """
        Returns whether the sync-feedback is currently active.
        """
        return self.active


    def get_sync_bias_raw(self):
        return float(self.state) # TODO separate sensor_value(float) from state(int)

    def get_sync_bias_modelled(self): # TODO to allow prediction for UI
        return self.get_sync_bias_raw()

    def get_sync_feedback_string(self, state=None, detail=False):
        if tension is None:
            state = self.state

        if self.mmu.is_enabled and self.sync_feedback_enabled and (self.active or detail):
            # Polarity varies slightly between modes on proportional sensor so ask controller
            polarity = self.ctrl.polarity()
            return 'compressed' if polarity > 0 else 'tension' if polarity < 0 else 'neutral'
        return "disabled"


    def enable_flowguard(self):
        if not self.flowguard_enabled:
            self.flowguard_enabled = True
            self.ctrl.flowguard.reset()
            self.log_info("MmuSyncFeedbackManager: Flowguard monitoring enabled")
        else:
            self.log_info("MmuSyncFeedbackManager: Flowguard already enabled")


    def disable_flowguard(self):
        if self.flowguard_enabled:
            self.flowguard_enabled = False
            self.log_info("MmuSyncFeedbackManager: Flowguard monitoring disabled")
        else:
            self.log_info("MmuSyncFeedbackManager: Flowguard already disabled")


    def adjust_filament_tension(self, use_gear_motor=True, max_move=None):
        """
        Relax the filament tension, preferring proportional control if available else sync-feedback sensor switches.
        By default uses gear stepper to achive the result but optionally can use just extruder stepper for
        extruder entry check using compression sensor 'max_move' is advisory maximum travel distance
        Return distance moved for correction and success flag
        """
        has_tension      = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        max_move = max_move or self.sync_feedback_buffer_maxrange

        if has_proportional:
            return self._adjust_filament_tension_proportional() # Doesn't support extruder stepper or max_move

        if has_tension or has_compression:
            return self._adjust_filament_tension_switch(use_gear_motor=use_gear_motor, max_move=max_move)

        # All sensors must be disabled...
        return 0, False
        

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

        if self.sync_feedback_enabled:
            if enable == 1:
                self.enable_flowguard()
            elif enable == 0:
                self.disable_flowguard()
            else:
                self.mmu.log_always("Flowguard monitoring is %s" % "enabled" if self.flowguard_active else "disabled")
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
                        if success:
                            self.mmu.log_info("Neutralized tension after moving %.2fmm" % actual)
                        else:
                            self.mmu.log_warning("Moved %.2fmm without neutralizing tenstion")

            except MmuError as ee:
                self.mmu.log_error("Error in MMU_SYNC_FEEDBACK: %s" % str(ee))


    #
    # Internal implementation --------------------------------------------------
    #

    def _handle_mmu_synced(self, eventtime):
        """
        Event indicating that gear stepper is now synced with extruder
        """
        if not self.mmu.is_enabled: return

        msg = "MmuSyncFeedbackManager: Synced MMU to extruder%s" % (" (sync feedback activated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if self.active: return

        # Enable sync feedback
        self.active = True
        self.new_autotuned_rd = None

        # The controller logic is in a completely standalone module for simulation
        # and debugging purposes so hook it in here with current config
        rd_start = self.mmu.get_rotation_distance(self.mmu.gate_selected)
        cfg = SyncControllerConfig(
            log_sync = (self.sync_feedback_debug_log != ""),
            log_file = self.sync_feedback_debug_log,
            buffer_range_mm = self.sync_feedback_buffer_range,
            buffer_max_range_mm = self.sync_feedback_buffer_maxrange,
            sensor_type = self._get_sensor_type(),
            rd_start = rd_start,
            flowguard_relief_mm = self.flowguard_max_relief,
            flowguard_motion_mm = self.flowguard_max_motion,
        )
        self.ctrl = SyncController(cfg)

        # Reset controller with initial rd and sensor reading
        self.state = self._get_sensor_state()
        status = self.ctrl.reset(enventtime, rd_start, self.state)
        self._process_status(status) # May adjust rotation_distance

        # Turn on extruder movement events
        self.extruder_monitor.register_callback(self._handle_extruder_movement, self.sync_feedback_extrude_threshold)


    def _handle_mmu_unsynced(self, eventtime):
        """
        Event indicating that gear stepper has been unsynced from extruder
        """
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return

        msg = "MmuSyncFeedbackManager: Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if not self.active: return

        # Deactivate sync feedback
        self.active = False

        if self.new_autotuned_rd is not None:
            self.mmu.log_info("MmuSyncFeedbackManager: Persisted Autotune rd recommendation: %.4f\n" % self.new_autotuned_rd)
            # PAUL TODO persist and adjust bowden length here -- add bowden adjustment to log_info

        # Restore default (last tuned) rotation distance
        self.set_default_rd(self, self.mmu.current_gate)

        # Optional but let's turn off extruder movement events
        extruder_monitor.remove_callback(self._handle_extruder_movement)


    def _handle_extruder_movement(self, eventtime, move):
        """
        Event call when extruder has moved more than threshold. This also allows for
        periodic rotation_distance adjustment, autotune and flowguard checking
        """
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return

        state = self._get_sensor_state()
        status = self.sync_controller.update(eventtime, move, state)
        self._process_status(status)


    def _handle_sync_feedback(self, eventtime, state):
        """
        Event call when sync-feedback discrete state changes.
        'state' should be -1 (tension), 0 (neutral), 1 (compressed)
        or can be a proportional float value between -1.0 and 1.0
        """
        self.state = state
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return

        msg = "MmuSyncFeedbackManager: Sync tension changed from %s to %s" % (" (sync feedback deactivated)" if self.sync_feedback_enabled else "")
        msg = "MmuSyncFeedbackManager: Sync tension changed from %s to %s%s" % (" (sync feedback deactivated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        move = self.extruder_monitor.get_and_reset_accumulated(self._handle_extruder_movement)
        status = self.sync_controller.update(eventtime, move, state)
        self._process_status(status)


    def _process_status(self, status):
        """
        Common logic to process the rd recommendations of the sync controller
        """
        output = status['output']

        # Handle flowguard trip
        flowgurd = output['flowguard']
        flowguard_trigger = flowguard.get('trigger', None):
        if flowguard_trigger:
            if self.flowguard_enabled:
                self.mmu.log_error("MmuSyncFeedbackManager: Flowguard detected a %s.\nReason for trip: %s" % (flowguard_trigger, flowguard['reason']))
                self.mmu.log_error("PAUL: flowguard trip. TODO TODO TODO")
            else:
                self.mmu.log_debug("MmuSyncFeedbackManager: Flowguard detected a %s, but handling is disabled.\nReason for trip: %s" % (flowguard_trigger, flowguard['reason']))
            self.ctrl.flowguard.reset()

        # Update instaneous gear stepper rotation_distance
        if output['rd_current'] != output['rd_prev']:
            self.mmu.log_debug("MmuSyncFeedbackManager: Updating rotation distance for gate %d from %.4f to %.4f" % (gate, rd_prev, rd_current))
            self.mmu.set_rotation_distance(rd)
        
        # Handle new autotune suggestions
        autotune = output['autotune']
        rd = autotune.get('rd', None)
        note = autotune.get('note', None)
        save = autotune.get('save', None)
        if rd is not None:
            if save and self.mmu.autotune_rotation_distance:
                self.mmu.log_debug("MmuSyncFeedbackManager: Autotune recommended new rd: %.4f\n%s\nThis will be persisted and bowden length updated when extruder is unsynced" % (rd, note))
                self.new_autotune_rd= rd
            else:
                self.mmu.log_debug("MmuSyncFeedbackManager: Autotune recommended new rd: %.4f (not saved)\n%s" % (rd, note))


    def _get_sensor_state(self):
        """
        Get current tnsion state based on current sensor feedback.
        Returns float in range [-1.0 .. 1.0]
        """
        sm = self.mmu.sensor_manager
        has_tension        = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression    = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional   = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        tension_active     = sm.check_sensor(self.mmu.SENSOR_TENSION)
        compression_active = sm.check_sensor(self.mmu.SENSOR_COMPRESSION)

        ss = self.SF_STATE_NEUTRAL

        if has_proportional:
            sm.get_sensor(self.mmu.SENSOR_PROPORTIONAL) # MOGGIE
            return 0 # PAUL TODO hook up to sensor value

        if has_tension and has_compression:
            # Allow for sync-feedback sensor designs with minimal travel where both sensors can be triggered at same time
            if tension_active == compression_active:
                ss = self.SF_STATE_NEUTRAL
            elif tension_active and not compression_active:
                ss = self.SF_STATE_TENSION
            else:
                ss = self.SF_STATE_COMPRESSION
        elif has_compression and not has_tension:
            ss = self.SF_STATE_COMPRESSION if compression_active else self.SF_STATE_TENSION
        elif has_tension and not has_compression:
            ss = self.SF_STATE_TENSION if tension_active else self.SF_STATE_COMPRESSION
        return ss


    def _get_sensor_type(self):
        """
        Return symbolic sensor type based on current active sensors
          "P" => proportional z ∈ [-1, +1]; enables EKF
          "D" => discrete dual-switch z ∈ {-1,0,+1}; Optional EKF
          "CO" => compression-only switch z ∈ {0,+1}
          "TO" => tension_only switch z ∈ {-1,0}
        """
        has_tension        = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression    = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional   = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        return (
            "P" if has_proportional else
            "D" if has_compression and has_tension else
            "CO" if has_compression else
            "TO" if has_tension
        )


    def _adjust_filament_tension_switch(self, use_gear_motor=True, max_move=None):
        """
        Helper to relax filament tension using the sync-feedback buffer. This can be performed either with the
        gear motor (default) or extruder motor (which is good as an extruder loading check)
        Returns distance moved and whether operation was successful (or None if not performed)
        """
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
        prop_state = float(self.state)  # [-1..+1], 0 ≈ neutral  # PAUL TODO
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
        prop_state = float(self.state)  # PAUL TODO
        if abs(prop_state) <= neutral_band:
            self.mmu.log_info(
                "Proportional adjust: neutral after initial "
                "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=yes)" %
                (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, prop_state)
            )
            return moved_total_mm, True

        # --- Fine adjustment loop (nudges) ---
        while abs(moved_total_mm) < maxrange_span_mm and steps < max_steps:
            prop_state = float(self.state)  # PAUL TODO
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
                prop_state = float(self.state)  # PAUL TODO
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
