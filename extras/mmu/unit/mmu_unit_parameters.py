# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Container for all mmu unit parameters
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ..mmu_constants import *


class MmuUnitParameters:

    def __init__(self, mmu_unit, config):
        logging.info("PAUL: init() for MmuUnitParameters")
        self._config = config
        self._mmu_unit = mmu_unit

        # Read user configuration ---------------------------------------------------------------------------

        # Automatic calibration / tuning options
        self.autocal_selector           = config.getint('autocal_selector', 0, minval=0, maxval=1) # Not exposed TODO placeholder for implementation
        self.skip_cal_rotation_distance = config.getint('skip_cal_rotation_distance', 0, minval=0, maxval=1)
        self.autotune_rotation_distance = config.getint('autotune_rotation_distance', 0, minval=0, maxval=1)
        self.autocal_bowden_length      = config.getint('autocal_bowden_length', 1, minval=0, maxval=1)
        self.autotune_bowden_length     = config.getint('autotune_bowden_length', 0, minval=0, maxval=1)
        self.skip_cal_encoder           = config.getint('skip_cal_encoder', 0, minval=0, maxval=1)
        self.autotune_encoder           = config.getint('autotune_encoder', 0, minval=0, maxval=1)
        
        # Configuration for gate loading and unloading
        self.gate_homing_endstop           = config.getchoice('gate_homing_endstop', {o: o for o in GATE_ENDSTOPS}, SENSOR_ENCODER)
        self.gate_homing_max               = config.getfloat('gate_homing_max', 100, minval=10)
        self.gate_parking_distance         = config.getfloat('gate_parking_distance', 23.)  # Can be +ve or -ve
#       self.gate_unload_buffer             = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting the gate # PAUL make this dynamic based on bowden_fast_unload_portion
        self.gate_preload_endstop          = config.getchoice('gate_preload_endstop', {o: o for o in GATE_ENDSTOPS + ['']}, '') # PAUL new - implement: SENSOR_GEAR_PREFIX or '' # PAUL is this really useful? maybe just the parking_distance is needed?
        self.gate_preload_endstop          = self.gate_preload_endstop or self.gate_homing_endstop
        self.gate_preload_homing_max       = config.getfloat('gate_preload_homing_max', self.gate_homing_max)
        self.gate_preload_parking_distance = config.getfloat('gate_preload_parking_distance', -10.)  # Can be +ve or -ve
        self.gate_preload_attempts         = config.getint('gate_preload_attempts', 1, minval=1, maxval=20)
        self.gate_endstop_to_encoder       = config.getfloat('gate_endstop_to_encoder', 0., minval=0.)
        self.gate_load_retries             = config.getint('gate_load_retries', 1, minval=1, maxval=5)
        self.gate_autoload                 = config.getint('gate_autoload', 1, minval=0, maxval=1)
        self.gate_final_eject_distance     = config.getfloat('gate_final_eject_distance', 0)
        
        # Configuration for (fast) bowden move
        self.bowden_homing_max                 = config.getfloat('bowden_homing_max', 2000., minval=100.)
        self.bowden_fast_unload_portion        = config.getfloat('bowden_fast_unload_portion', 95, minval=50, maxval=100)
        self.bowden_fast_load_portion          = config.getfloat('bowden_fast_load_portion', 95, minval=50, maxval=100)
        self.bowden_apply_correction           = config.getint('bowden_apply_correction', 0, minval=0, maxval=1)
        self.bowden_allowable_load_delta       = config.getfloat('bowden_allowable_load_delta', 10., minval=1.)
        self.bowden_pre_unload_test            = config.getint('bowden_pre_unload_test', 0, minval=0, maxval=1)
        self.bowden_pre_unload_error_tolerance = config.getfloat('bowden_pre_unload_error_tolerance', 100, minval=0, maxval=100)
        
        # Configuration for extruder and toolhead homing
        self.extruder_force_homing             = config.getint('extruder_force_homing', 0, minval=0, maxval=1)
        self.extruder_homing_endstop           = config.getchoice('extruder_homing_endstop', {o: o for o in EXTRUDER_ENDSTOPS}, SENSOR_EXTRUDER_NONE)
        self.extruder_homing_max               = config.getfloat('extruder_homing_max', 50., above=10.)
#        self.extruder_homing_buffer            = config.getfloat('extruder_homing_buffer', 30., minval=0.) # How far to short bowden load move to avoid overshooting # PAUL make this dynamic based on bowden_fast_load_portion
        self.extruder_collision_homing_step    = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max               = config.getfloat('toolhead_homing_max', 20., minval=0.)

        self.toolhead_unload_safety_margin     = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.)
        self.toolhead_move_error_tolerance     = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100)
        self.toolhead_entry_tension_test       = config.getint('toolhead_entry_tension_test', 1, minval=0, maxval=1)
        self.toolhead_post_load_tighten        = config.getint('toolhead_post_load_tighten', 60, minval=0, maxval=100)
        self.toolhead_post_load_tension_adjust = config.getint('toolhead_post_load_tension_adjust', 1, minval=0, maxval=1)
        
        # Synchronous motor control
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip    = config.getint('sync_form_tip', 0, minval=0, maxval=1)
        self.sync_purge       = config.getint('sync_purge', 0, minval=0, maxval=1)
        if self._mmu_unit.filament_always_gripped:
            self.sync_to_extruder = self.sync_form_tip = self.sync_purge = 1
        
        # TMC current control
        self.extruder_collision_homing_current = config.getint('extruder_collision_homing_current', 50, minval=10, maxval=100)
        self.sync_gear_current                 = config.getint('sync_gear_current', 50, minval=10, maxval=100)
        
        # Filament move speeds and accelaration
        self.gear_from_filament_buffer_speed = config.getfloat('gear_from_filament_buffer_speed', 150., minval=10.)
        self.gear_from_filament_buffer_accel = config.getfloat('gear_from_filament_buffer_accel', 400, minval=10.)
        self.gear_from_spool_speed           = config.getfloat('gear_from_spool_speed', 60, minval=10.)
        self.gear_from_spool_accel           = config.getfloat('gear_from_spool_accel', 100, minval=10.)
        self.gear_unload_speed               = config.getfloat('gear_unload_speed', self.gear_from_spool_speed, minval=10.)
        self.gear_unload_accel               = config.getfloat('gear_unload_accel', self.gear_from_spool_accel, minval=10.)
        self.gear_short_move_speed           = config.getfloat('gear_short_move_speed', 60., minval=1.)
        self.gear_short_move_accel           = config.getfloat('gear_short_move_accel', 400, minval=10.)
        self.gear_short_move_threshold       = config.getfloat('gear_short_move_threshold', self.gate_homing_max, minval=1.)
        self.gear_homing_speed               = config.getfloat('gear_homing_speed', 150, minval=1.)
        self.gear_buzz_accel                 = config.getfloat('gear_buzz_accel', 1000, minval=10.) # Not exposed
        
        # eSpooler
        self.espooler_min_distance                = config.getfloat('espooler_min_distance', 50., above=0)
        self.espooler_max_stepper_speed           = config.getfloat('espooler_max_stepper_speed', 300., above=0)
        self.espooler_min_stepper_speed           = config.getfloat('espooler_min_stepper_speed', 0., minval=0., below=self.espooler_max_stepper_speed)
        self.espooler_speed_exponent              = config.getfloat('espooler_speed_exponent', 0.5, above=0)
        self.espooler_assist_reduced_speed        = config.getint('espooler_assist_reduced_speed', 50, minval=0, maxval=100)
        self.espooler_printing_power              = config.getint('espooler_printing_power', 0, minval=0, maxval=100)
        self.espooler_assist_extruder_move_length = config.getfloat("espooler_assist_extruder_move_length", 100, above=10.)
        self.espooler_assist_burst_power          = config.getint("espooler_assist_burst_power", 50, minval=0, maxval=100)
        self.espooler_assist_burst_duration       = config.getfloat("espooler_assist_burst_duration", .4, above=0., maxval=10.)
        self.espooler_assist_burst_trigger        = config.getint("espooler_assist_burst_trigger", 0, minval=0, maxval=1)
        self.espooler_assist_burst_trigger_max    = config.getint("espooler_assist_burst_trigger_max", 3, minval=1)
        self.espooler_rewind_burst_power          = config.getint("espooler_rewind_burst_power", 50, minval=0, maxval=100)
        self.espooler_rewind_burst_duration       = config.getfloat("espooler_rewind_burst_duration", .4, above=0., maxval=10.)
        self.espooler_operations                  = list(config.getlist('espooler_operations', ESPOOLER_OPERATIONS))

        # Sync-feedback "buffer"
        self.sync_feedback_enabled           = config.getint('sync_feedback_enabled', 0, minval=0, maxval=1)
        self.sync_feedback_buffer_range      = config.getfloat('sync_feedback_buffer_range', 10., minval=0.)
        self.sync_feedback_buffer_maxrange   = config.getfloat('sync_feedback_buffer_maxrange', 10., minval=0.)
        self.sync_feedback_speed_multiplier  = config.getfloat('sync_feedback_speed_multiplier', 5, minval=1, maxval=50)
        self.sync_feedback_boost_multiplier  = config.getfloat('sync_feedback_boost_multiplier', 5, minval=1, maxval=50)
        self.sync_feedback_extrude_threshold = config.getfloat('sync_feedback_extrude_threshold', 5, above=1.)
        self.sync_feedback_debug_log         = config.getint('sync_feedback_debug_log', 0)
        self.sync_feedback_force_twolevel    = config.getint('sync_feedback_force_twolevel', 0) # Not exposed

        # FlowGuard
        self.flowguard_enabled            = config.getint('flowguard_enabled', 1, minval=0, maxval=1)
        self.flowguard_max_relief         = config.getfloat('flowguard_max_relief', 8, above=1.)
        self.flowguard_encoder_mode       = config.getint('flowguard_encoder_mode', 2, minval=0, maxval=2) # PAUL use constants?
        self.flowguard_encoder_max_motion = config.getfloat('flowguard_encoder_max_motion', 20, above=0.)

        # Heater/Dryer control
        self.heater_max_temp         = config.getfloat('heater_max_temp', 65, above=0.) # Never to exceed temp to avoid melting enclosure
        self.heater_default_dry_temp = config.getfloat('heater_default_dry_temp', 45, above=0.)
        self.heater_default_dry_time = config.getfloat('heater_default_dry_time', 300, above=0.)
        self.heater_default_humidity = config.getfloat('heater_default_humidity', 10, above=0.)
        self.heater_vent_macro       = config.get('heater_vent_macro', '')
        self.heater_vent_interval    = config.getfloat('heater_vent_interval', 0, minval=0)
        self.heater_rotate_interval  = config.getfloat('heater_rotate_interval', 5, minval=1)
        
        # Optional features
        self.startup_home_if_unloaded = config.getint('startup_home_if_unloaded', 0, minval=0, maxval=1)
        self.has_filament_buffer      = bool(config.getint('has_filament_buffer', 1, minval=0, maxval=1))
        self.encoder_move_validation  = config.getint('encoder_move_validation', 1, minval=0, maxval=1)


    def set_test_config(self, gcmd):
        if self._mmu_unit.has_sync_feedback():
            self.sync_feedback_enabled           = gcmd.get_int('SYNC_FEEDBACK_ENABLED', self.sync_feedback_enabled, minval=0, maxval=1)
            self.sync_feedback_buffer_range      = gcmd.get_float('SYNC_FEEDBACK_BUFFER_RANGE', self.sync_feedback_buffer_range, minval=0.)
            self.sync_feedback_buffer_maxrange   = gcmd.get_float('SYNC_FEEDBACK_BUFFER_MAXRANGE', self.sync_feedback_buffer_maxrange, minval=0.)
            self.sync_feedback_speed_multiplier  = gcmd.get_float('SYNC_FEEDBACK_SPEED_MULTIPLIER', self.sync_feedback_speed_multiplier, minval=1., maxval=50)
            self.sync_feedback_boost_multiplier  = gcmd.get_float('SYNC_FEEDBACK_BOOST_MULTIPLIER', self.sync_feedback_boost_multiplier, minval=1., maxval=50)
            self.sync_feedback_extrude_threshold = gcmd.get_float('SYNC_FEEDBACK_EXTRUDE_THRESHOLD', self.sync_feedback_extrude_threshold, above=1.)
            self.sync_feedback_debug_log         = gcmd.get_int('SYNC_FEEDBACK_DEBUG_LOG', self.sync_feedback_debug_log, minval=0, maxval=1)

            flowguard_enabled = gcmd.get_int('FLOWGUARD_ENABLED', self.flowguard_enabled, minval=0, maxval=1)
            if flowguard_enabled != self.flowguard_enabled:
                self._mmu_unit.sync_feedback.config_flowguard_feature(flowguard_enabled)
            self.flowguard_max_relief = gcmd.get_float('FLOWGUARD_MAX_RELIEF', self.flowguard_max_relief, above=1.)

        if self._mmu_unit.has_encoder():
            mode = gcmd.get_int('FLOWGUARD_ENCODER_MODE', self.flowguard_encoder_mode, minval=0, maxval=2)
            if mode != self.flowguard_encoder_mode:
                self.flowguard_encoder_mode = mode
                self._mmu_unit.encoder.set_encoder_mode(mode)
            self.flowguard_encoder_max_motion = gcmd.get_float('FLOWGUARD_ENCODER_MAX_MOTION', self.flowguard_encoder_max_motion, above=0.)


    def get_test_config(self):
        msg  = ""
        if self._mmu_unit.has_sync_feedback():
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

        if self.mmu_unit.has_encoder():
            msg += "\nflowguard_encoder_mode = %d" % self.flowguard_encoder_mode
            msg += "\nflowguard_encoder_max_motion = %.1f" % self.flowguard_encoder_max_motion
        return msg


    def check_test_config(self, param):
        return vars(self).get(param) is None
