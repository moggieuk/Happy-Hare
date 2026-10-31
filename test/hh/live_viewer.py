# Copyright (C) 2022-2026 Paul Morgan
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Bounded, versioned simulator telemetry. Capture and edits run on the printer thread.

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
import re


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
        self.pending_config = None
        self.config_revision = 0
        self.macro_catalog = self._macro_catalog()
        self.pending_moves = {}
        self.telemetry_records = collections.deque(maxlen=self.LIMIT*2)
        self.unsubscribe = session.mmu.movement_telemetry.subscribe(self.on_telemetry)
        self.wrap(session.printer.lookup_object('toolhead'), 'move', suppress_fallback=True)
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
        if record['event'] == 'standalone_load_retraction' and record['retracted_length'] > 0:
            self.purge_base_mm = self.extruded_mm
            self.capture('purge_release', 'Final load retraction', before=self.snapshot(), telemetry=record)
        if record['event'] == 'filament_cut':
            self.capture('cut', 'Toolhead filament cut', before=self.snapshot(),
                cutter=record['cutter'], origin=record.get('origin'), telemetry=record)
        if record['event'] == 'filament_pos_changed':
            self.capture('state', record['filament_pos_name'], telemetry=record)
        if record['event'] not in ('movement_start', 'movement_outcome'):
            return
        key = record['move_id']
        if record['event'] == 'movement_start':
            self.observe_configuration()
            self.flush_configuration()
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
            distance=record['actual_mm'], speed=record.get('extrusion_speed_mm_s', start['speed_mm_s']),
            source=start.get('source'), extruder=start.get('extruder'),
            context=start.get('context'), estimated_duration=record.get('estimated_duration_s'),
            acceleration=start['acceleration_mm_s2'], motors=motors,
            homing=start['homing'], endstop=start['endstop'], homed=record['homed'],
            error=record['error'], outcome=record['status'],
            fast_bowden=start['label'] in ('Fast loading move through bowden', 'Fast unloading move through bowden'))

    def read_telemetry(self, after=0):
        with self.lock:
            return {'schema': 'happy_hare.motion', 'version': 1,
                    'session_id': self.hh.mmu.movement_telemetry.session_id,
                    'records': [r for r in self.telemetry_records if r['sequence'] > after]}

    def wrap(self, owner, method, filament=False, suppress_fallback=False):
        original = owner.get(method) if isinstance(owner, dict) else getattr(owner, method, None)
        if original is None:
            return
        signature = inspect.signature(original)
        @functools.wraps(original)
        def movement(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            if suppress_fallback:
                # G1 source events own extrusion capture. Toolhead calls also
                # include printing, G0 and arc segments: never infer events here.
                self.depth += 1
                try:
                    return original(*args, **kwargs)
                finally:
                    self.depth -= 1
            if self.depth:
                if values.get('speed') is not None and values['speed'] > 0:
                    self.resolved_speed = values['speed']
                return original(*args, **kwargs)
            self.resolved_speed = values.get('speed')
            self.observe_configuration()
            self.flush_configuration()
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
        active_objects = {id(sensor) for _, sensor in hh.mmu.sensor_manager.get_sensor_states().values()}
        active_sensor_names = [name for name, (_, sensor) in sensors.items() if id(sensor) in active_objects]
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
        cutters = []
        cut_vars = hh.mmu.cut_tip_vars()
        if cut_vars is not None:
            cutters.append(dict(kind='toolhead', position=layout['nozzle']-float(cut_vars['blade_pos']),
                before_extruder=hh.mmu.cutter_before_extruder()))
        sequence = hh.printer.lookup_object('gcode_macro _MMU_SEQUENCE_VARS', None)
        extension = str(sequence.variables.get('user_post_unload_extension', '')) if sequence else ''
        post_macro = hh.mmu._macro_name(hh.mmu.p.post_unload_macro).upper()
        if post_macro == 'SERVO_CUTTER_ACTION' or (post_macro == '_MMU_POST_UNLOAD' and
                any(line.strip().split(' ', 1)[0].upper() == 'SERVO_CUTTER_ACTION' for line in extension.splitlines())):
            cutters.append(dict(kind='mmu', position=layout.get('mmu_exit', 0.)+10))
        drive = hh.mmu.drive()
        drive_motors = {kind: [motor.get_name() for motor in owner.get_steppers()]
                        for kind, owner in [('gear', drive.mmu_gear_stepper), ('extruder', drive.mmu_extruder_stepper)]}
        from extras.mmu.mmu_movement_telemetry import filament_context
        return plain(dict(configuration=self.configuration(), drive_motors=drive_motors, active_sensors=active_sensor_names, cutters=cutters, synced=hh.mmu.drive().get_sync_mode() in (1, 3), filament=filament_context(hh.mmu), filament_pos=hh.mmu.filament_pos,
            filament_pos_name=FILAMENT_POS_NAME_MAP.get(hh.mmu.filament_pos, 'UNKNOWN'), unit_sensors=unit_sensor_names, unit=hh.mmu.mmu_unit().name, units=[u.name for u in hh.mmu.mmu_machine.units], buffers=model.buffers, gate=hh.mmu.gate_selected, tips=model.tip[:], tails=model.tail[:],
            layout=layout, motors=self.motors.copy(),
            sensors={name: {'state': state[0], 'position': model.position(name), 'enabled': bool(state[1].runout_helper.sensor_enabled)}
                     for name, state in sensors.items()}, devices=devices))

    def capture(self, kind, label, **details):
        if self.depth:
            return
        state = self.snapshot()
        if kind in ('state', 'config'):
            self.observe_configuration(state)
            if kind == 'config' or (self.pending_config and state == self.pending_config['raw_state']):
                return
        self.flush_configuration()
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
            if gate >= 0 and details.get('homing') and details.get('homed'):
                target = details.get('endstop', '')
                sensor = state['sensors'].get(target)
                distance = details.get('distance') or details.get('requested') or 0
                # Successful leading-edge sensor homing is a concrete coordinate,
                # independent of accumulated schematic or raw travel offsets.
                # Ignore buffer/encoder and trailing-edge homes: those do not
                # locate the filament nose at a presence sensor.
                if (sensor and distance and sensor['state'] is (distance > 0)
                        and sensor['position'] is not None
                        and target in state.get('active_sensors', state['unit_sensors'])
                        and not any(kind in target for kind in ('compression', 'tension', 'encoder'))):
                    state['tips'][gate] = sensor['position']
                    details['homing_anchor'] = True
            if gate >= 0:
                tail = state['tails'][gate]
                for name, sensor in state['sensors'].items():
                    if name not in state.get('active_sensors', state['unit_sensors']) or sensor['state'] is not False:
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

    def _macro_catalog(self):
        macros = {name.split(' ', 1)[1]: obj for name, obj in self.hh.printer.objects.items()
                  if name.startswith('gcode_macro ') and hasattr(obj, 'variables')}
        assigned = set()
        for obj in macros.values():
            script = getattr(getattr(obj, 'template', None), 'script', '')
            for line in script.splitlines():
                if 'SET_GCODE_VARIABLE' not in line.upper():
                    continue
                macro = re.search(r'\bMACRO=([A-Za-z0-9_]+)', line, re.I)
                variable = re.search(r'\bVARIABLE=([A-Za-z0-9_]+)', line, re.I)
                if macro and variable:
                    assigned.add((macro[1].upper(), variable[1]))
        result = []
        for macro, obj in sorted(macros.items()):
            for name in sorted(obj.variables):
                if (name.startswith(('_', 'output_', 'saved_')) or
                        (macro.upper(), name) in assigned or
                        not re.fullmatch(r'[a-z][a-z0-9_]*', name)):
                    continue
                result.append((macro, name))
        return result

    def macro_parameters(self):
        result = []
        for macro, name in self.macro_catalog:
            obj = self.hh.printer.lookup_object('gcode_macro ' + macro)
            value = obj.variables[name]
            result.append(dict(id='macro:' + macro + ':' + name, name=name, macro=macro,
                               value=plain(value), literal=repr(value), kind='macro',
                               group=macro, section='Macro variables'))
        return result

    def configuration(self):
        units = self.hh.mmu.mmu_machine.units
        return plain(dict(parameters={str(i): {p['name']: p['value'] for p in self.parameters(i) if p['scope'] != 'other'}
                                      for i in range(len(units))},
                          calibration={unit.name: dict(
                              bowden_lengths={str(g): unit.calibrator.get_bowden_length(g) for g in unit.gate_range()},
                              rotation_distances={str(g): unit.calibrator.get_gear_rd(g) for g in unit.gate_range()},
                              encoder_clog_length=unit.calibrator.get_clog_detection_length()) for unit in units},
                          macros={p['id']: p['value'] for p in self.macro_parameters()},
                          sensors={name: bool(sensor.runout_helper.sensor_enabled)
                                   for name, (_, sensor) in self.hh.mmu.sensor_manager.get_sensor_states(all_sensors=True).items()}))

    def observe_configuration(self, state=None):
        if self.latest_raw is None:
            return
        state = self.snapshot() if state is None else state
        baseline = self.latest_raw['configuration']
        current = state['configuration']
        with self.lock:
            previous = self.pending_config['raw_state']['configuration'] if self.pending_config else baseline
            if current == previous:
                return
            self.config_revision += 1
            if current == baseline:
                self.pending_config = None
                return
            changes = []
            def compare(old, new, prefix=''):
                for key in sorted(set(old) | set(new)):
                    path = prefix + key
                    if isinstance(old.get(key), dict) and isinstance(new.get(key), dict):
                        compare(old[key], new[key], path + '.')
                    elif old.get(key) != new.get(key):
                        changes.append(dict(name=path, before=old.get(key), after=new.get(key)))
            compare(baseline, current)
            visual = copy.deepcopy(state)
            # Preserve reconciled schematic endpoints; configuration changes do not move filament.
            visual['tips'] = [tip + raw - previous for tip, raw, previous in
                              zip(self.latest['tips'], state['tips'], self.latest_raw['tips'])]
            for key in ('extruded_mm', 'purge_base_mm', 'nozzle_color', 'purge_color_start'):
                if key in self.latest:
                    visual[key] = copy.deepcopy(self.latest[key])
            self.pending_config = dict(version=2, session=self.session_id, seq=self.sequence + 1,
                time=self.hh.reactor.monotonic(), kind='config', label='Configuration changed',
                changes=plain(changes), before=copy.deepcopy(self.latest), state=visual,
                raw_state=copy.deepcopy(state), pending=True)

    def flush_configuration(self):
        with self.lock:
            if not self.pending_config:
                return
            event = self.pending_config
            event.pop('pending', None)
            self.sequence = event['seq']
            self.events.append(event)
            self.latest = event['state']
            self.latest_raw = event['raw_state']
            self.pending_config = None
            self.config_revision += 1

    def read(self, after=0):
        with self.lock:
            return copy.deepcopy({'version': 2, 'session': self.session_id,
                    'config_revision': self.config_revision, 'pending_config': self.pending_config,
                    'latest': self.pending_config['state'] if self.pending_config else self.latest,
                    'first': self.events[0]['seq'] if self.events else 0,
                    'events': [e for e in self.events if e['seq'] > after]})

    def parameters(self, unit_index):
        # Exactly the parameter families used by MMU_TEST_CONFIG. Guards remain
        # authoritative, and edits are dispatched through that command itself.
        mmu = self.hh.mmu
        unit = mmu.mmu_machine.units[unit_index]
        sets = [mmu.p, unit.p, unit.selector.p, unit.toolhead_wrapper.p]
        if mmu.gate_selected >= 0:
            from extras.mmu.commands.mmu_test_config import MmuCalibrationParameters
            sets.append(MmuCalibrationParameters(mmu))
        groups = ['Machine Wide', f'Unit {unit_index}', f'Unit {unit_index}',
                  f"Toolhead '{unit.toolhead_wrapper.name}' · Unit {unit_index}"]
        if mmu.gate_selected >= 0:
            groups.append(f'Other · Calibration ({mmu.mmu_unit().name} / gate {mmu.gate_selected})')
        result = {}
        for index, params in enumerate(sets):
            for spec in params._SPECS:
                if params._is_available(spec):
                    value = getattr(params, spec.name)
                    result[spec.name] = dict(name=spec.name, kind=spec.kind,
                        value=plain(value), section=spec.section, choices=spec.choices,
                        scope='machine' if index == 0 else 'unit' if index < 4 else 'other',
                        group=groups[index])
        return list(result.values())
