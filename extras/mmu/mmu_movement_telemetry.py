# Happy Hare MMU Software
# Copyright (C) 2022-2026 Paul Morgan
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Versioned motion events, independent of console/file log verbosity.

Subscribers run on the printer thread and must not block. A consumer exception
is isolated from motion and detaches that consumer. Endpoints are controller
observations, not proof of physical filament movement; encoder readings are
reported separately. An outcome can mean queue submission, not queue completion.
"""
import contextlib
import copy
import functools
import json
import logging
import uuid


class MovementTelemetry:
    def __init__(self, reactor):
        self.reactor = reactor
        self.session_id = uuid.uuid4().hex
        self.sequence = 0
        self.move_id = 0
        self.listeners = []
        self.operations = []

    def subscribe(self, callback):
        self.listeners.append(callback)
        def unsubscribe():
            if callback in self.listeners:
                self.listeners.remove(callback)
        return unsubscribe

    def emit(self, event, move_id, **fields):
        self.sequence += 1
        record = dict(schema='happy_hare.motion', version=1,
                      session_id=self.session_id, sequence=self.sequence,
                      move_id=move_id, event=event,
                      timestamp=self.reactor.monotonic(), **fields)
        for callback in tuple(self.listeners):
            try:
                callback(copy.deepcopy(record))
            except Exception:
                logging.exception('Movement telemetry subscriber failed; detached')
                if callback in self.listeners:
                    self.listeners.remove(callback)
        return record

    def state(self, event, **fields):
        """State observations share the stream, but do not form movement pairs."""
        operation = self.operations[-1] if self.operations else None
        start = operation.start_record if operation else None
        return self.emit(event, start['move_id'] if start else None, **fields)

    @property
    def current(self):
        return self.operations[-1]

    @contextlib.contextmanager
    def operation(self):
        operation = MovementOperation(self)
        self.operations.append(operation)
        try:
            yield operation
        except BaseException as exc:
            operation.finish(status='error', error=str(exc))
            raise
        finally:
            # Covers an early return after a start without inventing an outcome.
            operation.finish(status='incomplete', error='No movement outcome reported')
            self.operations.pop()


class MovementOperation:
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.start_record = None
        self.finished = False
        self.error = None

    def start(self, **fields):
        if self.start_record is not None:
            raise RuntimeError('Movement already started')
        self.telemetry.move_id += 1
        self.start_record = self.telemetry.emit('movement_start', self.telemetry.move_id, **fields)

    def finish(self, **fields):
        if self.start_record is None or self.finished:
            return
        self.finished = True
        record = self.start_record
        outcome = dict(actual_mm=None, position_mm=None, homed=None,
                       encoder_mm=None, encoder_delta_mm=None, sensors=None,
                       error=self.error,
                       elapsed_s=max(0., self.telemetry.reactor.monotonic()-record['timestamp']))
        outcome.update(fields)
        self.telemetry.emit('movement_outcome', record['move_id'],
                            unit=record['unit'], gate=record['gate'], **outcome)


def movement_operation(function):
    """Guarantee a terminal outcome for every start, including exceptional exits."""
    @functools.wraps(function)
    def run(self, *args, **kwargs):
        with self.movement_telemetry.operation():
            return function(self, *args, **kwargs)
    return run


class JsonlTelemetrySink:
    """Optional local capture. Open in append mode to retain separate boot sessions."""
    def __init__(self, filename):
        self.file = open(filename, 'a', encoding='utf-8', buffering=1)

    def __call__(self, record):
        self.file.write(json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n')

    def close(self):
        self.file.close()


def filament_context(mmu):
    """Loaded config/persisted hotend state; lengths are input-filament millimetres."""
    from .mmu_utils import MmuColorUtils
    unit = mmu.mmu_unit()
    extruder = unit.extruder_wrapper
    gate = mmu.gate_selected
    color = mmu.gate_color[gate] if gate >= 0 else ''
    return dict(extruder=extruder.name, retracted_length=extruder.retracted_length,
                residual_mm=unit.toolhead_wrapper.p.toolhead_residual_filament,
                fragment_mm=extruder.filament_remaining,
                retained_color=MmuColorUtils.color_to_rgb_hex(extruder.filament_remaining_color, '808182')[:6],
                incoming_color=MmuColorUtils.color_to_rgb_hex(color, '808182')[:6])
