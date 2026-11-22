# Happy Hare MMU Software
#
# NTAG deep reads: each driver's _ndef_tlv_extent decides how much of a Type 2 tag to read by
# walking its TLVs to the NDEF message. A binary (non-NDEF) layout such as TigerTag's used to be
# walked as if it were TLVs, so a later data byte could pass for an NDEF TLV and cut the read
# below the 32 bytes TigerTag needs: a TigerTag+ spool with green colour byte 0x03 was missed.
# The walk now stops at any TLV type a Type 2 data area can't hold and the full length is read.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_ndef_extent
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import logging
import struct
import unittest

from test.hh import install

install()

from extras.mmu.unit.nfc import tag_parser                              # noqa: E402
from extras.mmu.unit.nfc.pn532_driver import PN532Driver                # noqa: E402
from extras.mmu.unit.nfc.pn7160_driver import PN7160Handler             # noqa: E402  (PN7160Driver delegates to it)
from extras.mmu.unit.nfc.rc522_driver import RC522Driver                # noqa: E402

logging.getLogger().setLevel(logging.CRITICAL)

DRIVERS = (PN532Driver, RC522Driver, PN7160Handler)


def tigertag_image(magic, rgb=(0x61, 0xA3, 0x5D)):
    """TigerTag user memory from page 4: the 32-byte core (PLA, 1.75 mm, 1 kg) plus a zero
    twin-tag id. Only the version ID and colour vary, the bytes the TLV walk trips over."""
    raw = bytearray(36)
    struct.pack_into(">I", raw, 0, magic)
    struct.pack_into(">I", raw, 4, 0xFFFFFFFF)          # no product id
    struct.pack_into(">H", raw, 8, 38219)               # PLA
    raw[12], raw[13] = 0x8E, 56                         # filament, 1.75 mm
    raw[16:20] = bytes(rgb) + b"\xFF"
    raw[20:23] = (1000).to_bytes(3, "big")
    raw[23] = 21                                        # grams
    struct.pack_into(">HH", raw, 24, 240, 270)          # nozzle min/max
    raw[30], raw[31] = 80, 100                          # bed min/max
    return bytes(raw)


def ndef_tlv(payload):
    if len(payload) < 0xFF:
        return bytes([0x03, len(payload)]) + payload + b"\xFE"
    return bytes([0x03, 0xFF]) + len(payload).to_bytes(2, "big") + payload + b"\xFE"


def openspool_ndef(min_len):
    """An NDEF text-free MIME record with OpenSpool JSON, padded to at least min_len bytes."""
    body = {"protocol": "openspool", "version": "1.0", "type": "PLA", "color_hex": "FF0000",
            "brand": "Generic", "min_temp": 190, "max_temp": 220, "note": "x" * max(0, min_len)}
    payload = json.dumps(body).encode()
    mime = b"application/json"
    record = bytes([0xD2, len(mime)]) + bytes([len(payload)]) if len(payload) < 256 else None
    if record is None:   # long record: MB|ME|TNF=2 without SR
        record = bytes([0xC2, len(mime)]) + len(payload).to_bytes(4, "big")
    return ndef_tlv(record + mime + payload)


class TestWalker(unittest.TestCase):
    """The same cases through every driver's copy, so they can't drift apart."""

    def extent(self, data):
        results = {d.__name__: d._ndef_tlv_extent(bytes(data)) for d in DRIVERS}
        self.assertEqual(len(set(results.values())), 1, results)
        return next(iter(results.values()))

    # Standard NDEF layouts: found exactly as before
    def test_factory_blank_ntag(self):
        self.assertEqual(self.extent(b"\x03\x00\xFE" + bytes(13)), (2, 0))

    def test_plain_ndef_message(self):
        self.assertEqual(self.extent(b"\x03\x10" + bytes(16)), (18, 16))

    def test_lock_control_then_ndef(self):
        self.assertEqual(self.extent(b"\x01\x03\xA0\x0C\x34\x03\x20" + bytes(9)), (39, 32))

    def test_memory_control_null_padding_and_proprietary_then_ndef(self):
        data = b"\x02\x03\x00\x00\x00" + b"\x00\x00" + b"\xFD\x02\xAA\xBB" + b"\x03\x05" + bytes(5)
        self.assertEqual(self.extent(data), (18, 5))

    def test_long_form_length(self):
        self.assertEqual(self.extent(b"\x03\xFF\x01\x20" + bytes(12)), (4 + 0x120, 0x120))

    def test_terminator_before_ndef(self):
        self.assertIsNone(self.extent(b"\xFE" + bytes(15)))

    # Binary layouts: the walk stops at once instead of wandering into the data
    def test_tigertag_versions_are_not_ndef(self):
        for magic in (0x5BF59264, 0xBC0FCB97, 0x6C41A2E1):
            for green in (0x03, 0xA3, 0x00, 0xFE, 0x01):
                raw = tigertag_image(magic, rgb=(0x61, green, 0x00)) + bytes(28)
                for n in (16, 32, 48, 64):
                    self.assertIsNone(self.extent(raw[:n]), (hex(magic), green, n))

    def test_other_binary_formats_are_not_ndef(self):
        self.assertIsNone(self.extent(b"\x36\xEE\xEE\xEE\xEE" + bytes(11)))        # Elegoo
        self.assertIsNone(self.extent(b"\x7B\x00\x03\x05" + bytes(12)))            # Anycubic ACE

    def test_tigertag_plus_with_green_3_used_to_be_cut_short(self):
        """The reported case: the old walk read BC as a 15-byte TLV, landed on the green
        byte (0x03) and returned an NDEF extent of 19 bytes."""
        raw = tigertag_image(0xBC0FCB97, rgb=(0x61, 0x03, 0x00)) + bytes(28)
        self.assertIsNone(self.extent(raw[:32]))


class FakeChip:
    """robust_page_read over an NTAG image starting at page 4 (16 bytes = 4 pages per read)."""

    def __init__(self, user_memory):
        self.memory = bytes(user_memory)
        self.reads = 0

    def page_read(self, page):
        self.reads += 1
        offset = (page - 4) * 4
        chunk = self.memory[offset:offset + 16]
        return chunk if chunk else None


class TestDeepRead(unittest.TestCase):
    """The full read loop of the drivers built on robust_page_read (PN532, RC522)."""

    def read(self, driver_cls, memory, max_pages=16):
        chip = FakeChip(memory)
        drv = driver_cls.__new__(driver_cls)
        drv._debug = 0
        drv._name = "test"
        drv._transport_name = "test"
        drv._sleep = lambda s: None
        drv._release_current_target = lambda *a, **k: None
        drv.robust_page_read = chip.page_read
        return bytes(drv.ntag_read_ndef_user_memory(start_page=4, max_pages=max_pages))

    def test_tigertag_plus_reads_in_full_and_decodes(self):
        image = tigertag_image(0xBC0FCB97, rgb=(0x61, 0x03, 0x00)) + bytes(108)   # 144-byte NTAG213
        for drv in (PN532Driver, RC522Driver):
            data = self.read(drv, image)
            self.assertEqual(len(data), 64, drv.__name__)
            self.assertEqual(tag_parser.parse_tag(data).get("tag_format"), "tigertag", drv.__name__)

    def test_long_ndef_still_reads_past_the_fallback(self):
        """A compliant NDEF message longer than the 64-byte fallback is still read to its end."""
        image = openspool_ndef(150)
        self.assertGreater(len(image), 64)
        for drv in (PN532Driver, RC522Driver):
            data = self.read(drv, image + bytes(16))
            self.assertGreaterEqual(len(data), len(image) - 1, drv.__name__)   # through the NDEF, terminator optional
            self.assertEqual(tag_parser.parse_tag(data).get("tag_format"), "openspool", drv.__name__)

    def test_blank_ntag_reads_only_its_header(self):
        for drv in (PN532Driver, RC522Driver):
            self.assertEqual(self.read(drv, b"\x03\x00\xFE" + bytes(141)), b"\x03\x00", drv.__name__)


if __name__ == "__main__":
    unittest.main()
