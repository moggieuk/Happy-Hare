    cmd_MMU_TEST_CONFIG_help = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    def cmd_MMU_TEST_CONFIG(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        unit = gcmd.get_int('UNIT', 0, minval=0, maxval=self.mmu_machine.num_units)

        # Try to catch illegal parameters
        illegal_params = [
            p for p in gcmd.get_command_parameters()
            if vars(self).get(p.lower()) is None
            and self.selector().check_test_config(p.lower())
            and self.environment_manager.check_test_config(p.lower())
            and self.mmu_unit().sync_feedback.check_test_config(p.lower())
            and p.lower() not in [
                VARS_MMU_CALIB_BOWDEN_LENGTH,
                VARS_MMU_CALIB_CLOG_LENGTH
            ]
            and p.upper() not in ['QUIET']
        ]
        if illegal_params:
            raise gcmd.error("Unknown parameter: %s" % illegal_params)

        # Filament Speeds
        self.mmu_unit().p.gear_from_filament_buffer_speed = gcmd.get_float('GEAR_FROM_FILAMENT_BUFFER_SPEED', self.mmu_unit().p.gear_from_filament_buffer_speed, minval=10.)
        self.mmu_unit().p.gear_from_filament_buffer_accel = gcmd.get_float('GEAR_FROM_FILAMENT_BUFFER_ACCEL', self.mmu_unit().p.gear_from_filament_buffer_accel, minval=10.)
        self.mmu_unit().p.gear_from_spool_speed = gcmd.get_float('GEAR_FROM_SPOOL_SPEED', self.mmu_unit().p.gear_from_spool_speed, minval=10.)
        self.mmu_unit().p.gear_from_spool_accel = gcmd.get_float('GEAR_FROM_SPOOL_ACCEL', self.mmu_unit().p.gear_from_spool_accel, above=10.)
        self.mmu_unit().p.gear_unload_speed = gcmd.get_float('GEAR_UNLOAD_SPEED', self.mmu_unit().p.gear_unload_speed, minval=10.)
        self.mmu_unit().p.gear_unload_accel = gcmd.get_float('GEAR_UNLOAD_ACCEL', self.mmu_unit().p.gear_unload_accel, above=10.)
        self.mmu_unit().p.gear_short_move_speed = gcmd.get_float('GEAR_SHORT_MOVE_SPEED', self.mmu_unit().p.gear_short_move_speed, minval=10.)
        self.mmu_unit().p.gear_short_move_accel = gcmd.get_float('GEAR_SHORT_MOVE_ACCEL', self.mmu_unit().p.gear_short_move_accel, minval=10.)
        self.mmu_unit().p.gear_short_move_threshold = gcmd.get_float('GEAR_SHORT_MOVE_THRESHOLD', self.mmu_unit().p.gear_short_move_threshold, minval=0.)
        self.mmu_unit().p.gear_homing_speed = gcmd.get_float('GEAR_HOMING_SPEED', self.mmu_unit().p.gear_homing_speed, above=1.)
        self.p.extruder_homing_speed = gcmd.get_float('EXTRUDER_HOMING_SPEED', self.p.extruder_homing_speed, above=1.)
        self.p.extruder_load_speed = gcmd.get_float('EXTRUDER_LOAD_SPEED', self.p.extruder_load_speed, above=1.)
        self.p.extruder_unload_speed = gcmd.get_float('EXTRUDER_UNLOAD_SPEED', self.p.extruder_unload_speed, above=1.)
        self.p.extruder_sync_load_speed = gcmd.get_float('EXTRUDER_SYNC_LOAD_SPEED', self.p.extruder_sync_load_speed, above=1.)
        self.p.extruder_sync_unload_speed = gcmd.get_float('EXTRUDER_SYNC_UNLOAD_SPEED', self.p.extruder_sync_unload_speed, above=1.)
        self.p.extruder_accel = gcmd.get_float('EXTRUDER_ACCEL', self.p.extruder_accel, above=10.)

        # Synchronous motor control
        self.mmu_unit().p.sync_to_extruder = gcmd.get_int('SYNC_TO_EXTRUDER', self.mmu_unit().p.sync_to_extruder, minval=0, maxval=1)
        self.mmu_unit().p.sync_form_tip = gcmd.get_int('SYNC_FORM_TIP', self.mmu_unit().p.sync_form_tip, minval=0, maxval=1)
        self.mmu_unit().p.sync_purge = gcmd.get_int('SYNC_PURGE', self.mmu_unit().p.sync_purge, minval=0, maxval=1)
        if self.mmu_unit().filament_always_gripped: # PAUL needs to be override pre-unit!
            self.mmu_unit().p.sync_to_extruder = self.mmu_unit().p.sync_form_tip = self.mmu_unit().p.sync_purge = 1

        # TMC current control
        self.mmu_unit().p.sync_gear_current = gcmd.get_int('SYNC_GEAR_CURRENT', self.mmu_unit().p.sync_gear_current, minval=10, maxval=100)
        self.mmu_unit().p.extruder_collision_homing_current = gcmd.get_int('EXTRUDER_COLLISION_HOMING_CURRENT', self.mmu_unit().p.extruder_collision_homing_current, minval=10, maxval=100)
        self.p.extruder_form_tip_current = gcmd.get_int('EXTRUDER_FORM_TIP_CURRENT', self.p.extruder_form_tip_current, minval=100, maxval=150)
        self.p.extruder_purge_current = gcmd.get_int('EXTRUDER_PURGE_CURRENT', self.p.extruder_purge_current, minval=100, maxval=150)

        # Homing, loading and unloading controls
        gate_homing_endstop = gcmd.get('GATE_HOMING_ENDSTOP', self.mmu_unit().p.gate_homing_endstop)
        if gate_homing_endstop not in GATE_ENDSTOPS:
            raise gcmd.error("gate_homing_endstop is invalid. Options are: %s" % GATE_ENDSTOPS)
        if gate_homing_endstop != self.mmu_unit().p.gate_homing_endstop:
            self.mmu_unit().p.gate_homing_endstop = gate_homing_endstop
            self.mmu_unit().calibrator.adjust_bowden_lengths_on_homing_change()

        # Special bowden calibration (get current length after potential gate_homing_endstop change)
        gate_selected = max(self.gate_selected, 0) # Assume gate 0 if not known / bypass
        bowden_length = gcmd.get_float('MMU_CALIBRATION_BOWDEN_LENGTH', self.bowden_lengths[gate_selected], minval=0.)
        if bowden_length != self.bowden_lengths[gate_selected]:
            self.mmu_unit().calibrator.update_bowden_length(bowden_length, gate_selected)

        self.mmu_unit().p.gate_endstop_to_encoder = gcmd.get_float('GATE_SENSOR_TO_ENCODER', self.mmu_unit().p.gate_endstop_to_encoder)
        self.mmu_unit().p.gate_autoload = gcmd.get_int('GATE_AUTOLOAD', self.mmu_unit().p.gate_autoload, minval=0, maxval=1)
        self.mmu_unit().p.gate_final_eject_distance = gcmd.get_float('GATE_FINAL_EJECT_DISTANCE', self.mmu_unit().p.gate_final_eject_distance)
        self.gate_unload_buffer = gcmd.get_float('GATE_UNLOAD_BUFFER', self.gate_unload_buffer, minval=0.)
        self.mmu_unit().p.gate_homing_max = gcmd.get_float('GATE_HOMING_MAX', self.mmu_unit().p.gate_homing_max)
        self.mmu_unit().p.gate_parking_distance = gcmd.get_float('GATE_PARKING_DISTANCE', self.mmu_unit().p.gate_parking_distance)
        self.mmu_unit().p.gate_preload_homing_max = gcmd.get_float('GATE_PRELOAD_HOMING_MAX', self.mmu_unit().p.gate_preload_homing_max)
        self.mmu_unit().p.gate_preload_parking_distance = gcmd.get_float('GATE_PRELOAD_PARKING_DISTANCE', self.mmu_unit().p.gate_preload_parking_distance)

        self.p.bypass_autoload = gcmd.get_int('BYPASS_AUTOLOAD', self.p.bypass_autoload, minval=0, maxval=1)
        self.mmu_unit().p.bowden_apply_correction = gcmd.get_int('BOWDEN_APPLY_CORRECTION', self.mmu_unit().p.bowden_apply_correction, minval=0, maxval=1)
        self.mmu_unit().p.bowden_allowable_load_delta = gcmd.get_float('BOWDEN_ALLOWABLE_LOAD_DELTA', self.mmu_unit().p.bowden_allowable_load_delta, minval=1., maxval=50.)
        self.bowden_allowable_unload_delta = gcmd.get_float('BOWDEN_ALLOWABLE_UNLOAD_DELTA', self.bowden_allowable_unload_delta, minval=1., maxval=50.)
        self.mmu_unit().p.bowden_pre_unload_test = gcmd.get_int('BOWDEN_PRE_UNLOAD_TEST', self.mmu_unit().p.bowden_pre_unload_test, minval=0, maxval=1)

        extruder_homing_endstop = gcmd.get('EXTRUDER_HOMING_ENDSTOP', self.mmu_unit().p.extruder_homing_endstop)
        if extruder_homing_endstop not in EXTRUDER_ENDSTOPS:
            raise gcmd.error("extruder_homing_endstop is invalid. Options are: %s" % EXTRUDER_ENDSTOPS)
        self.mmu_unit().p.extruder_homing_endstop = extruder_homing_endstop

        self.mmu_unit().p.extruder_homing_max = gcmd.get_float('EXTRUDER_HOMING_MAX', self.mmu_unit().p.extruder_homing_max, above=10.)
        self.mmu_unit().p.extruder_force_homing = gcmd.get_int('EXTRUDER_FORCE_HOMING', self.mmu_unit().p.extruder_force_homing, minval=0, maxval=1)

        self.mmu_unit().p.toolhead_homing_max = gcmd.get_float('TOOLHEAD_HOMING_MAX', self.mmu_unit().p.toolhead_homing_max, minval=0.)
        self.p.toolhead_entry_to_extruder = gcmd.get_float('TOOLHEAD_ENTRY_TO_EXTRUDER', self.p.toolhead_entry_to_extruder, minval=0.)
        self.p.toolhead_sensor_to_nozzle = gcmd.get_float('TOOLHEAD_SENSOR_TO_NOZZLE', self.p.toolhead_sensor_to_nozzle, minval=0.)
        self.p.toolhead_extruder_to_nozzle = gcmd.get_float('TOOLHEAD_EXTRUDER_TO_NOZZLE', self.p.toolhead_extruder_to_nozzle, minval=0.)
        self.p.toolhead_residual_filament = gcmd.get_float('TOOLHEAD_RESIDUAL_FILAMENT', self.p.toolhead_residual_filament, minval=0.)
        self.p.toolhead_ooze_reduction = gcmd.get_float('TOOLHEAD_OOZE_REDUCTION', self.p.toolhead_ooze_reduction, minval=-5., maxval=20.)
        self.mmu_unit().p.toolhead_unload_safety_margin = gcmd.get_float('TOOLHEAD_UNLOAD_SAFETY_MARGIN', self.mmu_unit().p.toolhead_unload_safety_margin, minval=0.)
        self.mmu_unit().p.toolhead_post_load_tighten = gcmd.get_int('TOOLHEAD_POST_LOAD_TIGHTEN', self.mmu_unit().p.toolhead_post_load_tighten, minval=0, maxval=100)
        self.mmu_unit().p.toolhead_post_load_tension_adjust = gcmd.get_int('TOOLHEAD_POST_LOAD_TENSION_ADJUST', self.mmu_unit().p.toolhead_post_load_tension_adjust, minval=0, maxval=1)
        self.mmu_unit().p.toolhead_entry_tension_test = gcmd.get_int('TOOLHEAD_ENTRY_TENSION_TEST', self.mmu_unit().p.toolhead_entry_tension_test, minval=0, maxval=1)
        self.p.gcode_load_sequence = gcmd.get_int('GCODE_LOAD_SEQUENCE', self.p.gcode_load_sequence, minval=0, maxval=1)
        self.p.gcode_unload_sequence = gcmd.get_int('GCODE_UNLOAD_SEQUENCE', self.p.gcode_unload_sequence, minval=0, maxval=1)

        # Software behavior options
        self.p.extruder_temp_variance = gcmd.get_float('EXTRUDER_TEMP_VARIANCE', self.p.extruder_temp_variance, minval=1.)
        self.endless_spool_enabled = gcmd.get_int('ENDLESS_SPOOL_ENABLED', self.endless_spool_enabled, minval=0, maxval=1)
        self.p.endless_spool_on_load = gcmd.get_int('ENDLESS_SPOOL_ON_LOAD', self.p.endless_spool_on_load, minval=0, maxval=1)
        self.p.endless_spool_eject_gate = gcmd.get_int('ENDLESS_SPOOL_EJECT_GATE', self.p.endless_spool_eject_gate, minval=-1, maxval=self.num_gates - 1)

        prev_spoolman_support = self.p.spoolman_support
        spoolman_support = gcmd.get('SPOOLMAN_SUPPORT', self.p.spoolman_support)
        if spoolman_support not in SPOOLMAN_OPTIONS:
            raise gcmd.error("spoolman_support is invalid. Options are: %s" % SPOOLMAN_OPTIONS)
        if spoolman_support == SPOOLMAN_OFF:
            self.gate_spool_id[:] = [-1] * self.num_gates
        self.p.spoolman_support = spoolman_support

        prev_t_macro_color = self.p.t_macro_color
        t_macro_color = gcmd.get('T_MACRO_COLOR', self.p.t_macro_color)
        if t_macro_color not in T_MACRO_COLOR_OPTIONS:
            raise gcmd.error("t_macro_color is invalid. Options are: %s" % T_MACRO_COLOR_OPTIONS)
        self.p.t_macro_color = t_macro_color

        self.p.log_level = gcmd.get_int('LOG_LEVEL', self.p.log_level, minval=0, maxval=4)
        self.p.log_file_level = gcmd.get_int('LOG_FILE_LEVEL', self.p.log_file_level, minval=0, maxval=4)
        self.p.log_visual = gcmd.get_int('LOG_VISUAL', self.p.log_visual, minval=0, maxval=1)
        self.p.log_statistics = gcmd.get_int('LOG_STATISTICS', self.p.log_statistics, minval=0, maxval=1)
        self.p.log_m117_messages = gcmd.get_int('LOG_M117_MESSAGES', self.p.log_m117_messages, minval=0, maxval=1)

        console_gate_stat = gcmd.get('CONSOLE_GATE_STAT', self.p.console_gate_stat)
        if console_gate_stat not in GATE_STATS_TYPES:
            raise gcmd.error("console_gate_stat is invalid. Options are: %s" % GATE_STATS_TYPES)
        self.p.console_gate_stat = console_gate_stat

        self.p.slicer_tip_park_pos = gcmd.get_float('SLICER_TIP_PARK_POS', self.p.slicer_tip_park_pos, minval=0.)
        self.p.force_form_tip_standalone = gcmd.get_int('FORCE_FORM_TIP_STANDALONE', self.p.force_form_tip_standalone, minval=0, maxval=1)
        self.p.force_purge_standalone = gcmd.get_int('FORCE_PURGE_STANDALONE', self.p.force_purge_standalone, minval=0, maxval=1)
        self.p.strict_filament_recovery = gcmd.get_int('STRICT_FILAMENT_RECOVERY', self.p.strict_filament_recovery, minval=0, maxval=1)
        self.p.filament_recovery_on_pause = gcmd.get_int('FILAMENT_RECOVERY_ON_PAUSE', self.p.filament_recovery_on_pause, minval=0, maxval=1)
        self.mmu_unit().p.gate_preload_attempts = gcmd.get_int('GATE_PRELOAD_ATTEMPTS', self.mmu_unit().p.gate_preload_attempts, minval=1, maxval=20)
        self.mmu_unit().p.encoder_move_validation = gcmd.get_int('ENCODER_MOVE_VALIDATION', self.mmu_unit().p.encoder_move_validation, minval=0, maxval=1)
        self.mmu_unit().p.autotune_rotation_distance = gcmd.get_int('AUTOTUNE_ROTATION_DISTANCE', self.mmu_unit().p.autotune_rotation_distance, minval=0, maxval=1)
        self.mmu_unit().p.autotune_bowden_length = gcmd.get_int('AUTOTUNE_BOWDEN_LENGTH', self.mmu_unit().p.autotune_bowden_length, minval=0, maxval=1)
        self.p.retry_tool_change_on_error = gcmd.get_int('RETRY_TOOL_CHANGE_ON_ERROR', self.p.retry_tool_change_on_error, minval=0, maxval=1)
        self.p.print_start_detection = gcmd.get_int('PRINT_START_DETECTION', self.p.print_start_detection, minval=0, maxval=1)
        self.p.show_error_dialog = gcmd.get_int('SHOW_ERROR_DIALOG', self.p.show_error_dialog, minval=0, maxval=1)
        form_tip_macro = gcmd.get('FORM_TIP_MACRO', self.p.form_tip_macro)
        if form_tip_macro != self.p.form_tip_macro:
            self.form_tip_vars = None # If macro is changed invalidate defaults
        self.p.form_tip_macro = form_tip_macro
        self.p.purge_macro = gcmd.get('PURGE_MACRO', self.p.purge_macro)

        # Available only with espooler
        if self.has_espooler():
            self.mmu_unit().p.espooler_min_distance = gcmd.get_float('ESPOOLER_MIN_DISTANCE', self.mmu_unit().p.espooler_min_distance, above=0)
            self.mmu_unit().p.espooler_max_stepper_speed = gcmd.get_float('ESPOOLER_MAX_STEPPER_SPEED', self.mmu_unit().p.espooler_max_stepper_speed, above=0)
            self.mmu_unit().p.espooler_min_stepper_speed = gcmd.get_float('ESPOOLER_MIN_STEPPER_SPEED', self.mmu_unit().p.espooler_min_stepper_speed, minval=0., below=self.mmu_unit().p.espooler_max_stepper_speed)
            self.mmu_unit().p.espooler_speed_exponent = gcmd.get_float('ESPOOLER_SPEED_EXPONENT', self.mmu_unit().p.espooler_speed_exponent, above=0)
            self.mmu_unit().p.espooler_assist_reduced_speed = gcmd.get_int('ESPOOLER_ASSIST_REDUCED_SPEED', 50, minval=0, maxval=100)
            self.mmu_unit().p.espooler_printing_power = gcmd.get_int('ESPOOLER_PRINTING_POWER', self.mmu_unit().p.espooler_printing_power, minval=0, maxval=100)
            self.mmu_unit().p.espooler_assist_extruder_move_length = gcmd.get_float("ESPOOLER_ASSIST_EXTRUDER_MOVE_LENGTH", self.mmu_unit().p.espooler_assist_extruder_move_length, above=10.)
            self.mmu_unit().p.espooler_assist_burst_power = gcmd.get_int("ESPOOLER_ASSIST_BURST_POWER", self.mmu_unit().p.espooler_assist_burst_power, minval=0, maxval=100)
            self.mmu_unit().p.espooler_assist_burst_duration = gcmd.get_float("ESPOOLER_ASSIST_BURST_DURATION", self.mmu_unit().p.espooler_assist_burst_duration, above=0., maxval=10.)
            self.mmu_unit().p.espooler_assist_burst_trigger = gcmd.get_int("ESPOOLER_ASSIST_BURST_TRIGGER", self.mmu_unit().p.espooler_assist_burst_trigger, minval=0, maxval=1)
            self.mmu_unit().p.espooler_assist_burst_trigger_max = gcmd.get_int("ESPOOLER_ASSIST_BURST_TRIGGER_MAX", self.mmu_unit().p.espooler_assist_burst_trigger_max, minval=1)
            self.mmu_unit().p.espooler_rewind_burst_power = gcmd.get_int("ESPOOLER_REWIND_BURST_POWER", self.mmu_unit().p.espooler_rewind_burst_power, minval=0, maxval=100)
            self.mmu_unit().p.espooler_rewind_burst_duration = gcmd.get_float("ESPOOLER_REWIND_BURST_DURATION", self.mmu_unit().p.espooler_rewind_burst_duration, above=0., maxval=10.)

            espooler_operations = list(gcmd.get('ESPOOLER_OPERATIONS', ','.join(self.mmu_unit().p.espooler_operations)).split(','))
            for op in espooler_operations:
                if op not in ESPOOLER_OPERATIONS:
                    raise gcmd.error("espooler_operations '%s' is invalid. Options are: %s" % (op, ESPOOLER_OPERATIONS))
            self.mmu_unit().p.espooler_operations = espooler_operations

        # Available only with encoder
        if self.has_encoder(): # PAUL is this correct to get clog
            cal_clog_length = self.var_manager.get(VARS_MMU_CALIB_CLOG_LENGTH, None)
            clog_length = gcmd.get_float('MMU_CALIBRATION_CLOG_LENGTH', cal_clog_length, minval=1., maxval=100.)
            if clog_length != cal_clog_length:
                self.mmu_unit().calibrator.update_clog_detection_length(clog_length, force=True)

        # Currently hidden and testing options
        self.p.test_random_failures = gcmd.get_int('TEST_RANDOM_FAILURES', self.p.test_random_failures, minval=0, maxval=1)
        self.p.test_force_in_print = gcmd.get_int('TEST_FORCE_IN_PRINT', self.p.test_force_in_print, minval=0, maxval=1)
        self.p.canbus_comms_retries = gcmd.get_int('CANBUS_COMMS_RETRIES', self.p.canbus_comms_retries, minval=1, maxval=10)
        self.p.serious = gcmd.get_int('SERIOUS', self.p.serious, minval=0, maxval=1)

        # Sub components
# PAUL old        self.selector().set_test_config(gcmd)
        for m in self.managers:
            if hasattr(m, 'set_test_config'):
                m.set_test_config(gcmd)

        if not quiet:
            msg = "FILAMENT MOVEMENT SPEEDS:"
            msg += "\ngear_from_spool_speed = %.1f" % self.mmu_unit().p.gear_from_spool_speed
            msg += "\ngear_from_spool_accel = %.1f" % self.mmu_unit().p.gear_from_spool_accel
            msg += "\ngear_unload_speed = %.1f" % self.mmu_unit().p.gear_unload_speed
            msg += "\ngear_unload_accel = %.1f" % self.mmu_unit().p.gear_unload_accel
            if self.mmu_unit().p.has_filament_buffer:
                msg += "\ngear_from_filament_buffer_speed = %.1f" % self.mmu_unit().p.gear_from_filament_buffer_speed
                msg += "\ngear_from_filament_buffer_accel = %.1f" % self.mmu_unit().p.gear_from_filament_buffer_accel
            msg += "\ngear_short_move_speed = %.1f" % self.mmu_unit().p.gear_short_move_speed
            msg += "\ngear_short_move_accel = %.1f" % self.mmu_unit().p.gear_short_move_accel
            msg += "\ngear_short_move_threshold = %.1f" % self.mmu_unit().p.gear_short_move_threshold
            msg += "\ngear_homing_speed = %.1f" % self.mmu_unit().p.gear_homing_speed
            msg += "\nextruder_homing_speed = %.1f" % self.p.extruder_homing_speed
            msg += "\nextruder_load_speed = %.1f" % self.p.extruder_load_speed
            msg += "\nextruder_unload_speed = %.1f" % self.p.extruder_unload_speed
            msg += "\nextruder_sync_load_speed = %.1f" % self.p.extruder_sync_load_speed
            msg += "\nextruder_sync_unload_speed = %.1f" % self.p.extruder_sync_unload_speed
            msg += "\nextruder_accel = %.1f" % self.p.extruder_accel

            msg += "\n\nMOTOR SYNC CONTROL:"
            msg += "\nsync_to_extruder = %d" % self.mmu_unit().p.sync_to_extruder
            msg += "\nsync_form_tip = %d" % self.mmu_unit().p.sync_form_tip
            msg += "\nsync_purge = %d" % self.mmu_unit().p.sync_purge
            msg += "\nsync_gear_current = %d%%" % self.mmu_unit().p.sync_gear_current
            if self.has_encoder():
                msg += "\nextruder_collision_homing_current = %d%%" % self.mmu_unit().p.extruder_collision_homing_current
            msg += "\nextruder_form_tip_current = %d%%" % self.p.extruder_form_tip_current
            msg += "\nextruder_purge_current = %d%%" % self.p.extruder_purge_current
            msg += self.mmu_unit().sync_feedback.get_test_config()

            msg += "\n\nLOADING/UNLOADING:"
            msg += "\ngate_homing_endstop = %s" % self.mmu_unit().p.gate_homing_endstop
            if self.mmu_unit().p.gate_homing_endstop in [SENSOR_GATE] and self.has_encoder():
                msg += "\ngate_endstop_to_encoder = %s" % self.mmu_unit().p.gate_endstop_to_encoder
            msg += "\ngate_unload_buffer = %s" % self.gate_unload_buffer
            msg += "\ngate_homing_max = %s" % self.mmu_unit().p.gate_homing_max
            msg += "\ngate_parking_distance = %s" % self.mmu_unit().p.gate_parking_distance
            msg += "\ngate_preload_homing_max = %s" % self.mmu_unit().p.gate_preload_homing_max
            msg += "\ngate_preload_parking_distance = %s" % self.mmu_unit().p.gate_preload_parking_distance
            msg += "\ngate_autoload = %s" % self.mmu_unit().p.gate_autoload
            msg += "\ngate_final_eject_distance = %s" % self.mmu_unit().p.gate_final_eject_distance
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                msg += "\nbypass_autoload = %s" % self.p.bypass_autoload
            if self.has_encoder():
                msg += "\nbowden_apply_correction = %d" % self.mmu_unit().p.bowden_apply_correction
                msg += "\nbowden_allowable_load_delta = %d" % self.mmu_unit().p.bowden_allowable_load_delta
                msg += "\nbowden_pre_unload_test = %d" % self.mmu_unit().p.bowden_pre_unload_test
            msg += "\nextruder_force_homing = %d" % self.mmu_unit().p.extruder_force_homing
            msg += "\nextruder_homing_endstop = %s" % self.mmu_unit().p.extruder_homing_endstop
            msg += "\nextruder_homing_max = %.1f" % self.mmu_unit().p.extruder_homing_max
            msg += "\ntoolhead_extruder_to_nozzle = %.1f" % self.p.toolhead_extruder_to_nozzle
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                msg += "\ntoolhead_sensor_to_nozzle = %.1f" % self.p.toolhead_sensor_to_nozzle
                msg += "\ntoolhead_homing_max = %.1f" % self.mmu_unit().p.toolhead_homing_max
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                msg += "\ntoolhead_entry_to_extruder = %.1f" % self.p.toolhead_entry_to_extruder
            msg += "\ntoolhead_residual_filament = %.1f" % self.p.toolhead_residual_filament
            msg += "\ntoolhead_ooze_reduction = %.1f" % self.p.toolhead_ooze_reduction
            msg += "\ntoolhead_unload_safety_margin = %d" % self.mmu_unit().p.toolhead_unload_safety_margin
            msg += "\ntoolhead_entry_tension_test = %d" % self.mmu_unit().p.toolhead_entry_tension_test
            msg += "\ntoolhead_post_load_tighten = %d" % self.mmu_unit().p.toolhead_post_load_tighten
            msg += "\ntoolhead_post_load_tension_adjust = %d" % self.mmu_unit().p.toolhead_post_load_tension_adjust
            msg += "\ngcode_load_sequence = %d" % self.p.gcode_load_sequence
            msg += "\ngcode_unload_sequence = %d" % self.p.gcode_unload_sequence

            msg += "\n\nTIP FORMING/CUTTING:"
            msg += "\nform_tip_macro = %s" % self.p.form_tip_macro
            msg += "\nforce_form_tip_standalone = %d" % self.p.force_form_tip_standalone
            if not self.p.force_form_tip_standalone:
                msg += "\nslicer_tip_park_pos = %.1f" % self.p.slicer_tip_park_pos

            msg += "\n\nPURGING:"
            msg += "\npurge_macro = %s" % self.p.purge_macro
            msg += "\nforce_purge_standalone = %d" % self.p.force_purge_standalone

            if self.has_espooler():
                msg += "\n\nESPOOLER:"
                msg += "\nespooler_min_distance = %s" % self.mmu_unit().p.espooler_min_distance
                msg += "\nespooler_max_stepper_speed = %s" % self.mmu_unit().p.espooler_max_stepper_speed
                msg += "\nespooler_min_stepper_speed = %s" % self.mmu_unit().p.espooler_min_stepper_speed
                msg += "\nespooler_speed_exponent = %s" % self.mmu_unit().p.espooler_speed_exponent
                msg += "\nespooler_assist_reduced_speed = %s%%" % self.mmu_unit().p.espooler_assist_reduced_speed
                msg += "\nespooler_printing_power = %s%%" % self.mmu_unit().p.espooler_printing_power
                msg += "\nespooler_assist_extruder_move_length = %s" % self.mmu_unit().p.espooler_assist_extruder_move_length
                msg += "\nespooler_assist_burst_power = %d" % self.mmu_unit().p.espooler_assist_burst_power
                msg += "\nespooler_assist_burst_duration = %s" % self.mmu_unit().p.espooler_assist_burst_duration
                msg += "\nespooler_assist_burst_trigger = %d" % self.mmu_unit().p.espooler_assist_burst_trigger
                msg += "\nespooler_assist_burst_trigger_max = %d" % self.mmu_unit().p.espooler_assist_burst_trigger_max
                msg += "\nespooler_operations = %s"  % self.mmu_unit().p.espooler_operations

            # Heater/Environment
            msg += self.environment_manager.get_test_config()

            msg += "\n\nLOGGING:"
            msg += "\nlog_level = %d" % self.p.log_level
            msg += "\nlog_visual = %d" % self.p.log_visual
            if self.mmu_logger:
                msg += "\nlog_file_level = %d" % self.p.log_file_level
            msg += "\nlog_statistics = %d" % self.p.log_statistics
            msg += "\nlog_m117_messages = %d" % self.p.log_m117_messages
            msg += "\nconsole_gate_stat = %s" % self.p.console_gate_stat

            msg += "\n\nOTHER:"
            msg += "\nextruder_temp_variance = %.1f" % self.p.extruder_temp_variance
            msg += "\nendless_spool_enabled = %d" % self.endless_spool_enabled
            msg += "\nendless_spool_on_load = %d" % self.p.endless_spool_on_load
            msg += "\nendless_spool_eject_gate = %d" % self.p.endless_spool_eject_gate
            msg += "\nspoolman_support = %s" % self.p.spoolman_support
            msg += "\nt_macro_color = %s" % self.p.t_macro_color
            msg += "\npreload_attempts = %d" % self.preload_attempts
            if self.has_encoder():
                msg += "\nstrict_filament_recovery = %d" % self.p.strict_filament_recovery
                msg += "\nencoder_move_validation = %d" % self.mmu_unit().p.encoder_move_validation
                msg += "\nautotune_rotation_distance = %d" % self.mmu_unit().p.autotune_rotation_distance
            msg += "\nautotune_bowden_length = %d" % self.mmu_unit().p.autotune_bowden_length
            msg += "\nfilament_recovery_on_pause = %d" % self.p.filament_recovery_on_pause
            msg += "\nretry_tool_change_on_error = %d" % self.p.retry_tool_change_on_error
            msg += "\nprint_start_detection = %d" % self.p.print_start_detection
            msg += "\nshow_error_dialog = %d" % self.p.show_error_dialog

            # Selector and Servo
            msg += self.selector().get_test_config()

            # These are in mmu_vars.cfg and are offered here for convenience
            msg += "\n\nCALIBRATION (mmu_vars.cfg):"
            if self.mmu_unit().variable_bowden_lengths:
                msg += "\nmmu_calibration_bowden_lengths = %s" % self.bowden_lengths
            else:
                msg += "\nmmu_calibration_bowden_length = %.1f" % self.bowden_lengths[0]
            if self.has_encoder():
                msg += "\nmmu_calibration_clog_length = %.1f" % self.encoder().get_clog_detection_length()

            self.log_info(msg)

        # Some changes need additional action to be taken
        if prev_spoolman_support != self.p.spoolman_support:
            self._spoolman_sync()
        if prev_t_macro_color != self.p.t_macro_color:
            self._update_t_macros()
