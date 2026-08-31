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
            # gate_parking_distance's legal sign depends on this endstop (see
            # _validate_gate_parking_distance) - a value that was fine for the old endstop
            # can be unsafe for the new one (e.g. a positive park, fine on mmu_exit, driving
            # forward into a now-shared merge zone on encoder/mmu_shared_exit/extruder_entry).
            # Re-check it now rather than leaving a stale, now-unsafe value in place until
            # something else happens to touch it. Same for gate_preload_parking_distance
            # when gate_preload_endstop is '' and so inherits this one.
            self._validate_gate_parking_distance(self.gate_parking_distance)
            if not self.gate_preload_endstop:
                self._validate_gate_preload_parking_distance(self.gate_preload_parking_distance)

    def _on_gate_preload_endstop(self, old, new):
        if new != old:
            self._validate_gate_preload_parking_distance(self.gate_preload_parking_distance)

    def _on_flowguard_tuning_change(self, old, new):
        # Push live FlowGuard tuning (relief) to a running controller
        if new != old:
            self._mmu_unit.sync_feedback.apply_flowguard_tuning()

    def _on_sync_feedback_extrude_threshold_change(self, old, new):
        # Apply new extruder sampling threshold immediately if we're active
        if new != old:
            self._mmu_unit.sync_feedback.apply_extrude_threshold()


    # ---- Validators ----

    def _validate_nfc_gate_jog_scan_window(self, value):
        # Empty (or absent) disables MMU_NFC_SCAN
        if not value:
            return
        if len(value) != 2:
            raise ValueError("nfc_gate_jog_scan_window must be two values (neg, pos), e.g. 0,480")
        neg, pos = value[0], value[1]
        if neg > 0 or pos < 0:
            raise ValueError("nfc_gate_jog_scan_window must be (neg, pos) with neg <= 0 <= pos")
        # Both values are TARGETS measured from the gate homing point, not from the parked
        # position: _jog_scan homes to the gate for a datum first, then sweeps to gate+neg
        # and gate+pos.
        #
        # The return leg no longer constrains this - _load_gate() takes an extra_homing
        # budget now, so it scales with however far the sweep strayed. What is still worth
        # rejecting is a backward reach beyond the machine's own gate homing budget, which
        # means pulling filament further back than gate homing was ever configured to
        # recover. (The positive side is deliberately left unchecked so no configuration
        # that loads today stops loading; see the note in the plan.)
        if abs(neg) > self.gate_homing_max:
            raise ValueError(
                "nfc_gate_jog_scan_window backward reach (%.1fmm) cannot exceed gate_homing_max (%.1fmm)"
                % (abs(neg), self.gate_homing_max)
            )
        # Note: a forward reach past a SHARED gate datum is deliberately not rejected here.
        # Whether that is safe depends on the shared path being unoccupied, which is a
        # runtime property this validator cannot see. Nothing checks it at scan time
        # either - a sweep forward of a shared datum can meet another gate's filament.

    def _validate_nfc_preload_jog_scan_window(self, value):
        # Empty (or absent) disables the NFC scan-on-miss during MMU_PRELOAD
        if not value:
            return
        if len(value) != 2:
            raise ValueError("nfc_preload_jog_scan_window must be two values (neg, pos), e.g. 0,480")
        neg, pos = value[0], value[1]
        if neg > 0 or pos < 0:
            raise ValueError("nfc_preload_jog_scan_window must be (neg, pos) with neg <= 0 <= pos")
        # Same TARGETS-from-the-gate-datum semantics as nfc_gate_jog_scan_window, but
        # checked against the preload homing budget - preload can home to a different
        # endstop (e.g. the per-gate mmu_exit sensor) with its own recovery budget.
        if abs(neg) > self.gate_preload_homing_max:
            raise ValueError(
                "nfc_preload_jog_scan_window backward reach (%.1fmm) cannot exceed gate_preload_homing_max (%.1fmm)"
                % (abs(neg), self.gate_preload_homing_max)
            )

    # Parking distance sign convention: -ve = retraction (toward the gate/gears), +ve =
    # extrusion (forward, past the sensor). Parking forward past the sensor is only safe
    # on the per-gate mmu_exit sensor; the shared mmu_shared_exit, encoder and
    # extruder_entry endstops must park with a retraction.

    def _validate_gate_parking_distance(self, value):
        if value > 0 and self.gate_homing_endstop != SENSOR_EXIT_PREFIX:
            raise ValueError(
                "gate_parking_distance must be a retraction (<= 0) unless gate_homing_endstop "
                "is '%s' (got %.1f with endstop '%s')"
                % (SENSOR_EXIT_PREFIX, value, self.gate_homing_endstop))

    def _validate_gate_preload_parking_distance(self, value):
        endstop = self.gate_preload_endstop or self.gate_homing_endstop # '' inherits gate_homing_endstop
        if value > 0 and endstop != SENSOR_EXIT_PREFIX:
            raise ValueError(
                "gate_preload_parking_distance must be a retraction (<= 0) unless the preload "
                "endstop is '%s' (got %.1f with endstop '%s')"
                % (SENSOR_EXIT_PREFIX, value, endstop))


    # ---- Specs ----

    _SPECS: Sequence[ParamSpec] = (
        # Gate loading
        ParamSpec('gate_homing_endstop',              'choice', SENSOR_ENCODER, section="GATE HOMING", choices={o: o for o in GATE_ENDSTOPS}, on_change=_on_gate_homing_endstop),
        ParamSpec('gate_homing_max',                  'float', 100.0, section="GATE HOMING", limits=dict(minval=10)),
        ParamSpec('gate_parking_distance',            'float', -10.0, section="GATE HOMING", validator=_validate_gate_parking_distance),
        ParamSpec('gate_load_attempts',               'int',       1, section="GATE HOMING", limits=dict(minval=1, maxval=20)),

        # Gate preloading
        ParamSpec('gate_preload_endstop',             'choice',   '', section="GATE HOMING", choices={o: o for o in (GATE_ENDSTOPS + [''])}, on_change=_on_gate_preload_endstop),
        ParamSpec('gate_preload_homing_max',          'float', lambda self: self.gate_homing_max, section="GATE HOMING"),
        ParamSpec('gate_preload_parking_distance',    'float', -10.0, section="GATE HOMING", validator=_validate_gate_preload_parking_distance),
        ParamSpec('gate_preload_attempts',            'int',       2, section="GATE HOMING", limits=dict(minval=1, maxval=20)),
        ParamSpec('gate_autoload',                    'int',       1, section="GATE HOMING", limits=dict(minval=0, maxval=1)),

        ParamSpec('gate_endstop_to_encoder',          'float',   0.0, section="GATE HOMING", limits=dict(minval=0.0),           guard=_guard_encoder_offset),
        ParamSpec('gate_final_eject_distance',        'float',   0.0, section="GATE HOMING"),

        # NFC / RFID reading
        ParamSpec('nfc_gate_jog_scan_window',         'floatlist', [0.0, 0.0], section="NFC", validator=_validate_nfc_gate_jog_scan_window),
        ParamSpec('nfc_preload_jog_scan_window',      'floatlist', lambda self: self.nfc_gate_jog_scan_window, section="NFC", validator=_validate_nfc_preload_jog_scan_window),
        ParamSpec('nfc_deep_read',                    'int',    0,    section="NFC", limits=dict(minval=0, maxval=1)),
        ParamSpec('nfc_led_segment',                  'str',  'auto', section="NFC"),

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
        ParamSpec('extruder_homing_sync',             'int',       0, section="EXTRUDER MOVE", limits=dict(minval=0, maxval=1)),
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
