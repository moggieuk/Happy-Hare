# Copyright (C) 2022-2026 Paul Morgan
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Bounded, versioned simulator telemetry. Only capture() touches the printer.

HTTP readers consume immutable snapshots under a lock, including while the
console is inside a command. They never execute commands or enter the reactor.
"""
import collections
import math
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
        self.motors = {}
        self.model.observers.append(self.on_filament)
        session.printer.harness_stepper_observer = self.on_stepper
        session.printer.harness_drive_observer = self.on_drive
        self.capture('session', 'Simulator ready')

    def close(self):
        if self.on_filament in self.model.observers:
            self.model.observers.remove(self.on_filament)
        self.hh.printer.harness_stepper_observer = None
        self.hh.printer.harness_drive_observer = None

    def snapshot(self):
        hh, model = self.hh, self.model
        sensors = hh.mmu.sensor_manager.get_sensor_states(all_sensors=True)
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
        return plain(dict(unit=hh.mmu.mmu_unit().name, units=[u.name for u in hh.mmu.mmu_machine.units], buffers=model.buffers, gate=hh.mmu.gate_selected, tips=model.tip[:], tails=model.tail[:],
            layout=model.layout.copy(), motors=self.motors.copy(),
            sensors={name: {'state': state[0], 'position': model.position(name)}
                     for name, state in sensors.items()}, devices=devices))

    def capture(self, kind, label, **details):
        state = self.snapshot()
        with self.lock:
            if kind == 'state' and state == self.latest:
                return
            self.sequence += 1
            event = dict(version=1, session=self.session_id, seq=self.sequence,
                         time=self.hh.reactor.monotonic(), kind=kind, label=label,
                         state=state, **plain(details))
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
            return {'version': 1, 'session': self.session_id, 'latest': self.latest,
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
