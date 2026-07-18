import unittest
from unittest.mock import MagicMock, patch

from extras.mmu.unit.nfc.gate_state import (
    DIRECT_METADATA_SPOOL, EVENT_CHANGED, EVENT_REMOVED, EVENT_UID_ONLY,
    CurrentTag, GateState,
)
from extras.mmu.unit.nfc.klipper_interface import KlipperInterface
from test.nfc.harness import FakePrinter, FakeReactor


class TestGateState(unittest.TestCase):
    def test_new_resolved_tag_and_duplicate(self):
        state = GateState(2)
        self.assertEqual((EVENT_CHANGED, 2, 'A1', 42),
                         state.process_read('A1', 42))
        self.assertIsInstance(state.current_tag, CurrentTag)
        self.assertEqual(42, state.current_tag.spool_id)
        self.assertIsNone(state.process_read('A1', 42))

    def test_uid_only_and_metadata_only(self):
        state = GateState(1)
        self.assertEqual((EVENT_UID_ONLY, 1, 'AA', None),
                         state.process_read('AA', None))
        self.assertEqual((EVENT_CHANGED, 1, 'BB', None),
                         state.process_read('BB', DIRECT_METADATA_SPOOL))
        self.assertIs(state.current_spool, DIRECT_METADATA_SPOOL)

    def test_absence_is_debounced_and_scan_misses_are_ignored(self):
        state = GateState(0, absent_threshold=2)
        state.process_read('AB', 7)
        self.assertIsNone(state.process_read(None, None, scan_mode=True))
        self.assertEqual(0, state.miss_count)
        self.assertIsNone(state.process_read(None, None))
        self.assertEqual((EVENT_REMOVED, 0, None, 7),
                         state.process_read(None, None))
        self.assertIsNone(state.current_tag)

    def test_uid_change_replaces_tag_metadata_and_reset_clears_all(self):
        state = GateState(0)
        state.process_read('AA', 1)
        state.current_tag.meta['material'] = 'PLA'
        state.process_read('BB', 2)
        self.assertEqual({}, state.current_tag.meta)
        state.reset()
        self.assertEqual((None, None, None, 0),
                         (state.current_uid, state.current_spool,
                          state.current_tag, state.miss_count))


class TestKlipperInterface(unittest.TestCase):
    def setUp(self):
        self.gcode = MagicMock()
        self.mmu = MagicMock()
        self.mmu.action = None
        self.reactor = FakeReactor()
        self.subject = KlipperInterface(
            FakePrinter({'gcode': self.gcode, 'mmu': self.mmu}), self.reactor,
            name='lane_0', spoolman_enabled=False)

    def dispatch(self, *args, **kwargs):
        self.subject.dispatch(*args, **kwargs)
        self.assertEqual(1, len(self.reactor.callbacks))
        self.reactor.run_callbacks()
        return [c.args[0] for c in self.gcode.run_script_from_command.call_args_list]

    def test_resolved_change_writes_gate_map_directly(self):
        scripts = self.dispatch(EVENT_CHANGED, 3, 'AABB', 17, auto_created=True)
        self.assertIn('MMU_GATE_MAP GATE=3 SPOOLID=17 AVAILABLE=1 QUIET=1', scripts)
        self.assertIn('MMU_SPOOLMAN REFRESH=1 QUIET=1', scripts)
        self.assertIn('MMU_SPOOLMAN SYNC=1 QUIET=1', scripts)
        self.gcode.respond_info.assert_called_once()
        self.assertIn('spool 17 detected',
                      self.gcode.respond_info.call_args.args[0])

    def test_metadata_change_sanitizes_gate_map_values(self):
        scripts = self.dispatch(EVENT_CHANGED, 1, 'AA', None, meta={
            'brand': 'Bambu Lab', 'material': 'PLA / Basic',
            'color_hex': '#ff00aa', 'min_temp': 190.9, 'max_temp': 220,
            'diameter_mm': 1.75, 'weight_g': 1000.5,
        })
        gate_map_script = next(s for s in scripts if s.startswith('MMU_GATE_MAP'))
        self.assertIn('NAME=Bambu_PLA__Basic', gate_map_script)
        self.assertIn('MATERIAL=PLA__Basic', gate_map_script)
        self.assertIn('COLOR=#ff00aa', gate_map_script)
        self.assertIn('TEMP=220', gate_map_script)
        self.assertIn('MMU_SPOOLMAN SYNC=1 QUIET=1', scripts)

    def test_uid_only_writes_unknown_gate_map(self):
        scripts = self.dispatch(EVENT_UID_ONLY, 0, 'CAFE', None)
        self.assertIn(
            'MMU_GATE_MAP GATE=0 SPOOLID=-1 NAME=Unknown MATERIAL=Unknown '
            'COLOR=FFFFFF55 TEMP=0 AVAILABLE=1 QUIET=1', scripts)
        self.assertIn('no rich metadata or spool assignment',
                      self.gcode.respond_info.call_args.args[0])

    def test_removed_clears_gate_map(self):
        scripts = self.dispatch(EVENT_REMOVED, 0, None, 9)
        self.assertIn('MMU_GATE_MAP GATE=0 SPOOLID=-1 AVAILABLE=0 QUIET=1', scripts)
        self.assertIn('MMU_SPOOLMAN SYNC=1 QUIET=1', scripts)
        self.assertIn('spool removed', self.gcode.respond_info.call_args.args[0])

    def test_removed_during_busy_action_is_ignored(self):
        from extras.mmu.mmu_constants import ACTION_UNLOADING
        self.mmu.action = ACTION_UNLOADING
        scripts = self.dispatch(EVENT_REMOVED, 0, None, 9)
        self.assertEqual([], scripts)
        self.assertIn('ignoring removal', self.gcode.respond_info.call_args.args[0])

    def test_unknown_event_and_gcode_failure_do_not_escape(self):
        with patch('extras.mmu.unit.nfc.klipper_interface.logger'):
            self.subject._update_gate_map('bad-event', 0, None, None)
        self.gcode.run_script_from_command.assert_not_called()
        self.gcode.run_script_from_command.side_effect = RuntimeError('boom')
        with patch('extras.mmu.unit.nfc.klipper_interface.logger'):
            self.subject._update_gate_map(EVENT_REMOVED, 0, None, 2)


if __name__ == '__main__':
    unittest.main()
