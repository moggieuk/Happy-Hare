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
import logging, logging.handlers, threading, queue, time, contextlib, math, os.path, re
from random import randint
from extras.mmu_toolhead import MmuToolHead, MmuHoming
from extras.homing import Homing, HomingMove
from extras.mmu_led_effect import MmuLedEffect
import chelper, ast

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

# Class to track filament sensor state
class SimpleButtonHandler:
    handlers = {}
    def __init__(self, sensor_name):
        self.name = sensor_name
        self.enabled = True
        self.filament_present = False
        SimpleButtonHandler.handlers[sensor_name] = self

    def note_filament_present(self, eventtime, state):
        self.filament_present = state

    @staticmethod
    def is_filament_present(sensor_name):
        return SimpleButtonHandler.handlers[sensor_name].filament_present

# Mmu exception error class
class MmuError(Exception):
    pass

# Main klipper module
class Mmu:
    VERSION = 2.3		# When this is revved, Happy Hare will instruct users to re-run ./install.sh. Sync with install.sh!

    BOOT_DELAY = 2.0            # Delay before running bootup tasks

    # Calibration steps
    CALIBRATED_GEAR     = 0b00001
    CALIBRATED_ENCODER  = 0b00010
    CALIBRATED_SELECTOR = 0b00100
    CALIBRATED_BOWDEN   = 0b01000
    CALIBRATED_GATES    = 0b10000
    CALIBRATED_ALL      = 0b01111 # Calibrated gates is optional

    SERVO_MOVE_STATE = 2 # NEW V2
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
    FILAMENT_POS_START_BOWDEN = 1
    FILAMENT_POS_IN_BOWDEN = 2
    FILAMENT_POS_END_BOWDEN = 3
    FILAMENT_POS_HOMED_EXTRUDER = 4
    FILAMENT_POS_EXTRUDER_ENTRY = 5
    FILAMENT_POS_HOMED_TS = 6
    FILAMENT_POS_IN_EXTRUDER = 7    # AKA FILAMENT_POS_PAST_TS
    FILAMENT_POS_LOADED = 8         # AKA FILAMENT_POS_HOMED_NOZZLE

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

    # Standard endstop or pseudo endstop names
    ENDSTOP_EXTRUDER_COLLISION = "collision" # Fake endstop
    ENDSTOP_ENCODER            = "encoder"   # Fake endstop
    ENDSTOP_EXTRUDER_TOUCH     = "mmu_ext_touch"
    ENDSTOP_GEAR_TOUCH         = "mmu_gear_touch"
    ENDSTOP_GATE               = "mmu_gate"
    ENDSTOP_EXTRUDER           = "extruder"
    ENDSTOP_TOOLHEAD           = "toolhead"
    ENDSTOP_SELECTOR_TOUCH     = "mmu_sel_touch"
    ENDSTOP_SELECTOR_HOME      = "mmu_sel_home"

    EXTRUDER_ENDSTOPS = [ENDSTOP_EXTRUDER_COLLISION, ENDSTOP_GEAR_TOUCH, ENDSTOP_EXTRUDER]
    GATE_ENDSTOPS     = [ENDSTOP_GATE, ENDSTOP_ENCODER]

    # Stepper config sections
    SELECTOR_STEPPER_CONFIG    = "stepper_mmu_selector"
    GEAR_STEPPER_CONFIG        = "stepper_mmu_gear"

    # Vendor MMU's supported
    VENDOR_ERCF     = "ERCF"
    VENDOR_TRADRACK = "Tradrack"
    VENDOR_PRUSA    = "Prusa" # In progress
    VENDOR_OTHER    = "Other"

    # mmu_vars.cfg variables
    VARS_MMU_CALIB_CLOG_LENGTH      = "mmu_calibration_clog_length"
    VARS_MMU_ENABLE_ENDLESS_SPOOL   = "mmu_state_enable_endless_spool"
    VARS_MMU_ENDLESS_SPOOL_GROUPS   = "mmu_state_endless_spool_groups"
    VARS_MMU_TOOL_TO_GATE_MAP       = "mmu_state_tool_to_gate_map"
    VARS_MMU_GATE_STATUS            = "mmu_state_gate_status"
    VARS_MMU_GATE_MATERIAL          = "mmu_state_gate_material"
    VARS_MMU_GATE_COLOR             = "mmu_state_gate_color"
    VARS_MMU_GATE_SPOOL_ID          = "mmu_state_gate_spool_id"
    VARS_MMU_GATE_SELECTED          = "mmu_state_gate_selected"
    VARS_MMU_TOOL_SELECTED          = "mmu_state_tool_selected"
    VARS_MMU_FILAMENT_POS           = "mmu_state_filament_pos"
    VARS_MMU_CALIB_BOWDEN_LENGTH    = "mmu_calibration_bowden_length"
    VARS_MMU_CALIB_PREFIX           = "mmu_calibration_"
    VARS_MMU_GATE_STATISTICS_PREFIX = "mmu_statistics_gate_"
    VARS_MMU_SWAP_STATISTICS        = "mmu_statistics_swaps"
    VARS_MMU_SELECTOR_OFFSETS       = "mmu_selector_offsets"
    VARS_MMU_SELECTOR_BYPASS        = "mmu_selector_bypass"
    VARS_MMU_ENCODER_RESOLUTION     = "mmu_encoder_resolution"
    VARS_MMU_GEAR_ROTATION_DISTANCE = "mmu_gear_rotation_distance"

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

    UPGRADE_REMINDER = "Happy Hare minor version has changed which requires you to re-run\n'./install.sh' to update configuration files and klipper modules.\nMore details: https://github.com/moggieuk/Happy-Hare/blob/main/doc/upgrade.md"

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

        self.mmu_vendor = config.get('mmu_vendor', self.VENDOR_ERCF)
        self.mmu_version_string = config.get('mmu_version', "1.1")
        self.mmu_version = float(re.sub("[^0-9.]", "", self.mmu_version_string))
        self.virtual_selector = False # TODO untested WIP

        # Set CAD default parameters to ensure everything is set
        # These are default for ERCFv1.1 - the first MMU supported by Happy Hare
        #  cad_gate0_pos          - distance from endstop to first gate
        #  cad_gate_width         - width of each gate
        #  cad_block_width        - width of bearing block (ERCF v1.1)
        #  cad_bypass_block_width - width of bypass block (ERCF v1.1)
        #  cad_bypass_block_delta - distance from previous gate to bypass (ERCF v1.1)
        #  cad_bypass_offset      - distance from end of travel to the bypass
        #  cad_last_gate_offset   - distance from end of travel to last gate
        self.cad_gate0_pos = 4.2
        self.cad_gate_width = 21.
        self.cad_bypass_offset = 0.
        self.cad_last_gate_offset = 2.0

        self.cad_block_width = 5.
        self.cad_bypass_block_width = 6.
        self.cad_bypass_block_delta = 9.

        self.gate_parking_distance = 23.
        self.gate_endstop_to_encoder = 0.
        self.encoder_default_resolution = bmg_circ / (2 * 17) # TRCT5000 based sensor

        # Specific vendor build parameters / tuning. Mostly CAD related but a few exceptions like gate_park_distance
        if self.mmu_vendor.lower() == self.VENDOR_ERCF.lower():
            if self.mmu_version >= 2.0: # V2 community edition
                self.cad_gate0_pos = 4.0
                self.cad_gate_width = 23.
                self.cad_bypass_offset = 0.72
                self.cad_last_gate_offset = 14.4

                # Non CAD default parameters
                self.gate_parking_distance = 13.
                self.encoder_default_resolution = bmg_circ / (2 * 12) # Binky 12 tooth disc with BMG gear

                # Modifications:
                #  h = ThumperBlocks filament blocks
                if "h" in self.mmu_version_string:
                    self.cad_gate_width = 21.
                    self.gate_parking_distance = 11.

            else: # V1.1 original
                # Modifications:
                #  t = TripleDecky filament blocks
                #  s = Springy sprung servo selector
                #  b = Binky encoder upgrade
                if "t" in self.mmu_version_string:
                    self.cad_gate_width = 23. # Triple Decky is wider filament block
                    self.cad_block_width = 0. # Bearing blocks are not used
                    self.gate_parking_distance = 13. # Filament trap in block

                if "s" in self.mmu_version_string:
                    self.cad_last_gate_offset = 1.2 # Springy has additional bump stops

                if "b" in self.mmu_version_string:
                    self.encoder_default_resolution = bmg_circ / (2 * 12) # Binky 12 tooth disc with BMG gear

        elif self.mmu_vendor.lower() == self.VENDOR_TRADRACK.lower():
            self.cad_gate0_pos = 0.5
            self.cad_gate_width = 17.
            self.cad_bypass_offset = 0 # Doesn't have bypass
            self.cad_last_gate_offset = 1. # TODO this is a guess

            self.gate_parking_distance = 17. # Using Gate switch (had user reports from 15 - 17.5)
            self.encoder_default_resolution = bmg_circ / (2 * 12) # If fitted, assumed to by Binky

            # Modifications:
            #  e = has encoder modification
            #      Note: if have encoder but want to use gate sensor to part, then `gate_endstop_to_encoder`
            #            would need to be set and `gate_parking_distance` set back to original
            if "e" in self.mmu_version_string:
                self.gate_parking_distance = 39. # Assume using encoder if we have it
                self.gate_endstop_to_encoder = 15. # TODO this is a guess

        elif self.mmu_vendor.lower() == self.VENDOR_PRUSA.lower():
            raise self.config.error("Support for Prusa systems is comming soon! You can try with vendor=Other and configure `cad` dimensions (see doc)")

        else:
            # Some arbitary starting values for "Other" designs
            self.cad_gate0_pos = 1.
            self.cad_gate_width = 20.
            self.cad_bypass_offset = 1.
            self.cad_last_gate_offset = 10.
            self.gate_parking_distance = 20.

        # Allow CAD parameters to be customized
        self.cad_gate0_pos = config.getfloat('cad_gate0_pos', self.cad_gate0_pos, minval=0.)
        self.cad_gate_width = config.getfloat('cad_gate_width', self.cad_gate_width, above=0.)
        self.cad_bypass_offset = config.getfloat('cad_bypass_offset', self.cad_bypass_offset, minval=0.)
        self.cad_last_gate_offset = config.getfloat('cad_last_gate_offset', self.cad_last_gate_offset, above=0.)

        self.cad_block_width = config.getfloat('cad_block_width', self.cad_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_width = config.getfloat('cad_bypass_block_width', self.cad_bypass_block_width, above=0.) # ERCF v1.1 only
        self.cad_bypass_block_delta = config.getfloat('cad_bypass_block_delta', self.cad_bypass_block_delta, above=0.) # ERCF v1.1 only

        self.cad_selector_tolerance = config.getfloat('cad_selector_tolerance', 10., above=0.) # Extra movement allowed by selector

        # Printer interaction config
        self.extruder_name = config.get('extruder', 'extruder')
        self.timeout_pause = config.getint('timeout_pause', 72000, minval=120)
        self.default_idle_timeout = config.getint('default_idle_timeout', -1, minval=120)
        self.disable_heater = config.getint('disable_heater', 600, minval=60)
        self.default_extruder_temp = config.getfloat('default_extruder_temp', 200.)
        self.gcode_load_sequence = config.getint('gcode_load_sequence', 0)
        self.gcode_unload_sequence = config.getint('gcode_unload_sequence', 0)
        self.z_hop_height_error = config.getfloat('z_hop_height_error', 5., minval=0.)
        self.z_hop_height_toolchange = config.getfloat('z_hop_height_toolchange', 5., minval=0.)
        self.z_hop_speed = config.getfloat('z_hop_speed', 15., minval=1.)
        self.slicer_tip_park_pos = config.getfloat('slicer_tip_park_pos', 0., minval=0.)
        self.force_form_tip_standalone = config.getint('force_form_tip_standalone', 0, minval=0, maxval=1)
        self.persistence_level = config.getint('persistence_level', 0, minval=0, maxval=4)
        self.auto_calibrate_gates = config.getint('auto_calibrate_gates', 0, minval=0, maxval=1)
        self.strict_filament_recovery = config.getint('strict_filament_recovery', 0, minval=0, maxval=1)
        self.retry_tool_change_on_error = config.getint('retry_tool_change_on_error', 0, minval=0, maxval=1)
        self.print_start_detection = config.getint('print_start_detection', 1, minval=0, maxval=1)

        # Internal macro overrides
        self.pause_macro = config.get('pause_macro', 'PAUSE')
        self.form_tip_macro = config.get('form_tip_macro', '_MMU_FORM_TIP_STANDALONE')

        # User MMU setup
        self.mmu_num_gates = config.getint('mmu_num_gates')
        self.selector_offsets = list(config.getfloatlist('selector_offsets', []))
        self.bypass_offset = config.getfloat('selector_bypass', 0)
        self.default_tool_to_gate_map = list(config.getintlist('tool_to_gate_map', []))
        self.default_gate_status = list(config.getintlist('gate_status', []))
        self.default_gate_material = list(config.getlist('gate_material', []))
        self.default_gate_color = list(config.getlist('gate_color', []))
        self.default_gate_spool_id = list(config.getintlist('gate_spool_id', []))

        # Configuration for gate loading and unloading
        self.gate_homing_endstop = config.get('gate_homing_endstop', self.ENDSTOP_ENCODER) # "encoder" or "mmu_gate"
        if self.gate_homing_endstop not in self.GATE_ENDSTOPS:
            raise self.config.error("gate_homing_endstop is invalid. Options are: %s" % self.GATE_ENDSTOPS)
        self.gate_endstop_to_encoder = config.getfloat('gate_endstop_to_encoder', self.gate_endstop_to_encoder, minval=0.)
        self.gate_unload_buffer = config.getfloat('gate_unload_buffer', 30., minval=0.) # How far to short bowden move to avoid overshooting
        self.gate_homing_max = config.getfloat('gate_homing_max', 2 * self.gate_unload_buffer, minval=self.gate_unload_buffer)
        self.gate_parking_distance = config.getfloat('gate_parking_distance', self.gate_parking_distance) # Can be +ve or -ve
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
        self.extruder_homing_endstop = config.get('extruder_homing_endstop', self.ENDSTOP_EXTRUDER_COLLISION)
        if self.extruder_homing_endstop not in self.EXTRUDER_ENDSTOPS:
            raise self.config.error("extruder_homing_endstop is invalid. Options are: %s" % self.EXTRUDER_ENDSTOPS)
        self.extruder_homing_max = config.getfloat('extruder_homing_max', 50., above=10.)
        self.extruder_collision_homing_step = config.getint('extruder_collision_homing_step', 3,  minval=2, maxval=5)
        self.toolhead_homing_max = config.getfloat('toolhead_homing_max', 20., minval=0.)
        self.toolhead_extruder_to_nozzle = config.getfloat('toolhead_extruder_to_nozzle', 0., minval=5.) # For "sensorless"
        self.toolhead_sensor_to_nozzle = config.getfloat('toolhead_sensor_to_nozzle', 0., minval=5.) # For toolhead sensor
        self.toolhead_sync_unload = config.getint('toolhead_sync_unload', 0, minval=0, maxval=1)
        self.toolhead_unload_safety_margin = config.getfloat('toolhead_unload_safety_margin', 10., minval=0.) # Extra unload distance
        self.toolhead_move_error_tolerance = config.getfloat('toolhead_move_error_tolerance', 60, minval=0, maxval=100) # Allowable delta movement % before error

        # Extra Gear/Extruder synchronization controls
        self.sync_to_extruder = config.getint('sync_to_extruder', 0, minval=0, maxval=1)
        self.sync_form_tip = config.getint('sync_form_tip', 0, minval=0, maxval=1)

        # Servo control
        self.servo_down_angle = config.getfloat('servo_down_angle')
        self.servo_up_angle = config.getfloat('servo_up_angle')
        self.servo_move_angle = config.getfloat('servo_move_angle', self.servo_up_angle)
        self.servo_duration = config.getfloat('servo_duration', 0.2, minval=0.1)
        self.servo_active_down = config.getint('servo_active_down', 0, minval=0, maxval=1)
        self.servo_dwell = config.getfloat('servo_dwell', 0.5, minval=0.1)

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
        self.default_endless_spool_groups = list(config.getintlist('endless_spool_groups', []))
        self.tool_extrusion_multipliers = []
        self.tool_speed_multipliers = []

        # Logging
        self.log_level = config.getint('log_level', 1, minval=0, maxval=4)
        self.log_file_level = config.getint('log_file_level', 3, minval=-1, maxval=4)
        self.log_statistics = config.getint('log_statistics', 0, minval=0, maxval=1)
        self.log_visual = config.getint('log_visual', 1, minval=0, maxval=2)
        self.log_startup_status = config.getint('log_startup_status', 1, minval=0, maxval=2)

        # Currently hidden and testing options
        self.test_random_failures = config.getint('test_random_failures', 0, minval=0, maxval=1)

        # The following lists are the defaults (when reset) and will be overriden by values in mmu_vars.cfg...

        # Endless spool groups
        self.enable_endless_spool = self.default_enable_endless_spool
        if len(self.default_endless_spool_groups) > 0:
            if self.enable_endless_spool == 1 and len(self.default_endless_spool_groups) != self.mmu_num_gates:
                raise self.config.error("endless_spool_groups has a different number of values than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_endless_spool_groups.append(i)
        self.endless_spool_groups = list(self.default_endless_spool_groups)

        # Status (availability of filament) at each gate
        if len(self.default_gate_status) > 0:
            if not len(self.default_gate_status) == self.mmu_num_gates:
                raise self.config.error("gate_status has different number of values than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_gate_status.append(self.GATE_UNKNOWN)
        self.gate_status = list(self.default_gate_status)

        # Filmament material at each gate
        if len(self.default_gate_material) > 0:
            if not len(self.default_gate_material) == self.mmu_num_gates:
                raise self.config.error("gate_material has different number of entries than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_gate_material.append("")
        self.gate_material = list(self.default_gate_material)

        # Filmament color at each gate
        if len(self.default_gate_color) > 0:
            if not len(self.default_gate_color) == self.mmu_num_gates:
                raise self.config.error("gate_color has different number of entries than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_gate_color.append("")
        self._update_gate_color(list(self.default_gate_color))
       
        # SpoolID for each gate
        if len(self.default_gate_spool_id) > 0:
            if not len(self.default_gate_spool_id) == self.mmu_num_gates:
                raise self.config.error("gate_spool_id has different number of entries than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_gate_spool_id.append(-1)
        self.gate_spool_id = list(self.default_gate_spool_id)

        # Tool to gate mapping
        if len(self.default_tool_to_gate_map) > 0:
            if not len(self.default_tool_to_gate_map) == self.mmu_num_gates:
                raise self.config.error("tool_to_gate_map has different number of values than the number of gates")
        else:
            for i in range(self.mmu_num_gates):
                self.default_tool_to_gate_map.append(i)
        self.tool_to_gate_map = list(self.default_tool_to_gate_map)

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
        # TODO currently not registered directly as Tx commands because not visable by Mainsail/Fluuid
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
        self.gcode.register_command('MMU_TEST_ENCODER_RUNOUT', self.cmd_MMU_ENCODER_RUNOUT, desc = self.cmd_MMU_ENCODER_RUNOUT_help)
        self.gcode.register_command('MMU_FORM_TIP', self.cmd_MMU_FORM_TIP, desc = self.cmd_MMU_FORM_TIP_help)

        # Soak Testing
        self.gcode.register_command('MMU_SOAKTEST_SELECTOR', self.cmd_MMU_SOAKTEST_SELECTOR, desc = self.cmd_MMU_SOAKTEST_SELECTOR_help)
        self.gcode.register_command('MMU_SOAKTEST_LOAD_SEQUENCE', self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE, desc = self.cmd_MMU_SOAKTEST_LOAD_SEQUENCE_help)

        # TTG and Endless spool
        self.gcode.register_command('MMU_REMAP_TTG', self.cmd_MMU_REMAP_TTG, desc = self.cmd_MMU_REMAP_TTG_help)
        self.gcode.register_command('MMU_GATE_MAP', self.cmd_MMU_GATE_MAP, desc = self.cmd_MMU_GATE_MAP_help)
        self.gcode.register_command('MMU_ENDLESS_SPOOL', self.cmd_MMU_ENDLESS_SPOOL, desc = self.cmd_MMU_ENDLESS_SPOOL_help)
        self.gcode.register_command('MMU_CHECK_GATE', self.cmd_MMU_CHECK_GATE, desc = self.cmd_MMU_CHECK_GATE_help)
        self.gcode.register_command('MMU_TOOL_OVERRIDES', self.cmd_MMU_TOOL_OVERRIDES, desc = self.cmd_MMU_TOOL_OVERRIDES_help)

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

        # Internal handlers for Runout & Insertion for all sensor options
        self.gcode.register_command('__MMU_ENCODER_RUNOUT', self.cmd_MMU_ENCODER_RUNOUT, desc = self.cmd_MMU_ENCODER_RUNOUT_help)
        self.gcode.register_command('__MMU_ENCODER_INSERT', self.cmd_MMU_ENCODER_INSERT, desc = self.cmd_MMU_ENCODER_INSERT_help)
        self.gcode.register_command('__MMU_GATE_RUNOUT', self.cmd_MMU_GATE_RUNOUT, desc = self.cmd_MMU_GATE_RUNOUT_help)
        self.gcode.register_command('__MMU_GATE_INSERT', self.cmd_MMU_GATE_INSERT, desc = self.cmd_MMU_GATE_INSERT_help)
        self.gcode.register_command('__MMU_PRE_GATE_RUNOUT', self.cmd_MMU_PRE_GATE_RUNOUT, desc = self.cmd_MMU_PRE_GATE_RUNOUT_help)
        self.gcode.register_command('__MMU_PRE_GATE_INSERT', self.cmd_MMU_PRE_GATE_INSERT, desc = self.cmd_MMU_PRE_GATE_INSERT_help)
        self.gcode.register_command('__MMU_M400', self.cmd_MMU_M400, desc = self.cmd_MMU_M400_help) # Wait on both movequeues

        # Initializer tasks
        self.gcode.register_command('__MMU_BOOTUP_TASKS', self.cmd_MMU_BOOTUP_TASKS, desc = self.cmd_MMU_BOOTUP_TASKS_help) # Bootup tasks

        # We setup MMU hardware during configuration since some hardware like endstop requires
        # configuration during the MCU config phase, which happens before klipper connection
        # This assumes that the hardware configuartion appears before the `[mmu]` section
        # the installer by default already guarantees this order
        self._setup_mmu_hardware(config)

    def _setup_mmu_hardware(self, config):
        logging.info("MMU Hardware Initialization -------------------------------")

        # Selector and Gear h/w setup ------
        section = self.SELECTOR_STEPPER_CONFIG
        if config.has_section(section):
            # Inject options into selector stepper config regardless or what user sets
            config.fileconfig.set(section, 'position_min', -1.)
            config.fileconfig.set(section, 'position_max', self._get_max_selector_movement())
            config.fileconfig.set(section, 'homing_speed', self.selector_homing_speed)
        self.mmu_toolhead = MmuToolHead(config)
        self.mmu_kinematics = self.mmu_toolhead.get_kinematics()
        rails = self.mmu_toolhead.get_kinematics().rails
        self.selector_rail = rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]
        self.gear_rail = rails[1]
        self.gear_stepper = self.gear_rail.steppers[0]
        self.mmu_extruder_stepper = self.mmu_toolhead.mmu_extruder_stepper

        # Detect if selector touch is possible
        self.selector_touch = self.ENDSTOP_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enable

        # Setup filament homing sensors ------
        for name in [self.ENDSTOP_TOOLHEAD, self.ENDSTOP_GATE, self.ENDSTOP_EXTRUDER]:
            sensor = self.printer.lookup_object("filament_switch_sensor %s_sensor" % name, None)
            if sensor is not None:
                self.sensors[name] = sensor
                # With MMU this must not accidentally pause nor call user defined macros
                # (this is done in [mmu_sensors] but legacy setups may have discrete [filament_switch_sensors])
                self.sensors[name].runout_helper.runout_pause = False
                self.sensors[name].runout_helper.runout_gcode = None
                self.sensors[name].runout_helper.insert_gcode = None
                sensor_pin = self.config.getsection("filament_switch_sensor %s_sensor" % name).get("switch_pin")
    
                # Add sensor pin as an extra endstop for gear rail
                ppins = self.printer.lookup_object('pins')
                pin_params = ppins.parse_pin(sensor_pin, True, True)
                share_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
                ppins.allow_multi_use_pin(share_name)
                mcu_endstop = self.gear_rail.add_extra_endstop(sensor_pin, name)
 
                # This ensures rapid stopping of extruder stepper when endstop is hit on synced homing
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
                    self._log_debug("Found %s on extruder. Current control enabled. Stallguard 'touch' homing possible." % chip)

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

        if self._has_encoder() and not self._has_sensor(self.ENDSTOP_GATE) and self.enable_endless_spool == 1 and self.enable_clog_detection == 0:
            self._log_info("Warning: EndlessSpool mode requires clog detection to be enabled unless you have pre-gate sensors")

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
        if bowden_length:
            self.calibrated_bowden_length = bowden_length
            self._log_debug("Loaded saved reference bowden length: %.1f" % bowden_length)
            self.calibration_status |= self.CALIBRATED_BOWDEN
        else:
            self._log_always("Warning: Reference bowden length not found in mmu_vars.cfg. Probably not calibrated")

    def _initialize_state(self):
        self.is_enabled = True
        self.paused_extruder_temp = None
        self.is_homed = False
        self.last_print_stats = None
        self.tool_selected = self._next_tool = self._last_tool = self.TOOL_GATE_UNKNOWN
        self._last_toolchange = "Unknown"
        self.gate_selected = self.TOOL_GATE_UNKNOWN # We keep record of gate selected in case user messes with mapping in print
        self.servo_state = self.servo_angle = self.SERVO_UNKNOWN_STATE
        self.filament_pos = self.FILAMENT_POS_UNKNOWN
        self.filament_direction = self.DIRECTION_UNKNOWN
        self.filament_remaining = 0. # Tracker of filament left in extruder by cutter
        self.action = self.ACTION_IDLE
        self.calibrating = False
        self._clear_saved_toolhead_position()
        self._servo_reset_state()
        self._reset_job_statistics()
        self.print_state = self.resume_to_state = "ready"
        self.form_tip_vars = None # Current defaults of gcode variables for tip forming macro

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
            return tuple(round(float(int(hex_rgb[i:i + length // 3], 16)) / 255, 2) for i in range(0, length, length // 3))
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
            # Load tool to gate map
            tool_to_gate_map = self.variables.get(self.VARS_MMU_TOOL_TO_GATE_MAP, self.tool_to_gate_map)
            if len(tool_to_gate_map) == self.mmu_num_gates:
                self.tool_to_gate_map = tool_to_gate_map
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_TOOL_TO_GATE_MAP)

        if self.persistence_level >= 3:
            # Load gate status (filament present or not)
            gate_status = self.variables.get(self.VARS_MMU_GATE_STATUS, self.gate_status)
            if len(gate_status) == self.mmu_num_gates:
                self.gate_status = gate_status
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_GATE_STATUS)

            # Load filament material at each gate
            gate_material = self.variables.get(self.VARS_MMU_GATE_MATERIAL, self.gate_material)
            if len(gate_status) == self.mmu_num_gates:
                self.gate_material = gate_material
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_GATE_MATERIAL)

            # Load filament color at each gate
            gate_color = self.variables.get(self.VARS_MMU_GATE_COLOR, self.gate_color)
            if len(gate_status) == self.mmu_num_gates:
                self._update_gate_color(gate_color)
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_GATE_COLOR)

            # Load filament spool ID at each gate
            gate_spool_id = self.variables.get(self.VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id)
            if len(gate_status) == self.mmu_num_gates:
                self.gate_spool_id = gate_spool_id
            else:
                errors.append("Incorrect number of gates specified in %s" % self.VARS_MMU_GATE_SPOOL_ID)

        if self.persistence_level >= 4:
            # Load selected tool and gate
            tool_selected = self.variables.get(self.VARS_MMU_TOOL_SELECTED, self.tool_selected)
            gate_selected = self.variables.get(self.VARS_MMU_GATE_SELECTED, self.gate_selected)
            if gate_selected < self.mmu_num_gates and tool_selected < self.mmu_num_gates:
                self.tool_selected = tool_selected
                self.gate_selected = gate_selected

                if self.gate_selected >= 0:
                    self._set_gate_ratio(self._get_gate_ratio(self.gate_selected))
                    if self.tool_selected == self.TOOL_GATE_BYPASS: # Sanity check
                        self.tool_selected = self.TOOL_GATE_UNKNOWN
                    self._set_selector_pos(self.selector_offsets[self.gate_selected])
                    self.is_homed = True
                elif self.gate_selected == self.TOOL_GATE_BYPASS:
                    self.tool_selected = self.TOOL_GATE_BYPASS # Sanity check
                    self._set_selector_pos(self.bypass_offset)
                    self.is_homed = True
                else:
                    self.tool_selected = self.TOOL_GATE_UNKNOWN
                    self.gate_selected = self.TOOL_GATE_UNKNOWN
                    self.is_homed = False
            else:
                errors.append("Incorrect number of gates specified in %s or %s" % (self.VARS_MMU_TOOL_SELECTED, self.VARS_MMU_GATE_SELECTED))
            if gate_selected != self.TOOL_GATE_UNKNOWN and tool_selected != self.TOOL_GATE_UNKNOWN:
                self.filament_pos = self.variables.get(self.VARS_MMU_FILAMENT_POS, self.filament_pos)

        if len(errors) > 0:
            self._log_info("Warning: Some persisted state was ignored because it contained errors:\n%s" % ''.join(errors))

        swap_stats = self.variables.get(self.VARS_MMU_SWAP_STATISTICS, {})
        self.statistics.update(swap_stats)
        for gate in range(self.mmu_num_gates):
            self.gate_statistics[gate] = self.EMPTY_GATE_STATS_ENTRY.copy()
            gstats = self.variables.get("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, gate), None)
            if gstats:
                self.gate_statistics[gate].update(gstats)

    def handle_disconnect(self):
        self._log_debug('Klipper disconnected! MMU Shutdown')
        if self.queue_listener is not None:
            self.queue_listener.stop()

    def handle_ready(self):
        # Restore state if fully calibrated
        if not self._check_is_calibrated(silent=True):
            self._load_persisted_state()

        # Setup events for managing internal print state machine
        self.printer.register_event_handler("idle_timeout:printing", self._handle_idle_timeout_printing)
        self.printer.register_event_handler("idle_timeout:ready", self._handle_idle_timeout_ready)
        self.printer.register_event_handler("idle_timeout:idle", self._handle_idle_timeout_idle)
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

        # PAUL
        self.gcode.run_script_from_command("SET_GCODE_VARIABLE MACRO=_MMU_SET_LED VARIABLE=first_led_index VALUE=%d" % MmuLedEffect.first_led_index)

        self.estimated_print_time = self.printer.lookup_object('mcu').estimated_print_time
        self.last_selector_move_time = self.estimated_print_time(self.reactor.monotonic())
        self._schedule_mmu_bootup_tasks(self.BOOT_DELAY)

    def _schedule_mmu_bootup_tasks(self, delay=0.):
        waketime = self.reactor.monotonic() + delay
        self.reactor.register_callback(self._mmu_bootup_tasks, waketime)

    def _mmu_bootup_tasks(self, eventtime):
        self._log_trace("_bootup_tasks()")
        self._exec_gcode("__MMU_BOOTUP_TASKS")

    cmd_MMU_BOOTUP_TASKS_help = "Internal commands to complete bootup of MMU"
    def cmd_MMU_BOOTUP_TASKS(self, gcmd):
        try:
            self._log_always('(\_/)\n( *,*)\n(")_(") Happy Hare Ready')
            if self.log_startup_status > 0:
                self._log_always(self._tool_to_gate_map_to_human_string(self.log_startup_status == 1))
                self._display_visual_state(silent=self.persistence_level < 4)
            self._set_print_state("initialized")
            if self._has_encoder():
                self.encoder_sensor.set_clog_detection_length(self.variables.get(self.VARS_MMU_CALIB_CLOG_LENGTH, 15))
                self._disable_encoder_sensor() # Initially disable clog/runout detection
            self._servo_move()
            self.gate_status = self._validate_gate_status(self.gate_status) # Delay to allow for correct initial state
            self._update_filaments_from_spoolman()
        except Exception as e:
            self._log_always('Warning: Error booting up MMU: %s' % str(e))

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
                    self._log_debug("Error running %s: %s" % (macro, str(e)))

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

    def get_status(self, eventtime):
        return {
                'enabled': self.is_enabled,
                'is_locked': self._is_mmu_paused(), # TODO should deprecate now we have print_state
                'is_homed': self.is_homed,
                'tool': self.tool_selected,
                'gate': self.gate_selected,
                'material': self.gate_material[self.gate_selected] if self.gate_selected >= 0 else '',
                'next_tool': self._next_tool,
                'last_tool': self._last_tool,
                'last_toolchange': self._last_toolchange,
                'filament': "Loaded" if self.filament_pos == self.FILAMENT_POS_LOADED else
                            "Unloaded" if self.filament_pos == self.FILAMENT_POS_UNLOADED else
                            "Unknown",
                'filament_position': self.mmu_toolhead.get_position()[1],
                'filament_pos': self.filament_pos,
                'filament_direction': self.filament_direction,
                'servo': "Up" if self.servo_state == self.SERVO_UP_STATE else
                         "Down" if self.servo_state == self.SERVO_DOWN_STATE else
                         "Move" if self.servo_state == self.SERVO_MOVE_STATE else
                         "Unknown",
                'ttg_map': list(self.tool_to_gate_map),
                'gate_status': list(self.gate_status),
                'gate_material': list(self.gate_material),
                'gate_color': list(self.gate_color),
                'gate_color_rgb': self.gate_color_rgb,
                'gate_spool_id': list(self.gate_spool_id),
                'endless_spool_groups': list(self.endless_spool_groups),
                'tool_extrusion_multipliers': list(self.tool_extrusion_multipliers),
                'tool_speed_multipliers': list(self.tool_speed_multipliers),
                'action': self._get_action_string(),
                'has_bypass': self.bypass_offset > 0.,
                'sync_drive': self.mmu_toolhead.is_synced(),
                'print_state': self.print_state,
                'clog_detection': self.enable_clog_detection,
                'endless_spool': self.enable_endless_spool,
                'print_start_detection': self.print_start_detection,
        }

    def _reset_statistics(self):
        self.statistics = dict.fromkeys(['total_swaps', 'time_spent_loading', 'time_spent_unloading', 'total_pauses', 'time_spent_paused'], 0)
        self.gate_statistics = []
        for gate in range(self.mmu_num_gates):
            self.gate_statistics.append(self.EMPTY_GATE_STATS_ENTRY.copy())
        self._reset_job_statistics()

    def _reset_job_statistics(self):
        self.job_statistics = dict.fromkeys(['total_swaps', 'time_spent_loading', 'time_spent_unloading', 'total_pauses', 'time_spent_paused'], 0)
        self.tracked_start_time = 0
        self.pause_start_time = 0

    def _track_swap_completed(self):
        self.statistics['total_swaps'] += 1
        self.job_statistics['total_swaps'] += 1

    def _track_load_start(self):
        self.tracked_start_time = time.time()

    def _track_load_end(self):
        elapsed = time.time() - self.tracked_start_time
        self.statistics['time_spent_loading'] += elapsed
        self.job_statistics['time_spent_loading'] += elapsed

    def _track_unload_start(self):
        self.tracked_start_time = time.time()

    def _track_unload_end(self):
        elapsed = time.time() - self.tracked_start_time
        self.statistics['time_spent_unloading'] += elapsed
        self.job_statistics['time_spent_unloading'] += elapsed

    def _track_pause_start(self):
        self.statistics['total_pauses'] += 1
        self.job_statistics['total_pauses'] += 1
        self.pause_start_time = time.time()
        self._track_gate_statistics('pauses', self.gate_selected)

    def _track_pause_end(self):
        elapsed = time.time() - self.pause_start_time
        self.statistics['time_spent_paused'] += elapsed
        self.job_statistics['time_spent_paused'] += elapsed

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

    def _seconds_to_human_string(self, seconds):
        result = ""
        hours = int(math.floor(seconds / 3600.))
        if hours >= 1:
            result += "%d hours " % hours
        minutes = int(math.floor(seconds / 60.) % 60)
        if hours >= 1 or minutes >= 1:
            result += "%d minutes " % minutes
        result += "%d seconds" % int((math.floor(seconds) % 60))
        return result

    def _swap_statistics_to_human_string(self, total=True):
        (msg, stats) = ("MMU Total Statistics:", self.statistics) if total == True else ("MMU Last Print Statistics:", self.job_statistics)
        msg += "\n%d swaps completed" % stats['total_swaps']
        msg += "\n%s spent loading (average: %s)" % (self._seconds_to_human_string(stats['time_spent_loading']),
                                                     self._seconds_to_human_string(stats['time_spent_loading'] / stats['total_swaps']) if stats['total_swaps'] > 0 else "0")
        msg += "\n%s spent unloading (average: %s)" % (self._seconds_to_human_string(stats['time_spent_unloading']),
                                                       self._seconds_to_human_string(stats['time_spent_unloading'] / stats['total_swaps']) if stats['total_swaps'] > 0 else "0")
        msg += "\n%s spent paused (total pauses: %d)" % (self._seconds_to_human_string(stats['time_spent_paused']), stats['total_pauses'])
        return msg

    def _dump_statistics(self, force_log=False, total=False, job=False, gate=False, detail=False):
        if self.log_statistics or force_log:
            msg = ""
            if job:
                msg += self._swap_statistics_to_human_string(total=False)
            if total:
                msg += "\n\n" if msg != "" else ""
                msg += self._swap_statistics_to_human_string(total=True)
            if self._can_use_encoder() and gate:
                m,d = self._gate_statistics_to_human_string()
                msg += "\n\n" if msg != "" else ""
                msg += m
                if detail:
                    msg += "\n" if msg != "" else ""
                    msg += d
            self._log_always(msg)

        # This is good place to update the persisted stats...
        self._persist_swap_statistics()
        self._persist_gate_statistics()

    def _gate_statistics_to_human_string(self):
        msg = "Gate Statistics:\n"
        dbg = ""
        for gate in range(self.mmu_num_gates):
            #rounded = {k:round(v,1) if isinstance(v,float) else v for k,v in self.gate_statistics[gate].items()}
            rounded = self.gate_statistics[gate]
            load_slip_percent = (rounded['load_delta'] / rounded['load_distance']) * 100 if rounded['load_distance'] != 0. else 0.
            unload_slip_percent = (rounded['unload_delta'] / rounded['unload_distance']) * 100 if rounded['unload_distance'] != 0. else 0.
            quality = rounded['quality']
            # Give the gate a reliability grading based on "quality" which is based on slippage
            if quality < 0:
                status = "n/a"
            elif quality >= 0.985:
                status = "Perfect"
            elif quality >= 0.965:
                status = "Great"
            elif quality >= 0.95:
                status = "Good"
            elif quality >= 0.925:
                status = "Marginal"
            elif quality >= 0.90:
                status = "Degraded"
            elif quality >= 0.85:
                status = "Poor"
            else:
                status = "Terrible"
            msg += "#%d: %s" % (gate, status)
            msg += ", " if gate < (self.mmu_num_gates - 1) else ""
            dbg += "\nGate #%d: " % gate
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
        self.statistics['time_spent_loading'] = round(self.statistics['time_spent_loading'], 2)
        self.statistics['time_spent_unloading'] = round(self.statistics['time_spent_unloading'], 2)
        self.statistics['time_spent_paused'] = round(self.statistics['time_spent_paused'], 2)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % (self.VARS_MMU_SWAP_STATISTICS, self.statistics))

    def _persist_gate_map(self):
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_STATUS, self.gate_status))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_MATERIAL, list(map(lambda x: ("\'%s\'" %x), self.gate_material))))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_COLOR, list(map(lambda x: ("\'%s\'" %x), self.gate_color))))
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_SPOOL_ID, self.gate_spool_id))
        gcode = self.printer.lookup_object('gcode_macro _MMU_GATE_MAP_CHANGED', None)
        if gcode is not None:
            self._wrap_gcode_command("_MMU_GATE_MAP_CHANGED GATE=-1")

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
        message = "- DEBUG: %s" % message
        if self.mmu_logger and self.log_file_level > 1:
            self.mmu_logger.info(message)
        if self.log_level > 1:
            self.gcode.respond_info(message)

    def _log_trace(self, message):
        message = "- - TRACE: %s" % message
        if self.mmu_logger and self.log_file_level > 2:
            self.mmu_logger.info(message)
        if self.log_level > 2:
            self.gcode.respond_info(message)

    def _log_stepper(self, message):
        message = "- - - STEPPER: %s" % message
        if self.mmu_logger and self.log_file_level > 3:
            self.mmu_logger.info(message)
        if self.log_level > 3:
            self.gcode.respond_info(message)

    # Fun visual display of MMU state
    def _display_visual_state(self, direction=None, silent=False):
        if direction is not None:
            self.filament_direction = direction
        if not silent and self.log_visual > 0 and not self.calibrating:
            visual_str = self._state_to_human_string()
            self._log_always(visual_str)

    def _state_to_human_string(self, direction=None):
        tool_str = str(self.tool_selected) if self.tool_selected >=0 else "?"
        sensor_str = " [sensor] " if self._has_sensor(self.ENDSTOP_TOOLHEAD) else ""
        counter_str = " %.1fmm%s" % (self.mmu_toolhead.get_position()[1], " (e:%.1fmm)" % self._get_encoder_distance(dwell=None) if self._has_encoder() and self.encoder_move_validation else "")
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.filament_pos == self.FILAMENT_POS_LOADED:
            visual = "MMU BYPASS ----- [encoder] ----------->> [nozzle] LOADED"
        elif self.tool_selected == self.TOOL_GATE_BYPASS and self.filament_pos == self.FILAMENT_POS_UNLOADED:
            visual = "MMU BYPASS >.... [encoder] ............. [nozzle] UNLOADED"
        elif self.tool_selected == self.TOOL_GATE_BYPASS:
            visual = "MMU BYPASS >.... [encoder] ............. [nozzle] UNKNOWN"
        elif self.filament_pos == self.FILAMENT_POS_UNKNOWN:
            visual = "MMU [T%s] ..... [encoder] ............. [extruder] ...%s... [nozzle] UNKNOWN" % (tool_str, sensor_str)
        elif self.filament_pos == self.FILAMENT_POS_UNLOADED:
            visual = "MMU [T%s] >.... [encoder] ............. [extruder] ...%s... [nozzle] UNLOADED" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_START_BOWDEN:
            visual = "MMU [T%s] >>>>> [encoder] >>........... [extruder] ...%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_IN_BOWDEN:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>...... [extruder] ...%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_END_BOWDEN:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>> [extruder] ...%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_HOMED_EXTRUDER:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>| [extruder] ...%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_EXTRUDER_ENTRY:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>> [extruder] >>.%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_HOMED_TS:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>> [extruder] >>|%s... [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_IN_EXTRUDER:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>> [extruder] >>>%s>.. [nozzle]" % (tool_str, sensor_str)
            visual += counter_str
        elif self.filament_pos == self.FILAMENT_POS_LOADED:
            visual = "MMU [T%s] >>>>> [encoder] >>>>>>>>>>>>> [extruder] >>>%s>>> [nozzle] LOADED" % (tool_str, sensor_str)
            visual += counter_str

        visual2 = visual.replace("encoder", "En").replace("extruder", "Ex").replace("sensor", "Ts").replace("nozzle", "Nz").replace(">>", ">").replace("..", ".").replace("--", "-")
        if self.filament_direction == self.DIRECTION_UNLOAD:
            visual = visual.replace(">", "<")
            visual2 = visual2.replace(">", "<")
        return visual2 if self.log_visual == 2 else visual

    def _log_level_to_human_string(self, level):
        log = "OFF"
        if level > 3: log = "STEPPER"
        elif level > 2: log = "TRACE"
        elif level > 1: log = "DEBUG"
        elif level > 0: log = "INFO"
        elif level > -1: log = "ESSENTIAL MESSAGES"
        return log

    def _visual_log_level_to_human_string(self, level):
        log = "OFF"
        if level > 1: log = "SHORT"
        elif level > 0: log = "LONG"
        return log

### LOGGING AND STATISTICS FUNCTIONS GCODE FUNCTIONS

    cmd_MMU_STATS_help = "Dump and optionally reset the MMU statistics"
    def cmd_MMU_STATS(self, gcmd):
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
        config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)

        msg = "Happy Hare v%.1f running %s v%s" % (self.config_version, self.mmu_vendor, self.mmu_version_string)
        msg += " with %d gates" % (self.mmu_num_gates)
        msg += " (%s)" % ("DISABLED" if not self.is_enabled else "PAUSED" if self._is_mmu_paused() else "OPERATIONAL")
        msg += "\nServo in %s position" % ("UP" if self.servo_state == self.SERVO_UP_STATE else \
                "DOWN" if self.servo_state == self.SERVO_DOWN_STATE else "MOVE" if self.servo_state == self.SERVO_MOVE_STATE else "unknown")
        if self._has_encoder():
            msg += ", Encoder reads %.1fmm" % self._get_encoder_distance()
        msg += "\nPrint state is %s" % self.print_state.upper()
        msg += ". Selector is %s" % ("HOMED" if self.is_homed else "NOT HOMED")
        msg += ". Tool %s selected " % self._selected_tool_string()
        msg += " on gate %s" % self._selected_gate_string()
        msg += ". Toolhead position saved" if self.saved_toolhead_position else ""
        msg += "\nGear stepper is at %d%% and is %s to extruder" % (self.gear_percentage_run_current, "SYNCED" if self.mmu_toolhead.is_gear_synced_to_extruder() else "not synced")

        if config:
            msg += "\n\nConfiguration:"

            msg += "\nLoad Sequence"
            msg += "\n- Filament loads into gate by homing a maximum of %.1fmm to %s" % (self.gate_homing_max, "ENCODER" if self.gate_homing_endstop == self.ENDSTOP_ENCODER else "GATE SENSOR")
            msg += "\n- Bowden is loaded with a fast %.1fmm move" % self.calibrated_bowden_length
            if self._must_home_to_extruder():
                if self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION:
                    msg += " and then homes to extruder using COLLISION detection (at %d%% current)" % self.extruder_homing_current
                else:
                    msg += " and then homes to extruder using ENDSTOP '%s'" % self.extruder_homing_endstop
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder loads by homing a maximum of %.1fmm to TOOLHEAD SENSOR before moving the last %.1fmm the nozzle" % (self.toolhead_homing_max, self._get_home_position_to_nozzle())
            else:
                msg += "\n- Loads extruder by moving %.1fmm to the nozzle" % self._get_home_position_to_nozzle()

            msg += "\nUnload Sequence"
            msg += "\n- Tip is %s formed by %s" % (("sometimes", "SLICER") if not self.force_form_tip_standalone else ("always", ("'%s' macro" % self.form_tip_macro)))
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                msg += "\n- Extruder unloads by homing a maximum %.1fmm (%.1f home_to_nozzle + %.1f safety) less reported park position to TOOLHEAD SENSOR, then the remainder to exist extruder" % (self._get_home_position_to_nozzle() + self.toolhead_unload_safety_margin, self._get_home_position_to_nozzle(), self.toolhead_unload_safety_margin)
            else:
                msg += "\n- Extruder unloads by moving %.1fmm (%1f home_to_nozzle + %.1f saftey) less reported park position to exit extruder" % (self._get_home_position_to_nozzle() + self.toolhead_unload_safety_margin, self._get_home_position_to_nozzle(), self.toolhead_unload_safety_margin)
            if self._has_encoder() and self.bowden_pre_unload_test:
                msg += "\n- Bowden is unloaded with a short %.1fmm validation move before %.1fmm (%.1f calibration - %.1f buffer - %.1f validation) fast move" % (self.encoder_move_step_size, self.calibrated_bowden_length - self.gate_unload_buffer - self.encoder_move_step_size, self.calibrated_bowden_length, self.gate_unload_buffer, self.encoder_move_step_size)
            else:
                msg += "\n- Bowden is unloaded with a fast %.1fmm (%.1f calibration - %.1f buffer) move" % (self.calibrated_bowden_length - self.gate_unload_buffer, self.calibrated_bowden_length, self.gate_unload_buffer)
            msg += "\n- Filament is stored by homing a maximum of %.1fmm to %s and parking %.1fmm in the gate" % (self.gate_homing_max, "ENCODER" if self.gate_homing_endstop == self.ENDSTOP_ENCODER else "GATE SENSOR", self.gate_parking_distance)

            if self.toolhead_sync_unload or self.sync_form_tip or self.sync_to_extruder:
                msg += "\nGear and Extruder steppers are synchronized during: "
                msg += ("Print (at %d%% current), " % self.sync_gear_current) if self.sync_to_extruder else ""
                msg += "Tip forming, " if self.sync_form_tip else ""
                msg += "Extruder Unload " if self.toolhead_sync_unload else ""
            msg += "\nTip forming extruder current is %d%%" % self.extruder_form_tip_current

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
            msg += "\nLogging: Console %d(%s)" % (self.log_level, self._log_level_to_human_string(self.log_level))

            msg += ", Logfile %d(%s)" % (self.log_file_level, self._log_level_to_human_string(self.log_file_level))
            msg += ", Visual %d(%s)" % (self.log_visual, self._visual_log_level_to_human_string(self.log_visual))
            msg += ", Statistics %d(%s)" % (self.log_statistics, "ON" if self.log_statistics else "OFF")

        if not detail:
            msg += "\nFor details on TTG and endless spool groups use 'MMU_STATUS DETAIL=1'"
            if not config:
                msg += "\nFor configuration summary use 'MMU_STATUS SHOWCONFIG=1'"

        msg += "\n\n%s" % self._tool_to_gate_map_to_human_string(summary=True)
        msg += "\n\n%s" % self._state_to_human_string()

        if detail:
            msg += "\n\nTool/gate mapping%s" % (" and EndlessSpool groups:" if self.enable_endless_spool else ":")
            msg += "\n%s" % self._tool_to_gate_map_to_human_string()

        self._log_always(msg)


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

    def _servo_down(self, buzz_gear=True):
        if self.gate_selected == self.TOOL_GATE_BYPASS: return
        if self.servo_state == self.SERVO_DOWN_STATE: return
        self._log_debug("Setting servo to down (filament drive) position at angle: %d" % self.servo_down_angle)
        self._movequeues_wait_moves()
        self.servo.set_value(angle=self.servo_down_angle, duration=None if self.servo_active_down else self.servo_duration)
        if self.servo_angle != self.servo_down_angle and buzz_gear:
            oscillations = 3
            self.gear_buzz_accel = 1000
            for i in range(oscillations):
                self._trace_filament_move(None, 0.8, speed=25, accel=self.gear_buzz_accel, encoder_dwell=None)
                self._trace_filament_move(None, -0.8, speed=25, accel=self.gear_buzz_accel, encoder_dwell=None)
            self._movequeues_dwell(max(self.servo_dwell - self.servo_duration, 0))
        self.servo_angle = self.servo_down_angle
        self.servo_state = self.SERVO_DOWN_STATE

    def _servo_move(self): # Position servo for selector movement
        if self.servo_state == self.SERVO_MOVE_STATE: return
        self._log_debug("Setting servo to move (filament hold) position at angle: %d" % self.servo_move_angle)
        if self.servo_angle != self.servo_move_angle:
            self._movequeues_wait_moves()
            self.servo.set_value(angle=self.servo_move_angle, duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_dwell - self.servo_duration, 0))
            self.servo_angle = self.servo_move_angle
            self.servo_state = self.SERVO_MOVE_STATE

    def _servo_up(self, measure=False):
        if self.servo_state == self.SERVO_UP_STATE: return 0.
        self._log_debug("Setting servo to up (filament released) position at angle: %d" % self.servo_up_angle)
        delta = 0.
        if self.servo_angle != self.servo_up_angle:
            self._movequeues_wait_moves()
            if measure:
                initial_encoder_position = self._get_encoder_distance(dwell=None)
            self.servo.set_value(angle=self.servo_up_angle, duration=self.servo_duration)
            self._movequeues_dwell(max(self.servo_dwell - self.servo_duration, 0))
            if measure:
                # Report on spring back in filament then revert counter
                delta = self._get_encoder_distance() - initial_encoder_position
                if delta > 0.:
                    self._log_debug("Spring in filament measured  %.1fmm - adjusting encoder" % delta)
                    self._set_encoder_distance(initial_encoder_position, dwell=None)
        self.servo_angle = self.servo_up_angle
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
        if self._check_is_disabled(): return
        pos = gcmd.get('POS', "").lower()
        if pos == "up":
            self._servo_up()
        elif pos == "move":
            self._servo_move()
        elif pos == "down":
            if self._check_in_bypass(): return
            self._servo_down()
        elif pos == "":
            if self._check_in_bypass(): return
            angle = gcmd.get_float('ANGLE', None)
            if angle is not None:
                self._log_debug("Setting servo to angle: %d" % angle)
                self._servo_set_angle(angle)
            else:
                self._log_error("No position or angle specified. Try POS=... or ANGLE=...")
        else:
            self._log_error("Unknown servo position `%s`" % pos)

    cmd_MMU_MOTORS_OFF_help = "Turn off both MMU motors"
    def cmd_MMU_MOTORS_OFF(self, gcmd):
        if self._check_is_disabled(): return
        self._motors_off()
        self._servo_move()
        self._servo_reset_state()

    cmd_MMU_TEST_BUZZ_MOTOR_help = "Simple buzz the selected motor (default gear) for setup testing"
    def cmd_MMU_TEST_BUZZ_MOTOR(self, gcmd):
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
            small=min(self.servo_down_angle, self.servo_up_angle)
            large=max(self.servo_down_angle, self.servo_up_angle)
            mid=(self.servo_down_angle + self.servo_up_angle)/2
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
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        servo = gcmd.get_int('SERVO', 1, minval=0, maxval=1)
        sync = gcmd.get_int('SYNC', 1, minval=0, maxval=1)
        force_in_print = gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1)
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
                self._log_always("No counts measured. Ensure a tool was selected with servo down " +
                                  "before running calibration and that your encoder " +
                                  "is working properly")
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

    def _calibrate_bowden_length(self, start_pos, extruder_homing_max, repeats, save=True):
        try:
            self._log_always("Calibrating bowden length from reference Gate #0")
            self._select_tool(0)
            self._set_gate_ratio(1.)
            reference_sum = spring_max = 0.
            successes = 0
            for i in range(repeats):
                self._initialize_filament_position(dwell=True)    # Encoder 0000
                self._load_gate(allow_retry=False)
                self._load_bowden(start_pos)
                self._log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                self._home_to_extruder(extruder_homing_max)
                measured_movement = self._get_encoder_distance(dwell=True) + self._get_encoder_dead_space()
                spring = self._servo_up(measure=True)
                reference = measured_movement - spring

                # When homing using collision, we expect the filament to spring back.
                if not (self.extruder_homing_endstop == self.ENDSTOP_EXTRUDER_COLLISION and spring == 0.):
                    msg = "Pass #%d: Filament homed to extruder, encoder measured %.1fmm, " % (i+1, measured_movement)
                    msg += "filament sprung back %.1fmm" % spring
                    msg += "\n- Bowden calibration based on this pass is %.1f" % reference
                    self._log_always(msg)
                    reference_sum += reference
                    spring_max = max(spring, spring_max)
                    successes += 1
                else:
                    # No spring means we haven't reliably homed
                    self._log_always("Failed to detect a reliable home position on this attempt")

                self._initialize_filament_position(True)    # Encoder 0000
                self._unload_bowden(reference)
                self._unload_gate()
                self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)

            if successes > 0:
                average_reference = reference_sum / successes
                detection_length = (average_reference * 2.) / 100. + spring_max # 2% of bowden length plus spring seems to be good starting point
                msg = "Recommended calibration reference is %.1fmm" % average_reference
                if self.enable_clog_detection:
                    msg += ". Clog detection length: %.1fmm" % detection_length
                self._log_always(msg)

                if save:
                    self._set_calibrated_bowden_length(average_reference)
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_BOWDEN_LENGTH, average_reference))
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=1.0" % (self.VARS_MMU_CALIB_PREFIX, 0))
                    self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%.1f" % (self.VARS_MMU_CALIB_CLOG_LENGTH, detection_length))
                    self.encoder_sensor.set_clog_detection_length(detection_length)
                    self._log_always("Bowden calibration and clog detection length have been saved")
            else:
                self._log_always("All %d attempts at homing failed. MMU needs some adjustments!" % repeats)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of bowden length (on Gate #0) failed. Aborting, because:\n%s" % str(ee))
        finally:
            self._servo_auto()

    def _calibrate_gate(self, gate, length, repeats, save=True):
        try:
            pos_values, neg_values = [], []
            self._select_tool(gate)
            self._set_gate_ratio(1.)
            self._load_gate(allow_retry=False)
            self._log_always("%s gate %d over %.1fmm..." % ("Calibrating" if (gate > 0 and save) else "Validating calibration of", gate, length))

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
            self._log_always("(Gate #%d rotation_distance: %.6f vs Gate #0: %.6f)" % (gate, ratio * self.ref_gear_rotation_distance, self.ref_gear_rotation_distance))
            if not gate == 0: # Gate #0 is not calibrated, it is the reference
                if ratio > 0.8 and ratio < 1.2:
                    if save:
                        self.variables["%s%d" % (self.VARS_MMU_CALIB_PREFIX, gate)] = ratio
                        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%d VALUE=%.6f" % (self.VARS_MMU_CALIB_PREFIX, gate, ratio))
                        self._log_always("Calibration for gate #%d has been saved" % gate)
                        self.calibration_status |= self.CALIBRATED_GATES
                else:
                    self._log_always("Calibration ratio ignored because it is not considered valid (0.8 < ratio < 1.2)")
            self._unload_gate()
            self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for gate #%d failed. Aborting, because: %s" % (gate, str(ee)))
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
        gate_str = lambda gate : ("gate #%d" % gate) if gate >= 0 else "bypass"
        try:
            self._initialize_state()
            self.calibrating = True
            self._servo_move()
            max_movement = self._get_max_selector_movement(gate)
            self._log_always("Measuring the selector position for %s" % gate_str(gate))
            traveled, found_home = self._measure_to_home()

            # Test we actually homed
            if not found_home:
                self._log_always("Selector didn't find home position")
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

    def _calibrate_selector_auto(self, v1_bypass_block=-1, save=True):
        # Strategy is to find the two end gates, infer and set number of gates and distribute selector positions
        # Assumption: the user has manually positioned the selector aligned with gate #0 before calling
        try:
            self._log_always("Auto calibrating the selector. Excuse the whizz, bang, buzz, clicks...")
            self._initialize_state()
            self.calibrating = True
            self._servo_move()

            # Step 1 - position of gate #0
            self._log_always("Measuring the selector position for gate #0...")
            traveled, found_home = self._measure_to_home()
            if not found_home or traveled > self.cad_gate0_pos + self.cad_selector_tolerance:
                self._log_always("Selector didn't find home position or distance moved (%.1fmm) was larger than expected.\nAre you sure you aligned selector with gate #0 and removed filament?" % traveled)
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
                self._log_always("Didn't detect the end of the selector")
                return

            # Step 3 - bypass (v2) and last gate position
            self._log_always("Measuring the full selector length...")
            traveled, found_home = self._measure_to_home()
            if not found_home:
                self._log_always("Selector didn't find home position after full length move")
                return
            self._log_always("Maximum selector movement is %.1fmm" % traveled)
            bypass_pos = traveled - self.cad_bypass_offset
            last_gate_pos = traveled - self.cad_last_gate_offset

            # Step 4 - the calcs
            length = last_gate_pos - gate0_pos
            self._log_debug("Results: gate0_pos=%.1f, last_gate_pos=%.1f, length=%.1f" % (gate0_pos, last_gate_pos, length))
            selector_offsets = []
            if self.mmu_version >= 2.0:
                num_gates = int(round(length / self.cad_gate_width)) + 1
                adj_gate_width = length / (num_gates - 1)
                self._log_debug("Adjusted gate width: %.1f" % adj_gate_width)
                self.selector_offsets = []
                for i in range(num_gates):
                    selector_offsets.append(round(gate0_pos + (i * adj_gate_width), 1))
                bypass_offset = bypass_pos

            else:
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

            if num_gates != self.mmu_num_gates:
                self._log_error("You configued your MMU for %d gates but I counted %d! Please update `mmu_num_gates`" % (self.mmu_num_gates, num_gates))
                return

            self._log_always("Offsets %s and bypass %.1f" % (selector_offsets, bypass_offset))
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
        if self._check_is_disabled(): return
        if self._check_has_encoder(): return
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
        if self._check_is_disabled(): return

        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        if gate == -1:
            if gcmd.get_int('BYPASS', -1, minval=0, maxval=1) == 1:
                gate = self.TOOL_GATE_BYPASS

        if gate != -1:
            self._calibrate_selector(gate, save=save)
        else:
            v1_bypass_block = gcmd.get_int('BYPASS_BLOCK', -1, minval=1, maxval=3)
            self._calibrate_selector_auto(v1_bypass_block, save=save)

    # Start: Will home selector, select gate #0
    # End: Filament will unload
    cmd_MMU_CALIBRATE_BOWDEN_help = "Calibration of reference bowden length for gate #0"
    def cmd_MMU_CALIBRATE_BOWDEN(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_has_encoder(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(self.CALIBRATED_GEAR|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR): return

        approx_bowden_length = gcmd.get_float('BOWDEN_LENGTH', above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        extruder_homing_max = gcmd.get_float('HOMING_MAX', 100, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        try:
            self._reset_ttg_mapping() # To force tool = gate
            self._unload_tool()
            self.calibrating = True
            with self._require_encoder():
                self._calibrate_bowden_length(approx_bowden_length, extruder_homing_max, repeats, save)
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self.calibrating = False

    # Start: Will home selector, select gate #0 or required gate
    # End: Filament will unload
    cmd_MMU_CALIBRATE_GATES_help = "Optional calibration of individual MMU gate"
    def cmd_MMU_CALIBRATE_GATES(self, gcmd):
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
        self._log_info("Disable extruder heater")
        self.gcode.run_script_from_command("M104 S0")
        return self.reactor.NEVER

    def _handle_idle_timeout_printing(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "printing")

    def _handle_idle_timeout_ready(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "ready")

    def _handle_idle_timeout_idle(self, eventtime):
        self._handle_idle_timeout_event(eventtime, "idle")

    def _is_printer_printing(self):
        eventtime = self.reactor.monotonic()
        idle_timeout = self.printer.lookup_object("idle_timeout")
        return idle_timeout.get_status(eventtime)["state"] == "Printing"

    def _is_printing(self, force_in_print=False): # Actively printing and not paused
        return self.print_state in ["started", "printing"] or force_in_print

    def _is_in_print(self, force_in_print=False): # Printing or paused
        return self.print_state in ["started", "printing", "pause_locked", "paused"] or force_in_print

    def _is_paused(self): # Printer is paused according to pause_resume
        return self.pause_resume.is_paused

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
            self._exec_gcode("_MMU_PRINT_END STATE=standby")

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

    # MMU job state machine: initialized|ready|started|printing|complete|cancelled|error|pause_locked|paused|standby
    def _set_print_state(self, print_state, call_macro=True):
        if print_state != self.print_state:
            idle_timeout = self.printer.lookup_object("idle_timeout").idle_timeout
            self._log_debug("Job State: %s -> %s (MMU State: Encoder: %s, Synced: %s, Paused temp: %s, Resume to state: %s, Position saved: %s, z_hop @%.1fmm, pause_resume: %s, Idle timeout: %.2fs)"
                    % (self.print_state.upper(), print_state.upper(), self._get_encoder_state(), self.mmu_toolhead.is_gear_synced_to_extruder(), self.paused_extruder_temp,
                        self.resume_to_state, self.saved_toolhead_position, self.saved_toolhead_height, self._is_paused(), idle_timeout))
            if call_macro:
                gcode = self.printer.lookup_object('gcode_macro _MMU_PRINT_STATE_CHANGED', None)
                if gcode is not None:
                    self._wrap_gcode_command("_MMU_PRINT_STATE_CHANGED STATE='%s' OLD_STATE='%s'" % (print_state, self.print_state))
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
            self._enable_encoder_sensor(True) # Enable runout/clog detection
            self._initialize_filament_position(dwell=None) # Encoder 0000
            self._set_print_state("started", call_macro=False)

        if not pre_start_only and self.print_state not in ["printing"]:
            self._log_trace("_on_print_start(->printing)")
            self._sync_gear_to_extruder(self.sync_to_extruder, servo=True, current=True)
            msg = "MMU initialized ready for print"
            if self.filament_pos == self.FILAMENT_POS_LOADED:
                msg += " (initial tool T%s loaded)" % self.tool_selected
            else:
                msg += " (no filament loaded)"
            self._log_info(msg)
            self._set_print_state("printing")

    def _mmu_pause(self, reason, force_in_print=False):
        run_pause_macro = False
        if not self.paused_extruder_temp: # Only save the initial pause temp
            self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp
        self.resume_to_state = "printing" if self._is_in_print() else "ready"

        if self._is_printing(force_in_print) and not self._is_mmu_paused():
            self._log_error("An issue with the MMU has been detected. Print paused\nReason: %s" % reason)
            self._log_always("After fixing the issue, call \'RESUME\' to continue printing (MMU_UNLOCK can restore temperature)")

            self._track_pause_start()
            self._log_trace("Extruder heater will be disabled in %s" % self._seconds_to_human_string(self.disable_heater))
            self.reactor.update_timer(self.heater_off_handler, self.reactor.monotonic() + self.disable_heater) # Set extruder off timer
            self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.timeout_pause) # Set alternative pause idle_timeout
            self._disable_encoder_sensor() # Disable runout/clog detection in pause
            self._save_toolhead_position_and_lift("pause", z_hop_height=self.z_hop_height_error)
            run_pause_macro = True
            self._set_print_state("pause_locked")
            self.printer.send_event("mmu:mmu_paused", self) # Notify MMU paused event

        elif self._is_mmu_paused():
            self._log_error("An issue with the MMU has been detected whilst printer is paused\nReason: %s" % reason)

        else:
            self._log_error("An issue with the MMU has been detected whilst out of a print\nReason: %s" % reason)

        if self._is_printing(force_in_print):
            self._recover_filament_pos(strict=False, message=True)

        self._sync_gear_to_extruder(False, servo=True) # Should we just leave state where it ends up?

        if run_pause_macro:
            self._wrap_gcode_command(self.pause_macro)

    def _mmu_unlock(self):
        if self._is_mmu_paused():
            self._set_print_state("paused")
            self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout) # Restore original idle_timeout
            self._ensure_safe_extruder_temperature("pause", wait=True) # Important to wait for stable temperature to resume exactly how we paused

    def _mmu_resume(self):
        if self._is_mmu_paused():
            self._ensure_safe_extruder_temperature("pause", wait=True)
            self.paused_extruder_temp = None
            self._restore_toolhead_position("resume")
            self._sync_gear_to_extruder(self.sync_to_extruder and self.resume_to_state == "printing", servo=True, current=self.resume_to_state == "printing")
            self._initialize_filament_position() # Encoder 0000
            self._track_pause_end()
            self._enable_encoder_sensor(True) # Enable runout/clog detection if printing
            self._set_print_state(self.resume_to_state)
            self.resume_to_state = "ready"
            self.printer.send_event("mmu:mmu_resumed", self) # Notify MMU resumed event

    # If this is called automatically it will occur after the user's print ends.
    # Therefore don't do anything that requires operating kinematics
    def _on_print_end(self, state="complete"):
        if not self._is_in_endstate():
            self._log_trace("_on_print_end(%s)" % state)
            self._movequeues_wait_moves()
            self._clear_saved_toolhead_position()
            self.resume_to_state = "ready"
            self.paused_extruder_temp = None
            self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER) # Don't automatically turn off extruder heaters
            self._disable_encoder_sensor() # Disable runout/clog detection after print

            if self.printer.lookup_object("idle_timeout").idle_timeout != self.default_idle_timeout:
                self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout) # Restore original idle_timeout
            self._sync_gear_to_extruder(False, servo=True)
            self._set_print_state(state)
        if state == "standby" and not self._is_in_standby():
            self._set_print_state(state)

    def _save_toolhead_position_and_lift(self, operation=None, z_hop_height=None):
        homed = self.toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes']
        if operation and not self.saved_toolhead_position:
            if 'xyz' in homed:
                self._movequeues_wait_moves()
                self._log_debug("Saving toolhead position for %s" % operation)
                self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=MMU_state")
                self.saved_toolhead_position = operation
            else:
                self._log_info("Warning: MMU cannot save toolhead position because toolhead not homed!")
        elif operation:
            self._log_debug("Asked to save toolhead position for %s but it is already saved for %s. Ignored" % (operation, self.saved_toolhead_position))

        # Immediately lift toolhead off print (potentially going higher but not multiple times)
        if z_hop_height is not None and z_hop_height > 0 and z_hop_height > self.saved_toolhead_height:
            z_hop_height -= self.saved_toolhead_height
            self.saved_toolhead_height += z_hop_height
            if 'z' not in self.toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes']:
                self._log_info("Warning: MMU cannot lift toolhead because toolhead not homed!")
            else:
                self._log_debug("Lifting toolhead %.1fmm" % z_hop_height)
                act_z = self.toolhead.get_position()[2]
                max_z = self.toolhead.get_status(self.printer.get_reactor().monotonic())['axis_maximum'].z
                safe_z = z_hop_height if (act_z < (max_z - z_hop_height)) else (max_z - act_z)
                self.toolhead.manual_move([None, None, act_z + safe_z], self.z_hop_speed)

    def _restore_toolhead_position(self, operation):
        homed = self.toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes']
        if self.saved_toolhead_position:
            if 'xyz' in homed:
                self._log_debug("Restoring toolhead position for %s" % operation)
                self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=MMU_state MOVE=1 MOVE_SPEED=%.1f" % self.z_hop_speed)
                self._clear_saved_toolhead_position()
            else:
                self._log_info("Warning: MMU cannot restore toolhead position because toolhead not homed!")

    def _clear_saved_toolhead_position(self):
        self.saved_toolhead_position = None
        self.saved_toolhead_height = 0.

    def _disable_encoder_sensor(self):
        if self._has_encoder() and self.encoder_sensor.is_enabled():
            self._log_debug("Disabled encoder sensor. Status: %s" % self.encoder_sensor.get_status(0))
            self.encoder_sensor.disable()
            return True
        return False

    def _enable_encoder_sensor(self, force_in_print=False):
        if self._has_encoder() and self._is_in_print(force_in_print):
            if not self.encoder_sensor.is_enabled():
                self._log_debug("Enabled encoder sensor, force_in_print=%s. Status: %s" % (force_in_print, self.encoder_sensor.get_status(0)))
                self.encoder_sensor.enable()

    @contextlib.contextmanager
    def _wrap_disable_encoder(self):
        old_enable = self._disable_encoder_sensor()
        try:
            yield self
        finally:
            self._enable_encoder_sensor(old_enable)

    def _has_encoder(self):
        return self.encoder_sensor is not None

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
        if self._has_sensor('gate') and self.gate_homing_endstop == self.ENDSTOP_GATE:
            return self.gate_endstop_to_encoder
        else:
            return 0.

    def _initialize_filament_position(self, dwell=False):
        if self._encoder_dwell(dwell):
            self.encoder_sensor.reset_counts()
        self._set_filament_position()

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
        if self.tool_selected == self.TOOL_GATE_BYPASS and self.filament_pos not in [self.FILAMENT_POS_UNLOADED, self.FILAMENT_POS_UNKNOWN]:
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

    def _ensure_safe_extruder_temperature(self, source="auto", wait=False):
        extruder = self.printer.lookup_object(self.extruder_name)
        current_temp = extruder.get_status(0)['temperature']
        current_target_temp = extruder.heater.target_temp
        klipper_minimum_temp = extruder.get_heater().min_extrude_temp
        self._log_trace("_ensure_safe_extruder_temperature: current_temp=%s, paused_extruder_temp=%s, current_target_temp=%s, klipper_minimum_temp=%s, default_extruder_temp=%s, source=%s" % (current_temp, self.paused_extruder_temp, current_target_temp, klipper_minimum_temp, self.default_extruder_temp, source))

        if source == "pause":
            new_target_temp = self.paused_extruder_temp
            if self.paused_extruder_temp < klipper_minimum_temp:
                # Don't wait if just messing with cold printer
                wait = False
        elif source == "auto":
            if self._is_printing():
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
                self._log_error("Warning: Automatically heating extruder to %s temp (%.1f)" % (source, new_target_temp))
            else:
                self._log_info("Heating extruder to %s temp (%.1f)" % (source, new_target_temp))
            wait = True # Always wait to warm up

        self.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=%.1f" % new_target_temp)

        # Optionally wait until temperature is stable or at minimum saftey temp so extruder can move
        if wait:
            if abs(new_target_temp - current_temp) > 1:
                with self._wrap_action(self.ACTION_HEATING):
                    self._log_info("Waiting for extruder to reach target (%s) temperature (%.1f)" % (source, new_target_temp))
                    self.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.1f MAXIMUM=%.1f" % (new_target_temp - 1, new_target_temp + 1))

    def _selected_tool_string(self):
        if self.tool_selected == self.TOOL_GATE_BYPASS:
            return "bypass"
        elif self.tool_selected == self.TOOL_GATE_UNKNOWN:
            return "unknown"
        else:
            return "T%d" % self.tool_selected

    def _selected_gate_string(self):
        if self.gate_selected == self.TOOL_GATE_BYPASS:
            return "bypass"
        elif self.gate_selected == self.TOOL_GATE_UNKNOWN:
            return "unknown"
        else:
            return "#%d" % self.gate_selected

    def _is_filament_in_bowden(self):
        if self.filament_pos in [self.FILAMENT_POS_START_BOWDEN, self.FILAMENT_POS_IN_BOWDEN]:
            return True
        return False

    def _get_home_position_to_nozzle(self):
        if self._has_sensor(self.ENDSTOP_TOOLHEAD):
            return self.toolhead_sensor_to_nozzle
        else:
            return self.toolhead_extruder_to_nozzle

    def _set_action(self, action):
        if action == self.action: return
        old_action = self.action
        self.action = action
        gcode = self.printer.lookup_object('gcode_macro _MMU_ACTION_CHANGED', None)
        if gcode is not None:
            self._wrap_gcode_command("_MMU_ACTION_CHANGED ACTION='%s' OLD_ACTION='%s'" % (self._get_action_string(), self._get_action_string(old_action)))
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
        self._log_always("MMU enabled and reset")
        self._schedule_mmu_bootup_tasks()

    def _disable_mmu(self):
        if not self.is_enabled: return
        self._initialize_state()
        self.reactor.update_timer(self.heater_off_handler, self.reactor.NEVER)
        self.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%d" % self.default_idle_timeout)
        self.is_enabled = False
        self._set_print_state("standby")
        self._log_always("MMU disabled")

    def _random_failure(self):
        if self.test_random_failures and randint(0, 10) == 0:
            raise MmuError("Randomized testing failure")


### STATE GCODE COMMANDS

    cmd_MMU_help = "Enable/Disable functionality and reset state"
    def cmd_MMU(self, gcmd):
        enable = gcmd.get_int('ENABLE', minval=0, maxval=1)
        if enable == 1:
            self._enable_mmu()
        else:
            self._disable_mmu()

    cmd_MMU_HELP_help = "Display the complete set of MMU commands and function"
    def cmd_MMU_HELP(self, gcmd):
        macros = gcmd.get_int('MACROS', 0, minval=0, maxval=1)
        testing = gcmd.get_int('TESTING', 0, minval=0, maxval=1)
        steps = gcmd.get_int('STEPS', 0, minval=0, maxval=1)
        msg = "Happy Hare MMU commands: (use MMU_HELP MACROS=1 TESTING=1 STEPS=1 for full command set)\n"
        tmsg = "\nCalibration and testing commands:\n"
        mmsg = "\nMacros and callbacks (defined in mmu_software.cfg, mmu_filametrix.cfg, mmu_sequence.cfg):\n"
        smsg = "\nIndividual load/unload sequence steps:\n"
        cmds = list(self.gcode.ready_gcode_handlers.keys())
        cmds.sort()
        for c in cmds:
            d = self.gcode.gcode_help.get(c, "n/a")
            if c.startswith("MMU") and not c.startswith("MMU__"):
                if not "_CALIBRATE" in c and not "_TEST" in c and not "_SOAKTEST" in c:
                    if c not in ["MMU_UNLOAD", "MMU_CHANGE_TOOL_STANDALONE", "MMU_CHECK_GATES"]: # Remove aliases
                        msg += "%s : %s\n" % (c.upper(), d)
                else:
                    tmsg += "%s : %s\n" % (c.upper(), d)
            elif c.startswith("_MMU"):
                if not c.startswith("_MMU_STEP"):
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

    cmd_MMU_ENCODER_help = "Display encoder position and stats or temporarily enable/disable detection logic in encoder"
    def cmd_MMU_ENCODER(self, gcmd):
        if self._check_has_encoder(): return
        if self._check_is_disabled(): return
        value = gcmd.get_float('VALUE', -1, minval=0.)
        enable = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        if enable == 1:
            self._enable_encoder_sensor(True)
        elif enable == 0:
            self._disable_encoder_sensor()
            return
        elif value >= 0.:
            self._set_encoder_distance(value)
            return
        status = self.encoder_sensor.get_status(0)
        msg = "Encoder position: %.1f" % status['encoder_pos']
        if status['enabled']:
            clog = "Automatic" if status['detection_mode'] == 2 else "On" if status['detection_mode'] == 1 else "Off"
            msg += "\nClog/Runout detection: %s (Detection length: %.1f)" % (clog, status['detection_length'])
            msg += "\nTrigger headroom: %.1f (Minimum observed: %.1f)" % (status['headroom'], status['min_headroom'])
            msg += "\nFlowrate: %d %%" % status['flow_rate']
        self._log_info(msg)

    cmd_MMU_LED_help = "Manage mode of operation of optional MMU LED's"
    def cmd_MMU_LED(self, gcmd):
        if self._check_is_disabled(): return
        gcode_macro = self.printer.lookup_object("gcode_macro _MMU_SET_LED", None)
        if gcode_macro is not None:
            variables = gcode_macro.variables
            led_enable = gcmd.get_int('ENABLE', int(variables['led_enable']), minval=0, maxval=1)
            default_gate_effect = gcmd.get('EFFECT', variables['default_gate_effect'])
            default_exit_effect = gcmd.get('EXIT_EFFECT', variables['default_exit_effect'])

            if variables['led_enable'] and not led_enable:
                # Enabled to disabled
                self._wrap_gcode_command("_MMU_SET_LED EFFECT=off EXIT_EFFECT=off")
                gcode_macro.variables.update({'led_enable':led_enable, 'default_gate_effect':default_gate_effect, 'default_exit_effect':default_exit_effect})
            else:
                gcode_macro.variables.update({'led_enable':led_enable, 'default_gate_effect':default_gate_effect, 'default_exit_effect':default_exit_effect})
                self._wrap_gcode_command("_MMU_SET_LED EFFECT=default EXIT_EFFECT=default")

            self._log_always("LEDs are %s\nDefault gate effect: '%s'\nDefault exit effect: `%s`" % ("enabled" if led_enable else "disabled", default_gate_effect, default_exit_effect))
            self._log_always("ENABLE=[0|1] EFFECT=[off|gate_status|filament_color] EXIT_EFFECT=[off|on|filament_color]")
        else:
            self._log_error("LEDs not available")

    cmd_MMU_RESET_help = "Forget persisted state and re-initialize defaults"
    def cmd_MMU_RESET(self, gcmd):
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
        self.tool_to_gate_map = list(self.default_tool_to_gate_map)
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.tool_to_gate_map))
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

    cmd_MMU_FORM_TIP_help = "Convenience macro for calling the standalone tip forming functionality (or cutter logic)"
    def cmd_MMU_FORM_TIP(self, gcmd):
        if self._check_is_disabled(): return
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        show = bool(gcmd.get_int('SHOW', 0, minval=0, maxval=1))
        run = bool(gcmd.get_int('RUN', 1, minval=0, maxval=1))
        eject = bool(gcmd.get_int('EJECT', 0, minval=0, maxval=1))
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1))

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)

        if reset:
            if self.form_tip_vars is not None:
                gcode_macro.variables = dict(self.form_tip_vars)
                self.form_tip_vars = None
                self._log_always("Reset '%s' macro variables to defaults" % self.form_tip_macro)
            show = True

        if show:
            msg = "Variable settings for macro '%s':" % self.form_tip_macro
            for k, v in gcode_macro.variables.items():
                msg += "\nvariable_%s: %s" % (k, v)
            self._log_always(msg)
            return

        # Save restore point on first call
        if self.form_tip_vars is None:
            self.form_tip_vars = dict(gcode_macro.variables)

        for param in gcmd.get_command_parameters():
            value = gcmd.get(param)
            param = param.lower()
            if param.startswith("variable_"):
                self._log_always("Removing 'variable_' prefix from '%s' - not necessary" % param)
                param = param[9:]
            if param in gcode_macro.variables:
                gcode_macro.variables[param] = self._fix_type(value)
            else:
                self._log_always("Variable '%s' is not defined for '%s' macro" % (param, self.form_tip_macro))

        # Run the macro ensuring final_eject is set
        self._ensure_safe_extruder_temperature(wait=False)
        if run:
            self._sync_gear_to_extruder(self.sync_form_tip and self._is_in_print(force_in_print), servo=True, current=self._is_in_print(force_in_print)) # current mimick in print
            with self._wrap_extruder_current(self.extruder_form_tip_current, "for tip forming move"):
                gcode_macro.variables['final_eject'] = 1 if eject else 0
                msg = "Running '%s' with the following variable settings:" % self.form_tip_macro
                for k, v in gcode_macro.variables.items():
                    msg += "\nvariable_%s: %s" % (k, v)
                self._log_always(msg)

                # Perform the tip forming move and establish park_pos
                initial_extruder_position = self.mmu_extruder_stepper.stepper.get_commanded_position()
                initial_encoder_position = self._get_encoder_distance()
                initial_pa = self.printer.lookup_object(self.extruder_name).get_status(0)['pressure_advance'] # Capture PA in case user's tip forming resets it
                self._wrap_gcode_command(self.form_tip_macro)
                self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f" % initial_pa) # Restore PA
                measured = self._get_encoder_distance() - initial_encoder_position
                park_pos = gcode_macro.variables.get("output_park_pos", -1)
                try:
                    park_pos = float(park_pos)
                except ValueError:
                    park_pos = -1
                measured_park_pos = initial_extruder_position - self.mmu_extruder_stepper.stepper.get_commanded_position()
                if park_pos < 0:
                    self._log_always("After tip formation, extruder moved (park_pos): %.1f, encoder measured %.1f" % (measured_park_pos, measured))
                else:
                    # Means the macro reported it (usually for filament cutting)
                    limit = self._get_home_position_to_nozzle()
                    if park_pos > limit:
                        self._log_always("Warning: After tip formation, park_pos reported as: %.1f, which is larger than your '**_to_nozzle' distance of %.1f!" % (park_pos, limit))
                    else:
                        filament_remaining = park_pos - measured_park_pos
                        self._log_always("After tip formation, park_pos reported as: %.1f with %.1f filament remaining in extruder (extruder moved: %.1f, encoder measured %.1f)" % (park_pos, filament_remaining, measured_park_pos, measured))

                gcode_macro.variables['final_eject'] = 0
            self._sync_gear_to_extruder(False, servo=True)

    cmd_MMU_STEP_LOAD_GATE_help = "User composable loading step: Move filament from gate to start of bowden"
    def cmd_MMU_STEP_LOAD_GATE(self, gcmd):
        try:
            self._load_gate()
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_GATE_help = "User composable unloading step: Move filament from start of bowden and park in the gate"
    def cmd_MMU_STEP_UNLOAD_GATE(self, gcmd):
        full = gcmd.get_int('FULL', 0)
        try:
            self._unload_gate(homing_max=self.calibrated_bowden_length if full else None)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_GATE: %s" % str(ee))

    cmd_MMU_STEP_LOAD_BOWDEN_help = "User composable loading step: Smart loading of bowden"
    def cmd_MMU_STEP_LOAD_BOWDEN(self, gcmd):
        length = gcmd.get_float('LENGTH', self.calibrated_bowden_length)
        try:
            self._load_bowden(length)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_BOWDEN_help = "User composable unloading step: Smart unloading of bowden"
    def cmd_MMU_STEP_UNLOAD_BOWDEN(self, gcmd):
        length = gcmd.get_float('LENGTH', self.calibrated_bowden_length)
        try:
            self._unload_bowden(length)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_BOWDEN: %s" % str(ee))

    cmd_MMU_STEP_HOME_EXTRUDER_help = "User composable loading step: Home to extruder sensor or entrance through collision detection"
    def cmd_MMU_STEP_HOME_EXTRUDER(self, gcmd):
        try:
            self._home_to_extruder(self.extruder_homing_max)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_HOME_EXTRUDER: %s" % str(ee))

    cmd_MMU_STEP_LOAD_TOOLHEAD_help = "User composable loading step: Toolhead loading"
    def cmd_MMU_STEP_LOAD_TOOLHEAD(self, gcmd):
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        try:
            self._load_extruder(extruder_only)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_LOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_UNLOAD_TOOLHEAD_help = "User composable unloading step: Toolhead unloading"
    def cmd_MMU_STEP_UNLOAD_TOOLHEAD(self, gcmd):
        extruder_only = gcmd.get_int('EXTRUDER_ONLY', 0)
        park_pos = gcmd.get_float('PARK_POS', 0)
        try:
            self._unload_extruder(extruder_only, park_pos)
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_UNLOAD_TOOLHEAD: %s" % str(ee))

    cmd_MMU_STEP_HOMING_MOVE_help = "User composable loading step: Generic homing move"
    def cmd_MMU_STEP_HOMING_MOVE(self, gcmd):
        try:
            self._homing_move_cmd(gcmd, "User defined step homing move")
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_HOMING_MOVE: %s" % str(ee))

    cmd_MMU_STEP_MOVE_help = "User composable loading step: Generic move"
    def cmd_MMU_STEP_MOVE(self, gcmd):
        try:
            self._move_cmd(gcmd, "User defined step move")
        except MmuError as ee:
            raise gcmd.error("_MMU_STEP_MOVE: %s" % str(ee))

    cmd_MMU_STEP_SET_FILAMENT_help = "User composable loading step: Set filament position state"
    def cmd_MMU_STEP_SET_FILAMENT(self, gcmd):
        state = gcmd.get_int('STATE', )
        silent = gcmd.get_int('SILENT', 0)
        if state >= self.FILAMENT_POS_UNLOADED and state <= self.FILAMENT_POS_LOADED:
            self._set_filament_pos_state(state, silent)
        else:
            raise gcmd.error("_MMU_STEP_SET_FILAMENT: Invalid state '%d'" % state)


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
                    self._set_filament_pos_state(self.FILAMENT_POS_START_BOWDEN)
                    return
                else:
                    self._log_debug("Error loading filament - did not find home. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self._track_gate_statistics('servo_retries', self.gate_selected)
                        self._servo_up()
                        self._servo_down()

        self._set_gate_status(self.gate_selected, self.GATE_UNKNOWN)
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
        if homing_max is None:
            homing_max = self.gate_homing_max

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
                        self._set_filament_position(self.mmu_toolhead.get_position()[1] + delta)
                        _,_,measured,_ = self._trace_filament_move("Final parking", -park)
                        self._set_filament_position(self.mmu_toolhead.get_position()[1] + park)
                        # We don't expect any movement of the encoder unless it is free-spinning
                        if measured > self.encoder_min: # We expect 0, but relax the test a little (allow one pulse)
                            self._log_info("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED)
                        return
                self._log_debug("Filament did not clear encoder even after moving %.1fmm" % (self.encoder_move_step_size * max_steps))
        else:
            _,homed,_,_ = self._trace_filament_move("Reverse homing to gate sensor", -homing_max, motor="gear", homing_move=-1, endstop_name=self.ENDSTOP_GATE)
            if homed:
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
        length -= self.mmu_toolhead.get_position()[1]

        self._log_debug("Loading bowden tube")
        self._set_filament_direction(self.DIRECTION_LOAD)
        self._servo_down()
        tolerance = self.bowden_allowable_load_delta

        # See if we need to automatically set calibration ratio for this gate
        current_ratio = self.variables.get("%s%d" % (self.VARS_MMU_CALIB_PREFIX, self.gate_selected), None)
        if self._can_use_encoder() and self.auto_calibrate_gates and self.gate_selected > 0 and not current_ratio and not self.calibrating:
            reference_load = True
        else:
            reference_load = False
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
                self._log_always("Calibration ratio for gate #%d was missing. Value of %.6f has been automatically saved" % (self.gate_selected, ratio))
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
        if full:
            length -= self.toolhead_unload_safety_margin # Extra precaution against sync unload and small unload buffer
        length -= self.gate_unload_buffer

        self._log_debug("Unloading bowden tube")
        self._set_filament_direction(self.DIRECTION_UNLOAD)
        self._servo_down()
        tolerance = self.bowden_allowable_unload_delta

        # Optional safety step
        if full and self._has_encoder() and self.bowden_pre_unload_test:
            with self._require_encoder():
                self._log_debug("Performing bowden pre-unload test")
                _,_,_,delta = self._trace_filament_move("Bowden pre-unload test", -self.encoder_move_step_size)
                if delta > self.encoder_move_step_size * (self.bowden_pre_unload_error_tolerance/100.):
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
                    raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder")
                length -= self.encoder_move_step_size
                self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

        # "Fast" unload
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

        if self.extruder_homing_endstop != self.ENDSTOP_EXTRUDER_COLLISION:
            self._log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (self.extruder_homing_endstop, max_length))
            actual,homed,measured,_ = self._trace_filament_move("Homing filament to extruder", max_length, motor="gear", homing_move=1, endstop_name=self.extruder_homing_endstop)
            if homed:
                self._log_debug("Extruder entrance reached after %.1fmm (measured %.1fmm)" % (actual, measured))
        else:
            actual,homed,measured,_ = self._home_to_extruder_collision_detection(max_length)

        if not homed:
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder gear after moving %.1fmm" % max_length)

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
                    break
            self._log_debug("Extruder entrance%s found after %.1fmm move (%d steps), encoder measured %.1fmm (delta %.1fmm)"
                    % (" not" if not homed else "", step*(i+1), i+1, measured, delta))

        if delta > 5.0:
            self._log_info("Warning: A lot of slippage was detected whilst homing to extruder, you may want to reduce 'extruder_homing_current' and/or ensure a good grip on filament by gear drive")

        self._set_filament_position(self.mmu_toolhead.get_position()[1] - step) # Ignore last step movement
        return step*i, homed, measured, delta

    # Move filament from the home position (extruder or sensor) to the nozzle
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

            homed = False
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                # With toolhead sensor we first home to toolhead sensor past the extruder entrance
                if self.sensors[self.ENDSTOP_TOOLHEAD].runout_helper.filament_present:
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")
                self._log_debug("Homing up to %.1fmm to toolhead sensor%s" % (self.toolhead_homing_max, (" (synced)" if synced else "")))
                _,homed,_,_ = self._trace_filament_move("Homing to toolhead sensor", self.toolhead_homing_max, motor=motor, homing_move=1, endstop_name=self.ENDSTOP_TOOLHEAD)
                if homed:
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                else:
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure it is used only one time
            length = max(self._get_home_position_to_nozzle() - self.filament_remaining, 0)
            self.filament_remaining = 0.
            self._log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self._trace_filament_move("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)

            # Final sanity check
            self._log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))

            # Encoder based validation test
            if self._can_use_encoder() and not homed:
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
    def _unload_extruder(self, extruder_only=False, park_pos=0.):
        with self._wrap_action(self.ACTION_UNLOADING_EXTRUDER):
            self._log_debug("Extracting filament from extruder")
            self._set_filament_direction(self.DIRECTION_UNLOAD)
            self._ensure_safe_extruder_temperature(wait=False)

            # Don't allow sync without toolhead sensor because of risk of over unloading
            synced = self.toolhead_sync_unload and self._has_sensor(self.ENDSTOP_TOOLHEAD) and not extruder_only
            if synced:
                self._servo_down()
                speed = self.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                self._servo_up()
                speed = self.extruder_unload_speed
                motor = "extruder"

            length = self._get_home_position_to_nozzle() - park_pos + self.toolhead_unload_safety_margin
            homed = False
            ts_actual = 0.
            if self._has_sensor(self.ENDSTOP_TOOLHEAD):
                # With toolhead sensor we first home to toolhead sensor past the extruder entrance
                if not self.sensors[self.ENDSTOP_TOOLHEAD].runout_helper.filament_present:
                    self._log_info("Warning: Filament was not detected in extruder by toolhead sensor at start of extruder unload")
                    homed = True
                else:
                    self._log_debug("Reverse homing up to %.1fmm to toolhead sensor%s" % (length, (" (synced)" if synced else "")))
                    ts_actual,homed,_,_ = self._trace_filament_move("Reverse homing to toolhead sensor", -length, motor=motor, homing_move=-1, endstop_name=self.ENDSTOP_TOOLHEAD)
                if homed:
                    self._set_filament_pos_state(self.FILAMENT_POS_HOMED_TS)
                else:
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % length)

                if self.toolhead_sensor_to_nozzle > 0. and self.toolhead_extruder_to_nozzle > 0.:
                    length = self.toolhead_extruder_to_nozzle - self.toolhead_sensor_to_nozzle + self.toolhead_unload_safety_margin
                else:
                    length += ts_actual # Actual will be -ve value

            self._log_debug("Unloading last %.1fmm to exist the extruder..." % length)
            _,_,measured,delta = self._trace_filament_move("Unloading extruder", -length, speed=speed, motor=motor, wait=True)
            if motor == "extruder":
                # Best guess is that filament didn't move past extruder entrance
                self._set_filament_position(self.mmu_toolhead.get_position()[1] + self.toolhead_unload_safety_margin)

            # Final sanity check
            self._log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))

            # Encoder based validation test
            if self._can_use_encoder() and self._is_in_print() and not homed:
                if measured < self.encoder_min:
                    raise MmuError("Encoder not sensing any movement: Concluding filament either stuck in the extruder or tip forming erroneously ejected filament")
                elif synced and delta > length * (self.toolhead_move_error_tolerance/100.):
                    self._set_filament_pos_state(self.FILAMENT_POS_EXTRUDER_ENTRY)
                    raise MmuError("Encoder not sensing sufficent movement: Concluding filament either stuck in the extruder or tip forming erroneously ejected filament")

            self._random_failure()
            self._movequeues_wait_moves()
            self._set_filament_pos_state(self.FILAMENT_POS_END_BOWDEN)
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
                self._track_load_start()
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
            msg = "Load of %.1fmm filament successful" % (self.mmu_toolhead.get_position()[1])
            if self._can_use_encoder():
                msg += " (encoder measured %.1fmm)" % self._get_encoder_distance(dwell=None)
            self._log_info(msg)
        except MmuError as ee:
            if full:
                self._track_gate_statistics('load_failures', self.gate_selected)
            raise MmuError("Load sequence failed: %s" % (str(ee)))
        finally:
            if full:
                self._track_load_end()
            if not extruder_only:
                self._set_action(current_action)

    def _unload_sequence(self, length=None, check_state=False, skip_tip=False, extruder_only=False):
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
                self._track_unload_start()
                self._display_visual_state()

            # Check for cases where we must form tip
            if skip_tip:
                park_pos = self.slicer_tip_park_pos
                self._set_filament_position(-park_pos)
            elif self.filament_pos >= self.FILAMENT_POS_IN_EXTRUDER:
                detected, park_pos = self._form_tip_standalone(extruder_only=extruder_only)
                if detected:
                    # Definitely in extruder
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_EXTRUDER)
                else:
                    # No detection means we can assume we are somewhere in the bowden
                    self._set_filament_pos_state(self.FILAMENT_POS_IN_BOWDEN)

                gcode = self.printer.lookup_object('gcode_macro _MMU_POST_FORM_TIP', None)
                if gcode is not None:
                    self._wrap_gcode_command("_MMU_POST_FORM_TIP", exception=True)
            else:
                park_pos = 0.

            # Note: Conditionals deliberately coded this way to match macro alternative
            start_filament_pos = self.filament_pos
            unload_to_buffer = (start_filament_pos >= self.FILAMENT_POS_END_BOWDEN and not extruder_only)
            if self.gcode_unload_sequence:
                self._log_debug("Calling external user defined unloading sequence macro")
                self._wrap_gcode_command("_MMU_UNLOAD_SEQUENCE FILAMENT_POS=%d LENGTH=%.1f EXTRUDER_ONLY=%d PARK_POS=%.1f" % (start_filament_pos, length, extruder_only, park_pos), exception=True)

            elif extruder_only:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    self._unload_extruder(extruder_only=True, park_pos=park_pos)
                else:
                    self._log_debug("Assertion failure: Unexpected state %d in _unload_sequence(extruder_only=True)" % start_filament_pos)
                    raise MmuError("Cannot unload extruder because filament not in extruder!")

            elif start_filament_pos == self.FILAMENT_POS_UNLOADED:
                self._log_debug("Assertion failure: Unexpected state %d in _unload_sequence()" % start_filament_pos)
                raise MmuError("Cannot unload because already unloaded!")

            else:
                if start_filament_pos >= self.FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload encoder
                    self._unload_extruder(park_pos=park_pos)

                if start_filament_pos >= self.FILAMENT_POS_END_BOWDEN:
                    # Fast unload of bowden, then unload encoder
                    self._unload_bowden(length)
                    self._unload_gate()

                elif start_filament_pos >= self.FILAMENT_POS_START_BOWDEN:
                    # Have to do slow unload because we don't know exactly where we are
                    self._unload_gate(homing_max=length) # Full slow unload

            if unload_to_buffer and self.gate_status[self.gate_selected] != self.GATE_EMPTY:
                self._set_gate_status(self.gate_selected, self.GATE_AVAILABLE_FROM_BUFFER)

            # Encoder based validation test
            if self._can_use_encoder():
                movement = self._servo_up(measure=True)
                if movement > self.encoder_min:
                    self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
                    raise MmuError("It may be time to get the pliers out! Filament appears to stuck somewhere")
            else:
                self._servo_up()

            msg = "Unload of %.1fmm filament successful" % (self.mmu_toolhead.get_position()[1])
            if self._can_use_encoder():
                msg += " (encoder measured %.1fmm)" % self._get_encoder_distance(dwell=False)
            self._log_info(msg)

        except MmuError as ee:
            if not extruder_only:
                self._track_gate_statistics('unload_failures', self.gate_selected)
            raise MmuError("Unload sequence failed: %s" % (str(ee)))

        finally:
            if not extruder_only:
                self._track_unload_end()
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

    # Form tip and return True if filament was detected and the assumed filament (park) position
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
            return False, 0.

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.form_tip_macro)

        self._log_debug("Preparing to form tip...")
        with self._wrap_action(self.ACTION_FORMING_TIP):
            synced = self.sync_form_tip and not extruder_only
            self._sync_gear_to_extruder(synced, servo=True, current=False)
            self._ensure_safe_extruder_temperature(wait=False)

            # Perform the tip forming move and establish park_pos
            with self._wrap_extruder_current(self.extruder_form_tip_current, "for tip forming move"):
                initial_extruder_position = self.mmu_extruder_stepper.stepper.get_commanded_position()
                initial_encoder_position = self._get_encoder_distance()
                try:
                    initial_pa = self.printer.lookup_object(self.extruder_name).get_status(0)['pressure_advance'] # Capture PA in case user's tip forming resets it
                    self._log_info("Forming tip...")
                    self._wrap_gcode_command(self.form_tip_macro, exception=True)
                finally:
                    self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f" % initial_pa) # Restore PA
                self._movequeues_wait_moves()
                measured = self._get_encoder_distance(dwell=None) - initial_encoder_position
                park_pos = gcode_macro.variables.get("output_park_pos", -1)
                try:
                    park_pos = float(park_pos)
                except ValueError:
                    park_pos = -1
                filament_check = True
                measured_park_pos = initial_extruder_position - self.mmu_extruder_stepper.stepper.get_commanded_position()
                if park_pos < 0:
                    park_pos = measured_park_pos
                    self._log_trace("After tip formation, extruder moved (park_pos): %.1f, encoder measured %.1f" % (park_pos, measured))
                    self.filament_remaining = 0.
                else:
                    # Means the macro reported it (usually for filament cutting)
                    limit = self._get_home_position_to_nozzle()
                    if park_pos > limit:
                        self._log_always("Warning: After tip formation, park_pos reported as: %.1f, which is larger than your '**_to_nozzle' distance of %.1f! Ignored" % (park_pos, limit))
                        park_pos = measured_park_pos
                        self.filament_remaining = 0.
                    else:
                        self.filament_remaining = park_pos - measured_park_pos
                        self._log_trace("After tip formation, park_pos reported as: %.1f with %.1f filament remaining in extruder (extruder moved: %.1f, encoder measured %.1f)" % (park_pos, self.filament_remaining, measured_park_pos, measured))
                    filament_check = False
                self._set_filament_position(-park_pos)
                self._set_encoder_distance(initial_encoder_position + park_pos)

            # Encoder based validation test
            if self._can_use_encoder() and filament_check:
                # Logic to try to validate success and update presence of filament based on movement
                if filament_initially_present is True:
                    # With encoder we might be able to check for clog now
                    if not measured > self.encoder_min:
                        raise MmuError("No encoder movement: Concluding filament is stuck in extruder")
                else:
                    # Couldn't determine if we initially had filament at start (lack of sensors)
                    if not measured > self.encoder_min:
                        # No movement. We can be confident we are/were empty
                        return False, park_pos
                    if synced:
                        # A further test is needed to see if the filament is actually in the extruder
                        detected, moved = self._test_filament_in_extruder_by_retracting()
                        park_pos += moved
                        return detected, park_pos

            # We have to assume filament was present because no way to be sure
            return True, park_pos


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

        if sync:
            self._movequeues_sync()

        # Gear rail is driving the filament
        if motor in ["gear", "gear+extruder", "extruder"]:
            with self._wrap_sync_extruder_to_gear(motor in ["gear+extruder", "extruder"], extruder_only=(motor == "extruder")):
                if homing_move != 0:
                    self._log_stepper("%s HOME: dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, sync=%s, wait=%s"% (motor.upper(), dist, speed, accel, endstop_name, sync, wait))
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstop, self.mmu_toolhead)
                    try:
                        init_pos = pos[1]
                        pos[1] += dist
                        with self._wrap_accel(accel):
                            trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                        homed = True
                        if self.gear_rail.is_endstop_virtual(endstop_name):
                            # Stallguard doesn't do well at slow speed. Try to infer move completion
                            if abs(trig_pos[1] - dist) < 1.0:
                                    homed = False
                    except self.printer.command_error as e:
                        # CANbus mcu's often seen to exhibit "Communication timeout" errors so surface errors to user
                        if abs(trig_pos[1] - dist) > 0. and not "after full movement" in str(e):
                            self._log_error("Did not complete homing move: %s" % str(e))
                        else:
                            self._log_stepper("Did not home: %s" % str(e))
                        homed = False
                    finally:
                        halt_pos = self.mmu_toolhead.get_position()
                        actual = halt_pos[1] - init_pos
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
            self.printer.send_event("mmu:extruder_synced" if sync else "mmu:extruder_unsynced")

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
        self._select_tool(tool, move_servo=False)
        gate = self.tool_to_gate_map[tool]

        if self.gate_status[gate] == self.GATE_EMPTY:
            raise MmuError("Gate %d is empty!" % gate)

        self._load_sequence()

        # Activate the spool in SpoolMan, if enabled
        self._spoolman_activate_spool(self.gate_spool_id[gate])

        # Restore M220 and M221 overrides
        self._restore_tool_override(self.tool_selected)

    # Primary method to unload current tool but retains selection
    def _unload_tool(self, skip_tip=False):
        if self.filament_pos == self.FILAMENT_POS_UNLOADED:
            self._log_debug("Tool already unloaded")
            return

        self._log_debug("Unloading tool %s" % self._selected_tool_string())
        # Remember M220 and M221 overrides, potentially deactivate in SpoolMan
        self._record_tool_override()
        self._unload_sequence(skip_tip=skip_tip)
        self._spoolman_activate_spool(0)

    # This is the main function for initiating a tool change, it will handle unload if necessary
    def _change_tool(self, tool, in_print, skip_tip=True):
        self._log_debug("Tool change initiated %s" % ("with slicer tip forming" if skip_tip else "with standalone MMU tip forming"))
        skip_unload = False
        initial_tool_string = "Unknown" if self.tool_selected < 0 else ("T%d" % self.tool_selected)
        if tool == self.tool_selected and self.tool_to_gate_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
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
        if self.tool_to_gate_map[tool] == self.gate_selected and self.filament_pos == self.FILAMENT_POS_LOADED:
            self._select_tool(tool)
            self.gcode.run_script_from_command("M117 T%s" % tool)
            return False

        # Notify start of actual toolchange operation
        self.printer.send_event("mmu:toolchange", self, self._last_tool, self._next_tool)

        if in_print:
            self._save_toolhead_position_and_lift("change_tool", z_hop_height=self.z_hop_height_toolchange)
            gcode = self.printer.lookup_object('gcode_macro _MMU_PRE_UNLOAD', None)
            if gcode is not None:
                self._wrap_gcode_command("_MMU_PRE_UNLOAD", exception=True)

        # Identify the unitialized startup use case and make it easy for user
        if not self.is_homed or self.tool_selected == self.TOOL_GATE_UNKNOWN:
            self._log_info("MMU not homed, homing it before continuing...")
            self._home(tool)
            skip_unload = True

        if not skip_unload:
            self._unload_tool(skip_tip=skip_tip)

        if in_print:
            gcode = self.printer.lookup_object('gcode_macro _MMU_POST_UNLOAD', None)
            if gcode is not None:
                self._wrap_gcode_command("_MMU_POST_UNLOAD", exception=True)
        self._select_and_load_tool(tool)

        self._track_swap_completed()

        if in_print:
            gcode = self.printer.lookup_object('gcode_macro _MMU_POST_LOAD', None)
            if gcode is not None:
                self._wrap_gcode_command("_MMU_POST_LOAD", exception=True)
            self._restore_toolhead_position("change_tool")
        self._restore_tool_override(self.tool_selected) # Must be after _restore_toolhead_position()

        self.gcode.run_script_from_command("M117 T%s" % tool)
        return True

    def _unselect_tool(self):
        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        self._servo_auto()

    def _select_tool(self, tool, move_servo=True):
        if tool < 0 or tool >= self.mmu_num_gates:
            self._log_always("Tool %d does not exist" % tool)
            return

        gate = self.tool_to_gate_map[tool]
        if tool == self.tool_selected and gate == self.gate_selected:
            return

        self._log_debug("Selecting tool T%d on gate #%d..." % (tool, gate))
        self._select_gate(gate)
        self._set_tool_selected(tool)
        if move_servo:
            self._servo_auto()
        self._log_info("Tool T%d enabled%s" % (tool, (" on gate #%d" % gate) if tool != gate else ""))

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
        if gate == self.TOOL_GATE_UNKNOWN or gate == self.TOOL_GATE_BYPASS:
            self._set_gate_ratio(1.)
        else:
            self._set_gate_ratio(self._get_gate_ratio(gate))

    def _set_tool_selected(self, tool):
        self.tool_selected = tool
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_TOOL_SELECTED, self.tool_selected))

    def _set_gate_ratio(self, ratio=1.):
        self._log_trace("Setting MMU gear motor rotation distance ratio to %.6f" % ratio)
        new_rotation_distance = ratio * self.ref_gear_rotation_distance
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
            self._log_debug("Updating following gate/spool_id pairs from spoolman: %s" % gate_ids)
            try:
                webhooks = self.printer.lookup_object('webhooks')
                webhooks.call_remote_method("spoolman_get_filaments", gate_ids=gate_ids)
            except Exception as e:
                self._log_error("Error while retrieving spoolman info: %s" % str(e))


### CORE GCODE COMMANDS ##########################################################

    cmd_MMU_HOME_help = "Home the MMU selector"
    def cmd_MMU_HOME(self, gcmd):
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
                if self.tool_selected >= 0 and self.tool_to_gate_map[self.tool_selected] == gate:
                    pass
                else:
                    tool_found = False
                    for tool in range(len(self.tool_to_gate_map)):
                        if self.tool_to_gate_map[tool] == gate:
                            self._select_tool(tool)
                            tool_found = True
                            break
                    if not tool_found:
                        self._set_tool_selected(self.TOOL_GATE_UNKNOWN)
        except MmuError as ee:
            self._mmu_pause(str(ee))
        finally:
            self._servo_auto()

    cmd_MMU_CHANGE_TOOL_help = "Perform a tool swap (called from Tx command)"
    def cmd_MMU_CHANGE_TOOL(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(): return

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        standalone = bool(gcmd.get_int('STANDALONE', 0, minval=0, maxval=1))
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1))
        # TODO currently not registered directly as Tx commands because not visible by Mainsail/Fluuid
        cmd = gcmd.get_command().strip()
        match = re.match(r'[Tt](\d{1,3})$', cmd)
        if match:
            tool = int(match.group(1))
            if tool < 0 or tool > self.mmu_num_gates - 1:
                raise gcmd.error("Invalid tool")
        else:
            tool = gcmd.get_int('TOOL', minval=0, maxval=self.mmu_num_gates - 1)
        skip_tip = self._is_in_print() and not (standalone or self.force_form_tip_standalone)
        if self.filament_pos == self.FILAMENT_POS_UNKNOWN and self.is_homed: # Will be done later if not homed
            self._recover_filament_pos(message=True)

        if self._has_encoder():
            self.encoder_sensor.update_clog_detection_length()
        with self._wrap_disable_encoder(): # Don't want runout accidently triggering during tool change
            self._last_tool = self.tool_selected
            self._next_tool = tool
            attempts = 2 if self.retry_tool_change_on_error and (self._is_in_print(force_in_print) or standalone) else 1
            try:
                for i in range(attempts):
                    try:
                        if self._change_tool(tool, self._is_printing(force_in_print), skip_tip):
                            self._dump_statistics(job=not quiet, gate=not quiet)
                        continue
                    except MmuError as ee:
                        if i == attempts - 1:
                            self._mmu_pause("%s.\nOccured when changing tool: %s" % (str(ee), self._last_toolchange))
                            return
                        self._log_error("%s.\nOccured when changing tool: %s. Retrying..." % (str(ee), self._last_toolchange))
                        # Try again but recover_filament_pos will ensure conservative treatment of unload
                        self._recover_filament_pos()
    
                self._sync_gear_to_extruder(self.sync_to_extruder and self._is_in_print(force_in_print), servo=True, current=self._is_in_print(force_in_print))
            finally:
                self._next_tool = self.TOOL_GATE_UNKNOWN

    cmd_MMU_LOAD_help = "Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTUDER_ONLY=1)"
    def cmd_MMU_LOAD(self, gcmd):
        if self._check_is_disabled(): return
        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1) or in_bypass)
        with self._wrap_disable_encoder(): # Don't want runout accidently triggering during filament load
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

    cmd_MMU_EJECT_help = "Eject filament and park it in the MMU or optionally unloads just the extruder (EXTRUDER_ONLY=1)"
    def cmd_MMU_EJECT(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_is_calibrated(): return
        in_bypass = self.gate_selected == self.TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        skip_tip = bool(gcmd.get_int('SKIP_TIP', 0, minval=0, maxval=1))

        with self._wrap_disable_encoder(): # Don't want runout accidently triggering during filament load
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
        self._on_print_start()

    cmd_MMU_PRINT_END_help = "Cleans up state after after print end"
    def cmd_MMU_PRINT_END(self, gcmd):
        end_state = gcmd.get('STATE', "complete")
        if end_state in ["complete", "error", "cancelled", "ready", "standby"]:
            self._on_print_end(end_state)
        else:
            raise gcmd.error("Unknown endstate '%s'" % end_state)

    cmd_MMU_PAUSE_help = "Pause the current print and lock the MMU operations"
    def cmd_MMU_PAUSE(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        force_in_print = bool(gcmd.get_int('FORCE_IN_PRINT', 0, minval=0, maxval=1))
        self._mmu_pause("MMU_PAUSE macro was directly called", force_in_print)

    cmd_MMU_UNLOCK_help = "Wakeup the MMU prior to resume to restore temperatures and timeouts"
    def cmd_MMU_UNLOCK(self, gcmd):
        if self._check_is_disabled(): return
        if self._is_mmu_pause_locked():
            self._mmu_unlock()

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_RESUME_help = "Wrapper around default RESUME macro"
    def cmd_MMU_RESUME(self, gcmd):
        if not self.is_enabled:
            self._wrap_gcode_command("__RESUME", None) # User defined or Klipper default behavior
            return

        self._log_trace("MMU RESUME wrapper called")
        if not self._is_paused() and not self._is_mmu_paused():
            self._log_always("Print is not paused. Resume ignored.")
            return

        if self._is_mmu_pause_locked():
            self._mmu_unlock()

        if self._is_mmu_paused():
            # Sanity check we are ready to go
            if self._is_in_print() and self.filament_pos != self.FILAMENT_POS_LOADED:
                if self._check_sensor(self.ENDSTOP_TOOLHEAD) is True:
                    self._set_filament_pos_state(self.FILAMENT_POS_LOADED, silent=True)
                    self._log_always("Automatically set filament state to LOADED based on toolhead sensor")

        self._wrap_gcode_command("__RESUME", None)
        self._mmu_resume()
        # Continue printing...

    # Not a user facing command - used in automatic wrapper
    cmd_PAUSE_help = "Wrapper around default PAUSE macro"
    def cmd_PAUSE(self, gcmd):
        if self.is_enabled:
            self._log_trace("MMU PAUSE wrapper called")
            if not self.paused_extruder_temp: # Only save the initial pause temp
                self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp
        self._wrap_gcode_command("__PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_CLEAR_PAUSE_help = "Wrapper around default CLEAR_PAUSE macro"
    def cmd_CLEAR_PAUSE(self, gcmd):
        if self.is_enabled:
            self._log_trace("MMU CLEAR_PAUSE wrapper called")
        self._wrap_gcode_command("__CLEAR_PAUSE", None) # User defined or Klipper default behavior

    # Not a user facing command - used in automatic wrapper
    cmd_MMU_CANCEL_PRINT_help = "Wrapper around default CANCEL_PRINT macro"
    def cmd_MMU_CANCEL_PRINT(self, gcmd):
        if not self.is_enabled:
            self._wrap_gcode_command("__CANCEL_PRINT", None) # User defined or Klipper default behavior
            return

        self._log_debug("MMU_CANCEL_PRINT wrapper called")
        self._save_toolhead_position_and_lift(z_hop_height=self.z_hop_height_error)
        self._wrap_gcode_command("__CANCEL_PRINT", None)
        self._on_print_end("cancelled")

    cmd_MMU_RECOVER_help = "Recover the filament location and set MMU state after manual intervention/movement"
    def cmd_MMU_RECOVER(self, gcmd):
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
            gate = self.tool_to_gate_map[tool]
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
                    gate = self.tool_to_gate_map[tool]
                    if self.gate_status[gate] == self.GATE_EMPTY:
                        self._log_always("Skipping tool %d of %d because gate %d is empty" % (tool, self.mmu_num_gates, gate))
                    else:
                        self._log_always("Testing tool %d of %d (gate %d)" % (tool, self.mmu_num_gates, gate))
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
        if self._check_is_disabled(): return
        if self._check_in_bypass(): return
        self._servo_down()
        self._motors_off(motor="gear")

    cmd_MMU_TEST_TRACKING_help = "Test the tracking of gear feed and encoder sensing"
    def cmd_MMU_TEST_TRACKING(self, gcmd):
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
            if not self._is_filament_in_bowden():
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
        if self._check_is_disabled(): return
        actual,_,measured,_ = self._move_cmd(gcmd, "Test move")
        self._log_always("Moved %.1fmm%s" % (actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_HOMING_MOVE_help = "Test filament homing move to help debug setup / options"
    def cmd_MMU_TEST_HOMING_MOVE(self, gcmd):
        if self._check_is_disabled(): return
        actual,homed,measured,_ = self._homing_move_cmd(gcmd, "Test homing move")
        self._log_always("%s after %.1fmm%s" % (("Homed" if homed else "Did not home"), actual, (" (measured %.1fmm)" % measured) if self._can_use_encoder() else ""))

    cmd_MMU_TEST_CONFIG_help = "Runtime adjustment of MMU configuration for testing or in-print tweaking purposes"
    def cmd_MMU_TEST_CONFIG(self, gcmd):
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

        # TMC current control
        self.sync_gear_current = gcmd.get_int('SYNC_GEAR_CURRENT', self.sync_gear_current, minval=10, maxval=100)
        self.extruder_homing_current = gcmd.get_int('EXTRUDER_HOMING_CURRENT', self.extruder_homing_current, minval=10, maxval=100)
        self.extruder_form_tip_current = gcmd.get_int('EXTRUDER_FORM_TIP_CURRENT', self.extruder_form_tip_current, minval=100, maxval=150)

        # Homing, loading and unloading controls
        self.gate_homing_endstop = gcmd.get('GATE_HOMING_ENDSTOP', self.gate_homing_endstop)
        if self.gate_homing_endstop not in self.GATE_ENDSTOPS:
            raise gmd.error("gate_homing_endstop is invalid. Options are: %s" % self.GATE_ENSTOPS)
        self.gate_endstop_to_encoder = gcmd.get_float('GATE_ENDSTOP_TO_ENCODER', self.gate_endstop_to_encoder)
        self.gate_parking_distance = gcmd.get('GATE_PARKING_DISTANCE', self.gate_parking_distance)
        self.bowden_apply_correction = gcmd.get_int('BOWDEN_APPLY_CORRECTION', self.bowden_apply_correction, minval=0, maxval=1)
        self.bowden_allowable_unload_delta = self.bowden_allowable_load_delta = gcmd.get_float('BOWDEN_ALLOWABLE_LOAD_DELTA', self.bowden_allowable_load_delta, minval=1., maxval=50.)
        self.bowden_pre_unload_test = gcmd.get_int('BOWDEN_PRE_UNLOAD_TEST', self.bowden_pre_unload_test, minval=0, maxval=1)

        self.extruder_homing_endstop = gcmd.get('EXTRUDER_HOMING_ENDSTOP', self.extruder_homing_endstop)
        if self.extruder_homing_endstop not in self.EXTRUDER_ENDSTOPS:
            raise gmd.error("extruder_homing_endstop is invalid. Options are: %s" % self.EXTRUDER_ENDSTOPS)
        self.extruder_homing_max = gcmd.get_float('EXTRUDER_HOMING_MAX', self.extruder_homing_max, above=10.)
        self.extruder_force_homing = gcmd.get_int('EXTRUDER_FORCE_HOMING', self.extruder_force_homing, minval=0, maxval=1)

        self.toolhead_homing_max = gcmd.get_float('TOOLHEAD_HOMING_MAX', self.toolhead_homing_max, minval=0.)
        self.toolhead_sync_unload = gcmd.get_int('TOOLHEAD_SYNC_UNLOAD', self.toolhead_sync_unload, minval=0, maxval=1)
        self.toolhead_extruder_to_nozzle = gcmd.get_float('TOOLHEAD_EXTRUDER_TO_NOZZLE', self.toolhead_extruder_to_nozzle, minval=0.)
        self.toolhead_sensor_to_nozzle = gcmd.get_float('TOOLHEAD_SENSOR_TO_NOZZLE', self.toolhead_sensor_to_nozzle, minval=0.)
        self.gcode_load_sequence = gcmd.get_int('GCODE_LOAD_SEQUENCE', self.gcode_load_sequence, minval=0, maxval=1)
        self.gcode_unload_sequence = gcmd.get_int('GCODE_UNLOAD_SEQUENCE', self.gcode_unload_sequence, minval=0, maxval=1)

        # Software behavior options
        self.z_hop_height_error = gcmd.get_float('Z_HOP_HEIGHT_ERROR', self.z_hop_height_error, minval=0.)
        self.z_hop_height_toolchange = gcmd.get_float('Z_HOP_HEIGHT_TOOLCHANGE', self.z_hop_height_toolchange, minval=0.)
        self.z_hop_speed = gcmd.get_float('Z_HOP_SPEED', self.z_hop_speed, minval=1.)
        self.selector_touch = self.ENDSTOP_SELECTOR_TOUCH in self.selector_rail.get_extra_endstop_names() and self.selector_touch_enable
        self.enable_endless_spool = gcmd.get_int('ENABLE_ENDLESS_SPOOL', self.enable_endless_spool, minval=0, maxval=1)
        self.enable_spoolman = gcmd.get_int('ENABLE_SPOOLMAN', self.enable_spoolman, minval=0, maxval=1)
        self.log_level = gcmd.get_int('LOG_LEVEL', self.log_level, minval=0, maxval=4)
        self.log_visual = gcmd.get_int('LOG_VISUAL', self.log_visual, minval=0, maxval=2)
        self.log_statistics = gcmd.get_int('LOG_STATISTICS', self.log_statistics, minval=0, maxval=1)
        self.slicer_tip_park_pos = gcmd.get_float('SLICER_TIP_PARK_POS', self.slicer_tip_park_pos, minval=0.)
        self.force_form_tip_standalone = gcmd.get_int('FORCE_FORM_TIP_STANDALONE', self.force_form_tip_standalone, minval=0, maxval=1)
        self.strict_filament_recovery = gcmd.get_int('STRICT_FILAMENT_RECOVERY', self.strict_filament_recovery, minval=0, maxval=1)
        self.encoder_move_validation = gcmd.get_int('ENCODER_MOVE_VALIDATION', self.encoder_move_validation, minval=0, maxval=1)
        self.auto_calibrate_gates = gcmd.get_int('AUTO_CALIBRATE_GATES', self.auto_calibrate_gates, minval=0, maxval=1)
        self.retry_tool_change_on_error = gcmd.get_int('RETRY_TOOL_CHANGE_ON_ERROR', self.retry_tool_change_on_error, minval=0, maxval=1)
        self.print_start_detection = gcmd.get_int('PRINT_START_DETECTION', self.print_start_detection, minval=0, maxval=1)
        self.pause_macro = gcmd.get('PAUSE_MACRO', self.pause_macro)
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
        msg += "\nsync_gear_current = %d" % self.sync_gear_current
        msg += "\nextruder_homing_current = %d" % self.extruder_homing_current
        msg += "\nextruder_form_tip_current = %d" % self.extruder_form_tip_current

        msg += "\n\nLOADING/UNLOADING:"
        msg += "\ngate_homing_endstop = %s" % self.gate_homing_endstop
        if self.gate_homing_endstop == self.ENDSTOP_GATE:
            msg += "\ngate_endstop_to_encoder = %s" % self.gate_endstop_to_encoder
        msg += "\ngate_parking_distance = %s" % self.gate_parking_distance
        if self._has_encoder():
            msg += "\nbowden_apply_correction = %d" % self.bowden_apply_correction
            msg += "\nbowden_allowable_load_delta = %d" % self.bowden_allowable_load_delta
            msg += "\nbowden_pre_unload_test = %d" % self.bowden_pre_unload_test
        msg += "\nextruder_force_homing = %d" % self.extruder_force_homing
        msg += "\nextruder_homing_endstop = %s" % self.extruder_homing_endstop
        msg += "\nextruder_homing_max = %.1f" % self.extruder_homing_max
        msg += "\ntoolhead_sync_unload = %d" % self.toolhead_sync_unload
        msg += "\ntoolhead_homing_max = %.1f" % self.toolhead_homing_max
        msg += "\ntoolhead_extruder_to_nozzle = %.1f" % self.toolhead_extruder_to_nozzle
        msg += "\ntoolhead_sensor_to_nozzle = %.1f" % self.toolhead_sensor_to_nozzle
        msg += "\ngcode_load_sequence = %d" % self.gcode_load_sequence
        msg += "\ngcode_unload_sequence = %d" % self.gcode_unload_sequence

        msg += "\n\nOTHER:"
        msg += "\nz_hop_height_error = %.1f" % self.z_hop_height_error
        msg += "\nz_hop_height_toolchange = %.1f" % self.z_hop_height_toolchange
        msg += "\nz_hop_speed = %.1f" % self.z_hop_speed
        if self._has_encoder():
            msg += "\nenable_clog_detection = %d" % self.enable_clog_detection
            msg += "\nenable_endless_spool = %d" % self.enable_endless_spool
        msg += "\nenable_spoolman = %d" % self.enable_spoolman
        msg += "\nslicer_tip_park_pos = %.1f" % self.slicer_tip_park_pos
        msg += "\nforce_form_tip_standalone = %d" % self.force_form_tip_standalone
        if self._has_encoder():
            msg += "\nstrict_filament_recovery = %d" % self.strict_filament_recovery
            msg += "\nencoder_move_validation = %d" % self.encoder_move_validation
            msg += "\nauto_calibrate_gates = %d" % self.auto_calibrate_gates
        msg += "\nretry_tool_change_on_error = %d" % self.retry_tool_change_on_error
        msg += "\nprint_start_detection = %d" % self.print_start_detection
        msg += "\nlog_level = %d" % self.log_level
        msg += "\nlog_visual = %d" % self.log_visual
        msg += "\nlog_statistics = %d" % self.log_statistics
        msg += "\npause_macro = %s" % self.pause_macro
        msg += "\nform_tip_macro = %s" % self.form_tip_macro

        msg += "\n\nCALIBRATION:"
        msg += "\nmmu_calibration_bowden_length = %.1f" % self.calibrated_bowden_length
        if self._has_encoder():
            msg += "\nmmu_calibration_clog_length = %.1f" % clog_length
        self._log_info(msg)


###########################################
# RUNOUT, ENDLESS SPOOL and GATE HANDLING #
###########################################

    def _handle_runout(self, force_runout=False):
        if self.tool_selected < 0:
            raise MmuError("Filament runout or clog on an unknown or bypass tool - manual intervention is required")

        if self.filament_pos != self.FILAMENT_POS_LOADED and not force_runout:
            raise MmuError("Filament runout or clog when filament is not fully loaded - manual intervention is required")

        self._log_info("Issue on tool T%d" % self.tool_selected)
        self._save_toolhead_position_and_lift("runout", z_hop_height=self.z_hop_height_toolchange)

        # Check for clog by looking for filament in the encoder
        self._log_debug("Checking if this is a clog or a runout (state %d)..." % self.filament_pos)
        found = self._check_filament_at_gate()
        if found and not force_runout:
            if self._has_encoder():
                self.encoder_sensor.update_clog_detection_length()
            raise MmuError("A clog has been detected and requires manual intervention")

        # We have a filament runout
        with self._wrap_disable_encoder(): # Don't want runout accidently triggering during swap
            self._log_always("A runout has been detected")
            if self.enable_endless_spool:
                group = self.endless_spool_groups[self.gate_selected]
                self._log_info("EndlessSpool checking for additional spools in group %d..." % group)
                self._set_gate_status(self.gate_selected, self.GATE_EMPTY) # Indicate current gate is empty
                next_gate = -1
                checked_gates = []
                for i in range(self.mmu_num_gates - 1):
                    check = (self.gate_selected + i + 1) % self.mmu_num_gates
                    if self.endless_spool_groups[check] == group:
                        checked_gates.append(check)
                        if self.gate_status[check] != self.GATE_EMPTY:
                            next_gate = check
                            break
                if next_gate == -1:
                    self._log_info("No more available spools found in ES_Group_%d - manual intervention is required" % self.endless_spool_groups[self.tool_selected])
                    self._log_info(self._tool_to_gate_map_to_human_string())
                    raise MmuError("No more EndlessSpool spools available after checking gates %s" % checked_gates)
                self._log_info("Remapping T%d to gate #%d" % (self.tool_selected, next_gate))
                # Save the extruder temperature for the resume after swapping filaments.
                if not self.paused_extruder_temp: # Only save the initial pause temp
                    self.paused_extruder_temp = self.printer.lookup_object(self.extruder_name).heater.target_temp

                gcode = self.printer.lookup_object('gcode_macro _MMU_ENDLESS_SPOOL_PRE_UNLOAD', None)
                if gcode is not None:
                    self._wrap_gcode_command("_MMU_ENDLESS_SPOOL_PRE_UNLOAD", exception=True)

                detected, park_pos = self._form_tip_standalone(extruder_only=True)
                if not detected:
                    self._log_info("Filament didn't reach encoder after tip forming move")
                self._unload_tool(skip_tip=True)
                self._remap_tool(self.tool_selected, next_gate)
                self._select_and_load_tool(self.tool_selected)

                gcode = self.printer.lookup_object('gcode_macro _MMU_ENDLESS_SPOOL_POST_LOAD', None)
                if gcode is not None:
                    self._wrap_gcode_command("_MMU_ENDLESS_SPOOL_POST_LOAD", exception=True)
                self._restore_toolhead_position("EndlessSpool")

                self._sync_gear_to_extruder(self.sync_to_extruder and self._is_in_print(force_runout), servo=True, current=self._is_in_print())
                self._initialize_filament_position()    # Encoder 0000
                # Continue printing...
            else:
                raise MmuError("EndlessSpool mode is off - manual intervention is required")

    def _set_tool_to_gate(self, tool, gate):
        self.tool_to_gate_map[tool] = gate
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.tool_to_gate_map))

    def _set_gate_status(self, gate, state):
        if gate >= 0:
            if state != self.gate_status[gate]:
                self.gate_status[gate] = state
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_GATE_STATUS, self.gate_status))
                gcode = self.printer.lookup_object('gcode_macro _MMU_GATE_MAP_CHANGED', None)
                if gcode is not None:
                    self._wrap_gcode_command("_MMU_GATE_MAP_CHANGED GATE='%d'" % gate)

    # Use pre-gate sensors (if fitted) to "correct" gate status
    # Return True if update made
    def _validate_gate_status(self, gate_status):
        updated = False
        for gate, status in enumerate(gate_status):
            sensor = self.printer.lookup_object("filament_switch_sensor mmu_pre_gate_%d" % gate, None)
            if sensor is not None and sensor.runout_helper.sensor_enabled:
                detected = sensor.runout_helper.filament_present
                if detected and status == self.GATE_EMPTY:
                    gate_status[gate] = self.GATE_UNKNOWN
                    updated = True
                elif not detected and status != self.GATE_EMPTY:
                    gate_status[gate] = self.GATE_EMPTY
                    updated = True
        return gate_status

    def _get_filament_char(self, gate_status, no_space=False, show_source=False):
        if gate_status == self.GATE_AVAILABLE_FROM_BUFFER:
            return "B" if show_source else "*"
        elif gate_status == self.GATE_AVAILABLE:
            return "S" if show_source else "*"
        elif gate_status == self.GATE_EMPTY:
            return ("." if no_space else " ")
        else:
            return "?"

    def _tool_to_gate_map_to_human_string(self, summary=False):
        msg = ""
        if not summary:
            num_tools = self.mmu_num_gates
            for i in range(num_tools): # Tools
                msg += "\n" if i else ""
                gate = self.tool_to_gate_map[i]
                msg += "%s-> Gate #%d%s" % (("T%d " % i)[:3], gate, "(" + self._get_filament_char(self.gate_status[gate], show_source=True) + ")")
                if self.enable_endless_spool:
                    group = self.endless_spool_groups[gate]
                    es = " ES_Group_%s: " % group
                    prefix = ""
                    starting_gate = self.tool_to_gate_map[i]
                    for j in range(num_tools): # Gates
                        gate = (j + starting_gate) % num_tools
                        if self.endless_spool_groups[gate] == group:
                            es += "%s%d%s" % (prefix, gate,self._get_filament_char(self.gate_status[gate]))
                            prefix = "> "
                    msg += es
                if i == self.tool_selected:
                    msg += " [SELECTED on gate #%d]" % self.gate_selected
            msg += "\n\n"
            msg += self._gate_map_to_human_string(True)
        else:
            multi_tool = False
            msg_gates = "Gates: "
            msg_avail = "Avail: "
            msg_tools = "Tools: "
            msg_selct = "Selct: "
            for g in range(self.mmu_num_gates):
                msg_gates += ("|#%d " % g)[:4]
                msg_avail += "| %s " % self._get_filament_char(self.gate_status[g], no_space=True, show_source=True)
                tool_str = ""
                prefix = ""
                for t in range(self.mmu_num_gates):
                    if self.tool_to_gate_map[t] == g:
                        if len(prefix) > 0: multi_tool = True
                        tool_str += "%sT%d" % (prefix, t)
                        prefix = "+"
                if tool_str == "": tool_str = " . "
                msg_tools += ("|%s " % tool_str)[:4]
                if self.gate_selected == g:
                    msg_selct += ("| %s " % self._get_filament_char(self.gate_status[g], no_space=True))
                else:
                    msg_selct += "|---" if self.gate_selected != self.TOOL_GATE_UNKNOWN and self.gate_selected == (g - 1) else "----"
            msg += msg_gates
            msg += "|\n"
            msg += msg_tools
            msg += "|\n" # msg += "|%s\n" % (" Some gates support multiple tools!" if multi_tool else "")
            msg += msg_avail
            msg += "|\n"
            msg += msg_selct
            msg += "|" if self.gate_selected == self.mmu_num_gates - 1 else "-"
            if self.is_homed:
                msg += " Bypass" if self.gate_selected == self.TOOL_GATE_BYPASS else (" T%d" % self.tool_selected) if self.tool_selected >= 0 else ""
            else:
                msg += " NOT HOMED"
        return msg

    def _gate_map_to_human_string(self, detail=False):
        msg = "MMU Gates / Filaments:"
        for g in range(self.mmu_num_gates):
            material = self.gate_material[g] if self.gate_material[g] != "" else "n/a"
            color = self.gate_color[g] if self.gate_color[g] != "" else "n/a"
            available = {
                self.GATE_AVAILABLE_FROM_BUFFER: "Buffered",
                self.GATE_AVAILABLE: "Available",
                self.GATE_EMPTY: "Empty",
                self.GATE_UNKNOWN: "Unknown"
            }[self.gate_status[g]]
            if detail:
                msg += "\nGate #%d%s" % (g, "(" + self._get_filament_char(self.gate_status[g], show_source=True) + ")")
                tool_str = " -> "
                prefix = ""
                for t in range(self.mmu_num_gates):
                    if self.tool_to_gate_map[t] == g:
                        tool_str += "%sT%d" % (prefix, t)
                        prefix = ","
                msg += tool_str
                msg += "?, " if prefix == "" else ", "
            else:
                msg += ("\nGate #%d: " % g)
            msg += ("Status: %s, Material: %s, Color: %s" % (available, material, color))
            if self.enable_spoolman:
                spool_id = str(self.gate_spool_id[g]) if self.gate_spool_id[g] > 0 else "n/a"
                msg += (", SpoolID: %s" % (spool_id))
            if detail and g == self.gate_selected:
                msg += " [SELECTED%s]" % ((" supporting tool T%d" % self.tool_selected) if self.tool_selected >= 0 else "")
        return msg

    def _remap_tool(self, tool, gate, available=None):
        self._set_tool_to_gate(tool, gate)
        if available is not None:
            self._set_gate_status(gate, available)

    def _reset_ttg_mapping(self):
        self._log_debug("Resetting TTG map")
        self.tool_to_gate_map = self.default_tool_to_gate_map
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.tool_to_gate_map))
        self._unselect_tool()

    def _reset_gate_map(self):
        self._log_debug("Resetting gate map")
        self.gate_status = self._validate_gate_status(list(self.default_gate_status))
        self.gate_material = list(self.default_gate_material)
        self._update_gate_color(list(self.default_gate_color))
        self.gate_spool_id = list(self.default_gate_spool_id)
        self._persist_gate_map()


### GCODE COMMANDS FOR RUNOUT, TTG MAP, GATE MAP and GATE LOGIC ##################################

    cmd_MMU_ENCODER_RUNOUT_help = "Internal encoder filament runout handler"
    def cmd_MMU_ENCODER_RUNOUT(self, gcmd):
        if self._check_is_disabled(): return
        force_runout = bool(gcmd.get_int('FORCE_RUNOUT', 0, minval=0, maxval=1))
        try:
            self._handle_runout(force_runout)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_ENCODER_INSERT_help = "Internal encoder filament detection handler"
    def cmd_MMU_ENCODER_INSERT(self, gcmd):
        if self._check_is_disabled(): return
        self._log_debug("Filament insertion not implemented yet! Check back later")
        # TODO Future preload feature especially bypass :-)
        #try:
        #    self._handle_detection()
        #except MmuError as ee:
        #    self._mmu_pause(str(ee))

    cmd_MMU_GATE_RUNOUT_help = "Internal gate filament runout handler"
    def cmd_MMU_GATE_RUNOUT(self, gcmd):
        if self._check_is_disabled(): return
        force_runout = bool(gcmd.get_int('FORCE_RUNOUT', 0, minval=0, maxval=1))
        try:
            self._handle_runout(force_runout)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_GATE_INSERT_help = "Internal gate filament detection handler"
    def cmd_MMU_GATE_INSERT(self, gcmd):
        if self._check_is_disabled(): return
        self._log_debug("Filament insertion not implemented yet! Check back later")
        # TODO Future preload feature see MMU_ENCODER_INSERT

    # This callback is not protected by klipper is_printing check so be careful
    cmd_MMU_PRE_GATE_RUNOUT_help = "Internal pre-gate filament runout handler"
    def cmd_MMU_PRE_GATE_RUNOUT(self, gcmd):
        active = self._is_printer_printing()
        if self._check_is_disabled(): return
        try:
            gate = gcmd.get_int('GATE')
            self._log_debug("Filament runout detected by pre-gate sensor on gate #%d" % gate)
            self._set_gate_status(gate, self.GATE_EMPTY)
            if self._is_in_print() and active and gate == self.gate_selected and self.enable_endless_spool == 2:
                self._handle_runout()
        except MmuError as ee:
            self._mmu_pause(str(ee))
        
    # This callback is not protected by klipper is_printing check so be careful
    cmd_MMU_PRE_GATE_INSERT_help = "Internal pre-gate filament detection handler"
    def cmd_MMU_PRE_GATE_INSERT(self, gcmd):
        active = self._is_printer_printing()
        if self._check_is_disabled(): return
        try:
            gate = gcmd.get_int('GATE')
            self._log_debug("Filament insertion detected by pre-gate sensor on gate #%d" % gate)
            self._set_gate_status(gate, self.GATE_UNKNOWN)
            if not self._is_in_print() and not active:
                self.cmd_MMU_PRELOAD(gcmd)
        except MmuError as ee:
            self._mmu_pause(str(ee))

    cmd_MMU_M400_help = "Wait on both move queues"
    def cmd_MMU_M400(self, gcmd):
        self._movequeues_wait_moves(toolhead=True, mmu_toolhead=True)

    cmd_MMU_REMAP_TTG_help = "Display or remap a tool to a specific gate and set gate availability"
    def cmd_MMU_REMAP_TTG(self, gcmd):
        if self._check_is_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        ttg_map = gcmd.get('MAP', "!")
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        available = gcmd.get_int('AVAILABLE', -1, minval=0, maxval=1)

        if reset == 1:
            self._reset_ttg_mapping()
        elif ttg_map != "!":
            ttg_map = gcmd.get('MAP').split(",")
            if len(ttg_map) != self.mmu_num_gates:
                self._log_always("The number of map values (%d) is not the same as number of gates (%d)" % (len(ttg_map), self.mmu_num_gates))
                return
            self.tool_to_gate_map = []
            for gate in ttg_map:
                if gate.isdigit():
                    self.tool_to_gate_map.append(int(gate))
                else:
                    self.tool_to_gate_map.append(0)
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_MMU_TOOL_TO_GATE_MAP, self.tool_to_gate_map))
        elif gate != -1:
            if available == -1:
                available = self.gate_status[gate]
            if tool == -1:
                self._set_gate_status(gate, available)
            else:
                self._remap_tool(tool, gate, available)
        else:
            quiet = False # Display current TTG map
        if not quiet:
            self._log_info(self._tool_to_gate_map_to_human_string())

    cmd_MMU_GATE_MAP_help = "Display or define the type and color of filaments on each gate"
    def cmd_MMU_GATE_MAP(self, gcmd):
        if self._check_is_disabled(): return
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        gates = gcmd.get('GATES', "!")
        gmapstr = gcmd.get('MAP', "{}") # Hidden option for bulk update from moonraker component
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)

        try:
            gate_map = ast.literal_eval(gmapstr)
        except (SyntaxError, ValueError) as e:
            self._log_debug("Exception whilst parsing gate map in MMU_GATE_MAP: %s" % str(e))

        if reset:
            self._reset_gate_map()

        elif not gate_map == {}:
            for gate, fil in gate_map.items():
                if self.gate_spool_id[gate] == fil['spool_id']:
                    self.gate_material[gate] = fil['material']
                    self.gate_color[gate] = fil['color']
                else:
                    self._log_debug("Assertion failure: Spool_id changed for gate #%d in MMU_GATE_MAP. Dict=%s" % (gate, fil))

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
                color = self._validate_color(color)
                if color is None:
                    raise gcmd.error("Color specification must be in form 'rrggbb' hexadecimal value (no '#') or valid color name or empty string")
                self.gate_material[gate] = material
                self.gate_color[gate] = color
                self.gate_status[gate] = available
                self.gate_spool_id[gate] = spool_id

            self._update_gate_color(self.gate_color)
            self._persist_gate_map() # This will also update LED status
        else:
            quiet = False # Display current map

        if not quiet:
            self._log_info(self._gate_map_to_human_string())

    cmd_MMU_ENDLESS_SPOOL_help = "Diplay or Manage EndlessSpool functionality and groups"
    def cmd_MMU_ENDLESS_SPOOL(self, gcmd):
        if self._check_is_disabled(): return
        enabled = gcmd.get_int('ENABLE', -1, minval=0, maxval=1)
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        groups = gcmd.get('GROUPS', "!")

        if enabled >= 0:
            self.enable_endless_spool = enabled
            self._log_always("EndlessSpool is enabled")
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (self.VARS_MMU_ENABLE_ENDLESS_SPOOL, self.enable_endless_spool))
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
            self._log_info(self._tool_to_gate_map_to_human_string())

    cmd_MMU_TOOL_OVERRIDES_help = "Displays, sets or clears tool speed and extrusion factors (M220 & M221)"
    def cmd_MMU_TOOL_OVERRIDES(self, gcmd):
        if self._check_is_disabled(): return
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates)
        speed = gcmd.get_int('M220', None, minval=0, maxval=200)
        extrusion = gcmd.get_int('M221', None, minval=0, maxval=200)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            self._set_tool_override(tool, 100, 100)
        elif tool >= 0:
            self._set_tool_override(tool, speed, extrusion)

        msg = ""
        msg_tool = "Tools: "
        msg_sped = "M220 : "
        msg_extr = "M221 : "
         # First line
        for i in range(self.mmu_num_gates):
            range_end = 5
            tool_speed = self.tool_speed_multipliers[i] * 100
            tool_extr = self.tool_extrusion_multipliers[i] * 100
            if i > 9:
                range_end = 6

            msg_tool += ("| T%d  " % i)[:range_end]
            msg_sped += ("| %d  " % tool_speed)[:range_end]
            msg_extr += ("| %d  " % tool_extr)[:range_end]

        msg += msg_tool
        msg += "|\n"
        msg += msg_sped
        msg += "|\n"
        msg += msg_extr
        msg += "|\n"
        self._log_always(msg)

    cmd_MMU_CHECK_GATE_help = "Automatically inspects gate(s), parks filament and marks availability"
    def cmd_MMU_CHECK_GATE(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_calibrated(): return

        quiet = gcmd.get_int('QUIET', 0, minval=0, maxval=1)
        # These three parameters are mutually exclusive so we only process one
        tools = gcmd.get('TOOLS', "!")
        gates = gcmd.get('GATES', "!")
        tool = gcmd.get_int('TOOL', -1, minval=0, maxval=self.mmu_num_gates - 1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)

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
                                    gate = self.tool_to_gate_map[tool]
                                    gates_tools.append([gate, tool])
                        if len(gates_tools) == 0:
                            self._log_debug("No tools to check, assuming default tool is already loaded")
                            return
                    except ValueError as ve:
                        msg = "Invalid TOOLS parameter: %s" % tools
                        if self._is_in_print():
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
                    gate = self.tool_to_gate_map[tool]
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
    
                for gate, tool in gates_tools:
                    try:
                        self._select_gate(gate)
                        self._initialize_filament_position()    # Encoder 0000
                        self.calibrating = True # To suppress visual filament position
                        self._log_info("Checking gate #%d..." % gate)
                        self._load_gate(allow_retry=False, adjust_servo_on_error=False)
                        if tool >= 0:
                            self._log_info("Tool T%d - Filament detected. Gate #%d marked available" % (tool, gate))
                        else:
                            self._log_info("Gate #%d - Filament detected. Marked available" % gate)
                        self._set_gate_status(gate, max(self.gate_status[gate], self.GATE_AVAILABLE))
                        try:
                            self._unload_gate()
                        except MmuError as ee:
                            msg = "Failure during check gate #%d %s: %s" % (gate, "(T%d)" % tool if tool >= 0 else "", str(ee))
                            if self._is_in_print():
                                self._mmu_pause(msg)
                            else:
                                self._log_always(msg)
                            return
                    except MmuError as ee:
                        self._set_gate_status(gate, self.GATE_EMPTY)
                        self._set_filament_pos_state(self.FILAMENT_POS_UNLOADED, silent=True)
                        if tool >= 0:
                            msg = "Tool T%d on gate #%d marked EMPTY" % (tool, gate)
                        else:
                            msg = "Gate #%d marked EMPTY" % gate
                        if self._is_in_print():
                            # Use case of in-print verification of all tools used in print
                            self._mmu_pause(msg)
                            return
                        else:
                            self._log_info(msg)
                    finally:
                        self.calibrating = False
    
                # Reselect original tool and load filament if necessary
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
                    self._log_info(self._tool_to_gate_map_to_human_string(summary=True))
            finally:
                self._servo_auto()

    cmd_MMU_PRELOAD_help = "Preloads filament at specified or current gate"
    def cmd_MMU_PRELOAD(self, gcmd):
        if self._check_is_disabled(): return
        if self._check_not_homed(): return
        if self._check_in_bypass(): return
        if self._check_is_loaded(): return
        if self._check_is_calibrated(): return
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.mmu_num_gates - 1)
        self._log_always("Preloading filament in %s" % (("gate #%d" % gate) if gate >= 0 else "current gate"))
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
                        self._log_always("Filament detected and parked in gate #%d" % gate)
                        return
                    except MmuError as ee:
                        # Exception just means filament is not loaded yet, so continue
                        self._log_trace("Exception on encoder load move: %s" % str(ee))
                self._set_gate_status(gate, self.GATE_EMPTY)
                self._log_always("Filament not detected in gate #%d" % gate)
            except MmuError as ee:
                self._log_always("Filament preload for gate #%d failed: %s" % (gate, str(ee)))
            finally:
                self.calibrating = False
                self._servo_auto()

def load_config(config):
    return Mmu(config)
