# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Container for all parameters relating to logical mmu (i.e. all parameters shared accross units)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, ast
from typing               import Sequence

# Happy Hare imports
from .mmu_constants       import *
from .mmu_base_parameters import TunableParametersBase, ParamSpec


class MmuMachineParameters(TunableParametersBase):

    # ---- Guards ----

    def _guard_has_sensor_extruder_entry(self):
        return self._mmu_machine.mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY)

    def _guard_has_sensor(sensor):
        return lambda self: self._mmu_machine.mmu.sensor_manager.has_sensor(sensor)


    # ---- On-change hooks ----

    def _on_spoolman_support(self, old, new):
        if new != old:
            self.mmu._spoolman_sync()

    def _on_t_macro_color(self, old, new):
        if new != old:
            self.mmu._update_t_macros()


    # ---- Specs ----

    _SPECS: Sequence[ParamSpec] = (
        # Printer interaction config
        ParamSpec('timeout_pause',                 'int',   72000, section="PRINTER", limits=dict(minval=120)),
        ParamSpec('default_idle_timeout',          'int',      -1, section="PRINTER", limits=dict(minval=120)),
        ParamSpec('pending_spool_id_timeout',      'int',      20, section="PRINTER", limits=dict(minval=-1), hidden=True),
        ParamSpec('disable_heater',                'int',     600, section="PRINTER", limits=dict(minval=60)),
        ParamSpec('default_extruder_temp',         'float', 200.0, section="PRINTER", limits=dict(minval=0.0)),
        ParamSpec('extruder_temp_variance',        'float',   2.0, section="PRINTER", limits=dict(minval=1.0)),
        ParamSpec('gcode_load_sequence',           'int',       0, section="PRINTER", hidden=True),
        ParamSpec('gcode_unload_sequence',         'int',       0, section="PRINTER", hidden=True),
        ParamSpec('force_form_tip_standalone',     'int',       0, section="PRINTER", limits=dict(minval=0, maxval=1)),
        ParamSpec('slicer_tip_park_pos',           'float',   0.0, section="PRINTER", limits=dict(minval=0.0), guard=lambda self: not self.force_form_tip_standalone),
        ParamSpec('force_purge_standalone',        'int',       0, section="PRINTER", limits=dict(minval=0, maxval=1)),
        ParamSpec('strict_filament_recovery',      'int',       0, section="PRINTER", limits=dict(minval=0, maxval=1)),
        ParamSpec('filament_recovery_on_pause',    'int',       1, section="PRINTER", limits=dict(minval=0, maxval=1)),
        ParamSpec('retry_tool_change_on_error',    'int',       0, section="PRINTER", limits=dict(minval=0, maxval=1)),
        ParamSpec('print_start_detection',         'int',       1, section="PRINTER", limits=dict(minval=0, maxval=1), hidden=True),
        ParamSpec('startup_reset_ttg_map',         'int',       0, section="PRINTER", limits=dict(minval=0, maxval=1), hidden=True),
        ParamSpec('show_error_dialog',             'int',       1, section="PRINTER", limits=dict(minval=0, maxval=1)),

        # Internal macro overrides
        ParamSpec('pause_macro',                   'str',   'PAUSE',                   section="MACROS"),
        ParamSpec('action_changed_macro',          'str',   '_MMU_ACTION_CHANGED',     section="MACROS", hidden=True),
        ParamSpec('print_state_changed_macro',     'str',   '_MMU_PRINT_STATE_CHANGED',section="MACROS", hidden=True),
        ParamSpec('mmu_event_macro',               'str',   '_MMU_EVENT',              section="MACROS", hidden=True),
        ParamSpec('form_tip_macro',                'str',   '_MMU_FORM_TIP',           section="MACROS"),
        ParamSpec('purge_macro',                   'str',   '',                        section="MACROS"),
        ParamSpec('pre_unload_macro',              'str',   '_MMU_PRE_UNLOAD',         section="MACROS"),
        ParamSpec('post_form_tip_macro',           'str',   '_MMU_POST_FORM_TIP',      section="MACROS"),
        ParamSpec('post_unload_macro',             'str',   '_MMU_POST_UNLOAD',        section="MACROS"),
        ParamSpec('pre_load_macro',                'str',   '_MMU_PRE_LOAD',           section="MACROS"),
        ParamSpec('post_load_macro',               'str',   '_MMU_POST_LOAD_MACRO',    section="MACROS"),
        ParamSpec('unload_sequence_macro',         'str',   '_MMU_UNLOAD_SEQUENCE',    section="MACROS", hidden=True),
        ParamSpec('load_sequence_macro',           'str',   '_MMU_LOAD_SEQUENCE',      section="MACROS", hidden=True),

        # These macros are not currently exposed but provide future flexability
        ParamSpec('error_dialog_macro',            'str',   '_MMU_ERROR_DIALOG',       section="MACROS", hidden=True),
        ParamSpec('error_macro',                   'str',   '_MMU_ERROR',              section="MACROS", hidden=True),
        ParamSpec('toolhead_homing_macro',         'str',   '_MMU_AUTO_HOME',          section="MACROS", hidden=True),
        ParamSpec('park_macro',                    'str',   '_MMU_PARK',               section="MACROS", hidden=True),
        ParamSpec('save_position_macro',           'str',   '_MMU_SAVE_POSITION',      section="MACROS", hidden=True),
        ParamSpec('restore_position_macro',        'str',   '_MMU_RESTORE_POSITION',   section="MACROS", hidden=True),
        ParamSpec('clear_position_macro',          'str',   '_MMU_CLEAR_POSITION',     section="MACROS", hidden=True),

        # User default (reset state) gate map and TTG map
        ParamSpec('default_ttg_map',               'intlist', [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_status',           'intlist', [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_filament_name',    'list',    [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_material',         'list',    [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_color',            'list',    [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_temperature',      'intlist', [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_spool_id',         'intlist', [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_gate_speed_override',   'intlist', [], section="DEFAULT MAPS", hidden=True),
        ParamSpec('default_endless_spool_groups',  'intlist', [], section="DEFAULT MAPS", hidden=True),

        # Configuration for extruder dimensions
        ParamSpec('toolhead_extruder_to_nozzle',   'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=5.0)),
        ParamSpec('toolhead_sensor_to_nozzle',     'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0), guard=_guard_has_sensor(SENSOR_TOOLHEAD)),
        ParamSpec('toolhead_entry_to_extruder',    'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=0.0), guard=_guard_has_sensor(SENSOR_EXTRUDER_ENTRY)),
        ParamSpec('toolhead_residual_filament',    'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=0.0, maxval=50.0)),
        ParamSpec('toolhead_ooze_reduction',       'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=-5.0, maxval=20.0)),

        # TMC current control
        ParamSpec('extruder_form_tip_current',     'int',   100,  section="TOOLHEAD/EXTRUDER", limits=dict(minval=100, maxval=150)),
        ParamSpec('extruder_purge_current',        'int',   100,  section="TOOLHEAD/EXTRUDER", limits=dict(minval=100, maxval=150)),

        # Filament move speeds and accelaration
        ParamSpec('extruder_load_speed',           'float', 15.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0)),
        ParamSpec('extruder_unload_speed',         'float', 15.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0)),
        ParamSpec('extruder_sync_load_speed',      'float', 15.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0)),
        ParamSpec('extruder_sync_unload_speed',    'float', 15.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0)),
        ParamSpec('extruder_accel',                'float', 400.0,section="TOOLHEAD/EXTRUDER", limits=dict(above=10.0)),
        ParamSpec('extruder_homing_speed',         'float', 15.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=1.0)),

        # Macro toolhead tuning (max accel default is derived if 0)
        ParamSpec('macro_toolhead_max_accel',      'float',  0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=0.0)),
        ParamSpec('macro_toolhead_min_cruise_ratio','float', 0.0, section="TOOLHEAD/EXTRUDER", limits=dict(minval=0.0, below=1.0)),

        # Optional features
        ParamSpec('spoolman_support',              'choice', SPOOLMAN_OFF,         section="FEATURES", choices={o: o for o in SPOOLMAN_OPTIONS}),
        ParamSpec('t_macro_color',                 'choice', T_MACRO_COLOR_SLICER, section="FEATURES", choices={o: o for o in T_MACRO_COLOR_OPTIONS}),
        ParamSpec('endless_spool_groups',          'intlist', [], section="FEATURES"),
        ParamSpec('endless_spool_enabled',         'int',      0, section="FEATURES", limits=dict(minval=0, maxval=1)),
        ParamSpec('endless_spool_on_load',         'int',      0, section="FEATURES", limits=dict(minval=0, maxval=1)),
        ParamSpec('endless_spool_eject_gate',      'int',     -1, section="FEATURES", limits=dict(minval=-1)),
        ParamSpec('select_tool_macro',             'str',   None, section="FEATURES"),
        ParamSpec('select_tool_num_switches',      'int',      0, section="FEATURES", limits=dict(minval=0)),
        ParamSpec('bypass_autoload',               'int',      1, section="FEATURES", guard=_guard_has_sensor_extruder_entry, limits=dict(minval=0, maxval=1)),

        # Logging
        ParamSpec('log_level',                     'int',      1, section="LOGGING", limits=dict(minval=0, maxval=4)),
        ParamSpec('log_file_level',                'int',      2, section="LOGGING", limits=dict(minval=-1, maxval=4)),
        ParamSpec('log_statistics',                'int',      0, section="LOGGING", limits=dict(minval=0, maxval=1)),
        ParamSpec('log_visual',                    'int',      1, section="LOGGING", limits=dict(minval=0, maxval=1)),
        ParamSpec('log_startup_status',            'int',      1, section="LOGGING", limits=dict(minval=0, maxval=2)),
        ParamSpec('log_m117_messages',             'int',      1, section="LOGGING", limits=dict(minval=0, maxval=1)),

        # Cosmetic console stuff
        ParamSpec('console_stat_columns',          'list', ['unload', 'load', 'total'],     section="CONSOLE", hidden=True),
        ParamSpec('console_stat_rows',             'list', ['total', 'job', 'job_average'], section="CONSOLE", hidden=True),
        ParamSpec('console_gate_stat',             'choice', GATE_STATS_STRING,             section="CONSOLE", choices={o: o for o in GATE_STATS_TYPES}),
        ParamSpec('console_always_output_full',    'int',      1, section="CONSOLE", limits=dict(minval=0, maxval=1)),

        # Turn off splash bling for boring people
        ParamSpec('console_show_colored_text',     'int',      1, section="CONSOLE", limits=dict(minval=0, maxval=1), hidden=True),
        ParamSpec('console_show_filament_color',   'int',      1, section="CONSOLE", limits=dict(minval=0, maxval=1), hidden=True),

        # Build tuples of drying temp / drying time indexed by filament type
        # Stored as a string in config and parsed into a dict in _post_load_fixups()
        ParamSpec('drying_data',                   'str',   "{}", section="DRYING", hidden=True),

        # Currently hidden and testing options
        ParamSpec('test_random_failures',          'int',      0, section="TESTING", limits=dict(minval=0, maxval=1),  hidden=True),
        ParamSpec('test_force_in_print',           'int',      0, section="TESTING", limits=dict(minval=0, maxval=1),  hidden=True),

        # Klipper tuning (aka hacks) -------
        ParamSpec('suppress_kalico_warning',       'int',      0, section="KLIPPER TUNING", limits=dict(minval=0, maxval=1),  hidden=True),
        ParamSpec('update_trsync',                 'int',      0, section="KLIPPER TUNING", limits=dict(minval=0, maxval=1),  hidden=True),
        ParamSpec('canbus_comms_retries',          'int',      3, section="KLIPPER TUNING", limits=dict(minval=1, maxval=10), hidden=True),
        ParamSpec('update_bit_max_time',           'int',      0, section="KLIPPER TUNING", limits=dict(minval=0, maxval=1),  hidden=True),
        ParamSpec('update_aht10_commands',         'int',      0, section="KLIPPER TUNING", limits=dict(minval=0, maxval=1),  hidden=True),
    )


    def __init__(self, config, mmu_machine):
        self._mmu_machine = mmu_machine
        super().__init__(config)


    def _post_load_fixups(self):
        """
        Run once after initial loading to make final changes to instance variables
        """

        # Make sure we have no extra quoting
        self.form_tip_macro          = (self.form_tip_macro or '').replace("'", "")
        self.purge_macro             = (self.purge_macro or '').replace("'", "")
        self.pre_unload_macro        = (self.pre_unload_macro or '').replace("'", "")
        self.post_form_tip_macro     = (self.post_form_tip_macro or '').replace("'", "")
        self.post_unload_macro       = (self.post_unload_macro or '').replace("'", "")
        self.pre_load_macro          = (self.pre_load_macro or '').replace("'", "")
        self.post_load_macro         = (self.post_load_macro or '').replace("'", "")
        self.unload_sequence_macro   = (self.unload_sequence_macro or '').replace("'", "")
        self.load_sequence_macro     = (self.load_sequence_macro or '').replace("'", "")

        # Ensure list-ish fields are NEW lists
        self.default_ttg_map              = list(self.default_ttg_map)
        self.default_gate_status          = list(self.default_gate_status)
        self.default_gate_filament_name   = list(self.default_gate_filament_name)
        self.default_gate_material        = list(self.default_gate_material)
        self.default_gate_color           = list(self.default_gate_color)
        self.default_gate_temperature     = list(self.default_gate_temperature)
        self.default_gate_spool_id        = list(self.default_gate_spool_id)
        self.default_gate_speed_override  = list(self.default_gate_speed_override)
        self.default_endless_spool_groups = list(self.default_endless_spool_groups)

        self.endless_spool_groups         = list(self.endless_spool_groups)
        self.console_stat_columns         = list(self.console_stat_columns)
        self.console_stat_rows            = list(self.console_stat_rows)

        # Derived default for macro_toolhead_max_accel
        if self.macro_toolhead_max_accel == 0:
            self.macro_toolhead_max_accel = self._config.getsection('printer').getsection('toolhead').getint('max_accel', 5000)

        # endless_spool_eject_gate maxval depends on gate count
        max_gate = self._mmu_machine.num_gates - 1
        if self.endless_spool_eject_gate < -1:
            self.endless_spool_eject_gate = -1
        elif self.endless_spool_eject_gate > max_gate:
            self.endless_spool_eject_gate = max_gate

        # Parse drying_data (string) into dict with upper-case keys (exact behavior)
        drying_data_str = self.drying_data
        try:
            drying_data = ast.literal_eval(drying_data_str) if drying_data_str is not None else {}
            self.drying_data = dict((str(k).upper(), v) for k, v in drying_data.items())
        except Exception as e:
            raise self._config.error("Unparsable 'drying_data' parameter: %s" % str(e))
