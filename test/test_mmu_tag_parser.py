# Happy Hare test harness - milestone D: the RFID tag parser.
#
# extras/mmu/unit/nfc/tag_parser.py is 2600 lines and ~11 tag formats, and none of it had
# ever run. It is also the easiest thing in the whole feature to test: parse_tag() is a
# PURE FUNCTION over bytes. No Klipper, no fakes, no reader, no filament model - which is
# why this file imports nothing from test/hh except the overlay needed to make
# `extras.mmu.unit.nfc` importable at all.
#
# Tag images are BUILT here rather than captured from hardware. That is a deliberate
# trade: a captured dump proves one real tag parses, whereas a constructed one lets us
# state exactly which byte made the difference - and for the NDEF formats the wire format
# is a published standard, so a hand-built image is just as real. Formats whose payloads
# are proprietary binary (Bambu, Creality, QIDI, Anycubic) are NOT synthesised here;
# faking those convincingly would mean reimplementing the format, and a test that only
# proves my encoder matches my decoder is worthless. Those need captured dumps - see the
# graceful-degradation tests at the bottom for what is checked in the meantime.
#
# NDEF structure being built (NFC Forum Type 2, as read from page 4 onward):
#     TLV:    0x03 <len> <ndef message> 0xFE
#     record: <flags|TNF> <type_len> <payload_len> <type> <payload>
#             flags: MB 0x80, ME 0x40, SR 0x10 (short record), IL 0x08
#             TNF:   0x01 well-known ('T' text, 'U' URI), 0x02 MIME
#
#   ./venv/bin/python -m unittest test.test_mmu_tag_parser
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import unittest

from test.hh import install

install()   # put the fake klippy tree on sys.path so `extras.*` resolves

from extras.mmu.unit.nfc import tag_parser  # noqa: E402


# ---- NDEF builders ---------------------------------------------------------

def ndef_tlv(*records):
    """Wrap NDEF records in a Type 2 TLV, as parse_tag expects from page 4 on."""
    message = b''.join(records)
    if len(message) < 0xFF:
        header = bytes([0x03, len(message)])
    else:
        header = bytes([0x03, 0xFF, (len(message) >> 8) & 0xFF, len(message) & 0xFF])
    return header + message + b'\xFE'


def _record(tnf, type_bytes, payload, first=True, last=True):
    flags = tnf & 0x07
    if first:
        flags |= 0x80       # MB
    if last:
        flags |= 0x40       # ME
    if len(payload) < 256:
        flags |= 0x10       # SR
        length = bytes([len(payload)])
    else:
        length = len(payload).to_bytes(4, 'big')
    return bytes([flags, len(type_bytes)]) + length + type_bytes + payload


def text_record(text, lang='en', **kw):
    """Well-known Text record: status byte carries the language-code length."""
    lang_bytes = lang.encode('ascii')
    payload = bytes([len(lang_bytes) & 0x3F]) + lang_bytes + text.encode('utf-8')
    return _record(0x01, b'T', payload, **kw)


def uri_record(uri, prefix=0x00, **kw):
    return _record(0x01, b'U', bytes([prefix]) + uri.encode('utf-8'), **kw)


def mime_record(mime_type, payload, **kw):
    return _record(0x02, mime_type.encode('ascii'), payload, **kw)


def openspool_tag(**overrides):
    body = {'protocol': 'openspool', 'version': '1.0', 'type': 'PLA',
            'brand': 'Overture', 'color_hex': '00FF00',
            'min_temp': 190, 'max_temp': 220}
    body.update(overrides)
    return ndef_tlv(text_record(json.dumps(body)))


class TagParserTestCase(unittest.TestCase):
    def parse(self, raw, uid_hex=None):
        return tag_parser.parse_tag(raw, uid_hex=uid_hex)


class TestNdefPlumbing(TagParserTestCase):
    """The TLV/record layer everything text-based depends on."""

    def test_no_ndef_returns_none(self):
        # NOT a multiple of 64: see TestBlankTagMisdetectedAsBambu for why that matters
        self.assertIsNone(self.parse(b'\x00' * 48))

    def test_empty_input_returns_none(self):
        self.assertIsNone(self.parse(b''))
        self.assertIsNone(self.parse(None))

    def test_terminator_before_any_ndef(self):
        self.assertIsNone(self.parse(b'\xFE' + b'\x00' * 32))

    def test_truncated_tlv_does_not_raise(self):
        """A short read mid-tag must degrade, not explode."""
        full = openspool_tag()
        for cut in range(1, len(full)):
            with self.subTest(cut=cut):
                self.parse(full[:cut])      # must not raise

    def test_long_form_tlv_length(self):
        """>=255 byte payloads use the 3-byte 0xFF length form."""
        padded = openspool_tag(brand='B' * 300)
        info = self.parse(padded)
        self.assertIsNotNone(info)
        self.assertEqual(info['material'], 'PLA')

    def test_utf16_text_record(self):
        body = json.dumps({'protocol': 'openspool', 'type': 'PETG'})
        lang = b'en'
        payload = bytes([0x80 | len(lang)]) + lang + body.encode('utf-16')
        raw = ndef_tlv(_record(0x01, b'T', payload))
        info = self.parse(raw)
        self.assertIsNotNone(info, 'the UTF-16 status bit was not honoured')
        self.assertEqual(info['material'], 'PETG')


class TestOpenSpool(TagParserTestCase):
    """Detection is `protocol: openspool` in a JSON text record."""

    def test_full_tag(self):
        info = self.parse(openspool_tag())
        self.assertEqual(info['tag_format'], 'openspool')
        self.assertEqual(info['material'], 'PLA')
        self.assertEqual(info['brand'], 'Overture')
        self.assertEqual(info['color_hex'], '00FF00')
        self.assertEqual(info['min_temp'], 190)
        self.assertEqual(info['max_temp'], 220)
        self.assertEqual(info['diameter_mm'], 1.75)   # defaulted

    def test_colour_hash_is_stripped_and_uppercased(self):
        info = self.parse(openspool_tag(color_hex='#00ff00'))
        self.assertEqual(info['color_hex'], '00FF00')

    def test_material_may_come_from_either_key(self):
        raw = ndef_tlv(text_record(json.dumps(
            {'protocol': 'openspool', 'material': 'ABS'})))
        self.assertEqual(self.parse(raw)['material'], 'ABS')

    def test_no_material_is_rejected(self):
        raw = ndef_tlv(text_record(json.dumps(
            {'protocol': 'openspool', 'brand': 'Nobody'})))
        self.assertIsNone(self.parse(raw))

    def test_brand_defaults_to_generic(self):
        raw = ndef_tlv(text_record(json.dumps(
            {'protocol': 'openspool', 'type': 'PLA'})))
        self.assertEqual(self.parse(raw)['brand'], 'Generic')

    def test_non_integer_temps_are_dropped_not_fatal(self):
        info = self.parse(openspool_tag(min_temp='hot', max_temp=None))
        self.assertEqual(info['material'], 'PLA')
        self.assertNotIn('min_temp', info)
        self.assertNotIn('max_temp', info)

    def test_nonstandard_quotes_are_normalised_and_flagged(self):
        """
        Smart quotes happen when a tag is written from a phone. The parser recovers and
        says so via parse_warning - assert both halves.
        """
        body = '{“protocol”: “openspool”, “type”: “PLA”}'
        info = self.parse(ndef_tlv(text_record(body)))
        self.assertIsNotNone(info, 'smart-quoted JSON was not recovered')
        self.assertEqual(info['material'], 'PLA')
        self.assertIn('parse_warning', info)


class TestGenericNdefJson(TagParserTestCase):
    """Fallback for a JSON text record with recognisable filament fields."""

    def test_material_and_brand(self):
        raw = ndef_tlv(text_record(json.dumps(
            {'material': 'PETG', 'brand': 'Prusament', 'color_hex': 'FF0000'})))
        info = self.parse(raw)
        self.assertEqual(info['tag_format'], 'generic_ndef_json')
        self.assertEqual(info['material'], 'PETG')
        self.assertEqual(info['brand'], 'Prusament')
        self.assertEqual(info['color_hex'], 'FF0000')

    def test_openspool_is_not_swallowed_by_the_generic_path(self):
        info = self.parse(openspool_tag())
        self.assertEqual(info['tag_format'], 'openspool',
                         'openspool must win over the generic JSON fallback')

    def test_json_without_filament_fields_is_rejected(self):
        raw = ndef_tlv(text_record(json.dumps({'hello': 'world', 'n': 1})))
        self.assertIsNone(self.parse(raw))

    def test_plain_text_is_not_a_tag(self):
        self.assertIsNone(self.parse(ndef_tlv(text_record('just a label'))))

    def test_json_array_is_rejected(self):
        self.assertIsNone(self.parse(ndef_tlv(text_record('[1, 2, 3]'))))


class TestSimplyPrintUrl(TagParserTestCase):
    """URI records: simplyprint.io, or any URL carrying the known query params."""

    def test_simplyprint_host(self):
        raw = ndef_tlv(uri_record(
            'simplyprint.io/f?m=PLA&c=00FF00&mint=195&maxt=225', prefix=0x04))
        info = self.parse(raw)
        self.assertIsNotNone(info, 'a simplyprint.io URI was not recognised')
        self.assertEqual(info['material'], 'PLA')
        self.assertEqual(info['tag_format'], 'simplyprint_url')

    def test_uri_prefix_byte_is_expanded(self):
        """Prefix 0x04 means https:// - the host check depends on it expanding."""
        raw = ndef_tlv(uri_record('simplyprint.io/f?m=ABS', prefix=0x04))
        self.assertIsNotNone(self.parse(raw))

    def test_unrelated_url_is_not_a_tag(self):
        raw = ndef_tlv(uri_record('example.com/hello', prefix=0x04))
        self.assertIsNone(self.parse(raw))


class TestUnparseableInputIsSafe(TagParserTestCase):
    """
    Robustness sweep. A reader hands parse_tag whatever the chip returned, including
    partial reads and noise, so no input may raise - the caller
    (MmuNfcReader._read_tag_metadata) treats a None as "no rich data" and carries on with
    the UID.
    """

    def test_random_noise_never_raises(self):
        import random
        rng = random.Random(20260727)
        for i in range(200):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 96)))
            with self.subTest(i=i):
                self.parse(blob)        # must not raise

    def test_all_byte_values_as_tlv_type(self):
        for t in range(256):
            with self.subTest(tlv_type=t):
                self.parse(bytes([t, 4, 1, 2, 3, 4, 0xFE]))

    def test_declared_length_beyond_buffer(self):
        self.parse(b'\x03\x40\x01\x02')             # says 64 bytes, has 2
        self.parse(b'\x03\xFF\xFF\xFF\x01\x02')     # long form, absurd length

    def test_is_parse_error_accepts_none(self):
        self.assertFalse(tag_parser.is_parse_error(None))
        self.assertFalse(tag_parser.is_bambu_tag(None))


class TestProprietaryFormatsDegrade(TagParserTestCase):
    """
    Bambu, Creality, QIDI and Anycubic use proprietary binary payloads, and the first two
    need AES/HKDF that pycryptodome provides. No convincing image can be synthesised
    without reimplementing the format, and no crypto library is installed here, so what is
    asserted is that these paths DEGRADE rather than crash or false-positive.

    Restoring real coverage needs captured dumps from actual spools - the natural next
    step for this file.
    """

    def test_block_input_shape_is_accepted(self):
        """The MIFARE dict form must be handled without raising."""
        blocks = {i: bytes(16) for i in range(4, 8)}
        self.parse({'uid_bytes': bytes.fromhex('04A1B2C3'), 'blocks': blocks})

    def test_empty_blocks_returns_none(self):
        self.assertIsNone(self.parse({'uid_bytes': b'\x04\x01\x02\x03', 'blocks': {}}))

    def test_zeroed_blocks_do_not_false_positive(self):
        """An unwritten card must not be reported as a recognised branded spool."""
        blocks = {i: bytes(16) for i in range(64)}
        info = self.parse({'uid_bytes': bytes.fromhex('04A1B2C3'), 'blocks': blocks})
        if info is not None:
            self.assertNotIn(info.get('tag_format'),
                             ('bambu', 'creality_cfs', 'qidi', 'anycubic_ace'),
                             'a blank card was claimed as a branded tag')

    def test_key_derivation_degrades_without_crypto(self):
        """
        Bambu key derivation needs HKDF. With no crypto library it must fail cleanly -
        an empty/short key list or a caught exception - not take the reader down.
        """
        try:
            keys = tag_parser._bambu_derive_keys(bytes.fromhex('04A1B2C3D4E5F6'))
        except Exception:
            return          # a raised exception is caught by _read_tag_metadata
        self.assertIsInstance(keys, list)


if __name__ == '__main__':
    unittest.main()


class TestBlankTagMisdetectedAsBambu(TagParserTestCase):
    """
    A blank tag is reported as a Bambu Lab tag, in the DEFAULT configuration.

    _detect_bambu is explicitly a heuristic - its own docstring says "Detection is
    unreliable from raw bytes alone ... best-effort heuristic only". It flags a candidate
    when the dump is a multiple of 64 bytes, contains no NDEF TLV, and holds none of the
    ASCII keywords PLA/ABS/PETG/TPU/spoolman_id/openspool. An all-zeros buffer satisfies
    all three.

    That is reachable with shipped defaults, not a contrived length: tag_max_pages
    defaults to 16 (extras/mmu/unit/nfc/mmu_nfc_reader.py:119), so an NTAG deep read
    returns 16 pages x 4 bytes = exactly 64 bytes. Scan a blank or non-filament NTAG and
    the user is told:

        "Detected Bambu Lab tag but decryption/authentication not available;
         see README for hardware requirements"

    BLAST RADIUS IS MESSAGING ONLY, and worth being precise about: the result is a parse
    ERROR dict, is_parse_error() is True, and MmuNfcReader._read_tag_metadata therefore
    returns None - so no incorrect filament data reaches the gate map. The cost is a
    misleading diagnostic pointing the user at Bambu hardware requirements they do not
    need.

    A cheap tightening would be to require at least some non-zero entropy before
    claiming an encrypted tag.
    """

    BLANK_NTAG_READ = bytes(64)     # 16 pages x 4 bytes, the shipped default

    def test_blank_tag_is_currently_claimed_as_bambu(self):
        """Pins today's behaviour so a fix shows up as a change here."""
        info = self.parse(self.BLANK_NTAG_READ)
        self.assertIsNotNone(info)
        self.assertEqual(info.get('tag_format'), 'bambu')
        self.assertIn('Bambu', info.get('error', ''))

    def test_the_misdetection_is_contained_to_messaging(self):
        """No wrong filament data escapes: it is an error dict, so metadata is dropped."""
        info = self.parse(self.BLANK_NTAG_READ)
        self.assertTrue(tag_parser.is_parse_error(info))
        self.assertNotIn('material', info)

    def test_default_page_count_produces_exactly_the_trigger_length(self):
        """Documents WHY this is the default path rather than an edge case."""
        self.assertEqual(len(self.BLANK_NTAG_READ) % 64, 0)
        self.assertEqual(16 * 4, 64)

    @unittest.expectedFailure
    def test_blank_tag_should_not_be_claimed_as_bambu(self):
        """
        What should happen. Flips green if the heuristic gains an entropy check; delete
        this and invert test_blank_tag_is_currently_claimed_as_bambu then.
        """
        self.assertFalse(tag_parser._detect_bambu(self.BLANK_NTAG_READ))

    def test_a_written_ndef_tag_is_never_mistaken_for_bambu(self):
        """The NDEF escape hatch works, which is what keeps real tags safe."""
        self.assertFalse(tag_parser._detect_bambu(openspool_tag()))
