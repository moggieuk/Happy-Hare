# Happy Hare test harness - NFC reader transport selection in the rendered config.
#
# klippy resolves a reader's transport from `interface:`, falling back to the chip's
# default (reader_factory.SUPPORTED_INTERFACES). A pn532 defaults to i2c, so a PN532/SPI
# section without the key quietly became an I2C reader. The template now always writes
# the key from PARAM_NFC_READER_INTERFACE.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_spi
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install
from test.hh import cfg, profiles

install()   # put the fake klippy tree on sys.path so `extras.*` resolves

from extras.mmu.unit.nfc.pn532_driver import PN532SPIDriver  # noqa: E402


def reader_sections(profile):
    if isinstance(profile, str):
        profile = profiles.get(profile)
    hardware = cfg.render(profile)['config/base/mmu_hardware.cfg']
    out, name = {}, None
    for line in hardware.splitlines():
        stripped = line.strip()
        if stripped.startswith('[mmu_nfc_reader'):
            name = stripped.strip('[]').split(None, 1)[-1]
            out[name] = {}
            continue
        if stripped.startswith('['):
            name = None
            continue
        if name and ':' in stripped and not stripped.startswith('#'):
            key, _, value = stripped.partition(':')
            out[name][key.strip()] = value.split('#')[0].strip()
    return out


I2C_KEYS = ('i2c_mcu', 'i2c_address', 'i2c_speed', 'i2c_bus',
            'i2c_software_scl_pin', 'i2c_software_sda_pin')


class TestPn532SpiRender(unittest.TestCase):

    def test_common_reader_declares_spi(self):
        sections = reader_sections('nfc_pn532_spi')
        self.assertEqual(len(sections), 1, sections)
        keys = list(sections.values())[0]
        self.assertEqual((keys['reader_type'], keys.get('interface')), ('pn532', 'spi'))
        self.assertEqual(keys['cs_pin'], 'unit0:PA4')
        self.assertIn('spi_speed', keys)
        for key in I2C_KEYS:
            self.assertNotIn(key, keys)

    def test_per_gate_reader_declares_spi(self):
        sections = reader_sections('nfc_pn532_spi_per_gate')
        self.assertEqual(len(sections), 4, sections)
        gate0 = sections['unit0_nfc0']
        self.assertEqual((gate0['reader_type'], gate0.get('interface')), ('pn532', 'spi'))
        self.assertIn('cs_pin', gate0)
        for key in I2C_KEYS:
            self.assertNotIn(key, gate0)
        for name in ('unit0_nfc1', 'unit0_nfc2', 'unit0_nfc3'):
            self.assertEqual(sections[name]['reader_type'], 'rc522', name)
            self.assertEqual(sections[name]['interface'], 'spi', name)


class TestPn532SpiBoots(unittest.TestCase):
    """The rendered section must reach the SPI driver, not fall back to I2C."""

    def test_common_reader_is_built_on_spi(self):
        from test.hh.bootstrap import Session
        session = Session(profile='nfc_pn532_spi')
        try:
            session.boot()
            unit = session.printer.lookup_object('mmu_machine').units[0]
            reader = unit.nfc_manager.shared_reader
            self.assertIsNotNone(reader)
            self.assertEqual(reader.interface, 'spi')
            self.assertIsInstance(reader.reader, PN532SPIDriver)
        finally:
            session.close()

    def test_per_gate_reader_is_built_on_spi(self):
        from test.hh.bootstrap import Session
        session = Session(profile='nfc_pn532_spi_per_gate')
        try:
            session.boot()
            unit = session.printer.lookup_object('mmu_machine').units[0]
            readers = unit.nfc_manager.gate_readers
            self.assertEqual(readers[0].interface, 'spi')
            self.assertIsInstance(readers[0].reader, PN532SPIDriver)
            for gate in (1, 2, 3):
                self.assertEqual(readers[gate].reader_type, 'rc522', gate)
        finally:
            session.close()


class TestStaleChipPins(unittest.TestCase):
    """
    A hidden symbol keeps its old value (installer/build.py reads the raw user value),
    so pins left behind by switching chip type must not render: klippy rejects options
    the new chip does not read.
    """

    def test_common_pn532_drops_pn7160_pins(self):
        profile = profiles.NFC_PN532.derive(
            'nfc_pn532_stale_pn7160_pins',
            syms={'PARAM_NFC_READER_VEN_PIN': 'unit0:PB2',
                  'PARAM_NFC_READER_IRQ_PIN': 'unit0:PB3'})
        keys = reader_sections(profile)['unit0_nfc']
        self.assertNotIn('ven_pin', keys)
        self.assertNotIn('irq_pin', keys)

    def test_per_gate_pn532_drops_pn7160_pins(self):
        profile = profiles.NFC_PN532_SW_I2C.derive(
            'nfc_pn532_sw_i2c_stale_pn7160_pins',
            syms={'PARAM_NFC_READER_VEN_PIN_0': 'unit0:PB2',
                  'PARAM_NFC_READER_IRQ_PIN_1': 'unit0:PB3'})
        sections = reader_sections(profile)
        for name in ('unit0_nfc0', 'unit0_nfc1'):
            self.assertNotIn('ven_pin', sections[name], name)
            self.assertNotIn('irq_pin', sections[name], name)

    def test_pn7160_keeps_its_pins(self):
        profile = profiles.NFC_PN532.derive(
            'nfc_pn7160_pins',
            syms={'CHOICE_NFC_READER_TYPE_PN7160': True,
                  'PARAM_NFC_READER_VEN_PIN': 'unit0:PB2',
                  'PARAM_NFC_READER_IRQ_PIN': 'unit0:PB3'})
        keys = reader_sections(profile)['unit0_nfc']
        self.assertEqual((keys['reader_type'], keys['interface']), ('pn7160', 'i2c'))
        self.assertEqual((keys.get('ven_pin'), keys.get('irq_pin')),
                         ('unit0:PB2', 'unit0:PB3'))


class TestKconfigRangesMatchKlippy(unittest.TestCase):
    """menuconfig must not accept a value that klippy rejects at boot."""

    def _value(self, label, syms, symbol):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig(label, dict(profiles.BOXTURTLE.syms, **syms))
        return int(kc.syms[symbol].str_value)

    def test_pn7160_address_is_0x28_to_0x2b(self):
        # validate_reader_i2c_address() accepts only PN7160_I2C_ADDRESSES
        common = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
                  'CHOICE_NFC_READER_TYPE_PN7160': True}
        per_gate = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
                    'CHOICE_NFC_READER_TYPE_PN7160_0': True}
        for addr in (36, 39):
            self.assertIn(self._value('pn7160-addr', dict(common, PARAM_NFC_READER_I2C_ADDRESS=addr),
                                      'PARAM_NFC_READER_I2C_ADDRESS'), range(40, 44), addr)
            self.assertIn(self._value('pn7160-addr-0', dict(per_gate, PARAM_NFC_READER_I2C_ADDRESS_0=addr),
                                      'PARAM_NFC_READER_I2C_ADDRESS_0'), range(40, 44), addr)

    def test_spi_speed_floor_is_klippers(self):
        # MCU_SPI_from_config reads spi_speed with minval=100000
        common = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_COMMON_NFC_READER': True,
                  'PARAM_NFC_READER_SPI_SPEED': 10}
        per_gate = {'MMU_HAS_NFC_READER': True, 'MMU_HAS_PER_GATE_NFC_READERS': True,
                    'PARAM_NFC_READER_SPI_SPEED_0': 1}
        self.assertGreaterEqual(self._value('spi-speed', common, 'PARAM_NFC_READER_SPI_SPEED'), 100000)
        self.assertGreaterEqual(self._value('spi-speed-0', per_gate, 'PARAM_NFC_READER_SPI_SPEED_0'), 100000)


if __name__ == '__main__':
    unittest.main()
