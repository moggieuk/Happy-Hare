# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Container for all mmu unit parameters (runtime editable)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging
from typing                import Any, Dict, Sequence

# Happy Hare imports
from ..mmu_constants       import *
from ..mmu_base_parameters import TunableParametersBase, ParamSpec


class MmuUnitParameters(TunableParametersBase):
    """
    MMU-unit specific tunable parameters container.
    """

    # ---- Guards ----

    def _guard_has_encoder(self):
        return self._mmu_unit.has_encoder()

    def _guard_has_filament_buffer(self):
        return self._mmu_unit.has_filament_buffer()

    def _guard_has_buffer(self):
        return self._mmu_unit.has_buffer()

    def _guard_has_espooler(self):
        return self._mmu_unit.has_espooler()

    def _guard_has_heater(self):
        return self._mmu_unit.has_heater()

    def _guard_has_flowguard(self):
        return self._guard_has_encoder or self._guard_has_buffer

    def _guard_sync_tunable(self):
        return not self._mmu_unit.filament_always_gripped

    def _guard_encoder_offset(self):
        return not (self.gate_homing_endstop in [SENSOR_SHARED_EXIT] and self._mmu_unit.has_encoder())

    def _guard_has_sensor(sensor):
        return lambda self: self._mmu_unit.mmu.sensor_manager.has_sensor(sensor)


    # ---- On-change hooks ----

    def _on_flowguard_enabled(self, old, new):
        if new != old:
            self._mmu_unit.sync_feedback.config_flowguard_feature(new)

    def _on_encoder_change(self, old, new):
        if new != old:
            if self._mmu_unit.sync_feedback.flowguard_active:
                # If we are currently active make sure config change gets to encoder immediately
                self._mmu_unit.encoder.enable_flowguard(self._mmu_unit)

    def _on_gate_homing_endstop(self, old, new):
        if new != old:
            self._mmu_unit.calibrator.adjust_bowden_lengths_on_homing_change()

    def _on_flowguard_tuning_change(self, old, new):
        # Push live FlowGuard tuning (relief) to a running controller
        if new != old:
            self._mmu_unit.sync_feedback.apply_flowguard_tuning()

    def _on_sync_feedback_extrude_threshold_change(self, old, new):
        # Apply new extruder sampling threshold immediately if we're active
        if new != old:
            self._mmu_unit.sync_feedback.apply_extrude_threshold()


    # ---- Specs ----

    _SPECS: Sequence[ParamSpec] = (
        # Gate
        ParamSpec('gate_homing_endstop',              'choice', SENSOR_ENCODER, section="GATE HOMING", choices={o: o for o in GATE_ENDSTOPS}, on_change=_on_gate_homing_endstop),
        ParamSpec('gate_homing_max',                  'float', 100.0, section="GATE HOMING", limits=dict(minval=10)),
        ParamSpec('gate_parking_distance',            'float',  23.0, section="GATE HOMING"),

        ParamSpec('gate_preload_endstop',             'choice',   '', section="GATE HOMING", choices={o: o for o in (GATE_ENDSTOPS + [''])}),
        ParamSpec('gate_preload_homing_max',          'float', lambda self: self.gate_homing_max, section="GATE HOMING"),
        ParamSpec('gate_preload_parking_distance',    'float', -10.0, section="GATE HOMING"),
        ParamSpec('gate_preload_attempts',            'int',       1, section="GATE HOMING", limits=dict(minval=1, maxval=20)),
        ParamSpec('gate_endstop_to_encoder',          'float',   0.0, section="GATE HOMING", limits=dict(minval=0.0),           guard=_guard_encoder_offset),
        ParamSpec('gate_load_retries',                'int',       1, section="GATE HOMING", limits=dict(minval=1, maxval=5)),
        ParamSpec('gate_autoload',                    'int',       1, section="GATE HOMING", limits=dict(minval=0, maxval=1)),
        ParamSpec('gate_final_eject_distance',        'float',   0.0, section="GATE HOMING"),

        # Bowden
        ParamSpec('bowden_homing_max',                'float',2000.0, section="BOWDEN MOVE", limits=dict(minval=100.0)),
        ParamSpec('bowden_load_homing_buffer',        'float',  20.0, section="BOWDEN MOVE", limits=dict(minval=0, maxval=500)),
        ParamSpec('bowden_unload_homing_buffer',      'float',  40.0, section="BOWDEN MOVE", limits=dict(minval=0, maxval=500)),
        ParamSpec('bowden_apply_correction',          'int',       0, section="BOWDEN MOVE", limits=dict(minval=0, maxval=1),   guard=_guard_has_encoder),
        ParamSpec('bowden_allowable_encoder_delta',   'float',  20.0, section="BOWDEN MOVE", limits=dict(minval=1.0),           guard=_guard_has_encoder),
        ParamSpec('bowden_pre_unload_test',           'int',       0, section="BOWDEN MOVE", limits=dict(minval=0, maxval=1),   guard=_guard_has_encoder),
        ParamSpec('bowden_pre_unload_error_tolerance','int',     100, section="BOWDEN MOVE", limits=dict(minval=0, maxval=100), guard=_guard_has_encoder),
        ParamSpec('bowden_move_error_tolerance',      'int',      60, section="BOWDEN_MOVE", limits=dict(minval=0, maxval=100), guard=_guard_has_encoder),

        # Extruder/Toolhead
        ParamSpec('extruder_force_homing',            'int',       0, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),
        ParamSpec('extruder_homing_endstop',          'choice', SENSOR_EXTRUDER_NONE, section="EXTRUDER MOVE", choices={o: o for o in EXTRUDER_ENDSTOPS}),
        ParamSpec('extruder_homing_max',              'float',  50.0, section="EXTRUDER MOVE", limits=dict(above=10.0)),
        ParamSpec('extruder_collision_homing_step',   'int',       3, section="EXTRUDER MOVE", limits=dict(minval=2, maxval=5), guard=_guard_has_encoder),
        ParamSpec('toolhead_homing_max',              'float',  20.0, section="EXTRUDER MOVE", limits=dict(minval=0.0),         guard=_guard_has_sensor(SENSOR_TOOLHEAD)),
        ParamSpec('toolhead_unload_safety_margin',    'float',  10.0, section="EXTRUDER MOVE", limits=dict(minval=0.0)),
        ParamSpec('toolhead_move_error_tolerance',    'int',     100, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=100)),
        ParamSpec('toolhead_entry_tension_test',      'int',       1, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),
        ParamSpec('toolhead_post_load_tighten',       'int',      60, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=100)),
        ParamSpec('toolhead_post_load_tension_adjust','int',       1, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),

        # Sync motor control and currents
        ParamSpec('sync_to_extruder',                 'int',       0, section="MOTOR CONTROL", limits=dict(minval=0, maxval=1), guard=_guard_sync_tunable),
        ParamSpec('sync_form_tip',                    'int',       0, section="MOTOR CONTROL", limits=dict(minval=0, maxval=1), guard=_guard_sync_tunable),
        ParamSpec('sync_purge',                       'int',       0, section="MOTOR CONTROL", limits=dict(minval=0, maxval=1), guard=_guard_sync_tunable),
        ParamSpec('extruder_collision_homing_current','int',      50, section="MOTOR CONTROL", limits=dict(minval=10, maxval=100)),
        ParamSpec('sync_gear_current',                'int',      50, section="MOTOR CONTROL", limits=dict(minval=10, maxval=100)),

        # Filament motion
        ParamSpec('gear_load_speed',                  'float', 100.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_load_accel',                  'float', 100.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_from_filament_buffer_speed',  'float', 150.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0), guard=_guard_has_filament_buffer),
        ParamSpec('gear_from_filament_buffer_accel',  'float', 400.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0), guard=_guard_has_filament_buffer),
        ParamSpec('gear_unload_speed',                'float', lambda self: self.gear_load_speed, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_unload_accel',                'float', lambda self: self.gear_load_accel, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_short_move_speed',            'float',  80.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_short_move_accel',            'float', 400.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_short_move_threshold',        'float', lambda self: self.gate_homing_max, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_homing_speed',                'float', 150.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_buzz_accel',                  'float',1000.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0), hidden=True),

        ParamSpec('virtual_sensor_homing_speed',      'float',  15.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0, maxval=40.0), guard=_guard_has_buffer),

        # Encoder
        ParamSpec('encoder_dwell',                    'float',   0.1, section="ENCODER",    limits=dict(minval=0.0, maxval=2.0),  hidden=True),
        ParamSpec('encoder_move_step_size',           'float',  15.0, section="ENCODER",    limits=dict(minval=5.0, maxval=25.0), hidden=True),

        # eSpooler
        ParamSpec('espooler_min_distance',            'float',  50.0, section="ESPOOLER",   limits=dict(above=0.0),              guard=_guard_has_espooler),
        ParamSpec('espooler_max_stepper_speed',       'float', 300.0, section="ESPOOLER",   limits=dict(above=0.0),              guard=_guard_has_espooler),
        ParamSpec('espooler_min_stepper_speed',       'float',   0.0, section="ESPOOLER",   limits=dict(minval=0.0, below=lambda self: self.espooler_max_stepper_speed), guard=_guard_has_espooler),
        ParamSpec('espooler_speed_exponent',          'float',   0.5, section="ESPOOLER",   limits=dict(above=0.0),              guard=_guard_has_espooler),
        ParamSpec('espooler_assist_reduced_speed',    'int',      50, section="ESPOOLER",   limits=dict(minval=0, maxval=100),   guard=_guard_has_espooler),
        ParamSpec('espooler_printing_power',          'int',       0, section="ESPOOLER",   limits=dict(minval=0, maxval=100),   guard=_guard_has_espooler),
        ParamSpec('espooler_assist_extruder_move_length','float',100.0, section="ESPOOLER", limits=dict(above=10.0),             guard=_guard_has_espooler),
        ParamSpec('espooler_assist_burst_power',      'int',      50, section="ESPOOLER",   limits=dict(minval=0, maxval=100),   guard=_guard_has_espooler),
        ParamSpec('espooler_assist_burst_duration' ,  'float',   0.4, section="ESPOOLER",   limits=dict(above=0.0, maxval=10.0), guard=_guard_has_espooler),
        ParamSpec('espooler_assist_burst_trigger',    'int',       0, section="ESPOOLER",   limits=dict(minval=0, maxval=1),     guard=_guard_has_espooler),
        ParamSpec('espooler_assist_burst_trigger_max','int',       3, section="ESPOOLER",   limits=dict(minval=1),               guard=_guard_has_espooler),
        ParamSpec('espooler_rewind_burst_power',      'int',      50, section="ESPOOLER",   limits=dict(minval=0, maxval=100),   guard=_guard_has_espooler),
        ParamSpec('espooler_rewind_burst_duration' ,  'float',   0.4, section="ESPOOLER",   limits=dict(above=0.0, maxval=10.0), guard=_guard_has_espooler),
        ParamSpec('espooler_operations',              'list', ESPOOLER_OPERATIONS, section="ESPOOLER",                           guard=_guard_has_espooler),

        # Sync-feedback
        ParamSpec('sync_feedback_enabled',            'int',       0, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0, maxval=1),    guard=_guard_has_buffer, fmt="%d"),
        ParamSpec('sync_feedback_speed_multiplier',   'float',   5.0, section="SYNC FEEDBACK BUFFER", limits=dict(minval=1.0, maxval=50), guard=_guard_has_buffer, fmt="%.1f"),
        ParamSpec('sync_feedback_boost_multiplier',   'float',   5.0, section="SYNC FEEDBACK BUFFER", limits=dict(minval=1.0, maxval=50), guard=_guard_has_buffer, fmt="%.1f"),
        ParamSpec('sync_feedback_extrude_threshold',  'float',   5.0, section="SYNC FEEDBACK BUFFER", limits=dict(above=1.0),             guard=_guard_has_buffer, on_change=_on_sync_feedback_extrude_threshold_change, fmt="%.1f"),
        ParamSpec('sync_feedback_debug_log',          'int',       0, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0, maxval=1),    guard=_guard_has_buffer, fmt="%d"),
        ParamSpec('sync_feedback_force_twolevel',     'int',       0, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0, maxval=1),    guard=_guard_has_buffer, hidden=True),

        # Tangle prevention - gear current boost on high tension (spool resistance)
        ParamSpec('tangle_prevention_enabled',        'int',       1, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0, maxval=1),       guard=_guard_has_buffer, fmt="%d"),
        ParamSpec('tangle_prevention_threshold',      'float',   0.3, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0.2, maxval=0.9),   guard=_guard_has_buffer, fmt="%.2f"),
        ParamSpec('tangle_prevention_release',        'float',   0.2, section="SYNC FEEDBACK BUFFER", limits=dict(minval=0.15, maxval=0.8),  guard=_guard_has_buffer, fmt="%.2f"),

        # FlowGuard
        ParamSpec('flowguard_enabled',                'int',       1, section="FLOWGUARD", limits=dict(minval=0, maxval=1), on_change=_on_flowguard_enabled, fmt="%d"),
        ParamSpec('flowguard_max_relief',             'float',   8.0, section="FLOWGUARD", limits=dict(above=1.0),          guard=_guard_has_buffer, on_change=_on_flowguard_tuning_change, fmt="%.1f"),
        ParamSpec('flowguard_encoder_mode',           'int',       2, section="FLOWGUARD", limits=dict(minval=0, maxval=2), guard=_guard_has_encoder, on_change=_on_encoder_change, fmt="%d"),
        ParamSpec('flowguard_encoder_max_motion',     'float',  20.0, section="FLOWGUARD", limits=dict(above=0.0),          guard=_guard_has_encoder, on_change=_on_encoder_change, fmt="%.1f"),

        # Heater
        ParamSpec('heater_max_temp',                  'float',  65.0, section="HEATER",    limits=dict(above=0.0),  guard=_guard_has_heater, fmt="%.1f"),
        ParamSpec('heater_default_dry_temp',          'float',  45.0, section="HEATER",    limits=dict(above=0.0),  guard=_guard_has_heater, fmt="%.1f"),
        ParamSpec('heater_default_dry_time',          'float', 300.0, section="HEATER",    limits=dict(above=0.0),  guard=_guard_has_heater, fmt="%.1f"),
        ParamSpec('heater_default_dry_humidity',      'float',  25.0, section="HEATER",    limits=dict(above=0.0),  guard=_guard_has_heater, fmt="%.1f"),
        ParamSpec('heater_vent_macro',                'str',      '', section="HEATER",                             guard=_guard_has_heater),
        ParamSpec('heater_vent_interval',             'float',   0.0, section="HEATER",    limits=dict(minval=0.0), guard=_guard_has_heater, fmt="%.1f"),
        ParamSpec('heater_rotate_interval',           'float',   5.0, section="HEATER",    limits=dict(minval=1.0), guard=_guard_has_heater, fmt="%.1f"),

        # NFC reader
        # Spoolman lookups are gated on Happy Hare's own spoolman_support
        # ([mmu] section) -- there is no separate NFC-level enable switch.
        ParamSpec('moonraker_url',                      'str', 'http://127.0.0.1:7125', section="NFC READER"),
        ParamSpec('spoolman_rfid_key',                   'str', 'rfid_tag', section="NFC READER"),
        ParamSpec('spoolman_timeout',               'float',     5.0, section="NFC READER", limits=dict(minval=0.5, maxval=30.0)),
        ParamSpec('spoolman_cache_ttl',             'float',   300.0, section="NFC READER", limits=dict(minval=0.0, maxval=3600.0)),
        ParamSpec('tag_parsing',                      'bool',   False, section="NFC READER"),
        ParamSpec('bambu_reads',                      'bool',   False, section="NFC READER"),
        ParamSpec('spoolman_auto_create',             'bool',   False, section="NFC READER"),
        ParamSpec('tag_max_pages',                     'int',      16, section="NFC READER", limits=dict(minval=4, maxval=135)),
        ParamSpec('startup_polling',                   'int',      -1, section="NFC READER", limits=dict(minval=-1, maxval=1)),
        ParamSpec('startup_poll_delay',              'float',     0.0, section="NFC READER", limits=dict(minval=0.0, maxval=3600.0)),
        ParamSpec('poll_interval',                   'float',    10.0, section="NFC READER", limits=dict(minval=1.0, maxval=3600.0)),
        ParamSpec('absent_threshold',                  'int',       3, section="NFC READER", limits=dict(minval=1, maxval=255)),
        ParamSpec('transceive_delay',                'float',    0.25, section="NFC READER", limits=dict(minval=0.05, maxval=2.0)),
        ParamSpec('crc_delay',                       'float',    0.05, section="NFC READER", limits=dict(minval=0.005, maxval=1.0)),
        ParamSpec('log_file',                          'str', 'nfc_reader.log', section="NFC READER"),
        ParamSpec('debug',                              'int',       2, section="NFC READER", limits=dict(minval=0, maxval=4)),
        ParamSpec('console_output',                   'bool',   False, section="NFC READER"),
        ParamSpec('console_log_level',                 'int',       2, section="NFC READER", limits=dict(minval=1, maxval=4)),
        ParamSpec('low_level_debug',                  'bool',   False, section="NFC READER"),
        ParamSpec('scan_enabled',                     'bool',   False, section="NFC READER"),
        ParamSpec('scan_jog_mm',                     'float',   150.0, section="NFC READER", limits=dict(minval=1.0, maxval=500.0)),
        ParamSpec('scan_jog_max',                      'str',      '', section="NFC READER"),
        ParamSpec('scan_rewind_buffer_mm',           'float',    30.0, section="NFC READER", limits=dict(minval=0.0, maxval=500.0)),
        ParamSpec('scan_decode_retry_mm',            'float',     5.0, section="NFC READER", limits=dict(minval=0.0, maxval=50.0)),
        ParamSpec('scan_decode_retry_rounds',          'int',       5, section="NFC READER", limits=dict(minval=0, maxval=10)),
        ParamSpec('scan_reads_per_position',           'int',       1, section="NFC READER", limits=dict(minval=1, maxval=20)),
        ParamSpec('scan_poll_interval',              'float',    0.25, section="NFC READER", limits=dict(minval=0.01, maxval=5.0)),
        ParamSpec('scan_motion_mode',               'choice', 'continuous', section="NFC READER", choices={o: o for o in ('continuous', 'stopped')}),
        ParamSpec('scan_continuous_step_mm',         'float',   150.0, section="NFC READER", limits=dict(minval=1.0, maxval=500.0)),
        ParamSpec('scan_continuous_speed',           'float',   200.0, section="NFC READER", limits=dict(minval=1.0, maxval=500.0)),
        ParamSpec('scan_continuous_accel',           'float',  2000.0, section="NFC READER", limits=dict(minval=1.0, maxval=10000.0)),
        ParamSpec('scan_continuous_poll_interval',   'float',    0.03, section="NFC READER", limits=dict(minval=0.01, maxval=5.0)),
        ParamSpec('shared_bypass_enabled',            'bool',    True, section="NFC READER"),
        ParamSpec('shared_led_segment',             'choice',  'exit', section="NFC READER", choices={o: o for o in ('exit', 'entry', 'status', 'gate')}),
        ParamSpec('shared_missed_limit',               'int',       3, section="NFC READER", limits=dict(minval=1)),
        ParamSpec('force_spool_id',                   'bool',    True, section="NFC READER"),

        # Automatic calibration / tuning options
        ParamSpec('autocal_selector',                 'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('skip_cal_rotation_distance',       'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('autotune_rotation_distance',       'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('autocal_bowden_length',            'int',       1, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('autotune_bowden_length',           'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('skip_cal_encoder',                 'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1)),
        ParamSpec('autotune_encoder',                 'int',       0, section="AUTOTUNE", limits=dict(minval=0, maxval=1), hidden=True),

        # Optional
        ParamSpec('startup_home_selector',            'int',       0, section="OPTIONAL", limits=dict(minval=0, maxval=1)),
        ParamSpec('encoder_move_validation',          'int',       1, section="OPTIONAL", limits=dict(minval=0, maxval=1)),
    )


    def __init__(self, config, mmu_unit):
        self._mmu_unit = mmu_unit
        super().__init__(config)


    def _post_load_fixups(self):
        # gate_preload_endstop: if blank, inherit gate_homing_endstop
        self.gate_preload_endstop = self.gate_preload_endstop or self.gate_homing_endstop

        # filament_always_gripped: forces sync flags on
        if self._mmu_unit.filament_always_gripped:
            self.sync_to_extruder = 1
            self.sync_form_tip = 1
            self.sync_purge = 1
