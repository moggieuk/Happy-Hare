# Happy Hare MMU Software
#
# Shared constants and classes for each access and class loading
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

VERSION = 4.0 # When this is revved, Happy Hare will instruct users to re-run ./install.sh. Sync with install.sh!

# Vendor MMU's supported
VENDOR_ERCF           = "ERCF"
VENDOR_TRADRACK       = "Tradrack"
VENDOR_PRUSA          = "Prusa"
VENDOR_ANGRY_BEAVER   = "AngryBeaver"
VENDOR_BOX_TURTLE     = "BoxTurtle"
VENDOR_NIGHT_OWL      = "NightOwl"
VENDOR_3MS            = "3MS"
VENDOR_3D_CHAMELEON   = "3DChameleon"
VENDOR_PICO_MMU       = "PicoMMU"
VENDOR_QUATTRO_BOX    = "QuattroBox"
VENDOR_MMX            = "MMX"
VENDOR_VVD            = "VVD"
VENDOR_KMS            = "KMS"
VENDOR_EMU            = "EMU"
VENDOR_OTHER          = "Other"

UNIT_ALT_DISPLAY_NAMES = {
    VENDOR_ANGRY_BEAVER: "Angry Beaver",
    VENDOR_BOX_TURTLE:   "Box Turtle",
    VENDOR_NIGHT_OWL:    "Night Owl",
    VENDOR_VVD:          "BTT ViViD",
}

VENDORS = [
    VENDOR_ERCF,
    VENDOR_TRADRACK,
    VENDOR_PRUSA,
    VENDOR_ANGRY_BEAVER,
    VENDOR_BOX_TURTLE,
    VENDOR_NIGHT_OWL,
    VENDOR_3MS,
    VENDOR_3D_CHAMELEON,
    VENDOR_PICO_MMU,
    VENDOR_QUATTRO_BOX,
    VENDOR_MMX,
    VENDOR_VVD,
    VENDOR_KMS,
    VENDOR_EMU,
    VENDOR_OTHER
]

BOOT_DELAY = 2.5 # Delay before running bootup tasks

TOOL_GATE_UNKNOWN = -1
TOOL_GATE_BYPASS = -2

GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1 # Available to load from either buffer or spool
GATE_AVAILABLE_FROM_BUFFER = 2

FILAMENT_POS_UNKNOWN = -1
FILAMENT_POS_UNLOADED = 0       # Parked in gate
FILAMENT_POS_HOMED_GATE = 1     # Homed at either gate or gear sensor (currently assumed mutually exclusive sensors)
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
FORM_TIP_STANDALONE = 2         # Happy Hare forms tips

PURGE_NONE = 0                  # Skip purging after load
PURGE_SLICER = 1                # Slicer purges on wipetower
PURGE_STANDALONE = 2            # Happy Hare purges

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
ACTION_CUTTING_TIP = 10         # Cutting at toolhead e.g.  _MMU_CUT_TIP macro
ACTION_CUTTING_FILAMENT = 11    # Cutting at MMU e.g. EREC cutting macro
ACTION_PURGING = 12             # Non slicer purging e.g. when running blobifier

MACRO_EVENT_RESTART          = "restart"          # Params: None
MACRO_EVENT_GATE_MAP_CHANGED = "gate_map_changed" # Params: GATE changed or GATE=-1 for all
MACRO_EVENT_FILAMENT_GRIPPED = "filament_gripped" # Params: None

# Standard sensor and endstop or pseudo endstop names
SENSOR_ENCODER             = "encoder"        # Fake Gate endstop
SENSOR_GATE                = "mmu_gate"       # Gate
SENSOR_GEAR_PREFIX         = "mmu_gear"

SENSOR_EXTRUDER_NONE       = "none"           # Fake Extruder endstop aka don't attempt home
SENSOR_EXTRUDER_COLLISION  = "collision"      # Fake Extruder endstop
SENSOR_EXTRUDER_ENTRY      = "extruder"       # Extruder entry sensor
SENSOR_GEAR_TOUCH          = "mmu_gear_touch" # Stallguard based detection

SENSOR_COMPRESSION         = "filament_compression"  # Filament sync-feedback compression detection
SENSOR_TENSION             = "filament_tension"      # Filament sync-feedback tension detection
SENSOR_PROPORTIONAL        = "filament_proportional" # Proportional sync-feedback sensor

SENSOR_TOOLHEAD            = "toolhead"
SENSOR_EXTRUDER_TOUCH      = "mmu_ext_touch"

SENSOR_SELECTOR_TOUCH      = "mmu_sel_touch"  # For LinearSelector and LinearServoSelector
SENSOR_SELECTOR_HOME       = "mmu_sel_home"   # For LinearSelector and LinearServoSelector
SENSOR_PRE_GATE_PREFIX     = "mmu_pre_gate"

EXTRUDER_ENDSTOPS = [SENSOR_EXTRUDER_COLLISION, SENSOR_GEAR_TOUCH, SENSOR_EXTRUDER_ENTRY, SENSOR_EXTRUDER_NONE, SENSOR_COMPRESSION]
GATE_ENDSTOPS     = [SENSOR_GATE, SENSOR_ENCODER, SENSOR_GEAR_PREFIX, SENSOR_EXTRUDER_ENTRY]

# Statistics output types
GATE_STATS_STRING     = "string"
GATE_STATS_PERCENTAGE = "percentage"
GATE_STATS_EMOTICON   = "emoticon"

GATE_STATS_TYPES = [GATE_STATS_STRING, GATE_STATS_PERCENTAGE, GATE_STATS_EMOTICON]

# Levels of logging
LOG_ESSENTIAL = 0
LOG_INFO      = 1
LOG_DEBUG     = 2
LOG_TRACE     = 3
LOG_STEPPER   = 4
LOG_LEVELS = ['ESSENTAL', 'INFO', 'DEBUG', 'TRACE', 'STEPPER']

# States of espooler motor
ESPOOLER_OFF    = 'off'
ESPOOLER_REWIND = 'rewind'
ESPOOLER_ASSIST = 'assist'
ESPOOLER_PRINT  = 'print'  # Special in-print assist state for active gate
ESPOOLER_OPERATIONS = [ESPOOLER_OFF, ESPOOLER_REWIND, ESPOOLER_ASSIST, ESPOOLER_PRINT]

# Name used to save gcode state
TOOLHEAD_POSITION_STATE = 'MMU_state'

# Filament "grip" states
FILAMENT_UNKNOWN_STATE = -1
FILAMENT_RELEASE_STATE = 0
FILAMENT_DRIVE_STATE   = 1
FILAMENT_HOLD_STATE    = 2

# mmu_vars.cfg variables
VARS_MMU_REVISION                 = "mmu__revision"
VARS_MMU_ENABLE_ENDLESS_SPOOL     = "mmu_state_enable_endless_spool"
VARS_MMU_ENDLESS_SPOOL_GROUPS     = "mmu_state_endless_spool_groups"
VARS_MMU_TOOL_TO_GATE_MAP         = "mmu_state_tool_to_gate_map"
VARS_MMU_GATE_STATUS              = "mmu_state_gate_status"
VARS_MMU_GATE_MATERIAL            = "mmu_state_gate_material"
VARS_MMU_GATE_COLOR               = "mmu_state_gate_color"
VARS_MMU_GATE_FILAMENT_NAME       = "mmu_state_gate_filament_name"
VARS_MMU_GATE_TEMPERATURE         = "mmu_state_gate_temperature"
VARS_MMU_GATE_SPOOL_ID            = "mmu_state_gate_spool_id"
VARS_MMU_GATE_SPEED_OVERRIDE      = "mmu_state_gate_speed_override"
VARS_MMU_GATE_SELECTED            = "mmu_state_gate_selected"
VARS_MMU_TOOL_SELECTED            = "mmu_state_tool_selected"
VARS_MMU_LAST_TOOL                = "mmu_state_last_tool"
VARS_MMU_FILAMENT_POS             = "mmu_state_filament_pos"
VARS_MMU_FILAMENT_REMAINING       = "mmu_state_filament_remaining"
VARS_MMU_FILAMENT_REMAINING_COLOR = "mmu_state_filament_remaining_color"
VARS_MMU_SWAP_STATISTICS          = "mmu_statistics_swaps"
VARS_MMU_COUNTERS                 = "mmu_statistics_counters"

# Per-encoder variables (calibration)
VARS_MMU_ENCODER_RESOLUTION       = "mmu_encoder_resolution"
VARS_MMU_CALIB_CLOG_LENGTH        = "mmu_calibration_clog_length"

# Per-unit variables
VARS_MMU_GATE_STATISTICS_PREFIX   = "mmu_statistics_gate_"
VARS_MMU_GEAR_ROTATION_DISTANCES  = "mmu_gear_rotation_distances"
VARS_MMU_CALIB_BOWDEN_LENGTHS     = "mmu_calibration_bowden_lengths" # Per-gate calibrated bowden lengths
VARS_MMU_CALIB_BOWDEN_HOME        = "mmu_calibration_bowden_home"    # Was encoder, gate or gear sensor used as reference point

# Per-unit selector
VARS_MMU_SELECTOR_OFFSETS         = "mmu_selector_offsets"
VARS_MMU_SELECTOR_BYPASS          = "mmu_selector_bypass"
VARS_MMU_SELECTOR_GATE_POS        = "mmu_selector_gate_pos"
VARS_MMU_SELECTOR_ANGLES          = "mmu_selector_angles"
VARS_MMU_SELECTOR_BYPASS_ANGLE    = "mmu_selector_bypass_angle"
VARS_MMU_SERVO_ANGLES             = "mmu_servo_angles"               # Used on linear selectors with servo for filament grip

# Mainsail/Fluid visualization of extruder colors and other attributes
T_MACRO_COLOR_ALLGATES = 'allgates' # Color from gate map (all tools). Will add spool_id if spoolman is enabled
T_MACRO_COLOR_GATEMAP  = 'gatemap'  # As per gatemap but hide empty tools. Will add spool_id if spoolman is enabled
T_MACRO_COLOR_SLICER   = 'slicer'   # Color from slicer tool map. Will add spool_id if spoolman is enabled
T_MACRO_COLOR_OFF      = 'off'      # Turn off color and spool_id association
T_MACRO_COLOR_OPTIONS  = [T_MACRO_COLOR_GATEMAP, T_MACRO_COLOR_SLICER, T_MACRO_COLOR_ALLGATES, T_MACRO_COLOR_OFF]

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

# Use (common) unicode for improved formatting and klipper layout
UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE = '\u00A0', '\u00A0', '\u2014', '\u00B0', '\u2588', '\u2514'
UI_BOX_TL, UI_BOX_BL, UI_BOX_TR, UI_BOX_BR = '\u250C', '\u2514', '\u2510', '\u2518'
UI_BOX_L,  UI_BOX_R,  UI_BOX_T,  UI_BOX_B  = '\u251C', '\u2524', '\u252C', '\u2534'
UI_BOX_M,  UI_BOX_H,  UI_BOX_V             = '\u253C', '\u2500', '\u2502'
UI_EMOTICONS = [UI_DASH, '\U0001F60E', '\U0001F603', '\U0001F60A', '\U0001F610', '\U0001F61F', '\U0001F622', '\U0001F631']
UI_SQUARE, UI_CUBE = '\u00B2', '\u00B3'
