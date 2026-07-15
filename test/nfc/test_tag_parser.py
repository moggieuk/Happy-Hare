import json
import unittest

from extras.mmu.unit.nfc import tag_parser as parser


def short_ndef(record_type, payload, tnf=1):
    record = bytes([0xD0 | tnf, len(record_type), len(payload)]) + record_type + payload
    return bytes([0x03, len(record)]) + record + b'\xFE'


class TestNdefAndCbor(unittest.TestCase):
    def test_tlv_short_extended_and_malformed(self):
        self.assertEqual(b'abc', parser._find_ndef_tlv(b'\x00\x03\x03abc\xFE'))
        self.assertEqual(b'abc', parser._find_ndef_tlv(b'\x03\xFF\x00\x03abc'))
        self.assertIsNone(parser._find_ndef_tlv(b'\x03\x05abc'))
        self.assertIsNone(parser._find_ndef_tlv(b'\xFE'))

    def test_text_and_uri_records(self):
        text = short_ndef(b'T', b'\x02enHello')
        records = parser._parse_ndef_records(parser._find_ndef_tlv(text))
        self.assertEqual(['Hello'], parser._extract_text_from_records(records))
        uri = short_ndef(b'U', b'\x04example.com/path')
        records = parser._parse_ndef_records(parser._find_ndef_tlv(uri))
        self.assertEqual(['https://example.com/path'],
                         parser._extract_text_from_records(records))

    def test_cbor_core_types(self):
        value, end = parser._cbor_decode(
            b'\xA3\x61a\x01\x61b\x82\xF5\xF6\x61c\x43xyz')
        self.assertEqual({'a': 1, 'b': [True, None], 'c': b'xyz'}, value)
        self.assertEqual(15, end)
        with self.assertRaises(ValueError):
            parser._cbor_decode(b'')


class TestPublicTagParsing(unittest.TestCase):
    def test_elegoo_binary(self):
        raw = bytearray(24)
        raw[:5] = b'\x36\xEE\xEE\xEE\xEE'
        raw[7:15] = b'PLA PETG'
        raw[15:18] = bytes.fromhex('12ABEF')
        raw[18:22] = b'\x00\xAF\x03\xE8'
        info = parser.parse_tag(raw, 'AABB')
        self.assertEqual('elegoo', info['tag_format'])
        self.assertEqual(('PLA-PETG', '12ABEF', 1.75, 1000),
                         (info['material'], info['color_hex'],
                          info['diameter_mm'], info['weight_g']))

    def test_generic_ndef_json(self):
        payload = b'\x02en' + json.dumps({
            'material': 'PETG', 'brand': 'Acme', 'color_hex': '#aabbcc',
            'diameter_mm': 1.75, 'weight_g': 750,
        }).encode()
        info = parser.parse_tag(short_ndef(b'T', payload), '1234')
        self.assertEqual('generic_ndef_json', info['tag_format'])
        self.assertEqual(('PETG', 'AABBCC'),
                         (info['material'], info['color_hex']))

    def test_unknown_inputs_and_parse_error_contract(self):
        self.assertIsNone(parser.parse_tag(None))
        self.assertIsNone(parser.parse_tag(b'not a supported tag'))
        self.assertFalse(parser.is_parse_error(None))
        self.assertTrue(parser.is_parse_error({'error': 'bad tag'}))

    def test_trace_callback_failure_is_isolated(self):
        def broken_trace(*_args):
            raise RuntimeError('observer failed')
        self.assertIsNone(parser.parse_tag(b'unknown', trace=broken_trace))


if __name__ == '__main__':
    unittest.main()
