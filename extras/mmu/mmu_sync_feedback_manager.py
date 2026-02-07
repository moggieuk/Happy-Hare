# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle sync-feedback and adjustment of gear stepper rotation distance
#       to keep MMU in sync with extruder as well as some filament tension routines.
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

# MMU subcomponent clases
from .mmu_sync_controller  import SyncControllerConfig, SyncController
from .mmu_extruder_monitor import ExtruderMonitor
from .mmu_shared           import MmuError

class MmuSyncFeedbackManager:

    SF_STATE_NEUTRAL     = 0
    SF_STATE_COMPRESSION = 1
    SF_STATE_TENSION     = -1
    
    def __init__(self, mmu):
        self.mmu = mmu
        self.mmu.managers.append(self)

        self.estimated_state = float(self.SF_STATE_NEUTRAL)
        self.active = False           # Sync-feedback actively operating?
        self.flowguard_active = False # FlowGuard armed?
        self.ctrl = None
        self.flow_rate = 100.         # Estimated % flowrate (calc only for proportional sensors)

        # Process config
        self.sync_feedback_enabled           = self.mmu.config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range      = self.mmu.config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange   = self.mmu.config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_feedback_speed_multiplier  = self.mmu.config.getfloat('sync_feedback_speed_multiplier', 5, minval=1, maxval=50)
        self.sync_feedback_boost_multiplier  = self.mmu.config.getfloat('sync_feedback_boost_multiplier', 5, minval=1, maxval=50)
        self.sync_feedback_extrude_threshold = self.mmu.config.getfloat('sync_feedback_extrude_threshold', 5, above=1.)
        self.sync_feedback_debug_log         = self.mmu.config.getint('sync_feedback_debug_log', 0)
        self.sync_feedback_force_twolevel    = self.mmu.config.getint('sync_feedback_force_twolevel', 0) # Not exposed

        # FlowGuard
        self.flowguard_enabled               = self.mmu.config.getint('flowguard_enabled', 1, minval=0, maxval=1)
        self.flowguard_max_relief            = self.mmu.config.getfloat('flowguard_max_relief', 8, above=1.)
        self.flowguard_encoder_mode          = self.mmu.config.getint('flowguard_encoder_mode', 2, minval=0, maxval=2)
        self.flowguard_encoder_max_motion    = self.mmu.config.getfloat('flowguard_encoder_max_motion', 20, above=0.)

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
        self.extruder_monitor.enable()


    def handle_connect(self):
        self._init_controller()


    def handle_disconnect(self):
        pass
        

    def set_test_config(self, gcmd):
        if self.has_sync_feedback():
            self.sync_feedback_enabled           = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled, minval=0, maxval=1)
            self.sync_feedback_buffer_range      = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
            self.sync_feedback_buffer_maxrange   = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
            self.sync_feedback_speed_multiplier  = gcmd.get_float('SYNC_FEEDBACK_SPEED_MULTIPLIER', self.sync_feedback_speed_multiplier, minval=1., maxval=50)
            self.sync_feedback_boost_multiplier  = gcmd.get_float('SYNC_FEEDBACK_BOOST_MULTIPLIER', self.sync_feedback_boost_multiplier, minval=1., maxval=50)
            self.sync_feedback_extrude_threshold = gcmd.get_float('SYNC_FEEDBACK_EXTRUDE_THRESHOLD', self.sync_feedback_extrude_threshold, above=1.)
            self.sync_feedback_debug_log         = gcmd.get_int('SYNC_FEEDBACK_DEBUG_LOG', self.sync_feedback_debug_log, minval=0, maxval=1)

            flowguard_enabled = gcmd.get_int('FLOWGUARD_ENABLED', self.flowguard_enabled, minval=0, maxval=1)
            if flowguard_enabled != self.flowguard_enabled:
                self._config_flowguard_feature(flowguard_enabled)
            self.flowguard_max_relief = gcmd.get_float('FLOWGUARD_MAX_RELIEF', self.flowguard_max_relief, above=1.)

        if self.mmu.has_encoder():
            mode = gcmd.get_int('FLOWGUARD_ENCODER_MODE', self.flowguard_encoder_mode, minval=0, maxval=2)
            if mode != self.flowguard_encoder_mode:
                self.flowguard_encoder_mode = mode
                self.set_encoder_mode()
            self.flowguard_encoder_max_motion = gcmd.get_float('FLOWGUARD_ENCODER_MAX_MOTION', self.flowguard_encoder_max_motion, above=0.)


    def get_test_config(self):
        msg  = ""
        if self.has_sync_feedback():
            msg += "\nsync_feedback_enabled = %d" % self.sync_feedback_enabled
            msg += "\nsync_feedback_buffer_range = %.1f" % self.sync_feedback_buffer_range
            msg += "\nsync_feedback_buffer_maxrange = %.1f" % self.sync_feedback_buffer_maxrange
            msg += "\nsync_feedback_speed_multiplier = %.1f" % self.sync_feedback_speed_multiplier
            msg += "\nsync_feedback_boost_multiplier = %.1f" % self.sync_feedback_boost_multiplier
            msg += "\nsync_feedback_extrude_threshold = %.1f" % self.sync_feedback_extrude_threshold
            msg += "\nsync_feedback_debug_log = %d" % self.sync_feedback_debug_log
    
            msg += "\n\nFLOWGUARD:"
            msg += "\nflowguard_enabled = %d" % self.flowguard_enabled
            msg += "\nflowguard_max_relief = %.1f" % self.flowguard_max_relief

        if self.mmu.has_encoder():
            msg += "\nflowguard_encoder_mode = %d" % self.flowguard_encoder_mode
            msg += "\nflowguard_encoder_max_motion = %.1f" % self.flowguard_encoder_max_motion
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

        rd = self.mmu.calibration_manager.get_gear_rd(gate)
        self.mmu.log_debug("MmuSyncFeedbackManager: Setting default rotation distance for gate %d to %.4f" % (gate, rd))
        self.mmu.set_gear_rotation_distance(rd)


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


    def activate_flowguard(self, eventtime):
        if self.flowguard_enabled and not self.flowguard_active:
            self.flowguard_active = True
            # This resets controller with last good autotuned RD, resets flowguard and resumes autotune
            self._reset_controller(eventtime, hard_reset=False)
            self.ctrl.autotune.resume()
            self.mmu.log_info("FlowGuard monitoring activated and autotune resumed")


    def deactivate_flowguard(self, eventtime):
        if self.flowguard_enabled and self.flowguard_active:
            self.flowguard_active = False
            self.ctrl.autotune.pause() # Very likley this is a period that we want to exclude from autotuning
            self.mmu.log_info("FlowGuard monitoring deactivated and autotune paused")


    # This is "FlowGuard" on the encoder so manage it here
    def set_encoder_mode(self, mode=None):
        """
        Changing detection mode so ensure correct clog detection length
        """
        if not self.mmu.has_encoder(): return

        # Notify sensor of mode
        if mode is None: mode = self.flowguard_encoder_mode
        self.mmu.encoder_sensor.set_mode(mode)

        # Figure out the correct detection length based on mode
        cdl = self.flowguard_encoder_max_motion
        if mode == self.mmu.encoder_sensor.RUNOUT_AUTOMATIC:
            cdl = self.mmu.save_variables.allVariables.get(self.mmu.VARS_MMU_CALIB_CLOG_LENGTH, cdl)

        # Notify sensor of detection length
        self.mmu.encoder_sensor.set_clog_detection_length(cdl)


    def adjust_filament_tension(self, use_gear_motor=True, max_move=None):
        """
        Relax the filament tension, preferring proportional control if available else sync-feedback sensor switches.
        By default uses gear stepper to achive the result but optionally can use just extruder stepper for
        extruder entry check using compression sensor 'max_move' is advisory maximum travel distance
        Returns distance of the correction move and whether operation was successful (or None if not performed)
        """
        has_tension, has_compression, has_proportional = self.get_active_sensors()
        max_move = max_move or self.sync_feedback_buffer_maxrange

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
        has_tension      = sm.has_sensor(self.mmu.SENSOR_TENSION)
        has_compression  = sm.has_sensor(self.mmu.SENSOR_COMPRESSION)
        has_proportional = sm.has_sensor(self.mmu.SENSOR_PROPORTIONAL)
        return has_tension, has_compression, has_proportional


    def has_sync_feedback(self):
        return all(s is not None for s in self.get_active_sensors())


    #
    # GCODE Commands -----------------------------------------------------------
    #

    cmd_MMU_FLOWGUARD_help = "Enable/disable FlowGuard (clog-tangle detection)"
    cmd_MMU_FLOWGUARD_param_help = (
        "MMU_FLOWGUARD: %s\n" % cmd_MMU_FLOWGUARD_help
        + "ENABLE = [1|0] enable/disable FlowGuard clog/tangle detection\n"
        + "(no parameters for status report)"
    )
    def cmd_MMU_FLOWGUARD(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return

        if not self.sync_feedback_enabled:
            self.mmu.log_warning("Sync feedback is disabled or not configured. FlowGuard is unavailable")
            return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_FLOWGUARD_param_help), color=True)
            return

        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)

        if enable is not None:
            self._config_flowguard_feature(enable)
            return

        # Just report status
        if self.flowguard_enabled:
            active = " and currently active" if self.flowguard_active else " (not currently active)"
            self.mmu.log_always("FlowGuard monitoring feature is enabled%s" % active)
        else:
            self.mmu.log_always("FlowGuard monitoring feature is disabled")


    cmd_MMU_SYNC_FEEDBACK_help = "Controls sync feedback and applies filament tension adjustments"
    cmd_MMU_SYNC_FEEDBACK_param_help = (
        "MMU_SYNC_FEEDBACK: %s\n" % cmd_MMU_SYNC_FEEDBACK_help
        + "ENABLE         = [1|0] enable/disable sync feedback control\n"
        + "RESET          = [1|0] reset sync controller and return RD to last known good value\n"
        + "ADJUST_TENSION = [1|0] apply correction to neutralize filament tension\n"
        + "AUTOTUNE       = [1|0] allow saving of autotuned rotation distance\n"
        + "(no parameters for status report)"
    )
    def cmd_MMU_SYNC_FEEDBACK(self, gcmd):
        self.mmu.log_to_file(gcmd.get_commandline())
        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_bypass(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.mmu.log_always(self.mmu.format_help(self.cmd_MMU_SYNC_FEEDBACK_param_help), color=True)
            return

        if not self.has_sync_feedback():
            self.mmu.log_warning("No sync-feedback sensors!")
            return

        has_tension, has_compression, has_proportional = self.get_active_sensors()

        if not (has_proportional or has_tension or has_compression):
            self.mmu.log_warning("No sync-feedback sensors are enabled!")
            return

        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', None, minval=0, maxval=1)
        autotune = gcmd.get_int('AUTOTUNE', None, minval=0, maxval=1)
        adjust_tension = gcmd.get_int('ADJUST_TENSION', 0, minval=0, maxval=1)

        if enable is not None:
            self.sync_feedback_enabled = enable
            self.mmu.log_always("Sync feedback feature is %s" % ("enabled" if enable else "disabled"))

        if reset is not None and self.sync_feedback_enabled:
            self.mmu.log_always("Sync feedback reset")
            eventtime = self.mmu.reactor.monotonic()
            self._reset_controller(eventtime)

        if autotune is not None:
            self.mmu.autotune_rotation_distance = autotune
            self.mmu.log_always("Save Autotuned rotation distance feature is %s" % ("enabled" if autotune else "disabled"))

        if adjust_tension:
            try:
                with self.mmu.wrap_sync_gear_to_extruder():            # Cannot adjust sync feedback sensor if gears are not synced
                    with self.mmu._wrap_suspend_filament_monitoring(): # Avoid spurious runout during tiny corrective moves (unlikely)
                        actual,success = self.adjust_filament_tension()
                        if success:
                            self.mmu.log_info("Neutralized tension after moving %.2fmm" % actual)
                        elif success is False:
                            self.mmu.log_warning("Moved %.2fmm without neutralizing tension" % actual)
                        else:
                            self.mmu.log_warning("Operation not possible. Perhaps sensors are disabled?")

            except MmuError as ee:
                self.mmu.log_error("Error in MMU_SYNC_FEEDBACK: %s" % str(ee))

        if enable is None and autotune is None and not adjust_tension:
            # Just report status
            if self.sync_feedback_enabled:
                mode = self.ctrl.get_type_mode()
                active = " and currently active" if self.active else " (not currently active)"
                msg = "Sync feedback feature with type-%s sensor is enabled%s\n" % (mode, active)

                rd_start = self.mmu.calibration_manager.get_gear_rd()
                rd_current = self.ctrl.get_current_rd()
                rd_rec = self.ctrl.autotune.get_rec_rd()
                msg += "- Current RD: %.2f, Autotune recommended: %.2f, Default: %.2f\n" % (rd_current, rd_rec, rd_start)

                has_tension, has_compression, has_proportional = self.get_active_sensors()
                msg += "- State: %s\n" % self.get_sync_feedback_string(detail=True)
                msg += "- FlowGuard: %s" % ("Active" if self.flowguard_active else "Inactive")
                if has_proportional:
                    msg += " (Flowrate: %.1f%%)" % self.flow_rate

                self.mmu.log_always(msg)

            else:
                self.mmu.log_always("Sync feedback feature is disabled")
    

    def get_status(self, eventtime=None):
        self.flowguard_status['encoder_mode'] = self.flowguard_encoder_mode # Ok to mutate status
        return {
            'sync_feedback_state': self.get_sync_feedback_string(),
            'sync_feedback_enabled': self.is_enabled(),
            'sync_feedback_bias_raw': self._get_sync_bias_raw(),
            'sync_feedback_bias_modelled': self._get_sync_bias_modelled(),
            'sync_feedback_flow_rate': self.flow_rate,
            'flowguard': self.flowguard_status,
        }


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
        if not self.mmu.is_enabled: return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedbackManager: Synced MMU to extruder%s" % (" (sync feedback activated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
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
        self.extruder_monitor.register_callback(self._handle_extruder_movement, self.sync_feedback_extrude_threshold)


    def _handle_mmu_unsynced(self, eventtime=None):
        """
        Event indicating that gear stepper has been unsynced from extruder
        """
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        msg = "MmuSyncFeedbackManager: Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.sync_feedback_enabled else "")
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        if not self.active: return

        # Deactivate sync feedback
        self.active = False

        if self.new_autotuned_rd is not None:
            self.mmu.log_info("MmuSyncFeedbackManager: New Autotuned rotation distance (%.4f) for gate %d\n" % (self.new_autotuned_rd, self.mmu.gate_selected))
            self.mmu.calibration_manager.update_gear_rd(self.new_autotuned_rd)

        # Restore default (last tuned) rotation distance
        self.set_default_rd()

        # Optional but let's turn off extruder movement events
        self.extruder_monitor.remove_callback(self._handle_extruder_movement)


    def _handle_extruder_movement(self, eventtime, move):
        """
        Event call when extruder has moved more than threshold. This also allows for
        periodic rotation_distance adjustment, autotune and flowguard checking
        """
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()

        self.mmu.log_trace("MmuSyncFeedbackManager: Extruder movement event, move=%.1f" % move)

        state = self._get_sensor_state()
        status = self.ctrl.update(eventtime, move, state)
        self._process_status(eventtime, status)


    def _handle_sync_feedback(self, eventtime, state):
        """
        Event call when sync-feedback discrete state changes.
        'state' should be -1 (tension), 0 (neutral), 1 (compressed)
        or can be a proportional float value between -1.0 and 1.0
        """
        if not (self.mmu.is_enabled and self.sync_feedback_enabled and self.active): return
        if eventtime is None: eventtime = self.mmu.reactor.monotonic()
 
        msg = "MmuSyncFeedbackManager: Sync state changed to %s" % (self.get_sync_feedback_string(state))
        if self.mmu.mmu_machine.filament_always_gripped:
            self.mmu.log_debug(msg)
        else:
            self.mmu.log_info(msg)

        move = self.extruder_monitor.get_and_reset_accumulated(self._handle_extruder_movement)
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
            msg = "MmuSyncFeedbackManager: Autotune suggested new operational reference rd: %.4f\n%s" % (rd, note)
            if save and self.mmu.autotune_rotation_distance:
                self.new_autotuned_rd = rd
            self.mmu.log_debug(msg)

        # Always update instaneous gear stepper rotation_distance
        rd_current, rd_prev, rd_tuned = output['rd_current'], output['rd_prev'], output['rd_tuned']
        if rd_current != rd_prev:
            self.mmu.log_debug("MmuSyncFeedbackManager: Altered rotation distance for gate %d from %.4f to %.4f" % (self.mmu.gate_selected, rd_prev, rd_current))
            self.mmu.set_gear_rotation_distance(rd_current)

        # Proportional sensor (with autotune) allows for estimation of flow rate!
        if self.mmu.sensor_manager.has_sensor(self.mmu.SENSOR_PROPORTIONAL):
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
            rd_start = self.mmu.calibration_manager.get_gear_rd()
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
        rd_start = self.mmu.calibration_manager.get_gear_rd()
        cfg = SyncControllerConfig(
            log_sync = bool(self.sync_feedback_debug_log),
            buffer_range_mm = self.sync_feedback_buffer_range,
            buffer_max_range_mm = self.sync_feedback_buffer_maxrange,
            sensor_type = self._get_sensor_type(),
            use_twolevel_for_type_p = self.sync_feedback_force_twolevel,
            rd_start = rd_start,
            flowguard_relief_mm = self.flowguard_max_relief,
        )
        self.ctrl = SyncController(cfg)
        return self.ctrl


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
                msg = "Homing to tension sensor"
                sensor = self.mmu.SENSOR_TENSION
                homing_dir = 1

        else:
            # Tension state
            self.mmu.log_debug("Relaxing filament tension")
            direction = 1 if use_gear_motor else -1

            if self.sync_feedback_buffer_range == 0:
                msg = "Homing to compression sensor"
                sensor = self.mmu.SENSOR_COMPRESSION
                homing_dir = 1
            elif has_tension:
                msg = "Reverse homing off tension sensor"
                sensor = self.mmu.SENSOR_TENSION
                homing_dir = -1
            else:
                msg = "Homing to compression sensor"
                sensor = self.mmu.SENSOR_COMPRESSION
                homing_dir = 1

        actual,fhomed,_,_ = self.mmu.trace_filament_move(
            msg,
            max_move * direction,
            speed=speed,
            motor=motor,
            homing_move=homing_dir,
            endstop_name=sensor,
        )

        if fhomed and self.sync_feedback_buffer_range != 0:
            if use_gear_motor:
                # Move just a little more to find perfect neutral spot between sensors
                _,_,_,_ = self.mmu.trace_filament_move("Centering sync feedback buffer", (self.sync_feedback_buffer_range * direction) / 2.)
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
        final_state = self._get_sensor_state()
        success = abs(final_state) <= neutral_band
        self.mmu.log_info(
            "Proportional adjust: complete "
            "(nudge=%.2fmm, initial=%.2fmm, nudges=%.2fmm, total=%.2fmm, steps=%d, final_state=%.3f, success=%s)" %
            (nudge_mm, moved_initial_mm, moved_nudges_mm, moved_total_mm, steps, final_state, "yes" if success else "no")
        )
        return moved_total_mm, success
