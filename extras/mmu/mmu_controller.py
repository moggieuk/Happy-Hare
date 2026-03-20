# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
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

#import gc, sys, ast, random, logging, time, contextlib, math, os.path, re, unicodedata, traceback
import logging, sys, ast, random, time, contextlib, math, re, unicodedata, traceback

from itertools                  import repeat

# Klipper imports

# Happy Hare imports
from .mmu_constants             import *
from .mmu_logger                import MmuLogger
from .mmu_utils                 import MmuError, PurgeVolCalculator
from .mmu_sensor_manager        import MmuSensorManager
from .mmu_sensor_utils          import MmuRunoutHelper
from .mmu_led_manager           import MmuLedManager
from .mmu_filament_workflow     import MmuFilamentWorkflow
from .commands                  import COMMAND_REGISTRY
from .commands.mmu_base_command import *


# Main klipper module
class MmuController(MmuFilamentWorkflow):

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
        self.w3c_colors = dict(W3C_COLORS)      # Standard symbolic color names
        self.filament_remaining = 0.            # The amount of filament remaining in extruder hotend
        self._next_gate = None
        self.toolchange_retract = 0.            # Set from mmu_macro_vars
        self.toolchange_purge_volume = 0.       # During toolchange, the calculated purge volume
#PAUL        self.mmu_logger = None                  # Setup on connect
        self._standalone_sync = False           # Used to indicate synced extruder intention whilst out of print
        self._suppress_release_grip = False     # Used to suppress the relaxing of grip on recursive calls to prevent servo flutter
        self.bowden_start_pos = None            # If set then we can measure bowden progress
        self.has_blobifier = False              # Post load blobbling macro (like BLOBIFIER)
        self.has_mmu_cutter = False             # Post unload cutting macro (like EREC)
        self.has_toolhead_cutter = False        # Form tip cutting macro (like _MMU_CUT_TIP)
        self._is_running_test = False           # True while running QA or soak tests
        self.slicer_tool_map = None             # Set by startup gcode from slicer during print
        self.gear_run_current_percent = self.extruder_run_current_percent = 100 # Current run percentages
        self._gear_run_current_locked = False   # True if gear current is currently locked by wrap_gear_current()
        self.p = mmu_machine.params             # Parameters shortcut
        self.kalico = bool(self.printer.lookup_object('danger_options', False))

        # Tool speed and extrusion multipliers
        self.tool_speed_multipliers     = [1.0] * self.num_gates # M220 record
        self.tool_extrusion_multipliers = [1.0] * self.num_gates # M221 record

        # Complete setup of other components
        self.logger                = MmuLogger(self)

        # Managers are responsible for collectively handling all gates accross multiple mmu_units and encapsulate
        # specific functionality to reduce the complexity of this controller class
        self.led_manager           = MmuLedManager(self)
        self.sensor_manager        = MmuSensorManager(self) # Must be done during initialization because also sets up homing endstops

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
        self.gate_map_vars = [ (VARS_MMU_GATE_STATUS,         'gate_status', GATE_UNKNOWN),
                               (VARS_MMU_GATE_FILAMENT_NAME,  'gate_filament_name', ""),
                               (VARS_MMU_GATE_MATERIAL,       'gate_material', ""),
                               (VARS_MMU_GATE_COLOR,          'gate_color', ""),
                               (VARS_MMU_GATE_TEMPERATURE,    'gate_temperature', int(self.p.default_extruder_temp)),
                               (VARS_MMU_GATE_SPOOL_ID,       'gate_spool_id', -1),
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


        # Register GCODE commands ---------------------------------------------------------------------------

        for name, cls in sorted(COMMAND_REGISTRY.items()):
            try:
                cls(self)
            except Exception:
                raise self.config.error(f"Failed to register command class: {name}")

        # Initializer tasks
        self.gcode.register_command('__MMU_BOOTUP', self.cmd_MMU_BOOTUP, desc = self.cmd_MMU_BOOTUP_help) # Bootup tasks # PAUL move to commands??


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

        if self.p.update_aht10_commands: # Command set of AHT10 (on ViViD) for older klipper
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


# PAUL old
#    def _setup_logging(self):
#        """
#        Setup background file based logging before logging any messages
#        """
#        if self.mmu_logger is None and self.p.log_file_level >= 0:
#            logfile_path = self.printer.start_args['log_file']
#            dirname = os.path.dirname(logfile_path)
#            if dirname is None:
#                mmu_log = '/tmp/mmu.log'
#            else:
#                mmu_log = dirname + '/mmu.log'
#            logging.info("MMU: Log: %s" % mmu_log)
#            self.mmu_logger = MmuLogger(mmu_log)
#            self.mmu_logger.log("\n\n\nMMU Startup -----------------------------------------------\n")


    def handle_connect(self):
        logging.info("PAUL: handle_connect: MmuController")
        self.toolhead = self.printer.lookup_object('toolhead')
# PAUL this is now done via gate_selected event in handle_ready .. load_persisted state.  Hopefully that isn't too late(?)
# PAUL        self.sensor_manager.reset_active_unit(self.unit_selected) # PAUL .... EVENT NOW>>
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
#PAUL OLD            park_toolchange = sequence_vars_macro.variables.get('park_toolchange',(0))
#PAUL OLD            self.toolchange_retract = park_toolchange[-1]

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
        self.last_print_stats = self.paused_extruder_temp = self.reason_for_pause = None
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
        self.print_state = self.resume_to_state = "ready"
        self.form_tip_vars = None   # Current defaults of gcode variables for tip forming macro
        self._clear_slicer_tool_map()
        self.pending_spool_id = -1  # For automatic assignment of spool_id if set perhaps by rfid reader
        self.saved_toolhead_max_accel = None
        self.num_toolchanges = 0

        self.mmu_machine.reinit() # Will iterate over all mmu_units


# -----------------------------------------------------------------------------------------------------------
# Per gate (unit) component accessor router. All assume current gate unless directly specified
# These are key to multi mmu-unit support
# -----------------------------------------------------------------------------------------------------------

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

    def encoder(self, gate=None):
        if gate is None:
            gate = self.gate_selected
        unit = self.mmu_unit(gate)
        if unit:
            return unit.encoder
        return None

    def has_encoder(self, gate=None):
        return self.encoder(gate) is not None

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
    def _color_to_rgb_hex(self, color, default="000000"):
        if color in self.w3c_colors:
            color = self.w3c_colors.get(color)
        elif color == '':
            color = default
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

        # Load selected gate and tool
        gate_selected = self.var_manager.get(VARS_MMU_GATE_SELECTED, self.gate_selected)
        tool_selected = self.var_manager.get(VARS_MMU_TOOL_SELECTED, self.tool_selected)
        if not (TOOL_GATE_BYPASS <= gate_selected < self.num_gates):
            if gate_selected != TOOL_GATE_UNKNOWN:
                errors.append("Invalid gate specified with %s or %s" % (VARS_MMU_TOOL_SELECTED, VARS_MMU_GATE_SELECTED))
            tool_selected = gate_selected = TOOL_GATE_UNKNOWN

        # No need for unknown gate on type-B MMU's (could also be first time bootup)
        if self.mmu_unit().multigear and gate_selected == TOOL_GATE_UNKNOWN:
            gate_selected = self.mmu_unit().first_gate

        selector = self.selector(gate_selected)
        if gate_selected != TOOL_GATE_UNKNOWN and not selector.is_homed:
            errors.append(f"Persisted gate/tool {gate_selected}/{tool_selected} dropped because selector isn't homed")
            tool_selected = gate_selected = TOOL_GATE_UNKNOWN

        self._set_gate_selected(gate_selected) # Will send gate_selected event to set active sensor map
        self._set_tool_selected(tool_selected)
        self._ensure_ttg_match()               # Ensure tool/gate consistency. Will change tool if necessary

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

        self.log_warning(f"PAUL ** : gate={self.gate_selected}, tool={self.tool_selected}, unit={self.unit_selected}, fil_pos={self.filament_pos}")

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
                    self.log_warning(
                        f"Warning: filament_switch_sensor '{fsensor_name}' found in printer configuration.\n"
                        f"This may interfere with MMU functionality{pause_on_runout_msg}."
                    )

            # Use per gate sensors to adjust gate map
            self.gate_status = self._validate_gate_status(self.gate_status)

            try:
                # Can we verify gate selected? If so fix if necessary
                validated_gate = self._validate_gate_selected()
                if validated_gate is not None and validated_gate != self.gate_selected:
                    self.log_info(f"Filament detected in gate {validated_gate}")
                    self._set_gate_selected(validated_gate)

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
            except Exception as e:
                # This is recoverable so just report errors
                self.log_error(str(e))

            # Autohoming...
            for u in self.mmu_machine.units:
                if u.p.startup_home_selector:
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
                self.log_info(f"Selecting last gate used ({self.gate_selected})...")
                self.select_gate(self.gate_selected)

            # TTG map...
            if self.p.startup_reset_ttg_map:
                self._reset_ttg_map()
            self._ensure_ttg_match()

            # Initial state
            self._set_print_state("initialized")

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
            self.mmu_toolhead().quiesce()

            # Status to console...
            if self.p.log_startup_status:
                self.log_always(self._mmu_visual_to_string(), color=True)
                self._display_visual_state()

            # Finally report if any recovery is necessary by user
            self.report_necessary_recovery()

            self.log_warning(f"PAUL ** : gate={self.gate_selected}, tool={self.tool_selected}, unit={self.unit_selected}, fil_pos={self.filament_pos}")

        except Exception as e:
            self.log_assertion(f"Error booting up MMU: {e}", exc_info=sys.exc_info())

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
                self.mmu_toolhead().quiesce()

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
    # TODO: PAUL: perhaps better now to remove this method and
    # TODO: always just call self.mmu_toolhead().quiesce()
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


# -----------------------------------------------------------------------------------------------------------
# AGGREGTED PRINTER VARIABLES FOR "LOGICAL" MMU MACHINE
# -----------------------------------------------------------------------------------------------------------

    # Note: Returning new lists/dicts so that moonraker sees the change
    def get_status(self, eventtime):
        return {} # PAUL TEST
        status = {
            'enabled': self.is_enabled,
            'num_gates': self.num_gates,
            'is_homed': self.selector().is_homed, # Always true on type-B MMU's
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
            'espooler_active': self.espooler().get_operation(self.gate_selected)[0] if self.has_espooler() else ESPOOLER_NONE, # LEGACY
            'endless_spool_enabled': self.endless_spool_enabled,
            'print_start_detection': self.p.print_start_detection, # For Klippain. Not really sure it is necessary

            # DEPRECATED but still used or likely to be used
            'runout': self.is_handling_runout, # DEPRECATED but still used in HH macros (better to use operation)
            'is_paused': self.is_mmu_paused(), # DEPRECATED (better to use print_state)

            # TODO PAUL I think these are ok to delete but check/fix Klipperscreen
            'is_locked': self.is_mmu_paused(), # DEPRECATED (alias for is_paused)
            'is_in_print': self.is_in_print(), # DEPRECATED (use print_state)
            'endless_spool': self.endless_spool_enabled,           # DEPRECATED PAUL check UI need
            'has_bypass': self.selector().has_bypass(), # TODO deprecate because this is a per unit selector bypass
            'clog_detection': False,           # DEPRECATED PAUL check UI need
            'clog_detection_enabled': False,   # DEPRECATED PAUL check UI need
        }

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
# LOGGING AND STATISTICS FUNCTIONS
# -----------------------------------------------------------------------------------------------------------

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
                current = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()
                progress = abs(current - self.bowden_start_pos) / bowden_length
                if self.filament_direction == DIRECTION_UNLOAD:
                    progress = 1 - progress
                return round(max(0, min(100, progress * 100)))
        return -1


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


    # Logger moved to own module so these are redirects -------------

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


    # Fun visual display of current filament position
    def _display_visual_state(self):
        if self.p.log_visual and not self.calibrating:
            visual_str = self._state_to_string()
            self.log_always(visual_str, color=True)


    def _state_to_string(self, direction=None):
        arrow = "<" if self.filament_direction == DIRECTION_UNLOAD else ">"
        space = "."
        home  = "|"
        gs = "(g)" # SENSOR_SHARED_EXIT or SENSOR_EXIT_PREFIX
        es = "(e)" # SENSOR_EXTRUDER
        ts = "(t)" # SENSOR_TOOLHEAD
        past  = lambda pos: arrow if self.filament_pos >= pos else space
        homed = lambda pos, sensor: (' ',arrow,sensor) if self.filament_pos > pos else (home,space,sensor) if self.filament_pos == pos else (' ',space,sensor)
        trig  = lambda name, sensor: re.sub(r'[a-zA-Z]', '*', name) if self.sensor_manager.check_sensor(sensor) else name

        t_str   = ("[T%s] " % str(self.tool_selected)) if self.tool_selected >= 0 else "BYPASS " if self.tool_selected == TOOL_GATE_BYPASS else "[T?] "
        g_str   = "{}".format(past(FILAMENT_POS_UNLOADED))
        lg_str  = "{0}{0}".format(past(FILAMENT_POS_HOMED_GATE)) if not self.mmu_unit().require_bowden_move else ""
        gs_str  = "{0}{2} {1}{1}".format(*homed(FILAMENT_POS_HOMED_GATE, trig(gs, self.mmu_unit().p.gate_homing_endstop))) if self.mmu_unit().p.gate_homing_endstop in [SENSOR_SHARED_EXIT, SENSOR_EXIT_PREFIX, SENSOR_EXTRUDER_ENTRY] else ""
        en_str  = " En {0}".format(past(FILAMENT_POS_IN_BOWDEN if self.mmu_unit().p.gate_homing_endstop in [SENSOR_SHARED_EXIT, SENSOR_EXIT_PREFIX, SENSOR_EXTRUDER_ENTRY] else FILAMENT_POS_START_BOWDEN)) if self.has_encoder() else ""
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
                self.paused_extruder_temp = self.printer.lookup_object(self.mmu_unit().extruder_name()).heater.target_temp
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
    def _wrap_suspend_filament_monitoring(self):
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

    # For all encoder methods, 'dwell' means:
    #   True  - gives klipper a little extra time to deliver all encoder pulses when absolute accuracy is required
    #   False - wait for moves to complete and then read encoder
    #   None  - just read encoder without delay (assumes prior movements have completed)
    # Return 'False' if no encoder fitted
    def _encoder_dwell(self, dwell):
        if self.has_encoder():
            if dwell:
                self.movequeues_dwell(self.mmu_unit().p.encoder_dwell)
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
        if self.has_encoder() and self.mmu_unit().p.gate_homing_endstop in [SENSOR_SHARED_EXIT, SENSOR_EXIT_PREFIX]:
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
        gear_stepper = self.gear_rail().steppers[0]
        mcu_pos = gear_stepper.get_mcu_position()
        return mcu_pos * gear_stepper.get_step_dist()

    def _set_filament_position(self, position = 0.):
        pos = self.mmu_toolhead().get_position()
        pos[1] = position
        self.mmu_toolhead().set_position(pos)
        return position

    def _set_filament_remaining(self, length, color=''):
        self.filament_remaining = length
        self.var_manager.set(VARS_MMU_FILAMENT_REMAINING, max(0, round(length, 1)))
        self.var_manager.set(VARS_MMU_FILAMENT_REMAINING_COLOR, color, write=True)

    def _set_last_tool(self, tool):
        self._last_tool = tool
        self.var_manager.set(VARS_MMU_LAST_TOOL, tool, write=True)

    def _set_filament_pos_state(self, state, silent=False):
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

        self._adjust_espooler_assist()

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

    def _set_filament_direction(self, direction):
        self.filament_direction = direction

    def _gate_homing_string(self):
        return "ENCODER" if self.mmu_unit().p.gate_homing_endstop == SENSOR_ENCODER else "%s sensor" % self.mmu_unit().p.gate_homing_endstop

    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.mmu_unit().extruder_name())
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
        self._set_print_state("standby")
        self.log_always("MMU disabled")

    def motors_onoff(self, on=False, motor="all"):
        if motor in ["all", "gear", "gears"]:
            if on:
                self.reset_sync_gear_to_extruder(False)
            else:
                self.mmu_toolhead().unsync()

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
                self.log_warning("PAUL: manages_gate")
                if not force_unload and self.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_UNKNOWN]:
                    raise MmuError("Cannot home %s because has filament loaded" % mmu_unit.name)
                else:
                    try:
                        prev_gate = self.gate_selected
                        self._set_gate_selected(TOOL_GATE_UNKNOWN)
                        mmu_unit.selector.home(force_unload)
                        self.select_gate(prev_gate)
                    except MmuError as ee:
                        self._set_gate_selected(TOOL_GATE_UNKNOWN)
                        raise ee
            else:
                self.log_warning("PAUL: NOT manages_gate")
                # Safe to just home selector
                mmu_unit.selector.home()


    def select_gate(self, gate):
        try:
            if gate == self.gate_selected:
                self.selector().select_gate(gate) # Always give selector a chance to fix position
            else:
                self._next_gate = gate # Valid only during the gate selection process
                _prev_gate = self.gate_selected
                self.selector(gate).select_gate(gate)
                self._set_gate_selected(gate)
                self.led_manager.gate_map_changed(_prev_gate) # PAUL why do we need to call twice? Maybe to turn off prev?  Also could be klipper event (and part of set_gate_selected() logic)
                self.led_manager.gate_map_changed(gate)

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
        self._set_filament_direction(DIRECTION_LOAD)
        self.log_info("Bypass enabled")


    def _set_tool_selected(self, tool):
        self.log_info("PAUL: _set_tool_selected(%d)" % tool)
        if tool != self.tool_selected:
            self.tool_selected = tool
            self.printer.send_event("mmu:tool_selected", self.tool_selected)
            self.var_manager.set(VARS_MMU_TOOL_SELECTED, self.tool_selected, write=True)


    def _set_gate_selected(self, gate):
        self.log_info("PAUL: _set_gate_selected(%d)" % gate)
        self.gate_selected = gate

        new_unit = self._find_unit_by_gate(gate)
        if new_unit != self.unit_selected:
            self.unit_selected = new_unit
            self.log_info("PAUL: sending unit_selected event")
            self.printer.send_event("mmu:unit_selected", self.unit_selected)

        self.printer.send_event("mmu:gate_selected", self.gate_selected)
        self.log_info("PAUL: about to call sync_feedback %s" % self.mmu_unit().sync_feedback.mmu_unit.name)
        self.mmu_unit().sync_feedback.set_default_rd()

        self.var_manager.set(VARS_MMU_GATE_SELECTED, self.gate_selected, write=True)
        self.active_filament = {
            'filament_name': self.gate_filament_name[gate],
            'material': self.gate_material[gate],
            'color': self.gate_color[gate],
            'spool_id': self.gate_spool_id[gate],
            'temperature': self.gate_temperature[gate],
        } if gate >= 0 else {}


    # Return unit number for gate
    def _find_unit_by_gate(self, gate):
        unit = self.mmu_machine.get_mmu_unit_by_gate(gate)
        if unit:
            return unit.unit_index
        self.log_error("PAUL: Gate %d has no unit! Assuming unit %d" % (gate, self.unit_selected))
        return self.unit_selected


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
                    self.log_debug("Deactivating spool...")
                    spool_id = None  # id=0 no longer deactivates
                else:
                    self.log_debug("Activating spool %s..." % spool_id)
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
# Console formatting methods
# -----------------------------------------------------------------------------------------------------------

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
                rgb_hex = self._color_to_rgb_hex(self.gate_color[gate], "FFFFFF")
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


    def _ttg_map_to_string(self, tool=None, show_groups=True):
        """
        Format the TTG map (and optionally EndlessSpool groups) into a human-readable string.

        Args:
            tool: Specify the specific tool to display else all tools will be displayed
            show_groups: Flag to include the endless spool groups if available
        """
        if show_groups:
            msg = "TTG Map & EndlessSpool Groups:\n"
        else:
            msg = "TTG Map:\n" # String used to filter in KS-HH

        num_tools = self.num_gates
        tools = range(num_tools) if tool is None else [tool]

        for i in tools:
            gate = self.ttg_map[i]
            filament_char = self._get_filament_char(gate, show_swatch=True)
            msg += "\n" if i and tool is None else ""
            msg += "T{:<2}-> Gate{:>2}({})".format(i, gate, filament_char)

            if show_groups and self.endless_spool_enabled:
                group = self.endless_spool_groups[gate]
                msg += " Group %s:" % chr(ord('A') + group)
                gates_in_group = [(j + gate) % num_tools for j in range(num_tools)]
                msg += " >".join("{:>2}".format(g) for g in gates_in_group if self.endless_spool_groups[g] == group)

            if i == self.tool_selected:
                msg += " [SELECTED]"
        return msg


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
                    select_strings.append("----")

            unit_str = "{0:-^{width}}".format( " " + str(unit.name) + " ", width=len(gate_indices) * 4 + 1)
            msg_units += unit_str + (divider if not last_gate else "")
            msg_gates += sep
            msg_avail += sep
            msg_tools += "".join(tool_strings) + sep
            msg_selct += ("".join(select_strings) + "-")[:len(gate_indices) * 4 + 1] + (divider if not last_gate else "")
            msg_selct = msg_selct.replace("*", fil_swatch)

        lines = [msg_units] if len(self.mmu_machine.units) > 1 else []
        lines.extend([msg_gates, msg_tools, msg_avail, msg_selct])
        msg = "\n".join(lines)
        if self.selector().is_homed:
            msg += " " + self.selected_tool_string()
        else:
            msg += " NOT HOMED"
        return msg


    def _es_groups_to_string(self, title=None):
        """
        Return a formatted string listing EndlessSpool groups and their member gates.

        Args:
            title: Optionally supply a non-default title
        """
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


    def _gate_map_to_string(self):
        """
        Format per-gate filament details into a readable summary.
        """
        msg = "Gates / Filaments:" # String used to filter in KlipperScreen-HH
        available_status = {
            GATE_AVAILABLE_FROM_BUFFER: "Buffered",
            GATE_AVAILABLE: "On spool",
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
            filament_char = self._get_filament_char(g, show_swatch=True)
            tools = ",".join("T{}".format(t) for t in range(self.num_gates) if self.ttg_map[t] == g)
            tools_fstr = (" [{}]".format(tools) if tools else "")
            gate_fstr = "{}".format(g).ljust(2, UI_SPACE)
            gate_fstr = "{}({}){}:".format(gate_fstr, filament_char, tools_fstr).ljust(14 + len(filament_char), UI_SPACE)

            available_fstr = "{};".format(available).ljust(11, UI_SPACE)
            fil_fstr = "{} | {}{}C | {} | {}".format(material, temperature, UI_DEGREE, color, name)

            spool_option = (str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a")
            if self.p.spoolman_support == SPOOLMAN_OFF:
                spool_fstr = ""
            elif self.gate_spool_id[g] <= 0:
                spool_fstr = "Id: {};".format(spool_option).ljust(12, UI_SPACE)
            else:
                spool_fstr = "Id: {}".format(spool_option).ljust(8, UI_SPACE) + "--> "

            speed_fstr = " [Speed:{}%]".format(self.gate_speed_override[g]) if self.gate_speed_override[g] != 100 else ""
            extra_fstr = " [SELECTED]" if g == self.gate_selected else ""

            msg += "\n{}{}{}{}{}{}".format(gate_fstr, available_fstr, spool_fstr, fil_fstr, speed_fstr, extra_fstr)
        return msg


# -----------------------------------------------------------------------------------------------------------
# RUNOUT, ENDLESS SPOOL, TTG MAPPING and GATE HANDLING
# -----------------------------------------------------------------------------------------------------------

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


    # Use mmu entry (and gear) sensors to "correct" gate status
    # Return updated gate_status adjusted by sensor readings
    def _validate_gate_status(self, gate_status):
        v_gate_status = list(gate_status) # Ensure that webhooks sees get_status() change
        for gate, status in enumerate(v_gate_status):
            gear_detected = self.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, gate)
            if gear_detected is True:
                v_gate_status[gate] = GATE_AVAILABLE
            else:
                pre_detected = self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate)
                if pre_detected is True and status == GATE_EMPTY:
                    v_gate_status[gate] = GATE_UNKNOWN
                elif pre_detected is False and status != GATE_EMPTY:
                    v_gate_status[gate] = GATE_EMPTY
        return v_gate_status


    # Use post-mmu exit sensors to correct the selected gate.
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


# -----------------------------------------------------------------------------------------------------------
# INTERNAL PRINT WORKFLOW WRAPPER COMMANDS
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
            self.mmu._fix_started_state() # Get out of 'started' state before transistion to cancelled
            self.mmu.log_debug("MMU CANCEL_PRINT wrapper called")
            self.mmu._clear_mmu_error_dialog()
            self.mmu._save_toolhead_position_and_park("cancel")
            self.mmu.wrap_gcode_command("__CANCEL_PRINT", exception=None)
            self.mmu._on_print_end("cancelled")
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
                self.mmu._set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
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
            self.mmu._fix_started_state() # Get out of 'started' state
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
