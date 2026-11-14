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
        self.emit(200)
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

    def test_a_change_after_the_last_event_is_delivered_once_it_settles(self):
        first = self.subscribe(); self.ack(first)
        self.emit(); self.stream.pump(); self.ack(self.sent[-1])
        self.hh.mmu.gate_selected = 1
        self.stream.pump(); self.assertEqual(len(self.sent), 1)
        self.hh.mmu.reactor.advance(.5)
        self.stream.pump()
        self.assertEqual([r['event'] for r in self.sent[-1]['records']], ['state_delta'])

    def test_a_failed_settle_read_gets_the_capture_replaced(self):
        from unittest import mock
        first = self.subscribe(); self.ack(first)
        failed = self.stream.capture
        self.emit()
        with mock.patch.object(failed, 'state_provider', side_effect=RuntimeError('boom')):
            self.hh.mmu.reactor.advance(.5)
        self.assertTrue(failed.failed)
        self.stream.pump()
        self.assertIsNot(self.stream.capture, failed)

    def test_acknowledged_records_are_folded_so_only_the_unacknowledged_tail_is_kept(self):
        first = self.subscribe(); self.ack(first)
        self.emit(40)
        self.stream.pump()
        self.assertEqual(len(self.stream.capture.records), 40)
        self.ack(self.sent[-1])
        self.assertEqual(len(self.stream.capture.records), 0)
        self.emit(); self.stream.pump()
        self.assertEqual(self.sent[-1]['reset'], False)
        self.assertEqual([r['event'] for r in self.sent[-1]['records']], ['cut'])

    def test_a_consumer_that_has_not_acknowledged_pins_the_records(self):
        first = self.subscribe('a'); self.ack(first, 'a')
        self.subscribe('b')                                   # Never acknowledges its baseline
        self.emit(5); self.stream.pump()
        self.ack(self.sent[-1], 'a')
        self.assertEqual(len(self.stream.capture.records), 5)

    def test_a_late_subscriber_gets_the_current_state_without_folded_events(self):
        first = self.subscribe(); self.ack(first)
        self.hh.mmu.gate_selected = 1
        self.emit(); self.stream.pump(); self.ack(self.sent[-1])
        late = self.stream.subscribe('late', self.sent.append, lambda: False)
        self.assertEqual([r['event'] for r in late['records']], ['session'])
        self.assertEqual(late['records'][0]['state']['printer']['mmu']['gate'], 1)

    def test_a_stalled_consumer_gets_a_new_baseline_once_the_cap_is_passed(self):
        first = self.subscribe(); self.ack(first)
        self.emit(300)
        self.stream.pump()
        self.assertTrue(self.sent[-1]['reset'])
        self.assertLessEqual(len(self.stream.capture.records), self.stream.capture.limit)

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
