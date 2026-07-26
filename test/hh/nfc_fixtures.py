# Happy Hare test harness - NFC reader bus fixtures.
#
# Scripted bus responses so the REAL reader driver code runs against a synthetic
# chip. These were captured from the drivers themselves: the fake bus raises
# ScriptExhausted carrying the transcript so far (test/hh/klippy_root/extras/bus.py),
# so the first run tells you exactly what the driver asked for.
#
# RC522 init, from rc522_driver.py:200-237, is 7 register writes then:
#     tx = self._read(_TxControlReg)        # spi_transfer #1
#     if not (tx & 0x03): self._write(...)  # enable antenna TX pins
#     tx_final = self._read(_TxControlReg)  # spi_transfer #2
# _read returns resp['response'][1] (:190), so each scripted entry is a 2-byte list.
#
# We answer the first read with the antenna OFF and the second with it ON. That is
# the more useful of the two orderings: it exercises the enable branch rather than
# skipping it, and it matches a chip coming up from cold.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

_TX_CONTROL_OFF = [0x00, 0x00]      # antenna TX pins clear -> driver enables them
_TX_CONTROL_ON = [0x00, 0x03]       # bits 0-1 set -> "antenna on", init reports OK

# One full RC522 init cycle's worth of spi_transfer responses.
RC522_INIT = [_TX_CONTROL_OFF, _TX_CONTROL_ON]

# is_alive() re-reads TxControlReg, and init may be retried (MMU_NFC INIT=1, the
# ENABLE=0->1 re-init, and the bootup path). Rather than count call sites, prime a
# generous number of cycles - an over-long script is harmless because unconsumed
# entries are simply never read.
DEFAULT_CYCLES = 32


def rc522_script(cycles=DEFAULT_CYCLES):
    """
    One cold-start init (antenna off -> driver enables it -> reads back on), then a
    long tail of "antenna on".

    The tail must NOT keep alternating: is_alive() re-reads the same register
    (rc522_driver.py:239-241) and would consume the next cycle's antenna-off entry,
    leaving every reader reporting alive=False. Answering ON from then on models a
    chip that stays up, which is what the round-trip tests need.
    """
    return list(RC522_INIT) + [list(_TX_CONTROL_ON) for _ in range(2 * cycles)]


def prime_reader(reader, cycles=DEFAULT_CYCLES):
    """
    Give one MmuNfcReader's transport enough scripted responses to initialise.
    Returns True if a bus was found and primed.

    Only RC522 (SPI) is scripted today. PN532/PN7160 are I2C-framed and their init
    sequences have not been captured yet - deliberately left unprimed so a test using
    them fails loudly with the transcript rather than appearing to work.
    """
    driver = getattr(reader, 'reader', None)
    spi = getattr(driver, '_spi', None)
    if spi is None or not hasattr(spi, 'script'):
        return False
    spi.script.extend(rc522_script(cycles))
    return True


def prime_all(printer, cycles=DEFAULT_CYCLES):
    """Prime every configured reader on every unit. Returns the number primed."""
    machine = printer.lookup_object('mmu_machine', None)
    if machine is None:
        return 0
    primed = 0
    for unit in machine.units:
        manager = getattr(unit, 'nfc_manager', None)
        if manager is None:
            continue
        readers = list(getattr(manager, 'gate_readers', ()) or ())
        shared = getattr(manager, 'shared_reader', None)
        if shared is not None:
            readers.append(shared)
        for reader in readers:
            if reader is not None and prime_reader(reader, cycles):
                primed += 1
    return primed
