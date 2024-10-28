# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Goal: Main control class for any Klipper based MMU (includes filament driver/gear control)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import ast, random, logging, time, contextlib, math, os.path, re, unicodedata

# Klipper imports
import chelper
from extras.homing import Homing, HomingMove

# Happy Hare imports
from extras              import mmu_machine
from extras.mmu_machine  import MmuToolHead
from extras.mmu_leds     import MmuLeds
from extras.mmu_sensors  import MmuRunoutHelper

# MMU subcomponent clases
from .mmu_shared         import *
from .mmu_logger         import MmuLogger
from .mmu_selector       import VirtualSelector, LinearSelector
from .mmu_test           import MmuTest
from .mmu_utils          import DebugStepperMovement, PurgeVolCalculator
from .mmu_sensor_manager import MmuSensorManager


# Main klipper module
class Mmu:
    VERSION = 3.00 # When this is revved, Happy Hare will instruct users to re-run ./install.sh. Sync with install.sh!

    BOOT_DELAY = 2.5 # Delay before running bootup tasks

    # Calibration steps
    CALIBRATED_GEAR_0    = 0b00001 # Specifically rotation_distance for gate 0
    CALIBRATED_ENCODER   = 0b00010
    CALIBRATED_SELECTOR  = 0b00100 # Defaults true with VirtualSelector
    CALIBRATED_BOWDENS   = 0b01000 # Bowden length for all gates
    CALIBRATED_GEAR_RDS  = 0b10000
    CALIBRATED_ESSENTIAL = 0b01111
    CALIBRATED_ALL       = 0b11111

    TOOL_GATE_UNKNOWN = -1
    TOOL_GATE_BYPASS = -2

    GATE_UNKNOWN = -1
    GATE_EMPTY = 0
    GATE_AVAILABLE = 1 # Available to load from either buffer or spool
    GATE_AVAILABLE_FROM_BUFFER = 2

    FILAMENT_POS_UNKNOWN = -1
    FILAMENT_POS_UNLOADED = 0       # Parked in gate
    FILAMENT_POS_HOMED_GATE = 1     # Homed at either gate or post-gate sensor (currently assumed mutually exclusive sensors)
    FILAMENT_POS_START_BOWDEN = 2   # Point of fast load portion
    FILAMENT_POS_IN_BOWDEN = 3      # Some unknown position in the bowden
    FILAMENT_POS_END_BOWDEN = 4     # End of fast load portion
    FILAMENT_POS_HOMED_ENTRY = 5    # Homed at entry sensor
    FILAMENT_POS_HOMED_EXTRUDER = 6 # Collision homing case at extruder gear entry
    FILAMENT_POS_EXTRUDER_ENTRY = 7 # Past extruder gear entry
    FILAMENT_POS_HOMED_TS = 8       # Homed at toolhead sensor
    FILAMENT_POS_IN_EXTRUDER = 9    # In extruder past toolhead sensor
    FILAMENT_POS_LOADED = 10        # Homed to nozzle

    DIRECTION_LOAD = 1
    DIRECTION_UNKNOWN = 0
    DIRECTION_UNLOAD = -1

    FORM_TIP_NONE = 0               # Skip tip forming
    FORM_TIP_SLICER = 1             # Slicer forms tips
    FORM_TIP_STANDALONE = 2         # Happy Hare forms tips (default)

    ACTION_IDLE = 0
    ACTION_LOADING = 1
    ACTION_LOADING_EXTRUDER = 2
    ACTION_UNLOADING = 3
    ACTION_UNLOADING_EXTRUDER = 4
    ACTION_FORMING_TIP = 5
    ACTION_HEATING = 6
    ACTION_CHECKING = 7
    ACTION_HOMING = 8
    ACTION_SELECTING = 9

    MACRO_EVENT_RESTART          = "restart"          # Params: None
    MACRO_EVENT_GATE_MAP_CHANGED = "gate_map_changed" # Params: GATE changed or GATE=-1 for all
    MACRO_EVENT_FILAMENT_GRIPPED = "filament_gripped" # Params: None

    # Standard sensor and endstop or pseudo endstop names
    ENDSTOP_ENCODER             = "encoder"        # Fake Gate endstop
    ENDSTOP_GATE                = "mmu_gate"       # Gate
    ENDSTOP_POST_GATE_PREFIX    = "mmu_post_gate"

    ENDSTOP_EXTRUDER_NONE       = "none"           # Fake Extruder endstop aka don't attempt home
    ENDSTOP_EXTRUDER_COLLISION  = "collision"      # Fake Extruder endstop
    ENDSTOP_EXTRUDER_ENTRY      = "extruder"       # Extruder entry sensor
    ENDSTOP_GEAR_TOUCH          = "mmu_gear_touch" # Extruder

    ENDSTOP_TOOLHEAD            = "toolhead"
    ENDSTOP_EXTRUDER_TOUCH      = "mmu_ext_touch"

    ENDSTOP_SELECTOR_TOUCH      = "mmu_sel_touch"  # For LinearSelector
    ENDSTOP_SELECTOR_HOME       = "mmu_sel_home"   # For LinearSelector
    PRE_GATE_SENSOR_PREFIX      = "mmu_pre_gate"

    EXTRUDER_ENDSTOPS = [ENDSTOP_EXTRUDER_COLLISION, ENDSTOP_GEAR_TOUCH, ENDSTOP_EXTRUDER_ENTRY, ENDSTOP_EXTRUDER_NONE]
    GATE_ENDSTOPS     = [ENDSTOP_GATE, ENDSTOP_ENCODER, ENDSTOP_POST_GATE_PREFIX]

    # Statistics output types
    GATE_STATS_STRING           = "string"
    GATE_STATS_PERCENTAGE       = "percentage"
    GATE_STATS_EMOTICON         = "emoticon"

    GATE_STATS_TYPES = [GATE_STATS_STRING, GATE_STATS_PERCENTAGE, GATE_STATS_EMOTICON]

    # Gear/Extruder syncing
    SWITCH_SYNC_FEEDBACK_TENSION     = "sync_feedback_tension"
    SWITCH_SYNC_FEEDBACK_COMPRESSION = "sync_feedback_compression"
    SYNC_FEEDBACK_INTERVAL  = 0.5   # How often to check extruder direction
    SYNC_POSITION_TIMERANGE = 0.6   # Interval to compare movement
    SYNC_POSITION_MIN_DELTA = 0.001 # Min extruder move distance to be significant
    SYNC_STATE_NEUTRAL = 0
    SYNC_STATE_COMPRESSED = 1.
    SYNC_STATE_EXPANDED = -1.

    # Levels of logging
    LOG_ESSENTIAL = 0
    LOG_INFO      = 1
    LOG_DEBUG     = 2
    LOG_TRACE     = 3
    LOG_STEPPER   = 4
    LOG_LEVELS = ['ESSENTAL', 'INFO', 'DEBUG', 'TRACE', 'STEPPER']

    # Name used to save gcode state
    TOOLHEAD_POSITION_STATE = 'MMU_state'

    # Filament "grip" states
    FILAMENT_UNKNOWN_STATE = -1
    FILAMENT_RELEASE_STATE = 0
    FILAMENT_DRIVE_STATE   = 1
    FILAMENT_HOLD_STATE    = 2

    # mmu_vars.cfg variables
    VARS_MMU_REVISION                = "mmu__revision"
    VARS_MMU_CALIB_CLOG_LENGTH       = "mmu_calibration_clog_length"
    VARS_MMU_ENABLE_ENDLESS_SPOOL    = "mmu_state_enable_endless_spool"
    VARS_MMU_ENDLESS_SPOOL_GROUPS    = "mmu_state_endless_spool_groups"
    VARS_MMU_TOOL_TO_GATE_MAP        = "mmu_state_tool_to_gate_map"
    VARS_MMU_GATE_STATUS             = "mmu_state_gate_status"
    VARS_MMU_GATE_MATERIAL           = "mmu_state_gate_material"
    VARS_MMU_GATE_COLOR              = "mmu_state_gate_color"
    VARS_MMU_GATE_FILAMENT_NAME      = "mmu_state_gate_filament_name"
    VARS_MMU_GATE_TEMPERATURE        = "mmu_state_gate_temperature"
    VARS_MMU_GATE_SPOOL_ID           = "mmu_state_gate_spool_id"
    VARS_MMU_GATE_SPEED_OVERRIDE     = "mmu_state_gate_speed_override"
    VARS_MMU_GATE_SELECTED           = "mmu_state_gate_selected"
    VARS_MMU_TOOL_SELECTED           = "mmu_state_tool_selected"
    VARS_MMU_LAST_TOOL               = "mmu_state_last_tool"
    VARS_MMU_FILAMENT_POS            = "mmu_state_filament_pos"
    VARS_MMU_FILAMENT_REMAINING      = "mmu_state_filament_remaining"
    VARS_MMU_CALIB_BOWDEN_LENGTHS    = "mmu_calibration_bowden_lengths" # Per-gate calibrated bowden lengths
    VARS_MMU_CALIB_BOWDEN_HOME       = "mmu_calibration_bowden_home"    # Was encoder, gate or post-gate sensor used as reference point
    VARS_MMU_GATE_STATISTICS_PREFIX  = "mmu_statistics_gate_"
    VARS_MMU_SWAP_STATISTICS         = "mmu_statistics_swaps"
    VARS_MMU_COUNTERS                = "mmu_statistics_counters"
    VARS_MMU_ENCODER_RESOLUTION      = "mmu_encoder_resolution"
    VARS_MMU_GEAR_ROTATION_DISTANCES = "mmu_gear_rotation_distances"

    VARS_MMU_CALIB_BOWDEN_LENGTH     = "mmu_calibration_bowden_length" # Deprecated (for upgrade only)
    VARS_MMU_GEAR_ROTATION_DISTANCE  = "mmu_gear_rotation_distance"    # Deprecated (for upgrade only)
    VARS_MMU_CALIB_PREFIX            = "mmu_calibration_"              # Deprecated (for upgrade only)

    # Mainsail/Fluid visualization of extruder colors and other attributes
    T_MACRO_COLOR_ALLGATES = 'allgates' # Color from gate map (all tools). Will add spool_id if spoolman is enabled
    T_MACRO_COLOR_GATEMAP  = 'gatemap'  # As per gatemap but hide empty tools. Will add spool_id if spoolman is enabled
    T_MACRO_COLOR_SLICER   = 'slicer'   # Color from slicer tool map
    T_MACRO_COLOR_OPTIONS  = [T_MACRO_COLOR_GATEMAP, T_MACRO_COLOR_SLICER, T_MACRO_COLOR_ALLGATES]

    # Spoolman integration - modes of operation
    SPOOLMAN_OFF           = 'off'      # Spoolman disabled
    SPOOLMAN_READONLY      = 'readonly' # Get filament attributes only
    SPOOLMAN_PUSH          = 'push'     # Local gatemap is the source or truth
    SPOOLMAN_PULL          = 'pull'     # Spoolman db is the source of truth
    SPOOLMAN_OPTIONS       = [SPOOLMAN_OFF, SPOOLMAN_READONLY, SPOOLMAN_PUSH, SPOOLMAN_PULL]
    SPOOLMAN_CONFIG_ERROR  = "Moonraker/spoolman may not be configured (check moonraker.log)"

    # Automap strategies
    AUTOMAP_NONE           = 'none'
    AUTOMAP_FILAMENT_NAME  = 'filament_name'
    AUTOMAP_SPOOL_ID       = 'spool_id'
    AUTOMAP_MATERIAL       = 'material'
    AUTOMAP_CLOSEST_COLOR  = 'closest_color'
    AUTOMAP_COLOR          = 'color'
    AUTOMAP_OPTIONS        = [AUTOMAP_NONE, AUTOMAP_FILAMENT_NAME, AUTOMAP_SPOOL_ID, AUTOMAP_MATERIAL, AUTOMAP_CLOSEST_COLOR, AUTOMAP_COLOR]

    EMPTY_GATE_STATS_ENTRY = {'pauses': 0, 'loads': 0, 'load_distance': 0.0, 'load_delta': 0.0, 'unloads': 0, 'unload_distance': 0.0, 'unload_delta': 0.0, 'load_failures': 0, 'unload_failures': 0, 'quality': -1.}

    W3C_COLORS = [('aliceblue','#F0F8FF'), ('antiquewhite','#FAEBD7'), ('aqua','#00FFFF'), ('aquamarine','#7FFFD4'), ('azure','#F0FFFF'), ('beige','#F5F5DC'),
                  ('bisque','#FFE4C4'), ('black','#000000'), ('blanchedalmond','#FFEBCD'), ('blue','#0000FF'), ('blueviolet','#8A2BE2'), ('brown','#A52A2A'),
                  ('burlywood','#DEB887'), ('cadetblue','#5F9EA0'), ('chartreuse','#7FFF00'), ('chocolate','#D2691E'), ('coral','#FF7F50'),
                  ('cornflowerblue','#6495ED'), ('cornsilk','#FFF8DC'), ('crimson','#DC143C'), ('cyan','#00FFFF'), ('darkblue','#00008B'), ('darkcyan','#008B8B'),
                  ('darkgoldenrod','#B8860B'), ('darkgray','#A9A9A9'), ('darkgreen','#006400'), ('darkgrey','#A9A9A9'), ('darkkhaki','#BDB76B'),
                  ('darkmagenta','#8B008B'), ('darkolivegreen','#556B2F'), ('darkorange','#FF8C00'), ('darkorchid','#9932CC'), ('darkred','#8B0000'),
                  ('darksalmon','#E9967A'), ('darkseagreen','#8FBC8F'), ('darkslateblue','#483D8B'), ('darkslategray','#2F4F4F'), ('darkslategrey','#2F4F4F'),
                  ('darkturquoise','#00CED1'), ('darkviolet','#9400D3'), ('deeppink','#FF1493'), ('deepskyblue','#00BFFF'), ('dimgray','#696969'),
                  ('dimgrey','#696969'), ('dodgerblue','#1E90FF'), ('firebrick','#B22222'), ('floralwhite','#FFFAF0'), ('forestgreen','#228B22'),
                  ('fuchsia','#FF00FF'), ('gainsboro','#DCDCDC'), ('ghostwhite','#F8F8FF'), ('gold','#FFD700'), ('goldenrod','#DAA520'), ('gray','#808080'),
                  ('green','#008000'), ('greenyellow','#ADFF2F'), ('grey','#808080'), ('honeydew','#F0FFF0'), ('hotpink','#FF69B4'), ('indianred','#CD5C5C'),
                  ('indigo','#4B0082'), ('ivory','#FFFFF0'), ('khaki','#F0E68C'), ('lavender','#E6E6FA'), ('lavenderblush','#FFF0F5'), ('lawngreen','#7CFC00'),
                  ('lemonchiffon','#FFFACD'), ('lightblue','#ADD8E6'), ('lightcoral','#F08080'), ('lightcyan','#E0FFFF'), ('lightgoldenrodyellow','#FAFAD2'),
                  ('lightgray','#D3D3D3'), ('lightgreen','#90EE90'), ('lightgrey','#D3D3D3'), ('lightpink','#FFB6C1'), ('lightsalmon','#FFA07A'),
                  ('lightseagreen','#20B2AA'), ('lightskyblue','#87CEFA'), ('lightslategray','#778899'), ('lightslategrey','#778899'),
                  ('lightsteelblue','#B0C4DE'), ('lightyellow','#FFFFE0'), ('lime','#00FF00'), ('limegreen','#32CD32'), ('linen','#FAF0E6'),
                  ('magenta','#FF00FF'), ('maroon','#800000'), ('mediumaquamarine','#66CDAA'), ('mediumblue','#0000CD'), ('mediumorchid','#BA55D3'),
                  ('mediumpurple','#9370DB'), ('mediumseagreen','#3CB371'), ('mediumslateblue','#7B68EE'), ('mediumspringgreen','#00FA9A'),
                  ('mediumturquoise','#48D1CC'), ('mediumvioletred','#C71585'), ('midnightblue','#191970'), ('mintcream','#F5FFFA'), ('mistyrose','#FFE4E1'),
                  ('moccasin','#FFE4B5'), ('navajowhite','#FFDEAD'), ('navy','#000080'), ('oldlace','#FDF5E6'), ('olive','#808000'),
                  ('olivedrab','#6B8E23'), ('orange','#FFA500'), ('orangered','#FF4500'), ('orchid','#DA70D6'), ('palegoldenrod','#EEE8AA'),
                  ('palegreen','#98FB98'), ('paleturquoise','#AFEEEE'), ('palevioletred','#DB7093'), ('papayawhip','#FFEFD5'), ('peachpuff','#FFDAB9'),
                  ('peru','#CD853F'), ('pink','#FFC0CB'), ('plum','#DDA0DD'), ('powderblue','#B0E0E6'), ('purple','#800080'), ('red','#FF0000'),
                  ('rosybrown','#BC8F8F'), ('royalblue','#4169E1'), ('saddlebrown','#8B4513'), ('salmon','#FA8072'), ('sandybrown','#F4A460'),
                  ('seagreen','#2E8B57'), ('seashell','#FFF5EE'), ('sienna','#A0522D'), ('silver','#C0C0C0'), ('skyblue','#87CEEB'), ('slateblue','#6A5ACD'),
                  ('slategray','#708090'), ('slategrey','#708090'), ('snow','#FFFAFA'), ('springgreen','#00FF7F'), ('steelblue','#4682B4'),
                  ('tan','#D2B48C'), ('teal','#008080'), ('thistle','#D8BFD8'), ('tomato','#FF6347'), ('turquoise','#40E0D0'), ('violet','#EE82EE'),
                  ('wheat','#F5DEB3'), ('white','#FFFFFF'), ('whitesmoke','#F5F5F5'), ('yellow','#FFFF00'), ('yellowgreen','#9ACD32')]

    UPGRADE_REMINDER = "Sorry but Happy Hare requires you to re-run this to complete the update:\ncd ~/Happy-Hare\n./install.sh\nMore details: https://github.com/moggieuk/Happy-Hare/wiki/Upgrade-Notice"

    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.calibration_status = 0b0
        self.encoder_force_validation = False
        self.sync_feedback_last_state = 0. # 0 = Neutral
        self.sync_feedback_last_direction = 0 # 0 = Extruder not moving
        self.sync_feedback_operational = False
        self.w3c_colors = dict(self.W3C_COLORS)
        self.filament_remaining = 0.
        self._last_tool = self.TOOL_GATE_UNKNOWN
        self._toolhead_max_accel = self.config.getsection('printer').getsection('toolhead').getint('max_accel', 5000)
        self.internal_test = False # True while running QA tests
        self.toolchange_retract = 0. # Set from mmu_macro_vars
        self._can_write_variables = True
        self.toolchange_purge_volume = 0.
        self.mmu_logger = None # Setup on connect

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        # Instruct users to re-run ./install.sh if version number changes
        self.config_version = config.getfloat('happy_hare_version', 2.2) # v2.2 was the last release before versioning
        if self.config_version is not None and self.config_version < self.VERSION:
            raise self.config.error("Looks like you upgraded (v%s -> v%s)?\n%s" % (self.config_version, self.VERSION, self.UPGRADE_REMINDER))

        # Setup remaining hardware like MMU toolhead --------------------------------------------------------
        #
        # By default HH uses its modified homing extruder. Because this might have unknown consequences on certain
        # set-ups it can be disabled. If disabled, homing moves will still work, but the delay in mcu to mcu comms
        # can lead to several mm of error depending on speed. Also homing of just the extruder is not possible.
        self.homing_extruder = bool(config.getint('homing_extruder', 1, minval=0, maxval=1))

        # We setup MMU hardware during configuration since some hardware like endstop requires
        # configuration during the MCU config phase, which happens before klipper connection
        # This assumes that the hardware definition appears before the '[mmu]' section.
        # The default recommended install will guarantee this order
        self._setup_mmu_hardware(config)

        # Printer interaction config
        self.extruder_name = config.get('extruder', 'extruder')
        self.timeout_pause = config.getint('timeout_pause', 72000, minval=120)
        self.default_idle_timeout = config.getint('default_idle_timeout', -1, minval=120)
        self.pending_spool_id_timeout = config.getint('pending_spool_id_timeout', default=20, minval=-1) # Not currently exposed
        self.disable_heater = config.getint('disable_heater', 600, minval=60)
        self.default_extruder_temp = config.getfloat('default_extruder_temp', 200.)
        self.extruder_temp_variance = config.getfloat('extruder_temp_variance', 2., minval=1.)
        self.gcode_load_sequence = config.getint('gcode_load_sequence', 0)
        self.gcode_unload_sequence = config.getint('gcode_unload_sequence', 0)
        self.slicer_tip_park_pos = config.getfloat('slicer_tip_park_pos', 0., minval=0.)
        self.force_form_tip_standalone = config.getint('force_form_tip_standalone', 0, minval=0, maxval=1)
        self.autotune_rotation_distance = config.getint('autotune_rotation_distance', 0, minval=0, maxval=1) # autotune_rotation_distance
        self.autotune_bowden_length = config.getint('autotune_bowden_length', 0, minval=0, maxval=1) # autotune_bowden_length
        self.strict_filament_recovery = config.getint('strict_filament_recovery', 0, minval=0, maxval=1)
        self.filament_recovery_on_pause = config.getint('filament_recovery_on_pause', 1, minval=0, maxval=1)
        self.retry_tool_change_on_error = config.getint('retry_tool_change_on_error', 0, minval=0, maxval=1)
        self.print_start_detection = config.getint('print_start_detection', 1, minval=0, maxval=1)
        self.startup_home_if_unloaded = config.getint('startup_home_if_unloaded', 0, minval=0, maxval=1)
        self.startup_reset_ttg_map = config.getint('startup_reset_ttg_map', 0, minval=0, maxval=1)
        self.show_error_dialog = config.getint('show_error_dialog', 1, minval=0, maxval=1)

        # Internal macro overrides
        self.pause_macro = config.get('pause_macro', 'PAUSE')
        self.action_changed_macro = config.get('action_changed_macro', '_MMU_ACTION_CHANGED')
        self.print_state_changed_macro = config.get('print_state_changed_macro', '_MMU_PRINT_STATE_CHANGED')
        self.mmu_event_macro = config.get('mmu_event_macro', '_MMU_EVENT')
        self.form_tip_macro = config.get('form_tip_macro', '_MMU_FORM_TIP')
        self.pre_unload_macro = config.get('pre_unload_macro', '_MMU_PRE_UNLOAD')
        self.post_form_tip_macro = config.get('post_form_tip_macro', '_MMU_POST_FORM_TIP')
        self.post_unload_macro = config.get('post_unload_macro', '_MMU_POST_UNLOAD')
        self.pre_load_macro = config.get('pre_load_macro', '_MMU_PRE_LOAD')
        self.post_load_macro = config.get('post_load_macro', '_MMU_POST_LOAD_MACRO')
        self.unload_sequence_macro = config.get('unload_sequence_macro', '_MMU_UNLOAD_SEQUENCE')
        self.load_sequence_macro = config.get('load_sequence_macro', '_MMU_LOAD_SEQUENCE')
        self.error_dialog_macro = config.get('error_dialog_macro', '_MMU_ERROR_DIALOG') # Not exposed
        self.clear_position_macro = config.get('clear_position_macro', '_MMU_CLEAR_POSITION') # Not exposed
        self.save_position_macro = config.get('save_position_macro', '_MMU_SAVE_POSITION') # Not exposed
        self.restore_position_macro = config.get('restore_position_macro', '_MMU_RESTORE_POSITION') # Not exposed
        self.park_macro = config.get('park_macro', '_MMU_PARK') # Not exposed
        self.error_macro = config.get('error_macro', '_MMU_ERROR') # Not exposed

        # User MMU setup
        self.default_ttg_map = list(config.getintlist('tool_to_gate_map', []))
        self.default_gate_status = list(config.getintlist('gate_status', []))
        self.default_gate_filament_name = list(config.getlist('gate_filament_name', []))
        self.default_gate_material = list(config.getlist('gate_material', []))
        self.default_gate_color = list(config.getlist('gate_color', []))
        self.default_gate_temperature = list(config.getintlist('gate_temperature', []))
        self.default_gate_spool_id = list(config.getintlist('gate_spool_id', []))
        self.default_gate_speed_override = list(config.getintlist('gate_speed_override', []))

        # Configuration for gate loading and unloading
        self.gate_homing_endstop = config.getchoice('gate_homing_endstop', {o: o for o in self.GATE_ENDSTOPS}, self.ENDSTOP_ENCODER)
        self.gate_endstop_to_encoder = config.getfloat('gate_endstop_to_encoder', 0., minval=0.)
        self.gate_unload_buffer = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting
        self.gate_homing_max = config.getfloat('gate_homing_max', 2 * self.gate_unload_buffer, minval=self.gate_unload_buffer)
        self.gate_parking_distance = config.getfloat('gate_parking_distance', 23.) # Can be +ve or -ve
        self.gate_load_retries = config.getint('gate_load_retries', 2, minval=1, maxval=5)
        self.gate_autoload = config.getint('gate_autoload', 1, minval=0, maxval=1)
        self.bypass_autoload = config.getint('bypass_autoload', 1, minval=0, maxval=1)
        self.encoder_dwell = config.getfloat('encoder_dwell', 0.1, minval=0., maxval=2.) # Not exposed
        self.encoder_move_step_size = config.getfloat('encoder_move_step_size', 15., minval=5., maxval=25.) # Not exposed

        # Configuration for (fast) bowden move
        self.bowden_apply_correction = config.getint('bowden_apply_correction', 0, minval=0, maxval=1)
        self.bowden_allowable_load_delta = config.getfloat('bowden_allowable_load_delta', 10., minval=1.)
        self.bowden_allowable_unload_delta = config.getfloat('bowden_allowable_unload_delta', self.bowden_allowable_load_delta, minval=1.)
        self.bowden_move_error_tolerance = config.getfloat('bowden_move_error_tolerance', 60, minval=0, maxval=100) # Percentage of delta of move that results in error
        self.bowden_pre_unload_test = config.getint('bowden_pre_unload_test', 0, minval=0, maxval=1) # Check for bowden movement before full pull
        self.bowden_pre_unload_error_tolerance = config.getfloat('bowden_pre_unload_error_tolerance', 100, minval=0, maxval=100) # Allowable delta movement % before error

        # Configuration for extruder and toolhead homing
        self.extruder_force_homing = config.getint('extruder_force_homing', 0, minval=0, maxval=1)
        self.extruder_homing_endstop = config.getchoice('extruder_homing_endstop', {o: o for o in self.EXTRUDER_ENDSTOPS}, self.ENDSTOP_EXTRUDER_NONE)
        self.extruder_homing_max = config.getfloat('extruder_homing_max', 50., above=10.) # Extruder homing max
        self.extruder_collision_homing_step = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max = config.getfloat('toolhead_homing_max', 20., minval=0.) # Toolhead sensor homing max
        self.toolhead_extruder_to_nozzle = config.getfloat('toolhead_extruder_to_nozzle', 0., minval=5.) # For "sensorless"
        self.toolhead_sensor_to_nozzle = config.getfloat('toolhead_sensor_to_nozzle', 0., minval=1.) # For toolhead sensor
        self.toolhead_entry_to_extruder = config.getfloat('toolhead_entry_to_extruder', 0., minval=0.) # For extruder (entry) sensor
        self.toolhead_residual_filament = config.getfloat('toolhead_residual_filament', 0., minval=0., maxval=50.) # +ve value = reduction of load length
        self.toolhead_ooze_reduction = config.getfloat('toolhead_ooze_reduction', 0., minval=-5., maxval=20.) # +ve value = reduction of load length
        self.toolhead_unload_safety_margin = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.) # Extra unload distance
        self.toolhead_move_error_tolerance = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100) # Allowable delta movement % before error
        self.toolhead_post_load_tighten = config.getint('toolhead_post_load_tighten', 60, minval=0, maxval=100) # Whether to apply filament tightening move after load (if not synced)

        # Extra Gear/Extruder synchronization controls
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip = config.getint('sync_form_tip', 0, minval=0, maxval=1)
        self.sync_multiplier_high = config.getfloat('sync_multiplier_high', 1.05, minval=1., maxval=2.)
        self.sync_multiplier_low = config.getfloat('sync_multipler_low', 0.95, minval=0.5, maxval=1.)
        self.sync_feedback_enable = config.getint('sync_feedback_enable', 0, minval=0, maxval=1)

        # TMC current control
        self.extruder_collision_homing_current = config.getint('extruder_collision_homing_current', 50, minval=10, maxval=100)
        self.extruder_form_tip_current = config.getint('extruder_form_tip_current', 100, minval=100, maxval=150)
        self.sync_gear_current = config.getint('sync_gear_current', 50, minval=10, maxval=100)

        # Filament move speeds and accelaration
        self.gear_from_buffer_speed = config.getfloat('gear_from_buffer_speed', 150., minval=10.)
        self.gear_from_buffer_accel = config.getfloat('gear_from_buffer_accel', 400, minval=10.)
        self.gear_from_spool_speed = config.getfloat('gear_from_spool_speed', 60, minval=10.)
        self.gear_from_spool_accel = config.getfloat('gear_from_spool_accel', 100, minval=10.)
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

        # Optional features
        self.preload_attempts = config.getint('preload_attempts', 5, minval=1, maxval=20) # How many times to try to grab the filament
        self.encoder_move_validation = config.getint('encoder_move_validation', 1, minval=0, maxval=1) # Use encoder to check load/unload movement
        self.enable_clog_detection = config.getint('enable_clog_detection', 2, minval=0, maxval=2)
        self.spoolman_support = config.getchoice('spoolman_support', {o: o for o in self.SPOOLMAN_OPTIONS}, self.SPOOLMAN_OFF)
        self.t_macro_color = config.getchoice('t_macro_color', {o: o for o in self.T_MACRO_COLOR_OPTIONS}, self.T_MACRO_COLOR_SLICER)
        self.default_enable_endless_spool = config.getint('enable_endless_spool', 0, minval=0, maxval=1)
        self.endless_spool_final_eject = config.getfloat('endless_spool_final_eject', 50, minval=0.)
        self.endless_spool_on_load = config.getint('endless_spool_on_load', 0, minval=0, maxval=1)
        self.endless_spool_eject_gate = config.getint('endless_spool_eject_gate', -1, minval=-1, maxval=self.num_gates - 1)
        self.default_endless_spool_groups = list(config.getintlist('endless_spool_groups', []))
        self.tool_extrusion_multipliers = []
        self.tool_speed_multipliers = []

        # Logging
        self.log_level = config.getint('log_level', 1, minval=0, maxval=4)
        self.log_file_level = config.getint('log_file_level', 2, minval=-1, maxval=4)
        self.log_statistics = config.getint('log_statistics', 0, minval=0, maxval=1)
        self.log_visual = config.getint('log_visual', 1, minval=0, maxval=1)
        self.log_startup_status = config.getint('log_startup_status', 1, minval=0, maxval=2)

        # Cosmetic console stuff
        self.console_stat_columns = list(config.getlist('console_stat_columns', ['unload', 'load', 'total']))
        self.console_stat_rows = list(config.getlist('console_stat_rows', ['total', 'job', 'job_average']))
        self.console_gate_stat = config.getchoice('console_gate_stat', {o: o for o in self.GATE_STATS_TYPES}, self.GATE_STATS_STRING)
        self.console_always_output_full = config.getint('console_always_output_full', 1, minval=0, maxval=1)

        # Turn off splash bling for boring people
        self.serious = config.getint('serious', 0, minval=0, maxval=1)

        # Currently hidden and testing options
        self.test_random_failures = config.getint('test_random_failures', 0, minval=0, maxval=1)
        self.test_disable_encoder = config.getint('test_disable_encoder', 0, minval=0, maxval=1)
        self.test_force_in_print = config.getint('test_force_in_print', 0, minval=0, maxval=1)

        # Klipper tuning (aka hacks)
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

        # Establish defaults for "reset" operation ----------------------------------------------------------
        # These lists are the defaults (used when reset) and will be overriden by values in mmu_vars.cfg...

        # Endless spool groups
        self.enable_endless_spool = self.default_enable_endless_spool
        if len(self.default_endless_spool_groups) > 0:
            if self.enable_endless_spool == 1 and len(self.default_endless_spool_groups) != self.num_gates:
                raise self.config.error("endless_spool_groups has a different number of values than the number of gates")
        else:
            self.default_endless_spool_groups = list(range(self.num_gates))
        self.endless_spool_groups = list(self.default_endless_spool_groups)

        # Components of the gate map (status, material, color, spool_id, filament name, temperature, and speed override)
        self.gate_map_vars = [ (self.VARS_MMU_GATE_STATUS, 'gate_status', self.GATE_UNKNOWN),
                               (self.VARS_MMU_GATE_FILAMENT_NAME, 'gate_filament_name', ""),
                               (self.VARS_MMU_GATE_MATERIAL, 'gate_material', ""),
                               (self.VARS_MMU_GATE_COLOR, 'gate_color', ""),
                               (self.VARS_MMU_GATE_TEMPERATURE, 'gate_temperature', int(self.default_extruder_temp)),
                               (self.VARS_MMU_GATE_SPOOL_ID, 'gate_spool_id', -1),
                               (self.VARS_MMU_GATE_SPEED_OVERRIDE, 'gate_speed_override', 100) ]

        for _, attr, default in self.gate_map_vars:
            default_attr_name = "default_" + attr
            default_attr = getattr(self, default_attr_name)
            if len(default_attr) > 0:
                if len(default_attr) != self.num_gates:
                    raise self.config.error("%s has different number of entries than the number of gates" % attr)
            else:
                default_attr.extend([default] * self.num_gates)
            setattr(self, attr, list(default_attr))
            if attr == 'gate_color':
                self._update_gate_color(getattr(self, attr))

        # Tool to gate mapping
        if len(self.default_ttg_map) > 0:
            if not len(self.default_ttg_map) == self.num_gates:
                raise self.config.error("tool_to_gate_map has different number of values than the number of gates")
        else:
            self.default_ttg_map = list(range(self.num_gates))
        self.ttg_map = list(self.default_ttg_map)

        # Tool speed and extrusion multipliers
        self.tool_extrusion_multipliers.extend([1.] * self.num_gates)
        self.tool_speed_multipliers.extend([1.] * self.num_gates)

        # Register GCODE commands ---------------------------------------------------------------------------
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

        # Logging and Stats
        self.gcode.register_command('MMU_RESET', self.cmd_MMU_RESET, desc = self.cmd_MMU_RESET_help)
        self.gcode.register_command('MMU_STATS', self.cmd_MMU_STATS, desc = self.cmd_MMU_STATS_help)
        self.gcode.register_command('MMU_STATUS', self.cmd_MMU_STATUS, desc = self.cmd_MMU_STATUS_help)
        self.gcode.register_command('MMU_SENSORS', self.cmd_MMU_SENSORS, desc = self.cmd_MMU_SENSORS_help)

        # Calibration
        self.gcode.register_command('MMU_CALIBRATE_GEAR', self.cmd_MMU_CALIBRATE_GEAR, desc=self.cmd_MMU_CALIBRATE_GEAR_help)
        self.gcode.register_command('MMU_CALIBRATE_ENCODER', self.cmd_MMU_CALIBRATE_ENCODER, desc=self.cmd_MMU_CALIBRATE_ENCODER_help)
        self.gcode.register_command('MMU_CALIBRATE_BOWDEN', self.cmd_MMU_CALIBRATE_BOWDEN, desc = self.cmd_MMU_CALIBRATE_BOWDEN_help)
        self.gcode.register_command('MMU_CALIBRATE_GATES', self.cmd_MMU_CALIBRATE_GATES, desc = self.cmd_MMU_CALIBRATE_GATES_help)
        self.gcode.register_command('MMU_CALIBRATE_TOOLHEAD', self.cmd_MMU_CALIBRATE_TOOLHEAD, desc = self.cmd_MMU_CALIBRATE_TOOLHEAD_help)

        # Motor control
        self.gcode.register_command('MMU_MOTORS_OFF', self.cmd_MMU_MOTORS_OFF, desc = self.cmd_MMU_MOTORS_OFF_help)
        self.gcode.register_command('MMU_SYNC_GEAR_MOTOR', self.cmd_MMU_SYNC_GEAR_MOTOR, desc=self.cmd_MMU_SYNC_GEAR_MOTOR_help)

        # Core MMU functionality
        self.gcode.register_command('MMU', self.cmd_MMU, desc = self.cmd_MMU_help)
        self.gcode.register_command('MMU_LOG', self.cmd_MMU_LOG, desc = self.cmd_MMU_LOG_help)
        self.gcode.register_command('MMU_HELP', self.cmd_MMU_HELP, desc = self.cmd_MMU_HELP_help)
        self.gcode.register_command('MMU_ENCODER', self.cmd_MMU_ENCODER, desc = self.cmd_MMU_ENCODER_help)
        self.gcode.register_command('MMU_LED', self.cmd_MMU_LED, desc = self.cmd_MMU_LED_help)
        self.gcode.register_command('MMU_HOME', self.cmd_MMU_HOME, desc = self.cmd_MMU_HOME_help)
        self.gcode.register_command('MMU_SELECT', self.cmd_MMU_SELECT, desc = self.cmd_MMU_SELECT_help)
        self.gcode.register_command('MMU_PRELOAD', self.cmd_MMU_PRELOAD, desc = self.cmd_MMU_PRELOAD_help)
        self.gcode.register_command('MMU_SELECT_BYPASS', self.cmd_MMU_SELECT_BYPASS, desc = self.cmd_MMU_SELECT_BYPASS_help)
        self.gcode.register_command('MMU_CHANGE_TOOL', self.cmd_MMU_CHANGE_TOOL, desc = self.cmd_MMU_CHANGE_TOOL_help)
        # TODO currently not registered directly as Tx commands because not visible by Mainsail/Fluuid
        # for tool in range(self.num_gates):
        #     self.gcode.register_command('T%d' % tool, self.cmd_MMU_CHANGE_TOOL, desc = "Change to tool T%d" % tool)
        self.gcode.register_command('MMU_LOAD', self.cmd_MMU_LOAD, desc=self.cmd_MMU_LOAD_help)
        self.gcode.register_command('MMU_EJECT', self.cmd_MMU_EJECT, desc = self.cmd_MMU_EJECT_help)
        self.gcode.register_command('MMU_UNLOAD', self.cmd_MMU_EJECT, desc = self.cmd_MMU_EJECT_help) # Alias for MMU_EJECT
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
        self.gcode.register_command('_MMU_M400', self.cmd_MMU_M400, desc = self.cmd_MMU_M400_help) # Wait on both movequeues

        # Internal handlers for Runout & Insertion for all sensor options
        self.gcode.register_command('__MMU_ENCODER_RUNOUT', self.cmd_MMU_ENCODER_RUNOUT, desc = self.cmd_MMU_ENCODER_RUNOUT_help)
        self.gcode.register_command('__MMU_ENCODER_INSERT', self.cmd_MMU_ENCODER_INSERT, desc = self.cmd_MMU_ENCODER_INSERT_help)
        self.gcode.register_command('__MMU_GATE_RUNOUT_REMOVE', self.cmd_MMU_GATE_RUNOUT_REMOVE, desc = self.cmd_MMU_GATE_RUNOUT_REMOVE_help)
        self.gcode.register_command('__MMU_GATE_INSERT', self.cmd_MMU_GATE_INSERT, desc = self.cmd_MMU_GATE_INSERT_help)
        self.gcode.register_command('__MMU_EXTRUDER_RUNOUT_REMOVE', self.cmd_MMU_EXTRUDER_RUNOUT_REMOVE, desc = self.cmd_MMU_EXTRUDER_RUNOUT_REMOVE_help)
        self.gcode.register_command('__MMU_EXTRUDER_INSERT', self.cmd_MMU_EXTRUDER_INSERT, desc = self.cmd_MMU_EXTRUDER_INSERT_help)

        # Initializer tasks
        self.gcode.register_command('__MMU_BOOTUP', self.cmd_MMU_BOOTUP, desc = self.cmd_MMU_BOOTUP_help) # Bootup tasks

        # Load development test commands
        _ = MmuTest(self)

        # Apply Klipper hacks -------------------------------------------------------------------------------
        if self.update_trsync: # Timer too close mitigation
            try:
                import mcu
                mcu.TRSYNC_TIMEOUT = max(mcu.TRSYNC_TIMEOUT, 0.05)
            except Exception as e:
                self.log_error("Unable to update TRSYNC_TIMEOUT: %s" % str(e))

        if self.update_bit_max_time: # Neopixel update error mitigation
            try:
                from extras import neopixel
                neopixel.BIT_MAX_TIME = max(neopixel.BIT_MAX_TIME, 0.000030)
            except Exception as e:
                self.log_error("Unable to update BIT_MAX_TIME: %s" % str(e))

        # Initialize state and statistics variables
        self.reinit()
        self._reset_statistics()
        self.counters = {}

    # Initialize MMU hardare. Note that logging not set up yet so use main klippy logger
    def _setup_mmu_hardware(self, config):
        logging.info("MMU Hardware Initialization -------------------------------")

        self.mmu_machine = self.printer.lookup_object("mmu_machine")
        self.num_gates = self.mmu_machine.num_gates
        self.has_leds = self.has_led_animation = False

        # Dynamically instantiate the selector class
        self.selector = globals()[self.mmu_machine.selector_type](self)

        # Now we can instantiate the MMU toolhead
        self.mmu_toolhead = MmuToolHead(config, self)
        rails = self.mmu_toolhead.get_kinematics().rails
        self.gear_rail = rails[1]
        self.mmu_extruder_stepper = self.mmu_toolhead.mmu_extruder_stepper # Is a MmuExtruderStepper if 'self.homing_extruder' is True

        # Setup filament sensors that are also used for homing (endstops)
        for name in (
            [self.ENDSTOP_TOOLHEAD, self.ENDSTOP_GATE, self.ENDSTOP_EXTRUDER_ENTRY] +
            ["%s_%d" % (self.ENDSTOP_POST_GATE_PREFIX, i) for i in range(self.num_gates)]
        ):
            sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % name, None)
            if sensor is not None:
                if name == self.ENDSTOP_TOOLHEAD or isinstance(sensor.runout_helper, MmuRunoutHelper):

                    # Add sensor pin as an extra endstop for gear rail
                    sensor_pin = self.config.getsection("filament_switch_sensor %s_sensor" % name).get("switch_pin")
                    ppins = self.printer.lookup_object('pins')
                    pin_params = ppins.parse_pin(sensor_pin, True, True)
                    share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                    ppins.allow_multi_use_pin(share_name)

                    # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
                    # otherwise the extruder can continue to move a small (speed dependent) distance
                    mcu_endstop = self.gear_rail.add_extra_endstop(sensor_pin, name)
                    if self.homing_extruder and name == self.ENDSTOP_TOOLHEAD:
                        mcu_endstop.add_stepper(self.mmu_extruder_stepper.stepper)
                else:
                    logging.warning("Improper setup: Filament sensor %s is not defined in [mmu_sensors]" % name)

        # Get optional encoder setup
        self.encoder_sensor = self.printer.lookup_object('mmu_encoder mmu_encoder', None)
        if not self.encoder_sensor:
            logging.warning("No [mmu_encoder] definition found in mmu_hardware.cfg. Assuming encoder is not available")

    def _setup_logging(self):
        # Setup background file based logging before logging any messages
        if self.log_file_level >= 0:
            logfile_path = self.printer.start_args['log_file']
            dirname = os.path.dirname(logfile_path)
            if dirname is None:
                mmu_log = '/tmp/mmu.log'
            else:
                mmu_log = dirname + '/mmu.log'
            logging.info("MMU Log: %s" % mmu_log)
            self.mmu_logger = MmuLogger(mmu_log)
            self.mmu_logger.log("\n\n\nMMU Startup -----------------------------------------------\n")

    def handle_connect(self):
        self._setup_logging()

        self.toolhead = self.printer.lookup_object('toolhead')
        self.mmu_sensors = self.printer.lookup_object('mmu_sensors', None)
        self.sensor_manager = MmuSensorManager(self)

        # Sanity check extruder name
        extruder = self.printer.lookup_object(self.extruder_name, None)
        if not extruder:
            raise self.config.error("Extruder named '%s' not found on printer" % self.extruder_name)

        # See if we have a TMC controller capable of current control for filament collision detection and syncing
        # on gear_stepper and tip forming on extruder
        self.gear_tmc = self.extruder_tmc = None
        for chip in mmu_machine.TMC_CHIPS:
            if self.gear_tmc is None:
                self.gear_tmc = self.printer.lookup_object('%s %s' % (chip, mmu_machine.GEAR_STEPPER_CONFIG), None)
                if self.gear_tmc is not None:
                    self.log_debug("Found %s on gear_stepper. Current control enabled. Stallguard 'touch' homing possible." % chip)
            if self.extruder_tmc is None:
                self.extruder_tmc = self.printer.lookup_object("%s %s" % (chip, self.extruder_name), None)
                if self.extruder_tmc is not None:
                    self.log_debug("Found %s on extruder. Current control enabled. %s" % (chip, "Stallguard 'touch' homing possible." if self.homing_extruder else ""))
        if self.gear_tmc is None:
            self.log_debug("TMC driver not found for gear_stepper, cannot use current reduction for collision detection or while synchronized printing")
        if self.extruder_tmc is None:
            self.log_debug("TMC driver not found for extruder, cannot use current increase for tip forming move")

        # Establish gear_stepper initial gear_stepper and extruder currents
        self.gear_default_run_current = self.gear_tmc.get_status(0)['run_current'] if self.gear_tmc else None
        self.extruder_default_run_current = self.extruder_tmc.get_status(0)['run_current'] if self.extruder_tmc else None
        self.gear_percentage_run_current = self.gear_restore_percent_run_current = self.extruder_percentage_run_current = 100.

        # Sanity check that required klipper options are enabled
        self.print_stats = self.printer.lookup_object("print_stats", None)
        if self.print_stats is None:
            self.log_debug("[virtual_sdcard] is not found in config, advanced state control is not possible")
        self.pause_resume = self.printer.lookup_object('pause_resume', None)
        if self.pause_resume is None:
            raise self.config.error("MMU requires [pause_resume] to work, please add it to your config!")

        # Remember user setting of idle_timeout so it can be restored (if not overridden)
        if self.default_idle_timeout < 0:
            self.default_idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout

        # Sanity check to see that mmu_vars.cfg is included. This will verify path because default deliberately has 'mmu_revision' entry
        self.save_variables = self.printer.lookup_object('save_variables', None)
        rd_var = self.save_variables.allVariables.get(self.VARS_MMU_GEAR_ROTATION_DISTANCE, None)
        revision_var = self.save_variables.allVariables.get(self.VARS_MMU_REVISION, None)
        if revision_var is None:
            self.save_variables.allVariables[self.VARS_MMU_REVISION] = 0
        if not self.save_variables or (rd_var is None and revision_var is None):
            raise self.config.error("Calibration settings file (mmu_vars.cfg) not found. Check [save_variables] section in mmu_macro_vars.cfg\nAlso ensure you only have a single [save_variables] section defined in your printer config and it contains the line: mmu__revision = 0. If not, add this line and restart")

        # Upgrade legacy or scalar variables to lists -------------------------------------------------------
        bowden_length = self.save_variables.allVariables.get(self.VARS_MMU_CALIB_BOWDEN_LENGTH, None)
        if bowden_length:
            self.log_debug("Upgrading %s variable" % (self.VARS_MMU_CALIB_BOWDEN_LENGTH))
            bowden_lengths = self._ensure_list_size([round(bowden_length, 1)], self.num_gates)
            self.save_variables.allVariables.pop(self.VARS_MMU_CALIB_BOWDEN_LENGTH, None)
            # Can't write file now so we let this occur naturally on next write
            self.save_variables.allVariables[self.VARS_MMU_CALIB_BOWDEN_LENGTHS] = bowden_lengths
            self.save_variables.allVariables[self.VARS_MMU_CALIB_BOWDEN_HOME] = self.gate_homing_endstop

        rotation_distance = self.save_variables.allVariables.get(self.VARS_MMU_GEAR_ROTATION_DISTANCE, None)
        if rotation_distance:
            self.log_debug("Upgrading %s and %s variables" % (self.VARS_MMU_GEAR_ROTATION_DISTANCE, self.VARS_MMU_CALIB_PREFIX))
            rotation_distances = []
            for i in range(self.num_gates):
                ratio = self.save_variables.allVariables.get("%s%d" % (self.VARS_MMU_CALIB_PREFIX, i), 0)
                rotation_distances.append(round(rotation_distance * ratio, 6))
                self.save_variables.allVariables.pop("%s%d" % (self.VARS_MMU_CALIB_PREFIX, i), None)
            self.save_variables.allVariables.pop(self.VARS_MMU_GEAR_ROTATION_DISTANCE, None)
            # Can't write file now so we let this occur naturally on next write
            self.save_variables.allVariables[self.VARS_MMU_GEAR_ROTATION_DISTANCES] = rotation_distances
        else:
            self.save_variables.allVariables.pop("%s0" % self.VARS_MMU_CALIB_PREFIX, None)

        # Load bowden length configuration (calibration set with MMU_CALIBRATE_BOWDEN) ----------------------
        self.bowden_lengths = self.save_variables.allVariables.get(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, None)
        bowden_home = self.save_variables.allVariables.get(self.VARS_MMU_CALIB_BOWDEN_HOME, self.ENDSTOP_ENCODER)
        if self.bowden_lengths and bowden_home in self.GATE_ENDSTOPS:
            self.bowden_lengths = [-1 if x < 0 else x for x in self.bowden_lengths] # Ensure -1 value for uncalibrated
            # Ensure list size
            if len(self.bowden_lengths) == self.num_gates:
                self.log_debug("Loaded saved bowden lengths: %s" % self.bowden_lengths)
            else:
                self.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.VARS_MMU_CALIB_BOWDEN_LENGTHS)
                self.bowden_lengths = self._ensure_list_size(self.bowden_lengths, self.num_gates)

            # Ensure they are identical (just for optics) if variable_bowden_lengths is False
            if not self.mmu_machine.variable_bowden_lengths:
                self.bowden_lengths = [self.bowden_lengths[0]] * self.num_gates

            self._adjust_bowden_lengths()
            if not any(x == -1 for x in self.bowden_lengths):
                self.calibration_status |= self.CALIBRATED_BOWDENS
        else:
            self.log_always("Warning: Bowden lengths not found in mmu_vars.cfg. Probably not calibrated yet")
            self.bowden_lengths = [-1] * self.num_gates
        self.save_variables.allVariables[self.VARS_MMU_CALIB_BOWDEN_LENGTHS] = self.bowden_lengths
        self.save_variables.allVariables[self.VARS_MMU_CALIB_BOWDEN_HOME] = bowden_home

        # Load gear rotation distance configuration (calibration set with MMU_CALIBRATE_GEAR) ---------------
        self.default_rotation_distance = self.gear_rail.steppers[0].get_rotation_distance()[0] # TODO should be per gear in case they are disimilar?
        self.rotation_distances = self.save_variables.allVariables.get(self.VARS_MMU_GEAR_ROTATION_DISTANCES, None)
        if self.rotation_distances:
            self.rotation_distances = [-1 if x == 0 else x for x in self.rotation_distances] # Ensure -1 value for uncalibrated
            # Ensure list size
            if len(self.rotation_distances) == self.num_gates:
                self.log_debug("Loaded saved gear rotation distances: %s" % self.rotation_distances)
            else:
                self.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.VARS_MMU_GEAR_ROTATION_DISTANCES)
                self.rotation_distances = self._ensure_list_size(self.rotation_distances, self.num_gates)

            # Ensure they are identical (just for optics) if variable_rotation_distances is False
            if not self.mmu_machine.variable_rotation_distances:
                self.rotation_distances = [self.rotation_distances[0]] * self.num_gates

            if self.rotation_distances[0] != -1:
                self.calibration_status |= self.CALIBRATED_GEAR_0
            if not any(x == -1 for x in self.rotation_distances):
                self.calibration_status |= self.CALIBRATED_GEAR_RDS
        else:
            self.log_always("Warning: Gear rotation distances not found in mmu_vars.cfg. Probably not calibrated yet")
            self.rotation_distances = [-1] * self.num_gates
        self.save_variables.allVariables[self.VARS_MMU_GEAR_ROTATION_DISTANCES] = self.rotation_distances

        # Load encoder configuration (calibration set with MMU_CALIBRATE_ENCODER) ---------------------------
        self.encoder_resolution = 1.0
        if self.has_encoder():
            self.encoder_resolution = self.encoder_sensor.get_resolution()
            self.encoder_sensor.set_logger(self.log_debug) # Combine with MMU log
            self.encoder_sensor.set_extruder(self.extruder_name)
            self.encoder_sensor.set_mode(self.enable_clog_detection)

            resolution = self.save_variables.allVariables.get(self.VARS_MMU_ENCODER_RESOLUTION, None)
            if resolution:
                self.encoder_resolution = resolution
                self.encoder_sensor.set_resolution(resolution)
                self.log_debug("Loaded saved encoder resolution: %.6f" % resolution)
                self.calibration_status |= self.CALIBRATED_ENCODER
            else:
                self.log_always("Warning: Encoder resolution not found in mmu_vars.cfg. Probably not calibrated")
        else:
            self.calibration_status |= self.CALIBRATED_ENCODER # Pretend we are calibrated to avoid warnings

        # The threshold (mm) that determines real encoder movement (set to 1.5 pulses of encoder. i.e. to allow one rougue pulse)
        self.encoder_min = 1.5 * self.encoder_resolution

        # Sub components
        self.selector.handle_connect()

    def _ensure_list_size(self, lst, size, default_value=-1):
        lst = lst[:size]
        lst.extend([default_value] * (size - len(lst)))
        return lst

    def handle_disconnect(self):
        self.log_debug('Klipper disconnected! MMU Shutdown')
        if self.mmu_logger:
            self.mmu_logger.shutdown()

        # Sub components
        self.selector.handle_disconnect()

    def handle_ready(self):
        # Pull retraction length from macro config
        sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
        if sequence_vars_macro:
            park_toolchange = sequence_vars_macro.variables.get('park_toolchange',(0))
            self.toolchange_retract = park_toolchange[-1]

        # Reference correct extruder stepper which will definitely be available now
        self.mmu_extruder_stepper = self.mmu_toolhead.mmu_extruder_stepper
        if not self.homing_extruder:
            self.log_debug("Warning: Using original klipper extruder stepper. Extruder homing not possible")

        # Restore state (only if fully calibrated)
        self._load_persisted_state()

        # Setup events for managing internal print state machine
        self.printer.register_event_handler("idle_timeout:printing", self._handle_idle_timeout_printing)
        self.printer.register_event_handler("idle_timeout:ready", self._handle_idle_timeout_ready)
        self.printer.register_event_handler("idle_timeout:idle", self._handle_idle_timeout_idle)

        # Setup events for managing motor synchronization
        self.printer.register_event_handler("mmu:synced", self._handle_mmu_synced)
        self.printer.register_event_handler("mmu:unsynced", self._handle_mmu_unsynced)
        self.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)
        self._setup_sync_feedback()

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

        # Ensure that the LED control macro knows the indices of the segments of the LED chain and other essential data
        gcode_macro = self.printer.lookup_object("gcode_macro _MMU_SET_LED", None)
        if gcode_macro:
            try:
                led_chains = MmuLeds.chains
                self.has_led_animation = MmuLeds.led_effect_module
                led_vars = {}
                if led_chains:
                    self.has_leds = True
                    c_exit = led_chains['exit']
                    led_vars['exit_first_led_index'] = c_exit[0] if c_exit else -1
                    led_vars['exit_reverse_order'] = int(c_exit[0] > c_exit[-1]) if c_exit else 0
                    entry = led_chains['entry']
                    led_vars['entry_first_led_index'] = entry[0] if entry else -1
                    led_vars['entry_reverse_order'] = int(entry[0] > entry[-1]) if entry else 0
                    led_vars['status_led_index'] = led_chains['status'][0] if led_chains['status'] else -1
                    led_vars['led_strip'] = MmuLeds.led_strip
                    self.log_debug("LEDs support enabled %s" % "with optional animation" if MmuLeds.led_effect_module else "")
                else:
                    self.has_leds = False
                    self.log_debug("LEDs support is not configured")
                gcode_macro.variables.update(led_vars)
            except Exception as e:
                self.log_error('Error setting up the _MMU_SET_LED macro: %s' % str(e))
        else:
            self.log_error("LEDs macro _MMU_SET_LED not available")

        # Override user configuration based on actual h/w setup
        led_vars_macro = self.printer.lookup_object("gcode_macro _MMU_LED_VARS", None)
        if led_vars_macro:
            variables = led_vars_macro.variables
            led_vars = {}
            led_vars['led_enable'] = variables.get('led_enable', True) & self.has_leds
            led_vars['led_animation'] = variables.get('led_animation', True) & self.has_led_animation
            led_vars_macro.variables.update(led_vars)

        # Sub components
        self.selector.handle_ready()

        # Ensure sync_feedback starting state. This is mainly cosmetic because state is ensured when enabling
        self._update_sync_starting_state()

        # Schedule bootup tasks to run after klipper and hopefully spoolman have settled
        self._schedule_mmu_bootup_tasks(self.BOOT_DELAY)

    def reinit(self):
        self.is_enabled = self.runout_enabled = True
        self.is_handling_runout = self.calibrating = False
        self.last_print_stats = self.paused_extruder_temp = self.reason_for_pause = None
        self.tool_selected = self._next_tool = self.gate_selected = self.TOOL_GATE_UNKNOWN
        self._last_toolchange = "Unknown"
        self.active_filament = {}
        self.filament_pos = self.FILAMENT_POS_UNKNOWN
        self.filament_direction = self.DIRECTION_UNKNOWN
        self.action = self.ACTION_IDLE
        self._clear_saved_toolhead_position()
        self._reset_job_statistics()
        self.print_state = self.resume_to_state = "ready"
        self.form_tip_vars = None # Current defaults of gcode variables for tip forming macro
        self._clear_slicer_tool_map()
        self.pending_spool_id = None # For automatic assignment of spool_id if set perhaps by rfid reader

        # Sub components
        self.selector.reinit()

    def _clear_slicer_tool_map(self):
        self.slicer_tool_map = {'tools': {}, 'referenced_tools': [], 'initial_tool': None, 'purge_volumes': []}
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        self._update_t_macros() # Clear 'color' on Tx macros if displaying slicer colors

    # Helper to infer type for setting gcode macro variables
    def _fix_type(self, s):
        try:
            return float(s)
        except ValueError:
            try:
                return int(s)
            except ValueError:
                return s

    # Compare unicode strings with optional case insensitivity
    def _compare_unicode(self, a, b, case_insensitive=True):
        a = unicodedata.normalize('NFKC', a)
        b = unicodedata.normalize('NFKC', b)
        if case_insensitive:
            a = a.lower()
            b = b.lower()
        return a == b

    # This retuns the hex color format without leading '#' E.g. ff00e0
    def _color_to_rgb_hex(self, color):
        if color in self.w3c_colors:
            color = self.w3c_colors.get(color)
        elif color == '':
            color = "#000000"
        rgb_hex = color.lstrip('#').lower()
        return rgb_hex

    # This retuns a convenient RGB fraction tuple for controlling LEDs E.g. (0.32, 0.56, 1.00)
    # or integer version (82, 143, 255)
    def _color_to_rgb_tuple(self, color, fraction=True):
        rgb_hex = self._color_to_rgb_hex(color)
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
        x = re.search(r"^([a-f\d]{6})$", color)
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
        ref_rgb = self._color_to_rgb_tuple(ref_color, fraction=False)
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
    def _update_gate_color(self, new_color_map):
        self.gate_color = new_color_map

        # Recalculate RGB map for easy LED support
        self.gate_color_rgb = [self._color_to_rgb_tuple(i) for i in self.gate_color]

    # Helper to keep parallel RGB color map updated when slicer color or TTG changes
    def _update_slicer_color(self):
        self.slicer_color_rgb = [(0.,0.,0.)] * self.num_gates
        for tool_key, tool_value in self.slicer_tool_map['tools'].items():
            tool = int(tool_key)
            gate = self.ttg_map[tool]
            self.slicer_color_rgb[gate] = self._color_to_rgb_tuple(tool_value['color'])
        self._update_t_macros()
        self.mmu_macro_event(self.MACRO_EVENT_GATE_MAP_CHANGED, "GATE=-1") # Cheat to force LED update

    # Helper to determine purge volume for toolchange
    def _get_purge_volume(self, from_tool, to_tool):
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
            # Add volume of residual filament
            volume += math.pi * ((fil_diameter / 2) ** 2) * (self.filament_remaining + self.toolhead_residual_filament)
        return volume

    # Generate purge matrix based on filament colors
    def _generate_purge_matrix(self, tool_colors, purge_min, purge_max, multiplier):
        purge_vol_calc = PurgeVolCalculator(purge_min, purge_max, multiplier)

        # Build purge volume map (x=to_tool, y=from_tool)
        should_calc = lambda x,y: x < len(tool_colors) and y < len(tool_colors) and x != y
        purge_volumes = [
            [
                purge_vol_calc.calc_purge_vol_by_hex(tool_colors[y], tool_colors[x]) if should_calc(x,y) else 0
                for y in range(self.num_gates)
            ]
            for x in range(self.num_gates)
        ]
        return purge_volumes

    def _load_persisted_state(self):
        self.log_debug("Loading persisted MMU state")
        errors = []

        # Always load length of filament remaining in extruder (after cut) and last tool loaded
        self.filament_remaining = self.save_variables.allVariables.get(self.VARS_MMU_FILAMENT_REMAINING, self.filament_remaining)
        self._last_tool = self.save_variables.allVariables.get(self.VARS_MMU_LAST_TOOL, self._last_tool)

        # Load EndlessSpool config
        self.enable_endless_spool = self.save_variables.allVariables.get(self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool)
        endless_spool_groups = self.save_variables.allVariables.get(self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        if len(endless_spool_groups) == self.num_gates:
            self.endless_spool_groups = endless_spool_groups
        else:
            errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_ENDLESS_SPOOL_GROUPS)

        # Load TTG map
        tool_to_gate_map = self.save_variables.allVariables.get(self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map)
        if len(tool_to_gate_map) == self.num_gates:
            self.ttg_map = tool_to_gate_map
        else:
            errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_TOOL_TO_GATE_MAP)

        # Load gate map
        for var, attr, _ in self.gate_map_vars:
            value = self.save_variables.allVariables.get(var, getattr(self, attr))
            if len(value) == self.num_gates:
                if attr == "gate_color":
                    self._update_gate_color(value)
                else:
                    setattr(self, attr, value)
            else:
                errors.append("Incorrect number of gates specified in %s" % var)

        # Load selected tool and gate
        tool_selected = self.save_variables.allVariables.get(self.VARS_MMU_TOOL_SELECTED, self.tool_selected)
        gate_selected = self.save_variables.allVariables.get(self.VARS_MMU_GATE_SELECTED, self.gate_selected)
        if gate_selected < self.num_gates and tool_selected < self.num_gates:
            self._set_tool_selected(tool_selected)
            self._set_gate_selected(gate_selected)
            self._ensure_ttg_match() # Ensure tool/gate consistency
            self.selector.restore_gate_position()
        else:
            errors.append("Invalid tool or gate specified in %s or %s" % (self.VARS_MMU_TOOL_SELECTED, self.VARS_MMU_GATE_SELECTED))

        # Previous filament position
        self.filament_pos = self.save_variables.allVariables.get(self.VARS_MMU_FILAMENT_POS, self.filament_pos)

        if len(errors) > 0:
            self.log_info("Warning: Some persisted state was ignored because it contained errors:\n%s" % ''.join(errors))

        swap_stats = self.save_variables.allVariables.get(self.VARS_MMU_SWAP_STATISTICS, {})
        counters = self.save_variables.allVariables.get(self.VARS_MMU_COUNTERS, {})
        self.counters.update(counters)

        # Auto upgrade old names
        key_map = {"time_spent_loading": "load", "time_spent_unloading": "unload", "time_spent_paused": "pause"}
        swap_stats = {key_map.get(key, key): swap_stats[key] for key in swap_stats}
        swap_stats.pop('servo_retries', None) # Deprecated

        self.statistics.update(swap_stats)
        for gate in range(self.num_gates):
            self.gate_statistics[gate] = self.EMPTY_GATE_STATS_ENTRY.copy()
            gstats = self.save_variables.allVariables.get("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, gate), None)
            if gstats:
                self.gate_statistics[gate].update(gstats)

    def _schedule_mmu_bootup_tasks(self, delay=0.):
        waketime = self.reactor.monotonic() + delay
        self.reactor.register_callback(lambda pt: self._print_event("__MMU_BOOTUP"), waketime)

    cmd_MMU_BOOTUP_help = "Internal commands to complete bootup of MMU"
    def cmd_MMU_BOOTUP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        fversion = lambda f: "v{}.".format(int(f)) + '.'.join("{:0<2}".format(int(str(f).split('.')[1])))
        try:
            # Splash...
            msg = '{1}(\_/){0}\n{1}( {0}*,*{1}){0}\n{1}(")_("){0} {5}{2}H{0}{3}a{0}{4}p{0}{2}p{0}{3}y{0} {4}H{0}{2}a{0}{3}r{0}{4}e{0} {1}%s{0} {2}R{0}{3}e{0}{4}a{0}{2}d{0}{3}y{0}{1}...{0}{6}' % fversion(self.config_version)
            self.log_always(msg, color=True)
            self._set_print_state("initialized")

            # Use pre-gate sensors to adjust gate map
            self.gate_status = self._validate_gate_status(self.gate_status)

            # Sanity check filament pos based only on non-intrusive tests and recover if necessary
            recover_pos = False
            if self.filament_pos == self.FILAMENT_POS_LOADED and not self.sensor_manager.check_all_sensors_before(self.filament_pos, self.gate_selected):
                recover_pos = True
            elif self.filament_pos == self.FILAMENT_POS_UNLOADED and self.sensor_manager.check_any_sensors_in_path():
                recover_pos = True
            elif self.filament_pos == self.FILAMENT_POS_UNKNOWN:
                recover_pos = True
            if recover_pos:
                self.recover_filament_pos(can_heat=False, message=True, silent=True)

            # Apply startup options
            if self.startup_reset_ttg_map:
                self._reset_ttg_map()

            if self.startup_home_if_unloaded and self.check_if_not_calibrated(self.CALIBRATED_SELECTOR) and self.filament_pos == self.FILAMENT_POS_UNLOADED:
                self.home(0)

            if self.log_startup_status:
                self.log_always(self._mmu_visual_to_string())
                self._display_visual_state()

            if not self.check_if_not_calibrated(self.CALIBRATED_ALL, silent=None):
                if self.filament_pos != self.FILAMENT_POS_UNLOADED and self.TOOL_GATE_UNKNOWN in [self.gate_selected, self.tool_selected]:
                    self.log_error("Filament detected but tool/gate is unknown. Plese use MMU_RECOVER to correct state")

            if self.has_encoder():
                self.encoder_sensor.set_clog_detection_length(self.save_variables.allVariables.get(self.VARS_MMU_CALIB_CLOG_LENGTH, 15))
                self._disable_runout() # Initially disable clog/runout detection

            self.selector.filament_hold()
            self.movequeues_wait()

            # Sync with spoolman. Delay as long as possible to maximize the chance it is contactable after startup/reboot
            self._spoolman_sync()
        except Exception as e:
            self.log_error('Error booting up MMU: %s' % str(e))
        self.mmu_macro_event(self.MACRO_EVENT_RESTART)

    def _wrap_gcode_command(self, command, exception=False, variables=None, wait=False):
        try:
            macro = command.split()[0]
            if variables is not None:
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % macro)
                gcode_macro.variables.update(variables)
            self.log_trace("Running macro: %s%s" % (command, " (with override variables)" if variables is not None else ""))
            self.gcode.run_script_from_command(command)
            if wait:
                self.movequeues_wait()
        except Exception as e:
            if exception is not None:
                if exception:
                    raise MmuError("Error running %s: %s" % (macro, str(e)))
                else:
                    self.log_error("Error running %s: %s" % (macro, str(e)))
            else:
                raise

    def mmu_macro_event(self, event_name, params=""):
        if self.printer.lookup_object("gcode_macro %s" % self.mmu_event_macro, None) is not None:
            self._wrap_gcode_command("%s EVENT=%s %s" % (self.mmu_event_macro, event_name, params))

    # Wait on desired move queues
    def movequeues_wait(self, toolhead=True, mmu_toolhead=True):
        #self.log_trace("movequeues_wait(toolhead=%s, mmu_toolhead=%s)" % (toolhead, mmu_toolhead))
        if toolhead:
            self.toolhead.wait_moves()
        if mmu_toolhead:
            self.mmu_toolhead.wait_moves()

    # Dwell on desired move queues
    def movequeues_dwell(self, dwell, toolhead=True, mmu_toolhead=True):
        if dwell > 0.:
            if toolhead and mmu_toolhead:
                self.movequeues_sync()
            if toolhead:
                self.toolhead.dwell(dwell)
            if mmu_toolhead:
                self.mmu_toolhead.dwell(dwell)

    # Align timing of move queues
    def movequeues_sync(self):
        mmu_last_move = self.mmu_toolhead.get_last_move_time()
        last_move = self.toolhead.get_last_move_time()
        delta = mmu_last_move - last_move
        if delta > 0:
            self.toolhead.dwell(abs(delta))
        elif delta < 0:
            self.mmu_toolhead.dwell(abs(delta))


####################################
# LOGGING AND STATISTICS FUNCTIONS #
####################################

    def _get_action_string(self, action=None):
        if action is None:
            action = self.action

        return ("Idle" if action == self.ACTION_IDLE else
                "Loading" if action == self.ACTION_LOADING else
                "Unloading" if action == self.ACTION_UNLOADING else
                "Loading Ext" if action == self.ACTION_LOADING_EXTRUDER else
                "Exiting Ext" if action == self.ACTION_UNLOADING_EXTRUDER else
                "Forming Tip" if action == self.ACTION_FORMING_TIP else
                "Heating" if action == self.ACTION_HEATING else
                "Checking" if action == self.ACTION_CHECKING else
                "Homing" if action == self.ACTION_HOMING else
                "Selecting" if action == self.ACTION_SELECTING else
                "Unknown") # Error case - should not happen

    def _get_sync_feedback_string(self):
        if self.is_enabled and self.sync_feedback_enable and self.sync_feedback_operational:
            return 'compressed' if self.sync_feedback_last_state > 0.5 else 'expanded' if self.sync_feedback_last_state < -0.5 else 'neutral'
        return "disabled"

    # Returning new list() is so that clients like KlipperScreen sees the change
    def get_status(self, eventtime):
        status = {
            'enabled': self.is_enabled,
        }
        # TODO: Should this be in a conditional if self.is_enabled: block for performance?
        status.update({
            'num_gates': self.num_gates,
            'is_paused': self.is_mmu_paused(),
            'is_locked': self.is_mmu_paused(), # Alias for is_paused (deprecated)
            'is_homed': self.selector.is_homed,
            'is_in_print': self.is_in_print(),
            'tool': self.tool_selected,
            'gate': self.gate_selected,
            'active_filament': self.active_filament,
            'last_tool': self._last_tool,
            'next_tool': self._next_tool,
            'toolchange_purge_volume': self.toolchange_purge_volume,
            'last_toolchange': self._last_toolchange,
            'runout': self.is_handling_runout, # Deprecated (use operation)
            'operation': self.saved_toolhead_operation,
            'filament': "Loaded" if self.filament_pos == self.FILAMENT_POS_LOADED else
                        "Unloaded" if self.filament_pos == self.FILAMENT_POS_UNLOADED else
                        "Unknown",
            'filament_position': self.mmu_toolhead.get_position()[1],
            'filament_pos': self.filament_pos,
            'filament_direction': self.filament_direction,
            'ttg_map': list(self.ttg_map),
            'gate_status': list(self.gate_status),
            'gate_filament_name': list(self.gate_filament_name),
            'gate_material': list(self.gate_material),
            'gate_color': list(self.gate_color),
            'gate_temperature': list(self.gate_temperature),
            'gate_color_rgb': self.gate_color_rgb,
            'gate_spool_id': list(self.gate_spool_id),
            'slicer_color_rgb': self.slicer_color_rgb,
            'endless_spool_groups': list(self.endless_spool_groups),
            'tool_extrusion_multipliers': self.tool_extrusion_multipliers,
            'tool_speed_multipliers': self.tool_speed_multipliers,
            'slicer_tool_map': self.slicer_tool_map,
            'action': self._get_action_string(),
            'has_bypass': self.selector.has_bypass(),
            'sync_drive': self.mmu_toolhead.is_synced(),
            'sync_feedback_state': self._get_sync_feedback_string(),
            'print_state': self.print_state,
            'clog_detection': self.enable_clog_detection,
            'endless_spool': self.enable_endless_spool,
            'print_start_detection': self.print_start_detection, # For Klippain. Not really sure it is necessary
            'reason_for_pause': self.reason_for_pause if self.is_mmu_paused() else "",
            'extruder_filament_remaining': self.filament_remaining + self.toolhead_residual_filament,
            'spoolman_support': self.spoolman_support,
            'enable_spoolman': int(not self.spoolman_support == self.SPOOLMAN_OFF), # Legacy
        })
        status.update(self.selector.get_status())
        return status

    def _reset_statistics(self):
        self.statistics = {}
        self.last_statistics = {}
        self.track = {}
        self.gate_statistics = []
        for _ in range(self.num_gates):
            self.gate_statistics.append(self.EMPTY_GATE_STATS_ENTRY.copy())
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

    def _swap_statistics_to_string(self, total=True):
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
        total = self.console_always_output_full or total or not self.is_in_print()

        table_column_order = ['pre_unload', 'unload', 'post_unload', 'pre_load', 'load', 'post_load', 'total']
        table_include_columns = self._list_intersection(table_column_order, self.console_stat_columns) # To maintain the correct order and filter incorrect ones

        table_row_options = ['total', 'total_average', 'job', 'job_average', 'last']
        table_include_rows = self._list_intersection(self.console_stat_rows, table_row_options) # Keep the user provided order

        # Remove totals from table if not in print and not forcing total
        if not self.console_always_output_full and not total:
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
                'unload': '-',
                'post_unload': 'post',
                'pre_load': 'pre',
                'load': '-',
                'post_load': 'post',
                'total': 'swap'
            }
            # Group the top headers map. Omit the first column, because that'll be filled with the nr. of swaps
            table_extra_headers_map = {
                'unloading': ['pre_unload', 'unload', 'post_unload'],
                'loading': ['pre_load', 'load', 'post_load'],
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
            msg += UI_BOX_TL    + UI_BOX_T.join([UI_BOX_H * width for width in column_extra_header_widths]) + UI_BOX_TR + "\n"
            msg += UI_BOX_V     + UI_BOX_V.join([table_extra_headers[i].center(column_extra_header_widths[i], UI_SEPARATOR)
                for i in range(len(column_extra_header_widths))]) + UI_BOX_V + "\n"
            msg += UI_BOX_V     + UI_BOX_V.join([table_headers[i].center(column_widths[i], UI_SEPARATOR)
                for i in range(len(column_widths))]) + UI_BOX_V + "\n"
            msg += UI_BOX_L     + UI_BOX_M.join([UI_BOX_H * (width) for width in column_widths]) + UI_BOX_R + "\n"

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
        if self.log_statistics or force_log:
            if job or total:
                msg += self._swap_statistics_to_string(total=total)
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
        t = self.console_gate_stat
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
            self.save_variable("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, gate), self.gate_statistics[gate])
        # Good place to persist current clog length
        if self.has_encoder():
            self.save_variable(self.VARS_MMU_CALIB_CLOG_LENGTH, round(self.encoder_sensor.get_clog_detection_length(), 1))
        self._write_variables()

    def _persist_swap_statistics(self):
        self.statistics = {key: round(value, 2) if isinstance(value, float) else value for key, value in self.statistics.items()}
        self.save_variable(self.VARS_MMU_SWAP_STATISTICS, self.statistics, write=True)

    def _persist_counters(self):
        self.save_variable(self.VARS_MMU_COUNTERS, self.counters, write=True)

    def _color_message(self, msg):
        # 0=end_color, 1=grey, 2=red, 3=green, 4=blue, 5=bold_on, 6=bold_off
        html_msg = msg.format('</span>', '<span style=\"color:#C0C0C0\">', '<span style=\"color:#FF69B4\">', '<span style=\"color:#90EE90\">', '<span style=\"color:#87CEEB\">', '<b>', '</b>')
        msg = re.sub(r'\{\d\}', '', msg)
        if self.serious:
            html_msg = msg
        return html_msg, msg

    def log_to_file(self, msg, prefix='> '):
        msg = "%s%s" % (prefix, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)

    def log_error(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)
        self.gcode.respond_raw("!! %s" % html_msg)

    def log_always(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger:
            self.mmu_logger.log(msg)
        self.gcode.respond_info(html_msg)

    def log_info(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu_logger and self.log_file_level > 0:
            self.mmu_logger.log(msg)
        if self.log_level > 0:
            self.gcode.respond_info(html_msg)

    def log_debug(self, msg):
        msg = "%s DEBUG: %s" % (UI_SEPARATOR, msg)
        if self.mmu_logger and self.log_file_level > 1:
            self.mmu_logger.log(msg)
        if self.log_level > 1:
            self.gcode.respond_info(msg)

    def log_trace(self, msg):
        msg = "%s %s TRACE: %s" % (UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu_logger and self.log_file_level > 2:
            self.mmu_logger.log(msg)
        if self.log_level > 2:
            self.gcode.respond_info(msg)

    def log_stepper(self, msg):
        msg = "%s %s %s STEPPER: %s" % (UI_SEPARATOR, UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu_logger and self.log_file_level > 3:
            self.mmu_logger.log(msg)
        if self.log_level > 3:
            self.gcode.respond_info(msg)

    def log_enabled(self, level):
        return (self.mmu_logger and self.log_file_level >= level) or self.log_level >= level

    # Fun visual display of MMU state
    def _display_visual_state(self, silent=False):
        if not silent and self.log_visual and not self.calibrating:
            visual_str = self._state_to_string()
            self.log_always(visual_str, color=True)

    def _state_to_string(self, direction=None):
        arrow = "<" if self.filament_direction == self.DIRECTION_UNLOAD else ">"
        space = "."
        home  = "|"
        gs = "(g)" # ENDSTOP_GATE or ENDSTOP_POST_GATE_PREFIX
        es = "(e)" # ENDSTOP_EXTRUDER
        ts = "(t)" # ENDSTOP_TOOLHEAD
        past  = lambda pos: arrow if self.filament_pos >= pos else space
        homed = lambda pos, sensor: (' ',arrow,sensor) if self.filament_pos > pos else (home,space,sensor) if self.filament_pos == pos else (' ',space,sensor)
        trig  = lambda name, sensor: re.sub(r'[a-zA-Z]', '*', name) if self.sensor_manager.check_sensor(sensor) else name

        t_str   = ("[T%s] " % str(self.tool_selected)) if self.tool_selected >= 0 else "BYPASS " if self.tool_selected == self.TOOL_GATE_BYPASS else "[T?] "
        g_str   = "{}".format(past(self.FILAMENT_POS_UNLOADED))
        gs_str  = "{0}{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_GATE, trig(gs, self.gate_homing_endstop))) if self.gate_homing_endstop in [self.ENDSTOP_GATE, self.ENDSTOP_POST_GATE_PREFIX] else ""
        en_str  = " En {0}".format(past(self.FILAMENT_POS_IN_BOWDEN if self.gate_homing_endstop in [self.ENDSTOP_GATE, self.ENDSTOP_POST_GATE_PREFIX] else self.FILAMENT_POS_START_BOWDEN)) if self.has_encoder() else ""
        bowden1 = "{0}{0}{0}{0}".format(past(self.FILAMENT_POS_IN_BOWDEN))
        bowden2 = "{0}{0}{0}{0}".format(past(self.FILAMENT_POS_END_BOWDEN))
        es_str  = "{0}{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_ENTRY, trig(es, self.ENDSTOP_EXTRUDER_ENTRY))) if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY) else ""
        ex_str  = "{0}[{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_EXTRUDER, "Ex"))
        ts_str  = "{0}{2} {1}".format(*homed(self.FILAMENT_POS_HOMED_TS, trig(ts, self.ENDSTOP_TOOLHEAD))) if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD) else ""
        nz_str  = "{} Nz]".format(past(self.FILAMENT_POS_LOADED))
        summary = " {5}{4}LOADED{0}{6}" if self.filament_pos == self.FILAMENT_POS_LOADED else " {5}{4}UNLOADED{0}{6}" if self.filament_pos == self.FILAMENT_POS_UNLOADED else " {5}{2}UNKNOWN{0}{6}" if self.filament_pos == self.FILAMENT_POS_UNKNOWN else ""
        counter = " {5}%.1fmm{6}%s" % (self._get_filament_position(), " {1}(e:%.1fmm){0}" % self.get_encoder_distance(dwell=None) if self.has_encoder() and self.encoder_move_validation else "")
        visual = "".join((t_str, g_str, gs_str, en_str, bowden1, bowden2, es_str, ex_str, ts_str, nz_str, summary, counter))
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

        fversion = lambda f: "v{}.".format(int(f)) + '.'.join("{:0<2}".format(int(str(f).split('.')[1])))
        msg = "MMU: Happy Hare %s running %s v%s" % (fversion(self.config_version), self.mmu_machine.mmu_vendor, self.mmu_machine.mmu_version_string)
        msg += " with %d gates" % (self.num_gates)
        msg += " (%s)" % ("DISABLED" if not self.is_enabled else "PAUSED" if self.is_mmu_paused() else "OPERATIONAL")
        msg += self.selector.get_mmu_status_config()
        if self.has_encoder():
            msg += ". Encoder reads %.1fmm" % self.get_encoder_distance()
        msg += "\nPrint state is %s" % self.print_state.upper()
        msg += ". Tool %s selected on gate %s" % (self._selected_tool_string(), self._selected_gate_string())
        msg += ". Toolhead position saved" if self.saved_toolhead_operation else ""
        msg += "\nGear stepper at %d%% current and is %s to extruder" % (self.gear_percentage_run_current, "SYNCED" if self.mmu_toolhead.is_gear_synced_to_extruder() else "not synced")
        if self.sync_feedback_enable and self.sync_feedback_operational:
            msg += "\nSync feedback indicates filament in bowden is: %s" % self._get_sync_feedback_string().upper()
        elif self.sync_feedback_enable:
            msg += "\nSync feedback is disabled"

        if config:
            self.calibrated_bowden_length = self._get_bowden_length(self.gate_selected) # Temp scalar pulled from list
            msg += "\n\nLoad Sequence:"
            msg += "\n- Filament loads into gate by homing a maximum of %s to %s" % (self._f_calc("gate_homing_max"), self._gate_homing_string())
            msg += "\n- Bowden is loaded with a fast%s %s move" % (" CORRECTED" if self.bowden_apply_correction else "", self._f_calc("calibrated_bowden_length"))
            if self._must_home_to_extruder():
                if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION:
                    msg += ", then homes to extruder using COLLISION detection (at %d%% current)" % self.extruder_collision_homing_current
                else:
                    if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_NONE:
                        msg += ", no extruder homing is performed!"
                    else:
                        msg += ", then homes to extruder using ENDSTOP '%s'" % self.extruder_homing_endstop
                    if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_ENTRY:
                        msg += " and then moves %s to extruder extrance" % self._f_calc("toolhead_entry_to_extruder")
            if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder (synced) loads by homing a maximum of %s to TOOLHEAD SENSOR before moving the last %s to the nozzle" % (self._f_calc("toolhead_homing_max"), self._f_calc("toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining"))
            else:
                msg += "\n- Extruder (synced) loads by moving %s to the nozzle" % self._f_calc("toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining")
            if self._can_use_encoder() and not self.sync_to_extruder and self.enable_clog_detection and self.toolhead_post_load_tighten:
                msg += "\n- Filament in bowden is tightened by %.1fmm (%d%% of clog detection length) at reduced gear current to prevent false clog detection" % (min(self.encoder_sensor.get_clog_detection_length() * self.toolhead_post_load_tighten / 100, 15), self.toolhead_post_load_tighten)

            msg += "\n\nUnload Sequence:"
            msg += "\n- Tip is %s formed by %s%s" % (("sometimes", "SLICER", "") if not self.force_form_tip_standalone else ("always", ("'%s' macro" % self.form_tip_macro), " after initial retraction of %s" % self._f_calc("toolchange_retract")))
            msg += " and tip forming extruder current is %d%%" % self.extruder_form_tip_current

            msg += "\n- An estimated %s of filament is left in extruder (filament_remaining = tip-cutting fragment)" % self._f_calc("toolhead_residual_filament + filament_remaining")

            if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                msg += "\n- Extruder (synced) unloads by reverse homing a maximum of %s to EXTRUDER SENSOR" % self._f_calc("toolhead_entry_to_extruder + toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin")
            elif self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder (optionally synced) unloads by reverse homing a maximum %s to TOOLHEAD SENSOR" % self._f_calc("toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin")
                msg += ", then unloads by moving %s to exit extruder" % self._f_calc("toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle + toolhead_unload_safety_margin")
            else:
                msg += "\n- Extruder (optionally synced) unloads by moving %s less tip-cutting reported park position to exit extruder" % self._f_calc("toolhead_extruder_to_nozzle + toolhead_unload_safety_margin")

            if self.has_encoder() and self.bowden_pre_unload_test and not self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                msg += "\n- Bowden is unloaded with a short %s validation move before %s fast move" % (self._f_calc("encoder_move_step_size"), self._f_calc("calibrated_bowden_length - gate_unload_buffer - encoder_move_step_size"))
            else:
                msg += "\n- Bowden is unloaded with a fast %s move" % self._f_calc("calibrated_bowden_length - gate_unload_buffer")
            msg += "\n- Filament is stored by homing a maximum of %s to %s and parking %s in the gate\n" % (self._f_calc("gate_homing_max"), self._gate_homing_string(), self._f_calc("gate_parking_distance"))

            if self.sync_form_tip or self.sync_to_extruder:
                msg += "\nGear and Extruder steppers are synchronized during: "
                m = []
                if self.sync_to_extruder:
                    m.append("Print (at %d%% current %s sync feedback)" % (self.sync_gear_current, "with" if self.sync_feedback_enable else "without"))
                if self.sync_form_tip:
                    m.append("Tip forming")
                msg += ",".join(m)

            if hasattr(self.selector, 'use_touch_move'):
                msg += "\nSelector touch (stallguard) is %s - blocked gate recovery %s possible" % (("ENABLED", "is") if self.selector.use_touch_move() else ("DISABLED", "is not"))
            if self.has_encoder():
                msg += "\nMMU has an encoder. Non essential move validation is %s" % ("ENABLED" if self._can_use_encoder() else "DISABLED")
                msg += "\nRunout/Clog detection is %s" % ("AUTOMATIC" if self.enable_clog_detection == self.encoder_sensor.RUNOUT_AUTOMATIC else "ENABLED" if self.enable_clog_detection == self.encoder_sensor.RUNOUT_STATIC else "DISABLED")
                msg += " (%.1fmm runout)" % self.encoder_sensor.get_clog_detection_length()
                msg += ", EndlessSpool is %s" % ("ENABLED" if self.enable_endless_spool else "DISABLED")
            else:
                msg += "\nMMU does not have an encoder - move validation or clog detection / endless spool is not possible"
            msg += "\nSpoolMan is %s" % ("ENABLED (pulling gate map)" if self.spoolman_support == self.SPOOLMAN_PULL else "ENABLED (push gate map)" if self.spoolman_support == self.SPOOLMAN_PUSH else "ENABLED" if self.spoolman_support == self.SPOOLMAN_READONLY else "DISABLED")
            msg += "\nSensors: "
            sensors = self.sensor_manager.get_all_sensors()
            for name, state in sensors.items():
                msg += "%s (%s), " % (name.upper(), "Disabled" if state is None else ("Detected" if state is True else "Empty"))
            msg += "\nLogging: Console %d(%s)" % (self.log_level, self.LOG_LEVELS[self.log_level])

            msg += ", Logfile %d(%s)" % (self.log_file_level, self.LOG_LEVELS[self.log_file_level])
            msg += ", Visual %d(%s)" % (self.log_visual, on_off(self.log_visual))
            msg += ", Statistics %d(%s)" % (self.log_statistics, on_off(self.log_statistics))

        if not detail:
            msg += "\n\nFor details on TTG and EndlessSpool groups add 'DETAIL=1'"
            if not config:
                msg += ", for configuration add 'SHOWCONFIG=1'"

        msg += "\n\n%s" % self._mmu_visual_to_string()
        msg += "\n%s" % self._state_to_string()

        if detail:
            msg += "\n\n%s" % self._ttg_map_to_string()
            if self.enable_endless_spool:
                msg += "\n\n%s" % self._es_groups_to_string()
            msg += "\n\n%s" % self._gate_map_to_string()

        self.log_always(msg, color=True)
        self.check_if_not_calibrated(self.CALIBRATED_ALL, silent=None) # Always warn if not fully calibrated

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

    cmd_MMU_SENSORS_help = "Query state of sensors fitted to mmu"
    def cmd_MMU_SENSORS(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        eventtime = self.reactor.monotonic()

        if self.mmu_sensors:
            msg = ""
            # Sync feedback sensors (buttons)
            for sensor in [self.SWITCH_SYNC_FEEDBACK_TENSION, self.SWITCH_SYNC_FEEDBACK_COMPRESSION]:
                state = self.mmu_sensors.get_status(eventtime)[sensor]
                if state != -1 or detail:
                    msg += "%s: %s\n" % (sensor, 'TRIGGERED' if state == 1 else 'open' if state == 0 else '(disabled)')

            # All MMU filament sensors and endstops
            msg += self.sensor_manager.get_sensor_summary(include_disabled=detail)
            self.log_always(msg)
        else:
            self.log_always("No MMU sensors configured")

    # Instruct the selector to enguage the desired method of filament gripping based on MMU state
    def _auto_filament_grip(self):
        if self.is_printing() and self.mmu_toolhead.is_gear_synced_to_extruder():
            self.selector.filament_drive()
        elif not self.selector.is_homed or self.tool_selected < 0 or self.gate_selected < 0:
            self.selector.filament_hold()
        else:
            self.selector.filament_release()

    def motors_off(self, motor="all"):
        if motor in ["all", "gear", "gears"]:
            self.mmu_toolhead.unsync()
            stepper_enable = self.printer.lookup_object('stepper_enable')
            steppers = self.gear_rail.steppers if motor == "gears" else [self.gear_rail.steppers[0]]
            for stepper in steppers:
                se = stepper_enable.lookup_enable(stepper.get_name())
                se.motor_disable(self.mmu_toolhead.get_last_move_time())
        if motor in ["all", "selector"]:
            self._set_gate_selected(self.TOOL_GATE_UNKNOWN)
            self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
            self.selector.disable_motors()


### SERVO AND MOTOR GCODE FUNCTIONS ##############################################

    cmd_MMU_MOTORS_OFF_help = "Turn off all MMU motors and servos"
    def cmd_MMU_MOTORS_OFF(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self.motors_off()

    cmd_MMU_TEST_BUZZ_MOTOR_help = "Simple buzz the selected motor (default gear) for setup testing"
    def cmd_MMU_TEST_BUZZ_MOTOR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        motor = gcmd.get('MOTOR', "gear")
        if motor == "gear":
            found = self.buzz_gear_motor()
            if found is not None:
                self.log_info("Filament %s by gear motor buzz" % ("detected" if found else "not detected"))
        elif motor == "gears":
            try:
                for gate in range(self.num_gates):
                    self.mmu_toolhead.select_gear_stepper(gate)
                    found = self.buzz_gear_motor()
                    if found is not None:
                        self.log_info("Filament %s in gate %d by gear motor buzz" % ("detected" if found else "not detected", gate))
            finally:
                self.mmu_toolhead.select_gear_stepper(self.gate_selected)
        elif not self.selector.buzz_motor(motor):
            raise gcmd.error("Motor '%s' not known" % motor)

    cmd_MMU_SYNC_GEAR_MOTOR_help = "Sync the MMU gear motor to the extruder stepper"
    def cmd_MMU_SYNC_GEAR_MOTOR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_homed(): return
        grip = gcmd.get_int('GRIP', 1, minval=0, maxval=1)
        servo = gcmd.get_int('SERVO', 1, minval=0, maxval=1) # Deprecated (use GRIP=1 instead)
        sync = gcmd.get_int('SYNC', 1, minval=0, maxval=1)
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print current
        self._sync_gear_to_extruder(sync, grip=(grip and servo), current=self.is_in_print(force_in_print))


#########################
# CALIBRATION FUNCTIONS #
#########################

    def _calibrate_encoder(self, length, repeats, speed, min_speed, max_speed, accel, save=True):
        try:
            pos_values, neg_values = [], []
            self.log_always("%s over %.1fmm..." % ("Calibrating" if save else "Validating calibration", length))
            speed_incr = (max_speed - min_speed) / repeats
            test_speed = min_speed
            for x in range(repeats):
                if speed_incr > 0.:
                    self.log_always("Test run #%d, Speed=%.1f mm/s" % (x, test_speed))

                # Move forward
                self._initialize_filament_position(dwell=True)
                self.trace_filament_move(None, length, speed=test_speed, accel=accel, wait=True)
                counts = self._get_encoder_counts(dwell=True)
                pos_values.append(counts)
                self.log_always("%s+ counts: %d" % (UI_SPACE*2, counts))

                # Move backward
                self._initialize_filament_position(dwell=True)
                self.trace_filament_move(None, -length, speed=test_speed, accel=accel, wait=True)
                counts = self._get_encoder_counts(dwell=True)
                neg_values.append(counts)
                self.log_always("%s- counts: %d" % (UI_SPACE*2, counts))

                if counts == 0: break
                test_speed += speed_incr

            mean_pos = self._sample_stats(pos_values)['mean']
            mean_neg = self._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2

            if mean == 0:
                self.log_always("No counts measured. Ensure a tool was selected with filament gripped before running calibration and that your encoder is working properly")
                return

            resolution = length / mean
            old_result = mean * self.encoder_sensor.get_resolution()

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self._sample_stats(neg_values)
            self.log_always(msg)

            # Sanity check to ensure all teeth are reflecting / being counted. 20% tolerance
            if (abs(resolution - self.encoder_sensor.get_resolution()) / self.encoder_sensor.get_resolution()) > 0.2:
                self.log_always("Warning: Encoder is not detecting the expected number of counts based on CAD parameters which may indicate an issue")

            msg = "Before calibration measured length: %.2fmm" % old_result
            msg += "\nCalculated resolution of the encoder: %.6fmm (currently: %.6f)" % (resolution, self.encoder_sensor.get_resolution())
            self.log_always(msg)

            if save:
                self.encoder_sensor.set_resolution(resolution)
                self.save_variable(self.VARS_MMU_ENCODER_RESOLUTION, round(resolution, 6), write=True)
                self.log_always("Encoder calibration has been saved")
                self.calibration_status |= self.CALIBRATED_ENCODER

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of encoder failed. Aborting, because:\n%s" % str(ee))
        finally:
            if mean == 0:
                self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)

    # Calibrated bowden length is always from chosen gate homing point to the entruder gears
    # It can be adjusted if sensor setup changes post calibration, must consider:
    #   gate_endstop_to_encoder    .. potential dead space from gate sensor to encoder
    #   toolhead_entry_to_extruder .. distance for extruder entry sensor to extruder gears
    def _calibrate_bowden_length_auto(self, approximate_length, extruder_homing_max, repeats, save=True):

        # Can't allow "none" endstop during calibration so temporarily change
        endstop = self.extruder_homing_endstop
        self.extruder_homing_endstop = self.ENDSTOP_EXTRUDER_COLLISION if endstop == self.ENDSTOP_EXTRUDER_NONE else self.extruder_homing_endstop
        try:
            self.log_always("Calibrating bowden length for gate %d (automatic method) using %s as gate reference point" % (self.gate_selected, self._gate_homing_string()))
            reference_sum = spring_max = 0.
            successes = 0

            for i in range(repeats):
                self._initialize_filament_position(dwell=True)
                self._load_gate(allow_retry=False)
                self._load_bowden(approximate_length)
                self.log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                self._home_to_extruder(extruder_homing_max)
                actual = self._get_filament_position() - self.gate_parking_distance
                measured = self.get_encoder_distance(dwell=True) + self._get_encoder_dead_space()
                spring = self.selector.filament_release(measure=True) if self.has_encoder() else 0.
                reference = actual - spring

                # When homing using collision, we expect the filament to spring back.
                if not (endstop == self.ENDSTOP_EXTRUDER_COLLISION and spring == 0.):
                    msg = "Pass #%d: Filament homed to extruder after %.1fmm movement" % (i+1, actual)
                    if self.has_encoder():
                        msg += "\n(encoder measured %.1fmm, filament sprung back %.1fmm)" % (measured - self.gate_parking_distance, spring)
                    self.log_always(msg)
                    reference_sum += reference
                    spring_max = max(spring, spring_max)
                    successes += 1
                else:
                    # No spring means we haven't reliably homed
                    self.log_always("Failed to detect a reliable home position on this attempt")

                self._initialize_filament_position(True)
                self._unload_bowden(reference)
                self._unload_gate()

            if successes > 0:
                average_reference = reference_sum / successes
                clog_detection_length = (average_reference * 2) / 100. + spring_max # 2% of bowden length plus spring seems to be good starting point
                msg = "Recommended calibration bowden length is %.1fmm" % average_reference
                if self.has_encoder() and self.enable_clog_detection:
                    msg += ". Clog detection length: %.1fmm" % clog_detection_length
                self.log_always(msg)

                if save:
                    self._save_bowden_length(self.gate_selected, average_reference, endstop=self.gate_homing_endstop)
                    self.save_variable(self.VARS_MMU_CALIB_CLOG_LENGTH, round(clog_detection_length, 1))
                    if self.has_encoder():
                        self.encoder_sensor.set_clog_detection_length(clog_detection_length)
                        self.log_always("Calibrated bowden and clog detection length have been saved")
                    else:
                        self.log_always("Calibrated bowden length has been saved")
                    self._write_variables()
            else:
                self.log_error("All %d attempts at homing failed. MMU needs some adjustments!" % repeats)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of bowden length on gate %d failed. Aborting, because:\n%s" % (self.gate_selected, str(ee)))
        finally:
            self.extruder_homing_endstop = endstop
            self._auto_filament_grip()

    def _calibrate_bowden_length_manual(self, approx_bowden_length, save=True):
        try:
            self.log_always("Calibrating bowden length for gate %d (manual method) using %s as gate reference point" % (self.gate_selected, self._gate_homing_string()))
            self._set_filament_direction(self.DIRECTION_UNLOAD)
            self.selector.filament_drive()
            self.log_always("Finding %s endstop position..." % self.gate_homing_endstop)
            homed = False

            if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
                with self._require_encoder():
                    success = self._reverse_home_to_encoder(approx_bowden_length)
                    if success:
                        actual,_,_ = success
                        homed = True

            else: # Gate sensor... ENDSTOP_GATE is shared, but ENDSTOP_POST_GATE_PREFIX is specific
                actual,homed,_,_ = self.trace_filament_move("Reverse homing to gate sensor", -approx_bowden_length, motor="gear", homing_move=-1, endstop_name=self._get_gate_endstop_name())

            if homed:
                actual = abs(actual)
                clog_detection_length = (actual * 2) / 100. # 2% of bowden length
                self.log_always("Recommended calibration bowden length is %.1fmm" % actual)
                if save:
                    self._save_bowden_length(self.gate_selected, actual, endstop=self.gate_homing_endstop)
                    self.save_variable(self.VARS_MMU_CALIB_CLOG_LENGTH, round(clog_detection_length, 1))
                    if self.has_encoder():
                        self.encoder_sensor.set_clog_detection_length(clog_detection_length)
                        self.log_always("Bowden calibration and clog detection length have been saved")
                    else:
                        self.log_always("Bowden calibration length has been saved")
                    self._write_variables()

                self._unload_gate() # Use real method to park filament
            else:
                raise MmuError("Calibration of bowden length failed. Did not home to gate sensor after moving %.1fmm" % approx_bowden_length)
        finally:
            self._auto_filament_grip()

    def _calibrate_gate(self, gate, length, repeats, save=True):
        try:
            pos_values, neg_values = [], []
            self.select_tool(gate) # TODO: Probably should be select_gate() and not need the TTG map reset in caller
            self._load_gate(allow_retry=False)
            self.log_always("%s gate %d over %.1fmm..." % ("Calibrating" if (gate > 0 and save) else "Validating calibration of", gate, length))

            if gate == 0:
                self.log_always("Gate 0 is calibrated with MMU_CALIBRATE_GEAR and manual measurement, so this will run as a validation that encoder is calibrated correctly")

            for _ in range(repeats):
                self._initialize_filament_position(dwell=True)
                _,_,measured,delta = self.trace_filament_move("Calibration load movement", length, encoder_dwell=True)
                pos_values.append(measured)
                self.log_always("%s+ measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), self._get_encoder_counts(dwell=None)))
                self._initialize_filament_position(dwell=True)
                _,_,measured,delta = self.trace_filament_move("Calibration unload movement", -length, encoder_dwell=True)
                neg_values.append(measured)
                self.log_always("%s- measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), self._get_encoder_counts(dwell=None)))

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min).2f max=%(max).2f range=%(range).2f" % self._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min).2f max=%(max).2f range=%(range).2f" % self._sample_stats(neg_values)
            self.log_always(msg)

            mean_pos = self._sample_stats(pos_values)['mean']
            mean_neg = self._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2
            ratio = mean / length
            current_rd = self.gear_rail.steppers[0].get_rotation_distance()[0]
            new_rd = round(ratio * current_rd, 6)

            self.log_always("Calibration move of %d x %.1fmm, average encoder measurement: %.1fmm - Ratio is %.6f" % (repeats * 2, length, mean, ratio))
            self.log_always("Calculated gate %d rotation_distance: %.6f (currently: %.6f)" % (gate, new_rd, self.rotation_distances[gate]))
            if gate != 0: # Gate 0 is not calibrated, it is the reference and set with MMU_CALIBRATE_GEAR
                gate0_rd = self.rotation_distances[0]
                tolerance_range = (gate0_rd - gate0_rd * 0.2, gate0_rd + gate0_rd * 0.2) # Allow 20% variation from gate 0
                if tolerance_range[0] <= new_rd < tolerance_range[1]:
                    if save:
                        self._set_rotation_distance(new_rd)
                        self.rotation_distances[gate] = new_rd
                        self.save_variable(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, write=True)
                        self.log_always("Calibration for gate %d has been saved" % gate)
                else:
                    self.log_always("Calibration ignored because it is not considered valid (>20% difference from gate 0)")
            self._unload_gate()
            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for gate %d failed. Aborting, because: %s" % (gate, str(ee)))
        finally:
            self._auto_filament_grip()

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
    def _probe_toolhead(self, cold_temp=70, probe_depth=100, sensor_homing=50):
        # Ensure extruder is COLD
        self.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % self.extruder_name)
        current_temp = self.printer.lookup_object(self.extruder_name).get_status(0)['temperature']
        if current_temp > cold_temp:
            self.log_always("Waiting for extruder to cool")
            self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=%s MINIMUM=0 MAXIMUM=%d" % (self.extruder_name, cold_temp))

        # Enable the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        ge = stepper_enable.lookup_enable(self.mmu_extruder_stepper.stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        # Reliably force filament to the nozzle
        self.selector.filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", self.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
        if not fhomed:
            raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.toolhead_homing_max)
        self.selector.filament_release()
        actual,_,_,_ = self.trace_filament_move("Forcing filament to nozzle", probe_depth, motor="extruder")

        # Measure 'toolhead_sensor_to_nozzle'
        self.selector.filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Reverse homing to toolhead sensor", -probe_depth, motor="gear+extruder", homing_move=-1, endstop_name=self.ENDSTOP_TOOLHEAD)
        if fhomed:
            toolhead_sensor_to_nozzle = -actual
            self.log_always("Measured toolhead_sensor_to_nozzle: %.1f" % toolhead_sensor_to_nozzle)
        else:
            raise MmuError("Failed to reverse home to toolhead sensor")

        # Move to extruder extrance again
        self.selector.filament_release()
        actual,_,_,_ = self.trace_filament_move("Moving to extruder entrance", -(probe_depth - toolhead_sensor_to_nozzle), motor="extruder")

        # Measure 'toolhead_extruder_to_nozzle'
        self.selector.filament_drive()
        actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", self.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
        if fhomed:
            toolhead_extruder_to_nozzle = actual + toolhead_sensor_to_nozzle
            self.log_always("Measured toolhead_extruder_to_nozzle: %.1f" % toolhead_extruder_to_nozzle)
        else:
            raise MmuError("Failed to home to toolhead sensor")

        toolhead_entry_to_extruder = 0.
        if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
            # Retract clear of extruder sensor and then home in "extrude" direction
            actual,fhomed,_,_ = self.trace_filament_move("Reverse homing to extruder entry sensor", -(sensor_homing + toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle), motor="gear+extruder", homing_move=-1, endstop_name=self.ENDSTOP_EXTRUDER_ENTRY)
            actual,_,_,_ = self.trace_filament_move("Moving before extruder entry sensor", -20, motor="gear+extruder")
            actual,fhomed,_,_ = self.trace_filament_move("Homing to extruder entry sensor", 40, motor="gear+extruder", homing_move=1, endstop_name=self.ENDSTOP_EXTRUDER_ENTRY)

            # Measure to toolhead sensor and thus derive 'toolhead_entry_to_extruder'
            if fhomed:
                actual,fhomed,_,_ = self.trace_filament_move("Homing to toolhead sensor", sensor_homing, motor="gear+extruder", homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
                if fhomed:
                    toolhead_entry_to_extruder = actual - (toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle)
                    self.log_always("Measured toolhead_entry_to_extruder: %.1f" % toolhead_entry_to_extruder)
            else:
                raise MmuError("Failed to reverse home to toolhead sensor")

        # Unload and re-park filament
        self.selector.filament_release()
        actual,_,_,_ = self.trace_filament_move("Moving to extruder entrance", -sensor_homing, motor="extruder")

        return toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle, toolhead_entry_to_extruder


### CALIBRATION GCODE COMMANDS ###################################################

    cmd_MMU_CALIBRATE_GEAR_help = "Calibration routine for gear stepper rotational distance"
    def cmd_MMU_CALIBRATE_GEAR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        length = gcmd.get_float('LENGTH', 100., above=50.)
        measured = gcmd.get_float('MEASURED', -1, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        gate = self.gate_selected if self.gate_selected >= 0 else 0

        if reset:
            self.rotation_distances = [self.default_rotation_distance] * self.num_gates
            self._set_rotation_distance(self.default_rotation_distance)
            self.save_variable(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, write=True)
            self.log_always("Gear calibration for all gates has been reset")
            self.calibration_status &= ~self.CALIBRATED_GEAR_0
            self.calibration_status &= ~self.CALIBRATED_GEAR_RDS

        elif measured > 0:
            current_rd = self.gear_rail.steppers[0].get_rotation_distance()[0]
            new_rd = round(current_rd * measured / length, 6)
            self.log_always("Gear stepper 'rotation_distance' calculated to be %.6f (currently: %.6f" % (new_rd, current_rd))
            if save:
                all_gates = False
                if not self.mmu_machine.variable_rotation_distances or (gate == 0 and self.rotation_distances[0] == 0.):
                    # Initial calibration on gate 0 sets all gates as auto calibration starting point
                    self.rotation_distances = [new_rd] * self.num_gates
                    all_gates = True
                else:
                    self.rotation_distances[gate] = new_rd
                self._set_rotation_distance(new_rd)
                self.save_variable(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, write=True)
                self.log_always("Gear calibration for %s has been saved" % ("all gates" if all_gates else "gate %d" % gate))

                # This feature can be used to calibrate any gate gear but gate 0 is mandatory
                if self.rotation_distances[0] != -1:
                    self.calibration_status |= self.CALIBRATED_GEAR_0
                if not any(x == -1 for x in self.rotation_distances):
                    self.calibration_status |= self.CALIBRATED_GEAR_RDS
        else:
            raise gcmd.error("Must specify 'MEASURED=' and optionally 'LENGTH='")


    # Start: Assumes filament is loaded through encoder
    # End: Does not eject filament at end (filament same as start)
    cmd_MMU_CALIBRATE_ENCODER_help = "Calibration routine for the MMU encoder"
    def cmd_MMU_CALIBRATE_ENCODER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self._check_has_encoder(): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0, check_gates=[self.gate_selected]): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        speed = gcmd.get_float('SPEED', self.gear_from_buffer_speed, minval=10.)
        accel = gcmd.get_float('ACCEL', self.gear_from_buffer_accel, minval=10.)
        min_speed = gcmd.get_float('MINSPEED', speed, above=0.)
        max_speed = gcmd.get_float('MAXSPEED', speed, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        advance = 60. # Ensure filament is in encoder even if not loaded by user
        try:
            with self._require_encoder():
                self.selector.filament_drive()
                self.calibrating = True
                _,_,measured,_ = self.trace_filament_move("Checking for filament", advance)
                if measured < self.encoder_min:
                    raise MmuError("Filament not detected in encoder. Ensure filament is available and try again")
                self._calibrate_encoder(length, repeats, speed, min_speed, max_speed, accel, save)
                _,_,_,_ = self.trace_filament_move("Parking filament", -advance)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

    # Start: Will home selector, select gate 0
    # End: Filament will unload
    cmd_MMU_CALIBRATE_BOWDEN_help = "Calibration of reference bowden length for selected gate"
    def cmd_MMU_CALIBRATE_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_gate_not_valid(): return

        manual = bool(gcmd.get_int('MANUAL', 0, minval=0, maxval=1))
        if not self.has_encoder() and not manual:
            self.log_always("No encoder available. Use manual calibration method:\nWith gate selected, manually load filament all the way to the extruder gear\nThen run 'MMU_CALIBRATE_BOWDEN MANUAL=1 BOWDEN_LENGTH=xxx'\nWhere BOWDEN_LENGTH is greater than your real length")
            return
        if manual:
            if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_SELECTOR, check_gates=[self.gate_selected]): return
        else:
            if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR, check_gates=[self.gate_selected]): return

        approx_bowden_length = gcmd.get_float('BOWDEN_LENGTH', above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        extruder_homing_max = gcmd.get_float('HOMING_MAX', 150, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)

        try:
            self.calibrating = True
            if manual:
                self._calibrate_bowden_length_manual(approx_bowden_length, save)
            else:
                # Automatic method with encoder
                self._reset_ttg_map() # To force tool = gate
                self._unload_tool()
                with self._require_encoder():
                    self._calibrate_bowden_length_auto(approx_bowden_length, extruder_homing_max, repeats, save)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

    # Start: Will home selector, select gate 0 or required gate
    # End: Filament will unload
    cmd_MMU_CALIBRATE_GATES_help = "Optional calibration of individual MMU gate"
    def cmd_MMU_CALIBRATE_GATES(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        auto = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        if gate == -1 and not auto:
            raise gcmd.error("Must specify 'GATE=' or 'ALL=1' for all gates")
        if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR, check_gates=[gate] if gate != -1 else None): return

        try:
            self._reset_ttg_map() # To force tool = gate
            self._unload_tool()
            self.calibrating = True
            with self._require_encoder():
                if gate == -1:
                    self.log_always("Start the complete calibration of ancillary gates...")
                    for gate in range(self.num_gates - 1):
                        self._calibrate_gate(gate + 1, length, repeats, save=save)
                    self.log_always("Phew! End of auto gate calibration")
                else:
                    self._calibrate_gate(gate, length, repeats, save=(save and gate != 0))
            if not any(x == -1 for x in self.rotation_distances[1:]):
                self.calibration_status |= self.CALIBRATED_GEAR_RDS
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

    # Start: Test gate should already be selected
    # End: Filament will unload
    cmd_MMU_CALIBRATE_TOOLHEAD_help = "Automated measurement of key toolhead parameters"
    def cmd_MMU_CALIBRATE_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR|self.CALIBRATED_BOWDENS, check_gates=[self.gate_selected]): return
        if not self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
            raise gcmd.error("Sorry this feature requires a toolhead sensor")
        clean = gcmd.get_int('CLEAN', 0, minval=0, maxval=1)
        cut = gcmd.get_int('CUT', 0, minval=0, maxval=1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        line = "-----------------------------------------------\n"

        msg = "Reminder:\n"
        msg += "1) 'CLEAN=1' with clean extruder for: toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle (and toolhead_entry_to_extruder)\n"
        msg += "2) No flags with dirty extruder (no cut tip) for: toolhead_residual_filament (and toolhead_entry_to_extruder)\n"
        msg += "3) 'CUT=1' holding blade in for: variable_blade_pos\n"
        msg += "Desired gate should be selected but the filament unloaded\n"
        self.log_always(msg)

        if cut:
            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
            if gcode_macro is None:
                raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)
            gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.form_tip_macro, gcode_macro)
            if not ('blade_pos' in gcode_vars.variables and 'retract_length' in gcode_vars.variables):
                raise gcmd.error("Filament tip forming macro '%s' does not look like a cutting macro!" % self.form_tip_macro)

        try:
            self.calibrating = True
            self._initialize_filament_position(dwell=True)
            self._load_gate(allow_retry=False)
            self._load_bowden()
            self._home_to_extruder(self.extruder_homing_max)

            if cut:
                self.log_always("Measuring blade cutter postion (with filament fragment)...")
                tetn, tstn, tete = self._probe_toolhead()
                # Blade position is the difference between empty and extruder with full cut measurements for sensor to nozzle
                vbp = self.toolhead_sensor_to_nozzle - tstn
                msg = line
                if abs(vbp - self.toolhead_residual_filament) < 5:
                    self.log_error("Measurements did not make sense. Looks like probing went past the blade pos!\nAre you holding the blade closed or have cut filament in the extruder?")
                else:
                    msg += "Calibration Results (cut tip):\n"
                    msg += "> variable_blade_pos: %.1f (currently: %.1f)\n" % (vbp, gcode_vars.variables['blade_pos'])
                    msg += "> variable_retract_length: %.1f-%.1f, recommend: %.1f (currently: %.1f)\n" % (self.toolhead_residual_filament + self.toolchange_retract, vbp, vbp - 5., gcode_vars.variables['retract_length'])
                    msg += line
                    self.log_always(msg)
                    if save:
                        self.log_always("New calibrated blade_pos and retract_length active until restart. Update mmu_macro_vars.cfg to persist")
                        gcode_vars.variables['blade_pos'] = vbp
                        gcode_vars.variables['retract_length'] = vbp - 5.

            elif clean:
                self.log_always("Measuring clean toolhead dimensions after cold pull...")
                tetn, tstn, tete = self._probe_toolhead()
                msg = line
                msg += "Calibration Results (clean nozzle):\n"
                msg += "> toolhead_extruder_to_nozzle: %.1f (currently: %.1f)\n" % (tetn, self.toolhead_extruder_to_nozzle)
                msg += "> toolhead_sensor_to_nozzle: %.1f (currently: %.1f)\n" % (tstn, self.toolhead_sensor_to_nozzle)
                if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                    msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
                msg += line
                self.log_always(msg)
                if save:
                    self.log_always("New toolhead calibration active until restart. Update mmu_parameters.cfg to persist settings")
                    self.toolhead_extruder_to_nozzle = round(tetn, 1)
                    self.toolhead_sensor_to_nozzle = round(tstn, 1)
                    self.toolhead_entry_to_extruder = round(tete, 1)

            else:
                self.log_always("Measuring dirty toolhead dimensions (with filament residue)...")
                tetn, tstn, tete = self._probe_toolhead()
                # Ooze reduction is the difference between empty and dirty measurements for sensor to nozzle
                tor = self.toolhead_sensor_to_nozzle - tstn
                msg = line
                msg += "Calibration Results (dirty nozzle):\n"
                msg += "> toolhead_residual_filament: %.1f (currently: %.1f)\n" % (tor, self.toolhead_residual_filament)
                if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                    msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
                msg += line
                self.log_always(msg)
                if save:
                    self.log_always("New calibrated ooze reduction active until restart. Update mmu_parameters.cfg to persist")
                    self.toolhead_residual_filament = round(tor, 1)
                    self.toolhead_entry_to_extruder = round(tete, 1)

            # Unload and park filament
            self._unload_bowden()
            self._unload_gate()
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False


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
        self.pending_spool_id = None
        return self.reactor.NEVER

    def _check_pending_spool_id(self, gate):
        if self.pending_spool_id is not None:
            self.log_info("Spool ID: %s automatically applied to Gate %d" % (self.pending_spool_id, gate))
            self.gate_spool_id[gate] = self.pending_spool_id
            self._pending_spool_id_handler(0) # Prevent resue
            self._spoolman_update_filaments(gate) # Request update of material & color from Spoolman

    def _handle_idle_timeout_printing(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "printing")

    def _handle_idle_timeout_ready(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "ready")

    def _handle_idle_timeout_idle(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "idle")

    def _setup_sync_feedback(self):
        self.sync_feedback_timer = self.reactor.register_timer(self._update_sync_feedback)

    # Gear/Extruder sync feedback state should be -1 (expanded) and 1 (compressed)
    # or can be a proportional float value between -1.0 and 1.0
    def _handle_sync_feedback(self, eventtime, state):
        if not self.is_enabled: return
        self.log_trace("Got sync force feedback update. State: %s" % state)
        if abs(state) <= 1:
            self.sync_feedback_last_state = float(state)
            if self.sync_feedback_enable and self.sync_feedback_operational:
                self._update_sync_multiplier()

    def _handle_mmu_synced(self):
        if not self.is_enabled: return
        self.log_info("Synced MMU to extruder%s" % (" (sync feedback activated)" if self.sync_feedback_enable else ""))
        if not self.sync_feedback_operational:
            # Enable sync feedback
            self.sync_feedback_operational = True
            self.reactor.update_timer(self.sync_feedback_timer, self.reactor.NOW)
            self._update_sync_starting_state()

    def _handle_mmu_unsynced(self):
        if not self.is_enabled: return
        self.log_info("Unsynced MMU from extruder%s" % (" (sync feedback deactivated)" if self.sync_feedback_enable else ""))
        if self.sync_feedback_operational:
            # Disable sync feedback
            self.reactor.update_timer(self.sync_feedback_timer, self.reactor.NEVER)
            self.sync_feedback_operational = False
            self.sync_feedback_last_direction = self.SYNC_STATE_NEUTRAL
            self.log_trace("Reset sync multiplier")
            self._set_rotation_distance(self._get_rotation_distance(self.gate_selected))

    def _update_sync_feedback(self, eventtime):
        if self.is_enabled:
            estimated_print_time = self.printer.lookup_object('mcu').estimated_print_time(eventtime)
            extruder = self.toolhead.get_extruder()
            pos = extruder.find_past_position(estimated_print_time)
            past_pos = extruder.find_past_position(max(0., estimated_print_time - self.SYNC_POSITION_TIMERANGE))
            if abs(pos - past_pos) >= self.SYNC_POSITION_MIN_DELTA:
                prev_direction = self.sync_feedback_last_direction
                self.sync_feedback_last_direction = self.DIRECTION_LOAD if pos > past_pos else self.DIRECTION_UNLOAD if pos < past_pos else 0
                if self.sync_feedback_last_direction != prev_direction:
                    d = self.sync_feedback_last_direction
                    self.log_trace("New sync direction: %s" % ('extrude' if d == self.DIRECTION_LOAD else 'retract' if d == self.DIRECTION_UNLOAD else 'static'))
                    self._update_sync_multiplier()
        return eventtime + self.SYNC_FEEDBACK_INTERVAL

    def _update_sync_multiplier(self):
        if not self.sync_feedback_enable or not self.sync_feedback_operational: return
        if self.sync_feedback_last_direction == self.SYNC_STATE_NEUTRAL:
            multiplier = 1.
        else:
            go_slower = lambda s, d: abs(s - d) < abs(s + d)
            if go_slower(self.sync_feedback_last_state, self.sync_feedback_last_direction):
                # Expanded when extruding or compressed when retracting, so decrease the rotation distance of gear stepper to speed it up
                multiplier = 1. - (abs(1. - self.sync_multiplier_low) * abs(self.sync_feedback_last_state))
            else:
                # Compressed when extruding or expanded when retracting, so increase the rotation distance of gear stepper to slow it down
                multiplier = 1. + (abs(1. - self.sync_multiplier_high) * abs(self.sync_feedback_last_state))
        self.log_trace("Updated sync multiplier: %.4f" % multiplier)
        self._set_rotation_distance(self._get_rotation_distance(self.gate_selected) / multiplier)

    # Ensure correct sync_feedback starting assumption by generating a fake event
    def _update_sync_starting_state(self):
        if not self.mmu_sensors: return
        eventtime = self.reactor.monotonic()
        sss = self.SYNC_STATE_NEUTRAL

        if self.mmu_sensors.has_tension_switch and not self.mmu_sensors.has_compression_switch:
            sss = self.SYNC_STATE_EXPANDED if self.mmu_sensors.get_status(eventtime)[self.SWITCH_SYNC_FEEDBACK_TENSION] else self.SYNC_STATE_COMPRESSED
        elif self.mmu_sensors.has_compression_switch and not self.mmu_sensors.has_tension_switch:
            sss = self.SYNC_STATE_COMPRESSED if self.mmu_sensors.get_status(eventtime)[self.SWITCH_SYNC_FEEDBACK_COMPRESSION] else self.SYNC_STATE_EXPANDED
        elif self.mmu_sensors.has_compression_switch and self.mmu_sensors.has_tension_switch:
            state_expanded = self.mmu_sensors.get_status(eventtime)[self.SWITCH_SYNC_FEEDBACK_TENSION]
            state_compressed = self.mmu_sensors.get_status(eventtime)[self.SWITCH_SYNC_FEEDBACK_COMPRESSION]
            if state_expanded and state_compressed:
                self.log_error("Both expanded and compressed sync feedback sensors are triggered at the same time. Check hardware!")
            elif state_expanded:
                sss = self.SYNC_STATE_EXPANDED
            elif state_compressed:
                sss = self.SYNC_STATE_COMPRESSED
            else:
                pass # Assume neutral

        self._handle_sync_feedback(eventtime, sss)
        self.log_trace("Set initial sync feedback state to: %s" % self._get_sync_feedback_string())

    def is_printer_printing(self):
        return bool(self.print_stats and self.print_stats.state == "printing")

    def is_printer_paused(self):
        return self.pause_resume.is_paused

    def is_printing(self, force_in_print=False): # Actively printing and not paused
        return self.print_state in ["started", "printing"] or force_in_print or self.test_force_in_print

    def is_in_print(self, force_in_print=False): # Printing or paused
        return bool(self.print_state in ["printing", "pause_locked", "paused"] or force_in_print or self.test_force_in_print)

    def is_mmu_paused(self): # The MMU is paused
        return self.print_state in ["pause_locked", "paused"]

    def is_mmu_paused_and_locked(self): # The MMU is paused (and locked)
        return self.print_state in ["pause_locked"]

    def is_in_endstate(self):
        return self.print_state in ["complete", "cancelled", "error", "ready", "standby", "initialized"]

    def is_in_standby(self):
        return self.print_state in ["standby"]

    def _wakeup(self):
        if self.is_in_standby():
            self._set_print_state("idle")

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

        if self.print_stats and self.print_start_detection:
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
                            self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_START"))
                elif new_state in ["complete", "error"] and event_type == "ready":
                    self.log_trace("Automatically detected JOB %s, print_stats=%s, current mmu print_state=%s" % (new_state.upper(), new_state, self.print_state))
                    if new_state == "error":
                        self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=error AUTOMATIC=1"))
                    else:
                        self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=complete AUTOMATIC=1"))
                self.last_print_stats = dict(new_ps)

        # Capture transition to standby
        if event_type == "idle" and self.print_state != "standby":
            self.reactor.register_callback(lambda pt: self._print_event("MMU_PRINT_END STATE=standby AUTOMATIC=1"))

    def _print_event(self, command):
        try:
            self.gcode.run_script(command)
        except Exception:
            logging.exception("Error running job state initializer/finalizer")

    # MMU job state machine: initialized|ready|started|printing|complete|cancelled|error|pause_locked|paused|standby
    def _set_print_state(self, print_state, call_macro=True):
        if print_state != self.print_state:
            idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout
            self.log_debug("Job State: %s -> %s (MMU State: Encoder: %s, Synced: %s, Paused temp: %s, Resume to state: %s, Position saved for: %s, pause_resume: %s, Idle timeout: %.2fs)"
                    % (self.print_state.upper(), print_state.upper(), self._get_encoder_state(), self.mmu_toolhead.is_gear_synced_to_extruder(), self.paused_extruder_temp,
                        self.resume_to_state, self.saved_toolhead_operation, self.is_printer_paused(), idle_timeout))
            if call_macro:
                if self.printer.lookup_object("gcode_macro %s" % self.print_state_changed_macro, None) is not None:
                    self._wrap_gcode_command("%s STATE='%s' OLD_STATE='%s'" % (self.print_state_changed_macro, print_state, self.print_state))
            self.print_state = print_state

    # If this is called automatically when printing starts. The pre_start_only operations are performed on an idle_timeout
    # event so cannot block.  The remainder of moves will be called from the queue but they will be called early so
    # don't do anything that requires operating toolhead kinematics (we might not even be homed yet)
    def _on_print_start(self, pre_start_only=False):
        if self.print_state not in ["started", "printing"]:
            self.log_trace("_on_print_start(->started)")
            self._clear_saved_toolhead_position()
            self.paused_extruder_temp = None
            self._reset_job_statistics() # Reset job stats but leave persisted totals alone
            self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self.is_handling_runout = False
            self._clear_slicer_tool_map()
            self._enable_runout() # Enable runout/clog detection while printing
            self._initialize_encoder(dwell=None) # Encoder 0000
            self._set_print_state("started", call_macro=False)

        if not pre_start_only and self.print_state not in ["printing"]:
            self.log_trace("_on_print_start(->printing)")
            self._sync_gear_to_extruder(self.sync_to_extruder, grip=True, current=True)
            self._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=min_lifted_z VALUE=0" % self.park_macro) # Sequential printing movement "floor"
            self._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=False" % self.park_macro)
            msg = "Happy Hare initialized ready for print"
            if self.filament_pos == self.FILAMENT_POS_LOADED:
                msg += " (initial tool T%s loaded)" % self.tool_selected
            else:
                msg += " (no filament preloaded)"
            if self.ttg_map != self.default_ttg_map:
                msg += "\nWarning: Non default TTG map in effect"
            self.log_info(msg)
            self._set_print_state("printing")

    # Hack: Force state transistion to printing for any early moves if _MMU_PRINT_START not yet run
    def _fix_started_state(self):
        if self.is_printer_printing() and not self.is_in_print():
            self._wrap_gcode_command("MMU_PRINT_START")

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
            self._disable_runout() # Disable runout/clog detection after print

            if self.printer.lookup_object("idle_timeout").idle_timeout != self.default_idle_timeout:
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout) # Restore original idle_timeout
            self._set_print_state(state) # Must be before the unsyncing below for grip (servo) to operate
            self._sync_gear_to_extruder(False, grip=True)
        if state == "standby" and not self.is_in_standby():
            self._set_print_state(state)
        self._clear_macro_state()

    def handle_mmu_error(self, reason, force_in_print=False):
        self._fix_started_state() # Get out of 'started' state before transistion to pause

        run_pause_macro = recover_pos = send_event = False
        if self.is_in_print(force_in_print):
            if not self.is_mmu_paused():
                self.resume_to_state = 'printing' if self.is_in_print() else 'ready'
                self.reason_for_pause = reason
                self._display_mmu_error()
                self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp
                self.log_trace("Saved desired extruder temperature: %.1f%sC" % (self.paused_extruder_temp, UI_DEGREE))
                self._track_pause_start()
                self.log_trace("Extruder heater will be disabled in %s" % self._seconds_to_string(self.disable_heater))
                self.reactor.update_timer(self.hotend_off_timer, self.reactor.monotonic() + self.disable_heater) # Set extruder off timer
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.timeout_pause) # Set alternative pause idle_timeout
                self._disable_runout() # Disable runout/clog detection while in pause state
                self._save_toolhead_position_and_park('pause') # if already paused this is a no-op
                self._wrap_gcode_command(self.error_macro)
                run_pause_macro = not self.is_printer_paused()
                self._set_print_state("pause_locked")
                send_event = True
                recover_pos = self.filament_recovery_on_pause
            else:
                self.log_error("MMU issue detected whilst printer is paused\nReason: %s" % reason)
                self._wrap_gcode_command(self.error_macro)
                recover_pos = self.filament_recovery_on_pause

        else: # Not in a print (standalone operation)
            self.log_error("MMU issue: %s" % reason)
            # Restore original position if parked
            if self.saved_toolhead_operation:
                self._restore_toolhead_position(self.saved_toolhead_operation)

        # Be deliberate about order of these tasks
        if run_pause_macro:
            self._wrap_gcode_command(self.pause_macro)

        if recover_pos:
            self.recover_filament_pos(message=True)

        # Default to unsynced on error. Will be restored on resume/continue_printing
        self._sync_gear_to_extruder(False, grip=True)

        if send_event:
            self.printer.send_event("mmu:mmu_paused") # Notify MMU paused event

    # Displays MMU error/pause as pop-up dialog and/or via console
    def _display_mmu_error(self):
        msg= "Print%s paused" % (" was already" if self.is_printer_paused() else " will be")
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.error_dialog_macro, None)
        if self.show_error_dialog and dialog_macro is not None:
            # Klipper doesn't handle string quoting so strip problematic characters
            reason = self.reason_for_pause.replace("\n", ". ")
            for c in "#;'":
                reason = reason.replace(c, "")
            self._wrap_gcode_command('%s MSG="%s" REASON="%s"' % (self.error_dialog_macro, msg, reason))
        self.log_error("MMU issue detected. %s\nReason: %s" % (msg, self.reason_for_pause))
        self.log_always("After fixing, call RESUME to continue printing (MMU_UNLOCK to restore temperature)")

    def _clear_mmu_error_dialog(self):
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.error_dialog_macro, None)
        if self.show_error_dialog and dialog_macro is not None:
            self._wrap_gcode_command('RESPOND TYPE=command MSG="action:prompt_end"')

    def _mmu_unlock(self):
        if self.is_mmu_paused():
            self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout)
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
            self._enable_runout() # Enable runout/clog detection while printing
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

            # Restablish syncing state and grip (servo) position
            self._sync_gear_to_extruder(self.sync_to_extruder, grip=True, current=True)

        # Restore print position as final step so no delay
        self._restore_toolhead_position(operation, restore=restore)

        # Ready to continue printing...

    def _clear_macro_state(self):
        if self.printer.lookup_object('gcode_macro %s' % self.clear_position_macro, None) is not None:
            self._wrap_gcode_command(self.clear_position_macro)

    def _save_toolhead_position_and_park(self, operation, next_pos=None):
        eventtime = self.reactor.monotonic()
        homed = self.toolhead.get_status(eventtime)['homed_axes']
        if not self.saved_toolhead_operation:
            # Save toolhead position
            if 'xyz' in homed:
                # This is paranoia so I can be absolutely sure that Happy Hare leaves toolhead the same way when we are done
                gcode_pos = self.gcode_move.get_status(eventtime)['gcode_position']
                toolhead_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                self.log_debug("Saving toolhead gcode state and position (%s) for %s" % (toolhead_gcode_pos, operation))
                self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % self.TOOLHEAD_POSITION_STATE)
                self.saved_toolhead_operation = operation
                self.saved_toolhead_max_accel = self.toolhead.max_accel

                # Record the intended X,Y resume position (this is also passed to the pause/resume restore position in pause is later called)
                if next_pos:
                    self.gcode_move.saved_states[self.TOOLHEAD_POSITION_STATE]['last_position'][:2] = next_pos

                # Make sure we record the current speed/extruder overrides
                if self.tool_selected >= 0:
                    mmu_state = self.gcode_move.saved_states[self.TOOLHEAD_POSITION_STATE]
                    self.tool_speed_multipliers[self.tool_selected] = mmu_state['speed_factor'] * 60.
                    self.tool_extrusion_multipliers[self.tool_selected] = mmu_state['extrude_factor']

                # This will save the print position in the macro and apply park
                self._wrap_gcode_command(self.save_position_macro)
                self._wrap_gcode_command(self.park_macro)
            else:
                self.log_debug("Cannot save toolhead position or z-hop for %s because not homed" % operation)
        else:
            if 'xyz' in homed:
                # Re-apply parking for new operation. This will not change the saved position in macro
                self.saved_toolhead_operation = operation # Update operation in progress
                # Force re-park now because user may not be using HH client_macros. This can result
                # in duplicate calls to parking macro but it is itempotent and will ignore
                self._wrap_gcode_command(self.park_macro)

    def _restore_toolhead_position(self, operation, restore=True):
        eventtime = self.reactor.monotonic()
        if self.saved_toolhead_operation:
            # Inject speed/extruder overrides into gcode state restore data
            if self.tool_selected >= 0:
                mmu_state = self.gcode_move.saved_states[self.TOOLHEAD_POSITION_STATE]
                mmu_state['speed_factor'] = self.tool_speed_multipliers[self.tool_selected] / 60.
                mmu_state['extrude_factor'] = self.tool_extrusion_multipliers[self.tool_selected]

            # If this is the final "restore toolhead position" call then allow macro to restore position, then sanity check
            if not (self.is_mmu_paused() or self.is_printer_paused()) or (operation == "resume" and (self.is_mmu_paused() or self.is_printer_paused())):
                # Controlled by the RESTORE=0 flag to MMU_LOAD, MMU_EJECT, MMU_CHANGE_TOOL (only real use case is final eject)
                if restore:
                    self._wrap_gcode_command(self.restore_position_macro) # Restore macro position and clear saved

                    # Paranoia: no matter what macros do ensure position and state is good. Either last, next or none (current x,y)
                    sequence_vars_macro = self.printer.lookup_object("gcode_macro _MMU_SEQUENCE_VARS", None)
                    travel_speed = 200
                    if sequence_vars_macro:
                        if sequence_vars_macro.variables.get('restore_xy_pos', 'last') == 'none' and self.saved_toolhead_operation in ['toolchange']:
                            # Don't change x,y position on toolchange
                            current_pos = self.gcode_move.get_status(eventtime)['gcode_position']
                            self.gcode_move.saved_states[self.TOOLHEAD_POSITION_STATE]['last_position'][:2] = current_pos[:2]
                        travel_speed = sequence_vars_macro.variables.get('park_travel_speed', travel_speed)
                    gcode_pos = self.gcode_move.saved_states[self.TOOLHEAD_POSITION_STATE]['last_position']
                    display_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                    self.gcode.run_script_from_command("M204 S%d" % self.saved_toolhead_max_accel)
                    self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=%s MOVE=1 MOVE_SPEED=%.1f" % (self.TOOLHEAD_POSITION_STATE, travel_speed))
                    self.log_debug("Ensuring correct gcode state and position (%s) after %s" % (display_gcode_pos, operation))
                    self._clear_saved_toolhead_position()
                    return
                else:
                    # Special case of not restoring so just clear all saved state
                    self._wrap_gcode_command(self.clear_position_macro)
                    self._clear_saved_toolhead_position()
            else:
                pass # Resume will call here again shortly so we can ignore for now
        else:
            # Ensure all saved state is cleared
            self._wrap_gcode_command(self.clear_position_macro)
            self._clear_saved_toolhead_position()

    def _clear_saved_toolhead_position(self):
        self.saved_toolhead_operation = ''
        self.saved_toolhead_max_accel = 0

    def _disable_runout(self):
        enabled = self.runout_enabled
        if enabled:
            self.log_trace("Disabled runout detection")
            if self.has_encoder() and self.encoder_sensor.is_enabled():
                self.encoder_sensor.disable()
            self.sensor_manager.disable_runout(self.gate_selected)
            self.runout_enabled = False
        return enabled

    def _enable_runout(self):
        self.runout_enabled = True
        self.log_trace("Enabled runout detection")
        if self.has_encoder() and not self.encoder_sensor.is_enabled():
            self.encoder_sensor.enable()
        self.sensor_manager.enable_runout(self.gate_selected)

    @contextlib.contextmanager
    def _wrap_suspend_runout(self):
        enabled = self._disable_runout()
        try:
            yield self
        finally:
            if enabled:
                self._enable_runout()

    # To suppress visual filament position
    @contextlib.contextmanager
    def wrap_suppress_visual_log(self):
        log_visual = self.log_visual
        self.log_visual = 0
        try:
            yield self
        finally:
            self.log_visual = log_visual

    def has_encoder(self):
        return self.encoder_sensor is not None and not self.test_disable_encoder

    def _can_use_encoder(self):
        return self.encoder_sensor is not None and (self.encoder_move_validation or self.encoder_force_validation)

    def _check_has_encoder(self):
        if not self.has_encoder():
            self.log_error("No encoder fitted to MMU")
            return True
        return False

    def _get_encoder_state(self):
        if self.has_encoder():
            return "%s" % "Enabled" if self.encoder_sensor.is_enabled() else "Disabled"
        else:
            return "n/a"

    # For all encoder methods, 'dwell' means:
    #   True  - gives klipper a little extra time to deliver all encoder pulses when absolute accuracy is required
    #   False - wait for moves to complete and then read encoder
    #   None  - just read encoder without delay (assumes prior movements have completed)
    # Return 'False' if no encoder fitted
    def _encoder_dwell(self, dwell):
        if self.has_encoder():
            if dwell:
                self.movequeues_dwell(self.encoder_dwell)
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
        if not self.has_encoder():
            raise MmuError("Assertion failure: Encoder required for chosen operation but not present on MMU")
        self.encoder_force_validation = True
        try:
            yield self
        finally:
            self.encoder_force_validation = False

    def get_encoder_distance(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder_sensor.get_distance()
        else:
            return 0.

    def _get_encoder_counts(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder_sensor.get_counts()
        else:
            return 0

    def set_encoder_distance(self, distance, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder_sensor.set_distance(distance)

    def _initialize_encoder(self, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder_sensor.reset_counts()

    def _get_encoder_dead_space(self):
        if self.gate_homing_endstop in [self.ENDSTOP_GATE, self.ENDSTOP_POST_GATE_PREFIX]:
            return self.gate_endstop_to_encoder
        else:
            return 0.

    def _initialize_filament_position(self, dwell=False):
        self._initialize_encoder(dwell=dwell)
        self._set_filament_position()

    def _get_filament_position(self):
        return self.mmu_toolhead.get_position()[1]

    def _set_filament_position(self, position = 0.):
        pos = self.mmu_toolhead.get_position()
        pos[1] = position
        self.mmu_toolhead.set_position(pos)
        return position

    def _set_filament_remaining(self, length):
        self.filament_remaining = length
        self.save_variable(self.VARS_MMU_FILAMENT_REMAINING, round(length, 1), write=True)

    def _set_last_tool(self, tool):
        self._last_tool = tool
        self.save_variable(self.VARS_MMU_LAST_TOOL, tool, write=True)

    def _set_filament_pos_state(self, state, silent=False):
        self.filament_pos = state
        if self.gate_selected != self.TOOL_GATE_BYPASS or state == self.FILAMENT_POS_UNLOADED or state == self.FILAMENT_POS_LOADED:
            self._display_visual_state(silent=silent)

        # Minimal save_variable writes
        if state in [self.FILAMENT_POS_LOADED, self.FILAMENT_POS_UNLOADED]:
            self.save_variable(self.VARS_MMU_FILAMENT_POS, state, write=True)
        elif self.save_variables.allVariables.get(self.VARS_MMU_FILAMENT_POS, 0) != self.FILAMENT_POS_UNKNOWN:
            self.save_variable(self.VARS_MMU_FILAMENT_POS, self.FILAMENT_POS_UNKNOWN, write=True)

    def _set_filament_direction(self, direction):
        self.filament_direction = direction

    def _must_home_to_extruder(self):
        return self.extruder_force_homing or not self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD)

    def check_if_disabled(self):
        if not self.is_enabled:
            self.log_error("MMU is disabled. Please use MMU ENABLE=1 to use")
            return True
        self._wakeup()
        return False

    def check_if_bypass(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.filament_pos not in [self.FILAMENT_POS_UNLOADED]:
            self.log_error("Operation not possible. MMU is currently using bypass. Unload or select a different gate first")
            return True
        return False

    def check_if_not_homed(self):
        if not self.selector.is_homed:
            self.log_error("MMU is not homed")
            return True
        return False

    def check_if_loaded(self):
        if self.filament_pos not in [self.FILAMENT_POS_UNLOADED, self.FILAMENT_POS_UNKNOWN]:
            self.log_error("MMU has filament loaded")
            return True
        return False

    def check_if_gate_not_valid(self):
        if self.gate_selected < 0:
            self.log_error("No MMU gate selected")
            return True
        return False

    # Returns True is required calibration is not complete. Defaults to all gates
    # Params: required = bitmap of checks, check_gates = list of gates to consider
    def check_if_not_calibrated(self, required, silent=False, check_gates=None):

        # First quickly check if everything calibrated
        if not self.calibration_status & required == required:
            if check_gates is None:
                check_gates = list(range(self.num_gates))

            # We have to be more methodical and consider just gates of interest
            msg = ""
            if required & self.CALIBRATED_GEAR_0 and not self.calibration_status & self.CALIBRATED_GEAR_0:
                if self.mmu_machine.variable_rotation_distances:
                    uncalibrated = [gate for gate, value in enumerate(self.rotation_distances) if value == -1 and gate in check_gates]
                    if uncalibrated:
                        msg += "\nUse MMU_CALIBRATE_GEAR (with gate 0 selected)"
                        msg += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))

            if required & self.CALIBRATED_ENCODER and not self.calibration_status & self.CALIBRATED_ENCODER:
                msg += "\nUse MMU_CALIBRATE_ENCODER (with gate 0 selected)"

            if required & self.CALIBRATED_SELECTOR and not self.calibration_status & self.CALIBRATED_SELECTOR:
                uncalibrated = [gate for gate, value in enumerate(self.selector.selector_offsets) if value == -1 and gate in check_gates]
                if uncalibrated:
                    msg += "\nUse MMU_CALIBRATE_SELECTOR to calibrate selector offset on gates: %s" % ",".join(map(str, uncalibrated))

            if required & self.CALIBRATED_BOWDENS and not self.calibration_status & self.CALIBRATED_BOWDENS:
                if self.mmu_machine.variable_bowden_lengths:
                    uncalibrated = [gate for gate, value in enumerate(self.bowden_lengths) if value == -1 and gate in check_gates]
                    if uncalibrated:
                        msg += "\nUse MMU_CALIBRATE_BOWDEN (with gate selected)"
                        msg += " to calibrate bowden length gates: %s" % ",".join(map(str, uncalibrated))
                elif self.bowden_lengths[0] == -1:
                    msg += "\nUse MMU_CALIBRATE_BOWDEN (with gate 0 selected)"

            if required & self.CALIBRATED_GEAR_RDS and not self.calibration_status & self.CALIBRATED_GEAR_RDS:
                if self.mmu_machine.variable_rotation_distances:
                    uncalibrated = [gate for gate, value in enumerate(self.rotation_distances) if value == -1 and gate in check_gates]
                    if uncalibrated:
                        if self.has_encoder():
                            msg += "\nUse MMU_CALIBRATE_GEAR (with gate selected) or MMU_CALIBRATE_GATES GATE=xx"
                            msg += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                        else:
                            msg += "\nUse MMU_CALIBRATE_GEAR (with gate selected)"
                            msg += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                elif self.rotation_distances[0] == -1:
                    msg += "\nUse MMU_CALIBRATE_GEAR (with gate 0 selected)"
            if msg:
                msg = "Prerequsite calibration steps are not complete:" + msg
                if not silent:
                    if silent is None: # Bootup/status use case to avoid looking like error
                        self.log_always("{2}%s{0}" % msg, color=True)
                    else:
                        self.log_error(msg)
                return True
        return False

    def check_if_has_leds(self):
        if not self.has_leds:
            self.log_error("No LEDs configured on MMU")
            return True
        return False

    def check_if_spoolman_enabled(self):
        if self.spoolman_support == self.SPOOLMAN_OFF:
            self.log_error("Spoolman support is currently disabled")
            return True
        return False

    def _gate_homing_string(self):
        return "ENCODER" if self.gate_homing_endstop == self.ENDSTOP_ENCODER else "ENDSTOP '%s'" % self.gate_homing_endstop

    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.extruder_name)
        current_temp = extruder.get_status(0)['temperature']
        current_target_temp = extruder.heater.target_temp
        klipper_minimum_temp = extruder.get_heater().min_extrude_temp
        default_extruder_temp = self.gate_temperature[self.gate_selected] if self.gate_selected >= 0 else self.default_extruder_temp
        self.log_trace("_ensure_safe_extruder_temperature: current_temp=%s, paused_extruder_temp=%s, current_target_temp=%s, klipper_minimum_temp=%s, default_extruder_temp=%s, source=%s" % (current_temp, self.paused_extruder_temp, current_target_temp, klipper_minimum_temp, default_extruder_temp, source))

        if source == "pause":
            new_target_temp = self.paused_extruder_temp if self.paused_extruder_temp is not None else current_temp # Pause temp should not be None
            if self.paused_extruder_temp < klipper_minimum_temp:
                # Don't wait if just messing with cold printer
                wait = False
        elif source == "auto":
            if self.is_mmu_paused():
                # In a pause we always want to restore the temp we paused at
                new_target_temp = self.paused_extruder_temp if self.paused_extruder_temp is not None else current_temp # Pause temp should not be None
                source = "pause"
            elif self.is_printing():
                # While actively printing, we want to defer to the slicer for temperature
                new_target_temp = current_target_temp
                source = "slicer"
            else:
                # Standalone "just messing" case
                if current_target_temp > klipper_minimum_temp:
                    new_target_temp = current_target_temp
                    source = "current"
                else:
                    new_target_temp = default_extruder_temp
                    source = "default"

            if new_target_temp < klipper_minimum_temp:
                # If, for some reason, the target temp is below Klipper's minimum, set to minimum
                # set the target to Happy Hare's default. This strikes a balance between utility
                # and safety since Klipper's min is truly a bare minimum but our default should be
                # a more realistic temperature for safe operation.
                new_target_temp = default_extruder_temp
                source = "minimum"

        if new_target_temp > current_target_temp:
            if source in ["default", "minimum"]:
                # We use error log channel to avoid heating surprise. This will also cause popup in Klipperscreen
                self.log_error("Warning: Automatically heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            else:
                self.log_info("Heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            wait = True # Always wait to warm up

        if new_target_temp > 0:
            self.gcode.run_script_from_command("M104 S%.1f" % new_target_temp)

            # Optionally wait until temperature is stable or at minimum safe temp so extruder can move
            if wait and new_target_temp >= klipper_minimum_temp and abs(new_target_temp - current_temp) > self.extruder_temp_variance:
                with self.wrap_action(self.ACTION_HEATING):
                    self.log_info("Waiting for extruder to reach target (%s) temperature: %.1f%sC" % (source, new_target_temp, UI_DEGREE))
                    self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.1f MAXIMUM=%.1f" % (new_target_temp - self.extruder_temp_variance, new_target_temp + self.extruder_temp_variance))

    def _selected_tool_string(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS:
            return "Bypass"
        elif self.tool_selected == self.TOOL_GATE_UNKNOWN:
            return "Unknown"
        else:
            return "T%d" % self.tool_selected

    def _selected_gate_string(self):
        if self.gate_selected == self.TOOL_GATE_BYPASS:
            return "bypass"
        elif self.gate_selected == self.TOOL_GATE_UNKNOWN:
            return "unknown"
        else:
            return "#%d" % self.gate_selected

    def _set_action(self, action):
        if action == self.action: return action
        old_action = self.action
        self.action = action
        if self.printer.lookup_object("gcode_macro %s" % self.action_changed_macro, None) is not None:
            self._wrap_gcode_command("%s ACTION='%s' OLD_ACTION='%s'" % (self.action_changed_macro, self._get_action_string(), self._get_action_string(old_action)))
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
        self.log_always("MMU enabled and reset")
        self._schedule_mmu_bootup_tasks()

    def _disable_mmu(self):
        if not self.is_enabled: return
        self.reinit()
        self._disable_runout()
        self.reactor.update_timer(self.hotend_off_timer, self.reactor.NEVER)
        self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout)
        self.is_enabled = False
        self.printer.send_event("mmu:disabled")
        self._set_print_state("standby")
        self.log_always("MMU disabled")

    # Wrapper so we can minimize actual disk writes and batch updates
    def save_variable(self, variable, value, write=False):
        self.save_variables.allVariables[variable] = value
        if write:
            self._write_variables()

    def _write_variables(self):
        if self._can_write_variables:
            mmu_vars_revision = self.save_variables.allVariables.get(self.VARS_MMU_REVISION, 0) + 1
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_REVISION, mmu_vars_revision))

    @contextlib.contextmanager
    def _wrap_suspend_write_variables(self):
        self._can_write_variables = False
        try:
            yield self
        finally:
            self._can_write_variables = True
            self._write_variables()

    def _random_failure(self):
        if self.test_random_failures and random.randint(0, 10) == 0:
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
    def cmd_MMU_HELP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        testing = gcmd.get_int('TESTING', 0, minval=0, maxval=1)
        slicer = gcmd.get_int('SLICER', 0, minval=0, maxval=1)
        callbacks = gcmd.get_int('CALLBACKS', 0, minval=0, maxval=1)
        steps = gcmd.get_int('STEPS', 0, minval=0, maxval=1)
        msg = "Happy Hare MMU commands: (use MMU_HELP SLICER=1 CALLBACKS=1 TESTING=1 STEPS=1 for full command set)\n"
        tesing_msg = "\nCalibration and testing commands:\n"
        slicer_msg = "\nPrint start/end or slicer macros (defined in mmu_software.cfg\n"
        callback_msg = "\nCallbacks (defined in mmu_sequence.cfg, mmu_state.cfg)\n"
        seq_msg = "\nAdvanced load/unload sequence and steps:\n"
        cmds = list(self.gcode.ready_gcode_handlers.keys())
        cmds.sort()

        # Logic to partition commands:
        for c in cmds:
            d = self.gcode.gcode_help.get(c, "n/a")

            if (c.startswith("MMU_START") or c.startswith("MMU_END") or c in ["MMU_UPDATE_HEIGHT"]) and c not in ["MMU_ENDLESS_SPOOL"]:
                slicer_msg += "%s : %s\n" % (c.upper(), d) # Print start/end macros

            elif c.startswith("MMU") and not c.startswith("MMU__"):
                if any(substring in c for substring in ["_CALIBRATE", "_TEST", "_SOAKTEST", "MMU_COLD_PULL"]):
                    tesing_msg += "%s : %s\n" % (c.upper(), d) # Testing and calibration commands
                else:
                    if c not in ["MMU_UNLOAD", "MMU_CHANGE_TOOL_STANDALONE", "MMU_CHECK_GATES", "MMU_REMAP_TTG", "MMU_FORM_TIP"]: # Remove aliases
                        msg += "%s : %s\n" % (c.upper(), d) # Base command

            elif c.startswith("_MMU"):
                if c.startswith("_MMU_STEP") or c in ["_MMU_M400", "_MMU_LOAD_SEQUENCE", "_MMU_UNLOAD_SEQUENCE"]:
                    seq_msg += "%s : %s\n" % (c.upper(), d) # Invidual sequence step commands
                elif c.startswith("_MMU_PRE_") or c.startswith("_MMU_POST_") or c in ["_MMU_ACTION_CHANGED", "_MMU_EVENT", "_MMU_PRINT_STATE_CHANGED"]:
                    callback_msg += "%s : %s\n" % (c.upper(), d) # Callbacks

        msg += slicer_msg if slicer else ""
        msg += callback_msg if callbacks else ""
        msg += tesing_msg if testing else ""
        msg += seq_msg if steps else ""
        self.log_always(msg)

    cmd_MMU_ENCODER_help = "Display encoder position and stats or enable/disable runout detection logic in encoder"
    def cmd_MMU_ENCODER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self.check_if_disabled(): return
        value = gcmd.get_float('VALUE', -1, minval=0.)
        enable = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        if enable == 1:
            self.encoder_sensor.set_mode(self.enable_clog_detection)
        elif enable == 0:
            self.encoder_sensor.set_mode(self.encoder_sensor.RUNOUT_DISABLED)
        elif value >= 0.:
            self.set_encoder_distance(value)
            return
        status = self.encoder_sensor.get_status(0)
        msg = "Encoder position: %.1f" % status['encoder_pos']
        msg += "\nRunout detection: %s" % ("Enabled" if status['enabled'] else "Disabled")
        clog = "Automatic" if status['detection_mode'] == 2 else "On" if status['detection_mode'] == 1 else "Off"
        msg += "\nClog/Runout mode: %s (Detection length: %.1f)" % (clog, status['detection_length'])
        msg += "\nTrigger headroom: %.1f (Minimum observed: %.1f)" % (status['headroom'], status['min_headroom'])
        msg += "\nFlowrate: %d %%" % status['flow_rate']
        self.log_info(msg)

    cmd_MMU_LED_help = "Manage mode of operation of optional MMU LED's"
    def cmd_MMU_LED(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_has_leds(): return
        if self.check_if_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))

        set_led_macro = self.printer.lookup_object("gcode_macro _MMU_SET_LED", None)
        led_vars_macro = self.printer.lookup_object("gcode_macro _MMU_LED_VARS", None)
        if led_vars_macro and set_led_macro:

            current_led_enable = led_vars_macro.variables['led_enable']
            current_led_animation = led_vars_macro.variables['led_animation']
            led_enable = bool(gcmd.get_int('ENABLE', current_led_enable, minval=0, maxval=1))
            led_animation = bool(gcmd.get_int('ANIMATION', current_led_animation, minval=0, maxval=1))
            if led_animation and not self.has_led_animation:
                raise gcmd.error("Led animation is unavailable. Klipper led_effects module is missing")

            default_exit_effect = gcmd.get('EXIT_EFFECT', led_vars_macro.variables['default_exit_effect'])
            default_entry_effect = gcmd.get('ENTRY_EFFECT', led_vars_macro.variables['default_entry_effect'])
            default_status_effect = gcmd.get('STATUS_EFFECT', led_vars_macro.variables['default_status_effect'])

            led_vars = {}
            led_vars['led_enable'] = led_enable
            led_vars['led_animation'] = led_animation
            led_vars['default_exit_effect'] = default_exit_effect
            led_vars['default_entry_effect'] = default_entry_effect
            led_vars['default_status_effect'] = default_status_effect

            if current_led_enable and not led_enable:
                # Enabled to disabled
                self._wrap_gcode_command("_MMU_SET_LED EXIT_EFFECT=off ENTRY_EFFECT=off STATUS_EFFECT=off")
                led_vars_macro.variables.update(led_vars)
            else:
                if current_led_animation and not led_animation:
                    # Turning animation off so clear existing effects
                    self._wrap_gcode_command("_MMU_SET_LED EXIT_EFFECT=off ENTRY_EFFECT=off STATUS_EFFECT=off FADETIME=0")
                led_vars_macro.variables.update(led_vars)
                self._wrap_gcode_command("_MMU_SET_LED EXIT_EFFECT=default ENTRY_EFFECT=default STATUS_EFFECT=default")

            if not quiet:
                effect_string = lambda effect, enabled : ("'%s'" % effect) if enabled != -1 else "Unavailable"
                msg = "LEDs are %s\n" % ("enabled" if led_enable else "disabled")
                msg = "LED animations: %s\n" % ("unavailable" if not self.has_led_animation else "enabled" if led_animation else "disabled")
                msg += "Default exit effect: %s\n" % effect_string(default_exit_effect, set_led_macro.variables['exit_first_led_index'])
                msg += "Default entry effect: %s\n" % effect_string(default_entry_effect, set_led_macro.variables['entry_first_led_index'])
                msg += "Default status effect: %s\n" % effect_string(default_status_effect, set_led_macro.variables['status_led_index'])
                msg += "\nOptions:\nENABLE=[0|1]\nANIMATION=[0|1]\nEXIT_EFFECT=[off|gate_status|filament_color|slicer_color]\nENTRY_EFFECT=[off|gate_status|filament_color|slicer_color]\nSTATUS_EFFECT=[off|on|filament_color|slicer_color]"
                self.log_always(msg)
        else:
            self.log_error("LEDs not available")

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
        self.enable_endless_spool = self.default_enable_endless_spool
        self.save_variable(self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool)
        self.endless_spool_groups = list(self.default_endless_spool_groups)
        self.save_variable(self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
        self.ttg_map = list(self.default_ttg_map)
        self.save_variable(self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map)

        self.gate_status = self._validate_gate_status(list(self.default_gate_status))
        self.gate_filament_name = list(self.default_gate_filament_name)
        self.gate_material = list(self.default_gate_material)
        self._update_gate_color(list(self.default_gate_color))
        self.gate_temperature = list(self.default_gate_temperature)
        self.gate_spool_id = list(self.default_gate_spool_id)
        self.gate_speed_override = list(self.default_gate_speed_override)

        self.save_variable(self.VARS_MMU_GATE_SELECTED, self.gate_selected)
        self.save_variable(self.VARS_MMU_TOOL_SELECTED, self.tool_selected)
        self.save_variable(self.VARS_MMU_FILAMENT_POS, self.filament_pos)
        self._write_variables()
        self._persist_gate_map(sync=True)
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

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)
        gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.form_tip_macro, gcode_macro)

        if reset:
            if self.form_tip_vars is not None:
                gcode_vars.variables = dict(self.form_tip_vars)
                self.form_tip_vars = None
                self.log_always("Reset '%s' macro variables to defaults" % self.form_tip_macro)
            show = True

        if show:
            msg = "Variable settings for macro '%s':" % self.form_tip_macro
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
                self.log_error("Variable '%s' is not defined for '%s' macro" % (param, self.form_tip_macro))

        # Run the macro in test mode (final_eject is set)
        msg = "Running macro '%s' with the following variable settings:" % self.form_tip_macro
        for k, v in gcode_vars.variables.items():
            msg += "\nvariable_%s: %s" % (k, v)
        self.log_always(msg)

        if run:
            self._ensure_safe_extruder_temperature(wait=True)
            # Mimick in print if requested
            try:
                self._sync_gear_to_extruder(self.sync_form_tip and self.is_in_print(force_in_print), grip=True, current=self.is_in_print(force_in_print))
                _,_,_ = self._do_form_tip(test=True)
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
            except MmuError as ee:
                self.handle_mmu_error(str(ee))
            finally:
                self._sync_gear_to_extruder(False, grip=True)

    cmd_MMU_STEP_LOAD_GATE_help = "User composable loading step: Move filament from gate to start of bowden"
    def cmd_MMU_STEP_LOAD_GATE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            self._load_gate()
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_GATE_help = "User composable unloading step: Move filament from start of bowden and park in the gate"
    def cmd_MMU_STEP_UNLOAD_GATE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        full = gcmd.get_int('FULL', 0)
        try:
            self._unload_gate(homing_max=self._get_bowden_length(self.gate_selected) if full else None)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_LOAD_BOWDEN_help = "User composable loading step: Smart loading of bowden"
    def cmd_MMU_STEP_LOAD_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', None, minval=0.)
        try:
            self._load_bowden(length)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_BOWDEN_help = "User composable unloading step: Smart unloading of bowden"
    def cmd_MMU_STEP_UNLOAD_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', self._get_bowden_length(self.gate_selected))
        try:
            self._unload_bowden(length)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_HOME_EXTRUDER_help = "User composable loading step: Home to extruder sensor or entrance through collision detection"
    def cmd_MMU_STEP_HOME_EXTRUDER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            self._home_to_extruder(self.extruder_homing_max)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_HOME_EXTRUDER: %s" % str(ee))

    cmd_MMU_STEP_LOAD_TOOLHEAD_help = "User composable loading step: Toolhead loading"
    def cmd_MMU_STEP_LOAD_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        try:
            self._load_extruder(extruder_only)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_LOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_TOOLHEAD_help = "User composable unloading step: Toolhead unloading"
    def cmd_MMU_STEP_UNLOAD_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0))
        park_pos = gcmd.get_float('PARK_POS', -self._get_filament_position()) # +ve value
        try:
            # Precautionary validation of filament position
            park_pos = min(self.toolhead_extruder_to_nozzle, max(0, park_pos))
            self._set_filament_position(-park_pos)

            self._unload_extruder(extruder_only = extruder_only)
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_UNLOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_HOMING_MOVE_help = "User composable loading step: Generic homing move"
    def cmd_MMU_STEP_HOMING_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            self._homing_move_cmd(gcmd, "User defined step homing move")
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_HOMING_MOVE: %s" % str(ee))

    cmd_MMU_STEP_MOVE_help = "User composable loading step: Generic move"
    def cmd_MMU_STEP_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        try:
            self._move_cmd(gcmd, "User defined step move")
        except MmuError as ee:
            self.handle_mmu_error("_MMU_STEP_MOVE: %s" % str(ee))

    cmd_MMU_STEP_SET_FILAMENT_help = "User composable loading step: Set filament position state"
    def cmd_MMU_STEP_SET_FILAMENT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        state = gcmd.get_int('STATE', minval=self.FILAMENT_POS_UNKNOWN, maxval=self.FILAMENT_POS_LOADED)
        silent = gcmd.get_int('SILENT', 0)
        self._set_filament_pos_state(state, silent)


##############################################
# MODULAR FILAMENT LOAD AND UNLOAD FUNCTIONS #
##############################################

    # Load filament into gate. This is considered the starting positon for the rest of the filament loading
    # process. Note that this may overshoot the home position for the "encoder" technique but subsequent
    # bowden move will accommodate. Also for systems with gate sensor and encoder with gate sensor first,
    # there will be a gap in encoder readings that must be taken into consideration.
    def _load_gate(self, allow_retry=True, adjust_grip_on_error=True):
        self._validate_gate_config("load")
        self._set_filament_direction(self.DIRECTION_LOAD)
        self.selector.filament_drive()
        retries = self.gate_load_retries if allow_retry else 1

        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            with self._require_encoder():
                measured = 0.
                for i in range(retries):
                    msg = "Initial load into encoder" if i == 0 else ("Retry load into encoder (reetry #%d)" % i)
                    _,_,m,_ = self.trace_filament_move(msg, self.gate_homing_max)
                    measured += m
                    if (m) > 6.0:
                        self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], self.GATE_AVAILABLE)) # Don't reset if filament is buffered
                        self._set_filament_position(measured + self.gate_parking_distance)
                        self.set_encoder_distance(measured + self.gate_parking_distance)
                        self._set_filament_pos_state(self.FILAMENT_POS_START_BOWDEN)
                        return
                    else:
                        self.log_debug("Error loading filament - filament motion was not detected by the encoder. %s" % ("Retrying..." if i < retries - 1 else ""))
                        if i < retries - 1:
                            self.selector.filament_release()
                            self.selector.filament_drive()

        else: # Gate sensor... ENDSTOP_GATE is shared, but ENDSTOP_POST_GATE_PREFIX is specific
            for i in range(retries):
                endstop_name = self._get_gate_endstop_name()
                msg = ("Initial homing to %s sensor" % endstop_name) if i == 0 else ("Retry homing to gate sensor (retry #%d)" % i)
                actual,homed,measured,_ = self.trace_filament_move(msg, self.gate_homing_max, motor="gear", homing_move=1, endstop_name=endstop_name)
                if homed:
                    self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
                    self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], self.GATE_AVAILABLE)) # Don't reset if filament is buffered
                    self._set_filament_position(self.gate_parking_distance)
                    self.set_encoder_distance(self.gate_parking_distance)
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_GATE)
                    return
                else:
                    self.log_debug("Error loading filament - filament did not reach gate homing sensor. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self.selector.filament_release()
                        self.selector.filament_drive()

        self._set_gate_status(self.gate_selected, self.GATE_EMPTY)
        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        if adjust_grip_on_error:
            self._auto_filament_grip()
        msg = "Couldn't pick up filament at gate"
        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            msg += " (encoder didn't report enough movement)"
        else:
            msg += " (gate endstop didn't trigger)"
        raise MmuError(msg)

    # Unload filament through gate to final MMU park position.
    # Strategies include use of encoder or homing to gate/post-gate endstop and then parking
    # Allows the overriding of homing_max for slow unloads when we are unsure of filament position
    def _unload_gate(self, homing_max=None):
        self._validate_gate_config("unload")
        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self.selector.filament_drive()
        full = homing_max == self._get_bowden_length(self.gate_selected)
        homing_max = homing_max or self.gate_homing_max

        if full: # Means recovery operation
            # Safety step because this method is used as a defensive way to unload the entire bowden from unknown position
            # It handles the cases of filament still in extruder with not toolhead sensor or the small window where filament
            # is between extruder entrance and toolhead sensor (if toolhead sensor is available)
            length = self.toolhead_extruder_to_nozzle - self.toolhead_sensor_to_nozzle if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD) else self.toolhead_extruder_to_nozzle
            length = min(length + self.toolhead_unload_safety_margin, homing_max)
            self.log_debug("Performing synced pre-unload bowden move to ensure filament is not trapped in extruder")
            _,_,_,_ = self.trace_filament_move("Bowden safety pre-unload move", -length, motor="gear+extruder")

        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
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
                    if measured > self.encoder_min: # We expect 0, but relax the test a little (allow one pulse)
                        self.log_info("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                    self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                    return max(actual - self.gate_unload_buffer, 0)
                msg = "did not clear the encoder after moving %.1fmm" % homing_max

        else: # Using mmu_gate or mmu_post_gate sensor
            actual,homed,_,_ = self.trace_filament_move("Reverse homing to gate sensor", -homing_max, motor="gear", homing_move=-1, endstop_name=self._get_gate_endstop_name())
            if homed:
                self._set_filament_pos_state(self.FILAMENT_POS_HOMED_GATE)
                self.trace_filament_move("Final parking", -self.gate_parking_distance)
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                return max(actual - self.gate_unload_buffer, 0)
            else:
                msg = "did not home to gate sensor %s after moving %1.fmm" % (self.gate_homing_endstop, homing_max)

        raise MmuError("Failed to unload gate because %s" % msg)

    # Shared with manual bowden calibration routine
    def _reverse_home_to_encoder(self, homing_max):
        max_steps = int(homing_max / self.encoder_move_step_size) + 5
        delta = 0.
        actual = 0.
        for i in range(max_steps):
            msg = "Unloading step #%d from encoder" % (i+1)
            sactual,_,_,sdelta = self.trace_filament_move(msg, -self.encoder_move_step_size)
            delta += sdelta
            actual -= sactual
            # Large enough delta here means we are out of the encoder
            if sdelta >= self.encoder_move_step_size * 0.2: # 20 %
                actual -= sdelta
                park = self.gate_parking_distance - sdelta # will be between 8 and 20mm (for 23mm gate_parking_distance, 15mm step)
                return actual, park, delta
        self.log_debug("Filament did not clear encoder even after moving %.1fmm" % (self.encoder_move_step_size * max_steps))
        return None

    # Shared gate functions to deduplicate logic
    def _validate_gate_config(self, direction):
        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            if not self.has_encoder():
                raise MmuError("Attempting to %s encoder but encoder is not configured on MMU!" % direction)
        elif self.gate_homing_endstop in [self.ENDSTOP_GATE, self.ENDSTOP_POST_GATE_PREFIX]:
            sensor = self.gate_homing_endstop
            if self.gate_homing_endstop == self.ENDSTOP_POST_GATE_PREFIX:
                sensor += "_%d" % self.gate_selected
            if not self.sensor_manager.has_sensor(sensor):
                raise MmuError("Attempting to %s gate but gate sensor '%s' is not configured on MMU!" % (direction, sensor))
        else:
            raise MmuError("Unsupported gate endstop %s" % self.gate_homing_endstop)

    # Fast load of filament in bowden, usually the full length but if 'full' is False a specific length can be specified
    # Note that filament position will be measured from the gate "parking position" and so will be the gate_parking_distance
    # plus any overshoot. The start of the bowden move is from the parking homing point.
    # Returns ratio of measured movement to real movement IF it is "clean" and could be used for auto-calibration else 0
    def _load_bowden(self, length=None):
        bowden_length = self._get_bowden_length(self.gate_selected)
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Compensate for distance already moved (e.g. overshoot after encoder homing)
        length -= (self._get_filament_position() - self.gate_parking_distance)

        if length > 0:
            self.log_debug("Loading bowden tube")
            self._set_filament_direction(self.DIRECTION_LOAD)
            self.selector.filament_drive()
            if self.gate_selected > 0 and self.rotation_distances[self.gate_selected] <= 0:
                self.log_info("Warning: Gate %d not calibrated! Using default rotation distance from gate 0" % self.gate_selected)

            # "Fast" load
            _,_,_,delta = self.trace_filament_move("Fast loading move through bowden", length, track=True, encoder_dwell=bool(self.autotune_rotation_distance))
            delta -= self._get_encoder_dead_space()
            ratio = (length - delta) / length

            # Encoder based validation test
            if self._can_use_encoder() and delta >= length * (self.bowden_move_error_tolerance/100.) and not self.calibrating:
                raise MmuError("Failed to load bowden. Perhaps filament is stuck in gate. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

            # Encoder based validation test
            if self._can_use_encoder() and delta >= self.bowden_allowable_load_delta and not self.calibrating:
                ratio = 0. # Not considered valid for auto-calibration
                # Correction attempts to load the filament according to encoder reporting
                if self.bowden_apply_correction:
                    for i in range(2):
                        if delta >= self.bowden_allowable_load_delta:
                            msg = "Correction load move #%d into bowden" % (i+1)
                            _,_,_,d = self.trace_filament_move(msg, delta, track=True)
                            delta -= d
                            self.log_debug("Correction load move was necessary, encoder now measures %.1fmm" % self.get_encoder_distance())
                        else:
                            break
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)
                    if delta >= self.bowden_allowable_load_delta:
                        self.log_info("Warning: Excess slippage was detected in bowden tube load afer correction moves. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details"% (length, length - delta))
                else:
                    self.log_info("Warning: Excess slippage was detected in bowden tube load but 'bowden_apply_correction' is disabled. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details" % (length, length - delta))

                if delta >= self.bowden_allowable_load_delta:
                    self.log_debug("Possible causes of slippage:\nCalibration ref length too long (hitting extruder gear before homing)\nCalibration ratio for gate is not accurate\nMMU gears are not properly gripping filament\nEncoder reading is inaccurate\nFaulty servo")

            self._random_failure() # Testing
            self.movequeues_wait()
        else:
            # No bowden movement required
            ratio = 1.

        if full:
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
        elif self.filament_pos != self.FILAMENT_POS_IN_BOWDEN:
            self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)
            ratio = 0.
        return ratio # For auto-calibration

    # Fast unload of filament from exit of extruder gear (end of bowden) to position close to MMU (gate_unload_buffer away)
    def _unload_bowden(self, length=None):
        bowden_length = self._get_bowden_length(self.gate_selected)
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Shorten move by buffer used to ensure we don't overshoot
        length -= self.gate_unload_buffer

        if length > 0:
            self.log_debug("Unloading bowden tube")
            self._set_filament_direction(self.DIRECTION_UNLOAD)
            self.selector.filament_drive()

            # Optional pre-unload safety step
            if (full and self.has_encoder() and self.bowden_pre_unload_test and
                self.sensor_manager.check_sensor(self.ENDSTOP_EXTRUDER_ENTRY) is not False and
                self.sensor_manager.check_all_sensors_before(self.FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is not False
            ):
                with self._require_encoder():
                    self.log_debug("Performing bowden pre-unload test")
                    _,_,_,delta = self.trace_filament_move("Bowden pre-unload test", -self.encoder_move_step_size)
                    if delta > self.encoder_move_step_size * (self.bowden_pre_unload_error_tolerance/100.):
                        self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
                        raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder or filament not loaded")
                    length -= self.encoder_move_step_size
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

            # "Fast" unload
            ratio = 0.
            if self.sensor_manager.check_all_sensors_before(self.FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is not False:
                _,_,_,delta = self.trace_filament_move("Fast unloading move through bowden", -length, track=True, encoder_dwell=bool(self.autotune_rotation_distance))
                delta -= self._get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self._can_use_encoder() and delta >= self.bowden_allowable_unload_delta and not self.calibrating:
                    ratio = 0.
                    # Only a warning because _unload_gate() will deal with it
                    self.log_info("Warning: Excess slippage was detected in bowden tube unload. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

            self._random_failure() # Testing
            self.movequeues_wait()
        else:
            # No bowden movement required
            ratio = 1.

        if full:
            self._set_filament_pos_state(self.FILAMENT_POS_START_BOWDEN)
        elif self.filament_pos != self.FILAMENT_POS_IN_BOWDEN:
            self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)
            ratio = 0.
        return ratio # For auto-calibration

    # Optionally home filament to designated homing location at the extruder
    # Returns any homing distance for automatic calibration logic
    def _home_to_extruder(self, max_length):
        self._set_filament_direction(self.DIRECTION_LOAD)
        self.selector.filament_drive()
        measured = 0.
        homing_movement = None

        if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION:
            if self.has_encoder():
                actual,homed,measured,_ = self._home_to_extruder_collision_detection(max_length)
                homing_movement = actual
            else:
                raise MmuError("Cannot home to extruder using 'collision' method because encoder is not configured or disabled!")

        elif self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_NONE:
            homed = True

        else:
            self.log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (self.extruder_homing_endstop, max_length))
            actual,homed,measured,_ = self.trace_filament_move("Homing filament to extruder endstop", max_length, motor="gear", homing_move=1, endstop_name=self.extruder_homing_endstop)
            if homed:
                self.log_debug("Extruder endstop reached after %.1fmm (measured %.1fmm)" % (actual, measured))
                self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY)

                # Move the little bit more to reach extruder entrance if we homed to entry sensor
                # We do this here to allow _load_extruder() to work with "extruder_only" option
                if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_ENTRY:
                    _,_,measured,_ = self.trace_filament_move("Aligning filament to extruder gear", self.toolhead_entry_to_extruder, motor="gear")
            homing_movement = actual

        if not homed:
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder after moving %.1fmm" % max_length)

        if measured > (max_length * 0.8):
            self.log_info("Warning: 80%% of 'extruder_homing_max' was used homing. You may want to adjust your calibrated bowden length ('%s') or increase 'extruder_homing_max'" % self.VARS_MMU_CALIB_BOWDEN_LENGTH)

        self._set_filament_pos_state(self.FILAMENT_POS_HOMED_EXTRUDER)
        return homing_movement

    # Special extruder homing option for detecting the collision base on lack of encoder movement
    def _home_to_extruder_collision_detection(self, max_length):
        # Lock the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        ge = stepper_enable.lookup_enable(self.mmu_extruder_stepper.stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        step = self.extruder_collision_homing_step * math.ceil(self.encoder_resolution * 10) / 10
        self.log_debug("Homing to extruder gear, up to %.1fmm in %.1fmm steps" % (max_length, step))

        with self._wrap_gear_current(self.extruder_collision_homing_current, "for collision detection"):
            homed = False
            measured = delta = 0.
            for i in range(int(max_length / step)):
                msg = "Homing step #%d" % (i+1)
                _,_,smeasured,sdelta = self.trace_filament_move(msg, step, speed=self.gear_homing_speed)
                measured += smeasured
                delta += sdelta
                if sdelta >= self.encoder_min or abs(delta) > step: # Not enough or strange measured movement means we've hit the extruder
                    homed = True
                    measured -= step # Subtract the last step to improve accuracy
                    break
            self.log_debug("Extruder entrance%s found after %.1fmm move (%d steps), encoder measured %.1fmm (delta %.1fmm)"
                    % (" not" if not homed else "", step*(i+1), i+1, measured, delta))

        if delta > 5.0:
            self.log_info("Warning: A lot of slippage was detected whilst homing to extruder, you may want to reduce 'extruder_collision_homing_current' and/or ensure a good grip on filament by gear drive")

        self._set_filament_position(self._get_filament_position() - step) # Ignore last step movement
        return step*i, homed, measured, delta

    # Move filament from the extruder gears (entrance) to the nozzle
    # Returns any homing distance for automatic calibration logic
    def _load_extruder(self, extruder_only=False):
        with self.wrap_action(self.ACTION_LOADING_EXTRUDER):
            self.log_debug("Loading filament into extruder")
            self._set_filament_direction(self.DIRECTION_LOAD)

            # Important to wait for filaments with wildy different print temps. In practice, the time taken
            # to perform a swap should be adequate to reach the target temp but better safe than sorry
            self._ensure_safe_extruder_temperature(wait=True)
            homing_movement = None

            synced = not extruder_only
            if synced:
                self.selector.filament_drive()
                speed = self.extruder_sync_load_speed
                motor = "gear+extruder"
            else:
                self.selector.filament_release()
                speed = self.extruder_load_speed
                motor = "extruder"

            fhomed = False
            if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
                # With toolhead sensor we always first home to toolhead sensor past the extruder entrance
                if self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD):
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")
                self.log_debug("Homing up to %.1fmm to toolhead sensor%s" % (self.toolhead_homing_max, (" (synced)" if synced else "")))
                actual,fhomed,measured,_ = self.trace_filament_move("Homing to toolhead sensor", self.toolhead_homing_max, motor=motor, homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
                if fhomed:
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                    homing_movement = max(actual - (self.toolhead_extruder_to_nozzle - self.toolhead_sensor_to_nozzle), 0)
                else:
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure reduction is used only one time
            d = self.toolhead_sensor_to_nozzle if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD) else self.toolhead_extruder_to_nozzle
            length = max(d - self.filament_remaining - self.toolhead_residual_filament - self.toolhead_ooze_reduction - self.toolchange_retract, 0)
            self._set_filament_remaining(0.)
            self.log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self.trace_filament_move("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)

            # Encoder based validation test if short of deterministic sensors and test makes sense
            if self._can_use_encoder() and not fhomed and not extruder_only and self.gate_selected != self.TOOL_GATE_BYPASS:
                self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                if measured < self.encoder_min:
                    raise MmuError("Move to nozzle failed (encoder didn't sense any movement). Extruder may not have picked up filament or filament did not find homing sensor")
                elif delta > length * (self.toolhead_move_error_tolerance/100.):
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
                    raise MmuError("Move to nozzle failed (encoder didn't sense sufficient movement). Extruder may not have picked up filament or filament did not find homing sensor")

            # Tightening move to prevent erroneous clog detection / runout if gear stepper is not synced with extruder
            if self._can_use_encoder() and not extruder_only and self.gate_selected != self.TOOL_GATE_BYPASS and not self.sync_to_extruder and self.enable_clog_detection and self.toolhead_post_load_tighten:
                with self._wrap_gear_current(percent=50, reason="to tighten filament in bowden"):
                    # Servo will already be down
                    pullback = min(self.encoder_sensor.get_clog_detection_length() * self.toolhead_post_load_tighten / 100, 15) # % of current clog detection length
                    _,_,measured,delta = self.trace_filament_move("Tighening filament in bowden", -pullback, motor="gear", wait=True)
                    self.log_info("Filament tightened by %.1fmm to prevent false clog detection" % pullback)

            self._random_failure() # Testing
            self.movequeues_wait()
            self._set_filament_pos_state(self.FILAMENT_POS_LOADED)
            self.log_debug("Filament should be loaded to nozzle")
            return homing_movement

    # Extract filament past extruder gear (to end of bowden). Assume that tip has already been formed
    # and we are parked somewhere in the extruder either by slicer or by stand alone tip creation
    # But be careful:
    #   A poor tip forming routine or slicer could have popped the filament out of the extruder already
    # Ending point is either the exit of the extruder or at the extruder (entry) endstop if fitted
    def _unload_extruder(self, extruder_only=False, validate=True):
        with self.wrap_action(self.ACTION_UNLOADING_EXTRUDER):
            self.log_debug("Extracting filament from extruder")
            self._set_filament_direction(self.DIRECTION_UNLOAD)

            self._ensure_safe_extruder_temperature(wait=False)

            synced = self.selector.get_filament_grip_state() == self.FILAMENT_DRIVE_STATE and not extruder_only
            if synced:
                self.selector.filament_drive()
                speed = self.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                self.selector.filament_release()
                speed = self.extruder_unload_speed
                motor = "extruder"

            fhomed = False
            if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY) and not extruder_only:
                # BEST Strategy: Extruder exit movement leveraging extruder entry sensor. Must be synced
                synced = True
                self.selector.filament_drive()
                speed = self.extruder_sync_unload_speed
                motor = "gear+extruder"

                if not self.sensor_manager.check_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                    if self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD):
                        raise MmuError("Toolhead or extruder sensor failure. Extruder sensor reports no filament but toolhead sensor is still triggered")
                    else:
                        self.log_error("Warning: Filament was not detected by extruder (entry) sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                else:
                    hlength = self.toolhead_extruder_to_nozzle + self.toolhead_entry_to_extruder + self.toolhead_unload_safety_margin - self.toolhead_residual_filament - self.toolhead_ooze_reduction - self.toolchange_retract
                    self.log_debug("Reverse homing up to %.1fmm to extruder sensor (synced) to exit extruder" % hlength)
                    _,fhomed,_,_ = self.trace_filament_move("Reverse homing to extruder sensor", -hlength, motor=motor, homing_move=-1, endstop_name=self.ENDSTOP_EXTRUDER_ENTRY)

                if not fhomed:
                    raise MmuError("Failed to reach extruder entry sensor after moving %.1fmm" % hlength)
                else:
                    validate = False
                    # We know exactly where end of filament is so true up
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY)
                    self._set_filament_position(-(self.toolhead_extruder_to_nozzle + self.toolhead_entry_to_extruder))

                # Extra pedantic validation if we have toolhead sensor
                # TODO: There have been reports of this failing, perhaps because of klipper's late update of sensor state?
                #       So fromer MmuError() has been changed to error message
                if self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD):
                    self.log_error("Warning: Toolhead sensor still reports filament is present in toolhead! Possible sensor malfunction\nWill attempt to continue...")

            else:
                if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
                    # NEXT BEST: With toolhead sensor we first home to toolhead sensor. Optionally synced
                    if not self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD):
                        self.log_error("Warning: Filament was not detected in extruder by toolhead sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                    else:
                        hlength = self.toolhead_sensor_to_nozzle + self.toolhead_unload_safety_margin - self.toolhead_residual_filament - self.toolhead_ooze_reduction - self.toolchange_retract
                        self.log_debug("Reverse homing up to %.1fmm to toolhead sensor%s" % (hlength, (" (synced)" if synced else "")))
                        _,fhomed,_,_ = self.trace_filament_move("Reverse homing to toolhead sensor", -hlength, motor=motor, homing_move=-1, endstop_name=self.ENDSTOP_TOOLHEAD)
                    if not fhomed:
                        raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % hlength)
                    else:
                        validate = False
                        # We know exactly where end of filament is so true up
                        self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                        self._set_filament_position(-self.toolhead_sensor_to_nozzle)

                # Finish up with regular extruder exit movement. Optionally synced
                length = max(0, self.toolhead_extruder_to_nozzle + self._get_filament_position()) + self.toolhead_unload_safety_margin
                self.log_debug("Unloading last %.1fmm to exit the extruder%s" % (length, " (synced)" if synced else ""))
                _,_,measured,delta = self.trace_filament_move("Unloading extruder", -length, speed=speed, motor=motor, wait=True)

                # Best guess of filament position is right at extruder entrance or just beyond if synced
                if synced:
                    self._set_filament_position(-(self.toolhead_extruder_to_nozzle + self.toolhead_unload_safety_margin))
                else:
                    self._set_filament_position(-self.toolhead_extruder_to_nozzle)

                # Encoder based validation test if it has high chance of being useful
                # NOTE: This check which used to raise MmuError() is triping many folks up because they have poor tip forming
                #       logic so just log error and continue. This disguises the root cause problem but will make folks happier
                #       Not performed for slicer tip forming (validate=True) because everybody is ejecting the filament!
                if validate and self._can_use_encoder() and length > self.encoder_move_step_size and not extruder_only and self.gate_selected != self.TOOL_GATE_BYPASS:
                    self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                    msg = None
                    if measured < self.encoder_min:
                        msg = "any"
                    elif synced and delta > length * (self.toolhead_move_error_tolerance/100.):
                        msg = "suffient"
                    if msg:
                        self.log_error("Warning: Encoder not sensing %s movement during final extruder retraction move\nConcluding filament either stuck in the extruder, tip forming erroneously completely ejected filament or filament was not fully loaded\nWill attempt to continue..." % msg)

                self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)

            self._random_failure() # Testing
            self.movequeues_wait()
            self.log_debug("Filament should be out of extruder")

    # Use data from load or unload operation to auto-calibrate / auto-tune
    #
    # Data we can use:
    #  - ratio of large bowden move to that measured by encoder (0 if it can't be relied on)
    #  - the amount of unexpected homing necessary to reach endstop. We want some homing
    #    movement but we can't use excessive numbers for tuning (None indicates not available)
    #  - the direction of filament movement
    #
    # Things we could/can tune:
    #  - If gate 0, use the bowden move ratio to update encoder calibration ("encoder calibration"). Dangerous so not done!
    #  - If gate 0, use excess homing move to tune the calibrated bowden length ("bowden calibration")
    #    but only do this if bowden move ratio is reasonable. Can be done in both directions
    #  - If gate >0, use the bowden move ratio to set/tune the gear rotation_distance ("gate calibration")
    #    but only do this if homing movement data tells us we haven't overshot. Can be done in both directions
    #
    # Calibration replaces the previous value. Autotuning applies a moving average
    def _autotune(self, direction, bowden_move_ratio, homing_movement):
        msg = "Autotune: bowden move ratio: %.6f, Extra homing movement: %s" % (bowden_move_ratio, "n/a" if homing_movement is None else "%.1fmm" % homing_movement)
        if homing_movement is not None:
            # TODO Currently only works with gate >0. Could work with gate 0 if variable_rotation_distance is True
            # TODO and bowden is calibrated and we don't tune bowden below
            if (
                self.autotune_rotation_distance and self.gate_selected > 0
                and bowden_move_ratio > 0 and homing_movement > 0
            ):
                if direction in [self.DIRECTION_LOAD, self.DIRECTION_UNLOAD]:
                    # Encoder based automatic calibration of gate's gear rotation_distance aka MMU_CALIBRATE_GATES
                    current_rd = self.gear_rail.steppers[0].get_rotation_distance()[0]
                    new_rd = round(bowden_move_ratio * current_rd, 6)
                    gate0_rd = self.rotation_distances[0]
                    tolerance_range = (gate0_rd - gate0_rd * 0.1, gate0_rd + gate0_rd * 0.1) # Allow max 10% variation from gate 0 for autotune
                    if tolerance_range[0] <= new_rd < tolerance_range[1]:
                        if not self.calibrating and self.rotation_distances[self.gate_selected] > 0:
                            # Tuning existing calibration
                            new_rd = round((self.rotation_distances[self.gate_selected] * 5 + new_rd) / 6, 6) # Moving average
                            msg += "\nAutotuned rotation_distance: %.6f for gate %d (ratio: %.6f)" % (new_rd, self.gate_selected, bowden_move_ratio)
                        else:
                            # First time calibration or forced re-calibration
                            self.log_always("Calibrated rotation_distance: %.6f has been automatically saved for gate %d (ratio: %.6f)" % (new_rd, self.gate_selected, bowden_move_ratio))
                        self._set_rotation_distance(new_rd)
                        self.rotation_distances[self.gate_selected] = new_rd
                        self.save_variable(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, write=True)
                    else:
                        msg += "\nCalculated rotation_distance: %.6f for gate %d failed sanity check and has been ignored (ratio: %.6f)" % (new_rd, self.gate_selected, bowden_move_ratio)

            # TODO Currently only works with gate 0. Could work with other gates if variable_bowden_lengths is True
            # TODO and rotation distance is calibrated and not being tuned above
            if (
                self.autotune_bowden_length and self.gate_selected == 0
                and (0.9 < bowden_move_ratio < 1.1 or not self.has_encoder())
            ):
                if direction in [self.DIRECTION_LOAD, self.DIRECTION_UNLOAD]:
                    # Homing movement based automatic calibration of bowden length aka MMU_CALIBRATE_BOWDEN
                    bowden_length = self._get_bowden_length(self.gate_selected)
                    if homing_movement > 10: # Represents padding because we want some homing room
                        new_bl = bowden_length + homing_movement - 10.
                    elif homing_movement == 0:
                        new_bl = bowden_length - 5. # Reduce slightly
                    else:
                        new_bl = bowden_length
                    new_bl = round((bowden_length * 5 + new_bl) / 6, 1) # Moving average
                    if new_bl != bowden_length:
                        self._save_bowden_length(self.gate_selected, new_bl)
                        msg += "\nAutotuned bowden length: %.1f" % new_bl

            if self.gate_selected == 0 and homing_movement > 0 and bowden_move_ratio > 0:
                # Bowden movement based warning of encoder calibration aka MMU_CALIBRATE_ENCODER
                if not 0.95 < bowden_move_ratio < 1.05:
                    msg += "\nEncoder measurement on gate 0 was outside of desired calibration range. You may want to check function or recalibrate"
        else:
            msg += ". Tuning not possible"

        self.log_debug(msg)


##############################################
# LOAD / UNLOAD SEQUENCES AND FILAMENT TESTS #
##############################################

    def load_sequence(self, bowden_move=None, skip_extruder=False, extruder_only=False):
        self.movequeues_wait()

        bowden_length = self._get_bowden_length(self.gate_selected)
        if bowden_move is None:
            bowden_move = bowden_length
        if bowden_move > bowden_length:
            self.log_info("Warning: Restricting bowden load length to calibrated value of %.1fmm" % bowden_length)
        full = bowden_move == bowden_length

        self._set_filament_direction(self.DIRECTION_LOAD)
        self._initialize_filament_position(dwell=None)

        try:
            self.log_info("Loading %s..." % ("extruder" if extruder_only else "filament"))

            home = False
            if not extruder_only:
                self._display_visual_state()
                if full:
                    self._track_time_start('load')
                    home = self._must_home_to_extruder()
                else:
                    skip_extruder = True
                current_action = self._set_action(self.ACTION_LOADING)

            homing_movement = None # Track how much homing is done for calibrated bowden length optimization
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
            start_filament_pos = self.filament_pos

            # Note: Conditionals deliberately coded this way to match macro alternative
            if self.gcode_load_sequence:
                self.log_debug("Calling external user defined loading sequence macro")
                self._wrap_gcode_command("%s FILAMENT_POS=%d LENGTH=%.1f FULL=%d HOME_EXTRUDER=%d SKIP_EXTRUDER=%d EXTRUDER_ONLY=%d" % (self.load_sequence_macro, start_filament_pos, bowden_move, int(full), int(home), int(skip_extruder), int(extruder_only)), exception=True)

            elif extruder_only:
                if start_filament_pos < self.FILAMENT_POS_EXTRUDER_ENTRY:
                    self._load_extruder(extruder_only=True)
                else:
                    self.log_debug("Assertion failure: Unexpected state %d in load_sequence(extruder_only=True)" % start_filament_pos)
                    raise MmuError("Cannot load extruder because already in extruder. Unload first")

            elif start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                self.log_debug("Assertion failure: Unexpected state %d in load_sequence()" % start_filament_pos)
                raise MmuError("Cannot load because already in extruder. Unload first")

            else:
                if start_filament_pos <= self.FILAMENT_POS_UNLOADED:
                    self._load_gate()

                if start_filament_pos < self.FILAMENT_POS_END_BOWDEN:
                    bowden_move_ratio = self._load_bowden(bowden_move)

                if start_filament_pos < self.FILAMENT_POS_HOMED_EXTRUDER and home:
                    hm = self._home_to_extruder(self.extruder_homing_max)
                    if hm is not None:
                        homing_movement = (homing_movement or 0) + hm

                if not skip_extruder:
                    hm = self._load_extruder()
                    if hm is not None:
                        homing_movement = (homing_movement or 0) + hm

            self.movequeues_wait()
            msg = "Load of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                msg += " {1}(encoder measured %.1fmm){0}" % self.get_encoder_distance(dwell=None)
            self.log_info(msg, color=True)

            # Finally autotune calibrated bowden length
            if full and not extruder_only and not self.gcode_load_sequence:
                self._autotune(self.DIRECTION_LOAD, bowden_move_ratio, homing_movement)

        except MmuError as ee:
            if full:
                self._track_gate_statistics('load_failures', self.gate_selected)
            raise MmuError("Load sequence failed: %s" % (str(ee)))
        finally:
            if full:
                self._track_time_end('load')
                self._track_gate_statistics('loads', self.gate_selected)
            if not extruder_only:
                self._set_action(current_action)
            if not self.is_printing():
                self.selector.filament_release()

    def unload_sequence(self, bowden_move=None, check_state=False, form_tip=None, extruder_only=False, runout=False):
        self.movequeues_wait()

        bowden_length = self._get_bowden_length(self.gate_selected)
        if bowden_move is None:
            bowden_move = bowden_length
        if bowden_move > bowden_length:
            self.log_info("Warning: Restricting bowden unload length to calibrated value of %.1fmm" % bowden_length)
        full = bowden_move == bowden_length

        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self._initialize_filament_position(dwell=None)

        if check_state or self.filament_pos == self.FILAMENT_POS_UNKNOWN:
            # Let's determine where filament is and reset state before continuing
            self.recover_filament_pos(message=True)

        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            self.log_debug("Filament already ejected")
            self._auto_filament_grip()
            return

        try:
            self.log_info("Unloading %s..." % ("extruder" if extruder_only else "filament"))

            if not extruder_only:
                current_action = self._set_action(self.ACTION_UNLOADING)
                self._display_visual_state()
                self._track_time_start('unload')

            park_pos = 0.
            form_tip = form_tip if not None else self.FORM_TIP_STANDALONE
            if form_tip == self.FORM_TIP_SLICER:
                # Slicer was responsible for the tip, but the user must set the slicer_tip_park_pos
                park_pos = self.slicer_tip_park_pos
                self._set_filament_position(-park_pos)
                if park_pos == 0.:
                    self.log_error("Tip forming performed by slicer but 'slicer_tip_park_pos' not set")
                else:
                    self.log_debug("Tip forming performed by slicer, park_pos set to %.1fmm" % park_pos)

            elif form_tip == self.FORM_TIP_STANDALONE and (self.filament_pos >= self.FILAMENT_POS_IN_EXTRUDER or runout):
                # Extruder only in runout case to give filament best chance to reach gear
                detected = self.form_tip_standalone(extruder_only=(extruder_only or runout))
                park_pos = self._get_filament_position()

                # If handling runout warn if we don't see any filament near the gate
                if runout and (
                    self.sensor_manager.check_any_sensors_before(self.FILAMENT_POS_HOMED_GATE, self.gate_selected) is False or
                    (self.has_encoder() and self.get_encoder_distance() == 0)
                ):
                    self.log_info("Warning: Filament not seen near gate after tip forming move. Unload may not be possible")

                self._wrap_gcode_command(self.post_form_tip_macro, exception=True, wait=True)

            # Note: Conditionals deliberately coded this way to match macro alternative
            homing_movement = None # Track how much homing is done for calibrated bowden length optimization
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
            start_filament_pos = self.filament_pos
            unload_to_buffer = (start_filament_pos >= self.FILAMENT_POS_END_BOWDEN and not extruder_only)

            if self.gcode_unload_sequence:
                self.log_debug("Calling external user defined unloading sequence macro")
                self._wrap_gcode_command("%s FILAMENT_POS=%d LENGTH=%.1f EXTRUDER_ONLY=%d PARK_POS=%.1f" % (self.unload_sequence_macro, start_filament_pos, bowden_move, extruder_only, park_pos), exception=True)

            elif extruder_only:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    self._unload_extruder(extruder_only=True, validate=form_tip == self.FORM_TIP_STANDALONE)
                else:
                    self.log_debug("Assertion failure: Unexpected state %d in unload_sequence(extruder_only=True)" % start_filament_pos)
                    raise MmuError("Cannot unload extruder because filament not detected in extruder!")

            elif start_filament_pos == self.FILAMENT_POS_UNLOADED:
                self.log_debug("Assertion failure: Unexpected state %d in unload_sequence()" % start_filament_pos)
                raise MmuError("Cannot unload because already unloaded!")

            else:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload encoder
                    self._unload_extruder(validate=form_tip == self.FORM_TIP_STANDALONE)

                if start_filament_pos >= self.FILAMENT_POS_END_BOWDEN:
                    # Fast unload of bowden, then unload encoder
                    bowden_move_ratio = self._unload_bowden(bowden_move)
                    homing_movement = self._unload_gate()

                elif start_filament_pos >= self.FILAMENT_POS_HOMED_GATE:
                    # Have to do slow unload because we don't know exactly where we are
                    self._unload_gate(homing_max=bowden_move) # Full slow unload

            if unload_to_buffer and self.gate_status[self.gate_selected] != self.GATE_EMPTY:
                self._set_gate_status(self.gate_selected, self.GATE_AVAILABLE_FROM_BUFFER)

            # If runout then over unload to prevent accidental reload
            if runout and self.endless_spool_final_eject > 0.:
                self.log_info("Ejecting filament from MMU...")
                _,_,measured,_ = self.trace_filament_move("EndlessSpool final eject", -self.endless_spool_final_eject)

            # Encoder based validation test
            if self._can_use_encoder():
                movement = self.selector.filament_release(measure=True)
                if movement > self.encoder_min:
                    self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
                    self.log_trace("Encoder moved %.1fmm when filament was released!" % movement)
                    raise MmuError("Encoder sensed movement when the servo was released\nConcluding filament is stuck somewhere")
            else:
                self.selector.filament_release()

            self.movequeues_wait()
            msg = "Unload of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                msg += " {1}(encoder measured %.1fmm){0}" % self.get_encoder_distance(dwell=None)
            self.log_info(msg, color=True)

            # Finally autotune calibrated bowden length
            if full and not extruder_only and not self.gcode_unload_sequence:
                self._autotune(self.DIRECTION_UNLOAD, bowden_move_ratio, homing_movement)

        except MmuError as ee:
            if not extruder_only:
                self._track_gate_statistics('unload_failures', self.gate_selected)
            raise MmuError("Unload sequence failed: %s" % (str(ee)))

        finally:
            if full:
                self._track_time_end('unload')
                self._track_gate_statistics('unloads', self.gate_selected)
            if not extruder_only:
                self._set_action(current_action)


    # Form tip prior to extraction from the extruder. This can take the form of shaping the filament or could simply
    # activate a filament cutting mechanism. Sets filament position based on park pos
    # Returns True if filament is detected
    def form_tip_standalone(self, extruder_only=False):
        self.movequeues_wait()

        # Pre check to validate the presence of filament in the extruder and case where we don't need to form tip
        if self.sensor_manager.check_sensor(self.ENDSTOP_EXTRUDER_ENTRY) or self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD):
            filament_initially_present = True
        else:
            # Only the "extruder" sensor can definitely answer but believe toolhead if that is all we have
            filament_initially_present = self.sensor_manager.check_sensor(self.ENDSTOP_EXTRUDER_ENTRY)
            if filament_initially_present is None:
                filament_initially_present = self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD)

        if filament_initially_present is False:
            self.log_debug("Tip forming skipped because no filament was detected")

            if self.filament_pos == self.FILAMENT_POS_LOADED:
                self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

            self._set_filament_position(-self.toolhead_extruder_to_nozzle)
            return False

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.form_tip_macro)

        with self.wrap_action(self.ACTION_FORMING_TIP):
            synced = self.sync_form_tip and not extruder_only
            self._sync_gear_to_extruder(synced, grip=True, current=False)
            self._ensure_safe_extruder_temperature(wait=True)

            # Perform the tip forming move and establish park_pos
            initial_encoder_position = self.get_encoder_distance()
            park_pos, remaining, reported = self._do_form_tip()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            self._set_filament_remaining(remaining)

            # Encoder based validation test
            detected = True # Start with assumption that filament was present
            if self._can_use_encoder() and not reported:
                # Logic to try to validate success and update presence of filament based on movement
                if filament_initially_present is True:
                    # With encoder we might be able to check for clog now
                    if not measured > self.encoder_min:
                        raise MmuError("No encoder movement: Concluding filament is stuck in extruder")
                else:
                    # Couldn't determine if we initially had filament at start (lack of sensors)
                    if not measured > self.encoder_min:
                        # No movement. We can be confident we are/were empty
                        detected = False
                    elif synced:
                        # A further test is needed to see if the filament is actually in the extruder
                        detected, moved = self._test_filament_in_extruder_by_retracting()
                        park_pos += moved

            self._set_filament_position(-park_pos)
            self.set_encoder_distance(initial_encoder_position + park_pos)

            if detected or extruder_only:
                # Definitely in extruder
                self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
            else:
                # No detection. Best to assume we are somewhere in bowden for defensive unload
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

            return detected

    def _do_form_tip(self, test=False):
        with self._wrap_extruder_current(self.extruder_form_tip_current, "for tip forming move"):
            initial_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position()
            initial_encoder_position = self.get_encoder_distance()

            with self._wrap_pressure_advance(0., "for tip forming"):
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, "_MMU_FORM_TIP")
                self.log_info("Forming tip...")
                self._wrap_gcode_command("%s %s" % (self.form_tip_macro, "FINAL_EJECT=1" if test else ""), exception=True)
                self.movequeues_wait()

            final_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position()
            stepper_movement = (initial_mcu_pos - final_mcu_pos) * self.mmu_extruder_stepper.stepper.get_step_dist()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            park_pos = gcode_macro.variables.get("output_park_pos", -1)
            try:
                park_pos = float(park_pos)
            except ValueError as e:
                self.log_error("Reported 'output_park_pos: %s' could not be parsed: %s" % (park_pos, str(e)))
                park_pos = -1

            if park_pos < 0:
                # Use stepper movement
                reported = False
                filament_remaining = 0.
                park_pos = stepper_movement + self.toolhead_residual_filament + self.toolchange_retract
                msg = "After tip forming, extruder moved: %.1fmm thus park_pos calculated as %.1fmm (encoder measured %.1fmm)" % (stepper_movement, park_pos, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)
            else:
                # Means the macro reported it (usually for filament cutting)
                reported = True
                filament_remaining = park_pos - stepper_movement - self.toolhead_residual_filament - self.toolchange_retract
                msg = "After tip forming, park_pos reported as: %.1fmm with calculated %.1fmm filament remaining in extruder (extruder moved: %.1fmm, encoder measured %.1fmm)" % (park_pos, filament_remaining, stepper_movement, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)

            if not test and park_pos > self.toolhead_extruder_to_nozzle:
                self.log_error("Warning: Park_pos (%.1fmm) cannot be greater than 'toolhead_extruder_to_nozzle' distance of %.1fmm! Assumming fully unloaded from extruder\nWill attempt to continue..." % (park_pos, self.toolhead_extruder_to_nozzle))
                park_pos = self.toolhead_extruder_to_nozzle
                filament_remaining = 0.

        return park_pos, filament_remaining, reported


#################################
# FILAMENT MOVEMENT AND CONTROL #
#################################

    # Convenience wrapper around all gear and extruder motor movement that retains sync state, tracks movement and creates trace log
    # motor = "gear"           - gear motor(s) only on rail
    #         "gear+extruder"  - gear and extruder included on rail
    #         "extruder"       - extruder only on gear rail
    #         "both"           - gear and extruder together but independent (legacy, homing move not possible)
    #         "synced"         - gear synced with extruder as in print (homing move not possible)
    #
    # If homing move then endstop name can be specified.
    #         "mmu_gate"       - at the gate on MMU (when motor includes "gear")
    #         "extruder"       - just before extruder entrance (motor includes "gear" or "extruder")
    #         "toolhead"       - after extruder entrance (motor includes "gear" or "extruder")
    #         "mmu_gear_touch" - stallguard on gear (when motor includes "gear", only useful for motor="gear")
    #         "mmu_ext_touch"  - stallguard on nozzle (when motor includes "extruder", only useful for motor="extruder")
    #
    # All move distances are interpreted as relative
    # 'sync' will synchronize the MMU toolhead and Printer toolhead move queues before move
    # 'wait' will wait on appropriate move queue(s) after completion of move (forced to True if need encoder reading)
    # 'measure' whether we need to wait and measure encoder for movement
    # 'encoder_dwell' delay some additional time to ensure we have accurate encoder reading (if encoder fitted and required for measuring)
    #
    # All moves return: actual (relative), homed, measured, delta; mmu_toolhead.get_position[1] holds absolute position
    #
    def trace_filament_move(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0, endstop_name="default", track=False, sync=False, wait=False, encoder_dwell=False):
        self.mmu_toolhead.unsync() # Precaution
        encoder_start = self.get_encoder_distance(dwell=encoder_dwell)
        pos = self.mmu_toolhead.get_position()
        ext_pos = self.toolhead.get_position()
        homed = False
        actual = dist
        delta = 0.
        null_rtn = (0., False, 0., 0.)

        if homing_move != 0:
            # Check for valid endstop
            endstop = self.gear_rail.get_extra_endstop(endstop_name) if endstop_name is not None else self.gear_rail.get_endstops()
            if endstop is None:
                self.log_error("Endstop '%s' not found" % endstop_name)
                return null_rtn

        # Set sensible speeds and accelaration if not supplied
        if motor in ["gear"]:
            if homing_move != 0:
                speed = speed or self.gear_homing_speed
                accel = accel or min(self.gear_from_buffer_accel, self.gear_from_spool_accel)
            else:
                if abs(dist) > self.gear_short_move_threshold:
                    if self.gate_selected >= 0 and self.gate_status[self.gate_selected] != self.GATE_AVAILABLE_FROM_BUFFER and dist > 0:
                        speed = speed or self.gear_from_spool_speed
                        accel = accel or self.gear_from_spool_accel
                    else:
                        speed = speed or self.gear_from_buffer_speed
                        accel = accel or self.gear_from_buffer_accel
                else:
                    speed = speed or self.gear_short_move_speed
                    accel = accel or self.gear_short_move_accel

        elif motor in ["both", "gear+extruder", "synced"]:
            if homing_move != 0:
                speed = speed or min(self.gear_homing_speed, self.extruder_homing_speed)
                accel = accel or min(max(self.gear_from_buffer_accel, self.gear_from_spool_accel), self.extruder_accel)
            else:
                speed = speed or (self.extruder_sync_load_speed if dist > 0 else self.extruder_sync_unload_speed)
                accel = accel or min(max(self.gear_from_buffer_accel, self.gear_from_spool_accel), self.extruder_accel)

        elif motor in ["extruder"]:
            if homing_move != 0:
                speed = speed or self.extruder_homing_speed
                accel = accel or self.extruder_accel
            else:
                speed = speed or (self.extruder_load_speed if dist > 0 else self.extruder_unload_speed)
                accel = accel or self.extruder_accel

        else:
            self.log_error("Assertion failure: Invalid motor specification '%s'" % motor)
            return null_rtn

        # Apply pre-gate speed override
        if self.gate_selected >= 0:
            adjust = self.gate_speed_override[self.gate_selected] / 100.
            speed *= adjust
            accel *= adjust

        if sync:
            self.movequeues_sync()

        # Gear rail is driving the filament
        if motor in ["gear", "gear+extruder", "extruder"]:
            with self._wrap_sync_mode(MmuToolHead.EXTRUDER_SYNCED_TO_GEAR if motor == "gear+extruder" else MmuToolHead.EXTRUDER_ONLY_ON_GEAR if motor == "extruder" else None):
                if homing_move != 0:
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstop, self.mmu_toolhead)
                    init_ext_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position() # For non-homing extruder or if extruder not on gear rail
                    init_pos = pos[1]
                    pos[1] += dist
                    for _ in range(self.canbus_comms_retries):  # HACK: We can repeat because homing move
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
                                if self.log_enabled(self.LOG_STEPPER):
                                    self.log_stepper("Did not home: %s" % str(e))
                            homed = False
                        finally:
                            halt_pos = self.mmu_toolhead.get_position()
                            ext_actual = (self.mmu_extruder_stepper.stepper.get_mcu_position() - init_ext_mcu_pos) * self.mmu_extruder_stepper.stepper.get_step_dist()

                            # Support setup where a non-homing extruder is being used
                            if motor == "extruder" and not self.mmu_machine.homing_extruder:
                                # This isn't super accurate if extruder isn't (homing) MmuExtruder because doesn't have required endstop, thus this will
                                # overrun and even move slightly even if already homed. We can only correct the actual gear rail position.
                                halt_pos[1] += ext_actual
                                self.mmu_toolhead.set_position(halt_pos) # Correct the gear rail position

                            actual = halt_pos[1] - init_pos
                            if self.log_enabled(self.LOG_STEPPER):
                                self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, sync=%s, wait=%s >> %s" % (motor.upper(), dist, speed, accel, endstop_name, sync, wait, "%s halt_pos=%.1f (rail moved=%.1f, extruder moved=%.1f), trig_pos=%.1f" % ("HOMED" if homed else "DID NOT HOMED",  halt_pos[1], actual, ext_actual, trig_pos[1])))
                        if not got_comms_timeout:
                            break
                else:
                    if self.log_enabled(self.LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                    pos[1] += dist
                    with self.wrap_accel(accel):
                        self.mmu_toolhead.move(pos, speed)

        # Extruder is driving, gear rail is following
        elif motor in ["synced"]:
            with self._wrap_sync_mode(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER):
                self._ensure_safe_extruder_temperature(wait=False)
                if homing_move != 0:
                    self.log_error("Not possible to perform homing move while synced")
                else:
                    if self.log_enabled(self.LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)

        # Independent motors. Unsynced move
        elif motor == "both":
            with self._wrap_sync_mode(None):
                if homing_move != 0:
                    self.log_error("Not possible to perform homing move on two independent steppers")
                else:
                    self._ensure_safe_extruder_temperature(wait=False)
                    if self.log_enabled(self.LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                    pos[1] += dist
                    with self.wrap_accel(accel):
                        self.mmu_toolhead.move(pos, speed)
                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)

        if wait:
            self.movequeues_wait()
        else:
            self.mmu_toolhead.flush_step_generation() # TTC mitigation
            self.toolhead.flush_step_generation()     # TTC mitigation

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

    @contextlib.contextmanager
    def wrap_accel(self, accel):
        self.mmu_toolhead.get_kinematics().set_accel_limit(accel)
        try:
            yield self
        finally:
            self.mmu_toolhead.get_kinematics().set_accel_limit(None)


##############################################
# GENERAL FILAMENT RECOVERY AND MOVE HELPERS #
##############################################

    # This is a recovery routine to determine the most conservative location of the filament for unload purposes
    def recover_filament_pos(self, strict=False, can_heat=True, message=False, silent=False):
        with self._wrap_sync_gear_to_extruder():
            if message:
                self.log_info("Attempting to recover filament position...")

            ts = self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD)
            es = self.sensor_manager.check_sensor(self.ENDSTOP_EXTRUDER_ENTRY)
            gs = self.sensor_manager.check_sensor(self._get_gate_endstop_name())
            filament_detected = self.sensor_manager.check_any_sensors_in_path()
            if not filament_detected:
                filament_detected = self.check_filament_in_mmu() # Include encoder detection method

            # Loaded
            if ts:
                self._set_filament_pos_state(self.FILAMENT_POS_LOADED, silent=silent)

            # Somewhere in extruder
            elif filament_detected and can_heat and self._check_filament_still_in_extruder(): # Encoder based
                self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming
            elif ts is False and filament_detected and (self.strict_filament_recovery or strict) and can_heat and self._check_filament_still_in_extruder():
                # This case adds an additional encoder based test to see if filament is still being gripped by extruder
                # even though TS doesn't see it. It's a pedantic option so on turned on by strict flag
                self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming

            # At extruder entry
            elif es:
                self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY, silent=silent) # Allows for fast bowden unload move

            # Somewhere in bowden
            elif gs or filament_detected:
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN, silent=silent) # Prevents fast bowden unload move

            # Unloaded
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=silent)

        # If filament is found then ensure gate status is correct
        if self.gate_selected != self.TOOL_GATE_UNKNOWN and self.filament_pos >= self.FILAMENT_POS_START_BOWDEN and self.gate_status[self.gate_selected] == self.GATE_EMPTY:
            self._set_gate_status(self.gate_selected, self.GATE_AVAILABLE)

    # Check for filament in MMU using available sensors or encoder
    def check_filament_in_mmu(self):
        self.log_debug("Checking for filament in MMU...")
        detected = self.sensor_manager.check_any_sensors_in_path()
        if detected is None and self.has_encoder():
            self.selector.filament_drive()
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
            detected = False # Don't expect to get here but assume no filament
        return detected

    # Return True if filament runout detected by sensors
    def check_filament_runout(self):
        self.log_debug("Checking for runout...")
        runout = self.sensor_manager.check_for_runout()
        if runout is None and self.has_encoder():
            self.selector.filament_drive()
            detected = not self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
            runout = not detected
        if runout is None:
            self.log_debug("No sensors configured!")
            runout = False # Don't expect to get here but assume not runout
        return runout

    # Retract the filament by the extruder stepper only and see if we do not have any encoder movement
    # This assumes that we already tip formed, and the filament is parked somewhere in the extruder
    # Return if filament detected and distance filament moved during test
    def _test_filament_in_extruder_by_retracting(self, length=None):
        with self._require_encoder():
            self.log_debug("Testing for filament in extruder by retracting on extruder stepper only")
            move = self.encoder_move_step_size if length is None else length
            self.selector.filament_release()
            _,_,measured,_ = self.trace_filament_move("Moving extruder to test for filament exit", -move, speed=self.extruder_unload_speed, motor="extruder")
            detected = measured > self.encoder_min
            self.log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
            return detected, measured

    # Check for filament in extruder by moving extruder motor. Even with toolhead sensor this
    # can happen if the filament is in the short distance from sensor to gears
    def _check_filament_still_in_extruder(self):
        if self.has_encoder():
            self.log_debug("Checking for possibility of filament still in extruder gears...")
            self._ensure_safe_extruder_temperature(wait=False)
            self.selector.filament_release()
            move = self.encoder_move_step_size
            _,_,measured,_ = self.trace_filament_move("Checking extruder", -move, speed=self.extruder_unload_speed, motor="extruder")
            detected = measured > self.encoder_min
            self.log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
            return detected
        return False

    def buzz_gear_motor(self):
        if self.has_encoder():
            with self._require_encoder():
                initial_encoder_position = self.get_encoder_distance()
                self.trace_filament_move(None, 2.5 * self.encoder_resolution, accel=self.gear_buzz_accel, encoder_dwell=None)
                self.trace_filament_move(None, -2.5 * self.encoder_resolution, accel=self.gear_buzz_accel, encoder_dwell=None)
                measured = self.get_encoder_distance() - initial_encoder_position
                self.log_trace("After buzzing gear motor, encoder measured %.2f" % measured)
                self.set_encoder_distance(initial_encoder_position, dwell=None)
                return measured > self.encoder_min
        else:
            self.trace_filament_move(None, 5, accel=self.gear_buzz_accel)
            self.trace_filament_move(None, -5, accel=self.gear_buzz_accel)
        return None

    # Sync/unsync gear motor with extruder, handle filament engagement and current control
    # servo: True=move, False=don't mess
    # current: True=optionally reduce, False=restore to current default
    # Returns True if the gear was previously synced, otherwise False
    def _sync_gear_to_extruder(self, sync, grip=False, current=False):
        if self.gate_selected < 0: # Safety in case somehow called with bypass/unknown selected
            sync = current = False
        if grip:
            _ = self.selector.filament_drive() if sync else self._auto_filament_grip()
        _ = self._adjust_gear_current(self.sync_gear_current, "for extruder syncing") if current and sync else self._restore_gear_current()
        self.movequeues_wait() # Safety but should not be required(?)
        return self.mmu_toolhead.sync(MmuToolHead.GEAR_SYNCED_TO_EXTRUDER if sync else None) == MmuToolHead.GEAR_SYNCED_TO_EXTRUDER

    # This is used to protect the in print synchronization state and is used as an outermost wrapper for
    # calls back into Happy Hare during a print. It ensures that grip (servo) and current are correctly restored,
    # but like the rest of Happy Hare it employs lazy grip (servo) movement to reduce "flutter"
    @contextlib.contextmanager
    def _wrap_sync_gear_to_extruder(self):
        prev_gear_synced = self._sync_gear_to_extruder(False, grip=False, current=True)
        try:
            yield self
        finally:
            self._sync_gear_to_extruder(prev_gear_synced, grip=True, current=True)

    # This is used to protect just the mmu_toolhead sync state and is used to wrap individual moves. Typically
    # the starting state will be unsynced so this will simply unsync at the end of the move. It does not manage
    # grip (servo) movment or current control since that would lead to unecessary "flutter" and prematurely wear
    @contextlib.contextmanager
    def _wrap_sync_mode(self, sync_mode):
        prev_sync_mode = self.mmu_toolhead.sync_mode
        self.mmu_toolhead.sync(sync_mode)
        try:
            yield self
        finally:
            self.mmu_toolhead.sync(prev_sync_mode)

    def _adjust_gear_current(self, percent=100, reason=""):
        if self.gear_tmc and 0 < percent < 200 and percent != self.gear_percentage_run_current:
            self.log_info("Modifying MMU gear stepper run current to %d%% %s" % (percent, reason))
            self._set_tmc_current(mmu_machine.GEAR_STEPPER_CONFIG, (self.gear_default_run_current * percent) / 100., self.gear_tmc)
            self.gear_percentage_run_current = percent

    def _restore_gear_current(self):
        if self.gear_tmc and self.gear_percentage_run_current != self.gear_restore_percent_run_current:
            self.log_info("Restoring MMU gear stepper run current to %d%% configured" % self.gear_restore_percent_run_current)
            self._set_tmc_current(mmu_machine.GEAR_STEPPER_CONFIG, self.gear_default_run_current, self.gear_tmc)
            self.gear_percentage_run_current = self.gear_restore_percent_run_current

    @contextlib.contextmanager
    def _wrap_gear_current(self, percent=100, reason=""):
        self._adjust_gear_current(percent, reason)
        self.gear_restore_percent_run_current = percent # This will force restoration to this current not original (collision detection case)
        try:
            yield self
        finally:
            self.gear_restore_percent_run_current = 100
            self._restore_gear_current()

    @contextlib.contextmanager
    def _wrap_extruder_current(self, percent=100, reason=""):
        self._adjust_extruder_current(percent, reason)
        try:
            yield self
        finally:
            self._restore_extruder_current()

    def _adjust_extruder_current(self, percent=100, reason=""):
        if self.extruder_tmc and 0 < percent < 200 and percent != self.extruder_percentage_run_current:
            self.log_info("Modifying extruder stepper run current to %d%% %s" % (percent, reason))
            self._set_tmc_current(self.extruder_name, (self.extruder_default_run_current * percent) / 100., self.extruder_tmc)
            self.extruder_percentage_run_current = percent

    def _restore_extruder_current(self):
        if self.extruder_tmc and self.extruder_percentage_run_current != 100:
            self.log_info("Restoring extruder stepper run current to 100% configured")
            self._set_tmc_current(self.extruder_name, self.extruder_default_run_current, self.extruder_tmc)
            self.extruder_percentage_run_current = 100

    def _set_tmc_current(self, stepper, run_current, tmc):
        self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (stepper, run_current))
        # Duplicate klipper logic to avoid duplicate console messages (except klipper doesn't expose the cmdhelper object)
        #if tmc:
        #    print_time = self.toolhead.get_last_move_time()
        #    prev_cur, prev_hold_cur, req_hold_cur, max_cur = tmc.current_helper.get_current()
        #    tmc.current_helper.set_current(min(run_current, max_cur), req_hold_current, print_time)

    @contextlib.contextmanager
    def _wrap_pressure_advance(self, pa=0, reason=""):
        initial_pa = self.toolhead.get_extruder().get_status(0).get('pressure_advance', None)
        if initial_pa is not None:
            if reason:
                self.log_debug("Setting pressure advance %s: %.6f" % (reason, pa))
            self._set_pressure_advance(pa)
        try:
            yield self
        finally:
            if initial_pa is not None:
                if reason:
                    self.log_debug("Restoring pressure advance: %.6f" % initial_pa)
                self._set_pressure_advance(initial_pa)

    def _set_pressure_advance(self, pa):
        self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f" % pa)
        # TODO avoid klipper console messages?

    def _move_cmd(self, gcmd, trace_str):
        if self.check_if_disabled(): return (0., False, 0., 0.)
        if self.check_if_bypass(): return (0., False, 0., 0.)
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None)
        motor = gcmd.get('MOTOR', "gear")
        wait = bool(gcmd.get_int('WAIT', 0, minval=0, maxval=1))
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        if motor not in ["gear", "extruder", "gear+extruder", "synced", "both"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder', 'synced' or 'both'")
        if motor == "extruder":
            self.selector.filament_release()
        else:
            self.selector.filament_drive()
        self.log_debug("Moving '%s' motor %.1fmm..." % (motor, move))
        return self.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, sync=sync, wait=wait)

    def _homing_move_cmd(self, gcmd, trace_str):
        if self.check_if_disabled(): return (0., False, 0., 0.)
        if self.check_if_bypass(): return (0., False, 0., 0.)
        endstop = gcmd.get('ENDSTOP', "default")
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None) # Ignored for extruder led moves
        motor = gcmd.get('MOTOR', "gear")
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        if motor not in ["gear", "extruder", "gear+extruder"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder'")
        direction = -1 if move < 0 else 1
        stop_on_endstop = gcmd.get_int('STOP_ON_ENDSTOP', direction, minval=-1, maxval=1)
        valid_endstops = list(self.gear_rail.get_extra_endstop_names())
        if endstop not in valid_endstops:
            raise gcmd.error("Endstop name '%s' is not valid for motor '%s'. Options are: %s" % (endstop, motor, ', '.join(valid_endstops)))
        if self.gear_rail.is_endstop_virtual(endstop) and stop_on_endstop == -1:
            raise gcmd.error("Cannot reverse home on virtual (TMC stallguard) endstop '%s'" % endstop)
        if motor == "extruder":
            self.selector.filament_release()
        else:
            self.selector.filament_drive()
        self.log_debug("Homing '%s' motor to '%s' endstop, up to %.1fmm..." % (motor, endstop, move))
        return self.trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, homing_move=stop_on_endstop, endstop_name=endstop, sync=sync)


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

    # Primary method to select and loads tool. Assumes we are unloaded.
    def _select_and_load_tool(self, tool):
        self.log_debug('Loading tool T%d...' % tool)
        self.select_tool(tool, move_servo=False)
        gate = self.ttg_map[tool]
        if self.gate_status[gate] == self.GATE_EMPTY:
            if self.enable_endless_spool and self.endless_spool_on_load:
                next_gate, msg = self._get_next_endless_spool_gate(tool, gate)
                if next_gate == -1:
                    raise MmuError("Gate %d is empty!\nNo alternatives gates available after checking %s" % (gate, msg))

                self.log_error("Gate %d is empty! Checking for alternative gates %s" % (gate, msg))
                self.log_info("Remapping T%d to Gate %d" % (tool, next_gate))
                self._remap_tool(tool, next_gate)
                self.select_tool(tool, move_servo=False)
            else:
                raise MmuError("Gate %d is empty (and EndlessSpool on load is disabled)\nLoad gate, remap tool to another gate or correct state with 'MMU_CHECK_GATE GATE=%d' or 'MMU_GATE_MAP GATE=%d AVAILABLE=1'" % (gate, gate, gate))

        with self._wrap_track_time('pre_load'):
            self._wrap_gcode_command(self.pre_load_macro, exception=True, wait=True)
        self.load_sequence()
        self._spoolman_activate_spool(self.gate_spool_id[gate]) # Activate the spool in Spoolman
        self._restore_tool_override(self.tool_selected) # Restore M220 and M221 overrides
        with self._wrap_track_time('post_load'):
            self._wrap_gcode_command(self.post_load_macro, exception=True, wait=True)

    # Primary method to unload current tool but retains selection
    def _unload_tool(self, form_tip=None, runout=False):
        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            self.log_info("Tool already unloaded")
            return

        self.log_debug("Unloading tool %s" % self._selected_tool_string())
        self._set_last_tool(self.tool_selected)
        with self._wrap_track_time('pre_unload'):
            self._wrap_gcode_command(self.pre_unload_macro, exception=True, wait=True)
        self._record_tool_override() # Remember M220 and M221 overrides
        self.unload_sequence(form_tip=form_tip if not None else self.FORM_TIP_STANDALONE, runout=runout)
        self._spoolman_activate_spool(0) # Deactivate in SpoolMan
        with self._wrap_track_time('post_unload'):
            self._wrap_gcode_command(self.post_unload_macro, exception=True, wait=True)

    def _auto_home(self, tool=0):
        if not self.selector.is_homed or self.tool_selected == self.TOOL_GATE_UNKNOWN:
            self.log_info("MMU not homed, will home before continuing")
            self.home(tool)
        elif self.filament_pos == self.FILAMENT_POS_UNKNOWN and self.selector.is_homed:
            self.recover_filament_pos(message=True)

    # Important to always inform use of "toolchange" operation is case there is an error and manual recovery is necessary
    def _note_toolchange(self, m117_msg):
        self._last_toolchange = m117_msg
        self.gcode.run_script_from_command("M117 %s" % m117_msg)

    # Tell the sequence macros about where to move to next
    def _set_next_position(self, next_pos):
        if next_pos:
            self._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_xy VALUE=%s,%s" % (self.park_macro, next_pos[0], next_pos[1]))
            self._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=True" % self.park_macro)
        else:
            self._wrap_gcode_command("SET_GCODE_VARIABLE MACRO=%s VARIABLE=next_pos VALUE=False" % self.park_macro)


### TOOL AND GATE SELECTION ######################################################

    def home(self, tool, force_unload = None):
        if self.check_if_bypass(): return
        self.unselect_tool()
        self.selector.home(force_unload=force_unload)
        if tool >= 0:
            self.select_tool(tool)
        elif tool == self.TOOL_GATE_BYPASS:
            self.select_bypass()

    def select_gate(self, gate):
        try:
            self.selector.select_gate(gate)
            self._set_gate_selected(gate)
        except MmuError as ee:
            self.unselect_gate()
            raise ee

    def unselect_gate(self):
        self._set_gate_selected(self.TOOL_GATE_UNKNOWN)

    def select_tool(self, tool, move_servo=True):
        if tool < 0 or tool >= self.num_gates:
            self.log_always("Tool %d does not exist" % tool)
            return

        gate = self.ttg_map[tool]
        if tool == self.tool_selected and gate == self.gate_selected:
            return

        self.log_debug("Selecting tool T%d on Gate %d..." % (tool, gate))
        self.select_gate(gate)
        self._set_tool_selected(tool)
        if move_servo:
            self._auto_filament_grip()
        self.log_info("Tool T%d enabled%s" % (tool, (" on Gate %d" % gate) if tool != gate else ""))

    def select_bypass(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.gate_selected == self.TOOL_GATE_BYPASS: return
        if not self.selector.has_bypass():
            self.log_always("Bypass not configured")
            return
        self.log_info("Selecting filament bypass...")
        self.select_gate(self.TOOL_GATE_BYPASS)
        self._set_tool_selected(self.TOOL_GATE_BYPASS)
        self._set_filament_direction(self.DIRECTION_LOAD)
        self.log_info("Bypass enabled")

    def unselect_tool(self):
        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        self._auto_filament_grip()

    def _set_tool_selected(self, tool):
        #self.log_error("PAUL TEMP: _set_tool_selected(%d)" % tool)
        if tool != self.tool_selected:
            self.tool_selected = tool
            self.save_variable(self.VARS_MMU_TOOL_SELECTED, self.tool_selected, write=True)

    def _set_gate_selected(self, gate):
        #self.log_error("PAUL TEMP: _set_gate_selected(%d)" % gate)
        if gate != self.gate_selected:
            self.gate_selected = gate
            self.save_variable(self.VARS_MMU_GATE_SELECTED, self.gate_selected, write=True)
        self._set_rotation_distance(self._get_rotation_distance(self.gate_selected))
        self._update_sync_multiplier()
        self.active_filament = {
            'filament_name': self.gate_filament_name[gate],
            'material': self.gate_material[gate],
            'color': self.gate_color[gate],
            'spool_id': self.gate_spool_id[gate],
            'temperature': self.gate_temperature[gate],
        } if gate >= 0 else {}

    def _get_rotation_distance(self, gate):
        return self.rotation_distances[gate if gate >= 0 and self.mmu_machine.variable_rotation_distances else 0]

    def _set_rotation_distance(self, rd):
        if rd <= 0:
            rd = self.rotation_distances[0] if self.rotation_distances[0] > 0 else self.default_rotation_distance
            self.log_debug("Gate not calibrated, falling back to: %.6f" % rd)
        else:
            self.log_trace("Setting gear motor rotation distance: %.6f" % rd)
        #self.log_error("PAUL TEMP: _set_rotation_distance(%s)" % rd)
        self.gear_rail.steppers[0].set_rotation_distance(rd)

    def _get_bowden_length(self, gate):
        return self.bowden_lengths[gate if gate >= 0 and self.mmu_machine.variable_bowden_lengths else 0]

    # Used to update/persist bowden length during calibration or MMU_TEST_CONFIG
    def _save_bowden_length(self, gate, length, endstop=None):
        if gate >= 0:
            if endstop:
                self._adjust_bowden_lengths()
            self.bowden_lengths[gate] = length
            if gate == 0 and not self.mmu_machine.variable_bowden_lengths:
                self.bowden_lengths = [self.bowden_lengths[0]] * self.num_gates
            self.save_variable(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, self.bowden_lengths)
            if not any(x == -1 for x in self.bowden_lengths):
                self.calibration_status |= self.CALIBRATED_BOWDENS
        else:
            self.log_debug("Assertion failure: cannot save bowden length for gate: %d" % gate)

    # Adjustment if gate endstop has changed
    def _adjust_bowden_lengths(self):
        current_home = self.save_variables.allVariables.get(self.VARS_MMU_CALIB_BOWDEN_HOME, None)
        if self.gate_homing_endstop != current_home:
            adjustment = 0
            if current_home == self.ENDSTOP_ENCODER:
                adjustment = self.gate_endstop_to_encoder
            elif self.gate_homing_endstop == self.ENDSTOP_ENCODER:
                adjustment = -self.gate_endstop_to_encoder
            self.bowden_lengths = [length + adjustment if length != -1 else length for length in self.bowden_lengths]
            self.log_debug("Adjusted bowden lengths by %.1f: %s because of gate_homing_endstop change" % (adjustment, self.bowden_lengths))
            self.save_variable(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, self.bowden_lengths)
            self.save_variable(self.VARS_MMU_CALIB_BOWDEN_HOME, self.gate_homing_endstop)


### SPOOLMAN INTEGRATION #########################################################

    def _spoolman_sync(self, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_PULL: # Remote gate map
            # This will pull gate assignment and filament attributes from spoolman db thus replacing the local map
            self._spoolman_pull_gate_map(quiet=quiet)
        elif self.spoolman_support == self.SPOOLMAN_PUSH: # Local gate map
            # This will update spoolman with just the gate assignment (for visualization) and will update
            # local gate map attributes with data from spoolman thus overwriting the local map
            self._spoolman_push_gate_map(quiet=quiet)
        elif self.spoolman_support == self.SPOOLMAN_READONLY: # Get filament attributes only
            self._spoolman_update_filaments(quiet=quiet)

    def _spoolman_activate_spool(self, spool_id=-1):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            if spool_id < 0:
                self.log_debug("Spoolman spool_id not set for current gate")
            else:
                if spool_id == 0:
                    self.log_debug("Deactivating spool...")
                else:
                    self.log_debug("Activating spool %s..." % spool_id)
                webhooks.call_remote_method("spoolman_set_active_spool", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while setting active spool: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Request to send filament data from spoolman db (via moonraker)
    # gate=None means all gates with spool_id, else specific gate
    def _spoolman_update_filaments(self, gate=None, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        gate_ids = []
        if gate is None: # All gates
            for i in range(self.num_gates):
                if self.gate_spool_id[i] >= 0:
                    gate_ids.append((i, self.gate_spool_id[i]))
        elif self.gate_spool_id[gate] >= 0:
            gate_ids.append((gate, self.gate_spool_id[gate]))
        if len(gate_ids) > 0:
            self.log_debug("Requesting the following gate/spool_id pairs from Spoolman: %s" % gate_ids)
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("spoolman_get_filaments", gate_ids=gate_ids, silent=quiet)
            except Exception as e:
                self.log_error("Error while fetching filament attributes from spoolman: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Store the current gate to spool_id mapping in spoolman db (via moonraker)
    def _spoolman_push_gate_map(self, gate_ids=None, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Pushing gate mapping to Spoolman")
        if gate_ids is None: # All gates
            gate_ids = [(i, self.gate_spool_id[i]) for i in range(self.num_gates)]
        try:
            webhooks = self.printer.lookup_object('webhooks')
            self.log_debug("Storing gate map in spoolman db...")
            webhooks.call_remote_method("spoolman_push_gate_map", gate_ids=gate_ids, silent=quiet)
        except Exception as e:
            self.log_error("Error while pushing gate map to spoolman: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Request to update local gate based on the remote data stored in spoolman db
    def _spoolman_pull_gate_map(self, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Requesting the gate map from Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_pull_gate_map", silent=quiet)
        except Exception as e:
            self.log_error("Error while requesting gate map from spoolman: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Clear the spool to gate association in spoolman db
    def _spoolman_clear_gate_map(self, sync=False, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Requesting to clear the gate map in Spoolman")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_clear_spools_for_printer", sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while clearing spoolman gate mapping: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Refresh the spoolman cache to pick up changes from elsewhere
    def _spoolman_refresh(self, fix, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Requesting to refresh the spoolman gate cache")
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_refresh", fix=fix, silent=quiet)
        except Exception as e:
            self.log_error("Error while refreshing spoolman gate cache: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Force spool to map association in spoolman db
    def _spoolman_set_spool_gate(self, spool_id, gate, sync=False, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Setting spool %d to gate %d directly in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_set_spool_gate", spool_id=spool_id, gate=gate, sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while setting spoolman gate association: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    def _spoolman_unset_spool_gate(self, spool_id=None, gate=None, sync=False, quiet=True):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        self.log_debug("Unsetting spool %s or gate %s in spoolman db" % (spool_id, gate))
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_unset_spool_gate", spool_id=spool_id, gate=gate, sync=sync, silent=quiet)
        except Exception as e:
            self.log_error("Error while unsetting spoolman gate association: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Dump spool info to console
    def _spoolman_display_spool_info(self, spool_id):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_get_spool_info", spool_id=spool_id)
        except Exception as e:
            self.log_error("Error while displaying spool info: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))

    # Dump spool info to console
    def _spoolman_display_spool_location(self, printer=None):
        if self.spoolman_support == self.SPOOLMAN_OFF: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            webhooks.call_remote_method("spoolman_display_spool_location", printer=printer)
        except Exception as e:
            self.log_error("Error while displaying spool location map: %s\n%s" % (str(e), self.SPOOLMAN_CONFIG_ERROR))


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
            self._spoolman_clear_gate_map(sync=self.spoolman_support == self.SPOOLMAN_PULL, quiet=quiet)
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
                self._spoolman_set_spool_gate(spool_id, gate, sync=self.spoolman_support == self.SPOOLMAN_PULL, quiet=quiet)
            elif spool_id is None and gate is not None:
                self._spoolman_unset_spool_gate(gate=gate, sync=self.spoolman_support == self.SPOOLMAN_PULL, quiet=quiet)
            elif spool_id is not None and gate is None:
                self._spoolman_unset_spool_gate(spool_id=spool_id, sync=self.spoolman_support == self.SPOOLMAN_PULL, quiet=quiet)

        elif not run:
            if self.spoolman_support in [self.SPOOLMAN_PULL, self.SPOOLMAN_PUSH]:
                # Display gate association table from spoolman db for specified printer
                self._spoolman_display_spool_location(printer=printer)
            else:
                self.log_error("Spoolman gate map not available. Spoolman mode is: %s" % self.spoolman_support)


### CORE GCODE COMMANDS ##########################################################

    cmd_MMU_HOME_help = "Home the MMU selector"
    def cmd_MMU_HOME(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        self._fix_started_state()

        if self.check_if_not_calibrated(self.CALIBRATED_SELECTOR):
            self.log_always("Not calibrated. Will home to endstop only!")
            tool = -1
            force_unload = 0
        else:
            tool = gcmd.get_int('TOOL', 0, minval=0, maxval=self.num_gates - 1)
            force_unload = gcmd.get_int('FORCE_UNLOAD', None, minval=0, maxval=1)
        try:
            with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
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
        if self.check_if_not_calibrated(self.CALIBRATED_SELECTOR): return
        self._fix_started_state()

        bypass = gcmd.get_int('BYPASS', -1, minval=0, maxval=1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        if tool == -1 and gate == -1 and bypass == -1:
            raise gcmd.error("Error on 'MMU_SELECT': missing TOOL, GATE or BYPASS")

        try:
            self._select(bypass, tool, gate)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_SELECT_BYPASS_help = "Select the filament bypass"
    def cmd_MMU_SELECT_BYPASS(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(self.CALIBRATED_SELECTOR): return
        self._fix_started_state()

        try:
            self._select(1, -1, -1)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    def _select(self, bypass, tool, gate):
        try:
            if bypass != -1:
                self.select_bypass()
            elif tool != -1:
                self.select_tool(tool)
            else:
                self.select_gate(gate)
                # Find the first tool that maps to this gate or current tool if it maps
                # (Remember multiple tools can map to the same gate)
                if self.tool_selected >= 0 and self.ttg_map[self.tool_selected] == gate:
                    pass
                else:
                    for t, value in enumerate(self.ttg_map):
                        if value == gate:
                            self.select_tool(t)
                            break
                    else:
                        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        finally:
            self._auto_filament_grip()

    cmd_MMU_CHANGE_TOOL_help = "Perform a tool swap (called from Tx command)"
    def cmd_MMU_CHANGE_TOOL(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[]): return # TODO Hard to tell what gates to check so don't check for now
        self._fix_started_state()

        self.last_statistics = {}
        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))

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
            tool = gcmd.get_int('TOOL', minval=0, maxval=self.num_gates - 1)

        try:
            with self._wrap_suspend_runout(): # Don't want runout accidently triggering during tool change
                with self._wrap_suspend_write_variables(): # Reduce I/O activity to a minimum
                    self._auto_home(tool=tool)

                    if self.has_encoder():
                        self.encoder_sensor.update_clog_detection_length()

                    form_tip = self.FORM_TIP_SLICER if (self.is_printing() and not (standalone or self.force_form_tip_standalone)) else self.FORM_TIP_STANDALONE
                    self.log_debug("Tool change initiated %s" % ("with slicer tip forming" if form_tip == self.FORM_TIP_SLICER else "with standalone MMU tip forming" if form_tip == self.FORM_TIP_STANDALONE else "without tip forming"))
                    current_tool_string = self._selected_tool_string()

                    # Check if we are already loaded
                    if tool == self.tool_selected and self.ttg_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
                        self.log_always("Tool T%d is already loaded" % tool)
                        return

                    # Load only case
                    if self.filament_pos == self.FILAMENT_POS_UNLOADED:
                        msg = "Tool change requested: T%d" % tool
                        m117_msg = "> T%d" % tool
                    elif self.tool_selected == tool:
                        msg = "Reloading: T%d" % tool
                        m117_msg = "> T%d" % tool
                    else:
                        # Normal toolchange case
                        msg = "Tool change requested, from %s to T%d" % (current_tool_string, tool)
                        m117_msg = "%s > T%d" % (current_tool_string, tool)

                    self._note_toolchange(m117_msg)
                    self.log_always(msg)

                    # Check if new tool is mapped to current gate
                    if self.ttg_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
                        self.select_tool(tool)
                        self._note_toolchange("T%s" % tool)
                        return

                    # Determine purge volume for current toolchange. Valid only during toolchange operation
                    self.toolchange_purge_volume = self._get_purge_volume(self.tool_selected, tool)

                    # Ok, now ready to park and perform the swap
                    self._next_tool = tool # Valid only during the change process
                    self._save_toolhead_position_and_park('toolchange', next_pos=next_pos)
                    with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
                        self._track_time_start('total')
                        self.printer.send_event("mmu:toolchange", self._last_tool, self._next_tool)

                        attempts = 2 if self.retry_tool_change_on_error and (self.is_printing() or standalone) else 1 # TODO: replace with inattention timer
                        try:
                            for i in range(attempts):
                                try:
                                    if self.filament_pos != self.FILAMENT_POS_UNLOADED:
                                        self._unload_tool(form_tip=form_tip)
                                    self._set_next_position(next_pos)
                                    self._select_and_load_tool(tool)
                                    break
                                except MmuError as ee:
                                    if i == attempts - 1:
                                        raise MmuError("%s.\nOccured when changing tool: %s" % (str(ee), self._last_toolchange))
                                    self.log_error("%s.\nOccured when changing tool: %s. Retrying..." % (str(ee), self._last_toolchange))
                                    # Try again but recover_filament_pos will ensure conservative treatment of unload
                                    self.recover_filament_pos()

                            self._track_swap_completed()
                            self._track_time_end('total')
                            self.gcode.run_script_from_command("M117 T%s" % tool)
                        finally:
                            self._next_tool = self.TOOL_GATE_UNKNOWN

                    # Updates swap statistics
                    self._dump_statistics(job=not quiet, gate=not quiet)
                    self._persist_swap_statistics()
                    self._persist_gate_statistics()

                    # Deliberately outside of _wrap_gear_synced_to_extruder() so there is no absolutely no delay after restoring position
                    self._continue_after('toolchange', restore=restore)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

        finally:
            self.toolchange_purge_volume = 0.

    cmd_MMU_LOAD_help = "Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTRUDER_ONLY=1)"
    def cmd_MMU_LOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        self._fix_started_state()

        self.last_statistics = {}
        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1) or in_bypass)
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        try:
            with self._wrap_suspend_runout(): # Don't want runout accidently triggering during filament load
                if self.filament_pos != self.FILAMENT_POS_UNLOADED:
                    self.log_always("Filament already loaded")
                    return

                self._note_toolchange("> %s" % self._selected_tool_string())
                with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
                    if not extruder_only:
                        self._save_toolhead_position_and_park('load')
                        self._select_and_load_tool(self.tool_selected)
                        self._persist_gate_statistics()
                        self._continue_after('load', restore=restore)
                    else:
                        self.load_sequence(bowden_move=0., extruder_only=True)
        except MmuError as ee:
            self.handle_mmu_error("%s.\nOccured when loading tool: %s" % (str(ee), self._last_toolchange))
            if self.tool_selected == self.TOOL_GATE_BYPASS:
                self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)

    cmd_MMU_EJECT_help = "aka MMU_UNLOAD Eject filament and park it in the MMU or optionally unloads just the extruder (EXTRUDER_ONLY=1)"
    def cmd_MMU_EJECT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        self._fix_started_state()

        self.last_statistics = {}
        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        form_tip = self.FORM_TIP_STANDALONE if not skip_tip else self.FORM_TIP_NONE

        try:
            with self._wrap_suspend_runout(): # Don't want runout accidently triggering during filament load
                if self.filament_pos == self.FILAMENT_POS_UNLOADED:
                    self.log_always("Filament not loaded")
                    return

                self._note_toolchange("")
                with self._wrap_sync_gear_to_extruder(): # Don't undo syncing if called in print
                    if not extruder_only:
                        self._save_toolhead_position_and_park('unload')
                        self._unload_tool(form_tip=form_tip)
                        self._persist_gate_statistics()
                        self._continue_after('unload', restore=restore)
                    else:
                        self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=True) # Ensure tool tip is performed
                        self.unload_sequence(bowden_move=0., form_tip=form_tip, extruder_only=True)
                        if in_bypass:
                            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                            self.log_always("Please pull the filament out clear of the MMU selector")
        except MmuError as ee:
            self.handle_mmu_error("%s.\nOccured when unloading tool" % str(ee))

    cmd_MMU_PRINT_START_help = "Forces initialization of MMU state ready for print (usually automatic)"
    def cmd_MMU_PRINT_START(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_in_print():
            self._on_print_start()
            self._clear_macro_state()

    cmd_MMU_PRINT_END_help = "Forces clean up of state after after print end"
    def cmd_MMU_PRINT_END(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        automatic = gcmd.get_int('AUTOMATIC', 0, minval=0, maxval=1)
        end_state = gcmd.get('STATE', "complete")
        if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
            if not automatic and end_state in ["complete"]:
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
    cmd_MMU_RESUME_help = "Wrapper around default RESUME macro"
    def cmd_MMU_RESUME(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            # User defined or Klipper default behavior
            self._wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), None)
            return

        self.log_debug("MMU RESUME wrapper called")
        if not self.is_printer_paused() and not self.is_mmu_paused():
            self.log_always("Print is not paused. Resume ignored.")
            return

        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        try:
            self._clear_mmu_error_dialog()
            if self.is_mmu_paused_and_locked():
                self._mmu_unlock()

            # Decide if we are ready to resume and give user opportunity to fix state first
            if self.sensor_manager.check_sensor(self.ENDSTOP_TOOLHEAD) is True:
                self._set_filament_pos_state(self.FILAMENT_POS_LOADED, silent=True)
                self.log_always("Automatically set filament state to LOADED based on toolhead sensor")
            if self.filament_pos not in [self.FILAMENT_POS_UNLOADED, self.FILAMENT_POS_LOADED]:
                raise MmuError("Cannot resume because filament position not indicated as fully loaded (or unloaded). Ensure filament is loaded/unloaded and run:\n MMU_RECOVER LOADED=1 or MMU_RECOVER LOADED=0 or just MMU_RECOVER\nto reset state, then RESUME again")

            # Prevent BASE_RESUME from moving toolhead
            if self.TOOLHEAD_POSITION_STATE in self.gcode_move.saved_states:
                gcode_pos = self.gcode_move.get_status(self.reactor.monotonic())['gcode_position']
                self.gcode_move.saved_states['PAUSE_STATE']['last_position'][:3] = gcode_pos[:3]

            self._wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())))
            self._continue_after("resume", force_in_print=force_in_print)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # Not a user facing command - used in automatic wrapper
    cmd_PAUSE_help = "Wrapper around default PAUSE macro"
    def cmd_PAUSE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._fix_started_state() # Get out of 'started' state before transistion to pause
            self.log_debug("MMU PAUSE wrapper called")
            self._save_toolhead_position_and_park("pause")
        self._wrap_gcode_command("__PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_CLEAR_PAUSE_help = "Wrapper around default CLEAR_PAUSE macro"
    def cmd_CLEAR_PAUSE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self.log_debug("MMU CLEAR_PAUSE wrapper called")
            self._clear_macro_state()
        self._wrap_gcode_command("__CLEAR_PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_CANCEL_PRINT_help = "Wrapper around default CANCEL_PRINT macro"
    def cmd_MMU_CANCEL_PRINT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._fix_started_state() # Get out of 'started' state before transistion to cancelled
            self.log_debug("MMU_CANCEL_PRINT wrapper called")
            self._clear_mmu_error_dialog()
            self._save_toolhead_position_and_park("cancel")
            self._wrap_gcode_command("__CANCEL_PRINT", None)
            self._on_print_end("cancelled")
        else:
            self._wrap_gcode_command("__CANCEL_PRINT", None) # User defined or Klipper default behavior

    cmd_MMU_RECOVER_help = "Recover the filament location and set MMU state after manual intervention/movement"
    def cmd_MMU_RECOVER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        tool = gcmd.get_int('TOOL', self.TOOL_GATE_UNKNOWN, minval=-2, maxval=self.num_gates - 1)
        mod_gate = gcmd.get_int('GATE', self.TOOL_GATE_UNKNOWN, minval=-2, maxval=self.num_gates - 1)
        loaded = gcmd.get_int('LOADED', -1, minval=0, maxval=1)
        strict = gcmd.get_int('STRICT', 0, minval=0, maxval=1)

        try:
            with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
                if self.TOOL_GATE_BYPASS in (tool, mod_gate) and not self.selector.has_bypass():
                    self.log_always("Bypass not configured")
                    return

                if tool == self.TOOL_GATE_BYPASS:
                    self._set_gate_selected(self.TOOL_GATE_BYPASS)
                    self._set_tool_selected(self.TOOL_GATE_BYPASS)
                    self.selector.restore_gate_position()
                elif tool >= 0: # If tool is specified then use and optionally override the gate
                    self._set_tool_selected(tool)
                    gate = self.ttg_map[tool]
                    if mod_gate >= 0:
                        gate = mod_gate
                    if gate >= 0:
                        self._set_gate_selected(gate)
                        self._remap_tool(tool, gate, loaded)
                        self.selector.restore_gate_position()
                elif tool == self.TOOL_GATE_UNKNOWN and self.tool_selected == self.TOOL_GATE_BYPASS and loaded == -1:
                    # This is to be able to get out of "stuck in bypass" state
                    self.log_info("Warning: Making assumption that bypass is unloaded")
                    self._set_filament_direction(self.DIRECTION_UNKNOWN)
                    self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=True)
                    self._auto_filament_grip()
                    return

                if loaded == 1:
                    self._set_filament_direction(self.DIRECTION_LOAD)
                    self._set_filament_pos_state(self.FILAMENT_POS_LOADED)
                    return
                elif loaded == 0:
                    self._set_filament_direction(self.DIRECTION_UNLOAD)
                    self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                    return

                # Filament position not specified so auto recover
                self.recover_filament_pos(strict=strict, message=True)
                self._auto_filament_grip()
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
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL): return
        loops = gcmd.get_int('LOOP', 10)
        rand = gcmd.get_int('RANDOM', 0)
        to_nozzle = gcmd.get_int('FULL', 0)
        try:
            for l in range(loops):
                self.log_always("Testing loop %d / %d" % (l, loops))
                for t in range(self.num_gates):
                    tool = t
                    if rand == 1:
                        tool = random.randint(0, self.num_gates - 1)
                    gate = self.ttg_map[tool]
                    if self.gate_status[gate] == self.GATE_EMPTY:
                        self.log_always("Skipping tool %d of %d because Gate %d is empty" % (tool, self.num_gates, gate))
                    else:
                        self.log_always("Testing tool %d of %d (Gate %d)" % (tool, self.num_gates, gate))
                        if not to_nozzle:
                            self.select_tool(tool)
                            self.load_sequence(bowden_move=100., skip_extruder=True)
                            self.unload_sequence(bowden_move=100.)
                        else:
                            self._select_and_load_tool(tool)
                            self._unload_tool()
            self.select_tool(0)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_TEST_GRIP_help = "Test the MMU grip for a Tool"
    def cmd_MMU_TEST_GRIP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        self.selector.filament_drive()
        self.motors_off(motor="gear")

    cmd_MMU_TEST_TRACKING_help = "Test the tracking of gear feed and encoder sensing"
    def cmd_MMU_TEST_TRACKING(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_homed(): return
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
        step = gcmd.get_float('STEP', 1, minval=0.5, maxval=20)
        sensitivity = gcmd.get_float('SENSITIVITY', self.encoder_resolution, minval=0.1, maxval=10)
        if direction == 0: return
        try:
            if self.filament_pos not in [self.FILAMENT_POS_START_BOWDEN, self.FILAMENT_POS_IN_BOWDEN]:
                # Ready MMU for test if not already setup
                self._unload_tool()
                self.load_sequence(bowden_move=100. if direction == self.DIRECTION_LOAD else 200., skip_extruder=True)
                self.selector.filament_drive()
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
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[self.gate_selected]): return
        full = gcmd.get_int('FULL', 0, minval=0, maxval=1)
        try:
            if full:
                self.load_sequence(skip_extruder=True)
            else:
                length = gcmd.get_float('LENGTH', 100., minval=10., maxval=self._get_bowden_length(self.gate_selected))
                self.load_sequence(bowden_move=length, skip_extruder=True)
        except MmuError as ee:
            self.handle_mmu_error("Load test failed: %s" % str(ee))

    cmd_MMU_TEST_MOVE_help = "Test filament move to help debug setup / options"
    def cmd_MMU_TEST_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1)) # Hidden option
        with DebugStepperMovement(self, debug):
            actual,_,measured,_ = self._move_cmd(gcmd, "Test move")
        self.log_always("Moved %.1fmm%s" % (actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_HOMING_MOVE_help = "Test filament homing move to help debug setup / options"
    def cmd_MMU_TEST_HOMING_MOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        debug = bool(gcmd.get_int('DEBUG', 0, minval=0, maxval=1)) # Hidden option
        with DebugStepperMovement(self, debug):
            actual,homed,measured,_ = self._homing_move_cmd(gcmd, "Test homing move")
        self.log_always("%s after %.1fmm%s" % (("Homed" if homed else "Did not home"), actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_CONFIG_help = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    def cmd_MMU_TEST_CONFIG(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))

        # Try to catch illegal parameters
        illegal_params = [
            p for p in gcmd.get_command_parameters()
            if vars(self).get(p.lower()) is None
            and vars(self.selector).get(p.lower()) is None
            and p.lower() not in [
                self.VARS_MMU_CALIB_BOWDEN_LENGTH,
                self.VARS_MMU_CALIB_CLOG_LENGTH
            ]
            and p.upper() not in ['QUIET']
        ]
        if illegal_params:
            raise gcmd.error("Unknown parameter: %s" % illegal_params)

        # Filament Speeds
        self.gear_from_buffer_speed = gcmd.get_float('GEAR_FROM_BUFFER_SPEED', self.gear_from_buffer_speed, minval=10.)
        self.gear_from_buffer_accel = gcmd.get_float('GEAR_FROM_BUFFER_ACCEL', self.gear_from_buffer_accel, minval=10.)
        self.gear_from_spool_speed = gcmd.get_float('GEAR_FROM_SPOOL_SPEED', self.gear_from_spool_speed, minval=10.)
        self.gear_from_spool_accel = gcmd.get_float('GEAR_FROM_SPOOL_ACCEL', self.gear_from_spool_accel, above=10.)
        self.gear_short_move_speed = gcmd.get_float('GEAR_SHORT_MOVE_SPEED', self.gear_short_move_speed, minval=10.)
        self.gear_short_move_accel = gcmd.get_float('GEAR_SHORT_MOVE_ACCEL', self.gear_short_move_accel, minval=10.)
        self.gear_short_move_threshold = gcmd.get_float('GEAR_SHORT_MOVE_THRESHOLD', self.gear_short_move_threshold, minval=0.)
        self.gear_homing_speed = gcmd.get_float('GEAR_HOMING_SPEED', self.gear_homing_speed, above=1.)
        self.extruder_homing_speed = gcmd.get_float('EXTRUDER_HOMING_SPEED', self.extruder_homing_speed, above=1.)
        self.extruder_load_speed = gcmd.get_float('EXTRUDER_LOAD_SPEED', self.extruder_load_speed, above=1.)
        self.extruder_unload_speed = gcmd.get_float('EXTRUDER_UNLOAD_SPEED', self.extruder_unload_speed, above=1.)
        self.extruder_sync_load_speed = gcmd.get_float('EXTRUDER_SYNC_LOAD_SPEED', self.extruder_sync_load_speed, above=1.)
        self.extruder_sync_unload_speed = gcmd.get_float('EXTRUDER_SYNC_UNLOAD_SPEED', self.extruder_sync_unload_speed, above=1.)
        self.extruder_accel = gcmd.get_float('EXTRUDER_ACCEL', self.extruder_accel, above=10.)

        # Synchronous motor control
        self.sync_form_tip = gcmd.get_int('SYNC_FORM_TIP', self.sync_form_tip, minval=0, maxval=1)
        self.sync_to_extruder = gcmd.get_int('SYNC_TO_EXTRUDER', self.sync_to_extruder, minval=0, maxval=1)
        self.sync_feedback_enable = gcmd.get_int('SYNC_FEEDBACK_ENABLE', self.sync_feedback_enable, minval=0, maxval=1)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)

        # TMC current control
        self.sync_gear_current = gcmd.get_int('SYNC_GEAR_CURRENT', self.sync_gear_current, minval=10, maxval=100)
        self.extruder_collision_homing_current = gcmd.get_int('EXTRUDER_COLLISION_HOMING_CURRENT', self.extruder_collision_homing_current, minval=10, maxval=100)
        self.extruder_form_tip_current = gcmd.get_int('EXTRUDER_FORM_TIP_CURRENT', self.extruder_form_tip_current, minval=100, maxval=150)

        # Homing, loading and unloading controls
        gate_homing_endstop = gcmd.get('GATE_HOMING_ENDSTOP', self.gate_homing_endstop)
        if gate_homing_endstop not in self.GATE_ENDSTOPS:
            raise gcmd.error("gate_homing_endstop is invalid. Options are: %s" % self.GATE_ENDSTOPS)
        if gate_homing_endstop != self.gate_homing_endstop:
            self.gate_homing_endstop = gate_homing_endstop
            self._adjust_bowden_lengths()
            self._write_variables()

        # Special bowden calibration (get current length after potential gate_homing_endstop change)
        gate_selected = max(self.gate_selected, 0) # Assume gate 0 if not known / bypass
        bowden_length = gcmd.get_float('MMU_CALIBRATION_BOWDEN_LENGTH', self.bowden_lengths[gate_selected], minval=0.)
        if bowden_length != self.bowden_lengths[gate_selected]:
            self._save_bowden_length(gate_selected, bowden_length, endstop=self.gate_homing_endstop)
            self._write_variables()

        self.gate_endstop_to_encoder = gcmd.get_float('GATE_ENDSTOP_TO_ENCODER', self.gate_endstop_to_encoder)
        self.gate_parking_distance = gcmd.get_float('GATE_PARKING_DISTANCE', self.gate_parking_distance)
        self.gate_autoload = gcmd.get_int('GATE_AUTOLOAD', self.gate_autoload, minval=0, maxval=1)
        self.bypass_autoload = gcmd.get_int('BYPASS_AUTOLOAD', self.bypass_autoload, minval=0, maxval=1)
        self.bowden_apply_correction = gcmd.get_int('BOWDEN_APPLY_CORRECTION', self.bowden_apply_correction, minval=0, maxval=1)
        self.bowden_allowable_unload_delta = self.bowden_allowable_load_delta = gcmd.get_float('BOWDEN_ALLOWABLE_LOAD_DELTA', self.bowden_allowable_load_delta, minval=1., maxval=50.)
        self.bowden_pre_unload_test = gcmd.get_int('BOWDEN_PRE_UNLOAD_TEST', self.bowden_pre_unload_test, minval=0, maxval=1)

        extruder_homing_endstop = gcmd.get('EXTRUDER_HOMING_ENDSTOP', self.extruder_homing_endstop)
        if extruder_homing_endstop not in self.EXTRUDER_ENDSTOPS:
            raise gcmd.error("extruder_homing_endstop is invalid. Options are: %s" % self.EXTRUDER_ENDSTOPS)
        self.extruder_homing_endstop = extruder_homing_endstop

        self.extruder_homing_max = gcmd.get_float('EXTRUDER_HOMING_MAX', self.extruder_homing_max, above=10.)
        self.extruder_force_homing = gcmd.get_int('EXTRUDER_FORCE_HOMING', self.extruder_force_homing, minval=0, maxval=1)

        self.toolhead_homing_max = gcmd.get_float('TOOLHEAD_HOMING_MAX', self.toolhead_homing_max, minval=0.)
        self.toolhead_entry_to_extruder = gcmd.get_float('TOOLHEAD_ENTRY_TO_EXTRUDER', self.toolhead_entry_to_extruder, minval=0.)
        self.toolhead_sensor_to_nozzle = gcmd.get_float('TOOLHEAD_SENSOR_TO_NOZZLE', self.toolhead_sensor_to_nozzle, minval=0.)
        self.toolhead_extruder_to_nozzle = gcmd.get_float('TOOLHEAD_EXTRUDER_TO_NOZZLE', self.toolhead_extruder_to_nozzle, minval=0.)
        self.toolhead_residual_filament = gcmd.get_float('TOOLHEAD_RESIDUAL_FILAMENT', self.toolhead_residual_filament, minval=0.)
        self.toolhead_ooze_reduction = gcmd.get_float('TOOLHEAD_OOZE_REDUCTION', self.toolhead_ooze_reduction, minval=-5., maxval=20.)
        self.toolhead_unload_safety_margin = gcmd.get_float('TOOLHEAD_UNLOAD_SAFETY_MARGIN', self.toolhead_unload_safety_margin, minval=0.)
        self.toolhead_post_load_tighten = gcmd.get_int('TOOLHEAD_POST_LOAD_TIGHTEN', self.toolhead_post_load_tighten, minval=0, maxval=100)
        self.gcode_load_sequence = gcmd.get_int('GCODE_LOAD_SEQUENCE', self.gcode_load_sequence, minval=0, maxval=1)
        self.gcode_unload_sequence = gcmd.get_int('GCODE_UNLOAD_SEQUENCE', self.gcode_unload_sequence, minval=0, maxval=1)

        # Software behavior options
        self.extruder_temp_variance = gcmd.get_float('EXTRUDER_TEMP_VARIANCE', self.extruder_temp_variance, minval=1.)
        self.enable_endless_spool = gcmd.get_int('ENABLE_ENDLESS_SPOOL', self.enable_endless_spool, minval=0, maxval=1)
        self.endless_spool_on_load = gcmd.get_int('ENDLESS_SPOOL_ON_LOAD', self.endless_spool_on_load, minval=0, maxval=1)
        self.endless_spool_eject_gate = gcmd.get_int('ENDLESS_SPOOL_EJECT_GATE', self.endless_spool_eject_gate, minval=-1, maxval=self.num_gates - 1)

        prev_spoolman_support = self.spoolman_support
        spoolman_support = gcmd.get('SPOOLMAN_SUPPORT', self.spoolman_support)
        if spoolman_support not in self.SPOOLMAN_OPTIONS:
            raise gcmd.error("spoolman_support is invalid. Options are: %s" % self.SPOOLMAN_OPTIONS)
        self.spoolman_support = spoolman_support

        prev_t_macro_color = self.t_macro_color
        t_macro_color = gcmd.get('T_MACRO_COLOR', self.t_macro_color)
        if t_macro_color not in self.T_MACRO_COLOR_OPTIONS:
            raise gcmd.error("t_macro_color is invalid. Options are: %s" % self.T_MACRO_COLOR_OPTIONS)
        self.t_macro_color = t_macro_color

        self.log_level = gcmd.get_int('LOG_LEVEL', self.log_level, minval=0, maxval=4)
        self.log_file_level = gcmd.get_int('LOG_FILE_LEVEL', self.log_file_level, minval=0, maxval=4)
        self.log_visual = gcmd.get_int('LOG_VISUAL', self.log_visual, minval=0, maxval=1)
        self.log_statistics = gcmd.get_int('LOG_STATISTICS', self.log_statistics, minval=0, maxval=1)

        console_gate_stat = gcmd.get('CONSOLE_GATE_STAT', self.console_gate_stat)
        if console_gate_stat not in self.GATE_STATS_TYPES:
            raise gcmd.error("console_gate_stat is invalid. Options are: %s" % self.GATE_STATS_TYPES)
        self.console_gate_stat = console_gate_stat

        self.slicer_tip_park_pos = gcmd.get_float('SLICER_TIP_PARK_POS', self.slicer_tip_park_pos, minval=0.)
        self.force_form_tip_standalone = gcmd.get_int('FORCE_FORM_TIP_STANDALONE', self.force_form_tip_standalone, minval=0, maxval=1)
        self.strict_filament_recovery = gcmd.get_int('STRICT_FILAMENT_RECOVERY', self.strict_filament_recovery, minval=0, maxval=1)
        self.filament_recovery_on_pause = gcmd.get_int('FILAMENT_RECOVERY_ON_PAUSE', self.filament_recovery_on_pause, minval=0, maxval=1)
        self.preload_attempts = gcmd.get_int('PRELOAD_ATTEMPTS', self.preload_attempts, minval=1, maxval=20)
        self.encoder_move_validation = gcmd.get_int('ENCODER_MOVE_VALIDATION', self.encoder_move_validation, minval=0, maxval=1)
        self.autotune_rotation_distance = gcmd.get_int('AUTOTUNE_ROTATION_DISTANCE', self.autotune_rotation_distance, minval=0, maxval=1)
        self.autotune_bowden_length = gcmd.get_int('AUTOTUNE_BOWDEN_LENGTH', self.autotune_bowden_length, minval=0, maxval=1)
        self.retry_tool_change_on_error = gcmd.get_int('RETRY_TOOL_CHANGE_ON_ERROR', self.retry_tool_change_on_error, minval=0, maxval=1)
        self.print_start_detection = gcmd.get_int('PRINT_START_DETECTION', self.print_start_detection, minval=0, maxval=1)
        self.show_error_dialog = gcmd.get_int('SHOW_ERROR_DIALOG', self.show_error_dialog, minval=0, maxval=1)
        form_tip_macro = gcmd.get('FORM_TIP_MACRO', self.form_tip_macro)
        if form_tip_macro != self.form_tip_macro:
            self.form_tip_vars = None # If macro is changed invalidate defaults
        self.form_tip_macro = form_tip_macro

        # Available only with encoder
        if self.has_encoder():
            self.enable_clog_detection = gcmd.get_int('ENABLE_CLOG_DETECTION', self.enable_clog_detection, minval=0, maxval=2)
            self.encoder_sensor.set_mode(self.enable_clog_detection)
            clog_length = gcmd.get_float('MMU_CALIBRATION_CLOG_LENGTH', self.encoder_sensor.get_clog_detection_length(), minval=1., maxval=100.)
            if clog_length != self.encoder_sensor.get_clog_detection_length():
                self.encoder_sensor.set_clog_detection_length(clog_length)

        # Currently hidden and testing options
        self.test_random_failures = gcmd.get_int('TEST_RANDOM_FAILURES', self.test_random_failures, minval=0, maxval=1)
        self.test_disable_encoder = gcmd.get_int('TEST_DISABLE_ENCODER', self.test_disable_encoder, minval=0, maxval=1)
        self.test_force_in_print = gcmd.get_int('TEST_FORCE_IN_PRINT', self.test_force_in_print, minval=0, maxval=1)
        self.canbus_comms_retries = gcmd.get_int('CANBUS_COMMS_RETRIES', self.canbus_comms_retries, minval=1, maxval=10)
        self.serious = gcmd.get_int('SERIOUS', self.serious, minval=0, maxval=1)

        # Sub components
        self.selector.set_test_config(gcmd)

        if not quiet:
            msg = "FILAMENT MOVEMENT SPEEDS:"
            msg += "\ngear_from_buffer_speed = %.1f" % self.gear_from_buffer_speed
            msg += "\ngear_from_buffer_accel = %.1f" % self.gear_from_buffer_accel
            msg += "\ngear_from_spool_speed = %.1f" % self.gear_from_spool_speed
            msg += "\ngear_from_spool_accel = %.1f" % self.gear_from_spool_accel
            msg += "\ngear_short_move_speed = %.1f" % self.gear_short_move_speed
            msg += "\ngear_short_move_accel = %.1f" % self.gear_short_move_accel
            msg += "\ngear_short_move_threshold = %.1f" % self.gear_short_move_threshold
            msg += "\ngear_homing_speed = %.1f" % self.gear_homing_speed
            msg += "\nextruder_homing_speed = %.1f" % self.extruder_homing_speed
            msg += "\nextruder_load_speed = %.1f" % self.extruder_load_speed
            msg += "\nextruder_unload_speed = %.1f" % self.extruder_unload_speed
            msg += "\nextruder_sync_load_speed = %.1f" % self.extruder_sync_load_speed
            msg += "\nextruder_sync_unload_speed = %.1f" % self.extruder_sync_unload_speed
            msg += "\nextruder_accel = %.1f" % self.extruder_accel

            msg += "\n\nTMC & MOTOR SYNC CONTROL:"
            msg += "\nsync_to_extruder = %d" % self.sync_to_extruder
            msg += "\nsync_form_tip = %d" % self.sync_form_tip
            msg += "\nsync_feedback_enable = %d" % self.sync_feedback_enable
            msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
            msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
            msg += "\nsync_gear_current = %d" % self.sync_gear_current
            msg += "\nextruder_collision_homing_current = %d" % self.extruder_collision_homing_current
            msg += "\nextruder_form_tip_current = %d" % self.extruder_form_tip_current

            msg += "\n\nLOADING/UNLOADING:"
            msg += "\ngate_homing_endstop = %s" % self.gate_homing_endstop
            if self.gate_homing_endstop in [self.ENDSTOP_GATE, self.ENDSTOP_POST_GATE_PREFIX] and self.has_encoder():
                msg += "\ngate_endstop_to_encoder = %s" % self.gate_endstop_to_encoder
            msg += "\ngate_parking_distance = %s" % self.gate_parking_distance
            msg += "\ngate_autoload = %s" % self.gate_autoload
            if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                msg += "\nbypass_autoload = %s" % self.bypass_autoload
            if self.has_encoder():
                msg += "\nbowden_apply_correction = %d" % self.bowden_apply_correction
                msg += "\nbowden_allowable_load_delta = %d" % self.bowden_allowable_load_delta
                msg += "\nbowden_pre_unload_test = %d" % self.bowden_pre_unload_test
            msg += "\nextruder_force_homing = %d" % self.extruder_force_homing
            msg += "\nextruder_homing_endstop = %s" % self.extruder_homing_endstop
            msg += "\nextruder_homing_max = %.1f" % self.extruder_homing_max
            msg += "\ntoolhead_extruder_to_nozzle = %.1f" % self.toolhead_extruder_to_nozzle
            if self.sensor_manager.has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\ntoolhead_sensor_to_nozzle = %.1f" % self.toolhead_sensor_to_nozzle
                msg += "\ntoolhead_homing_max = %.1f" % self.toolhead_homing_max
            if self.sensor_manager.has_sensor(self.ENDSTOP_EXTRUDER_ENTRY):
                msg += "\ntoolhead_entry_to_extruder = %.1f" % self.toolhead_entry_to_extruder
            msg += "\ntoolhead_residual_filament = %.1f" % self.toolhead_residual_filament
            msg += "\ntoolhead_ooze_reduction = %.1f" % self.toolhead_ooze_reduction
            msg += "\ntoolhead_unload_safety_margin = %d" % self.toolhead_unload_safety_margin
            msg += "\ntoolhead_post_load_tighten = %d" % self.toolhead_post_load_tighten
            msg += "\ngcode_load_sequence = %d" % self.gcode_load_sequence
            msg += "\ngcode_unload_sequence = %d" % self.gcode_unload_sequence

            msg += "\n\nTIP FORMING:"
            msg += "\nform_tip_macro = %s" % self.form_tip_macro
            msg += "\nslicer_tip_park_pos = %.1f" % self.slicer_tip_park_pos
            msg += "\nforce_form_tip_standalone = %d" % self.force_form_tip_standalone

            msg += "\n\nLOGGING:"
            msg += "\nlog_level = %d" % self.log_level
            msg += "\nlog_visual = %d" % self.log_visual
            if self.mmu_logger:
                msg += "\nlog_file_level = %d" % self.log_file_level
            msg += "\nlog_statistics = %d" % self.log_statistics
            msg += "\nconsole_gate_stat = %s" % self.console_gate_stat

            msg += "\n\nOTHER:"
            msg += "\nextruder_temp_variance = %.1f" % self.extruder_temp_variance
            if self.has_encoder():
                msg += "\nenable_clog_detection = %d" % self.enable_clog_detection
            msg += "\nenable_endless_spool = %d" % self.enable_endless_spool
            msg += "\nendless_spool_on_load = %d" % self.endless_spool_on_load
            msg += "\nendless_spool_eject_gate = %d" % self.endless_spool_eject_gate
            msg += "\nspoolman_support = %s" % self.spoolman_support
            msg += "\nt_macro_color = %s" % self.t_macro_color
            msg += "\npreload_attempts = %d" % self.preload_attempts
            if self.has_encoder():
                msg += "\nstrict_filament_recovery = %d" % self.strict_filament_recovery
                msg += "\nencoder_move_validation = %d" % self.encoder_move_validation
                msg += "\nautotune_rotation_distance = %d" % self.autotune_rotation_distance
            msg += "\nautotune_bowden_length = %d" % self.autotune_bowden_length
            msg += "\nfilament_recovery_on_pause = %d" % self.filament_recovery_on_pause
            msg += "\nretry_tool_change_on_error = %d" % self.retry_tool_change_on_error
            msg += "\nprint_start_detection = %d" % self.print_start_detection
            msg += "\nshow_error_dialog = %d" % self.show_error_dialog

            # These are in mmu_vars.cfg and are offered here for convenience
            msg += "\n\nCALIBRATION (mmu_vars.cfg):"
            if self.mmu_machine.variable_bowden_lengths:
                msg += "\nmmu_calibration_bowden_lengths = %s" % self.bowden_lengths
            else:
                msg += "\nmmu_calibration_bowden_length = %.1f" % self.bowden_lengths[0]
            if self.has_encoder():
                msg += "\nmmu_calibration_clog_length = %.1f" % self.encoder_sensor.get_clog_detection_length()

            # Sub components
            msg += self.selector.get_test_config()

            self.log_info(msg)

        # Some changes need additional action to be taken
        if prev_spoolman_support != self.spoolman_support:
            self._spoolman_sync()
        if prev_t_macro_color != self.t_macro_color:
            self._update_t_macros()


###########################################
# RUNOUT, ENDLESS SPOOL and GATE HANDLING #
###########################################

    def _runout(self, force_runout=False):
        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during handling
            self.is_handling_runout = force_runout # Best starting assumption
            self._save_toolhead_position_and_park('runout')

            if self.tool_selected < 0:
                raise MmuError("Filament runout or clog on an unknown or bypass tool\nManual intervention is required")

            if self.filament_pos != self.FILAMENT_POS_LOADED and not force_runout:
                raise MmuError("Filament runout or clog occured but filament is marked as not loaded(?)\nManual intervention is required")

            self.log_debug("Issue on tool T%d" % self.tool_selected)

            # Check for clog by looking for filament at the gate (or in the encoder)
            if not force_runout:
                if not self.check_filament_runout():
                    if self.has_encoder():
                        self.encoder_sensor.update_clog_detection_length()
                    self.is_handling_runout = False
                    raise MmuError("A clog has been detected and requires manual intervention")

            # We definitely have a filament runout
            self.is_handling_runout = True # Will remain true until complete and continue or resume after error
            if self.enable_endless_spool:
                self._set_gate_status(self.gate_selected, self.GATE_EMPTY) # Indicate current gate is empty
                next_gate, msg = self._get_next_endless_spool_gate(self.tool_selected, self.gate_selected)
                if next_gate == -1:
                    raise MmuError("Runout detected\nNo alternative gates available after checking %s" % msg)

                self.log_error("A runout has been detected. Checking for alternative gates %s" % msg)
                self.log_info("Remapping T%d to Gate %d" % (self.tool_selected, next_gate))

                if self.endless_spool_eject_gate > 0:
                    self.log_info("Ejecting filament remains to designated waste gate %d" % self.endless_spool_eject_gate)
                    self.select_gate(self.endless_spool_eject_gate)
                self._unload_tool(runout=True)
                self.select_gate(next_gate) # Necessary if unloaded to waste gate
                self._remap_tool(self.tool_selected, next_gate)
                self._select_and_load_tool(self.tool_selected)
            else:
                raise MmuError("Runout detected\nEndlessSpool mode is off - manual intervention is required")

        self._continue_after("endless_spool")
        self.pause_resume.send_resume_command() # Undo what runout sensor handling did

    def _get_next_endless_spool_gate(self, tool, gate):
        group = self.endless_spool_groups[gate]
        next_gate = -1
        checked_gates = []
        for i in range(self.num_gates - 1):
            check = (gate + i + 1) % self.num_gates
            if self.endless_spool_groups[check] == group:
                checked_gates.append(check)
                if self.gate_status[check] != self.GATE_EMPTY:
                    next_gate = check
                    break
        msg = "for T%d in EndlessSpool Group %s %s" % (tool, chr(ord('A') + group), checked_gates)
        return next_gate, msg

    def _set_gate_status(self, gate, state):
        if gate >= 0:
            if state != self.gate_status[gate]:
                self.gate_status[gate] = state
                self.save_variable(self.VARS_MMU_GATE_STATUS, self.gate_status, write=True)
                self.mmu_macro_event(self.MACRO_EVENT_GATE_MAP_CHANGED, "GATE=%d" % gate)

    # Use pre-gate (and post-gate) sensors to "correct" gate status
    # Return updated gate_status
    def _validate_gate_status(self, gate_status):
        for gate, status in enumerate(gate_status):
            detected = self.sensor_manager.check_gate_sensor(self.ENDSTOP_POST_GATE_PREFIX, gate)
            if detected is True:
                gate_status[gate] = self.GATE_AVAILABLE
            else:
                detected = self.sensor_manager.check_gate_sensor(self.PRE_GATE_SENSOR_PREFIX, gate)
                if detected is True and status == self.GATE_EMPTY:
                    gate_status[gate] = self.GATE_UNKNOWN
                elif detected is False and status != self.GATE_EMPTY:
                    gate_status[gate] = self.GATE_EMPTY
        return gate_status

    def _get_gate_endstop_name(self):
        return "%s_%d" % (self.gate_homing_endstop, self.gate_selected) if self.gate_homing_endstop == self.ENDSTOP_POST_GATE_PREFIX else self.ENDSTOP_GATE

    def _get_filament_char(self, gate, no_space=False, show_source=False):
        gate_status = self.gate_status[gate]
        if self.enable_endless_spool and gate == self.endless_spool_eject_gate:
            return "W"
        elif gate_status == self.GATE_AVAILABLE_FROM_BUFFER:
            return "B" if show_source else "*"
        elif gate_status == self.GATE_AVAILABLE:
            return "S" if show_source else "*"
        elif gate_status == self.GATE_EMPTY:
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

            if show_groups and self.enable_endless_spool:
                group = self.endless_spool_groups[gate]
                msg += " Group %s:" % chr(ord('A') + group)
                gates_in_group = [(j + gate) % num_tools for j in range(num_tools)]
                #msg += " >".join("{:>2}({})".format(g, self._get_filament_char(g, show_source=False)) for g in gates_in_group if self.endless_spool_groups[g] == group)
                msg += " >".join("{:>2}".format(g) for g in gates_in_group if self.endless_spool_groups[g] == group)

            if i == self.tool_selected:
                msg += " [SELECTED]"
        return msg

    def _mmu_visual_to_string(self):
        multi_tool = False
        num_gates = self.num_gates
        gate_indices = range(num_gates)
        msg_gates = "Gates: " + "".join("|{:^3}".format(g) if g < 10 else "| {:2}".format(g) for g in gate_indices) + "|"
        msg_avail = "Avail: " + "".join("| %s " % self._get_filament_char(g, no_space=True, show_source=True) for g in gate_indices) + "|"
        tool_strings = []
        for g in gate_indices:
            tool_str = "+".join("T%d" % t for t in gate_indices if self.ttg_map[t] == g)
            multi_tool |= len(tool_str) > 2
            tool_strings.append(("|%s " % (tool_str if tool_str else " {} ".format(UI_SEPARATOR)))[:4])
        msg_tools = "Tools: " + "".join(tool_strings) + "|"
        #msg_tools += " Some gates support multiple tools!" if multi_tool else ""
        select_strings = ["|---" if self.gate_selected != self.TOOL_GATE_UNKNOWN and self.gate_selected == (g - 1) else "----" for g in gate_indices]
        for i, g in enumerate(gate_indices):
            if self.gate_selected == g:
                select_strings[i] = "| %s " % self._get_filament_char(g, no_space=True)
        msg_selct = "Selct: " + "".join(select_strings) + ("|" if self.gate_selected == num_gates - 1 else "-")
        msg = "\n".join([msg_gates, msg_tools, msg_avail, msg_selct])
        if self.selector.is_homed:
            msg += " " + self._selected_tool_string()
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
            self.GATE_AVAILABLE_FROM_BUFFER: "Buffer",
            self.GATE_AVAILABLE: "Spool",
            self.GATE_EMPTY: "Empty",
            self.GATE_UNKNOWN: "Unknown"
        }

        for g in range(self.num_gates):
            available = available_status[self.gate_status[g]]
            name = self.gate_filament_name[g] or "n/a"
            material = self.gate_material[g] or "n/a"
            color = self.gate_color[g] or "n/a"
            temperature = self.gate_temperature[g] or "n/a"

            gate_detail = ""
            if detail:
                filament_char = self._get_filament_char(g, show_source=False)
                tools_supported = ", ".join("T{}".format(t) for t in range(self.num_gates) if self.ttg_map[t] == g)
                tools_str = " supporting {}; ".format(tools_supported) if tools_supported else " "
                selected = "[SELECTED]" if g == self.gate_selected else ""
                gate_detail = "\nGate {}({}){}{}".format(g, filament_char, selected, tools_str)
            else:
                gate_detail = "\nGate {}: ".format(g)

            msg += "{}Status: {}".format(gate_detail, available)
            speed_option = ", Load Speed: {}%".format(self.gate_speed_override[g]) if self.gate_speed_override[g] != 100 else ""
            spool_option = str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a"
            if self.spoolman_support == self.SPOOLMAN_OFF:
                msg += ", Material: {}, Color: {}, Name: {}, Temp: {}{}".format(material, color, name, temperature, speed_option)
            elif self.gate_spool_id[g] <= 0 and self.spoolman_support == self.SPOOLMAN_PUSH:
                msg += ", SpoolId: {}, Material: {}, Color: {}, Name: {}, Temp: {}{}".format(spool_option, material, color, name, temperature, speed_option)
            else:
                if self.gate_spool_id[g] > 0:
                    msg += ", SpoolId: {} --> Material: {}, Color: {}, Name: {}, Temp: {}{}".format(spool_option, material, color, name, temperature, speed_option)
                else:
                    msg += ", SpoolId: n/a{}".format(speed_option)
        return msg

    def _remap_tool(self, tool, gate, available=None):
        self.ttg_map[tool] = gate
        self._persist_ttg_map()
        self._ensure_ttg_match()
        self._update_slicer_color() # Indexed by gate
        if available is not None:
            self._set_gate_status(gate, available)

    # Find a tool that maps to gate (for recovery)
    def _ensure_ttg_match(self):
        if self.gate_selected in [self.TOOL_GATE_UNKNOWN, self.TOOL_GATE_BYPASS]:
            self._set_tool_selected(self.gate_selected)
        elif not self.is_in_print():
            possible_tools = [tool for tool in range(self.num_gates) if self.ttg_map[tool] == self.gate_selected]
            if possible_tools:
                if self.tool_selected not in possible_tools:
                    self.log_debug("Resetting tool selected to match current gate")
                    self._set_tool_selected(possible_tools[0])
            else:
                self.log_info("Resetting tool selected to unknown because current gate isn't associated with tool")
                self._set_tool_selected(self.TOOL_GATE_UNKNOWN)

    def _persist_ttg_map(self):
        self.save_variable(self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map, write=True)

    def _reset_ttg_map(self):
        self.log_debug("Resetting TTG map")
        self.ttg_map = list(self.default_ttg_map)
        self._persist_ttg_map()
        self._ensure_ttg_match()
        self._update_slicer_color() # Indexed by gate

    def _persist_gate_map(self, sync=False, gate_ids=None):
        self.save_variable(self.VARS_MMU_GATE_STATUS, self.gate_status)
        self.save_variable(self.VARS_MMU_GATE_FILAMENT_NAME, self.gate_filament_name)
        self.save_variable(self.VARS_MMU_GATE_MATERIAL, self.gate_material)
        self.save_variable(self.VARS_MMU_GATE_COLOR, self.gate_color)
        self.save_variable(self.VARS_MMU_GATE_TEMPERATURE, self.gate_temperature)
        self.save_variable(self.VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id)
        self.save_variable(self.VARS_MMU_GATE_SPEED_OVERRIDE, self.gate_speed_override)
        self._write_variables()
        self._update_t_macros()

        # Also persist to spoolman db if pushing updates for visability
        if sync and self.spoolman_support == self.SPOOLMAN_PUSH:
            if gate_ids is None:
                gate_ids = list(enumerate(self.gate_spool_id))
            if gate_ids:
                self._spoolman_push_gate_map(gate_ids)

        if self.printer.lookup_object("gcode_macro %s" % self.mmu_event_macro, None) is not None:
            self.mmu_macro_event(self.MACRO_EVENT_GATE_MAP_CHANGED, "GATE=-1")

    def _reset_gate_map(self):
        self.log_debug("Resetting gate map")
        self.gate_status = self._validate_gate_status(list(self.default_gate_status))
        self.gate_filament_name = list(self.default_gate_filament_name)
        self.gate_material = list(self.default_gate_material)
        self._update_gate_color(list(self.default_gate_color))
        self.gate_temperature = list(self.default_gate_temperature)
        self.gate_spool_id = list(self.default_gate_spool_id)
        self.gate_speed_override = list(self.default_gate_speed_override)
        self._persist_gate_map(sync=True)

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
        if strategy == self.AUTOMAP_FILAMENT_NAME:
            search_in = self.gate_filament_name
            tool_field = 'name'
        elif strategy == self.AUTOMAP_SPOOL_ID:
            search_in = self.gate_spool_id
            tool_field = 'spool_id' # Placeholders for future support
        elif strategy == self.AUTOMAP_MATERIAL:
            search_in = self.gate_material
            tool_field = 'material'
        elif strategy in [self.AUTOMAP_CLOSEST_COLOR, self.AUTOMAP_COLOR]:
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
            if strategy != self.AUTOMAP_CLOSEST_COLOR:
                for gn, gate_feature in enumerate(search_in):
                    # When matching by name normalize possible unicode characters and match case-insensitive
                    if strategy == self.AUTOMAP_FILAMENT_NAME:
                        equal = self._compare_unicode(tool_to_remap[tool_field], gate_feature)
                    else:
                        equal = tool_to_remap[tool_field] == gate_feature
                    if equal:
                        remaps.append("T%s --> G%s (%s)" % (tool, gn, gate_feature))
                        self._wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))
                if not remaps:
                    errors.append("No gates found for tool %s with %s %s" % (tool, strategy_str, tool_to_remap[tool_field]))

            # 'colors' search for closest
            elif strategy == self.AUTOMAP_CLOSEST_COLOR:
                if tool_to_remap['material'] == "unknown":
                    errors.append("When automapping with closest color, the tool material must be set.")
                if tool_to_remap['material'] not in self.gate_material:
                    errors.append("No gate has a filament matching the desired material (%s). Available are : %s" % (tool_to_remap['material'], self.gate_material))
                if not errors:
                    color_list = []
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material']:
                            color_list.append(color)
                    if not color_list:
                        errors.append("Gates with %s are mssing color information..." % tool_to_remap['material'])

                if not errors:
                    closest, distance = self._find_closest_color(tool_to_remap['color'], color_list)
                    for gn, color in enumerate(search_in):
                        gm = "".join(self.gate_material[gn].strip()).replace('#', '').lower()
                        if gm == tool_to_remap['material']:
                            if closest == color:
                                t = self.console_gate_stat
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
                                self._wrap_gcode_command("MMU_TTG_MAP TOOL=%d GATE=%d QUIET=1" % (tool, gn))

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
                if spool_id >= 0 and not self.spoolman_support == self.SPOOLMAN_OFF and self.gate_status[gate] != self.GATE_EMPTY and self.t_macro_color == self.T_MACRO_COLOR_GATEMAP:
                    t_vars['spool_id'] = self.gate_spool_id[gate]
                else:
                    t_vars.pop('spool_id', None)

                if self.t_macro_color == self.T_MACRO_COLOR_SLICER:
                    st = self.slicer_tool_map['tools'].get(str(tool), None)
                    rgb_hex = self._color_to_rgb_hex(st.get('color', None)) if st else None
                    if rgb_hex:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)
                elif self.t_macro_color in [self.T_MACRO_COLOR_GATEMAP, self.T_MACRO_COLOR_ALLGATES]:
                    rgb_hex = self._color_to_rgb_hex(self.gate_color[gate])
                    if self.gate_status[gate] != self.GATE_EMPTY or self.t_macro_color == self.T_MACRO_COLOR_ALLGATES:
                        t_vars['color'] = rgb_hex
                    else:
                        t_vars.pop('color', None)
                else:
                    t_vars.pop('color', None)
                t_macro.variables = t_vars

### GCODE COMMANDS FOR RUNOUT, TTG MAP, GATE MAP and GATE LOGIC ##################

    cmd_MMU_TEST_RUNOUT_help = "Manually invoke the clog/runout detection logic for testing"
    def cmd_MMU_TEST_RUNOUT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        try:
            self._runout(True)
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
            self._runout()
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_ENCODER_INSERT_help = "Internal encoder filament insert detection handler"
    def cmd_MMU_ENCODER_INSERT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        # TODO Possible future bypass preload feature - make gate act like bypass

    # Callback to handle gate filament sensors on MMU.
    # SENSOR will contain sensor name,
    # GATE will be set if specific pre-gate or post-gate sensor.
    cmd_MMU_GATE_RUNOUT_REMOVE_help = "Internal MMU filament remove/runout handler"
    def cmd_MMU_GATE_RUNOUT_REMOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        do_runout = gcmd.get_int('DO_RUNOUT', 0) # Treat as runout(send_pause_command was issued) or "remove"
        if not self.is_enabled:
            if do_runout:
                self.pause_resume.send_resume_command() # Undo what runout sensor handling did
            return
        self._fix_started_state()
        gate = gcmd.get_int('GATE', None)
        sensor = gcmd.get('SENSOR', "")
        try:
            # Update gate map from pre-gate sensor
            if sensor.startswith(self.PRE_GATE_SENSOR_PREFIX) and gate is not None:
                # Ignore pre-gate runout if endless_spool_eject_gate feature is active and we want filament to be consumed to clear gate
                if not(self.enable_endless_spool and self.endless_spool_eject_gate > 0):
                    self._set_gate_status(gate, self.GATE_EMPTY)
                else:
                    self.log_trace("Ignoring runout detected by %s because endless_spool_eject_gate is active" % sensor)

            if do_runout:
                if self.is_in_print() and (gate is None or gate == self.gate_selected):
                    self.log_debug("Handling runout detected by MMU %s sensor" % sensor)
                    self._runout(True) # Will send_resume_command() or fail and pause
                else:
                    self.log_debug("Assertion failure: runout detected by %s but not in print or occured on unexpected gate. Ignored" % sensor)
                    self.pause_resume.send_resume_command() # Undo what runout sensor handling did
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # This callback is not protected by klipper "is printing" check so be careful
    cmd_MMU_GATE_INSERT_help = "Internal MMU filament detection handler"
    def cmd_MMU_GATE_INSERT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()
        sensor = gcmd.get('SENSOR', "")
        gate = gcmd.get_int('GATE', None)
        try:
            if sensor.startswith(self.PRE_GATE_SENSOR_PREFIX) and gate is not None:
                self.log_debug("Handling insertion detected by MMU %s sensor" % sensor)
                self._set_gate_status(gate, self.GATE_UNKNOWN)
                self._check_pending_spool_id(gate) # Have spool_id ready?
                if not self.is_in_print() and self.gate_autoload:
                    self.gcode.run_script_from_command("MMU_PRELOAD GATE=%d" % gate)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    # Callback to handle filament sensor at extruder entrance
    # This is not protected by klipper "is printing" check
    cmd_MMU_EXTRUDER_RUNOUT_REMOVE_help = "Internal extruder filament remove/runout handler"
    def cmd_MMU_EXTRUDER_RUNOUT_REMOVE(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        do_runout = gcmd.get_int('DO_RUNOUT', 0)
        if not self.is_enabled:
            if do_runout:
                self.pause_resume.send_resume_command() # Undo what runout sensor handling did
            return

        if do_runout:
            if self.is_in_print():
                self.handle_mmu_error("Filament runout occured at extruder. Manual intervention is required")
            else:
                self.log_debug("Assertion failure: runout detected by extruder sensor but not in print")
                self.pause_resume.send_resume_command() # Undo what runout sensor handling did

    # Callback to handle filament sensor at extruder entrance
    # This is not protected by klipper "is not printing" check
    cmd_MMU_EXTRUDER_INSERT_help = "Internal extruder filament insertion handler"
    def cmd_MMU_EXTRUDER_INSERT(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()
        try:
            if self.gate_selected != self.TOOL_GATE_BYPASS:
                msg = "bypass not selected"
            elif self.is_printing():
                msg = "actively printing"
            elif self.filament_pos != self.FILAMENT_POS_UNLOADED :
                msg = "extruder cannot be verified as unloaded"
            elif not self.bypass_autoload:
                msg = "bypass autoload is disabled"
            else:
                self.log_debug("Autoloading extruder")
                with self._wrap_suspend_runout():
                    self._note_toolchange("> Bypass")
                    self.load_sequence(bowden_move=0., extruder_only=True)
                return
            self.log_debug("Ignoring extruder insertion because %s" % msg)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_M400_help = "Wait on both move queues"
    def cmd_MMU_M400(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        self.movequeues_wait(toolhead=True, mmu_toolhead=True)

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
        available = gcmd.get_int('AVAILABLE', self.GATE_UNKNOWN, minval=self.GATE_EMPTY, maxval=self.GATE_AVAILABLE)

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
            if not available == self.GATE_UNKNOWN or (available == self.GATE_UNKNOWN and status == self.GATE_EMPTY):
                status = available
            if tool == -1:
                self._set_gate_status(gate, status)
            else:
                self._remap_tool(tool, gate, status)
        else:
            quiet = False # Display current TTG map
        if not quiet:
            msg = self._ttg_map_to_string(show_groups=detail)
            if not detail and self.enable_endless_spool:
                msg += "\nDETAIL=1 to see EndlessSpool map"
            self.log_info(msg)

    cmd_MMU_GATE_MAP_help = "Display or define the type and color of filaments on each gate"
    def cmd_MMU_GATE_MAP(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}")                                # Hidden option for bulk filament update from moonraker component
        replace = bool(gcmd.get_int('REPLACE', 0, minval=0, maxval=1)) # Hidden option for bulk filament
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        next_spool_id = gcmd.get_int('NEXT_SPOOLID', None, minval=-1)

        try:
            gate_map = ast.literal_eval(gmapstr)
        except Exception as e:
            self.log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))

        if reset:
            self._reset_gate_map()

        if next_spool_id:
            self.pending_spool_id = next_spool_id
            self.reactor.update_timer(self.pending_spool_id_timer, self.reactor.monotonic() + self.pending_spool_id_timeout)

        if gate_map:
            self.log_debug("Received gate map update (replace: %s) from Spoolman" % replace)
            if replace:
                # Replace map
                for gate, fil in gate_map.items():
                    self.gate_spool_id[gate] = fil['spool_id']
                    if fil['spool_id'] >= 0:
                        self.gate_filament_name[gate] = fil.get('name', '')
                        self.gate_material[gate] = fil.get('material', '')
                        self.gate_color[gate] = fil.get('color', '')
                        self.gate_temperature[gate] = fil.get('temp', '') or int(self.default_extruder_temp)
                    else:
                        # Clear attributes (should only get here in spoolman "pull" mode)
                        self.gate_filament_name[gate] = ''
                        self.gate_material[gate] = ''
                        self.gate_color[gate] = ''
                        self.gate_temperature[gate] = int(self.default_extruder_temp)
                        self.gate_speed_override[gate] = 100
            else:
                # Update map
                for gate, fil in gate_map.items():
                    if fil and self.gate_spool_id[gate] == fil.get('spool_id', None):
                        self.gate_filament_name[gate] = fil.get('name', '')
                        self.gate_material[gate] = fil.get('material', '')
                        self.gate_color[gate] = fil.get('color', '')
                        self.gate_temperature[gate] = fil.get('temp', '') or int(self.default_extruder_temp)
                    else:
                        self.log_debug("Assertion failure: Spool_id changed for Gate %d in MMU_GATE_MAP. Attributes=%s" % (gate, fil))

            self._update_gate_color(self.gate_color)
            self._persist_gate_map() # This will also update LED status

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

            gate_ids = []
            for gate in gatelist:
                available = gcmd.get_int('AVAILABLE', self.gate_status[gate], minval=-1, maxval=2)
                name = gcmd.get('NAME', None)
                material = gcmd.get('MATERIAL', None)
                color = gcmd.get('COLOR', None)
                spool_id = gcmd.get_int('SPOOLID', None, minval=-1)
                temperature = gcmd.get_int('TEMP', int(self.default_extruder_temp))
                speed_override = gcmd.get_int('SPEED', self.gate_speed_override[gate], minval=10, maxval=150)

                if self.spoolman_support != self.SPOOLMAN_PULL:
                    # Local gate map, can update attributes
                    name = name or self.gate_filament_name[gate]
                    material = (material or self.gate_material[gate]).upper()
                    color = (color or self.gate_color[gate]).lower()
                    temperature = temperature or self.gate_temperature
                    spool_id = spool_id or self.gate_spool_id[gate]

                    if spool_id != self.gate_spool_id[gate]:
                        if spool_id in self.gate_spool_id:
                            old_gate = self.gate_spool_id.index(spool_id)
                            if old_gate != gate:
                                self.gate_spool_id[old_gate] = -1
                                gate_ids.append((old_gate, -1))
                        gate_ids.append((gate, spool_id))
                    color = self._validate_color(color)
                    if color is None:
                        raise gcmd.error("Color specification must be in form 'rrggbb' hexadecimal value (no '#') or valid color name or empty string")
                    self.gate_status[gate] = available
                    self.gate_filament_name[gate] = name
                    self.gate_material[gate] = material
                    self.gate_color[gate] = color
                    self.gate_temperature[gate] = temperature
                    self.gate_spool_id[gate] = spool_id
                    self.gate_speed_override[gate] = speed_override

                else:
                    # Remote (spoolman) gate map, don't update attributes that are available from spoolman
                    self.gate_status[gate] = available
                    self.gate_speed_override[gate] = speed_override
                    if any(x is not None for x in [material, color, spool_id, name]):
                        self.log_error("Spoolman mode is '%s': Can only set gate status and speed override locally\nUse MMU_SPOOLMAN or update spoolman directly" % self.SPOOLMAN_PULL)
                        return

            self._update_gate_color(self.gate_color)
            self._persist_gate_map(sync=bool(gate_ids), gate_ids=gate_ids) # This will also update LED status

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
            self.enable_endless_spool = enabled
            self.save_variable(self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool, write=True)
            if enabled and not quiet:
                self.log_always("EndlessSpool is enabled")
        if not self.enable_endless_spool:
            self.log_always("EndlessSpool is disabled")
            return

        if reset:
            self.log_debug("Resetting EndlessSpool groups")
            self.enable_endless_spool = self.default_enable_endless_spool
            self.endless_spool_groups = self.default_endless_spool_groups

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
            self.save_variable(self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups, write=True)

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
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.num_gates - 1)
        material = gcmd.get('MATERIAL', "unknown")
        color = gcmd.get('COLOR', "").lower()
        name = gcmd.get('NAME', "") # Filament name
        temp = gcmd.get_int('TEMP', 0, minval=0)
        used = bool(gcmd.get_int('USED', 1, minval=0, maxval=1)) # Is used in print (i.e a referenced tool or not)
        purge_volumes = gcmd.get('PURGE_VOLUMES', "")
        num_slicer_tools = gcmd.get_int('NUM_SLICER_TOOLS', self.num_gates, minval=1, maxval=self.num_gates) # Allow slicer to have less tools than MMU gates
        automap_strategy = gcmd.get('AUTOMAP', None)

        quiet = False
        if reset:
            self._clear_slicer_tool_map()
            quiet = True

        if tool >= 0:
            self.slicer_tool_map['tools'][str(tool)] = {'color': color, 'material': material, 'temp': temp, 'name': name, 'in_use': used}
            if used:
                self.slicer_tool_map['referenced_tools'] = sorted(set(self.slicer_tool_map['referenced_tools'] + [tool]))
                if automap_strategy and automap_strategy != self.AUTOMAP_NONE:
                    self._automap_gate(tool, automap_strategy)
            if color:
                self._update_slicer_color()
            quiet = True

        if initial_tool is not None:
            self.slicer_tool_map['initial_tool'] = initial_tool
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
                elif n == num_slicer_tools * 2:
                    calc = lambda x,y: volumes[y] + volumes[num_slicer_tools + x] # Build matrix with sum of "from" list then "to" list
                elif n == num_slicer_tools ** 2:
                    calc = lambda x,y: volumes[y + x * num_slicer_tools] # Full NxN matrix supplied in rows of "from" for each "to"
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
                        msg += "T%d (Gate %d, %s, %s, %d%sC)" % (int(t), self.ttg_map[int(t)], params['material'], params['color'], params['temp'], UI_DEGREE)
                        msg += " Not used\n" if detail and not params['in_use'] else "\n"
                if self.slicer_tool_map['initial_tool'] is not None:
                    msg += "Initial Tool: T%d" % self.slicer_tool_map['initial_tool']
                    msg += " (will use bypass)\n" if colors <= 1 and self.tool_selected == self.TOOL_GATE_BYPASS else "\n"
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

        self.slicer_tool_map['purge_volumes'] = self._generate_purge_matrix(tool_rgb_colors, min_purge, max_purge, multiplier)
        self.log_always("Purge map updated. Use 'MMU_SLICER_TOOL_MAP PURGE_MAP=1' to view")

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
            with self._wrap_suspend_runout(): # Don't want runout accidently triggering during gate check
                with self._wrap_suspend_write_variables(): # Reduce I/O activity to a minimum
                    with self.wrap_action(self.ACTION_CHECKING):
                        with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
                            tool_selected = self.tool_selected
                            filament_pos = self.filament_pos
                            self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
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
                                        self._auto_filament_grip()
                                        return
                                except ValueError:
                                    raise MmuError("Invalid TOOLS parameter: %s" % tools)
                            elif self.gate_selected >= 0:
                                # No parameters means current gate
                                gates_tools.append([self.gate_selected, -1])
                            else:
                                raise MmuError("Current gate is invalid")

                            # Force initial eject
                            if filament_pos != self.FILAMENT_POS_UNLOADED:
                                self.log_info("Unloading current tool prior to checking gates")
                                self._unload_tool() # Can throw MmuError

                            if len(gates_tools) > 1:
                                self.log_info("Will check gates: %s" % ', '.join(str(g) for g,t in gates_tools))
                            with self.wrap_suppress_visual_log():
                                for gate, tool in gates_tools:
                                    try:
                                        self.select_gate(gate)
                                        self.log_info("Checking Gate %d..." % gate)
                                        self._load_gate(allow_retry=False, adjust_grip_on_error=False)
                                        if tool >= 0:
                                            self.log_info("Tool T%d - Filament detected. Gate %d marked available" % (tool, gate))
                                        else:
                                            self.log_info("Gate %d - Filament detected. Marked available" % gate)
                                        self._set_gate_status(gate, max(self.gate_status[gate], self.GATE_AVAILABLE))
                                        try:
                                            self._unload_gate()
                                        except MmuError as ee:
                                            raise MmuError("Failure during check Gate %d %s: %s" % (gate, "(T%d)" % tool if tool >= 0 else "", str(ee)))
                                    except MmuError as ee:
                                        self._set_gate_status(gate, self.GATE_EMPTY)
                                        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=True)
                                        if tool >= 0:
                                            msg = "Tool T%d on Gate %d marked EMPTY" % (tool, gate)
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
                            # We don't do this when printing because this is expected to preceed the loading initial tool
                            if not self.is_printing():
                                try:
                                    if tool_selected == self.TOOL_GATE_BYPASS:
                                        self.select_bypass()
                                    elif tool_selected != self.TOOL_GATE_UNKNOWN:
                                        if filament_pos == self.FILAMENT_POS_LOADED:
                                            self.log_info("Restoring tool loaded prior to checking gates")
                                            self._select_and_load_tool(tool_selected)
                                        else:
                                            self.select_tool(tool_selected)
                                except MmuError as ee:
                                    raise MmuError("Failure re-selecting Tool %d: %s" % (tool_selected, str(ee)))
                            else:
                                # At least restore the selected tool, but don't re-load filament
                                self.select_tool(tool_selected)

                            if not quiet:
                                self.log_info(self._mmu_visual_to_string())

                            self._auto_filament_grip()
        except MmuError as ee:
            self.handle_mmu_error(str(ee))

    cmd_MMU_PRELOAD_help = "Preloads filament at specified or current gate"
    def cmd_MMU_PRELOAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return

        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        if self.check_if_not_calibrated(self.CALIBRATED_ESSENTIAL, check_gates=[gate] if gate != -1 else [self.gate_selected]): return

        self.log_always("Preloading filament in %s" % (("Gate %d" % gate) if gate >= 0 else "current gate"))
        try:
            with self.wrap_action(self.ACTION_CHECKING):
                with self._wrap_sync_gear_to_extruder(): # Don't undo syncing state if called in print
                    with self.wrap_suppress_visual_log():
                        # If gate not specified assume current gate
                        if gate == -1:
                            gate = self.gate_selected
                        else:
                            self.select_gate(gate)
                        for _ in range(self.preload_attempts):
                            self.log_always("Loading...")
                            try:
                                self._load_gate(allow_retry=False, adjust_grip_on_error=False)
                                self._check_pending_spool_id(gate) # Have spool_id ready?
                                self.log_always("Parking...")
                                self._unload_gate()
                                self.log_always("Filament detected and parked in Gate %d" % gate)
                                return
                            except MmuError as ee:
                                # Exception just means filament is not loaded yet, so continue
                                self.log_trace("Exception on preload: %s" % str(ee))
                        self.log_always("Filament not detected in Gate %d" % gate)
                        self._set_gate_status(gate, self.GATE_EMPTY)
                        self._initialize_encoder() # Encoder 0000
                        self._auto_filament_grip()
        except MmuError as ee:
            self.handle_mmu_error("Filament preload for Gate %d failed: %s" % (gate, str(ee)))

def load_config(config):
    return Mmu(config)
