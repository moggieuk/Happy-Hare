import unittest

from extras.mmu.unit.nfc.pn532_driver import (
    PN532Driver, _PN532Base, _parse_inlist_payload,
)
from extras.mmu.unit.nfc.pn7160_driver import (
    PN7160I2CStatusError, _frame_summary, _gid, _hex, _message_type, _oid,
)
from extras.mmu.unit.nfc.rc522_driver import (
    _rc522_parse_byte, _rc522_parse_hex_bytes, _rc522_reg_value,
)


class TestPn532Protocol(unittest.TestCase):
    def test_inlist_payload(self):
        parsed = _parse_inlist_payload([1, 2, 0x04, 0x00, 0x08, 4,
                                        0xDE, 0xAD, 0xBE, 0xEF])
        self.assertEqual(('DEADBEEF', 2, 0x0400, 0x08),
                         (parsed['uid'], parsed['target'], parsed['atqa'],
                          parsed['sak']))
        for malformed in (None, [], [0], [1, 2], [1, 1, 4, 0, 8, 0]):
            self.assertIsNone(_parse_inlist_payload(malformed))

    def test_command_frame_checksum(self):
        frame = _PN532Base._build_frame([0x02])
        self.assertEqual([0, 0, 0xFF], frame[:3])
        self.assertEqual(0, (frame[3] + frame[4]) & 0xFF)
        self.assertEqual(0, sum(frame[5:-1]) & 0xFF)
        self.assertEqual(0, frame[-1])

    def test_i2c_response_frame_parsing(self):
        payload = [0xD5, 0x03, 0x32, 0x01, 0x06, 0x07]
        length = len(payload)
        frame = [1, 0, 0, 0xFF, length, (-length) & 0xFF] + payload
        frame += [(-sum(payload)) & 0xFF, 0]
        self.assertEqual(payload[2:], PN532Driver._check_frame(frame, 0x03))


class TestPn7160Protocol(unittest.TestCase):
    def test_header_helpers_and_summary(self):
        frame = [0x41, 0x05, 0x01, 0x00]
        self.assertEqual((2, 1, 5),
                         (_message_type(frame), _gid(frame), _oid(frame)))
        self.assertIn('len=4', _frame_summary(frame))
        self.assertEqual('AA 01', _hex([0xAA, 1]))
        self.assertIn('short', _frame_summary([1]))

    def test_status_error_preserves_context(self):
        error = PN7160I2CStatusError(3, [1, 2], 'CORE_INIT')
        self.assertEqual((3, [1, 2], 'CORE_INIT'),
                         (error.status, error.response, error.label))
        self.assertIn('CORE_INIT', str(error))


class TestRc522Parsers(unittest.TestCase):
    def test_numeric_and_hex_parsers(self):
        self.assertEqual(0x2A, _rc522_reg_value('0x2a'))
        self.assertEqual(255, _rc522_parse_byte('FF'))
        self.assertEqual([0xDE, 0xAD, 0xBE, 0xEF],
                         _rc522_parse_hex_bytes('DE AD:BE-EF'))
        self.assertEqual(0, _rc522_parse_byte('256'))


if __name__ == '__main__':
    unittest.main()
