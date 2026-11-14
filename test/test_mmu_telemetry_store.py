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


HEADER = dict(event='session', schema='happy_hare.motion', version=1,
              session_id='test', sequence=0, timestamp=0, state={'parameter': 10})

def event(seq, kind='cut', **fields):
    return dict(sequence=seq, timestamp=seq, event=kind, **fields)


class TestTelemetryStore(unittest.TestCase):
    def test_current_checkpoint_includes_edited_parameters(self):
        history = TelemetryHistory()
        history.append(dict(records=[HEADER, event(1, 'toolchange'), event(2, 'state_delta',
            changes=[dict(path=['parameter'], value=20)]), event(3, 'toolchange'), event(4)]))
        current = history.read(history='current')
        self.assertEqual([r['sequence'] for r in current['records']], [4])
        self.assertEqual(current['records'][0]['state'], {'parameter': 20})
        self.assertEqual(history.read()['records'][0]['state'], {'parameter': 10})

    def test_records_are_shared_but_the_checkpoint_is_a_copy(self):
        history = TelemetryHistory()
        record = event(1)
        history.append(dict(records=[HEADER, record]))
        batch = history.read(0)
        self.assertIs(batch['records'][0], record)
        batch = history.read()
        batch['records'][0]['state']['parameter'] = 99
        self.assertEqual(history.read()['records'][0]['state'], {'parameter': 10})

    def test_rollover_checkpoint(self):
        history = TelemetryHistory(2)
        history.append(dict(records=[HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=30)]),event(2),event(3)]))
        self.assertEqual(history.read(0)['records'][0]['state'], {'parameter':30})

    def test_each_rotated_file_has_current_baseline_and_startup_preserves_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 1, 3, 'on')
            recorder.write([HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=40)]), event(2)])
            recorder.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]['state'], {'parameter':40})
            self.assertEqual([r['sequence'] for r in records], [1,2])
            recorder = TelemetryRecorder(path, 10000, 3, 'on')
            recorder.write([HEADER]); recorder.close()
            self.assertEqual(json.loads(Path(str(path)+'.1').read_text().splitlines()[0])['state'], {'parameter':40})

    def test_toolchange_mode_records_one_command_from_its_start_to_its_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            path.write_text('older\n')
            recorder = TelemetryRecorder(path, 10000, 3, 'toolchange')
            recorder.write([HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=50)]), event(2),
                            event(3, 'toolchange'), event(4), event(5, 'state_delta', changes=[dict(path=['parameter'],value=60)]),
                            event(6, 'toolchange_end'), event(7)])
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['event'] for r in lines], ['session', 'toolchange', 'cut', 'state_delta', 'toolchange_end'])
            self.assertEqual(lines[0]['state'], {'parameter': 50})              # Known state at the start of the command
            self.assertEqual(lines[-2]['changes'][0]['value'], 60)                # The late delta precedes the end record
            self.assertEqual(Path(str(path)+'.1').read_text(), 'older\n')       # One slot in the rolling set
            self.assertEqual(recorder.mode, 'off')
            self.assertIsNone(recorder.file)

    def test_off_mode_writes_nothing_but_tracks_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 10000, 3)
            recorder.write([HEADER, event(1, 'state_delta', changes=[dict(path=['parameter'],value=70)])])
            self.assertFalse(path.exists())
            self.assertEqual(recorder.header['state'], {'parameter': 70})

    def test_record_mode_records_switch_the_recorder_in_stream_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 10000, 3)
            recorder.write([HEADER, event(1, 'record_mode', mode='on'), event(2), event(3, 'record_mode', mode='off'), event(4)])
            self.assertEqual([r['event'] for r in map(json.loads, path.read_text().splitlines())], ['session', 'cut'])
            self.assertEqual(recorder.mode, 'off')
            self.assertIsNone(recorder.file)

    def test_repeating_a_mode_does_not_rotate_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 10000, 3)
            recorder.write([HEADER, event(1, 'record_mode', mode='on'), event(2), event(3, 'record_mode', mode='on'), event(4)])
            self.assertEqual([r['event'] for r in map(json.loads, path.read_text().splitlines())], ['session', 'cut', 'cut'])
            self.assertFalse(Path(str(path)+'.1').exists())

    def test_a_new_baseline_inside_a_toolchange_segment_is_written_to_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'motion.jsonl'
            recorder = TelemetryRecorder(path, 10000, 3, 'toolchange')
            reset = dict(HEADER, state={'parameter': 80}, sequence=10, checkpoint=True)
            recorder.write([HEADER, event(1, 'toolchange'), reset, event(11), event(12, 'toolchange_end')])
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['event'] for r in lines], ['session', 'toolchange', 'session', 'cut', 'toolchange_end'])
            self.assertEqual(lines[2]['state'], {'parameter': 80})

    def test_size_rotation_keeps_movement_pair_together(self):
        for kind in ('movement', 'selector_movement'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.check_movement_pair_rotation(directory, kind)

    def check_movement_pair_rotation(self, directory, kind):
        path = Path(directory)/'motion.jsonl'
        recorder = TelemetryRecorder(path, 1, 3, 'on')
        recorder.write([HEADER, event(1, kind+'_start', move_id=1), event(2),
                        event(3, kind+'_outcome', move_id=1)])
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
