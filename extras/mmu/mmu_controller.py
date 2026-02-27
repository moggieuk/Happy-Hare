# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Main control class for any Klipper based MMU (includes filament driver/gear control)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import gc, sys, ast, random, logging, time, contextlib, math, os.path, re, unicodedata, traceback


# Klipper imports
import chelper
from ..homing                   import Homing, HomingMove
from ..tmc                      import TMCCommandHelper

# Happy Hare imports
from .mmu_constants             import *
from .mmu_logger                import MmuLogger
from .mmu_test                  import MmuTest
from .mmu_utils                 import MmuError, DebugStepperMovement, PurgeVolCalculator
from .mmu_sensor_manager        import MmuSensorManager
from .mmu_sensor_utils          import MmuRunoutHelper
from .mmu_led_manager           import MmuLedManager
from .mmu_environment_manager   import MmuEnvironmentManager
from .mmu_parameters            import MmuParameters
from .unit.mmu_encoder          import RUNOUT_DISABLED, RUNOUT_STATIC, RUNOUT_AUTOMATIC


# Main klipper module
class MmuController:

    def __init__(self, mmu_machine, config):
        logging.info("PAUL: init() for MmuController")
        self.mmu_machine = mmu_machine
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

#        self.mmu_machine = self.printer.lookup_object("mmu_machine")
        self.num_gates = self.mmu_machine.num_gates
        self.gate_selected = TOOL_GATE_UNKNOWN # PAUL TEMP HACK to get through initialization
        self.w3c_colors = dict(W3C_COLORS)
        self.filament_remaining = 0.
        self._last_tool = self._next_tool = TOOL_GATE_UNKNOWN
        self._next_gate = None
        self.toolchange_retract = 0.          # Set from mmu_macro_vars
        self.toolchange_purge_volume = 0.
        self.mmu_logger = None                # Setup on connect
        self._standalone_sync = False         # Used to indicate synced extruder intention whilst out of print
        self._suppress_release_grip = False   # Used to suppress the relaxing of grip on recursive calls to prevent servo flutter
        self.bowden_start_pos = None          # If set then we can measure bowden progress
        self.has_blobifier = False            # Post load blobbling macro (like BLOBIFIER)
        self.has_mmu_cutter = False           # Post unload cutting macro (like EREC)
        self.has_toolhead_cutter = False      # Form tip cutting macro (like _MMU_CUT_TIP)
        self._is_running_test = False         # True while running QA or soak tests
        self.slicer_tool_map = None

        # Parameters
        self.p = mmu_machine.params

# MOGGIE PAUL FIX ME .. just getting first unit vv 
        self.extruder_name = self.mmu_unit(0).extruder_name # TODO Idea: This could be per-gate to map gates to different extruders!
# MOGGIE ^^

        self.kalico = bool(self.printer.lookup_object('danger_options', False))

        self.tool_extrusion_multipliers = []
        self.tool_speed_multipliers = []

        # Complete setup of other components
        self._setup_logging()
#        if not self.mmu_machine.homing_extruder:
#            self.log_debug("Warning: Using original klipper extruder stepper. Extruder homing not possible")

# PAUL move this
#        # Dynamically instantiate and register the selector class for each unit
#        for unit in self.mmu_machine.units:
#            selector = globals()[unit.selector_type](self, unit)
#            if not isinstance(selector, BaseSelector):
#                raise self.config.error("Invalid Selector class for MMU unit %s" % unit.name)
#            unit.set_selector(selector)
#
        # Managers are responsible for handling multiple mmu_units and encapsulate specific
        # functionality to reduce the complexity of main class
        self.sensor_manager        = MmuSensorManager(self) # Must be done during initialization because also sets up homing endstops
        self.led_manager           = MmuLedManager(self)
        self.environment_manager   = MmuEnvironmentManager(self)

# PAUL
#        #self.environment_manager   = MmuEnvironmentManager(self)
#        self.managers = [
#            self.sensor_manager,
#            self.led_manager,
#            #self.environment_manager,
#        ]

        # Establish defaults for "reset" operation ----------------------------------------------------------
        # These lists are the defaults (used when reset) and will be overriden by values in mmu_vars.cfg...

        # Endless spool groups
        self.endless_spool_enabled = self.p.endless_spool_enabled
        if len(self.p.default_endless_spool_groups) > 0:
            if self.endless_spool_enabled == 1 and len(self.p.default_endless_spool_groups) != self.num_gates:
                raise self.config.error("endless_spool_groups has a different number of values than the number of gates")
        else:
            self.p.default_endless_spool_groups = list(range(self.num_gates))
        self.endless_spool_groups = list(self.p.default_endless_spool_groups)

        # Components of the gate map (status, material, color, spool_id, filament name, temperature, and speed override)
        self.gate_map_vars = [ (VARS_MMU_GATE_STATUS, 'gate_status', GATE_UNKNOWN),
                               (VARS_MMU_GATE_FILAMENT_NAME, 'gate_filament_name', ""),
                               (VARS_MMU_GATE_MATERIAL, 'gate_material', ""),
                               (VARS_MMU_GATE_COLOR, 'gate_color', ""),
                               (VARS_MMU_GATE_TEMPERATURE, 'gate_temperature', int(self.p.default_extruder_temp)),
                               (VARS_MMU_GATE_SPOOL_ID, 'gate_spool_id', -1),
                               (VARS_MMU_GATE_SPEED_OVERRIDE, 'gate_speed_override', 100) ]

        for _, attr, default in self.gate_map_vars:
            default_attr = getattr(self.p, "default_" + attr)
            if len(default_attr) > 0:
                if len(default_attr) != self.num_gates:
                    raise self.config.error("%s has different number of entries than the number of gates" % attr)
            else:
                default_attr.extend([default] * self.num_gates)
            setattr(self, attr, list(default_attr))
        self._update_gate_color_rgb()

        # Tool to gate mapping
        if len(self.p.default_ttg_map) > 0:
            if not len(self.p.default_ttg_map) == self.num_gates:
                raise self.config.error("tool_to_gate_map has different number of values than the number of gates")
        else:
            self.p.default_ttg_map = list(range(self.num_gates))
        self.ttg_map = list(self.p.default_ttg_map)

        # Tool speed and extrusion multipliers
        self.tool_extrusion_multipliers.extend([1.] * self.num_gates)
        self.tool_speed_multipliers.extend([1.] * self.num_gates)

        # Register GCODE commands ---------------------------------------------------------------------------

        # Logging and Stats
        self.gcode.register_command('MMU_RESET', self.cmd_MMU_RESET, desc = self.cmd_MMU_RESET_help)
        self.gcode.register_command('MMU_STATS', self.cmd_MMU_STATS, desc = self.cmd_MMU_STATS_help)
        self.gcode.register_command('MMU_STATUS', self.cmd_MMU_STATUS, desc = self.cmd_MMU_STATUS_help)

        # Motor control
        self.gcode.register_command('MMU_MOTORS_OFF', self.cmd_MMU_MOTORS_OFF, desc = self.cmd_MMU_MOTORS_OFF_help)
        self.gcode.register_command('MMU_MOTORS_ON', self.cmd_MMU_MOTORS_ON, desc = self.cmd_MMU_MOTORS_ON_help)
        self.gcode.register_command('MMU_SYNC_GEAR_MOTOR', self.cmd_MMU_SYNC_GEAR_MOTOR, desc=self.cmd_MMU_SYNC_GEAR_MOTOR_help)

        # Core MMU functionality
        self.gcode.register_command('MMU', self.cmd_MMU, desc = self.cmd_MMU_help)
        self.gcode.register_command('MMU_LOG', self.cmd_MMU_LOG, desc = self.cmd_MMU_LOG_help)
        self.gcode.register_command('MMU_HELP', self.cmd_MMU_HELP, desc = self.cmd_MMU_HELP_help)
        self.gcode.register_command('MMU_ENCODER', self.cmd_MMU_ENCODER, desc = self.cmd_MMU_ENCODER_help)
        self.gcode.register_command('MMU_ESPOOLER', self.cmd_MMU_ESPOOLER, desc = self.cmd_MMU_ESPOOLER_help)
        self.gcode.register_command('MMU_HOME', self.cmd_MMU_HOME, desc = self.cmd_MMU_HOME_help)
        self.gcode.register_command('MMU_SELECT', self.cmd_MMU_SELECT, desc = self.cmd_MMU_SELECT_help)
        self.gcode.register_command('MMU_SELECT_BYPASS', self.cmd_MMU_SELECT_BYPASS, desc = self.cmd_MMU_SELECT_BYPASS_help) # Alias for MMU_SELECT BYPASS=1
        self.gcode.register_command('MMU_PRELOAD', self.cmd_MMU_PRELOAD, desc = self.cmd_MMU_PRELOAD_help)
        self.gcode.register_command('MMU_CHANGE_TOOL', self.cmd_MMU_CHANGE_TOOL, desc = self.cmd_MMU_CHANGE_TOOL_help)
        # TODO Currently cannot not registered directly as Tx commands because cannot attach color/spool_id required by Mailsail
        #for tool in range(self.num_gates):
        #    self.gcode.register_command('T%d' % tool, self.cmd_MMU_CHANGE_TOOL, desc = "Change to tool T%d" % tool)
        self.gcode.register_command('MMU_LOAD', self.cmd_MMU_LOAD, desc=self.cmd_MMU_LOAD_help)
        self.gcode.register_command('MMU_EJECT', self.cmd_MMU_EJECT, desc = self.cmd_MMU_EJECT_help)
        self.gcode.register_command('MMU_UNLOAD', self.cmd_MMU_UNLOAD, desc = self.cmd_MMU_UNLOAD_help)
        self.gcode.register_command('MMU_PAUSE', self.cmd_MMU_PAUSE, desc = self.cmd_MMU_PAUSE_help)
        self.gcode.register_command('MMU_UNLOCK', self.cmd_MMU_UNLOCK, desc = self.cmd_MMU_UNLOCK_help)
        self.gcode.register_command('MMU_RECOVER', self.cmd_MMU_RECOVER, desc = self.cmd_MMU_RECOVER_help)

        # Endstops for print start / stop. Automatically called if printing from virtual SD-card
        self.gcode.register_command('MMU_PRINT_START', self.cmd_MMU_PRINT_START, desc = self.cmd_MMU_PRINT_START_help)
        self.gcode.register_command('MMU_PRINT_END', self.cmd_MMU_PRINT_END, desc = self.cmd_MMU_PRINT_END_help)

        # User Setup and Testing
        self.gcode.register_command('MMU_TEST_BUZZ_MOTOR', self.cmd_MMU_TEST_BUZZ_MOTOR, desc=self.cmd_MMU_TEST_BUZZ_MOTOR_help)
        self.gcode.register_command('MMU_TEST_GRIP', self.cmd_MMU_TEST_GRIP, desc = self.cmd_MMU_TEST_GRIP_help)
        self.gcode.register_command('MMU_TEST_LOAD', self.cmd_MMU_TEST_LOAD, desc=self.cmd_MMU_TEST_LOAD_help)
        self.gcode.register_command('MMU_TEST_MOVE', self.cmd_MMU_TEST_MOVE, desc = self.cmd_MMU_TEST_MOVE_help)
        self.gcode.register_command('MMU_TEST_HOMING_MOVE', self.cmd_MMU_TEST_HOMING_MOVE, desc = self.cmd_MMU_TEST_HOMING_MOVE_help)
        self.gcode.register_command('MMU_TEST_TRACKING', self.cmd_MMU_TEST_TRACKING, desc=self.cmd_MMU_TEST_TRACKING_help)
        self.gcode.register_command('MMU_TEST_CONFIG', self.cmd_MMU_TEST_CONFIG, desc = self.cmd_MMU_TEST_CONFIG_help)
        self.gcode.register_command('MMU_TEST_RUNOUT', self.cmd_MMU_TEST_RUNOUT, desc = self.cmd_MMU_TEST_RUNOUT_help)
        self.gcode.register_command('MMU_TEST_FORM_TIP', self.cmd_MMU_TEST_FORM_TIP, desc = self.cmd_MMU_TEST_FORM_TIP_help)
        self.gcode.register_command('MMU_TEST_PURGE', self.cmd_MMU_TEST_PURGE, desc = self.cmd_MMU_TEST_PURGE_help)

        # Soak Testing
        self.gcode.register_command('MMU_SOAKTEST_LOAD_SEQUENCE', self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE, desc = self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE_help)

        # Mapping stuff (TTG, Gate map, Slicer toolmap, Endless spool, Spoolman)
        self.gcode.register_command('MMU_TTG_MAP', self.cmd_MMU_TTG_MAP, desc = self.cmd_MMU_TTG_MAP_help)
        self.gcode.register_command('MMU_GATE_MAP', self.cmd_MMU_GATE_MAP, desc = self.cmd_MMU_GATE_MAP_help)
        self.gcode.register_command('MMU_ENDLESS_SPOOL', self.cmd_MMU_ENDLESS_SPOOL, desc = self.cmd_MMU_ENDLESS_SPOOL_help)
        self.gcode.register_command('MMU_CHECK_GATE', self.cmd_MMU_CHECK_GATE, desc = self.cmd_MMU_CHECK_GATE_help)
        self.gcode.register_command('MMU_TOOL_OVERRIDES', self.cmd_MMU_TOOL_OVERRIDES, desc = self.cmd_MMU_TOOL_OVERRIDES_help)
        self.gcode.register_command('MMU_SLICER_TOOL_MAP', self.cmd_MMU_SLICER_TOOL_MAP, desc = self.cmd_MMU_SLICER_TOOL_MAP_help)
        self.gcode.register_command('MMU_CALC_PURGE_VOLUMES', self.cmd_MMU_CALC_PURGE_VOLUMES, desc = self.cmd_MMU_CALC_PURGE_VOLUMES_help)
        self.gcode.register_command('MMU_SPOOLMAN', self.cmd_MMU_SPOOLMAN, desc = self.cmd_MMU_SPOOLMAN_help)

        # For use in user controlled load and unload macros
        self.gcode.register_command('_MMU_STEP_LOAD_GATE', self.cmd_MMU_STEP_LOAD_GATE, desc = self.cmd_MMU_STEP_LOAD_GATE_help)
        self.gcode.register_command('_MMU_STEP_UNLOAD_GATE', self.cmd_MMU_STEP_UNLOAD_GATE, desc = self.cmd_MMU_STEP_UNLOAD_GATE_help)
        self.gcode.register_command('_MMU_STEP_LOAD_BOWDEN', self.cmd_MMU_STEP_LOAD_BOWDEN, desc = self.cmd_MMU_STEP_LOAD_BOWDEN_help)
        self.gcode.register_command('_MMU_STEP_UNLOAD_BOWDEN', self.cmd_MMU_STEP_UNLOAD_BOWDEN, desc = self.cmd_MMU_STEP_UNLOAD_BOWDEN_help)
        self.gcode.register_command('_MMU_STEP_HOME_EXTRUDER', self.cmd_MMU_STEP_HOME_EXTRUDER, desc = self.cmd_MMU_STEP_HOME_EXTRUDER_help)
        self.gcode.register_command('_MMU_STEP_LOAD_TOOLHEAD', self.cmd_MMU_STEP_LOAD_TOOLHEAD, desc = self.cmd_MMU_STEP_LOAD_TOOLHEAD_help)
        self.gcode.register_command('_MMU_STEP_UNLOAD_TOOLHEAD', self.cmd_MMU_STEP_UNLOAD_TOOLHEAD, desc = self.cmd_MMU_STEP_UNLOAD_TOOLHEAD_help)
        self.gcode.register_command('_MMU_STEP_HOMING_MOVE', self.cmd_MMU_STEP_HOMING_MOVE, desc = self.cmd_MMU_STEP_HOMING_MOVE_help)
        self.gcode.register_command('_MMU_STEP_MOVE', self.cmd_MMU_STEP_MOVE, desc = self.cmd_MMU_STEP_MOVE_help)
        self.gcode.register_command('_MMU_STEP_SET_FILAMENT', self.cmd_MMU_STEP_SET_FILAMENT, desc = self.cmd_MMU_STEP_SET_FILAMENT_help)
        self.gcode.register_command('_MMU_STEP_SET_ACTION', self.cmd_MMU_STEP_SET_ACTION, desc = self.cmd_MMU_STEP_SET_ACTION_help)
        self.gcode.register_command('_MMU_M400', self.cmd_MMU_M400, desc = self.cmd_MMU_M400_help) # Wait on both movequeues

        # Internal handlers for Runout & Insertion for all sensor options
        self.gcode.register_command('__MMU_ENCODER_RUNOUT', self.cmd_MMU_ENCODER_RUNOUT, desc = self.cmd_MMU_ENCODER_RUNOUT_help)
        self.gcode.register_command('__MMU_ENCODER_INSERT', self.cmd_MMU_ENCODER_INSERT, desc = self.cmd_MMU_ENCODER_INSERT_help)
        self.gcode.register_command('__MMU_SENSOR_RUNOUT', self.cmd_MMU_SENSOR_RUNOUT, desc = self.cmd_MMU_SENSOR_RUNOUT_help)
        self.gcode.register_command('__MMU_SENSOR_REMOVE', self.cmd_MMU_SENSOR_REMOVE, desc = self.cmd_MMU_SENSOR_REMOVE_help)
        self.gcode.register_command('__MMU_SENSOR_INSERT', self.cmd_MMU_SENSOR_INSERT, desc = self.cmd_MMU_SENSOR_INSERT_help)
        self.gcode.register_command('__MMU_SENSOR_CLOG', self.cmd_MMU_SENSOR_CLOG, desc = self.cmd_MMU_SENSOR_CLOG_help)
        self.gcode.register_command('__MMU_SENSOR_TANGLE', self.cmd_MMU_SENSOR_TANGLE, desc = self.cmd_MMU_SENSOR_TANGLE_help)

        # Initializer tasks
        self.gcode.register_command('__MMU_BOOTUP', self.cmd_MMU_BOOTUP, desc = self.cmd_MMU_BOOTUP_help) # Bootup tasks

        # Load development test commands
        self.mmu_test = MmuTest(self)

        # Apply Klipper hacks -------------------------------------------------------------------------------
        if self.p.update_trsync: # Timer too close mitigation
            try:
                import mcu
                mcu.TRSYNC_TIMEOUT = max(mcu.TRSYNC_TIMEOUT, 0.05)
            except Exception as e:
                self.log_error("Unable to update TRSYNC_TIMEOUT: %s" % str(e))

        if self.p.update_bit_max_time: # Neopixel update error mitigation
            try:
                from extras import neopixel
                neopixel.BIT_MAX_TIME = max(neopixel.BIT_MAX_TIME, 0.000030)
            except Exception as e:
                self.log_error("Unable to update BIT_MAX_TIME: %s" % str(e))

        if self.p.update_aht10_commands: # Command set of AHT10 (on ViViD)
            try:
                from extras import aht10
                aht10.AHT10_COMMANDS = {
                    'INIT'              :[0xBE, 0x08, 0x00],
                    'MEASURE'           :[0xAC, 0x33, 0x00],
                    'RESET'             :[0xBE, 0x08, 0x00]
                }
            except Exception as e:
                self.log_error("Unable to update AHT10_COMMANDS: %s" % str(e))

        # Initialize state and statistics variables
        self.reinit()
        self._reset_statistics()
        self.counters = {}

    def _setup_logging(self):
        # Setup background file based logging before logging any messages
        if self.mmu_logger is None and self.p.log_file_level >= 0:
            logfile_path = self.printer.start_args['log_file']
            dirname = os.path.dirname(logfile_path)
            if dirname is None:
                mmu_log = '/tmp/mmu.log'
            else:
                mmu_log = dirname + '/mmu.log'
            logging.info("MMU: Log: %s" % mmu_log)
            self.mmu_logger = MmuLogger(mmu_log)
            self.mmu_logger.log("\n\n\nMMU Startup -----------------------------------------------\n")

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.sensor_manager.reset_active_unit(self.unit_selected)
        self.var_manager = self.mmu_machine.var_manager


# PAUL all this must be multi-extruder !!! AND multi-gear
        # Sanity check extruder name
        extruder = self.printer.lookup_object(self.extruder_name, None)
        if not extruder:
            raise self.config.error("Extruder named '%s' not found on printer" % self.extruder_name)

        # Establish gear_stepper initial gear_stepper and extruder currents and current percentage
        self.gear_tmc = self.mmu_unit().gear_tmc
        if self.gear_tmc is None:
            self.log_debug("TMC driver not found for gear_stepper, cannot use current reduction for collision detection or while synchronized printing")
        else:
            self.log_debug("Found TMC on gear_stepper. Current control enabled. Stallguard 'touch' homing possible")

        self.extruder_tmc = self.mmu_unit(0).extruder_tmc # PAUL HACK
        if self.extruder_tmc is None:
            self.log_debug("TMC driver not found for extruder, cannot use current increase for tip forming move")

        self.gear_default_run_current = self.gear_tmc.get_status(0)['run_current'] if self.gear_tmc else None
        self.extruder_default_run_current = self.extruder_tmc.get_status(0)['run_current'] if self.extruder_tmc else None
        self.gear_percentage_run_current = self.extruder_percentage_run_current = 100 # Current run percentages
        self._gear_current_locked = False # True if gear current is currently locked by wrap_gear_current()

        # Establish gear_stepper initial gear_stepper and extruder currents and current percentage
        self.gear_tmc = self.mmu_unit(0).gear_tmc # PAUL HACK
        if self.gear_tmc is None:
            self.log_debug("TMC driver not found for gear_stepper, cannot use current reduction for collision detection or while synchronized printing")
        else:
            self.log_debug("Found TMC on gear_stepper. Current control enabled. Stallguard 'touch' homing possible")
        self.extruder_tmc = self.mmu_machine.extruder_tmc
        self.gear_default_run_current = self.gear_tmc.get_status(0)['run_current'] if self.gear_tmc else None
        self.gear_percentage_run_current = self.gear_restore_percent_run_current = self.extruder_percentage_run_current = 100.

        self.gear_percentage_run_current = self.extruder_percentage_run_current = 100 # Current run percentages
        self._gear_current_locked = False # True if gear current is currently locked by wrap_gear_current()

        # Sanity check that required klipper options are enabled
        self.print_stats = self.printer.lookup_object("print_stats", None)
        if self.print_stats is None:
            self.log_debug("[virtual_sdcard] is not found in config, advanced state control is not possible")
        self.pause_resume = self.printer.lookup_object('pause_resume', None)
        if self.pause_resume is None:
            raise self.config.error("MMU requires [pause_resume] to work, please add it to your config!")

        # Remember user setting of idle_timeout so it can be restored (if not overridden)
        if self.p.default_idle_timeout < 0:
            self.p.default_idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout

        # Establish existence of Blobifier and filament cutter options
        # TODO: A little bit hacky until a more universal approach is implemented
        sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro:
            self.has_blobifier = 'blob' in sequence_vars_macro.variables.get('user_post_load_extension', '').lower()   # E.g. "BLOBIFIER" (old method of adding)
            self.has_mmu_cutter = 'cut' in sequence_vars_macro.variables.get('user_post_unload_extension', '').lower() # E.g. "EREC_CUTTER_ACTION"
        self.has_toolhead_cutter = 'cut' in self.p.form_tip_macro.lower()                                              # E.g. "_MMU_CUT_TIP"

# PAUL now in unit
#        # Let selector know
#        for unit in self.mmu_machine.units:
#            unit.selector.handle_connect()

# PAUL add in subcomponent loop... from v342
#        # Sub components
#        for m in self.managers:
#            if hasattr(m, 'handle_connect'):
#                m.handle_connect()

    def handle_disconnect(self):
        self.log_debug('Klipper disconnected!')

        # Let selector know
        for unit in self.mmu_machine.units:
            unit.selector.handle_disconnect()
# PAUL add in subcomponent loop... from v342
#        for m in self.managers:
#            if hasattr(m, 'handle_disconnect'):
#                m.handle_disconnect()

    def handle_ready(self):
        # Pull retraction length from macro config
        sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro:
            park_toolchange = sequence_vars_macro.variables.get('park_toolchange',(0))
            self.toolchange_retract = park_toolchange[-1]

        # Restore state (only if fully calibrated)
        self._load_persisted_state()

        # Setup events for managing internal print state machine
        self.printer.register_event_handler("idle_timeout:printing", self._handle_idle_timeout_printing)
        self.printer.register_event_handler("idle_timeout:ready", self._handle_idle_timeout_ready)
        self.printer.register_event_handler("idle_timeout:idle", self._handle_idle_timeout_idle)

        self._setup_hotend_off_timer()
        self._setup_pending_spool_id_timer()
        self._clear_saved_toolhead_position()

        # This is a bit naughty to register commands here but I need to make sure we are the outermost wrapper
        try:
            prev_pause = self.gcode.register_command('PAUSE', None)
            if prev_pause is not None:
                self.gcode.register_command('__PAUSE', prev_pause)
                self.gcode.register_command('PAUSE', self.cmd_PAUSE, desc = self.cmd_PAUSE_help)
            else:
                self.log_error('No existing PAUSE macro found!')

            prev_resume = self.gcode.register_command('RESUME', None)
            if prev_resume is not None:
                self.gcode.register_command('__RESUME', prev_resume)
                self.gcode.register_command('RESUME', self.cmd_MMU_RESUME, desc = self.cmd_MMU_RESUME_help)
            else:
                self.log_error('No existing RESUME macro found!')

            prev_clear_pause = self.gcode.register_command('CLEAR_PAUSE', None)
            if prev_clear_pause is not None:
                self.gcode.register_command('__CLEAR_PAUSE', prev_clear_pause)
                self.gcode.register_command('CLEAR_PAUSE', self.cmd_CLEAR_PAUSE, desc = self.cmd_CLEAR_PAUSE_help)
            else:
                self.log_error('No existing CLEAR_PAUSE macro found!')

            prev_cancel = self.gcode.register_command('CANCEL_PRINT', None)
            if prev_cancel is not None:
                self.gcode.register_command('__CANCEL_PRINT', prev_cancel)
                self.gcode.register_command('CANCEL_PRINT', self.cmd_MMU_CANCEL_PRINT, desc = self.cmd_MMU_CANCEL_PRINT_help)
            else:
                self.log_error('No existing CANCEL_PRINT macro found!')
        except Exception as e:
            self.log_error('Error trying to wrap PAUSE/RESUME/CLEAR_PAUSE/CANCEL_PRINT macros: %s' % str(e))

        # Let selector know
        for unit in self.mmu_machine.units:
            unit.selector.handle_ready()
# PAUL add in subcomponent loop... from v342
#        for m in self.managers:
#            if hasattr(m, 'handle_ready'):
#                m.handle_ready()

        # Schedule bootup tasks to run after klipper and hopefully spoolman have settled
# PAUL temp disable
#        self._schedule_mmu_bootup_tasks(BOOT_DELAY)

    def reinit(self):
        self.is_enabled = self.runout_enabled = True
        self.runout_last_enable_time = self.reactor.monotonic()
        self.is_handling_runout = self.calibrating = False
        self.last_print_stats = self.paused_extruder_temp = self.reason_for_pause = None
        self.tool_selected = self._next_tool = self.gate_selected = TOOL_GATE_UNKNOWN
        self.unit_selected = 0 # Which MMU unit is active (has active gate) if more than one
        self._last_toolchange = "Unknown"
        self.active_filament = {}
        self.filament_pos = FILAMENT_POS_UNKNOWN
        self.filament_direction = DIRECTION_UNKNOWN
        self.action = ACTION_IDLE
        self._old_action = None
        self._clear_saved_toolhead_position()
        self._reset_job_statistics()
        self.print_state = self.resume_to_state = "ready"
        self.form_tip_vars = None # Current defaults of gcode variables for tip forming macro
        self._clear_slicer_tool_map()
        self.pending_spool_id = -1 # For automatic assignment of spool_id if set perhaps by rfid reader
        self.saved_toolhead_max_accel = None
        self.num_toolchanges = 0

        self.mmu_machine.reinit() # Will iterate over all mmu_units
# PAUL ?? this is now per-unit put in mmu_unit.reinit()?        self.sync_feedback_manager.reinit()

    # Per gate (unit) component accessor router. All assume current gate unless directly specified  --------------------
    # This is key to multi mmu-unit support

    def mmu_unit(self, gate=None):
        if gate is None: gate = self.gate_selected
        if gate < 0: gate = 0
        return self.mmu_machine.get_mmu_unit_by_gate(gate)

    def selector(self, gate=None):
        return self.mmu_unit(gate).selector

    def mmu_toolhead(self, gate=None):
        return self.mmu_unit(gate).mmu_toolhead

    def gear_rail(self, gate=None):
        return self.mmu_toolhead(gate).get_kinematics().rails[1]

    def espooler(self, gate=None):
        unit = self.mmu_unit(gate)
        if unit:
            return unit.espooler
        return None

    def has_espooler(self, gate=None):
        return self.espooler(gate) is not None

    def _check_has_espooler(self): # PAUL caution on how this command will multiplex
        if any(self.has_espooler(gate) for gate in range(self.num_gates)):
            return False
        self.log_error("No espoolers configured!")
        return True

    def encoder(self, gate=None):
        if gate is None:
            gate = self.gate_selected
        unit = self.mmu_unit(gate)
        if unit:
            return unit.encoder
        return None

    def has_encoder(self, gate=None):
        return self.encoder(gate) is not None

    def _check_has_encoder(self):
        if not self.has_encoder():
            self.log_error("No encoder fitted to this MMU unit")
            return True
        return False

    def _can_use_encoder(self):
        return self.has_encoder() and self.mmu_unit().p.encoder_move_validation

    def _get_encoder_state(self):
        if self.has_encoder():
            return "%s" % "Enabled" if self.encoder().is_enabled() else "Disabled"
        else:
            return "n/a"

    # -----------------------------------------------------------------------------------------------------------

    def _clear_slicer_tool_map(self):
        skip = self.slicer_tool_map.get('skip_automap', False) if self.slicer_tool_map else False
        self.slicer_tool_map = {'tools': {}, 'referenced_tools': [], 'initial_tool': None, 'purge_volumes': [], 'total_toolchanges': None}
        self._restore_automap_option(skip)
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        self._update_t_macros() # Clear 'color' on Tx macros if displaying slicer colors

    def _restore_automap_option(self, skip=False):
        self.slicer_tool_map['skip_automap'] = skip

    # Helper to infer type for setting gcode macro variables
    def _fix_type(self, s):
        try:
            return float(s)
        except ValueError:
            try:
                return int(s)
            except ValueError:
                return s

    # Helper to ensure int when strings may be passed from UI
    def safe_int(self, i, default=0):
        try:
            return int(i)
        except ValueError:
            return default

    # Compare unicode strings with optional case insensitivity
    def _compare_unicode(self, a, b, case_insensitive=True):
        a = unicodedata.normalize('NFKC', a)
        b = unicodedata.normalize('NFKC', b)
        if case_insensitive:
            a = a.lower()
            b = b.lower()
        return a == b

    # Format color string for display
    def _format_color(self, color):
        x = re.search(r"^([a-f\d]{6})(ff)?$", color, re.IGNORECASE)
        if x is not None:
            return '#' + x.group(1).upper()

        x = re.search(r"^([a-f\d]{6}([a-f\d]{2})?)$", color, re.IGNORECASE)
        if x is not None:
            return '#' + x.group().upper()

        return color

    # This retuns the hex color format without leading '#' E.g. ff00e080
    # Support alpha channel (Nice for Mainsail/Fluidd UI)
    def _color_to_rgb_hex(self, color):
        if color in self.w3c_colors:
            color = self.w3c_colors.get(color)
        elif color == '':
            color = "000000"
        rgb_hex = color.lstrip('#').lower()
        return rgb_hex[0:8]

    # This retuns a convenient RGB fraction tuple for controlling LEDs E.g. (0.32, 0.56, 1.00)
    # or integer version (82, 143, 255). Alpha channel is cut
    def _color_to_rgb_tuple(self, color, fraction=True):
        rgb_hex = self._color_to_rgb_hex(color)[:6]
        length = len(rgb_hex)
        if fraction:
            if length % 3 == 0:
                return tuple(round(float(int(rgb_hex[i:i + length // 3], 16)) / 255, 3) for i in range(0, length, length // 3))
            return (0.,0.,0.)
        else:
            if length % 3 == 0:
                return tuple(int(rgb_hex[i:i+2], 16) for i in (0, 2, 4))
            return (0,0,0)

    # Helper to return validated color string or None if invalid
    def _validate_color(self, color):
        color = color.lower()
        if color == "":
            return ""

        # Try w3c named color
        if color in self.w3c_colors:
            return color

        # Try RGB color
        color = color.lstrip('#').lower()
        x = re.search(r"^([a-f\d]{6}([a-f\d]{2})?)$", color, re.IGNORECASE)
        if x is not None and x.group() == color:
            return color

        return None # Not valid

    # Helper for finding the closest color
    # Example:
    #   color_list = ['123456', 'abcdef', '789abc', '4a7d9f', '010203']
    #   _find_closest_color('4b7d8e', color_list) returns '4a7d9f'
    def _find_closest_color(self, ref_color, color_list):
        weighted_euclidean_distance = lambda color1, color2, weights=(0.3, 0.59, 0.11): (
            sum(weights[i] * (a - b) ** 2 for i, (a, b) in enumerate(zip(color1, color2)))
        )
        ref_rgb = self._color_to_rgb_tuple(ref_color)
        min_distance = float('inf')
        closest_color = None
        for color in color_list:
            color_rgb = self._color_to_rgb_tuple(color)
            distance = weighted_euclidean_distance(ref_rgb, color_rgb)
            if distance < min_distance:
                min_distance = distance
                closest_color = color
        return closest_color, min_distance

    # Helper to keep parallel RGB color map updated when color changes
    def _update_gate_color_rgb(self):
        # Recalculate RGB map for easy LED support
        self.gate_color_rgb = [self._color_to_rgb_tuple(i) for i in self.gate_color]

    # Helper to keep parallel RGB color map updated when slicer color or TTG changes
    # Will also update the t_macro colors
    def _update_slicer_color_rgb(self):
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        for tool_key, tool_value in self.slicer_tool_map['tools'].items():
            tool = int(tool_key)
            gate = self.ttg_map[tool]
            self.slicer_color_rgb[gate] = self._color_to_rgb_tuple(tool_value['color'])
        self._update_t_macros()
        self.led_manager.gate_map_changed(None) # Force LED update

    # Helper to determine purge volume for toolchange
    def _calc_purge_volume(self, from_tool, to_tool):
        fil_diameter = 1.75
        volume = 0.

        if to_tool >= 0:
            slicer_purge_volumes = self.slicer_tool_map['purge_volumes']
            if slicer_purge_volumes:
                if from_tool >= 0:
                    volume = slicer_purge_volumes[from_tool][to_tool]
                else:
                    # Assume worse case because we don't know from_tool
                    volume = max(row[to_tool] for row in slicer_purge_volumes)

        # Always add volume of residual filament (cut fragment and bit always left in the hotend)
        volume += math.pi * ((fil_diameter / 2) ** 2) * (self.filament_remaining + self.p.toolhead_residual_filament)
        return volume

    # Generate purge matrix based on filament colors
    def _generate_purge_matrix(self, tool_colors, purge_min, purge_max, multiplier):
        purge_vol_calc = PurgeVolCalculator(purge_min, purge_max, multiplier)

        # Build purge volume map (x=to_tool, y=from_tool)
        should_calc = lambda x,y: x < len(tool_colors) and y < len(tool_colors) and x != y
        purge_volumes = [
            [
                purge_vol_calc.calc_purge_vol_by_hex(tool_colors[y], tool_colors[x]) if should_calc(x,y) else 0
                for x in range(self.num_gates)
            ]
            for y in range(self.num_gates)
        ]
        return purge_volumes

    def _load_persisted_state(self):
        self.log_debug("Loading persisted MMU state")
        errors = []

        # Always load length of filament remaining in extruder (after cut) and last tool loaded
        self.filament_remaining = self.var_manager.get(VARS_MMU_FILAMENT_REMAINING, self.filament_remaining)
        self._last_tool = self.var_manager.get(VARS_MMU_LAST_TOOL, self._last_tool)

        # Load EndlessSpool config
        self.endless_spool_enabled = self.var_manager.get(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.endless_spool_enabled)
        endless_spool_groups = self.var_manager.get(VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        if len(endless_spool_groups) == self.num_gates:
            self.endless_spool_groups = endless_spool_groups
        else:
            errors.append("Incorrect number of gates specified in %s" % VARS_MMU_ENDLESS_SPOOL_GROUPS)

        # Load TTG map
        tool_to_gate_map = self.var_manager.get(VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map)
        if len(tool_to_gate_map) == self.num_gates:
            self.ttg_map = tool_to_gate_map
        else:
            errors.append("Incorrect number of gates specified in %s" % VARS_MMU_TOOL_TO_GATE_MAP)

        # Load gate map
        for var, attr, _ in self.gate_map_vars:
            value = self.var_manager.get(var, getattr(self, attr))
            if len(value) == self.num_gates:
                setattr(self, attr, value)
            else:
                errors.append("Incorrect number of gates specified with %s" % var)
        self._update_gate_color_rgb()

        # Load selected tool and gate
        tool_selected = self.var_manager.get(VARS_MMU_TOOL_SELECTED, self.tool_selected)
        gate_selected = self.var_manager.get(VARS_MMU_GATE_SELECTED, self.gate_selected)
        if (
            not (TOOL_GATE_BYPASS <= gate_selected <= self.num_gates) or
            gate_selected == TOOL_GATE_UNKNOWN
        ):
            errors.append("Invalid gate specified with %s or %s" % (VARS_MMU_TOOL_SELECTED, VARS_MMU_GATE_SELECTED))
            tool_selected = gate_selected = TOOL_GATE_UNKNOWN

        # Don't allow unknown gate on type-B MMU's (could also be first time bootup)
        if self.mmu_unit().multigear and gate_selected == TOOL_GATE_UNKNOWN:
# PAUL            gate_selected = 0
            gate_selected = self.mmu_unit().first_gate

        self.selector(gate_selected).restore_gate(gate_selected)
        self._set_gate_selected(gate_selected)
        self._set_tool_selected(tool_selected)
        self._ensure_ttg_match() # Ensure tool/gate consistency

        # Previous filament position
        self.filament_pos = self.var_manager.get(VARS_MMU_FILAMENT_POS, self.filament_pos)

        if len(errors) > 0:
            self.log_warning("Warning: Some persisted state was ignored because it contained errors:\n%s" % '\n'.join(errors))

        swap_stats = self.var_manager.get(VARS_MMU_SWAP_STATISTICS, {})
        counters = self.var_manager.get(VARS_MMU_COUNTERS, {})
        self.counters.update(counters)

        # Auto upgrade old names
        key_map = {"time_spent_loading": "load", "time_spent_unloading": "unload", "time_spent_paused": "pause"}
        swap_stats = {key_map.get(key, key): swap_stats[key] for key in swap_stats}
        swap_stats.pop('servo_retries', None) # DEPRECATED

        self.statistics.update(swap_stats)
        for gate in range(self.num_gates):
            self.gate_statistics[gate] = dict(EMPTY_GATE_STATS_ENTRY)
            gstats = self.var_manager.get("%s%d" % (VARS_MMU_GATE_STATISTICS_PREFIX, gate), None)
            if gstats:
                self.gate_statistics[gate].update(gstats)

    def _schedule_mmu_bootup_tasks(self, delay=0.):
        waketime = self.reactor.monotonic() + delay
        self.reactor.register_callback(lambda pt: self._print_event("__MMU_BOOTUP"), waketime)

    def _fversion(self, v):
        return "v{major}.{minor}.{patch}".format(
            major=int(v),
            minor=str(v).split('.')[1][0] if '.' in str(v) and len(str(v).split('.')[1]) > 0 else '0',
            patch=str(v).split('.')[1][1:] if '.' in str(v) and len(str(v).split('.')[1]) > 1 else '0'
        )

    cmd_MMU_BOOTUP_help = "Internal commands to complete bootup of MMU"
    def cmd_MMU_BOOTUP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        self.selector.bootup() # PAUL TODO should be all units

        try:
            # Splash...
            msg = '{1}(\_/){0}\n{1}( {0}*,*{1}){0}\n{1}(")_("){0} {5}{2}H{0}{3}a{0}{4}p{0}{2}p{0}{3}y{0} {4}H{0}{2}a{0}{3}r{0}{4}e{0} {1}%s{0} {2}R{0}{3}e{0}{4}a{0}{2}d{0}{3}y{0}{1}...{0}{6}' % self._fversion(self.mmu_machine.happy_hare_version)
            self.log_always(msg, color=True)
            if self.p.kalico:
                msg = "Warning: You are running on Kalico (Danger-Klipper). Support is not guaranteed!"
                if self.p.suppress_kalico_warning:
                    self.log_trace(msg + " Message was suppressed.")
                else:
                    self.log_warning(msg)

            # Look for filament_switch_sensors already configured to warn for possible conflicts
            for section in self.config.get_prefix_sections('filament_switch_sensor'):
                # Determine if this is created by HH or user
                fsensor = self.printer.lookup_object(section.get_name())
                if not isinstance(fsensor.runout_helper, MmuRunoutHelper):
                    fsensor_name = section.get_name().split()[1]
                    pause_on_runout = section.getboolean('pause_on_runout', False)
                    pause_on_runout_msg = " and/or pause during prints unintentionally" if pause_on_runout else ""
                    self.log_warning("Warning: filament_switch_sensor '%s' found in printer configuration. This may interfere with MMU functionality%s." % (fsensor_name, pause_on_runout_msg))

            self._set_print_state("initialized")

            # Use per gate sensors to adjust gate map
            self.gate_status = self._validate_gate_status(self.gate_status)

            # Can we verify gate selected? If so fix now
            gate_selected = self._validate_gate_selected()
            if gate_selected is not None and gate_selected != self.gate_selected:
                self.selector().restore_gate(gate_selected)
                self._set_gate_selected(gate_selected)
                self._ensure_ttg_match() # Ensure tool/gate consistency

            # Sanity check filament pos based only on non-intrusive tests and recover if necessary
            if self.sensor_manager.check_all_sensors_after(
                FILAMENT_POS_END_BOWDEN, self.gate_selected
            ):
                self._set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)

            elif (
                (self.filament_pos == FILAMENT_POS_LOADED and
                 not self.sensor_manager.check_any_sensors_after(FILAMENT_POS_END_BOWDEN, self.gate_selected)) or

                (self.filament_pos == FILAMENT_POS_UNLOADED and
                 self.sensor_manager.check_any_sensors_in_path()) or

                self.filament_pos not in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]
            ):
                self.recover_filament_pos(can_heat=False, message=True, silent=True)

            # Apply startup options
            if self.p.startup_reset_ttg_map:
                self._reset_ttg_map()

            if self.mmu_unit().p.startup_home_if_unloaded and not self.check_if_not_calibrated(CalibrationManager.CALIBRATED_SELECTOR) and self.filament_pos == FILAMENT_POS_UNLOADED:
                self.home(0)

            if self.p.log_startup_status:
                self.log_always(self._mmu_visual_to_string())
                self._display_visual_state()
            self.report_necessary_recovery()

            # Ensure espooler print assist is correct
            self._adjust_espooler_assist()

            # Initially disable clog/runout detection
            self._disable_filament_monitoring()

            self.reset_sync_gear_to_extruder(False) # Intention is not to sync unless we have to
            self.mmu_toolhead.quiesce()

            # Sync with spoolman. Delay as long as possible to maximize the chance it is contactable after startup/reboot
            self._spoolman_sync()

            # Sync lane data to Moonraker for slicer integration and cleanup old lanes
            self._moonraker_sync_lane_data()

# PAUL older logic
#            if self.has_encoder():
#                cdl = self.var_manager.get(self.VARS_MMU_CALIB_CLOG_LENGTH, None, namespace=self.encoder().name)
#                if cdl:
#                    self.encoder().set_clog_detection_length(cdl)
#                self._disable_runout() # Initially disable clog/runout detection
#
#            self.reset_sync_gear_to_extruder(False) # Intention is not to sync unless we have to
#            self.movequeues_wait()
#
#            # Sync with spoolman. Delay as long as possible to maximize the chance it is contactable after startup/reboot
#            self._spoolman_sync()
        except Exception as e:
            logging.error(traceback.format_exc())
            self.log_error('Error booting up MMU: %s' % str(e))
            raise e # PAUL TESTING TEMP

        self.mmu_macro_event(MACRO_EVENT_RESTART)

    # Wrap execution of gcode command to allow for control over:
    #  - error handling
    #  - passing of additional variables
    #  - waiting on completion
    def wrap_gcode_command(self, command, exception=False, variables=None, wait=False):
        try:
            command = command.replace("''", "")
            macro = command.split()[0]
            if not macro: return

            if variables:
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % macro, None)
                if gcode_macro:
                    gcode_macro.variables.update(variables)

            self.log_trace("Running macro: %s%s" % (command, " (with override variables)" if variables is not None else ""))

            self.gcode.run_script_from_command(command)
            if wait:
                self.mmu_toolhead.quiesce()

        except Exception as e:
            if exception is not None:
                if exception:
                    raise MmuError("Error running %s: %s" % (macro, str(e)))
                else:
                    self.log_error("Error running %s: %s" % (macro, str(e)))
            else:
                raise

    def mmu_macro_event(self, event_name, params=""):
        if self.printer.lookup_object("gcode_macro %s" % self.p.mmu_event_macro, None) is not None:
            self.wrap_gcode_command("%s EVENT=%s %s" % (self.p.mmu_event_macro, event_name, params))

    # Wait on desired move queues
    # TODO: perhaps better now to remove this method and
    # TODO: always just call self.mmu_toolhead.quiesce()
    def movequeues_wait(self, toolhead=True, mmu_toolhead=True):
        #self.log_trace("movequeues_wait(toolhead=%s, mmu_toolhead=%s)" % (toolhead, mmu_toolhead))
        if toolhead:
            self.toolhead.wait_moves()
        if mmu_toolhead:
            self.mmu_toolhead().wait_moves()

    # Dwell on desired move queues
    def movequeues_dwell(self, dwell, toolhead=True, mmu_toolhead=True):
        if dwell > 0.:
            if toolhead:
                self.toolhead.dwell(dwell)
            if mmu_toolhead:
                self.mmu_toolhead().dwell(dwell)


####################################
# LOGGING AND STATISTICS FUNCTIONS #
####################################

    def _get_action_string(self, action=None):
        if action is None:
            action = self.action

        return ("Idle" if action == ACTION_IDLE else
                "Loading" if action == ACTION_LOADING else
                "Unloading" if action == ACTION_UNLOADING else
                "Loading Ext" if action == ACTION_LOADING_EXTRUDER else
                "Exiting Ext" if action == ACTION_UNLOADING_EXTRUDER else
                "Forming Tip" if action == ACTION_FORMING_TIP else
                "Cutting Tip" if action == ACTION_CUTTING_TIP else
                "Heating" if action == ACTION_HEATING else
                "Checking" if action == ACTION_CHECKING else
                "Homing" if action == ACTION_HOMING else
                "Selecting" if action == ACTION_SELECTING else
                "Cutting Filament" if action == ACTION_CUTTING_FILAMENT else
                "Purging" if action == ACTION_PURGING else
                "Unknown") # Error case - should not happen

    def _get_bowden_progress(self):
        if self.bowden_start_pos is not None:
            bowden_length = self.calibration_manager.get_bowden_length()
            if bowden_length > 0:
                current = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()
                progress = abs(current - self.bowden_start_pos) / bowden_length
                if self.filament_direction == DIRECTION_UNLOAD:
                    progress = 1 - progress
                return round(max(0, min(100, progress * 100)))
        return -1

    # Returning new list() is so that clients like KlipperScreen sees the change
    def get_status(self, eventtime):
        status = {
            'enabled': self.is_enabled,
            'num_gates': self.num_gates,
            'is_homed': self.selector().is_homed,
            'print_state': self.print_state,
            'unit': self.unit_selected,
            'tool': self.tool_selected,
            'gate': self._next_gate if self._next_gate is not None else self.gate_selected,
            'active_filament': self.active_filament,
            'num_toolchanges': self.num_toolchanges,
            'last_tool': self._last_tool,
            'next_tool': self._next_tool,
            'toolchange_purge_volume': self.toolchange_purge_volume,
            'last_toolchange': self._last_toolchange,
            'operation': self.saved_toolhead_operation,
            'filament': "Loaded" if self.filament_pos == FILAMENT_POS_LOADED else
                        "Unloaded" if self.filament_pos == FILAMENT_POS_UNLOADED else
                        "Unknown",
            'filament_position': self.mmu_toolhead().get_position()[1],
            'filament_pos': self.filament_pos, # State machine position
            'filament_direction': self.filament_direction,
            'pending_spool_id': self.pending_spool_id,
            'ttg_map': self.ttg_map,
            'endless_spool_groups': self.endless_spool_groups,
            'gate_status': self.gate_status,
            'gate_filament_name': self.gate_filament_name,
            'gate_material': self.gate_material,
            'gate_color': self.gate_color,
            'gate_temperature': self.gate_temperature,
            'gate_spool_id': self.gate_spool_id,
            'gate_speed_override': self.gate_speed_override,
            'gate_color_rgb': self.gate_color_rgb,
            'slicer_color_rgb': self.slicer_color_rgb,
            'tool_extrusion_multipliers': self.tool_extrusion_multipliers,
            'tool_speed_multipliers': self.tool_speed_multipliers,
            'slicer_tool_map': self.slicer_tool_map,
            'action': self._get_action_string(),
            'sync_drive': self.mmu_toolhead().is_synced(),
            'reason_for_pause': self.reason_for_pause if self.is_mmu_paused() else "",
            'extruder_filament_remaining': self.filament_remaining + self.p.toolhead_residual_filament,
            'spoolman_support': self.p.spoolman_support,
            'bowden_progress': self._get_bowden_progress(), # Simple 0-100%. -1 if not performing bowden move
            'espooler_active': self.espooler.get_operation(self.gate_selected)[0] if self.has_espooler() else '',
            'endless_spool_enabled': self.endless_spool_enabled,
            'print_start_detection': self.p.print_start_detection, # For Klippain. Not really sure it is necessary

            # DEPRECATED but still used or likely to be used
            'runout': self.is_handling_runout, # DEPRECATED but still used in HH macros (better to use operation)
            'is_paused': self.is_mmu_paused(), # DEPRECATED (better to use print_state)

            # PAUL I think these are ok to delete but check/fix Klipperscreen
            'is_locked': self.is_mmu_paused(), # DEPRECATED (alias for is_paused)
            'is_in_print': self.is_in_print(), # DEPRECATED (use print_state)
            'endless_spool': self.endless_spool_enabled,           # DEPRECATED PAUL check UI need
            'has_bypass': self.selector().has_bypass(), # TODO deprecate because this is a per unit selector bypass
            'clog_detection': False,           # DEPRECATED PAUL check UI need
            'clog_detection_enabled': False,   # DEPRECATED PAUL check UI need
        }

        status['sensors'] = self.sensor_manager.get_status(eventtime)
        status.update(self.environment_manager.get_status(eventtime))

        if self.has_espooler():
            status.update(self.espooler().get_status(eventtime))

        if self.has_encoder():
            status['encoder'] = self.encoder().get_status(eventtime)

        return status

    def _reset_statistics(self):
        self.statistics = {}
        self.last_statistics = {}
        self.track = {}
        self.gate_statistics = []
        for _ in range(self.num_gates):
            self.gate_statistics.append(dict(EMPTY_GATE_STATS_ENTRY))
        self._reset_job_statistics()

    def _reset_job_statistics(self):
        self.job_statistics = {}

    def _track_time_start(self, name):
        self.track[name] = self.toolhead.get_last_move_time()

    def _track_time_end(self, name):
        if name not in self.track:
            return # Timer not initialized
        self.statistics.setdefault(name, 0)
        self.job_statistics.setdefault(name, 0)
        elapsed = self.toolhead.get_last_move_time() - self.track[name]
        self.statistics[name] += elapsed
        self.job_statistics[name] += elapsed
        self.last_statistics[name] = elapsed

    @contextlib.contextmanager
    def _wrap_track_time(self, name):
        self._track_time_start(name)
        try:
            yield self
        finally:
            self._track_time_end(name)

    def _track_swap_completed(self):
        self.statistics.setdefault('total_swaps', 0)
        self.job_statistics.setdefault('total_swaps', 0)
        self.statistics.setdefault('swaps_since_pause', 0)
        self.statistics.setdefault('swaps_since_pause_record', 0)

        self.statistics['swaps_since_pause'] += 1
        self.statistics['swaps_since_pause_record'] = max(self.statistics['swaps_since_pause_record'], self.statistics['swaps_since_pause'])
        self.statistics['total_swaps'] += 1
        self.job_statistics['total_swaps'] += 1

    def _track_pause_start(self):
        self.statistics.setdefault('total_pauses', 0)
        self.job_statistics.setdefault('total_pauses', 0)

        self.statistics['total_pauses'] += 1
        self.job_statistics['total_pauses'] += 1
        self.statistics['swaps_since_pause'] = 0

        self._track_time_start('pause')
        self._track_gate_statistics('pauses', self.gate_selected)

    def _track_pause_end(self):
        self._track_time_end('pause')

    # Per gate tracking
    def _track_gate_statistics(self, key, gate, count=1):
        try:
            if gate >= 0:
                if isinstance(count, float):
                    self.gate_statistics[gate][key] = round(self.gate_statistics[gate][key] + count, 3)
                else:
                    self.gate_statistics[gate][key] += count
        except Exception as e:
            self.log_debug("Exception whilst tracking gate stats: %s" % str(e))

    def _seconds_to_short_string(self, seconds):
        if isinstance(seconds, (float, int)) or seconds.isnumeric():
            s = int(seconds)
            h = s // 3600
            m = (s // 60) % 60
            ms = int(round((seconds * 1000) % 1000, 0))
            s = s % 60

            if h > 0:
                return "{hour}:{min:0>2}:{sec:0>2}".format(hour=h, min=m, sec=s)
            if m > 0:
                return "{min}:{sec:0>2}".format(min=m, sec=s)
            if s >= 10:
                return "{sec}.{tenths}".format(sec=s, tenths=int(round(ms / 100, 0)))
            return "{sec}.{hundreds:0>2}".format(sec=s, hundreds=int(round(ms / 10, 0)))
        return seconds

    def _seconds_to_string(self, seconds):
        result = ""
        hours = int(math.floor(seconds / 3600.))
        if hours >= 1:
            result += "%d hours " % hours
        minutes = int(math.floor(seconds / 60.) % 60)
        if hours >= 1 or minutes >= 1:
            result += "%d minutes " % minutes
        result += "%d seconds" % int((math.floor(seconds) % 60))
        return result

    def _swap_statistics_to_string(self, total=True, detail=False):
        #
        # +-----------+---------------------+----------------------+----------+
        # |  114(46)  |      unloading      |       loading        | complete |
        # |   swaps   | pre  |   -   | post | pre  |   -   | post  |   swap   |
        # +-----------+------+-------+------+------+-------+-------+----------+
        # |   total   | 0:07 | 47:19 | 0:00 | 0:01 | 37:11 | 33:39 |  2:00:38 |
        # |     - avg | 0:00 |  0:24 | 0:00 | 0:00 |  0:19 |  0:17 |     1:03 |
        # | this job  | 0:00 | 10:27 | 0:00 | 0:00 |  8:29 |  8:30 |    28:02 |
        # |     - avg | 0:00 |  0:13 | 0:00 | 0:00 |  0:11 |  0:11 |     0:36 |
        # |      last | 0:00 |  0:12 | 0:00 | 0:00 |  0:10 |  0:14 |     0:39 |
        # +-----------+------+-------+------+------+-------+-------+----------+
        # Time spent paused: ...
        #
        msg = "MMU Statistics:\n"
        lifetime = self.statistics
        job = self.job_statistics
        last = self.last_statistics
        total = self.p.console_always_output_full or total or not self.is_in_print()

        table_column_order = ['pre_unload', 'form_tip', 'unload', 'post_unload', 'pre_load', 'load', 'purge', 'post_load', 'total']
        table_include_columns = self._list_intersection(table_column_order, self.p.console_stat_columns if not detail else table_column_order) # To maintain the correct order and filter incorrect ones

        table_row_options = ['total', 'total_average', 'job', 'job_average', 'last']
        table_include_rows = self._list_intersection(self.p.console_stat_rows, table_row_options) # Keep the user provided order

        # Remove totals from table if not in print and not forcing total
        if not self.p.console_always_output_full and not total:
            if 'total'         in table_include_rows: table_include_rows.remove('total')
            if 'total_average' in table_include_rows: table_include_rows.remove('total_average')
        if not self.is_in_print():
            if 'job'           in table_include_rows: table_include_rows.remove('job')
            if 'job_average'   in table_include_rows: table_include_rows.remove('job_average')

        if len(table_include_rows) > 0:
            # Map the row names (as described in macro_vars) to the proper values. stats is mandatory
            table_rows_map = {
                'total':         {'stats': lifetime, 'name': 'total '},
                'total_average': {'stats': lifetime, 'name': UI_CASCADE + ' avg', 'devide': lifetime.get('total_swaps', 1)},
                'job':           {'stats': job,      'name': 'this job '},
                'job_average':   {'stats': job,      'name': UI_CASCADE + ' avg', 'devide': job.get('total_swaps', 1)},
                'last':          {'stats': last,     'name': 'last'}
            }
            # Map the saved timing values to proper column titles
            table_headers_map = {
                'pre_unload': 'pre',
                'form_tip': 'tip',
                'unload': '-',
                'post_unload': 'post',
                'pre_load': 'pre',
                'load': '-',
                'purge': 'purge',
                'post_load': 'post',
                'total': 'swap'
            }
            # Group the top headers map. Omit the first column, because that'll be filled with the nr. of swaps
            table_extra_headers_map = {
                'unloading': ['pre_unload', 'form_tip', 'unload', 'post_unload'],
                'loading': ['pre_load', 'load', 'purge', 'post_load'],
                'complete': ['total']
            }
            # Extract the table headers that will be used
            table_headers = [table_headers_map[key] for key in table_include_columns]
            # Insert the first column. This is normally empty but will sit below the number of swaps
            table_headers.insert(0, 'swaps')

            # Filter out the top (group) headers ( If none of the unload columns are present, unloading can be removed)
            table_extra_headers = [key for key, values in table_extra_headers_map.items() if self._list_intersection(values, table_include_columns)]

            # Dictionary keys have no predefined order, so re-order them (Lucky the columns are alphabetical)
            table_extra_headers.sort(reverse=True)
            # Include the number of swaps in the top-left corner of the table
            if self.is_in_print():
                if total:
                    table_extra_headers.insert(0, '%d(%d)' % (lifetime.get('total_swaps', 0), job.get('total_swaps', 0)))
                else:
                    table_extra_headers.insert(0, '%d' % (job.get('total_swaps', 0)))
            else:
                table_extra_headers.insert(0, '%d' % (lifetime.get('total_swaps', 0)))

            # Build the table and populate with times
            table = []
            for row in table_include_rows:
                name = table_rows_map[row].get('name', row)
                stats = table_rows_map[row]['stats']
                devide = max(1, table_rows_map[row].get('devide', 1))
                table.append([name])
                table[-1].extend(["-" if key not in stats else self._seconds_to_short_string(stats.get(key, 0) / devide) for key in table_include_columns])

            # Calculate the needed column widths (The +2 is for a margin on both ends)
            column_extra_header_widths = [len(table_extra_header) + 2 for table_extra_header in table_extra_headers]
            column_widths =              [max(len(table_headers[c]), max(len(row[c]) for row in table)) + 2 for c in range(len(table_include_columns) + 1) ]

            # If an 'extra_header' is wider then the sum of the columns beneath it, widen up those columns
            for i, w in enumerate(column_extra_header_widths):
                start = sum(max(1, len(self._list_intersection(table_extra_headers_map.get(table_extra_header, ['']), table_include_columns)))
                    for table_extra_header in table_extra_headers[0:i])
                end = start + max(1, len(self._list_intersection(table_extra_headers_map.get(table_extra_headers[i], ['']), table_include_columns)))
                while (sum(column_widths[start:end]) + (end - start - 1)) < w:
                    for c in range(start, end):
                        column_widths[c] += 1
                column_extra_header_widths[i] = sum(column_widths[start:end]) + (end - start - 1)

            # Build the table header
            msg += UI_BOX_TL + UI_BOX_T.join([UI_BOX_H * width for width in column_extra_header_widths]) + UI_BOX_TR + "\n"
            msg += UI_BOX_V  + UI_BOX_V.join([table_extra_headers[i].center(column_extra_header_widths[i], UI_SEPARATOR)
                for i in range(len(column_extra_header_widths))]) + UI_BOX_V + "\n"
            msg += UI_BOX_V  + UI_BOX_V.join([table_headers[i].center(column_widths[i], UI_SEPARATOR)
                for i in range(len(column_widths))]) + UI_BOX_V + "\n"
            msg += UI_BOX_L  + UI_BOX_M.join([UI_BOX_H * (width) for width in column_widths]) + UI_BOX_R + "\n"

            # Build the table body
            for row in table:
                msg += UI_BOX_V + UI_BOX_V.join([row[i].rjust(column_widths[i] - 1, UI_SEPARATOR) + UI_SEPARATOR
                    for i in range(len(column_widths))]) + UI_BOX_V + "\n"

            # Table footer
            msg += UI_BOX_BL    + UI_BOX_B.join([UI_BOX_H * width for width in column_widths]) + UI_BOX_BR + "\n"

        # Pause data
        if total:
            msg += "\n%s spent paused over %d pauses (All time)" % (self._seconds_to_short_string(lifetime.get('pause', 0)), lifetime.get('total_pauses', 0))
        if self.is_in_print():
            msg += "\n%s spent paused over %d pauses (This job)" % (self._seconds_to_short_string(job.get('pause', 0)), job.get('total_pauses', 0))
            if self.slicer_tool_map['total_toolchanges'] is not None:
                msg += "\n%d / %d toolchanges" % (self.num_toolchanges, self.slicer_tool_map['total_toolchanges'])
            else:
                msg += "\n%d toolchanges" % self.num_toolchanges
        msg += "\nNumber of swaps since last incident: %d (Record: %d)" % (lifetime.get('swaps_since_pause', 0), lifetime.get('swaps_since_pause_record', 0))

        return msg

    def _list_intersection(self, list1, list2):
        result = []
        for item in list1:
            if item in list2:
                result.append(item)
        return result

    def _dump_statistics(self, force_log=False, total=False, job=False, gate=False, detail=False, showcounts=False):
        msg = ""
        if self.p.log_statistics or force_log:
            if job or total:
                msg += self._swap_statistics_to_string(total=total, detail=detail)
            if self._can_use_encoder() and gate:
                m,d = self._gate_statistics_to_string()
                msg += "\n\n" if msg != "" else ""
                msg += m
                if detail:
                    msg += "\n" if msg != "" else ""
                    msg += d

        if showcounts and self.counters:
            if msg:
                msg += "\n\n"
            msg += "Consumption counters:\n"
            for counter, metric in self.counters.items():
                if metric['limit'] >= 0 and metric['count'] > metric['limit']:
                    msg += "Count %s: %d (above limit %d), Warning: %s" % (counter, metric['count'], metric['limit'], metric.get('warning', ""))
                elif metric['limit'] >= 0:
                    msg += "Count %s: %d (limit %d%s)\n" % (counter, metric['count'], metric['limit'], ", will pause" if metric.get('pause', False) else "")
                else:
                    msg += "Count %s: %d\n" % (counter, metric['count'])

        if msg:
            self.log_always(msg)

    def _gate_statistics_to_string(self):
        msg = "Gate Statistics:\n"
        dbg = ""
        t = self.p.console_gate_stat
        for gate in range(self.num_gates):
            #rounded = {k:round(v,1) if isinstance(v,float) else v for k,v in self.gate_statistics[gate].items()}
            rounded = self.gate_statistics[gate]
            load_slip_percent = (rounded['load_delta'] / rounded['load_distance']) * 100 if rounded['load_distance'] != 0. else 0.
            unload_slip_percent = (rounded['unload_delta'] / rounded['unload_distance']) * 100 if rounded['unload_distance'] != 0. else 0.
            quality = rounded['quality']
            # Give the gate a reliability grading based on "quality" which is based on slippage
            if t == 'percentage':
                status = '%s%%' % min(100, round(quality * 100, 1)) if quality >= 0 else "n/a"
            elif quality < 0:
                status = UI_EMOTICONS[0] if t == 'emoticon' else "n/a"
            elif quality >= 0.985:
                status = UI_EMOTICONS[1] if t == 'emoticon' else "Perfect"
            elif quality >= 0.965:
                status = UI_EMOTICONS[2] if t == 'emoticon' else "Great"
            elif quality >= 0.95:
                status = UI_EMOTICONS[3] if t == 'emoticon' else "Good"
            elif quality >= 0.925:
                status = UI_EMOTICONS[4] if t == 'emoticon' else "Marginal"
            elif quality >= 0.90:
                status = UI_EMOTICONS[5] if t == 'emoticon' else "Degraded"
            elif quality >= 0.85:
                status = UI_EMOTICONS[6] if t == 'emoticon' else "Poor"
            else:
                status = UI_EMOTICONS[7] if t == 'emoticon' else "Terrible"
            msg += "%d:%s" % (gate, status)
            msg += ", " if gate < (self.num_gates - 1) else ""
            dbg += "\nGate %d: " % gate
            dbg += "Load: (monitored: %.1fmm slippage: %.1f%%)" % (rounded['load_distance'], load_slip_percent)
            dbg += "; Unload: (monitored: %.1fmm slippage: %.1f%%)" % (rounded['unload_distance'], unload_slip_percent)
            dbg += "; Failures: (load: %d unload: %d pauses: %d)" % (rounded['load_failures'], rounded['unload_failures'], rounded['pauses'])
            dbg += "; Quality: %.1f%%" % ((rounded['quality'] * 100.) if rounded['quality'] >= 0. else 0.)
        return msg, dbg

    def _persist_gate_statistics(self):
        for gate in range(self.num_gates):
            mmu_unit = self.mmu_unit(gate)
            adj_gate = gate - mmu_unit.first_gate
            self.var_manager.set("%s%d" % (VARS_MMU_GATE_STATISTICS_PREFIX, adj_gate), self.gate_statistics[gate], namespace=mmu_unit.name)

# PAUL old logic
#        # Also a good place to persist current clog length
#        if self.has_encoder():
#            self.calibration_manager.save_clog_detection_length(round(self.encoder().get_clog_detection_length(), 1), force=False)

        # Also a good place to update the persisted calibrated clog length (for auto mode)
        if self.has_encoder():
            mode = self.mmu_unit().sync_feedback.p.flowguard_encoder_mode
            if mode == RUNOUT_AUTOMATIC:
                cdl = self.encoder().get_clog_detection_length()
                self.calibration_manager.update_clog_detection_length(round(cdl, 1))

        self.var_manager.write()

    def _persist_swap_statistics(self):
        self.statistics = {key: round(value, 2) if isinstance(value, float) else value for key, value in self.statistics.items()}
        self.var_manager.set(VARS_MMU_SWAP_STATISTICS, self.statistics, write=True)

    def _persist_counters(self):
        self.var_manager.set(VARS_MMU_COUNTERS, self.counters, write=True)


    def format_help(self, msg, supplement=None):
        """
        Format a help message and optional supplement into a nicely aligned block.

        The input `msg` is expected to be multi-line with the first line containing
        either "command: description" or just a single heading line. Subsequent
        lines may contain parameter definitions in the form "name = value".

        This function:
          - Keeps the heading (and highlights the command using UI markers "{5}" / "{6}").
          - Aligns parameter names into a column (minimum width 10).
          - Prefixes parameter lines with a cascade/UI marker using `UI_CASCADE`.
          - Uses `UI_SPACE` as the fill character when padding parameter names.
          - Optionally appends a supplement block (if provided) wrapped with UI markers.

        Args:
            msg: The main help message (multi-line).
            supplement: Optional supplemental text (multi-line) appended after the main block.

        Returns:
            The formatted help string.
        """
        if not msg:
            return ""

        lines = msg.splitlines()
        if not lines:
            return msg

        # Format the heading (first line). If the heading contains ":", split into
        # command and description and wrap the command in UI markers.
        first_line = lines[0].rstrip()
        if ":" in first_line:
            cmd, helpstr = first_line.split(":", 1)
            formatted_help = "{5}" + cmd.strip() + "{6} : " + helpstr.strip()
        else:
            formatted_help = first_line

        # Compute parameter name column width: minimum 10, else longest name+1.
        param_lines = [ln for ln in lines[1:] if "=" in ln]
        def param_name_length(ln):
            name = ln.split("=", 1)[0].strip()
            return len(name) + 1

        param_width = max(10, max((param_name_length(ln) for ln in param_lines), default=0))

        # Build formatted parameter lines
        formatted_params: list[str] = []
        for ln in lines[1:]:
            if "=" in ln:
                key, value = ln.split("=", 1)
                key_str = key.strip()
                value_str = value.strip()
                padded_key = key_str.ljust(param_width, UI_SPACE)
                padded = f"{padded_key}= {value_str}"
                formatted_line = f"{{4}}{UI_CASCADE} {padded}{{0}}"
            else:
                formatted_line = f"{{4}}{UI_CASCADE} {ln.rstrip()}{{0}}"
            formatted_params.append(formatted_line)

        # Handle supplement if provided
        formatted_supplement = ""
        if supplement is not None:
            supp_lines = supplement.splitlines()
            if supp_lines:
                first = supp_lines[0].strip()
                formatted_supplement = "{3}{5}" + first + "{6}"
                if len(supp_lines) > 1:
                    formatted_supplement += "\n" + "\n".join(line.rstrip() for line in supp_lines[1:])
                formatted_supplement += "{0}"

        main_block = "\n".join([formatted_help] + formatted_params) if formatted_params else formatted_help
        return main_block + (("\n" + formatted_supplement) if formatted_supplement else "")


    def _color_message(self, msg):
        try:
            html_msg = msg.format(
                '</span>',                       # {0} COLOR OFF
                '<span style="color:#C0C0C0">',  # {1} COLOR GREY
                '<span style="color:#FF69B4">',  # {2} COLOR RED
                '<span style="color:#90EE90">',  # {3} COLOR GREEN
                '<span style="color:#87CEEB">',  # {4} COLOR CYAN
                '<b>',                           # {5} BOLD ON
                '</b>'                           # {6} BOLD OFF
            )
        except (IndexError, KeyError, ValueError) as e:
            html_msg = msg

        msg = re.sub(r'\{\d\}', '', msg) # Remove numbered placeholders for plain msg
        if self.p.serious:
            html_msg = msg
        return html_msg, msg

    def log_to_file(self, msg, prefix='> '):
        msg = "%s%s" % (prefix, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)

    def log_assertion(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)

        # Capture stack trace (exclude this frame for cleaner output)
        stack = "".join(traceback.format_stack()[:-1])

        if self.mmu_logger:
            self.mmu_logger.log(msg)
            self.mmu_logger.log("Stack trace:\n%s" % stack)

        self.gcode.respond_raw("!! Happy Hare Assertion: %s" % html_msg)

    def log_error(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)
        self.gcode.respond_raw("!! %s" % html_msg)

    def log_warning(self, msg):
        self.log_always("{2}%s{0}" % msg, color=True)

    def log_always(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)
        self.gcode.respond_info(html_msg)

    def log_info(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger and self.p.log_file_level > 0:
            self.mmu_logger.log(msg)
        if self.p.log_level > 0:
            self.gcode.respond_info(html_msg)

    def log_debug(self, msg):
        msg = "%s DEBUG: %s" % (UI_SEPARATOR, msg)
        if self.mmu_logger and self.p.log_file_level > 1:
            self.mmu_logger.log(msg)
        if self.p.log_level > 1:
            self.gcode.respond_info(msg)

    def log_trace(self, msg):
        msg = "%s %s TRACE: %s" % (UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu_logger and self.p.log_file_level > 2:
            self.mmu_logger.log(msg)
        if self.p.log_level > 2:
            self.gcode.respond_info(msg)

    def log_stepper(self, msg):
        msg = "%s %s %s STEPPER: %s" % (UI_SEPARATOR, UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu_logger and self.p.log_file_level > 3:
            self.mmu_logger.log(msg)
        if self.p.log_level > 3:
            self.gcode.respond_info(msg)

    def log_enabled(self, level):
        return (self.mmu_logger and self.p.log_file_level >= level) or self.p.log_level >= level

    # Fun visual display of MMU state
    def _display_visual_state(self, silent=False):
        if not silent and self.p.log_visual and not self.calibrating:
            visual_str = self._state_to_string()
            self.log_always(visual_str, color=True)

    def _state_to_string(self, direction=None):
        arrow = "<" if self.filament_direction == DIRECTION_UNLOAD else ">"
        space = "."
        home  = "|"
        gs = "(g)" # SENSOR_GATE or SENSOR_GEAR_PREFIX
        es = "(e)" # SENSOR_EXTRUDER
        ts = "(t)" # SENSOR_TOOLHEAD
        past  = lambda pos: arrow if self.filament_pos >= pos else space
        homed = lambda pos, sensor: (' ',arrow,sensor) if self.filament_pos > pos else (home,space,sensor) if self.filament_pos == pos else (' ',space,sensor)
        trig  = lambda name, sensor: re.sub(r'[a-zA-Z]', '*', name) if self.sensor_manager.check_sensor(sensor) else name

        t_str   = ("[T%s] " % str(self.tool_selected)) if self.tool_selected >= 0 else "BYPASS " if self.tool_selected == TOOL_GATE_BYPASS else "[T?] "
        g_str   = "{}".format(past(FILAMENT_POS_UNLOADED))
        lg_str  = "{0}{0}".format(past(FILAMENT_POS_HOMED_GATE)) if not self.mmu_unit().require_bowden_move else ""
        gs_str  = "{0}{2} {1}{1}".format(*homed(FILAMENT_POS_HOMED_GATE, trig(gs, self.mmu_unit().p.gate_homing_endstop))) if self.mmu_unit().p.gate_homing_endstop in [SENSOR_GATE, SENSOR_GEAR_PREFIX, SENSOR_EXTRUDER_ENTRY] else ""
        en_str  = " En {0}".format(past(FILAMENT_POS_IN_BOWDEN if self.mmu_unit().p.gate_homing_endstop in [SENSOR_GATE, SENSOR_GEAR_PREFIX, SENSOR_EXTRUDER_ENTRY] else FILAMENT_POS_START_BOWDEN)) if self.has_encoder() else ""
        bowden1 = "{0}{0}{0}{0}".format(past(FILAMENT_POS_IN_BOWDEN)) if self.mmu_unit().require_bowden_move else ""
        bowden2 = "{0}{0}{0}{0}".format(past(FILAMENT_POS_END_BOWDEN)) if self.mmu_unit().require_bowden_move else ""
        es_str  = "{0}{2} {1}{1}".format(*homed(FILAMENT_POS_HOMED_ENTRY, trig(es, SENSOR_EXTRUDER_ENTRY))) if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY) and self.mmu_unit().require_bowden_move else ""
        ex_str  = "{0}[{2} {1}{1}".format(*homed(FILAMENT_POS_HOMED_EXTRUDER, "Ex"))
        ts_str  = "{0}{2} {1}".format(*homed(FILAMENT_POS_HOMED_TS, trig(ts, SENSOR_TOOLHEAD))) if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD) else ""
        nz_str  = "{} Nz]".format(past(FILAMENT_POS_LOADED))
        summary = " {5}{4}LOADED{0}{6}" if self.filament_pos == FILAMENT_POS_LOADED else " {5}{4}UNLOADED{0}{6}" if self.filament_pos == FILAMENT_POS_UNLOADED else " {5}{2}UNKNOWN{0}{6}" if self.filament_pos == FILAMENT_POS_UNKNOWN else ""
        counter = " {5}%.1fmm{6}%s" % (self._get_filament_position(), " {1}(e:%.1fmm){0}" % self.get_encoder_distance(dwell=None) if self.has_encoder() and self.mmu_unit().p.encoder_move_validation else "")
        visual = "".join((t_str, g_str, lg_str, gs_str, en_str, bowden1, bowden2, es_str, ex_str, ts_str, nz_str, summary, counter))
        return visual


### LOGGING AND STATISTICS FUNCTIONS GCODE FUNCTIONS #############################

    cmd_MMU_STATS_help = "Dump and optionally reset the MMU statistics"
    def cmd_MMU_STATS(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        counter = gcmd.get('COUNTER', None)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        total = bool(gcmd.get_int('TOTAL', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        showcounts = bool(gcmd.get_int('SHOWCOUNTS', 0, minval=0, maxval=1))

        if counter:
            counter = counter.strip()
            delete = bool(gcmd.get_int('DELETE', 0, minval=0, maxval=1))
            limit = gcmd.get_int('LIMIT', 0, minval=-1)
            incr = gcmd.get_int('INCR', 0, minval=1)
            quiet = True
            if delete:
                _ = self.counters.pop(counter, None)
            elif reset:
                if counter in self.counters:
                    self.counters[counter]['count'] = 0
            elif not limit == 0:
                if counter not in self.counters:
                    self.counters[counter] = {'count': 0}
                warning = gcmd.get('WARNING', self.counters[counter].get('warning', ""))
                pause = bool(gcmd.get_int('PAUSE', self.counters[counter].get('pause', 0), minval=0, maxval=1))
                self.counters[counter].update({'limit': limit, 'warning': warning, 'pause': pause})
            elif incr:
                if counter in self.counters:
                    metric = self.counters[counter]
                    metric['count'] += incr
                    if metric['limit'] >= 0 and metric['count'] > metric['limit']:
                        warn = "Warning: %s" % metric.get('warning', "")
                        msg = "Count %s (%d) above limit %d" % (counter, metric['count'], metric['limit'])
                        msg += "\nUse 'MMU_STATS COUNTER=%s RESET=1' to reset" % counter
                        if metric.get('pause', False):
                            self.handle_mmu_error("%s\n%s" % (warn, msg))
                        else:
                            self.log_error(warn)
                            self.log_always(msg)
                else:
                    self.counters[counter] = {'count': 0, 'limit': -1, 'warning': ""}
            self._persist_counters()
        elif reset:
            self._reset_statistics()
            self._persist_swap_statistics()
            self._persist_gate_statistics()
            if not quiet:
                self._dump_statistics(force_log=True, total=True)
            return

        if not quiet:
            self._dump_statistics(force_log=True, total=total or detail, job=True, gate=True, detail=detail, showcounts=showcounts)

    cmd_MMU_STATUS_help = "Complete dump of current MMU state and important configuration"
    def cmd_MMU_STATUS(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)
        on_off = lambda x: "ON" if x else "OFF"

        msg = "MMU: Happy Hare %s controlling %d units:" % (self._fversion(self.mmu_machine.happy_hare_version), self.mmu_machine.num_units)
        msg += (" (DISABLED)" if not self.is_enabled else " (PAUSED)" if self.is_mmu_paused() else "")
        for i in range(self.mmu_machine.num_units):
            unit = self.mmu_machine.get_mmu_unit_by_index(i)
            if unit.mmu_vendor != unit.display_name:
                msg += "\n+ '%s' a %s v%s" % (unit.display_name, unit.mmu_vendor, unit.mmu_version_string)
            else:
                msg += "\n+ %s v%s" % (unit.mmu_vendor, unit.mmu_version_string)
            msg += " (gates %d-%d)" % (unit.first_gate, unit.first_gate + unit.num_gates)
            msg += "\n%s %s" % (UI_CASCADE, self.selector().get_mmu_status_config())
            if self.has_encoder():
                msg += ". Encoder reads %.1fmm" % self.get_encoder_distance()

        msg += "\nPrint state is %s" % self.print_state.upper()
        msg += ". Tool %s selected on gate %s%s" % (self.selected_tool_string(), self.selected_gate_string(), self.selected_unit_string())
        msg += ". Toolhead position saved" if self.saved_toolhead_operation else ""
        msg += "\nMMU gear stepper at %d%% current and is %s to extruder" % (self.gear_percentage_run_current, "SYNCED" if self.mmu_toolhead().is_gear_synced_to_extruder() else "not synced")
        if self._standalone_sync:
            msg += ". Standalone sync mode is ENABLED"
        if self.mmu_unit().sync_feedback.is_enabled():
            msg += "\nSync feedback indicates filament in bowden is: %s" % self.mmu_unit().sync_feedback.get_sync_feedback_string(detail=True).upper()
            if not self.mmu_unit().sync_feedback.is_active():
                msg += " (not currently active)"
        else:
            msg += "\nSync feedback is disabled"

        if config:
            self.calibrated_bowden_length = self.calibration_manager.get_bowden_length() # Temp scalar pulled from list for _f_calc()
            msg += "\n\nLOAD SEQUENCE:"

            # Gate loading
            msg += "\n- Filament loads into gate by homing a maximum of %s to %s" % (self._f_calc("gate_homing_max"), self._gate_homing_string())

            # Bowden loading
            if self.mmu_unit().require_bowden_move:
                if self._must_buffer_extruder_homing():
                    if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                        msg += "\n- Bowden is loaded with a fast%s %s move" % (" CORRECTED" if self.mmu_unit().p.bowden_apply_correction else "", self._f_calc("calibrated_bowden_length - toolhead_entry_to_extruder - extruder_homing_buffer"))
                    else:
                        msg += "\n- Bowden is loaded with a fast%s %s move" % (" CORRECTED" if self.mmu_unit().p.bowden_apply_correction else "", self._f_calc("calibrated_bowden_length - extruder_homing_buffer"))
                else:
                    msg += "\n- Bowden is loaded with a full fast%s %s move" % (" CORRECTED" if self.mmu_unit().p.bowden_apply_correction else "", self._f_calc("calibrated_bowden_length"))
            else:
                msg += "\n- No fast bowden move is required"

            # Extruder homing
            if self._must_home_to_extruder():
                if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_COLLISION:
                    msg += ", then homes a maximum of %s to extruder using COLLISION detection (at %d%% current)" % (self._f_calc("extruder_homing_max"), self.mmu_unit().p.extruder_collision_homing_current)
                elif self.mmu_unit().p.extruder_homing_endstop == SENSOR_GEAR_TOUCH:
                    msg += ", then homes a maxium of %s to extruder using 'touch' (stallguard) detection" % self._f_calc("extruder_homing_max")
                else:
                    msg += ", then homes a maximum of %s to %s sensor" % (self._f_calc("extruder_homing_max"), self.extruder_homing_endstop.upper())
                if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                    msg += " and then moves %s to extruder extrance" % self._f_calc("toolhead_entry_to_extruder")
            else:
                if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_NONE and not self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                    msg += ". WARNING: no extruder homing is performed - extruder loading cannot be precise"
                else:
                    msg += ", no extruder homing is necessary"

            # Extruder loading
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                msg += "\n- Extruder (synced) loads by homing a maximum of %s to TOOLHEAD sensor before moving the last %s to the nozzle" % (self._f_calc("toolhead_homing_max"), self._f_calc("toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining"))
            else:
                msg += "\n- Extruder (synced) loads by moving %s to the nozzle" % self._f_calc("toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining")

            # Purging
            if self.p.force_purge_standalone:
                if self.p.purge_macro:
                    msg += "\n- Purging is always managed by Happy Hare using '%s' macro with extruder purging current of %d%%" % (
                        self.p.purge_macro, self.p.extruder_purge_current)
                else:
                    msg += "\n- No purging is performed!"
            else:
                if self.p.purge_macro:
                    msg += "\n- Purging is managed by slicer when printing. Otherwise by Happy Hare using '%s' macro with extruder purging current of %d%% when not printing" % (
                        self.p.purge_macro, self.p.extruder_purge_current)
                else:
                    msg += "\n- Purging is managed by slicer only when printing"

            # Tightening
            has_tension = self.sensor_manager.has_sensor(SENSOR_TENSION)
            has_compression = self.sensor_manager.has_sensor(SENSOR_COMPRESSION)
            has_proportional = self.sensor_manager.has_sensor(SENSOR_PROPORTIONAL)
            if (
                self.mmu_unit().p.toolhead_post_load_tighten
                and not self.mmu_unit().p.sync_to_extruder
                and self._can_use_encoder()
                and self.mmu_unit().sync_feedback.p.flowguard_encoder_mode
            ):
                msg += "\n- Filament in bowden is tightened by %.1fmm (%d%% of clog detection length) at reduced gear current to prevent false clog detection" % (min(self.encoder().get_clog_detection_length() * self.mmu_unit().p.toolhead_post_load_tighten / 100, 15), self.mmu_unit().p.toolhead_post_load_tighten)
            elif (
                self.mmu_unit().p.toolhead_post_load_tension_adjust
                and (self.mmu_unit().p.sync_to_extruder or self.mmu_unit().p.sync_purge)
                and (has_tension or has_compression or has_proportional)
                and self.mmu_unit().sync_feedback.is_enabled()
            ):
                msg += "\n- Filament in bowden will be adjusted a maximum of %.1fmm to neutralize tension" % (self.mmu_unit().sync_feedback.p.sync_feedback_buffer_range or self.mmu_unit().sync_feedback.p.sync_feedback_buffer_maxrange)

            msg += "\n\nUNLOAD SEQUENCE:"

            # Tip forming
            if self.p.force_form_tip_standalone:
                if self.p.form_tip_macro:
                    msg += "\n- Tip is always formed by Happy Hare using '%s' macro after initial retract of %s with extruder current of %d%%" % (
                        self.p.form_tip_macro, self._f_calc("toolchange_retract"), self.p.extruder_form_tip_current)
                else:
                    msg += "\n- No tip forming is performed!"
            else:
                if self.p.form_tip_macro:
                    msg += "\n- Tip is formed by slicer when printing. Otherwise by Happy Hare using '%s' macro after initial retract of %s with extruder current of %d%%" % (
                        self.p.form_tip_macro, self._f_calc("toolchange_retract"), self.p.extruder_form_tip_current)
                else:
                    msg += "\n- Tip is formed by slicer only when printing"

            # Extruder unloading
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                msg += "\n- Extruder (synced) unloads by reverse homing a maximum of %s to EXTRUDER sensor" % self._f_calc("toolhead_entry_to_extruder + toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin")
            elif self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                msg += "\n- Extruder (optionally synced) unloads by reverse homing a maximum %s to TOOLHEAD sensor" % self._f_calc("toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin")
                msg += ", then unloads by moving %s to exit extruder" % self._f_calc("toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle + toolhead_unload_safety_margin")
            else:
                msg += "\n- Extruder (optionally synced) unloads by moving %s less tip-cutting reported park position to exit extruder" % self._f_calc("toolhead_extruder_to_nozzle + toolhead_unload_safety_margin")

            # Bowden unloading
            if self.mmu_unit().require_bowden_move:
                if self.has_encoder() and self.mmu_unit().p.bowden_pre_unload_test and not self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                    msg += "\n- Bowden is unloaded with a short %s validation move before %s fast move" % (self._f_calc("encoder_move_step_size"), self._f_calc("calibrated_bowden_length - gate_unload_buffer - encoder_move_step_size"))
                else:
                    msg += "\n- Bowden is unloaded with a fast %s move" % self._f_calc("calibrated_bowden_length - gate_unload_buffer")
            else:
                msg += "\n- No fast bowden move is required"

            # Gate parking
            msg += "\n- Filament is stored by homing a maximum of %s to %s and parking %s in the gate\n" % (self._f_calc("gate_homing_max"), self._gate_homing_string(), self._f_calc("gate_parking_distance"))

            if self.mmu_unit().p.sync_form_tip or self.mmu_unit().p.sync_purge or self.mmu_unit().p.sync_to_extruder:
                msg += "\nGear and Extruder steppers are synchronized during: "
                m = []
                if self.mmu_unit().p.sync_to_extruder:
                    m.append("Print (at %d%% current %s sync feedback)" % (self.mmu_unit().p.sync_gear_current, "with" if self.mmu_unit().sync_feedback.is_enabled() else "without"))
                if self.mmu_unit().p.sync_form_tip:
                    m.append("Tip forming")
                if self.mmu_unit().p.sync_purge:
                    m.append("Purging")
                msg += ", ".join(m)

            if hasattr(self.selector(), 'use_touch_move'):
                msg += "\nSelector touch (stallguard) is %s - blocked gate recovery %s possible" % (("ENABLED", "is") if self.selector().use_touch_move() else ("DISABLED", "is not"))
            if self.has_encoder():
                msg += "\nMMU has an encoder. Non essential move validation is %s" % ("ENABLED" if self._can_use_encoder() else "DISABLED")
                msg += "\nFlowGuard detection is %s" % ("AUTOMATIC" if self.mmu_unit().sync_feedback.p.flowguard_encoder_mode == RUNOUT_AUTOMATIC else "ENABLED" if self.mmu_unit().sync_feedback.p.flowguard_encoder_mode == RUNOUT_STATIC else "DISABLED")
                msg += " (%.1fmm runout)" % self.encoder().get_clog_detection_length()
                msg += ", EndlessSpool is %s" % ("ENABLED" if self.endless_spool_enabled else "DISABLED")
            else:
                msg += "\nMMU does not have an encoder - move validation or clog detection is not possible"
            msg += "\nSpoolMan is %s" % ("ENABLED (pulling gate map)" if self.p.spoolman_support == SPOOLMAN_PULL else "ENABLED (push gate map)" if self.p.spoolman_support == SPOOLMAN_PUSH else "ENABLED" if self.p.spoolman_support == SPOOLMAN_READONLY else "DISABLED")
            msg += "\nSensors: "
            sensors = self.sensor_manager.get_active_sensors(all_sensors=True)
            for name, state in sensors.items():
                msg += "%s (%s), " % (name.upper(), "Disabled" if state is None else ("Detected" if state is True else "Empty"))
            msg += "\nLogging: Console %d(%s)" % (self.p.log_level, LOG_LEVELS[self.p.log_level])

            msg += ", Logfile %d(%s)" % (self.p.log_file_level, LOG_LEVELS[self.p.log_file_level])
            msg += ", Visual %d(%s)" % (self.p.log_visual, on_off(self.p.log_visual))
            msg += ", Statistics %d(%s)" % (self.p.log_statistics, on_off(self.p.log_statistics))

        if not detail:
            msg += "\n\nFor details on TTG and EndlessSpool groups add 'DETAIL=1'"
            if not config:
                msg += ", for configuration add 'SHOWCONFIG=1'"

        msg += "\n\n%s" % self._mmu_visual_to_string()
        msg += "\n%s" % self._state_to_string()

        if detail:
            msg += "\n\n%s" % self._ttg_map_to_string()
            if self.endless_spool_enabled:
                msg += "\n\n%s" % self._es_groups_to_string()
            msg += "\n\n%s" % self._gate_map_to_string()

        self.log_always(msg, color=True)

        # Always warn if not fully calibrated or needs recovery
        self.report_necessary_recovery(use_autotune=False)

    def _f_calc(self, formula):
        format_var = lambda p: p + ':' + "%.1f" % vars(self).get(p.lower())
        terms = re.split('(\+|\-)', formula)
        result = eval(formula, {}, vars(self))
        formatted_formula = "%.1fmm (" % result
        for term in terms:
            term = term.strip()
            if term in ('+', '-'):
                formatted_formula += " " + term + " "
            elif len(terms) > 1:
                formatted_formula += format_var(term)
            else:
                formatted_formula += term
        formatted_formula += ")"
        return formatted_formula

    def _get_encoder_summary(self, detail=False):
        status = self.encoder().get_status(0)
        msg = "Encoder position: %.1f" % status['encoder_pos']
        if detail:
            msg += "\n- FlowGuard/Runout: %s" % ("Active" if status['enabled'] else "Inactive")
            clog = "Automatic" if status['detection_mode'] == 2 else "On" if status['detection_mode'] == 1 else "Off"
            msg += "\n- FlowGuard mode: %s (Detection length: %.1f)" % (clog, status['detection_length'])
            msg += "\n- Remaining headroom before trigger: %.1f (min: %.1f)" % (status['headroom'], status['min_headroom'])
            msg += "\n- Flowrate: %d %%" % status['flow_rate']
        return msg

    def motors_onoff(self, on=False, motor="all"):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        steppers = self.gear_rail().steppers if motor == "gears" else [self.gear_rail().steppers[0]] if self.gear_rail().steppers else []
        if on:
            if motor in ["all", "gear", "gears"]:
                for stepper in steppers:
                    se = stepper_enable.lookup_enable(stepper.get_name())
                    se.motor_enable(self.mmu_toolhead().get_last_move_time())
            if motor in ["all", "selector"]:
                self.mmu_machine.enable_motors()
                self.selector().restore_gate()
                self.selector().filament_hold_move() # Aka selector move position
        else:
            if motor in ["all", "gear", "gears"]:
                self.mmu_toolhead().unsync()
                for stepper in steppers:
                    se = stepper_enable.lookup_enable(stepper.get_name())
                    se.motor_disable(self.mmu_toolhead().get_last_move_time())
            if motor in ["all", "selector"]:
                self.selector().restore_gate(TOOL_GATE_UNKNOWN)
                self.mmu_machine.disable_motors()


### SERVO AND MOTOR GCODE FUNCTIONS ##############################################

    # This command will loose sync state
    cmd_MMU_MOTORS_OFF_help = "Turn off all MMU motors and servos"
    def cmd_MMU_MOTORS_OFF(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self.sync_gear_to_extruder(False, force_grip=True)
        self.motors_onoff(on=False)

    cmd_MMU_MOTORS_ON_help = "Turn on all MMU motors and servos"
    def cmd_MMU_MOTORS_ON(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self.motors_onoff(on=True)
        self.reset_sync_gear_to_extruder(False)

    cmd_MMU_TEST_BUZZ_MOTOR_help = "Simple buzz the selected motor (default gear) for setup testing"
    def cmd_MMU_TEST_BUZZ_MOTOR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        motor = gcmd.get('MOTOR', "gear")

        with self.wrap_sync_gear_to_extruder():
            if motor == "gear":
                found = self.buzz_gear_motor()
                if found is not None:
                    self.log_info("Filament %s by gear motor buzz" % ("detected" if found else "not detected"))
            elif motor == "gears":
                try:
                    for gate in range(self.num_gates):
                        self.mmu_toolhead().select_gear_stepper(gate)
                        found = self.buzz_gear_motor()
                        if found is not None:
                            self.log_info("Filament %s in gate %d by gear motor buzz" % ("detected" if found else "not detected", gate))
                finally:
                    self.mmu_toolhead().select_gear_stepper(self.gate_selected)
            elif not self.selector().buzz_motor(motor):
                raise gcmd.error("Motor '%s' not known" % motor)

    cmd_MMU_SYNC_GEAR_MOTOR_help = "Sync the MMU gear motor to the extruder stepper"
    cmd_MMU_SYNC_GEAR_MOTOR_param_help = (
        "MMU_SYNC_GEAR_MOTOR: %s\n" % cmd_MMU_SYNC_GEAR_MOTOR_help
        + "SYNC = [0|1] Specify whether to force extruder/mmu syncing out of a print"
        + "(no parameters will default SYNC=1)"
    )
    def cmd_MMU_SYNC_GEAR_MOTOR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.log_always(self.format_help(self.cmd_MMU_SYNC_GEAR_MOTOR_param_help), color=True)
            return

        if self.check_if_bypass(): return
        if self.check_if_not_homed(): return
        sync = gcmd.get_int('SYNC', 1, minval=0, maxval=1)

        if not sync and self.check_if_always_gripped(): return

        if not self.is_in_print() and self._standalone_sync != sync:
            self._standalone_sync = sync # Make sticky if not in a print
            if self._standalone_sync:
                self.log_info("MMU gear stepper will be synced with extruder whenever filament is in extruder")
            else:
                self.log_info("MMU gear stepper is unsynced from extruder")

        if sync and self.filament_pos < FILAMENT_POS_EXTRUDER_ENTRY:
            self.log_warning("Will temporarily sync, but filament position does not indicate in extruder!\nUse 'MMU_RECOVER' to correct the filament position.")

        self.reset_sync_gear_to_extruder(sync, force_grip=True, skip_extruder_check=True)


#########################
# CALIBRATION FUNCTIONS #
#########################

    def _sample_stats(self, values):
        mean = stdev = vmin = vmax = 0.
        if values:
            mean = sum(values) / len(values)
            diff2 = [( v - mean )**2 for v in values]
            stdev = math.sqrt( sum(diff2) / max((len(values) - 1), 1))
            vmin = min(values)
            vmax = max(values)
        return {'mean': mean, 'stdev': stdev, 'min': vmin, 'max': vmax, 'range': vmax - vmin}

    # Filament is assumed to be at the extruder and will be at extruder again when complete
    def _probe_toolhead(self, cold_temp=70, probe_depth=100, sensor_homing=80):
        # Ensure extruder is COLD
        self.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % self.extruder_name)
        current_temp = self.printer.lookup_object(self.extruder_name).get_status(0)['temperature']
        if current_temp > cold_temp:
            self.log_always("Waiting for extruder to cool")
            self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=%s MINIMUM=0 MAXIMUM=%d" % (self.extruder_name, cold_temp))

        # Enable the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
        ge = stepper_enable.lookup_enable(extruder_stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        # Reliably force filament to the nozzle
        self.selector().filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", self.mmu_unit().p.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
        if not fhomed:
            raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.mmu_unit().p.toolhead_homing_max)
        self.selector().filament_release()
        actual,_,_,_ = self.trace_filament_move("Forcing filament to nozzle", probe_depth, motor="extruder")

        # Measure 'toolhead_sensor_to_nozzle'
        self.selector().filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Reverse homing off toolhead sensor", -probe_depth, motor="gear+extruder", homing_move=-1, endstop_name=SENSOR_TOOLHEAD)
        if fhomed:
            toolhead_sensor_to_nozzle = -actual
            self.log_always("Measured toolhead_sensor_to_nozzle: %.1f" % toolhead_sensor_to_nozzle)
        else:
            raise MmuError("Failed to reverse home to toolhead sensor")

        # Move to extruder extrance again
        self.selector().filament_release()
        actual,_,_,_ = self.trace_filament_move("Moving to extruder entrance", -(probe_depth - toolhead_sensor_to_nozzle), motor="extruder")

        # Measure 'toolhead_extruder_to_nozzle'
        self.selector().filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", self.mmu_unit().p.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
        if fhomed:
            toolhead_extruder_to_nozzle = actual + toolhead_sensor_to_nozzle
            self.log_always("Measured toolhead_extruder_to_nozzle: %.1f" % toolhead_extruder_to_nozzle)
        else:
            raise MmuError("Failed to home to toolhead sensor")

        toolhead_entry_to_extruder = 0.
        if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
            # Retract clear of extruder sensor and then home in "extrude" direction
            actual,fhomed,_,_ = self.trace_filament_move("Reverse homing off extruder entry sensor", -(sensor_homing + toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle), motor="gear+extruder", homing_move=-1, endstop_name=SENSOR_EXTRUDER_ENTRY)
            actual,_,_,_ = self.trace_filament_move("Moving before extruder entry sensor", -20, motor="gear+extruder")
            actual,fhomed,_,_ = self.trace_filament_move("Homing to extruder entry sensor", 40, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_EXTRUDER_ENTRY)

            # Measure to toolhead sensor and thus derive 'toolhead_entry_to_extruder'
            if fhomed:
                actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", sensor_homing, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
                if fhomed:
                    toolhead_entry_to_extruder = actual - (toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle)
                    self.log_always("Measured toolhead_entry_to_extruder: %.1f" % toolhead_entry_to_extruder)
            else:
                raise MmuError("Failed to reverse home to toolhead sensor")

        # Unload and re-park filament
        self.selector().filament_release()
        actual,_,_,_ = self.trace_filament_move("Moving to extruder entrance", -sensor_homing, motor="extruder")

        return toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle, toolhead_entry_to_extruder


#######################
# MMU STATE FUNCTIONS #
#######################

    def _setup_hotend_off_timer(self):
        self.hotend_off_timer = self.reactor.register_timer(self._hotend_off_handler, self.reactor.NEVER)

    def _hotend_off_handler(self, eventtime):
        if not self.is_printing():
            self.log_info("Disabled extruder heater")
            self.gcode.run_script_from_command("M104 S0")
        return self.reactor.NEVER

    def _setup_pending_spool_id_timer(self):
        self.pending_spool_id_timer = self.reactor.register_timer(self._pending_spool_id_handler, self.reactor.NEVER)

    def _pending_spool_id_handler(self, eventtime):
        self.pending_spool_id = -1
        return self.reactor.NEVER

    def _check_pending_spool_id(self, gate):
        if self.pending_spool_id > 0 and self.p.spoolman_support != SPOOLMAN_PULL:
            self.log_info("Spool ID: %s automatically assigned to gate %d" % (self.pending_spool_id, gate))
            mod_gate_ids = self.assign_spool_id(gate, self.pending_spool_id)

            # Request sync and update of filament attributes from Spoolman
            if self.p.spoolman_support == SPOOLMAN_PUSH:
                self._spoolman_push_gate_map(mod_gate_ids)
            elif self.p.spoolman_support == SPOOLMAN_READONLY:
                self._spoolman_update_filaments(mod_gate_ids)

        # Disable timer to prevent reuse
        self.pending_spool_id = -1
        self.reactor.update_timer(self.pending_spool_id_timer, self.reactor.NEVER)

    # Assign spool id to gate and clear from other gates returning list of changes
    def assign_spool_id(self, gate, spool_id):
        self.gate_spool_id[gate] = spool_id
        mod_gate_ids = [(gate, spool_id)]
        for i, sid in enumerate(self.gate_spool_id):
            if sid == spool_id and i != gate:
                self.gate_spool_id[i] = -1
                mod_gate_ids.append((i, -1))
        return mod_gate_ids

    def _handle_idle_timeout_printing(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "printing")

    def _handle_idle_timeout_ready(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "ready")

    def _handle_idle_timeout_idle(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "idle")

    def is_printing(self, force_in_print=False): # Actively printing and not paused
        return self.print_state in ["started", "printing"] or force_in_print or self.p.test_force_in_print

    def is_in_print(self, force_in_print=False): # Printing or paused
        return bool(self.print_state in ["printing", "pause_locked", "paused"] or force_in_print or self.p.test_force_in_print)

    def is_mmu_paused(self): # The MMU is paused
        return self.print_state in ["pause_locked", "paused"]

    def is_mmu_paused_and_locked(self): # The MMU is paused (and locked)
        return self.print_state in ["pause_locked"]

    def is_in_endstate(self):
        return self.print_state in ["complete", "cancelled", "error", "ready", "standby", "initialized"]

    def is_in_standby(self):
        return self.print_state in ["standby"]

    def is_printer_printing(self):
        return bool(self.print_stats and self.print_stats.state == "printing")

    def is_printer_paused(self):
        return self.pause_resume.is_paused

    def is_paused(self):
        return self.is_printer_paused() or self.is_mmu_paused()

    def _wakeup(self):
        if self.is_in_standby():
            self._set_print_state("idle")

    def _check_not_printing(self):
        if self.is_printing():
            self.log_error("Operation not possible because currently printing")
            return True
        return False

    # Track print events simply to ease internal print state transitions. Specificly we want to detect
    # the start and end of a print and falling back into 'standby' state on idle
    #
    # Klipper reference sources for state:
    # print_stats: {'filename': '', 'total_duration': 0.0, 'print_duration': 0.0,
    #               'filament_used': 0.0, 'state': standby|printing|paused|complete|cancelled|error,
    #               'message': '', 'info': {'total_layer': None, 'current_layer': None}}
    # idle_status: {'state': Idle|Ready|Printing, 'printing_time': 0.0}
    # pause_resume: {'is_paused': True|False}
    #
    def _handle_idle_timeout_event(self, eventtime, event_type):
        if not self.is_enabled: return
        self.log_trace("Processing idle_timeout '%s' event" % event_type)

        if self.print_stats and self.p.print_start_detection:
            new_ps = self.print_stats.get_status(eventtime)
            if self.last_print_stats is None:
                self.last_print_stats = dict(new_ps)
                self.last_print_stats['state'] = 'initialized'
            prev_ps = self.last_print_stats
            old_state = prev_ps['state']
            new_state = new_ps['state']
            if new_state is not old_state:
                if new_state == "printing" and event_type == "printing":
                    # Figure out the difference between initial job start and resume
                    if prev_ps['state'] == "paused" and prev_ps['filename'] == new_ps['filename'] and prev_ps['total_duration'] < new_ps['total_duration']:
                        # This is a 'resumed' state so ignore
                        self.log_trace("Automaticaly detected RESUME (ignored), print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                    else:
                        # This is a 'started' state
                        self.log_trace("Automaticaly detected JOB START, print_status:print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                        if self.print_state not in ["started", "printing"]:
                            self._on_print_start(pre_start_only=True)
                            self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_START AUTOMATIC=1"))
                elif new_state in ["complete", "error"] and event_type == "ready":
                    self.log_trace("Automatically detected JOB %s, print_stats=%s, current mmu print_state=%s" % (new_state.upper(), new_state, self.print_state))
                    if new_state == "error":
                        self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=error AUTOMATIC=1"))
                    else:
                        self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=complete AUTOMATIC=1"))
                self.last_print_stats = dict(new_ps)

        # Capture transition to standby
        if event_type == "idle" and self.print_state != "standby":
            self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=standby IDLE_TIMEOUT=1"))

    def _print_event(self, command):
        try:
            self.gcode.run_script(command)
        except Exception:
            logging.exception("MMU: Error running job state initializer/finalizer")

    # MMU job state machine: initialized|ready|started|printing|complete|cancelled|error|pause_locked|paused|standby
    def _set_print_state(self, print_state, call_macro=True):
        if print_state != self.print_state:
            idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout
            self.log_debug("Job State: %s -> %s (MMU State: Encoder: %s, Synced: %s, Paused temp: %s, Resume to state: %s, Position saved for: %s, pause_resume: %s, Idle timeout: %.2fs)"
                    % (self.print_state.upper(), print_state.upper(), self._get_encoder_state(), self.mmu_toolhead().is_gear_synced_to_extruder(), self.paused_extruder_temp,
                        self.resume_to_state, self.saved_toolhead_operation, self.is_printer_paused(), idle_timeout))
            if call_macro:
                self.led_manager.print_state_changed(print_state, self.print_state)
                if self.printer.lookup_object("gcode_macro %s" % self.p.print_state_changed_macro, None) is not None:
                    self.wrap_gcode_command("%s STATE='%s' OLD_STATE='%s'" % (self.p.print_state_changed_macro, print_state, self.print_state))
            self.print_state = print_state

    # If this is called automatically when printing starts. The pre_start_only operations are performed on an idle_timeout
    # event so cannot block.  The remainder of moves will be called from the queue but they will be called early so
    # don't do anything that requires operating toolhead kinematics (we might not even be homed yet)
    def _on_print_start(self, pre_start_only=False):
        if self.print_state not in ["started", "printing"]:
            self.log_trace("_on_print_start(->started)")
            self._clear_saved_toolhead_position()
            self.num_toolchanges = 0
            self.paused_extruder_temp = None
            self._reset_job_statistics() # Reset job stats but leave persisted totals alone
            self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self.is_handling_runout = False
            self._clear_slicer_tool_map()
            self._enable_filament_monitoring() # Enable filament monitoring while printing
            self._initialize_encoder(dwell=None) # Encoder 0000
            self._set_print_state("started", call_macro=False)

        if not pre_start_only and self.print_state not in ["printing"]:
            self.log_trace("_on_print_start(->printing)")
            self.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=min_lifted_z VALUE=0" % self.p.park_macro) # Sequential printing movement "floor"
            self.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=False" % self.p.park_macro)
            msg = "Happy Hare initialized ready for print"
            if self.filament_pos == FILAMENT_POS_LOADED:
                msg += " (initial tool %s loaded)" % self.selected_tool_string()
            else:
                msg += " (no filament preloaded)"
            if self.ttg_map != self.p.default_ttg_map:
                msg += "\nWarning: Non default TTG map in effect"
            self.log_info(msg)
            self._set_print_state("printing")

            # Establish syncing state and grip (servo) position
            # (must call after print_state is set so we know we are printing)
            self.reset_sync_gear_to_extruder(self.mmu_unit().p.sync_to_extruder)

            # Ensure espooler wasn't reset
            self._adjust_espooler_assist()

    # Hack: Force state transistion to printing for any early moves if MMU_PRINT_START not yet run
    def _fix_started_state(self):
        if self.is_printer_printing() and not self.is_in_print():
            self.wrap_gcode_command("MMU_PRINT_START FIX_STATE=1")

    # If this is called automatically it will occur after the user's print ends.
    # Therefore don't do anything that requires operating kinematics
    def _on_print_end(self, state="complete"):
        if not self.is_in_endstate():
            self.log_trace("_on_print_end(%s)" % state)
            self.movequeues_wait()
            self._clear_saved_toolhead_position()
            self.resume_to_state = "ready"
            self.paused_extruder_temp = None
            self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER) # Don't automatically turn off extruder heaters

            self._restore_automap_option()
            self._disable_filament_monitoring() # Disable filament monitoring

            if self.printer.lookup_object("idle_timeout").idle_timeout != self.p.default_idle_timeout:
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.p.default_idle_timeout) # Restore original idle_timeout

            self._standalone_sync = False # Safer to clear this on print end or idle_timeout to standby to avoid user confusion
            self._set_print_state(state)

            # Establish syncing state and grip (servo) position
            # (must call after print_state is set)
            self.reset_sync_gear_to_extruder(False) # Intention is not to sync unless we have to

        if state == "standby" and not self.is_in_standby():
            self._set_print_state(state)
        self._clear_macro_state(reset=True)

    def handle_mmu_error(self, reason, force_in_print=False):
        self._fix_started_state() # Get out of 'started' state before transistion to mmu pause

        run_pause_macro = run_error_macro = recover_pos = send_event = False
        if self.is_in_print(force_in_print):
            if not self.is_mmu_paused():
                self._disable_filament_monitoring() # Disable filament monitoring while in paused state
                self._track_pause_start()
                self.resume_to_state = 'printing' if self.is_in_print() else 'ready'
                self.reason_for_pause = reason # Only store reason on first error
                self._display_mmu_error()
                self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp
                self.log_trace("Saved desired extruder temperature: %.1f%sC" % (self.paused_extruder_temp, UI_DEGREE))
                self.reactor.update_timer(self.hotend_off_timer, self.reactor.monotonic() + self.p.disable_heater) # Set extruder off timer
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.p.timeout_pause) # Set alternative pause idle_timeout
                self.log_trace("Extruder heater will be disabled in %s" % (self._seconds_to_string(self.p.disable_heater)))
                self.log_trace("Idle timeout in %s" % self._seconds_to_string(self.p.timeout_pause))
                self._save_toolhead_position_and_park('pause') # if already paused this is a no-op
                run_error_macro = True
                run_pause_macro = not self.is_printer_paused()
                send_event = True
                recover_pos = self.p.filament_recovery_on_pause
                self._set_print_state("pause_locked")
            else:
                self.log_error("MMU issue detected whilst printer is paused\nReason: %s" % reason)
                recover_pos = self.p.filament_recovery_on_pause

        else: # Not in a print (standalone operation)
            self.log_error("MMU issue: %s" % reason)
            # Restore original position if parked because there will be no resume
            if self.saved_toolhead_operation:
                self._restore_toolhead_position(self.saved_toolhead_operation)

        # Be deliberate about order of these tasks
        if run_error_macro:
            self.wrap_gcode_command(self.p.error_macro)

        if run_pause_macro:
            # Report errors and ensure we always pause
            self.wrap_gcode_command(self.p.pause_macro, exception=False)
            self.pause_resume.send_pause_command()

        if recover_pos:
            self.recover_filament_pos(message=True)

        # Intention is not to sync unless we have to but will be restored on resume/continue_printing
        self.reset_sync_gear_to_extruder(False)

        if send_event:
            self.printer.send_event("mmu:mmu_paused") # Notify MMU paused event

    # Displays MMU error/pause as pop-up dialog and/or via console
    def _display_mmu_error(self):
        msg= "Print%s paused" % (" was already" if self.is_printer_paused() else " will be")
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.p.error_dialog_macro, None)
        if self.p.show_error_dialog and dialog_macro is not None:
            # Klipper doesn't handle string quoting so strip problematic characters
            reason = self.reason_for_pause.replace("\n", ". ")
            for c in "#;'":
                reason = reason.replace(c, "")
            self.wrap_gcode_command('%s MSG="%s" REASON="%s"' % (self.p.error_dialog_macro, msg, reason))
        self.log_error("MMU issue detected. %s\nReason: %s" % (msg, self.reason_for_pause))
        self.log_always("After fixing, call RESUME to continue printing (MMU_UNLOCK to restore temperature)")

    def _clear_mmu_error_dialog(self):
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.p.error_dialog_macro, None)
        if self.p.show_error_dialog and dialog_macro is not None:
            self.wrap_gcode_command('RESPOND TYPE=command MSG="action:prompt_end"')

    def _mmu_unlock(self):
        if self.is_mmu_paused():
            self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.p.default_idle_timeout)
            self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER)

            # Important to wait for stable temperature to resume exactly how we paused
            if self.paused_extruder_temp:
                self.log_info("Enabled extruder heater")
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self._set_print_state("paused")

    # Continue after load/unload/change_tool/runout operation or pause/error
    def _continue_after(self, operation, force_in_print=False, restore=True):
        self.log_debug("Continuing from %s state after %s" % (self.print_state, operation))
        if self.is_mmu_paused() and operation == 'resume':
            self.reason_for_pause = None
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self.paused_extruder_temp = None
            self._track_pause_end()
            if self.is_in_print(force_in_print):
                self._enable_filament_monitoring() # Enable filament monitoring while printing
            self._set_print_state(self.resume_to_state)
            self.resume_to_state = "ready"
            self.printer.send_event("mmu:mmu_resumed")
        elif self.is_mmu_paused():
            # If paused we can only continue on resume
            return

        if self.is_printing(force_in_print):
            self.sensor_manager.confirm_loaded() # Can throw MmuError
            self.is_handling_runout = False
            self._initialize_encoder(dwell=None) # Encoder 0000

            # Restablish desired syncing state and grip (servo) position
            self.reset_sync_gear_to_extruder(self.mmu_unit().p.sync_to_extruder, force_in_print=force_in_print)

        # Good place to reset the _next_tool marker because after any user fix on toolchange error/pause
        self._next_tool = TOOL_GATE_UNKNOWN

        # PAUL TODO?  Perhaps neutralize filament tension here if filament LOADED and SYNCED? (or perhaps in the is_printing() block?)

        # Restore print position as final step so no delay
        self._restore_toolhead_position(operation, restore=restore)

        # Ensure espooler wasn't reset
        self._adjust_espooler_assist()

        # Ready to continue printing...

    def _clear_macro_state(self, reset=False):
        if self.printer.lookup_object('gcode_macro %s' % self.p.clear_position_macro, None) is not None:
            self.wrap_gcode_command("%s%s" % (self.p.clear_position_macro, " RESET=1" if reset else ""))

    def _save_toolhead_position_and_park(self, operation, next_pos=None):
        if operation not in ['complete', 'cancel'] and 'xyz' not in self.toolhead.get_status(self.reactor.monotonic())['homed_axes']:
            self.gcode.run_script_from_command(self.p.toolhead_homing_macro)
            self.movequeues_wait()

        eventtime = self.reactor.monotonic()
        homed = self.toolhead.get_status(eventtime)['homed_axes']
        if 'xyz' in homed:
            if not self.saved_toolhead_operation:
                # Save toolhead position

                # This is paranoia so I can be absolutely sure that Happy Hare leaves toolhead the same way when we are done
                gcode_pos = self.gcode_move.get_status(eventtime)['gcode_position']
                toolhead_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                self.log_debug("Saving toolhead gcode state and position (%s) for %s" % (toolhead_gcode_pos, operation))
                self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % TOOLHEAD_POSITION_STATE)
                self.saved_toolhead_operation = operation

                # Save toolhead velocity limits and set user defined for macros
                self.saved_toolhead_max_accel = self.toolhead.max_accel
                self.saved_toolhead_min_cruise_ratio = self.toolhead.get_status(eventtime).get('minimum_cruise_ratio', None)
                cmd = "SET_VELOCITY_LIMIT ACCEL=%.4f" % self.p.macro_toolhead_max_accel
                if self.saved_toolhead_min_cruise_ratio is not None:
                    cmd += " MINIMUM_CRUISE_RATIO=%.4f" % self.p.macro_toolhead_min_cruise_ratio
                self.gcode.run_script_from_command(cmd)

                # Record the intended X,Y resume position (this is also passed to the pause/resume restore position in pause is later called)
                if next_pos:
                    self.gcode_move.saved_states[TOOLHEAD_POSITION_STATE]['last_position'][:2] = next_pos

                # Make sure we record the current speed/extruder overrides
                if self.tool_selected >= 0:
                    mmu_state = self.gcode_move.saved_states[TOOLHEAD_POSITION_STATE]
                    self.tool_speed_multipliers[self.tool_selected] = mmu_state['speed_factor'] * 60.
                    self.tool_extrusion_multipliers[self.tool_selected] = mmu_state['extrude_factor']

                # This will save the print position in the macro and apply park
                self.wrap_gcode_command(self.p.save_position_macro)
                self.wrap_gcode_command(self.p.park_macro)
            else:
                # Re-apply parking for new operation (this will not change the saved position in macro)

                self.saved_toolhead_operation = operation # Update operation in progress
                # Force re-park now because user may not be using HH client_macros. This can result
                # in duplicate calls to parking macro but it is itempotent and will ignore
                self.wrap_gcode_command(self.p.park_macro)
        else:
            self.log_debug("Cannot save toolhead position or z-hop for %s because not homed" % operation)

    def _restore_toolhead_position(self, operation, restore=True):
        eventtime = self.reactor.monotonic()
        if self.saved_toolhead_operation:
            # Inject speed/extruder overrides into gcode state restore data
            if self.tool_selected >= 0:
                mmu_state = self.gcode_move.saved_states[TOOLHEAD_POSITION_STATE]
                mmu_state['speed_factor'] = self.tool_speed_multipliers[self.tool_selected] / 60.
                mmu_state['extrude_factor'] = self.tool_extrusion_multipliers[self.tool_selected]

            # If this is the final "restore toolhead position" call then allow macro to restore position, then sanity check
            # Note: if user calls BASE_RESUME, print will restart but from incorrect position that could be restored later!
            if not self.is_paused() or operation == "resume":
                # Controlled by the RESTORE=0 flag to MMU_LOAD, MMU_EJECT, MMU_CHANGE_TOOL (only real use case is final unload and perhaps initial load)
                restore_macro = "%s RESTORE=%d" % (self.p.restore_position_macro, int(restore))
                # Restore macro position and clear saved
                self.wrap_gcode_command(restore_macro) # Restore macro position and clear saved

                if restore:
                    # Paranoia: no matter what macros do ensure position and state is good. Either last, next or none (current x,y)
                    sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
                    travel_speed = 200
                    if sequence_vars_macro:
                        if sequence_vars_macro.variables.get('restore_xy_pos', 'last') == 'none' and self.saved_toolhead_operation in ['toolchange']:
                            # Don't change x,y position on toolchange
                            current_pos = self.gcode_move.get_status(eventtime)['gcode_position']
                            self.gcode_move.saved_states[TOOLHEAD_POSITION_STATE]['last_position'][:2] = current_pos[:2]
                        travel_speed = sequence_vars_macro.variables.get('park_travel_speed', travel_speed)
                    gcode_pos = self.gcode_move.saved_states[TOOLHEAD_POSITION_STATE]['last_position']
                    display_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                    self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=%s MOVE=1 MOVE_SPEED=%.1f" % (TOOLHEAD_POSITION_STATE, travel_speed))
                    self.log_debug("Ensuring correct gcode state and position (%s) after %s" % (display_gcode_pos, operation))

                self._clear_saved_toolhead_position()

                # Always restore toolhead velocity limits
                if self.saved_toolhead_max_accel:
                    cmd = "SET_VELOCITY_LIMIT ACCEL=%.4f" % self.saved_toolhead_max_accel
                    if self.saved_toolhead_min_cruise_ratio is not None:
                        cmd += " MINIMUM_CRUISE_RATIO=%.4f" % self.saved_toolhead_min_cruise_ratio
                    self.gcode.run_script_from_command(cmd)
                    self.saved_toolhead_max_accel = None
            else:
                pass # Resume will call here again shortly so we can ignore for now
        else:
            # Ensure all saved state is cleared
            self._clear_macro_state()
            self._clear_saved_toolhead_position()

    def _clear_saved_toolhead_position(self):
        self.saved_toolhead_operation = ''

    def _disable_filament_monitoring(self):
        eventtime = self.reactor.monotonic()
        enabled = self.runout_enabled
        self.runout_enabled = False
        self.log_trace("Disabled FlowGuard and runout detection")
        if self.has_encoder() and self.encoder().is_enabled():
            self.encoder().disable()
        self.sensor_manager.disable_runout(self.gate_selected)
        self.mmu_unit().sync_feedback.deactivate_flowguard(eventtime)
        return enabled

    def _enable_filament_monitoring(self):
        eventtime = self.reactor.monotonic()
        self.runout_enabled = True
        self.log_trace("Enabled FlowGuard and runout detection")
        if self.has_encoder() and not self.encoder().is_enabled():
            self.encoder().enable()
        self.sensor_manager.enable_runout(self.gate_selected)
        self.mmu_unit().sync_feedback.activate_flowguard(eventtime)
        self.runout_last_enable_time = eventtime

    @contextlib.contextmanager
    def _wrap_suspend_filament_monitoring(self):
        enabled = self._disable_runout()
        try:
            yield self
        finally:
            if enabled:
                self._enable_runout()

    # To suppress visual filament position
    @contextlib.contextmanager
    def wrap_suppress_visual_log(self):
        log_visual = self.p.log_visual
        self.p.log_visual = 0
        try:
            yield self
        finally:
            self.p.log_visual = log_visual

    # For all encoder methods, 'dwell' means:
    #   True  - gives klipper a little extra time to deliver all encoder pulses when absolute accuracy is required
    #   False - wait for moves to complete and then read encoder
    #   None  - just read encoder without delay (assumes prior movements have completed)
    # Return 'False' if no encoder fitted
    def _encoder_dwell(self, dwell):
        if self.has_encoder():
            if dwell:
                self.movequeues_dwell(self.p.encoder_dwell)
                self.movequeues_wait()
                return True
            elif dwell is False and self._can_use_encoder():
                self.movequeues_wait()
                return True
            elif dwell is None and self._can_use_encoder():
                return True
        return False

    @contextlib.contextmanager
    def _require_encoder(self):
        """
        Context: Forces encoder to validate despite user config by overriding
        'encoder_move_validation' setting. Will log coding error with assertion.
        """
        params = self.mmu_unit().p
        prev = params.encoder_move_validation
        if not self.has_encoder():
            self.log_assertion("Encoder required for chosen operation but not present on MMU")
            params.encoder_move_validation = False
        else:
            params.encoder_move_validation = True
        try:
            yield self
        finally:
            params.encoder_move_validation = prev

    def get_encoder_distance(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder().get_distance()
        else:
            return 0.

    def _get_encoder_counts(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder().get_counts()
        else:
            return 0

    def set_encoder_distance(self, distance, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder().set_distance(distance)

    def _initialize_encoder(self, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder().reset_counts()

    def _get_encoder_dead_space(self):
        if self.has_encoder() and self.mmu_unit().p.gate_homing_endstop in [SENSOR_GATE, SENSOR_GEAR_PREFIX]:
            return self.mmu_unit().p.gate_endstop_to_encoder
        else:
            return 0.

    def _initialize_filament_position(self, dwell=False):
        self._initialize_encoder(dwell=dwell)
        self._set_filament_position()

    def _get_filament_position(self):
        return self.mmu_toolhead().get_position()[1]

    def _get_live_filament_position(self):
        """
        Return the approximate live filament position
        """
        gear_stepper = self.gear_rail.steppers[0]
        mcu_pos = gear_stepper.get_mcu_position()
        return mcu_pos * gear_stepper.get_step_dist()

    def _set_filament_position(self, position = 0.):
        pos = self.mmu_toolhead().get_position()
        pos[1] = position
        self.mmu_toolhead().set_position(pos)
        return position

    def _set_filament_remaining(self, length, color=''):
        self.filament_remaining = length
        self.var_manger.set(VARS_MMU_FILAMENT_REMAINING, max(0, round(length, 1)))
        self.var_manger.set(VARS_MMU_FILAMENT_REMAINING_COLOR, color, write=True)

    def _set_last_tool(self, tool):
        self._last_tool = tool
        self.var_manager.set(VARS_MMU_LAST_TOOL, tool, write=True)

    def _set_filament_pos_state(self, state, silent=False):
        if self.filament_pos != state:
            self.filament_pos = state
            if self.gate_selected != TOOL_GATE_BYPASS or state == FILAMENT_POS_UNLOADED or state == FILAMENT_POS_LOADED:
                self._display_visual_state(silent=silent)

            # Minimal save_variable writes
            if state in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]:
                self.var_manager.save_variable(VARS_MMU_FILAMENT_POS, state, write=True)
            elif self.var_manager.save_variables.allVariables.get(VARS_MMU_FILAMENT_POS, 0) != FILAMENT_POS_UNKNOWN:
                self.var_manager.save_variable(VARS_MMU_FILAMENT_POS, FILAMENT_POS_UNKNOWN, write=True)

        self._adjust_espooler_assist()

    def _adjust_espooler_assist(self):
        """
        Ensure espooler print assist is in correct state based on whether the filament is in the extruder or not
        """
        if self.has_espooler():
            if self.filament_pos == FILAMENT_POS_LOADED:
                if ESPOOLER_PRINT in self.mmu_unit().p.espooler_operations and self.mmu_unit().p.espooler_printing_power == 0:
                    # Enable in-print assist because filament is in the extruder
                    self.espooler.set_print_assist_mode(self.gate_selected)
            else:
                # Ensure in-print assist mode is removed
                # (it could have been enabled manually with MMU_ESPOOLER)
                self.espooler.reset_print_assist_mode()

    def _set_filament_direction(self, direction):
        self.filament_direction = direction

    def _must_home_to_extruder(self):
        return self.mmu_unit().p.extruder_homing_endstop != SENSOR_EXTRUDER_NONE and (self.mmu_unit().p.extruder_force_homing or not self.sensor_manager.has_sensor(SENSOR_TOOLHEAD))

    def _must_buffer_extruder_homing(self):
        return self._must_home_to_extruder() and self.mmu_unit().p.extruder_homing_endstop != SENSOR_EXTRUDER_COLLISION

    def check_if_disabled(self):
        if not self.is_enabled:
            self.log_error("Operation not possible. MMU is disabled. Please use MMU ENABLE=1 to use")
            return True
        self._wakeup()
        return False

    def check_if_printing(self):
        if self.is_printing():
            self.log_error("Operation not possible. Printer is actively printing")
            return True
        return False

    def check_if_bypass(self):
        if self.tool_selected == TOOL_GATE_BYPASS and self.filament_pos not in [FILAMENT_POS_UNLOADED]:
            self.log_error("Operation not possible. MMU is currently using bypass. Unload or select a different gate first")
            return True
        return False

    def check_if_not_homed(self):
        if not self.selector().is_homed:
            self.log_error("Operation not possible. MMU selector is not homed")
            return True
        return False

    def check_if_loaded(self):
        if self.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_UNKNOWN]:
            self.log_error("Operation not possible. Filament is loaded")
            return True
        return False

    def check_if_not_loaded(self):
        if self.filament_pos != FILAMENT_POS_LOADED:
            self.log_error("Operation not possible. Filament is not loaded")
            return True
        return False

    def check_if_gate_not_valid(self):
        if self.gate_selected < 0:
            self.log_error("Operation not possible. No MMU gate selected")
            return True
        return False

    def check_if_always_gripped(self):
        if self.mmu_unit().filament_always_gripped:
            self.log_error("Operation not possible. MMU design doesn't allow for manual override of syncing state.\nSyncing will be enabled if filament is inside the extruder.\nUse `MMU_RECOVER` to correct filament position if necessary.")
            return True
        return False

    def check_if_no_bowden_move(self):
        if not self.mmu_unit().require_bowden_move:
            self.log_error("Operation not possible. MMU design does not require bowden move/calibration")
            return True
        return False

    # Check if everything calibrated
    # Returns True is required calibration is not complete. Defaults to all gates or a specific gate can
    # be specified. The purpose of this is to highlight to the user what is not fully calibrated on their
    # MMU. It will default to not reporting calibration steps that are optional based on "autotune" options
    # Params: required     - bitmap of required calibration checks
    #         silent       - report errors (None = report but don't log as error)
    #         check_gates  - list of gates to consider (None = all)
    #         use_autotune - True = don't warn if handled by autocal/autotune options, False = warn
    def check_if_not_calibrated(self, required, silent=False, check_gates=None, use_autotune=True):
        calibrated = True

        if check_gates is None:
            check_gates = list(range(self.num_gates))

        # What mmu_units are involved?
        mmu_units = []
        for g in check_gates:
            u = self.mmu_unit(g)
            if u not in mmu_units:
                mmu_units.append(u)

        # Iterate over mmu_units with separate calibration message for each
        for mmu_unit in mmu_units:
            if not self.calibration_manager.check_calibrated(mmu_unit, required):
                rmsg = omsg = ""
                if (
                    (not use_autotune or not self.mmu_unit().p.autocal_selector) and
                    (required & CalibrationManager.CALIBRATED_SELECTOR) and
                    not self.calibration_manager.check_calibrated(mmu_unit, CalibrationManager.CALIBRATED_SELECTOR)
                ):
                    unit_check_gates = [gate for gate in check_gates if mmu_unit.manages_gate(gate)]
                    uncalibrated = mmu_unit.selector.get_uncalibrated_gates(unit_check_gates)
                    if uncalibrated:
                        info = "\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: %s" % ",".join(map(str, uncalibrated))
                        if self.mmu_unit().p.autocal_selector:
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not self.mmu_unit().p.skip_cal_rotation_distance) and
                    (required & CalibrationManager.CALIBRATED_GEAR_0) and
                    not self.calibration_manager.check_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_0)
                ):
                    uncalibrated = self.rotation_distances[mmu_unit.first_gate] == -1
                    if uncalibrated:
                        info = "\n- Use MMU_CALIBRATE_GEAR (with first gate selected)"
                        info += " to calibrate gear rotation_distance for first gate"
                        if self.mmu_unit().p.skip_cal_rotation_distance:
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not self.mmu_unit().p.skip_cal_encoder) and
                    (required & CalibrationManager.CALIBRATED_ENCODER) and
                    not self.calibration_manager.check_calibrated(mmu_unit, CalibrationManager.CALIBRATED_ENCODER)
                ):
                    info = "\n- Use MMU_CALIBRATE_ENCODER (with first gate selected)"
                    if self.mmu_unit().p.skip_cal_encoder:
                        omsg += info
                    else:
                        rmsg += info

                if (
                    mmu_unit.variable_rotation_distances and
                    (not use_autotune or not (self.mmu_unit().p.skip_cal_rotation_distance or self.mmu_unit().p.autotune_rotation_distance)) and
                    (required & CalibrationManager.CALIBRATED_GEAR_RDS) and
                    not self.calibration_manager.check_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_RDS)
                ):
                    uncalibrated = [lgate for lgate, value in enumerate(self.rotation_distances[mmu_unit.first_gate:mmu_unit.first_gate + mmu_unit.num_gates]) if lgate != 0 and value == -1 and lgate + mmu_unit.first_gate in check_gates]
                    if uncalibrated:
                        if mmu_unit.encoder:
                            info = "\n- Use MMU_CALIBRATE_GEAR (with appropriate gate selected) or MMU_CALIBRATE_GATES GATE=xx"
                            info += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                        else:
                            info = "\n- Use MMU_CALIBRATE_GEAR (with appropriate gate selected)"
                            info += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                        if (self.mmu_unit().p.skip_cal_rotation_distance or self.mmu_unit().p.autotune_rotation_distance):
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not self.mmu_unit().p.autocal_bowden_length) and
                    (required & CalibrationManager.CALIBRATED_BOWDENS) and
                    not self.calibration_manager.check_calibrated(mmu_unit, CalibrationManager.CALIBRATED_BOWDENS)
                ):
                    if mmu_unit.variable_bowden_lengths:
                        uncalibrated = [lgate for lgate, value in enumerate(self.bowden_lengths[mmu_unit.first_gate:mmu_unit.first_gate + mmu_unit.num_gates]) if value == -1 and lgate + mmu_unit.first_gate in check_gates]
                        if uncalibrated:
                            info = "\n- Use MMU_CALIBRATE_BOWDEN (with gate selected)"
                            info += " to calibrate bowden length gates: %s" % ",".join(map(str, uncalibrated))
                            if self.mmu_unit().p.autocal_bowden_length:
                                omsg += info
                            else:
                                rmsg += info
                    else:
                        uncalibrated = self.bowden_lengths[mmu_unit.first_gate] == -1
                        if uncalibrated:
                            info = "\n- Use MMU_CALIBRATE_BOWDEN (with first gate selected) to calibrate bowden length"
                            if self.mmu_unit().p.autocal_bowden_length:
                                omsg += info
                            else:
                                rmsg += info

                if rmsg or omsg:
                    msg = "Warning: Calibration steps are not complete for mmu unit %s:" % mmu_unit.name
                    if rmsg:
                        msg += "\nRequired:%s" % rmsg
                    if omsg:
                        msg += "\nOptional (handled by autocal/autotune):%s" % omsg
                    if not silent:
                        if silent is None: # Bootup/status use case to avoid looking like error
                            self.log_always("{2}%s{0}" % msg, color=True)
                        else:
                            self.log_error(msg)
                    calibrated = False

        return not calibrated

    def check_if_spoolman_enabled(self):
        if self.p.spoolman_support == SPOOLMAN_OFF:
            self.log_error("Spoolman support is currently disabled")
            return True
        return False

    def _gate_homing_string(self):
        return "ENCODER" if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER else "%s sensor" % self.mmu_unit().p.gate_homing_endstop

    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.extruder_name)
        current_temp = extruder.get_status(0)['temperature']
        current_target_temp = extruder.heater.target_temp
        klipper_minimum_temp = extruder.get_heater().min_extrude_temp
        gate_temp = self.gate_temperature[self.gate_selected] if self.gate_selected >= 0 and self.gate_temperature[self.gate_selected] > 0 else self.p.default_extruder_temp
        self.log_trace("_ensure_safe_extruder_temperature: current_temp=%s, paused_extruder_temp=%s, current_target_temp=%s, klipper_minimum_temp=%s, gate_temp=%s, default_extruder_temp=%s, source=%s" % (current_temp, self.paused_extruder_temp, current_target_temp, klipper_minimum_temp, gate_temp, self.p.default_extruder_temp, source))

        if source == "pause":
            new_target_temp = self.paused_extruder_temp if self.paused_extruder_temp is not None else current_temp # Pause temp should not be None
            if self.paused_extruder_temp < klipper_minimum_temp:
                # Don't wait if just messing with cold printer
                wait = False

        elif source == "auto": # Normal case
            if self.is_mmu_paused():
                # In a pause we always want to restore the temp we paused at
                if self.paused_extruder_temp is not None:
                    new_target_temp = self.paused_extruder_temp
                    source = "pause"
                else: # Pause temp should not be None
                    new_target_temp = current_temp
                    source = "current"

            elif self.is_printing():
                if current_target_temp < klipper_minimum_temp:
                    # Almost certainly means the initial tool change before slicer has set
                    if self.gate_selected >= 0:
                        new_target_temp = gate_temp
                        source = "gatemap"
                    else:
                        new_target_temp = self.p.default_extruder_temp
                        source = "mmu default"
                else:
                    # While actively printing, we want to defer to the slicer for temperature
                    new_target_temp = current_target_temp
                    source = "slicer"

            else:
                # Standalone "just messing" case
                if current_target_temp > klipper_minimum_temp:
                    new_target_temp = current_target_temp
                    source = "current"
                else:
                    if self.gate_selected >= 0:
                        new_target_temp = gate_temp
                        source = "gatemap"
                    else:
                        new_target_temp = self.p.default_extruder_temp
                        source = "mmu default"

            # Final safety check
            if new_target_temp <= klipper_minimum_temp:
                new_target_temp = self.p.default_extruder_temp
                source = "mmu default"

        if new_target_temp > current_target_temp:
            if source in ["mmu default", "gatemap"]:
                # We use error log channel to avoid heating surprise. This will also cause popup in Klipperscreen
                self.log_error("Alert: Automatically heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            else:
                self.log_info("Heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            wait = True # Always wait to warm up

        if new_target_temp > 0:
            self.gcode.run_script_from_command("M104 S%.1f" % new_target_temp)

            # Optionally wait until temperature is stable or at minimum safe temp so extruder can move
            if wait and new_target_temp >= klipper_minimum_temp and abs(new_target_temp - current_temp) > self.p.extruder_temp_variance:
                with self.wrap_action(ACTION_HEATING):
                    self.log_info("Waiting for extruder to reach target (%s) temperature: %.1f%sC" % (source, new_target_temp, UI_DEGREE))
                    self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=%s MINIMUM=%.1f MAXIMUM=%.1f" % (self.extruder_name, new_target_temp - self.p.extruder_temp_variance, new_target_temp + self.p.extruder_temp_variance))

    def selected_tool_string(self, tool=None):
        if tool is None:
            tool = self.tool_selected
        if tool == TOOL_GATE_BYPASS:
            return "Bypass"
        elif tool == TOOL_GATE_UNKNOWN:
            return "Unknown"
        else:
            return "T%d" % tool

    def selected_gate_string(self, gate=None):
        if gate is None:
            gate = self.gate_selected
        if gate == TOOL_GATE_BYPASS:
            return "bypass"
        elif gate == TOOL_GATE_UNKNOWN:
            return "unknown"
        else:
            return "#%d" % gate

    def selected_unit_string(self, unit=None):
        return " (unit #%d)" % self.unit_selected if self.mmu_machine.num_units > 1 else ""

    def _set_action(self, action):
        if action == self.action: return action
        old_action = self.action
        self.action = action
        self.led_manager.action_changed(action, old_action)
        if self.printer.lookup_object("gcode_macro %s" % self.p.action_changed_macro, None) is not None:
            self.wrap_gcode_command("%s ACTION='%s' OLD_ACTION='%s'" % (self.p.action_changed_macro, self._get_action_string(), self._get_action_string(old_action)))
        return old_action

    @contextlib.contextmanager
    def wrap_action(self, new_action):
        old_action = self._set_action(new_action)
        try:
            yield (old_action, new_action)
        finally:
            self._set_action(old_action)

    def _enable_mmu(self):
        if self.is_enabled: return
        self.reinit()
        self._load_persisted_state()
        self.is_enabled = True
        self.printer.send_event("mmu:enabled")
        self.log_always("MMU enabled")
        self._schedule_mmu_bootup_tasks()

    def _disable_mmu(self):
        if not self.is_enabled: return
        self.reinit()
        self._disable_filament_monitoring()
        self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER)
        self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.p.default_idle_timeout)
        self.motors_onoff(on=False) # Will also unsync gear
        self.is_enabled = False
        self.printer.send_event("mmu:disabled")
        self._set_print_state("standby")
        self.log_always("MMU disabled")

    def _random_failure(self):
        if self.p.test_random_failures and random.randint(0, 10) == 0:
            raise MmuError("Randomized testing failure")


### STATE GCODE COMMANDS #########################################################

    cmd_MMU_help = "Enable/Disable functionality and reset state"
    def cmd_MMU(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        enable = gcmd.get_int('ENABLE', minval=0, maxval=1)
        if enable == 1:
            self._enable_mmu()
        else:
            self._disable_mmu()

    cmd_MMU_HELP_help = "Display the complete set of MMU commands and function"
    cmd_MMU_HELP_param_help = (
        "MMU_HELP: %s\n" % cmd_MMU_HELP_help
        + "ALL       = [0|1]\n"
        + "TESTING   = [0|1]\n"
        + "SLICER    = [0|1]\n"
        + "CALLBACKS = [0|1]\n"
        + "STEPS     = [0|1]\n"
        + "CMD       = _command_\n"
        + "SHOWPARMS = [0|1]\n"
    )
    def cmd_MMU_HELP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        testing = gcmd.get_int('TESTING', 0, minval=0, maxval=1)
        slicer = gcmd.get_int('SLICER', 0, minval=0, maxval=1)
        callbacks = gcmd.get_int('CALLBACKS', 0, minval=0, maxval=1)
        steps = gcmd.get_int('STEPS', 0, minval=0, maxval=1)
        show_all = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        if show_all:
            testing = slicer = callback = steps = 1
        cmd = gcmd.get('CMD', None)
        show_params = bool(gcmd.get_int('SHOWPARAMS', 0, minval=0, maxval=1))

        msg = "Happy Hare MMU commands:\n"
        msg += "(use SLICER=1 CALLBACKS=1 TESTING=1 STEPS=1, ALL=1 for subsets (add 'SHOWPARAMS=1' for params). Use 'MMU_HELP CMD=command' or 'command HELP=1' for individual parameter summary)\n"
        testing_msg = "\nCalibration and testing commands:\n"
        slicer_msg = "\nPrint start/end or slicer macros (defined in mmu_software.cfg\n"
        callback_msg = "\nCallbacks (defined in mmu_sequence.cfg, mmu_state.cfg)\n"
        seq_msg = "\nAdvanced load/unload sequence and steps:\n"

        if help:
            self.log_always(self.format_help(self.cmd_MMU_HELP_param_help), color=True)
            return

        command_classes = [self, self.sensor_manager, self.led_manager, self.selector()] # All Happy Hare classes defining 'MMU_*' commands
        def get_param_help(c):
            cu = c.upper().lstrip('_')
            for cls in command_classes:
                attr_name = 'cmd_%s_param_help' % cu
                if hasattr(cls, attr_name):
                    return self.format_help(getattr(cls, attr_name))
            return ''

        if cmd is not None:
            param_help = get_param_help(cmd)
            if param_help:
                self.log_always(param_help, color=True)
            elif any(hasattr(cls, 'cmd_%s' % cmd) for cls in command_classes):
                self.log_always("Sorry, command %s has no parameter help" % cmd)
            else:
                self.log_always("Command %s does not exist" % cmd)
            return

        cmds = list(self.gcode.ready_gcode_handlers.keys())
        cmds.sort()

        # Logic to partition commands:
        for c in cmds:
            cu = c.upper()
            d = self.gcode.gcode_help.get(c, "n/a")

            if (c.startswith("MMU_START") or c.startswith("MMU_END") or c in ["MMU_UPDATE_HEIGHT"]) and c not in ["MMU_ENDLESS_SPOOL"]:
                if show_params and get_param_help(cu):
                    slicer_msg += get_param_help(cu)
                else:
                    slicer_msg += "%s: %s\n" % (cu, d) # Print start/end macros

            elif c.startswith("MMU") and not c.startswith("MMU__"):
                if any(substring in c for substring in ["_CALIBRATE", "_TEST", "_SOAKTEST", "MMU_COLD_PULL"]):
                    if show_params and get_param_help(cu):
                        testing_msg += get_param_help(cu)
                    else:
                        testing_msg += "%s: %s\n" % (cu, d) # Testing and calibration commands
                else:
                    if c not in ["MMU_CHANGE_TOOL_STANDALONE", "MMU_CHECK_GATES", "MMU_REMAP_TTG", "MMU_FORM_TIP"]: # Remove aliases
                        if show_params and get_param_help(cu):
                            msg += get_param_help(cu)
                        else:
                            msg += self.format_help("%s: %s" % (cu, d)) # Base command

            elif c.startswith("_MMU"):
                if c.startswith("_MMU_STEP") or c in ["_MMU_M400", "_MMU_LOAD_SEQUENCE", "_MMU_UNLOAD_SEQUENCE"]:
                    if show_params and get_param_help(cu):
                        seq_msg += get_param_help(cu)
                    else:
                        seq_msg += "%s: %s\n" % (cu, d) # Invidual sequence step commands
                elif c.startswith("_MMU_PRE_") or c.startswith("_MMU_POST_") or c in ["_MMU_ACTION_CHANGED", "_MMU_EVENT", "_MMU_PRINT_STATE_CHANGED"]:
                    callback_msg += "%s: %s\n" % (cu, d) # Callbacks

        msg += slicer_msg if slicer else ""
        msg += callback_msg if callbacks else ""
        msg += testing_msg if testing else ""
        msg += seq_msg if steps else ""
        self.log_always(msg, color=True)

    cmd_MMU_ENCODER_help = "Display encoder position and stats or enable/disable runout detection logic in encoder"
    def cmd_MMU_ENCODER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self.check_if_disabled(): return
        value = gcmd.get_float('VALUE', -1, minval=0.)
        enable = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        if enable == 1:
# PAUL old            self.encoder().set_mode(self.enable_clog_detection)
            self.mmu_unit().sync_feedback.set_encoder_mode() # PAUL pass mode MOGGIE
        elif enable == 0:
# PAUL old            self.encoder().set_mode(self.encoder().RUNOUT_DISABLED)
            self.mmu_unit().sync_feedback.set_encoder_mode(RUNOUT_DISABLED) # PAUL contant name?? MOGGIE
        elif value >= 0.:
            self.set_encoder_distance(value)
            return
        self.log_info(self._get_encoder_summary(detail=True))


    cmd_MMU_ESPOOLER_help = "Direct control of espooler or display of current status"
    cmd_MMU_ESPOOLER_param_help = (
        "MMU_ESPOOLER: %s\n" % cmd_MMU_ESPOOLER_help
        + "ALLOFF = [0|1] Quick way to turn all espoolers off\n"
        + "TRIGGER = [0|1] Fire in-print trigger for testing\n"
        + "BURST = [0|1] Jog in direction of OPERATION (assist|rewind) using configured burst duration and power\n"
        + "DURATION = [0-10] Override duration of PWM signal (seconds) for burst operations\n"
        + "GATE = g Specify gate to operate on (defaults to current gate)\n"
        + "LOOSEN = [0|1] Quick way to loosen filament on spool\n"
        + "OPERATION = [assist|off|print|rewind] Set espooler operation mode\n"
        + "POWER = [0-100] Override default % power to apply to espooler motor\n"
        + "QUIET = [0|1] Used to suppress console/log output\n"
        + "RESET = [0|1] Turn of in-print assist\n"
        + "TIGHTEN = [0|1] Quick way to tighten filament on spool\n"
        + "(no parameters for status report)"
    )
    def cmd_MMU_ESPOOLER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.log_always(self.format_help(self.cmd_MMU_ESPOOLER_param_help), color=True)
            return

        if self._check_has_espooler(): return

        operation = gcmd.get('OPERATION', None)
        burst = gcmd.get_int('BURST', 0, minval=0, maxval=1)
        tighten = gcmd.get_int('TIGHTEN', 0, minval=0, maxval=1)
        loosen = gcmd.get_int('LOOSEN', 0, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        alloff = bool(gcmd.get_int('ALLOFF', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        trigger = bool(gcmd.get_int('TRIGGER', 0, minval=0, maxval=1))
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.num_gates - 1)

        if reset:
            # Turn off in-print assist mode
            self.espooler.reset_print_assist_mode()

        if trigger:
            # Mimick in-print assist trigger
            # No gate specified = similar to extruder movement
            # With gate specified = similar to filament tension trigger
            self.espooler.advance(gate)

        if alloff:
            for gate in range(self.num_gates):
                self.espooler.set_operation(gate, 0, ESPOOLER_OFF)

        elif tighten or loosen:
            if gate is None:
                gate = self.gate_selected
            if gate < 0:
                raise gcmd.error("Invalid gate")

            power = self.mmu_unit().p.espooler_assist_burst_power if loosen else self.mmu_unit().p.espooler_rewind_burst_power
            duration = self.mmu_unit().p.espooler_assist_burst_duration if loosen else self.mmu_unit().p.espooler_rewind_burst_duration
            operation = ESPOOLER_ASSIST if loosen else ESPOOLER_REWIND
            self.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, operation)

        elif operation is not None:
            operation = operation.lower()

            if gate is None:
                gate = self.gate_selected
            if gate < 0:
                raise gcmd.error("Invalid gate")

            # Determine power
            if burst:
                default_power = self.mmu_unit().p.espooler_assist_burst_power if operation == ESPOOLER_ASSIST else self.mmu_unit().p.espooler_rewind_burst_power
            else:
                default_power = self.mmu_unit().p.espooler_printing_power if operation == ESPOOLER_PRINT else 50
            power = gcmd.get_int('POWER', default_power, minval=0, maxval=100) if operation != ESPOOLER_OFF else 0

            if burst:
                default_duration = self.mmu_unit().p.espooler_assist_burst_duration if operation == ESPOOLER_ASSIST else self.mmu_unit().p.espooler_rewind_burst_duration
                duration = gcmd.get_float('DURATION', default_duration, above=0., maxval=10.)

                if operation in [ESPOOLER_ASSIST, ESPOOLER_REWIND]:
                    self.log_info("Espooler burst on gate %d for %.1fs at %d%% power in %s direction" % (gate, duration, power, operation))
                    self.printer.send_event("mmu:espooler_burst", gate, power / 100., duration, operation)
                else:
                    self.log_error("Must specify 'assist' or 'rewind' operation for burst")

            elif operation not in ESPOOLER_OPERATIONS:
                raise gcmd.error("Invalid operation. Options are: %s" % ", ".join(ESPOOLER_OPERATIONS))

            elif operation == ESPOOLER_PRINT:
                if self.is_printing():
                    self.log_warning("Cannot set in-print assist mode for non selected gate while printing")
                else:
                    if gate != self.gate_selected:
                        self.log_warning("In-print assist mode set for non selected gate - for testing only")
                    self.espooler.set_operation(gate, power / 100, ESPOOLER_PRINT)

            elif operation != ESPOOLER_OFF:
                self.espooler.set_operation(gate, power / 100, operation)
            else:
                self.espooler.set_operation(gate, 0, ESPOOLER_OFF)

        if not quiet:
            msg = ""
            for gate in range(self.num_gates):
                if msg:
                    msg += "\n"
                msg += "{}".format(gate).ljust(2, UI_SPACE) + ": "
                if self.has_espooler():
                    operation, value = self.espooler.get_operation(gate)
                    burst = ""
                    if operation == ESPOOLER_PRINT and value == 0:
                        burst = " [assist for %.1fs at %d%% power " % (self.mmu_unit().p.espooler_assist_burst_duration, self.mmu_unit().p.espooler_assist_burst_power)
                        if self.mmu_unit().p.espooler_assist_burst_trigger:
                            burst += "on trigger, max %d bursts]" % self.mmu_unit().p.espooler_assist_burst_trigger_max
                        else:
                            burst += "every %.1fmm of extruder movement]" % self.mmu_unit().p.espooler_assist_extruder_move_length
                    msg += "{}".format(operation).ljust(7, UI_SPACE) + " (%d%%)%s" % (round(value * 100), burst)
                else:
                    msg += "not fitted"
            self.log_always(msg)


    cmd_MMU_RESET_help = "Forget persisted state and re-initialize defaults"
    def cmd_MMU_RESET(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        confirm = gcmd.get_int('CONFIRM', 0, minval=0, maxval=1)
        if confirm != 1:
            self.log_always("You must re-run and add 'CONFIRM=1' to reset all state back to default")
            return
        self.reinit()
        self._reset_statistics()
        self._reset_endless_spool()
        self._reset_ttg_map()
        self._reset_gate_map()
        self.var_manager.set(VARS_MMU_GATE_SELECTED, self.gate_selected)
        self.var_manager.set(VARS_MMU_TOOL_SELECTED, self.tool_selected)
        self.var_manager.set(VARS_MMU_FILAMENT_POS, self.filament_pos)
        self.var_manager.write()
        self.log_always("MMU state reset")
        self._schedule_mmu_bootup_tasks()


#########################################################
# STEP FILAMENT LOAD/UNLOAD MACROS FOR USER COMPOSITION #
#########################################################

    cmd_MMU_TEST_FORM_TIP_help = "Convenience macro for calling the standalone tip forming functionality (or cutter logic)"
    def cmd_MMU_TEST_FORM_TIP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        show = bool(gcmd.get_int('SHOW', 0, minval=0, maxval=1))
        run = bool(gcmd.get_int('RUN', 1, minval=0, maxval=1))
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print syncing and current

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, None)
        if gcode_macro is None:
            raise gcmd.error("Filament tip forming macro '%s' not found" % self.p.form_tip_macro)
        gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.p.form_tip_macro, gcode_macro)

        if reset:
            if self.form_tip_vars is not None:
                gcode_vars.variables = dict(self.form_tip_vars)
                self.form_tip_vars = None
                self.log_always("Reset '%s' macro variables to defaults" % self.p.form_tip_macro)
            show = True

        if show:
            msg = "Variable settings for macro '%s':" % self.p.form_tip_macro
            for k, v in gcode_vars.variables.items():
                msg += "\nvariable_%s: %s" % (k, v)
            self.log_always(msg)
            return

        # Save restore point on first call
        if self.form_tip_vars is None:
            self.form_tip_vars = dict(gcode_vars.variables)

        for param in gcmd.get_command_parameters():
            value = gcmd.get(param)
            param = param.lower()
            if param.startswith("variable_"):
                self.log_always("Removing 'variable_' prefix from '%s' - not necessary" % param)
                param = param[9:]
            if param in gcode_vars.variables:
                gcode_vars.variables[param] = self._fix_type(value)
            elif param not in ["reset", "show", "run", "force_in_print"]:
                self.log_error("Variable '%s' is not defined for '%s' macro" % (param, self.p.form_tip_macro))

        # Run the macro in test mode (final_eject is set)
        msg = "Running macro '%s' with the following variable settings:" % self.p.form_tip_macro
        for k, v in gcode_vars.variables.items():
            msg += "\nvariable_%s: %s" % (k, v)
        self.log_always(msg)

        try:
            with self.wrap_sync_gear_to_extruder():
                if run:
                    self._ensure_safe_extruder_temperature(wait=True)

                    # Ensure sync state and mimick in print if requested
                    self.reset_sync_gear_to_extruder(self.mmu_unit().p.sync_form_tip, force_in_print=force_in_print)

                    _,_,_ = self._do_form_tip(test=not self.is_in_print(force_in_print))
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED)

        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_TEST_PURGE_help = "Convenience macro for calling the standalone purging macro"
    def cmd_MMU_TEST_PURGE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        last_tool = gcmd.get_int('LAST_TOOL', self._last_tool, minval=0, maxval=self.num_gates - 1)
        next_tool = gcmd.get_int('NEXT_TOOL', self.tool_selected, minval=0, maxval=self.num_gates - 1)
        if next_tool < 0: next_tool = 0

        if not self.p.purge_macro:
            self.log_warning("Purge not possible because `purge_macro` is not defined")
            return

        try:
            # Determine purge volume for test (mimick regular call to purge macro)
            self.toolchange_purge_volume = self._calc_purge_volume(last_tool, next_tool)

            _last_tool, _next_tool = self._last_tool, self._next_tool
            self._last_tool, self._next_tool = last_tool, next_tool # Valid only during this test

            msg = "Note that the suggested purge volume is based on the current MMU_SLICER_TOOL_MAP"
            msg += "\nIf this is not set you might find it useful to run 'MMU_CALC_PURGE_VOLUMES MULTIPLIER=..'"
            msg += "\nto create a purge volume map from current filament colors. You can also specify"
            msg += "'LAST_TOOL=.. NEXT_TOOL=..' to this command to override currently loaded tool"
            self.log_info(msg)

            self.log_info("Calling purge macro '%s'" % self.p.purge_macro)
            with self.wrap_action(ACTION_PURGING):
                self.purge_standalone()

        except MmuError as ee:
            self.handle_mmu_error(str(ee))

        finally:
            self.toolchange_purge_volume = 0.
            self._last_tool, self._next_tool = _last_tool, _next_tool # Restore real values


    cmd_MMU_STEP_LOAD_GATE_help = "User composable loading step: Move filament from gate to start of bowden"
    def cmd_MMU_STEP_LOAD_GATE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            with self.wrap_sync_gear_to_extruder():
                self._load_gate()
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_GATE_help = "User composable unloading step: Move filament from start of bowden and park in the gate"
    cmd_MMU_STEP_UNLOAD_GATE_param_help = (
        "_MMU_STEP_UNLOAD_GATE: %s\n" % cmd_MMU_STEP_UNLOAD_GATE_help
        + "FULL   = [0|1]\n"
    )
    def cmd_MMU_STEP_UNLOAD_GATE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        help = bool(gcmd.get_int('HELP', 0, minval=0, maxval=1))
        full = bool(gcmd.get_int('FULL', 0, minval=0, maxval=1))

        if help:
            self.log_always(self.format_help(self.cmd_MMU_STEP_UNLOAD_GATE_param_help), color=True)
            return

        try:
            with self.wrap_sync_gear_to_extruder():
                _,_ = self._unload_gate(homing_max=self.calibration_manager.get_bowden_length() if full else None)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_LOAD_BOWDEN_help = "User composable loading step: Smart loading of bowden"
    def cmd_MMU_STEP_LOAD_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', None, minval=0.)
        start_pos = gcmd.get_float('START_POS', 0.)
        try:
            with self.wrap_sync_gear_to_extruder():
                _,_ = self._load_bowden(length, start_pos=start_pos)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_BOWDEN_help = "User composable unloading step: Smart unloading of bowden"
    def cmd_MMU_STEP_UNLOAD_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', self._get_bowden_length(self.gate_selected))
        try:
            with self.wrap_sync_gear_to_extruder():
                _ = self._unload_bowden(length)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_HOME_EXTRUDER_help = "User composable loading step: Home to extruder sensor or entrance through collision detection"
    def cmd_MMU_STEP_HOME_EXTRUDER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            with self.wrap_sync_gear_to_extruder():
                _,_ = self._home_to_extruder(self.mmu_unit().p.extruder_homing_max)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_HOME_EXTRUDER: %s" % str(ee))

    cmd_MMU_STEP_LOAD_TOOLHEAD_help = "User composable loading step: Toolhead loading"
    def cmd_MMU_STEP_LOAD_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        try:
            with self.wrap_sync_gear_to_extruder():
                _ = self._load_extruder(extruder_only)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_TOOLHEAD_help = "User composable unloading step: Toolhead unloading"
    def cmd_MMU_STEP_UNLOAD_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0))
        park_pos = gcmd.get_float('PARK_POS', -self._get_filament_position()) # +ve value
        try:
            with self.wrap_sync_gear_to_extruder():
                # Precautionary validation of filament position
                park_pos = min(self.p.toolhead_extruder_to_nozzle, max(0, park_pos))
                self._set_filament_position(-park_pos)
                self._unload_extruder(extruder_only = extruder_only)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_HOMING_MOVE_help = "User composable loading step: Generic homing move"
    def cmd_MMU_STEP_HOMING_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))
        try:
            with self.wrap_sync_gear_to_extruder():
                self._homing_move_cmd(gcmd, "User defined step homing move", allow_bypass=allow_bypass)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_HOMING_MOVE: %s" % str(ee))

    cmd_MMU_STEP_MOVE_help = "User composable loading step: Generic move"
    def cmd_MMU_STEP_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))
        try:
            with self.wrap_sync_gear_to_extruder():
                self._move_cmd(gcmd, "User defined step move", allow_bypass=allow_bypass)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_MOVE: %s" % str(ee))

    cmd_MMU_STEP_SET_FILAMENT_help = "User composable loading step: Set filament position state"
    def cmd_MMU_STEP_SET_FILAMENT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        state = gcmd.get_int('STATE', minval=FILAMENT_POS_UNKNOWN, maxval=FILAMENT_POS_LOADED)
        silent = gcmd.get_int('SILENT', 0)
        self._set_filament_pos_state(state, silent)

    cmd_MMU_STEP_SET_ACTION_help = "User composable loading step: Set action state"
    def cmd_MMU_STEP_SET_ACTION(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if gcmd.get_int('RESTORE', 0):
            if self._old_action is not None:
                self._set_action(self._old_action)
            self._old_action = None
        else:
            state = gcmd.get_int('STATE', minval=ACTION_IDLE, maxval=ACTION_PURGING)
            if self._old_action is None:
                self._old_action = self._set_action(state)
            else:
                self._set_action(state)


##############################################
# MODULAR FILAMENT LOAD AND UNLOAD FUNCTIONS #
##############################################

    # Preload selected gate as little as possible. If a full gate load is the only option
    # this will then park correctly after pre-load
    def _preload_gate(self):
        gate_sensor = self.sensor_manager.check_gate_sensor(SENSOR_GEAR_PREFIX, self.gate_selected)
        if gate_sensor is not None:
            if gate_sensor:
                self.log_always("Filament already preloaded")
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
                return
            else:
                # Minimal load to gear sensor if fitted
                endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_GEAR_PREFIX, self.gate_selected)
                self.log_always("Preloading...")
                msg = "Homing to %s sensor" % endstop_name
                with self._wrap_suspend_filament_monitoring():
                    actual,homed,measured,_ = self.trace_filament_move(msg, self.mmu_unit().p.gate_preload_homing_max, motor="gear", homing_move=1, endstop_name=endstop_name)
                    if homed:
                        self.trace_filament_move("Final parking", -self.mmu_unit().p.gate_preload_parking_distance)
                        self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
                        self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                        self.log_always("Filament detected and loaded in gate %d" % self.gate_selected)
                        return
        else:
            # Full gate load if no gear sensor
            for _ in range(self.mmu_unit().p.gate_preload_attempts):
                self.log_always("Loading...")
                try:
                    self._load_gate(allow_retry=False)
                    self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                    self.log_always("Parking...")
                    _,_ = self._unload_gate()
                    self.log_always("Filament detected and parked in gate %d" % self.gate_selected)
                    return
                except MmuError as ee:
                    # Exception just means filament is not loaded yet, so continue
                    self.log_trace("Exception on preload: %s" % str(ee))

        if self.sensor_manager.check_gate_sensor(SENSOR_PRE_GATE_PREFIX, self.gate_selected):
            self._set_gate_status(self.gate_selected, GATE_UNKNOWN)
            self.log_warning("Filament detected by pre-gate %d sensor but did not complete preload" % self.gate_selected)
        else:
            self._set_gate_status(self.gate_selected, GATE_EMPTY)
            raise MmuError("Filament not detected")

    # Eject final clear of gate. Important for MMU's where filament is always gripped (e.g. most type-B)
    def _eject_from_gate(self, gate=None):
        # If gate not specified assume current gate
        if gate is None:
            gate = self.gate_selected
        else:
            self.select_gate(gate)

        self.selector().filament_drive()

        self.log_always("Ejecting...")
        if self.sensor_manager.has_gate_sensor(SENSOR_GEAR_PREFIX, gate):
            endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_GEAR_PREFIX, gate)
            msg = "Reverse homing off %s sensor" % endstop_name
            actual,homed,measured,_ = self.trace_filament_move(msg, -self.mmu_unit().p.gate_homing_max, motor="gear", homing_move=-1, endstop_name=endstop_name)
            if homed:
                self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
            else:
                raise MmuError("Filament did not exit gate homing sensor: %s" % endstop_name)

        if self.mmu_unit().p.gate_final_eject_distance > 0:
            msg = "Ejecting filament out of gate"
            if self.sensor_manager.check_gate_sensor(SENSOR_PRE_GATE_PREFIX, gate) is not None:
                # Use homing move so we don't "over eject"
                self.trace_filament_move(msg, -self.mmu_unit().p.gate_final_eject_distance, motor="gear", homing_move=-1, endstop_name=SENSOR_PRE_GATE_PREFIX, wait=True)
            else:
                self.trace_filament_move(msg, -self.mmu_unit().p.gate_final_eject_distance, wait=True)

        self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True) # Should already be in this position
        self._set_gate_status(gate, GATE_EMPTY)
        self.log_always("The filament in gate %d can be removed" % gate)

    # Load filament into gate. This is considered the starting position for the rest of the filament loading
    # process. Note that this may overshoot the home position for the "encoder" technique but subsequent
    # bowden move will accommodate. Also for systems with gate sensor and encoder with gate sensor first,
    # there will be a gap in encoder readings that must be taken into consideration.
    # Return the overshoot past homing point
    def _load_gate(self, allow_retry=True):
        self._validate_gate_config("load")
        self._set_filament_direction(DIRECTION_LOAD)
        self.selector().filament_drive()
        retries = self.mmu_unit().p.gate_load_retries if allow_retry else 1

        if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER:
            with self._require_encoder():
                measured = 0.
                for i in range(retries):
                    msg = "Initial load into encoder" if i == 0 else ("Retry load into encoder (reetry #%d)" % i)
                    _,_,m,_ = self.trace_filament_move(msg, self.mmu_unit().p.gate_homing_max)
                    measured += m
                    if m > 6.0:
                        self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                        self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)
                        return measured
                    else:
                        self.log_debug("Error loading filament - filament motion was not detected by the encoder. %s" % ("Retrying..." if i < retries - 1 else ""))
                        if i < retries - 1:
                            self.selector().filament_release()
                            self.selector().filament_drive()

        else: # Gate sensor... SENSOR_GATE is shared, but SENSOR_GEAR_PREFIX is gate specific
            for i in range(retries):
                endstop_name = self.sensor_manager.get_mapped_endstop_name(self.mmu_unit().p.gate_homing_endstop)
                msg = ("Initial homing to %s sensor" % endstop_name) if i == 0 else ("Retry homing to gate sensor (retry #%d)" % i)
                h_dir = -1 if self.mmu_unit().p.gate_parking_distance < 0 and self.sensor_manager.check_sensor(endstop_name) else 1 # Reverse home?
                actual,homed,measured,_ = self.trace_filament_move(msg, h_dir * self.mmu_unit().p.gate_homing_max, motor="gear", homing_move=h_dir, endstop_name=endstop_name)
                if homed:
                    self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
                    self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    return 0.
                else:
                    self.log_debug("Error loading filament - filament did not reach gate homing sensor. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self.selector().filament_release()
                        self.selector().filament_drive()

        self._set_gate_status(self.gate_selected, GATE_EMPTY)
        self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
        msg = "Couldn't pick up filament at gate"
        if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER:
            msg += " (encoder didn't report enough movement)"
        else:
            msg += " (gate endstop didn't trigger)"
        msg += "\nGate marked as empty. Use 'MMU_GATE_MAP GATE=%d AVAILABLE=1' to reset" % self.gate_selected
        raise MmuError(msg)

    # Unload filament through gate to final MMU park position.
    # Strategies include use of encoder or homing to gate/gear endstop and then parking
    # Allows the overriding of homing_max for slow unloads when we are unsure of filament position
    # Returns the amount of homing performed to aid calibration
    def _unload_gate(self, homing_max=None):
        self._validate_gate_config("unload")
        self._set_filament_direction(DIRECTION_UNLOAD)
        self.selector().filament_drive()
# PAUL old        full = homing_max == self._get_bowden_length(self.gate_selected)
        full = homing_max == self.calibration_manager.get_bowden_length()
        homing_max = homing_max or self.mmu_unit().p.gate_homing_max

        if full: # Means recovery operation
            # Safety step because this method is used as a defensive way to unload the entire bowden from unknown position
            # It handles the cases of filament still in extruder with no toolhead sensor or, if toolhead sensor is available,
            # the small window where filament is between extruder entrance and toolhead sensor
            homing_max += self.mmu_unit().p.gate_homing_max # Full bowden may not be quite enough
            length = self.p.toolhead_extruder_to_nozzle
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                length -= self.p.toolhead_sensor_to_nozzle # Can safely reduce the base move distance because starting point in toolhead sensor
            length += self.mmu_unit().p.toolhead_unload_safety_margin # Add safety margin

            self.log_debug("Performing synced pre-unload bowden move of %.1fmm to ensure filament is not trapped in extruder" % length)

            if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER:
                _,_,_,_ = self.trace_filament_move("Bowden safety pre-unload move", -length, motor="gear+extruder")
            else:
                endstop_name = self.sensor_manager.get_mapped_endstop_name(self.mmu_unit().p.gate_homing_endstop)
                actual,homed,_,_ = self.trace_filament_move("Bowden safety pre-unload move", -length, motor="gear+extruder", homing_move=-1, endstop_name=endstop_name)
                # In case we ended up homing during the safety pre-unload, lets just do our parking and be done
                # This can easily happen when your parking distance is configured to park the filament past the
                # gate sensor instead of behind the gate sensor and the filament position is determined to be
                # "somewhere in the bowden tube"
                if homed:
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    self.trace_filament_move("Final parking", -self.mmu_unit().p.gate_parking_distance)
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return actual, self.gate_unload_buffer

        if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER:
            with self._require_encoder():
                if full:
                    self.log_info("Slowly unloading bowden because unsure of filament position...")
                else:
                    self.log_trace("Unloading gate using the encoder")
                success = self._reverse_home_to_encoder(homing_max)
                if success:
                    actual,park,_ = success
                    _,_,measured,_ = self.trace_filament_move("Final parking", -park)
                    # We don't expect any movement of the encoder unless it is free-spinning
                    if measured > self.encoder().movement_min(): # We expect 0, but relax the test a little (allow one pulse)
                        self.log_warning("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return actual, self.gate_unload_buffer
                msg = "did not clear the encoder after moving %.1fmm" % homing_max

        else: # Using mmu_gate or mmu_gear_N sensor
            endstop_name = self.sensor_manager.get_mapped_endstop_name(self.mmu_unit().p.gate_homing_endstop)
            actual,homed,_,_ = self.trace_filament_move("Reverse homing off %s sensor" % endstop_name, -homing_max, motor="gear", homing_move=-1, endstop_name=endstop_name)
            if homed:
                self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                self.trace_filament_move("Final parking", -self.mmu_unit().p.gate_parking_distance)
                self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                return actual, self.gate_unload_buffer
            msg = "did not home to sensor '%s' after moving %1.fmm" % (self.mmu_unit().p.gate_homing_endstop, homing_max)

        raise MmuError("Failed to unload gate because %s" % msg)

    # Shared with manual bowden calibration routine
    def _reverse_home_to_encoder(self, homing_max):
        max_steps = int(math.ceil(homing_max / self.p.encoder_move_step_size))
        delta = 0.
        actual = 0.
        for i in range(max_steps):
            msg = "Unloading step #%d from encoder" % (i+1)
            sactual,_,_,sdelta = self.trace_filament_move(msg, -self.p.encoder_move_step_size)
            delta += sdelta
            actual -= sactual
            # Large enough delta here means we are out of the encoder
            if sdelta >= self.p.encoder_move_step_size * 0.2: # 20 %
                actual -= sdelta
                park = self.mmu_unit().p.gate_parking_distance - sdelta # will be between 8 and 20mm (for 23mm gate_parking_distance, 15mm step)
                return actual, park, delta
        self.log_debug("Filament did not clear encoder even after moving %.1fmm" % (self.p.encoder_move_step_size * max_steps))
        return None

    # Shared gate functions to deduplicate logic
    def _validate_gate_config(self, direction):
        if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER:
            if not self.has_encoder():
                raise MmuError("Attempting to %s encoder but encoder is not configured on MMU!" % direction)
        elif self.mmu_unit().p.gate_homing_endstop in GATE_ENDSTOPS:
            sensor = self.mmu_unit().p.gate_homing_endstop
            if self.mmu_unit().p.gate_homing_endstop == SENSOR_GEAR_PREFIX:
                sensor += "_%d" % self.gate_selected
            if not self.sensor_manager.has_sensor(sensor):
                raise MmuError("Attempting to %s gate but sensor '%s' is not configured on MMU!" % (direction, sensor))
        else:
            raise MmuError("Unsupported gate endstop %s" % self.mmu_unit().p.gate_homing_endstop)

    # Fast load of filament in bowden, usually the full length but if 'full' is False a specific length can be specified
    # Note that filament position will be measured from the gate "parking position" and so will be the gate_parking_distance
    # plus any overshoot. The start of the bowden move is from the parking homing point.
    # Returns ratio of measured movement to real movement IF it is "clean" and could be used for auto-calibration else 0
    def _load_bowden(self, length=None, start_pos=0.):
        bowden_length = self.calibration_manager.get_bowden_length()
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Compensate for distance already moved for gate homing endstop (e.g. overshoot after encoder based gate homing)
        length -= start_pos

        try:
            # Do we need to reduce by buffer amount to ensure we don't overshoot homing sensor
            deficit = 0.
            if full:
                if self._must_buffer_extruder_homing():
                    deficit = self.extruder_homing_buffer
                    # Further reduce to compensate for distance from extruder sensor to extruder entry gear
                    deficit -= self.p.toolhead_entry_to_extruder if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY else 0
                length -= deficit # Reduce fast move distance

            if length > 0:
                self.log_debug("Loading bowden tube")
                self._set_filament_direction(DIRECTION_LOAD)
                self.selector().filament_drive()
                self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = (self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()) - start_pos

                if self.gate_selected > 0 and self.rotation_distances[self.gate_selected] <= 0:
                    self.log_warning("Warning: gate %d not calibrated! Using default rotation distance" % self.gate_selected)

                # "Fast" load
                _,_,_,delta = self.trace_filament_move("Fast loading move through bowden", length, track=True, encoder_dwell=bool(self.mmu_unit().p.autotune_rotation_distance))
                delta -= self._get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self._can_use_encoder() and delta >= length * (self.bowden_move_error_tolerance / 100.) and not self.calibrating:
                    raise MmuError("Failed to load bowden. Perhaps filament is stuck in gate. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                # Encoder based validation test
                if self._can_use_encoder() and delta >= self.mmu_unit().p.bowden_allowable_load_delta and not self.calibrating:
                    ratio = 0. # Not considered valid for auto-calibration
                    # Correction attempts to load the filament according to encoder reporting
                    if self.mmu_unit().p.bowden_apply_correction:
                        for i in range(2):
                            if delta >= self.mmu_unit().p.bowden_allowable_load_delta:
                                msg = "Correction load move #%d into bowden" % (i+1)
                                _,_,_,d = self.trace_filament_move(msg, delta, track=True)
                                delta = d
                                self.log_debug("Correction load move was necessary, encoder now measures %.1fmm" % self.get_encoder_distance())
                            else:
                                self.log_debug("Correction load complete, delta %.1fmm is less than 'bowden_allowable_unload_delta' (%.1fmm)" % (delta, self.mmu_unit().p.bowden_allowable_load_delta))
                                break
                        self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                        if delta >= self.mmu_unit().p.bowden_allowable_load_delta:
                            self.log_warning("Warning: Excess slippage was detected in bowden tube load afer correction moves. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details"% (length, length - delta))
                    else:
                        self.log_warning("Warning: Excess slippage was detected in bowden tube load but 'bowden_apply_correction' is disabled. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details" % (length, length - delta))

                    if delta >= self.mmu_unit().p.bowden_allowable_load_delta:
                        self.log_debug("Possible causes of slippage:\nCalibration ref length too long (hitting extruder gear before homing)\nCalibration ratio for gate is not accurate\nMMU gears are not properly gripping filament\nEncoder reading is inaccurate\nFaulty servo")

                self._random_failure() # Testing
                self.movequeues_wait()
            else:
                # No bowden movement required
                ratio = 1.

            if full:
                self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)
            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = 0.
            return ratio, deficit # For auto-calibration
        finally:
            self.bowden_start_pos = None

    # Fast unload of filament from exit of extruder gear (end of bowden) to position close to MMU (gate_unload_buffer away)
    def _unload_bowden(self, length=None):
        bowden_length = self.calibration_manager.get_bowden_length()
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Shorten move by gate buffer used to ensure we don't overshoot homing point
        length -= self.gate_unload_buffer

        try:
            if length > 0:
                self.log_debug("Unloading bowden tube")
                self._set_filament_direction(DIRECTION_UNLOAD)
                self.selector().filament_drive()

                # Optional pre-unload safety step
                if (full and self.has_encoder() and self.mmu_unit().p.bowden_pre_unload_test and
                    self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY) is not False and
                    self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is not False
                ):
                    with self._require_encoder():
                        self.log_debug("Performing bowden pre-unload test")
                        _,_,_,delta = self.trace_filament_move("Bowden pre-unload test", -self.p.encoder_move_step_size)
                        if delta > self.p.encoder_move_step_size * (self.mmu_unit().p.bowden_pre_unload_error_tolerance / 100.):
                            self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
                            raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder or filament not loaded\nOptionally use MMU_RECOVER to recover filament position")
                        length -= self.p.encoder_move_step_size
                        self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()

                # Sensor validation
                if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is False:
                    sensors = self.sensor_manager.get_all_sensors()
                    sensor_msg = ''
                    sname = []
                    for name, state in sensors.items():
                        sensor_msg += "%s (%s), " % (name.upper(), "Disabled" if state is None else ("Detected" if state is True else "Empty"))
                        if state is False:
                            sname.append(name)
                    self.log_warning("Warning: Possible sensor malfunction - %s sensor indicated no filament present prior to unloading bowden\nWill ignore and attempt to continue..." % ", ".join(sname))
                    self.log_debug("Sensor state: %s" % sensor_msg)

                # "Fast" unload
                _,_,_,delta = self.trace_filament_move("Fast unloading move through bowden", -length, track=True, encoder_dwell=bool(self.mmu_unit().p.autotune_rotation_distance))
                delta -= self._get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self._can_use_encoder() and delta >= self.bowden_allowable_unload_delta and not self.calibrating:
                    ratio = 0.
                    # Only a warning because _unload_gate() will deal with it
                    self.log_warning("Warning: Excess slippage was detected in bowden tube unload. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                self._random_failure() # Testing
                self.movequeues_wait()
            else:
                # No bowden movement required
                ratio = 1.

            if full:
                self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)
            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = 0.
            return ratio # For auto-calibration

        finally:
            self.bowden_start_pos = None

    # Optionally home filament to designated homing location at the extruder
    # Returns any homing distance and extra movement for automatic calibration logic
    #         or None if not applicable
    def _home_to_extruder(self, max_length):
        self._set_filament_direction(DIRECTION_LOAD)
        self.selector().filament_drive()
        measured = extra = 0.
        homing_movement = None

        if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_NONE:
            homed = True

        elif self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_COLLISION:
            if self.has_encoder():
                actual,homed,measured,_ = self._home_to_extruder_collision_detection(max_length)
                homing_movement = actual
            else:
                raise MmuError("Cannot home to extruder using 'collision' method because encoder is not configured or disabled!")

        else:
            self.log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (self.mmu_unit().p.extruder_homing_endstop, max_length))
            actual,homed,measured,_ = self.trace_filament_move("Homing filament to extruder endstop", max_length, motor="gear", homing_move=1, endstop_name=self.mmu_unit().p.extruder_homing_endstop)
            if homed:
                self.log_debug("Extruder endstop '%s' reached after %.1fmm (measured %.1fmm)" % (self.mmu_unit().p.extruder_homing_endstop, actual, measured))
                self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)

                # Make adjustment based on sensor: extruder - move a little move, compression - back off a little
                if self.mmu_unit().p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                    extra = self.p.toolhead_entry_to_extruder
                    _,_,measured,_ = self.trace_filament_move("Aligning filament to extruder gear", extra, motor="gear")
                elif self.mmu_unit().p.extruder_homing_endstop == SENSOR_COMPRESSION:
                    # We don't actually back off because the buffer absorbs the overrun but we still report for calibration
                    extra = -(self.mmu_unit().sync_feedback.p.sync_feedback_buffer_range / 2.)

            homing_movement = actual

        if not homed:
            self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder '%s' endstop after moving %.1fmm" % (self.mmu_unit().p.extruder_homing_endstop, max_length))

        if measured > (max_length * 0.8):
            self.log_warning("Warning: 80%% of 'extruder_homing_max' was used homing. You may want to increase 'extruder_homing_max'" % VARS_MMU_CALIB_BOWDEN_LENGTH)

        self._set_filament_pos_state(FILAMENT_POS_HOMED_EXTRUDER)
        return homing_movement, extra

    # Special extruder homing option for detecting the collision base on lack of encoder movement
    def _home_to_extruder_collision_detection(self, max_length):
        # Lock the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
        ge = stepper_enable.lookup_enable(extruder_stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        step = self.mmu_unit().p.extruder_collision_homing_step * math.ceil(self.encoder().get_resolution() * 10) / 10
        self.log_debug("Homing to extruder gear, up to %.1fmm in %.1fmm steps" % (max_length, step))

        with self.wrap_gear_current(self.mmu_unit().p.extruder_collision_homing_current, "for collision detection"):
            homed = False
            measured = delta = 0.
            i = 0
            for i in range(int(max_length / step)):
                msg = "Homing step #%d" % (i+1)
                _,_,smeasured,sdelta = self.trace_filament_move(msg, step, speed=self.mmu_unit().p.gear_homing_speed)
                measured += smeasured
                delta += sdelta
                if sdelta >= self.encoder().movement_min() or abs(delta) > step: # Not enough or strange measured movement means we've hit the extruder
                    homed = True
                    measured -= step # Subtract the last step to improve accuracy
                    break
            self.log_debug("Extruder entrance%s found after %.1fmm move (%d steps), encoder measured %.1fmm (delta %.1fmm)"
                    % (" not" if not homed else "", step*(i+1), i+1, measured, delta))

        if delta > 5.0:
            self.log_warning("Warning: A lot of slippage was detected whilst homing to extruder, you may want to reduce 'extruder_collision_homing_current' and/or ensure a good grip on filament by gear drive")

        self._set_filament_position(self._get_filament_position() - step) # Ignore last step movement
        return step*i, homed, measured, delta

    # Move filament from the extruder gears (entrance) to the nozzle
    # Returns any homing distance for automatic calibration logic
    def _load_extruder(self, extruder_only=False):
        with self.wrap_action(ACTION_LOADING_EXTRUDER):
            self.log_debug("Loading filament into extruder")
            self._set_filament_direction(DIRECTION_LOAD)

            # Important to wait for filaments with wildy different print temps. In practice, the time taken
            # to perform a swap should be adequate to reach the target temp but better safe than sorry
            self._ensure_safe_extruder_temperature(wait=True)
            homing_movement = None

            has_tension = self.sensor_manager.has_sensor(SENSOR_TENSION)
            has_compression = self.sensor_manager.has_sensor(SENSOR_COMPRESSION)
            has_proportional = self.sensor_manager.has_sensor(SENSOR_PROPORTIONAL)
            has_toolhead = self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)

            synced = not extruder_only
            if synced:
                self.selector().filament_drive()
                speed = self.p.extruder_sync_load_speed
                motor = "gear+extruder"
            else:
                self.selector().filament_release()
                speed = self.p.extruder_load_speed
                motor = "extruder"

            fhomed = False
            if has_toolhead:
                # With toolhead sensor for accuracy we always first home to toolhead sensor past the extruder entrance
                # The remaining load distance is relative to the toolhead sensor
                if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")
                self.log_debug("Homing up to %.1fmm to toolhead sensor%s" % (self.mmu_unit().p.toolhead_homing_max, (" (synced)" if synced else "")))
                actual,fhomed,measured,_ = self.trace_filament_move("Homing to toolhead sensor", self.mmu_unit().p.toolhead_homing_max, motor=motor, homing_move=1, endstop_name=SENSOR_TOOLHEAD)
                if fhomed:
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                    homing_movement = max(actual - (self.p.toolhead_extruder_to_nozzle - self.p.toolhead_sensor_to_nozzle), 0)
                else:
                    if self.gate_selected != TOOL_GATE_BYPASS:
                        self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    else:
                        # For bypass its best to assume we didn't enter the extruder at all
                        self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.mmu_unit().p.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure reduction is used only one time
            d = self.p.toolhead_sensor_to_nozzle if has_toolhead else self.p.toolhead_extruder_to_nozzle
            length = max(d - self.filament_remaining - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract, 0)

            # If we have a compression sensor indicating compression we can detect failure in the critical extruder entrance transition
            # by performing the initial load with just the extruder motor and checking that the sensor un-triggers before continuing
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and self.mmu_unit().p.toolhead_entry_tension_test
                and synced
                and not has_toolhead
                and self.sensor_manager.check_sensor(SENSOR_COMPRESSION)
            ):
                max_range = self.mmu_unit().sync_feedback.p.sync_feedback_buffer_maxrange * 2 # Arbitary but buffer_maxrange is not enough to overcome bowden slack
                if length > max_range:
                    self.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_range)
                    actual,success = self.mmu_unit().sync_feedback.adjust_filament_tension(use_gear_motor=False, max_move=max_range)
                    if success:
                        length -= actual
                    else:
                        self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                        raise MmuError("Failed to load filament passed the extruder entrance (sync-feedback buffer didn't detect neutral tension)")

            self.log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self.trace_filament_move("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)
            self._set_filament_remaining(0.)

            # Encoder based validation test to validate the filament was picked up by extruder. This runs if we are
            # short of deterministic sensors and test makes sense
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and self._can_use_encoder()
                and not fhomed
                and not extruder_only
            ):
                self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                if measured < self.encoder().movement_min():
                    raise MmuError("Move to nozzle failed (encoder didn't sense any movement). Extruder may not have picked up filament or filament did not find homing sensor")
                elif delta > length * (self.mmu_unit().p.toolhead_move_error_tolerance / 100.):
                    self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
                    raise MmuError("Move to nozzle failed (encoder didn't sense sufficient movement). Extruder may not have picked up filament or filament did not find homing sensor")

            # Make post load filament tension adjustments for reliability. If encoder is fitted, the "post_load_tighten"
            # will aid reliability in subsequent clog detection (and takes prescedence), else "post_load_tention_adjust" will try
            # to neutralize the filament tension. Don't run on bypass gate.
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and not extruder_only
            ):
                if (
                    self.mmu_unit().p.toolhead_post_load_tighten
                    and not self.mmu_unit().p.sync_to_extruder
                    and self._can_use_encoder()
                    and self.mmu_unit().sync_feedback.p.flowguard_encoder_mode
                ):
                    # Tightening move to prevent erroneous encoder clog detection/runout if gear stepper is not synced with extruder
                    with self.wrap_gear_current(percent=50, reason="to tighten filament in bowden"):
                        # Filament will already be gripped so perform fixed MMU only retract
# MOGGIE encoder_sensor
                        pullback = min(self.encoder_sensor.get_clog_detection_length() * self.mmu_unit().p.toolhead_post_load_tighten / 100, 15) # % of current clog detection length or 15mm min
                        _,_,measured,delta = self.trace_filament_move("Tighening filament in bowden", -pullback, motor="gear", wait=True)
                        self.log_info("Filament tightened by %.1fmm to prevent false clog detection" % pullback)

                elif (
                    self.mmu_unit().p.toolhead_post_load_tension_adjust
                    and (self.mmu_unit().p.sync_to_extruder or self.mmu_unit().p.sync_purge)
                    and (has_tension or has_compression or has_proportional)
                    and self.mmu_unit().sync_feedback.is_enabled()
                ):
                    # Try to put filament in neutral tension by centering between sensors
                    # Two methods are available based on switch only sensors or proportional feedback
                    actual,success = self.mmu_unit().sync_feedback.adjust_filament_tension()
                    if success:
                        self.log_info("Filament tension in bowden successfully relaxed")
                    else:
                        self.log_warning("Unsuccessful in relaxing filament tension after adjusting %.1fmm" % actual)

            self._random_failure() # Testing
            self.movequeues_wait()
            self._set_filament_pos_state(FILAMENT_POS_LOADED)
            self.log_debug("Filament should be loaded to nozzle")
            return homing_movement # Will only have value if we have toolhead sensor

    # Extract filament past extruder gear (to end of bowden). Assume that tip has already been formed
    # and we are parked somewhere in the extruder either by slicer or by stand alone tip creation
    # But be careful:
    #   A poor tip forming routine or slicer could have popped the filament out of the extruder already
    # Ending point is either the exit of the extruder or at the extruder (entry) endstop if fitted
    # Return True if we were synced
    def _unload_extruder(self, extruder_only=False, validate=True):
        with self.wrap_action(ACTION_UNLOADING_EXTRUDER):
            self.log_debug("Extracting filament from extruder")
            self._set_filament_direction(DIRECTION_UNLOAD)

            self._ensure_safe_extruder_temperature(wait=False)

            synced = self.selector().get_filament_grip_state() == FILAMENT_DRIVE_STATE and not extruder_only
            if synced:
                self.selector().filament_drive()
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                self.selector().filament_release()
                speed = self.p.extruder_unload_speed
                motor = "extruder"

            fhomed = False
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY) and not extruder_only:
                # BEST Strategy: Extruder exit movement leveraging extruder entry sensor. Must be synced
                synced = True
                self.selector().filament_drive()
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"

                if not self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY):
                    if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                        raise MmuError("Toolhead or extruder sensor failure. Extruder sensor reports no filament but toolhead sensor is still triggered")
                    else:
                        self.log_warning("Warning: Filament was not detected by extruder (entry) sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                else:
                    hlength = self.p.toolhead_extruder_to_nozzle + self.p.toolhead_entry_to_extruder + self.mmu_unit().p.toolhead_unload_safety_margin - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract
                    self.log_debug("Reverse homing up to %.1fmm off extruder sensor (synced) to exit extruder" % hlength)
                    _,fhomed,_,_ = self.trace_filament_move("Reverse homing off extruder sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_EXTRUDER_ENTRY)

                if not fhomed:
                    raise MmuError("Failed to reach extruder entry sensor after moving %.1fmm" % hlength)
                else:
                    validate = False
                    # We know exactly where end of filament is so true up
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)
                    self._set_filament_position(-(self.p.toolhead_extruder_to_nozzle + self.p.toolhead_entry_to_extruder))

                # TODO There have been reports of this failing, perhaps because of klipper's late update of sensor state? Maybe query_endstop instead
                #      So former MmuError() has been changed to error message
                if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                    self.log_warning("Warning: Toolhead sensor still reports filament is present in toolhead! Possible sensor malfunction\nWill attempt to continue...")

            else:
                if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                    # NEXT BEST: With toolhead sensor we first home to toolhead sensor. Optionally synced
                    if not self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                        self.log_warning("Warning: Filament was not detected in extruder by toolhead sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                    else:
                        hlength = self.p.toolhead_sensor_to_nozzle + self.mmu_unit().p.toolhead_unload_safety_margin - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract
                        self.log_debug("Reverse homing up to %.1fmm off toolhead sensor%s" % (hlength, (" (synced)" if synced else "")))
                        _,fhomed,_,_ = self.trace_filament_move("Reverse homing off toolhead sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_TOOLHEAD)
                    if not fhomed:
                        raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % hlength)
                    else:
                        validate = False
                        # We know exactly where end of filament is so true up
                        self._set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                        self._set_filament_position(-self.p.toolhead_sensor_to_nozzle)

                # Finish up with regular extruder exit movement. Optionally synced
                length = max(0, self.p.toolhead_extruder_to_nozzle + self._get_filament_position()) + self.mmu_unit().p.toolhead_unload_safety_margin
                self.log_debug("Unloading last %.1fmm to exit the extruder%s" % (length, " (synced)" if synced else ""))
                _,_,measured,delta = self.trace_filament_move("Unloading extruder", -length, speed=speed, motor=motor, wait=True)

                # Best guess of filament position is right at extruder entrance or just beyond if synced
                if synced:
                    self._set_filament_position(-(self.p.toolhead_extruder_to_nozzle + self.mmu_unit().p.toolhead_unload_safety_margin))
                else:
                    self._set_filament_position(-self.p.toolhead_extruder_to_nozzle)

                # Encoder based validation test if it has high chance of being useful
                # NOTE: This check which used to raise MmuError() is triping many folks up because they have poor tip forming
                #       logic so just log error and continue. This disguises the root cause problem but will make folks happier
                #       Not performed for slicer tip forming (validate=True) because everybody is ejecting the filament!
                if validate and self._can_use_encoder() and length > self.p.encoder_move_step_size and not extruder_only and self.gate_selected != TOOL_GATE_BYPASS:
                    self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                    msg = None
                    if measured < self.encoder().movement_min:
                        msg = "any"
                    elif synced and delta > length * (self.mmu_unit().p.toolhead_move_error_tolerance / 100.):
                        msg = "suffient"
                    if msg:
                        self.log_warning("Warning: Encoder not sensing %s movement during final extruder retraction move\nConcluding filament either stuck in the extruder, tip forming erroneously completely ejected filament or filament was not fully loaded\nWill attempt to continue..." % msg)

                self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)

            self._random_failure() # Testing
            self.movequeues_wait()
            self.log_debug("Filament should be out of extruder")
            return synced


##############################################
# LOAD / UNLOAD SEQUENCES AND FILAMENT TESTS #
##############################################

    def load_sequence(self, bowden_move=None, skip_extruder=False, purge=None, extruder_only=False):
        self.movequeues_wait()

        bowden_length = self.calibration_manager.get_bowden_length() # -1 if not calibrated
        if bowden_move is None:
            bowden_move = bowden_length

        if bowden_move > bowden_length and bowden_length >= 0:
            bowden_move = bowden_length
            self.log_warning("Warning: Restricting bowden load length to calibrated value of %.1fmm" % bowden_length)

        full = bowden_move == bowden_length
        calibrating = bowden_length < 0 and not extruder_only
        macros_and_track = not extruder_only and full

        self._set_filament_direction(DIRECTION_LOAD)
        self._initialize_filament_position(dwell=None)

        try:
            home = False
            if not extruder_only:
                current_action = self._set_action(ACTION_LOADING)
                if full:
                    home = self._must_home_to_extruder() or calibrating
                else:
                    skip_extruder = True

            if macros_and_track:
                self._track_time_start('load')
                # PRE_LOAD user defined macro
                with self._wrap_track_time('pre_load'):
                    self.wrap_gcode_command(self.p.pre_load_macro, exception=True, wait=True)

            self.log_info("Loading %s..." % ("extruder" if extruder_only else "filament"))
            if not extruder_only:
                self._display_visual_state()

            homing_movement = None # Track how much homing is done for calibrated bowden length optimization
            deficit = 0.           # Amount of homing that would be expected (because bowden load is shortened)
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
            overshoot = 0.
            calibrated_bowden_length = None
            start_filament_pos = self.filament_pos

            # Note: Conditionals deliberately coded this way to match macro alternative
            if self.p.gcode_load_sequence and not calibrating:
                self.log_debug("Calling external user defined loading sequence macro")
                self.wrap_gcode_command("%s FILAMENT_POS=%d LENGTH=%.1f FULL=%d HOME_EXTRUDER=%d SKIP_EXTRUDER=%d EXTRUDER_ONLY=%d" % (self.p.load_sequence_macro, start_filament_pos, bowden_move, int(full), int(home), int(skip_extruder), int(extruder_only)), exception=True)

            elif extruder_only:
                if start_filament_pos < FILAMENT_POS_EXTRUDER_ENTRY:
                    _ = self._load_extruder(extruder_only=True)
                else:
                    raise MmuError("Cannot load extruder because filament already in extruder (state: %s). Unload first" % start_filament_pos)

            elif start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                raise MmuError("Cannot load because filament already in extruder (state: %s). Unload first" % start_filament_pos)

            else:
                if start_filament_pos <= FILAMENT_POS_UNLOADED:
                    overshoot = self._load_gate()

                if calibrating:
                    if self.mmu_unit().p.extruder_homing_endstop in [SENSOR_EXTRUDER_NONE, SENSOR_EXTRUDER_COLLISION]:
                        raise MmuError("Auto calibration is not possible with 'extruder_homing_endstop: %s'" % SENSOR_EXTRUDER_NONE)

                    self.log_warning("Auto calibrating bowden length on gate %d using %s as gate reference point" % (self.gate_selected, self._gate_homing_string()))
                    if self.sensor_manager.check_sensor(self.mmu_unit().p.extruder_homing_endstop):
                        raise MmuError("The %s sensor triggered before homing. Check filament and sensor operation" % self.mmu_unit().p.extruder_homing_endstop)

                    hm, extra = self._home_to_extruder(self.mmu_unit().p.bowden_homing_max)
                    if hm is None:
                        raise MmuError("Failed to auto calibrate bowden because unable to home to extruder after moving %.1fmm" % self.mmu_unit().p.bowden_homing_max)

                    calibrated_bowden_length = overshoot + hm + extra
                else:
                    if start_filament_pos < FILAMENT_POS_END_BOWDEN:
                        bowden_move_ratio, deficit = self._load_bowden(bowden_move, start_pos=overshoot)

                    if start_filament_pos < FILAMENT_POS_HOMED_EXTRUDER and home:
                        hm, _ = self._home_to_extruder(self.mmu_unit().p.extruder_homing_max)
                        if hm is not None:
                            homing_movement = (homing_movement or 0) + hm

                if not skip_extruder:
                    hm = self._load_extruder()
                    if hm is not None:
                        homing_movement = (homing_movement or 0) + hm

            self.movequeues_wait()
            msg = "Load of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = self.mmu_unit().p.gate_parking_distance + self._get_encoder_dead_space()
                msg += " {1}(adjusted encoder: %.1fmm){0}" % (final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # Notify manager if calibrating/autotuning
            if calibrating:
                self.calibration_manager.update_bowden_calibration(calibrated_bowden_length)
            elif full and not extruder_only and not self.p.gcode_load_sequence:
                self.calibration_manager.load_telemetry(bowden_move_ratio, homing_movement, deficit)

            # Activate loaded spool in Spoolman
            self._spoolman_activate_spool(self.gate_spool_id[self.gate_selected])

            # Deal with purging
            if purge == PURGE_SLICER and not skip_extruder:
                self.log_debug("Purging expected to be performed by slicer")

            elif purge == PURGE_STANDALONE and not skip_extruder:
                with self._wrap_track_time('purge'):

                    # Restore the expected sync state now before running this macro
                    # (we also must force correction of filament grip for old blobifer/unsynced functionality)
                    self.reset_sync_gear_to_extruder(not extruder_only and self.mmu_unit().p.sync_purge, force_grip=True)

                    with self.wrap_action(ACTION_PURGING):
                        self.purge_standalone()

            # POST_LOAD user defined macro
            if macros_and_track:
                with self._wrap_track_time('post_load'):

                    # Restore the expected sync state now before running this macro
                    self.reset_sync_gear_to_extruder(not extruder_only and self.mmu_unit().p.sync_purge)

                    if self.has_blobifier: # Legacy blobifer integration. purge_macro now preferred
                        with self.wrap_action(ACTION_PURGING):
                            self.wrap_gcode_command(self.p.post_load_macro, exception=True, wait=True)
                    else:
                        self.wrap_gcode_command(self.p.post_load_macro, exception=True, wait=True)

        except MmuError as ee:
            self._track_gate_statistics('load_failures', self.gate_selected)
            raise MmuError("Load sequence failed because:\n%s" % (str(ee)))

        finally:
            self._track_gate_statistics('loads', self.gate_selected)

            if not extruder_only:
                self._set_action(current_action)

            if macros_and_track:
                self._track_time_end('load')

    def unload_sequence(self, bowden_move=None, check_state=False, form_tip=None, extruder_only=False):
        self.movequeues_wait()

        bowden_length = self.calibration_manager.get_bowden_length() # -1 if not calibrated yet
        if bowden_length < 0:
            bowden_length = self.mmu_unit().p.bowden_homing_max # Special case - if not calibrated then apply the max possible bowden length

        if bowden_move is None:
            bowden_move = bowden_length

        if bowden_move > bowden_length and bowden_length >= 0:
            bowden_move = bowden_length
            self.log_warning("Warning: Restricting bowden unload length to calibrated value of %.1fmm" % bowden_length)

        calibrated = bowden_move >= 0
        full = bowden_move == bowden_length
        macros_and_track = not extruder_only and full
        runout = self.is_handling_runout

        self._set_filament_direction(DIRECTION_UNLOAD)
        self._initialize_filament_position(dwell=None)

        if check_state or self.filament_pos == FILAMENT_POS_UNKNOWN:
            # Let's determine where filament is and reset state before continuing
            self.recover_filament_pos(message=True)

        if self.filament_pos == FILAMENT_POS_UNLOADED:
            self.log_debug("Filament already ejected")
            return

        try:
            if not extruder_only:
                current_action = self._set_action(ACTION_UNLOADING)

            # Deactivate spool immediately before tip forming/cutting
            # Tip forming/cutting macros use the extruder to execute, hence
            # any retraction / de retraction moves are accounted in Spoolman.
            # By de-activating early, the retraction performed from the macro
            # is deliberately not accounted in spoolman
            self._spoolman_activate_spool(0)

            # Run PRE_UNLOAD user defined macro
            if macros_and_track:
                self._track_time_start('unload')
                with self._wrap_track_time('pre_unload'):
                    self.wrap_gcode_command(self.p.pre_unload_macro, exception=True, wait=True)

            self.log_info("Unloading %s..." % ("extruder" if extruder_only else "filament"))
            if not extruder_only:
                self._display_visual_state()

            synced_extruder_unload = False
            park_pos = 0.
            do_form_tip = form_tip if form_tip is not None else FORM_TIP_STANDALONE # Default to standalone
            if do_form_tip == FORM_TIP_SLICER:
                # Slicer was responsible for the tip, but the user must set the slicer_tip_park_pos
                park_pos = self.p.slicer_tip_park_pos
                self._set_filament_position(-park_pos)
                if park_pos == 0.:
                    self.log_error("Tip forming performed by slicer but 'slicer_tip_park_pos' not set")
                else:
                    self.log_debug("Tip forming performed by slicer, park_pos set to %.1fmm" % park_pos)

            elif do_form_tip == FORM_TIP_STANDALONE and (self.filament_pos >= FILAMENT_POS_IN_EXTRUDER or runout):
                with self._wrap_track_time('form_tip'):
                    # Extruder only in runout case to give filament best chance to reach gear
                    detected = self.form_tip_standalone(extruder_only=(extruder_only or runout))
                    park_pos = self._get_filament_position()

                    # If handling runout warn if we don't see any filament near the gate
                    if runout and (
                        self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected) is False or
                        (self.has_encoder() and self.get_encoder_distance() == 0)
                    ):
                        self.log_warning("Warning: Filament not seen near gate after tip forming move. Unload may not be possible")

                    self.wrap_gcode_command(self.p.post_form_tip_macro, exception=True, wait=True)

            # Note: Conditionals deliberately coded this way to match macro alternative
            homing_movement = None # Track how much homing is done for calibrated bowden length optimization
            deficit = 0.           # Amount of homing that would be expected (because bowden load is shortened)
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
            start_filament_pos = self.filament_pos
            unload_to_buffer = (start_filament_pos >= FILAMENT_POS_END_BOWDEN and not extruder_only)

            if self.p.gcode_unload_sequence and calibrated:
                self.log_debug("Calling external user defined unloading sequence macro")
                self.wrap_gcode_command(
                    "%s FILAMENT_POS=%d LENGTH=%.1f EXTRUDER_ONLY=%d PARK_POS=%.1f" % (
                        self.p.unload_sequence_macro,
                        start_filament_pos,
                        bowden_move,
                        extruder_only,
                        park_pos
                    ),
                    exception=True
                )

            elif extruder_only:
                if start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                    synced_extruder_unload = self._unload_extruder(extruder_only=True, validate=do_form_tip == FORM_TIP_STANDALONE)
                else:
                    raise MmuError("Cannot unload extruder because filament not detected in extruder! (state: %s)" % start_filament_pos)

            elif start_filament_pos == FILAMENT_POS_UNLOADED:
                raise MmuError("Cannot unload because already unloaded!")

            else:
                if start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload to gate
                    synced_extruder_unload = self._unload_extruder(validate=do_form_tip == FORM_TIP_STANDALONE)

                if (
                    (start_filament_pos >= FILAMENT_POS_END_BOWDEN and calibrated) or
                    (start_filament_pos >= FILAMENT_POS_HOMED_GATE and not full)
                ):
                    # Fast unload of bowden, then unload gate
                    bowden_move_ratio = self._unload_bowden(bowden_move)
                    homing_movement, deficit = self._unload_gate()

                elif start_filament_pos >= FILAMENT_POS_HOMED_GATE:
                    # We have to do slow unload because we don't know exactly where we are. We use
                    # full bowden length or max possible length if bowden is uncalibrated
                    _,_ = self._unload_gate(homing_max=bowden_move if calibrated else self.mmu_unit().p.bowden_homing_max)

            # Set future "from buffer" flag (also used for faster loading speed)
            if unload_to_buffer and self.gate_status[self.gate_selected] != GATE_EMPTY:
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE_FROM_BUFFER)

            # If runout then over unload to prevent accidental reload
            if runout:
                self._eject_from_gate()

# Currently disabled because it results in servo "flutter" that users don't like
#            # Encoder based validation test
#            if self._can_use_encoder():
#                movement = self.selector().filament_release(measure=True)
#                if movement > self.encoder().movement_min():
#                    self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
#                    self.log_trace("Encoder moved %.1fmm when filament was released!" % movement)
#                    raise MmuError("Encoder sensed movement when the servo was released\nConcluding filament is stuck somewhere")

            self.movequeues_wait()
            msg = "Unload of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = self.mmu_unit().p.gate_parking_distance + self._get_encoder_dead_space() + (self.mmu_unit().p.toolhead_unload_safety_margin if not synced_extruder_unload else 0.)
                msg += " {1}(adjusted encoder: %.1fmm){0}" % -(final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # Notify autotune manager
            if full and not extruder_only and not self.p.gcode_unload_sequence:
                self.calibration_manager.note_unload_telemetry(bowden_move_ratio, homing_movement, deficit)

            # POST_UNLOAD user defined macro
            if macros_and_track:
                with self._wrap_track_time('post_unload'):

                    # Restore the expected sync state now before running this macro
                    self.reset_sync_gear_to_extruder(not extruder_only and self.mmu_unit().p.sync_to_extruder)

                    if self.has_mmu_cutter:
                        with self.wrap_action(ACTION_CUTTING_FILAMENT):
                            self.wrap_gcode_command(self.p.post_unload_macro, exception=True, wait=True)
                    else:
                        self.wrap_gcode_command(self.p.post_unload_macro, exception=True, wait=True)

        except MmuError as ee:
            self._track_gate_statistics('unload_failures', self.gate_selected)
            raise MmuError("Unload sequence failed because:\n%s" % (str(ee)))

        finally:
            self._track_gate_statistics('unloads', self.gate_selected)

            if not extruder_only:
                self._set_action(current_action)

            if macros_and_track:
                self._track_time_end('unload')

    # Form tip prior to extraction from the extruder. This can take the form of shaping the filament or could simply
    # activate a filament cutting mechanism. Sets filament position based on park pos
    # Returns True if filament is detected
    def form_tip_standalone(self, extruder_only=False):
        self.movequeues_wait()

        # Pre check to validate the presence of filament in the extruder and case where we don't need to form tip
        filament_initially_present = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        if filament_initially_present is False:
            self.log_debug("Tip forming skipped because no filament was detected")

            if self.filament_pos == FILAMENT_POS_LOADED:
                self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
            else:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

            self._set_filament_position(-self.p.toolhead_extruder_to_nozzle)
            return False

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.p.form_tip_macro)

        with self.wrap_action(ACTION_CUTTING_TIP if self.has_toolhead_cutter else ACTION_FORMING_TIP):
            sync = self.reset_sync_gear_to_extruder(not extruder_only and self.mmu_unit().p.sync_form_tip)
            self._ensure_safe_extruder_temperature(wait=True)

            # Perform the tip forming move and establish park_pos
            initial_encoder_position = self.get_encoder_distance()
            park_pos, remaining, reported = self._do_form_tip()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            self._set_filament_remaining(remaining, self.gate_color[self.gate_selected] if self.gate_selected != TOOL_GATE_UNKNOWN else '')

            # Encoder based validation test
            detected = True # Start with assumption that filament was present
            if self._can_use_encoder() and not reported:
                # Logic to try to validate success and update presence of filament based on movement
                if filament_initially_present is True:
                    # With encoder we might be able to check for clog now
                    if not measured > self.encoder().movement_min():
                        raise MmuError("No encoder movement: Concluding filament is stuck in extruder")
                else:
                    # Couldn't determine if we initially had filament at start (lack of sensors)
                    if not measured > self.encoder().movement_min():
                        # No movement. We can be confident we are/were empty
                        detected = False
                    elif sync:
                        # A further test is needed to see if the filament is actually in the extruder
                        detected, moved = self.test_filament_still_in_extruder_by_retracting()
                        park_pos += moved

            self._set_filament_position(-park_pos)
            self.set_encoder_distance(initial_encoder_position + park_pos)

            if detected or extruder_only:
                # Definitely in extruder
                self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
            else:
                # No detection. Best to assume we are somewhere in bowden for defensive unload
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

            return detected

    def _do_form_tip(self, test=False):
        with self.wrap_extruder_current(self.p.extruder_form_tip_current, "for tip forming move"):
            extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
            initial_mcu_pos = extruder_stepper.get_mcu_position()
            initial_encoder_position = self.get_encoder_distance()

            with self._wrap_pressure_advance(0., "for tip forming"):
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, "_MMU_FORM_TIP")
                self.log_info("Forming tip...")
                self.wrap_gcode_command("%s %s" % (self.p.form_tip_macro, "FINAL_EJECT=1" if test else ""), exception=True, wait=True)

            final_mcu_pos = extruder_stepper.get_mcu_position()
            stepper_movement = (initial_mcu_pos - final_mcu_pos) * extruder_stepper.get_step_dist()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            park_pos = gcode_macro.variables.get("output_park_pos", -1)
            try:
                park_pos = float(park_pos)
            except ValueError as e:
                self.log_error("Reported 'output_park_pos: %s' could not be parsed: %s" % (park_pos, str(e)))
                park_pos = -1

            reported = False
            if park_pos < 0:
                # Use stepper movement (tip forming)
                filament_remaining = 0.
                park_pos = stepper_movement + self.p.toolhead_residual_filament + self.toolchange_retract
                msg = "After tip forming, extruder moved: %.1fmm thus park_pos calculated as %.1fmm (encoder measured %.1fmm total movement)" % (stepper_movement, park_pos, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)
            else:
                # Means the macro reported it (filament cutting)
                if park_pos == 0:
                    self.log_warning("Warning: output_park_pos was reported as 0mm and may not be set correctly\nWill attempt to continue...")
                reported = True
                filament_remaining = park_pos - stepper_movement - self.p.toolhead_residual_filament - self.toolchange_retract
                msg = "After tip cutting, park_pos reported as: %.1fmm with calculated %.1fmm filament remaining in extruder (extruder moved: %.1fmm, encoder measured %.1fmm total movement)" % (park_pos, filament_remaining, stepper_movement, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)

            if not test:
                # Important sanity checks to spot misconfiguration
                if park_pos > self.p.toolhead_extruder_to_nozzle:
                    self.log_warning("Warning: park_pos (%.1fmm) cannot be greater than 'toolhead_extruder_to_nozzle' distance of %.1fmm! Assumming fully unloaded from extruder\nWill attempt to continue..." % (park_pos, self.p.toolhead_extruder_to_nozzle))
                    park_pos = self.p.toolhead_extruder_to_nozzle
                    filament_remaining = 0.

                if filament_remaining < 0:
                    self.log_warning("Warning: Calculated filament remaining after cut is negative (%.1fmm)! Suspect misconfiguration of output_park_pos (%.1fmm).\nWill attempt to continue assuming no cut filament remaining..." % (filament_remaining, park_pos))
                    park_pos = 0.
                    filament_remaining = 0.

        return park_pos, filament_remaining, reported

    def purge_standalone(self):
        if self.p.purge_macro:
            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.purge_macro, None)
            if gcode_macro:
                self.log_info("Purging...")
                with self._wrap_extruder_current(self.p.extruder_purge_current, "for filament purge"):
                    # The macro to decide on the purge volume, but expect to be based on this.
                    msg = "Suggested purge volume of %.1fmm%s calculated from:\n" % (self.toolchange_purge_volume, UI_CUBE)
                    msg += "- toolhead_residual_filament: %.1fmm\n" % self.p.toolhead_residual_filament
                    msg += "- filament_remaining (previous cut fragment): %.1fmm\n" % self.filament_remaining
                    msg += "- slicer purge volume for toolchange %s > %s" % (self.selected_tool_string(self._last_tool), self.selected_tool_string(self._next_tool))
                    self.log_debug(msg)
                    self.wrap_gcode_command(self.p.purge_macro, exception=True, wait=True)
            else:
                self.log_warning("Purge macro %s not found" % self.p.purge_macro)


#################################
# FILAMENT MOVEMENT AND CONTROL #
#################################

    # Convenience wrapper around all gear and extruder motor movement that retains sync state, tracks movement and creates trace log
    # motor = "gear"           - gear motor(s) only on rail
    #         "gear+extruder"  - gear and extruder included on rail
    #         "extruder"       - extruder only on gear rail
    #         "synced"         - gear synced with extruder as in print (homing move not possible)
    #
    # If homing move then endstop name can be specified.
    #         "mmu_gate"       - at the gate on MMU (when motor includes "gear")
    #         "mmu_gear_N"     - post past the filament drive gear
    #         "extruder"       - just before extruder entrance (motor includes "gear" or "extruder")
    #         "toolhead"       - after extruder entrance (motor includes "gear" or "extruder")
    #         "mmu_gear_touch" - stallguard on gear (when motor includes "gear", only useful for motor="gear")
    #         "mmu_ext_touch"  - stallguard on nozzle (when motor includes "extruder", only useful for motor="extruder")
    #
    # All move distances are interpreted as relative
    # 'wait' will wait on appropriate move queue(s) after completion of move (forced to True if need encoder reading)
    # 'measure' whether we need to wait and measure encoder for movement
    # 'encoder_dwell' delay some additional time to ensure we have accurate encoder reading (if encoder fitted and required for measuring)
    #
    # All moves return: actual (relative), homed, measured, delta; mmu_toolhead().get_position[1] holds absolute position
    #
    def trace_filament_move(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0, endstop_name="default", track=False, wait=False, encoder_dwell=False, speed_override=True):
        encoder_start = self.get_encoder_distance(dwell=encoder_dwell)
        pos = self.mmu_toolhead().get_position()
        ext_pos = self.toolhead.get_position()
        homed = False
        actual = dist
        delta = 0.
        null_rtn = (0., False, 0., 0.)

        if homing_move != 0:
            # Check for valid endstop
            if endstop_name is None:
                endstops = self.gear_rail().get_endstops()
            else:
                endstop_name = self.sensor_manager.get_mapped_endstop_name(endstop_name)
                endstops = self.gear_rail().get_extra_endstop(endstop_name)
                if endstops is None:
                    self.log_error("Endstop '%s' not found" % endstop_name)
                    return null_rtn

        # Set sensible speeds and accelaration if not supplied
        if motor in ["gear"]:
            if homing_move != 0:
                speed = speed or self.mmu_unit().p.gear_homing_speed
                accel = accel or min(self.mmu_unit().p.gear_from_filament_buffer_accel, self.mmu_unit().p.gear_from_spool_accel)
            else:
                if abs(dist) > self.mmu_unit().p.gear_short_move_threshold:
                    if dist < 0:
                        speed = speed or self.mmu_unit().p.gear_unload_speed
                        accel = accel or self.mmu_unit().p.gear_unload_accel
                    elif (not self.mmu_unit().p.has_filament_buffer or (self.gate_selected >= 0 and self.gate_status[self.gate_selected] != GATE_AVAILABLE_FROM_BUFFER)):
                        speed = speed or self.mmu_unit().p.gear_from_spool_speed
                        accel = accel or self.mmu_unit().p.gear_from_spool_accel
                    else:
                        speed = speed or self.mmu_unit().p.gear_from_filament_buffer_speed
                        accel = accel or self.mmu_unit().p.gear_from_filament_buffer_accel
                else:
                    speed = speed or self.mmu_unit().p.gear_short_move_speed
                    accel = accel or self.mmu_unit().p.gear_short_move_accel

        elif motor in ["gear+extruder", "synced"]:
            if homing_move != 0:
                speed = speed or min(self.mmu_unit().p.gear_homing_speed, self.p.extruder_homing_speed)
                accel = accel or min(max(self.mmu_unit().p.gear_from_filament_buffer_accel, self.mmu_unit().p.gear_from_spool_accel), self.p.extruder_accel)
            else:
                speed = speed or (self.p.extruder_sync_load_speed if dist > 0 else self.p.extruder_sync_unload_speed)
                accel = accel or min(max(self.mmu_unit().p.gear_from_filament_buffer_accel, self.mmu_unit().p.gear_from_spool_accel), self.p.extruder_accel)

        elif motor in ["extruder"]:
            if homing_move != 0:
                speed = speed or self.p.extruder_homing_speed
                accel = accel or self.p.extruder_accel
            else:
                speed = speed or (self.p.extruder_load_speed if dist > 0 else self.p.extruder_unload_speed)
                accel = accel or self.p.extruder_accel

        else:
            self.log_assertion("Invalid motor specification '%s'" % motor)
            return null_rtn

        # Apply per-gate speed override
        if self.gate_selected >= 0 and speed_override:
            adjust = self.gate_speed_override[self.gate_selected] / 100.
            speed *= adjust
            accel *= adjust

        def _set_sync_mode(sync_mode):
            self.mmu_toolhead.sync(sync_mode)
            if sync_mode == MmuToolHead.GEAR_SYNCED_TO_EXTRUDER:
                self._adjust_gear_current(percent=self.mmu_unit().p.sync_gear_current, reason="for extruder synced move")
            else:
                self._restore_gear_current() # 100%

        with self._wrap_espooler(motor, dist, speed, accel, homing_move):
            wait = wait or self._wait_for_espooler # Allow eSpooler wrapper to force wait

            # Gear rail is driving the filament
            start_pos = self.mmu_toolhead.get_position()[1]
            if motor in ["gear", "gear+extruder", "extruder"]:
                _set_sync_mode(MmuToolHead.EXTRUDER_SYNCED_TO_GEAR if motor == "gear+extruder" else MmuToolHead.EXTRUDER_ONLY_ON_GEAR if motor == "extruder" else MmuToolHead.GEAR_ONLY)
                if homing_move != 0:
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstops, self.mmu_toolhead)
                    init_ext_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position() # For non-homing extruder or if extruder not on gear rail
                    init_pos = pos[1]
                    pos[1] += dist
                    for _ in range(self.p.canbus_comms_retries):  # HACK: We can repeat because homing move
                        got_comms_timeout = False # HACK: Logic to try to mask CANbus timeout issues
                        try:
                            #initial_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position()
                            #init_pos = pos[1]
                            #pos[1] += dist
                            with self.wrap_accel(accel):
                                trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                            homed = True
                            if self.gear_rail.is_endstop_virtual(endstop_name):
                                # Stallguard doesn't do well at slow speed. Try to infer move completion
                                if abs(trig_pos[1] - dist) < 1.0:
                                    homed = False
                        except self.printer.command_error as e:
                            # CANbus mcu's often seen to exhibit "Communication timeout" so surface errors to user
                            if abs(trig_pos[1] - dist) > 0. and "after full movement" not in str(e):
                                if 'communication timeout' in str(e).lower():
                                    got_comms_timeout = True
                                    speed *= 0.8 # Reduce speed by 20%
                                self.log_error("Did not complete homing move: %s" % str(e))
                            else:
                                if self.log_enabled(LOG_STEPPER):
                                    self.log_stepper("Did not home: %s" % str(e))
                            homed = False
                        finally:
                            halt_pos = self.mmu_toolhead.get_position()
                            ext_actual = (self.mmu_extruder_stepper.stepper.get_mcu_position() - init_ext_mcu_pos) * self.mmu_extruder_stepper.stepper.get_step_dist()

                            # Support setup where a non-homing extruder is being used
                            if motor == "extruder" and not self.unit().homing_extruder:
                                # This isn't super accurate if extruder isn't (homing) MmuExtruder because doesn't have required endstop, thus this will
                                # overrun and even move slightly even if already homed. We can only correct the actual gear rail position.
                                halt_pos[1] += ext_actual
                                self.mmu_toolhead.set_position(halt_pos) # Correct the gear rail position

                            actual = halt_pos[1] - init_pos
                            if self.log_enabled(LOG_STEPPER):
                                self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s >> %s" % (
                                        motor.upper(), dist, speed, accel, endstop_name, wait,
                                        (
                                            "%s halt_pos=%.1f (rail moved=%.1f, extruder moved=%.1f), "
                                            "start_pos=%.1f, trig_pos=%.1f"
                                            % (
                                                "HOMED" if homed else "DID NOT HOMED",
                                                halt_pos[1], actual, ext_actual, start_pos, trig_pos[1],
                                            )
                                        ),
                                    )
                                )

                        if not got_comms_timeout:
                            break
                else:
                    if self.log_enabled(LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
                    pos[1] += dist
                    with self.wrap_accel(accel):
                        self.mmu_toolhead.move(pos, speed)

            # Extruder is driving, gear rail is following
            elif motor in ["synced"]:
                _set_sync_mode(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER)
                if homing_move != 0:
                    self.log_error("Not possible to perform homing move while synced")
                else:
                    if self.log_enabled(LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)

            self.mmu_toolhead.flush_step_generation() # TTC mitigation (TODO still required?)
            self.toolhead.flush_step_generation()     # TTC mitigation (TODO still required?)
            if wait:
                self.movequeues_wait()

        encoder_end = self.get_encoder_distance(dwell=encoder_dwell)
        measured = encoder_end - encoder_start
        delta = abs(actual) - measured # +ve means measured less than moved, -ve means measured more than moved
        if trace_str:
            if homing_move != 0:
                trace_str += ". Stepper: '%s' %s after moving %.1fmm (of max %.1fmm), encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, ("homed" if homed else "did not home"), actual, dist, measured, delta)
                trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead.get_position()[1], encoder_end)
            else:
                trace_str += ". Stepper: '%s' moved %.1fmm, encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, dist, measured, delta)
            trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead.get_position()[1], encoder_end)
            self.log_trace(trace_str)

        if self._can_use_encoder() and motor == "gear" and track:
            if dist > 0:
                self._track_gate_statistics('load_distance', self.gate_selected, dist)
                self._track_gate_statistics('load_delta', self.gate_selected, delta)
            else:
                self._track_gate_statistics('unload_distance', self.gate_selected, -dist)
                self._track_gate_statistics('unload_delta', self.gate_selected, delta)
            if dist != 0:
                quality = abs(1. - delta / dist)
                cur_quality = self.gate_statistics[self.gate_selected]['quality']
                if cur_quality < 0:
                    self.gate_statistics[self.gate_selected]['quality'] = quality
                else:
                    # Average down over 10 swaps
                    self.gate_statistics[self.gate_selected]['quality'] = (cur_quality * 9 + quality) / 10

        return actual, homed, measured, delta

# PAUL old logic
#        with self._wrap_espooler(motor, dist, speed, accel, homing_move):
#            wait = wait or self._wait_for_espooler # Allow eSpooler wrapper to force wait
#
#            # Gear rail is driving the filament
#            start_pos = self.mmu_toolhead.get_position()[1]
#            if motor in ["gear", "gear+extruder", "extruder"]:
#                with self._wrap_sync_mode(MmuToolHead.EXTRUDER_SYNCED_TO_GEAR if motor == "gear+extruder" else MmuToolHead.EXTRUDER_ONLY_ON_GEAR if motor == "extruder" else MmuToolHead.GEAR_ONLY):
#                    if homing_move != 0:
#                        trig_pos = [0., 0., 0., 0.]
#                        hmove = HomingMove(self.printer, endstops, self.mmu_toolhead())
#                        extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
#                        init_ext_mcu_pos = extruder_stepper.get_mcu_position() # For non-homing extruder or if extruder not on gear rail
#                        init_pos = pos[1]
#                        pos[1] += dist
#                        for _ in range(self.canbus_comms_retries):  # HACK: We can repeat because homing move
#                            got_comms_timeout = False # HACK: Logic to try to mask CANbus timeout issues
#                            try:
#                                #initial_mcu_pos = extruder.stepper.get_mcu_position()
#                                #init_pos = pos[1]
#                                #pos[1] += dist
#                                with self.wrap_accel(accel):
#                                    trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
#                                homed = True
#                                if self.gear_rail().is_endstop_virtual(endstop_name):
#                                    # Stallguard doesn't do well at slow speed. Try to infer move completion
#                                    if abs(trig_pos[1] - dist) < 1.0:
#                                        homed = False
#                            except self.printer.command_error as e:
#                                # CANbus mcu's often seen to exhibit "Communication timeout" so surface errors to user
#                                if abs(trig_pos[1] - dist) > 0. and "after full movement" not in str(e):
#                                    if 'communication timeout' in str(e).lower():
#                                        got_comms_timeout = True
#                                        speed *= 0.8 # Reduce speed by 20%
#                                    self.log_error("Did not complete homing move: %s" % str(e))
#                                else:
#                                    if self.log_enabled(self.LOG_STEPPER):
#                                        self.log_stepper("Did not home: %s" % str(e))
#                                homed = False
#                            finally:
#                                halt_pos = self.mmu_toolhead().get_position()
#                                ext_actual = (extruder_stepper.get_mcu_position() - init_ext_mcu_pos) * extruder_stepper.get_step_dist()
#
#                                # Support setup where a non-homing extruder is being used
#                                if motor == "extruder" and not self.mmu_machine.homing_extruder:
#                                    # This isn't super accurate if extruder isn't (homing) MmuExtruder because doesn't have required endstop, thus this will
#                                    # overrun and even move slightly even if already homed. We can only correct the actual gear rail position.
#                                    halt_pos[1] += ext_actual
#                                    self.mmu_toolhead().set_position(halt_pos) # Correct the gear rail position
#
#                                actual = halt_pos[1] - init_pos
#                                if self.log_enabled(self.LOG_STEPPER):
#                                    self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s >> %s" % (motor.upper(), dist, speed, accel, endstop_name, wait, "%s halt_pos=%.1f (rail moved=%.1f, extruder moved=%.1f), start_pos=%.1f, trig_pos=%.1f" % ("HOMED" if homed else "DID NOT HOMED",  halt_pos[1], actual, ext_actual, start_pos, trig_pos[1])))
#                            if not got_comms_timeout:
#                                break
#                    else:
#                        if self.log_enabled(self.LOG_STEPPER):
#                            self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
#                        pos[1] += dist
#                        with self.wrap_accel(accel):
#                            self.mmu_toolhead().move(pos, speed)
#
#            # Extruder is driving, gear rail is following
#            elif motor in ["synced"]:
#                with self._wrap_sync_mode(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER):
#                    if homing_move != 0:
#                        self.log_error("Not possible to perform homing move while synced")
#                    else:
#                        if self.log_enabled(self.LOG_STEPPER):
#                            self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
#                        ext_pos[3] += dist
#                        self.toolhead.move(ext_pos, speed)
#
#            self.mmu_toolhead().flush_step_generation() # TTC mitigation (TODO still required?)
#            self.toolhead.flush_step_generation()       # TTC mitigation (TODO still required?)
#            if wait:
#                self.movequeues_wait()
#
#        encoder_end = self.get_encoder_distance(dwell=encoder_dwell)
#        measured = encoder_end - encoder_start
#        delta = abs(actual) - measured # +ve means measured less than moved, -ve means measured more than moved
#        if trace_str:
#            if homing_move != 0:
#                trace_str += ". Stepper: '%s' %s after moving %.1fmm (of max %.1fmm), encoder measured %.1fmm (delta %.1fmm)"
#                trace_str = trace_str % (motor, ("homed" if homed else "did not home"), actual, dist, measured, delta)
#                trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead().get_position()[1], encoder_end)
#            else:
#                trace_str += ". Stepper: '%s' moved %.1fmm, encoder measured %.1fmm (delta %.1fmm)"
#                trace_str = trace_str % (motor, dist, measured, delta)
#            trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead().get_position()[1], encoder_end)
#            self.log_trace(trace_str)
#
#        if self._can_use_encoder() and motor == "gear" and track:
#            if dist > 0:
#                self._track_gate_statistics('load_distance', self.gate_selected, dist)
#                self._track_gate_statistics('load_delta', self.gate_selected, delta)
#            else:
#                self._track_gate_statistics('unload_distance', self.gate_selected, -dist)
#                self._track_gate_statistics('unload_delta', self.gate_selected, delta)
#            if dist != 0:
#                quality = abs(1. - delta / dist)
#                cur_quality = self.gate_statistics[self.gate_selected]['quality']
#                if cur_quality < 0:
#                    self.gate_statistics[self.gate_selected]['quality'] = quality
#                else:
#                    # Average down over 10 swaps
#                    self.gate_statistics[self.gate_selected]['quality'] = (cur_quality * 9 + quality) / 10
#
#        return actual, homed, measured, delta

    # Used to force accelaration override for homing moves 
    @contextlib.contextmanager  
    def wrap_accel(self, accel):
        self.mmu_toolhead.get_kinematics().set_accel_limit(accel)
        try:                    
            yield self
        finally:            
            self.mmu_toolhead.get_kinematics().set_accel_limit(None)
                                
    # Used to wrap certain unload moves and activate eSpooler. Ensures eSpooler is always stopped
    @contextlib.contextmanager          
    def _wrap_espooler(self, motor, dist, speed, accel, homing_move):
        self._wait_for_espooler = False     
        espooler_operation = ESPOOLER_OFF
                                                
        if self.has_espooler():                 
            pwm_value = 0                   
            if abs(dist) >= self.mmu_unit().p.espooler_min_distance and speed > self.mmu_unit().p.espooler_min_stepper_speed:
                if dist > 0 and ESPOOLER_ASSIST in self.mmu_unit().p.espooler_operations:
                    espooler_operation = ESPOOLER_ASSIST
                elif dist < 0 and ESPOOLER_REWIND in self.mmu_unit().p.espooler_operations:
                    espooler_operation = ESPOOLER_REWIND
                            
                if espooler_operation == ESPOOLER_OFF:
                    pwm_value = 0
                elif speed >= self.mmu_unit().p.espooler_max_stepper_speed:
                    pwm_value = 1
                else:
                    pwm_value = (speed / self.mmu_unit().p.espooler_max_stepper_speed) ** self.mmu_unit().p.espooler_speed_exponent

            # Reduce assist speed compared to rewind but also apply the "print" minimum
            # We want rewind to be faster than assist but never non-functional
            if espooler_operation == ESPOOLER_ASSIST:
                pwm_value = max(pwm_value * (self.mmu_unit().p.espooler_assist_reduced_speed / 100), self.mmu_unit().p.espooler_printing_power / 100)
                    
            if espooler_operation != ESPOOLER_OFF:
                self._wait_for_espooler = not homing_move
                self.espooler.set_operation(self.gate_selected, pwm_value, espooler_operation) 
        try:        
            # Note gate_selected doesn't change in this use case, it's just filament movement
            yield self

        finally:
            self._wait_for_espooler = False
            if espooler_operation != ESPOOLER_OFF:
                self.espooler.set_operation(self.gate_selected, 0, ESPOOLER_OFF)


##############################################
# GENERAL FILAMENT RECOVERY AND MOVE HELPERS #
##############################################

    # Report on need to recover and necessary calibration
    def report_necessary_recovery(self, use_autotune=True):
        if not self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ALL, silent=None, use_autotune=use_autotune):
            if self.filament_pos != FILAMENT_POS_UNLOADED and TOOL_GATE_UNKNOWN in [self.gate_selected, self.tool_selected]:
                self.log_error("Filament detected but tool/gate is unknown. Plese use MMU_RECOVER GATE=xx to correct state")
            elif self.filament_pos not in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]:
                self.log_error("Filament not detected as either unloaded or fully loaded. Please check and use MMU_RECOVER to correct state or fix before continuing")


    # This is a recovery routine to determine the most conservative location of the filament (for unload purposes)
    # Also, ensures that the filament availabilty is updated if filament is found
    def recover_filament_pos(self, strict=False, can_heat=True, message=False, silent=False):
        if message:
            self.log_info("Attempting to recover filament position...")

        ts = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        es = self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
        gs = self.sensor_manager.check_sensor(self.sensor_manager.get_mapped_endstop_name(self.mmu_unit().p.gate_homing_endstop))

        filament_detected = self.sensor_manager.check_any_sensors_in_path()
        looks_loaded = self.sensor_manager.check_all_sensors_in_path()
        if not filament_detected:
            filament_detected = self.check_filament_in_mmu() # Include encoder detection method

        # Definitely loaded
        if ts:
            self._set_filament_pos_state(FILAMENT_POS_LOADED, silent=silent)

        # Probably loaded: Unless strict we will continue to assume loaded in the absence of sensors to say otherwise
        elif not strict and self.filament_pos == FILAMENT_POS_LOADED and looks_loaded:
            pass

        # Somewhere in extruder
        elif filament_detected and can_heat and self.check_filament_in_extruder(): # Encoder based
            self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming on unload
        elif ts is False and filament_detected and (self.p.strict_filament_recovery or strict) and can_heat and self.check_filament_in_extruder():
            # This case adds an additional encoder based test to see if filament is still being gripped by extruder
            # even though TS doesn't see it. It's a pedantic option so on turned on by strict flag
            self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming

        # At extruder entry
        elif es:
            self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY, silent=silent) # Allows for fast bowden unload move

        # Parked at gate (when parking distance is not a retract i.e. gs sensor expected to be triggered)
        elif gs and filament_detected and self.mmu_unit().p.gate_parking_distance <= 0:
            self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # Somewhere in bowden
        elif gs or filament_detected:
            self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=silent) # Prevents fast bowden unload move

            # Sensor sanity check
            if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False) is False:
                sensors = self.sensor_manager.get_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False)
                malfunction = ", ".join(sorted(k for k, v in sensors.items() if v is False))
                self.log_warning("Filament determined to be somewhere in bowden but the following sensors are unexpectedly not triggered: %s\nCheck for further sensor malfunction with MMU_SENSORS command. Also validate the correct gate is selected.\nRe-run MMU_RECOVER when ready" % malfunction)

        # Unloaded
        else:
            self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # If filament is detected then ensure gate status is correct
        if self.gate_selected != TOOL_GATE_UNKNOWN and filament_detected:
            gate_status = self.gate_status[self.gate_selected]
            if self.filament_pos >= FILAMENT_POS_START_BOWDEN and gate_status < GATE_AVAILABLE:
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
            elif gate_status == GATE_EMPTY:
                self._set_gate_status(self.gate_selected, GATE_UNKNOWN)

#    # This is a recovery routine to determine the most conservative location of the filament (for unload purposes)
#    # Also, ensures that the filament availabilty is updated if filament is found
#    def recover_filament_pos(self, strict=False, can_heat=True, message=False, silent=False):
#        if message:
#            self.log_info("Attempting to recover filament position...")
#
#        ts = self.sensor_manager.check_sensor(self.SENSOR_TOOLHEAD)
#        es = self.sensor_manager.check_sensor(self.SENSOR_EXTRUDER_ENTRY)
#
#        gs = self.sensor_manager.check_sensor(self.sensor_manager.get_mapped_endstop_name(self.gate_homing_endstop))
#
#        filament_detected = self.sensor_manager.check_any_sensors_in_path()
#        if not filament_detected:
#            filament_detected = self.check_filament_in_mmu() # Include encoder detection method
#
#        # Loaded
#        if ts:
#            self._set_filament_pos_state(self.FILAMENT_POS_LOADED, silent=silent)
#
#        # Somewhere in extruder
#        elif filament_detected and can_heat and self.check_filament_in_extruder(): # Encoder based
#            self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming
#        elif ts is False and filament_detected and (self.strict_filament_recovery or strict) and can_heat and self.check_filament_in_extruder():
#            # This case adds an additional encoder based test to see if filament is still being gripped by extruder
#            # even though TS doesn't see it. It's a pedantic option so on turned on by strict flag
#            self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming
#
#        # At extruder entry
#        elif es:
#            self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY, silent=silent) # Allows for fast bowden unload move
#
#        # Parked at gate (when parking distance is not a retract i.e. gs sensor expected to be triggered)
#        elif gs and filament_detected and self.gate_parking_distance <= 0:
#            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=silent)
#
#        # Somewhere in bowden
#        elif gs or filament_detected:
#            self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN, silent=silent) # Prevents fast bowden unload move
#
#        # Unloaded
#        else:
#            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=silent)
#
#        # If filament is detected then ensure gate status is correct
#        if self.gate_selected != self.TOOL_GATE_UNKNOWN and filament_detected:
#            gate_status = self.gate_status[self.gate_selected]
#            if self.filament_pos >= self.FILAMENT_POS_START_BOWDEN and gate_status < self.GATE_AVAILABLE:
#                self._set_gate_status(self.gate_selected, self.GATE_AVAILABLE)
#            elif gate_status == self.GATE_EMPTY:
#                self._set_gate_status(self.gate_selected, self.GATE_UNKNOWN)

    # Check for filament in MMU using available sensors or encoder
    def check_filament_in_mmu(self):
        self.log_debug("Checking for filament in MMU...")
        detected = self.sensor_manager.check_any_sensors_in_path()
        if not detected and self.has_encoder():
            self.selector().filament_drive()
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected

    # Check for filament at currently selected gate
    def check_filament_in_gate(self):
        self.log_debug("Checking for filament at gate...")
        detected = self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected)
        if not detected and self.has_encoder():
            self.selector().filament_drive()
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected

    # Return True if filament runout detected by sensors
    def check_filament_runout(self):
        self.log_debug("Checking for runout...")
        runout = self.sensor_manager.check_for_runout()
        if runout is None and self.has_encoder():
            self.selector().filament_drive()
            detected = not self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
            runout = not detected
        if runout is None:
            self.log_debug("No sensors configured!")
        return runout

    # Return True/False if detected or None if test not possible
    def check_filament_in_extruder(self):
        # First double check extruder entry sensor if fitted
        es = self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
        if es is not None:
            return es

        # Now toolhead if fitted
        ts = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        if ts is True:
            return True

        # Finally resort to movement test with encoder
        detected,_ = self.test_filament_still_in_extruder_by_retracting()
        return detected

    # Check for filament in extruder by moving extruder motor. Even with toolhead sensor this can
    # happen if the filament is in the short distance from sensor to gears. Requires encoder
    # Return True/False if detected or None if test not possible
    def test_filament_still_in_extruder_by_retracting(self):
        detected = None
        measured = 0
        if self.has_encoder() and not self.mmu_machine.filament_always_gripped:
            with self._require_encoder(): # Force quality measurement
                self.log_info("Checking for possibility of filament still in extruder gears...")
                self._ensure_safe_extruder_temperature(wait=False)
                self.selector().filament_release()
                move = self.p.encoder_move_step_size
                _,_,measured,_ = self.trace_filament_move("Checking extruder", -move, speed=self.p.extruder_unload_speed, motor="extruder")
                detected = measured > self.encoder().movement_min()
                self.log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
        return detected, measured

    def buzz_gear_motor(self):
        if self.has_encoder():
            with self._require_encoder(): # Force quality measurement
                initial_encoder_position = self.get_encoder_distance()
                self.trace_filament_move(None, 2.5 * self.encoder().get_resolution(), accel=self.mmu_unit().p.gear_buzz_accel, encoder_dwell=None)
                self.trace_filament_move(None, -2.5 * self.encoder().get_resolution(), accel=self.mmu_unit().p.gear_buzz_accel, encoder_dwell=None)
                measured = self.get_encoder_distance() - initial_encoder_position
                self.log_trace("After buzzing gear motor, encoder measured %.2f" % measured)
                self.set_encoder_distance(initial_encoder_position, dwell=None)
                return measured > self.encoder().movement_min()
        else:
            self.trace_filament_move(None, 5, accel=self.mmu_unit().p.gear_buzz_accel)
            self.trace_filament_move(None, -5, accel=self.mmu_unit().p.gear_buzz_accel)
        return None


    def reset_sync_gear_to_extruder(self, sync_intention, force_grip=False, force_in_print=False, skip_extruder_check=False):
        """
        Reset the gear-to-extruder sync state based on MMU type and current state.

        Args:
            sync_intention (bool):
                Desired sync state during printing, derived from parameters such as
                `sync_to_extruder`, `sync_form_tip`, and `sync_purge`.

            force_grip (bool, optional):
                If True, forces an immediate filament grip state change
                (typically triggers a servo movement).

            force_in_print (bool, optional):
                Forces the logic to behave as if the printer is currently in a print.
                Primarily used for testing.

            skip_extruder_check (bool, optional):
                Normally, syncing only occurs if filament is present in the extruder.
                When True, this overrides that check. Used by the
                `MMU_SYNC_GEAR_MOTOR` command.

        Returns:
            bool: The final sync state that was applied.
        """
        bypass_selected = self.gate_selected == TOOL_GATE_BYPASS
        in_print_context = self.is_in_print(force_in_print)
        actively_printing = self.is_printing(force_in_print)

        filament_past_entry = self.filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY
        extruder_check_ok = filament_past_entry or skip_extruder_check

        always_gripped = self.mmu_machine.filament_always_gripped
        standalone_sync_requested = self._standalone_sync

        # In a non-print context we also honor the caller's explicit intention.
        wants_sync_out_of_print = always_gripped or standalone_sync_requested or sync_intention

        if bypass_selected:
            sync = False

        elif in_print_context:
            if actively_printing:
                # During active printing, respect the print-time sync setting.
                sync = bool(self.mmu_unit().p.sync_to_extruder)
            else:
                # In print context but not actively printing (e.g., paused/warming),
                # sync only if filament is present (or overridden) and sync is needed/requested
                sync = extruder_check_ok and (always_gripped or standalone_sync_requested)

        else:
            # Not in a print: sync if filament is present (or overridden) and any
            # condition requires/requests syncing.
            sync = extruder_check_ok and wants_sync_out_of_print

        self.sync_gear_to_extruder(sync, force_grip=force_grip)
        return sync


    def sync_gear_to_extruder(self, sync, gate=None, force_grip=False):
        """
        Sync or unsync the gear motor with the extruder, handling filament engagement and current control.

        This method:
          - Applies safety guards to prevent syncing when bypass/unknown gates are selected or the
            selector is not homed.
          - Manages filament grip/release (especially important on type-A designs to avoid "buzz"
            and to reduce servo flutter).
          - Updates the toolhead sync mode.
          - Adjusts gear stepper current while synced, and restores it when unsynced. On multigear
            machines, it also ensures the previously-used gear stepper's current is restored when
            switching gates.

        Args:
            sync (bool):
                True to sync the gear motor to the extruder; False to unsync.

            gate (Optional[int]):
                Gate to apply current adjustments to. If None, defaults to `self.gate_selected`.
                This is optional because on some type-B designs this method may be called before
                `self.gate_selected` is finalized.

            force_grip (bool, optional):
                If True, forces a filament release action when unsyncing even if release suppression
                is enabled higher in the call stack.

        Returns:
            None
        """
        # Default to current selection; some designs call this before gate selection is finalized.
        if gate is None:
            gate = self.gate_selected

        bypass_or_unknown_gate = gate < 0
        selector_ready = getattr(self.selector, "is_homed", False)

        # Protect cases where we should not sync (type-B always has a homed selector).
        if bypass_or_unknown_gate or not selector_ready:
            sync = False
            self._standalone_sync = False

        # Filament grip handling (do this before syncing to avoid "buzz" movement on type-A MMUs).
        if sync:
            self.selector().filament_drive()
        else:
            # There are situations where we want to be lazy to avoid servo "flutter":
            #   - `_suppress_release_grip` is True unless we are the outermost caller.
            #   - `force_grip` can override that suppression.
            should_release = force_grip or not self._suppress_release_grip
            if should_release:
                self.selector().filament_release()

        # Sync / unsync toolhead mode (avoid redundant calls).
        desired_sync_mode = MmuToolHead.GEAR_SYNCED_TO_EXTRUDER if sync else None
        if desired_sync_mode != self.mmu_toolhead.sync_mode:
            self.movequeues_wait()  # Safety: likely unnecessary but ensures no queued moves conflict.
            self.mmu_toolhead.sync(desired_sync_mode)

        # Current control:
        # - While synced, optionally reduce current for the active gear stepper.
        # - On multigear systems, restore current on the previously-used gear stepper if gate differs.
        # - When unsynced, restore current to 100%.
        if sync:
            if self.mmu_machine.multigear and gate != self.gate_selected:
                self._restore_gear_current()  # Restore previous gear stepper to 100%

            self._adjust_gear_current(
                gate=gate,
                percent=self.mmu_unit().p.sync_gear_current,
                reason="for extruder syncing",
            )
        else:
            self._restore_gear_current()  # 100%


    @contextlib.contextmanager
    def wrap_sync_gear_to_extruder(self):
        """
        Context manager that protects gear/extruder synchronization, current,
        and filament grip state.

        This is intended to be used as the outermost wrapper around "MMU_*"
        command handlers that may temporarily alter sync, motor current, or
        grip state during a print or standalone operation.

        Behavior:
            - Captures the current sync state before entering the context.
            - Suppresses filament release in nested calls to prevent servo
              "flutter" caused by repeated grip/release transitions.
            - Ensures only the outermost wrapper clears suppression.
            - Restores the previous sync state on exit (via
              `reset_sync_gear_to_extruder`), allowing normal logic to
              reconcile grip and current safely.

        Yields:
            self: Allows the caller to operate within the protected context.
        Example:
            with self.wrap_sync_gear_to_extruder():
                self.sync_gear_to_extruder(True)
                ...
        """
        # Capture current sync state so it can be restored on exit.
        previous_sync = (
            self.mmu_toolhead.sync_mode == MmuToolHead.GEAR_SYNCED_TO_EXTRUDER
        )

        # Suppress grip release only at the outermost level.
        outermost_wrapper = not self._suppress_release_grip
        self._suppress_release_grip = True

        try:
            yield self
        finally:
            # Only the outermost wrapper clears suppression.
            if outermost_wrapper:
                self._suppress_release_grip = False

            # Restore prior sync state. Logic inside reset_sync_gear_to_extruder
            # may consult the global suppression flag when reconciling grip.
            self.reset_sync_gear_to_extruder(previous_sync)



# PAUL old reference code
#    # Reset correct sync state based on MMU type and state
#    #   sync_intention: sync intention when printing based on sync_to_extruder, sync_form_tip, sync_purge
#    #   force_in_print used to mimick printing behavior often for testing
#    #
#    # This logic is tricky. Have to consider:
#    #   If bypass is selected we cannot sync
#    #   If in a print then used desired sync state if actively printing or desired or necessary sync state
#    #   If not consider desired (_standalone_sync) or necessary (always_gripped) sync state
#    def reset_sync_gear_to_extruder(self, sync_intention, force_in_print=False):
#        if self.gate_selected == self.TOOL_GATE_BYPASS:
#            sync = False
#        elif self.is_in_print(force_in_print):
#            sync = (
#                (self.is_printing(force_in_print) and self.sync_to_extruder) or
#                (
#                    not self.is_printing(force_in_print) and
#                    self.filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY and
#                    (
#                        self.mmu_unit().filament_always_gripped or
#                        self._standalone_sync
#                    )
#                )
#            )
#        else:
#            sync = (
#                self.filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY and
#                (
#                    self.mmu_unit().filament_always_gripped or
#                    self._standalone_sync or
#                    sync_intention
#                )
#            )
#        self.sync_gear_to_extruder(sync)
#        return sync
#
#    # Sync/unsync gear motor with extruder, handle filament engagement and current control
#    def sync_gear_to_extruder(self, sync, gate=None):
##        self.log_error("PAUL: sync_gear_to_extruder(sync=%s)" % sync)
#        # Safety in case somehow called with bypass/unknown selected. Usually this is called after
#        # self.gate_selected is set, but can be before on type-B designs hence optional gate parameter
#        gate = gate if gate is not None else self.gate_selected
#
#        # Protect cases where we shouldn't sync (note type-B always have homed selector)
#        if gate < 0 or not self.selector().is_homed:
#            sync = False
#            self._standalone_sync = False
#
#        # Handle filament grip before sync (type-A MMU) because of potential "buzz" movement
#        if sync:
#            self.selector().filament_drive()
#        elif not self._suppress_release_grip:
#            # There are situations where we want this to be lazy to avoid "flutter" (of servo)
#            self.selector().filament_release()
#
#        # Sync / Unsync
#        new_sync_mode = MmuToolHead.GEAR_SYNCED_TO_EXTRUDER if sync else None
#        if new_sync_mode != self.mmu_toolhead().sync_mode:
#            self.movequeues_wait() # TODO Safety but should not be required(?)
#            self.mmu_toolhead().sync(new_sync_mode)
#
#        # See if we need to set a reduced gear current. If we do then make sure it is
#        # restored on previous gear stepper if we are on a multigear MMU
#        if sync:
#            # Reset current on old gear stepper before adjusting new
#            if self.mmu_unit().multigear and gate != self.gate_selected:
#                self._restore_gear_current()
#            self._adjust_gear_current(gate=gate, percent=self.sync_gear_current, reason="for extruder syncing")
#        else:
#            self._restore_gear_current()
#
#    # This is used to protect synchronization, current and grip states and is used as an outermost wrapper
#    # for "MMU_" commands back into Happy Hare during a print or standalone operation
#    #   supress_fix_grip: prevents subsequent recursive calls from relaxing grip thus avoiding flutter
#    @contextlib.contextmanager
#    def wrap_sync_gear_to_extruder(self):
#        prev_sync = self.mmu_toolhead().sync_mode == MmuToolHead.GEAR_SYNCED_TO_EXTRUDER
#
#        # Turn espooler in-print assist off
#        espooler_state = None
#        if self.has_espooler():
#            espooler_state = self.espooler().get_operation(self.gate_selected)
#            self._espooler_assist_off()
#
#        # Outermost-only suppression of grip release
#        clear_suppress = not self._suppress_release_grip
#        self._suppress_release_grip = True
#        try:
#            yield self
#        finally:
#            # Only the outermost wrapper clears suppression
#            if clear_suppress:
#                self._suppress_release_grip = False
#
#            # Restore sync state (logic can act on global suppression flag)
#            self.reset_sync_gear_to_extruder(prev_sync)
#
#            # Restore espooler state
#            if self.has_espooler() and espooler_state is not None:
#                self.espooler().set_operation(self.gate_selected, espooler_state[1], espooler_state[0])

    # ---------- TMC Stepper Current Control ----------
            
    @contextlib.contextmanager
    def wrap_gear_current(self, percent=100, reason=""):
        """     
            Run block of logic with gear stepper current set to desired percentage and then
            restore to previous setting
        """ 
        prev_percent = self._adjust_gear_current(percent=percent, reason=reason)
        self._gear_current_locked = True 
        try:
            yield self
        finally:
            self._gear_current_locked = False
            self._restore_gear_current(percent=prev_percent)
    
    def _adjust_gear_current(self, gate=None, percent=100, reason="", restore=False):
        current_percent = self.gear_percentage_run_current

        if self._gear_current_locked: return current_percent
        if gate is None: gate = self.gate_selected
        if gate < 0: return current_percent
        if not (0 < percent < 200): return current_percent
        if not self.gear_tmc: return current_percent
        if percent == self.gear_percentage_run_current: return current_percent
          
        gear_stepper_name = mmu_machine.GEAR_STEPPER_CONFIG
        if self.mmu_machine.multigear and gate > 0:
            gear_stepper_name = "%s_%d" % (mmu_machine.GEAR_STEPPER_CONFIG, gate)
        if restore:
            msg = "Restoring MMU %s run current to %d%% ({}A)" % (gear_stepper_name, percent)
        else:   
            msg = "Modifying MMU %s run current to %d%% ({}A) %s" % (gear_stepper_name, percent, reason)
        target_current = (self.gear_default_run_current * percent) / 100.0
        self._set_tmc_current(gear_stepper_name, target_current, msg)
        self.gear_percentage_run_current = percent # Update global record of current %
        return percent

    def _restore_gear_current(self, gate=None, percent=100):
        _ = self._adjust_gear_current(gate=gate, percent=percent, restore=True)

    @contextlib.contextmanager
    def _wrap_extruder_current(self, percent=100, reason=""):
        prev_percent = self._adjust_extruder_current(percent, reason)
        try:
            yield self
        finally:
            self._restore_extruder_current(percent=prev_percent)

    def _adjust_extruder_current(self, percent=100, reason="", restore=False):
        current_percent = self.extruder_percentage_run_current

        if not self.extruder_tmc: return current_percent
        if not (0 < percent < 200): return current_percent
        if percent == self.extruder_percentage_run_current: return current_percent

        if restore:
            msg = "Restoring extruder stepper run current to %d%% ({}A)"
        else:
            msg = "Modifying extruder stepper run current to %d%% ({}A) %s" % (percent, reason)
        self._set_tmc_current(self.extruder_name, (self.extruder_default_run_current * percent) / 100., msg)
        self.extruder_percentage_run_current = percent # Update global record of current %
        return percent

    def _restore_extruder_current(self, percent=100):
        _ = self._adjust_extruder_current(percent=percent, restore=True)

    def _set_tmc_current(self, stepper, run_current, msg):
        self.log_info(msg.format("%.2f" % run_current))
        self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (stepper, run_current))



    # ---------- Pressure Advance Control ----------

    @contextlib.contextmanager
    def _wrap_pressure_advance(self, pa=0, reason=""):
        extruder = self.toolhead.get_extruder()
        initial_pa = extruder.get_status(0).get('pressure_advance')

        if initial_pa is None:
            yield self
            return

        try:
            if reason:
                self.log_debug("Setting pressure advance %s: %.4f" % (reason, pa))
            self._set_pressure_advance(pa)

            yield self

        finally:
            if reason:
                self.log_debug("Restoring pressure advance: %.4f" % initial_pa)
            self._set_pressure_advance(initial_pa)

    def _set_pressure_advance(self, pa):
        self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f QUIET=1" % pa)



    # Logic shared with MMU_TEST_MOVE and _MMU_STEP_MOVE
    def _move_cmd(self, gcmd, trace_str, allow_bypass=False):
        if self.check_if_disabled(): return (0., False, 0., 0.)
        if not allow_bypass and self.check_if_bypass(): return (0., False, 0., 0.)
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None)
        motor = gcmd.get('MOTOR', "gear")
        wait = bool(gcmd.get_int('WAIT', 1, minval=0, maxval=1)) # Wait for move to complete (make move synchronous)
        if motor not in ["gear", "extruder", "gear+extruder", "synced"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder' or 'synced'")
        if motor == "extruder":
            self.selector().filament_release()
        else:
            self.selector().filament_drive()
        self.log_debug("Moving '%s' motor %.1fmm..." % (motor, move))
        return self.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, wait=wait)

    # Logic shared with MMU_TEST_HOMING_MOVE and _MMU_STEP_HOMING_MOVE
    def _homing_move_cmd(self, gcmd, trace_str, allow_bypass=False):
        if self.check_if_disabled(): return (0., False, 0., 0.)
        if not allow_bypass and self.check_if_bypass(): return (0., False, 0., 0.)
        endstop = gcmd.get('ENDSTOP', "default")
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None) # Ignored for extruder led moves
        motor = gcmd.get('MOTOR', "gear")
        if motor not in ["gear", "extruder", "gear+extruder"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder'")
        direction = -1 if move < 0 else 1
        stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', direction, minval=-1, maxval=1)
        if abs(stop_on_endstop) != 1:
            raise gcmd.error("STOP_ON_ENDSTOP can only be 1 (extrude direction) or -1 (retract direction)")
        endstop = self.sensor_manager.get_mapped_endstop_name(endstop)
        valid_endstops = list(self.gear_rail().get_extra_endstop_names())
        if endstop not in valid_endstops:
            raise gcmd.error("Endstop name '%s' is not valid for motor '%s'. Options are: %s" % (endstop, motor, ', '.join(valid_endstops)))
        if self.gear_rail().is_endstop_virtual(endstop) and stop_on_endstop == -1:
            raise gcmd.error("Cannot reverse home on virtual (TMC stallguard) endstop '%s'" % endstop)
        if motor == "extruder":
            self.selector().filament_release()
        else:
            self.selector().filament_drive()
        self.log_debug("Homing '%s' motor to '%s' endstop, up to %.1fmm..." % (motor, endstop, move))
        return self.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, homing_move=stop_on_endstop, endstop_name=endstop)


############################
# TOOL SELECTION FUNCTIONS #
############################

    def _record_tool_override(self):
        tool = self.tool_selected
        if tool >= 0:
            current_speed_factor = self.gcode_move.get_status(0)['speed_factor']
            current_extrude_factor = self.gcode_move.get_status(0)['extrude_factor']
            if self.tool_speed_multipliers[tool] != current_speed_factor or self.tool_extrusion_multipliers[tool] != current_extrude_factor:
                self.tool_speed_multipliers[tool] = current_speed_factor
                self.tool_extrusion_multipliers[tool] = current_extrude_factor
                self.log_debug("Saved speed/extrusion multiplier for tool T%d as %d%% and %d%%" % (tool, current_speed_factor * 100, current_extrude_factor * 100))

    def _restore_tool_override(self, tool):
        if tool == self.tool_selected:
            current_speed_factor = self.gcode_move.get_status(0)['speed_factor']
            current_extrude_factor = self.gcode_move.get_status(0)['extrude_factor']
            speed_factor = self.tool_speed_multipliers[tool]
            extrude_factor = self.tool_extrusion_multipliers[tool]
            self.gcode.run_script_from_command("M220 S%d" % (speed_factor * 100))
            self.gcode.run_script_from_command("M221 S%d" % (extrude_factor * 100))
            if current_speed_factor != speed_factor or current_extrude_factor != extrude_factor:
                self.log_debug("Restored speed/extrusion multiplier for tool T%d as %d%% and %d%%" % (tool, speed_factor * 100, extrude_factor * 100))

    def _set_tool_override(self, tool, speed_percent, extrude_percent):
        if tool == -1:
            for i in range(self.num_gates):
                if speed_percent is not None:
                    self.tool_speed_multipliers[i] = speed_percent / 100
                if extrude_percent is not None:
                    self.tool_extrusion_multipliers[i] = extrude_percent / 100
                self._restore_tool_override(i)
            if speed_percent is not None:
                self.log_debug("Set speed multiplier for all tools as %d%%" % speed_percent)
            if extrude_percent is not None:
                self.log_debug("Set extrusion multiplier for all tools as %d%%" % extrude_percent)
        else:
            if speed_percent is not None:
                self.tool_speed_multipliers[tool] = speed_percent / 100
                self.log_debug("Set speed multiplier for tool T%d as %d%%" % (tool, speed_percent))
            if extrude_percent is not None:
                self.tool_extrusion_multipliers[tool] = extrude_percent / 100
                self.log_debug("Set extrusion multiplier for tool T%d as %d%%" % (tool, extrude_percent))
            self._restore_tool_override(tool)


    # Primary method to select and loads tool. Assumes we are unloaded
    def _select_and_load_tool(self, tool, purge=None):

        try:
            # Determine purge volume for toolchange/load. Valid only during toolchange/load operation
            self.toolchange_purge_volume = self._calc_purge_volume(self._last_tool, tool)

            self.log_debug('Loading tool %s...' % self.selected_tool_string(tool))
            self.select_tool(tool)
            gate = self.ttg_map[tool] if tool >= 0 else self.gate_selected
            if self.gate_status[gate] == GATE_EMPTY:
                if self.endless_spool_enabled and self.p.endless_spool_on_load:
                    next_gate, msg = self._get_next_endless_spool_gate(tool, gate)
                    if next_gate == -1:
                        raise MmuError("Gate %d is empty!\nNo alternatives gates available after checking %s" % (gate, msg))

                    self.log_error("Gate %d is empty! Checking for alternative gates %s" % (gate, msg))
                    self.log_info("Remapping %s to gate %d" % (self.selected_tool_string(tool), next_gate))
                    self._remap_tool(tool, next_gate)
                    self.select_tool(tool)
                else:
                    raise MmuError("Gate %d is empty (and EndlessSpool on load is disabled)\nLoad gate, remap tool to another gate or correct state with 'MMU_CHECK_GATE GATE=%d' or 'MMU_GATE_MAP GATE=%d AVAILABLE=1'" % (gate, gate, gate))

            self.load_sequence(purge=purge)
            self._restore_tool_override(self.tool_selected) # Restore M220 and M221 overrides

        finally:
            self.toolchange_purge_volume = 0.

    # Primary method to unload current tool but retain selection
    def _unload_tool(self, form_tip=None, prev_tool=None):
        if self.filament_pos == FILAMENT_POS_UNLOADED:
            self.log_info("Tool already unloaded")
            return

        self.log_debug("Unloading tool %s" % self.selected_tool_string())
        # Use the actual tool that was in use *before* this toolchange began
        # Falls back to current selection if not provided (backwards compatible)
        self._set_last_tool(self.tool_selected if prev_tool is None else prev_tool)
        self._record_tool_override() # Remember M220 and M221 overrides
        self.unload_sequence(form_tip=form_tip)

# PAUL old logic
#    # Primary method to select and loads tool. Assumes we are unloaded.
#    def _select_and_load_tool(self, tool, purge=None):
#        self.log_debug('Loading tool %s...' % self.selected_tool_string(tool))
#        self.select_tool(tool)
#        gate = self.ttg_map[tool] if tool >= 0 else self.gate_selected
#        if self.gate_status[gate] == self.GATE_EMPTY:
#            if self.enable_endless_spool and self.endless_spool_on_load:
#                next_gate, msg = self._get_next_endless_spool_gate(tool, gate)
#                if next_gate == -1:
#                    raise MmuError("Gate %d is empty!\nNo alternatives gates available after checking %s" % (gate, msg))
#
#                self.log_error("Gate %d is empty! Checking for alternative gates %s" % (gate, msg))
#                self.log_info("Remapping %s to gate %d" % (self.selected_tool_string(tool), next_gate))
#                self._remap_tool(tool, next_gate)
#                self.select_tool(tool)
#            else:
#                raise MmuError("Gate %d is empty (and EndlessSpool on load is disabled)\nLoad gate, remap tool to another gate or correct state with 'MMU_CHECK_GATE GATE=%d' or 'MMU_GATE_MAP GATE=%d AVAILABLE=1'" % (gate, gate, gate))
#
#        self.load_sequence(purge=purge)
#        self._restore_tool_override(self.tool_selected) # Restore M220 and M221 overrides
#
#    # Primary method to unload current tool but retain selection
#    def _unload_tool(self, form_tip=None, prev_tool=None):
#        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
#            self.log_info("Tool already unloaded")
#            return
#
#        self.log_debug("Unloading tool %s" % self.selected_tool_string())
#        # Use the actual tool that was in use *before* this toolchange began.
#        # Falls back to current selection if not provided (backwards compatible).
#        self._set_last_tool(self.tool_selected if prev_tool is None else prev_tool)
#        self._record_tool_override() # Remember M220 and M221 overrides
#        self.unload_sequence(form_tip=form_tip)

    def _auto_home(self, tool=0):
        if not self.selector().is_homed or self.tool_selected == TOOL_GATE_UNKNOWN:
            self.log_info("MMU selector not homed, will home before continuing")
            self.home(tool)
        elif self.filament_pos == FILAMENT_POS_UNKNOWN and self.selector().is_homed:
            self.recover_filament_pos(message=True)

    # Important to always inform use of "toolchange" operation is case there is an error and manual recovery is necessary
    def _note_toolchange(self, m117_msg):
        self._last_toolchange = m117_msg
        if self.p.log_m117_messages:
            self.gcode.run_script_from_command("M117 %s" % m117_msg)

    # Tell the sequence macros about where to move to next
    def _set_next_position(self, next_pos):
        if next_pos:
            self.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_xy VALUE=%s,%s" % (self.p.park_macro, next_pos[0], next_pos[1]))
            self.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=True" % self.p.park_macro)
        else:
            self.wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=False" % self.p.park_macro)


### TOOL AND GATE SELECTION ######################################################

    def home(self, tool, force_unload = None):
        if self.check_if_bypass(): return
        self._set_tool_selected(TOOL_GATE_UNKNOWN)
        self.selector().home(force_unload=force_unload)
        if tool >= 0:
            self.select_tool(tool)
        elif tool == TOOL_GATE_BYPASS:
            self.select_bypass()

    def select_gate(self, gate):
        try:
            if gate == self.gate_selected:
                self.selector().select_gate(gate) # Always give selector a chance to fix position
            else:
                self._next_gate = gate # Valid only during the gate selection process
                _prev_gate = self.gate_selected
                self.selector().select_gate(gate)
                self._set_gate_selected(gate)
                self.led_manager.gate_map_changed(_prev_gate)
                self.led_manager.gate_map_changed(gate)

        except MmuError as ee:
            self.unselect_gate()
            raise ee
        finally:
            self._next_gate = None

    def unselect_gate(self):
        self.selector().select_gate(TOOL_GATE_UNKNOWN) # Required for type-B MMU's to unsync
        self._set_gate_selected(TOOL_GATE_UNKNOWN)
        self._espooler_assist_off()

    def select_tool(self, tool):
        if tool < 0 or tool >= self.num_gates:
            self.log_always("Tool %s does not exist" % self.selected_tool_string(tool))
            return

        gate = self.ttg_map[tool]
        if tool == self.tool_selected and gate == self.gate_selected:
            self.select_gate(gate) # Some selectors need to be re-synced
            return

        self.log_debug("Selecting tool %s on gate %d..." % (self.selected_tool_string(tool), gate))
        self.select_gate(gate)
        self._set_tool_selected(tool)
        self.log_info("Tool %s enabled%s" % (self.selected_tool_string(tool), (" on gate %d" % gate) if tool != gate else ""))

    def select_bypass(self):
        if self.tool_selected == TOOL_GATE_BYPASS and self.gate_selected == TOOL_GATE_BYPASS: return
        self.log_info("Selecting filament bypass...")
        self.select_gate(TOOL_GATE_BYPASS)
        self._set_tool_selected(TOOL_GATE_BYPASS)
        self._set_filament_direction(DIRECTION_LOAD)
        self.log_info("Bypass enabled")

    def _set_tool_selected(self, tool):
        if tool != self.tool_selected:
            self.tool_selected = tool
            self.var_manager.set(VARS_MMU_TOOL_SELECTED, self.tool_selected, write=True)

    def _set_gate_selected(self, gate):
        self.gate_selected = gate

        new_unit = self.find_unit_by_gate(gate)
        if new_unit != self.unit_selected:
            self.unit_selected = new_unit
            self.sensor_manager.reset_active_unit(new_unit)

        self.sensor_manager.reset_active_gate(self.gate_selected) # Call after unit_selected is set
        self.mmu_unit().sync_feedback.set_default_rd() # Will always set rotation_distance

        self.var_manager.set(VARS_MMU_GATE_SELECTED, self.gate_selected, write=True)
        self.active_filament = {
            'filament_name': self.gate_filament_name[gate],
            'material': self.gate_material[gate],
            'color': self.gate_color[gate],
            'spool_id': self.gate_spool_id[gate],
            'temperature': self.gate_temperature[gate],
        } if gate >= 0 else {}

    # Return unit number for gate
    def find_unit_by_gate(self, gate):
        unit = self.mmu_machine.get_mmu_unit_by_gate(gate)
        if unit:
            return unit.unit_index
        self.log_assertion("Gate %d has no unit! Assuming unit 0" % gate)
        return 0

# PAUL moved to calibrator
#    # Set the active gear stepper rotation distance
#    def set_gear_rotation_distance(self, rd):
#        if rd:
#            self.log_trace("Setting gear motor rotation distance: %.4f" % rd)
#            if self.gear_rail.steppers:
#                self.gear_rail.steppers[0].set_rotation_distance(rd)

    def _moonraker_push_lane_data(self, gate_ids = None):
        gate_ids = [(i, self.gate_spool_id[i]) for i in range(self.num_gates)] if gate_ids is None else gate_ids
        if gate_ids:
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("moonraker_push_lane_data", gate_ids=gate_ids)
            except Exception as e:
                self.log_debug("Failed to push lane data to Moonraker: %s" % str(e))

    def _moonraker_sync_lane_data(self):
        # Push all current gate data to Moonraker
        self._moonraker_push_lane_data()

        # Request cleanup of old lanes that no longer exist
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("moonraker_cleanup_lane_data", num_gates=self.num_gates)
        except Exception as e:
            self.log_debug("Failed to cleanup old lane data: %s" % str(e))

### SPOOLMAN INTEGRATION #########################################################

    def _spoolman_sync(self, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_PULL: # Remote gate map
            # This will pull gate assignment and filament attributes from spoolman db thus replacing the local map
            self._spoolman_pull_gate_map(quiet=quiet)
        elif self.p.spoolman_support == SPOOLMAN_PUSH: # Local gate map
            # This will update spoolman with just the gate assignment (for visualization) and will update
            # local gate map attributes with data from spoolman thus overwriting the local map
            self._spoolman_push_gate_map(quiet=quiet)
        elif self.p.spoolman_support == SPOOLMAN_READONLY: # Get filament attributes only
            self._spoolman_update_filaments(quiet=quiet)

    def _spoolman_activate_spool(self, spool_id=-1):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            if spool_id < 0:
                self.log_debug("Spoolman spool_id not set for current gate")
            else:
                if spool_id == 0:
                    self.log_debug("Deactivating spool...")
                    spool_id = None # Moonraker API changed and id=0 no longer deactivates
                else:
                    self.log_debug("Activating spool %s..." % spool_id)
                    webhooks.call_remote_method("spoolman_set_active_spool", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while setting active spool: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Request to send filament data from spoolman db (via moonraker)
    # gate=None means all gates with spool_id, else specific gate
    def _spoolman_update_filaments(self, gate_ids=None, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        if gate_ids is None: # All gates
            pruned_gate_ids = [(g, self.gate_spool_id[g]) for g in range(self.num_gates) if self.gate_spool_id[g] >= 0]
        else:
            pruned_gate_ids = [(g, sid) for g, sid in gate_ids if sid >= 0]
        if len(pruned_gate_ids) > 0:
            self.log_debug("Requesting the following gate/spool_id pairs from Spoolman: %s" % pruned_gate_ids)
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("spoolman_get_filaments", gate_ids=pruned_gate_ids, silent=quiet)
            except Exception as e:
                self.log_error("Error while fetching filament attributes from spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Store the current gate to spool_id mapping in spoolman db (via moonraker)
    def _spoolman_push_gate_map(self, gate_ids=None, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Pushing gate mapping to Spoolman")
        if gate_ids is None: # All gates
            gate_ids = [(i, self.gate_spool_id[i]) for i in range(self.num_gates)]
        try:
            webhooks = self.printer.lookup_object('webhooks')
            self.log_debug("Storing gate map in spoolman db...")
            webhooks.call_remote_method("spoolman_push_gate_map", gate_ids=gate_ids, silent=quiet)
        except Exception as e:
            self.log_error("Error while pushing gate map to spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Request to update local gate based on the remote data stored in spoolman db
    def _spoolman_pull_gate_map(self, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting the gate map from Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_pull_gate_map", silent=quiet)
        except Exception as e:
            self.log_error("Error while requesting gate map from spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Clear the spool to gate association in spoolman db
    def _spoolman_clear_gate_map(self, sync=False, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting to clear the gate map in Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_clear_spools_for_printer", sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while clearing spoolman gate mapping: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Refresh the spoolman cache to pick up changes from elsewhere
    def _spoolman_refresh(self, fix, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting to refresh the spoolman gate cache")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_refresh", fix=fix, silent=quiet)
        except Exception as e:
            self.log_error("Error while refreshing spoolman gate cache: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Force spool to map association in spoolman db
    def _spoolman_set_spool_gate(self, spool_id, gate, sync=False, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Setting spool %d to gate %d directly in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_set_spool_gate", spool_id=spool_id, gate=gate, sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while setting spoolman gate association: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    def _spoolman_unset_spool_gate(self, spool_id=None, gate=None, sync=False, quiet=True):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Unsetting spool %s or gate %s in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_unset_spool_gate", spool_id=spool_id, gate=gate, sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while unsetting spoolman gate association: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Dump spool info to console
    def _spoolman_display_spool_info(self, spool_id):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_get_spool_info", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while displaying spool info: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))

    # Dump spool info to console
    def _spoolman_display_spool_location(self, printer=None):
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_display_spool_location", printer=printer)
        except Exception as e:
            self.log_error("Error while displaying spool location map: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))



### SPOOLMAN COMMANDS ############################################################

    cmd_MMU_SPOOLMAN_help = "Manage spoolman integration"
    def cmd_MMU_SPOOLMAN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_spoolman_enabled(): return

        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        clear = bool(gcmd.get_int('CLEAR', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))
        fix = bool(gcmd.get_int('FIX', 0, minval=0, maxval=1))
        spool_id = gcmd.get_int('SPOOLID', None, minval=1)
        gate = gcmd.get_int('GATE', None, minval=-1, maxval=self.num_gates - 1)
        printer = gcmd.get('PRINTER', None) # Option to see other printers (only for gate association table atm)
        spoolinfo = gcmd.get_int('SPOOLINFO', None, minval=-1) # -1 or 0 is active spool
        run = False

        if refresh:
            # Rebuild cache in moonraker and sync local and remote
            self._spoolman_refresh(fix, quiet=quiet)
            if not sync:
                self._spoolman_sync(quiet=quiet)
            run = True

        if clear:
            # Clear the gate allocation in spoolman db
            self._spoolman_clear_gate_map(sync=self.p.spoolman_support == SPOOLMAN_PULL, quiet=quiet)
            run = True

        if sync:
            # Sync local and remote gate maps
            self._spoolman_sync(quiet=quiet)
            run = True

        # Rest of the options are mutually exclusive
        if spoolinfo is not None:
            # Dump spool info for active spool or specifed spool id
            self._spoolman_display_spool_info(spoolinfo if spoolinfo > 0 else None)

        elif spool_id is not None or gate is not None:
            # Update a record in spoolman db
            if spool_id is not None and gate is not None:
                self._spoolman_set_spool_gate(spool_id, gate, sync=self.p.spoolman_support == SPOOLMAN_PULL, quiet=quiet)
            elif spool_id is None and gate is not None:
                self._spoolman_unset_spool_gate(gate=gate, sync=self.p.spoolman_support == SPOOLMAN_PULL, quiet=quiet)
            elif spool_id is not None and gate is None:
                self._spoolman_unset_spool_gate(spool_id=spool_id, sync=self.p.spoolman_support == SPOOLMAN_PULL, quiet=quiet)

        elif not run:
            if self.p.spoolman_support in [SPOOLMAN_PULL, SPOOLMAN_PUSH]:
                # Display gate association table from spoolman db for specified printer
                self._spoolman_display_spool_location(printer=printer)
            else:
                self.log_error("Spoolman gate map not available. Spoolman mode is: %s" % self.p.spoolman_support)


### CORE GCODE COMMANDS ##########################################################

    cmd_MMU_HOME_help = "Home the MMU selector"
    def cmd_MMU_HOME(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self._fix_started_state()

        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_SELECTOR):
            self.log_always("Not calibrated. Will home to endstop only!")
            tool = -1
            force_unload = 0
        else:
            tool = gcmd.get_int('TOOL', 0, minval=0, maxval=self.num_gates - 1)
            force_unload = gcmd.get_int('FORCE_UNLOAD', None, minval=0, maxval=1)

        try:
            with self.wrap_sync_gear_to_extruder():
                self.home(tool, force_unload=force_unload)
                if tool == -1:
                    self.log_always("Homed")
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_SELECT_help = "Select the specified logical tool (following TTG map) or physical gate"
    def cmd_MMU_SELECT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_loaded(): return
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_SELECTOR, check_gates=[gate] if gate >= 0 else None): return
        self._fix_started_state()

        bypass = gcmd.get_int('BYPASS', -1, minval=0, maxval=1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        if tool == -1 and gate == -1 and bypass == -1:
            raise gcmd.error("Error on 'MMU_SELECT': missing TOOL, GATE or BYPASS")

        try:
            with self.wrap_sync_gear_to_extruder():
                self._select(bypass, tool, gate)
                msg = self._mmu_visual_to_string()
                msg += "\n%s" % self._state_to_string()
                self.log_info(msg, color=True)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_SELECT_BYPASS_help = "Select the filament bypass"
    def cmd_MMU_SELECT_BYPASS(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_SELECTOR): return
        self._fix_started_state()

        try:
            with self.wrap_sync_gear_to_extruder():
                self._select(1, -1, -1)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    def _select(self, bypass, tool, gate):
        if bypass != -1:
            self.select_bypass()
        elif tool != -1:
            self.select_tool(tool)
        else:
            self.select_gate(gate)
            self._ensure_ttg_match()

    cmd_MMU_CHANGE_TOOL_help = "Perform a tool swap (called from Tx command)"
    def cmd_MMU_CHANGE_TOOL(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL, check_gates=[]): return # TODO Hard to tell what gates to check so don't check for now
        self._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        skip_purge = bool(gcmd.get_int('SKIP_PURGE', 0, minval=0, maxval=1))

        # Handle "next_pos" option for toolhead position restoration
        next_pos = None
        sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro and sequence_vars_macro.variables.get('restore_xy_pos', 'last') == 'next':
            # Convert next position to absolute coordinates
            next_pos = gcmd.get('NEXT_POS', None)
            if next_pos:
                try:
                    x, y = map(float, next_pos.split(','))
                    gcode_status = self.gcode_move.get_status(self.reactor.monotonic())
                    if not gcode_status['absolute_coordinates']:
                        gcode_pos = gcode_status['gcode_position']
                        x += gcode_pos[0]
                        y += gcode_pos[1]
                    next_pos = [x, y]
                except (ValueError, KeyError, TypeError) as ee:
                    # If something goes wrong it is better to ignore next pos completely
                    self.log_error("Error parsing NEXT_POS: %s" % str(ee))

        # To support Tx commands linked directly (currently not used because of Mainsail visibility which requires macros)
        cmd = gcmd.get_command().strip()
        match = re.match(r'[Tt](\d{1,3})$', cmd)
        if match:
            tool = int(match.group(1))
            if tool < 0 or tool > self.num_gates - 1:
                raise gcmd.error("Invalid tool")
        else:
            # Special case for UI driven change tool where gate is chosen
            tool = None
            gate = gcmd.get_int('GATE', None, minval=0, maxval=self.num_gates - 1)
            if gate is not None:
                if gate == self.gate_selected:
                    self.log_always("Gate %s is already loaded as %s" % (gate, self.selected_tool_string(tool)))
                    return

                possible_tools = [tool for tool in range(self.num_gates) if self.ttg_map[tool] == gate]
                if not possible_tools:
                    self.log_error("No tool associated with gate %s. Check tool-to-gate mapping with MMU_TTG_MAP" % gate)
                    return

                if self.tool_selected in possible_tools:
                    self._remap_tool(self.tool_selected, gate)
                    tool = self.tool_selected
                else:
                    tool = possible_tools[0]

            if tool is None:
                tool = gcmd.get_int('TOOL', minval=0, maxval=self.num_gates - 1)

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during tool change
                    with self.var_manager.wrap_suspend_write_variables(): # Reduce I/O activity to a minimum
                        self._auto_home(tool=tool)
                        if self.has_encoder():
                            self.encoder().note_clog_detection_length()

                        do_form_tip = FORM_TIP_STANDALONE
                        if skip_tip:
                            do_form_tip = FORM_TIP_NONE
                        elif self.is_printing() and not (standalone or self.p.force_form_tip_standalone):
                            do_form_tip = FORM_TIP_SLICER

                        do_purge = PURGE_STANDALONE
                        if skip_purge:
                            do_purge = PURGE_NONE
                        elif self.is_printing() and not (standalone or self.p.force_purge_standalone):
                            do_purge = PURGE_SLICER

                        tip_msg = ("with slicer tip forming" if do_form_tip == FORM_TIP_SLICER else
                                   "with standalone MMU tip forming" if do_form_tip == FORM_TIP_STANDALONE else
                                   "without tip forming")
                        purge_msg = ("slicer purging" if do_purge == PURGE_SLICER else
                                     "standalone MMU purging" if do_purge == PURGE_STANDALONE else
                                     "without purging")
                        self.log_debug("Tool change initiated %s and %s" % (tip_msg, purge_msg))

                        current_tool_string = self.selected_tool_string()
                        new_tool_string = self.selected_tool_string(tool)

                        # Check if we are already loaded
                        if (
                            tool == self.tool_selected and
                            self.ttg_map[tool] == self.gate_selected and
                            self.filament_pos == FILAMENT_POS_LOADED
                        ):
                            self.log_always("Tool %s is already loaded" % self.selected_tool_string(tool))
                            return

                        # Load only case
                        if self.filament_pos == FILAMENT_POS_UNLOADED:
                            msg = "Tool change requested: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        elif self.tool_selected == tool:
                            msg = "Reloading: %s" % new_tool_string
                            m117_msg = "> %s" % new_tool_string
                        else:
                            # Normal toolchange case
                            msg = "Tool change requested, from %s to %s" % (current_tool_string, new_tool_string)
                            m117_msg = "%s > %s" % (current_tool_string, new_tool_string)

                        self._note_toolchange(m117_msg)
                        self.log_always(msg)

                        # Check if new tool is mapped to current gate
                        if self.ttg_map[tool] == self.gate_selected and self.filament_pos == FILAMENT_POS_LOADED:
                            self.select_tool(tool)
                            self._note_toolchange(self.selected_tool_string(tool))
                            return

                        # Ok, now ready to park and perform the swap
                        self._next_tool = tool # Valid only during the change process - cleared in _continue_after()
                        self.last_statistics = {}
                        self._save_toolhead_position_and_park('toolchange', next_pos=next_pos)
                        self._set_next_position(next_pos) # This can also clear next_position
                        self._track_time_start('total')
                        self.printer.send_event("mmu:toolchange", self._last_tool, self._next_tool)

                        # Remember the tool that was actually in use before any load attempts
                        prev_tool = self.tool_selected

                        attempts = 2 if self.p.retry_tool_change_on_error and (self.is_printing() or standalone) else 1 # TODO Replace with inattention timer
                        try:
                            for i in range(attempts):
                                try:
                                    if self.filament_pos != FILAMENT_POS_UNLOADED:
                                        self._unload_tool(form_tip=do_form_tip, prev_tool=prev_tool)
                                    self._select_and_load_tool(tool, purge=do_purge)
                                    break
                                except MmuError as ee:
                                    if i == attempts - 1:
                                        raise MmuError("%s.\nOccured when changing tool: %s" % (str(ee), self._last_toolchange))
                                    self.log_error("%s.\nOccured when changing tool: %s. Retrying..." % (str(ee), self._last_toolchange))
                                    # Try again but recover_filament_pos will ensure conservative treatment of unload
                                    self.recover_filament_pos()

                            self._track_swap_completed()
                            if self.p.log_m117_messages:
                                self.gcode.run_script_from_command("M117 T%s" % tool)
                        finally:
                            self._track_time_end('total')

                    # Updates swap statistics
                    self.num_toolchanges += 1
                    self._dump_statistics(job=not quiet, gate=not quiet)
                    self._persist_swap_statistics()
                    self._persist_gate_statistics()

                    # Deliberately outside of _wrap_gear_synced_to_extruder() so there is no absolutely no delay after restoring position
                    self._continue_after('toolchange', restore=restore)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))


    cmd_MMU_LOAD_help = "Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTRUDER_ONLY=1)"
    def cmd_MMU_LOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        self._fix_started_state()

        in_bypass = self.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1) or in_bypass)
        skip_purge = bool(gcmd.get_int('SKIP_PURGE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        do_purge = PURGE_STANDALONE if not skip_purge else PURGE_NONE

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament load
                    if self.filament_pos != FILAMENT_POS_UNLOADED:
                        self.log_always("Filament already loaded")
                        return

                    self._note_toolchange("> %s" % self.selected_tool_string())

                    if extruder_only:
                        self.load_sequence(bowden_move=0., extruder_only=True, purge=do_purge)

                    else:
                        self._next_tool = self.tool_selected # Valid only during the load process - cleared in _continue_after()
                        self.last_statistics = {}
                        self._save_toolhead_position_and_park('load')
                        if self.tool_selected == TOOL_GATE_UNKNOWN:
                            self.log_error("Selected gate is not mapped to any tool. Will load filament but be sure to use MMU_TTG_MAP to assign tool")

                        self._select_and_load_tool(self.tool_selected, purge=do_purge)
                        self._persist_gate_statistics()
                        self._continue_after('load', restore=restore)

                    self._persist_swap_statistics()

        except MmuError as ee:
            self.handle_mmu_error("%s.\nOccured when loading tool: %s" % (str(ee), self._last_toolchange))
            if self.tool_selected == TOOL_GATE_BYPASS:
                self._set_filament_pos_state(FILAMENT_POS_UNKNOWN)

    cmd_MMU_UNLOAD_help = "Unloads filament and parks it at the gate or optionally unloads just the extruder (EXTRUDER_ONLY=1)"
    def cmd_MMU_UNLOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        self._fix_started_state()

        if self.filament_pos == FILAMENT_POS_UNLOADED:
            self.log_always("Filament not loaded")
            return

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament unload
                    self._mmu_unload_eject(gcmd)

                    self._persist_swap_statistics()

        except MmuError as ee:
            self.handle_mmu_error("%s.\nOccured when unloading tool" % str(ee))


    cmd_MMU_EJECT_help = "Alias for MMU_UNLOAD if filament is loaded but will fully eject filament from MMU (release from gear) if already in unloaded state"
    def cmd_MMU_EJECT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return

        gate = gcmd.get_int('GATE', self.gate_selected, minval=0, maxval=self.num_gates - 1)
        force = bool(gcmd.get_int('FORCE', 0, minval=0, maxval=1))
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL, check_gates=[gate]): return
        self._fix_started_state()

        can_crossload = (self.mmu_unit().can_crossload or self.mmu_unit().multigear) and self.sensor_manager.has_gate_sensor(SENSOR_GEAR_PREFIX, gate)
        if not can_crossload and gate != self.gate_selected:
            if self.check_if_loaded(): return

        # Determine if full eject_from_gate is necessary
        in_bypass = self.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        can_eject_from_gate = (
            not extruder_only
            and not (in_bypass and self.filament_pos != FILAMENT_POS_UNLOADED and gate >= 0)
            and (
                (self.mmu_unit(gate).multigear and gate != self.gate_selected)
                or self.filament_pos == FILAMENT_POS_UNLOADED
                or force
            )
        )

        if not can_eject_from_gate and self.filament_pos == FILAMENT_POS_UNLOADED:
            self.log_always("Filament not loaded")
            return

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament eject

                    current_gate = self.gate_selected
                    if gate != current_gate:
                        self.select_gate(gate)

                    try:
                        self._mmu_unload_eject(gcmd)
                        if can_eject_from_gate:
                            self.log_always("Ejecting filament out of %s" % ("current gate" if gate == current_gate else "gate %d" % gate))
                            self._eject_from_gate()

                    finally:
                        if self.gate_selected != current_gate:
                            # If necessary or easy restore previous gate
                            if self.is_in_print() or self.mmu_machine.multigear or self.filament_pos != FILAMENT_POS_UNLOADED:
                                self.select_gate(current_gate)
                            else:
                                # Lazy movement means we have side effect of changed tool/gate
                                self._ensure_ttg_match()
                                self._initialize_encoder() # Encoder 0000

                    self._persist_swap_statistics()

        except MmuError as ee:
            self.handle_mmu_error("Filament eject for gate %d failed: %s" % (gate, str(ee)))


    # Common logic for MMU_UNLOAD and MMU_EJECT
    def _mmu_unload_eject(self, gcmd):
        in_bypass = self.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        do_form_tip = FORM_TIP_STANDALONE if not skip_tip else FORM_TIP_NONE

        self._note_toolchange("< %s" % self.selected_tool_string())

        if extruder_only:
            self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=True) # Ensure tool tip is performed
            self.unload_sequence(bowden_move=0., form_tip=do_form_tip, extruder_only=True)
            if in_bypass:
                self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                self.log_always("Please pull the filament out from the MMU")
        else:
            if self.filament_pos != FILAMENT_POS_UNLOADED:
                self.last_statistics = {}
                self._save_toolhead_position_and_park('unload')
                self._unload_tool(form_tip=do_form_tip)
                self._persist_gate_statistics()
                self._continue_after('unload', restore=restore)

    # Bookend for start of MMU based print
    cmd_MMU_PRINT_START_help = "Forces initialization of MMU state ready for print (usually automatic)"
    def cmd_MMU_PRINT_START(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_in_print():
            self._on_print_start()
            self._clear_macro_state(reset=True)

    # Bookend for end of MMU based print
    cmd_MMU_PRINT_END_help = "Forces clean up of state after after print end"
    def cmd_MMU_PRINT_END(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        idle_timeout = gcmd.get_int('IDLE_TIMEOUT', 0, minval=0, maxval=1)
        end_state = gcmd.get('STATE', "complete")
        if not self.is_in_endstate():
            if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
                if not idle_timeout and end_state in ["complete"]:
                    self._save_toolhead_position_and_park("complete")
                self._on_print_end(end_state)
            else:
                raise gcmd.error("Unknown endstate '%s'" % end_state)

    cmd_MMU_LOG_help = "Logs messages in MMU log"
    def cmd_MMU_LOG(self, gcmd):
        msg = gcmd.get('MSG', "").replace("\\n", "\n").replace(" ", UI_SPACE)
        if gcmd.get_int('ERROR', 0, minval=0, maxval=1):
            self.log_error(msg)
        elif gcmd.get_int('DEBUG', 0, minval=0, maxval=1):
            self.log_debug(msg)
        else:
            self.log_info(msg)

    cmd_MMU_PAUSE_help = "Pause the current print and lock the MMU operations"
    def cmd_MMU_PAUSE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        msg = gcmd.get('MSG',"MMU_PAUSE macro was directly called")
        self.handle_mmu_error(msg, force_in_print)

    cmd_MMU_UNLOCK_help = "Wakeup the MMU prior to resume to restore temperatures and timeouts"
    def cmd_MMU_UNLOCK(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self._clear_mmu_error_dialog()
        if self.is_mmu_paused_and_locked():
            self._mmu_unlock()

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_RESUME_help = "Wrapper around default user RESUME macro"
    def cmd_MMU_RESUME(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            # User defined or Klipper default behavior
            self.wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), None)
            return

        self.log_debug("MMU RESUME wrapper called")
        if not self.is_paused():
            self.log_always("Print is not paused. Resume ignored.")
            return

        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        try:
            self._clear_mmu_error_dialog()
            if self.is_mmu_paused_and_locked():
                self._mmu_unlock()

            # Decide if we are ready to resume and give user opportunity to fix state first
            if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD) is True:
                self._set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
                self.log_always("Automatically set filament state to LOADED based on toolhead sensor")
            if self.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_LOADED]:
                raise MmuError("Cannot resume because filament position not indicated as fully loaded (or unloaded). Ensure filament is loaded/unloaded and run:\n MMU_RECOVER LOADED=1 or MMU_RECOVER LOADED=0 or just MMU_RECOVER\nto reset state, then RESUME again")

            # Prevent BASE_RESUME from moving toolhead
            if TOOLHEAD_POSITION_STATE in self.gcode_move.saved_states:
                gcode_pos = self.gcode_move.get_status(self.reactor.monotonic())['gcode_position']
                try:
                    self.gcode_move.saved_states['PAUSE_STATE']['last_position'][:3] = gcode_pos[:3]
                except KeyError:
                    self.log_error("PAUSE_STATE not defined!")

            self.wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), exception=None)
            self._continue_after("resume", force_in_print=force_in_print)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # Not a user facing command - used in automatic wrapper
    cmd_PAUSE_help = "Wrapper around default PAUSE macro"
    def cmd_PAUSE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._fix_started_state() # Get out of 'started' state
            self.log_debug("MMU PAUSE wrapper called")
            self._save_toolhead_position_and_park("pause")
        self.wrap_gcode_command(" ".join(("__PAUSE", gcmd.get_raw_command_parameters())), exception=None)

    # Not a user facing command - used in automatic wrapper
    cmd_CLEAR_PAUSE_help = "Wrapper around default CLEAR_PAUSE macro"
    def cmd_CLEAR_PAUSE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self.log_debug("MMU CLEAR_PAUSE wrapper called")
            self._clear_macro_state()
            if self.saved_toolhead_operation == 'pause':
                self._clear_saved_toolhead_position()
        self.wrap_gcode_command("__CLEAR_PAUSE", exception=None)

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_CANCEL_PRINT_help = "Wrapper around default CANCEL_PRINT macro"
    def cmd_MMU_CANCEL_PRINT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._fix_started_state() # Get out of 'started' state before transistion to cancelled
            self.log_debug("MMU_CANCEL_PRINT wrapper called")
            self._clear_mmu_error_dialog()
            self._save_toolhead_position_and_park("cancel")
            self.wrap_gcode_command("__CANCEL_PRINT", exception=None)
            self._on_print_end("cancelled")
        else:
            self.wrap_gcode_command("__CANCEL_PRINT", exception=None)

    cmd_MMU_RECOVER_help = "Recover the filament location or manually fix MMU state after intervention/error"
    cmd_MMU_RECOVER_param_help = (
        "MMU_RECOVER: %s\n" % cmd_MMU_RECOVER_help
        + "TOOL = t Optionally force the assignment of specified tool number\n"
        + "GATE = g Optionally force the assignment of the specified gate number (fixes TTG map)\n"
        + "BYPASS = 1 Used to force the assignemnt of the bypass Tool/Gate\n"
        + "LOADED = [0|1] Force unloaded or loaded (in the extruder) state\n"
        + "STRICT = 1 If auto-recovering state, allows extended tests including extruder heating\n"
        + "(no parameters for automatic filament position recovery)"
    )
    cmd_MMU_RECOVER_supplement_help = (
        "Examples:\n"
        + "MMU_RECOVER ...automatically recover filament position\n"
        + "MMU_RECOVER LOADED=1 ...to indicate filament is in the extruder\n"
        + "MMU_RECOVER TOOL=2 GATE=3 ...to indicate T2 is currently loaded from gate 3"
    )
    def cmd_MMU_RECOVER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return

        if gcmd.get_int('HELP', 0, minval=0, maxval=1):
            self.log_always(self.format_help(self.cmd_MMU_RECOVER_param_help, self.cmd_MMU_RECOVER_supplement_help), color=True)
            return

        tool = gcmd.get_int('TOOL', TOOL_GATE_UNKNOWN, minval=-2, maxval=self.num_gates - 1)
        mod_gate = gcmd.get_int('GATE', TOOL_GATE_UNKNOWN, minval=-2, maxval=self.num_gates - 1)
        if gcmd.get_int('BYPASS', None, minval=0, maxval=1):
            mod_gate = TOOL_GATE_BYPASS
            tool = TOOL_GATE_BYPASS
        loaded = gcmd.get_int('LOADED', -1, minval=0, maxval=1)
        strict = gcmd.get_int('STRICT', 0, minval=0, maxval=1)

        try:
            if tool == TOOL_GATE_BYPASS:
                self.selector().restore_gate(TOOL_GATE_BYPASS)
                self._set_gate_selected(TOOL_GATE_BYPASS)
                self._set_tool_selected(TOOL_GATE_BYPASS)
                self._ensure_ttg_match()

            elif tool >= 0: # If tool is specified then use and optionally override the gate
                self._set_tool_selected(tool)
                gate = self.ttg_map[tool]
                if mod_gate >= 0:
                    gate = mod_gate
                if gate >= 0:
                    self.selector().restore_gate(gate)
                    self._set_gate_selected(gate)
                    self.log_info("Remapping T%d to gate %d" % (tool, gate))
                    self._remap_tool(tool, gate, loaded)

            elif mod_gate >= 0: # If only gate specified then just reset and ensure tool is correct
                self.selector().restore_gate(mod_gate)
                self._set_gate_selected(mod_gate)
                self._ensure_ttg_match()

            elif tool == TOOL_GATE_UNKNOWN and self.tool_selected == TOOL_GATE_BYPASS and loaded == -1:
                # This is to be able to get out of "stuck in bypass" state
                ts = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
                es = self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
                if ts or es: # TODO use check_all_sensors() call when sensor_manager is fixed
                    self._set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
                else:
                    if es is None and ts is None:
                        self.log_warning("Warning: Making assumption that bypass is unloaded because no toolhead sensors are present")
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)
                self._set_filament_direction(DIRECTION_UNKNOWN)
                return

            if loaded == 1:
                self._set_filament_direction(DIRECTION_LOAD)
                self._set_filament_pos_state(FILAMENT_POS_LOADED)
            elif loaded == 0:
                self._set_filament_direction(DIRECTION_UNLOAD)
                self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
            else:
                # Filament position not specified so auto recover
                self.recover_filament_pos(strict=strict, message=True)

            # Reset sync state
            self.reset_sync_gear_to_extruder(False)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))


### GCODE COMMANDS INTENDED FOR TESTING ##########################################

    cmd_MMU_SOAKTEST_LOAD_SEQUENCE_help = "Soak test tool load/unload sequence"
    def cmd_MMU_SOAKTEST_LOAD_SEQUENCE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_homed(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL): return
        loops = gcmd.get_int('LOOP', 2)
        rand = gcmd.get_int('RANDOM', 0)
        to_nozzle = gcmd.get_int('FULL', 0)
        try:
            with self.wrap_sync_gear_to_extruder():
                for l in range(loops):
                    self.log_always("Testing loop %d / %d" % (l, loops))
                    for t in range(self.num_gates):
                        tool = t
                        if rand == 1:
                            tool = random.randint(0, self.num_gates - 1)
                        gate = self.ttg_map[tool]
                        if self.gate_status[gate] == GATE_EMPTY:
                            self.log_always("Skipping tool %d of %d because gate %d is empty" % (tool, self.num_gates, gate))
                        else:
                            self.log_always("Testing tool %d of %d (gate %d)" % (tool, self.num_gates, gate))
                            if not to_nozzle:
                                self.select_tool(tool)
                                self.load_sequence(bowden_move=100., skip_extruder=True)
                                self.unload_sequence(bowden_move=100.)
                            else:
                                self._select_and_load_tool(tool, purge=PURGE_NONE)
                                self._unload_tool()
                self.select_tool(0)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_TEST_GRIP_help = "Test the MMU grip for a Tool"
    def cmd_MMU_TEST_GRIP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        self.selector().filament_drive()
        self.motors_onoff(on=False, motor="gear")

    cmd_MMU_TEST_TRACKING_help = "Test the tracking of gear feed and encoder sensing"
    def cmd_MMU_TEST_TRACKING(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_homed(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
        step = gcmd.get_float('STEP', 1, minval=0.5, maxval=20)
        sensitivity = gcmd.get_float('SENSITIVITY', self.encoder().get_resolution(), minval=0.1, maxval=10)
        if direction == 0: return
        try:
            with self.wrap_sync_gear_to_extruder():
                if self.filament_pos not in [FILAMENT_POS_START_BOWDEN, FILAMENT_POS_IN_BOWDEN]:
                    # Ready MMU for test if not already setup
                    self._unload_tool()
                    self.load_sequence(bowden_move=100. if direction == DIRECTION_LOAD else 200., skip_extruder=True)
                    self.selector().filament_drive()
                with self._require_encoder():
                    self._initialize_filament_position()
                    for i in range(1, int(100 / step)):
                        self.trace_filament_move(None, direction * step, encoder_dwell=None)
                        measured = self.get_encoder_distance()
                        moved = i * step
                        drift = int(round((moved - measured) / sensitivity))
                        if drift > 0:
                            drift_str = "++++++++!!"[0:drift]
                        elif (moved - measured) < 0:
                            drift_str = "--------!!"[0:-drift]
                        else:
                            drift_str = ""
                        self.log_info("Gear/Encoder : %05.2f / %05.2f mm %s" % (moved, measured, drift_str))
                self._unload_tool()
        except MmuError as ee:
            self.handle_mmu_error("Tracking test failed: %s" % str(ee))

    cmd_MMU_TEST_LOAD_help = "For quick testing filament loading from gate to the extruder"
    def cmd_MMU_TEST_LOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        full = gcmd.get_int('FULL', 0, minval=0, maxval=1)
        try:
            with self.wrap_sync_gear_to_extruder():
                if full:
                    self.load_sequence(skip_extruder=True)
                else:
                    length = gcmd.get_float('LENGTH', 100., minval=10., maxval=self.calibration_manager.get_bowden_length())
                    self.load_sequence(bowden_move=length, skip_extruder=True)
        except MmuError as ee:
            self.handle_mmu_error("Load test failed: %s" % str(ee))

    cmd_MMU_TEST_MOVE_help = "Test filament move to help debug setup / options"
    def cmd_MMU_TEST_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1)) # Hidden option
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))

        with self.wrap_sync_gear_to_extruder():
            with DebugStepperMovement(self, debug):
                actual,_,measured,_ = self._move_cmd(gcmd, "Test move", allow_bypass=allow_bypass)
            self.log_always("Moved %.1fmm%s" % (actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_HOMING_MOVE_help = "Test filament homing move to help debug setup / options"
    def cmd_MMU_TEST_HOMING_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        allow_bypass = bool(gcmd.get_int('ALLOW_BYPASS', 0, minval=0, maxval=1))

        with self.wrap_sync_gear_to_extruder():
            debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1)) # Hidden option
            with DebugStepperMovement(self, debug):
                actual,homed,measured,_ = self._homing_move_cmd(gcmd, "Test homing move", allow_bypass=allow_bypass)
            self.log_always("%s after %.1fmm%s" % (("Homed" if homed else "Did not home"), actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_CONFIG_help = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    def cmd_MMU_TEST_CONFIG(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))

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
            self.calibration_manager.adjust_bowden_lengths_on_homing_change()

        # Special bowden calibration (get current length after potential gate_homing_endstop change)
        gate_selected = max(self.gate_selected, 0) # Assume gate 0 if not known / bypass
        bowden_length = gcmd.get_float('MMU_CALIBRATION_BOWDEN_LENGTH', self.bowden_lengths[gate_selected], minval=0.)
        if bowden_length != self.bowden_lengths[gate_selected]:
            self.calibration_manager.update_bowden_length(bowden_length, gate_selected)

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
        if self.has_encoder():
            cal_clog_length = self.var_manager.save_variables.allVariables.get(VARS_MMU_CALIB_CLOG_LENGTH, None)
            clog_length = gcmd.get_float('MMU_CALIBRATION_CLOG_LENGTH', cal_clog_length, minval=1., maxval=100.)
            if clog_length != cal_clog_length:
                self.calibration_manager.update_clog_detection_length(clog_length, force=True)

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


###########################################
# RUNOUT, ENDLESS SPOOL and GATE HANDLING #
###########################################

    # Handler for all "runout" type events including "clog" and "tangle".
    # If event_type is None then caller isn't sure (runout or clog)
    def _runout(self, event_type=None, sensor=None):
        with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during handling
            self.is_handling_runout = (event_type == "runout") # Best starting assumption
            self._save_toolhead_position_and_park('runout') # includes "clog" and "tangle"

            type_str = event_type or "runout/clog/tangle"
            if self.tool_selected < 0:
                raise MmuError("Filament %s on an unknown or bypass tool\nManual intervention is required" % type_str)

            if self.filament_pos != FILAMENT_POS_LOADED and event_type is None:
                raise MmuError("Filament %s occured but filament is marked as not loaded(?)\nManual intervention is required" % type_str)

            self.log_debug("Issue on tool T%d" % self.tool_selected)

            # Check for clog/tangle by looking for filament at the gate (or in the encoder)
            if event_type is None:
                if not self.check_filament_runout():
                    if self.has_encoder():
# MOGGIE encoder_sensor
                        self.encoder_sensor.note_clog_detection_length()
                    # Eliminate runout
                    event_type = "clog/tangle"
                    self.is_handling_runout = False
                    raise MmuError("A clog/tangle has been detected and requires manual intervention")
                else:
                    # We definitely have a filament runout
                    type_str = event_type = "runout"
                    self.is_handling_runout = True # Will remain true until complete and continue or resume after error

            if event_type == "runout":
                if self.endless_spool_enabled:
                    self._next_tool = self.tool_selected # Valid only during the reload process - cleared in _continue_after()
                    self._set_gate_status(self.gate_selected, GATE_EMPTY) # Indicate current gate is empty
                    next_gate, msg = self._get_next_endless_spool_gate(self.tool_selected, self.gate_selected)
                    if next_gate == -1:
                        raise MmuError("Runout detected on %s\nNo alternative gates available after checking %s" % (sensor, msg))

                    self.log_error("A runout has been detected. Checking for alternative gates %s" % msg)
                    self.log_info("Remapping T%d to gate %d" % (self.tool_selected, next_gate))

                    if self.p.endless_spool_eject_gate > 0:
                        self.log_info("Ejecting filament remains to designated waste gate %d" % self.p.endless_spool_eject_gate)
                        self.select_gate(self.p.endless_spool_eject_gate)
                    self._unload_tool(form_tip=FORM_TIP_STANDALONE)
                    self._eject_from_gate() # Push completely out of gate
                    self.select_gate(next_gate) # Necessary if unloaded to waste gate
                    self._remap_tool(self.tool_selected, next_gate)
                    self._select_and_load_tool(self.tool_selected, purge=PURGE_STANDALONE) # if user has set up standalone purging, respect option and purge.

                    self._continue_after("endless_spool")
                    self.pause_resume.send_resume_command() # Undo what runout sensor handling did
                    return
                else:
                    raise MmuError("Runout detected on %s\nEndlessSpool mode is off - manual intervention is required" % sensor)

            raise MmuError("A %s has been detected on %s and requires manual intervention" % (type_str, sensor))

    def _get_next_endless_spool_gate(self, tool, gate):
        group = self.endless_spool_groups[gate]
        next_gate = -1
        checked_gates = []
        for i in range(self.num_gates - 1):
            check = (gate + i + 1) % self.num_gates
            if self.endless_spool_groups[check] == group:
                checked_gates.append(check)
                if self.gate_status[check] != GATE_EMPTY:
                    next_gate = check
                    break
        alt_gates = "(checked gates: %s)" % ",".join(map(str, checked_gates))
        msg = "for T%d in EndlessSpool Group %s %s" % (tool, chr(ord('A') + group), alt_gates)
        return next_gate, msg

    # Use pre-gate (and gear) sensors to "correct" gate status
    # Return updated gate_status adjusted by sensor readings
    def _validate_gate_status(self, gate_status):
        v_gate_status = list(gate_status) # Ensure that webhooks sees get_status() change
        for gate, status in enumerate(v_gate_status):
            gear_detected = self.sensor_manager.check_gate_sensor(SENSOR_GEAR_PREFIX, gate)
            if gear_detected is True:
                v_gate_status[gate] = GATE_AVAILABLE
            else:
                pre_detected = self.sensor_manager.check_gate_sensor(SENSOR_PRE_GATE_PREFIX, gate)
                if pre_detected is True and status == GATE_EMPTY:
                    v_gate_status[gate] = GATE_UNKNOWN
                elif pre_detected is False and status != GATE_EMPTY:
                    v_gate_status[gate] = GATE_EMPTY
        return v_gate_status

    # Use post-gear sensors to correct the selected gate.
    # Returns the unique detected gate index, or None if zero/multiple detected.
    def _validate_gate_selected(self):
        gate = None
        for g in range(self.num_gates):
            if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, g, loading=True) is True:
                if gate is None:
                    gate = g
                else:
                    return None
        return gate

    def _get_filament_char(self, gate, no_space=False, show_source=False):
        show_source &= self.mmu_unit().p.has_filament_buffer
        gate_status = self.gate_status[gate]
        if self.endless_spool_enabled and gate == self.p.endless_spool_eject_gate:
            return "W"
        elif gate_status == GATE_AVAILABLE_FROM_BUFFER:
            return "B" if show_source else "*"
        elif gate_status == GATE_AVAILABLE:
            return "S" if show_source else "*"
        elif gate_status == GATE_EMPTY:
            return (UI_SEPARATOR if no_space else " ")
        else:
            return "?"

    def _ttg_map_to_string(self, tool=None, show_groups=True):
        if show_groups:
            msg = "TTG Map & EndlessSpool Groups:\n"
        else:
            msg = "TTG Map:\n" # String used to filter in KS-HH
        num_tools = self.num_gates
        tools = range(num_tools) if tool is None else [tool]
        for i in tools:
            gate = self.ttg_map[i]
            filament_char = self._get_filament_char(gate, show_source=False)
            msg += "\n" if i and tool is None else ""
            msg += "T{:<2}-> Gate{:>2}({})".format(i, gate, filament_char)

            if show_groups and self.endless_spool_enabled:
                group = self.endless_spool_groups[gate]
                msg += " Group %s:" % chr(ord('A') + group)
                gates_in_group = [(j + gate) % num_tools for j in range(num_tools)]
                #msg += " >".join("{:>2}({})".format(g, self._get_filament_char(g, show_source=False)) for g in gates_in_group if self.endless_spool_groups[g] == group)
                msg += " >".join("{:>2}".format(g) for g in gates_in_group if self.endless_spool_groups[g] == group)

            if i == self.tool_selected:
                msg += " [SELECTED]"
        return msg

    def _mmu_visual_to_string(self):
        divider = UI_SPACE + UI_SEPARATOR + UI_SPACE
        msg_units = "Unit : "
        msg_gates = "Gate : "
        msg_avail = "Avail: "
        msg_tools = "Tools: "
        msg_selct = "Selct: "
        for unit in range(self.mmu_machine.num_units):
            unit = self.mmu_machine.get_mmu_unit_by_index(unit)
            gate_indices = range(unit.first_gate, unit.first_gate + unit.num_gates)
            last_gate = gate_indices[-1] == self.num_gates - 1
            sep = ("|" + divider) if not last_gate else "|"
            tool_strings = []
            select_strings = []
            for g in gate_indices:
                msg_gates += "".join("|{:^3}".format(g) if g < 10 else "| {:2}".format(g))
                msg_avail += "".join("| %s " % self._get_filament_char(g, no_space=True, show_source=True))
                tool_str = "+".join("T%d" % t for t in range(self.num_gates) if self.ttg_map[t] == g)
                tool_strings.append(("|%s " % (tool_str if tool_str else " {} ".format(UI_SEPARATOR)))[:4])
                if self.gate_selected == g and self.gate_selected != TOOL_GATE_UNKNOWN:
                    select_strings.append("|\%s/|" % (UI_SEPARATOR if self.filament_pos < FILAMENT_POS_START_BOWDEN else "*"))
                else:
                    select_strings.append("----")
            unit_str = "{0:-^{width}}".format( " " + str(unit) + " ", width=len(gate_indices) * 4 + 1)
            msg_units += unit_str + (divider if not last_gate else "")
            msg_gates += sep
            msg_avail += sep
            msg_tools += "".join(tool_strings) + sep
            msg_selct += ("".join(select_strings) + "-")[:len(gate_indices) * 4 + 1] + (divider if not last_gate else "")
        lines = [msg_units] if len(self.mmu_machine.units) > 1 else []
        lines.extend([msg_gates, msg_tools, msg_avail, msg_selct])
        msg = "\n".join(lines)
        if self.selector().is_homed:
            msg += " " + self.selected_tool_string()
        else:
            msg += " NOT HOMED"
        return msg

    def _es_groups_to_string(self, title=None):
        msg = "%s:\n" % title if title else "EndlessSpool Groups:\n"
        groups = {}
        for gate in range(self.num_gates):
            group = self.endless_spool_groups[gate]
            if group not in groups:
                groups[group] = [gate]
            else:
                groups[group].append(gate)
        msg += "\n".join(
            "Group %s: Gates: %s" % (chr(ord('A') + group), ", ".join(map(str, gates)))
            for group, gates in groups.items()
        )
        return msg

    def _gate_map_to_string(self, detail=False):
        msg = "Gates / Filaments:" # String used to filter in KS-HH
        available_status = {
            GATE_AVAILABLE_FROM_BUFFER: "Buffer",
            GATE_AVAILABLE: "Spool",
            GATE_EMPTY: "Empty",
            GATE_UNKNOWN: "Unknown"
        }

        for g in range(self.num_gates):
            available = available_status[self.gate_status[g]]
            name = self.gate_filament_name[g] or "Unknown"
            material = self.gate_material[g] or "Unknown"
            color = self._format_color(self.gate_color[g] or "n/a")
            temperature = self.gate_temperature[g] or "n/a"

            gate_fstr = ""
            if detail:
                filament_char = self._get_filament_char(g, show_source=False)
                tools = ",".join("T{}".format(t) for t in range(self.num_gates) if self.ttg_map[t] == g)
                tools_fstr = (" [{}]".format(tools) if tools else "")
                gate_fstr = "{}".format(g).ljust(2, UI_SPACE)
                gate_fstr = "{}({}){}:".format(gate_fstr, filament_char, tools_fstr).ljust(15, UI_SPACE)
            else:
                gate_fstr = "{}:".format(g).ljust(3, UI_SPACE)

            available_fstr = "{};".format(available).ljust(9, UI_SPACE)
            fil_fstr = "{} | {}{}C | {} | {}".format(material, temperature, UI_DEGREE, color, name)

            spool_option = (str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a")
            if self.p.spoolman_support == SPOOLMAN_OFF:
                spool_fstr = ""
            elif self.gate_spool_id[g] <= 0:
                spool_fstr = "Id: {};".format(spool_option).ljust(12, UI_SPACE)
            else:
                spool_fstr = "Id: {}".format(spool_option).ljust(8, UI_SPACE) + "--> "

            speed_fstr = " [Speed:{}%]".format(self.gate_speed_override[g]) if self.gate_speed_override[g] != 100 else ""
            extra_fstr = " [Selected]" if detail and g == self.gate_selected else ""

            msg += "\n{}{}{}{}{}{}".format(gate_fstr, available_fstr, spool_fstr, fil_fstr, speed_fstr, extra_fstr)
        return msg

    # Remap a tool/gate relationship and gate filament availability
    def _remap_tool(self, tool, gate, available=None):
        self.ttg_map = list(self.ttg_map) # Ensure that webhook sees get_status() change
        self.ttg_map[tool] = gate
        self._persist_ttg_map()
        self._ensure_ttg_match()
        self._update_slicer_color_rgb() # Indexed by gate
        if available is not None:
            self._set_gate_status(gate, available)

    # Find and set a tool that maps to gate (for recovery)
    def _ensure_ttg_match(self):
        if self.gate_selected in [TOOL_GATE_UNKNOWN, TOOL_GATE_BYPASS]:
            self._set_tool_selected(self.gate_selected)
        else:
            possible_tools = [tool for tool in range(self.num_gates) if self.ttg_map[tool] == self.gate_selected]
            if possible_tools:
                if self.tool_selected not in possible_tools:
                    self.log_debug("Resetting tool selected to match TTG map for current gate (%d)" % self.gate_selected)
                    self._set_tool_selected(possible_tools[0])
            else:
                self.log_warning("Resetting tool selected to unknown because current gate (%d) isn't associated with tool in TTG map" % self.gate_selected)
                self._set_tool_selected(TOOL_GATE_UNKNOWN)

    def _persist_ttg_map(self):
        self.var_manager.set(VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map, write=True)

    def _reset_ttg_map(self):
        self.log_debug("Resetting TTG map")
        self.ttg_map = list(self.p.default_ttg_map)
        self._persist_ttg_map()
        self._ensure_ttg_match()
        self._update_slicer_color_rgb() # Indexed by gate

    def _persist_endless_spool(self):
        self.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.endless_spool_enabled)
        self.var_manager.set(VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        self.var_manager.write()

    def _reset_endless_spool(self):
        self.log_debug("Resetting Endless Spool mapping")
        self.endless_spool_enabled = self.p.default_endless_spool_enabled
        self.endless_spool_groups = list(self.p.default_endless_spool_groups)
        self._persist_endless_spool()

    def _set_gate_status(self, gate, state):
        if 0 <= gate < self.num_gates:
            if state != self.gate_status[gate]:
                self.gate_status = list(self.gate_status) # Ensure that webhooks sees get_status() change
                self.gate_status[gate] = state
                self._persist_gate_status()
                self.led_manager.gate_map_changed(gate)
                self.mmu_macro_event(MACRO_EVENT_GATE_MAP_CHANGED, "GATE=%d" % gate)

    def _persist_gate_status(self):
        self.var_manager.set(VARS_MMU_GATE_STATUS, self.gate_status, write=True)

    # Ensure that webhooks sees get_status() change after gate map update. It is important to call this prior to
    # updating gate_map so change is always seen. This approach removes need to copy lists on every call to get_status()
    def _renew_gate_map(self):
        self.gate_status = list(self.gate_status)
        self.gate_filament_name = list(self.gate_filament_name)
        self.gate_material = list(self.gate_material)
        self.gate_color = list(self.gate_color)
        self.gate_temperature = list(self.gate_temperature)
        self.gate_spool_id = list(self.gate_spool_id)
        self.gate_speed_override = list(self.gate_speed_override)

    def _persist_gate_map(self, spoolman_sync=False, gate_ids=None):
        self.var_manager.set(VARS_MMU_GATE_STATUS, self.gate_status)
        self.var_manager.set(VARS_MMU_GATE_FILAMENT_NAME, self.gate_filament_name)
        self.var_manager.set(VARS_MMU_GATE_MATERIAL, self.gate_material)
        self.var_manager.set(VARS_MMU_GATE_COLOR, self.gate_color)
        self.var_manager.set(VARS_MMU_GATE_TEMPERATURE, self.gate_temperature)
        self.var_manager.set(VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id)
        self.var_manager.set(VARS_MMU_GATE_SPEED_OVERRIDE, self.gate_speed_override)
        self.var_manager.write()
        self._update_t_macros()

        # Also persist to spoolman db if pushing updates for visability
        if spoolman_sync:
            if self.p.spoolman_support == SPOOLMAN_PUSH:
                if gate_ids is None:
                    gate_ids = list(enumerate(self.gate_spool_id))
                if gate_ids:
                    self._spoolman_push_gate_map(gate_ids)
            elif self.p.spoolman_support == SPOOLMAN_READONLY:
                self._spoolman_update_filaments(gate_ids)

        self.led_manager.gate_map_changed(None)
        if self.printer.lookup_object("gcode_macro %s" % self.p.mmu_event_macro, None) is not None:
            self.mmu_macro_event(MACRO_EVENT_GATE_MAP_CHANGED, "GATE=-1")

    def _reset_gate_map(self):
        self.log_debug("Resetting gate map")
        self.gate_status = self._validate_gate_status(self.p.default_gate_status)
        self.gate_filament_name = list(self.p.default_gate_filament_name)
        self.gate_material = list(self.p.default_gate_material)
        self.gate_color = list(self.p.default_gate_color)
        self.gate_temperature = list(self.p.default_gate_temperature)
        if self.p.spoolman_support in [SPOOLMAN_OFF, SPOOLMAN_PULL]:
            self.gate_spool_id = [-1] * self.num_gates
        else:
            self.gate_spool_id = list(self.p.default_gate_spool_id)
        self.gate_speed_override = list(self.p.default_gate_speed_override)
        self._update_gate_color_rgb()
        self._persist_gate_map(spoolman_sync=True)

    def _automap_gate(self, tool, strategy):
        if tool is None:
            self.log_error("Automap tool called without a tool argument")
            return
        tool_to_remap = self.slicer_tool_map['tools'][str(tool)]
        # strategy checks
        if strategy in ['spool_id']:
            self.log_error("'%s' automapping strategy is not yet supported. Support for this feature is on the way, please be patient." % strategy)
            return

        # Create printable strategy string
        strategy_str = strategy.replace("_", " ").title()

        # Deduct search_in and tool_field based on strategy
        # tool fields are like {'color': color, 'material': material, 'temp': temp, 'name': name, 'in_use': used}
        if strategy == AUTOMAP_FILAMENT_NAME:
            search_in = self.gate_filament_name
            tool_field = 'name'
        elif strategy == AUTOMAP_SPOOL_ID:
            search_in = self.gate_spool_id
            tool_field = 'spool_id' # Placeholders for future support
        elif strategy == AUTOMAP_MATERIAL:
            search_in = self.gate_material
            tool_field = 'material'
        elif strategy in [AUTOMAP_CLOSEST_COLOR, AUTOMAP_COLOR]:
            search_in = self.gate_color
            tool_field = 'color'
        else:
            self.log_error("Invalid automap strategy '%s'" % strategy)
            return

        # Automapping logic
        errors = []
        warnings = []
        messages = []
        remaps = []

        if not tool_to_remap[tool_field]:
            errors.append("%s of tool %s must be set. When using automapping all referenced tools must have a %s" % (tool_field, tool, strategy_str))

        if not errors:
            # 'standard' exactly matching fields
            if strategy != AUTOMAP_CLOSEST_COLOR:
                for gn, gate_feature in enumerate(search_in):
                    # When matching by name normalize possible unicode characters and match case-insensitive
                    if strategy == AUTOMAP_FILAMENT_NAME:
                        equal = self._compare_unicode(tool_to_remap[tool_field], gate_feature)
                    elif strategy == AUTOMAP_COLOR:
                        equal = tool_to_remap[tool_field].upper().ljust(8,'F') == gate_feature.upper().ljust(8,'F')
                    else:
                        equal = tool_to_remap[tool_field] == gate_feature
                    if equal:
                        remaps.append("T%s --> G%s (%s)" % (tool, gn, gate_feature))
                        self.wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))
                if not remaps:
                    errors.append("No gates found for tool %s with %s %s" % (tool, strategy_str, tool_to_remap[tool_field]))

            # 'colors' search for closest
            elif strategy == AUTOMAP_CLOSEST_COLOR:
                if tool_to_remap['material'] == "unknown":
                    errors.append("When automapping with closest color, the tool material must be set.")
                if tool_to_remap['material'] not in self.gate_material:
                    errors.append("No gate has a filament matching the desired material (%s). Available are : %s" % (tool_to_remap['material'], self.gate_material))
                if not errors:
                    color_list = []
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material'].lower():
                            color_list.append(color)
                    if not color_list:
                        errors.append("Gates with %s are missing color information..." % tool_to_remap['material'])

                if not errors:
                    closest, distance = self._find_closest_color(tool_to_remap['color'], color_list)
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material'].lower():
                            if closest == color:
                                t = self.p.console_gate_stat
                                if distance > 0.5:
                                    warnings.append("Color matching is significantly different ! %s" % (UI_EMOTICONS[7] if t == 'emoticon' else ''))
                                elif distance > 0.2:
                                    warnings.append("Color matching might be noticebly different %s" % (UI_EMOTICONS[5] if t == 'emoticon' else ''))
                                elif distance > 0.05:
                                    warnings.append("Color matching seems quite good %s" % (UI_EMOTICONS[3] if t == 'emoticon' else ''))
                                elif distance > 0.02:
                                    warnings.append("Color matching is excellent %s" % (UI_EMOTICONS[2] if t == 'emoticon' else ''))
                                elif distance < 0.02:
                                    warnings.append("Color matching is perfect %s" % (UI_EMOTICONS[1] if t == 'emoticon' else ''))
                                remaps.append("T%s --> G%s (%s with closest color: %s)" % (tool, gn, gm, color))
                                self.wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))

                if not remaps:
                    errors.append("Unable to find a suitable color for tool %s (color: %s)" % (tool, tool_to_remap['color']))
            if len(remaps) > 1:
                warnings.append("Multiple gates found for tool %s with %s '%s'" % (tool, strategy_str, tool_to_remap[tool_field]))

        # Display messages while automapping
        if remaps:
            remaps.insert(0, "Automatically mapped tool %s based on %s" % (tool, strategy_str))
            for msg in remaps:
                self.log_always(msg)
        if messages:
            for msg in messages:
                self.log_always(msg)
        # Display warnings while automapping
        for msg in warnings:
            self.log_info(msg)
        # Display errors while automapping
        if errors:
            reason = ["Error during automapping"]
            if self.is_printing():
                self.handle_mmu_error("\n".join(reason+errors))
            else:
                self.log_error(reason[0])
                for e in errors:
                    self.log_error(e)

    # Set 'color' and 'spool_id' variable on the Tx macro for Mainsail/Fluidd to pick up
    # We don't use SET_GCODE_VARIABLE because the macro variable may not exist ahead of time
    def _update_t_macros(self):
        for tool in range(self.num_gates):
            gate = self.ttg_map[tool]
            t_macro = self.printer.lookup_object("gcode_macro T%d" % tool, None)

            if t_macro:
                t_vars = dict(t_macro.variables) # So Mainsail sees the update

                spool_id = self.gate_spool_id[gate]
                if (self.p.t_macro_color != T_MACRO_COLOR_OFF and
                    spool_id >= 0 and
                    self.p.spoolman_support != SPOOLMAN_OFF and
                    self.gate_status[gate] != GATE_EMPTY):

                    t_vars['spool_id'] = self.gate_spool_id[gate]
                else:
                    t_vars.pop('spool_id', None)

                if self.p.t_macro_color == T_MACRO_COLOR_SLICER:
                    st = self.slicer_tool_map['tools'].get(str(tool), None)
                    rgb_hex = self._color_to_rgb_hex(st.get('color', None)) if st else None
                    if rgb_hex:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)

                elif self.p.t_macro_color in [T_MACRO_COLOR_GATEMAP, T_MACRO_COLOR_ALLGATES]:
                    rgb_hex = self._color_to_rgb_hex(self.gate_color[gate])
                    if self.gate_status[gate] != GATE_EMPTY or self.p.t_macro_color == T_MACRO_COLOR_ALLGATES:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)

                else: # 'off' case
                    t_vars.pop('color', None)

                t_macro.variables = t_vars

### GCODE COMMANDS FOR RUNOUT, TTG MAP, GATE MAP and GATE LOGIC ##################

    cmd_MMU_TEST_RUNOUT_help = "Manually invoke the clog/runout detection logic for testing"
    def cmd_MMU_TEST_RUNOUT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return

        event_type = gcmd.get('TYPE', None)
        try:
            with self.wrap_sync_gear_to_extruder():
                self._runout(event_type=event_type, sensor="TEST")
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_ENCODER_RUNOUT_help = "Internal encoder filament runout handler"
    def cmd_MMU_ENCODER_RUNOUT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            self.pause_resume.send_resume_command() # Undo what runout sensor handling did
            return
        self._fix_started_state()
        try:
            with self.wrap_sync_gear_to_extruder():
                self._runout(sensor="Encoder")
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_ENCODER_INSERT_help = "Internal encoder filament insert detection handler"
    def cmd_MMU_ENCODER_INSERT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        # TODO Possible future bypass preload feature - make gate act like bypass

    # Callback to handle runout event from an MMU sensors. Note that pause_resume.send_pause_command()
    # will have already been issued but no PAUSE command
    # Params:
    #   EVENTTIME will contain reactor time that the sensor triggered and command was queued
    #   SENSOR will contain sensor name
    #   GATE will be set if specific pre-gate or gear sensor
    cmd_MMU_SENSOR_RUNOUT_help= "Internal MMU filament runout handler"
    def cmd_MMU_SENSOR_RUNOUT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            self.pause_resume.send_resume_command() # Undo what runout sensor handling did
            return
        self._fix_started_state()
        eventtime = gcmd.get_float('EVENTTIME', self.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = self.sensor_manager.get_unitless_sensor_name(raw_sensor)
        process_runout = False

        try:
            with self.wrap_sync_gear_to_extruder():
                if eventtime < self.runout_last_enable_time:
                    self.log_assertion("Late sensor runout event on %s. Ignored" % raw_sensor)

                elif sensor and self.sensor_manager.check_sensor(sensor):
                    self.log_assertion("Runout handler suspects sensor malfunction on %s. Ignored" % raw_sensor)

                else:
                    # Always update gate map from pre-gate sensor
                    if sensor.startswith(SENSOR_PRE_GATE_PREFIX) and gate != self.gate_selected:
                        self._set_gate_status(gate, GATE_EMPTY)

                    # Real runout to process...
                    if sensor.startswith(SENSOR_PRE_GATE_PREFIX) and gate == self.gate_selected:
                        if self.endless_spool_enabled and self.p.endless_spool_eject_gate == gate:
                            self.log_trace("Ignoring filament runout detected by %s because endless_spool_eject_gate is active on that gate" % raw_sensor)
                        else:
                            process_runout = True

                    elif sensor == SENSOR_GATE and gate is None:
                        process_runout = True

                    elif sensor.startswith(SENSOR_GEAR_PREFIX) and gate == self.gate_selected:
                        process_runout = True

                    elif sensor.startswith(SENSOR_EXTRUDER_ENTRY):
                        raise MmuError("Filament runout occured at extruder. Manual intervention is required")

                    else:
                        self.log_assertion("Unexpected/unhandled sensor runout event on %s. Ignored" % raw_sensor)

                if process_runout:
                    self._runout(event_type="runout", sensor=sensor) # Will send_resume_command() or fail and pause
                else:
                    self.pause_resume.send_resume_command() # Undo what runout sensor handling did
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_SENSOR_CLOG_help= "Internal MMU filament clog handler"
    def cmd_MMU_SENSOR_CLOG(self, gcmd):
        try:
            with self.wrap_sync_gear_to_extruder():
                self._clog_tangle(gcmd, "clog")
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_SENSOR_TANGLE_help= "Internal MMU filament tangle handler"
    def cmd_MMU_SENSOR_TANGLE(self, gcmd):
        try:
            with self.wrap_sync_gear_to_extruder():
                self._clog_tangle(gcmd, "tangle")
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # Common callback to handle clog/tangle event from an MMU sensors.
    # Note that pause_resume.send_pause_command() will have already been issued but no PAUSE command
    # Params:
    #   EVENTTIME will contain reactor time that the sensor triggered and command was queued
    #   SENSOR will contain sensor name
    def _clog_tangle(self, gcmd, event_type):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            self.pause_resume.send_resume_command() # Undo what runout sensor handling did
            return
        self._fix_started_state()
        eventtime = gcmd.get_float('EVENTTIME', self.reactor.monotonic())
        sensor = gcmd.get('SENSOR', "")
        self._runout(event_type=event_type, sensor=sensor) # Will send_resume_command() or fail and pause

    # Callback to handle insert event from an MMU sensor
    # Params:
    #   EVENTTIME will contain reactor time that the sensor triggered and command was queued
    #   SENSOR will contain sensor name
    #   GATE will be set if specific pre-gate or gear sensor
    cmd_MMU_SENSOR_INSERT_help= "Internal MMU filament insertion handler"
    def cmd_MMU_SENSOR_INSERT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()
        eventtime = gcmd.get_float('EVENTTIME', self.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = self.sensor_manager.get_unitless_sensor_name(raw_sensor)

        try:
            with self.wrap_sync_gear_to_extruder():
                if sensor.startswith(SENSOR_PRE_GATE_PREFIX) and gate is not None:
                    self._set_gate_status(gate, GATE_UNKNOWN)
                    self._check_pending_spool_id(gate) # Have spool_id ready?
                    if not self.is_printing() and self.mmu_unit().p.gate_autoload:
                        self.gcode.run_script_from_command("MMU_PRELOAD GATE=%d" % gate)

                elif sensor == SENSOR_EXTRUDER_ENTRY:
                    if self.gate_selected != TOOL_GATE_BYPASS:
                        msg = "bypass not selected"
                    elif self.is_printing():
                        msg = "actively printing" # Should not get here!
                    elif self.filament_pos != FILAMENT_POS_UNLOADED:
                        msg = "extruder cannot be verified as unloaded. Try running MMU_RECOVER to fix state"
                    elif not self.p.bypass_autoload:
                        msg = "bypass autoload is disabled"
                    else:
                        self.log_debug("Autoloading extruder")
                        with self._wrap_suspend_filament_monitoring():
                            self._note_toolchange("> Bypass")
                            self.load_sequence(bowden_move=0., extruder_only=True, purge=PURGE_NONE) # TODO PURGE_STANDALONE?
                        return
                    self.log_debug("Ignoring extruder insertion because %s" % msg)

                else:
                    self.log_assertion("Unexpected/unhandled sensor insert event on %s. Ignored" % raw_sensor)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # Callback to handle removal event from an MMU sensor (only mmu_pre_gate for now). A removal
    # event can happen both in an out of a print
    # Params:
    #   EVENTTIME will contain reactor time that the sensor triggered and command was queued
    #   SENSOR will contain sensor name
    #   GATE will be set if specific pre-gate or gear sensor
    cmd_MMU_SENSOR_REMOVE_help= "Internal MMU filament removal handler"
    def cmd_MMU_SENSOR_REMOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()
        eventtime = gcmd.get_float('EVENTTIME', self.reactor.monotonic())
        gate = gcmd.get_int('GATE', None)
        raw_sensor = gcmd.get('SENSOR', "")
        sensor = self.sensor_manager.get_unitless_sensor_name(raw_sensor)

        try:
            with self.wrap_sync_gear_to_extruder():
                if sensor.startswith(SENSOR_PRE_GATE_PREFIX) and gate is not None:
                    # Ignore pre-gate runout if endless_spool_eject_gate feature is active and we want filament to be consumed to clear gate
                    if not(self.endless_spool_enabled and self.p.endless_spool_eject_gate > 0):
                        self._set_gate_status(gate, GATE_EMPTY)
                    else:
                        self.log_trace("Ignoring filament removal detected by %s because endless_spool_eject_gate is active" % raw_sensor)

                else:
                    self.log_assertion("Unexpected/unhandled sensor remove event on %s. Ignored" % raw_sensor)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_M400_help = "Wait on both move queues"
    def cmd_MMU_M400(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        self.mmu_toolhead.quiesce()

    cmd_MMU_TTG_MAP_help = "aka MMU_REMAP_TTG Display or remap a tool to a specific gate and set gate availability"
    def cmd_MMU_TTG_MAP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        ttg_map = gcmd.get('MAP', "!")
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        available = gcmd.get_int('AVAILABLE', GATE_UNKNOWN, minval=GATE_EMPTY, maxval=GATE_AVAILABLE)

        try:
            if reset == 1:
                self._reset_ttg_map()
            elif ttg_map != "!":
                ttg_map = gcmd.get('MAP').split(",")
                if len(ttg_map) != self.num_gates:
                    self.log_always("The number of map values (%d) is not the same as number of gates (%d)" % (len(ttg_map), self.num_gates))
                    return
                self.ttg_map = []
                for gate in ttg_map:
                    if gate.isdigit():
                        self.ttg_map.append(int(gate))
                    else:
                        self.ttg_map.append(0)
                self._persist_ttg_map()
            elif gate != -1:
                status = self.gate_status[gate]
                if not available == GATE_UNKNOWN or (available == GATE_UNKNOWN and status == GATE_EMPTY):
                    status = available
                if tool == -1:
                    self._set_gate_status(gate, status)
                else:
                    self._remap_tool(tool, gate, status)
            else:
                quiet = False # Display current TTG map
            if not quiet:
                msg = self._ttg_map_to_string(show_groups=detail)
                if not detail and self.endless_spool_enabled:
                    msg += "\nDETAIL=1 to see EndlessSpool map"
                self.log_info(msg)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_GATE_MAP_help = "Display or define the type and color of filaments on each gate"
    def cmd_MMU_GATE_MAP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}")                                # Hidden option for bulk filament update (from moonraker/ui components)
        replace = bool(gcmd.get_int('REPLACE', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        from_spoolman = bool(gcmd.get_int('FROM_SPOOLMAN', 0, minval=0, maxval=1)) # Hidden option for bulk filament update from spoolman
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        next_spool_id = gcmd.get_int('NEXT_SPOOLID', None, minval=-1)

        gate_map = None
        try:
            gate_map = ast.literal_eval(gmapstr)
        except Exception as e:
            self.log_error("Recieved unparsable gate map update. See log for more details")
            self.log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))
            return

        if reset:
            self._reset_gate_map()
        else:
            self._renew_gate_map() # Ensure that webhooks sees changes

        if next_spool_id:
            if self.p.spoolman_support != SPOOLMAN_PULL:
                if next_spool_id > 0:
                    self.pending_spool_id = next_spool_id
                    self.reactor.update_timer(self.pending_spool_id_timer, self.reactor.monotonic() + self.p.pending_spool_id_timeout)
                else:
                    # Disable timer to prevent reuse
                    self.pending_spool_id = -1
                    self.reactor.update_timer(self.pending_spool_id_timer, self.reactor.NEVER)
            else:
                self.log_error("Cannot use use NEXT_SPOOLID feature with spoolman_support: pull. Use 'push' or 'readonly' modes")
                return

        changed_gate_ids = []

        if gate_map: # --------- BATCH UPDATE from spoolman or UI --------
            try:
                self.log_debug("Received gate map update (replace: %s)" % replace)
                if replace:
                    # Replace complete map including spool_id (should only be in spoolman "pull" mode)
                    if self.p.spoolman_support != SPOOLMAN_PULL:
                        self.log_assertion("Received gate map replacement update but not in spoolman 'pull' mode")

                    # If from spoolman gate_map should be a full gate list with spool_id = -1 for unset gates
                    for gate, fil in gate_map.items():
                        if not (0 <= gate < self.num_gates):
                            self.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate)
                            continue

                        # Update gate attributes if we have valid spool_id
                        spool_id = self.safe_int(fil.get('spool_id', -1))
                        self.gate_spool_id[gate] = spool_id
                        self.gate_filament_name[gate] = fil.get('name', '')
                        self.gate_material[gate] = fil.get('material', '')
                        self.gate_color[gate] = fil.get('color', '')
                        self.gate_temperature[gate] = max(
                            self.safe_int(fil.get('temp', self.p.default_extruder_temp)),
                            self.p.default_extruder_temp
                        )
                        # gate_speed_override and gate_status can be set locally
                else:
                    # Update map (ui or from spoolman in "readonly" and "push" modes)
                    ids_dict = {}
                    for gate, fil in gate_map.items():
                        if not (0 <= gate < self.num_gates):
                            self.log_assertion("Illegal gate number %d supplied in gate map update - ignored" % gate)
                            continue

                        spool_id = self.safe_int(fil.get('spool_id', -1))
                        if (not from_spoolman or spool_id != -1):
                            # Update attributes but don't allow spoolman to accidently clear
                            self.gate_filament_name[gate] = fil.get('name', '')
                            self.gate_material[gate] = fil.get('material', '')
                            self.gate_color[gate] = fil.get('color', '')
                            self.gate_temperature[gate] = max(
                                self.safe_int(fil.get('temp', self.p.default_extruder_temp)),
                                self.p.default_extruder_temp
                            )
                            self.gate_speed_override[gate] = self.safe_int(fil.get('speed_override', self.gate_speed_override[gate]))
                            self.gate_status[gate] = self.safe_int(fil.get('status', self.gate_status[gate])) # For UI manual fixing of availabilty

                        # If spool_id has changed, clean up possible stale use of old one
                        if spool_id != self.gate_spool_id[gate]:
                            self.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate)
                            mod_gate_ids = self.assign_spool_id(gate, spool_id)
                            for (gate, sid) in mod_gate_ids:
                                ids_dict[gate] = sid

                    changed_gate_ids = list(ids_dict.items())

            except Exception as e:
                self.log_debug("Invalid MAP parameter: %s\nException: %s" % (gate_map, str(e)))
                raise gcmd.error("Invalid MAP parameter. See mmu.log for details")

        elif gates != "!" or gate >= 0:
            gatelist = []
            if gates != "!":
                # List of gates
                try:
                    for gate in gates.split(','):
                        gate = int(gate)
                        if 0 <= gate < self.num_gates:
                            gatelist.append(gate)
                except ValueError:
                    raise gcmd.error("Invalid GATES parameter: %s" % gates)
            else:
                # Specifying one gate (filament)
                gatelist.append(gate)

            ids_dict = {}
            for gate in gatelist:
                available = gcmd.get_int('AVAILABLE', self.gate_status[gate], minval=-1, maxval=2)
                name = gcmd.get('NAME', None)
                material = gcmd.get('MATERIAL', None)
                color = gcmd.get('COLOR', None)
                spool_id = gcmd.get_int('SPOOLID', None, minval=-1)
                temperature = gcmd.get_int('TEMP', int(self.p.default_extruder_temp))
                speed_override = gcmd.get_int('SPEED', self.gate_speed_override[gate], minval=10, maxval=150)

                if self.p.spoolman_support != SPOOLMAN_PULL:
                    # Local gate map, can update attributes
                    spool_id = spool_id or self.gate_spool_id[gate]
                    name = name if name is not None else self.gate_filament_name[gate]
                    material = (material if material is not None else self.gate_material[gate]).upper()
                    color = (color if color is not None else self.gate_color[gate]).lower()
                    temperature = temperature or self.gate_temperature[gate]
                    color = self._validate_color(color)
                    if color is None:
                        raise gcmd.error("Color specification must be in form 'rrggbb' or 'rrggbbaa' hexadecimal value (no '#') or valid color name or empty string")
                    self.gate_filament_name[gate] = name
                    self.gate_material[gate] = material
                    self.gate_color[gate] = color
                    self.gate_temperature[gate] = temperature
                    self.gate_speed_override[gate] = speed_override
                    self.gate_status[gate] = available

                    if spool_id != self.gate_spool_id[gate]:
                        self.log_debug("Spool_id changed for gate %d in MMU_GATE_MAP" % gate)
                        mod_gate_ids = self.assign_spool_id(gate, spool_id)
                        for (gate, sid) in mod_gate_ids:
                            ids_dict[gate] = sid

                else:
                    # Remote (spoolman) gate map, don't update local attributes that are set by spoolman
                    self.gate_status[gate] = available
                    self.gate_speed_override[gate] = speed_override
                    if any(x is not None for x in [material, color, spool_id, name]):
                        self.log_error("Spoolman mode is '%s': Can only set gate status and speed override locally\nUse MMU_SPOOLMAN or update spoolman directly" % SPOOLMAN_PULL)
                        return

            changed_gate_ids = list(ids_dict.items())

        # Ensure everything is synced
        self._update_gate_color_rgb()

        # Caution, make sure that an update from spoolman does end up in infinite loop!
        self._persist_gate_map(spoolman_sync=bool(changed_gate_ids) and not from_spoolman, gate_ids=changed_gate_ids) # This will also update LED status

        if not quiet:
            self.log_info(self._gate_map_to_string(detail))

    cmd_MMU_ENDLESS_SPOOL_help = "Diplay or Manage EndlessSpool functionality and groups"
    def cmd_MMU_ENDLESS_SPOOL(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        enabled = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', "!")

        if enabled >= 0:
            self.endless_spool_enabled = enabled
            self.var_manager.set(VARS_MMU_ENABLE_ENDLESS_SPOOL, self.endless_spool_enabled, write=True)
            if enabled and not quiet:
                self.log_always("EndlessSpool is enabled")
        if not self.endless_spool_enabled:
            self.log_always("EndlessSpool is disabled")
            return

        if reset:
            self._reset_endless_spool()

        elif groups != "!":
            groups = gcmd.get('GROUPS', ",".join(map(str, self.endless_spool_groups))).split(",")
            if len(groups) != self.num_gates:
                self.log_always("The number of group values (%d) is not the same as number of gates (%d)" % (len(groups), self.num_gates))
                return
            self.endless_spool_groups = []
            for group in groups:
                if group.isdigit():
                    self.endless_spool_groups.append(int(group))
                else:
                    self.endless_spool_groups.append(0)
            self._persist_endless_spool()

        else:
            quiet = False # Display current map

        if not quiet:
            self.log_info(self._es_groups_to_string())

    cmd_MMU_TOOL_OVERRIDES_help = "Displays, sets or clears tool speed and extrusion factors (M220 & M221)"
    def cmd_MMU_TOOL_OVERRIDES(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates)
        speed = gcmd.get_int('M220', None, minval=0, maxval=200)
        extrusion = gcmd.get_int('M221', None, minval=0, maxval=200)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            self._set_tool_override(tool, 100, 100)
        elif tool >= 0:
            self._set_tool_override(tool, speed, extrusion)

        msg_tool = "Tools: "
        msg_sped = "M220 : "
        msg_extr = "M221 : "
        for i in range(self.num_gates):
            range_end = 6 if i > 9 else 5
            tool_speed = int(self.tool_speed_multipliers[i] * 100)
            tool_extr = int(self.tool_extrusion_multipliers[i] * 100)
            msg_tool += ("| T%d  " % i)[:range_end]
            msg_sped += ("| %d  " % tool_speed)[:range_end]
            msg_extr += ("| %d  " % tool_extr)[:range_end]
        msg = "|\n".join([msg_tool, msg_sped, msg_extr]) + "|\n"
        self.log_always(msg)

    cmd_MMU_SLICER_TOOL_MAP_help = "Display or define the tools used in print as specified by slicer"
    def cmd_MMU_SLICER_TOOL_MAP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self._fix_started_state()

        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        purge_map = bool(gcmd.get_int('PURGE_MAP', 0, minval=0, maxval=1))
        sparse_purge_map = bool(gcmd.get_int('SPARSE_PURGE_MAP', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        initial_tool = gcmd.get_int('INITIAL_TOOL', None, minval=0, maxval=self.num_gates - 1)
        total_toolchanges = gcmd.get_int('TOTAL_TOOLCHANGES', None, minval=0)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        material = gcmd.get('MATERIAL', "unknown")
        color = gcmd.get('COLOR', "").lower()
        name = gcmd.get('NAME', "") # Filament name
        temp = gcmd.get_int('TEMP', 0, minval=0)
        used = bool(gcmd.get_int('USED', 1, minval=0, maxval=1)) # Is used in print (i.e a referenced tool or not)
        purge_volumes = gcmd.get('PURGE_VOLUMES', "")
        num_slicer_tools = gcmd.get_int('NUM_SLICER_TOOLS', self.num_gates, minval=1, maxval=self.num_gates) # Allow slicer to have less tools than MMU gates
        automap_strategy = gcmd.get('AUTOMAP', None)
        skip_automap = gcmd.get_int('SKIP_AUTOMAP', None, minval=0, maxval=1)

        quiet = False
        if reset:
            self._clear_slicer_tool_map()
            quiet = True
        else:
            self.slicer_tool_map = dict(self.slicer_tool_map) # Ensure that webhook sees get_status() change

        # This is a "one-print" option that supresses automatic automap. If specified, set the skip option
        # else leave it be. It will be reset at print end
        if skip_automap is not None:
            # This is a "one-print" option that supresses automatic automap
            self._restore_automap_option(bool(skip_automap))

        if tool >= 0:
            self.slicer_tool_map['tools'][str(tool)] = {'color': color, 'material': material, 'temp': temp, 'name': name, 'in_use': used}
            if used:
                self.slicer_tool_map['referenced_tools'] = sorted(set(self.slicer_tool_map['referenced_tools'] + [tool]))
                if not self.slicer_tool_map['skip_automap'] and automap_strategy and automap_strategy != AUTOMAP_NONE:
                    self._automap_gate(tool, automap_strategy)
            if color:
                self._update_slicer_color_rgb()
            quiet = True

        if initial_tool is not None:
            self.slicer_tool_map['initial_tool'] = initial_tool
            self.slicer_tool_map['referenced_tools'] = sorted(set(self.slicer_tool_map['referenced_tools'] + [initial_tool]))
            quiet = True

        if total_toolchanges is not None:
            self.slicer_tool_map['total_toolchanges'] = total_toolchanges
            quiet = True

        if purge_volumes != "":
            try:
                volumes = list(map(float, purge_volumes.split(',')))
                n = len(volumes)
                num_tools = self.num_gates
                if n == 1:
                    calc = lambda x,y: volumes[0] * 2 # Build a single value matrix
                elif n == num_slicer_tools:
                    calc = lambda x,y: volumes[y] + volumes[x] # Will build symmetrical purge matrix "from" followed by "to"
                elif n == num_slicer_tools ** 2:
                    calc = lambda x,y: volumes[y + x * num_slicer_tools] # Full NxN matrix supplied in rows of "from" for each "to"
                elif n == num_slicer_tools * 2:
                    calc = lambda x,y: volumes[y] + volumes[num_slicer_tools + x] # Build matrix with sum of "from" list then "to" list
                else:
                    raise gcmd.error("Incorrect number of values for PURGE_VOLUMES. Expected 1, %d, %d, or %d, got %d" % (num_tools, num_tools * 2, num_tools ** 2, n))
                # Build purge volume map (x=to_tool, y=from_tool)
                should_calc = lambda x,y: x < num_slicer_tools and y < num_slicer_tools and x != y
                self.slicer_tool_map['purge_volumes'] = [
                    [
                        calc(x,y) if should_calc(x,y) else 0
                        for y in range(self.num_gates)
                    ]
                    for x in range(self.num_gates)
                ]
            except ValueError as e:
                raise gcmd.error("Error parsing PURGE_VOLUMES: %s" % str(e))
            quiet = True

        if not quiet:
            colors = sum(1 for tool in self.slicer_tool_map['tools'] if self.slicer_tool_map['tools'][tool]['in_use'])

            have_purge_map = len(self.slicer_tool_map['purge_volumes']) > 0
            msg = "No slicer tool map loaded"
            if colors > 0 or self.slicer_tool_map['initial_tool'] is not None:
                msg = "--------- Slicer MMU Tool Summary ---------\n"
                msg += "Single color print" if colors <= 1 else "%d color print" % colors
                msg += " (Purge volume map loaded)\n" if colors > 1 and have_purge_map else "\n"
                for t, params in self.slicer_tool_map['tools'].items():
                    if params['in_use'] or detail:
                        msg += "T%d (gate %d, %s, %s, %d%sC)" % (int(t), self.ttg_map[int(t)], params['material'], params['color'], params['temp'], UI_DEGREE)
                        msg += " Not used\n" if detail and not params['in_use'] else "\n"
                if self.slicer_tool_map['initial_tool'] is not None:
                    msg += "Initial Tool: T%d" % self.slicer_tool_map['initial_tool']
                    msg += " (will use bypass)\n" if colors <= 1 and self.tool_selected == TOOL_GATE_BYPASS else "\n"
                msg += "-------------------------------------------"
            if detail or purge_map or sparse_purge_map:
                if have_purge_map:
                    rt = self.slicer_tool_map['referenced_tools']
                    volumes = [row[:num_slicer_tools] for row in self.slicer_tool_map['purge_volumes'][:num_slicer_tools]]
                    msg += "\nPurge Volume Map (mm^3):\n"
                    msg += "To ->" + UI_SEPARATOR.join("{}T{: <2}".format(UI_SPACE, i) for i in range(num_slicer_tools)) + "\n"
                    msg += '\n'.join([
                        "T{: <2}{}{}".format(y, UI_SEPARATOR, ' '.join(
                            map(lambda v, x, y=y: str(round(v)).rjust(4, UI_SPACE)
                                if (not sparse_purge_map or (y in rt and x in rt)) and v > 0
                                else "{}{}-{}".format(UI_SPACE, UI_SPACE, UI_SPACE),
                                row, range(len(row))
                            )
                        ))
                        for y, row in enumerate(volumes)
                    ])

            elif have_purge_map:
                msg += "\nDETAIL=1 to see purge volume map"
            self.log_always(msg)

    cmd_MMU_CALC_PURGE_VOLUMES_help = "Calculate purge volume matrix based on filament color overriding slicer tool map import"
    def cmd_MMU_CALC_PURGE_VOLUMES(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self._fix_started_state()

        min_purge = gcmd.get_int('MIN', 0, minval=0)
        max_purge = gcmd.get_int('MAX', 800, minval=1)
        multiplier = gcmd.get_float('MULTIPLIER', 1., above=0.)
        source = gcmd.get('SOURCE', 'gatemap')
        if source not in ['gatemap', 'slicer']:
            raise gcmd.error("Invalid color source: %s. Options are: gatemap, slicer" % source)
        if min_purge >= max_purge:
            raise gcmd.error("MAX purge volume must be greater than MIN")

        tool_rgb_colors = []
        if source == 'slicer':
            # Pull colors from existing slicer map
            for tool in range(self.num_gates):
                tool_info = self.slicer_tool_map['tools'].get(str(tool))
                if tool_info:
                    tool_rgb_colors.append(self._color_to_rgb_hex(tool_info.get('color', '')))
                else:
                    tool_rgb_colors.append(self._color_to_rgb_hex(''))
        else:
            # Logic to use tools mapped to gate colors with current ttg map
            for tool in range(self.num_gates):
                gate = self.ttg_map[tool]
                tool_rgb_colors.append(self._color_to_rgb_hex(self.gate_color[gate]))

        try:
            self.slicer_tool_map['purge_volumes'] = self._generate_purge_matrix(tool_rgb_colors, min_purge, max_purge, multiplier)
            self.log_always("Purge map updated. Use 'MMU_SLICER_TOOL_MAP PURGE_MAP=1' to view")
        except Exception as e:
            raise MmuError("Error generating purge volues: %s" % str(e))

    cmd_MMU_CHECK_GATE_help = "Automatically inspects gate(s), parks filament and marks availability"
    def cmd_MMU_CHECK_GATE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        self._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        # These three parameters are mutually exclusive so we only process one
        tools = gcmd.get('TOOLS', "!")
        gates = gcmd.get('GATES', "!")
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates = None if gate == -1 else [gate]): return # TODO Incomplete/simplified gate selection

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during gate check
                    with self._wrap_suspendwrite_variables():  # Reduce I/O activity to a minimum
                        with self.wrap_action(ACTION_CHECKING):
                            tool_selected = self.tool_selected
                            filament_pos = self.filament_pos
                            gates_tools = []
                            if gate >= 0:
                                # Individual gate
                                gates_tools.append([gate, -1])
                            elif tool >= 0:
                                # Individual tool
                                gate = self.ttg_map[tool]
                                gates_tools.append([gate, tool])
                            elif all_gates:
                                for gate in range(self.num_gates):
                                    gates_tools.append([gate, -1])
                            elif gates != "!":
                                # List of gates
                                try:
                                    for gate in gates.split(','):
                                        gate = int(gate)
                                        if 0 <= gate < self.num_gates:
                                            gates_tools.append([gate, -1])
                                except ValueError:
                                    raise MmuError("Invalid GATES parameter: %s" % tools)
                            elif tools != "!":
                                # Tools used in print (may be empty list)
                                try:
                                    for tool in tools.split(','):
                                        if not tool == "":
                                            tool = int(tool)
                                            if 0 <= tool < self.num_gates:
                                                gate = self.ttg_map[tool]
                                                gates_tools.append([gate, tool])
                                    if len(gates_tools) == 0:
                                        self.log_debug("No tools to check, assuming default tool is already loaded")
                                        return
                                except ValueError:
                                    raise MmuError("Invalid TOOLS parameter: %s" % tools)
                            elif self.gate_selected >= 0:
                                # No parameters means current gate
                                gates_tools.append([self.gate_selected, -1])
                            else:
                                raise MmuError("Current gate is invalid")

                            # Force initial eject
                            if filament_pos != FILAMENT_POS_UNLOADED:
                                self.log_info("Unloading current tool prior to checking gates")

                                # Perform full unload sequence including parking
                                self._note_toolchange("< %s" % self.selected_tool_string())
                                self.last_statistics = {}
                                self._save_toolhead_position_and_park('unload')
                                self._unload_tool(form_tip=FORM_TIP_STANDALONE)
                                self._persist_gate_statistics()
                                self._continue_after('unload')

                            if len(gates_tools) > 1:
                                self.log_info("Will check gates: %s" % ', '.join(str(g) for g,t in gates_tools))
                            with self.wrap_suppress_visual_log():
                                self._set_tool_selected(TOOL_GATE_UNKNOWN)
                                for gate, tool in gates_tools:
                                    try:
                                        self.select_gate(gate)
                                        self.log_info("Checking gate %d..." % gate)
                                        _ = self._load_gate(allow_retry=False)
                                        if tool >= 0:
                                            self.log_info("Tool T%d - Filament detected. Gate %d marked available" % (tool, gate))
                                        else:
                                            self.log_info("Gate %d - Filament detected. Marked available" % gate)
                                        self._set_gate_status(gate, max(self.gate_status[gate], GATE_AVAILABLE))
                                        try:
                                            _,_ = self._unload_gate()
                                        except MmuError as ee:
                                            raise MmuError("Failure during check gate %d %s:\n%s" % (gate, "(T%d)" % tool if tool >= 0 else "", str(ee)))
                                    except MmuError as ee:
                                        self._set_gate_status(gate, GATE_EMPTY)
                                        self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)
                                        if tool >= 0:
                                            msg = "Tool T%d on gate %d marked EMPTY" % (tool, gate)
                                        else:
                                            msg = "Gate %d marked EMPTY" % gate
                                        self.log_debug("Gate marked empty because: %s" % str(ee))
                                        if self.is_in_print():
                                            raise MmuError("%s%s" % ("Required " if self.is_printing() else "", msg))
                                        else:
                                            self.log_always(msg)
                                    finally:
                                        self._initialize_encoder() # Encoder 0000

                            # If not printing select original tool and load filament if necessary
                            # We don't do this when printing because this is expected to preceed loading initial tool
                            if not self.is_printing():
                                try:
                                    if tool_selected == TOOL_GATE_BYPASS:
                                        self.select_bypass()
                                    elif tool_selected != TOOL_GATE_UNKNOWN:
                                        if filament_pos == FILAMENT_POS_LOADED:
                                            self.log_info("Restoring tool loaded prior to checking gates")

                                            # Perform full load sequence including parking
                                            self._note_toolchange("> %s" % self.selected_tool_string(tool=tool_selected))
                                            self.last_statistics = {}
                                            self._save_toolhead_position_and_park('load')
                                            self._select_and_load_tool(tool_selected, purge=PURGE_NONE)
                                            self._persist_gate_statistics()
                                            self._continue_after('load')
                                        else:
                                            self.select_tool(tool_selected)
                                except MmuError as ee:
                                    raise MmuError("Failure re-selecting Tool %d:\n%s" % (tool_selected, str(ee)))
                            else:
                                # At least restore the selected tool, but don't re-load filament
                                self.select_tool(tool_selected)

                            if not quiet:
                                self.log_info(self._mmu_visual_to_string())

        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_PRELOAD_help = "Preloads filament at specified or current gate"
    def cmd_MMU_PRELOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_printing(): return
        if self.check_if_not_homed(): return 
                                        
        gate = gcmd.get_int('GATE', self.gate_selected, minval=0, maxval=self.num_gates - 1)
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[gate]): return
                                                
        can_crossload = (                       
            (self.mmu_machine.can_crossload or self.mmu_machine.multigear)
            and self.sensor_manager.has_gate_sensor(SENSOR_GEAR_PREFIX, gate)
        )                               
        if not can_crossload:   
            if self.check_if_bypass(): return
            if self.check_if_loaded(): return
                                
        self.log_always("Preloading filament in %s..." % ("current gate" if gate == self.gate_selected else "gate %d" % gate))
        try:                
            with self.wrap_sync_gear_to_extruder():
                with self.wrap_suppress_visual_log():
                    with self.wrap_action(ACTION_CHECKING):
                            
                        current_gate = self.gate_selected
                        if gate != current_gate:
                            self.select_gate(gate)
                                
                        try:    
                            self._preload_gate()
                                
                        finally:
                            if self.gate_selected != current_gate:
                                # If necessary or easy restore previous gate
                                if self.is_in_print() or self.mmu_machine.multigear or self.filament_pos != FILAMENT_POS_UNLOADED:
                                    self.select_gate(current_gate)
                                else:
                                    # Lazy movement means we have side effect of changed tool/gate
                                    self._ensure_ttg_match()
                                    self._initialize_encoder() # Encoder 0000
        except MmuError as ee:          
            self.handle_mmu_error("Filament preload for gate %d failed: %s" % (gate, str(ee)))
