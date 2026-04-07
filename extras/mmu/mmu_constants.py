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
FILAMENT_POS_HOMED_GATE = 1     # Homed at either gate or mmu exit sensor (currently assumed mutually exclusive sensors)
FILAMENT_POS_START_BOWDEN = 2   # Point of fast load portion
FILAMENT_POS_IN_BOWDEN = 3      # Some unknown position in the bowden
FILAMENT_POS_END_BOWDEN = 4     # End of fast load portion
FILAMENT_POS_HOMED_ENTRY = 5    # Homed at entry sensor
FILAMENT_POS_HOMED_EXTRUDER = 6 # Collision homing case at extruder gear entry
FILAMENT_POS_EXTRUDER_ENTRY = 7 # Past extruder gear entry
FILAMENT_POS_HOMED_TS = 8       # Homed at toolhead sensor
FILAMENT_POS_IN_EXTRUDER = 9    # In extruder past toolhead sensor
FILAMENT_POS_LOADED = 10        # Homed to nozzle

FILAMENT_POS_NAME_MAP = {
   -1: "UNKNOWN",
    0: "UNLOADED AND PARKED",
    1: "HOMED AT GATE",
    2: "START OF BOWDEN",
    3: "IN BOWDEN",
    4: "END OF BOWDEN",
    5: "HOMED AT EXTRUDER SENSOR",
    6: "AT EXTRUDER GEAR",
    7: "PAST EXTRUDER_GEAR",
    8: "HOMED AT TOOLHEAD SENSOR",
    9: "IN EXTRUDER",
   10: "LOADED IN NOZZLE",
}

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
SENSOR_ENCODER           = "encoder"               # Fake Gate endstop using encoder
SENSOR_SHARED_EXIT       = "mmu_shared_exit"
SENSOR_EXIT_PREFIX       = "mmu_exit"

SENSOR_EXTRUDER_NONE     = "none"                  # Fake Extruder endstop aka don't attempt home
SENSOR_EXTRUDER_ENCODER  = "encoder"               # Fake Extruder endstop (uses encoder to detect collision)
SENSOR_EXTRUDER_ENTRY    = "extruder"              # Extruder entry sensor
SENSOR_GEAR_TOUCH        = "mmu_gear_touch"        # Stallguard based detection

SENSOR_COMPRESSION       = "filament_compression"  # Filament sync-feedback compression detection
SENSOR_TENSION           = "filament_tension"      # Filament sync-feedback tension detection
SENSOR_PROPORTIONAL      = "filament_proportional" # Proportional sync-feedback sensor

SENSOR_TOOLHEAD          = "toolhead"
SENSOR_EXTRUDER_TOUCH    = "mmu_ext_touch"

SENSOR_SELECTOR_TOUCH    = "mmu_sel_touch"  # For LinearSelector and LinearServoSelector
SENSOR_SELECTOR_HOME     = "mmu_sel_home"   # For LinearSelector and LinearServoSelector
SENSOR_ENTRY_PREFIX      = "mmu_entry"

EXTRUDER_ENDSTOPS = [SENSOR_EXTRUDER_ENCODER, SENSOR_GEAR_TOUCH, SENSOR_EXTRUDER_ENTRY, SENSOR_EXTRUDER_NONE, SENSOR_COMPRESSION]
GATE_ENDSTOPS     = [SENSOR_SHARED_EXIT, SENSOR_ENCODER, SENSOR_EXIT_PREFIX, SENSOR_EXTRUDER_ENTRY]

# Gear/Extruder synchronization modes (None = unsynced)
DRIVE_UNSYNCED                = None
DRIVE_EXTRUDER_SYNCED_TO_GEAR = 1 # Aka 'gear+extruder'
DRIVE_EXTRUDER_ONLY_ON_GEAR   = 2 # Aka 'extruder' (only)
DRIVE_GEAR_SYNCED_TO_EXTRUDER = 3 # Aka 'extruder+gear'
DRIVE_GEAR_ONLY               = 4 # Aka 'gear' (same state as unsync() but with protective wait)

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
ESPOOLER_NONE   = ''
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
VARS_MMU_REVISION                  = "mmu__revision"
VARS_MMU_ENABLE_ENDLESS_SPOOL      = "mmu_state_enable_endless_spool"
VARS_MMU_ENDLESS_SPOOL_GROUPS      = "mmu_state_endless_spool_groups"
VARS_MMU_TOOL_TO_GATE_MAP          = "mmu_state_tool_to_gate_map"
VARS_MMU_GATE_STATUS               = "mmu_state_gate_status"
VARS_MMU_GATE_MATERIAL             = "mmu_state_gate_material"
VARS_MMU_GATE_COLOR                = "mmu_state_gate_color"
VARS_MMU_GATE_FILAMENT_NAME        = "mmu_state_gate_filament_name"
VARS_MMU_GATE_TEMPERATURE          = "mmu_state_gate_temperature"
VARS_MMU_GATE_SPOOL_ID             = "mmu_state_gate_spool_id"
VARS_MMU_GATE_SPEED_OVERRIDE       = "mmu_state_gate_speed_override"
VARS_MMU_GATE_SELECTED             = "mmu_state_gate_selected"
VARS_MMU_TOOL_SELECTED             = "mmu_state_tool_selected"
VARS_MMU_LAST_TOOL                 = "mmu_state_last_tool"
VARS_MMU_FILAMENT_POS              = "mmu_state_filament_pos"
VARS_MMU_FILAMENT_REMAINING        = "mmu_state_filament_remaining"
VARS_MMU_FILAMENT_REMAINING_COLOR  = "mmu_state_filament_remaining_color"
VARS_MMU_SWAP_STATISTICS           = "mmu_statistics_swaps"
VARS_MMU_COUNTERS                  = "mmu_statistics_counters"

# Per-encoder variables (calibration)
VARS_MMU_ENCODER_RESOLUTION        = "mmu_encoder_resolution"
VARS_MMU_ENCODER_CLOG_LENGTH       = "mmu_encoder_clog_length"

# Per-unit variables
VARS_MMU_GATE_STATISTICS_PREFIX    = "mmu_statistics_gate_"
VARS_MMU_GEAR_ROTATION_DISTANCES   = "mmu_gear_rotation_distances"
VARS_MMU_BOWDEN_LENGTHS            = "mmu_bowden_lengths"             # Per-gate calibrated bowden lengths
VARS_MMU_BOWDEN_HOME               = "mmu_bowden_home"                # Was encoder, gate or mmu exit sensor used as reference point

# Per-unit selector
VARS_MMU_SELECTOR_OFFSETS          = "mmu_selector_offsets"
VARS_MMU_SELECTOR_BYPASS_OFFSET    = "mmu_selector_bypass_offset"
VARS_MMU_SELECTOR_LAST_POS         = "mmu_selector_last_pos"          # Persisted gate position (can save the need to re-home on startup)
VARS_MMU_SELECTOR_ANGLES           = "mmu_selector_angles"
VARS_MMU_SELECTOR_BYPASS_ANGLE     = "mmu_selector_bypass_angle"
VARS_MMU_SELECTOR_RELEASE_ANGLE    = "mmu_selector_release_angle"
VARS_MMU_SELECTOR_SERVO_ANGLES     = "mmu_selector_servo_angles"      # Used on linear selectors with servo for filament grip

# Mainsail/Fluid visualization of extruder colors and other attributes
T_MACRO_COLOR_ALLGATES = 'allgates' # Color from gate map (all tools). Will add spool_id if spoolman is enabled
T_MACRO_COLOR_GATEMAP  = 'gatemap'  # As per gatemap but hide empty tools. Will add spool_id if spoolman is enabled
T_MACRO_COLOR_SLICER   = 'slicer'   # Color from slicer tool map. Will add spool_id if spoolman is enabled
T_MACRO_COLOR_OFF      = 'off'      # Turn off color and spool_id association
T_MACRO_COLOR_OPTIONS  = [T_MACRO_COLOR_GATEMAP, T_MACRO_COLOR_SLICER, T_MACRO_COLOR_ALLGATES, T_MACRO_COLOR_OFF]

# Default color or "unknown" - translucent grey (matches various UIs)
UNKNOWN_FILAMENT_COLOR = '#808182E3'

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

# Calibration steps
CALIBRATED_GEAR_0    = 0b00001 # Specifically rotation_distance for gate 0
CALIBRATED_ENCODER   = 0b00010
CALIBRATED_SELECTOR  = 0b00100 # Defaults true with VirtualSelector
CALIBRATED_BOWDENS   = 0b01000 # Bowden length for all gates
CALIBRATED_GEAR_RDS  = 0b10000 # rotation_distance for other gates (optional)
CALIBRATED_ESSENTIAL = 0b01111
CALIBRATED_ALL       = 0b11111

UNCALIBRATED = -1              # Magic constant for uncalibrated rotation distance / bowden

# Encoder runout/clog detection modes
ENCODER_RUNOUT_DISABLED = 0
ENCODER_RUNOUT_STATIC = 1
ENCODER_RUNOUT_AUTOMATIC = 2

# Drying states (mostly relevant for per-gate heaters)
DRYING_STATE_NONE      = ''
DRYING_STATE_QUEUED    = 'queued'
DRYING_STATE_ACTIVE    = 'active'
DRYING_STATE_COMPLETE  = 'complete'
DRYING_STATE_CANCELLED = 'canceled'

EMPTY_GATE_STATS_ENTRY = {'pauses': 0, 'loads': 0, 'load_distance': 0.0, 'load_delta': 0.0, 'unloads': 0, 'unload_distance': 0.0, 'unload_delta': 0.0, 'load_failures': 0, 'unload_failures': 0, 'quality': -1.}

UPGRADE_REMINDER = "Sorry but Happy Hare requires you to re-run this to complete the update:\ncd ~/Happy-Hare\n./install.sh\nMore details: https://github.com/moggieuk/Happy-Hare/wiki/Upgrade-Notice"

# TMC chips to search for
TMC_CHIPS = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]

# Klipper TMC and stepper params that are shared to avoid replicated definitions
SHAREABLE_TMC_PARAMS     = ['run_current', 'hold_current', 'interpolate', 'sense_resistor', 'stealthchop_threshold']
SHAREABLE_STEPPER_PARAMS = ['rotation_distance', 'gear_ratio', 'microsteps', 'full_steps_per_rotation']
OTHER_STEPPER_PARAMS     = ['step_pin', 'dir_pin', 'enable_pin', 'endstop_pin', 'rotation_distance', 'pressure_advance', 'pressure_advance_smooth_time']

# Use (common) unicode for improved formatting and klipper layout
UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE = '\u00A0', '\u00A0', '\u2014', '\u00B0', '\u2588', '\u2514'
UI_BOX_TL, UI_BOX_BL, UI_BOX_TR, UI_BOX_BR = '\u250C', '\u2514', '\u2510', '\u2518'
UI_BOX_L,  UI_BOX_R,  UI_BOX_T,  UI_BOX_B  = '\u251C', '\u2524', '\u252C', '\u2534'
UI_BOX_M,  UI_BOX_H,  UI_BOX_V             = '\u253C', '\u2500', '\u2502'
UI_EMOTICONS = [UI_DASH, '\U0001F60E', '\U0001F603', '\U0001F60A', '\U0001F610', '\U0001F61F', '\U0001F622', '\U0001F631']
UI_SQUARE, UI_CUBE = '\u00B2', '\u00B3'
UI_SUPERSCRIPT_1, UI_SUPERSCRIPT_2, UI_SUPERSCRIPT_3 = '\u00B9', '\u00B2', '\u00B3'
UI_BULLET, UI_SOLID_CIRCLE, UI_SOLID_SQUARE, UI_SOLID_TRIANGLE, UI_FISHEYE = '\u2022', '\u25CF', '\u25A0', '\u25BC', '\u25C9'
