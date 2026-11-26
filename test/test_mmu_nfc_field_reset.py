# Happy Hare test harness - RC522 field reset before select.

import unittest

from test.hh.bootstrap import install

install()

from extras.mmu.unit.nfc import rc522_driver
from extras.mmu.unit.nfc.rc522_driver import RC522Driver

READ = lambda reg: ((reg << 1) & 0x7E) | 0x80
WRITE = lambda reg: (reg << 1) & 0x7E

TX_CONTROL = rc522_driver._TxControlReg
FIFO_DATA = rc522_driver._FIFODataReg
COM_IRQ = rc522_driver._ComIrqReg


class FakeSpi:
    """Answers register reads from a table and records every write, in order."""

    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.writes = []                # [(reg_write_addr, value), ...]

    def spi_send(self, data, minclock=0, reqclock=0):
        data = list(data)
        if len(data) >= 2:
            self.writes.append((data[0], data[1]))

    def spi_transfer(self, data, minclock=0, reqclock=0):
        addr = data[0]
        for reg, value in self.registers.items():
            if READ(reg) == addr:
                return {'response': [0x00, value]}
        return {'response': [0x00, 0x00]}


def driver(registers=None):
    spi = FakeSpi(registers)
    return RC522Driver(spi, name="gate0", debug=0, sleep_fn=lambda _s: None), spi


class TestSelectResetsField(unittest.TestCase):
    """A tag left ACTIVE by the previous read ignores REQA unless the field is cycled first."""

    def test_field_is_cycled_before_reqa(self):
        drv, spi = driver({TX_CONTROL: 0x83, COM_IRQ: 0x00})
        drv._select_iso14443a_target(timeout=0.001)
        tx = [i for i, (a, _v) in enumerate(spi.writes) if a == WRITE(TX_CONTROL)]
        reqa = [i for i, (a, v) in enumerate(spi.writes) if a == WRITE(FIFO_DATA) and v == 0x26]
        self.assertTrue(reqa, 'select must still send REQA')
        self.assertGreaterEqual(len(tx), 2)
        self.assertEqual(spi.writes[tx[0]][1], 0x80, 'antenna drivers (bits 1:0) off')
        self.assertEqual(spi.writes[tx[1]][1], 0x83, 'antenna drivers back on, other bits kept')
        self.assertLess(tx[1], reqa[0], 'the field must be back up before REQA is sent')


if __name__ == '__main__':
    unittest.main()
