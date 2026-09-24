# Happy Hare test harness - PN532-over-I2C bus status handling.
#
# Klipper's bus.MCU_I2C.i2c_read()/i2c_write() shut the printer down on a NACK - on new
# firmware because the i2c_transfer wrapper calls invoke_shutdown(), on old firmware
# because command_i2c_read shuts the MCU down itself. Where the firmware has i2c_transfer
# the driver sends the raw command and reads the status (i2c_transport.py), so a loose
# cable takes the reader offline rather than the print. Where it doesn't, the driver must
# fall back to the plain calls WITHOUT a retry= argument, which Klipper <= v0.13.0 rejects.
#
# The real PN532Driver runs against the scripted fake MCU_I2C
# (test/hh/klippy_root/extras/bus.py). On the transfer path every call - writes too -
# consumes one scripted entry; on the fallback path only reads do.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_pn532_i2c
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh.bootstrap import install

install()   # put the fake klippy tree on sys.path so `extras.*` resolves

from extras.mmu.unit.nfc import reader_factory  # noqa: E402
from extras.mmu.unit.nfc.i2c_transport import I2CStatusError  # noqa: E402
from extras.mmu.unit.nfc.pn532_driver import (  # noqa: E402
    PN532Driver, PN532_ACK, run_low_level_debug)
from extras.mmu.unit.nfc.pn7160_driver import (  # noqa: E402
    PN7160Error, PN7160I2CStatusError)

MCU_I2C = reader_factory.bus_module.MCU_I2C

NACK = {'i2c_bus_status': 'NACK'}
WRITE_OK = []
READY = [0x01]
ACK = [0x01] + PN532_ACK


def _frame(cmd_resp, payload):
    """A PN532->host I2C read buffer, leading status byte included."""
    data = [0xD5, cmd_resp] + list(payload)
    return ([0x01, 0x00, 0x00, 0xFF, len(data), (-len(data)) & 0xFF]
            + data + [(-sum(data)) & 0xFF, 0x00])


FIRMWARE = _frame(0x03, [0x32, 0x01, 0x06, 0x07])


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def pn532(transfer_support=True, script=(), low_level_debug=False):
    i2c = MCU_I2C(None, addr=0x24, transfer_support=transfer_support)
    i2c.script.extend(script)
    clock = _Clock()
    drv = PN532Driver(i2c, 'gate0', debug=0, low_level_debug=low_level_debug,
                      sleep_fn=clock.sleep, time_fn=clock.now)
    return drv, i2c


def ops(i2c):
    return set(op for op, _ in i2c.transcript)


class TestNewFirmwareUsesCheckedTransfer(unittest.TestCase):

    def test_firmware_version_goes_through_i2c_transfer_only(self):
        drv, i2c = pn532(script=[WRITE_OK, READY, ACK, READY, FIRMWARE])
        version = drv.get_firmware_version()
        self.assertEqual(version['ic'], 0x32)
        self.assertEqual(ops(i2c), {'i2c_transfer'})

    def test_nack_on_command_write_is_a_failed_exchange(self):
        drv, i2c = pn532(script=[NACK])
        self.assertIsNone(drv.get_firmware_version())
        self.assertEqual(len(i2c.transcript), 1)

    def test_nack_while_waiting_for_the_response_is_a_failed_exchange(self):
        drv, _ = pn532(script=[WRITE_OK, READY, ACK, NACK])
        self.assertIsNone(drv.get_firmware_version())

    def test_nack_on_every_exchange_reports_not_alive(self):
        drv, _ = pn532(script=[NACK] * 20)
        self.assertFalse(drv.is_alive())

    def test_init_against_a_nacking_chip_raises_its_documented_error(self):
        drv, _ = pn532(script=[NACK] * 20)
        with self.assertRaises(RuntimeError) as ctx:
            drv.init()
        self.assertNotIsInstance(ctx.exception, I2CStatusError)

    def test_probe_start_reports_a_nacked_send(self):
        drv, _ = pn532(script=[NACK])
        self.assertFalse(drv.probe_start())
        self.assertIsNone(drv._probe_stage)

    def test_probe_poll_ends_the_probe_on_a_nack(self):
        drv, _ = pn532(script=[WRITE_OK, NACK])
        self.assertTrue(drv.probe_start())
        self.assertIs(drv.probe_poll(), False)
        self.assertIsNone(drv._probe_stage)

    def test_status_error_carries_status_and_label(self):
        drv, _ = pn532(script=[NACK])
        with self.assertRaises(I2CStatusError) as ctx:
            drv._i2c_read(1, label='status')
        self.assertEqual(ctx.exception.status, 'NACK')
        self.assertEqual(ctx.exception.label, 'status')


class _GcodeError(Exception):
    pass


class _FakeGcmd:
    error = _GcodeError

    def __init__(self, **params):
        self.params = params
        self.responses = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, **kwargs):
        return int(self.params.get(name, default))

    def respond_info(self, msg):
        self.responses.append(msg)


class TestLowLevelConsoleNack(unittest.TestCase):
    """A non-gcmd.error exception reaching Klipper's gcode dispatcher is an
    'Internal error' shutdown, so a NACK from the console must become gcmd.error."""

    def test_raw_read_nack_is_a_gcode_error(self):
        drv, _ = pn532(script=[NACK], low_level_debug=True)
        gcmd = _FakeGcmd(RAW_READ='1', LEN='1')
        with self.assertRaises(_GcodeError) as ctx:
            run_low_level_debug(gcmd, drv, 'gate0', 'MMU_NFC_READER', True)
        self.assertIn('NACK', str(ctx.exception))

    def test_raw_write_nack_is_a_gcode_error(self):
        drv, _ = pn532(script=[NACK], low_level_debug=True)
        gcmd = _FakeGcmd(RAW_WRITE='00')
        with self.assertRaises(_GcodeError):
            run_low_level_debug(gcmd, drv, 'gate0', 'MMU_NFC_READER', True)


class TestOldFirmwareFallsBack(unittest.TestCase):
    """The fake's i2c_read(write, read_len) rejects retry=, like Klipper <= v0.13.0."""

    def test_firmware_version_uses_plain_read_and_write(self):
        drv, i2c = pn532(transfer_support=False,
                         script=[READY, ACK, READY, FIRMWARE])
        version = drv.get_firmware_version()
        self.assertEqual(version['ic'], 0x32)
        self.assertEqual(ops(i2c), {'i2c_write', 'i2c_read'})

    def test_probe_uses_plain_read_and_write(self):
        drv, i2c = pn532(transfer_support=False, script=[[0x00]])
        self.assertTrue(drv.probe_start())
        self.assertIsNone(drv.probe_poll())
        self.assertEqual(ops(i2c), {'i2c_write', 'i2c_read'})


class TestStartupWarning(unittest.TestCase):

    def test_warns_when_a_nack_would_shut_down_the_mcu(self):
        drv, _ = pn532(transfer_support=False, low_level_debug=True)
        with self.assertLogs('mmu_rfid.reader', level='WARNING') as captured:
            drv.init()
        self.assertTrue(any('shut down the MCU' in line for line in captured.output))

    def test_no_warning_with_i2c_transfer(self):
        drv, _ = pn532(low_level_debug=True)
        with self.assertLogs('mmu_rfid.reader', level='INFO') as captured:
            drv.init()
        self.assertFalse(any('WARNING' in line for line in captured.output))


class TestSupportIsCheckedPerCall(unittest.TestCase):

    def test_transfer_cmd_bound_after_construction_is_used(self):
        # MCU_I2C.build_config() sets i2c_transfer_cmd after create_reader() has
        # already built the driver.
        drv, i2c = pn532(transfer_support=False)
        i2c.i2c_transfer_cmd = MCU_I2C(None, addr=0x24).i2c_transfer_cmd
        i2c.i2c_transfer_cmd._owner = i2c
        i2c.script.extend([WRITE_OK, READY, ACK, READY, FIRMWARE])
        self.assertIsNotNone(drv.get_firmware_version())
        self.assertEqual(ops(i2c), {'i2c_transfer'})

    def test_host_without_the_attribute_falls_back(self):
        drv, i2c = pn532(transfer_support=False,
                         script=[READY, ACK, READY, FIRMWARE])
        del i2c.i2c_transfer_cmd
        self.assertIsNotNone(drv.get_firmware_version())
        self.assertEqual(ops(i2c), {'i2c_write', 'i2c_read'})


class TestSharedStatusError(unittest.TestCase):

    def test_pn7160_error_is_still_a_pn7160_error(self):
        err = PN7160I2CStatusError('NACK', [0xAB], label='nci_header')
        self.assertIsInstance(err, PN7160Error)
        self.assertIsInstance(err, I2CStatusError)
        self.assertEqual(str(err), 'I2C label=nci_header status=NACK response=AB')


if __name__ == '__main__':
    unittest.main()
