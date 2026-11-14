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

    def test_a_recording_mode_change_commits_the_settled_state_then_tells_consumers_in_order(self):
        first = self.subscribe(); self.ack(first)
        self.hh.mmu.gate_selected = 1
        self.assertFalse(self.stream.set_record_mode('on'))   # A consumer is already subscribed
        self.stream.pump()
        records = self.sent[-1]['records']
        self.assertEqual([r['event'] for r in records], ['state_delta', 'record_mode'])
        self.assertEqual(records[1]['mode'], 'on')
        changes = {tuple(c['path']): c['value'] for c in records[0]['changes']}
        self.assertEqual(changes[('printer', 'mmu', 'gate')], 1)
        self.assertEqual(changes[('printer', 'mmu', 'telemetry', 'record')], 'on')

    def test_a_subscriber_after_recording_was_requested_learns_it_with_its_baseline(self):
        self.assertTrue(self.stream.set_record_mode('toolchange'))     # Nobody subscribed: Moonraker must be asked
        batch = self.subscribe()
        self.assertEqual([r['event'] for r in batch['records']], ['session', 'record_mode'])
        self.assertEqual(batch['records'][0]['state']['printer']['mmu']['telemetry']['record'], 'toolchange')

    def test_one_toolchange_mode_ends_with_its_own_command_only(self):
        self.stream.set_record_mode('toolchange'); self.subscribe()
        telemetry = self.hh.mmu.movement_telemetry
        telemetry.toolchange_end(command='earlier')            # Armed while that command was running
        self.assertEqual(self.stream.record_mode, 'toolchange')
        telemetry.toolchange_start(command='MMU_LOAD'); telemetry.toolchange_end(command='MMU_LOAD')
        self.assertEqual(self.stream.record_mode, 'off')

    def test_unknown_recording_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self.stream.set_record_mode('sometimes')

    def test_the_default_telemetry_mode_is_off_and_a_configured_one_applies_at_startup(self):
        from test.hh import profiles
        self.assertEqual((self.hh.mmu.p.default_telemetry_mode, self.hh.mmu.telemetry_api.stream.record_mode), ('off', 'off'))
        base = profiles.get('boxturtle')
        configured = profiles.Profile('boxturtle_telemetry_on', syms={
            **base.syms, 'CHOICE_DEFAULT_TELEMETRY_MODE_ON': True})
        with session(configured) as hh:
            hh.boot(calibrate=True)
            self.assertEqual(hh.mmu.p.default_telemetry_mode, 'on')
            self.assertEqual(hh.mmu.telemetry_api.stream.record_mode, 'on')

    def test_default_telemetry_mode_is_hidden_from_the_runtime_parameter_listing(self):
        self.assertNotIn('default_telemetry_mode', self.hh.mmu.p.format_params())
        self.assertIn('default_telemetry_mode', self.hh.mmu.p.format_params(include_hidden=True))

    def test_mmu_telemetry_command_sets_and_reports_the_mode(self):
        webhooks = self.hh.printer.lookup_object('webhooks')
        self.hh.run_gcode('MMU_TELEMETRY MODE=on')
        self.assertEqual(self.hh.mmu.telemetry_api.stream.record_mode, 'on')
        self.assertEqual([c[0] for c in webhooks.calls if c[0] == 'mmu_telemetry_wake'], ['mmu_telemetry_wake'])
        self.hh.run_gcode('MMU_TELEMETRY MODE=off')
        self.assertEqual(self.hh.mmu.telemetry_api.stream.record_mode, 'off')
        self.hh.run_gcode('MMU_TELEMETRY')                        # Reports only
        self.assertEqual(self.hh.mmu.telemetry_api.stream.record_mode, 'off')
        with self.assertRaises(Exception):
            self.hh.run_gcode('MMU_TELEMETRY MODE=sometimes')

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
