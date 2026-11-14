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
import copy
import logging
import math

SETTLE_DELAY = .25  # Quiet time after the last event before state that settled late is read


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


def display_color(color):
    """Six-digit hex for the viewer, grey when the colour is unset"""
    from ..mmu_utils import MmuColorUtils
    return MmuColorUtils.color_to_rgb_hex(color, '808182')[:6]


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
    # Gate map attributes shown for a spool, beside the status, colour and weights read above
    GATE_MAP_ATTRIBUTES = ('gate_filament_name', 'gate_material', 'gate_vendor', 'gate_td', 'gate_temperature',
                           'gate_spool_id', 'gate_speed_override', 'gate_spool_rfid', 'ttg_map', 'endless_spool_groups')
    MACRO_VARIABLES = ('blade_pos', 'cutter_before_extruder',
                       'cooling_tube_position', 'cooling_tube_length')

    def __init__(self, mmu, record_mode=lambda: 'off'):
        self.mmu = mmu
        self.record_mode = record_mode

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
                output = spooler.output_power(direction, power)
                operations[str(gate)] = dict(direction=direction if output else 'stopped', output_power=output)
            units[unit.name]['espooler'] = dict(name=spooler.name, operations=operations)
        for unit in units.values():
            extruder = unit['extruder']
            extruder['filament_remaining_color'] = display_color(extruder['filament_remaining_color'])
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
                     selector=mmu.selector().get_status(now), telemetry=dict(record=self.record_mode()),
                     sync_mode=drive.get_sync_mode(), gate_status=list(mmu.gate_status), gate_color=[display_color(c) for c in mmu.gate_color],
                     gate_initial_weight=list(mmu.gate_initial_weight), gate_remaining_weight=list(mmu.gate_remaining_weight),
                     **{name: list(getattr(mmu, name)) for name in self.GATE_MAP_ATTRIBUTES},
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
when it leaves. State is read at each event, and once more when events have stopped
(SETTLE_DELAY), so a value assigned just after an event is recorded without a polling
timer. Changes that cancel before the next read are intentionally lost.
The stream folds acknowledged records into a rolling baseline, so only unacknowledged records are
kept; the limit only bounds a consumer that stops acknowledging.
"""
    def __init__(self, mmu, limit=256, record_mode=lambda: 'off'):
        if limit < 2:
            raise ValueError('Telemetry limit must be at least two records')
        self.telemetry = mmu.movement_telemetry
        self.state_provider = ReplayState(mmu, record_mode)
        self.limit = limit
        self.records = collections.deque()
        self.sequence = 0
        self.failed = False  # Set when the source detaches this capture after an error; the stream then replaces it
        self.on_toolchange_end = None
        self.in_toolchange = False  # Its start was seen, so its end is not that of an earlier command
        self.reactor = mmu.reactor
        self.open_moves = set()  # Started but not finished; state is not sampled in the middle of a move
        self.settle_timer = self.reactor.register_timer(self._settle, self.reactor.NEVER)
        self.state = copy.deepcopy(self.state_provider())
        # Parameter sets are read once; later edits are config_changed events routed by section name
        self.scopes = self.state_provider.scopes()
        self.baseline = merge(copy.deepcopy(self.state), self.state_provider.static())
        self.baseline_sequence = 0
        self.baseline_time = mmu.reactor.monotonic()
        self.unsubscribe = self.telemetry.subscribe(self.on_event)

    def close(self):
        if self.settle_timer is not None:
            self.reactor.unregister_timer(self.settle_timer)
            self.settle_timer = None
        self.unsubscribe()

    def _config_delta(self, source):
        value = plain(source['value'])
        return [dict(path=[*path, source['name']], value=value) for path in self.scopes.get(source['scope'], ())]

    def on_event(self, source):
        # Every event, including a configuration change, commits whatever else has changed since the last one
        try:
            current, delta = self._read()
            record = None
            if source['event'] == 'config_changed':
                delta += self._config_delta(source)
            else:
                record = plain({key: value for key, value in source.items()
                                if key not in ('schema', 'version', 'session_id', 'sequence')})
        except Exception:
            self.failed = True  # The source detaches a listener that raises
            raise
        self._commit(source['timestamp'], current, delta, record)
        if source['event'] in ('movement_start', 'selector_movement_start'):
            self.open_moves.add(source['move_id'])
        elif source['event'] in ('movement_outcome', 'selector_movement_outcome'):
            self.open_moves.discard(source['move_id'])
        self.reactor.update_timer(self.settle_timer, self.reactor.NEVER if self.open_moves
                                  else self.reactor.monotonic() + SETTLE_DELAY)
        if source['event'] == 'toolchange':
            self.in_toolchange = True
        elif source['event'] == 'toolchange_end' and self.in_toolchange:
            self.in_toolchange = False
            if self.on_toolchange_end:
                self.on_toolchange_end()

    def mark_record_mode(self, mode):
        """Commit the settled state, then tell the consumers, in order, that recording changed mode."""
        try:
            current, delta = self._read()
        except Exception:
            self.failed = True
            raise
        self._commit(self.reactor.monotonic(), current, delta, dict(event='record_mode', mode=mode,
                                                                    timestamp=self.reactor.monotonic()))

    def _read(self):
        current = self.state_provider()
        return current, changes(self.state, current)

    def _commit(self, timestamp, current, delta, record=None):
        if delta:
            self._append(dict(event='state_delta', timestamp=timestamp, changes=delta))
            self.state = current
        if record:
            self._append(record)

    def _settle(self, eventtime):
        # Events have stopped: record whatever the last one left behind
        if not self.open_moves:
            try:
                current, delta = self._read()
            except Exception:
                logging.exception('MMU telemetry settle read failed')
                self.failed = True  # A timer must not raise; the stream replaces a failed capture
            else:
                self._commit(eventtime, current, delta)
        return self.reactor.NEVER

    def _append(self, record):
        self.sequence += 1
        self.records.append(dict(sequence=self.sequence, **record))
        while len(self.records) > self.limit:  # A consumer has stopped acknowledging
            self.fold_through(self.records[0]['sequence'])

    def fold_through(self, sequence):
        """Fold records up to a sequence into the baseline: nothing at or before it needs replaying."""
        while self.records and self.records[0]['sequence'] <= sequence:
            expired = self.records.popleft()
            if expired['event'] == 'state_delta':
                apply_changes(self.baseline, expired['changes'])
            self.baseline_sequence = expired['sequence']
            self.baseline_time = expired['timestamp']

    def read(self, after=None, limit=None, history="start", detach=True):
        """Records after the cursor; a stale or missing cursor restarts at the baseline.
        history='current' folds everything recorded so far into that baseline instead of replaying it.
        Records are copied unless detach is False: appended records are never modified, so the
        stream, which serializes them at once, shares them. The baseline header is always a copy."""
        reset = after is None or after < self.baseline_sequence or after > self.sequence
        records = []
        if reset:
            # Only the baseline is edited in place (when old records expire), so only it is copied
            header = dict(schema='happy_hare.motion', version=1, event='session',
                          session_id=self.telemetry.session_id,
                          sequence=self.baseline_sequence, timestamp=self.baseline_time,
                          checkpoint=self.baseline_sequence > 0, state=copy.deepcopy(self.baseline))
            records.append(header)
            if history == 'current':
                for record in self.records:
                    if record['event'] == 'state_delta':
                        apply_changes(header['state'], record['changes'])
                    header.update(sequence=record['sequence'], timestamp=record['timestamp'], checkpoint=True)
        if not (reset and history == 'current'):
            pending = [record for record in self.records if reset or record['sequence'] > after]
            records.extend(copy.deepcopy(pending) if detach else pending)
        return dict(schema='happy_hare.motion', version=1, session_id=self.telemetry.session_id,
                    reset=reset, records=records if limit is None else records[:limit])
