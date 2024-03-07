# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Goal: Firmware to control any Klipper based MMU
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import sys # To detect python2 or python3
import logging, logging.handlers, threading, queue, time, contextlib, math, os.path, re
from random import randint
from extras.mmu_toolhead import MmuToolHead, MmuHoming
from extras.homing import Homing, HomingMove
from extras.mmu_leds import MmuLeds
import chelper, ast

if sys.version_info[0] < 3:
    # No unicode. Not worth the hassle!
    UI_SPACE = ' '
    UI_SEPARATOR = '.'
    UI_DASH = '-'
    UI_DEGREE = '^'
    UI_BOX_BL = '+'
    UI_EMOTICONS = ['?', 'A+', 'A', 'B', 'C', 'C-', 'D', 'F']
else:
    # Use unicode for improved formatting and klipper layout
    UI_SPACE = '\u00A0'
    UI_SEPARATOR = '\u00A0'
    UI_DASH = '\u2014'
    UI_DEGREE = '\u00B0'
    UI_BOX_BL = '\u2514'
    UI_EMOTICONS = [UI_DASH, '\U0001F60E', '\U0001F603', '\U0001F60A', '\U0001F610', '\U0001F61F', '\U0001F622', '\U0001F631']

# Forward all messages through a queue (polled by background thread)
class QueueHandler(logging.Handler):
    def __init__(self, queue):
        logging.Handler.__init__(self)
        self.queue = queue

    def emit(self, record):
        try:
            self.format(record)
            record.msg = record.message
            record.args = None
            record.exc_info = None
            self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)

# Poll log queue on background thread and log each message to logfile
class QueueListener(logging.handlers.TimedRotatingFileHandler):
    def __init__(self, filename):
        logging.handlers.TimedRotatingFileHandler.__init__(self, filename, when='midnight', backupCount=5)
        self.bg_queue = queue.Queue()
        self.bg_thread = threading.Thread(target=self._bg_thread)
        self.bg_thread.start()

    def _bg_thread(self):
        while True:
            record = self.bg_queue.get(True)
            if record is None:
                break
            self.handle(record)

    def stop(self):
        self.bg_queue.put_nowait(None)
        self.bg_thread.join()

# Class to improve formatting of multi-line messages
class MultiLineFormatter(logging.Formatter):
    def format(self, record):
        indent = ' ' * 9
        lines = super(MultiLineFormatter, self).format(record)
        return lines.replace('\n', '\n' + indent)

# Mmu exception error class
class MmuError(Exception):
    pass

# Main klipper module
class Mmu:
    VERSION = 2.50 # When this is revved, Happy Hare will instruct users to re-run ./install.sh. Sync with install.sh!

    BOOT_DELAY = 2.0 # Delay before running bootup tasks

    # Calibration steps
    CALIBRATED_GEAR     = 0b00001
    CALIBRATED_ENCODER  = 0b00010
    CALIBRATED_SELECTOR = 0b00100
    CALIBRATED_BOWDEN   = 0b01000
    CALIBRATED_GATES    = 0b10000
    CALIBRATED_ALL      = 0b01111 # Calibrated gates is optional

    SERVO_MOVE_STATE = 2
    SERVO_DOWN_STATE = 1
    SERVO_UP_STATE = 0
    SERVO_UNKNOWN_STATE = -1

    TOOL_GATE_UNKNOWN = -1
    TOOL_GATE_BYPASS = -2

    GATE_UNKNOWN = -1
    GATE_EMPTY = 0
    GATE_AVAILABLE = 1 # Available to load from either buffer or spool
    GATE_AVAILABLE_FROM_BUFFER = 2

    FILAMENT_POS_UNKNOWN = -1
    FILAMENT_POS_UNLOADED = 0
    FILAMENT_POS_HOMED_GATE = 1
    FILAMENT_POS_START_BOWDEN = 2
    FILAMENT_POS_IN_BOWDEN = 3
    FILAMENT_POS_END_BOWDEN = 4
    FILAMENT_POS_HOMED_ENTRY = 5
    FILAMENT_POS_HOMED_EXTRUDER = 6
    FILAMENT_POS_EXTRUDER_ENTRY = 7
    FILAMENT_POS_HOMED_TS = 8
    FILAMENT_POS_IN_EXTRUDER = 9 # AKA FILAMENT_POS_PAST_TS
    FILAMENT_POS_LOADED = 10     # AKA FILAMENT_POS_HOMED_NOZZLE

    DIRECTION_LOAD = 1
    DIRECTION_UNKNOWN = 0
    DIRECTION_UNLOAD = -1

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

    # Standard sensor and endstop or pseudo endstop names
    ENDSTOP_ENCODER            = "encoder"        # Fake Gate endstop
    ENDSTOP_GATE               = "mmu_gate"       # Gate

    ENDSTOP_EXTRUDER_NONE      = "none"           # Fake Extruder endstop aka don't attempt home
    ENDSTOP_EXTRUDER_COLLISION = "collision"      # Fake Extruder endstop
    ENDSTOP_EXTRUDER           = "extruder"       # Extruder
    ENDSTOP_GEAR_TOUCH         = "mmu_gear_touch" # Extruder

    ENDSTOP_TOOLHEAD           = "toolhead"
    ENDSTOP_EXTRUDER_TOUCH     = "mmu_ext_touch"

    ENDSTOP_SELECTOR_TOUCH     = "mmu_sel_touch"
    ENDSTOP_SELECTOR_HOME      = "mmu_sel_home"
    PRE_GATE_SENSOR_PREFIX     = "mmu_pre_gate"

    EXTRUDER_ENDSTOPS = [ENDSTOP_EXTRUDER_COLLISION, ENDSTOP_GEAR_TOUCH, ENDSTOP_EXTRUDER, ENDSTOP_EXTRUDER_NONE]
    GATE_ENDSTOPS     = [ENDSTOP_GATE, ENDSTOP_ENCODER]

    # Statistics output types
    GATE_STATS_STRING     = "string"
    GATE_STATS_PERCENTAGE = "percentage"
    GATE_STATS_EMOTICON   = "emoticon"

    GATE_STATS_TYPES = [GATE_STATS_STRING, GATE_STATS_PERCENTAGE, GATE_STATS_EMOTICON]

    # Stepper config sections
    SELECTOR_STEPPER_CONFIG    = "stepper_mmu_selector"
    GEAR_STEPPER_CONFIG        = "stepper_mmu_gear"

    # Gear/Extruder syncing
    SWITCH_SYNC_FEEDBACK_TENSION     = "sync_feedback_tension"
    SWITCH_SYNC_FEEDBACK_COMPRESSION = "sync_feedback_compression"
    SYNC_FEEDBACK_INTERVAL  = 0.5   # How often to check extruder direction
    SYNC_POSITION_TIMERANGE = 0.6   # Interval to compare movement
    SYNC_POSITION_MIN_DELTA = 0.001 # Min extruder move distance to be significant

    # Vendor MMU's supported
    VENDOR_ERCF     = "ERCF"
    VENDOR_TRADRACK = "Tradrack"
    VENDOR_PRUSA    = "Prusa"
    VENDOR_OTHER    = "Other"

    VENDORS = [VENDOR_ERCF, VENDOR_TRADRACK, VENDOR_PRUSA, VENDOR_OTHER]

    # mmu_vars.cfg variables
    VARS_MMU_CALIB_CLOG_LENGTH      = "mmu_calibration_clog_length"
    VARS_MMU_ENABLE_ENDLESS_SPOOL   = "mmu_state_enable_endless_spool"
    VARS_MMU_ENDLESS_SPOOL_GROUPS   = "mmu_state_endless_spool_groups"
    VARS_MMU_TOOL_TO_GATE_MAP       = "mmu_state_tool_to_gate_map"
    VARS_MMU_GATE_STATUS            = "mmu_state_gate_status"
    VARS_MMU_GATE_MATERIAL          = "mmu_state_gate_material"
    VARS_MMU_GATE_COLOR             = "mmu_state_gate_color"
    VARS_MMU_GATE_SPOOL_ID          = "mmu_state_gate_spool_id"
    VARS_MMU_GATE_SPEED_OVERRIDE    = "mmu_state_gate_speed_override"
    VARS_MMU_GATE_SELECTED          = "mmu_state_gate_selected"
    VARS_MMU_TOOL_SELECTED          = "mmu_state_tool_selected"
    VARS_MMU_FILAMENT_POS           = "mmu_state_filament_pos"
    VARS_MMU_CALIB_BOWDEN_LENGTH    = "mmu_calibration_bowden_length"
    VARS_MMU_CALIB_BOWDEN_HOME      = "mmu_calibration_bowden_home"
    VARS_MMU_CALIB_PREFIX           = "mmu_calibration_"
    VARS_MMU_GATE_STATISTICS_PREFIX = "mmu_statistics_gate_"
    VARS_MMU_SWAP_STATISTICS        = "mmu_statistics_swaps"
    VARS_MMU_SELECTOR_OFFSETS       = "mmu_selector_offsets"
    VARS_MMU_SELECTOR_BYPASS        = "mmu_selector_bypass"
    VARS_MMU_ENCODER_RESOLUTION     = "mmu_encoder_resolution"
    VARS_MMU_GEAR_ROTATION_DISTANCE = "mmu_gear_rotation_distance"
    VARS_MMU_SERVO_ANGLES           = "mmu_servo_angles"

    EMPTY_GATE_STATS_ENTRY = {'pauses': 0, 'loads': 0, 'load_distance': 0.0, 'load_delta': 0.0, 'unloads': 0, 'unload_distance': 0.0, 'unload_delta': 0.0, 'servo_retries': 0, 'load_failures': 0, 'unload_failures': 0, 'quality': -1.}

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

    UPGRADE_REMINDER = "Sorry but Happy Hare requires you to re-run\n'./install.sh' to complete the update.\nMore details: https://github.com/moggieuk/Happy-Hare/blob/main/doc/upgrade.md"

    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.estimated_print_time = None
        self.last_selector_move_time = 0
        self.calibration_status = 0b0
        self.calibrated_bowden_length = -1
        self.ref_gear_rotation_distance = 1.
        self.encoder_force_validation = False
        self.sync_feedback_last_state = 0. # Neutral
        self.sync_feedback_last_direction = 0 # Extruder not moving
        self.sync_feedback_operational = False
        self.w3c_colors = dict(self.W3C_COLORS)

        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        # Instruct users to re-run ./install.sh if version number changes
        self.config_version = config.getfloat('happy_hare_version', 2.2) # v2.2 was the last release before versioning
        if self.config_version is not None and self.config_version < self.VERSION:
            raise self.config.error("Looks like you upgraded (v%s -> v%s)?\n%s" % (self.config_version, self.VERSION, self.UPGRADE_REMINDER))

        # MMU hardware (steppers, servo, encoder and optional sensors)
        self.selector_stepper = self.gear_stepper = self.mmu_extruder_stepper = self.encoder_sensor = self.servo = None
        self.sensors = {}
        bmg_circ = 23.

        self.mmu_vendor = config.getchoice('mmu_vendor', {o: o for o in self.VENDORS}, self.VENDOR_ERCF)
        self.mmu_version_string = config.get('mmu_version', "1.1")
        self.mmu_version = float(re.sub("[^0-9.]", "", self.mmu_version_string))

        # To simplfy config some parameters, mostly CAD related but a few exceptions
        # like default encoder resolution are set based on vendor and version setting

        # Set CAD default parameters to ensure everything is set
        # These are default for ERCFv1.1 - the first MMU supported by Happy Hare
        #  cad_gate0_pos          - approximate distance from endstop to first gate
        #  cad_gate_width         - width of each gate
        #  cad_bypass_offset      - distance from end of travel to the bypass
        #  cad_last_gate_offset   - distance from end of travel to last gate
        #  cad_block_width        - width of bearing block (ERCF v1.1)
        #  cad_bypass_block_width - width of bypass block (ERCF v1.1)
        #  cad_bypass_block_delta - distance from previous gate to bypass (ERCF v1.1)
        #
        #  encoder_default_resolution - resolution of a single encoder "count"
        self.cad_gate0_pos = 4.2
        self.cad_gate_width = 21.
        self.cad_bypass_offset = 0
        self.cad_last_gate_offset = 2.
        self.cad_block_width = 5.
        self.cad_bypass_block_width = 6.
        self.cad_bypass_block_delta = 9.
        self.cad_selector_tolerance = 10.

        # Vendor specific attributes
        self.variable_gate_ratios = 1 # Whether ratio of each gate can be different and needs separate calibration

        # Non CAD default parameters
        self.encoder_default_resolution = bmg_circ / (2 * 17) # TRCT5000 based sensor

        # Specific vendor build parameters / tuning.
        if self.mmu_vendor.lower() == self.VENDOR_ERCF.lower():
            if self.mmu_version >= 2.0: # V2 community edition
                self.cad_gate0_pos = 4.0
                self.cad_gate_width = 23.
                self.cad_bypass_offset = 0.72
                self.cad_last_gate_offset = 14.4

                self.encoder_default_resolution = bmg_circ / (2 * 12) # Binky 12 tooth disc with BMG gear

                # Modifications:
                #  h = ThumperBlocks filament blocks
                if "h" in self.mmu_version_string:
                    self.cad_gate_width = 21.

            else: # V1.1 original
                # Modifications:
                #  t = TripleDecky filament blocks
                #  s = Springy sprung servo selector
                #  b = Binky encoder upgrade
                if "t" in self.mmu_version_string:
                    self.cad_gate_width = 23. # Triple Decky is wider filament block
                    self.cad_block_width = 0. # Bearing blocks are not used

                if "s" in self.mmu_version_string:
                    self.cad_last_gate_offset = 1.2 # Springy has additional bump stops

                if "b" in self.mmu_version_string:
                    self.encoder_default_resolution = bmg_circ / (2 * 12) # Binky 12 tooth disc with BMG gear

        elif self.mmu_vendor.lower() == self.VENDOR_TRADRACK.lower():
            self.cad_gate0_pos = 2.5
            self.cad_gate_width = 17.
            self.cad_bypass_offset = 0 # Doesn't have bypass
            self.cad_last_gate_offset = 0. # Doesn't have reliable hard stop at limit of travel

            self.encoder_default_resolution = bmg_circ / (2 * 12) # If fitted, assumed to by Binky
            self.variable_gate_ratios = 0

            # Modifications:
            #  e = has encoder modification
            if "e" in self.mmu_version_string:
                pass

        elif self.mmu_vendor.lower() == self.VENDOR_PRUSA.lower():
            raise self.config.error("Support for Prusa systems is comming soon! You can try with vendor=Other and configure `cad` dimensions (see doc)")

        # Allow all CAD parameters to be customized
        self.cad_gate0_pos = config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_bypass_offset = config.getfloat('cad_bypass_offset', self.cad_bypass_offset, minval=0.)
        self.cad_last_gate_offset = config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)
        self.cad_block_width = config.getfloat('cad_block_width', self.cad_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_width = config.getfloat('cad_bypass_block_width', self.cad_bypass_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_delta = config.getfloat('cad_bypass_block_delta', self.cad_bypass_block_delta, above=0.) # ERCF v1.1 only
        self.cad_selector_tolerance = config.getfloat('cad_selector_tolerance', self.cad_selector_tolerance, above=0.) # Extra movement allowed by selector
        # Allow model parameters to be customized
        self.variable_gate_ratios = config.getint('variable_gate_ratios', self.variable_gate_ratios, minval=0, maxval=1)

        # Printer interaction config
        self.extruder_name = config.get('extruder', 'extruder')
        self.timeout_pause = config.getint('timeout_pause', 72000, minval=120)
        self.default_idle_timeout = config.getint('default_idle_timeout', -1, minval=120)
        self.disable_heater = config.getint('disable_heater', 600, minval=60)
        self.default_extruder_temp = config.getfloat('default_extruder_temp', 200.)
        self.gcode_load_sequence = config.getint('gcode_load_sequence', 0)
        self.gcode_unload_sequence = config.getint('gcode_unload_sequence', 0)
        self.z_hop_height_toolchange = config.getfloat('z_hop_height_toolchange', 0.2, minval=0.)
        self.z_hop_height_error = config.getfloat('z_hop_height_error', 1., minval=0.)
        self.z_hop_speed = config.getfloat('z_hop_speed', 15., minval=1.)
        self.restore_toolhead_xy_position = config.getint('restore_toolhead_xy_postion', 0) # Not currently exposed
        self.slicer_tip_park_pos = config.getfloat('slicer_tip_park_pos', 0., minval=0.)
        self.force_form_tip_standalone = config.getint('force_form_tip_standalone', 0, minval=0, maxval=1)
        self.persistence_level = config.getint('persistence_level', 0, minval=0, maxval=4)
        self.auto_calibrate_gates = config.getint('auto_calibrate_gates', 0, minval=0, maxval=1)
        self.strict_filament_recovery = config.getint('strict_filament_recovery', 0, minval=0, maxval=1)
        self.filament_recovery_on_pause = config.getint('filament_recovery_on_pause', 1, minval=0, maxval=1)
        self.retry_tool_change_on_error = config.getint('retry_tool_change_on_error', 0, minval=0, maxval=1)
        self.print_start_detection = config.getint('print_start_detection', 1, minval=0, maxval=1)
        self.show_error_dialog = config.getint('show_error_dialog', 1, minval=0, maxval=1)

        # Internal macro overrides
        self.pause_macro = config.get('pause_macro', 'PAUSE')
        self.action_changed_macro = config.get('action_changed_macro', '_MMU_ACTION_CHANGED')
        self.print_state_changed_macro = config.get('print_state_changed_macro', '_MMU_PRINT_STATE_CHANGED')
        self.gate_map_changed_macro = config.get('gate_map_changed_macro', '_MMU_GATE_MAP_CHANGED')
        self.form_tip_macro = config.get('form_tip_macro', '_MMU_FORM_TIP')
        self.pre_unload_macro = config.get('pre_unload_macro', '_MMU_PRE_UNLOAD')
        self.post_form_tip_macro = config.get('post_form_tip_macro', '_MMU_POST_FORM_TIP')
        self.post_unload_macro = config.get('post_unload_macro', '_MMU_POST_UNLOAD')
        self.pre_load_macro = config.get('pre_load_macro', '_MMU_PRE_LOAD')
        self.post_load_macro = config.get('post_load_macro', '_MMU_POST_LOAD_MACRO')
        self.unload_sequence_macro = config.get('unload_sequence_macro', '_MMU_UNLOAD_SEQUENCE')
        self.load_sequence_macro = config.get('load_sequence_macro', '_MMU_LOAD_SEQUENCE')
        self.error_dialog_macro = config.get('error_dialog_macro', '_MMU_ERROR_DIALOG') # Not currently exposed
        self.clear_position_macro = config.get('clear_position_macro', '_MMU_CLEAR_POSITION') # Not currently exposed

        # User MMU setup
        self.mmu_num_gates = config.getint('mmu_num_gates')
        self.selector_offsets = list(config.getfloatlist('selector_offsets', []))
        self.bypass_offset = config.getfloat('selector_bypass', 0)
        self.default_ttg_map = list(config.getintlist('tool_to_gate_map', []))
        self.default_gate_status = list(config.getintlist('gate_status', []))
        self.default_gate_material = list(config.getlist('gate_material', []))
        self.default_gate_color = list(config.getlist('gate_color', []))
        self.default_gate_spool_id = list(config.getintlist('gate_spool_id', []))
        self.default_gate_speed_override = list(config.getintlist('gate_speed_override', []))

        # Configuration for gate loading and unloading
        self.gate_homing_endstop = config.getchoice('gate_homing_endstop', {o: o for o in self.GATE_ENDSTOPS}, self.ENDSTOP_ENCODER)
        self.gate_endstop_to_encoder = config.getfloat('gate_endstop_to_encoder', 0., minval=0.)
        self.gate_unload_buffer = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting
        self.gate_homing_max = config.getfloat('gate_homing_max', 2 * self.gate_unload_buffer, minval=self.gate_unload_buffer)
        self.gate_parking_distance = config.getfloat('gate_parking_distance', 23.) # Can be +ve or -ve
        self.gate_load_retries = config.getint('gate_load_retries', 2, minval=1, maxval=5)
        self.encoder_move_step_size = config.getfloat('encoder_move_step_size', 15., minval=5., maxval=25.) # Not exposed
        self.encoder_dwell = config.getfloat('encoder_dwell', 0.1, minval=0., maxval=2.) # Not exposed
        self.encoder_default_resolution = config.getfloat('encoder_default_resolution', self.encoder_default_resolution)

        # Configuration for (fast) bowden move
        self.bowden_apply_correction = config.getint('bowden_apply_correction', 0, minval=0, maxval=1)
        self.bowden_allowable_load_delta = config.getfloat('bowden_allowable_load_delta', 10., minval=1.)
        self.bowden_allowable_unload_delta = config.getfloat('bowden_allowable_unload_delta', self.bowden_allowable_load_delta, minval=1.)
        self.bowden_move_error_tolerance = config.getfloat('bowden_move_error_tolerance', 60, minval=0, maxval=100) # Percentage of delta of move that results in error
        self.bowden_pre_unload_test = config.getint('bowden_pre_unload_test', 0, minval=0, maxval=1) # Check for bowden movement before full pull
        self.bowden_pre_unload_error_tolerance = config.getfloat('bowden_pre_unload_error_tolerance', 100, minval=0, maxval=100) # Allowable delta movement % before error

        # Configuration for extruder and toolhead homing
        self.extruder_force_homing = config.getint('extruder_force_homing', 0, minval=0, maxval=1)
        self.extruder_homing_endstop = config.getchoice('extruder_homing_endstop', {o: o for o in self.EXTRUDER_ENDSTOPS}, self.ENDSTOP_EXTRUDER_COLLISION)
        self.extruder_homing_max = config.getfloat('extruder_homing_max', 50., above=10.)
        self.extruder_collision_homing_step = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max = config.getfloat('toolhead_homing_max', 20., minval=0.)
        self.toolhead_extruder_to_nozzle = config.getfloat('toolhead_extruder_to_nozzle', 0., minval=5.) # For "sensorless"
        self.toolhead_sensor_to_nozzle = config.getfloat('toolhead_sensor_to_nozzle', 0., minval=1.) # For toolhead sensor
        self.toolhead_entry_to_extruder = config.getfloat('toolhead_entry_to_extruder', 0., minval=0.) # For extruder (entry) sensor
        self.toolhead_ooze_reduction = config.getfloat('toolhead_ooze_reduction', 0., minval=-10., maxval=25.) # +ve value = reduction of load length
        self.toolhead_unload_safety_margin = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.) # Extra unload distance
        self.toolhead_move_error_tolerance = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100) # Allowable delta movement % before error

        # Extra Gear/Extruder synchronization controls
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip = config.getint('sync_form_tip', 0, minval=0, maxval=1)
        self.sync_multiplier_high = config.getfloat('sync_multiplier_high', 1.05, minval=1., maxval=2.)
        self.sync_multiplier_low = config.getfloat('sync_multipler_low', 0.95, minval=0.5, maxval=1.)
        self.sync_feedback_enable = config.getint('sync_feedback_enable', 0, minval=0, maxval=1)

        # Servo control
        self.servo_angles = {}
        self.servo_angles['down'] = config.getint('servo_down_angle', 90)
        self.servo_angles['up'] = config.getint('servo_up_angle', 90)
        self.servo_angles['move'] = config.getint('servo_move_angle', self.servo_angles['up'])
        self.servo_duration = config.getfloat('servo_duration', 0.2, minval=0.1)
        self.servo_active_down = config.getint('servo_active_down', 0, minval=0, maxval=1)
        self.servo_dwell = config.getfloat('servo_dwell', 0.4, minval=0.1)
        self.servo_buzz_gear_on_down = config.getint('servo_buzz_gear_on_down', 3, minval=0, maxval=10)

        # TMC current control
        self.extruder_homing_current = config.getint('extruder_homing_current', 50, minval=10, maxval=100)
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

        # Selector speeds
        self.selector_move_speed = config.getfloat('selector_move_speed', 200, minval=1.)
        self.selector_homing_speed = config.getfloat('selector_homing_speed', 100, minval=1.)
        self.selector_touch_speed = config.getfloat('selector_touch_speed', 60, minval=1.)

        # Optional features
        self.encoder_move_validation = config.getint('encoder_move_validation', 1, minval=0, maxval=1) # Use encoder to check load/unload movement
        self.selector_touch_enable = config.getint('selector_touch_enable', 1, minval=0, maxval=1)
        self.enable_clog_detection = config.getint('enable_clog_detection', 2, minval=0, maxval=2)
        self.enable_spoolman = config.getint('enable_spoolman', 0, minval=0, maxval=1)
        self.default_enable_endless_spool = config.getint('enable_endless_spool', 0, minval=0, maxval=1)
        self.endless_spool_final_eject = config.getfloat('endless_spool_final_eject', 50, minval=0.)
        self.endless_spool_on_load = config.getint('endless_spool_on_load', 0, minval=0, maxval=1)
        self.default_endless_spool_groups = list(config.getintlist('endless_spool_groups', []))
        self.tool_extrusion_multipliers = []
        self.tool_speed_multipliers = []

        # Logging
        self.log_level = config.getint('log_level', 1, minval=0, maxval=4)
        self.log_file_level = config.getint('log_file_level', 3, minval=-1, maxval=4)
        self.log_statistics = config.getint('log_statistics', 0, minval=0, maxval=1)
        self.log_visual = config.getint('log_visual', 1, minval=0, maxval=2) # TODO reduce max value to 1
        self.log_startup_status = config.getint('log_startup_status', 1, minval=0, maxval=2)

        # Cosmetic console stuff
        self.console_stat_columns = list(config.getlist('console_stat_columns', ['unload', 'load', 'total']))
        self.console_stat_rows = list(config.getlist('console_stat_rows', ['total', 'job', 'job_average']))
        self.console_gate_stat = config.get('console_gate_stat', {o: o for o in self.GATE_STATS_TYPES}, self.GATE_STATS_STRING)
        self.console_always_output_full = config.getint('console_always_output_full', 1, minval=0, maxval=1)

        # Currently hidden and testing options
        self.homing_extruder = config.getint('homing_extruder', 1, minval=0, maxval=1) # Special MMU homing extruder or klipper default
        self.virtual_selector = bool(config.getint('virtual_selector', 0, minval=0, maxval=1))
        self.test_random_failures = config.getint('test_random_failures', 0, minval=0, maxval=1)
        self.test_disable_encoder = config.getint('test_disable_encoder', 0, minval=0, maxval=1)
        self.test_force_in_print = config.getint('test_force_in_print', 0, minval=0, maxval=1)
        self.canbus_comms_retries = config.getint('canbus_comms_retries', 3, minval=1, maxval=10) # Workaround CANbus communication timeout error

        # The following lists are the defaults (when reset) and will be overriden by values in mmu_vars.cfg...

        # Endless spool groups
        self.enable_endless_spool = self.default_enable_endless_spool
        if len(self.default_endless_spool_groups) > 0:
            if self.enable_endless_spool == 1 and len(self.default_endless_spool_groups) != self.mmu_num_gates:
                raise self.config.error("endless_spool_groups has a different number of values than the number of gates")
        else:
            self.default_endless_spool_groups = list(range(self.mmu_num_gates))
        self.endless_spool_groups = list(self.default_endless_spool_groups)

        # Components of the gate map (status, material, color, spool_id and speed override)
        self.gate_map_vars = [ (self.VARS_MMU_GATE_STATUS, 'gate_status', self.GATE_UNKNOWN),
                               (self.VARS_MMU_GATE_MATERIAL, 'gate_material', ""),
                               (self.VARS_MMU_GATE_COLOR, 'gate_color', ""),
                               (self.VARS_MMU_GATE_SPOOL_ID, 'gate_spool_id', -1),
                               (self.VARS_MMU_GATE_SPEED_OVERRIDE, 'gate_speed_override', 100) ]

        for var, attr, default in self.gate_map_vars:
            default_attr_name = "default_" + attr
            default_attr = getattr(self, default_attr_name)
            if len(default_attr) > 0:
                if len(default_attr) != self.mmu_num_gates:
                    raise self.config.error("%s has different number of entries than the number of gates" % attr)
            else:
                default_attr.extend([default] * self.mmu_num_gates)
            setattr(self, attr, list(default_attr))
            if attr == 'gate_color':
                self._update_gate_color(getattr(self, attr))

        # Tool to gate mapping
        if len(self.default_ttg_map) > 0:
            if not len(self.default_ttg_map) == self.mmu_num_gates:
                raise self.config.error("tool_to_gate_map has different number of values than the number of gates")
        else:
            self.default_ttg_map = list(range(self.mmu_num_gates))
        self.ttg_map = list(self.default_ttg_map)

        # Tool speed and extrusion multipliers
        for i in range(self.mmu_num_gates):
            self.tool_extrusion_multipliers.append(1.)
            self.tool_speed_multipliers.append(1.)

        # Initialize state and statistics variables
        self._initialize_state()
        self._reset_statistics()

        # Logging
        self.queue_listener = None
        self.mmu_logger = None

        # Register GCODE commands
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
        self.gcode.register_command('MMU_CALIBRATE_SELECTOR', self.cmd_MMU_CALIBRATE_SELECTOR, desc = self.cmd_MMU_CALIBRATE_SELECTOR_help)
        self.gcode.register_command('MMU_CALIBRATE_BOWDEN', self.cmd_MMU_CALIBRATE_BOWDEN, desc = self.cmd_MMU_CALIBRATE_BOWDEN_help)
        self.gcode.register_command('MMU_CALIBRATE_GATES', self.cmd_MMU_CALIBRATE_GATES, desc = self.cmd_MMU_CALIBRATE_GATES_help)

        # Servo and motor control
        self.gcode.register_command('MMU_SERVO', self.cmd_MMU_SERVO, desc = self.cmd_MMU_SERVO_help)
        self.gcode.register_command('MMU_MOTORS_OFF', self.cmd_MMU_MOTORS_OFF, desc = self.cmd_MMU_MOTORS_OFF_help)
        self.gcode.register_command('MMU_SYNC_GEAR_MOTOR', self.cmd_MMU_SYNC_GEAR_MOTOR, desc=self.cmd_MMU_SYNC_GEAR_MOTOR_help)

        # Core MMU functionality
        self.gcode.register_command('MMU', self.cmd_MMU, desc = self.cmd_MMU_help)

        # Endstops for print start / stop. Automatically called if printing from virtual SD-card
        self.gcode.register_command('_MMU_PRINT_START', self.cmd_MMU_PRINT_START, desc = self.cmd_MMU_PRINT_START_help)
        self.gcode.register_command('_MMU_PRINT_END', self.cmd_MMU_PRINT_END, desc = self.cmd_MMU_PRINT_END_help)

        self.gcode.register_command('MMU_HELP', self.cmd_MMU_HELP, desc = self.cmd_MMU_HELP_help)
        self.gcode.register_command('MMU_ENCODER', self.cmd_MMU_ENCODER, desc = self.cmd_MMU_ENCODER_help)
        self.gcode.register_command('MMU_LED', self.cmd_MMU_LED, desc = self.cmd_MMU_LED_help)
        self.gcode.register_command('MMU_HOME', self.cmd_MMU_HOME, desc = self.cmd_MMU_HOME_help)
        self.gcode.register_command('MMU_SELECT', self.cmd_MMU_SELECT, desc = self.cmd_MMU_SELECT_help)
        self.gcode.register_command('MMU_PRELOAD', self.cmd_MMU_PRELOAD, desc = self.cmd_MMU_PRELOAD_help)
        self.gcode.register_command('MMU_SELECT_BYPASS', self.cmd_MMU_SELECT_BYPASS, desc = self.cmd_MMU_SELECT_BYPASS_help)
        self.gcode.register_command('MMU_CHANGE_TOOL', self.cmd_MMU_CHANGE_TOOL, desc = self.cmd_MMU_CHANGE_TOOL_help)
        # TODO currently not registered directly as Tx commands because not visible by Mainsail/Fluuid
        # for tool in range(self.mmu_num_gates):
        #     self.gcode.register_command('T%d' % tool, self.cmd_MMU_CHANGE_TOOL, desc = "Change to tool T%d" % tool)
        self.gcode.register_command('MMU_LOAD', self.cmd_MMU_LOAD, desc=self.cmd_MMU_LOAD_help)
        self.gcode.register_command('MMU_EJECT', self.cmd_MMU_EJECT, desc = self.cmd_MMU_EJECT_help)
        self.gcode.register_command('MMU_UNLOAD', self.cmd_MMU_EJECT, desc = self.cmd_MMU_EJECT_help) # Alias for MMU_EJECT
        self.gcode.register_command('MMU_PAUSE', self.cmd_MMU_PAUSE, desc = self.cmd_MMU_PAUSE_help)
        self.gcode.register_command('MMU_UNLOCK', self.cmd_MMU_UNLOCK, desc = self.cmd_MMU_UNLOCK_help)
        self.gcode.register_command('MMU_RECOVER', self.cmd_MMU_RECOVER, desc = self.cmd_MMU_RECOVER_help)

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
        self.gcode.register_command('_MMU_TEST', self.cmd_MMU_TEST, desc = self.cmd_MMU_TEST_help) # Internal for testing

        # Soak Testing
        self.gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc = self.cmd_MMU_SOAKTEST_SELECTOR_help)
        self.gcode.register_command('MMU_SOAKTEST_LOAD_SEQUENCE', self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE, desc = self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE_help)

        # TTG and Endless spool
        self.gcode.register_command('MMU_TTG_MAP', self.cmd_MMU_TTG_MAP, desc = self.cmd_MMU_TTG_MAP_help)
        self.gcode.register_command('MMU_GATE_MAP', self.cmd_MMU_GATE_MAP, desc = self.cmd_MMU_GATE_MAP_help)
        self.gcode.register_command('MMU_ENDLESS_SPOOL', self.cmd_MMU_ENDLESS_SPOOL, desc = self.cmd_MMU_ENDLESS_SPOOL_help)
        self.gcode.register_command('MMU_CHECK_GATE', self.cmd_MMU_CHECK_GATE, desc = self.cmd_MMU_CHECK_GATE_help)
        self.gcode.register_command('MMU_TOOL_OVERRIDES', self.cmd_MMU_TOOL_OVERRIDES, desc = self.cmd_MMU_TOOL_OVERRIDES_help)
        self.gcode.register_command('MMU_SLICER_TOOL_MAP', self.cmd_MMU_SLICER_TOOL_MAP, desc = self.cmd_MMU_SLICER_TOOL_MAP_help)

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
        self.gcode.register_command('__MMU_GATE_RUNOUT', self.cmd_MMU_GATE_RUNOUT, desc = self.cmd_MMU_GATE_RUNOUT_help)
        self.gcode.register_command('__MMU_GATE_INSERT', self.cmd_MMU_GATE_INSERT, desc = self.cmd_MMU_GATE_INSERT_help)

        # Initializer tasks
        self.gcode.register_command('__MMU_BOOTUP_TASKS', self.cmd_MMU_BOOTUP_TASKS, desc = self.cmd_MMU_BOOTUP_TASKS_help) # Bootup tasks

        # We setup MMU hardware during configuration since some hardware like endstop requires
        # configuration during the MCU config phase, which happens before klipper connection
        # This assumes that the hardware configuartion appears before the `[mmu]` section
        # the installer by default already guarantees this order
        self._setup_mmu_hardware(config)

    def _setup_mmu_hardware(self, config):
        logging.info("MMU Hardware Initialization -------------------------------")
        self.has_leds = False

        # Selector and Gear h/w setup ------
        section = self.SELECTOR_STEPPER_CONFIG
        if config.has_section(section):
            # Inject options into selector stepper config regardless or what user sets
            config.fileconfig.set(section, 'position_min', -1.)
            config.fileconfig.set(section, 'position_max', self._get_max_selector_movement())
            config.fileconfig.set(section, 'homing_speed', self.selector_homing_speed)
        self.mmu_toolhead = MmuToolHead(config, self.homing_extruder)
        self.mmu_kinematics = self.mmu_toolhead.get_kinematics()
        rails = self.mmu_toolhead.get_kinematics().rails
        self.selector_rail = rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]
        self.gear_rail = rails[1]
        self.gear_stepper = self.gear_rail.steppers[0]
        self.mmu_extruder_stepper = self.mmu_toolhead.mmu_extruder_stepper # Available now if `self.homing_extruder` is True

        # Detect if selector touch is possible
        self.selector_touch = self.ENDSTOP_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enable

        # Setup filament homing sensors ------
        for name in [self.ENDSTOP_TOOLHEAD, self.ENDSTOP_GATE, self.ENDSTOP_EXTRUDER]:
            sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % name, None)
            if sensor is not None:
                self.sensors[name] = sensor
                # With MMU toolhead sensors must not accidentally pause nor call user defined macros
                # (this is done in [mmu_sensors] but legacy setups may have discrete [filament_switch_sensors])
                if name not in [self.ENDSTOP_GATE]:
                    self.sensors[name].runout_helper.runout_pause = False
                    self.sensors[name].runout_helper.runout_gcode = None
                    self.sensors[name].runout_helper.insert_gcode = None

                # Add sensor pin as an extra endstop for gear rail
                sensor_pin = self.config.getsection("filament_switch_sensor %s_sensor" % name).get("switch_pin")
                ppins = self.printer.lookup_object('pins')
                pin_params = ppins.parse_pin(sensor_pin, True, True)
                share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                ppins.allow_multi_use_pin(share_name)
                mcu_endstop = self.gear_rail.add_extra_endstop(sensor_pin, name)

                # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
                if self.homing_extruder:
                    mcu_endstop.add_stepper(self.mmu_extruder_stepper.stepper)

        # Get servo and (optional) encoder setup -----
        self.servo = self.printer.lookup_object('mmu_servo mmu_servo', None)
        if not self.servo:
            raise self.config.error("No [mmu_servo] definition found in mmu_hardware.cfg")
        self.encoder_sensor = self.printer.lookup_object('mmu_encoder mmu_encoder', None)
        if not self.encoder_sensor:
            # MMU logging not set up so use main klippy logger
            logging.warn("No [mmu_encoder] definition found in mmu_hardware.cfg. Assuming encoder is not available")

    def _setup_logging(self):
        # Setup background file based logging before logging any messages
        if self.log_file_level >= 0:
            logfile_path = self.printer.start_args['log_file']
            dirname = os.path.dirname(logfile_path)
            if dirname is None:
                mmu_log = '/tmp/mmu.log'
            else:
                mmu_log = dirname + '/mmu.log'
            self._log_debug("mmu_log=%s" % mmu_log)
            self.queue_listener = QueueListener(mmu_log)
            self.queue_listener.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
            queue_handler = QueueHandler(self.queue_listener.bg_queue)
            self.mmu_logger = logging.getLogger('mmu')
            self.mmu_logger.setLevel(logging.INFO)
            self.mmu_logger.addHandler(queue_handler)

    def handle_connect(self):
        self._setup_logging()
        self.toolhead = self.printer.lookup_object('toolhead')
        self.mmu_sensors = self.printer.lookup_object('mmu_sensors', None)

        # Sanity check extruder name
        extruder = self.printer.lookup_object(self.extruder_name, None)
        if not extruder:
            raise self.config.error("Extruder named `%s` not found on printer" % self.extruder_name)

        # See if we have a TMC controller capable of current control for filament collision detection and syncing
        # on gear_stepper and tip forming on extruder
        self.selector_tmc = self.gear_tmc = self.extruder_tmc = None
        tmc_chips = ["tmc2209", "tmc2130", "tmc2208", "tmc2660", "tmc5160", "tmc2240"]
        for chip in tmc_chips:
            if self.selector_tmc is None:
                self.selector_tmc = self.printer.lookup_object('%s stepper_mmu_selector' % chip, None)
                if self.selector_tmc is not None:
                    self._log_debug("Found %s on selector_stepper. Stallguard 'touch' homing possible." % chip)
            if self.gear_tmc is None:
                self.gear_tmc = self.printer.lookup_object('%s stepper_mmu_gear' % chip, None)
                if self.gear_tmc is not None:
                    self._log_debug("Found %s on gear_stepper. Current control enabled. Stallguard 'touch' homing possible." % chip)
            if self.extruder_tmc is None:
                self.extruder_tmc = self.printer.lookup_object("%s %s" % (chip, self.extruder_name), None)
                if self.extruder_tmc is not None:
                    self._log_debug("Found %s on extruder. Current control enabled. %s" % (chip, "Stallguard 'touch' homing possible." if self.homing_extruder else ""))

        if self.selector_tmc is None:
            self._log_debug("TMC driver not found for selector_stepper, cannot use sensorless homing and recovery")
        if self.gear_tmc is None:
            self._log_debug("TMC driver not found for gear_stepper, cannot use current reduction for collision detection or while synchronized printing")
        if self.extruder_tmc is None:
            self._log_debug("TMC driver not found for extruder, cannot use current increase for tip forming move")

        # Establish gear_stepper initial gear_stepper and extruder currents
        self.gear_default_run_current = self.gear_tmc.get_status(0)['run_current'] if self.gear_tmc else None
        self.extruder_default_run_current = self.extruder_tmc.get_status(0)['run_current'] if self.extruder_tmc else None
        self.gear_percentage_run_current = self.gear_restore_percent_run_current = self.extruder_percentage_run_current = 100.

        # Sanity check that required klipper options are enabled
        self.print_stats = self.printer.lookup_object("print_stats", None)
        if self.print_stats is None:
            self._log_debug("[virtual_sdcard] is not found in config, advanced state control is not possible")
        self.pause_resume = self.printer.lookup_object('pause_resume', None)
        if self.pause_resume is None:
            raise self.config.error("MMU requires [pause_resume] to work, please add it to your config!")

        # Sanity check to see that mmu_vars.cfg is included. This will verify path because default has single entry
        self.variables = self.printer.lookup_object('save_variables').allVariables
        if self.variables == {}:
            raise self.config.error("Calibration settings not found: mmu_vars.cfg probably not found. Check [save_variables] section in mmu_software.cfg")

        # Remember user setting of idle_timeout so it can be restored (if not overridden)
        if self.default_idle_timeout < 0:
            self.default_idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout

        # Configure gear stepper calibration (set with MMU_CALIBRATE_GEAR)
        rotation_distance = self.variables.get(self.VARS_MMU_GEAR_ROTATION_DISTANCE, None)
        if rotation_distance:
            self.gear_stepper.set_rotation_distance(rotation_distance)
            self._log_debug("Loaded saved gear rotation distance: %.6f" % rotation_distance)
            self.calibration_status |= self.CALIBRATED_GEAR
        else:
            self._log_always("Warning: Gear rotation_distance not found in mmu_vars.cfg. Probably not calibrated")
        self.ref_gear_rotation_distance = self.gear_stepper.get_rotation_distance()[0]

        # Configure encoder calibration (set with MMU_CALIBRATE_ENCODER)
        self.encoder_resolution = self.encoder_default_resolution
        if self._has_encoder():
            self.encoder_sensor.set_logger(self._log_debug) # Combine with MMU log
            self.encoder_sensor.set_extruder(self.extruder_name)
            self.encoder_sensor.set_mode(self.enable_clog_detection)

            resolution = self.variables.get(self.VARS_MMU_ENCODER_RESOLUTION, None)
            if resolution:
                self.encoder_resolution = resolution
                self.encoder_sensor.set_resolution(resolution)
                self._log_debug("Loaded saved encoder resolution: %.6f" % resolution)
                self.calibration_status |= self.CALIBRATED_ENCODER
            else:
                self._log_always("Warning: Encoder resolution not found in mmu_vars.cfg. Probably not calibrated")
        else:
            self.calibration_status |= self.CALIBRATED_ENCODER # Pretend we are calibrated to avoid warnings

        # The threshold (mm) that determines real encoder movement (set to 1.5 pulses of encoder. i.e. allow one error pulse)
        self.encoder_min = 1.5 * self.encoder_resolution

        # Configure selector calibration (set with MMU_CALIBRATE_SELECTOR)
        selector_offsets = self.variables.get(self.VARS_MMU_SELECTOR_OFFSETS, None)
        if selector_offsets:
            if len(selector_offsets) == self.mmu_num_gates:
                self.selector_offsets = selector_offsets
                self._log_debug("Loaded saved selector offsets: %s" % selector_offsets)
                self.calibration_status |= self.CALIBRATED_SELECTOR
            else:
                self._log_error("Incorrect number of gates specified in %s" % self.VARS_MMU_SELECTOR_OFFSETS)
                self.selector_offsets = [0.] * self.mmu_num_gates
        else:
            self._log_always("Warning: Selector offsets not found in mmu_vars.cfg. Probably not calibrated")
            self.selector_offsets = [0.] * self.mmu_num_gates
        bypass_offset = self.variables.get(self.VARS_MMU_SELECTOR_BYPASS, None)
        if bypass_offset:
            self.bypass_offset = bypass_offset
            self._log_debug("Loaded saved bypass offset: %s" % bypass_offset)
        else:
            self.bypass_offset = 0

        # Set bowden length from calibration
        bowden_length = self.variables.get(self.VARS_MMU_CALIB_BOWDEN_LENGTH, None)
        bowden_home = self.variables.get(self.VARS_MMU_CALIB_BOWDEN_HOME, self.ENDSTOP_ENCODER)
        if bowden_length and bowden_home in self.GATE_ENDSTOPS:
            self.calibrated_bowden_length = bowden_length
            if bowden_home != self.gate_homing_endstop:
                if bowden_home == self.ENDSTOP_ENCODER:
                    self.calibrated_bowden_length += self.gate_endstop_to_encoder
                else:
                    self.calibrated_bowden_length -= self.gate_endstop_to_encoder
                self._log_debug("Loaded and adjusted reference bowden length: %.1f" % bowden_length)
            else:
                self._log_debug("Loaded saved reference bowden length: %.1f" % bowden_length)
            self.calibration_status |= self.CALIBRATED_BOWDEN
        else:
            self._log_always("Warning: Reference bowden length not found in mmu_vars.cfg. Probably not calibrated")

        # Override with saved/calibrated servo positions
        try:
            servo_angles = self.variables.get(self.VARS_MMU_SERVO_ANGLES, {})
            self.servo_angles.update(servo_angles)
        except Exception as e:
            raise self.config.error("Exception whilst parsing servo angles from 'mmu_vars.cfg': %s" % str(e))

    def handle_disconnect(self):
        self._log_debug('Klipper disconnected! MMU Shutdown')
        if self.queue_listener is not None:
            self.queue_listener.stop()

    def handle_ready(self):
        # Reference correct extruder stepper which will definitely be available now
        self.mmu_extruder_stepper = self.mmu_toolhead.mmu_extruder_stepper
        if not self.homing_extruder:
            self._log_debug("Warning: Using original klipper extruder stepper")

        # Restore state if fully calibrated
        if not self._check_is_calibrated(silent=True):
            self._load_persisted_state()

        # Setup events for managing internal print state machine
        self.printer.register_event_handler("idle_timeout:printing", self._handle_idle_timeout_printing)
        self.printer.register_event_handler("idle_timeout:ready", self._handle_idle_timeout_ready)
        self.printer.register_event_handler("idle_timeout:idle", self._handle_idle_timeout_idle)

        # Setup events for managing motor synchronization. We use 'mmu:print_synced' instead of
        # 'mmu:print_synced' events so feedback only used while actually printing
        self.printer.register_event_handler("mmu:print_synced", self._enable_sync_feedback)
        self.printer.register_event_handler("mmu:print_unsynced", self._disable_sync_feedback)
        self.printer.register_event_handler("mmu:sync_feedback", self._handle_sync_feedback)
        self._setup_sync_feedback()

        self._setup_heater_off_reactor()
        self._clear_saved_toolhead_position()

        # This is a bit naughty to register commands here but I need to make sure we are the outermost wrapper
        try:
            prev_pause = self.gcode.register_command('PAUSE', None)
            if prev_pause is not None:
                self.gcode.register_command('__PAUSE', prev_pause)
                self.gcode.register_command('PAUSE', self.cmd_PAUSE, desc = self.cmd_PAUSE_help)
            else:
                self._log_error('No existing PAUSE macro found!')

            prev_resume = self.gcode.register_command('RESUME', None)
            if prev_resume is not None:
                self.gcode.register_command('__RESUME', prev_resume)
                self.gcode.register_command('RESUME', self.cmd_MMU_RESUME, desc = self.cmd_MMU_RESUME_help)
            else:
                self._log_error('No existing RESUME macro found!')

            prev_clear_pause = self.gcode.register_command('CLEAR_PAUSE', None)
            if prev_clear_pause is not None:
                self.gcode.register_command('__CLEAR_PAUSE', prev_clear_pause)
                self.gcode.register_command('CLEAR_PAUSE', self.cmd_CLEAR_PAUSE, desc = self.cmd_CLEAR_PAUSE_help)
            else:
                self._log_error('No existing CLEAR_PAUSE macro found!')

            prev_cancel = self.gcode.register_command('CANCEL_PRINT', None)
            if prev_cancel is not None:
                self.gcode.register_command('__CANCEL_PRINT', prev_cancel)
                self.gcode.register_command('CANCEL_PRINT', self.cmd_MMU_CANCEL_PRINT, desc = self.cmd_MMU_CANCEL_PRINT_help)
            else:
                self._log_error('No existing CANCEL_PRINT macro found!')
        except Exception as e:
            self._log_error('Error trying to wrap PAUSE/RESUME/CLEAR_PAUSE/CANCEL_PRINT macros: %s' % str(e))

        # Ensure that the LED control macro knows the indices of the segments of the LED chain and other essential data
        gcode_macro = self.printer.lookup_object("gcode_macro _MMU_SET_LED", None)
        if gcode_macro:
            try:
                led_chains = MmuLeds.chains
                led_vars = {}
                if led_chains:
                    led_vars['led_enable'] = True
                    exit = led_chains['exit']
                    led_vars['exit_first_led_index'] = exit[0] if exit else -1
                    led_vars['exit_reverse_order'] = int(exit[0] > exit[-1]) if exit else 0
                    entry = led_chains['entry']
                    led_vars['entry_first_led_index'] = entry[0] if entry else -1
                    led_vars['entry_reverse_order'] = int(entry[0] > entry[-1]) if entry else 0
                    led_vars['status_led_index'] = led_chains['status'][0] if led_chains['status'] else -1
                    led_vars['led_strip'] = MmuLeds.led_strip
                    self.has_leds = True
                    self._log_debug("LEDs support enabled")
                else:
                    led_vars['led_enable'] = False
                    self._log_debug("LEDs support is not configured")
                gcode_macro.variables.update(led_vars)
            except Exception as e:
                self._log_error('Error setting up the _MMU_SET_LED macro: %s' % str(e))
        else:
            self._log_error("LEDs macro _MMU_SET_LED not available")

        self.estimated_print_time = self.printer.lookup_object('mcu').estimated_print_time
        self.last_selector_move_time = self.estimated_print_time(self.reactor.monotonic())
        self._schedule_mmu_bootup_tasks(self.BOOT_DELAY)

    def _initialize_state(self):
        self.is_enabled = self.runout_enabled = True
        self.is_homed = self.is_handling_runout = self.calibrating = False
        self.last_print_stats = self.paused_extruder_temp = self.reason_for_pause = None
        self.tool_selected = self._next_tool = self._last_tool = self.TOOL_GATE_UNKNOWN
        self._last_toolchange = "Unknown"
        self.gate_selected = self.TOOL_GATE_UNKNOWN # We keep record of gate selected in case user messes with mapping in print
        self.active_gate = {}
        self.servo_state = self.servo_angle = self.SERVO_UNKNOWN_STATE
        self.filament_pos = self.FILAMENT_POS_UNKNOWN
        self.filament_direction = self.DIRECTION_UNKNOWN
        self.filament_remaining = 0. # Tracker of filament left in extruder by cutter
        self.action = self.ACTION_IDLE
        self._clear_saved_toolhead_position()
        self._servo_reset_state()
        self._reset_job_statistics()
        self.print_state = self.resume_to_state = "ready"
        self.form_tip_vars = None # Current defaults of gcode variables for tip forming macro
        self.custom_color_rgb = [(0.,0.,0.)] * self.mmu_num_gates
        self._clear_slicer_tool_map()

    def _clear_slicer_tool_map(self):
        self.slicer_tool_map = {'tools': {}, 'initial_tool': None, 'purge_volumes': []}

    # Helper to infer type for setting gcode macro variables
    def _fix_type(self, s):
        try:
            return float(s)
        except ValueError:
            try:
                return int(s)
            except ValueError:
                return s

    # This retuns a convenient RGB spec for controlling LEDs in form (0.32, 0.56, 1.00)
    def _color_to_rgb(self, color):
        if color in self.w3c_colors:
            color = self.w3c_colors.get(color)
        elif color == '':
            color = "#000000"
        hex_rgb = color.lstrip('#')
        length = len(hex_rgb)
        if length % 3 == 0:
            return tuple(round(float(int(hex_rgb[i:i + length // 3], 16)) / 255, 3) for i in range(0, length, length // 3))
        return (0.,0.,0.)

    # Helper to return validated color string or None if invalid
    def _validate_color(self, color):
        color = color.lower()
        if color == "":
            return ""

        # Try w3c named color
        if color in self.w3c_colors:
            return color

        # Try RGB color
        color = color.lstrip('#')
        x = re.search("^([a-f\d]{6})$", color)
        if x is not None and x.group() == color:
            return color

        return None # Not valid

    # Helper to keep parallel RGB color map updated when color changes
    def _update_gate_color(self, new_color_map):
        self.gate_color = new_color_map

        # Recalculate RGB map for easy LED support
        self.gate_color_rgb = [self._color_to_rgb(i) for i in self.gate_color]

    def _load_persisted_state(self):
        self._log_debug("Loaded persisted MMU state, level: %d" % self.persistence_level)
        errors = []

        if self.persistence_level >= 1:
            # Load EndlessSpool config
            self.enable_endless_spool = self.variables.get(self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool)
            endless_spool_groups = self.variables.get(self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups)
            if len(endless_spool_groups) == self.mmu_num_gates:
                self.endless_spool_groups = endless_spool_groups
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_ENDLESS_SPOOL_GROUPS)

        if self.persistence_level >= 2:
            # Load TTG map
            tool_to_gate_map = self.variables.get(self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map)
            if len(tool_to_gate_map) == self.mmu_num_gates:
                self.ttg_map = tool_to_gate_map
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_TOOL_TO_GATE_MAP)

        if self.persistence_level >= 3:
            # Load gate map
            for var, attr, default in self.gate_map_vars:
                value = self.variables.get(var, getattr(self, attr))
                if len(value) == self.mmu_num_gates:
                    if attr == "gate_color":
                        self._update_gate_color(value)
                    else:
                        setattr(self, attr, value)
                else:
                    errors.append("Incorrect number of gates specified in %s" % var)

        if self.persistence_level >= 4:
            # Load selected tool and gate
            tool_selected = self.variables.get(self.VARS_MMU_TOOL_SELECTED, self.tool_selected)
            gate_selected = self.variables.get(self.VARS_MMU_GATE_SELECTED, self.gate_selected)
            if gate_selected < self.mmu_num_gates and tool_selected < self.mmu_num_gates:
                self._set_tool_selected(tool_selected)
                self._set_gate_selected(gate_selected)

                if self.gate_selected >= 0:
                    if self.tool_selected < 0 or self.ttg_map[self.tool_selected] != self.gate_selected:
                        # Find a tool that maps to gate
                        for tool in range(self.mmu_num_gates):
                            if self.ttg_map[tool] == self.gate_selected:
                                self._set_tool_selected(tool)
                                break
                        else:
                            errors.append("Reset persisted tool - does not map to gate")
                            self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
                    self._set_selector_pos(self.selector_offsets[self.gate_selected])
                elif self.gate_selected == self.TOOL_GATE_BYPASS:
                    self._set_tool_selected(self.TOOL_GATE_BYPASS)
                    self._set_selector_pos(self.bypass_offset)
                else: # TOOL_GATE_UNKNOWN
                    self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
                    self.is_homed = False
            else:
                errors.append("Incorrect number of gates specified in %s or %s" % (self.VARS_MMU_TOOL_SELECTED, self.VARS_MMU_GATE_SELECTED))
            if gate_selected != self.TOOL_GATE_UNKNOWN and tool_selected != self.TOOL_GATE_UNKNOWN:
                self.filament_pos = self.variables.get(self.VARS_MMU_FILAMENT_POS, self.filament_pos)

        if len(errors) > 0:
            self._log_info("Warning: Some persisted state was ignored because it contained errors:\n%s" % ''.join(errors))

        swap_stats = self.variables.get(self.VARS_MMU_SWAP_STATISTICS, {})

        # Auto upgrade old names
        key_map = {"time_spent_loading": "load", "time_spent_unloading": "unload", "time_spent_paused": "pause"}
        swap_stats = {key_map.get(key, key): swap_stats[key] for key in swap_stats}

        self.statistics.update(swap_stats)
        for gate in range(self.mmu_num_gates):
            self.gate_statistics[gate] = self.EMPTY_GATE_STATS_ENTRY.copy()
            gstats = self.variables.get("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, gate), None)
            if gstats:
                self.gate_statistics[gate].update(gstats)

    def _schedule_mmu_bootup_tasks(self, delay=0.):
        waketime = self.reactor.monotonic() + delay
        self.reactor.register_callback(self._mmu_bootup_tasks, waketime)

    def _mmu_bootup_tasks(self, eventtime):
        self._log_trace("_bootup_tasks()")
        self._exec_gcode("__MMU_BOOTUP_TASKS")

    cmd_MMU_BOOTUP_TASKS_help = "Internal commands to complete bootup of MMU"
    def cmd_MMU_BOOTUP_TASKS(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        fversion = lambda f: "v{}.".format(int(f)) + '.'.join("{:0<2}".format(int(str(f).split('.')[1])))
        try:
            self._log_always('(\_/)\n( *,*)\n(")_(") Happy Hare %s Ready' % fversion(self.config_version))
            if self.log_startup_status > 0:
                self._log_always(self._ttg_map_to_string(summary=self.log_startup_status == 1))
                self._display_visual_state(silent=self.persistence_level < 4)
            self._set_print_state("initialized")
            if self._has_encoder():
                self.encoder_sensor.set_clog_detection_length(self.variables.get(self.VARS_MMU_CALIB_CLOG_LENGTH, 15))
                self._disable_runout() # Initially disable clog/runout detection
            self._servo_move()
            self.gate_status = self._validate_gate_status(self.gate_status) # Delay to allow for correct initial state
            self._update_filaments_from_spoolman()
        except Exception as e:
            self._log_error('Warning: Error booting up MMU: %s' % str(e))

    cmd_MMU_TEST_help = "Internal Happy Hare testing"
    def cmd_MMU_TEST(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return

        feedback = gcmd.get_float('SYNC_EVENT', None, minval=-1., maxval=1.)
        if feedback is not None:
            self._log_info("Sending 'mmu:sync_feedback %.2f' event" % feedback)
            self.printer.send_event("mmu:sync_feedback", self.reactor.monotonic(), feedback)

        if gcmd.get_int('DUMP_UNICODE', 0, minval=0, maxval=1):
            self._log_info("UI_SPACE=%s, UI_SEPARATOR=%s, UI_DASH=%s, UI_DEGREE=%s, UI_BOX_BL=%s" % (UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BOX_BL))
            self._log_info("UI_EMOTICONS=%s" % UI_EMOTICONS)

    def _wrap_gcode_command(self, command, exception=False, variables=None):
        try:
            macro = command.split()[0]
            if variables is not None:
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % macro)
                gcode_macro.variables.update(variables)
            self._log_trace("Running macro: %s%s" % (command, " (with override variables)" if variables is not None else ""))
            self.gcode.run_script_from_command(command)
        except Exception as e:
            if exception is not None:
                if exception:
                    raise MmuError("Error running %s: %s" % (macro, str(e)))
                else:
                    self._log_error("Error running %s: %s" % (macro, str(e)))
            else:
                raise

    def _movequeues_wait_moves(self, toolhead=True, mmu_toolhead=True):
        #self._log_trace("_movequeues_wait_moves(toolhead=%s, mmu_toolhead=%s)" % (toolhead, mmu_toolhead))
        if toolhead:
            self.toolhead.wait_moves()
        if mmu_toolhead:
            self.mmu_toolhead.wait_moves()

    def _movequeues_dwell(self, dwell, toolhead=True, mmu_toolhead=True):
        if dwell > 0.:
            if toolhead and mmu_toolhead:
                self._movequeues_sync()
            if toolhead:
                self.toolhead.dwell(dwell)
            if mmu_toolhead:
                self.mmu_toolhead.dwell(dwell)

    def _movequeues_sync(self):
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
        if self.sync_feedback_enable and self.sync_feedback_operational:
            return 'compressed' if self.sync_feedback_last_state > 0.1 else 'expanded' if self.sync_feedback_last_state < -0.1 else 'neutral'
        return "disabled"

    def get_status(self, eventtime):
        return {
                'enabled': self.is_enabled,
                'is_paused': self._is_mmu_paused(),
                'is_locked': self._is_mmu_paused(), # Alias for is_paused
                'is_homed': self.is_homed,
                'tool': self.tool_selected,
                'gate': self.gate_selected,
                'active_gate': self.active_gate,
                'next_tool': self._next_tool,
                'last_tool': self._last_tool,
                'last_toolchange': self._last_toolchange,
                'runout': self.is_handling_runout,
                'filament': "Loaded" if self.filament_pos == self.FILAMENT_POS_LOADED else
                            "Unloaded" if self.filament_pos == self.FILAMENT_POS_UNLOADED else
                            "Unknown",
                'filament_position': self._get_filament_position(),
                'filament_pos': self.filament_pos,
                'filament_direction': self.filament_direction,
                'servo': "Up" if self.servo_state == self.SERVO_UP_STATE else
                         "Down" if self.servo_state == self.SERVO_DOWN_STATE else
                         "Move" if self.servo_state == self.SERVO_MOVE_STATE else
                         "Unknown",
                'ttg_map': list(self.ttg_map),
                'gate_status': list(self.gate_status),
                'gate_material': list(self.gate_material),
                'gate_color': list(self.gate_color),
                'gate_color_rgb': self.gate_color_rgb,
                'gate_spool_id': list(self.gate_spool_id),
                'custom_color_rgb': list(self.custom_color_rgb),
                'endless_spool_groups': list(self.endless_spool_groups),
                'tool_extrusion_multipliers': list(self.tool_extrusion_multipliers),
                'tool_speed_multipliers': list(self.tool_speed_multipliers),
                'slicer_tool_map': self.slicer_tool_map,
                'action': self._get_action_string(),
                'has_bypass': self.bypass_offset > 0.,
                'sync_drive': self.mmu_toolhead.is_synced(),
                'sync_feedback_state': self._get_sync_feedback_string(),
                'print_state': self.print_state,
                'clog_detection': self.enable_clog_detection,
                'endless_spool': self.enable_endless_spool,
                'print_start_detection': self.print_start_detection, # For Klippain. Not really sure it is necessary
                'reason_for_pause': self.reason_for_pause if self._is_mmu_paused() else "",
        }

    def _reset_statistics(self):
        self.statistics = {}
        self.last_statistics = {}
        self.track = {}
        self.gate_statistics = []
        for gate in range(self.mmu_num_gates):
            self.gate_statistics.append(self.EMPTY_GATE_STATS_ENTRY.copy())
        self._reset_job_statistics()

    def _reset_job_statistics(self):
        self.job_statistics = {}
        self.tracked_start_time = 0
        self.pause_start_time = 0

    def _track_time_start(self, name):
        self.track[name] = time.time()
        self._log_trace("track times: " + str(self.track))

    def _track_time_end(self, name):
        if name not in self.track:
            return #timer not initialized
        self.statistics.setdefault(name, 0)
        self.job_statistics.setdefault(name, 0)
        self._log_trace("statistics: " + str(self.statistics))

        elapsed = time.time() - self.track[name]
        self.statistics[name] += elapsed
        self.job_statistics[name] += elapsed
        self.last_statistics[name] = elapsed

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
        self.pause_start_time = time.time()
        self._track_gate_statistics('pauses', self.gate_selected)

    def _track_pause_end(self):
        self._track_time_end('pause')

    # Per gate tracking
    def _track_gate_statistics(self, key, gate, count=1):
        try:
            if gate >= self.TOOL_GATE_UNKNOWN:
                if isinstance(count, float):
                    self.gate_statistics[gate][key] = round(self.gate_statistics[gate][key] + count, 3)
                else:
                    self.gate_statistics[gate][key] += count
            else:
                self._log_debug("Unknown gate provided to record gate stats")
        except Exception as e:
            self._log_debug("Exception whilst tracking gate stats: %s" % str(e))

    def _seconds_to_short_string(self, seconds):
        if isinstance(seconds, float) or isinstance(seconds, int) or seconds.isnumeric():
            seconds = int(seconds)
            if seconds >= 3600:
                return "{hour}:{min:0>2}:{sec:0>2}".format(hour=seconds // 3600, min=(seconds // 60) % 60, sec=seconds % 60)
            if seconds >= 60:
                return "{min}:{sec:0>2}".format(min=(seconds // 60) % 60, sec=seconds % 60)
            return "0:{sec:0>2}".format(sec=seconds % 60)
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
        # | all time  | 0:07 | 47:19 | 0:00 | 0:01 | 37:11 | 33:39 |  2:00:38 |
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
        total = self.console_always_output_full or total or not self._is_in_print()

        table_column_order = ['pre_unload', 'unload', 'post_unload', 'pre_load', 'load', 'post_load', 'total']
        table_include_columns = self._list_intersection(table_column_order, self.console_stat_columns) # To maintain the correct order and filter incorrect ones

        table_row_options = ['total', 'total_average', 'job', 'job_average', 'last']
        table_include_rows = self._list_intersection(self.console_stat_rows, table_row_options) # Keep the user provided order

        # Remove totals from table if not in print and not forcing total
        if not self.console_always_output_full and not total:
            if 'total'         in table_include_rows: table_include_rows.remove('total')
            if 'total_average' in table_include_rows: table_include_rows.remove('total_average')
        if not self._is_in_print():
            if 'job'           in table_include_rows: table_include_rows.remove('job')
            if 'job_average'   in table_include_rows: table_include_rows.remove('job_average')

        if len(table_include_rows) > 0:
            # Map the row names (as described in macro_vars) to the proper values. stats is mandatory
            table_rows_map = {
                'total':         {'stats': lifetime, 'name': 'total '},
                'total_average': {'stats': lifetime, 'name': UI_BOX_BL + ' avg', 'devide': lifetime.get('total_swaps', 1)}, 
                'job':           {'stats': job,      'name': 'this job '},
                'job_average':   {'stats': job,      'name': UI_BOX_BL + ' avg', 'devide': job.get('total_swaps', 1)},
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
            table_extra_headers = [key for key in table_extra_headers_map if len(self._list_intersection(table_extra_headers_map[key], table_include_columns)) > 0]
            # Dictionary keys have no predefined order, so re-order them (Lucky the columns are alphabetical)
            table_extra_headers.sort(reverse=True)
            # Include the number of swaps in the top-left corner of the table
            if self._is_in_print():
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
            for i in range(len(column_extra_header_widths)):
                w = column_extra_header_widths[i]

                start = sum(max(1, len(self._list_intersection(table_extra_headers_map.get(table_extra_header, ['']), table_include_columns))) for table_extra_header in table_extra_headers[0:i])
                end = start + max(1, len(self._list_intersection(table_extra_headers_map.get(table_extra_headers[i], ['']), table_include_columns)))
                while (sum(column_widths[start:end]) + (end - start - 1)) < w:
                    for c in range(start, end):
                        column_widths[c] += 1
                column_extra_header_widths[i] = sum(column_widths[start:end]) + (end - start - 1)

            # Build the table header
            msg += "+" +   "+".join([UI_DASH * width for width in column_extra_header_widths])                                                                 + "+\n"
            msg += "|" +   "|".join([table_extra_headers[i].center(column_extra_header_widths[i], UI_SEPARATOR) for i in range(len(column_extra_header_widths))])  + "|\n"
            msg += "|" +   "|".join([table_headers[i].center(column_widths[i], UI_SEPARATOR) for i in range(len(column_widths))])                                  + "|\n"
            msg += "+" +   "+".join([UI_DASH * (width) for width in column_widths])                                                                            + "+\n"

            # Build the table body
            for row in table:
                msg += "|" +   "|".join([row[i].rjust(column_widths[i] - 1, UI_SEPARATOR) + UI_SEPARATOR for i in range(len(column_widths))]) + "|\n"

            # Table footer
            msg += "+" + "+".join([UI_DASH * width for width in column_widths]) + "+\n"

        # Pause data
        if total:
            msg += "\n%s spent paused over %d pauses (All time)" % (self._seconds_to_short_string(lifetime.get('pause', 0)), lifetime.get('total_pauses', 0))
        if self._is_in_print():
            msg += "\n%s spent paused over %d pauses (This job)" % (self._seconds_to_short_string(job.get('pause', 0)), job.get('total_pauses', 0))
        msg += "\nNumber of swaps since last incident: %d (Record: %d)" % (lifetime.get('swaps_since_pause', 0), lifetime.get('swaps_since_pause_record', 0))

        return msg

    def _list_intersection(self, list1, list2):
        result = []
        for item in list1:
            if item in list2:
                result.append(item)
        return result

    def _dump_statistics(self, force_log=False, total=False, job=False, gate=False, detail=False):
        if self.log_statistics or force_log:
            msg = ""
            if job or total:
                msg += self._swap_statistics_to_string(total=total)
            if self._can_use_encoder() and gate:
                m,d = self._gate_statistics_to_string()
                msg += "\n\n" if msg != "" else ""
                msg += m
                if detail:
                    msg += "\n" if msg != "" else ""
                    msg += d
            self._log_always(msg)
    
        # This is good place to update the persisted stats...
        self._persist_swap_statistics()
        self._persist_gate_statistics()

    def _gate_statistics_to_string(self):
        msg = "Gate Statistics:\n"
        dbg = ""
        t = self.console_gate_stat
        for gate in range(self.mmu_num_gates):
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
            msg += "#%d: %s" % (gate, status)
            msg += ", " if gate < (self.mmu_num_gates - 1) else ""
            dbg += "\nGate %d: " % gate
            dbg += "Load: (monitored: %.1fmm slippage: %.1f%%)" % (rounded['load_distance'], load_slip_percent)
            dbg += "; Unload: (monitored: %.1fmm slippage: %.1f%%)" % (rounded['unload_distance'], unload_slip_percent)
            dbg += "; Failures: (servo: %d load: %d unload: %d pauses: %d)" % (rounded['servo_retries'], rounded['load_failures'], rounded['unload_failures'], rounded['pauses'])
            dbg += "; Quality: %.1f%%" % ((rounded['quality'] * 100.) if rounded['quality'] >= 0. else 0.)
        return msg, dbg

    def _persist_gate_statistics(self):
        for gate in range(self.mmu_num_gates):
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=\"%s\"" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, gate, self.gate_statistics[gate]))
        # Good place to persist current clog length
        if self._has_encoder():
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_CLOG_LENGTH, self.encoder_sensor.get_clog_detection_length()))

    def _persist_swap_statistics(self):
        for key in self.statistics:
            if isinstance(self.statistics[key], float):
                self.statistics[key] = round(self.statistics[key], 2)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SWAP_STATISTICS, self.statistics))

    def _persist_gate_map(self):
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_STATUS, self.gate_status))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_GATE_MATERIAL, list(map(lambda x: ('%s' %x), self.gate_material))))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_GATE_COLOR, list(map(lambda x: ('%s' %x), self.gate_color))))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_SPEED_OVERRIDE, self.gate_speed_override))
        if self.printer.lookup_object("gcode_macro %s" % self.gate_map_changed_macro, None) is not None:
            self._wrap_gcode_command("%s GATE=-1" % self.gate_map_changed_macro)

    def _log_to_file(self, message):
        message = "> %s" % message
        if self.mmu_logger:
            self.mmu_logger.info(message)

    def _log_error(self, message):
        if self.mmu_logger:
            self.mmu_logger.info(message)
        self.gcode.respond_raw("!! %s" % message)

    def _log_always(self, message):
        if self.mmu_logger:
            self.mmu_logger.info(message)
        self.gcode.respond_info(message)

    def _log_info(self, message):
        if self.mmu_logger and self.log_file_level > 0:
            self.mmu_logger.info(message)
        if self.log_level > 0:
            self.gcode.respond_info(message)

    def _log_debug(self, message):
        message = "%s DEBUG: %s" % (UI_SEPARATOR, message)
        if self.mmu_logger and self.log_file_level > 1:
            self.mmu_logger.info(message)
        if self.log_level > 1:
            self.gcode.respond_info(message)

    def _log_trace(self, message):
        message = "%s %s TRACE: %s" % (UI_SEPARATOR, UI_SEPARATOR, message)
        if self.mmu_logger and self.log_file_level > 2:
            self.mmu_logger.info(message)
        if self.log_level > 2:
            self.gcode.respond_info(message)

    def _log_stepper(self, message):
        message = "%s %s %s STEPPER: %s" % (UI_SEPARATOR, UI_SEPARATOR, UI_SEPARATOR, message)
        if self.mmu_logger and self.log_file_level > 3:
            self.mmu_logger.info(message)
        if self.log_level > 3:
            self.gcode.respond_info(message)

    # Fun visual display of MMU state
    def _display_visual_state(self, direction=None, silent=False):
        if direction is not None:
            self.filament_direction = direction
        if not silent and self.log_visual and not self.calibrating:
            visual_str = self._state_to_string()
            self._log_always(visual_str)

    def _state_to_string(self, direction=None):
        arrow = "<" if self.filament_direction == self.DIRECTION_UNLOAD else ">"
        space = "."
        home  = "|"
        gs = "(g)"
        es = "(e)"
        ts = "(t)"
        past  = lambda pos: arrow if self.filament_pos >= pos else space
        homed = lambda pos, sensor: (' ',arrow,sensor) if self.filament_pos > pos else (home,space,sensor) if self.filament_pos == pos else (' ',space,sensor)
        trig  = lambda name, sensor: re.sub(r'[a-zA-Z]', '*', name) if self._check_sensor(sensor) else name

        t_str   = ("[T%s] " % str(self.tool_selected)) if self.tool_selected >= 0 else "BYPASS " if self.tool_selected == self.TOOL_GATE_BYPASS else "[T?] "
        g_str   = "{}".format(past(self.FILAMENT_POS_UNLOADED))
        gs_str  = "{0}{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_GATE, trig(gs, self.ENDSTOP_GATE))) if self._has_sensor(self.ENDSTOP_GATE) else ""
        en_str  = " En {0}".format(past(self.FILAMENT_POS_IN_BOWDEN if self.gate_homing_endstop == self.ENDSTOP_GATE else self.FILAMENT_POS_START_BOWDEN)) if self._has_encoder() else ""
        bowden1 = "{0}{0}{0}{0}".format(past(self.FILAMENT_POS_IN_BOWDEN))
        bowden2 = "{0}{0}{0}{0}".format(past(self.FILAMENT_POS_END_BOWDEN))
        es_str  = "{0}{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_ENTRY, trig(es, self.ENDSTOP_EXTRUDER))) if self._has_sensor(self.ENDSTOP_EXTRUDER) else ""
        ex_str  = "{0}[{2} {1}{1}".format(*homed(self.FILAMENT_POS_HOMED_EXTRUDER, "Ex"))
        ts_str  = "{0}{2} {1}".format(*homed(self.FILAMENT_POS_HOMED_TS, trig(ts, self.ENDSTOP_TOOLHEAD))) if self._has_sensor(self.ENDSTOP_TOOLHEAD) else ""
        nz_str  = "{} Nz]".format(past(self.FILAMENT_POS_LOADED))
        summary = " LOADED" if self.filament_pos == self.FILAMENT_POS_LOADED else " UNLOADED" if self.filament_pos == self.FILAMENT_POS_UNLOADED else " UNKNOWN" if self.filament_pos == self.FILAMENT_POS_UNKNOWN else ""
        counter = " %.1fmm%s" % (self.mmu_toolhead.get_position()[1], " (e:%.1fmm)" % self._get_encoder_distance(dwell=None) if self._has_encoder() and self.encoder_move_validation else "")

        visual = "".join((t_str, g_str, gs_str, en_str, bowden1, bowden2, es_str, ex_str, ts_str, nz_str, summary, counter))
        return visual

    def _log_level_to_string(self, level):
        log = "OFF"
        if level > 3: log = "STEPPER"
        elif level > 2: log = "TRACE"
        elif level > 1: log = "DEBUG"
        elif level > 0: log = "INFO"
        elif level > -1: log = "ESSENTIAL MESSAGES"
        return log

### LOGGING AND STATISTICS FUNCTIONS GCODE FUNCTIONS

    cmd_MMU_STATS_help = "Dump and optionally reset the MMU statistics"
    def cmd_MMU_STATS(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        total = gcmd.get_int('TOTAL', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)
        if reset:
            self._reset_statistics()
            self._persist_swap_statistics()
            self._persist_gate_statistics()
            self._dump_statistics(force_log=True, total=True)
        else:
            self._dump_statistics(force_log=True, total=total or detail, job=True, gate=True, detail=detail)

    cmd_MMU_STATUS_help = "Complete dump of current MMU state and important configuration"
    def cmd_MMU_STATUS(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)
        on_off = lambda x: "ON" if x else "OFF"

        fversion = lambda f: "v{}.".format(int(f)) + '.'.join("{:0<2}".format(int(str(f).split('.')[1])))
        msg = "MMU: Happy Hare %s running %s v%s" % (fversion(self.config_version), self.mmu_vendor, self.mmu_version_string)
        msg += " with %d gates" % (self.mmu_num_gates)
        msg += " (%s)" % ("DISABLED" if not self.is_enabled else "PAUSED" if self._is_mmu_paused() else "OPERATIONAL")
        msg += "\nServo in %s position" % ("UP" if self.servo_state == self.SERVO_UP_STATE else \
                "DOWN" if self.servo_state == self.SERVO_DOWN_STATE else "MOVE" if self.servo_state == self.SERVO_MOVE_STATE else "unknown")
        if self._has_encoder():
            msg += ", Encoder reads %.1fmm" % self._get_encoder_distance()
        msg += "\nPrint state is %s" % self.print_state.upper()
        msg += ". Selector is %s" % ("HOMED" if self.is_homed else "NOT HOMED")
        msg += ". Tool %s selected " % self._selected_tool_string()
        msg += " on Gate %s" % self._selected_gate_string()
        msg += ". Toolhead position saved" if self.saved_toolhead_position else ""
        msg += "\nGear stepper is at %d%% and is %s to extruder" % (self.gear_percentage_run_current, "SYNCED" if self.mmu_toolhead.is_gear_synced_to_extruder() else "not synced")
        if self.mmu_toolhead.is_gear_synced_to_extruder():
            msg += "\nSync feedback indicates filament in bowden is: %s" % self._get_sync_feedback_string().upper()

        if config:
            msg += "\n\nLoad Sequence:"
            msg += "\n- Filament loads into gate by homing a maximum of %.1fmm ('gate_homing_max') to %s" % (self.gate_homing_max, self._gate_homing_string())
            msg += "\n- Bowden is loaded with a fast%s %.1fmm ('calibration_bowden_length') move" % (" CORRECTED" if self.bowden_apply_correction else "", self.calibrated_bowden_length)
            if self._must_home_to_extruder():
                if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION:
                    msg += ", then homes to extruder using COLLISION detection (at %d%% current)" % self.extruder_homing_current
                else:
                    if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_NONE:
                        msg += ", no extruder homing is performed!"
                    else:
                        msg += ", then homes to extruder using ENDSTOP '%s'" % self.extruder_homing_endstop
                    if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER:
                        msg += " and then moves %.1fmm ('toolhead_entry_to_entruder') to extruder extrance" % self.toolhead_entry_to_extruder
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder (synced) loads by homing a maximum of %.1fmm ('toolhead_homing_max') to TOOLHEAD SENSOR before moving the last %.1fmm ('toolhead_sensor_to_nozzle - toolhead_ooze_reduction') to the nozzle" % (self.toolhead_homing_max, self.toolhead_sensor_to_nozzle - self.toolhead_ooze_reduction)
            else:
                msg += "\n- Extruder (synced) loads by moving %.1fmm ('toolhead_extruder_to_nozzle - toolhead_ooze_reduction') to the nozzle" % (self.toolhead_extruder_to_nozzle - self.toolhead_ooze_reduction)

            msg += "\n\nUnload Sequence:"
            msg += "\n- Tip is %s formed by %s" % (("sometimes", "SLICER") if not self.force_form_tip_standalone else ("always", ("'%s' macro" % self.form_tip_macro)))
            msg += " and tip forming extruder current is %d%%" % self.extruder_form_tip_current

            if self._has_sensor(self.ENDSTOP_EXTRUDER):
                msg += "\n- Extruder (synced) unloads by reverse homing a maximum of %.1fmm ('toolhead_entry_to_extruder + toolhead_extruder_to_nozzle + toolhead_unload_safety_margin') to EXTRUDER SENSOR" % (self.toolhead_entry_to_extruder + self.toolhead_extruder_to_nozzle + self.toolhead_unload_safety_margin)
            elif self._has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder (optionally synced) unloads by reverse homing a maximum %.1fmm ('toolhead_sensor_to_nozzle + toolhead_unload_safety_margin') to TOOLHEAD SENSOR" % (self.toolhead_sensor_to_nozzle + self.toolhead_unload_safety_margin)
                msg += ", then unloads by moving %.1fmm ('toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle + toolhead_unload_safety_margin') to exit extruder" % (self.toolhead_extruder_to_nozzle - self.toolhead_sensor_to_nozzle + self.toolhead_unload_safety_margin)
            else:
                msg += "\n- Extruder (optionally synced) unloads by moving %.1fmm ('toolhead_extruder_to_nozzle + toolhead_unload_safety_margin') less reported park position to exit extruder" % (self.toolhead_extruder_to_nozzle + self.toolhead_unload_safety_margin)

            if self._has_encoder() and self.bowden_pre_unload_test and not self._has_sensor(self.ENDSTOP_EXTRUDER):
                msg += "\n- Bowden is unloaded with a short %.1fmm ('encoder_move_step_size') validation move before %.1fmm ('calibration_bowden_length - gate_unload_buffer - encoder_move_step_size') fast move" % (self.encoder_move_step_size, self.calibrated_bowden_length - self.gate_unload_buffer - self.encoder_move_step_size)
            else:
                msg += "\n- Bowden is unloaded with a fast %.1fmm ('calibration_bowden_length - gate_unload_buffer') move" % (self.calibrated_bowden_length - self.gate_unload_buffer)
            msg += "\n- Filament is stored by homing a maximum of %.1fmm ('gate_homing_max') to %s and parking %.1fmm ('gate_parking_distance') in the gate" % (self.gate_homing_max, self._gate_homing_string(), self.gate_parking_distance)

            if self.sync_form_tip or self.sync_to_extruder:
                msg += "\nGear and Extruder steppers are synchronized during: "
                msg += ("Print (at %d%% current)" % self.sync_gear_current) if self.sync_to_extruder else ""
                msg += " and tip forming" if self.sync_form_tip else ""

            msg += "\n\nSelector touch (stallguard) is %s - blocked gate recovery %s possible" % (("ENABLED", "is") if self.selector_touch else ("DISABLED", "is not"))
            p = self.persistence_level
            msg += "\nPersistence: %s state is persisted across restarts" % ("All" if p == 4 else "Gate status, TTG map & EndlessSpool groups" if p == 3 else "TTG map & EndlessSpool groups" if p == 2 else "EndlessSpool groups" if p == 1 else "No")
            if self._has_encoder():
                msg += "\nMMU has an encoder. Non essential move validation is %s" % ("ENABLED" if self._can_use_encoder() else "DISABLED")
                msg += "\nRunout/Clog detection is %s" % ("AUTOMATIC" if self.enable_clog_detection == self.encoder_sensor.RUNOUT_AUTOMATIC else "ENABLED" if self.enable_clog_detection == self.encoder_sensor.RUNOUT_STATIC else "DISABLED")
                msg += " (%.1fmm runout)" % self.encoder_sensor.get_clog_detection_length()
                msg += ", EndlessSpool is %s" % ("ENABLED" if self.enable_endless_spool else "DISABLED")
            else:
                msg += "\nMMU does not have an encoder - move validation or clog detection / endless spool is not possible"
            msg += "\nSpoolMan is %s. " % ("ENABLED" if self.enable_spoolman else "DISABLED")
            msg += "Sensors: "
            sensors = self._check_all_sensors()
            for name, state in sensors.items():
                msg += "%s (%s), " % (name.upper(), "Disabled" if state is None else ("Detected" if state == True else "Empty"))
            msg += "\nLogging: Console %d(%s)" % (self.log_level, self._log_level_to_string(self.log_level))

            msg += ", Logfile %d(%s)" % (self.log_file_level, self._log_level_to_string(self.log_file_level))
            msg += ", Visual %d(%s)" % (self.log_visual, on_off(self.log_visual))
            msg += ", Statistics %d(%s)" % (self.log_statistics, on_off(self.log_statistics))

        if not detail:
            msg += "\n\nFor details on TTG and EndlessSpool groups add 'DETAIL=1'"
            if not config:
                msg += ", for configuration add 'SHOWCONFIG=1'"

        msg += "\n\n%s" % self._ttg_map_to_string(summary=True)
        msg += "\n\n%s" % self._state_to_string()

        if detail:
            msg += "\n\n%s" % self._ttg_map_to_string(title="TTG Map & EndlessSpool Groups")
            msg += "\n\n%s" % self._gate_map_to_string()

        self._log_always(msg)

    cmd_MMU_SENSORS_help = "Query state of sensors fitted to mmu"
    def cmd_MMU_SENSORS(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        eventtime = self.reactor.monotonic()
        if self.mmu_sensors:

            # Sync feedback sensors
            trg_string = lambda s : 'TRIGGERED' if s == 1 else 'open' if s == 0 else 'not available'
            for sensor in [self.SWITCH_SYNC_FEEDBACK_TENSION, self.SWITCH_SYNC_FEEDBACK_COMPRESSION]:
                state = self.mmu_sensors.get_status(eventtime)[sensor]
                if state != -1:
                    self._log_always("%s: %s" % (sensor, trg_string(state)))

            # Endstop sensors
            sensors = self._check_all_sensors()
            for name, state in sensors.items():
                if state is not None:
                    self._log_always("%s: %s" % (name, trg_string(state)))

            # Pre-gate sensors
            for gate in range(self.mmu_num_gates):
                name, state = "%s_%d" % (self.PRE_GATE_SENSOR_PREFIX, gate), self._check_pre_gate_sensor(gate)
                if state is not None:
                    self._log_always("%s: %s" % (name, trg_string(state)))
        else:
            self._log_always("No MMU sensors configured")


#############################
# SERVO AND MOTOR FUNCTIONS #
#############################

    def _servo_reset_state(self):
        self.servo_state = self.SERVO_UNKNOWN_STATE
        self.servo_angle = self.SERVO_UNKNOWN_STATE

    def _servo_set_angle(self, angle):
        self.servo.set_value(angle=angle, duration=self.servo_duration)
        self.servo_angle = angle
        self.servo_state = self.SERVO_UNKNOWN_STATE

    def _servo_save_pos(self, pos):
        if self.servo_angle != self.SERVO_UNKNOWN_STATE:
            self.servo_angles[pos] = self.servo_angle
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SERVO_ANGLES, self.servo_angles))
            self._log_info("Servo angle '%d' for position '%s' has been saved" % (self.servo_angle, pos))
        else:
            self._log_info("Servo angle unknown")

    def _servo_down(self, buzz_gear=True):
        if self.gate_selected == self.TOOL_GATE_BYPASS: return
        if self.servo_state == self.SERVO_DOWN_STATE: return
        self._log_debug("Setting servo to down (filament drive) position at angle: %d" % self.servo_angles['down'])
        self._movequeues_wait_moves()
        self.servo.set_value(angle=self.servo_angles['down'], duration=None if self.servo_active_down else self.servo_duration)
        if self.servo_angle != self.servo_angles['down'] and buzz_gear and self.servo_buzz_gear_on_down > 0:
            self.gear_buzz_accel = 1000
            for i in range(self.servo_buzz_gear_on_down):
                self._trace_filament_move(None, 0.8, speed=25, accel=self.gear_buzz_accel, encoder_dwell=None)
                self._trace_filament_move(None, -0.8, speed=25, accel=self.gear_buzz_accel, encoder_dwell=None)
            self._movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
        self.servo_angle = self.servo_angles['down']
        self.servo_state = self.SERVO_DOWN_STATE

    def _servo_move(self): # Position servo for selector movement
        if self.servo_state == self.SERVO_MOVE_STATE: return
        self._log_debug("Setting servo to move (filament hold) position at angle: %d" % self.servo_angles['move'])
        if self.servo_angle != self.servo_angles['move']:
            self._movequeues_wait_moves()
            self.servo.set_value(angle=self.servo_angles['move'], duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            self.servo_angle = self.servo_angles['move']
            self.servo_state = self.SERVO_MOVE_STATE

    def _servo_up(self, measure=False):
        if self.servo_state == self.SERVO_UP_STATE: return 0.
        self._log_debug("Setting servo to up (filament released) position at angle: %d" % self.servo_angles['up'])
        delta = 0.
        if self.servo_angle != self.servo_angles['up']:
            self._movequeues_wait_moves()
            if measure:
                initial_encoder_position = self._get_encoder_distance(dwell=None)
            self.servo.set_value(angle=self.servo_angles['up'], duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_dwell, self.servo_duration, 0))
            if measure:
                # Report on spring back in filament then revert counter
                delta = self._get_encoder_distance() - initial_encoder_position
                if delta > 0.:
                    self._log_debug("Spring in filament measured  %.1fmm - adjusting encoder" % delta)
                    self._set_encoder_distance(initial_encoder_position, dwell=None)
        self.servo_angle = self.servo_angles['up']
        self.servo_state = self.SERVO_UP_STATE
        return delta

    def _servo_auto(self):
        if not self.is_homed or self.tool_selected < 0 or self.gate_selected < 0:
            self._servo_move()
        else:
            self._servo_up()

    def _motors_off(self, motor="all"):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        if motor in ["all", "gear"]:
            self._sync_gear_to_extruder(False)
            ge = stepper_enable.lookup_enable(self.gear_stepper.get_name())
            ge.motor_disable(self.mmu_toolhead.get_last_move_time())
        if motor in ["all", "selector"]:
            self._servo_move()
            self.is_homed = False
            self._set_gate_selected(self.TOOL_GATE_UNKNOWN)
            self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
            se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
            se.motor_disable(self.mmu_toolhead.get_last_move_time())

### SERVO AND MOTOR GCODE FUNCTIONS

    cmd_MMU_SERVO_help = "Move MMU servo to position specified position or angle"
    def cmd_MMU_SERVO(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        save = gcmd.get_int('SAVE', 0)
        pos = gcmd.get('POS', "").lower()
        if pos == "up":
            if save:
                self._servo_save_pos(pos)
            else:
                self._servo_up()
        elif pos == "move":
            if save:
                self._servo_save_pos(pos)
            else:
                self._servo_move()
        elif pos == "down":
            if self._check_in_bypass(): return
            if save:
                self._servo_save_pos(pos)
            else:
                self._servo_down()
        elif save:
            self._log_error("Servo position not specified for save")
        elif pos == "":
            if self._check_in_bypass(): return
            angle = gcmd.get_int('ANGLE', None)
            if angle is not None:
                self._log_debug("Setting servo to angle: %d" % angle)
                self._servo_set_angle(angle)
            else:
                self._log_always("Current servo angle: %d, Positions: %s" % (self.servo_angle, self.servo_angles))
                self._log_info("Use POS= or ANGLE= to move position")
        else:
            self._log_error("Unknown servo position `%s`" % pos)

    cmd_MMU_MOTORS_OFF_help = "Turn off both MMU motors"
    def cmd_MMU_MOTORS_OFF(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        self._motors_off()
        self._servo_move()
        self._servo_reset_state()

    cmd_MMU_TEST_BUZZ_MOTOR_help = "Simple buzz the selected motor (default gear) for setup testing"
    def cmd_MMU_TEST_BUZZ_MOTOR(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        motor = gcmd.get('MOTOR', "gear")
        if motor == "gear":
            found = self._buzz_gear_motor()
            self._log_info("Filament %s by gear motor buzz" % ("detected" if found else "not detected"))
        elif motor == "selector":
            pos = self.mmu_toolhead.get_position()[0]
            self._trace_selector_move(None, pos + 5, wait=False)
            self._trace_selector_move(None, pos - 5, wait=False)
        elif motor == "servo":
            self._movequeues_wait_moves()
            old_state = self.servo_state
            small=min(self.servo_angles['down'], self.servo_angles['up'])
            large=max(self.servo_angles['down'], self.servo_angles['up'])
            mid=(self.servo_angles['down'] + self.servo_angles['up'])/2
            self.servo.set_value(angle=mid, duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
            self.servo.set_value(angle=abs(mid+small)/2, duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
            self.servo.set_value(angle=abs(mid+large)/2, duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_duration, 0.5), mmu_toolhead=False)
            self._movequeues_wait_moves()
            if old_state == self.SERVO_DOWN_STATE:
                self._servo_down(buzz_gear=False)
            elif old_state == self.SERVO_MOVE_STATE:
                self._servo_move()
            else:
                self._servo_up()
        else:
            raise gcmd.error("Motor '%s' not known" % motor)

    cmd_MMU_SYNC_GEAR_MOTOR_help = "Sync the MMU gear motor to the extruder stepper"
    def cmd_MMU_SYNC_GEAR_MOTOR(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        servo = gcmd.get_int('SERVO', 1, minval=0, maxval=1)
        sync = gcmd.get_int('SYNC', 1, minval=0, maxval=1)
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print current
        self._sync_gear_to_extruder(sync, servo=servo, current=self._is_in_print(force_in_print))


#########################
# CALIBRATION FUNCTIONS #
#########################

    def _set_calibrated_bowden_length(self, reference):
        self.variables[self.VARS_MMU_CALIB_BOWDEN_LENGTH] = reference
        self.calibrated_bowden_length = reference
        self.calibration_status |= self.CALIBRATED_BOWDEN

    def _calibrate_encoder(self, length, repeats, speed, min_speed, max_speed, accel, save=True):
        try:
            pos_values, neg_values = [], []
            self._log_always("Testing over %.1fmm" % length)
            speed_incr = (max_speed - min_speed) / repeats
            test_speed = min_speed
            for x in range(repeats):
                if speed_incr > 0.:
                    self._log_always("Test run #%d, Speed=%.1f mm/s" % (x, test_speed))

                # Move forward
                self._initialize_filament_position(dwell=True)    # Encoder 0000
                self._trace_filament_move(None, length, speed=test_speed, accel=accel, wait=True)
                counts = self._get_encoder_counts(dwell=True)
                pos_values.append(counts)
                self._log_always("+ counts =  %d" % counts)

                # Move backward
                self._initialize_filament_position(dwell=True)    # Encoder 0000
                self._trace_filament_move(None, -length, speed=test_speed, accel=accel, wait=True)
                counts = self._get_encoder_counts(dwell=True)
                neg_values.append(counts)
                self._log_always("- counts =  %d" % counts)

                if counts == 0: break
                test_speed += speed_incr

            self._log_always("Load direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self._sample_stats(pos_values))
            self._log_always("Unload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self._sample_stats(neg_values))

            mean_pos = self._sample_stats(pos_values)['mean']
            mean_neg = self._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2

            if mean == 0:
                self._log_always("No counts measured. Ensure a tool was selected with servo down before running calibration and that your encoder is working properly")
                return

            resolution = length / mean
            old_result = mean * self.encoder_sensor.get_resolution()
            new_result = mean * resolution

            # Sanity check to ensure all teeth are reflecting / being counted. 20% tolerance
            if (abs(resolution - self.encoder_default_resolution) / self.encoder_default_resolution) > 0.2:
                self._log_always("Warning: Encoder is not detecting the expected number of counts. It is possible that reflections from some teeth are unreliable")

            msg = "Before calibration measured length = %.2fmm" % old_result
            msg += "\nResulting resolution of the encoder = %.6fmm" % resolution
            msg += "\nAfter calibration measured length = %.2fmm" % new_result
            self._log_always(msg)

            if save:
                self.encoder_sensor.set_resolution(resolution)
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.6f" % (self.VARS_MMU_ENCODER_RESOLUTION, resolution))
                self._log_always("Encoder calibration has been saved")
                self.calibration_status |= self.CALIBRATED_ENCODER

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of encoder failed. Aborting, because:\n%s" % str(ee))
        finally:
            if mean == 0:
                self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

    # Calibrated bowden length is always from chosen gate homing point to the entruder gears
    # It can be adjusted if sensor setup changes post calibration, must consider:
    #   gate_endstop_to_encoder    .. potential dead space from gate sensor to encoder
    #   toolhead_entry_to_extruder .. distance for extruder entry sensor to extruder gears
    def _calibrate_bowden_length_auto(self, approximate_length, extruder_homing_max, repeats, save=True):
        try:
            self._log_always("Calibrating bowden length on reference Gate 0 using %s as gate reference point" % self._gate_homing_string())
            self._select_tool(0)
            self._set_gate_ratio(1.)
            reference_sum = spring_max = 0.
            successes = 0

            # Can't allow "none" endstop during calibration
            endstop = self.extruder_homing_endstop
            self.extruder_homing_endstop = self.ENDSTOP_EXTRUDER_COLLISION if endstop == self.ENDSTOP_EXTRUDER_NONE else self.extruder_homing_endstop
            for i in range(repeats):
                self._initialize_filament_position(dwell=True) # Encoder 0000
                self._load_gate(allow_retry=False)
                self._load_bowden(approximate_length)
                self._log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                self._home_to_extruder(extruder_homing_max)
                actual = self._get_filament_position()
                measured = self._get_encoder_distance(dwell=True) + self._get_encoder_dead_space()
                spring = self._servo_up(measure=True) if self._has_encoder() else 0.
                reference = actual - spring

                # When homing using collision, we expect the filament to spring back.
                if not (endstop == self.ENDSTOP_EXTRUDER_COLLISION and spring == 0.):
                    msg = "Pass #%d: Filament homed to extruder after %.1fmm movement" % (i+1, actual)
                    if self._has_encoder():
                        msg += "\n(encoder measured %.1fmm, filament sprung back %.1fmm)" % (measured, spring)
                    msg += "\n- Bowden calibration based on this pass is %.1f" % reference
                    self._log_always(msg)
                    reference_sum += reference
                    spring_max = max(spring, spring_max)
                    successes += 1
                else:
                    # No spring means we haven't reliably homed
                    self._log_always("Failed to detect a reliable home position on this attempt")

                self._initialize_filament_position(True) # Encoder 0000
                self._unload_bowden(reference)
                self._unload_gate()
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)

            if successes > 0:
                average_reference = reference_sum / successes
                detection_length = (average_reference * 2.) / 100. + spring_max # 2% of bowden length plus spring seems to be good starting point
                msg = "Recommended calibration bowden length is %.1fmm" % average_reference
                if self._has_encoder() and self.enable_clog_detection:
                    msg += ". Clog detection length: %.1fmm" % detection_length
                self._log_always(msg)

                if save:
                    self._set_calibrated_bowden_length(average_reference)
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (self.VARS_MMU_CALIB_BOWDEN_HOME, self.gate_homing_endstop))
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_BOWDEN_LENGTH, average_reference))
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=1.0" % (self.VARS_MMU_CALIB_PREFIX, 0))
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_CLOG_LENGTH, detection_length))
                    if self._has_encoder():
                        self.encoder_sensor.set_clog_detection_length(detection_length)
                        self._log_always("Bowden calibration and clog detection length have been saved")
                    else:
                        self._log_always("Bowden calibration length has been saved")
            else:
                self._log_error("All %d attempts at homing failed. MMU needs some adjustments!" % repeats)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of bowden length (on Gate 0) failed. Aborting, because:\n%s" % str(ee))
        finally:
            self.extruder_homing_endstop = endstop
            self._servo_auto()

    def _calibrate_bowden_length_manual(self, approx_bowden_length, save=True):
        if self.gate_selected != 0 and not self.virtual_selector:
            raise MmuError("Calibration of bowden length must be performed on gate 0")
        try:
            self._log_always("Calibrating bowden length (manual method) using %s as gate reference point" % self._gate_homing_string())
            self._set_filament_direction(self.DIRECTION_UNLOAD)
            self._servo_down()
            self._set_gate_ratio(1.)
            self._log_always("Finding gate position...")
            actual,homed,measured,_ = self._trace_filament_move("Reverse homing to gate sensor", -approx_bowden_length, motor="gear", homing_move=-1, endstop_name=self.ENDSTOP_GATE)
            if homed:
                actual = abs(actual)
                self._log_always("Recommended calibration bowden length is %.1fmm" % actual)
                if save:
                    self._set_calibrated_bowden_length(actual)
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_BOWDEN_LENGTH, actual))
                    self._log_always("Bowden calibration length has been saved")
                self._unload_gate() # Use real method to park filament
            else:
                raise MmuError("Calibration of bowden length failed. Did not home to gate sensor after moving %.1fmm" % approx_bowden_length)
        finally:
            self._servo_auto()

    def _calibrate_gate(self, gate, length, repeats, save=True):
        try:
            pos_values, neg_values = [], []
            self._select_tool(gate)
            self._set_gate_ratio(1.)
            self._load_gate(allow_retry=False)
            self._log_always("%s Gate %d over %.1fmm..." % ("Calibrating" if (gate > 0 and save) else "Validating calibration of", gate, length))

            for x in range(repeats):
                self._initialize_filament_position(dwell=True)    # Encoder 0000
                _,_,measured,delta = self._trace_filament_move("Calibration load movement", length, encoder_dwell=True)
                pos_values.append(measured)
                self._log_always("+ measured =  %.1fmm (counts = %d)" % ((length - delta), self._get_encoder_counts(dwell=None)))
                self._initialize_filament_position(dwell=True)    # Encoder 0000
                _,_,measured,delta = self._trace_filament_move("Calibration unload movement", -length, encoder_dwell=True)
                neg_values.append(measured)
                self._log_always("- measured =  %.1fmm (counts = %d)" % ((length - delta), self._get_encoder_counts(dwell=None)))

            self._log_always("Load direction: mean=%(mean).1f stdev=%(stdev).2f min=%(min).1f max=%(max).1f range=%(range).1f" % self._sample_stats(pos_values))
            self._log_always("Unload direction: mean=%(mean).1f stdev=%(stdev).2f min=%(min).1f max=%(max).1f range=%(range).1f" % self._sample_stats(neg_values))

            mean_pos = self._sample_stats(pos_values)['mean']
            mean_neg = self._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2
            ratio = mean / length

            self._log_always("Calibration move of %d x %.1fmm, average encoder measurement: %.1fmm - Ratio is %.6f" % (repeats * 2, length, mean, ratio))
            self._log_always("(Gate %d rotation_distance: %.6f vs Gate 0: %.6f)" % (gate, ratio * self.ref_gear_rotation_distance, self.ref_gear_rotation_distance))
            if not gate == 0: # Gate 0 is not calibrated, it is the reference
                if ratio > 0.8 and ratio < 1.2:
                    if save:
                        self.variables["%s%d" % (self.VARS_MMU_CALIB_PREFIX, gate)] = ratio
                        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=%.6f" % (self.VARS_MMU_CALIB_PREFIX, gate, ratio))
                        self._log_always("Calibration for Gate %d has been saved" % gate)
                        self.calibration_status |= self.CALIBRATED_GATES
                else:
                    self._log_always("Calibration ratio ignored because it is not considered valid (0.8 < ratio < 1.2)")
            self._unload_gate()
            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for Gate %d failed. Aborting, because: %s" % (gate, str(ee)))
        finally:
            self._servo_auto()

    def _get_max_selector_movement(self, gate=-1):
        n = gate if gate >= 0 else self.mmu_num_gates - 1

        if self.mmu_vendor.lower() == self.VENDOR_ERCF.lower():
            # ERCF Designs
            if self.mmu_version >= 2.0 or "t" in self.mmu_version_string:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)
            else:
                max_movement = self.cad_gate0_pos + (n * self.cad_gate_width) + (n//3) * self.cad_block_width

        else:
            # Everything else
            max_movement = self.cad_gate0_pos + (n * self.cad_gate_width)

        max_movement += self.cad_last_gate_offset if gate in [self.TOOL_GATE_UNKNOWN] else 0.
        max_movement += self.cad_selector_tolerance
        return max_movement

    def _calibrate_selector(self, gate, save=True):
        gate_str = lambda gate : ("Gate %d" % gate) if gate >= 0 else "bypass"
        try:
            self._initialize_state()
            self.calibrating = True
            self._servo_move()
            max_movement = self._get_max_selector_movement(gate)
            self._log_always("Measuring the selector position for %s" % gate_str(gate))
            traveled, found_home = self._measure_to_home()

            # Test we actually homed
            if not found_home:
                self._log_error("Selector didn't find home position")
                return

            # Warn and don't save if the measurement is unexpected
            if traveled > max_movement:
                self._log_always("Selector move measured %.1fmm. More than the anticipated maximum of %.1fmm. Save disabled" % (traveled, max_movement))
                save = 0
            else:
                self._log_always("Selector move measured %.1fmm" % traveled)

            if save:
                if gate >= 0:
                    self.selector_offsets[gate] = round(traveled, 1)
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets))
                    self.calibration_status |= self.CALIBRATED_SELECTOR
                else:
                    self.bypass_offset = round(traveled, 1)
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SELECTOR_BYPASS, self.bypass_offset))
                self._log_always("Selector offset (%.1fmm) for %s has been saved" % (traveled, gate_str(gate)))
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self.calibrating = False
            self._motors_off()

    def _calibrate_selector_auto(self, save=True, v1_bypass_block=-1):
        # Strategy is to find the two end gates, infer and set number of gates and distribute selector positions
        # Assumption: the user has manually positioned the selector aligned with gate 0 before calling
        try:
            self._log_always("Auto calibrating the selector. Excuse the whizz, bang, buzz, clicks...")
            self._initialize_state()
            self.calibrating = True
            self._servo_move()

            # Step 1 - position of gate 0
            self._log_always("Measuring the selector position for gate 0...")
            traveled, found_home = self._measure_to_home()
            if not found_home or traveled > self.cad_gate0_pos + self.cad_selector_tolerance:
                self._log_error("Selector didn't find home position or distance moved (%.1fmm) was larger than expected.\nAre you sure you aligned selector with gate 0 and removed filament?" % traveled)
                return
            gate0_pos = traveled

            # Step 2 - end of selector
            max_movement = self._get_max_selector_movement()
            self._log_always("Searching for end of selector... (up to %.1fmm)" % max_movement)
            self._trace_selector_move("Moving off endstop", self.cad_gate0_pos)
            if self.selector_touch:
                halt_pos, found_home = self._trace_selector_move("Detecting end of selector movement",
			max_movement, speed=self.selector_touch_speed, homing_move=1, endstop_name=self.ENDSTOP_SELECTOR_TOUCH)
            else:
                # This might not sound good!
                self._trace_selector_move("Forceably detecting end of selector movement", max_movement, speed=self.selector_homing_speed)
                found_home = True
            if not found_home:
                msg = "Didn't detect the end of the selector"
                if self.cad_last_gate_offset > 0:
                    self._log_error(msg)
                    return
                else:
                    self._log_always(msg)

            # Step 3a - selector length
            self._log_always("Measuring the full selector length...")
            traveled, found_home = self._measure_to_home()
            if not found_home:
                self._log_error("Selector didn't find home position after full length move")
                return
            self._log_always("Maximum selector movement is %.1fmm" % traveled)

            # Step 3b - bypass and last gate position (measured back from limit of travel)
            if self.cad_bypass_offset > 0:
                bypass_pos = traveled - self.cad_bypass_offset
            else:
                bypass_pos = 0.
            if self.cad_last_gate_offset > 0:
                # This allows the error to be averaged
                last_gate_pos = traveled - self.cad_last_gate_offset
            else:
                # This simply assumes theoretical distance
                last_gate_pos = gate0_pos + (self.mmu_num_gates - 1) * self.cad_gate_width

            # Step 4 - the calcs
            length = last_gate_pos - gate0_pos
            self._log_debug("Results: gate0_pos=%.1f, last_gate_pos=%.1f, length=%.1f" % (gate0_pos, last_gate_pos, length))
            selector_offsets = []

            if self.mmu_vendor.lower() == self.VENDOR_ERCF.lower() and self.mmu_version == 1.1:
                # ERCF v1.1 special case
                num_gates = int(round(length / (self.cad_gate_width + self.cad_block_width / 3))) + 1
                num_blocks = (num_gates - 1) // 3
                bypass_offset = 0.
                if v1_bypass_block >= 0:
                    adj_gate_width = (length - (num_blocks - 1) * self.cad_block_width - self.cad_bypass_block_width) / (num_gates - 1)
                else:
                    adj_gate_width = (length - num_blocks * self.cad_block_width) / (num_gates - 1)
                self._log_debug("Adjusted gate width: %.1f" % adj_gate_width)
                for i in range(num_gates):
                    bypass_adj = (self.cad_bypass_block_width - self.cad_block_width) if (i // 3) >= v1_bypass_block else 0.
                    selector_offsets.append(round(gate0_pos + (i * adj_gate_width) + (i // 3) * self.cad_block_width + bypass_adj, 1))
                    if ((i + 1) / 3) == v1_bypass_block:
                        bypass_offset = selector_offsets[i] + self.cad_bypass_block_delta

            else:
                # Generic Type-A MMU case
                num_gates = int(round(length / self.cad_gate_width)) + 1
                adj_gate_width = length / (num_gates - 1)
                self._log_debug("Adjusted gate width: %.1f" % adj_gate_width)
                for i in range(num_gates):
                    selector_offsets.append(round(gate0_pos + (i * adj_gate_width), 1))
                bypass_offset = bypass_pos

            if num_gates != self.mmu_num_gates:
                self._log_error("You configued your MMU for %d gates but I counted %d! Please update `mmu_num_gates`" % (self.mmu_num_gates, num_gates))
                return

            self._log_always("Offsets: %s%s" % (selector_offsets, (" (bypass: %.1f)" % bypass_offset) if bypass_offset > 0 else " (no bypass fitted)"))
            if save:
                self.selector_offsets = selector_offsets
                self.bypass_offset = bypass_offset
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SELECTOR_OFFSETS, self.selector_offsets))
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SELECTOR_BYPASS, self.bypass_offset))
                self._log_always("Selector calibration has been saved")
                self.calibration_status |= self.CALIBRATED_SELECTOR

            self._home(0, force_unload=0)
        except MmuError as ee:
            self._mmu_pause(str(ee))
            self._motors_off()
        finally:
            self.calibrating = False

    def _sample_stats(self, values):
        mean = stdev = vmin = vmax = 0.
        if values:
            mean = sum(values) / len(values)
            diff2 = [( v - mean )**2 for v in values]
            stdev = math.sqrt( sum(diff2) / max((len(values) - 1), 1))
            vmin = min(values)
            vmax = max(values)
        return {'mean': mean, 'stdev': stdev, 'min': vmin, 'max': vmax, 'range': vmax - vmin}


### CALIBRATION GCODE COMMANDS

    cmd_MMU_CALIBRATE_GEAR_help = "Calibration routine for gear stepper rotational distance"
    def cmd_MMU_CALIBRATE_GEAR(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        length = gcmd.get_float('LENGTH', 100., above=50.)
        measured = gcmd.get_float('MEASURED', above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)

        new_rotation_distance = self.ref_gear_rotation_distance * measured / length

        self._log_always("Gear stepper `rotation_distance` calculated to be %.6f" % new_rotation_distance)
        if save:
            self.gear_stepper.set_rotation_distance(new_rotation_distance)
            self.ref_gear_rotation_distance = new_rotation_distance
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.6f" % (self.VARS_MMU_GEAR_ROTATION_DISTANCE, new_rotation_distance))
            self._log_always("Gear calibration has been saved")
            self.calibration_status |= self.CALIBRATED_GEAR

    # Start: Assumes filament is loaded through encoder
    # End: Does not eject filament at end (filament same as start)
    cmd_MMU_CALIBRATE_ENCODER_help = "Calibration routine for the MMU encoder"
    def cmd_MMU_CALIBRATE_ENCODER(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_has_encoder(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(self.CALIBRATED_GEAR): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        speed = gcmd.get_float('SPEED', self.gear_from_buffer_speed, minval=10.)
        accel = gcmd.get_float('ACCEL', self.gear_from_buffer_accel, minval=10.)
        min_speed = gcmd.get_float('MINSPEED', speed, above=0.)
        max_speed = gcmd.get_float('MAXSPEED', speed, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        try:
            self._servo_down()
            self.calibrating = True
            with self._require_encoder():
                self._calibrate_encoder(length, repeats, speed, min_speed, max_speed, accel, save)
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self.calibrating = False

    cmd_MMU_CALIBRATE_SELECTOR_help = "Calibration of the selector positions or postion of specified gate"
    def cmd_MMU_CALIBRATE_SELECTOR(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        if gate == -1 and gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
            gate = self.TOOL_GATE_BYPASS

        if gate != -1:
            self._calibrate_selector(gate, save=save)
        else:
            self._calibrate_selector_auto(save=save, v1_bypass_block=gcmd.get_int('BYPASS_BLOCK', -1, minval=1, maxval=3))

    # Start: Will home selector, select gate 0
    # End: Filament will unload
    cmd_MMU_CALIBRATE_BOWDEN_help = "Calibration of reference bowden length for gate 0"
    def cmd_MMU_CALIBRATE_BOWDEN(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return

        manual = bool(gcmd.get_int('MANUAL', 0, minval=0, maxval=1))
        if not self._has_encoder() and not manual:
            self._log_always("No encoder available. Use manual calibration method:\nWith gate 0 selected, manually load filament all the way to the extruder gear\nThen run `MMU_CALIBRATE_BOWDEN MANUAL=1 BOWDEN_LENGTH=xxx`\nWhere BOWDEN_LENGTH is greater than your real length")
            return
        if manual:
            if self._check_is_calibrated(self.CALIBRATED_GEAR|self.CALIBRATED_SELECTOR): return
        else:
            if self._check_is_calibrated(self.CALIBRATED_GEAR|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR): return

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
                self._reset_ttg_mapping() # To force tool = gate
                self._unload_tool()
                with self._require_encoder():
                    self._calibrate_bowden_length_auto(approx_bowden_length, extruder_homing_max, repeats, save)
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self.calibrating = False

    # Start: Will home selector, select gate 0 or required gate
    # End: Filament will unload
    cmd_MMU_CALIBRATE_GATES_help = "Optional calibration of individual MMU gate"
    def cmd_MMU_CALIBRATE_GATES(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(self.CALIBRATED_GEAR|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR|self.CALIBRATED_BOWDEN): return
        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        auto = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        if gate == -1 and not auto:
            raise gcmd.error("Must specify 'GATE=' or 'ALL=1' for all gates")
        try:
            self._reset_ttg_mapping() # To force tool = gate
            self._unload_tool()
            self.calibrating = True
            with self._require_encoder():
                if gate == -1:
                    self._log_always("Start the complete calibration of ancillary gates...")
                    for gate in range(self.mmu_num_gates - 1):
                        self._calibrate_gate(gate + 1, length, repeats, save=save)
                    self._log_always("Phew! End of auto gate calibration")
                else:
                    self._calibrate_gate(gate, length, repeats, save=(save and gate != 0))
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self.calibrating = False


#######################
# MMU STATE FUNCTIONS #
#######################

    def _setup_heater_off_reactor(self):
        self.heater_off_handler = self.reactor.register_timer(self._handle_pause_timeout, self.reactor.NEVER)

    def _handle_pause_timeout(self, eventtime):
        self._log_info("Disabled extruder heater")
        self.gcode.run_script_from_command("M104 S0")
        return self.reactor.NEVER

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
        self._log_trace("Got sync force feedback update: eventtime=%s, state=%s" % (eventtime, state))
        if abs(state) <= 1:
            self.sync_feedback_last_state = float(state)
            if self.sync_feedback_enable and self.sync_feedback_operational:
                self._update_sync_multiplier()

    def _enable_sync_feedback(self):
        if self.sync_feedback_operational: return
        self.sync_feedback_operational = True
        self.reactor.update_timer(self.sync_feedback_timer, self.reactor.NOW)
        self._update_sync_multiplier()

    def _disable_sync_feedback(self):
        if not self.sync_feedback_operational: return
        self.reactor.update_timer(self.sync_feedback_timer, self.reactor.NEVER)
        self.sync_feedback_operational = False
        self.sync_feedback_last_direction = 0
        self._log_trace("Reset sync multiplier")
        self._set_gate_ratio(self._get_gate_ratio(self.gate_selected))

    def _update_sync_feedback(self, eventtime):
        estimated_print_time = self.printer.lookup_object('mcu').estimated_print_time(eventtime)
        extruder = self.toolhead.get_extruder()
        pos = extruder.find_past_position(estimated_print_time)
        past_pos = extruder.find_past_position(max(0., estimated_print_time - self.SYNC_POSITION_TIMERANGE))
        if abs(pos - past_pos) >= self.SYNC_POSITION_MIN_DELTA:
            prev_direction = self.sync_feedback_last_direction
            self.sync_feedback_last_direction = self.DIRECTION_LOAD if pos > past_pos else self.DIRECTION_UNLOAD if pos < past_pos else 0
            if self.sync_feedback_last_direction != prev_direction:
                d = self.sync_feedback_last_direction
                self._log_trace("New sync direction: %s" % ('extrude' if d == self.DIRECTION_LOAD else 'retract' if d == self.DIRECTION_UNLOAD else 'static'))
                self._update_sync_multiplier()
        return eventtime + self.SYNC_FEEDBACK_INTERVAL

    def _update_sync_multiplier(self):
        if not self.sync_feedback_enable: return
        if self.sync_feedback_last_direction == 0:
            multiplier = 1.
        else:
            go_slower = lambda s, d: abs(s - d) < abs(s + d)
            if go_slower(self.sync_feedback_last_state, self.sync_feedback_last_direction):
                # Expanded when extruding or compressed when retracting, so decrease the rotation distance of gear stepper to speed it up
                multiplier = 1. - (abs(1. - self.sync_multiplier_low) * abs(self.sync_feedback_last_state))
            else:
                # Compressed when extruding or expanded when retracting, so increase the rotation distance of gear stepper to slow it down
                multiplier = 1. + (abs(1. - self.sync_multiplier_high) * abs(self.sync_feedback_last_state))
        self._log_trace("Updated sync multiplier: %.4f" % multiplier)
        self._set_gate_ratio(self._get_gate_ratio(self.gate_selected) / multiplier)

    def _is_printer_printing(self):
        return self.print_stats.state == "printing"

    def _is_printer_paused(self):
        return self.pause_resume.is_paused

    def _is_printing(self, force_in_print=False): # Actively printing and not paused
        return self.print_state in ["started", "printing"] or force_in_print or self.test_force_in_print

    def _is_in_print(self, force_in_print=False): # Printing or paused
        return self.print_state in ["printing", "pause_locked", "paused"] or force_in_print or self.test_force_in_print

    def _is_mmu_paused(self): # The MMU is paused
        return self.print_state in ["pause_locked", "paused"]

    def _is_mmu_pause_locked(self): # The MMU is paused (and locked)
        return self.print_state in ["pause_locked"]

    def _is_in_endstate(self):
        return self.print_state in ["complete", "cancelled", "error", "ready", "standby", "initialized"]

    def _is_in_standby(self):
        return self.print_state in ["standby"]

    def _wakeup(self):
        if self._is_in_standby():
            self._set_print_state("idle")

    # Track print events simply to ease internal print state transitions. Specificly we want to detect
    # the start and end of a print and falling back into 'standby' state on idle
    #
    # Klipper reference sources for state:
    # print_stats: {'filename': '', 'total_duration': 0.0, 'print_duration': 0.0,
    #               'filament_used': 0.0, 'state': standby|printing|paused|complete|cancelled|error,
    #               'message': '', 'info': {'total_layer': None, 'current_layer': None}}
    # idle_status: {'state': Idle|Ready|Printing, `printing_time`: 0.0}
    # pause_resume: {'is_paused': True|False}
    #
    def _handle_idle_timeout_event(self, eventtime, event_type):
        if not self.is_enabled: return
        self._log_trace("Processing idle_timeout '%s' event" % event_type)

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
                        self._log_trace("Automaticaly detected RESUME (ignored), print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                    else:
                        # This is a 'started' state
                        self._log_trace("Automaticaly detected JOB START, print_status:print_stats=%s, current mmu print_state=%s" % (new_state, self.print_state))
                        if self.print_state not in ["started", "printing"]:
                            self._on_print_start(pre_start_only=True)
                            self.reactor.register_callback(self._print_start_event_handler)
                elif new_state in ["complete", "error"] and event_type == "ready":
                    self._log_trace("Automatically detected JOB %s, print_stats=%s, current mmu print_state=%s" % (new_state.upper(), new_state, self.print_state))
                    if new_state == "error":
                        self.reactor.register_callback(self._print_error_event_handler)
                    else:
                        self.reactor.register_callback(self._print_complete_event_handler)
                self.last_print_stats = dict(new_ps)

        # Capture transition to standby
        if event_type == "idle" and self.print_state != "standby":
            self.reactor.register_callback(self._print_standby_event_handler)

    def _exec_gcode(self, command):
        try:
            self.gcode.run_script(command)
        except Exception:
            logging.exception("Error running job state initializer/finalizer or bootup tasks")

    def _print_start_event_handler(self, eventtime):
        self._log_trace("_print_start_event_handler()")
        self._exec_gcode("_MMU_PRINT_START")

    def _print_complete_event_handler(self, eventtime):
        self._log_trace("_print_complete_event_handler()")
        self._exec_gcode("_MMU_PRINT_END STATE=complete")

    def _print_error_event_handler(self, eventtime):
        self._log_trace("_print_error_event_handler()")
        self._exec_gcode("_MMU_PRINT_END STATE=error")

    def _print_standby_event_handler(self, eventtime):
        self._log_trace("_print_standby_event_handler()")
        self._exec_gcode("_MMU_PRINT_END STATE=standby")

    # MMU job state machine: initialized|ready|started|printing|complete|cancelled|error|pause_locked|paused|standby
    def _set_print_state(self, print_state, call_macro=True):
        if print_state != self.print_state:
            idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout
            self._log_debug("Job State: %s -> %s (MMU State: Encoder: %s, Synced: %s, Paused temp: %s, Resume to state: %s, Position saved: %s, z_height saved: %.2fmm, pause_resume: %s, Idle timeout: %.2fs)"
                    % (self.print_state.upper(), print_state.upper(), self._get_encoder_state(), self.mmu_toolhead.is_gear_synced_to_extruder(), self.paused_extruder_temp,
                        self.resume_to_state, self.saved_toolhead_position, self.saved_toolhead_height, self._is_printer_paused(), idle_timeout))
            if call_macro:
                if self.printer.lookup_object("gcode_macro %s" % self.print_state_changed_macro, None) is not None:
                    self._wrap_gcode_command("%s STATE='%s' OLD_STATE='%s'" % (self.print_state_changed_macro, print_state, self.print_state))
            self.print_state = print_state

    # If this is called automatically when printing starts. The pre_start_only operations are performed on an idle_timeout
    # event so cannot block.  The remainder of moves will be called from the queue but they will be called early so
    # don't do anything that requires operating toolhead kinematics (we might not even be homed yet)
    def _on_print_start(self, pre_start_only=False):
        if self.print_state not in ["started", "printing"]:
            self._log_trace("_on_print_start(->started)")
            self._clear_saved_toolhead_position()
            self.paused_extruder_temp = None
            self._reset_job_statistics() # Reset job stats but leave persisted totals alone
            self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self.is_handling_runout = False
            self._clear_slicer_tool_map()
            self._enable_runout() # Enable runout/clog detection while printing
            self._initialize_filament_position(dwell=None) # Encoder 0000
            self._set_print_state("started", call_macro=False)

        if not pre_start_only and self.print_state not in ["printing"]:
            self._log_trace("_on_print_start(->printing)")
            self._sync_gear_to_extruder(self.sync_to_extruder, servo=True, current=True)
            msg = "Happy Hare initialized ready for print"
            if self.filament_pos == self.FILAMENT_POS_LOADED:
                msg += " (initial tool T%s loaded)" % self.tool_selected
            else:
                msg += " (no filament preloaded)"
            if self.ttg_map != self.default_ttg_map:
                msg += "\nWarning: Non default TTG map in effect"
            self._log_info(msg)
            self._set_print_state("printing")

    # Force state transistion to printing for any early moves
    def _fix_started_state(self):
        if self._is_printer_printing() and not self._is_in_print():
            self._wrap_gcode_command("_MMU_PRINT_START")

    def _mmu_pause(self, reason, force_in_print=False):
        self._fix_started_state() # Get out of 'started' state before transistion to pause

        run_pause_macro = recover_pos = send_event = False
        if self._is_in_print(force_in_print):
            if not self._is_mmu_paused():
                self.resume_to_state = "printing" if self._is_in_print() else "ready"
                self.reason_for_pause = reason
                self._display_mmu_error()
                self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp
                self._log_trace("Saved desired extruder temperature: %.1f%sC" % (self.paused_extruder_temp, UI_DEGREE))
                self._track_pause_start()
                self._log_trace("Extruder heater will be disabled in %s" % self._seconds_to_string(self.disable_heater))
                self.reactor.update_timer(self.heater_off_handler, self.reactor.monotonic() + self.disable_heater) # Set extruder off timer
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.timeout_pause) # Set alternative pause idle_timeout
                self._disable_runout() # Disable runout/clog detection while in pause state
                self._save_toolhead_position_and_lift("mmu_pause", z_hop_height=self.z_hop_height_error, force_in_print=force_in_print)
                run_pause_macro = not self._is_printer_paused()
                self._set_print_state("pause_locked")
                send_event = True
                recover_pos = self.filament_recovery_on_pause
            else:
                self._log_error("MMU issue detected whilst printer is paused\nReason: %s" % reason)
                recover_pos = self.filament_recovery_on_pause
        else:
            self._log_error("MMU issue detected whilst out of a print\nReason: %s" % reason)

        # Be deliberate about order of these tasks
        if run_pause_macro:
            self._wrap_gcode_command(self.pause_macro)

        if recover_pos:
            self._recover_filament_pos(strict=False, message=True)

        self._sync_gear_to_extruder(False, servo=True)

        if send_event:
            self.printer.send_event("mmu:mmu_paused") # Notify MMU paused event

    # Displays MMU error/pause as pop-up dialog and/or via console
    def _display_mmu_error(self):
        msg= "Print%s paused" % (" was already" if self._is_printer_paused() else " will be")
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.error_dialog_macro, None)
        if self.show_error_dialog and dialog_macro is not None:
            # Klipper doesn't handle string quoting so strip problematic characters
            reason = self.reason_for_pause.replace("\n", ". ")
            for c in "#;'":
                reason = reason.replace(c, "")
            self._wrap_gcode_command('%s MSG="%s" REASON="%s"' % (self.error_dialog_macro, msg, reason))
        self._log_error("MMU issue detected. %s\nReason: %s" % (msg, self.reason_for_pause))
        self._log_always("After fixing, call RESUME to continue printing (MMU_UNLOCK to restore temperature)")

    def _clear_mmu_error_dialog(self):
        dialog_macro = self.printer.lookup_object('gcode_macro %s' % self.error_dialog_macro, None)
        if self.show_error_dialog and dialog_macro is not None:
            self._wrap_gcode_command('RESPOND TYPE=command MSG="action:prompt_end"')

    def _mmu_unlock(self):
        if self._is_mmu_paused():
            self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout)
            self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER)

            # Important to wait for stable temperature to resume exactly how we paused
            if self.paused_extruder_temp:
                self._log_info("Enabled extruder heater")
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self._set_print_state("paused")

    def _mmu_resume(self):
        if self._is_mmu_paused():
            self.reason_for_pause = None
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self.paused_extruder_temp = None
            self._track_pause_end()
            self._enable_runout() # Enable runout/clog detection while printing
            self._set_print_state(self.resume_to_state)
            sync = self.resume_to_state == "printing"
            self.resume_to_state = "ready"
            self._continue_printing("resume", sync=sync)
            self.printer.send_event("mmu:mmu_resumed") # Notify MMU resumed event

    def _continue_printing(self, operation, sync=True):
        self._clear_macro_state()
        self.is_handling_runout = False # Covers errorless runout handling and mmu_resume()
        if self._is_in_print():
            self._sync_gear_to_extruder(self.sync_to_extruder and sync, servo=True, current=sync)
        self._restore_toolhead_position(operation)
        self._initialize_filament_position() # Encoder 0000
        # Ready to continue printing...

    def _clear_macro_state(self):
        if self.printer.lookup_object('gcode_macro %s' % self.clear_position_macro, None) is not None:
            self._wrap_gcode_command(self.clear_position_macro)

    # If this is called automatically it will occur after the user's print ends.
    # Therefore don't do anything that requires operating kinematics or execute gcode
    def _on_print_end(self, state="complete"):
        if not self._is_in_endstate():
            self._log_trace("_on_print_end(%s)" % state)
            self._movequeues_wait_moves()
            self._clear_saved_toolhead_position()
            self.resume_to_state = "ready"
            self.paused_extruder_temp = None
            self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self._disable_runout() # Disable runout/clog detection after print

            if self.printer.lookup_object("idle_timeout").idle_timeout != self.default_idle_timeout:
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout) # Restore original idle_timeout
            self._sync_gear_to_extruder(False, servo=True)
            self._set_print_state(state)
        if state == "standby" and not self._is_in_standby():
            self._set_print_state(state)

    def _save_toolhead_position_and_lift(self, operation=None, z_hop_height=None, force_in_print=False):
        if operation and not self.saved_toolhead_position:
            self._movequeues_wait_moves()
            eventtime = self.reactor.monotonic()
            homed = self.toolhead.get_status(eventtime)['homed_axes']
            gcode_move = self.printer.lookup_object("gcode_move")

            # Save toolhead position
            if 'xyz' in homed:
                gcode_pos = gcode_move.get_status(eventtime)['gcode_position']
                toolhead_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                self._log_debug("Saving toolhead gcode state and position (%s) for %s" % (toolhead_gcode_pos, operation))
                self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=MMU_state")
                self.saved_toolhead_position = operation

                # Make sure we record the current speed/extruder overrides
                if self.tool_selected >= 0:
                    mmu_state = gcode_move.saved_states['MMU_state']
                    self.tool_speed_multipliers[self.tool_selected] = mmu_state['speed_factor'] * 60.
                    self.tool_extrusion_multipliers[self.tool_selected] = mmu_state['extrude_factor']

                # Lift toolhead off print the specified z-hop
                if self._is_in_print(force_in_print) and z_hop_height is not None and z_hop_height > 0:
                    self._log_debug("Lifting toolhead %.1fmm" % z_hop_height)
                    act_z = self.saved_toolhead_height = gcode_pos.z
                    max_z = self.toolhead.get_status(eventtime)['axis_maximum'].z
                    max_z -= gcode_move.get_status(eventtime)['homing_origin'].z
                    safe_z = z_hop_height if (act_z < (max_z - z_hop_height)) else (max_z - act_z)
                    self.gcode.run_script_from_command("G90")
                    self.gcode.run_script_from_command("G1 Z%.4f F%d" %(act_z + safe_z, self.z_hop_speed * 60))
            else:
                self._log_debug("Cannot save toolhead position or z-hop for %s because not homed" % operation)

        elif operation:
            self._log_debug("Asked to save toolhead position for %s but it is already saved for %s. Ignored" % (operation, self.saved_toolhead_position))

    def _restore_toolhead_position(self, operation):
        if self.saved_toolhead_position:
            eventtime = self.reactor.monotonic()
            gcode_move = self.printer.lookup_object("gcode_move")
            gcode_pos = gcode_move.get_status(eventtime)['gcode_position']

            # Inject speed/extruder overrides into gcode state restore data
            if self.tool_selected >= 0:
                mmu_state = self.printer.lookup_object("gcode_move").saved_states['MMU_state']
                mmu_state['speed_factor'] = self.tool_speed_multipliers[self.tool_selected] / 60.
                mmu_state['extrude_factor'] = self.tool_extrusion_multipliers[self.tool_selected]

            if self.restore_toolhead_xy_position:
                # Restore pre-pause position and state
                self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=MMU_state MOVE=1 MOVE_SPEED=%.1f" % self.z_hop_speed)
                toolhead_gcode_pos = " ".join(["%s:%.1f" % (a, v) for a, v in zip("XYZE", gcode_pos)])
                self._log_debug("Restored gcode state and position (%s) after %s" % (toolhead_gcode_pos, operation))
            else:
                # Default: Only undo the z-hop move...
                if self.saved_toolhead_height >= 0:
                    self._log_debug("Restoring toolhead height")
                    self.gcode.run_script_from_command("G90")
                    self.gcode.run_script_from_command("G1 Z%.4f F%d" % (self.saved_toolhead_height, self.z_hop_speed * 60))
                # But ensure gcode state...
                self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=MMU_state")
                self._log_debug("Restored gcode state and z-hop position only (Z:%.1f) after %s" % (self.saved_toolhead_height, operation))

        self._clear_saved_toolhead_position()

    def _clear_saved_toolhead_position(self):
        self.saved_toolhead_position = None
        self.saved_toolhead_height = -1.

    def _disable_runout(self):
        enabled = self.runout_enabled
        if enabled:
            self._log_trace("Disabled runout detection")
            if self._has_encoder() and self.encoder_sensor.is_enabled():
                self.encoder_sensor.disable()
            self._set_sensor_runout(False)
            self.runout_enabled = False
        return enabled

    def _enable_runout(self):
        self.runout_enabled = True
        self._log_trace("Enabled runout detection")
        if self._has_encoder() and not self.encoder_sensor.is_enabled():
            self.encoder_sensor.enable()
        self._set_sensor_runout(True)

    def _set_sensor_runout(self, restore):
        for gate in range(self.mmu_num_gates):
            sensor = self.printer.lookup_object("filament_switch_sensor %s_%d" % (self.PRE_GATE_SENSOR_PREFIX, gate), None)
            if sensor:
                sensor.runout_helper.enable_runout(restore and (gate == self.gate_selected))
        sensor = self.sensors.get(self.ENDSTOP_GATE, None)
        if sensor is not None:
            sensor.runout_helper.enable_runout(restore and (self.gate_selected != self.TOOL_GATE_UNKNOWN))

    def _check_runout(self):
        if self._is_mmu_paused() and not self.resume_to_state == "printing": return
        if self._check_pre_gate_sensor(self.gate_selected) is False:
            raise MmuError("Runout check failed. Pre-gate sensor for gate %d does not detect filament" % self.gate_selected)
        if self._check_sensor(self.ENDSTOP_GATE) is False:
            raise MmuError("Runout check failed. %s sensor does not detect filament" % self.ENDSTOP_GATE)

    @contextlib.contextmanager
    def _wrap_suspend_runout(self):
        enabled = self._disable_runout()
        try:
            yield self
        finally:
            if enabled:
                self._enable_runout()

    def _has_encoder(self):
        return self.encoder_sensor is not None and not self.test_disable_encoder

    def _can_use_encoder(self):
        return self.encoder_sensor is not None and (self.encoder_move_validation or self.encoder_force_validation)

    def _check_has_encoder(self):
        if not self._has_encoder():
            self._log_error("No encoder fitted to MMU")
            return True
        return False

    def _get_encoder_state(self):
        if self._has_encoder():
            return "%s" % "Enabled" if self.encoder_sensor.is_enabled() else "Disabled"
        else:
            return "n/a"

    # For all encoder methods, 'dwell' means:
    #   True  - gives klipper a little extra time to deliver all encoder pulses when absolute accuracy is required
    #   False - wait for moves to complete and then read encoder
    #   None  - just read encoder without delay (assumes prior movements have completed)
    def _encoder_dwell(self, dwell):
        if self._has_encoder():
            if dwell:
                self._movequeues_dwell(self.encoder_dwell)
                self._movequeues_wait_moves()
                return True
            elif dwell is False and self._can_use_encoder():
                self._movequeues_wait_moves()
                return True
            elif dwell is None and self._can_use_encoder():
                return True
        return False

    @contextlib.contextmanager
    def _require_encoder(self):
        if not self._has_encoder():
            raise MmuError("Assertion failure: Encoder required for chosen operation but not present on MMU")
        self.encoder_force_validation = True
        try:
            yield self
        finally:
            self.encoder_force_validation = False

    def _get_encoder_distance(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder_sensor.get_distance()
        else:
            return 0.

    def _get_encoder_counts(self, dwell=False):
        if self._encoder_dwell(dwell):
            return self.encoder_sensor.get_counts()
        else:
            return 0

    def _set_encoder_distance(self, distance, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder_sensor.set_distance(distance)

    def _get_encoder_dead_space(self):
        if self._has_sensor(self.ENDSTOP_GATE) and self.gate_homing_endstop == self.ENDSTOP_GATE:
            return self.gate_endstop_to_encoder
        else:
            return 0.

    def _initialize_filament_position(self, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder_sensor.reset_counts()
        self._set_filament_position()

    def _get_filament_position(self):
        return self.mmu_toolhead.get_position()[1]

    def _set_filament_position(self, position = 0.):
        pos = self.mmu_toolhead.get_position()
        pos[1] = position
        self.mmu_toolhead.set_position(pos)
        return position

    def _set_filament_pos_state(self, state, silent=False):
        self.filament_pos = state
        if self.gate_selected != self.TOOL_GATE_BYPASS or state == self.FILAMENT_POS_UNLOADED or state == self.FILAMENT_POS_LOADED:
            self._display_visual_state(silent=silent)

        # Minimal save_variable writes
        if state in [self.FILAMENT_POS_LOADED, self.FILAMENT_POS_UNLOADED]:
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_FILAMENT_POS, state))
        elif self.variables.get(self.VARS_MMU_FILAMENT_POS, 0) != self.FILAMENT_POS_UNKNOWN:
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_FILAMENT_POS, self.FILAMENT_POS_UNKNOWN))

    def _set_filament_direction(self, direction):
        self.filament_direction = direction

    def _has_sensor(self, name):
        return self.sensors[name].runout_helper.sensor_enabled if name in self.sensors else False

    # Return sensor state or None if not installed
    def _check_sensor(self, name):
        sensor = self.sensors.get(name, None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = sensor.runout_helper.filament_present
            self._log_trace("(%s sensor %s filament)" % (name, "detects" if detected else "does not detect"))
            return detected
        else:
            return None

    # Return pre-gate sensor state or None if not installed
    def _check_pre_gate_sensor(self, gate):
        sensor = self.printer.lookup_object("filament_switch_sensor %s_%d" % (self.PRE_GATE_SENSOR_PREFIX, gate), None)
        if sensor is not None and sensor.runout_helper.sensor_enabled:
            detected = sensor.runout_helper.filament_present
            self._log_trace("(%s_%d sensor %s filament)" % (self.PRE_GATE_SENSOR_PREFIX, gate, "detects" if detected else "does not detect"))
            return detected
        else:
            return None

    # Return dict of all sensor states (or None if sensor disabled)
    def _check_all_sensors(self):
        result = {}
        for name, sensor in self.sensors.items():
            result[name] = sensor.runout_helper.filament_present if sensor.runout_helper.sensor_enabled else None
        return result

    def _must_home_to_extruder(self):
        return self.extruder_force_homing or not self._has_sensor(self.ENDSTOP_TOOLHEAD)

    def _check_is_disabled(self):
        if not self.is_enabled:
            self._log_error("MMU is disabled. Please use MMU ENABLE=1 to use")
            return True
        self._wakeup()
        return False

    def _check_in_bypass(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.filament_pos not in [self.FILAMENT_POS_UNLOADED]:
                self._log_error("Operation not possible. MMU is currently using bypass. Unload or select a different gate first")
                return True
        return False

    def _check_not_bypass(self):
        if self.tool_selected != self.TOOL_GATE_BYPASS:
            self._log_error("Bypass not selected. Please use MMU_SELECT_BYPASS first")
            return True
        return False

    def _check_not_homed(self):
        if not self.is_homed:
            self._log_error("MMU is not homed")
            return True
        return False

    def _check_is_loaded(self):
        if self.filament_pos not in [self.FILAMENT_POS_UNLOADED, self.FILAMENT_POS_UNKNOWN]:
            self._log_error("MMU has filament loaded")
            return True
        return False

    def _check_is_calibrated(self, required=None, silent=False):
        if not required:
            required = self.CALIBRATED_ALL
        if not (self.calibration_status & required == required):
            steps = []
            if self.CALIBRATED_GEAR & required and not self.calibration_status & self.CALIBRATED_GEAR:
                    steps.append("MMU_CALIBRATE_GEAR")
            if self.CALIBRATED_ENCODER & required and not self.calibration_status & self.CALIBRATED_ENCODER:
                steps.append("MMU_CALIBRATE_ENCODER")
            if self.CALIBRATED_SELECTOR & required and not self.calibration_status & self.CALIBRATED_SELECTOR:
                steps.append("MMU_CALIBRATE_SELECTOR")
            if self.CALIBRATED_BOWDEN & required and not self.calibration_status & self.CALIBRATED_BOWDEN:
                steps.append("MMU_CALIBRATE_BOWDEN")
            if self.CALIBRATED_GATES & required and not self.calibration_status & self.CALIBRATED_GATES:
                steps.append("MMU_CALIBRATE_GATES")
            msg = "Prerequsite calibration steps are not complete. Please run:"
            if not silent:
                self._log_error("%s %s" % (msg, ", ".join(steps)))
            return True
        return False

    def _check_has_leds(self):
        if not self.has_leds:
            self._log_error("No LEDs configured on MMU")
            return True
        return False

    def _gate_homing_string(self):
        return "ENCODER" if self.gate_homing_endstop == self.ENDSTOP_ENCODER else "ENDSTOP '%s'" % self.gate_homing_endstop

    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.extruder_name)
        current_temp = extruder.get_status(0)['temperature']
        current_target_temp = extruder.heater.target_temp
        klipper_minimum_temp = extruder.get_heater().min_extrude_temp
        self._log_trace("_ensure_safe_extruder_temperature: current_temp=%s, paused_extruder_temp=%s, current_target_temp=%s, klipper_minimum_temp=%s, default_extruder_temp=%s, source=%s" % (current_temp, self.paused_extruder_temp, current_target_temp, klipper_minimum_temp, self.default_extruder_temp, source))

        if source == "pause":
            new_target_temp = self.paused_extruder_temp if self.paused_extruder_temp is not None else current_temp # Pause temp should not be None
            if self.paused_extruder_temp < klipper_minimum_temp:
                # Don't wait if just messing with cold printer
                wait = False
        elif source == "auto":
            if self._is_mmu_paused():
                # In a pause we always want to restore the temp we paused at
                new_target_temp = self.paused_extruder_temp if self.paused_extruder_temp is not None else current_temp # Pause temp should not be None
                source = "pause"
            elif self._is_printing():
                # While actively printing, we want to defer to the slicer for temperature
                new_target_temp = current_target_temp
                source = "slicer"
            else:
                # Standalone "just messing" case
                if current_target_temp > klipper_minimum_temp:
                    new_target_temp = current_target_temp
                    source = "current"
                else:
                    new_target_temp = self.default_extruder_temp
                    source = "default"

            if new_target_temp < klipper_minimum_temp:
                # If, for some reason, the target temp is below Klipper's minimum, set to minimum
                # set the target to Happy Hare's default. This strikes a balance between utility
                # and safety since Klipper's min is truly a bare minimum but our min should be
                # a more realistic temperature for safe operation.
                new_target_temp = self.default_extruder_temp
                source = "minimum"

        if current_temp < new_target_temp:
            wait = True

        if new_target_temp > current_target_temp:
            if source in ["default", "minimum"]:
                # We use error channel to aviod heating surprise. This will also cause popup in Klipperscreen
                self._log_error("Warning: Automatically heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            else:
                self._log_info("Heating extruder to %s temp (%.1f%sC)" % (source, new_target_temp, UI_DEGREE))
            wait = True # Always wait to warm up

        if new_target_temp > 0:
            self.gcode.run_script_from_command("M104 S%.1f" % new_target_temp)

            # Optionally wait until temperature is stable or at minimum safe temp so extruder can move
            if wait and new_target_temp >= klipper_minimum_temp and abs(new_target_temp - current_temp) > 2:
                with self._wrap_action(self.ACTION_HEATING):
                    self._log_info("Waiting for extruder to reach target (%s) temperature: %.1f%sC" % (source, new_target_temp, UI_DEGREE))
                    self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.1f MAXIMUM=%.1f" % (new_target_temp - 1, new_target_temp + 1))

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
        if action == self.action: return
        old_action = self.action
        self.action = action
        if self.printer.lookup_object("gcode_macro %s" % self.action_changed_macro, None) is not None:
            self._wrap_gcode_command("%s ACTION='%s' OLD_ACTION='%s'" % (self.action_changed_macro, self._get_action_string(), self._get_action_string(old_action)))
        return old_action

    @contextlib.contextmanager
    def _wrap_action(self, new_action):
        old_action = self._set_action(new_action)
        try:
            yield (old_action, new_action)
        finally:
            self._set_action(old_action)

    def _enable_mmu(self):
        if self.is_enabled: return
        self._initialize_state()
        if not self._check_is_calibrated(silent=True):
            self._load_persisted_state()
        self.is_enabled = True
        self.printer.send_event("mmu:enabled")
        self._log_always("MMU enabled and reset")
        self._schedule_mmu_bootup_tasks()

    def _disable_mmu(self):
        if not self.is_enabled: return
        self._initialize_state()
        self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER)
        self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout)
        self.is_enabled = False
        self.printer.send_event("mmu:disabled")
        self._set_print_state("standby")
        self._log_always("MMU disabled")

    def _random_failure(self):
        if self.test_random_failures and randint(0, 10) == 0:
            raise MmuError("Randomized testing failure")


### STATE GCODE COMMANDS

    cmd_MMU_help = "Enable/Disable functionality and reset state"
    def cmd_MMU(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        enable = gcmd.get_int('ENABLE', minval=0, maxval=1)
        if enable == 1:
            self._enable_mmu()
        else:
            self._disable_mmu()

    cmd_MMU_HELP_help = "Display the complete set of MMU commands and function"
    def cmd_MMU_HELP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        macros = gcmd.get_int('MACROS', 0, minval=0, maxval=1)
        testing = gcmd.get_int('TESTING', 0, minval=0, maxval=1)
        steps = gcmd.get_int('STEPS', 0, minval=0, maxval=1)
        msg = "Happy Hare MMU commands: (use MMU_HELP MACROS=1 TESTING=1 STEPS=1 GCODE=1 for full command set)\n"
        tmsg = "\nCalibration and testing commands:\n"
        mmsg = "\nMacros and callbacks (defined in mmu_software.cfg, mmu_form_tip.cfg, mmu_cut_tip.cfg, mmu_sequence.cfg, mmu_state.cfg, mmu_leds.cfg):\n"
        smsg = "\nIndividual load/unload sequence steps:\n"
        cmds = list(self.gcode.ready_gcode_handlers.keys())
        cmds.sort()
        for c in cmds:
            d = self.gcode.gcode_help.get(c, "n/a")
            if c.startswith("MMU_START") or (c.startswith("MMU_END") and c not in ["MMU_ENDLESS_SPOOL"]):
                mmsg += "%s : %s\n" % (c.upper(), d)
            elif c.startswith("MMU") and not c.startswith("MMU__"):
                if not "_CALIBRATE" in c and not "_TEST" in c and not "_SOAKTEST" in c:
                    if c not in ["MMU_UNLOAD", "MMU_CHANGE_TOOL_STANDALONE", "MMU_CHECK_GATES", "MMU_REMAP_TTG", "MMU_FORM_TIP"]: # Remove aliases
                        msg += "%s : %s\n" % (c.upper(), d)
                else:
                    tmsg += "%s : %s\n" % (c.upper(), d)
            elif c.startswith("_MMU"):
                if not c.startswith("_MMU_STEP") and c not in ["_MMU_M400"]:
                    if not c.endswith("_VARS") and c not in ["_MMU_AUTO_HOME", "_MMU_CLEAR_POSITION", "_MMU_PARK", "_MMU_RESTORE_POSITION", "_MMU_SAVE_POSITION", "_MMU_SET_LED", "_MMU_LED_ACTION_CHANGED", "_MMU_LED_GATE_MAP_CHANGED", "_MMU_LED_PRINT_STATE_CHANGED", "_MMU_TEST", "_MMU_CUT_TIP", "_MMU_FORM_TIP", "_MMU_ERROR_DIALOG", "_MMU_RUN_MARKERS"]: # Remove internal helpers
                        mmsg += "%s : %s\n" % (c.upper(), d)
                else:
                    smsg += "%s : %s\n" % (c.upper(), d)
        if testing:
            msg += tmsg
        if macros:
            msg += mmsg
        if steps:
            msg += smsg
        self._log_always(msg)

    cmd_MMU_ENCODER_help = "Display encoder position and stats or enable/disable runout detection logic in encoder"
    def cmd_MMU_ENCODER(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self._check_is_disabled(): return
        value = gcmd.get_float('VALUE', -1, minval=0.)
        enable = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        if enable == 1:
            self.encoder_sensor.set_mode(self.enable_clog_detection)
        elif enable == 0:
            self.encoder_sensor.set_mode(self.encoder_sensor.RUNOUT_DISABLED)
            return
        elif value >= 0.:
            self._set_encoder_distance(value)
            return
        status = self.encoder_sensor.get_status(0)
        msg = "Encoder position: %.1f" % status['encoder_pos']
        msg += "\nRunout detection: %s" % ("Enabled" if status['enabled'] else "Disabled")
        clog = "Automatic" if status['detection_mode'] == 2 else "On" if status['detection_mode'] == 1 else "Off"
        msg += "\nClog/Runout mode: %s (Detection length: %.1f)" % (clog, status['detection_length'])
        msg += "\nTrigger headroom: %.1f (Minimum observed: %.1f)" % (status['headroom'], status['min_headroom'])
        msg += "\nFlowrate: %d %%" % status['flow_rate']
        self._log_info(msg)

    cmd_MMU_LED_help = "Manage mode of operation of optional MMU LED's"
    def cmd_MMU_LED(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_has_leds(): return
        if self._check_is_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu_num_gates - 1)

        if reset:
            self.custom_color_rgb = [(0.,0.,0.)] * self.mmu_num_gates

        if gate is not None:
            self.custom_color_rgb[gate] = self._color_to_rgb(gcmd.get('COLOR', '000000'))
            quiet = True

        gcode_macro = self.printer.lookup_object("gcode_macro _MMU_SET_LED", None)
        gcode_vars = self.printer.lookup_object("gcode_macro _MMU_LED_VARS", gcode_macro)
        if gcode_macro:
            try:
                variables = gcode_vars.variables
                macro_variables = gcode_macro.variables
                current_led_enable = variables['led_enable']
                led_enable = bool(gcmd.get_int('ENABLE', current_led_enable, minval=0, maxval=1))
                default_exit_effect = gcmd.get('EXIT_EFFECT', variables['default_exit_effect'])
                default_entry_effect = gcmd.get('ENTRY_EFFECT', variables['default_entry_effect'])
                default_status_effect = gcmd.get('STATUS_EFFECT', variables['default_status_effect'])

                led_vars = {}
                led_vars['led_enable'] = led_enable
                led_vars['default_exit_effect'] = default_exit_effect
                led_vars['default_entry_effect'] = default_entry_effect
                led_vars['default_status_effect'] = default_status_effect
                if current_led_enable and not led_enable:
                    # Enabled to disabled
                    self._wrap_gcode_command("_MMU_SET_LED EXIT_EFFECT=off ENTRY_EFFECT=off STATUS_EFFECT=off")
                    gcode_vars.variables.update(led_vars)
                else:
                    gcode_vars.variables.update(led_vars)
                    self._wrap_gcode_command("_MMU_SET_LED EXIT_EFFECT=default ENTRY_EFFECT=default STATUS_EFFECT=default")

                if not quiet:
                    effect_string = lambda effect, enabled : ("'%s'" % effect) if enabled != -1 else "Unavailable"
                    msg = "LEDs are %s\n" % ("enabled" if led_enable else "disabled")
                    msg += "Default exit effect: %s\n" % effect_string(default_exit_effect, macro_variables['exit_first_led_index'])
                    msg += "Default entry effect: %s\n" % effect_string(default_entry_effect, macro_variables['entry_first_led_index'])
                    msg += "Default status effect: %s\n" % effect_string(default_status_effect, macro_variables['status_led_index'])
                    msg += "\nOptions:\nENABLE=[0|1]\nEXIT_EFFECT=[off|gate_status|filament_color|custom_color]\nENTRY_EFFECT=[off|gate_status|filament_color|custom_color]\nSTATUS_EFFECT=[off|on|filament_color|custom_color]"
                    self._log_always(msg)
            except Exception as e:
                # Probably/hopefully just means the macro is missing or been messed with
                self._log_error('Error communicating with the _MMU_SET_LED macro: %s' % str(e))
        else:
            self._log_error("LEDs not available")

    cmd_MMU_RESET_help = "Forget persisted state and re-initialize defaults"
    def cmd_MMU_RESET(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        confirm = gcmd.get_int('CONFIRM', 0, minval=0, maxval=1)
        if confirm != 1:
            self._log_always("You must re-run and add 'CONFIRM=1' to reset all state back to default")
            return
        self._initialize_state()
        self._reset_statistics()
        self.enable_endless_spool = self.default_enable_endless_spool
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool))
        self.endless_spool_groups = list(self.default_endless_spool_groups)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups))
        self.ttg_map = list(self.default_ttg_map)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map))
        self.gate_status = self._validate_gate_status(list(self.default_gate_status))
        self.gate_material = list(self.default_gate_material)
        self._update_gate_color(list(self.default_gate_color))
        self.gate_spool_id = list(self.default_gate_spool_id)
        self._persist_gate_map()
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_GATE_SELECTED, self.gate_selected))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_TOOL_SELECTED, self.tool_selected))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_FILAMENT_POS, self.filament_pos))
        self._log_always("MMU state reset")
        self._schedule_mmu_bootup_tasks()


#########################################################
# STEP FILAMENT LOAD/UNLOAD MACROS FOR USER COMPOSITION #
#########################################################

    cmd_MMU_TEST_FORM_TIP_help = "Convenience macro for calling the standalone tip forming functionality (or cutter logic)"
    def cmd_MMU_TEST_FORM_TIP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        show = bool(gcmd.get_int('SHOW', 0, minval=0, maxval=1))
        run = bool(gcmd.get_int('RUN', 1, minval=0, maxval=1))
        eject = bool(gcmd.get_int('EJECT', 0, minval=0, maxval=1))
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print syncing and current

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)
        gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.form_tip_macro, gcode_macro)

        if reset:
            if self.form_tip_vars is not None:
                gcode_vars.variables = dict(self.form_tip_vars)
                self.form_tip_vars = None
                self._log_always("Reset '%s' macro variables to defaults" % self.form_tip_macro)
            show = True

        if show:
            msg = "Variable settings for macro '%s':" % self.form_tip_macro
            for k, v in gcode_vars.variables.items():
                msg += "\nvariable_%s: %s" % (k, v)
            self._log_always(msg)
            return

        # Save restore point on first call
        if self.form_tip_vars is None:
            self.form_tip_vars = dict(gcode_vars.variables)

        for param in gcmd.get_command_parameters():
            value = gcmd.get(param)
            param = param.lower()
            if param.startswith("variable_"):
                self._log_always("Removing 'variable_' prefix from '%s' - not necessary" % param)
                param = param[9:]
            if param in gcode_vars.variables:
                gcode_vars.variables[param] = self._fix_type(value)
            elif param not in ["reset", "show", "run", "eject", "force_in_print"]:
                self._log_error("Variable '%s' is not defined for '%s' macro" % (param, self.form_tip_macro))

        # Run the macro in test mode (final_eject is set)
        msg = "Running macro '%s' with the following variable settings:" % self.form_tip_macro
        for k, v in gcode_vars.variables.items():
            msg += "\nvariable_%s: %s" % (k, v)
        self._log_always(msg)

        if run:
            self._ensure_safe_extruder_temperature(wait=False)
            # Mimick in print if requested
            self._sync_gear_to_extruder(self.sync_form_tip and self._is_in_print(force_in_print), servo=True, current=self._is_in_print(force_in_print))
            _,_,_ = self._do_form_tip(test=True)
            self._sync_gear_to_extruder(False, servo=True)

    cmd_MMU_STEP_LOAD_GATE_help = "User composable loading step: Move filament from gate to start of bowden"
    def cmd_MMU_STEP_LOAD_GATE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        try:
            self._load_gate()
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_GATE_help = "User composable unloading step: Move filament from start of bowden and park in the gate"
    def cmd_MMU_STEP_UNLOAD_GATE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        full = gcmd.get_int('FULL', 0)
        try:
            self._unload_gate(homing_max=self.calibrated_bowden_length if full else None)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_LOAD_BOWDEN_help = "User composable loading step: Smart loading of bowden"
    def cmd_MMU_STEP_LOAD_BOWDEN(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', self.calibrated_bowden_length)
        try:
            self._load_bowden(length)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_BOWDEN_help = "User composable unloading step: Smart unloading of bowden"
    def cmd_MMU_STEP_UNLOAD_BOWDEN(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        length = gcmd.get_float('LENGTH', self.calibrated_bowden_length)
        try:
            self._unload_bowden(length)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_HOME_EXTRUDER_help = "User composable loading step: Home to extruder sensor or entrance through collision detection"
    def cmd_MMU_STEP_HOME_EXTRUDER(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        try:
            self._home_to_extruder(self.extruder_homing_max)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_HOME_EXTRUDER: %s" % str(ee))

    cmd_MMU_STEP_LOAD_TOOLHEAD_help = "User composable loading step: Toolhead loading"
    def cmd_MMU_STEP_LOAD_TOOLHEAD(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        try:
            self._load_extruder(extruder_only)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_TOOLHEAD_help = "User composable unloading step: Toolhead unloading"
    def cmd_MMU_STEP_UNLOAD_TOOLHEAD(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0))
        park_pos = gcmd.get_float('PARK_POS', -self._get_filament_position()) # +ve value
        try:
            # Precautionary validation of filament position
            if park_pos > self.toolhead_extruder_to_nozzle:
                park_pos = self.toolhead_extruder_to_nozzle
            park_pos = min(0, park_pos)
            self._set_filament_position(-park_pos)

            self._unload_extruder(extruder_only = extruder_only)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_HOMING_MOVE_help = "User composable loading step: Generic homing move"
    def cmd_MMU_STEP_HOMING_MOVE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        try:
            self._homing_move_cmd(gcmd, "User defined step homing move")
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_HOMING_MOVE: %s" % str(ee))

    cmd_MMU_STEP_MOVE_help = "User composable loading step: Generic move"
    def cmd_MMU_STEP_MOVE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        try:
            self._move_cmd(gcmd, "User defined step move")
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_MOVE: %s" % str(ee))

    cmd_MMU_STEP_SET_FILAMENT_help = "User composable loading step: Set filament position state"
    def cmd_MMU_STEP_SET_FILAMENT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        state = gcmd.get_int('STATE', minval=self.FILAMENT_POS_UNKNOWN, maxval=self.FILAMENT_POS_LOADED)
        silent = gcmd.get_int('SILENT', 0)
        self._set_filament_pos_state(state, silent)


##############################################
# MODULAR FILAMENT LOAD AND UNLOAD FUNCTIONS #
##############################################

    # Load filament into gate. This is considered the "zero" positon for filament loading. Note that this
    # may overshoot for the "encoder" loading technique but subsequent bowden move will accommodate. Also
    # for systems with gate sensor and encoder with gate sensor first, there will be a gap in encoder readings
    # on next bowden move
    def _load_gate(self, allow_retry=True, adjust_servo_on_error=True):
        self._validate_gate_config("load")
        self._set_filament_direction(self.DIRECTION_LOAD)
        self._servo_down()
        retries = self.gate_load_retries if allow_retry else 1

        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            with self._require_encoder():
                measured = 0.
                for i in range(retries):
                    msg = "Initial load into encoder" if i == 0 else ("Retry load into encoder #%d" % i)
                    _,_,m,_ = self._trace_filament_move(msg, self.gate_homing_max)
                    measured += m
                    if (m) > 6.0:
                        self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], self.GATE_AVAILABLE)) # Don't reset if filament is buffered
                        self._set_filament_position(measured)
                        self._set_filament_pos_state(self.FILAMENT_POS_START_BOWDEN)
                        return
                    else:
                        self._log_debug("Error loading filament - not enough detected at encoder. %s" % ("Retrying..." if i < retries - 1 else ""))
                        if i < retries - 1:
                            self._track_gate_statistics('servo_retries', self.gate_selected)
                            self._servo_up()
                            self._servo_down()
        else:
            for i in range(retries):
                msg = "Initial homing to gate sensor" if i == 0 else ("Retry homing to gate sensor #%d" % i)
                actual,homed,measured,_ = self._trace_filament_move(msg, self.gate_homing_max, motor="gear", homing_move=1, endstop_name=self.ENDSTOP_GATE)
                if homed:
                    self._log_debug("Gate endstop reached after %.1fmm (measured %.1fmm)" % (actual, measured))
                    self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], self.GATE_AVAILABLE)) # Don't reset if filament is buffered
                    self._initialize_filament_position()
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_GATE)
                    return
                else:
                    self._log_debug("Error loading filament - did not find home. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self._track_gate_statistics('servo_retries', self.gate_selected)
                        self._servo_up()
                        self._servo_down()

        self._set_gate_status(self.gate_selected, self.GATE_EMPTY)
        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        if adjust_servo_on_error:
            self._servo_auto()
        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            raise MmuError("Error loading filament at gate - not enough movement detected at encoder")
        else:
            raise MmuError("Error loading filament at gate - gate endstop didn't trigger")

    # Unload filament through gate to final MMU park position.
    # Strategies include use of encoder or homing to gate endstop and then parking
    # Allows the overriding of homing_max for slow unloads when we are unsure of filament position
    def _unload_gate(self, homing_max=None):
        self._validate_gate_config("unload")
        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self._servo_down()
        full = homing_max == self.calibrated_bowden_length
        homing_max = homing_max or self.gate_homing_max

        # Safety step because this method is used as a defensive way to unload the entire bowden from unkown position
        # It handles the small window where filament is between extruder entrance and toolhead sensor
        if full and self._has_sensor(self.ENDSTOP_TOOLHEAD):
            length = self.toolhead_extruder_to_nozzle - self.toolhead_sensor_to_nozzle
            self._log_debug("Performing safety synced pre-unload bowden move")
            _,_,_,delta = self._trace_filament_move("Bowden pre-unload move", -length, motor="gear+extruder")
            homing_max -= length

        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            with self._require_encoder():
                self._log_debug("Slow unload of the encoder")
                max_steps = int(homing_max / self.encoder_move_step_size) + 5
                delta = 0.
                for i in range(max_steps):
                    msg = "Unloading step #%d from encoder" % (i+1)
                    _,_,_,sdelta = self._trace_filament_move(msg, -self.encoder_move_step_size)
                    delta += sdelta
                    # Large enough delta here means we are out of the encoder
                    if sdelta >= self.encoder_move_step_size * 0.2: # 20 %
                        park = self.gate_parking_distance - sdelta # will be between 8 and 20mm (for 23mm gate_parking_distance, 15mm step)
                        self._set_filament_position(self._get_filament_position() + delta)
                        _,_,measured,_ = self._trace_filament_move("Final parking", -park)
                        self._set_filament_position(self._get_filament_position() + park)
                        # We don't expect any movement of the encoder unless it is free-spinning
                        if measured > self.encoder_min: # We expect 0, but relax the test a little (allow one pulse)
                            self._log_info("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                        return
                self._log_debug("Filament did not clear encoder even after moving %.1fmm" % (self.encoder_move_step_size * max_steps))
        else:
            _,homed,_,_ = self._trace_filament_move("Reverse homing to gate sensor", -homing_max, motor="gear", homing_move=-1, endstop_name=self.ENDSTOP_GATE)
            if homed:
                self._set_filament_pos_state(self.FILAMENT_POS_HOMED_GATE)
                # Final parking step
                self._trace_filament_move("Final parking", -self.gate_parking_distance)
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                return
            else:
                self._log_debug("Did not home to gate sensor")

        raise MmuError("Unloading gate failed")

    # Shared gate functions to deduplicate logic
    def _validate_gate_config(self, direction):
        if self.gate_homing_endstop == self.ENDSTOP_ENCODER:
            if not self._has_encoder():
                raise MmuError("Attempting to %s encoder but encoder is not configured on MMU!" % direction)
        elif self.gate_homing_endstop == self.ENDSTOP_GATE:
            if not self._has_sensor(self.ENDSTOP_GATE):
                raise MmuError("Attempting to %s gate but gate sensor '%s' is not configured on MMU!" % (direction, self.ENDSTOP_GATE))
        else:
            raise MmuError("Unsupported gate endstop")

    # Fast load of filament in bowden, optionally to the end
    # Handles setting of cailbration ratio if not set
    def _load_bowden(self, length):
        if length <= 0: return
        if self.calibrated_bowden_length > 0 and not self.calibrating:
            length = min(length, self.calibrated_bowden_length)
        full = length == self.calibrated_bowden_length
        length -= self._get_filament_position()

        self._log_debug("Loading bowden tube")
        self._set_filament_direction(self.DIRECTION_LOAD)
        self._servo_down()
        tolerance = self.bowden_allowable_load_delta

        # See if we need to automatically set calibration ratio for this gate
        current_ratio = self.variables.get("%s%d" % (self.VARS_MMU_CALIB_PREFIX, self.gate_selected), None)
        reference_load = False
        if self.variable_gate_ratios:
            if self._can_use_encoder() and self.auto_calibrate_gates and self.gate_selected > 0 and not current_ratio and not self.calibrating:
                reference_load = True
            if current_ratio is None and not self.calibrating:
                self._log_info("Warning: Gate %d not calibrated! Using default 1.0 gear ratio!" % self.gate_selected)

        # "Fast" load
        _,_,_,delta = self._trace_filament_move("Course loading move into bowden", length, track=True, encoder_dwell=reference_load)
        delta -= self._get_encoder_dead_space()

        # Encoder based validation test
        if self._can_use_encoder() and delta >= length * (self.bowden_move_error_tolerance/100.) and not self.calibrating:
            raise MmuError("Failed to load bowden. Perhaps filament is stuck in gate. Gear moved %.1fmm, Encoder delta %.1fmm" % (length, delta))

        if reference_load:
            ratio = (length - delta) / length
            if ratio > 0.9 and ratio < 1.1:
                self.variables["%s%d" % (self.VARS_MMU_CALIB_PREFIX, self.gate_selected)] = ratio
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=%.6f" % (self.VARS_MMU_CALIB_PREFIX, self.gate_selected, ratio))
                self._log_always("Calibration ratio for Gate %d was missing. Value of %.6f has been automatically saved" % (self.gate_selected, ratio))
                self._set_gate_ratio(ratio)

        # Encoder based validation test
        elif self._can_use_encoder() and delta >= tolerance and not self.calibrating and current_ratio:
            # Correction attempts to load the filament according to encoder reporting
            if self.bowden_apply_correction:
                for i in range(2):
                    if delta >= tolerance:
                        msg = "Correction load move #%d into bowden" % (i+1)
                        _,_,_,delta = self._trace_filament_move(msg, delta, track=True)
                        self._log_debug("Correction load move was necessary, encoder now measures %.1fmm" % self._get_encoder_distance())
                    else:
                        break
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)
                if delta >= tolerance:
                    self._log_info("Warning: Excess slippage was detected in bowden tube load afer correction moves. Gear moved %.1fmm, Encoder delta %.1fmm. See mmu.log for more details"% (length, delta))
            else:
                self._log_info("Warning: Excess slippage was detected in bowden tube load but 'bowden_apply_correction' is disabled. Gear moved %.1fmm, Encoder delta %.1fmm. See mmu.log for more details" % (length, delta))

            if delta >= tolerance:
                self._log_debug("Possible causes of slippage:\nCalibration ref length too long (hitting extruder gear before homing)\nCalibration ratio for gate is not accurate\nMMU gears are not properly gripping filament\nEncoder reading is inaccurate\nFaulty servo")

        self._random_failure()
        self._movequeues_wait_moves()
        if full:
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
        elif not self.filament_pos == self.FILAMENT_POS_IN_BOWDEN:
            self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

    # Fast unload of filament from exit of extruder gear (end of bowden) to position close to MMU (gate_unload_buffer away)
    def _unload_bowden(self, length):
        if length <= 0: return
        if self.calibrated_bowden_length > 0 and not self.calibrating:
            length = min(length, self.calibrated_bowden_length)
        full = length == self.calibrated_bowden_length
        length -= self.gate_unload_buffer

        self._log_debug("Unloading bowden tube")
        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self._servo_down()
        tolerance = self.bowden_allowable_unload_delta

        # Optional safety step
        if full and self._has_encoder() and self.bowden_pre_unload_test and not (self._check_sensor(self.ENDSTOP_EXTRUDER) is False) and not (self._check_sensor(self.ENDSTOP_GATE) is False):
            with self._require_encoder():
                self._log_debug("Performing bowden pre-unload test")
                _,_,_,delta = self._trace_filament_move("Bowden pre-unload test", -self.encoder_move_step_size)
                if delta > self.encoder_move_step_size * (self.bowden_pre_unload_error_tolerance/100.):
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
                    raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder")
                length -= self.encoder_move_step_size
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

        # "Fast" unload
        if not (self._check_sensor(self.ENDSTOP_GATE) is False):
            _,_,_,delta = self._trace_filament_move("Course unloading move from bowden", -length, track=True)
            delta -= self._get_encoder_dead_space()

            # Encoder based validation test
            if self._can_use_encoder() and delta >= tolerance and not self.calibrating:
                # Only a warning because _unload_gate() will deal with it
                self._log_info("Warning: Excess slippage was detected in bowden tube unload. Gear moved %.1fmm, Encoder delta %.1fmm" % (length, delta))

        self._random_failure()
        self._movequeues_wait_moves()
        if full:
            self._set_filament_pos_state(self.FILAMENT_POS_START_BOWDEN)
        elif not self.filament_pos == self.FILAMENT_POS_IN_BOWDEN:
            self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

    # Optionally home filament to designated homing location at the extruder
    def _home_to_extruder(self, max_length):
        self._set_filament_direction(self.DIRECTION_LOAD)
        self._servo_down()
        measured = 0

        if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION:
            if self._has_encoder():
                actual,homed,measured,_ = self._home_to_extruder_collision_detection(max_length)
            else:
                raise MmuError("Attempting to home to extruder using 'collision' endstop but encoder is not configured on MMU!")

        elif self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_NONE:
            homed = True

        else:
            self._log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (self.extruder_homing_endstop, max_length))
            actual,homed,measured,_ = self._trace_filament_move("Homing filament to extruder endstop", max_length, motor="gear", homing_move=1, endstop_name=self.extruder_homing_endstop)
            if homed:
                self._log_debug("Extruder endstop reached after %.1fmm (measured %.1fmm)" % (actual, measured))
                self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY)

                # Move the little bit more to reach extruder entrance if we homed to entry sensor
                # We do this here to allow _load_extruder() to work with "extruder_only" option
                if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER:
                    _,_,measured,_ = self._trace_filament_move("Aligning filament to extruder gear", self.toolhead_entry_to_extruder, motor="gear")

        if not homed:
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder after moving %.1fmm" % max_length)

        if measured > (max_length * 0.8):
            self._log_info("Warning: 80%% of 'extruder_homing_max' was used homing. You may want to adjust your calibrated bowden length ('%s') or increase 'extruder_homing_max'" % self.VARS_MMU_CALIB_BOWDEN_LENGTH)

        self._set_filament_pos_state(self.FILAMENT_POS_HOMED_EXTRUDER)

    # Special extruder homing option for detecting the collision base on lack of encoder movement
    def _home_to_extruder_collision_detection(self, max_length):
        # Lock the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        ge = stepper_enable.lookup_enable(self.mmu_extruder_stepper.stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        step = self.extruder_collision_homing_step * math.ceil(self.encoder_resolution * 10) / 10
        self._log_debug("Homing to extruder gear, up to %.1fmm in %.1fmm steps" % (max_length, step))

        with self._wrap_gear_current(self.extruder_homing_current, "for collision detection"):
            homed = False
            measured = delta = 0.
            for i in range(int(max_length / step)):
                msg = "Homing step #%d" % (i+1)
                _,_,smeasured,sdelta = self._trace_filament_move(msg, step, speed=self.gear_homing_speed)
                measured += smeasured
                delta += sdelta
                if sdelta >= self.encoder_min or abs(delta) > step: # Not enough or strange measured movement means we've hit the extruder
                    homed = True
                    measured -= step # Subtract the last step to improve accuracy
                    break
            self._log_debug("Extruder entrance%s found after %.1fmm move (%d steps), encoder measured %.1fmm (delta %.1fmm)"
                    % (" not" if not homed else "", step*(i+1), i+1, measured, delta))

        if delta > 5.0:
            self._log_info("Warning: A lot of slippage was detected whilst homing to extruder, you may want to reduce 'extruder_homing_current' and/or ensure a good grip on filament by gear drive")

        self._set_filament_position(self._get_filament_position() - step) # Ignore last step movement
        return step*i, homed, measured, delta

    # Move filament from the extruder gears (entrance) to the nozzle
    def _load_extruder(self, extruder_only=False):
        with self._wrap_action(self.ACTION_LOADING_EXTRUDER):
            self._log_debug("Loading filament into extruder")
            self._set_filament_direction(self.DIRECTION_LOAD)

            # Important to wait for filaments with wildy different print temps. In practice, the time taken
            # to perform a swap should be adequate to reach the target temp but better safe than sorry
            self._ensure_safe_extruder_temperature(wait=True)

            synced = not extruder_only
            if synced:
                self._servo_down()
                speed = self.extruder_sync_load_speed
                motor = "gear+extruder"
            else:
                self._servo_up()
                speed = self.extruder_load_speed
                motor = "extruder"

            fhomed = False
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                # With toolhead sensor we always first home to toolhead sensor past the extruder entrance
                if self._check_sensor(self.ENDSTOP_TOOLHEAD):
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")
                self._log_debug("Homing up to %.1fmm to toolhead sensor%s" % (self.toolhead_homing_max, (" (synced)" if synced else "")))
                _,fhomed,measured,_ = self._trace_filament_move("Homing to toolhead sensor", self.toolhead_homing_max, motor=motor, homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
                if fhomed:
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                else:
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure reduction is used only one time
            d = self.toolhead_sensor_to_nozzle if self._has_sensor(self.ENDSTOP_TOOLHEAD) else self.toolhead_extruder_to_nozzle
            length = max(d - self.filament_remaining - self.toolhead_ooze_reduction, 0)
            self.filament_remaining = 0.
            self._log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self._trace_filament_move("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)

            # Encoder based validation test
            if self._can_use_encoder() and not fhomed:
                self._log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                if measured < self.encoder_min:
                    raise MmuError("Move to nozzle failed (encoder didn't sense any movement). Extruder may not have picked up filament or filament did not home correctly")
                elif delta > length * (self.toolhead_move_error_tolerance/100.):
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
                    raise MmuError("Move to nozzle failed (encoder didn't sense sufficient movement). Extruder may not have picked up filament or filament did not home correctly")

            self._random_failure()
            self._movequeues_wait_moves()
            self._set_filament_pos_state(self.FILAMENT_POS_LOADED)
            self._log_debug("Filament should loaded to nozzle")

    # Extract filament past extruder gear (to end of bowden). Assume that tip has already been formed
    # and we are parked somewhere in the extruder either by slicer or by stand alone tip creation
    # But be careful:
    #   A poor tip forming routine or slicer could have popped the filament out of the extruder already
    # Ending point is either the exit of the extruder or at the extruder (entry) endstop if fitted
    def _unload_extruder(self, extruder_only=False, validate=True):
        with self._wrap_action(self.ACTION_UNLOADING_EXTRUDER):
            self._log_debug("Extracting filament from extruder")
            self._set_filament_direction(self.DIRECTION_UNLOAD)
            self._ensure_safe_extruder_temperature(wait=False)

            synced = self.servo_state == self.SERVO_DOWN_STATE and not extruder_only
            if synced:
                self._servo_down()
                speed = self.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                self._servo_up()
                speed = self.extruder_unload_speed
                motor = "extruder"

            fhomed = False
            if self._has_sensor(self.ENDSTOP_EXTRUDER) and not extruder_only:
                # BEST Strategy: Extruder exit movement leveraging extruder entry sensor. Must be synced
                synced = True
                self._servo_down()
                speed = self.extruder_sync_unload_speed
                motor = "gear+extruder"

                if not self._check_sensor(self.ENDSTOP_EXTRUDER):
                    self._log_info("Warning: Filament was not detected by extruder (entry) sensor at start of extruder unload")
                    fhomed = True # Assumption
                else:
                    hlength = self.toolhead_extruder_to_nozzle + self.toolhead_entry_to_extruder + self.toolhead_unload_safety_margin
                    self._log_debug("Reverse homing up to %.1fmm to extruder sensor (synced) to exit extruder" % hlength)
                    _,fhomed,_,_ = self._trace_filament_move("Reverse homing to extruder sensor", -hlength, motor=motor, homing_move=-1, endstop_name=self.ENDSTOP_EXTRUDER)

                if not fhomed:
                    raise MmuError("Failed to reach extruder entry sensor after moving %.1fmm" % hlength)
                else:
                    validate = False
                    # We know exactly where end of filament is so true up
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_ENTRY)
                    self._set_filament_position(-(self.toolhead_extruder_to_nozzle + self.toolhead_entry_to_extruder))

                # Extra pedantic validation if we have toolhead sensor
                if self._check_sensor(self.ENDSTOP_TOOLHEAD):
                    raise MmuError("Toolhead sensor still reports filament is present in toolhead! Possible sensor malfunction")

            else:
                if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                    # NEXT BEST: With toolhead sensor we first home to toolhead sensor. Optionally synced
                    if not self._check_sensor(self.ENDSTOP_TOOLHEAD):
                        self._log_info("Warning: Filament was not detected in extruder by toolhead sensor at start of extruder unload")
                        fhomed = True # Assumption
                    else:
                        hlength = self.toolhead_sensor_to_nozzle + self.toolhead_unload_safety_margin
                        self._log_debug("Reverse homing up to %.1fmm to toolhead sensor%s" % (hlength, (" (synced)" if synced else "")))
                        _,fhomed,_,_ = self._trace_filament_move("Reverse homing to toolhead sensor", -hlength, motor=motor, homing_move=-1, endstop_name=self.ENDSTOP_TOOLHEAD)
                    if not fhomed:
                        raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % hlength)
                    else:
                        validate = False
                        # We know exactly where end of filament is so true up
                        self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                        self._set_filament_position(-self.toolhead_sensor_to_nozzle)

                # Finish up with regular extruder exit movement. Optionally synced
                length = max(0, self.toolhead_extruder_to_nozzle + self._get_filament_position()) + self.toolhead_unload_safety_margin
                self._log_debug("Unloading last %.1fmm to exit the extruder%s" % (length, " (synced)" if synced else ""))
                _,_,measured,delta = self._trace_filament_move("Unloading extruder", -length, speed=speed, motor=motor, wait=True)

                # Best guess of filament position is right at extruder entrance or just beyond if synced
                if synced:
                    self._set_filament_position(-(self.toolhead_extruder_to_nozzle + self.toolhead_unload_safety_margin))
                else:
                    self._set_filament_position(-self.toolhead_extruder_to_nozzle)

                # Encoder based validation test if it has high chance of being useful
                # NOTE: This check which use to raise MmuError() is triping many folks up because they have poor tip forming
                #       logic so just log error and continue. This disguises the root cause problem but will make folks happier
                #       Not performed for slicer tip forming (validate=True) because everybody is ejecting the filament!
                if validate and self._can_use_encoder() and length > self.encoder_move_step_size:
                    self._log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                    msg = None
                    if measured < self.encoder_min:
                        msg = "any"
                    elif synced and delta > length * (self.toolhead_move_error_tolerance/100.):
                        msg = "suffient"
                    if msg:
                        self._log_error("Encoder not sensing %s movement:\nConcluding filament either stuck in the extruder or tip forming erroneously completely ejected filament" % msg)
                        self._log_info("Will attempt to continue...")
                self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)

            self._random_failure()
            self._movequeues_wait_moves()
            self._log_debug("Filament should be out of extruder")


##############################################
# LOAD / UNLOAD SEQUENCES AND FILAMENT TESTS #
##############################################

    def _load_sequence(self, length=None, skip_extruder=False, extruder_only=False):
        self._movequeues_wait_moves()
        self._log_info("Loading %s..." % ("extruder" if extruder_only else "filament"))
        full = home = False
        if not length:
            length = self.calibrated_bowden_length
        self._set_filament_direction(self.DIRECTION_LOAD)
        self._initialize_filament_position(dwell=None)    # Encoder 0000

        if not extruder_only:
            self._display_visual_state()
            if (length >= self.calibrated_bowden_length):
                if (length > self.calibrated_bowden_length):
                    length = self.calibrated_bowden_length
                    self._log_info("Restricting load length to extruder calibration reference of %.1fmm" % length)
                full = True
                self._track_time_start('load')
                home = self._must_home_to_extruder()
            else:
                skip_extruder = True
            current_action = self._set_action(self.ACTION_LOADING)

        try:
            # Note: Conditionals deliberately coded this way to match macro alternative
            start_filament_pos = self.filament_pos
            if self.gcode_load_sequence:
                self._log_debug("Calling external user defined loading sequence macro")
                self._wrap_gcode_command("_MMU_LOAD_SEQUENCE FILAMENT_POS=%d LENGTH=%.1f FULL=%d HOME_EXTRUDER=%d SKIP_EXTRUDER=%d EXTRUDER_ONLY=%d" % (start_filament_pos, length, int(full), int(home), int(skip_extruder), int(extruder_only)), exception=True)

            elif extruder_only:
                if start_filament_pos < self.FILAMENT_POS_EXTRUDER_ENTRY:
                    self._load_extruder(extruder_only=True)
                else:
                    self._log_debug("Assertion failure: Unexpected state %d in _load_sequence(extruder_only=True)" % start_filament_pos)
                    raise MmuError("Cannot load extruder because already in extruder. Unload first")

            elif start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                self._log_debug("Assertion failure: Unexpected state %d in _load_sequence()" % start_filament_pos)
                raise MmuError("Cannot load because already in extruder. Unload first")

            else:
                if start_filament_pos <= self.FILAMENT_POS_UNLOADED:
                    self._load_gate()

                if start_filament_pos < self.FILAMENT_POS_END_BOWDEN:
                    self._load_bowden(length)

                if start_filament_pos < self.FILAMENT_POS_HOMED_EXTRUDER and home:
                    self._home_to_extruder(self.extruder_homing_max)

                if not skip_extruder:
                    self._load_extruder()

            self._movequeues_wait_moves()
            msg = "Load of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                msg += " (encoder measured %.1fmm)" % self._get_encoder_distance(dwell=None)
            self._log_info(msg)
        except MmuError as ee:
            if full:
                self._track_gate_statistics('load_failures', self.gate_selected)
            raise MmuError("Load sequence failed: %s" % (str(ee)))
        finally:
            if full:
                self._track_time_end('load')
            if not extruder_only:
                self._set_action(current_action)
            if not self._is_printing():
                self._servo_up()

    def _unload_sequence(self, length=None, check_state=False, skip_tip=False, extruder_only=False, runout=False):
        self._movequeues_wait_moves()
        if not length:
            length = self.calibrated_bowden_length
        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self._initialize_filament_position(dwell=None)    # Encoder 0000

        if check_state or self.filament_pos == self.FILAMENT_POS_UNKNOWN:
            # Let's determine where filament is and reset state before continuing
            self._recover_filament_pos(message=True)

        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            self._log_debug("Filament already ejected")
            self._servo_auto()
            return

        try:
            self._log_info("Unloading %s..." % ("extruder" if extruder_only else "filament"))
            if not extruder_only:
                current_action = self._set_action(self.ACTION_UNLOADING)
                self._track_time_start('unload')
                self._display_visual_state()

            park_pos = 0.
            if skip_tip and not runout:
                # Slicer was responsible for the tip, but the user must sent the slicer_tip_park_pos
                park_pos = self.slicer_tip_park_pos
                self._set_filament_position(-park_pos)
                if park_pos == 0.:
                    self._log_error("Tip forming performed by slicer but 'slicer_tip_park_pos' not set")
                else:
                    self._log_debug("Tip forming performed by slicer, park_pos set to %.1fmm" % park_pos)

            elif self.filament_pos >= self.FILAMENT_POS_IN_EXTRUDER or runout:
                # Extruder only in runout case to give filament best chance to reach gear
                detected = self._form_tip_standalone(extruder_only=(extruder_only or runout))
                park_pos = self._get_filament_position()

                if runout:
                    if self._check_pre_gate_sensor(self.gate_selected) is False or self._check_sensor(self.ENDSTOP_GATE) is False or (self._has_encoder() and self._get_encoder_distance() == 0):
                        self._log_info("Warning: Filament not seen at MMU after after tip forming move. Unload may not be possible")

                self._wrap_gcode_command(self.post_form_tip_macro, exception=True)

            # Note: Conditionals deliberately coded this way to match macro alternative
            start_filament_pos = self.filament_pos
            unload_to_buffer = (start_filament_pos >= self.FILAMENT_POS_END_BOWDEN and not extruder_only)
            if self.gcode_unload_sequence:
                self._log_debug("Calling external user defined unloading sequence macro")
                self._wrap_gcode_command("_MMU_UNLOAD_SEQUENCE FILAMENT_POS=%d LENGTH=%.1f EXTRUDER_ONLY=%d PARK_POS=%.1f" % (start_filament_pos, length, extruder_only, park_pos), exception=True)

            elif extruder_only:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    self._unload_extruder(extruder_only=True, validate=not skip_tip)
                else:
                    self._log_debug("Assertion failure: Unexpected state %d in _unload_sequence(extruder_only=True)" % start_filament_pos)
                    raise MmuError("Cannot unload extruder because filament not in extruder!")

            elif start_filament_pos == self.FILAMENT_POS_UNLOADED:
                self._log_debug("Assertion failure: Unexpected state %d in _unload_sequence()" % start_filament_pos)
                raise MmuError("Cannot unload because already unloaded!")

            else:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload encoder
                    self._unload_extruder(validate=not skip_tip)

                if start_filament_pos >= self.FILAMENT_POS_END_BOWDEN:
                    # Fast unload of bowden, then unload encoder
                    self._unload_bowden(length)
                    self._unload_gate()

                elif start_filament_pos >= self.FILAMENT_POS_HOMED_GATE:
                    # Have to do slow unload because we don't know exactly where we are
                    self._unload_gate(homing_max=length) # Full slow unload

            if unload_to_buffer and self.gate_status[self.gate_selected] != self.GATE_EMPTY:
                self._set_gate_status(self.gate_selected, self.GATE_AVAILABLE_FROM_BUFFER)

            # If runout then over unload to prevent accidental reload
            if runout and self.endless_spool_final_eject > 0.:
                self._log_info("Ejecting filament from MMU...")
                _,_,measured,_ = self._trace_filament_move("EndlessSpool final eject", -self.endless_spool_final_eject)

            # Encoder based validation test
            if self._can_use_encoder():
                movement = self._servo_up(measure=True)
                if movement > self.encoder_min:
                    self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
                    raise MmuError("It may be time to get the pliers out! Filament appears to stuck somewhere")
            else:
                self._servo_up()

            msg = "Unload of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                msg += " (encoder measured %.1fmm)" % self._get_encoder_distance(dwell=False)
            self._log_info(msg)

        except MmuError as ee:
            if not extruder_only:
                self._track_gate_statistics('unload_failures', self.gate_selected)
            raise MmuError("Unload sequence failed: %s" % (str(ee)))

        finally:
            if not extruder_only:
                self._track_time_end('unload')
                self._set_action(current_action)

    # This is a recovery routine to determine the most conservative location of the filament for unload purposes
    def _recover_filament_pos(self, strict=False, message=False):
        if message:
            self._log_info("Attempting to recover filament position...")
        ts = self._check_sensor(self.ENDSTOP_TOOLHEAD)
        if ts is None: # Not installed
            if self._check_filament_in_mmu():
                if self._check_filament_still_in_extruder():
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
                else:
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN) # This prevents fast unload move
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        elif ts: # Filament detected in toolhead
            self._set_filament_pos_state(self.FILAMENT_POS_LOADED)
        else: # Filament not detected in toolhead
            if self._check_filament_in_mmu():
                if self.strict_filament_recovery or strict:
                    if self._check_filament_still_in_extruder():
                        self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
                    else:
                        self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)
                else:
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN) # Slight risk of it still being gripped by extruder
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)

    # Retract the filament by the extruder stepper only and see if we do not have any encoder movement
    # This assumes that we already tip formed, and the filament is parked somewhere in the extruder
    # Return if filament detected and distance filament moved during test
    def _test_filament_in_extruder_by_retracting(self, length=None):
        with self._require_encoder():
            self._log_debug("Testing for filament in extruder by retracting on extruder stepper only")
            if not length:
                length = self.encoder_move_step_size
            self._servo_up()
            _,_,measured,_ = self._trace_filament_move("Moving extruder to test for filament exit", -length, speed=self.extruder_unload_speed, motor="extruder")
            detected = measured > self.encoder_min
            self._log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
            return detected, measured

    # Form tip prior to extraction from the extruder. This can take the form of shaping the filament or could simply
    # activate a filament cutting mechanism. Sets filament position based on park pos
    # Returns True if filament is detected
    def _form_tip_standalone(self, extruder_only=False):
        self._movequeues_wait_moves()

        # Pre check to validate the presence of filament in the extruder and case where we don't need to form tip
        if self._check_sensor(self.ENDSTOP_EXTRUDER) or self._check_sensor(self.ENDSTOP_TOOLHEAD):
            filament_initially_present = True
        else:
            # Only the "extruder" sensor can definitely answer but believe toolhead if that is all we have
            filament_initially_present = self._check_sensor(self.ENDSTOP_EXTRUDER)
            if filament_initially_present is None:
                filament_initially_present = self._check_sensor(self.ENDSTOP_TOOLHEAD)

        if filament_initially_present is False:
            self._log_debug("Tip forming skipped because no filament was detected")

            if self.filament_pos == self.FILAMENT_POS_LOADED:
                self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
            else:
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

            self._set_filament_position(-self.toolhead_extruder_to_nozzle)
            return False

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.form_tip_macro)

        with self._wrap_action(self.ACTION_FORMING_TIP):
            synced = self.sync_form_tip and not extruder_only
            self._sync_gear_to_extruder(synced, servo=True, current=False)
            self._ensure_safe_extruder_temperature(wait=False)

            # Perform the tip forming move and establish park_pos
            initial_encoder_position = self._get_encoder_distance()
            park_pos, self.filament_remaining, reported = self._do_form_tip()
            measured = self._get_encoder_distance(dwell=None) - initial_encoder_position

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
            self._set_encoder_distance(initial_encoder_position + park_pos)

            if detected:
                # Definitely in extruder
                self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
            else:
                # No detection. Best to assume we are somewhere in bowden for defensive unload
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

            return detected

    def _do_form_tip(self, test=False):
        with self._wrap_extruder_current(self.extruder_form_tip_current, "for tip forming move"):
            initial_extruder_position = self.mmu_extruder_stepper.stepper.get_commanded_position()
            initial_encoder_position = self._get_encoder_distance()

            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, "_MMU_FORM_TIP")
            try:
                initial_pa = self.printer.lookup_object(self.extruder_name).get_status(0)['pressure_advance'] # Capture PA in case user's tip forming resets it
                self._log_info("Forming tip...")
                self._wrap_gcode_command("%s%s" % (self.form_tip_macro, " FINAL_EJECT=1" if test else ""), exception=True)
            finally:
                self._movequeues_wait_moves()
                self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f" % initial_pa) # Restore PA

            stepper_movement = initial_extruder_position - self.mmu_extruder_stepper.stepper.get_commanded_position()
            measured = self._get_encoder_distance(dwell=None) - initial_encoder_position
            park_pos = gcode_macro.variables.get("output_park_pos", -1)
            try:
                park_pos = float(park_pos)
            except ValueError as e:
                self._log_error("Reported `output_park_pos: %s` could not be parsed: %s" % (park_pos, str(e)))
                park_pos = -1

            if park_pos < 0:
                # Use stepper movement
                reported = False
                filament_remaining = 0.
                park_pos = stepper_movement
                msg = "After tip formation, extruder moved (park_pos): %.1fmm, encoder measured %.1fmm" % (park_pos, measured)
                if test:
                    self._log_always(msg)
                else:
                    self._log_trace(msg)
            else:
                # Means the macro reported it (usually for filament cutting)
                reported = True
                filament_remaining = park_pos - stepper_movement
                msg = "After tip formation, park_pos reported as: %.1fmm with %.1fmm filament remaining in extruder (extruder moved: %.1fmm, encoder measured %.1fmm)" % (park_pos, filament_remaining, stepper_movement, measured)
                if test:
                    self._log_always(msg)
                else:
                    self._log_trace(msg)

            if not test and park_pos > self.toolhead_extruder_to_nozzle:
                self._log_error("Warning: Park_pos (%.1fmm) cannot be greater than 'toolhead_extruder_to_nozzle' distance of %.1fmm! Assumming fully unloaded from extruder" % (park_pos, self.toolhead_extruder_to_nozzle))
                park_pos = self.toolhead_extruder_to_nozzle
                filament_remaining = 0.

        return park_pos, filament_remaining, reported


#################################
# SELECTOR MOVEMENT AND CONTROL #
#################################

    def _home(self, tool = -1, force_unload = -1):
        if self.virtual_selector: return
        if self._check_in_bypass(): return
        with self._wrap_action(self.ACTION_HOMING):
            self._log_info("Homing MMU...")

            if force_unload != -1:
                self._log_debug("(asked to %s)" % ("force unload" if force_unload == 1 else "not unload"))
            if force_unload == 1:
                # Forced unload case for recovery
                self._unload_sequence(check_state=True)
            elif force_unload == -1 and self.filament_pos != self.FILAMENT_POS_UNLOADED:
                # Automatic unload case
                self._unload_sequence()

            self._unselect_tool()
            self._home_selector()
            if tool >= 0:
                self._select_tool(tool)

    def _home_selector(self):
        self.is_homed = False
        self.gate_selected = self.TOOL_GATE_UNKNOWN
        self._servo_move()
        self._movequeues_wait_moves()
        homing_state = MmuHoming(self.printer, self.mmu_toolhead)
        homing_state.set_axes([0])
        try:
            self.mmu_kinematics.home(homing_state)
            self.is_homed = True
        except Exception as e: # Homing failed
            self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))
        self.last_selector_move_time = self.estimated_print_time(self.reactor.monotonic())

    def _position_selector(self, target):
        if not self.selector_touch:
            self._trace_selector_move("Positioning selector", target)
        else:
            init_pos = self.mmu_toolhead.get_position()[0]
            halt_pos, successful = self._attempt_selector_touch_move(target)
            travel = abs(init_pos - halt_pos)
            if not successful:
                if travel < 3.5: # Filament stuck in the current selector
                    self._log_info("Selector is blocked by inside filament, trying to recover...")
                    msg = "Resetting selector by a distance of: %.1fmm" % -travel
                    self._trace_selector_move(msg, init_pos) # Realign selector

                    # See if we can detect filament in the encoder
                    found = self._check_filament_at_gate()
                    if not found:
                        # Try to engage filament to the encoder
                        _,_,measured,delta = self._trace_filament_move("Trying to re-enguage encoder", 45.)
                        if measured < self.encoder_min:
                            raise MmuError("Selector recovery failed. Path is probably internally blocked and unable to move filament to clear")

                    # Now try a full unload sequence
                    try:
                        self._unload_sequence(check_state=True)
                    except MmuError as ee:
                        # Add some more context to the error and re-raise
                        raise MmuError("Selector recovery failed because: %s" % (str(ee)))

                    # Ok, now check if selector can now reach proper target
                    self._home_selector()
                    successful, halt_pos = self._attempt_selector_touch_move(target)
                    if not successful:
                        # Selector path is still blocked
                        self.is_homed = False
                        self._unselect_tool()
                        raise MmuError("Selector recovery failed. Path is probably internally blocked")
                else: # Selector path is blocked, probably not internally
                    self.is_homed = False
                    self._unselect_tool()
                    raise MmuError("Selector path is probably externally blocked")

    def _attempt_selector_touch_move(self, target):
        halt_pos,homed = self._trace_selector_move("Attempting selector 'touch' movement", target, homing_move=1, endstop_name=self.ENDSTOP_SELECTOR_TOUCH)
        delta = abs(target - halt_pos)
        if delta <= 1.5:
            msg = "Truing selector %.1fmm to %.1fmm" % (delta, target)
            halt_pos,_ = self._trace_selector_move(msg, target)
            return halt_pos, True
        else:
            return halt_pos, False

    # Raw wrapper around all selector moves except homing
    # Returns position after move, if homed (homing moves), trigger position (homing moves)
    def _trace_selector_move(self, trace_str, new_pos, speed=None, homing_move=0, endstop_name=None, wait=True):
        if trace_str:
            self._log_trace(trace_str)
        speed = speed or self.selector_move_speed
        accel = self.mmu_toolhead.get_selector_limits()[1]
        pos = self.mmu_toolhead.get_position()
        homed = False

        if homing_move != 0:
            self._log_stepper("SELECTOR: position=%.1f, speed=%.1f, accel=%.1f homing_move=%d, endstop_name=%s" % (new_pos, speed, accel, homing_move, endstop_name))

            # Klipper generates TTC errors for tiny homing moves!
            if abs(new_pos - pos[0]) < 0.01: # Workaround for Timer Too Close error with short homing moves
                self._log_trace("Warning: short homing move detected on selector - ignored")
                return pos[0], homed

            # Check for valid endstop
            endstop = self.selector_rail.get_extra_endstop(endstop_name) if endstop_name is not None else self.selector_rail.get_endstops()
            if endstop is None:
                self._log_error("Endstop '%s' not found on selector rail" % endstop_name)
                return pos[0], homed

            # Don't allow stallguard home moves in rapid succession (TMC limitation)
            if endstop and self.selector_rail.is_endstop_virtual(endstop_name):
                current_time = self.estimated_print_time(self.reactor.monotonic())
                time_since_last = self.last_selector_move_time + 1.0 - current_time # 1 sec recovery time
                if (time_since_last) > 0:
                    self._log_trace("Waiting %.2f seconds before next touch move" % time_since_last)
                    self._movequeues_dwell(time_since_last, toolhead=False)

            hmove = HomingMove(self.printer, endstop, self.mmu_toolhead)
            try:
                trig_pos = [0., 0., 0., 0.]
                pos[0] = new_pos
                trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                homed = True
                if self.selector_rail.is_endstop_virtual(endstop_name):
                    # Stallguard doesn't do well at slow speed. Try to infer move completion
                    if abs(trig_pos[0] - new_pos) < 1.0:
                        homed = False
            except self.printer.command_error as e:
                homed = False
            finally:
                pos = self.mmu_toolhead.get_position()
                self._log_stepper("SELECTOR: halt_pos=%.1f, homed=%s, trig_pos=%.1f" % (pos[0], homed, trig_pos[0]))

        else:
            self._log_stepper("SELECTOR: position=%.1f, speed=%.1f, accel=%.1f" % (new_pos, speed, accel))
            pos[0] = new_pos
            self.mmu_toolhead.move(pos, speed)
            if wait:
                self._movequeues_wait_moves(toolhead=False)

        self.last_selector_move_time = self.estimated_print_time(self.reactor.monotonic())
        return pos[0], homed

    def _set_selector_pos(self, new_pos):
        pos = self.mmu_toolhead.get_position()
        pos[0] = new_pos
        self.mmu_toolhead.set_position(pos, homing_axes=(0,))
        self.is_homed = True
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_toolhead.get_last_move_time())

    def _measure_to_home(self):
        selector_steps = self.selector_stepper.get_step_dist()
        init_mcu_pos = self.selector_stepper.get_mcu_position()
        found_home = False
        try:
            homing_state = MmuHoming(self.printer, self.mmu_toolhead)
            homing_state.set_axes([0])
            self.mmu_kinematics.home(homing_state)
            found_home = True
        except Exception as e:
            pass # Home not found
        mcu_position = self.selector_stepper.get_mcu_position()
        traveled = abs(mcu_position - init_mcu_pos) * selector_steps
        return traveled, found_home


#################################
# FILAMENT MOVEMENT AND CONTROL #
#################################

    # Convenience wrapper around all gear and extruder motor movement that tracks measured movement and create trace log entry
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
    # 'sync' will synchronize the MMU toolhead and Printer toolhead before move
    # 'wait' will wait on appropriate move queue(s) after completion of move (forced to True if need encoder reading)
    # 'measure' whether we need to wait and measure encoder for movement
    # 'encoder_dwell' delay some additional time to ensure we have accurate encoder reading (if encoder fitted and required for measuring)
    #
    # All moves return: actual (relative), homed, measured, delta; mmu_toolhead.get_position[1] holds absolute position
    #
    def _trace_filament_move(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0, endstop_name="default",
			track=False, sync=False, wait=False, encoder_dwell=False):
        self._sync_gear_to_extruder(False) # Safety - ensure we reset extruder syncing
        encoder_start = self._get_encoder_distance(dwell=encoder_dwell)
        pos = self.mmu_toolhead.get_position()
        ext_pos = self.toolhead.get_position()
        homed = False
        actual = dist
        delta = 0.
        null_rtn = (0., False, 0., 0.)

        if homing_move != 0:
            # Klipper generates TTC errors for tiny homing moves!
            if abs(dist) < 0.01: # Workaround for Timer Too Close error with short homing moves
                self._log_trace("Warning: short homing move detected on gear - ignored")
                return null_rtn

            # Check for valid endstop
            endstop = self.gear_rail.get_extra_endstop(endstop_name) if endstop_name is not None else self.gear_rail.get_endstops()
            if endstop is None:
                self._log_error("Endstop '%s' not found" % endstop_name)
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
            self._log_error("Assertion failure: Invalid motor specification '%'" % motor)
            return null_rtn

        # Apply pre-gate speed override
        if self.gate_selected >= 0:
            adjust = self.gate_speed_override[self.gate_selected] / 100.
            speed *= adjust
            accel *= adjust

        if sync:
            self._movequeues_sync()

        # Gear rail is driving the filament
        if motor in ["gear", "gear+extruder", "extruder"]:
            with self._wrap_sync_extruder_to_gear(motor in ["gear+extruder", "extruder"], extruder_only=(motor == "extruder")):
                if homing_move != 0:
                    self._log_stepper("%s HOME: dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, sync=%s, wait=%s"% (motor.upper(), dist, speed, accel, endstop_name, sync, wait))
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstop, self.mmu_toolhead)
                    init_pos = pos[1]
                    pos[1] += dist
                    got_comms_timeout = False # HACK: Logic to try to mask CANbus timeout issues
                    for attempt in range(self.canbus_comms_retries):  # HACK: We can repeat because homing move
                        try:
                            initial_mcu_pos = self.mmu_extruder_stepper.stepper.get_mcu_position() # For not homing extruder
                            #init_pos = pos[1]
                            #pos[1] += dist
                            with self._wrap_accel(accel):
                                trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                            homed = True
                            if self.gear_rail.is_endstop_virtual(endstop_name):
                                # Stallguard doesn't do well at slow speed. Try to infer move completion
                                if abs(trig_pos[1] - dist) < 1.0:
                                        homed = False
                        except self.printer.command_error as e:
                            # CANbus mcu's often seen to exhibit "Communication timeout" so surface errors to user
                            if abs(trig_pos[1] - dist) > 0. and not "after full movement" in str(e):
                                if 'communication timeout' in str(e).lower():
                                    got_comms_timeout = True
                                self._log_error("Did not complete homing move: %s" % str(e))
                            else:
                                self._log_stepper("Did not home: %s" % str(e))
                            homed = False
                        finally:
                            halt_pos = self.mmu_toolhead.get_position()
                            actual = halt_pos[1] - init_pos
                            if not self.homing_extruder:
                                # This isn't super accurate if extruder is on different mcu from MMU gear steppers, but it's
                                # good enough if the user has opted out of the default homing extruder
                                actual = (self.mmu_extruder_stepper.stepper.get_mcu_position() - initial_mcu_pos) * self.mmu_extruder_stepper.stepper.get_step_dist()
                        if not got_comms_timeout:
                            break
                else:
                    self._log_stepper("%s: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                    pos[1] += dist
                    with self._wrap_accel(accel):
                        self.mmu_toolhead.move(pos, speed)

        # Extruder is driving the filament
        elif motor in ["synced"]:
            self._ensure_safe_extruder_temperature(wait=False)
            with self._wrap_sync_gear_to_extruder(True):
                if homing_move != 0:
                    self._log_error("Not possible to perform homing move while synced")
                else:
                    self._log_stepper("%s: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)

        # Independent motors
        elif motor == "both":
            if homing_move != 0:
                self._log_error("Not possible to perform homing move on two independent steppers")
            else:
                self._ensure_safe_extruder_temperature(wait=False)
                self._log_stepper("%s: dist=%.1f, speed=%.1f, accel=%.1f, sync=%s, wait=%s" % (motor.upper(), dist, speed, accel, sync, wait))
                pos[1] += dist
                with self._wrap_accel(accel):
                    self.mmu_toolhead.move(pos, speed)
                ext_pos[3] += dist
                self.toolhead.move(ext_pos, speed)

        if not homing_move and wait:
            self._movequeues_wait_moves()

        encoder_end = self._get_encoder_distance(dwell=encoder_dwell)
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
            self._log_trace(trace_str)

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
    def _wrap_accel(self, accel):
        self.mmu_kinematics.set_accel_limit(accel)
        try:
            yield self
        finally:
            self.mmu_kinematics.set_accel_limit(None)


#################################
# GENERAL FILAMENT MOVE HELPERS #
#################################

    # Check for filament in MMU using available sensors or encoder
    def _check_filament_in_mmu(self):
        self._log_debug("Checking for filament in MMU...")
        if any(self._check_all_sensors().values()):
            self._log_debug("Filament detected by sensors: %s" % ', '.join([key for key, value in self._check_all_sensors().items() if value]))
            return True
        elif not self._has_sensor(self.ENDSTOP_GATE) and self._has_encoder():
            self._servo_down()
            found = self._buzz_gear_motor()
            self._log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if found else "not detected"))
            return found
        self._log_debug("Filament not detected by sensors: %s" % ', '.join([key for key, value in self._check_all_sensors().items()]))
        return False

    # Check for filament at selected gate
    def _check_filament_at_gate(self):
        self._log_debug("Checking for filament at gate...")
        if self._has_sensor(self.ENDSTOP_GATE):
            detected = self._check_sensor(self.ENDSTOP_GATE)
            self._log_debug("Filament %s by gate sensor" % "detected" if detected else "not detected")
            return detected
        elif self._has_encoder():
            self._servo_down()
            found = self._buzz_gear_motor()
            self._log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if found else "not detected"))
            return found
        self._log_debug("No sensors configured!")
        return False

    # Check for filament in extruder by moving extruder motor. Even with toolhead sensor this
    # can happen if the filament is in the short distance from sensor to gears
    def _check_filament_still_in_extruder(self):
        if self._has_encoder():
            self._log_debug("Checking for possibility of filament still in extruder gears...")
            self._ensure_safe_extruder_temperature(wait=False)
            self._servo_up()
            length = self.encoder_move_step_size
            _,_,measured,_ = self._trace_filament_move("Checking extruder", -length, speed=self.extruder_unload_speed, motor="extruder")
            detected = measured > self.encoder_min
            self._log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
            return detected
        return False

    def _buzz_gear_motor(self):
        with self._require_encoder():
            initial_encoder_position = self._get_encoder_distance()
            self._trace_filament_move(None, 2.5, accel=self.gear_buzz_accel, encoder_dwell=None)
            self._trace_filament_move(None, -2.5, accel=self.gear_buzz_accel, encoder_dwell=None)
            measured = self._get_encoder_distance() - initial_encoder_position
            self._log_trace("After buzzing gear motor, encoder measured %.2f" % measured)
            self._set_encoder_distance(initial_encoder_position, dwell=None)
            return measured > self.encoder_min

    # Sync/unsync gear motor with extruder
    # servo: True=move, False=don't mess
    # current: True=optionally reduce, False=restore to current default
    def _sync_gear_to_extruder(self, sync, servo=False, current=False):
        prev_sync_state = self.mmu_toolhead.is_gear_synced_to_extruder()
        if servo:
            if sync:
                self._servo_down()
            else:
                self._servo_auto()
        if prev_sync_state != sync:
            self._log_debug("%s gear stepper and extruder" % ("Syncing" if sync else "Unsyncing"))
            self.mmu_toolhead.sync_gear_to_extruder(self.extruder_name if sync else None)
            # This event only occurs when syncing during print, not loading/unloading movements
            self.printer.send_event("mmu:print_synced" if sync else "mmu:print_unsynced")

        # Option to reduce current during print
        if current and sync:
            self._adjust_gear_current(self.sync_gear_current, "for extruder syncing")
        else:
            self._restore_gear_current()
        return prev_sync_state

    def _adjust_gear_current(self, percent=100, reason=""):
         if self.gear_tmc and percent != self.gear_percentage_run_current and percent > 0 and percent < 200:
             self._log_info("Modifying MMU gear stepper run current to %d%% %s" % (percent, reason))
             self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=stepper_mmu_gear CURRENT=%.2f" % ((self.gear_default_run_current * percent) / 100.))
             self.gear_percentage_run_current = percent

    def _restore_gear_current(self):
        if self.gear_tmc and self.gear_percentage_run_current != self.gear_restore_percent_run_current:
            self._log_info("Restoring MMU gear stepper run current to %d%% configured" % self.gear_restore_percent_run_current)
            self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=stepper_mmu_gear CURRENT=%.2f" % self.gear_default_run_current)
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

    def _adjust_extruder_current(self, percent=100, reason=""):
         if self.extruder_tmc and percent != self.extruder_percentage_run_current and percent > 0 and percent < 200:
             self._log_info("Modifying extruder_stepper run current to %d%% %s" % (percent, reason))
             self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (self.extruder_name, (self.extruder_default_run_current * percent) / 100.))
             self.extruder_percentage_run_current = percent

    def _restore_extruder_current(self):
        if self.extruder_tmc and self.extruder_percentage_run_current != 100:
            self._log_info("Restoring extruder_stepper run current to 100% configured")
            self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (self.extruder_name, self.extruder_default_run_current))
            self.extruder_percentage_run_current = 100

    @contextlib.contextmanager
    def _wrap_extruder_current(self, percent=100, reason=""):
        self._adjust_extruder_current(percent, reason)
        try:
            yield self
        finally:
            self._restore_extruder_current()

    @contextlib.contextmanager
    def _wrap_sync_gear_to_extruder(self, enable):
        if enable:
            self.mmu_toolhead.sync_gear_to_extruder(self.extruder_name)
        try:
            yield self
        finally:
            if enable:
                self.mmu_toolhead.sync_gear_to_extruder(None)

    @contextlib.contextmanager
    def _wrap_sync_extruder_to_gear(self, enable, extruder_only=False):
        if enable:
            self.mmu_toolhead.sync_extruder_to_gear(self.extruder_name, extruder_only=extruder_only)
        try:
            yield self
        finally:
            if enable:
                self.mmu_toolhead.sync_extruder_to_gear(None)

    def _move_cmd(self, gcmd, trace_str):
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        move = gcmd.get_float('MOVE', 100.)
        speed = gcmd.get_float('SPEED', None)
        accel = gcmd.get_float('ACCEL', None)
        motor = gcmd.get('MOTOR', "gear")
        wait = bool(gcmd.get_int('WAIT', 0, minval=0, maxval=1))
        sync = bool(gcmd.get_int('SYNC', 0, minval=0, maxval=1))
        if motor not in ["gear", "extruder", "gear+extruder", "synced", "both"]:
            raise gcmd.error("Valid motor names are 'gear', 'extruder', 'gear+extruder', 'synced' or 'both'")
        if motor == "extruder":
            self._servo_up()
        else:
            self._servo_down()
        self._log_debug("Moving '%s' motor %.1fmm..." % (motor, move))
        return self._trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, sync=sync, wait=wait)

    def _homing_move_cmd(self, gcmd, trace_str):
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
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
            self._servo_up()
        else:
            self._servo_down()
            if self.gear_rail.is_endstop_virtual(endstop):
                self._movequeues_dwell(1, toolhead=False) # TMC needs time to settle after gear buzz for servo
        self._log_debug("Homing '%s' motor to '%s' endstop, up to %.1fmm..." % (motor, endstop, move))
        return self._trace_filament_move(trace_str, move, speed=speed, accel=accel, motor=motor, homing_move=stop_on_endstop, endstop_name=endstop, sync=sync)


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
                self._log_debug("Saved speed/extrusion multiplier for tool T%d as %d%% and %d%%" % (tool, current_speed_factor * 100, current_extrude_factor * 100))

    def _restore_tool_override(self, tool):
        if tool == self.tool_selected:
            current_speed_factor = self.gcode_move.get_status(0)['speed_factor']
            current_extrude_factor = self.gcode_move.get_status(0)['extrude_factor']
            speed_factor = self.tool_speed_multipliers[tool]
            extrude_factor = self.tool_extrusion_multipliers[tool]
            self.gcode.run_script_from_command("M220 S%d" % (speed_factor * 100))
            self.gcode.run_script_from_command("M221 S%d" % (extrude_factor * 100))
            if current_speed_factor != speed_factor or current_extrude_factor != extrude_factor:
                self._log_debug("Restored speed/extrusion multiplier for tool T%d as %d%% and %d%%" % (tool, speed_factor * 100, extrude_factor * 100))

    def _set_tool_override(self, tool, speed_percent, extrude_percent):
        if tool == -1:
            for i in range(self.mmu_num_gates):
                if speed_percent is not None:
                    self.tool_speed_multipliers[i] = speed_percent / 100
                if extrude_percent is not None:
                    self.tool_extrusion_multipliers[i] = extrude_percent / 100
                self._restore_tool_override(i)
            if speed_percent is not None:
                self._log_debug("Set speed multiplier for all tools as %d%%" % speed_percent)
            if extrude_percent is not None:
                self._log_debug("Set extrusion multiplier for all tools as %d%%" % extrude_percent)
        else:
            if speed_percent is not None:
                self.tool_speed_multipliers[tool] = speed_percent / 100
                self._log_debug("Set speed multiplier for tool T%d as %d%%" % (tool, speed_percent))
            if extrude_percent is not None:
                self.tool_extrusion_multipliers[tool] = extrude_percent / 100
                self._log_debug("Set extrusion multiplier for tool T%d as %d%%" % (tool, extrude_percent))
            self._restore_tool_override(tool)

    # Primary method to select and loads tool. Assumes we are unloaded.
    def _select_and_load_tool(self, tool):
        self._log_debug('Loading tool T%d...' % tool)
        gate = self.ttg_map[tool]
        if self.gate_status[gate] == self.GATE_EMPTY:
            if self.enable_endless_spool and self.endless_spool_on_load:
                self._log_info("Gate %d is empty!" % gate)
                next_gate, checked_gates = self._get_next_endless_spool_gate(tool, gate)
                if next_gate == -1:
                    raise MmuError("No EndlessSpool alternatives available after reviewing gates: %s" % checked_gates)
                self._log_info("Remapping T%d to Gate %d" % (tool, next_gate))
                gate = self._remap_tool(tool, next_gate)
            else:
                raise MmuError("Gate %d is empty! Use 'MMU_CHECK_GATE GATE=%d' to reset" % (gate, gate))
        
        self._track_time_start('pre_load')
        self._wrap_gcode_command(self.pre_load_macro, exception=True)
        self._track_time_end('pre_load')

        self._select_tool(tool, move_servo=False)
        self._update_filaments_from_spoolman(gate) # Request update of material & color from Spoolman
        self._load_sequence()
        self._spoolman_activate_spool(self.gate_spool_id[gate]) # Activate the spool in Spoolman
        self._restore_tool_override(self.tool_selected) # Restore M220 and M221 overrides

        self._track_time_start('post_load')
        self._wrap_gcode_command(self.post_load_macro, exception=True)
        self._track_time_end('post_load') 

    # Primary method to unload current tool but retains selection
    def _unload_tool(self, skip_tip=False, runout=False):
        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            self._log_debug("Tool already unloaded")
            return

        self._log_debug("Unloading tool %s" % self._selected_tool_string())
        self._track_time_start('pre_unload')
        self._wrap_gcode_command(self.pre_unload_macro, exception=True)
        self._track_time_end('pre_unload')

        self._record_tool_override() # Remember M220 and M221 overrides
        self._unload_sequence(skip_tip=skip_tip, runout=runout)
        self._spoolman_activate_spool(0) # Deactivate in SpoolMan

        self._track_time_start('post_unload')
        self._wrap_gcode_command(self.post_unload_macro, exception=True)
        self._track_time_end('post_unload')

    # This is the main function for initiating a tool change, it will handle unload if necessary
    def _change_tool(self, tool, skip_tip=True):
        self._log_debug("Tool change initiated %s" % ("with slicer tip forming" if skip_tip else "with standalone MMU tip forming"))
        self._track_time_start('total')
        skip_unload = False
        initial_tool_string = self._selected_tool_string()
        if tool == self.tool_selected and self.ttg_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
            self._log_always("Tool T%d is already loaded" % tool)
            return False

        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            skip_unload = True
            msg = "Tool change requested: T%d" % tool
            m117_msg = ("> T%d" % tool)
        elif self.tool_selected == tool:
            msg = "Reloading: T%d" % tool
            m117_msg = ("> T%d" % tool)
        else:
            msg = "Tool change requested, from %s to T%d" % (initial_tool_string, tool)
            m117_msg = ("%s > T%d" % (initial_tool_string, tool))
        # Important to always inform user in case there is an error and manual recovery is necessary
        self._last_toolchange = m117_msg
        self.gcode.run_script_from_command("M117 %s" % m117_msg)
        self._log_always(msg)

        # Check TTG map. We might be mapped to same gate
        if self.ttg_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
            self._select_tool(tool)
            self.gcode.run_script_from_command("M117 T%s" % tool)
            return False

        # Identify the unitialized startup use case and make it easy for user
        if not self.is_homed or self.tool_selected == self.TOOL_GATE_UNKNOWN:
            self._log_info("MMU not homed, homing it before continuing...")
            self._home(tool)
            skip_unload = True

        # Notify start of actual toolchange operation
        self.printer.send_event("mmu:toolchange", self._last_tool, self._next_tool)

        if not skip_unload:
            self._unload_tool(skip_tip=skip_tip)

        self._select_and_load_tool(tool)
        self._track_swap_completed()
        
        self._track_time_end('total')

        self.gcode.run_script_from_command("M117 T%s" % tool)
        return True

    def _unselect_tool(self):
        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        self._servo_auto()

    def _select_tool(self, tool, move_servo=True):
        if tool < 0 or tool >= self.mmu_num_gates:
            self._log_always("Tool %d does not exist" % tool)
            return

        gate = self.ttg_map[tool]
        if tool == self.tool_selected and gate == self.gate_selected:
            return

        self._log_debug("Selecting tool T%d on Gate %d..." % (tool, gate))
        self._select_gate(gate)
        self._set_tool_selected(tool)
        if move_servo:
            self._servo_auto()
        self._log_info("Tool T%d enabled%s" % (tool, (" on Gate %d" % gate) if tool != gate else ""))

    def _select_bypass(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.gate_selected == self.TOOL_GATE_BYPASS: return
        if self.bypass_offset == 0:
            self._log_always("Bypass not configured")
            return
        self._log_info("Selecting filament bypass...")
        self._select_gate(self.TOOL_GATE_BYPASS)
        self.filament_direction = self.DIRECTION_LOAD
        self._set_tool_selected(self.TOOL_GATE_BYPASS)
        self._log_info("Bypass enabled")

    def _select_gate(self, gate):
        if gate == self.gate_selected: return

        if self.virtual_selector:
            self.mmu_toolhead.select_gear_stepper(gate)
            self._set_gate_selected(gate)
            return

        with self._wrap_action(self.ACTION_SELECTING):
            self._servo_move()
            if gate == self.TOOL_GATE_BYPASS:
                offset = self.bypass_offset
            else:
                offset = self.selector_offsets[gate]
            self._position_selector(offset)
            self._set_gate_selected(gate)

    def _set_gate_selected(self, gate):
        self.gate_selected = gate
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_GATE_SELECTED, self.gate_selected))
        self._set_gate_ratio(self._get_gate_ratio(gate) if gate >= 0 else 1.)
        if gate >= 0:
            self.active_gate = {'material': self.gate_material[gate], 'color': self.gate_color[gate], 'spool_id': self.gate_spool_id[gate]}
        else:
            self.active_gate = {}

    def _set_tool_selected(self, tool):
        self.tool_selected = tool
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_TOOL_SELECTED, self.tool_selected))

    def _set_gate_ratio(self, ratio=1.):
        new_rotation_distance = ratio * self.ref_gear_rotation_distance
        self._log_trace("Setting gear motor rotation distance: %.6f (ratio: %.6f)" % (new_rotation_distance, ratio))
        self.gear_stepper.set_rotation_distance(new_rotation_distance)

    def _get_gate_ratio(self, gate):
        if gate < 0: return 1.
        ratio = self.variables.get("%s%d" % (self.VARS_MMU_CALIB_PREFIX, gate), 1.)
        if ratio > 0.8 and ratio < 1.2:
            return ratio
        else:
            self._log_always("Warning: %s%d value (%.6f) is invalid. Using reference value 1.0. Re-run MMU_CALIBRATE_GATES GATE=%d" % (self.VARS_MMU_CALIB_PREFIX, gate, ratio, gate))
            return 1.

    def _spoolman_activate_spool(self, spool_id=-1):
        if not self.enable_spoolman: return
        try:
            webhooks = self.printer.lookup_object('webhooks')
            if spool_id < 0:
                self._log_debug("Spoolman spool_id not set for current gate")
            else:
                if spool_id == 0:
                    self._log_debug("Deactivating spool ...")
                else:
                    self._log_debug("Activating spool %s..." % spool_id)
                webhooks.call_remote_method("spoolman_set_active_spool", spool_id=spool_id)
        except Exception as e:
            self._log_error("Error while calling spoolman_set_active_spool: %s" % str(e))

    # Tell moonraker component we are interested in filament data
    # gate=None means all gates with spool_id, else specific gate
    def _update_filaments_from_spoolman(self, gate=None):
        if not self.enable_spoolman: return
        gate_ids = []
        if gate is None: # All gates
            for i in range(self.mmu_num_gates):
                if self.gate_spool_id[i] >= 0:
                    gate_ids.append((i, self.gate_spool_id[i]))
        elif self.gate_spool_id[gate] >= 0:
            gate_ids.append((gate, self.gate_spool_id[gate]))
        if len(gate_ids) > 0:
            self._log_debug("Requesting the following gate/spool_id pairs from Spoolman: %s" % gate_ids)
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("spoolman_get_filaments", gate_ids=gate_ids)
            except Exception as e:
                self._log_error("Error while retrieving spoolman info: %s" % str(e))

### CORE GCODE COMMANDS ##########################################################

    cmd_MMU_HOME_help = "Home the MMU selector"
    def cmd_MMU_HOME(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self.virtual_selector: return
        if self._check_is_calibrated(self.CALIBRATED_SELECTOR):
            self._log_always("Will home to endstop only!")
            tool = -1
            force_unload = 0
        else:
            tool = gcmd.get_int('TOOL', 0, minval=0, maxval=self.mmu_num_gates - 1)
            force_unload = gcmd.get_int('FORCE_UNLOAD', -1, minval=0, maxval=1)
        try:
            self._home(tool, force_unload)
            if tool == -1:
                self._log_always("Homed")
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_SELECT_help = "Select the specified logical tool (following TTG map) or physical gate"
    def cmd_MMU_SELECT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(self.CALIBRATED_SELECTOR): return
        bypass = gcmd.get_int('BYPASS', -1, minval=0, maxval=1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        if tool == -1 and gate == -1 and bypass == -1:
            raise gcmd.error("Error on 'MMU_SELECT': missing TOOL, GATE or BYPASS")
        self._select(bypass, tool, gate)

    cmd_MMU_SELECT_BYPASS_help = "Select the filament bypass"
    def cmd_MMU_SELECT_BYPASS(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        self._select(1, -1, -1)

    def _select(self, bypass, tool, gate):
        try:
            if bypass != -1:
                self._select_bypass()
            elif tool != -1:
                self._select_tool(tool)
            else:
                self._select_gate(gate)
                # Find the first tool that maps to this gate or current tool if it maps
                # (Remember multiple tools can map to the same gate)
                if self.tool_selected >= 0 and self.ttg_map[self.tool_selected] == gate:
                    pass
                else:
                    for tool in range(len(self.ttg_map)):
                        if self.ttg_map[tool] == gate:
                            self._select_tool(tool)
                            break
                    else:
                        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self._servo_auto()

    cmd_MMU_CHANGE_TOOL_help = "Perform a tool swap (called from Tx command)"
    def cmd_MMU_CHANGE_TOOL(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(): return
        self.last_statistics = {}
        self._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        cmd = gcmd.get_command().strip()
        match = re.match(r'[Tt](\d{1,3})$', cmd)
        if match:
            tool = int(match.group(1))
            if tool < 0 or tool > self.mmu_num_gates - 1:
                raise gcmd.error("Invalid tool")
        else:
            tool = gcmd.get_int('TOOL', minval=0, maxval=self.mmu_num_gates - 1)
        skip_tip = self._is_printing() and not (standalone or self.force_form_tip_standalone)
        if self.filament_pos == self.FILAMENT_POS_UNKNOWN and self.is_homed: # Will be done later if not homed
            self._recover_filament_pos(message=True)

        self._save_toolhead_position_and_lift("change_tool", z_hop_height=self.z_hop_height_toolchange)

        if self._has_encoder():
            self.encoder_sensor.update_clog_detection_length()

        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during tool change
            self._last_tool = self.tool_selected
            self._next_tool = tool

            attempts = 2 if self.retry_tool_change_on_error and (self._is_printing() or standalone) else 1 # TODO: replace with inattention timer
            try:
                for i in range(attempts):
                    try:
                        if self._change_tool(tool, skip_tip):
                            self._dump_statistics(job=not quiet, gate=not quiet)
                        continue
                    except MmuError as ee:
                        if i == attempts - 1:
                            self._mmu_pause("%s.\nOccured when changing tool: %s" % (str(ee), self._last_toolchange))
                            return
                        self._log_error("%s.\nOccured when changing tool: %s. Retrying..." % (str(ee), self._last_toolchange))
                        # Try again but recover_filament_pos will ensure conservative treatment of unload
                        self._recover_filament_pos()
            finally:
                self._next_tool = self.TOOL_GATE_UNKNOWN

        # If actively printing then we must restore toolhead position, if paused, mmu_resume will do this
        if self._is_printing():
            try:
                self._check_runout() # Can throw MmuError
                self._continue_printing("change_tool") # Continue printing...
            except MmuError as ee:
                self._mmu_pause(str(ee))

    cmd_MMU_LOAD_help = "Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTRUDER_ONLY=1)"
    def cmd_MMU_LOAD(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        self._fix_started_state()

        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1) or in_bypass)

        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during filament load
            try:
                if not extruder_only:
                    self._select_and_load_tool(self.tool_selected) # This could change gate tool is mapped to
                elif extruder_only and self.filament_pos != self.FILAMENT_POS_LOADED:
                    self._load_sequence(length=0, extruder_only=True)
                else:
                    self._log_always("Filament already loaded")
            except MmuError as ee:
                self._mmu_pause(str(ee))
                if self.tool_selected == self.TOOL_GATE_BYPASS:
                    self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)

    cmd_MMU_EJECT_help = "aka MMU_UNLOAD Eject filament and park it in the MMU or optionally unloads just the extruder (EXTRUDER_ONLY=1)"
    def cmd_MMU_EJECT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_is_calibrated(): return
        self.last_statistics = {}
        self._fix_started_state()

        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))

        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during filament load
            try:
                if not extruder_only:
                    self._unload_tool(skip_tip=skip_tip)
                elif extruder_only and self.filament_pos != self.FILAMENT_POS_UNLOADED:
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER, silent=True) # Ensure tool tip is performed
                    self._unload_sequence(length=0, skip_tip=skip_tip, extruder_only=True)
                    if in_bypass:
                        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                        self._log_always("Please pull the filament out clear of the MMU selector")
                else:
                    self._log_always("Filament not loaded")
            except MmuError as ee:
                self._mmu_pause(str(ee))

    cmd_MMU_PRINT_START_help = "Initialize MMU state and ready for print"
    def cmd_MMU_PRINT_START(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self._is_in_print():
            self._on_print_start()
            self._clear_macro_state()

    cmd_MMU_PRINT_END_help = "Cleans up state after after print end"
    def cmd_MMU_PRINT_END(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        end_state = gcmd.get('STATE', "complete")
        if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
            self._on_print_end(end_state)
            self._clear_macro_state()
        else:
            raise gcmd.error("Unknown endstate '%s'" % end_state)

    cmd_MMU_PAUSE_help = "Pause the current print and lock the MMU operations"
    def cmd_MMU_PAUSE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)) # Mimick in-print
        msg = gcmd.get('MSG',"MMU_PAUSE macro was directly called")
        self._mmu_pause(msg, force_in_print)

    cmd_MMU_UNLOCK_help = "Wakeup the MMU prior to resume to restore temperatures and timeouts"
    def cmd_MMU_UNLOCK(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._is_mmu_pause_locked():
            self._clear_mmu_error_dialog()
            self._mmu_unlock()

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_RESUME_help = "Wrapper around default RESUME macro"
    def cmd_MMU_RESUME(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            # User defined or Klipper default behavior
            self._wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())), None)
            return

        self._log_trace("MMU RESUME wrapper called")
        if not self._is_printer_paused() and not self._is_mmu_paused():
            self._log_always("Print is not paused. Resume ignored.")
            return

        try:
            self._clear_mmu_error_dialog()
            if self._is_mmu_pause_locked():
                self._mmu_unlock()
            if self._is_in_print():
                self._check_runout() # Can throw MmuError
                # Convenience of the user in case they forgot to set filament position state
                if self.filament_pos != self.FILAMENT_POS_LOADED:
                    if self._check_sensor(self.ENDSTOP_TOOLHEAD) is True:
                        self._set_filament_pos_state(self.FILAMENT_POS_LOADED, silent=True)
                        self._log_always("Automatically set filament state to LOADED based on toolhead sensor")
            self._wrap_gcode_command(" ".join(("__RESUME", gcmd.get_raw_command_parameters())))
            if self._is_mmu_paused():
                self._mmu_resume() # Continue printing...
            else:
                self._continue_printing("resume") # Continue printing...
        except MmuError as ee:
            self._mmu_pause(str(ee))

    # Not a user facing command - used in automatic wrapper
    cmd_PAUSE_help = "Wrapper around default PAUSE macro"
    def cmd_PAUSE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled:
            self._wrap_gcode_command("__PAUSE", None) # User defined or Klipper default behavior
            return

        self._log_trace("MMU PAUSE wrapper called")
        self._fix_started_state() # Get out of 'started' state before transistion to pause
        self._save_toolhead_position_and_lift("pause", z_hop_height=self.z_hop_height_error)
        self._wrap_gcode_command("__PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_CLEAR_PAUSE_help = "Wrapper around default CLEAR_PAUSE macro"
    def cmd_CLEAR_PAUSE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._log_trace("MMU CLEAR_PAUSE wrapper called")
        self._wrap_gcode_command("__CLEAR_PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_CANCEL_PRINT_help = "Wrapper around default CANCEL_PRINT macro"
    def cmd_MMU_CANCEL_PRINT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self.is_enabled:
            self._log_debug("MMU_CANCEL_PRINT wrapper called")
            self._save_toolhead_position_and_lift(z_hop_height=self.z_hop_height_error) # Lift Z but don't save
            self._clear_mmu_error_dialog()
            self._wrap_gcode_command("__CANCEL_PRINT", None)
            self._on_print_end("cancelled")
        else:
            self._wrap_gcode_command("__CANCEL_PRINT", None) # User defined or Klipper default behavior

    cmd_MMU_RECOVER_help = "Recover the filament location and set MMU state after manual intervention/movement"
    def cmd_MMU_RECOVER(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        tool = gcmd.get_int('TOOL', self.TOOL_GATE_UNKNOWN, minval=-2, maxval=self.mmu_num_gates - 1)
        mod_gate = gcmd.get_int('GATE', self.TOOL_GATE_UNKNOWN, minval=-2, maxval=self.mmu_num_gates - 1)
        loaded = gcmd.get_int('LOADED', -1, minval=0, maxval=1)
        strict = gcmd.get_int('STRICT', 0, minval=0, maxval=1)

        if (tool == self.TOOL_GATE_BYPASS or mod_gate == self.TOOL_GATE_BYPASS) and self.bypass_offset == 0:
            self._log_always("Bypass not configured")
            return

        if tool == self.TOOL_GATE_BYPASS:
            self._set_gate_selected(self.TOOL_GATE_BYPASS)
            self._set_tool_selected(self.TOOL_GATE_BYPASS)
            self._set_selector_pos(self.bypass_offset) # In case selector stepper was turned off
        elif tool >= 0: # If tool is specified then use and optionally override the gate
            self._set_tool_selected(tool)
            gate = self.ttg_map[tool]
            if mod_gate >= 0:
                gate = mod_gate
            if gate >= 0:
                self._remap_tool(tool, gate, loaded)
                self._set_gate_selected(gate)
                self._set_selector_pos(self.selector_offsets[self.gate_selected]) # In case selector stepper was turned off
        elif tool == self.TOOL_GATE_UNKNOWN and self.tool_selected == self.TOOL_GATE_BYPASS and loaded == -1:
            # This is to be able to get out of "stuck in bypass" state
            self._log_info("Warning: Making assumption that bypass is unloaded")
            self.filament_direction = self.DIRECTION_UNKNOWN
            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=True)
            self._servo_auto()
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
        self._recover_filament_pos(strict=strict, message=True)
        self._servo_auto()


### GCODE COMMANDS INTENDED FOR TESTING #####################################

    cmd_MMU_SOAKTEST_SELECTOR_help = "Soak test of selector movement"
    def cmd_MMU_SOAKTEST_SELECTOR(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(self.CALIBRATED_SELECTOR): return
        loops = gcmd.get_int('LOOP', 100)
        servo = bool(gcmd.get_int('SERVO', 0))
        home = bool(gcmd.get_int('HOME', 1))
        try:
            self._home()
            for l in range(loops):
                self._log_always("Testing loop %d / %d" % (l + 1, loops))
                tool = randint(0, self.mmu_num_gates)
                if tool == self.mmu_num_gates:
                    self._select_bypass()
                else:
                    if randint(0, 10) == 0 and home:
                        self._home(tool)
                    else:
                        self._select_tool(tool, move_servo=servo)
                if servo:
                    self._servo_down()
        except MmuError as ee:
            self._log_error("Soaktest abandoned because of error")
            self._log_always(str(ee))

    cmd_MMU_SOAKTEST_LOAD_SEQUENCE_help = "Soak test tool load/unload sequence"
    def cmd_MMU_SOAKTEST_LOAD_SEQUENCE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        if self._check_not_homed(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(): return
        loops = gcmd.get_int('LOOP', 10)
        random = gcmd.get_int('RANDOM', 0)
        to_nozzle = gcmd.get_int('FULL', 0)
        try:
            for l in range(loops):
                self._log_always("Testing loop %d / %d" % (l, loops))
                for t in range(self.mmu_num_gates):
                    tool = t
                    if random == 1:
                        tool = randint(0, self.mmu_num_gates - 1)
                    gate = self.ttg_map[tool]
                    if self.gate_status[gate] == self.GATE_EMPTY:
                        self._log_always("Skipping tool %d of %d because Gate %d is empty" % (tool, self.mmu_num_gates, gate))
                    else:
                        self._log_always("Testing tool %d of %d (Gate %d)" % (tool, self.mmu_num_gates, gate))
                        if not to_nozzle:
                            self._select_tool(tool)
                            self._load_sequence(length=100, skip_extruder=True)
                            self._unload_sequence(length=100)
                        else:
                            self._select_and_load_tool(tool)
                            self._unload_tool()
            self._select_tool(0)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_TEST_GRIP_help = "Test the MMU grip for a Tool"
    def cmd_MMU_TEST_GRIP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        self._servo_down()
        self._motors_off(motor="gear")

    cmd_MMU_TEST_TRACKING_help = "Test the tracking of gear feed and encoder sensing"
    def cmd_MMU_TEST_TRACKING(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_has_encoder(): return
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        if self._check_not_homed(): return
        if self._check_is_calibrated(): return
        direction = gcmd.get_int('DIRECTION', 1, minval=-1, maxval=1)
        step = gcmd.get_float('STEP', 1, minval=0.5, maxval=20)
        sensitivity = gcmd.get_float('SENSITIVITY', self.encoder_resolution, minval=0.1, maxval=10)
        if direction == 0: return
        try:
            if not self.filament_pos in [self.FILAMENT_POS_START_BOWDEN, self.FILAMENT_POS_IN_BOWDEN]:
                # Ready MMU for test if not already setup
                self._unload_tool()
                self._load_sequence(length=100 if direction == 1 else 200, skip_extruder=True)
            with self._require_encoder():
                self._initialize_filament_position()    # Encoder 0000
                for i in range(1, int(100 / step)):
                    self._trace_filament_move(None, direction * step, encoder_dwell=None)
                    measured = self._get_encoder_distance()
                    moved = i * step
                    drift = int(round((moved - measured) / sensitivity))
                    if drift > 0:
                        drift_str = "++++++++!!"[0:drift]
                    elif (moved - measured) < 0:
                        drift_str = "--------!!"[0:-drift]
                    else:
                        drift_str = ""
                    self._log_info("Gear/Encoder : %05.2f / %05.2f mm %s" % (moved, measured, drift_str))
            self._unload_tool()
        except MmuError as ee:
            self._log_always("Tracking test failed: %s" % str(ee))

    cmd_MMU_TEST_LOAD_help = "For quick testing filament loading from gate to the extruder"
    def cmd_MMU_TEST_LOAD(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(): return
        full = gcmd.get_int('FULL', 0, minval=0, maxval=1)
        try:
            if full:
                self._load_sequence(skip_extruder=True)
            else:
                length = gcmd.get_float('LENGTH', 100, minval=10., maxval=self.calibrated_bowden_length)
                self._load_sequence(length=length, skip_extruder=True)
        except MmuError as ee:
            self._log_always("Load test failed: %s" % str(ee))

    cmd_MMU_TEST_MOVE_help = "Test filament move to help debug setup / options"
    def cmd_MMU_TEST_MOVE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        actual,_,measured,_ = self._move_cmd(gcmd, "Test move")
        self._log_always("Moved %.1fmm%s" % (actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_HOMING_MOVE_help = "Test filament homing move to help debug setup / options"
    def cmd_MMU_TEST_HOMING_MOVE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        actual,homed,measured,_ = self._homing_move_cmd(gcmd, "Test homing move")
        self._log_always("%s after %.1fmm%s" % (("Homed" if homed else "Did not home"), actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_CONFIG_help = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    def cmd_MMU_TEST_CONFIG(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        # Try to catch illegal parameters
        illegal_params = [p for p in gcmd.get_command_parameters() if vars(self).get(p.lower()) is None]
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
        # Selector speeds
        self.selector_move_speed = gcmd.get_float('SELECTOR_MOVE_SPEED', self.selector_move_speed, minval=1.)
        self.selector_homing_speed = gcmd.get_float('SELECTOR_HOMING_SPEED', self.selector_homing_speed, minval=1.)
        self.selector_touch_speed = gcmd.get_float('SELECTOR_TOUCH_SPEED', self.selector_touch_speed, minval=1.)
        self.selector_touch_enable = gcmd.get_int('SELECTOR_TOUCH_ENABLE', self.selector_touch_enable, minval=0, maxval=1)

        # Synchronous motor control
        self.sync_form_tip = gcmd.get_int('SYNC_FORM_TIP', self.sync_form_tip, minval=0, maxval=1)
        self.sync_to_extruder = gcmd.get_int('SYNC_TO_EXTRUDER', self.sync_to_extruder, minval=0, maxval=1)
        self.sync_feedback_enable = gcmd.get_int('SYNC_FEEDBACK_ENABLE', self.sync_feedback_enable, minval=0, maxval=1)
        self.sync_multiplier_high = gcmd.get_float('SYNC_MULTIPLIER_HIGH', self.sync_multiplier_high, minval=1., maxval=2.)
        self.sync_multiplier_low = gcmd.get_float('SYNC_MULTIPLIER_LOW', self.sync_multiplier_low, minval=0.5, maxval=1.)

        # TMC current control
        self.sync_gear_current = gcmd.get_int('SYNC_GEAR_CURRENT', self.sync_gear_current, minval=10, maxval=100)
        self.extruder_homing_current = gcmd.get_int('EXTRUDER_HOMING_CURRENT', self.extruder_homing_current, minval=10, maxval=100) # TODO rename extruder_collision_homing_current
        self.extruder_form_tip_current = gcmd.get_int('EXTRUDER_FORM_TIP_CURRENT', self.extruder_form_tip_current, minval=100, maxval=150)

        # Homing, loading and unloading controls
        gate_homing_endstop = gcmd.get('GATE_HOMING_ENDSTOP', self.gate_homing_endstop)
        if gate_homing_endstop not in self.GATE_ENDSTOPS:
            raise gcmd.error("gate_homing_endstop is invalid. Options are: %s" % self.GATE_ENDSTOPS)
        if gate_homing_endstop != self.gate_homing_endstop:
            if gate_homing_endstop == self.ENDSTOP_ENCODER:
                self.calibrated_bowden_length += self.gate_endstop_to_encoder
            else:
                self.calibrated_bowden_length -= self.gate_endstop_to_encoder
        self.gate_homing_endstop = gate_homing_endstop

        self.gate_endstop_to_encoder = gcmd.get_float('GATE_ENDSTOP_TO_ENCODER', self.gate_endstop_to_encoder)
        self.gate_parking_distance = gcmd.get_float('GATE_PARKING_DISTANCE', self.gate_parking_distance)
        self.bowden_apply_correction = gcmd.get_int('BOWDEN_APPLY_CORRECTION', self.bowden_apply_correction, minval=0, maxval=1)
        self.bowden_allowable_unload_delta = self.bowden_allowable_load_delta = gcmd.get_float('BOWDEN_ALLOWABLE_LOAD_DELTA', self.bowden_allowable_load_delta, minval=1., maxval=50.)
        self.bowden_pre_unload_test = gcmd.get_int('BOWDEN_PRE_UNLOAD_TEST', self.bowden_pre_unload_test, minval=0, maxval=1)

        self.extruder_homing_endstop = gcmd.get('EXTRUDER_HOMING_ENDSTOP', self.extruder_homing_endstop)
        if self.extruder_homing_endstop not in self.EXTRUDER_ENDSTOPS:
            raise gcmd.error("extruder_homing_endstop is invalid. Options are: %s" % self.EXTRUDER_ENDSTOPS)
        self.extruder_homing_max = gcmd.get_float('EXTRUDER_HOMING_MAX', self.extruder_homing_max, above=10.)
        self.extruder_force_homing = gcmd.get_int('EXTRUDER_FORCE_HOMING', self.extruder_force_homing, minval=0, maxval=1)

        self.toolhead_homing_max = gcmd.get_float('TOOLHEAD_HOMING_MAX', self.toolhead_homing_max, minval=0.)
        self.toolhead_entry_to_extruder = gcmd.get_float('TOOLHEAD_ENTRY_TO_EXTRUDER', self.toolhead_entry_to_extruder, minval=0.)
        self.toolhead_sensor_to_nozzle = gcmd.get_float('TOOLHEAD_SENSOR_TO_NOZZLE', self.toolhead_sensor_to_nozzle, minval=0.)
        self.toolhead_extruder_to_nozzle = gcmd.get_float('TOOLHEAD_EXTRUDER_TO_NOZZLE', self.toolhead_extruder_to_nozzle, minval=0.)
        self.toolhead_ooze_reduction = gcmd.get_float('TOOLHEAD_OOZE_REDUCTION', self.toolhead_ooze_reduction, minval=0.)
        self.gcode_load_sequence = gcmd.get_int('GCODE_LOAD_SEQUENCE', self.gcode_load_sequence, minval=0, maxval=1)
        self.gcode_unload_sequence = gcmd.get_int('GCODE_UNLOAD_SEQUENCE', self.gcode_unload_sequence, minval=0, maxval=1)

        # Software behavior options
        self.z_hop_height_toolchange = gcmd.get_float('Z_HOP_HEIGHT_TOOLCHANGE', self.z_hop_height_toolchange, minval=0.)
        self.z_hop_height_error = gcmd.get_float('Z_HOP_HEIGHT_ERROR', self.z_hop_height_error, minval=0.)
        self.z_hop_speed = gcmd.get_float('Z_HOP_SPEED', self.z_hop_speed, minval=1.)
        self.selector_touch = self.ENDSTOP_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enable
        self.enable_endless_spool = gcmd.get_int('ENABLE_ENDLESS_SPOOL', self.enable_endless_spool, minval=0, maxval=1)
        self.endless_spool_on_load = gcmd.get_int('ENDLESS_SPOOL_ON_LOAD', self.endless_spool_on_load, minval=0, maxval=1)
        self.enable_spoolman = gcmd.get_int('ENABLE_SPOOLMAN', self.enable_spoolman, minval=0, maxval=1)
        self.log_level = gcmd.get_int('LOG_LEVEL', self.log_level, minval=0, maxval=4)
        self.log_file_level = gcmd.get_int('LOG_FILE_LEVEL', self.log_file_level, minval=0, maxval=4)
        self.log_visual = gcmd.get_int('LOG_VISUAL', self.log_visual, minval=0, maxval=2)
        self.log_statistics = gcmd.get_int('LOG_STATISTICS', self.log_statistics, minval=0, maxval=1)
        self.console_gate_stat = gcmd.get('CONSOLE_GATE_STAT', self.console_gate_stat)
        if self.console_gate_stat not in self.GATE_STATS_TYPES:
            raise gcmd.error("console_gate_stat is invalid. Options are: %s" % self.GATE_STATS_TYPES)
        self.slicer_tip_park_pos = gcmd.get_float('SLICER_TIP_PARK_POS', self.slicer_tip_park_pos, minval=0.)
        self.force_form_tip_standalone = gcmd.get_int('FORCE_FORM_TIP_STANDALONE', self.force_form_tip_standalone, minval=0, maxval=1)
        self.strict_filament_recovery = gcmd.get_int('STRICT_FILAMENT_RECOVERY', self.strict_filament_recovery, minval=0, maxval=1)
        self.filament_recovery_on_pause = gcmd.get_int('FILAMENT_RECOVERY_ON_PAUSE', self.filament_recovery_on_pause, minval=0, maxval=1)
        self.encoder_move_validation = gcmd.get_int('ENCODER_MOVE_VALIDATION', self.encoder_move_validation, minval=0, maxval=1)
        self.auto_calibrate_gates = gcmd.get_int('AUTO_CALIBRATE_GATES', self.auto_calibrate_gates, minval=0, maxval=1)
        self.retry_tool_change_on_error = gcmd.get_int('RETRY_TOOL_CHANGE_ON_ERROR', self.retry_tool_change_on_error, minval=0, maxval=1)
        self.print_start_detection = gcmd.get_int('PRINT_START_DETECTION', self.print_start_detection, minval=0, maxval=1)
        self.show_error_dialog = gcmd.get_int('SHOW_ERROR_DIALOG', self.show_error_dialog, minval=0, maxval=1)
        form_tip_macro = gcmd.get('FORM_TIP_MACRO', self.form_tip_macro)
        if form_tip_macro != self.form_tip_macro:
            self.form_tip_vars = None # If macro is changed invalidate defaults
        self.form_tip_macro = form_tip_macro

        # Calibration
        self.calibrated_bowden_length = gcmd.get_float('MMU_CALIBRATION_BOWDEN_LENGTH', self.calibrated_bowden_length, minval=10.)

        # Available only with encoder
        if self._has_encoder():
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

        msg = "SPEEDS:"
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
        msg += "\nselector_move_speed = %.1f" % self.selector_move_speed
        msg += "\nselector_homing_speed = %.1f" % self.selector_homing_speed
        msg += "\nselector_touch_speed = %.1f" % self.selector_touch_speed
        msg += "\nselector_touch_enable = %d" % self.selector_touch_enable

        msg += "\n\nTMC & MOTOR SYNC CONTROL:"
        msg += "\nsync_to_extruder = %d" % self.sync_to_extruder
        msg += "\nsync_form_tip = %d" % self.sync_form_tip
        msg += "\nsync_feedback_enable = %d" % self.sync_feedback_enable
        msg += "\nsync_multiplier_high = %.2f" % self.sync_multiplier_high
        msg += "\nsync_multiplier_low = %.2f" % self.sync_multiplier_low
        msg += "\nsync_gear_current = %d" % self.sync_gear_current
        msg += "\nextruder_homing_current = %d" % self.extruder_homing_current
        msg += "\nextruder_form_tip_current = %d" % self.extruder_form_tip_current

        msg += "\n\nLOADING/UNLOADING:"
        msg += "\ngate_homing_endstop = %s" % self.gate_homing_endstop
        if self.gate_homing_endstop == self.ENDSTOP_GATE and self._has_encoder():
            msg += "\ngate_endstop_to_encoder = %s" % self.gate_endstop_to_encoder
        msg += "\ngate_parking_distance = %s" % self.gate_parking_distance
        if self._has_encoder():
            msg += "\nbowden_apply_correction = %d" % self.bowden_apply_correction
            msg += "\nbowden_allowable_load_delta = %d" % self.bowden_allowable_load_delta
            msg += "\nbowden_pre_unload_test = %d" % self.bowden_pre_unload_test
        msg += "\nextruder_force_homing = %d" % self.extruder_force_homing
        msg += "\nextruder_homing_endstop = %s" % self.extruder_homing_endstop
        msg += "\nextruder_homing_max = %.1f" % self.extruder_homing_max
        msg += "\ntoolhead_extruder_to_nozzle = %.1f" % self.toolhead_extruder_to_nozzle
        if self._has_sensor(self.ENDSTOP_TOOLHEAD):
            msg += "\ntoolhead_sensor_to_nozzle = %.1f" % self.toolhead_sensor_to_nozzle
            msg += "\ntoolhead_homing_max = %.1f" % self.toolhead_homing_max
        if self._has_sensor(self.ENDSTOP_EXTRUDER):
            msg += "\ntoolhead_entry_to_extruder = %.1f" % self.toolhead_entry_to_extruder
        msg += "\ntoolhead_ooze_reduction = %.1f" % self.toolhead_ooze_reduction
        msg += "\ngcode_load_sequence = %d" % self.gcode_load_sequence
        msg += "\ngcode_unload_sequence = %d" % self.gcode_unload_sequence

        msg += "\n\nTIP FORMING:"
        msg += "\nform_tip_macro = %s" % self.form_tip_macro
        msg += "\nslicer_tip_park_pos = %.1f" % self.slicer_tip_park_pos
        msg += "\nforce_form_tip_standalone = %d" % self.force_form_tip_standalone

        msg += "\n\nOTHER:"
        msg += "\nz_hop_height_toolchange = %.1f" % self.z_hop_height_toolchange
        msg += "\nz_hop_height_error = %.1f" % self.z_hop_height_error
        msg += "\nz_hop_speed = %.1f" % self.z_hop_speed
        if self._has_encoder():
            msg += "\nenable_clog_detection = %d" % self.enable_clog_detection
        msg += "\nenable_endless_spool = %d" % self.enable_endless_spool
        msg += "\nendless_spool_on_load = %d" % self.endless_spool_on_load
        msg += "\nenable_spoolman = %d" % self.enable_spoolman
        if self._has_encoder():
            msg += "\nstrict_filament_recovery = %d" % self.strict_filament_recovery
            msg += "\nencoder_move_validation = %d" % self.encoder_move_validation
            msg += "\nauto_calibrate_gates = %d" % self.auto_calibrate_gates
        msg += "\nfilament_recovery_on_pause = %d" % self.filament_recovery_on_pause
        msg += "\nretry_tool_change_on_error = %d" % self.retry_tool_change_on_error
        msg += "\nprint_start_detection = %d" % self.print_start_detection
        msg += "\nshow_error_dialog = %d" % self.show_error_dialog
        msg += "\nlog_level = %d" % self.log_level
        msg += "\nlog_file_level = %d" % self.log_file_level
        if self.mmu_logger:
            msg += "\nlog_visual = %d" % self.log_visual
        msg += "\nlog_statistics = %d" % self.log_statistics
        msg += "\nconsole_gate_stat = %s" % self.console_gate_stat

        msg += "\n\nCALIBRATION:"
        msg += "\nmmu_calibration_bowden_length = %.1f" % self.calibrated_bowden_length
        if self._has_encoder():
            msg += "\nmmu_calibration_clog_length = %.1f" % self.encoder_sensor.get_clog_detection_length()
        self._log_info(msg)


###########################################
# RUNOUT, ENDLESS SPOOL and GATE HANDLING #
###########################################

    def _runout(self, force_runout=False):
        if self.tool_selected < 0:
            raise MmuError("Filament runout or clog on an unknown or bypass tool - manual intervention is required")

        if self.filament_pos != self.FILAMENT_POS_LOADED and not force_runout:
            raise MmuError("Filament runout or clog occured but filament is not fully loaded! - manual intervention is required")

        self._log_info("Issue on tool T%d" % self.tool_selected)
        self._save_toolhead_position_and_lift("runout", z_hop_height=self.z_hop_height_toolchange)
        self._wrap_gcode_command("PAUSE", exception=True) # Should be after toolhead position is saved

        # Check for clog by looking for filament at the gate (or in the encoder)
        if not force_runout:
            self._log_debug("Checking if this is a clog or a runout (state %d)..." % self.filament_pos)
            if self._check_filament_at_gate():
                if self._has_encoder():
                    self.encoder_sensor.update_clog_detection_length()
                raise MmuError("A clog has been detected and requires manual intervention")

        # We have a filament runout
        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during swap
            self._log_error("A runout has been detected")
            self.is_handling_runout = True # Will remain true until complete and continue or resume after error

            if self.enable_endless_spool:
                self._set_gate_status(self.gate_selected, self.GATE_EMPTY) # Indicate current gate is empty
                next_gate, checked_gates = self._get_next_endless_spool_gate(self.tool_selected, self.gate_selected)

                if next_gate == -1:
                    raise MmuError("No EndlessSpool alternatives available after reviewing gates: %s" % checked_gates)
                self._log_info("Remapping T%d to Gate %d" % (self.tool_selected, next_gate))

                # TODO perhaps figure out how to call _change_tool() here for better user feeback
                self._unload_tool(runout=True)
                self._remap_tool(self.tool_selected, next_gate)
                self._select_and_load_tool(self.tool_selected)
            else:
                raise MmuError("EndlessSpool mode is off - manual intervention is required")

        self._check_runout() # Can throw MmuError
        self._wrap_gcode_command("RESUME", exception=True)
        self._continue_printing("endless_spool") # Continue printing...

    def _get_next_endless_spool_gate(self, tool, gate):
        group = self.endless_spool_groups[gate]
        self._log_info("EndlessSpool checking for additional gates in Group_%d for T%d..." % (group, tool))
        next_gate = -1
        checked_gates = []
        for i in range(self.mmu_num_gates - 1):
            check = (gate + i + 1) % self.mmu_num_gates
            if self.endless_spool_groups[check] == group:
                checked_gates.append(check)
                if self.gate_status[check] != self.GATE_EMPTY:
                    next_gate = check
                    break
        return next_gate, checked_gates

    def _set_tool_to_gate(self, tool, gate):
        self.ttg_map[tool] = gate
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map))

    def _set_gate_status(self, gate, state):
        if gate >= 0:
            if state != self.gate_status[gate]:
                self.gate_status[gate] = state
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_STATUS, self.gate_status))
                if self.printer.lookup_object("gcode_macro %s" % self.gate_map_changed_macro, None) is not None:
                    self._wrap_gcode_command("%s GATE='%d'" % (self.gate_map_changed_macro, gate))

    # Use pre-gate sensors (if fitted) to "correct" gate status
    # Return updated gate_status
    def _validate_gate_status(self, gate_status):
        updated = False
        for gate, status in enumerate(gate_status):
            detected = self._check_pre_gate_sensor(gate)
            if detected is True and status == self.GATE_EMPTY:
                gate_status[gate] = self.GATE_UNKNOWN
                updated = True
            elif detected is False and status != self.GATE_EMPTY:
                gate_status[gate] = self.GATE_EMPTY
                updated = True
        return gate_status

    def _get_filament_char(self, gate_status, no_space=False, show_source=False):
        if gate_status == self.GATE_AVAILABLE_FROM_BUFFER:
            return "B" if show_source else "*"
        elif gate_status == self.GATE_AVAILABLE:
            return "S" if show_source else "*"
        elif gate_status == self.GATE_EMPTY:
            return (UI_SEPARATOR if no_space else " ")
        else:
            return "?"

    def _ttg_map_to_string(self, title=None, summary=False, tool=None, show_groups=True):
        msg = "%s:\n" % title if title else "TTG Map:\n" # String used to filter in KS-HH
        if not summary:
            num_tools = self.mmu_num_gates
            tools = range(num_tools) if tool is None else [tool]
            for i in tools:
                gate = self.ttg_map[i]
                filament_char = self._get_filament_char(self.gate_status[gate], show_source=False)
                msg += "\n" if i and tool is None else ""

                if self.enable_endless_spool and show_groups:
                    msg += "T{:<2}".format(i)
                else:
                    msg += "T{:<2}-> Gate{:>2}({})".format(i, gate, filament_char)

                if self.enable_endless_spool:
                    group = self.endless_spool_groups[gate]
                    es = ""
                    if show_groups:
                        msg += " in EndlessSpool Group %s :" % chr(ord('A') + group)
                        gates_in_group = [(j + gate) % num_tools for j in range(num_tools)]
                    else:
                        es = " >"
                        gates_in_group = [(j + gate + 1) % num_tools for j in range(num_tools - 1)]

                    gs = " >".join("{:>2}({})".format(g, self._get_filament_char(self.gate_status[g], show_source=False)) for g in gates_in_group if self.endless_spool_groups[g] == group)
                    if gs:
                        msg += (es + gs)

                if i == self.tool_selected:
                    msg += " [SELECTED]"
        else:
            multi_tool = False
            num_gates = self.mmu_num_gates
            gate_indices = range(num_gates)
            msg_gates = "Gates: " + "".join("|{:^3}".format(g) if g < 10 else "| {:2}".format(g) for g in gate_indices) + "|"
            msg_avail = "Avail: " + "".join("| %s " % self._get_filament_char(self.gate_status[g], no_space=True, show_source=True) for g in gate_indices) + "|"
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
                    select_strings[i] = "| %s " % self._get_filament_char(self.gate_status[g], no_space=True)
            msg_selct = "Selct: " + "".join(select_strings) + ("|" if self.gate_selected == num_gates - 1 else "-")
            msg = "\n".join([msg_gates, msg_tools, msg_avail, msg_selct])
            if self.is_homed:
                msg += " " + self._selected_tool_string()
            else:
                msg += " NOT HOMED"
        return msg

    def _gate_map_to_string(self, detail=False):
        msg = "Gates / Filaments:" # String used to filter in KS-HH
        available_status = {
            self.GATE_AVAILABLE_FROM_BUFFER: "Buffer",
            self.GATE_AVAILABLE: "Spool",
            self.GATE_EMPTY: "Empty",
            self.GATE_UNKNOWN: "Unknown"
        }

        for g in range(self.mmu_num_gates):
            material = self.gate_material[g] or "n/a"
            color = self.gate_color[g] or "n/a"
            available = available_status[self.gate_status[g]]

            gate_detail = ""
            if detail:
                filament_char = self._get_filament_char(self.gate_status[g], show_source=False)
                tools_supported = ", ".join("T{}".format(t) for t in range(self.mmu_num_gates) if self.ttg_map[t] == g)
                tools_str = " supporting {}".format(tools_supported) if tools_supported else "?, "
                gate_detail = "\nGate {}({}){}".format(g, filament_char, tools_str)
                if g == self.gate_selected:
                    gate_detail += " [SELECTED]"
            else:
                gate_detail = "\nGate {}: ".format(g)

            spool_id = str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a"
            spool_info = ", SpoolID: {}".format(spool_id) if self.enable_spoolman else ""
            speed_info = ", Load Speed: {}%".format(self.gate_speed_override[g]) if self.gate_speed_override[g] != 100 else ""
            msg += "{}Status: {}, Material: {}, Color: {}{}{}".format(gate_detail, available, material, color, spool_info, speed_info)
        return msg

    def _remap_tool(self, tool, gate, available=None):
        self._set_tool_to_gate(tool, gate)
        if available is not None:
            self._set_gate_status(gate, available)
        return gate

    def _reset_ttg_mapping(self):
        self._log_debug("Resetting TTG map")
        self.ttg_map = list(self.default_ttg_map)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map))
        self._unselect_tool()

    def _reset_gate_map(self):
        self._log_debug("Resetting gate map")
        self.gate_status = self._validate_gate_status(list(self.default_gate_status))
        self.gate_material = list(self.default_gate_material)
        self._update_gate_color(list(self.default_gate_color))
        self.gate_spool_id = list(self.default_gate_spool_id)
        self.gate_speed_override = list(self.default_gate_speed_override)
        self._persist_gate_map()


### GCODE COMMANDS FOR RUNOUT, TTG MAP, GATE MAP and GATE LOGIC ##################################

    cmd_MMU_TEST_RUNOUT_help = "Manually invoke the clog/runout detection logic for testing"
    def cmd_MMU_TEST_RUNOUT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        try:
            self._runout(True)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_ENCODER_RUNOUT_help = "Internal encoder filament runout handler"
    def cmd_MMU_ENCODER_RUNOUT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()

        try:
            self._log_debug("Filament runout/clog detected by MMU encoder")
            self._runout()
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_ENCODER_INSERT_help = "Internal encoder filament insert detection handler"
    def cmd_MMU_ENCODER_INSERT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()

        self._log_trace("Filament insertion detected by encoder")
        # TODO Future bypass preload feature - make gate act like bypass

    # Callback to handle filament sensor on MMU. If GATE parameter is set then it is a pre-gate
    # sensor that fired.  This is not protected by klipper is_printing check so be careful when
    # runout handle_logic is fired
    cmd_MMU_GATE_RUNOUT_help = "Internal MMU filament runout handler"
    def cmd_MMU_GATE_RUNOUT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        self._fix_started_state()

        try:
            gate = gcmd.get_int('GATE', None)
            do_runout = gcmd.get_int('DO_RUNOUT', 0)
            if gate is not None:
                self._set_gate_status(gate, self.GATE_EMPTY)

            if do_runout:
                if self._is_in_print() and (gate is None or gate == self.gate_selected):
                    self._log_debug("Handling runout detected by MMU %s" % (("pre-gate sensor #%d" % gate) if gate is not None else "gate sensor"))
                    self._runout(True)
                else:
                    self._log_debug("Assertion failure: runout detected but not in print or occured on unexpected gate. Ignored")
                    self.pause_resume.send_resume_command() # Undo what runout sensor handling did
        except MmuError as ee:
            self._mmu_pause(str(ee))

    # This callback is not protected by klipper "is printing" check so be careful
    cmd_MMU_GATE_INSERT_help = "Internal MMU filament detection handler"
    def cmd_MMU_GATE_INSERT(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if not self.is_enabled: return
        try:
            gate = gcmd.get_int('GATE', None)
            if gate is not None:
                self._log_debug("Handling insertion detected by MMU %s" % (("pre-gate sensor #%d" % gate) if gate is not None else "gate sensor"))
                self._set_gate_status(gate, self.GATE_UNKNOWN)
                if not self._is_in_print():
                    self.gcode.run_script_from_command("MMU_PRELOAD GATE=%d" % gate)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_M400_help = "Wait on both move queues"
    def cmd_MMU_M400(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        self._movequeues_wait_moves(toolhead=True, mmu_toolhead=True)

    cmd_MMU_TTG_MAP_help = "aka MMU_REMAP_TTG Display or remap a tool to a specific gate and set gate availability"
    def cmd_MMU_TTG_MAP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        ttg_map = gcmd.get('MAP', "!")
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        available = gcmd.get_int('AVAILABLE', self.GATE_UNKNOWN, minval=self.GATE_EMPTY, maxval=self.GATE_AVAILABLE)

        if reset == 1:
            self._reset_ttg_mapping()
        elif ttg_map != "!":
            ttg_map = gcmd.get('MAP').split(",")
            if len(ttg_map) != self.mmu_num_gates:
                self._log_always("The number of map values (%d) is not the same as number of gates (%d)" % (len(ttg_map), self.mmu_num_gates))
                return
            self.ttg_map = []
            for gate in ttg_map:
                if gate.isdigit():
                    self.ttg_map.append(int(gate))
                else:
                    self.ttg_map.append(0)
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.ttg_map))
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
            self._log_info(self._ttg_map_to_string(show_groups=False))

    cmd_MMU_GATE_MAP_help = "Display or define the type and color of filaments on each gate"
    def cmd_MMU_GATE_MAP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        refresh = bool(gcmd.get_int('REFRESH', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}") # Hidden option for bulk update from moonraker component
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)

        try:
            gate_map = ast.literal_eval(gmapstr)
        except Exception as e:
            self._log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))

        if reset:
            self._reset_gate_map()

        elif refresh:
            self._update_filaments_from_spoolman()
            quiet = True

        elif not gate_map == {}:
            self._log_debug("Received gate map update from Spoolman: %s" % gmapstr)
            for gate, fil in gate_map.items():
                if self.gate_spool_id[gate] == fil['spool_id']:
                    self.gate_material[gate] = fil['material']
                    self.gate_color[gate] = fil['color']
                else:
                    self._log_debug("Assertion failure: Spool_id changed for Gate %d in MMU_GATE_MAP. Dict=%s" % (gate, fil))

            self._update_gate_color(self.gate_color)
            self._persist_gate_map() # This will also update LED status

        elif gates != "!" or gate >= 0:
            gatelist = []
            if gates != "!":
                # List of gates
                try:
                    for gate in gates.split(','):
                        gate = int(gate)
                        if gate >= 0 and gate < self.mmu_num_gates:
                            gatelist.append(gate)
                except ValueError as ve:
                    raise gcmd.error("Invalid GATES parameter: %s" % gates)
            else:
                # Specifying one gate (filament)
                gatelist.append(gate)

            for gate in gatelist:
                available = gcmd.get_int('AVAILABLE', self.gate_status[gate], minval=-1, maxval=2)
                material = "".join(gcmd.get('MATERIAL', self.gate_material[gate]).split()).replace('#', '').upper()[:10]
                color = "".join(gcmd.get('COLOR', self.gate_color[gate]).split()).replace('#', '').lower()
                spool_id = gcmd.get_int('SPOOLID', self.gate_spool_id[gate], minval=-1)
                speed_override = gcmd.get_int('SPEED', self.gate_speed_override[gate], minval=10, maxval=150)
                color = self._validate_color(color)
                if color is None:
                    raise gcmd.error("Color specification must be in form 'rrggbb' hexadecimal value (no '#') or valid color name or empty string")
                self.gate_material[gate] = material
                self.gate_color[gate] = color
                self.gate_status[gate] = available
                self.gate_spool_id[gate] = spool_id
                self.gate_speed_override[gate] = speed_override

            self._update_gate_color(self.gate_color)
            self._persist_gate_map() # This will also update LED status
        else:
            quiet = False # Display current map

        if not quiet:
            self._log_info(self._gate_map_to_string())

    cmd_MMU_ENDLESS_SPOOL_help = "Diplay or Manage EndlessSpool functionality and groups"
    def cmd_MMU_ENDLESS_SPOOL(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        enabled = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', "!")

        if enabled >= 0:
            self.enable_endless_spool = enabled
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool))
            if enabled and not quiet:
                self._log_always("EndlessSpool is enabled")
        if not self.enable_endless_spool:
            self._log_always("EndlessSpool is disabled")
            return

        if reset:
            self._log_debug("Resetting EndlessSpool groups")
            self.enable_endless_spool = self.default_enable_endless_spool
            self.endless_spool_groups = self.default_endless_spool_groups

        elif groups != "!":
            groups = gcmd.get('GROUPS', ",".join(map(str, self.endless_spool_groups))).split(",")
            if len(groups) != self.mmu_num_gates:
                self._log_always("The number of group values (%d) is not the same as number of gates (%d)" % (len(groups), self.mmu_num_gates))
                return
            self.endless_spool_groups = []
            for group in groups:
                if group.isdigit():
                    self.endless_spool_groups.append(int(group))
                else:
                    self.endless_spool_groups.append(0)
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_ENDLESS_SPOOL_GROUPS, self.endless_spool_groups))

        else:
            quiet = False # Display current map

        if not quiet:
            self._log_info(self._ttg_map_to_string(title="EndlessSpool Groups"))

    cmd_MMU_TOOL_OVERRIDES_help = "Displays, sets or clears tool speed and extrusion factors (M220 & M221)"
    def cmd_MMU_TOOL_OVERRIDES(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates)
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
        for i in range(self.mmu_num_gates):
            range_end = 6 if i > 9 else 5
            tool_speed = int(self.tool_speed_multipliers[i] * 100)
            tool_extr = int(self.tool_extrusion_multipliers[i] * 100)
            msg_tool += ("| T%d  " % i)[:range_end]
            msg_sped += ("| %d  " % tool_speed)[:range_end]
            msg_extr += ("| %d  " % tool_extr)[:range_end]
        msg = "|\n".join([msg_tool, msg_sped, msg_extr]) + "|\n"
        self._log_always(msg)

    cmd_MMU_SLICER_TOOL_MAP_help = "Display or define the tools used in print as specified by slicer"
    def cmd_MMU_SLICER_TOOL_MAP(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        display = bool(gcmd.get_int('DISPLAY', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        initial_tool = gcmd.get_int('INITIAL_TOOL', None, minval=0, maxval=self.mmu_num_gates - 1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        material = gcmd.get('MATERIAL', "unknown")
        color = gcmd.get('COLOR', "").lower()
        temp = gcmd.get_int('TEMP', 0, minval=0)
        purge_volumes = gcmd.get('PURGE_VOLUMES', "")

        quiet = False
        if reset:
            self._clear_slicer_tool_map()
            quiet = True
        if tool >= 0:
            self.slicer_tool_map['tools'][str(tool)] = {'color': color, 'material': material, 'temp': temp}
            quiet = True
        if initial_tool is not None:
            self.slicer_tool_map['initial_tool'] = initial_tool
            quiet = True
        if purge_volumes != "":
            try:
                volumes = list(map(float, purge_volumes.split(',')))
                n = len(volumes)
                num_tools = self.mmu_num_gates
                if num_tools ** 2 == n:
                    # Full NxN matrix supplied
                    self.slicer_tool_map['purge_volumes'] = [volumes[i * num_tools : (i + 1) * num_tools] for i in range(num_tools)]
                else:
                    if n == 1:
                        calc = lambda x,y: volumes[0] * 2 # Build a single value matrix
                    elif num_tools == n:
                        calc = lambda x,y: volumes[x] + volumes[y] # Will build symmetrical purge matrix
                    elif num_tools * 2 == n:
                        calc = lambda x,y: volumes[x] + volumes[num_tools + y] # Build matrix with sum of unload and load tools
                    else:
                        raise gcmd.error("Incorrect number of values for PURGE_VOLUMES. Expect 1, %d, %d, or %d, got %d" % (num_tools, num_tools * 2, num_tools ** 2, n))
                    self.slicer_tool_map['purge_volumes'] = [
                        [
                            calc(x,y) if x != y else 0
                                for y in range(num_tools)
                        ]
                        for x in range(num_tools)
                    ]
            except ValueError as e:
                raise gcmd.error("Error parsing PURGE_VOLUMES: %s" % str(e))
            quiet = True

        if display or not quiet:
            colors = len(self.slicer_tool_map['tools'])
            have_purge_map = len(self.slicer_tool_map['purge_volumes']) > 0
            msg = "No slicer tool map loaded"
            if colors > 0 or self.slicer_tool_map['initial_tool'] is not None:
                msg = "--------- Slicer MMU Tool Summary ---------\n"
                msg += "Single color print" if colors <= 1 else "%d color print" % colors
                msg += " (Purge volume map loaded)\n" if colors > 1 and have_purge_map else "\n"
                for t, params in self.slicer_tool_map['tools'].items():
                    msg += "T%d (Gate %d, %s, %s, %d%sC)\n" % (int(t), self.ttg_map[int(t)], params['material'], params['color'], params['temp'], UI_DEGREE)
                if self.slicer_tool_map['initial_tool'] is not None:
                    msg += "Initial Tool: T%d\n" % self.slicer_tool_map['initial_tool']
                msg += "-------------------------------------------"
            if detail:
                if have_purge_map:
                    #msg += "\n".join([" ".join(map(lambda x: str(round(x)).rjust(4, "\u2800"), row)) for row in self.slicer_tool_map['purge_volumes']])
                    msg += "\nPurge Volume Map:\n"
                    msg += "To ->" + UI_SEPARATOR.join("{}T{: <2}".format(UI_SPACE, i) for i in range(self.mmu_num_gates)) + "\n"
                    msg += '\n'.join(["T{: <2}{}{}".format(i, UI_SEPARATOR, ' '.join(map(lambda x: str(round(x)).rjust(4, UI_SPACE) if x > 0 else "{}{}-{}".format(UI_SPACE, UI_SPACE, UI_SPACE), row))) for i, row in enumerate(self.slicer_tool_map['purge_volumes'])])
            elif have_purge_map:
                msg += "\nDETAIL=1 to see purge volumes"
            self._log_always(msg)

    # TODO default to current gate; MMU_CHECK_GATES default to all gates. Add ALL=1 flag
    cmd_MMU_CHECK_GATE_help = "Automatically inspects gate(s), parks filament and marks availability"
    def cmd_MMU_CHECK_GATE(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(): return
        self._fix_started_state()

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        # These three parameters are mutually exclusive so we only process one
        tools = gcmd.get('TOOLS', "!")
        gates = gcmd.get('GATES', "!")
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)

        with self._wrap_suspend_runout(): # Don't want runout accidently triggering during gate check
            with self._wrap_action(self.ACTION_CHECKING):
                try:
                    tool_selected = self.tool_selected
                    filament_pos = self.filament_pos
                    self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
                    gates_tools = []
                    if tools != "!":
                        # Tools used in print (may be empty list)
                        try:
                            for tool in tools.split(','):
                                if not tool == "":
                                    tool = int(tool)
                                    if tool >= 0 and tool < self.mmu_num_gates:
                                        gate = self.ttg_map[tool]
                                        gates_tools.append([gate, tool])
                            if len(gates_tools) == 0:
                                self._log_debug("No tools to check, assuming default tool is already loaded")
                                return
                        except ValueError as ve:
                            msg = "Invalid TOOLS parameter: %s" % tools
                            if self._is_printing():
                                self._mmu_pause(msg)
                            else:
                                self._log_always(msg)
                            return
                    elif gates != "!":
                        # List of gates
                        try:
                            for gate in gates.split(','):
                                gate = int(gate)
                                if gate >= 0 and gate < self.mmu_num_gates:
                                    gates_tools.append([gate, -1])
                        except ValueError as ve:
                            self._log_always("Invalid GATES parameter: %s" % gates)
                            return
                    elif tool >= 0:
                        # Individual tool
                        gate = self.ttg_map[tool]
                        gates_tools.append([gate, tool])
                    elif gate >= 0:
                        # Individual gate
                        gates_tools.append([gate, -1])
                    else:
                        # No parameters means all gates
                        for gate in range(self.mmu_num_gates):
                            gates_tools.append([gate, -1])

                    # Force initial eject
                    try:
                        if not filament_pos == self.FILAMENT_POS_UNLOADED:
                            self._log_info("Unloading current tool prior to checking gates")
                            self._unload_tool()
                    except MmuError as ee:
                        self._mmu_pause(str(ee))
                        return

                    if len(gates_tools) > 1:
                        self._log_info("Will check gates: %s" % ', '.join(str(g) for g,t in gates_tools))
                    for gate, tool in gates_tools:
                        try:
                            self._select_gate(gate)
                            self._initialize_filament_position() # Encoder 0000
                            self.calibrating = True # To suppress visual filament position
                            self._log_info("Checking Gate %d..." % gate)
                            self._load_gate(allow_retry=False, adjust_servo_on_error=False)
                            if tool >= 0:
                                self._log_info("Tool T%d - Filament detected. Gate %d marked available" % (tool, gate))
                            else:
                                self._log_info("Gate %d - Filament detected. Marked available" % gate)
                            self._set_gate_status(gate, max(self.gate_status[gate], self.GATE_AVAILABLE))
                            try:
                                self._unload_gate()
                            except MmuError as ee:
                                msg = "Failure during check Gate %d %s: %s" % (gate, "(T%d)" % tool if tool >= 0 else "", str(ee))
                                if self._is_printing():
                                    self._mmu_pause(msg)
                                else:
                                    self._log_always(msg)
                                return
                        except MmuError as ee:
                            self._set_gate_status(gate, self.GATE_EMPTY)
                            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=True)
                            if tool >= 0:
                                msg = "Tool T%d on Gate %d marked EMPTY" % (tool, gate)
                            else:
                                msg = "Gate %d marked EMPTY" % gate
                            if self._is_printing():
                                # Use case of in-print verification of all tools used in print
                                self._mmu_pause("Required " + msg)
                                self._log_debug("Reason: %s" % str(ee))
                                return
                            else:
                                self._log_info(msg)
                                self._log_debug("Reason: %s" % str(ee))
                        finally:
                            self.calibrating = False

                    # If not printing select original tool and load filament if necessary
                    # We don't do this when printing because this is expected to preceed the loading initial tool
                    if not self._is_printing():
                        try:
                            if tool_selected == self.TOOL_GATE_BYPASS:
                                self._select_bypass()
                            elif tool_selected != self.TOOL_GATE_UNKNOWN:
                                if filament_pos == self.FILAMENT_POS_LOADED:
                                    self._log_info("Restoring tool loaded prior to checking gates")
                                    self._select_and_load_tool(tool_selected)
                                else:
                                    self._select_tool(tool_selected)
                        except MmuError as ee:
                            self._log_always("Failure re-selecting Tool %d: %s" % (tool_selected, str(ee)))

                    if not quiet:
                        self._log_info(self._ttg_map_to_string(summary=True))
                finally:
                    self._servo_auto()

    cmd_MMU_PRELOAD_help = "Preloads filament at specified or current gate"
    def cmd_MMU_PRELOAD(self, gcmd):
        self._log_to_file(gcmd.get_commandline())
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(): return

        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        self._log_always("Preloading filament in %s" % (("Gate %d" % gate) if gate >= 0 else "current gate"))
        with self._wrap_action(self.ACTION_CHECKING):
            try:
                self.calibrating = True # To suppress visual filament position display
                # If gate not specified assume current gate
                if gate == -1:
                    gate = self.gate_selected
                else:
                    self._select_gate(gate)
                self._initialize_filament_position()    # Encoder 0000
                for i in range(5):
                    self._log_always("Loading...")
                    try:
                        self._load_gate(allow_retry=False, adjust_servo_on_error=False)
                        # Caught the filament, so now park it in the gate
                        self._log_always("Parking...")
                        self._unload_gate()
                        self._log_always("Filament detected and parked in Gate %d" % gate)
                        return
                    except MmuError as ee:
                        # Exception just means filament is not loaded yet, so continue
                        self._log_trace("Exception on preload: %s" % str(ee))
                self._log_always("Filament not detected in Gate %d" % gate)
                self._set_gate_status(gate, self.GATE_EMPTY)
            except MmuError as ee:
                self._log_always("Filament preload for Gate %d failed: %s" % (gate, str(ee)))
            finally:
                self.calibrating = False
                self._servo_auto()

def load_config(config):
    return Mmu(config)
