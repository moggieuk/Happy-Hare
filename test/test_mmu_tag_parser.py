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
# are proprietary binary (Bambu, Creality, QIDI, Anycubic) are NOT synthesized here;
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
import logging
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
    """Fallback for a JSON text record with recognizable filament fields."""

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
        self.assertIsNotNone(info, 'a simplyprint.io URI was not recognized')
        self.assertEqual(info['material'], 'PLA')
        self.assertEqual(info['tag_format'], 'simplyprint_url')

    def test_uri_prefix_byte_is_expanded(self):
        """Prefix 0x04 means https:// - the host check depends on it expanding."""
        raw = ndef_tlv(uri_record('simplyprint.io/f?m=ABS', prefix=0x04))
        self.assertIsNotNone(self.parse(raw))

    def test_unrelated_url_is_not_a_tag(self):
        raw = ndef_tlv(uri_record('example.com/hello', prefix=0x04))
        self.assertIsNone(self.parse(raw))


def tigertag_image(magic, material_id=38219, brand_id=0, rgb=(0x61, 0xA3, 0x5D), weight_g=1000,
                   nozzle=(240, 270), bed=(80, 100), type_id=0x8E, diameter_id=56):
    """TigerTag user memory from page 4: the 32-byte core plus a zero twin-tag id."""
    import struct
    raw = bytearray(36)
    struct.pack_into(">I", raw, 0, magic)
    struct.pack_into(">I", raw, 4, 0xFFFFFFFF)          # no product id
    struct.pack_into(">H", raw, 8, material_id)
    raw[12], raw[13] = type_id, diameter_id             # default: filament, 1.75 mm
    struct.pack_into(">H", raw, 14, brand_id)
    raw[16:20] = bytes(rgb) + b"\xFF"
    raw[20:23] = weight_g.to_bytes(3, "big")
    raw[23] = 21                                        # grams
    struct.pack_into(">HH", raw, 24, *nozzle)
    raw[30], raw[31] = bed
    return bytes(raw)


class TestTigerTagVersions(TagParserTestCase):
    """Only the version IDs in TigerTag's registry (database/id_version.json) are TigerTags."""

    def test_registry_versions_parse(self):
        for magic, name in ((0x5BF59264, "TigerTag"), (0xBC0FCB97, "TigerTag+")):
            info = self.parse(tigertag_image(magic))
            self.assertEqual(info.get("tag_format"), "tigertag", hex(magic))
            self.assertEqual(info.get("tigertag_version"), name)
            self.assertEqual(info.get("color_hex"), "61A35D")
            self.assertEqual(info.get("weight_g"), 1000)
            self.assertEqual((info.get("min_temp"), info.get("max_temp")), (240, 270))

    def test_blank_init_chip_is_not_a_filament(self):
        self.assertIsNone(tag_parser._try_tigertag(tigertag_image(0x6C41A2E1)))
        self.assertTrue(tag_parser._has_tigertag_magic(tigertag_image(0x6C41A2E1)))

    def test_ids_outside_the_registry_are_not_tigertags(self):
        """0x5BF04674, 0xBC0A5927 and 0x6C2B2DF1 were never issued by TigerTag."""
        for magic in (0x5BF04674, 0xBC0A5927, 0x6C2B2DF1, 0x00000000):
            raw = tigertag_image(magic)
            self.assertFalse(tag_parser._has_tigertag_magic(raw), hex(magic))
            self.assertIsNone(tag_parser._try_tigertag(raw), hex(magic))
            info = self.parse(raw)
            self.assertNotEqual((info or {}).get("tag_format"), "tigertag", hex(magic))


class TestTigerTagTables(TagParserTestCase):
    """Tables generated from TigerTag's registry by utils/update_tigertag_tables.py."""

    def test_brand_and_material_added_by_the_registry(self):
        info = self.parse(tigertag_image(0x5BF59264, material_id=48469, brand_id=628))
        self.assertEqual((info.get("material"), info.get("brand")), ("PEEK-CF", "Buddy3D"))

    def test_existing_material_names_are_kept(self):
        """TigerTag renamed some materials; a gate's MATERIAL mustn't change under users."""
        for material_id, name in ((24629, "PLA-HS"), (11506, "PLA-LW"), (7649, "PETG-HS")):
            self.assertEqual(self.parse(tigertag_image(0x5BF59264, material_id=material_id)).get("material"), name)

    def test_type_and_diameter_come_from_the_registry_tables(self):
        info = self.parse(tigertag_image(0x5BF59264, type_id=173, diameter_id=221))
        self.assertEqual((info.get("tigertag_type"), info.get("diameter_mm")), ("Resin", 2.85))
        info = self.parse(tigertag_image(0x5BF59264))
        self.assertEqual((info.get("tigertag_type"), info.get("diameter_mm")), ("Filament", 1.75))

    def test_unknown_type_and_diameter_are_left_out(self):
        info = self.parse(tigertag_image(0x5BF59264, type_id=7, diameter_id=9))
        self.assertNotIn("tigertag_type", info)
        self.assertNotIn("diameter_mm", info)
        self.assertEqual(info.get("tigertag_diameter_id"), 9)

    def test_placeholder_ids_give_no_name(self):
        info = self.parse(tigertag_image(0x5BF59264, material_id=65535, brand_id=65535))
        self.assertNotIn("material", info)
        self.assertNotIn("brand", info)


class TestTigerTagDownloadedTables(TagParserTestCase):
    """Moonraker's downloaded registry (config/mmu/optional/tigertag), added once at startup."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        # load_tigertag_registry adds to the module tables: restore them after each test
        self.saved = [(t, dict(t)) for t in (getattr(tag_parser, s.attr) for s in tag_parser._tigertag_registry.TABLES)]

    def tearDown(self):
        import shutil
        for table, copy in self.saved:
            table.clear()
            table.update(copy)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, rows):
        import os
        with open(os.path.join(self.dir, name + ".json"), "w", encoding="utf-8") as f:
            f.write(rows if isinstance(rows, str) else json.dumps(rows))

    def brand(self, brand_id):
        return self.parse(tigertag_image(0x5BF59264, brand_id=brand_id)).get("brand")

    def test_new_id_from_the_download_resolves(self):
        self.assertIsNone(self.brand(64999))
        self.write("id_brand", [{"id": 64999, "name": "Newbrand"}, {"id": 628, "name": "Buddy3D"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 1)
        self.assertEqual(self.brand(64999), "Newbrand")

    def test_download_never_renames_a_built_in_entry(self):
        self.write("id_material", [{"id": 24629, "label": "PLA High Speed"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 0)
        info = self.parse(tigertag_image(0x5BF59264, material_id=24629))
        self.assertEqual(info.get("material"), "PLA-HS")

    def test_unreadable_or_bad_files_change_nothing(self):
        self.write("id_brand", "{not json")
        self.write("id_aspect", [{"id": "x", "label": 5}, "junk"])
        self.write("id_material", {"not": "a list"})
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 0)
        self.assertEqual(self.brand(628), "Buddy3D")

    def test_new_type_and_diameter_from_the_download(self):
        self.write("id_type", [{"id": 200, "label": "Pellets"}])
        self.write("id_diameter", [{"id": 99, "label": "3.00"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 2)
        info = self.parse(tigertag_image(0x5BF59264, type_id=200, diameter_id=99))
        self.assertEqual((info.get("tigertag_type"), info.get("diameter_mm")), ("Pellets", 3.0))

    def test_diameter_entries_must_be_millimetres(self):
        self.write("id_diameter", [{"id": 90, "label": "abc"}, {"id": 91, "label": "0"},
                                   {"id": 92, "label": "-1.75"}, {"id": 93, "label": "175"},
                                   {"id": 221, "label": "9.99"}])   # last: built-in never replaced
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 0)
        self.assertEqual(self.parse(tigertag_image(0x5BF59264, diameter_id=221)).get("diameter_mm"), 2.85)

    def test_newly_issued_version_is_recognised(self):
        """A version TigerTag issues after this release is read once the registry has it."""
        new = 0x1234ABCD
        self.assertNotEqual((self.parse(tigertag_image(new)) or {}).get("tag_format"), "tigertag")
        self.write("id_version", [{"id": new, "version": "1.1", "name": "TigerTag 1.1", "tag": "TIGER_TAG_V1.1"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 1)
        info = self.parse(tigertag_image(new, material_id=24629))
        self.assertEqual(info.get("tag_format"), "tigertag")
        self.assertEqual(info.get("tigertag_version"), "TigerTag 1.1")
        self.assertEqual(info.get("material"), "PLA-HS")

    def test_new_blank_chip_version_is_not_a_filament(self):
        self.write("id_version", [{"id": 0x1234ABCE, "version": "1.0", "name": "TigerTag Init 2"}])
        tag_parser.load_tigertag_registry(self.dir)
        self.assertTrue(tag_parser._has_tigertag_magic(tigertag_image(0x1234ABCE)))
        self.assertIsNone(tag_parser._try_tigertag(tigertag_image(0x1234ABCE)))

    def test_version_entries_are_checked_strictly(self):
        before = dict(tag_parser._TIGERTAG_MAGIC)
        rejected = [
            {"id": 0x22220001, "version": "2.0", "name": "TigerTag 2"},   # another chip layout
            {"id": 0x22220002, "name": "No version"},
            {"id": 0x22220003, "version": 1.1, "name": "Numeric version"},
            {"id": 0, "version": "1.0", "name": "RFID Empty"},            # empty chip
            {"id": 2 ** 32, "version": "1.0", "name": "Too big"},
            {"id": -1, "version": "1.0", "name": "Negative"},
            {"id": True, "version": "1.0", "name": "Boolean id"},
            {"id": "1542820452", "version": "1.0", "name": "String id"},
            {"id": 0x22220004, "version": "1.0", "name": "  "},
            {"id": 0x5BF59264, "version": "1.0", "name": "Renamed"},      # built-in never replaced
            "junk",
        ]
        self.write("id_version", rejected)
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 0)
        self.assertEqual(tag_parser._TIGERTAG_MAGIC, before)
        for magic in (0x22220001, 0x22220002, 0x22220003, 0x22220004):
            self.assertNotEqual((self.parse(tigertag_image(magic)) or {}).get("tag_format"), "tigertag", hex(magic))

    def test_ids_must_fit_the_chip_field(self):
        """Aspect, type, diameter and unit are one byte on the chip; material and brand two."""
        self.write("id_aspect", [{"id": 256, "label": "Too wide"}, {"id": 200, "label": "Fits"}])
        self.write("id_brand", [{"id": 65536, "name": "Too wide"}, {"id": 64999, "name": "Fits"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 2)
        self.assertNotIn(256, tag_parser._TIGERTAG_ASPECTS)
        self.assertNotIn(65536, tag_parser._TIGERTAG_BRANDS)
        self.assertEqual(tag_parser._TIGERTAG_BRANDS.get(64999), "Fits")

    def test_names_must_be_safe_text(self):
        """Names reach the gate map and G-code: no quotes, escapes, comments, control chars or essays."""
        bad = ['Quote"d', "Apos'trophe", "Back\\slash", "Semi;colon", "Hash#tag",
               "New\nline", "Tab\tbed", "Null\x00", "", "   ", "x" * 49]
        self.write("id_brand", [{"id": 64000 + i, "name": n} for i, n in enumerate(bad)]
                   + [{"id": 64999, "name": "Bambu Lab m\u00b3 3D-Fuel (Pro)"}])
        self.assertEqual(tag_parser.load_tigertag_registry(self.dir), 1)
        self.assertEqual(tag_parser._TIGERTAG_BRANDS.get(64999), "Bambu Lab m\u00b3 3D-Fuel (Pro)")
        self.assertFalse([i for i in range(64000, 64000 + len(bad)) if i in tag_parser._TIGERTAG_BRANDS])

    def test_downloaded_version_decodes_like_a_built_in_one(self):
        """The version table is the only gate: once listed, a version decodes with no field checks,
        so a low-temperature filament or a resin chip with no temperatures still resolves."""
        new = 0x1234ABCD
        self.write("id_version", [{"id": new, "version": "1.1", "name": "TigerTag 1.1"}])
        tag_parser.load_tigertag_registry(self.dir)
        for fields in (dict(nozzle=(60, 100)), dict(type_id=173, nozzle=(0, 0), bed=(0, 0)),
                       dict(type_id=0x55, diameter_id=3)):
            info = self.parse(tigertag_image(new, **fields))
            self.assertEqual(info.get("tag_format"), "tigertag", fields)
            self.assertEqual(info.get("tigertag_version"), "TigerTag 1.1")

    def test_missing_folder_changes_nothing(self):
        import os
        self.assertEqual(tag_parser.load_tigertag_registry(os.path.join(self.dir, "nope")), 0)


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
    need AES/HKDF that pycryptodome provides. No convincing image can be synthesized
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
        """An unwritten card must not be reported as a recognized branded spool."""
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


class TestBlankTagBambuRejection(TagParserTestCase):
    """
    Sector-sized blank reads must not satisfy the best-effort Bambu heuristic.  Uniform
    buffers carry no evidence of encrypted data, whether a reader fills them with 0x00,
    0xFF, or another byte.  Non-uniform binary data remains eligible so this guard does
    not turn into a speculative entropy threshold for real Bambu tags.
    """

    BLANK_NTAG_READ = bytes(64)     # 16 pages x 4 bytes, the shipped default

    def test_blank_tag_is_unrecognised_not_bambu(self):
        self.assertFalse(tag_parser._detect_bambu(self.BLANK_NTAG_READ))
        self.assertIsNone(self.parse(self.BLANK_NTAG_READ))

    def test_erased_tag_is_unrecognised_not_bambu(self):
        erased = b'\xFF' * 64
        self.assertFalse(tag_parser._detect_bambu(erased))
        self.assertIsNone(self.parse(erased))

    def test_default_page_count_produces_exactly_the_trigger_length(self):
        """Documents WHY this is the default path rather than an edge case."""
        self.assertEqual(len(self.BLANK_NTAG_READ) % 64, 0)
        self.assertEqual(16 * 4, 64)

    def test_nonuniform_binary_data_remains_a_bambu_candidate(self):
        candidate = b'\xA5\x5A' * 32
        self.assertTrue(tag_parser._detect_bambu(candidate))

    def test_a_written_ndef_tag_is_never_mistaken_for_bambu(self):
        """The NDEF escape hatch works, which is what keeps real tags safe."""
        self.assertFalse(tag_parser._detect_bambu(openspool_tag()))


class TestParseDiagnostics(TagParserTestCase):
    """
    parse_tag reports what it tried through the caller's trace callback, and only
    there: the caller owns the debug level, so a parse with no callback is silent
    however the machine is configured.
    """

    def test_parse_without_a_callback_logs_nothing(self):
        handler = logging.Handler(level=logging.INFO)
        records = []
        handler.emit = lambda record: records.append(record.getMessage())
        channels = ('mmu_rfid.tag_parser', 'mmu_rfid.reader')
        saved = {}
        for name in channels:
            logger_obj = logging.getLogger(name)
            saved[name] = (logger_obj.level, logger_obj.propagate)
            logger_obj.addHandler(handler)
            logger_obj.setLevel(logging.INFO)
            logger_obj.propagate = False
        try:
            self.parse(openspool_tag())
            self.parse(b'\x00' * 32)            # matches nothing
        finally:
            for name in channels:
                logger_obj = logging.getLogger(name)
                logger_obj.setLevel(saved[name][0])
                logger_obj.propagate = saved[name][1]
                logger_obj.removeHandler(handler)
        self.assertEqual(records, [])

    def test_the_callback_still_reports_the_conclusion(self):
        seen = []
        tag_parser.parse_tag(openspool_tag(),
                             trace=lambda level, msg, *a: seen.append(msg % a))
        self.assertTrue(any('matched OpenSpool' in line for line in seen), seen)

        seen = []
        tag_parser.parse_tag(b'\x00' * 32,
                             trace=lambda level, msg, *a: seen.append(msg % a))
        self.assertTrue(any('unrecognized' in line or 'no NDEF' in line
                            for line in seen), seen)
