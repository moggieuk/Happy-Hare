# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify replay checkpoints and independently loadable rotated logs.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import json
from pathlib import Path
import tempfile
import unittest
from components.mmu_telemetry_store import TelemetryHistory, TelemetryRecorder


HEADER = dict(event='session', schema='happy_hare.motion', version=2,
              session_id='test', sequence=0, timestamp=0, state={'parameter': 10})

def event(seq, kind='cut', **fields):
    return dict(sequence=seq, timestamp=seq, event=kind, **fields)


class TestTelemetryStore(unittest.TestCase):
    def test_latest_operation_checkpoint_includes_edited_parameters(self):
        history = TelemetryHistory()
        history.append(dict(records=[HEADER, event(1, 'toolchange'), event(2, 'state_delta',
            changes=[dict(path=['parameter'], value=20)]), event(3, 'toolchange'), event(4)]))
        latest = history.read()
        self.assertEqual([r['sequence'] for r in latest['records']], [2, 3, 4])
        self.assertEqual(latest['records'][0]['state'], {'parameter': 20})
        self.assertEqual(history.read(history='start')['records'][0]['state'], {'parameter': 10})

    def test_rollover_checkpoint_and_pending_cancellation(self):
        history = TelemetryHistory(2)
        history.append(dict(records=[HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=30)]),event(2),event(3)]))
        self.assertEqual(history.read(0)['records'][0]['state'], {'parameter':30})
        history.append(dict(records=[event(4, 'state_delta', pending=True, changes=[])]))
        self.assertTrue(history.read(3)['records'][0]['pending'])
        history.append(dict(records=[]))
        self.assertEqual(history.read(3)['records'], [])

    def test_each_rotated_file_has_current_baseline_and_startup_preserves_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 1, 3)
            recorder.write([HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=40)]), event(2)])
            recorder.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]['state'], {'parameter':40})
            self.assertEqual([r['sequence'] for r in records], [1,2])
            recorder = TelemetryRecorder(path, 10000, 3)
            recorder.write([HEADER]); recorder.close()
            self.assertEqual(json.loads(Path(str(path)+'.1').read_text().splitlines()[0])['state'], {'parameter':40})

    def test_size_rotation_keeps_movement_pair_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 1, 3)
            recorder.write([HEADER, event(1, 'movement_start', move_id=1), event(2),
                            event(3, 'movement_outcome', move_id=1)])
            recorder.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['sequence'] for r in records], [0,1,2,3])


class TestTelemetryWriter(unittest.IsolatedAsyncioTestCase):
    async def test_close_during_failed_write_does_not_lose_stop_signal(self):
        import asyncio
        import threading
        from components.mmu_telemetry_store import TelemetryWriter
        entered, release = threading.Event(), threading.Event()
        warnings = []
        class BrokenRecorder:
            closed = False
            def write(self, records):
                entered.set()
                release.wait(2)
                raise OSError('disk failed')
            def close(self): self.closed = True
        recorder = BrokenRecorder()
        writer = TelemetryWriter(recorder, warnings.append)
        writer.submit([HEADER])
        while not entered.is_set():
            await asyncio.sleep(.001)
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 2)
        self.assertTrue(recorder.closed)
        self.assertEqual(warnings, ['unable to write log'])
        await writer.close()
