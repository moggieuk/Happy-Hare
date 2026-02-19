# Happy Hare MMU Software
# Main module
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
import gc, sys, ast, random, logging, time, contextlib, math, os.path, re, unicodedata, traceback

# Klipper imports

# Happy Hare imports

# MMU subcomponent clases


# Main klipper module
class MmuParameters:

    def __init__(self, mmu, mmu_unit, config):
        self.config = config
        self.mmu = mmu
        self.mmu_unit = mmu_unit

        # Read user configuration ---------------------------------------------------------------------------

        # Automatic calibration / tuning options
        self.autocal_selector = config.getint('autocal_selector', 0, minval=0, maxval=1) # Not exposed TODO placeholder for implementation
        self.skip_cal_rotation_distance = config.getint('skip_cal_rotation_distance', 0, minval=0, maxval=1)
        self.autotune_rotation_distance = config.getint('autotune_rotation_distance', 0, minval=0, maxval=1)
        self.autocal_bowden_length = config.getint('autocal_bowden_length', 1, minval=0, maxval=1)
        self.autotune_bowden_length = config.getint('autotune_bowden_length', 0, minval=0, maxval=1)
        self.skip_cal_encoder = config.getint('skip_cal_encoder', 0, minval=0, maxval=1)
        self.autotune_encoder = config.getint('autotune_encoder', 0, minval=0, maxval=1)
        
        # Printer interaction config
        self.startup_home_if_unloaded = config.getint('startup_home_if_unloaded', 0, minval=0, maxval=1)
        
        # Configuration for gate loading and unloading
        self.gate_homing_endstop = config.getchoice('gate_homing_endstop', {o: o for o in self.mmu.GATE_ENDSTOPS}, self.mmu.SENSOR_ENCODER)
        self.gate_homing_max = config.getfloat('gate_homing_max', 100, minval=10)
        self.gate_parking_distance = config.getfloat('gate_parking_distance', 23.)  # Can be +ve or -ve
#       self.gate_unload_buffer = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting the gate # PAUL make this dynamic based on bowden_fast_unload_portion
        self.gate_preload_endstop = config.getchoice('gate_preload_endstop', {o: o for o in self.mmu.GATE_ENDSTOPS}, self.mmu.SENSOR_ENCODER) # PAUL new - implement
        self.gate_preload_homing_max = config.getfloat('gate_preload_homing_max', self.gate_homing_max)
        self.gate_preload_parking_distance = config.getfloat('gate_preload_parking_distance', -10.)  # Can be +ve or -ve
        self.gate_preload_attempts = config.getint('gate_preload_attempts', 1, minval=1, maxval=20)
        self.gate_endstop_to_encoder = config.getfloat('gate_endstop_to_encoder', 0., minval=0.)
        self.gate_load_retries = config.getint('gate_load_retries', 1, minval=1, maxval=5)
        self.gate_autoload = config.getint('gate_autoload', 1, minval=0, maxval=1)
        self.gate_final_eject_distance = config.getfloat('gate_final_eject_distance', 0)
        
        # Configuration for (fast) bowden move
        self.bowden_homing_max = config.getfloat('bowden_homing_max', 2000., minval=100.)
        self.bowden_fast_unload_portion = config.getfloat('bowden_fast_unload_portion', 95, minval=50, maxval=100)
        self.bowden_fast_load_portion = config.getfloat('bowden_fast_load_portion', 95, minval=50, maxval=100)
        self.bowden_apply_correction = config.getint('bowden_apply_correction', 0, minval=0, maxval=1)
        self.bowden_allowable_load_delta = config.getfloat('bowden_allowable_load_delta', 10., minval=1.)
        self.bowden_pre_unload_test = config.getint('bowden_pre_unload_test', 0, minval=0, maxval=1)
        self.bowden_pre_unload_error_tolerance = config.getfloat('bowden_pre_unload_error_tolerance', 100, minval=0, maxval=100)
        
        # Configuration for extruder and toolhead homing
        self.extruder_force_homing = config.getint('extruder_force_homing', 0, minval=0, maxval=1)
        self.extruder_homing_endstop = config.getchoice('extruder_homing_endstop', {o: o for o in self.mmu.EXTRUDER_ENDSTOPS}, self.mmu.SENSOR_EXTRUDER_NONE)
        self.extruder_homing_max = config.getfloat('extruder_homing_max', 50., above=10.)
#        self.extruder_homing_buffer = config.getfloat('extruder_homing_buffer', 30., minval=0.) # How far to short bowden load move to avoid overshooting # PAUL make this dynamic based on bowden_fast_load_portion
        self.extruder_collision_homing_step = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max = config.getfloat('toolhead_homing_max', 20., minval=0.)

        self.toolhead_unload_safety_margin = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.)
        self.toolhead_move_error_tolerance = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100)
        self.toolhead_entry_tension_test = config.getint('toolhead_entry_tension_test', 1, minval=0, maxval=1)
        self.toolhead_post_load_tighten = config.getint('toolhead_post_load_tighten', 60, minval=0, maxval=100)
        self.toolhead_post_load_tension_adjust = config.getint('toolhead_post_load_tension_adjust', 1, minval=0, maxval=1)
        
        # Synchronous motor control
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip = config.getint('sync_form_tip', 0, minval=0, maxval=1)
        self.sync_purge = config.getint('sync_purge', 0, minval=0, maxval=1)
        if self.mmu_unit.filament_always_gripped:
            self.sync_to_extruder = self.sync_form_tip = self.sync_purge = 1
        
        # TMC current control
        self.extruder_collision_homing_current = config.getint('extruder_collision_homing_current', 50, minval=10, maxval=100)
        self.sync_gear_current = config.getint('sync_gear_current', 50, minval=10, maxval=100)
        
        # Filament move speeds and accelaration
        self.gear_from_filament_buffer_speed = config.getfloat('gear_from_filament_buffer_speed', 150., minval=10.)
        self.gear_from_filament_buffer_accel = config.getfloat('gear_from_filament_buffer_accel', 400, minval=10.)
        self.gear_from_spool_speed = config.getfloat('gear_from_spool_speed', 60, minval=10.)
        self.gear_from_spool_accel = config.getfloat('gear_from_spool_accel', 100, minval=10.)
        self.gear_unload_speed = config.getfloat('gear_unload_speed', self.gear_from_spool_speed, minval=10.)
        self.gear_unload_accel = config.getfloat('gear_unload_accel', self.gear_from_spool_accel, minval=10.)
        self.gear_short_move_speed = config.getfloat('gear_short_move_speed', 60., minval=1.)
        self.gear_short_move_accel = config.getfloat('gear_short_move_accel', 400, minval=10.)
        self.gear_short_move_threshold = config.getfloat('gear_short_move_threshold', self.gate_homing_max, minval=1.)
        self.gear_homing_speed = config.getfloat('gear_homing_speed', 150, minval=1.)
        self.gear_buzz_accel = config.getfloat('gear_buzz_accel', 1000, minval=10.) # Not exposed
        
        # eSpooler
        self.espooler_min_distance = config.getfloat('espooler_min_distance', 50., above=0)
        self.espooler_max_stepper_speed = config.getfloat('espooler_max_stepper_speed', 300., above=0)
        self.espooler_min_stepper_speed = config.getfloat('espooler_min_stepper_speed', 0., minval=0., below=self.espooler_max_stepper_speed)
        self.espooler_speed_exponent = config.getfloat('espooler_speed_exponent', 0.5, above=0)
        self.espooler_assist_reduced_speed = config.getint('espooler_assist_reduced_speed', 50, minval=0, maxval=100)
        self.espooler_printing_power = config.getint('espooler_printing_power', 0, minval=0, maxval=100)
        self.espooler_assist_extruder_move_length = config.getfloat("espooler_assist_extruder_move_length", 100, above=10.)
        self.espooler_assist_burst_power = config.getint("espooler_assist_burst_power", 50, minval=0, maxval=100)
        self.espooler_assist_burst_duration = config.getfloat("espooler_assist_burst_duration", .4, above=0., maxval=10.)
        self.espooler_assist_burst_trigger = config.getint("espooler_assist_burst_trigger", 0, minval=0, maxval=1)
        self.espooler_assist_burst_trigger_max = config.getint("espooler_assist_burst_trigger_max", 3, minval=1)
        self.espooler_rewind_burst_power = config.getint("espooler_rewind_burst_power", 50, minval=0, maxval=100)
        self.espooler_rewind_burst_duration = config.getfloat("espooler_rewind_burst_duration", .4, above=0., maxval=10.)
        self.espooler_operations = list(config.getlist('espooler_operations', self.mmu.ESPOOLER_OPERATIONS))
        
        # Optional features
        self.has_filament_buffer = bool(config.getint('has_filament_buffer', 1, minval=0, maxval=1))
        self.encoder_move_validation = config.getint('encoder_move_validation', 1, minval=0, maxval=1)

        # CUSTOM MMU
        #cad_gate0_pos
        #cad_gate_width
        #cad_bypass_offset
        #cad_last_gate_offset
        #cad_selector_tolerance

def load_config_prefix(config):
    return MmuParameters(config)

