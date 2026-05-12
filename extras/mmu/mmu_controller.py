# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Main control class for any Klipper based MMU (includes filament gear(extruder) stepper control)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

#import gc, sys, ast, random, logging, time, contextlib, math, os.path, re, unicodedata, traceback
import logging, sys, ast, random, time, contextlib, math, re, unicodedata, traceback

from itertools                  import repeat

# Klipper imports

# Happy Hare imports
from .mmu_constants             import *
from .mmu_logger                import MmuLogger
from .mmu_utils                 import MmuError, MmuColorUtils
from .mmu_sensor_manager        import MmuSensorManager
from .mmu_sensor_utils          import MmuRunoutHelper
from .mmu_led_manager           import MmuLedManager
from .mmu_filament_movement     import MmuFilamentMovement
from .mmu_print_state_machine   import MmuPrintStateMachine
from .mmu_gate_maps             import MmuGateMaps
from .commands                  import COMMAND_REGISTRY
from .commands.mmu_base_command import *


# Main klipper module
class MmuController(MmuFilamentMovement):

    def __init__(self, config, mmu_machine):
        logging.info("PAUL: init() for MmuController")
        self.config = config
        self.mmu_machine = mmu_machine
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

        self.num_gates = self.mmu_machine.num_gates
        self.unit_selected = 0                  # Which MMU unit is active (has active gate) if more than one
        self.gate_selected = self.tool_selected = TOOL_GATE_UNKNOWN
        self._last_tool = self._next_tool       = TOOL_GATE_UNKNOWN
        self._next_gate = None
        self.toolchange_retract = 0.            # Set from mmu_macro_vars
        self.toolchange_purge_volume = 0.       # During toolchange, the total calculated purge volume
        self._slicer_purge_volume = 0.          # During toolchange, the slicer contributed part of purge volume
        self._standalone_sync = False           # Used to indicate synced extruder intention whilst out of print
        self._suppress_release_grip = False     # Used to suppress the relaxing of grip on recursive calls to prevent servo flutter
        self.bowden_start_pos = None            # If set then we can measure bowden progress
        self.has_blobifier = False              # Post load blobbling macro (like BLOBIFIER)
        self.has_mmu_cutter = False             # Post unload cutting macro (like EREC)
        self.has_toolhead_cutter = False        # Form tip cutting macro (like _MMU_CUT_TIP)
        self._is_running_test = False           # True while running QA or soak tests
        self.gear_run_current_percent = 100     # Current run percentage of active gear stepper
        self.extruder_run_current_percent = 100 # Current run percentage of active extruder
        self._gear_run_current_locked = False   # True if changes to gear current is currently locked by wrap_gear_current()
        self.p = mmu_machine.params             # Shared Parameters shortcut

        self.kalico = bool(self.printer.lookup_object('danger_options', False))

        # Tool speed and extrusion multipliers
        self.tool_speed_multipliers     = [1.0] * self.num_gates # M220 record
        self.tool_extrusion_multipliers = [1.0] * self.num_gates # M221 record


        # Complete setup of other contoller components ------------------------------------------------------

        self.logger         = MmuLogger(self)            # Handles console logging and separate file based log
        self.psm            = MmuPrintStateMachine(self) # Manages an augmented printer state machine
        self.led_manager    = MmuLedManager(self)        # Manages leds accross all units
        self.sensor_manager = MmuSensorManager(self)     # Manages sensors accross all units
        self.gate_maps      = MmuGateMaps(self)          # Gate map / TTG map / EndlessSpool state


        # Register GCODE commands ---------------------------------------------------------------------------

        for name, cls in sorted(COMMAND_REGISTRY.items()):
            try:
                cls(self)
            except Exception:
                raise self.config.error(f"Failed to register command class: {name}")


        # Bootup tasks --------------------------------------------------------------------------------------

        # Scheduled as regular gcode command to ensure everything is copecetic prior to running
        self.gcode.register_command('__MMU_BOOTUP', self.cmd_MMU_BOOTUP, desc = self.cmd_MMU_BOOTUP_help)


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

        if self.p.update_aht10_commands: # Command set of AHT10 (on ViViD) for older klipper versions
            try:
                from extras import aht10
                aht10.AHT10_COMMANDS = {
                    'INIT'    :[0xBE, 0x08, 0x00],
                    'MEASURE' :[0xAC, 0x33, 0x00],
                    'RESET'   :[0xBE, 0x08, 0x00]
                }
            except Exception as e:
                self.log_error("Unable to update AHT10_COMMANDS: %s" % str(e))


        # Initialize state and statistics variables
        self.reinit()
        self._reset_statistics()
        self.counters = {}

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:disconnect', self.handle_disconnect)
        self.printer.register_event_handler('klippy:ready', self.handle_ready)


    def handle_connect(self):
        logging.info("PAUL: handle_connect: MmuController")
        self.toolhead = self.printer.lookup_object('toolhead')
        self.var_manager = self.mmu_machine.var_manager

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


    def handle_disconnect(self):
        self.log_debug('Klipper disconnected!')


    def handle_ready(self):
        logging.info("PAUL: handle_ready: MmuController ========================================")
        # Pull retraction length from macro config
        sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro:
            park_toolchange = sequence_vars_macro.variables.get('park_toolchange', [0])
            self.toolchange_retract = park_toolchange[-1] if isinstance(park_toolchange, (list, tuple)) else float(park_toolchange)

        # Restore state (only if fully calibrated)
        self._load_persisted_state()

        # Setup events for managing internal print state machine
        self.psm.register_event_handlers()

        self._setup_hotend_off_timer()
        self._setup_pending_spool_id_timer()
        self._clear_saved_toolhead_position()

        # This is a bit naughty to register commands here but I need to make sure we are the outermost wrapper
        try:
            wrappers = [
                ('PAUSE',        MmuWrapperPauseCommand),
                ('RESUME',       MmuWrapperResumeCommand),
                ('CLEAR_PAUSE',  MmuWrapperClearPauseCommand),
                ('CANCEL_PRINT', MmuWrapperCancelPrintCommand),
            ]

            for name, wrapper_cls in wrappers:
                prev = self.gcode.register_command(name, None)
                if prev is None:
                    self.log_error(f'No existing {name} macro found!')
                    continue

                # Rename existing command
                self.gcode.register_command(f'__{name}', prev)

                # Register replacement
                wrapper_cls(self)

        except Exception as e:
            self.log_error(
                'Error trying to wrap PAUSE/RESUME/CLEAR_PAUSE/CANCEL_PRINT macros: %s' % e
            )

        # Schedule bootup tasks to run after klipper and hopefully spoolman have settled
        self._schedule_mmu_bootup_tasks(BOOT_DELAY)


    def reinit(self):
        """
        Ensure clean state on initializtion and after MMU enable/disable operation
        """
        self.is_enabled = self.runout_enabled = True
        self.runout_last_enable_time = self.reactor.monotonic()
        self.is_handling_runout = self.calibrating = False
        self.unit_selected = 0
        self.tool_selected = self.gate_selected = TOOL_GATE_UNKNOWN
        self._next_tool = TOOL_GATE_UNKNOWN
        self._last_toolchange = "Unknown"
        self.active_filament = {}
        self.filament_pos = FILAMENT_POS_UNKNOWN
        self.filament_direction = DIRECTION_UNKNOWN
        self.action = ACTION_IDLE
        self._old_action = None
        self._clear_saved_toolhead_position()
        self._reset_job_statistics()
        self.form_tip_vars = None   # Current defaults of gcode variables for tip forming macro
        self.gate_maps.clear_slicer_tool_map()
        self.pending_spool_id = -1  # For automatic assignment of spool_id if set perhaps by rfid reader
        self.saved_toolhead_max_accel = None
        self.num_toolchanges = 0

        self.psm.reinit()
        self.mmu_machine.reinit() # Will iterate over all mmu_units


    def _load_persisted_state(self):
        self.log_debug("Loading persisted MMU state")
        errors = []

        # Load last tool
        self._last_tool = self.var_manager.get(VARS_MMU_LAST_TOOL, self._last_tool)

        # Load gate-map / TTG-map / EndlessSpool state
        errors.extend(self.gate_maps.load_persisted_state())

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
        self.reactor.register_callback(lambda pt: self.psm.print_event("__MMU_BOOTUP"), waketime)


    def _fversion(self, v):
        return "v{major}.{minor}.{patch}".format(
            major=int(v),
            minor=str(v).split('.')[1][0] if '.' in str(v) and len(str(v).split('.')[1]) > 0 else '0',
            patch=str(v).split('.')[1][1:] if '.' in str(v) and len(str(v).split('.')[1]) > 1 else '0'
        )


    cmd_MMU_BOOTUP_help = "Internal commands to complete bootup of MMU"
    def cmd_MMU_BOOTUP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())

        self.log_warning(f"PAUL: BOOTUP_START : gate={self.gate_selected}, tool={self.tool_selected}, unit={self.unit_selected}, fil_pos={self.filament_pos}")

        try:
            # Splash...
            version = self._fversion(self.mmu_machine.happy_hare_version)
            msg = (
                "{1}(\\_/){0}\n"
                "{1}( {0}*,*{1}){0}\n"
                "{1}(\")_(\"){0} "
                "{5}{2}H{0}{3}a{0}{4}p{0}{2}p{0}{3}y{0} "
                "{4}H{0}{2}a{0}{3}r{0}{4}e{0} "
                "{1}" + version + "{0} "
                "{2}R{0}{3}e{0}{4}a{0}{2}d{0}{3}y{0}{1}...{0}{6}"
            )
            self.log_always(msg, color=True)

            # Kalico (especially "bleeding edge" users) need reminding of possible incompatibility
            if self.kalico:
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
                    msg = (
                        f"Warning: filament_switch_sensor '{fsensor_name}' found in printer configuration.\n"
                        f"This may interfere with MMU functionality{pause_on_runout_msg}."
                    )
                    if pause_on_runout:
                        self.log_warning(msg)
                    else:
                        self.log_info(msg)

            # Use per gate sensors to adjust gate map
            self.gate_status = self.gate_maps.validate_gate_status(self.gate_status)

            try:
                # Can we verify gate selected? If so fix if necessary
                validated_gate = self.gate_maps.validate_gate_selected()
                if validated_gate is not None and validated_gate != self.gate_selected:
                    self.log_info(f"Filament detected in gate {validated_gate}")
                    self._set_gate_selected(validated_gate)

                # Sanity check filament pos based only on non-intrusive tests and recover if necessary
                if self.sensor_manager.check_all_sensors_after(
                    FILAMENT_POS_END_BOWDEN, self.gate_selected
                ):
                    self.set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)

                elif (
                    (self.filament_pos == FILAMENT_POS_LOADED and
                     not self.sensor_manager.check_any_sensors_after(FILAMENT_POS_END_BOWDEN, self.gate_selected)) or

                    (self.filament_pos == FILAMENT_POS_UNLOADED and
                     self.sensor_manager.check_any_sensors_in_path()) or

                    self.filament_pos not in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]
                ):
                    self.recover_filament_pos(can_heat=False, message=True, silent=True)

            except Exception as e:
                # This is recoverable so just report errors
                self.log_error(str(e))

            # Autohoming...
            for u in self.mmu_machine.units:

                if not u.calibrator.check_calibrated(CALIBRATED_SELECTOR):
                    self.log_warning(f"Cannot autohome selector for {u.name} because selector is not yet calibrated")
                    continue

                if u.p.startup_home_selector: # PAUL and selector is calibrated!
                    unit_loaded = (
                        self.gate_selected != TOOL_GATE_UNKNOWN and
                        u.manages_gate(self.gate_selected) and
                        self.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_UNKNOWN]
                    )

                    if unit_loaded:
                        self.log_warning(f"Skipping autohome of {u.name} because it may have filament loaded")
                        continue

                    try:
                        self.home_unit(u) # Will reselect previous gate

                    except Exception as e:
                        # This is recoverable so just report errors
                        self.log_error(str(e))

            # Make sure the gate is really selected (allows selectors to initialize themselves)
            if self.gate_selected != TOOL_GATE_UNKNOWN:
                try:
                    self.log_info(f"Selecting last gate used ({self.gate_selected})...")
                    self.select_gate(self.gate_selected)
                except Exception as e:
                    # This is recoverable so just report errors
                    self.log_error(str(e))

            # Initial state (this will also enable the leds with opening effect)
            self.psm.set_print_state("initialized")

            # TTG map...
            if self.p.startup_reset_ttg_map:
                self.gate_maps.reset_ttg_map()
            self.gate_maps.ensure_ttg_match()

            # Ensure espooler print assist is in correct state
            self._adjust_espooler_assist()

            # Initially disable clog/runout detection
            self._disable_filament_monitoring()

            # Sync with spoolman. Delay as long as possible to maximize the chance it is contactable after startup/reboot
            self._spoolman_sync()

            # Sync lane data to Moonraker for slicer integration and cleanup old lanes
            self._moonraker_sync_lane_data()

            # Reset sync state (intention is not to sync unless we have to)
            self.reset_sync_gear_to_extruder(False)
            self.movequeue_wait()

            # Status to console...
            if self.p.log_startup_status:
                self.log_always(self._mmu_visual_to_string(), color=True)
                self._display_visual_state()

            # Finally report if any recovery is necessary by user
            self.report_necessary_recovery()

        except Exception as e:
            self.log_assertion(f"Error booting up MMU: {e}", exc_info=sys.exc_info())

        self.log_warning(f"PAUL: BOOTUP_END : gate={self.gate_selected}, tool={self.tool_selected}, unit={self.unit_selected}, fil_pos={self.filament_pos}")

        # Restart hook
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
                self.movequeue_wait()

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



# -----------------------------------------------------------------------------------------------------------
# Per-gate component accessor router.
# All assume current gate unless directly specified.
# These methods are key to multiple mmu-unit support whilst retaining central control logic
# -----------------------------------------------------------------------------------------------------------

    def mmu_unit(self, gate=None):
        if gate is None: gate = self.gate_selected
        mmu_unit = self.mmu_machine.get_mmu_unit_by_gate(gate)
        if mmu_unit is None:
            mmu_unit = self.mmu_machine.get_mmu_unit_by_index(0)
        return mmu_unit


    def gear(self, gate=None):
        if gate is None: gate = self.gate_selected
        return self.mmu_unit(gate).gear_stepper_obj(gate)


    def selector(self, gate=None):
        return self.mmu_unit(gate).selector


    def espooler(self, gate=None):
        mmu_unit = self.mmu_unit(gate)
        if mmu_unit:
            return mmu_unit.espooler
        return None


    def has_espooler(self, gate=None):
        return self.espooler(gate) is not None


    def encoder(self, gate=None):
        if gate is None:
            gate = self.gate_selected
        mmu_unit = self.mmu_unit(gate)
        if mmu_unit:
            return mmu_unit.encoder
        return None


    def has_encoder(self, gate=None):
        return self.encoder(gate) is not None


    def can_use_encoder(self, gate=None):
        return self.has_encoder(gate) and self.mmu_unit(gate).p.encoder_move_validation


    def get_encoder_state(self, gate=None):
        if self.has_encoder(gate):
            return "%s" % "Enabled" if self.encoder(gate).is_enabled() else "Disabled"
        else:
            return "n/a"



# -----------------------------------------------------------------------------------------------------------
# AGGREGATED PRINTER VARIABLES FOR "LOGICAL" MMU MACHINE
# -----------------------------------------------------------------------------------------------------------

    # Note: Returning new lists/dicts so that moonraker sees the change
    def get_status(self, eventtime):
        status = {
            'enabled': self.is_enabled,
            'num_gates': self.num_gates,
            'is_homed': self.selector().is_homed, # Always true on type-B MMU's
            'print_state': self.psm.print_state,
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
            'filament_position': self.gear().get_mode_position(),
            'filament_pos': self.filament_pos, # State machine position
            'filament_direction': self.filament_direction,
            'pending_spool_id': self.pending_spool_id,
            'tool_extrusion_multipliers': self.tool_extrusion_multipliers,
            'tool_speed_multipliers': self.tool_speed_multipliers,
            'action': self._get_action_string(),
            'sync_drive': self.gear().is_synced_to_extruder(),
            'reason_for_pause': self.psm.reason_for_pause if self.is_mmu_paused() else "",
            'spoolman_support': self.p.spoolman_support,
            'bowden_progress': self._get_bowden_progress(), # Simple 0-100%. -1 if not performing bowden move
            'print_start_detection': self.p.print_start_detection, # For Klippain. Not really sure it is necessary

            # DEPRECATED but possibly still used in UI's or by users custom macros
            'espooler_active': self.espooler().get_operation(self.gate_selected)[0] if self.has_espooler() else ESPOOLER_NONE, # DEPRECATED
            'runout': self.is_handling_runout, # DEPRECATED but still used in HH macros (better to use operation)
            'is_paused': self.is_mmu_paused(), # DEPRECATED (better to use print_state)
            'is_locked': self.is_mmu_paused(), # DEPRECATED (alias for is_paused) Still referenced in Mainsail interface
            'is_in_print': self.is_in_print(), # DEPRECATED (use print_state) Still referenced in Mainsail interface
            'has_bypass': self.selector().has_unit_bypass(), # Not really necessary anymore and shortcut to active unit "has_bypass" # PAUL TODO does this need to be forced to True now? (bypass always possible?)
            'clog_detection': False,           # DEPRECATED
            'clog_detection_enabled': False,   # DEPRECATED
        }

        # Adds status for gate map, ttg map, endless spool, etc
        status.update(self.gate_maps.get_status(eventtime))

        # Adds extruder status (like filament remaining)
        status.update(self.mmu_unit().extruder_wrapper.get_status(eventtime))

        # Add in active sensors
        status['sensors'] = self.sensor_manager.get_status(eventtime)

        if self.has_encoder():
            status['encoder'] = self.encoder().get_status(eventtime)

        # The following variables are historically per-gate so aggregate units...
        def merge_unit_status_list(eventtime, *, attr, key, fill_value):
            out = []
            for unit in self.mmu_machine.units:
                obj = getattr(unit, attr, None)
                if obj is None:
                    out.extend(repeat(fill_value, unit.num_gates))
                else:
                    out.extend(obj.get_status(eventtime)[key])
            return out

        # Merge espooler status and fill in units without espooler
        status["espooler"] = merge_unit_status_list(eventtime, attr="espooler", key="espooler", fill_value=ESPOOLER_NONE)

        # Merge environment status and fill in units without heater
        status["drying_state"] = merge_unit_status_list(eventtime, attr="environment_manager", key="drying_state", fill_value=DRYING_STATE_NONE)

        return status


# -----------------------------------------------------------------------------------------------------------
# CONSOLE LOGGING AND FORMATTING FUNCTIONS
# -----------------------------------------------------------------------------------------------------------

    # Fun visual display of current filament position
    def _display_visual_state(self):
        if self.p.log_visual and not self.calibrating:
            visual_str = self._state_to_string()
            self.log_always(visual_str, color=True)


    def _state_to_string(self, direction=None):
        arrow = "<" if self.filament_direction == DIRECTION_UNLOAD else ">"
        space = "."
        home = "|"

        gs = "(g)"
        es = "(e)"
        ts = "(t)"

        def past(pos):
            return arrow if self.filament_pos >= pos else space

        def homed(pos, sensor):
            if self.filament_pos > pos:
                return " ", arrow, sensor
            if self.filament_pos == pos:
                return home, space, sensor
            return " ", space, sensor

        def trig(name, sensor):
            if self.sensor_manager.check_sensor(sensor):
                return re.sub(r"[a-zA-Z]", "*", name)
            return name

        gate_endstop = self.mmu_unit().p.gate_homing_endstop
        require_bowden = self.mmu_unit().require_bowden_move

        t_str = (
            f"[T{self.tool_selected}] "
            if self.tool_selected >= 0
            else "BYPASS "
            if self.tool_selected == TOOL_GATE_BYPASS
            else "[T?] "
        )

        g_str = f"{past(FILAMENT_POS_UNLOADED)}"

        lg_str = (
            "{0}{0}".format(past(FILAMENT_POS_HOMED_GATE))
            if not require_bowden
            else ""
        )

        gs_str = (
            "{0}{2} {1}{1}".format(
                *homed(FILAMENT_POS_HOMED_GATE, trig(gs, gate_endstop))
            )
            if gate_endstop in [
                SENSOR_SHARED_EXIT,
                SENSOR_EXIT_PREFIX,
                SENSOR_EXTRUDER_ENTRY,
            ]
            else ""
        )

        en_pos = (
            FILAMENT_POS_IN_BOWDEN
            if gate_endstop in [
                SENSOR_SHARED_EXIT,
                SENSOR_EXIT_PREFIX,
                SENSOR_EXTRUDER_ENTRY,
            ]
            else FILAMENT_POS_START_BOWDEN
        )
        en_str = f" En {past(en_pos)}" if self.has_encoder() else ""

        bowden1 = (
            "{0}{0}{0}{0}".format(past(FILAMENT_POS_IN_BOWDEN))
            if require_bowden
            else ""
        )
        bowden2 = (
            "{0}{0}{0}{0}".format(past(FILAMENT_POS_END_BOWDEN))
            if require_bowden
            else ""
        )

        es_str = (
            "{0}{2} {1}{1}".format(
                *homed(FILAMENT_POS_HOMED_ENTRY, trig(es, SENSOR_EXTRUDER_ENTRY))
            )
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY) and require_bowden
            else ""
        )

        ex_str = "{0}[{2} {1}{1}".format(
            *homed(FILAMENT_POS_HOMED_EXTRUDER, "Ex")
        )

        ts_str = (
            "{0}{2} {1}".format(
                *homed(FILAMENT_POS_HOMED_TS, trig(ts, SENSOR_TOOLHEAD))
            )
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)
            else ""
        )

        nz_str = f"{past(FILAMENT_POS_LOADED)} Nz]"

        summary = (
            " {5}{4}LOADED{0}{6}"
            if self.filament_pos == FILAMENT_POS_LOADED
            else " {5}{4}UNLOADED{0}{6}"
            if self.filament_pos == FILAMENT_POS_UNLOADED
            else " {5}{2}UNKNOWN{0}{6}"
            if self.filament_pos == FILAMENT_POS_UNKNOWN
            else ""
        )

        encoder_str = (
            " {1}(e:%.1fmm){0}" % self.get_encoder_distance(dwell=None)
            if self.has_encoder() and self.mmu_unit().p.encoder_move_validation
            else ""
        )
        counter = " {5}%.1fmm{6}%s" % (self.get_filament_position(), encoder_str)

        return "".join(
            (
                t_str,
                g_str,
                lg_str,
                gs_str,
                en_str,
                bowden1,
                bowden2,
                es_str,
                ex_str,
                ts_str,
                nz_str,
                summary,
                counter,
            )
        )


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
            bowden_length = self.mmu_unit().calibrator.get_bowden_length()
            if bowden_length > 0:
                current = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_bowden_position()
                progress = abs(current - self.bowden_start_pos) / bowden_length
                if self.filament_direction == DIRECTION_UNLOAD:
                    progress = 1 - progress
                return round(max(0, min(100, progress * 100)))
        return -1


    def _get_filament_char(self, gate, show_letter=False, show_swatch=False, symbol=None):
        """
        Return a gate’s display character (swatch or status letter) based on UI and availability.

        Args:
            show_swatch: Flag to always return the swatch character
            show_letter: Flag to provide letter indication of "from spool" or "from buffer" if not forcing swatch
        """
        show_letter = (
            show_letter and
            self.mmu_unit(gate).p.has_filament_buffer and
            not self.has_espooler(gate)
        )
        gate_status = self.gate_status[gate]

        swatch = "*" # Fallback character
        if self.p.console_show_filament_color:
            symbol = symbol or UI_SOLID_SQUARE
            if self.gate_color[gate]:
                rgb_hex = MmuColorUtils.color_to_rgb_hex(self.gate_color[gate], "FFFFFF")
                swatch = '{{%s}}%s{{}}' % (rgb_hex, symbol)
            else:
                swatch = symbol

        if self.endless_spool_enabled and gate == self.p.endless_spool_eject_gate:
            return "W" # Always show waste gate for filament tips if configured
        elif gate_status == GATE_AVAILABLE_FROM_BUFFER:
            return "B" if show_letter and not show_swatch else swatch
        elif gate_status == GATE_AVAILABLE:
            return "S" if show_letter and not show_swatch else swatch
        elif gate_status == GATE_EMPTY:
            return "-" if show_letter or show_swatch else UI_SEPARATOR

        return "?" if show_letter or show_swatch else UI_SEPARATOR


    def _mmu_visual_to_string(self):
        """
        Build a multi-line ASCII visualization of units, gates, tools, availability, and selection state.
        """
        divider = UI_SPACE + UI_SEPARATOR + UI_SPACE
        msg_units = "Unit : "
        msg_gates = "Gate : "
        msg_tools = "Tools: "
        msg_avail = "Avail: "
        msg_selct = "Selct: "
        for unit in range(self.mmu_machine.num_units):
            unit = self.mmu_machine.get_mmu_unit_by_index(unit)
            gate_indices = range(unit.first_gate, unit.first_gate + unit.num_gates)
            last_gate = gate_indices[-1] == self.num_gates - 1
            sep = ("|" + divider) if not last_gate else "|"
            tool_strings = []
            select_strings = []
            fil_swatch = UI_SEPARATOR
            selct_char = "~" if unit.selector.is_homed else "X"
            for g in gate_indices:
                msg_gates += "".join("|{:^3}".format(g) if g < 10 else "| {:2}".format(g))

                fc = self._get_filament_char(g)
                fcs = self._get_filament_char(g, show_letter=True, show_swatch=True)
                msg_avail += "".join("|%s%s%s" % (fc, fcs, fc))

                tool_str = "+".join("T%d" % t for t in range(self.num_gates) if self.ttg_map[t] == g)
                tool_strings.append(("|%s " % (tool_str if tool_str else " {} ".format(UI_SEPARATOR)))[:4])

                if g == self.gate_selected:
                    if self.filament_pos < FILAMENT_POS_START_BOWDEN:
                        fil_swatch = UI_SEPARATOR
                    else:
                        fil_swatch = self._get_filament_char(g, show_swatch=True, symbol=UI_SOLID_TRIANGLE)

                    select_strings.append("|\*/|")
                else:
                    select_strings.append(selct_char * 4)

            unit_str = "{0:-^{width}}".format( " " + str(unit.name) + " ", width=len(gate_indices) * 4 + 1)
            msg_units += unit_str + (divider if not last_gate else "")
            msg_gates += sep
            msg_avail += sep
            msg_tools += "".join(tool_strings) + sep
            msg_selct += ("".join(select_strings) + selct_char)[:len(gate_indices) * 4 + 1] + (divider if not last_gate else "")
            msg_selct = msg_selct.replace("*", fil_swatch)

        lines = [msg_units] if len(self.mmu_machine.units) > 1 else []
        lines.extend([msg_gates, msg_tools, msg_avail, msg_selct])
        msg = "\n".join(lines)
        if self.tool_selected != TOOL_GATE_UNKNOWN:
            msg += " " + self.selected_tool_string()
        return msg


# -----------------------------------------------------------------------------------------------------------
# SWAP STATISTIC FUNCTIONS AND REPORTING
# -----------------------------------------------------------------------------------------------------------

    def _sample_stats(self, values):
        mean = stdev = vmin = vmax = 0.
        if values:
            mean = sum(values) / len(values)
            diff2 = [( v - mean )**2 for v in values]
            stdev = math.sqrt( sum(diff2) / max((len(values) - 1), 1))
            vmin = min(values)
            vmax = max(values)
        return {'mean': mean, 'stdev': stdev, 'min': vmin, 'max': vmax, 'range': vmax - vmin}


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

            if self.can_use_encoder() and gate:
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

        # Also a good place to update the persisted calibrated clog length (for auto mode)
        if self.has_encoder():
            mode = self.mmu_unit().sync_feedback.p.flowguard_encoder_mode
            if mode == ENCODER_RUNOUT_AUTOMATIC:
                cdl = self.encoder().get_clog_detection_length()
                self.mmu_unit().calibrator.update_clog_detection_length(round(cdl, 1))

        self.var_manager.write()


    def _persist_swap_statistics(self):
        self.statistics = {key: round(value, 2) if isinstance(value, float) else value for key, value in self.statistics.items()}
        self.var_manager.set(VARS_MMU_SWAP_STATISTICS, self.statistics, write=True)



# -----------------------------------------------------------------------------------------------------------
# GATE MAP / TTG MAP ACCESSOR PROPERTIES
# (gate maps are now in separate module but this keeps accessors intact
# -----------------------------------------------------------------------------------------------------------

    @property
    def gate_status(self):
        return self.gate_maps.gate_status

    @gate_status.setter
    def gate_status(self, value):
        self.gate_maps.gate_status = value
        self.gate_maps._dirty = True

    @property
    def gate_filament_name(self):
        return self.gate_maps.gate_filament_name

    @gate_filament_name.setter
    def gate_filament_name(self, value):
        self.gate_maps.gate_filament_name = value
        self.gate_maps._dirty = True

    @property
    def gate_material(self):
        return self.gate_maps.gate_material

    @gate_material.setter
    def gate_material(self, value):
        self.gate_maps.gate_material = value
        self.gate_maps._dirty = True

    @property
    def gate_color(self):
        return self.gate_maps.gate_color

    @gate_color.setter
    def gate_color(self, value):
        self.gate_maps.gate_color = value
        self.gate_maps._dirty = True

    @property
    def gate_temperature(self):
        return self.gate_maps.gate_temperature

    @gate_temperature.setter
    def gate_temperature(self, value):
        self.gate_maps.gate_temperature = value
        self.gate_maps._dirty = True

    @property
    def gate_spool_id(self):
        return self.gate_maps.gate_spool_id

    @gate_spool_id.setter
    def gate_spool_id(self, value):
        self.gate_maps.gate_spool_id = value
        self.gate_maps._dirty = True

    @property
    def gate_speed_override(self):
        return self.gate_maps.gate_speed_override

    @gate_speed_override.setter
    def gate_speed_override(self, value):
        self.gate_maps.gate_speed_override = value
        self.gate_maps._dirty = True

    @property
    def gate_color_rgb(self):
        return self.gate_maps.gate_color_rgb

    @gate_color_rgb.setter
    def gate_color_rgb(self, value):
        self.gate_maps.gate_color_rgb = value
        self.gate_maps._dirty = True

    @property
    def endless_spool_enabled(self):
        return self.gate_maps.endless_spool_enabled

    @endless_spool_enabled.setter
    def endless_spool_enabled(self, value):
        self.gate_maps.endless_spool_enabled = value

    @property
    def endless_spool_groups(self):
        return self.gate_maps.endless_spool_groups

    @endless_spool_groups.setter
    def endless_spool_groups(self, value):
        self.gate_maps.endless_spool_groups = value

    @property
    def slicer_color_rgb(self):
        return self.gate_maps.slicer_color_rgb

    @slicer_color_rgb.setter
    def slicer_color_rgb(self, value):
        self.gate_maps.slicer_color_rgb = value

    @property
    def ttg_map(self):
        return self.gate_maps.ttg_map

    @ttg_map.setter
    def ttg_map(self, value):
        self.gate_maps.ttg_map = value

    @property
    def slicer_tool_map(self):
        return self.gate_maps.slicer_tool_map

    @slicer_tool_map.setter
    def slicer_tool_map(self, value):
        self.gate_maps.slicer_tool_map = value


# -----------------------------------------------------------------------------------------------------------
# MMU PRINT STATE MACHINE ACCESSOR METHODS
# (printer state is now in own module but this keeps landing sites intact)
# -----------------------------------------------------------------------------------------------------------

    def is_printing(self, force_in_print=False): # Actively printing and not paused
        return self.psm.is_printing(force_in_print)

    def is_in_print(self, force_in_print=False): # Printing or paused
        return self.psm.is_in_print(force_in_print)

    def is_mmu_paused(self): # The MMU is paused
        return self.psm.is_mmu_paused()

    def is_mmu_paused_and_locked(self): # The MMU is paused (and locked)
        return self.psm.is_mmu_paused_and_locked()

    def is_in_endstate(self):
        return self.psm.is_in_endstate()

    def is_in_standby(self):
        return self.psm.is_in_standby()

    def is_printer_printing(self):
        return self.psm.is_printer_printing()

    def is_printer_paused(self):
        return self.psm.is_printer_paused()

    def is_paused(self):
        return self.psm.is_paused()

    def wakeup(self):
        self.psm.wakeup()

    def print_event(self, command):
        self.psm.print_event(command)

    def set_print_state(self, print_state, call_macro=True):
        self.psm.set_print_state(print_state, call_macro=call_macro)

    def on_print_start(self, pre_start_only=False):
        self.psm.on_print_start(pre_start_only=pre_start_only)

    def fix_started_state(self):
        self.psm.fix_started_state()

    def on_print_end(self, state="complete"):
        self.psm.on_print_end(state=state)


# -----------------------------------------------------------------------------------------------------------
# MMU LOGGER ACCESSOR METHODS
# (logging is now in own module but this keeps landing sites intact)
# -----------------------------------------------------------------------------------------------------------

    def _persist_counters(self):
        self.var_manager.set(VARS_MMU_COUNTERS, self.counters, write=True)

    def log_to_file(self, msg, prefix='> '):
        self.logger.log_to_file(msg, prefix)

    def log_assertion(self, msg, exc_info=None, color=False):
        self.logger.log_assertion(msg, exc_info, color)

    def log_error(self, msg, color=False):
        self.logger.log_error(msg, color)

    def log_warning(self, msg):
        self.logger.log_warning(msg)

    def log_always(self, msg, color=False):
        self.logger.log_always(msg, color)

    def log_info(self, msg, color=False):
        self.logger.log_info(msg, color)

    def log_debug(self, msg):
        self.logger.log_debug(msg)

    def log_trace(self, msg):
        self.logger.log_trace(msg)

    def log_stepper(self, msg):
        self.logger.log_stepper(msg)

    def log_enabled(self, level):
        return self.logger.log_enabled(level)


# -----------------------------------------------------------------------------------------------------------
# MMU STATE FUNCTIONS
# -----------------------------------------------------------------------------------------------------------

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
            mod_gate_ids = self.gate_maps.assign_spool_id(gate, self.pending_spool_id)

            # Request sync and update of filament attributes from Spoolman
            if self.p.spoolman_support == SPOOLMAN_PUSH:
                self._spoolman_push_gate_map(mod_gate_ids)
            elif self.p.spoolman_support == SPOOLMAN_READONLY:
                self._spoolman_update_filaments(mod_gate_ids)

        # Disable timer to prevent reuse
        self.pending_spool_id = -1
        self.reactor.update_timer(self.pending_spool_id_timer, self.reactor.NEVER)


    def handle_mmu_error(self, reason, force_in_print=False):
        self.psm.fix_started_state() # Get out of 'started' state before transistion to mmu pause

        run_pause_macro = run_error_macro = recover_pos = send_event = False
        if self.is_in_print(force_in_print):
            if not self.is_mmu_paused():
                self._disable_filament_monitoring() # Disable filament monitoring while in paused state
                self._track_pause_start()
                self.psm.resume_to_state = 'printing' if self.is_in_print() else 'ready'
                self.psm.reason_for_pause = reason # Only store reason on first error
                self._display_mmu_error()
                self.psm.paused_extruder_temp = self.printer.lookup_object(self.mmu_unit().extruder_name()).heater.target_temp
                self.log_trace("Saved desired extruder temperature: %.1f%sC" % (self.psm.paused_extruder_temp, UI_DEGREE))
                self.reactor.update_timer(self.hotend_off_timer, self.reactor.monotonic() + self.p.disable_heater) # Set extruder off timer
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.p.timeout_pause) # Set alternative pause idle_timeout
                self.log_trace("Extruder heater will be disabled in %s" % (self._seconds_to_string(self.p.disable_heater)))
                self.log_trace("Idle timeout in %s" % self._seconds_to_string(self.p.timeout_pause))
                self._save_toolhead_position_and_park('pause') # if already paused this is a no-op
                run_error_macro = True
                run_pause_macro = not self.is_printer_paused()
                send_event = True
                recover_pos = self.p.filament_recovery_on_pause
                self.psm.set_print_state("pause_locked")
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
            reason = self.psm.reason_for_pause.replace("\n", ". ")
            for c in "#;'":
                reason = reason.replace(c, "")
            self.wrap_gcode_command('%s MSG="%s" REASON="%s"' % (self.p.error_dialog_macro, msg, reason))
        self.log_error("MMU issue detected. %s\nReason: %s" % (msg, self.psm.reason_for_pause))
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
            if self.psm.paused_extruder_temp:
                self.log_info("Enabled extruder heater")
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self.psm.set_print_state("paused")


    # Continue after load/unload/change_tool/runout operation or pause/error
    def _continue_after(self, operation, force_in_print=False, restore=True):
        self.log_debug("Continuing from %s state after %s" % (self.psm.print_state, operation))
        if self.is_mmu_paused() and operation == 'resume':
            self.psm.reason_for_pause = None
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self.psm.paused_extruder_temp = None
            self._track_pause_end()
            if self.is_in_print(force_in_print):
                self._enable_filament_monitoring() # Enable filament monitoring while printing
            self.psm.set_print_state(self.psm.resume_to_state)
            self.psm.resume_to_state = "ready"
            self.printer.send_event("mmu:mmu_resumed")
        elif self.is_mmu_paused():
            # If paused we can only continue on resume
            return

        if self.is_printing(force_in_print):
            self.sensor_manager.confirm_loaded() # Can throw MmuError
            self.is_handling_runout = False
            self.initialize_encoder(dwell=None) # Encoder 0000

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
            self.movequeue_wait()

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
                    self.tool_speed_multipliers[self.tool_selected] = mmu_state['speed_factor'] * 60. # PAUL why * 60? tool_speed_mult is %?
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
                mmu_state['speed_factor'] = self.tool_speed_multipliers[self.tool_selected] / 60. # PAUL why / 60? tool_speed_mult is a %?
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
    def wrap_suspend_filament_monitoring(self):
        enabled = self._disable_filament_monitoring()
        try:
            yield self
        finally:
            if enabled:
                self._enable_filament_monitoring()


    # To suppress visual filament position
    @contextlib.contextmanager
    def wrap_suppress_visual_log(self):
        log_visual = self.p.log_visual
        self.p.log_visual = 0
        try:
            yield self
        finally:
            self.p.log_visual = log_visual


# -----------------------------------------------------------------------------------------------------------
# FILAMENT POSITION & (OPTIONAL) ENCODER ACCESS
# -----------------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def require_encoder(self):
        """
        Context: Forces encoder to validate despite user config by overriding 'encoder_move_validation' setting.
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


    def _encoder_dwell(self, dwell=False):
        """
        For all encoder methods, 'dwell' means:
          True  - gives klipper a little extra time to deliver all encoder pulses when absolute accuracy is required
          False - wait for moves to complete and then read encoder
          None  - just read encoder without delay (caller responsible for ensuring prior movements have completed)
        """
        if dwell is True:
            self.log_info(f"PAUL: _encoder_dwell({dwell})")
            self.movequeue_dwell(self.mmu_unit().p.encoder_dwell)
            self.movequeue_wait()
        elif dwell is False:
            self.log_info(f"PAUL: _encoder_dwell({dwell})")
            self.movequeue_wait()


    def initialize_encoder(self, dwell=False):
        if not self.has_encoder(): return

        self._encoder_dwell(dwell)
        self.encoder().reset_counts()


    def get_encoder_counts(self, dwell=False):
        if not self.has_encoder(): return 0.

        self._encoder_dwell(dwell)
        return self.encoder().get_counts()


    def get_encoder_distance(self, dwell=False):
        if not self.has_encoder(): return 0.

        self._encoder_dwell(dwell)
        return self.encoder().get_distance()


    def set_encoder_distance(self, distance, dwell=False):
        if not self.has_encoder(): return

        self._encoder_dwell(dwell)
        self.encoder().set_distance(distance)


    def adjust_encoder_distance(self, adjustment):
        """
        Apply distance adjustment to encoder. No need to wait/dwell
        """
        if not self.has_encoder(): return

        current = self.encoder().get_distance()
        self.encoder().set_distance(current + adjustment)


    def initialize_filament_position(self, dwell=False):
        self.initialize_encoder(dwell=dwell)
        self.set_filament_position()


    def _get_live_bowden_position(self):
        """
        Return the approximate live filament position for dynamic feedback of position
        """
        return self.gear().get_live_filament_position()


    def get_filament_position(self):
        return self.gear().get_filament_position()


    def set_filament_position(self, pos= 0.):
        self.gear().set_filament_position(pos)


    def set_filament_direction(self, direction):
        self.filament_direction = direction


    def set_filament_pos_state(self, state, silent=False):
        if self.filament_pos != state:
            self.filament_pos = state
            if self.gate_selected != TOOL_GATE_BYPASS or state == FILAMENT_POS_UNLOADED or state == FILAMENT_POS_LOADED:
                if not silent:
                    self._display_visual_state()

            # Minimal save_variable writes
            if state in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]:
                self.var_manager.set(VARS_MMU_FILAMENT_POS, state, write=True)
            elif self.var_manager.get(VARS_MMU_FILAMENT_POS, 0) != FILAMENT_POS_UNKNOWN:
                self.var_manager.set(VARS_MMU_FILAMENT_POS, FILAMENT_POS_UNKNOWN, write=True)

        # Good place to ensure espooler state
        self._adjust_espooler_assist()


# -----------------------------------------------------------------------------------------------------------
#
# -----------------------------------------------------------------------------------------------------------

    def _set_last_tool(self, tool):
        self._last_tool = tool
        self.var_manager.set(VARS_MMU_LAST_TOOL, tool, write=True)


    def _adjust_espooler_assist(self):
        """
        Ensure espooler print assist is in correct state based on whether the filament is in the extruder or not
        """
        if self.has_espooler():
            if self.filament_pos == FILAMENT_POS_LOADED:
                if ESPOOLER_PRINT in self.mmu_unit().p.espooler_operations and self.mmu_unit().p.espooler_printing_power == 0:
                    # Enable in-print assist because filament is in the extruder
                    self.espooler().set_print_assist_mode(self.gate_selected)
            else:
                # Ensure in-print assist mode is removed
                # (it could have been enabled manually with MMU_ESPOOLER)
                self.espooler().reset_print_assist_mode()


    def _gate_homing_string(self):
        return "ENCODER" if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER else "%s sensor" % self.mmu_unit().p.gate_homing_endstop


    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.mmu_unit().extruder_name())
        current_temp = extruder.get_status(0)['temperature']
        current_target_temp = extruder.heater.target_temp
        klipper_minimum_temp = extruder.get_heater().min_extrude_temp
        gate_temp = self.gate_temperature[self.gate_selected] if self.gate_selected >= 0 and self.gate_temperature[self.gate_selected] > 0 else self.p.default_extruder_temp
        self.log_trace("_ensure_safe_extruder_temperature: current_temp=%s, paused_extruder_temp=%s, current_target_temp=%s, klipper_minimum_temp=%s, gate_temp=%s, default_extruder_temp=%s, source=%s" % (current_temp, self.psm.paused_extruder_temp, current_target_temp, klipper_minimum_temp, gate_temp, self.p.default_extruder_temp, source))

        if source == "pause":
            new_target_temp = self.psm.paused_extruder_temp if self.psm.paused_extruder_temp is not None else current_temp # Pause temp should not be None
            if self.psm.paused_extruder_temp is not None and self.psm.paused_extruder_temp < klipper_minimum_temp:
                # Don't wait if just messing with cold printer
                wait = False

        elif source == "auto": # Normal case
            if self.is_mmu_paused():
                # In a pause we always want to restore the temp we paused at
                if self.psm.paused_extruder_temp is not None:
                    new_target_temp = self.psm.paused_extruder_temp
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
                    self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=%s MINIMUM=%.1f MAXIMUM=%.1f" % (self.mmu_unit().extruder_name(), new_target_temp - self.p.extruder_temp_variance, new_target_temp + self.p.extruder_temp_variance))


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
        self.psm.set_print_state("standby")
        self.log_always("MMU disabled")


    def motors_onoff(self, on=False, motor="all"):
        if motor in ["all", "gear", "gears"]:
            if on:
                self.reset_sync_gear_to_extruder(False)
            else:
                self.gear().sync_mode(DRIVE_UNSYNCED)

        for mmu_unit in self.mmu_machine.units:
            mmu_unit.motors_onoff(on, motor)


    def _random_failure(self):
        """
        For developer testing to introduce random failures in loading/unload operations
        """
        if self.p.test_random_failures and random.randint(0, 10) == 0:
            raise MmuError("Randomized testing failure")


# -----------------------------------------------------------------------------------------------------------
# TOOL SELECTION FUNCTIONS
# -----------------------------------------------------------------------------------------------------------

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
            self.log_debug('Loading tool %s...' % self.selected_tool_string(tool))
            from_gate = self.gate_selected
            self.select_tool(tool)
            gate = self.ttg_map[tool] if tool >= 0 else self.gate_selected
            if self.gate_status[gate] == GATE_EMPTY:
                if self.endless_spool_enabled and self.p.endless_spool_on_load:
                    next_gate, msg = self.gate_maps.get_next_endless_spool_gate(tool, gate)
                    if next_gate == -1:
                        raise MmuError("Gate %d is empty!\nNo alternatives gates available after checking %s" % (gate, msg))

                    self.log_error("Gate %d is empty! Checking for alternative gates %s" % (gate, msg))
                    self.log_info("Remapping %s to gate %d" % (self.selected_tool_string(tool), next_gate))
                    self.gate_maps.remap_tool(tool, next_gate)
                    self.select_tool(tool)

                else:
                    raise MmuError("Gate %d is empty (and EndlessSpool on load is disabled)\nLoad gate, remap tool to another gate or correct state with 'MMU_CHECK_GATE GATE=%d' or 'MMU_GATE_MAP GATE=%d AVAILABLE=1'" % (gate, gate, gate))

            # Determine purge volume for toolchange/load. Valid only during toolchange/load operation
            self.toolchange_purge_volume, self._slicer_purge_volume  = self._calc_purge_volume(self._last_tool, tool, from_gate, self.gate_selected)

            self.load_sequence(purge=purge)
            self._restore_tool_override(self.tool_selected) # Restore M220 and M221 overrides

        finally:
            self.toolchange_purge_volume = self._slicer_purge_volume = 0.


    def _calc_purge_volume(self, from_tool, to_tool, from_gate, to_gate):
        """
        Helper to determine purge volume for toolchange.
        Uses new printer toolhead for residuals

          Rtn:
            Tuple (total purge volume, slicer portion of volume)

          TODO FIXME: This is no longer correct if switching between toolheads because
                      color in previous toolhead is not the last slicer color
        """

        fil_diameter = 1.75
        svolume = 0.
            
        if to_tool >= 0:
            slicer_purge_volumes = self.slicer_tool_map['purge_volumes']
            if slicer_purge_volumes:
                if from_tool >= 0: 
                    svolume = slicer_purge_volumes[from_tool][to_tool]
                else:   
                    # Assume worse case because we don't know from_tool
                    svolume = max(row[to_tool] for row in slicer_purge_volumes)
                    
        # Always add volume of residual filament (cut fragment and bit always left in the hotend)
        to_unit = self.mmu_unit(to_gate)
        remaining = to_unit.extruder_wrapper.get_status(0)['extruder_filament_remaining']
        total = svolume + math.pi * ((fil_diameter / 2) ** 2) * remaining

        return total, svolume


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


    def home_unit(self, mmu_unit, force_unload=None):
        if mmu_unit.selector.requires_homing: # Make a no-op for MMU's that don't require homing (like class-B designs)
            if mmu_unit.manages_gate(self.gate_selected):
                if not force_unload and self.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_UNKNOWN]:
                    raise MmuError("Cannot home %s because has filament loaded" % mmu_unit.name)
                else:
                    try:
                        prev_gate = self.gate_selected
                        self._set_gate_selected(TOOL_GATE_UNKNOWN)
                        mmu_unit.selector.home(force_unload)
                        if prev_gate != TOOL_GATE_UNKNOWN:
                            self.select_gate(prev_gate)
                        else:
                            self.select_gate(mmu_unit.first_gate)
                    except MmuError as ee:
                        self._set_gate_selected(TOOL_GATE_UNKNOWN)
                        raise ee
            else:
                # Safe to just home selector
                mmu_unit.selector.home()


    def select_gate(self, gate):
        self.log_warning(f"PAUL: select_gate({gate}): gate_selected:{self.gate_selected}")
        try:
            if gate == self.gate_selected:
                self.selector().select_gate(gate) # Always give selector a chance to fix position
            else:
                self._next_gate = gate # Valid only during the gate selection process
                _prev_gate = self.gate_selected
                self.selector(gate).select_gate(gate)
                self._set_gate_selected(gate) # Will send gate/unit changed events

        except MmuError as ee:
            self.unselect_gate()
            raise ee
        finally:
            self._next_gate = None


    def unselect_gate(self):
        self.selector().select_gate(TOOL_GATE_UNKNOWN) # Required for type-B MMU's to unsync
        self._set_gate_selected(TOOL_GATE_UNKNOWN)


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
        if (
            self.tool_selected == TOOL_GATE_BYPASS and
            self.gate_selected == TOOL_GATE_BYPASS
        ):
            return

        self.log_info("Selecting filament bypass...")
        self.select_gate(TOOL_GATE_BYPASS)
        self._set_tool_selected(TOOL_GATE_BYPASS)
        self.set_filament_direction(DIRECTION_LOAD)
        self.log_info("Bypass enabled")


    def _set_tool_selected(self, tool):
        self.log_info("PAUL: _set_tool_selected(%d)" % tool)
        if tool != self.tool_selected:
            self.tool_selected = tool
            self.printer.send_event("mmu:tool_selected", self.tool_selected)
            self.var_manager.set(VARS_MMU_TOOL_SELECTED, self.tool_selected, write=True)


    def _set_gate_selected(self, gate):
        self.log_info("PAUL: _set_gate_selected(%d)" % gate)
        prev_gate = self.gate_selected
        self.gate_selected = gate

        new_unit_index = self.mmu_unit(gate).unit_index
        if new_unit_index != self.unit_selected:
            self.unit_selected = new_unit_index
            self.printer.send_event("mmu:unit_selected", self.unit_selected)

        self.printer.send_event("mmu:gate_selected", self.gate_selected)
        self.mmu_unit(gate).calibrator.restore_gear_rd()

        # Update from/to leds after selection
        self.led_manager.gate_map_changed(prev_gate)
        self.led_manager.gate_map_changed(gate)

        self.var_manager.set(VARS_MMU_GATE_SELECTED, self.gate_selected, write=True)
        self.active_filament = {
            'filament_name': self.gate_filament_name[gate],
            'material': self.gate_material[gate],
            'color': self.gate_color[gate],
            'spool_id': self.gate_spool_id[gate],
            'temperature': self.gate_temperature[gate],
        } if gate >= 0 else {}



# -----------------------------------------------------------------------------------------------------------
# MOONRAKER HOOKS
# -----------------------------------------------------------------------------------------------------------

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



# -----------------------------------------------------------------------------------------------------------
# SPOOLMAN INTEGRATION
# -----------------------------------------------------------------------------------------------------------

    def _spoolman_sync(self, quiet = True):
        """
        Synchronize gate and filament data with Spoolman based on the configured support mode.

        Behavior depends on `self.p.spoolman_support`:

        - SPOOLMAN_PULL:
            Pull gate assignments and filament attributes from the Spoolman
            database, replacing the local gate map.

        - SPOOLMAN_PUSH:
            Push local gate assignments to Spoolman (for visualization), and
            update local gate map filament attributes from Spoolman, potentially
            overwriting local attribute data.

        - SPOOLMAN_READONLY:
            Update local filament attributes from Spoolman without modifying
            gate assignments.

        Args:
            quiet (bool): If True, suppress non-critical logging during synchronization.
        """
        if self.p.spoolman_support == SPOOLMAN_PULL:   # Remote gate map
            self._spoolman_pull_gate_map(quiet=quiet)
    
        elif self.p.spoolman_support == SPOOLMAN_PUSH: # Local gate map
            self._spoolman_push_gate_map(quiet=quiet)
    
        elif self.p.spoolman_support == SPOOLMAN_READONLY: # Get filament attributes only
            self._spoolman_update_filaments(quiet=quiet)


    def _spoolman_activate_spool(self, spool_id=-1):
        """
        Activate or deactivate a Spoolman spool via Moonraker.

        Args:
            spool_id: Spool ID to activate. 0 deactivates; negative values
                result in no action.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            if spool_id < 0:
                self.log_debug("Spoolman spool_id not set for current gate")
            else:
                if spool_id == 0:
                    self.log_debug("Deactivating spoolman spool...")
                    spool_id = None  # id=0 no longer deactivates
                else:
                    self.log_debug("Activating spoolman spool %s..." % spool_id)
                webhooks.call_remote_method("spoolman_set_active_spool", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while setting active spool: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_update_filaments(self, gate_ids=None, quiet=True):
        """
        Request filament attributes from Spoolman for specified gates.

        Args:
            gate_ids: Optional list of (gate_id, spool_id) pairs. If None,
                all gates with valid spool IDs are requested.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        if gate_ids is None:
            pruned_gate_ids = [(g, self.gate_spool_id[g])
                               for g in range(self.num_gates)
                               if self.gate_spool_id[g] >= 0]
        else:
            pruned_gate_ids = [(g, sid) for g, sid in gate_ids if sid >= 0]

        if pruned_gate_ids:
            self.log_debug("Requesting the following gate/spool_id pairs from Spoolman: %s" % pruned_gate_ids)
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("spoolman_get_filaments",
                                            gate_ids=pruned_gate_ids,
                                            silent=quiet)
            except Exception as e:
                self.log_error("Error while fetching filament attributes from spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_push_gate_map(self, gate_ids=None, quiet=True):
        """
        Push the current gate-to-spool mapping to Spoolman.

        Args:
            gate_ids: Optional list of (gate_id, spool_id) pairs. If None,
                all gates are pushed.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Pushing gate mapping to Spoolman")
        if gate_ids is None:
            gate_ids = [(i, self.gate_spool_id[i]) for i in range(self.num_gates)]
        try:
            webhooks = self.printer.lookup_object('webhooks')
            self.log_debug("Storing gate map in spoolman db...")
            webhooks.call_remote_method("spoolman_push_gate_map", gate_ids=gate_ids, silent=quiet)
        except Exception as e:
            self.log_error("Error while pushing gate map to spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_pull_gate_map(self, quiet=True):
        """
        Request and apply the gate map stored in Spoolman.

        Args:
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting the gate map from Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_pull_gate_map", silent=quiet)
        except Exception as e:
            self.log_error("Error while requesting gate map from spoolman: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_clear_gate_map(self, sync=False, quiet=True):
        """
        Clear spool-to-gate associations in Spoolman.

        Args:
            sync: If True, request synchronous clearing.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting to clear the gate map in Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_clear_spools_for_printer", sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while clearing spoolman gate mapping: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_refresh(self, fix, quiet=True):
        """
        Refresh the Spoolman cache to pick up external changes.

        Args:
            fix: Whether to request corrective reconciliation.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Requesting to refresh the spoolman gate cache")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_refresh", fix=fix, silent=quiet)
        except Exception as e:
            self.log_error("Error while refreshing spoolman gate cache: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_set_spool_gate(self, spool_id, gate, sync=False, quiet=True):
        """
        Force a spool-to-gate association in Spoolman.

        Args:
            spool_id: Spool ID to associate.
            gate: Gate ID to assign the spool to.
            sync: If True, request synchronous update.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Setting spool %d to gate %d directly in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_set_spool_gate",
                                        spool_id=spool_id, gate=gate,
                                        sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while setting spoolman gate association: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_unset_spool_gate(self, spool_id=None, gate=None, sync=False, quiet=True):
        """
        Remove a spool-to-gate association in Spoolman.

        Args:
            spool_id: Optional spool ID to unassign.
            gate: Optional gate ID to clear.
            sync: If True, request synchronous update.
            quiet: If True, suppress non-critical output.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        self.log_debug("Unsetting spool %s or gate %s in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_unset_spool_gate",
                                        spool_id=spool_id, gate=gate,
                                        sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while unsetting spoolman gate association: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_display_spool_info(self, spool_id):
        """
        Request and display detailed information for a spool.

        Args:
            spool_id: Spool ID to query.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_get_spool_info", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while displaying spool info: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))


    def _spoolman_display_spool_location(self, printer=None):
        """
        Request and display the spool location map.

        Args:
            printer: Optional printer identifier to filter results.
        """
        if self.p.spoolman_support == SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_display_spool_location", printer=printer)
        except Exception as e:
            self.log_error("Error while displaying spool location map: %s\n%s" % (str(e), SPOOLMAN_CONFIG_ERROR))




# -----------------------------------------------------------------------------------------------------------
# RUNOUT HANDLING
# -----------------------------------------------------------------------------------------------------------

    def _runout(self, event_type=None, sensor=None):
        """
        Handler for all "runout" type events including "clog" and "tangle".

        Args:
          event_type - type of runout, if None then caller isn't sure (runout or clog)
          sensor     - sensor that triggered the event or None if forced
        """
        with self.wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during handling
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
                        self.encoder().note_clog_detection_length()
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
                    self.gate_maps.set_gate_status(self.gate_selected, GATE_EMPTY) # Indicate current gate is empty
                    next_gate, msg = self.gate_maps.get_next_endless_spool_gate(self.tool_selected, self.gate_selected)
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
                    self.gate_maps.remap_tool(self.tool_selected, next_gate)
                    self._select_and_load_tool(self.tool_selected, purge=PURGE_STANDALONE) # if user has set up standalone purging, respect option and purge.

                    self._continue_after("endless_spool")
                    self.pause_resume.send_resume_command() # Undo what runout sensor handling did
                    return
                else:
                    raise MmuError("Runout detected on %s\nEndlessSpool mode is off - manual intervention is required" % sensor)

            raise MmuError("A %s has been detected on %s and requires manual intervention" % (type_str, sensor))


    # Wait for all movement to stop
    def movequeue_wait(self):
        self.toolhead.wait_moves()


    def movequeue_dwell(self, dwell):
        if dwell > 0.:
            self.toolhead.dwell(dwell)


# -----------------------------------------------------------------------------------------------------------
# INTERNAL (HIDDEN) PRINT WORKFLOW WRAPPER COMMANDS
# -----------------------------------------------------------------------------------------------------------

class MmuWrapperCancelPrintCommand(BaseCommand):

    CMD = "CANCEL_PRINT"
    HELP_BRIEF = "Internal wrapper around default CANCEL_PRINT command"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, None, CATEGORY_INTERNAL)

    def _run(self, gcmd):
        if self.mmu.is_enabled:
            self.mmu.psm.fix_started_state() # Get out of 'started' state before transistion to cancelled
            self.mmu.log_debug("MMU CANCEL_PRINT wrapper called")
            self.mmu._clear_mmu_error_dialog()
            self.mmu._save_toolhead_position_and_park("cancel")
            self.mmu.wrap_gcode_command("__CANCEL_PRINT", exception=None)
            self.mmu.psm.on_print_end("cancelled")
        else:
            self.mmu.wrap_gcode_command("__CANCEL_PRINT", exception=None)


class MmuWrapperResumeCommand(BaseCommand):

    CMD = "RESUME"
    HELP_BRIEF = "Internal wrapper around default RESUME command"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, None, CATEGORY_INTERNAL)

    def _run(self, gcmd):
        if not self.mmu.is_enabled:
            # User defined or Klipper default behavior
            self.mmu.wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), None)
            return

        self.mmu.log_debug("MMU RESUME wrapper called")
        if not self.mmu.is_paused():
            self.mmu.log_always("Print is not paused. Resume ignored.")
            return

        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        try:
            self.mmu._clear_mmu_error_dialog()
            if self.mmu.is_mmu_paused_and_locked():
                self.mmu._mmu_unlock()

            # Decide if we are ready to resume and give user opportunity to fix state first
            if self.mmu.sensor_manager.check_sensor(SENSOR_TOOLHEAD) is True:
                self.mmu.set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
                self.mmu.log_always("Automatically set filament state to LOADED based on toolhead sensor")
            if self.mmu.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_LOADED]:
                raise MmuError("Cannot resume because filament position not indicated as fully loaded (or unloaded). Ensure filament is loaded/unloaded and run:\n MMU_RECOVER LOADED=1 or MMU_RECOVER LOADED=0 or just MMU_RECOVER\nto reset state, then RESUME again")

            # Prevent BASE_RESUME from moving toolhead
            if TOOLHEAD_POSITION_STATE in self.mmu.gcode_move.saved_states:
                gcode_pos = self.mmu.gcode_move.get_status(self.mmu.reactor.monotonic())['gcode_position']
                try:
                    self.mmu.gcode_move.saved_states['PAUSE_STATE']['last_position'][:3] = gcode_pos[:3]
                except KeyError:
                    self.mmu.log_error("PAUSE_STATE not defined!")

            self.mmu.wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), exception=None)
            self.mmu._continue_after("resume", force_in_print=force_in_print)
        except MmuError as ee:
            self.mmu.handle_mmu_error(str(ee))


class MmuWrapperPauseCommand(BaseCommand):

    CMD = "PAUSE"
    HELP_BRIEF = "Internal wrapper around default PAUSE command"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, None, CATEGORY_INTERNAL)

    def _run(self, gcmd):
        if self.mmu.is_enabled:
            self.mmu.psm.fix_started_state() # Get out of 'started' state
            self.mmu.log_debug("MMU PAUSE wrapper called")
            self.mmu._save_toolhead_position_and_park("pause")
        self.mmu.wrap_gcode_command(" ".join(("__PAUSE", gcmd.get_raw_command_parameters())), exception=None)


class MmuWrapperClearPauseCommand(BaseCommand):

    CMD = "CLEAR_PAUSE"
    HELP_BRIEF = "Internal wrapper around default CLEAR_PAUSE command"
    HELP_PARAMS = "%s: %s\n" % (CMD, HELP_BRIEF)
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, None, CATEGORY_INTERNAL)

    def _run(self, gcmd):
        if self.mmu.is_enabled:
            self.mmu.log_debug("MMU CLEAR_PAUSE wrapper called")
            self.mmu._clear_macro_state()
            if self.mmu.saved_toolhead_operation == 'pause':
                self.mmu._clear_saved_toolhead_position()
        self.mmu.wrap_gcode_command("__CLEAR_PAUSE", exception=None)
