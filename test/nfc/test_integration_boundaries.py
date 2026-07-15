import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from extras.mmu.unit.nfc.reader_resolver import resolve_gate_reader
from extras.mmu.unit.nfc.scan_jog import (
    chunk_interval, corrected_homing_actual, distance_from_trapezoid_time,
)
from extras.mmu.unit.nfc.spoolman_client import SpoolmanClient
from test.nfc.harness import ConfigError, FakePrinter, fake_gate


class TestReaderResolver(unittest.TestCase):
    def make_unit(self, readers=None, reader=None, num_gates=2):
        return SimpleNamespace(first_gate=4, nfc_readers=readers,
                               nfc_reader=reader, num_gates=num_gates,
                               name='unit0')

    def test_resolves_global_gate_to_local_reader(self):
        reader = object()
        unit = self.make_unit(['reader left', 'reader right'])
        mmu = SimpleNamespace(mmu_machine=MagicMock())
        mmu.mmu_machine.get_mmu_unit_by_gate.return_value = unit
        result = resolve_gate_reader(
            FakePrinter({'reader right': reader}), mmu, 5, 'lane5', [])
        self.assertEqual((reader, unit, 'reader right'), result)

    def test_single_gate_singular_reader_is_allowed(self):
        reader = object()
        unit = self.make_unit(None, 'reader only', 1)
        mmu = SimpleNamespace(mmu_machine=MagicMock())
        mmu.mmu_machine.get_mmu_unit_by_gate.return_value = unit
        self.assertIs(reader, resolve_gate_reader(
            FakePrinter({'reader only': reader}), mmu, 4, 'lane4', [])[0])

    def test_missing_mmu_unit_reader_and_duplicate_are_rejected(self):
        printer = FakePrinter()
        with self.assertRaisesRegex(ConfigError, 'mmu.*not available'):
            resolve_gate_reader(printer, None, 0, 'lane', [])
        mmu = SimpleNamespace(mmu_machine=MagicMock())
        mmu.mmu_machine.get_mmu_unit_by_gate.return_value = None
        with self.assertRaisesRegex(ConfigError, 'does not belong'):
            resolve_gate_reader(printer, mmu, 0, 'lane', [])

        reader = object()
        unit = self.make_unit(['reader left', 'reader right'])
        mmu.mmu_machine.get_mmu_unit_by_gate.return_value = unit
        other = SimpleNamespace(_name='other', _shared=False,
                                _reader_object=reader)
        with self.assertRaisesRegex(ConfigError, 'already assigned'):
            resolve_gate_reader(FakePrinter({'reader left': reader}), mmu,
                                4, 'lane', [other])


class TestScanMath(unittest.TestCase):
    def test_trapezoid_distance_boundaries_and_sign(self):
        self.assertEqual(0, distance_from_trapezoid_time(100, 0, 50, 100))
        self.assertEqual(100, distance_from_trapezoid_time(100, 99, 50, 100))
        distances = [distance_from_trapezoid_time(100, t, 50, 100)
                     for t in (0.1, 0.5, 1.0, 2.0)]
        self.assertEqual(sorted(distances), distances)

    def test_chunk_interval_uses_safe_speed(self):
        unit = SimpleNamespace(p=SimpleNamespace(gear_short_move_speed=0))
        mmu = MagicMock()
        mmu.drive.return_value.mmu_unit = unit
        gate = fake_gate(printer=FakePrinter({'mmu': mmu}), _gate=2)
        self.assertEqual(0.5, chunk_interval(gate, -40))

    def test_corrects_full_distance_homing_report(self):
        gate = fake_gate(_scan_continuous_accel=100, _name='lane')
        corrected = corrected_homing_actual(
            gate, 100, 100, elapsed=0.5, speed=50, accel=100)
        self.assertAlmostEqual(12.5, corrected)
        self.assertEqual(8, corrected_homing_actual(
            gate, 100, 8, elapsed=0.5, speed=50, accel=100))


class TestSpoolmanClient(unittest.TestCase):
    def setUp(self):
        self.client = SpoolmanClient('http://spoolman/', cache_ttl=30)

    def test_normalization_and_record_matching(self):
        self.assertEqual('AABBCC', self.client._normalise_uid('aa:bb-cc'))
        spools = [{'id': 1, 'extra': {'rfid': '"AA-BB"'}},
                  {'id': 2, 'extra': {}}]
        self.assertEqual(1, self.client._find_spool_record_by_uid(
            spools, 'aa:bb')['id'])

    def test_lookup_uses_detail_and_cache(self):
        self.client._fetch_spools = MagicMock(return_value=[{
            'id': 7, 'extra': {'rfid': '"ABCD"'}}])
        self.client._fetch_spool_detail = MagicMock(return_value={
            'id': 7, 'extra': {'rfid': '"ABCD"'}, 'remaining_weight': 50})
        self.assertEqual(7, self.client.lookup_spool_by_uid('ab-cd'))
        self.assertEqual(7, self.client.lookup_spool_by_uid('ABCD'))
        self.client._fetch_spools.assert_called_once()

    def test_set_uid_encodes_spoolman_extra_and_invalidates_cache(self):
        self.client._cache['AABB'] = ({'id': 3}, 999999999)
        self.client._patch_spool = MagicMock(return_value=True)
        self.assertTrue(self.client.set_spool_uid(3, 'AA-BB'))
        self.client._patch_spool.assert_called_once_with(
            3, {'extra': {'rfid': json.dumps('AA-BB')}}, plural=False)
        self.assertNotIn('AABB', self.client._cache)
        with patch('extras.mmu.unit.nfc.spoolman_client.logger'):
            self.assertFalse(self.client.set_spool_uid(None, 'AA'))

    def test_get_uid_for_spool_handles_json_quotes(self):
        self.client._fetch_spool_detail = MagicMock(return_value={
            'extra': {'rfid': '"de:ad:be:ef"'}})
        self.assertEqual('DEADBEEF', self.client.get_uid_for_spool(9))


if __name__ == '__main__':
    unittest.main()
