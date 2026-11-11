# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Printer-independent replay checkpoints and rotated JSONL recording.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from copy import deepcopy
import json
import logging
from pathlib import Path


def apply_delta(state, changes):
    for change in changes:
        target = state
        for key in change['path'][:-1]:
            target = target.setdefault(key, {})
        key = change['path'][-1]
        if change.get('remove'):
            target.pop(key, None)
        else:
            target[key] = deepcopy(change['value'])


class TelemetryHistory:
    def __init__(self, limit=6000):
        self.limit = limit
        self.header = None
        self.records = deque()

    def append(self, batch):
        committed = []
        for record in batch['records']:
            if record['event'] == 'session':
                self.header = deepcopy(record)
                self.records.clear()
                committed.append(record)
            elif record['sequence'] > self.sequence:
                self.records.append(deepcopy(record))
                committed.append(record)
                while len(self.records) > self.limit:
                    self._advance(self.header, self.records.popleft())
        return committed

    @property
    def sequence(self):
        return self.records[-1]['sequence'] if self.records else self.header['sequence'] if self.header else 0

    @staticmethod
    def _advance(header, record):
        if record['event'] == 'state_delta':
            apply_delta(header['state'], record['changes'])
        header.update(sequence=record['sequence'], timestamp=record['timestamp'], checkpoint=True)

    def read(self, after=None, limit=128, history='latest'):
        if self.header is None:
            raise ValueError('Telemetry has no initial state')
        reset = after is None or after < self.header['sequence'] or after > self.sequence
        header = deepcopy(self.header) if reset else None
        boundary = None
        if reset and history == 'current':
            boundary = self.sequence + 1
        elif reset and history == 'latest':
            boundary = next((r['sequence'] for r in reversed(self.records) if r['event'] == 'toolchange'), None)
        if boundary is not None:
            for record in self.records:
                if record['sequence'] >= boundary:
                    break
                self._advance(header, record)
        cursor = header['sequence'] if reset else after
        records = [header] if reset else []
        for record in self.records:
            if record['sequence'] > cursor:
                if len(records) >= limit:
                    break
                records.append(record)
        return dict(schema='happy_hare.motion', version=1, session_id=self.header['session_id'],
                    reset=reset, records=deepcopy(records))


class TelemetryRecorder:
    """Called only on a dedicated worker. Every rotated file is independently replayable."""
    def __init__(self, path, max_bytes, backups):
        self.path = Path(path)
        self.max_bytes, self.backups = max_bytes, backups
        self.header = None
        self.file = None
        self.size = 0
        self.active_moves = set()

    def _rotate(self):
        if self.file:
            self.file.close()
            self.file = None
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else Path(str(self.path) + '.' + str(index-1))
            target = Path(str(self.path) + '.' + str(index))
            if source.exists():
                source.replace(target)
        self.file = self.path.open('w', encoding='utf-8')
        self.size = 0

    def _line(self, record):
        line = json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n'
        self.file.write(line)
        self.size += len(line.encode('utf-8'))

    def write(self, records):
        for record in records:
            if record['event'] == 'session':
                self.header = deepcopy(record)
                self.active_moves.clear()
                self._rotate()
                self._line(record)
                continue
            if self.header is None:
                raise ValueError('Recording requires an initial checkpoint')
            if self.size >= self.max_bytes and not self.active_moves:
                self._rotate()
                self._line(self.header)
            self._line(record)
            if record['event'] in ('movement_start', 'selector_movement_start'):
                self.active_moves.add(record['move_id'])
            elif record['event'] in ('movement_outcome', 'selector_movement_outcome'):
                self.active_moves.discard(record['move_id'])
            TelemetryHistory._advance(self.header, record)
        if self.file:
            self.file.flush()

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


class TelemetryWriter:
    """Bounded asynchronous handoff; all file operations stay on one worker."""
    def __init__(self, recorder, failed, capacity=64):
        self.recorder, self.failed = recorder, failed
        self.queue = asyncio.Queue(maxsize=capacity)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mmu-telemetry')
        self.task = asyncio.create_task(self._run())
        self.stopped = False

    def submit(self, records):
        if self.stopped:
            return
        try:
            self.queue.put_nowait(records)
        except asyncio.QueueFull:
            self.stopped = True
            self.failed('disk writer queue is full')

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            records = await self.queue.get()
            try:
                if records is None:
                    return
                await loop.run_in_executor(self.executor, self.recorder.write, records)
            except Exception:
                self.stopped = True
                logging.exception('MMU telemetry recording stopped')
                self.failed('unable to write log')
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
            finally:
                self.queue.task_done()

    async def close(self):
        if self.task is None:
            return
        self.stopped = True
        # Drain before sending the stop signal: an I/O failure discards queued data.
        await self.queue.join()
        await self.queue.put(None)
        await self.task
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.recorder.close)
        finally:
            self.executor.shutdown(wait=False)
            self.task = None
