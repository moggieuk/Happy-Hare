# Happy Hare MMU Software
# Copyright (C) 2022-2026 Paul Morgan. GPLv3.
"""Opt-in, bounded capture of source events and the state needed to replay them.

Capture runs on the printer thread. Readers receive detached copies. No printer
objects, simulator coordinates or per-stepper samples enter the recording.
"""
import collections
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


class ReplayState:
    """Read a small allowlist directly, avoiding the broad mmu.get_status scan."""
    UNIT_PARAMETERS = ('gate_homing_endstop', 'extruder_homing_endstop', 'gate_endstop_to_encoder',
                       'gate_parking_distance', 'gate_endstop_to_extruder')
    TOOLHEAD_PARAMETERS = ('toolhead_entry_to_extruder', 'toolhead_extruder_to_nozzle',
                           'toolhead_sensor_to_nozzle', 'toolhead_residual_filament',
                           'toolhead_ooze_reduction')
    MACRO_VARIABLES = ('blade_pos', 'cutter_before_extruder',
                       'cooling_tube_position', 'cooling_tube_length')

    def __init__(self, mmu):
        self.mmu = mmu

    @staticmethod
    def select(obj, names):
        return {name: getattr(obj, name) for name in names if hasattr(obj, name)}

    def __call__(self):
        from .mmu_utils import MmuColorUtils
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
                gates=list(unit.gate_range()),
                filament_always_gripped=bool(unit.filament_always_gripped),
                parameters=self.select(unit.p, self.UNIT_PARAMETERS),
                toolhead=dict(name=unit.toolhead_wrapper.name,
                              parameters=self.select(unit.toolhead_wrapper.p, self.TOOLHEAD_PARAMETERS)),
                bowden_lengths={str(g): unit.calibrator.get_bowden_length(g) for g in unit.gate_range()},
                sensors=[name for name, (_, sensor) in sensors.items() if id(sensor) in unit_sensors],
                extruder=dict(name=unit.extruder_wrapper.name,
                              target_temperature=mmu.printer.lookup_object(unit.extruder_wrapper.name).get_heater().get_temp(now)[1],
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
                     sync_mode=drive.get_sync_mode(), gate_color=[MmuColorUtils.color_to_rgb_hex(c, '808182')[:6] for c in mmu.gate_color],
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
    def __init__(self, mmu, limit=6000, state_provider=None):
        if limit < 2:
            raise ValueError('Telemetry limit must be at least two records')
        self.telemetry = mmu.movement_telemetry
        self.state_provider = state_provider or ReplayState(mmu)
        self.limit = limit
        self.lock = threading.Lock()
        self.records = collections.deque()
        self.pending = None
        self.sequence = 0
        self.state = copy.deepcopy(self.state_provider())
        self.baseline = copy.deepcopy(self.state)
        self.baseline_sequence = 0
        self.baseline_time = mmu.reactor.monotonic()
        self.unsubscribe = self.telemetry.subscribe(self.on_event)

    def close(self):
        self.unsubscribe()

    def on_event(self, source):
        current = self.state_provider()
        delta = changes(self.state, current)
        record = {key: value for key, value in source.items()
                  if key not in ('schema', 'version', 'session_id', 'sequence')}
        with self.lock:
            self.pending = None
            if delta:
                self._append(dict(event='state_delta', timestamp=source['timestamp'], changes=delta))
                self.state = copy.deepcopy(current)
            self._append(record)

    def observe(self):
        """Preview idle edits without committing changes that may be reverted."""
        current = self.state_provider()
        with self.lock:
            delta = changes(self.state, current)
            self.pending = dict(event='state_delta', sequence=self.sequence+1,
                                timestamp=self.telemetry.reactor.monotonic(),
                                changes=delta, pending=True) if delta else None

    def _append(self, record):
        self.sequence += 1
        self.records.append(dict(sequence=self.sequence, **copy.deepcopy(record)))
        while len(self.records) > self.limit:
            expired = self.records.popleft()
            if expired['event'] == 'state_delta':
                apply_changes(self.baseline, expired['changes'])
            self.baseline_sequence = expired['sequence']
            self.baseline_time = expired['timestamp']

    def read(self, after=None):
        with self.lock:
            reset = after is None or after < self.baseline_sequence or after > self.sequence
            records = [dict(schema='happy_hare.motion', version=2, event='session',
                            session_id=self.telemetry.session_id,
                            sequence=self.baseline_sequence, timestamp=self.baseline_time,
                            checkpoint=self.baseline_sequence > 0, state=copy.deepcopy(self.baseline))] if reset else []
            records.extend(record for record in self.records if reset or record['sequence'] > after)
            if self.pending:
                records.append(self.pending)
            response = dict(schema='happy_hare.motion', version=2,
                            session_id=self.telemetry.session_id,
                            reset=reset, records=records)
        # Committed records and pending deltas are replaced, never mutated.
        # Only the rolling baseline needs copying while the producer is locked.
        return copy.deepcopy(response)
