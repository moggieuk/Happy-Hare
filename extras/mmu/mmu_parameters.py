# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Container for all shared mmu parameters
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, ast

# Klipper imports

# Happy Hare imports
from .mmu_shared    import *


# Main klipper module
class MmuParameters:

    def __init__(self, mmu_machine, config):
        self._mmu_machine = mmu_machine
        self._config = config

        # Read user configuration ---------------------------------------------------------------------------

        # Printer interaction config
        self.timeout_pause = config.getint('timeout_pause', 72000, minval=120)
        self.default_idle_timeout = config.getint('default_idle_timeout', -1, minval=120)
        self.pending_spool_id_timeout = config.getint('pending_spool_id_timeout', default=20, minval=-1) # Not currently exposed
        self.disable_heater = config.getint('disable_heater', 600, minval=60)
        self.default_extruder_temp = config.getfloat('default_extruder_temp', 200., minval=0.)
        self.extruder_temp_variance = config.getfloat('extruder_temp_variance', 2., minval=1.)
        self.gcode_load_sequence = config.getint('gcode_load_sequence', 0)
        self.gcode_unload_sequence = config.getint('gcode_unload_sequence', 0)
        self.slicer_tip_park_pos = config.getfloat('slicer_tip_park_pos', 0., minval=0.)
        self.force_form_tip_standalone = config.getint('force_form_tip_standalone', 0, minval=0, maxval=1)
        self.force_purge_standalone = config.getint('force_purge_standalone', 0, minval=0, maxval=1)
        self.strict_filament_recovery = config.getint('strict_filament_recovery', 0, minval=0, maxval=1)
        self.filament_recovery_on_pause = config.getint('filament_recovery_on_pause', 1, minval=0, maxval=1)
        self.retry_tool_change_on_error = config.getint('retry_tool_change_on_error', 0, minval=0, maxval=1)
        self.print_start_detection = config.getint('print_start_detection', 1, minval=0, maxval=1)
        self.startup_reset_ttg_map = config.getint('startup_reset_ttg_map', 0, minval=0, maxval=1)
        self.show_error_dialog = config.getint('show_error_dialog', 1, minval=0, maxval=1)

        # Internal macro overrides
        self.pause_macro = config.get('pause_macro', 'PAUSE')
        self.action_changed_macro = config.get('action_changed_macro', '_MMU_ACTION_CHANGED')
        self.print_state_changed_macro = config.get('print_state_changed_macro', '_MMU_PRINT_STATE_CHANGED')
        self.mmu_event_macro = config.get('mmu_event_macro', '_MMU_EVENT')
        self.form_tip_macro = config.get('form_tip_macro', '_MMU_FORM_TIP').replace("'", "")
        self.purge_macro = config.get('purge_macro', '').replace("'", "")
        self.pre_unload_macro = config.get('pre_unload_macro', '_MMU_PRE_UNLOAD').replace("'", "")
        self.post_form_tip_macro = config.get('post_form_tip_macro', '_MMU_POST_FORM_TIP').replace("'", "")
        self.post_unload_macro = config.get('post_unload_macro', '_MMU_POST_UNLOAD').replace("'", "")
        self.pre_load_macro = config.get('pre_load_macro', '_MMU_PRE_LOAD').replace("'", "")
        self.post_load_macro = config.get('post_load_macro', '_MMU_POST_LOAD_MACRO').replace("'", "")
        self.unload_sequence_macro = config.get('unload_sequence_macro', '_MMU_UNLOAD_SEQUENCE').replace("'", "")
        self.load_sequence_macro = config.get('load_sequence_macro', '_MMU_LOAD_SEQUENCE').replace("'", "")

        # These macros are not currently exposed but provide future flexability
        self.error_dialog_macro = config.get('error_dialog_macro', '_MMU_ERROR_DIALOG') # Not exposed
        self.error_macro = config.get('error_macro', '_MMU_ERROR') # Not exposed
        self.toolhead_homing_macro = config.get('toolhead_homing_macro', '_MMU_AUTO_HOME') # Not exposed
        self.park_macro = config.get('park_macro', '_MMU_PARK') # Not exposed
        self.save_position_macro = config.get('save_position_macro', '_MMU_SAVE_POSITION') # Not exposed
        self.restore_position_macro = config.get('restore_position_macro', '_MMU_RESTORE_POSITION') # Not exposed
        self.clear_position_macro = config.get('clear_position_macro', '_MMU_CLEAR_POSITION') # Not exposed

        # User default (reset state) gate map and TTG map
        self.default_ttg_map = list(config.getintlist('default_ttg_map', []))
        self.default_gate_status = list(config.getintlist('default_gate_status', []))
        self.default_gate_filament_name = list(config.getlist('default_gate_filament_name', []))
        self.default_gate_material = list(config.getlist('default_gate_material', []))
        self.default_gate_color = list(config.getlist('default_gate_color', []))
        self.default_gate_temperature = list(config.getintlist('default_gate_temperature', []))
        self.default_gate_spool_id = list(config.getintlist('default_gate_spool_id', []))
        self.default_gate_speed_override = list(config.getintlist('default_gate_speed_override', []))
        self.default_endless_spool_groups = list(config.getintlist('default_endless_spool_groups', []))

        self.bypass_autoload = config.getint('bypass_autoload', 1, minval=0, maxval=1)
        self.encoder_dwell = config.getfloat('encoder_dwell', 0.1, minval=0., maxval=2.) # Not exposed
        self.encoder_move_step_size = config.getfloat('encoder_move_step_size', 15., minval=5., maxval=25.) # Not exposed

        # Configuration for extruder dimensions
        self.toolhead_extruder_to_nozzle = config.getfloat('toolhead_extruder_to_nozzle', 0., minval=5.) # For "sensorless"
        self.toolhead_sensor_to_nozzle = config.getfloat('toolhead_sensor_to_nozzle', 0., minval=1.) # For toolhead sensor
        self.toolhead_entry_to_extruder = config.getfloat('toolhead_entry_to_extruder', 0., minval=0.) # For extruder (entry) sensor
        self.toolhead_residual_filament = config.getfloat('toolhead_residual_filament', 0., minval=0., maxval=50.) # +ve value = reduction of load length
        self.toolhead_ooze_reduction = config.getfloat('toolhead_ooze_reduction', 0., minval=-5., maxval=20.) # +ve value = reduction of load length

        # TMC current control
        self.extruder_form_tip_current = config.getint('extruder_form_tip_current', 100, minval=100, maxval=150)
        self.extruder_purge_current = config.getint('extruder_purge_current', 100, minval=100, maxval=150)

        # Filament move speeds and accelaration
        self.extruder_load_speed = config.getfloat('extruder_load_speed', 15, minval=1.)
        self.extruder_unload_speed = config.getfloat('extruder_unload_speed', 15, minval=1.)
        self.extruder_sync_load_speed = config.getfloat('extruder_sync_load_speed', 15., minval=1.)
        self.extruder_sync_unload_speed = config.getfloat('extruder_sync_unload_speed', 15., minval=1.)
        self.extruder_accel = config.getfloat('extruder_accel', 400, above=10.)
        self.extruder_homing_speed = config.getfloat('extruder_homing_speed', 15, minval=1.)

        self.macro_toolhead_max_accel = config.getfloat('macro_toolhead_max_accel', 0, minval=0)
        self.macro_toolhead_min_cruise_ratio = config.getfloat('macro_toolhead_min_cruise_ratio', minval=0., below=1.)
        if self.macro_toolhead_max_accel == 0:
            self.macro_toolhead_max_accel = config.getsection('printer').getsection('toolhead').getint('max_accel', 5000)

        # Optional features
        self.spoolman_support = config.getchoice('spoolman_support', {o: o for o in SPOOLMAN_OPTIONS}, SPOOLMAN_OFF)
        self.t_macro_color = config.getchoice('t_macro_color', {o: o for o in T_MACRO_COLOR_OPTIONS}, T_MACRO_COLOR_SLICER)
        self.endless_spool_groups = list(config.getintlist('endless_spool_groups', []))
        self.endless_spool_enabled = config.getint('endless_spool_enabled', 0, minval=0, maxval=1)
        self.endless_spool_on_load = config.getint('endless_spool_on_load', 0, minval=0, maxval=1)
        self.endless_spool_eject_gate = config.getint('endless_spool_eject_gate', -1, minval=-1, maxval=self._mmu_machine.num_gates - 1)
        self.select_tool_macro = config.get('select_tool_macro', default=None)
        self.select_tool_num_switches = config.getint('select_tool_num_switches', default=0, minval=0)

        # Logging
        self.log_level = config.getint('log_level', 1, minval=0, maxval=4)
        self.log_file_level = config.getint('log_file_level', 2, minval=-1, maxval=4)
        self.log_statistics = config.getint('log_statistics', 0, minval=0, maxval=1)
        self.log_visual = config.getint('log_visual', 1, minval=0, maxval=1)
        self.log_startup_status = config.getint('log_startup_status', 1, minval=0, maxval=2)
        self.log_m117_messages = config.getint('log_m117_messages', 1, minval=0, maxval=1)

        # Cosmetic console stuff
        self.console_stat_columns = list(config.getlist('console_stat_columns', ['unload', 'load', 'total']))
        self.console_stat_rows = list(config.getlist('console_stat_rows', ['total', 'job', 'job_average']))
        self.console_gate_stat = config.getchoice('console_gate_stat', {o: o for o in GATE_STATS_TYPES}, GATE_STATS_STRING)
        self.console_always_output_full = config.getint('console_always_output_full', 1, minval=0, maxval=1)

        # Turn off splash bling for boring people
        self.serious = config.getint('serious', 0, minval=0, maxval=1) # Not exposed

        # Build tuples of drying temp / drying time indexed by filament type
        drying_data_str = config.get('drying_data', {})
        try:
            drying_data = ast.literal_eval(drying_data_str)
            # Store as upper case keys (If there are duplicate keys differing only by case, the last one wins)
            self.drying_data = dict((str(k).upper(), v) for k, v in drying_data.items())
        except Exception as e:
            raise config.error("Unparsable 'drying_data' parameter: %s" % str(e))

        # Currently hidden and testing options
        self.test_random_failures = config.getint('test_random_failures', 0, minval=0, maxval=1) # Not exposed
        self.test_force_in_print = config.getint('test_force_in_print', 0, minval=0, maxval=1) # Not exposed

        # Klipper tuning (aka hacks) -------

        # Kalico (Danger Klipper) installation
        self.suppress_kalico_warning = config.getint('suppress_kalico_warning', 0, minval=0, maxval=1) # Not exposed

        # Timer too close is a catch all error, however it has been found to occur on some systems during homing and probing
        # operations especially so with CANbus connected mcus. Happy Hare using many homing moves for reliable extruder loading
        # and unloading and enabling this option affords klipper more tolerance and avoids this dreaded error.
        self.update_trsync = config.getint('update_trsync', 0, minval=0, maxval=1)

        # Some CANbus boards are prone to this but it have been seen on regular USB boards where a comms
        # timeout will kill the print. Since it seems to occur only on homing moves perhaps because of too
        # high a microstep setting or speed. They can be safely retried to workaround.
        # This has been working well in practice.
        self.canbus_comms_retries = config.getint('canbus_comms_retries', 3, minval=1, maxval=10)

        # Older neopixels have very finiky timing and can generate lots of "Unable to obtain 'neopixel_result' response"
        # errors in klippy.log. This has been linked to subsequent Timer too close errors. An often cited workaround is
        # to increase BIT_MAX_TIME in neopixel.py. This option does that automatically for you to save dirtying klipper.
        self.update_bit_max_time = config.getint('update_bit_max_time', 0, minval=0, maxval=1)

        # This is required for the BTT AHT10 used on the ViViD MMU
        self.update_aht10_commands = config.getint('update_aht10_commands', 0, minval=0, maxval=1)
