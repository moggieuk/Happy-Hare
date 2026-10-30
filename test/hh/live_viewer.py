# Copyright (C) 2022-2026 Paul Morgan
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Bounded, versioned simulator telemetry. Only capture() touches the printer.

HTTP readers consume immutable snapshots under a lock, including while the
console is inside a command. They never execute commands or enter the reactor.
"""
import copy
import collections
import math
import inspect
import functools
import threading
import uuid


def plain(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


class LiveViewer:
    LIMIT = 3000

    def __init__(self, session):
        self.hh = session
        self.model = session.filament()
        self.session_id = uuid.uuid4().hex
        self.events = collections.deque(maxlen=self.LIMIT)
        self.lock = threading.Lock()
        self.sequence = 0
        self.latest = None
        self.latest_raw = None
        self.extruded_mm = 0.
        self.purge_base_mm = 0.
        self.nozzle_color = None
        self.purge_color_start = None
        self.motors = {}
        self.depth = 0
        self.wrapped = []
        self.model.observers.append(self.on_filament)
        session.printer.harness_stepper_observer = self.on_stepper
        session.printer.harness_drive_observer = self.on_drive
        self.pending_moves = {}
        self.telemetry_records = collections.deque(maxlen=self.LIMIT*2)
        self.unsubscribe = session.mmu.movement_telemetry.subscribe(self.on_telemetry)
        self.wrap(session.printer.lookup_object('toolhead'), 'move')
        effects = getattr(session.printer, 'harness_macro_effects', {})
        for name, effect in list(effects.items()):
            if getattr(effect, '__name__', '') == '_effect_form_tip':
                self.wrap(effects, name)
        for name, obj in session.printer.objects.items():
            if name.startswith('mmu_stepper '):
                for method in ('do_move', 'do_homing_move', 'do_background_move'):
                    self.wrap(obj, method)
        self.capture('session', 'Simulator ready')

    def on_telemetry(self, record):
        with self.lock:
            self.telemetry_records.append(record)
        if record['event'] == 'filament_pos_changed':
            self.capture('state', record['filament_pos_name'], telemetry=record)
        if record['event'] not in ('movement_start', 'movement_outcome'):
            return
        key = record['move_id']
        if record['event'] == 'movement_start':
            self.pending_moves[key] = (record, self.snapshot())
            self.depth += 1
            return
        pending = self.pending_moves.pop(key, None)
        if pending is None:
            return
        self.depth -= 1
        start, before = pending
        after = self.snapshot()
        motors = {name: value-before['motors'].get(name, 0.)
                  for name, value in after['motors'].items()
                  if value != before['motors'].get(name, 0.)}
        self.capture('movement', start['label'], before=before,
            origin='happy_hare.motion', telemetry={'start': start, 'outcome': record},
            duration=record['elapsed_s'], requested=start['requested_mm'],
            distance=record['actual_mm'], speed=start['speed_mm_s'],
            acceleration=start['acceleration_mm_s2'], motors=motors,
            homing=start['homing'], endstop=start['endstop'], homed=record['homed'],
            error=record['error'], outcome=record['status'],
            fast_bowden=start['label'] in ('Fast loading move through bowden', 'Fast unloading move through bowden'))

    def read_telemetry(self, after=0):
        with self.lock:
            return {'schema': 'happy_hare.motion', 'version': 1,
                    'session_id': self.hh.mmu.movement_telemetry.session_id,
                    'records': [r for r in self.telemetry_records if r['sequence'] > after]}

    def wrap(self, owner, method, filament=False):
        original = owner.get(method) if isinstance(owner, dict) else getattr(owner, method, None)
        if original is None:
            return
        signature = inspect.signature(original)
        @functools.wraps(original)
        def movement(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            if self.depth:
                if values.get('speed') is not None and values['speed'] > 0:
                    self.resolved_speed = values['speed']
                return original(*args, **kwargs)
            self.resolved_speed = values.get('speed')
            before, start = self.snapshot(), self.hh.reactor.monotonic()
            label = values.get('trace_str', method.replace('_', ' '))
            result, error = None, None
            self.depth += 1
            try:
                result = original(*args, **kwargs)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                self.depth -= 1
                after = self.snapshot()
                motors = {name: value-before['motors'].get(name, 0.)
                          for name, value in after['motors'].items()
                          if value != before['motors'].get(name, 0.)}
                actual = result[0] if filament and result is not None else None
                self.capture('movement', label, before=before, origin='harness_fallback',
                    duration=max(0., self.hh.reactor.monotonic()-start),
                    requested=values.get('dist'), distance=actual,
                    speed=self.resolved_speed, motors=motors,
                    fast_bowden=label in ('Fast loading move through bowden', 'Fast unloading move through bowden'),
                    homing=values.get('homing_move', method == 'do_homing_move'),
                    endstop=values.get('endstop_name'),
                    homed=bool(result[1]) if filament and result is not None else None,
                    error=error)
        self.wrapped.append((owner, method, original))
        if isinstance(owner, dict):
            owner[method] = movement
        else:
            setattr(owner, method, movement)

    def close(self):
        self.unsubscribe()
        for owner, method, original in reversed(self.wrapped):
            if isinstance(owner, dict):
                owner[method] = original
            else:
                setattr(owner, method, original)
        self.wrapped.clear()
        if self.on_filament in self.model.observers:
            self.model.observers.remove(self.on_filament)
        self.hh.printer.harness_stepper_observer = None
        self.hh.printer.harness_drive_observer = None

    def snapshot(self):
        hh, model = self.hh, self.model
        sensors = hh.mmu.sensor_manager.get_sensor_states(all_sensors=True)
        selected_sensors = hh.mmu.sensor_manager.get_sensor_states(unit=hh.mmu.mmu_unit().unit_index)
        selected_objects = {id(sensor) for _, sensor in selected_sensors.values()}
        unit_sensor_names = [name for name, (_, sensor) in sensors.items() if id(sensor) in selected_objects]
        devices = {}
        for name, obj in hh.printer.objects.items():
            if ('encoder' in name or 'buffer' in name) and hasattr(obj, 'get_status'):
                devices[name] = obj.get_status(hh.reactor.monotonic())
        for unit in hh.mmu.mmu_machine.units:
            for kind in ('encoder', 'buffer'):
                obj = getattr(unit, kind, None)
                if obj is not None:
                    getter = getattr(obj, 'get_status', None)
                    status = getter(hh.reactor.monotonic()) if getter else {}
                    if kind == 'buffer':
                        geometry = model.buffers.get(obj.name)
                        if geometry is not None:
                            gate = hh.mmu.gate_selected
                            travel = geometry['travel'].get(gate)
                            status = dict(travel=travel, maximum=geometry['maxrange'],
                                          value=None if travel is None else 2*travel/geometry['maxrange']-1)
                        elif obj.proportional_sensor is not None:
                            status = obj.proportional_sensor.get_status(hh.reactor.monotonic())
                        else:
                            status = dict(value=None, spring=obj.buffer_spring_state)
                    devices[unit.name + ':' + kind] = status
        from extras.mmu.mmu_constants import FILAMENT_POS_NAME_MAP
        layout = model.layout.copy()
        params = hh.mmu.mmu_unit().toolhead_wrapper.p
        layout['extruder_gears'] = layout['extruder_entry'] + params.toolhead_entry_to_extruder
        layout['nozzle'] = layout['extruder_gears'] + params.toolhead_extruder_to_nozzle
        from extras.mmu.mmu_movement_telemetry import filament_context
        return plain(dict(filament=filament_context(hh.mmu), filament_pos=hh.mmu.filament_pos,
            filament_pos_name=FILAMENT_POS_NAME_MAP.get(hh.mmu.filament_pos, 'UNKNOWN'), unit_sensors=unit_sensor_names, unit=hh.mmu.mmu_unit().name, units=[u.name for u in hh.mmu.mmu_machine.units], buffers=model.buffers, gate=hh.mmu.gate_selected, tips=model.tip[:], tails=model.tail[:],
            layout=layout, motors=self.motors.copy(),
            sensors={name: {'state': state[0], 'position': model.position(name)}
                     for name, state in sensors.items()}, devices=devices))

    def capture(self, kind, label, **details):
        if self.depth:
            return
        state = self.snapshot()
        with self.lock:
            if kind == 'state' and state == self.latest_raw:
                return
            raw_before = details.get('before')
            if raw_before is not None:
                details['raw_before'] = copy.deepcopy(raw_before)
            if raw_before is not None and self.latest_raw is not None and raw_before['tips'] != self.latest_raw['tips']:
                # Explicit harness placement/recovery between captured movements.
                self.latest = copy.deepcopy(raw_before)
                self.latest_raw = copy.deepcopy(raw_before)
            raw_state = copy.deepcopy(state)
            if self.nozzle_color is None:
                self.nozzle_color = state['filament']['retained_color']
                self.purge_color_start = self.nozzle_color
            extruder_moves = False
            if self.latest is not None and self.latest_raw is not None:
                for gate, tip in enumerate(state['tips']):
                    delta = tip - self.latest_raw['tips'][gate]
                    state['tips'][gate] = self.latest['tips'][gate] + delta
                if 'before' in details:
                    details['before'] = copy.deepcopy(self.latest)
                gate = state['gate']
                if gate >= 0:
                    extruder_moves = any('extruder' in name and
                        abs(value-self.latest_raw['motors'].get(name, 0)) > 1e-9
                        for name, value in state['motors'].items())
                    before_tip = self.latest['tips'][gate]
                    if not extruder_moves and state['tips'][gate] > before_tip:
                        state['tips'][gate] = min(state['tips'][gate],
                            max(before_tip, state['layout']['extruder_gears']))
            gate = state['gate']
            if gate >= 0:
                tail = state['tails'][gate]
                for name, sensor in state['sensors'].items():
                    if name not in state['unit_sensors'] or sensor['state'] is not False:
                        continue
                    if 'filament_compression' in name or 'filament_tension' in name or 'encoder' in name:
                        continue
                    short = name.split(':')[-1]
                    if short.startswith('mmu_') and short.rsplit('_', 1)[-1].isdigit() and int(short.rsplit('_', 1)[-1]) != gate:
                        continue
                    position = sensor['position']
                    if position is not None and (tail is None or tail <= position):
                        state['tips'][gate] = min(state['tips'][gate], position-0.001)
            if gate >= 0 and extruder_moves:
                nozzle = state['layout']['nozzle']
                amount = max(0., state['tips'][gate] - nozzle)
                if state['tips'][gate] < self.latest['tips'][gate] and self.latest['tips'][gate] >= nozzle-.01:
                    self.purge_base_mm = self.extruded_mm
                if amount:
                    if int((self.extruded_mm-self.purge_base_mm)/36) != int((self.extruded_mm+amount-self.purge_base_mm)/36) or self.extruded_mm == self.purge_base_mm:
                        self.purge_color_start = self.nozzle_color
                    incoming = state['filament']['incoming_color']
                    length = max(3., state['filament']['residual_mm'] + state['filament']['fragment_mm'])
                    fraction = 1-math.exp(-amount/length)
                    self.nozzle_color = ''.join(f'{round(int(self.nozzle_color[i:i+2],16)*(1-fraction)+int(incoming[i:i+2],16)*fraction):02x}' for i in (0,2,4))
                self.extruded_mm += amount
                state['tips'][gate] = min(state['tips'][gate], nozzle)
            state['extruded_mm'] = self.extruded_mm
            state['purge_base_mm'] = self.purge_base_mm
            state['nozzle_color'] = self.nozzle_color
            state['purge_color_start'] = self.purge_color_start
            if 'before' in details:
                details['before'].setdefault('extruded_mm', self.latest.get('extruded_mm', 0.))
            self.latest_raw = raw_state
            self.sequence += 1
            event = dict(version=2, session=self.session_id, seq=self.sequence,
                         time=self.hh.reactor.monotonic(), kind=kind, label=label,
                         state=state, raw_state=raw_state, **plain(details))
            self.events.append(event)
            self.latest = state

    def on_filament(self, gate, delta, start_tip, start_tail):
        self.capture('filament', 'Filament travel', gate=gate, distance=delta)

    def on_drive(self, distance, origin='manual'):
        if not distance or self.hh.mmu.gate_selected < 0:
            return
        from extras.mmu.mmu_constants import (DRIVE_EXTRUDER_ONLY,
            DRIVE_EXTRUDER_SYNCED_TO_GEAR, DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        drive = self.hh.mmu.drive()
        mode = drive.get_sync_mode()
        if origin == 'toolhead':
            owners = [drive.mmu_extruder_stepper]
            if mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
                owners.append(drive.mmu_gear_stepper)
        elif mode in (DRIVE_EXTRUDER_SYNCED_TO_GEAR, DRIVE_GEAR_SYNCED_TO_EXTRUDER):
            owners = [drive.mmu_gear_stepper, drive.mmu_extruder_stepper]
        else:
            owners = [drive.mmu_extruder_stepper if mode == DRIVE_EXTRUDER_ONLY else drive.mmu_gear_stepper]
        seen = set()
        for owner in owners:
            for motor in owner.get_steppers():
                name = motor.get_name()
                if name not in seen:
                    seen.add(name)
                    self.on_stepper(name, distance)

    def on_stepper(self, name, distance):
        self.motors[name] = self.motors.get(name, 0.) + distance
        self.capture('stepper', name, motor=name, distance=distance)

    def read(self, after=0):
        with self.lock:
            return {'version': 2, 'session': self.session_id, 'latest': self.latest,
                    'first': self.events[0]['seq'] if self.events else 0,
                    'events': [e for e in self.events if e['seq'] > after]}

    def parameters(self, unit_index):
        # Exactly the parameter families used by MMU_TEST_CONFIG. Guards remain
        # authoritative, and edits are dispatched through that command itself.
        mmu = self.hh.mmu
        unit = mmu.mmu_machine.units[unit_index]
        sets = [mmu.p, unit.p, unit.selector.p, unit.toolhead_wrapper.p]
        if mmu.gate_selected >= 0:
            from extras.mmu.commands.mmu_test_config import MmuCalibrationParameters
            sets.append(MmuCalibrationParameters(mmu))
        result = {}
        for params in sets:
            for spec in params._SPECS:
                if params._is_available(spec):
                    value = getattr(params, spec.name)
                    result[spec.name] = dict(name=spec.name, kind=spec.kind,
                        value=plain(value), section=spec.section, choices=spec.choices)
        return list(result.values())
