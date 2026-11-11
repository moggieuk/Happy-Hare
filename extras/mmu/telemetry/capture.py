# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Opt-in, bounded capture of source events and the state needed to replay them.
#
# Capture runs on the printer thread. Readers receive detached copies. No printer
# objects, simulator coordinates or per-stepper samples enter the recording.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import collections
import itertools
import copy
import math
import threading


def plain(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def changes(previous, current, path=()):
    """Leaf replacements with explicit removals; None remains a valid value."""
    result = []
    for key in sorted(previous.keys() | current.keys()):
        child = (*path, key)
        if key not in current:
            result.append(dict(path=list(child), remove=True))
        elif key not in previous:
            result.append(dict(path=list(child), value=current[key]))
        elif isinstance(previous[key], dict) and isinstance(current[key], dict):
            result.extend(changes(previous[key], current[key], child))
        elif previous[key] != current[key]:
            result.append(dict(path=list(child), value=current[key]))
    return result


def apply_changes(state, delta):
    for change in delta:
        target = state
        for key in change['path'][:-1]:
            target = target.setdefault(key, {})
        key = change['path'][-1]
        if change.get('remove'):
            target.pop(key, None)
        else:
            target[key] = copy.deepcopy(change['value'])


def merge(target, source):
    """Deep-merge source into target in place."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


class ReplayState:
    """
    Read a small allowlist directly, avoiding the broad mmu.get_status scan.
    Tunable parameter sets are read once by static(); runtime edits arrive as config_changed events.
    """
    MACRO_VARIABLES = ('blade_pos', 'cutter_before_extruder',
                       'cooling_tube_position', 'cooling_tube_length')

    def __init__(self, mmu):
        self.mmu = mmu

    @staticmethod
    def select(obj, names):
        return {name: getattr(obj, name) for name in names if hasattr(obj, name)}

    @staticmethod
    def values(*sets):
        return {spec.name: getattr(params, spec.name) for params in sets
                for spec in params._SPECS if hasattr(params, spec.name)}

    def parameter_sets(self):
        """(params, state path) for every tunable set; the set's config section names its events."""
        mmu = self.mmu
        machine = ('printer', 'mmu_machine')
        sets = [(mmu.p, machine + ('parameters',))]
        for unit in mmu.mmu_machine.units:
            unit_path = machine + ('units', unit.name)
            sets.append((unit.p, unit_path + ('parameters',)))
            sets.append((unit.selector.p, unit_path + ('parameters',)))
            sets.append((unit.toolhead_wrapper.p, unit_path + ('toolhead', 'parameters')))
        return sets

    def scopes(self):
        """Config section name -> state paths holding that section's parameters."""
        scopes = {}
        for params, path in self.parameter_sets():
            if params.scope():
                scopes.setdefault(params.scope(), []).append(list(path))
        return scopes

    def static(self):
        """Tunable parameter sets, read once when capture starts."""
        state = {}
        grouped = {}
        for params, path in self.parameter_sets():
            grouped.setdefault(path, []).append(params)
        for path, sets in grouped.items():
            target = state
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = self.values(*sets)
        return plain(state)

    def __call__(self):
        from ..mmu_utils import MmuColorUtils
        mmu = self.mmu
        now = mmu.reactor.monotonic()
        manager = mmu.sensor_manager
        sensors = manager.get_sensor_states(all_sensors=True)
        active = {id(sensor) for _, sensor in manager.get_sensor_states().values()}
        units = {}
        for unit in mmu.mmu_machine.units:
            encoder = unit.encoder
            buffer = unit.buffer
            unit_sensors = {id(sensor) for _, sensor in
                            manager.get_sensor_states(unit=unit.unit_index).values()}
            units[unit.name] = dict(
                gates=list(unit.gate_range()), selector_type=unit.selector_type,
                has_per_gate_heaters=any(unit.filament_heaters),
                filament_always_gripped=bool(unit.filament_always_gripped),
                toolhead=dict(name=unit.toolhead_wrapper.name),
                bowden_lengths={str(g): unit.calibrator.get_bowden_length(g) for g in unit.gate_range()},
                sensors=[name for name, (_, sensor) in sensors.items() if id(sensor) in unit_sensors],
                extruder=dict(name=unit.extruder_wrapper.name,
                              retracted_length=unit.extruder_wrapper.retracted_length,
                              **unit.extruder_wrapper.get_status(now)),
                encoder=None if encoder is None else dict(name=encoder.name, distance=encoder.get_distance(), status=encoder.get_status(now),
                    parameters=self.select(encoder, ('resolution', 'desired_headroom',
                        'average_samples', 'calibration_length', 'no_movement_samples', 'flowrate_samples'))),
                buffer=None if buffer is None else dict(
                    name=buffer.name, **self.select(buffer, ('buffer_range', 'buffer_maxrange', 'buffer_spring_state')),
                    proportional=buffer.proportional_sensor is not None,
                    value=None if buffer.proportional_sensor is None else buffer.proportional_sensor.get_status(now)['value']))
        for unit in mmu.mmu_machine.units:
            spooler = unit.espooler
            if spooler is None:
                continue
            operations = {}
            for gate in unit.gate_range():
                direction, power = spooler.get_operation(gate)
                output = max(0, min(1, power)) / spooler.scale if direction in ('assist', 'rewind') else 0
                if not spooler.is_pwm:
                    output = float(output > 0)
                operations[str(gate)] = dict(direction=direction if output else 'stopped', output_power=output)
            units[unit.name]['espooler'] = dict(name=spooler.name, operations=operations)
        for unit in units.values():
            extruder = unit['extruder']
            extruder['filament_remaining_color'] = MmuColorUtils.color_to_rgb_hex(extruder['filament_remaining_color'], '808182')[:6]
        macro_name = mmu._macro_name(mmu.p.form_tip_macro)
        macros = {}
        for name in (macro_name, macro_name + '_VARS'):
            obj = mmu.printer.lookup_object('gcode_macro ' + name, None)
            if obj is not None:
                macros[name] = {key: obj.variables[key] for key in self.MACRO_VARIABLES if key in obj.variables}
        drive = mmu.drive()
        return plain(dict(printer=dict(
            mmu=dict(unit=mmu.mmu_unit().name, tool=mmu.tool_selected, gate=mmu.gate_selected,
                     filament_pos=mmu.filament_pos, filament_direction=mmu.filament_direction,
                     filament_grip_state=mmu.selector().get_filament_grip_state(),
                     sync_mode=drive.get_sync_mode(), gate_status=list(mmu.gate_status), gate_color=[MmuColorUtils.color_to_rgb_hex(c, '808182')[:6] for c in mmu.gate_color],
                     gate_initial_weight=list(mmu.gate_initial_weight), gate_remaining_weight=list(mmu.gate_remaining_weight),
                     active_sensors=[name for name, (_, sensor) in sensors.items() if id(sensor) in active],
                     sensors={name: dict(triggered=value, enabled=bool(sensor.runout_helper.sensor_enabled))
                              for name, (value, sensor) in sensors.items()},
                     drive_motors={kind: [motor.get_name() for motor in owner.get_steppers()]
                                   for kind, owner in (('gear', drive.mmu_gear_stepper), ('extruder', drive.mmu_extruder_stepper))}),
            mmu_machine=dict(num_gates=mmu.num_gates, units=units, form_tip_macro=macro_name,
                             purge_macro=mmu._macro_name(mmu.p.purge_macro))),
            macros=macros))


class TelemetryCapture:
    """Session baseline plus ordered deltas; unchanged state costs no wire bytes.

Create only when a consumer requests capture, after Klipper is ready, and close
when it leaves. Changes that cancel before the next event are intentionally lost.
A rolling baseline keeps bounded exports usable after older records expire.
"""
    def __init__(self, mmu, limit=512):
        if limit < 2:
            raise ValueError('Telemetry limit must be at least two records')
        self.telemetry = mmu.movement_telemetry
        self.state_provider = ReplayState(mmu)
        self.limit = limit
        self.lock = threading.Lock()
        self.records = collections.deque()
        self.sequence = 0
        self.state = copy.deepcopy(self.state_provider())
        # Parameter sets are read once; later edits are config_changed events routed by section name
        self.scopes = self.state_provider.scopes()
        self.baseline = merge(copy.deepcopy(self.state), self.state_provider.static())
        self.baseline_sequence = 0
        self.baseline_time = mmu.reactor.monotonic()
        self.unsubscribe = self.telemetry.subscribe(self.on_event)

    def close(self):
        self.unsubscribe()

    def _config_delta(self, source):
        value = plain(source['value'])
        return [dict(path=[*path, source['name']], value=value) for path in self.scopes.get(source['scope'], ())]

    def on_event(self, source):
        # Every event, including a configuration change, commits whatever else has changed since the last one
        current = self.state_provider()
        delta = changes(self.state, current)
        record = None
        if source['event'] == 'config_changed':
            delta += self._config_delta(source)
        else:
            record = {key: value for key, value in source.items()
                      if key not in ('schema', 'version', 'session_id', 'sequence')}
        with self.lock:
            if delta:
                self._append(dict(event='state_delta', timestamp=source['timestamp'], changes=delta))
                self.state = copy.deepcopy(current)
            if record:
                self._append(record)

    def _append(self, record):
        self.sequence += 1
        self.records.append(dict(sequence=self.sequence, **copy.deepcopy(record)))
        while len(self.records) > self.limit:
            expired = self.records.popleft()
            if expired['event'] == 'state_delta':
                apply_changes(self.baseline, expired['changes'])
            self.baseline_sequence = expired['sequence']
            self.baseline_time = expired['timestamp']

    def read(self, after=None, limit=None, history="start"):
        with self.lock:
            reset = after is None or after < self.baseline_sequence or after > self.sequence
            records = [dict(schema='happy_hare.motion', version=1, event='session',
                            session_id=self.telemetry.session_id,
                            sequence=self.baseline_sequence, timestamp=self.baseline_time,
                            checkpoint=self.baseline_sequence > 0, state=copy.deepcopy(self.baseline))] if reset else []
            boundary = None
            if reset and history == 'current':
                boundary = self.sequence + 1
            elif reset and history == 'latest':
                boundary = next((r['sequence'] for r in reversed(self.records)
                                 if r['event'] == 'toolchange'), None)
            if boundary is not None:
                for record in self.records:
                    if record['sequence'] >= boundary:
                        break
                    if record['event'] == 'state_delta':
                        apply_changes(records[0]['state'], record['changes'])
                    records[0].update(sequence=record['sequence'], timestamp=record['timestamp'], checkpoint=True)
            remaining = None if limit is None else max(0, limit - len(records))
            if reset or after != self.sequence:
                records.extend(itertools.islice((record for record in self.records
                    if (reset and (boundary is None or record['sequence'] >= boundary))
                    or (not reset and record['sequence'] > after)), remaining))
            response = dict(schema='happy_hare.motion', version=1,
                            session_id=self.telemetry.session_id,
                            reset=reset, records=records)
        # Committed records are replaced, never mutated.
        # Only the rolling baseline needs copying while the producer is locked.
        return copy.deepcopy(response)
