# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Verify acknowledged telemetry delivery and subscriber isolation.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import copy
import unittest
from test.hh import session


class TestTelemetryStream(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot(calibrate=True)
        from extras.mmu.telemetry.stream import TelemetryStream
        self.stream = TelemetryStream(self.hh.mmu)
        self.sent = []

    def tearDown(self):
        self.stream.close()
        self.hh.close()

    def subscribe(self, key='viewer'):
        return self.stream.subscribe(key, self.sent.append, lambda: False)

    def ack(self, batch, key='viewer'):
        self.stream.ack(key, batch['stream_id'], batch['delivery_id'])

    def emit(self, count=1):
        for i in range(count):
            self.hh.mmu.movement_telemetry.state('cut')

    def test_lazy_capture_and_disconnect_cleanup(self):
        self.assertIsNone(self.stream.capture)
        batch = self.subscribe()
        self.assertEqual(batch['records'][0]['event'], 'session')
        self.stream.unsubscribe('viewer')
        self.assertIsNone(self.stream.capture)
        self.assertNotEqual(self.subscribe()['stream_id'], batch['stream_id'])

    def test_one_outstanding_batch_and_wrong_ack_ignored(self):
        first = self.subscribe()
        self.emit(300)
        for i in range(4): self.stream.pump()
        self.assertEqual(self.sent, [])
        self.stream.ack('viewer', first['stream_id'], -1)
        self.stream.pump()
        self.assertEqual(self.sent, [])
        self.ack(first)
        self.stream.pump()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0]['records']), 128)
        self.stream.pump()
        self.assertEqual(len(self.sent), 1)
        self.ack(self.sent[-1]); self.stream.pump()
        self.assertEqual(self.sent[-1]['records'][0]['sequence'], 129)

    def test_slow_client_gets_explicit_checkpoint_after_overflow(self):
        first = self.subscribe()
        self.stream.capture.limit = 3
        self.emit(10)
        self.ack(first); self.stream.pump()
        reset = self.sent[-1]
        self.assertTrue(reset['reset'])
        self.assertTrue(reset['records'][0]['checkpoint'])
        self.assertEqual(len(self.stream.capture.records), 3)

    def test_clients_are_independent(self):
        first = self.subscribe('a'); self.subscribe('b')
        self.emit(); self.ack(first, 'a'); self.stream.pump()
        self.assertEqual(len(self.sent), 1)
        self.stream.unsubscribe('a')
        self.assertIsNotNone(self.stream.capture)
        self.stream.unsubscribe('b')
        self.assertIsNone(self.stream.capture)

    def test_a_lost_acknowledgement_is_resent_after_the_timeout(self):
        from unittest import mock
        first = self.subscribe()
        self.emit()
        self.stream.pump(); self.assertEqual(self.sent, [])       # The first batch is still unacknowledged
        later = self.hh.mmu.reactor.monotonic() + 6
        with mock.patch.object(self.hh.mmu.reactor, 'monotonic', return_value=later):
            self.stream.pump()
        self.assertEqual(len(self.sent), 1)
        self.assertNotEqual(self.sent[0]['delivery_id'], first['delivery_id'])
        self.assertEqual([r['event'] for r in self.sent[0]['records']], ['session', 'cut'])

    def test_a_failed_capture_is_replaced_and_consumers_get_a_new_baseline(self):
        from unittest import mock
        first = self.subscribe(); self.ack(first)
        failed = self.stream.capture
        with mock.patch.object(failed, 'state_provider', side_effect=RuntimeError('boom')):
            self.emit()                                          # The source detaches the failing capture
        self.assertTrue(failed.failed)
        self.assertEqual(self.hh.mmu.movement_telemetry.listeners, [])
        self.stream.pump()
        self.assertIsNot(self.stream.capture, failed)
        self.assertNotEqual(self.sent[-1]['stream_id'], first['stream_id'])
        self.assertTrue(self.sent[-1]['reset'])
        self.emit()
        self.assertEqual(len(self.hh.mmu.movement_telemetry.listeners), 1)

    def test_closing_with_notice_tells_each_consumer(self):
        self.subscribe()
        self.stream.close(notify=True)
        self.assertEqual(self.sent[-1]['closed'], True)
        self.assertEqual(self.stream.clients, {})

    def test_idle_change_is_delivered_with_the_next_event(self):
        first = self.subscribe(); self.ack(first)
        self.hh.mmu.gate_selected = 1
        self.stream.pump(); self.assertEqual(self.sent, [])
        self.emit()
        self.stream.pump()
        self.assertEqual([r['event'] for r in self.sent[-1]['records']], ['state_delta', 'cut'])

    def test_idle_polling_does_not_copy_the_baseline(self):
        from unittest import mock
        first = self.subscribe(); self.ack(first)
        with mock.patch('copy.deepcopy', wraps=copy.deepcopy) as spy:
            for i in range(8): self.stream.pump()
        self.assertEqual(spy.call_count, 0)
        self.emit()
        with mock.patch('copy.deepcopy', wraps=copy.deepcopy) as spy:
            self.stream.pump()                            # New records are shared, not copied
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(len(self.sent), 1)

    def test_a_reset_copies_the_baseline_so_later_folds_do_not_change_it(self):
        self.subscribe()
        batch = self.stream.capture.read()
        before = copy.deepcopy(batch['records'][0]['state'])
        self.stream.capture.limit = 2
        self.emit(5)
        self.assertEqual(batch['records'][0]['state'], before)

    def test_current_checkpoint_matches_moonraker_history_without_old_events(self):
        from components.mmu_telemetry_store import TelemetryHistory
        self.subscribe()
        self.hh.mmu.gate_selected = 1
        self.emit()
        history = TelemetryHistory()
        history.append(self.stream.capture.read())
        for mode in ('start', 'current'):
            source = self.stream.capture.read(history=mode)
            self.assertEqual(source, history.read(history=mode))
        current = history.read(history='current')
        self.assertEqual(len(current['records']), 1)
        self.assertEqual(current['records'][0]['state']['printer']['mmu']['gate'], 1)
