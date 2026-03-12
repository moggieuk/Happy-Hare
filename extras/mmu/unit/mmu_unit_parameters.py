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
        #return lambda self: self._mmu_unit.has_sensor(sensor)
        return lambda self: self._mmu_unit.has_espooler() # PAUL temp


    # ---- On-change hooks ----
    def _on_flowguard_enabled(self, old, new):
        if new != old:
            self._mmu_unit.sync_feedback.config_flowguard_feature(new)

    def _on_encoder_mode(self, old, new):
        if new != old:
            self._mmu_unit.encoder.set_encoder_mode(new)

    def _on_gate_homing_endstop(self, old, new):
        if new != old:
            self._mmu_unit.calibrator.adjust_bowden_lengths_on_homing_change()


    # ---- Specs ----
    _SPECS: Sequence[ParamSpec] = (
        # Gate
        ParamSpec('gate_homing_endstop',              'choice', SENSOR_ENCODER, section="GATE HOMING", choices={o: o for o in GATE_ENDSTOPS}, on_change=_on_gate_homing_endstop),
        ParamSpec('gate_homing_max',                  'float', 100.0, section="GATE HOMING", limits=dict(minval=10)),
        ParamSpec('gate_parking_distance',            'float',  23.0, section="GATE HOMING"),

        ParamSpec('gate_preload_endstop',             'choice',   '', section="GATE HOMING", choices={o: o for o in (GATE_ENDSTOPS + [''])}),
        ParamSpec('gate_preload_homing_max',          'float', lambda self: self.gate_homing_max, section="GATE"),
        ParamSpec('gate_preload_parking_distance',    'float', -10.0, section="GATE HOMING"),
        ParamSpec('gate_preload_attempts',            'int',       1, section="GATE HOMING", limits=dict(minval=1, maxval=20)),
        ParamSpec('gate_endstop_to_encoder',          'float',   0.0, section="GATE HOMING", guard=_guard_encoder_offset, limits=dict(minval=0.0)),
        ParamSpec('gate_load_retries',                'int',       1, section="GATE HOMING", limits=dict(minval=1, maxval=5)),
        ParamSpec('gate_autoload',                    'int',       1, section="GATE HOMING", limits=dict(minval=0, maxval=1)),
        ParamSpec('gate_final_eject_distance',        'float',   0.0, section="GATE HOMING"),

        # Bowden
        ParamSpec('bowden_homing_max',                'float',2000.0, section="BOWDEN MOVE", limits=dict(minval=100.0)),
        ParamSpec('bowden_fast_unload_portion',       'float',  95.0, section="BOWDEN MOVE", limits=dict(minval=50, maxval=100)),
        ParamSpec('bowden_fast_load_portion',         'float',  95.0, section="BOWDEN MOVE", limits=dict(minval=50, maxval=100)),
        ParamSpec('bowden_apply_correction',          'int',       0, section="BOWDEN MOVE", guard=_guard_has_encoder, limits=dict(minval=0, maxval=1)),
        ParamSpec('bowden_allowable_load_delta',      'float',  10.0, section="BOWDEN MOVE", guard=_guard_has_encoder, limits=dict(minval=1.0)),
        ParamSpec('bowden_pre_unload_test',           'int',       0, section="BOWDEN MOVE", guard=_guard_has_encoder, limits=dict(minval=0, maxval=1)),
        ParamSpec('bowden_pre_unload_error_tolerance','float', 100.0, section="BOWDEN MOVE", limits=dict(minval=0, maxval=100)),

        # Extruder/Toolhead
        ParamSpec('extruder_force_homing',            'int',       0, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),
        ParamSpec('extruder_homing_endstop',          'choice', SENSOR_EXTRUDER_NONE, section="EXTRUDER MOVE", choices={o: o for o in EXTRUDER_ENDSTOPS}),
        ParamSpec('extruder_homing_max',              'float',  50.0, section="EXTRUDER MOVE", limits=dict(above=10.0)),
        ParamSpec('extruder_collision_homing_step',   'int',       3, section="EXTRUDER MOVE", guard=_guard_has_encoder, limits=dict(minval=2, maxval=5)),
        ParamSpec('toolhead_homing_max',              'float',  20.0, section="EXTRUDER MOVE", guard=_guard_has_sensor(SENSOR_TOOLHEAD), limits=dict(minval=0.0)),
        ParamSpec('toolhead_unload_safety_margin',    'float',  10.0, section="EXTRUDER MOVE", limits=dict(minval=0.0)),
        ParamSpec('toolhead_move_error_tolerance',    'float',  60.0, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=100)),
        ParamSpec('toolhead_entry_tension_test',      'int',       1, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),
        ParamSpec('toolhead_post_load_tighten',       'int',      60, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=100)),
        ParamSpec('toolhead_post_load_tension_adjust','int',       1, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),

        # Sync motor control and currents
        ParamSpec('sync_to_extruder',                 'int',       0, section="MOTOR CONTROL", guard=_guard_sync_tunable, limits=dict(minval=0, maxval=1)),
        ParamSpec('sync_form_tip',                    'int',       0, section="MOTOR CONTROL", guard=_guard_sync_tunable, limits=dict(minval=0, maxval=1)),
        ParamSpec('sync_purge',                       'int',       0, section="MOTOR CONTROL", guard=_guard_sync_tunable, limits=dict(minval=0, maxval=1)),
        ParamSpec('extruder_collision_homing_current','int',      50, section="MOTOR CONTROL", limits=dict(minval=10, maxval=100)),
        ParamSpec('sync_gear_current',                'int',      50, section="MOTOR CONTROL", limits=dict(minval=10, maxval=100)),

        # Filament motion
        ParamSpec('gear_from_filament_buffer_speed',  'float', 150.0, section="FILAMENT MOVEMENT SPEEDS", guard=lambda self: self.has_filament_buffer, limits=dict(minval=10.0)),
        ParamSpec('gear_from_filament_buffer_accel',  'float', 400.0, section="FILAMENT MOVEMENT SPEEDS", guard=lambda self: self.has_filament_buffer, limits=dict(minval=10.0)),

        ParamSpec('gear_from_spool_speed',            'float',  60.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_from_spool_accel',            'float', 100.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_unload_speed',                'float', lambda self: self.gear_from_spool_speed, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_unload_accel',                'float', lambda self: self.gear_from_spool_accel, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_short_move_speed',            'float',  60.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_short_move_accel',            'float', 400.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0)),
        ParamSpec('gear_short_move_threshold',        'float', lambda self: self.gate_homing_max, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_homing_speed',                'float', 150.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=1.0)),
        ParamSpec('gear_buzz_accel',                  'float',1000.0, section="FILAMENT MOVEMENT SPEEDS", limits=dict(minval=10.0), hidden=True),

        # eSpooler
        ParamSpec('espooler_min_distance',            'float',  50.0, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(above=0.0)),
        ParamSpec('espooler_max_stepper_speed',       'float', 300.0, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(above=0.0)),
        ParamSpec('espooler_min_stepper_speed',       'float',   0.0, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0.0, below=lambda self: self.espooler_max_stepper_speed)),
        ParamSpec('espooler_speed_exponent',          'float',   0.5, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(above=0.0)),
        ParamSpec('espooler_assist_reduced_speed',    'int',      50, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0, maxval=100)),
        ParamSpec('espooler_printing_power',          'int',       0, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0, maxval=100)),
        ParamSpec('espooler_assist_extruder_move_length','float',100.0, section="ESPOOLER", guard=_guard_has_espooler, limits=dict(above=10.0)),
        ParamSpec('espooler_assist_burst_power',      'int',      50, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0, maxval=100)),
        ParamSpec('espooler_assist_burst_duration' ,  'float',   0.4, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(above=0.0, maxval=10.0)),
        ParamSpec('espooler_assist_burst_trigger',    'int',       0, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0, maxval=1)),
        ParamSpec('espooler_assist_burst_trigger_max','int',       3, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=1)),
        ParamSpec('espooler_rewind_burst_power',      'int',      50, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(minval=0, maxval=100)),
        ParamSpec('espooler_rewind_burst_duration' ,  'float',   0.4, section="ESPOOLER",   guard=_guard_has_espooler, limits=dict(above=0.0, maxval=10.0)),
        ParamSpec('espooler_operations',              'list', ESPOOLER_OPERATIONS, section="ESPOOLER", guard=_guard_has_espooler),

        # Sync-feedback
        ParamSpec('sync_feedback_enabled',            'int',       0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=0, maxval=1), fmt="%d"),
        ParamSpec('sync_feedback_buffer_range',       'float',  10.0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=0.0), fmt="%.1f"),
        ParamSpec('sync_feedback_buffer_maxrange',    'float',  10.0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=0.0), fmt="%.1f"),
        ParamSpec('sync_feedback_speed_multiplier',   'float',   5.0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=1.0, maxval=50), fmt="%.1f"),
        ParamSpec('sync_feedback_boost_multiplier',   'float',   5.0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=1.0, maxval=50), fmt="%.1f"),
        ParamSpec('sync_feedback_extrude_threshold',  'float',   5.0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(above=1.0), fmt="%.1f"),
        ParamSpec('sync_feedback_debug_log',          'int',       0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=0, maxval=1), fmt="%d"),
        ParamSpec('sync_feedback_force_twolevel',     'int',       0, section="SYNC FEEDBACK BUFFER", guard=_guard_has_buffer, limits=dict(minval=0, maxval=1), hidden=True),

        # FlowGuard
        ParamSpec('flowguard_enabled',                'int',       1, section="FLOWGUARD", guard=_guard_has_flowguard, limits=dict(minval=0, maxval=1), on_change=_on_flowguard_enabled, fmt="%d"),
        ParamSpec('flowguard_max_relief',             'float',   8.0, section="FLOWGUARD", guard=_guard_has_buffer, limits=dict(above=1.0), fmt="%.1f"),
        ParamSpec('flowguard_encoder_mode',           'int',       2, section="FLOWGUARD", guard=_guard_has_encoder, limits=dict(minval=0, maxval=2), on_change=_on_encoder_mode, fmt="%d"),
        ParamSpec('flowguard_encoder_max_motion',     'float',  20.0, section="FLOWGUARD", guard=_guard_has_encoder, limits=dict(above=0.0), fmt="%.1f"),

        # Heater
        ParamSpec('heater_max_temp',                  'float',  65.0, section="HEATER",    guard=_guard_has_heater, limits=dict(above=0.0), fmt="%.1f"),
        ParamSpec('heater_default_dry_temp',          'float',  45.0, section="HEATER",    guard=_guard_has_heater, limits=dict(above=0.0), fmt="%.1f"),
        ParamSpec('heater_default_dry_time',          'float', 300.0, section="HEATER",    guard=_guard_has_heater, limits=dict(above=0.0), fmt="%.1f"),
        ParamSpec('heater_default_humidity',          'float',  10.0, section="HEATER",    guard=_guard_has_heater, limits=dict(above=0.0), fmt="%.1f"),
        ParamSpec('heater_vent_macro',                'str',      '', section="HEATER",    guard=_guard_has_heater),
        ParamSpec('heater_vent_interval',             'float',   0.0, section="HEATER",    guard=_guard_has_heater, limits=dict(minval=0.0), fmt="%.1f"),
        ParamSpec('heater_rotate_interval',           'float',   5.0, section="HEATER",    guard=_guard_has_heater, limits=dict(minval=1.0), fmt="%.1f"),

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
        ParamSpec('has_filament_buffer',              'int',       1, section="OPTIONAL", limits=dict(minval=0, maxval=1)),
        ParamSpec('encoder_move_validation',          'int',       1, section="OPTIONAL", limits=dict(minval=0, maxval=1)),
    )


    def __init__(self, mmu_unit, config):
        logging.info("PAUL: init() for MmuUnitParameters")
        self._mmu_unit = mmu_unit
        super().__init__(config)


    def _post_load_fixups(self) -> None:
        # gate_preload_endstop: if blank, inherit gate_homing_endstop
        self.gate_preload_endstop = self.gate_preload_endstop or self.gate_homing_endstop

        # filament_always_gripped: forces sync flags on
        if self._mmu_unit.filament_always_gripped:
            self.sync_to_extruder = 1
            self.sync_form_tip = 1
            self.sync_purge = 1
