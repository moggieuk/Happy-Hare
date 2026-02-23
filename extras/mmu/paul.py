        # Read user configuration ---------------------------------------------------------------------------
        #
        # Printer interaction config
        self.extruder_name = self.mmu_machine.extruder_name # TODO Idea: This could be per-gate to map gates to different extruders!
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
        self.startup_home_if_unloaded = config.getint('startup_home_if_unloaded', 0, minval=0, maxval=1)
        self.startup_reset_ttg_map = config.getint('startup_reset_ttg_map', 0, minval=0, maxval=1)
        self.show_error_dialog = config.getint('show_error_dialog', 1, minval=0, maxval=1)

        # Automatic calibration / tuning options
        self.autocal_selector = config.getint('autocal_selector', 0, minval=0, maxval=1) # Not exposed TODO placeholder for implementation
        self.skip_cal_rotation_distance = config.getint('skip_cal_rotation_distance', 0, minval=0, maxval=1)
        self.autotune_rotation_distance = config.getint('autotune_rotation_distance', 0, minval=0, maxval=1)
        self.autocal_bowden_length = config.getint('autocal_bowden_length', 1, minval=0, maxval=1)
        self.autotune_bowden_length = config.getint('autotune_bowden_length', 0, minval=0, maxval=1)
        self.skip_cal_encoder = config.getint('skip_cal_encoder', 0, minval=0, maxval=1)
        self.autotune_encoder = config.getint('autotune_encoder', 0, minval=0, maxval=1)

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
        self.default_ttg_map = list(config.getintlist('tool_to_gate_map', []))
        self.default_gate_status = list(config.getintlist('gate_status', []))
        self.default_gate_filament_name = list(config.getlist('gate_filament_name', []))
        self.default_gate_material = list(config.getlist('gate_material', []))
        self.default_gate_color = list(config.getlist('gate_color', []))
        self.default_gate_temperature = list(config.getintlist('gate_temperature', []))
        self.default_gate_spool_id = list(config.getintlist('gate_spool_id', []))
        self.default_gate_speed_override = list(config.getintlist('gate_speed_override', []))

        # Configuration for gate loading and unloading
        self.gate_homing_endstop = config.getchoice('gate_homing_endstop', {o: o for o in self.GATE_ENDSTOPS}, self.SENSOR_ENCODER)
        self.gate_homing_max = config.getfloat('gate_homing_max', 100, minval=10)
        self.gate_parking_distance = config.getfloat('gate_parking_distance', 23.) # Can be +ve or -ve
        self.gate_unload_buffer = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting the gate # PAUL make this dynamic based on bowden_fast_unload_portion
        self.gate_preload_endstop = config.getchoice('gate_preload_endstop', {o: o for o in self.GATE_ENDSTOPS}, self.SENSOR_ENCODER) # PAUL new - implement
        self.gate_preload_homing_max = config.getfloat('gate_preload_homing_max', self.gate_homing_max)
        self.gate_preload_parking_distance = config.getfloat('gate_preload_parking_distance', -10.) # Can be +ve or -ve
        self.gate_preload_attempts = config.getint('gate_preload_attempts', 1, minval=1, maxval=20) # How many times to try to grab the filament
        self.gate_endstop_to_encoder = config.getfloat('gate_endstop_to_encoder', 0., minval=0.)
        self.gate_load_retries = config.getint('gate_load_retries', 1, minval=1, maxval=5)
        self.gate_autoload = config.getint('gate_autoload', 1, minval=0, maxval=1)
        self.gate_final_eject_distance = config.getfloat('gate_final_eject_distance', 0)
        self.bypass_autoload = config.getint('bypass_autoload', 1, minval=0, maxval=1)
        self.encoder_dwell = config.getfloat('encoder_dwell', 0.1, minval=0., maxval=2.) # Not exposed
        self.encoder_move_step_size = config.getfloat('encoder_move_step_size', 15., minval=5., maxval=25.) # Not exposed

        # Configuration for (fast) bowden move
        self.bowden_homing_max = config.getfloat('bowden_homing_max', 2000., minval=100.)
        self.bowden_fast_unload_portion = config.getfloat('bowden_fast_unload_portion', 95, minval=50, maxval=100) # % of calibrated bowden length for fast (non-homing) unloading move # PAUL new - implement me
        self.bowden_fast_load_portion = config.getfloat('bowden_fast_load_portion', 95, minval=50, maxval=100)     # % of calibrated bowden length for fast (non-homing) loading move # PAUL new - implement me
        self.bowden_apply_correction = config.getint('bowden_apply_correction', 0, minval=0, maxval=1)
        self.bowden_allowable_load_delta = config.getfloat('bowden_allowable_load_delta', 10., minval=1.)
        self.bowden_allowable_unload_delta = config.getfloat('bowden_allowable_unload_delta', self.bowden_allowable_load_delta, minval=1.)
        self.bowden_move_error_tolerance = config.getfloat('bowden_move_error_tolerance', 60, minval=0, maxval=100) # Percentage of delta of move that results in error
        self.bowden_pre_unload_test = config.getint('bowden_pre_unload_test', 0, minval=0, maxval=1) # Check for bowden movement before full pull
        self.bowden_pre_unload_error_tolerance = config.getfloat('bowden_pre_unload_error_tolerance', 100, minval=0, maxval=100) # Allowable delta movement % before error

        # Configuration for extruder and toolhead homing
        self.extruder_force_homing = config.getint('extruder_force_homing', 0, minval=0, maxval=1)
        self.extruder_homing_endstop = config.getchoice('extruder_homing_endstop', {o: o for o in self.EXTRUDER_ENDSTOPS}, self.SENSOR_EXTRUDER_NONE)
        self.extruder_homing_max = config.getfloat('extruder_homing_max', 50., above=10.) # Extruder homing max
        self.extruder_homing_buffer = config.getfloat('extruder_homing_buffer', 30., minval=0.) # How far to short bowden load move to avoid overshooting # PAUL make this dynamic based on bowden_fast_load_portion
        self.extruder_collision_homing_step = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max = config.getfloat('toolhead_homing_max', 20., minval=0.) # Toolhead sensor homing max
        self.toolhead_extruder_to_nozzle = config.getfloat('toolhead_extruder_to_nozzle', 0., minval=5.) # For "sensorless"
        self.toolhead_sensor_to_nozzle = config.getfloat('toolhead_sensor_to_nozzle', 0., minval=1.) # For toolhead sensor
        self.toolhead_entry_to_extruder = config.getfloat('toolhead_entry_to_extruder', 0., minval=0.) # For extruder (entry) sensor
        self.toolhead_residual_filament = config.getfloat('toolhead_residual_filament', 0., minval=0., maxval=50.) # +ve value = reduction of load length
        self.toolhead_ooze_reduction = config.getfloat('toolhead_ooze_reduction', 0., minval=-5., maxval=20.) # +ve value = reduction of load length
        self.toolhead_unload_safety_margin = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.) # Extra unload distance
        self.toolhead_move_error_tolerance = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100) # Allowable delta movement % before error
        self.toolhead_entry_tension_test = config.getint('toolhead_entry_tension_test', 1, minval=0, maxval=1) # Use filament compression to test for successful extruder entry (requires compression sensor)
        self.toolhead_post_load_tighten = config.getint('toolhead_post_load_tighten', 60, minval=0, maxval=100) # Whether to apply filament tightening move after load (if not synced)
        self.toolhead_post_load_tension_adjust = config.getint('toolhead_post_load_tension_adjust', 1, minval=0, maxval=1) # Whether to use sync-feedback sensor to adjust tension (synced)

        # Synchronous motor control
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip = config.getint('sync_form_tip', 0, minval=0, maxval=1)
        self.sync_purge = config.getint('sync_purge', 0, minval=0, maxval=1)
        if self.mmu_unit().filament_always_gripped: # PAUL needs to be override pre-unit!
            self.sync_to_extruder = self.sync_form_tip = self.sync_purge = 1

        # TMC current control
        self.extruder_collision_homing_current = config.getint('extruder_collision_homing_current', 50, minval=10, maxval=100)
        self.extruder_form_tip_current = config.getint('extruder_form_tip_current', 100, minval=100, maxval=150)
        self.extruder_purge_current = config.getint('extruder_purge_current', 100, minval=100, maxval=150)
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

        self.extruder_load_speed = config.getfloat('extruder_load_speed', 15, minval=1.)
        self.extruder_unload_speed = config.getfloat('extruder_unload_speed', 15, minval=1.)
        self.extruder_sync_load_speed = config.getfloat('extruder_sync_load_speed', 15., minval=1.)
        self.extruder_sync_unload_speed = config.getfloat('extruder_sync_unload_speed', 15., minval=1.)
        self.extruder_accel = config.getfloat('extruder_accel', 400, above=10.)
        self.extruder_homing_speed = config.getfloat('extruder_homing_speed', 15, minval=1.)

        self.gear_buzz_accel = config.getfloat('gear_buzz_accel', 1000, minval=10.) # Not exposed

        self.macro_toolhead_max_accel = config.getfloat('macro_toolhead_max_accel', 0, minval=0)
        self.macro_toolhead_min_cruise_ratio = config.getfloat('macro_toolhead_min_cruise_ratio', minval=0., below=1.)
        if self.macro_toolhead_max_accel == 0:
            self.macro_toolhead_max_accel = config.getsection('printer').getsection('toolhead').getint('max_accel', 5000)

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
        self.espooler_operations = list(config.getlist('espooler_operations', self.ESPOOLER_OPERATIONS))

        # Optional features
        self.has_filament_buffer = bool(config.getint('has_filament_buffer', 1, minval=0, maxval=1))
        self.encoder_move_validation = config.getint('encoder_move_validation', 1, minval=0, maxval=1) # Use encoder to check load/unload movement
        self.spoolman_support = config.getchoice('spoolman_support', {o: o for o in self.SPOOLMAN_OPTIONS}, self.SPOOLMAN_OFF)
        self.t_macro_color = config.getchoice('t_macro_color', {o: o for o in self.T_MACRO_COLOR_OPTIONS}, self.T_MACRO_COLOR_SLICER)
        self.default_endless_spool_enabled = config.getint('endless_spool_enabled', 0, minval=0, maxval=1)
        self.endless_spool_on_load = config.getint('endless_spool_on_load', 0, minval=0, maxval=1)
        self.endless_spool_eject_gate = config.getint('endless_spool_eject_gate', -1, minval=-1, maxval=self.num_gates - 1)
        self.default_endless_spool_groups = list(config.getintlist('endless_spool_groups', []))
        self.tool_extrusion_multipliers = []
        self.tool_speed_multipliers = []
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
        self.console_gate_stat = config.getchoice('console_gate_stat', {o: o for o in self.GATE_STATS_TYPES}, self.GATE_STATS_STRING)
        self.console_always_output_full = config.getint('console_always_output_full', 1, minval=0, maxval=1)

        # Turn off splash bling for boring people
        self.serious = config.getint('serious', 0, minval=0, maxval=1) # Not exposed
        # Suppress the Kalico warning for dangerous people
        self.suppress_kalico_warning = config.getint('suppress_kalico_warning', 0, minval=0, maxval=1) # Not exposed

        # Currently hidden and testing options
        self.test_random_failures = config.getint('test_random_failures', 0, minval=0, maxval=1) # Not exposed
        self.test_force_in_print = config.getint('test_force_in_print', 0, minval=0, maxval=1) # Not exposed

        # Klipper tuning (aka hacks)
        self.kalico = bool(self.printer.lookup_object('danger_options', False)) # Detect Kalico (Danger Klipper) installation

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

