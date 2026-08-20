# Happy Hare test harness - static NFC receiver-gain configuration.

import unittest

from test.hh.bootstrap import install
from test.hh import cfg, profiles

install()

from extras.mmu.unit.nfc import pn5180_driver, pn7160_driver, rc522_driver
from extras.mmu.unit.nfc.pn532_driver import _PN532Base
from extras.mmu.unit.nfc.reader_factory import rx_gain_from_config
from extras.mmu.unit.nfc.rx_gain import RX_GAIN_DB


class ConfigError(Exception):
    pass


class FakeConfig:
    error = ConfigError

    def __init__(self, gain):
        self.gain = gain

    def getint(self, _name, default=None, minval=None):
        value = default if self.gain is None else self.gain
        if minval is not None and value < minval:
            raise self.error('below minimum')
        return value

    def get_name(self):
        return 'mmu_nfc_reader gate0'


class TestGainValidation(unittest.TestCase):

    def test_every_documented_hardware_value_is_accepted(self):
        for reader_type, gains in RX_GAIN_DB.items():
            for gain in (0,) + gains:
                self.assertEqual(
                    rx_gain_from_config(FakeConfig(gain), reader_type), gain)

    def test_value_from_another_chip_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            rx_gain_from_config(FakeConfig(60), 'pn5180')
        self.assertIn('33, 40, 50, 57', str(caught.exception))
        self.assertIn('pn5180', str(caught.exception))


class TestGainTemplate(unittest.TestCase):

    def reader_gain(self, base, symbol, gain, reader):
        profile = profiles.get(base).derive(
            base + '_rx_gain_test', syms={symbol: gain})
        parser = cfg.assemble(cfg.render(profile))
        return dict(parser.items('mmu_nfc_reader ' + reader))['rx_gain']

    def test_common_reader_gain_is_rendered(self):
        self.assertEqual(
            self.reader_gain('nfc_pn5180', 'PARAM_NFC_READER_RX_GAIN',
                             40, 'unit0_nfc'),
            '40')

    def test_per_gate_reader_gain_is_rendered(self):
        self.assertEqual(
            self.reader_gain('nfc_per_gate', 'PARAM_NFC_READER_RX_GAIN_0',
                             38, 'unit0_nfc0'),
            '38')


class FakeSpi:
    def __init__(self, register_value=0):
        self.register_value = register_value
        self.writes = []

    def spi_transfer(self, _data):
        return {'response': [0, self.register_value]}

    def spi_send(self, data):
        self.writes.append(list(data))


class TestMfrcGainWrites(unittest.TestCase):

    def test_rc522_preserves_non_gain_register_bits(self):
        spi = FakeSpi(register_value=0x8B)
        driver = rc522_driver.RC522Driver(
            spi, 'gate0', debug=0, sleep_fn=lambda _seconds: None)
        self.assertTrue(driver.set_rx_gain(43))
        self.assertEqual(
            spi.writes[-1],
            [((rc522_driver._RFCfgReg << 1) & 0x7E), 0xEB])

    def test_pn532_all_transport_base_emits_write_register(self):
        driver = _PN532Base(
            'gate0', 0.250, 0.050, 0, False,
            sleep_fn=lambda _seconds: None, time_fn=lambda: 0.0)
        driver._transport_name = 'pn532/test'
        calls = []
        driver._transceive = lambda *args, **kwargs: calls.append((args, kwargs)) or []
        self.assertTrue(driver.set_rx_gain(38))
        self.assertEqual(calls[0][0][0], [0x08, 0x63, 0x16, 0x58])
        self.assertEqual(calls[0][0][1], 0x09)


class TestPn5180GainWrites(unittest.TestCase):

    def core(self, register_value=0xA4):
        core = pn5180_driver.PN5180Core.__new__(pn5180_driver.PN5180Core)
        core.rx_gain_code = None
        core.commands = []
        core.writes = []
        core._transceive_command = lambda data: core.commands.append(list(data)) or []
        core.read_register = lambda reg: register_value
        core.write_register = lambda reg, value: core.writes.append((reg, value))
        return core

    def test_gain_maps_to_rf_control_rx(self):
        core = self.core()
        driver = pn5180_driver.PN5180Driver.__new__(pn5180_driver.PN5180Driver)
        driver._name = 'gate0'
        driver._core = core
        self.assertTrue(driver.set_rx_gain(50))
        self.assertEqual(core.rx_gain_code, 2)
        self.assertEqual(core.writes[-1], (pn5180_driver.RF_CONTROL_RX, 0xA6))

    def test_protocol_load_reapplies_static_gain(self):
        core = self.core()
        core.rx_gain_code = 3
        core.load_rf_config(0x0D, 0x8D)
        self.assertEqual(
            core.commands, [[pn5180_driver.CMD_LOAD_RF_CONFIG, 0x0D, 0x8D]])
        self.assertEqual(core.writes[-1], (pn5180_driver.RF_CONTROL_RX, 0xA7))


class FakePn7160Handler:
    def __init__(self):
        self.commands = []
        self.events = []

    def command(self, frame, expected_gid, expected_oid, timeout=1.0):
        self.commands.append((list(frame), expected_gid, expected_oid, timeout))
        return [0x40, 0x02, 0x01, 0x00], []

    def connect_nci(self, reset, keep_config):
        self.events.append(('connect', reset, keep_config))

    def configure_total_duration(self):
        self.events.append(('duration',))

    def configure_rx_gain(self, code, db):
        self.events.append(('gain', code, db))

    def configure_discovery_map(self):
        self.events.append(('map',))


class TestPn7160GainWrites(unittest.TestCase):

    def test_both_enabled_rf_profiles_receive_i_and_q_gain(self):
        handler = FakePn7160Handler()
        handler._name = 'gate0'
        pn7160_driver.PN7160Handler.configure_rx_gain(handler, 6, 53)
        self.assertEqual(len(handler.commands), 2)
        frames = [call[0] for call in handler.commands]
        self.assertEqual([frame[7] for frame in frames], [0x3C, 0x20])
        for frame in frames:
            self.assertEqual(frame[:7], [0x20, 0x02, 0x0A, 0x01, 0xA0, 0x0D, 0x06])
            self.assertEqual(frame[8:], [0x44, 0x66, 0x0A, 0x00, 0x00])

    def test_full_setup_reapplies_gain_after_clear_config_reset(self):
        handler = FakePn7160Handler()
        driver = pn7160_driver.PN7160Driver.__new__(pn7160_driver.PN7160Driver)
        driver._handler = handler
        driver._rx_gain = (4, 44)
        driver._alive = False
        driver._needs_full_setup = True
        driver._setup_for_read(full=True)
        self.assertEqual(handler.events, [
            ('connect', True, False), ('duration',), ('gain', 4, 44), ('map',)])


if __name__ == '__main__':
    unittest.main()
