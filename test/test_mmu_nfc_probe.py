# Happy Hare test harness - driver-level tests for the non-blocking presence probe.
#
# test_mmu_nfc_scan.py covers the probe through the manager, against a VirtualNfcChip
# faked at the DRIVER boundary. That proves the orchestration but says nothing about the
# register sequences the real chips need - and those are where this change is riskiest,
# because a homing probe has to talk to the chip in halves.
#
# So these tests run the REAL RC522 driver against the scripted SPI bus
# (test/hh/klippy_root/extras/bus.py), asserting the actual transcript. Two things are
# specifically load-bearing and invisible to the higher-level tests:
#
#   1. The probe must use WUPA (0x52), not REQA (0x26). Per ISO14443-3 only tags in IDLE
#      answer REQA, so a tag that answered the previous tick is in READY and goes silent -
#      the probe would detect a tag once and then appear to break.
#   2. probe_stop() drops the RF field to return tags to IDLE (so the following
#      read_target(), which does use REQA, still works) and MUST restore the antenna TX
#      bits. is_alive() reports the reader dead if they are left clear.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_probe
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh.bootstrap import install

install()

from extras.mmu.unit.nfc import rc522_driver
from extras.mmu.unit.nfc.rc522_driver import RC522Driver

logging.getLogger().setLevel(logging.CRITICAL)

# Register addresses as they appear on the wire
WRITE = lambda reg: (reg << 1) & 0x7E
READ = lambda reg: ((reg << 1) & 0x7E) | 0x80

TX_CONTROL = rc522_driver._TxControlReg
BIT_FRAMING = rc522_driver._BitFramingReg
COM_IRQ = rc522_driver._ComIrqReg
COM_IEN = rc522_driver._ComIEnReg
FIFO_LEVEL = rc522_driver._FIFOLevelReg
FIFO_DATA = rc522_driver._FIFODataReg
ERROR_REG = rc522_driver._ErrorReg
COMMAND = rc522_driver._CommandReg


class FakeSpi:
    """
    Minimal stand-in for the harness MCU_SPI: records every transaction and answers
    reads from a per-register table rather than a flat script, so a test doesn't have to
    predict the exact read order (which is what it is trying to assert).
    """

    def __init__(self, registers=None):
        self.transcript = []            # [(op, [bytes]), ...]
        self.registers = dict(registers or {})

    def spi_send(self, data, minclock=0, reqclock=0):
        self.transcript.append(('send', list(data)))

    def spi_transfer(self, data, minclock=0, reqclock=0):
        self.transcript.append(('transfer', list(data)))
        addr = data[0]
        for reg, value in self.registers.items():
            if READ(reg) == addr:
                return {'response': [0x00, value]}
        return {'response': [0x00, 0x00]}

    # -- assertion helpers --------------------------------------------------

    def writes_to(self, reg):
        """Values written to 'reg', in order."""
        return [payload[1] for op, payload in self.transcript
                if op == 'send' and payload and payload[0] == WRITE(reg)]

    def written_bytes(self):
        """Every byte written, for 'was this command ever sent' checks."""
        return [tuple(payload) for op, payload in self.transcript if op == 'send']


def driver(registers=None):
    spi = FakeSpi(registers)
    return RC522Driver(spi, name="gate0", debug=0, sleep_fn=lambda _s: None), spi


class TestProbeUsesWupa(unittest.TestCase):
    """The REQA/READY trap: a repeated presence probe must wake tags from READY."""

    def test_probe_sends_wupa_not_reqa(self):
        drv, spi = driver()
        drv.probe_start()
        loaded = spi.writes_to(FIFO_DATA)
        self.assertEqual(loaded, [0x52],
                         'the probe must load WUPA (0x52). REQA (0x26) is only answered '
                         'by tags in IDLE, so a tag already woken by the previous tick '
                         'would go silent and the probe would detect it exactly once')

    def test_probe_uses_seven_bit_framing(self):
        """WUPA/REQA are 7-bit short frames."""
        drv, spi = driver()
        drv.probe_start()
        self.assertIn(0x07, spi.writes_to(BIT_FRAMING),
                      'a short frame needs BitFraming 0x07 before StartSend')

    def test_select_cascade_still_uses_reqa(self):
        """
        _request_a drives the real select, which wants REQA semantics. The probe's WUPA
        must not have been swapped in there.
        """
        drv, spi = driver({COM_IRQ: 0x00})
        drv._request_a(timeout=0.001)
        self.assertIn(0x26, spi.writes_to(FIFO_DATA),
                      '_request_a must still send REQA')
        self.assertNotIn(0x52, spi.writes_to(FIFO_DATA))


class TestProbeIsNonBlocking(unittest.TestCase):
    """probe_start/probe_poll are the two halves of one _transceive, split at its sleep."""

    def test_probe_never_sleeps(self):
        """
        The entire point: a probe tick must not pause the reactor. sleep_fn raises here,
        so any sleep on the probe path fails the test.
        """
        def boom(_seconds):
            raise AssertionError('probe path slept - that stalls the drip-homing move')

        spi = FakeSpi({COM_IRQ: 0x30, ERROR_REG: 0x00, FIFO_LEVEL: 0x02})
        drv = RC522Driver(spi, name="gate0", debug=0, sleep_fn=boom)
        drv.probe_start()
        self.assertTrue(drv.probe_poll())

    def test_poll_returns_none_while_the_exchange_is_in_flight(self):
        """No IRQ bits set yet: answer None so a later tick collects the result."""
        drv, spi = driver({COM_IRQ: 0x00})
        drv.probe_start()
        self.assertIsNone(drv.probe_poll(),
                          'an unfinished exchange must report None, not False - a False '
                          'restarts the scan and throws away the one in flight')

    def test_poll_reports_true_when_a_tag_answers(self):
        drv, _spi = driver({COM_IRQ: 0x30, ERROR_REG: 0x00, FIFO_LEVEL: 0x02})
        drv.probe_start()
        self.assertTrue(drv.probe_poll())

    def test_poll_reports_false_when_the_timer_expires(self):
        """TimerIRq with no Rx/Idle is the RC522's "nobody answered"."""
        drv, _spi = driver({COM_IRQ: 0x01})
        drv.probe_start()
        self.assertFalse(drv.probe_poll())

    def test_poll_reports_false_on_a_protocol_error(self):
        drv, _spi = driver({COM_IRQ: 0x30, ERROR_REG: 0x08})   # collision
        drv.probe_start()
        self.assertFalse(drv.probe_poll())

    def test_short_fifo_is_not_a_detection(self):
        """A valid ATQA is 2 bytes; anything less isn't a tag answering."""
        drv, _spi = driver({COM_IRQ: 0x30, ERROR_REG: 0x00, FIFO_LEVEL: 0x01})
        drv.probe_start()
        self.assertFalse(drv.probe_poll())

    def test_poll_without_a_start_is_false(self):
        drv, _spi = driver({COM_IRQ: 0x30})
        self.assertFalse(drv.probe_poll(),
                         'polling with nothing in flight must not report a detection')

    def test_repeated_probes_keep_detecting_a_present_tag(self):
        """Every tick against a tag that is still there must report present."""
        drv, _spi = driver({COM_IRQ: 0x30, ERROR_REG: 0x00, FIFO_LEVEL: 0x02})
        for tick in range(5):
            drv.probe_start()
            self.assertTrue(drv.probe_poll(), 'tick %d lost the tag' % tick)


class TestProbeStopRestoresTheAntenna(unittest.TestCase):
    """
    probe_stop() cycles the RF field so tags come back up in IDLE and answer the REQA
    that read_target() uses. It must leave the antenna ON afterwards.
    """

    def test_field_is_dropped_and_restored(self):
        drv, spi = driver({TX_CONTROL: 0x03})
        drv.probe_start()
        drv.probe_stop()
        tx_writes = spi.writes_to(TX_CONTROL)
        self.assertTrue(tx_writes, 'probe_stop must touch TxControlReg to reset tags')
        self.assertFalse(tx_writes[0] & 0x03,
                         'the field must actually be dropped, or tags stay in READY and '
                         'the following read_target() REQA finds nothing')
        self.assertTrue(tx_writes[-1] & 0x03,
                         'the antenna TX bits must be restored - is_alive() reads them '
                         'and would report the reader dead')

    def test_reader_is_still_alive_after_a_probe_cycle(self):
        """The end-to-end consequence of getting the restore wrong."""
        drv, _spi = driver({TX_CONTROL: 0x03})
        drv.probe_start()
        drv.probe_poll()
        drv.probe_stop()
        self.assertTrue(drv.is_alive(),
                        'a probe cycle left the reader looking dead')

    def test_antenna_is_restored_even_if_the_reset_fails(self):
        """A failed field reset must not strand the antenna off."""
        class FlakySpi(FakeSpi):
            def __init__(self):
                FakeSpi.__init__(self, {TX_CONTROL: 0x03})
                self.fail_next_send = False

            def spi_send(self, data, minclock=0, reqclock=0):
                # Fail the write that drops the field, after recording it
                FakeSpi.spi_send(self, data, minclock, reqclock)
                if data[0] == WRITE(TX_CONTROL) and not (data[1] & 0x03):
                    raise RuntimeError('SPI glitch')

        spi = FlakySpi()
        drv = RC522Driver(spi, name="gate0", debug=0, sleep_fn=lambda _s: None)
        drv.probe_start()
        self.assertFalse(drv.probe_stop(), 'a failed reset should report failure')
        self.assertTrue(spi.writes_to(TX_CONTROL)[-1] & 0x03,
                        'the error path must still leave the antenna on')

    def test_stop_is_safe_with_nothing_in_flight(self):
        drv, _spi = driver({TX_CONTROL: 0x03})
        self.assertTrue(drv.probe_stop())


class TestProbeContractShape(unittest.TestCase):
    """MmuNfcReader.has_probe_support() depends on these names existing."""

    def test_driver_exposes_the_full_contract(self):
        drv, _spi = driver()
        for name in ('probe_start', 'probe_poll', 'probe_stop'):
            self.assertTrue(callable(getattr(drv, name, None)),
                            'RC522Driver.%s missing - the reader would silently fall '
                            'back to the blocking shim' % name)


if __name__ == '__main__':
    unittest.main()
