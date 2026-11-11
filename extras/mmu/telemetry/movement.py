# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Versioned motion events, independent of console/file log verbosity.
#
# Subscribers run on the printer thread and must not block. A consumer exception
# is isolated from motion and detaches that consumer. Endpoints are controller
# observations, not proof of physical filament movement; encoder readings are
# reported separately. An outcome can mean queue submission, not queue completion.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import contextlib
import copy
import functools
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

    def config_changed(self, scope, name, value):
        """A tunable value changed at runtime. Scope is the owning config section name."""
        if self.listeners:
            self.state('config_changed', scope=scope, name=name, value=value)

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
            # Mark an unfinished start explicitly on early return.
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
