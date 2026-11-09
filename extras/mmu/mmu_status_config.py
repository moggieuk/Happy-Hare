# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Effective geometry and object references for subscription-based clients.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
UNIT_PARAMETERS = ('gate_homing_endstop', 'extruder_homing_endstop', 'gate_endstop_to_encoder',
                   'gate_parking_distance', 'gate_endstop_to_extruder')
TOOLHEAD_PARAMETERS = ('toolhead_entry_to_extruder', 'toolhead_extruder_to_nozzle',
                       'toolhead_sensor_to_nozzle', 'toolhead_residual_filament', 'toolhead_ooze_reduction')


def select(obj, names):
    return {name: getattr(obj, name) for name in names if hasattr(obj, name)}


def configuration(mmu):
    manager = mmu.sensor_manager
    sensors = manager.get_sensor_states(all_sensors=True)
    units = {}
    for unit in mmu.mmu_machine.units:
        members = {id(sensor) for _, sensor in manager.get_sensor_states(unit=unit.unit_index).values()}
        buffer = unit.buffer
        units[unit.name] = dict(
            gates=list(unit.gate_range()), selector_type=unit.selector_type,
            filament_always_gripped=bool(unit.filament_always_gripped),
            has_per_gate_heaters=any(unit.filament_heaters),
            heaters=list(unit.filament_heaters) if any(unit.filament_heaters) else [unit.filament_heater],
            parameters=select(unit.p, UNIT_PARAMETERS),
            toolhead=dict(name=unit.toolhead_wrapper.name, parameters=select(unit.toolhead_wrapper.p, TOOLHEAD_PARAMETERS)),
            extruder=dict(name=unit.extruder_wrapper.name),
            bowden_lengths={str(g): unit.calibrator.get_bowden_length(g) for g in unit.gate_range()},
            sensors=[name for name, (_, sensor) in sensors.items() if id(sensor) in members],
            encoder=None if unit.encoder is None else dict(name=unit.encoder.name),
            buffer=None if buffer is None else dict(name=buffer.name,
                proportional=buffer.proportional_sensor is not None,
                **select(buffer, ('buffer_range', 'buffer_maxrange', 'buffer_spring_state'))))
    return dict(num_gates=mmu.num_gates, units=units,
                form_tip_macro=mmu._macro_name(mmu.p.form_tip_macro),
                purge_macro=mmu._macro_name(mmu.p.purge_macro))
