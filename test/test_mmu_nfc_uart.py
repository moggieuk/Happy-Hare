# Happy Hare test harness - driver-level tests for the PN532 HSU/UART transport.
#
# The UART transport is structurally unlike I2C and SPI, and the differences are all
# invisible from higher up. I2C/SPI are query-then-read with a host-chosen length;
# UART is a push stream with neither. So the risk sits in three places, and this
# file is organised around them:
#
#   1. THE FRAMER. Reassembling frames out of a byte stream: leading padding,
#      garbage resync, partial frames held across ticks, both checksums. Tested as a
#      pure unit - no reactor, no driver, no port.
#   2. THE TWO PROBE INVARIANTS. _probe_status_ready() must mean "a COMPLETE frame
#      is buffered", not "bytes are available" (a partial-frame true is the subtle
#      bug that works on a fast host and fails on a slow one), and the fetch methods
#      must consume from the accumulator rather than re-read the port.
#   3. THE ABORT BUDGET. On an empty gate _probe_abort() runs about every 2s for a
#      whole drip-homing move, so it must stay inside the ~20ms the I2C abort costs.
#
# Everything here uses the CONSTRUCTOR SEAM (serial_factory=, sleep_fn=, time_fn=)
# rather than the fake serial module's registry, following test_mmu_nfc_probe.py's
# hand-rolled FakeSpi. One fake clock serves both time seams: its sleep() advances
# the now() it reports, so a driver timeout costs zero real time and poll counts
# become assertable.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_uart
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import random
import unittest

from test.hh.bootstrap import install

install()

import serial as fake_serial

from extras.mmu.unit.nfc.log import READER_CHANNEL
from extras.mmu.unit.nfc.pn532_driver import (
    PN532Driver, PN532SPIDriver, PN532_ACK, check_preambled_frame)
from extras.mmu.unit.nfc.pn532_uart_driver import (
    PN532UARTDriver, _HSUFrameReader, FRAME_ACK, FRAME_NACK, FRAME_INFO)
from test.hh.nfc_fixtures import (
    PN532_UART_ACK, PN532_UART_NACK, PN532_UART_ERROR, PN532_HSU_PADDING,
    PN532_UART_GARBAGE, PN532_FIRMWARE_RESP, PN532_NO_TARGET, PN532UartChip,
    pn532_frame, pn532_inlist_resp, pn532_uart_probe_script)

logging.getLogger().setLevel(logging.CRITICAL)

FIRMWARE_PAYLOAD = [0x32, 0x01, 0x06, 0x07]


class Clock:
    """
    One object for both time seams: sleep() advances the clock now() reports.

    Needed because _transceive() clamps its ACK timeout to at least 50ms, so a test
    cannot ask for a shorter wait. Without this, every negative-path test below
    would busy-spin through 50ms of real time. It also makes elapsed time an
    assertion surface - see TestProbeAbortBudget.
    """

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def elapsed(self, since):
        return self.t - since


def _raise_io(*_args, **_kwargs):
    """Stand in for a yanked USB adapter: ENXIO is what a dead fd actually gives."""
    raise OSError(6, 'Device not configured')


def capture_records(logger_name=READER_CHANNEL, level=logging.INFO):
    """
    Collect records at 'level' and above. Used instead of assertNoLogs, which needs
    Python 3.10 - Klipper hosts run whatever the distro ships - and alongside
    assertLogs, which fails when nothing is logged. "Nothing was logged" is the
    interesting half of a per-reader debug level.
    """
    class Collector(logging.Handler):
        def __init__(self):
            logging.Handler.__init__(self, level=level)
            self.records = []

        def emit(self, record):
            self.records.append(record.getMessage())

    return Collector(), logging.getLogger(logger_name)


def capture_warnings(logger_name=READER_CHANNEL):
    return capture_records(logger_name, level=logging.WARNING)


def framer(chunks):
    """A framer reading from a fixed list of chunks, one per pump()."""
    queue = list(chunks)
    return _HSUFrameReader(lambda n: queue.pop(0) if queue else b'')


def driver(chunks=(), chip=None, name="gate0", debug=0, **kwargs):
    """A real PN532UARTDriver on a fake port. Returns (driver, port, clock)."""
    clock = Clock()
    port = fake_serial.Serial('/dev/fake-nfc', 115200, timeout=0)
    port.feed(*chunks)
    if chip is not None:
        port.on_write = chip.on_write
    drv = PN532UARTDriver('/dev/fake-nfc', name=name, debug=debug,
                          serial_factory=lambda p, b: port,
                          sleep_fn=clock.sleep, time_fn=clock.now, **kwargs)
    return drv, port, clock


# ═══════════════════════════════════════════════════════════════════════════════
# 1. The framer
# ═══════════════════════════════════════════════════════════════════════════════

class TestHsuFramer(unittest.TestCase):
    """Pure stream-reassembly tests: no reactor, no driver, no port."""

    def _one(self, chunks, pumps=1):
        f = framer(chunks)
        for _ in range(pumps):
            f.pump()
        return f, f.next_frame()

    def test_plain_ack(self):
        f, got = self._one([PN532_UART_ACK])
        self.assertEqual(got[0], FRAME_ACK)
        self.assertEqual(list(got[1]), PN532_ACK,
                         'the framer must normalise an ACK back to the canonical '
                         'preamble-first form the driver compares against')

    def test_nack(self):
        _f, got = self._one([PN532_UART_NACK])
        self.assertEqual(got[0], FRAME_NACK)

    def test_leading_padding_is_discarded(self):
        """
        A real chip emits 0x00 padding ahead of a frame. Note the discard count is
        padding + 1: the frame's own preamble 0x00 is indistinguishable from padding,
        so the framer syncs on the two-byte start code and re-synthesises a preamble.
        """
        f, got = self._one([PN532_HSU_PADDING + PN532_UART_ACK])
        self.assertEqual(list(got[1]), PN532_ACK)
        self.assertEqual(f.discarded, len(PN532_HSU_PADDING) + 1)

    def test_garbage_then_resync(self):
        f, got = self._one([PN532_UART_GARBAGE + PN532_FIRMWARE_RESP])
        self.assertEqual(check_preambled_frame(got[1], 0x03), FIRMWARE_PAYLOAD)
        self.assertEqual(f.discarded, len(PN532_UART_GARBAGE) + 1,
                         'garbage plus the frame preamble - see the padding test')

    def test_partial_frame_is_not_a_frame(self):
        """
        THE invariant behind _probe_status_ready(). At 115200 a frame takes several
        milliseconds and straddles reactor ticks; acting on half of one is the bug
        that works on a fast host and fails on a slow one.
        """
        f = framer([PN532_UART_ACK[:4], PN532_UART_ACK[4:]])
        f.pump()
        self.assertIsNone(f.peek_frame(),
                          'four of six ACK bytes is not a frame')
        f.pump()
        self.assertEqual(f.next_frame()[0], FRAME_ACK)

    def test_frame_arriving_one_byte_per_read(self):
        chunks = [PN532_FIRMWARE_RESP[i:i + 1]
                  for i in range(len(PN532_FIRMWARE_RESP))]
        f = framer(chunks)
        got = None
        for _ in range(len(chunks) + 2):
            f.pump()
            got = f.next_frame()
            if got:
                break
        self.assertIsNotNone(got, 'a byte-at-a-time frame must still reassemble')
        self.assertEqual(check_preambled_frame(got[1], 0x03), FIRMWARE_PAYLOAD)

    def test_start_code_split_across_reads(self):
        """A trailing lone 0x00 may be the first half of a start code."""
        f = framer([b'\x11\x00', b'\xFF\x00\xFF\x00'])
        f.pump()
        self.assertIsNone(f.peek_frame())
        f.pump()
        self.assertEqual(f.next_frame()[0], FRAME_ACK)

    def test_extended_frame_rejected_with_forward_progress(self):
        """
        0xFF 0xFF marks an extended frame. No command this driver issues can answer
        with one, so it is garbage - but rejecting it must not stall the stream.
        """
        ext = bytes([0x00, 0x00, 0xFF, 0xFF, 0xFF, 0x00, 0x05, 0x00])
        f, got = self._one([ext + PN532_FIRMWARE_RESP])
        self.assertEqual(got[0], FRAME_INFO,
                         'the good frame behind the extended one must still arrive')
        self.assertEqual(f.bad_frames, 1)

    def test_bad_length_checksum_resyncs(self):
        bad_lcs = bytes([0x00, 0x00, 0xFF, 0x04, 0x00, 0xD5, 0x03, 1, 2, 3, 0x00])
        f, got = self._one([bad_lcs + PN532_FIRMWARE_RESP])
        self.assertEqual(check_preambled_frame(got[1], 0x03), FIRMWARE_PAYLOAD)
        self.assertGreaterEqual(f.bad_frames, 1)

    def test_bad_data_checksum_resyncs(self):
        corrupt = bytearray(PN532_FIRMWARE_RESP)
        corrupt[-2] ^= 0xFF                     # break the DCS
        f, got = self._one([bytes(corrupt) + PN532_FIRMWARE_RESP])
        self.assertEqual(check_preambled_frame(got[1], 0x03), FIRMWARE_PAYLOAD)
        self.assertGreaterEqual(f.bad_frames, 1)

    def test_missing_postamble_still_parses(self):
        """
        The postamble is optional on purpose: it can lag the DCS by a byte time, and
        waiting for it would cost every exchange one whole poll interval.
        """
        _f, got = self._one([PN532_FIRMWARE_RESP[:-1]])
        self.assertEqual(check_preambled_frame(got[1], 0x03), FIRMWARE_PAYLOAD)

    def test_error_frame_is_a_frame_but_not_a_match(self):
        """TFI 0x7F parses cleanly, then fails the TFI check - not silence."""
        _f, got = self._one([PN532_UART_ERROR])
        self.assertEqual(got[0], FRAME_INFO)
        self.assertIsNone(check_preambled_frame(got[1], 0x4B))

    def test_overflow_drops_oldest_bytes(self):
        f = framer([b'\x99' * 900])
        f.pump()
        self.assertLessEqual(len(f.buffered()), f.MAX_BUFFER)
        self.assertGreater(f.discarded, 0)

    def test_reset_discards_the_accumulator(self):
        f = framer([PN532_UART_ACK[:3]])
        f.pump()
        f.reset()
        self.assertEqual(f.buffered(), b'')
        self.assertIsNone(f.peek_frame())

    def test_fuzz_every_valid_frame_is_recovered(self):
        """
        Valid frames embedded in random garbage at random offsets, split into random
        chunks. Resync bugs are invisible to hand-written scripts, which is exactly
        why this is here.
        """
        rnd = random.Random(20260729)
        candidates = [PN532_UART_ACK, PN532_UART_NACK, PN532_FIRMWARE_RESP,
                      pn532_inlist_resp([0x01, 0x02, 0x03, 0x04])]
        for trial in range(200):
            frames = [rnd.choice(candidates)
                      for _ in range(rnd.randint(1, 5))]
            stream = b''
            for frame in frames:
                # Junk must not contain 0x00, or it could forge a start code and the
                # expectation below would be genuinely ambiguous rather than wrong.
                junk = bytes(rnd.randrange(1, 256)
                             for _ in range(rnd.randint(0, 4)))
                stream += junk.replace(b'\x00', b'\x7E') + frame
            chunks, i = [], 0
            while i < len(stream):
                n = rnd.randint(1, 9)
                chunks.append(stream[i:i + n])
                i += n
            f = framer(chunks)
            kinds, total = [], 0
            for _ in range(len(chunks) + 2):
                total += f.pump()
                while True:
                    got = f.next_frame()
                    if got is None:
                        break
                    kinds.append(got[0])
            expected = [FRAME_ACK if x == PN532_UART_ACK else
                        FRAME_NACK if x == PN532_UART_NACK else FRAME_INFO
                        for x in frames]
            self.assertEqual(kinds, expected,
                             'trial %d lost or invented a frame: %s'
                             % (trial, stream.hex()))
            self.assertEqual(total, len(stream), 'every byte must be pumped once')


# ═══════════════════════════════════════════════════════════════════════════════
# 2. The probe invariants
# ═══════════════════════════════════════════════════════════════════════════════

class TestProbeInvariants(unittest.TestCase):

    def test_status_ready_is_false_on_a_partial_frame(self):
        """
        _probe_status_ready() must mean "a complete frame is buffered". The naive
        in_waiting > 0 would let probe_poll() run _probe_fetch_ack() against half an
        ACK, fail the check and fire _probe_abort().
        """
        drv, _port, _clock = driver([PN532_UART_ACK[:4], PN532_UART_ACK[4:]])
        self.assertFalse(drv._probe_status_ready(),
                         'four of six ACK bytes must not read as ready')
        self.assertTrue(drv._probe_status_ready())

    def test_fetch_ack_does_not_touch_the_port(self):
        """
        On I2C the ACK read is its own bus transaction. Here the bytes are already in
        the accumulator, and a second read would return nothing.
        """
        drv, port, _clock = driver([PN532_UART_ACK])
        self.assertTrue(drv._probe_status_ready())
        reads_before = port.reads
        self.assertTrue(drv._probe_fetch_ack())
        self.assertEqual(port.reads, reads_before,
                         '_probe_fetch_ack must consume from the accumulator')

    def test_fetch_response_does_not_touch_the_port(self):
        drv, port, _clock = driver([pn532_inlist_resp([0xDE, 0xAD, 0xBE, 0xEF])])
        self.assertTrue(drv._probe_status_ready())
        reads_before = port.reads
        payload = drv._probe_fetch_response(0x4B, 32)
        self.assertEqual(port.reads, reads_before)
        self.assertIsNotNone(payload)

    def test_probe_cycle_finds_a_tag(self):
        """The full state machine: start, ACK tick, response tick."""
        drv, _port, _clock = driver(
            pn532_uart_probe_script([0xDE, 0xAD, 0xBE, 0xEF]))
        self.assertTrue(drv.probe_start())
        results = [drv.probe_poll() for _ in range(6)]
        self.assertIn(True, results, 'the probe must report the tag: %r' % results)
        self.assertIsNone(results[0],
                          'the first tick collects the ACK, it cannot answer yet')

    def test_probe_cycle_reports_empty_gate(self):
        drv, _port, _clock = driver(pn532_uart_probe_script(None))
        self.assertTrue(drv.probe_start())
        results = [drv.probe_poll() for _ in range(6)]
        self.assertIn(False, results,
                      'an empty scan must resolve to False so the manager restarts '
                      'it: %r' % results)
        self.assertNotIn(True, results)

    def test_probe_survives_a_split_ack(self):
        """Straddling reactor ticks is the whole point of the contract."""
        drv, _port, _clock = driver(
            pn532_uart_probe_script([0x11, 0x22, 0x33, 0x44], split_ack=True))
        drv.probe_start()
        results = [drv.probe_poll() for _ in range(8)]
        self.assertIn(True, results, '%r' % results)

    def test_probe_resyncs_past_garbage(self):
        drv, _port, _clock = driver(
            pn532_uart_probe_script([0x11, 0x22, 0x33, 0x44], garbage=True))
        drv.probe_start()
        results = [drv.probe_poll() for _ in range(8)]
        self.assertIn(True, results, '%r' % results)

    def test_send_flushes_stale_bytes(self):
        """
        A new command starts a new exchange, so a leftover frame can only be
        mistaken for this command's ACK. On I2C an unread response waits inside the
        chip and cannot do this; here it is already in the OS buffer.
        """
        drv, port, _clock = driver([PN532_NO_TARGET])
        drv._framer.pump()
        self.assertNotEqual(drv._framer.buffered(), b'')
        drv._send([0x02])
        self.assertEqual(drv._framer.buffered(), b'')
        self.assertGreater(port.resets, 0, 'the OS buffer must be flushed too')


class TestProbeAbortBudget(unittest.TestCase):
    """
    On an empty gate the 2s watchdog fires and MmuNfcManager restarts the scan, so
    _probe_abort() runs about every 2 seconds for a whole drip-homing move. The I2C
    abort costs at most 4 x 5ms; this must match, or it reintroduces exactly the
    reactor-hogging the probe split removed.
    """

    def test_abort_stays_within_budget_when_the_stream_is_idle(self):
        drv, _port, clock = driver([])
        start = clock.now()
        drv._probe_abort()
        self.assertLessEqual(clock.elapsed(start), drv._ABORT_MAX_TIME,
                             'an idle abort must finish inside the quiet window')

    def test_abort_stays_within_budget_when_bytes_keep_arriving(self):
        """A chip babbling must not let the abort run past its ceiling."""
        drv, _port, clock = driver([b'\x00' * 8] * 200)
        start = clock.now()
        drv._probe_abort()
        self.assertLessEqual(clock.elapsed(start), drv._ABORT_MAX_TIME + 0.005,
                             'the hard ceiling must bound a busy stream')

    def test_abort_writes_the_cancel_ack(self):
        drv, port, _clock = driver([])
        drv._probe_abort()
        self.assertIn(bytes(PN532_ACK), port.writes,
                      'a bare ACK frame is the documented way to cancel a command')

    def test_abort_flushes_after_draining_not_before(self):
        """
        ORDER MATTERS. reset_input_buffer() drops only what the OS already holds, so
        flushing before the quiet window just moves the garbage - a 20-byte frame is
        still ~1.7ms in transit at 115200.
        """
        drv, port, _clock = driver([PN532_NO_TARGET])
        drv._probe_abort()
        self.assertGreater(port.resets, 0)
        self.assertGreater(port.reads, 0, 'the drain must actually read')
        self.assertEqual(drv._framer.buffered(), b'',
                         'the accumulator must be clear once the abort returns')

    def test_abort_clears_the_probe_stage(self):
        drv, _port, _clock = driver([])
        drv.probe_start()
        drv._probe_abort()
        self.assertIsNone(drv._probe_stage)
        self.assertFalse(drv.probe_poll(),
                         'a poll after an abort must report done, not resume')


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Wake, transceive and the transport contract
# ═══════════════════════════════════════════════════════════════════════════════

class TestWakePreamble(unittest.TestCase):

    def test_uart_sends_the_resync_burst_before_the_first_command(self):
        chip = PN532UartChip()
        drv, port, _clock = driver(chip=chip)
        drv.init()
        self.assertTrue(port.writes, 'init() must write something')
        self.assertEqual(port.writes[0], drv._WAKE_PREAMBLE,
                         'the HSU receiver needs the full 0x55 burst before it will '
                         'see a command frame at all, and it must go out as ONE '
                         'contiguous write; got %r' % (port.writes[0],))
        self.assertTrue(drv._WAKE_PREAMBLE.startswith(b'\x55\x55'),
                        'the burst is 0x55 0x55 then padding')

    def test_wake_burst_precedes_get_firmware_version(self):
        chip = PN532UartChip()
        drv, port, _clock = driver(chip=chip)
        drv.init()
        framed = [i for i, w in enumerate(port.writes)
                  if w[:3] == b'\x00\x00\xFF']
        burst = [i for i, w in enumerate(port.writes)
                 if w.startswith(b'\x55\x55')]
        self.assertTrue(framed and burst)
        self.assertLess(burst[0], framed[0])

    def test_i2c_and_spi_wake_preamble_are_no_ops(self):
        """
        Regression guard on the base hook: it is called once per _wake_pn532 attempt
        on every transport, so it must cost nothing where it is not needed.
        """
        for cls in (PN532Driver, PN532SPIDriver):
            self.assertIsNone(cls._transport_wake_preamble(object.__new__(cls)),
                              '%s must not act on the wake hook' % cls.__name__)

    def test_init_reports_a_dead_chip_rather_than_hanging(self):
        drv, _port, _clock = driver([])          # chip never answers
        with self.assertRaises(RuntimeError):
            drv.init()


class TestReadAckAndRecv(unittest.TestCase):

    def test_stale_info_frame_ahead_of_the_ack_is_skipped(self):
        """
        Unlike I2C, where an unread response waits in the chip, the OS buffer can
        hold a whole late frame from an abandoned exchange. Skipping it and carrying
        on is correct; failing the exchange is not.
        """
        drv, _port, _clock = driver([PN532_NO_TARGET, PN532_UART_ACK])
        self.assertTrue(drv._read_ack(timeout=0.5),
                        'a leftover info frame must not fail the ACK wait')

    def test_nack_fails_fast_without_burning_the_timeout(self):
        drv, _port, clock = driver([PN532_UART_NACK])
        start = clock.now()
        self.assertFalse(drv._read_ack(timeout=1.0))
        self.assertLess(clock.elapsed(start), 1.0,
                        'a NACK is a definite answer, not a reason to keep waiting')

    def test_ack_timeout_costs_no_real_time(self):
        drv, _port, clock = driver([])
        start = clock.now()
        self.assertFalse(drv._read_ack(timeout=1.0))
        self.assertGreaterEqual(clock.elapsed(start), 1.0,
                                'the injected clock must drive the deadline')

    def test_recv_ignores_read_len(self):
        """
        A UART frame carries its own length, so read_len is meaningless here.
        Truncating to it would corrupt any response longer than the caller's guess.
        """
        long_payload = list(range(20))
        drv, _port, _clock = driver([pn532_frame(0x4B, long_payload)])
        payload = drv._recv(0x4B, read_len=8, timeout=0.2)
        self.assertEqual(payload, long_payload)

    def test_recv_skips_a_frame_for_a_different_command(self):
        drv, _port, _clock = driver([PN532_FIRMWARE_RESP, PN532_NO_TARGET])
        self.assertEqual(drv._recv(0x4B, timeout=0.5), [0x00],
                         'a response for another command must be discarded, '
                         'not returned or treated as fatal')

    def test_full_read_target_against_a_reactive_chip(self):
        chip = PN532UartChip(uid='04AABBCC')
        drv, _port, _clock = driver(chip=chip)
        drv.init()
        target = drv.read_target(timeout=0.5)
        self.assertIsNotNone(target, 'commands seen: %r' % chip.commands)
        self.assertEqual(target['uid'], '04AABBCC')

    def test_read_target_on_an_empty_field(self):
        chip = PN532UartChip(uid=None)
        drv, _port, _clock = driver(chip=chip)
        drv.init()
        self.assertIsNone(drv.read_target(timeout=0.5))


class TestDebugLevelIsPerReader(unittest.TestCase):
    """
    debug: is documented per-reader, and level 4 must be no exception. A machine can
    run several readers, so tracing the one that will not talk must not turn every
    other reader (or the tag parser) into a firehose in the same klippy.log.
    """

    def send_one_command(self, name, debug):
        drv, _port, _clock = driver(name=name, debug=debug)
        drv._send([0x02])                       # _send logs its TX frame at level 4

    def test_only_the_traced_reader_logs(self):
        """
        Both readers still log the events they always log (a port opening); it is the
        per-transaction detail that must belong to one reader only.
        """
        with self.assertLogs(READER_CHANNEL, level='INFO') as captured:
            self.send_one_command('quiet_gate', 0)
            self.send_one_command('traced_gate', 4)
        traced = [line for line in captured.output if '_send: TX' in line]
        self.assertTrue(traced, 'the debug: 4 reader logged no TX detail at all')
        self.assertFalse([line for line in traced if 'quiet_gate' in line],
                         'a reader at debug: 0 traced because another reader was at '
                         'debug: 4 - the level is per-reader, not per-machine')

    def test_level_four_is_what_turns_it_on(self):
        with self.assertLogs(READER_CHANNEL, level='INFO') as captured:
            self.send_one_command('gate4', 4)
        self.assertTrue(any('_send: TX' in line for line in captured.output),
                        'debug: 4 must produce the per-transaction TX line')

    def test_lower_levels_emit_nothing_per_transaction(self):
        handler, logger_obj = capture_records()
        logger_obj.addHandler(handler)
        previous, propagate = logger_obj.level, logger_obj.propagate
        logger_obj.setLevel(logging.INFO)
        logger_obj.propagate = False        # keep the events off the test console
        try:
            for level in (0, 3):
                self.send_one_command('gate%d' % level, level)
        finally:
            logger_obj.setLevel(previous)
            logger_obj.propagate = propagate
            logger_obj.removeHandler(handler)
        self.assertEqual([r for r in handler.records if '_send: TX' in r], [],
                         'debug: 3 and below must not emit per-transaction detail')


class TestUartContractShape(unittest.TestCase):
    """
    The transport contract is prose, not an ABC, so a partial transport dies with
    AttributeError at GCode time rather than at construction. Assert the shape.
    """

    TRANSPORT = ('_send', '_read_ack', '_recv', '_check_frame',
                 '_probe_status_ready', '_probe_fetch_ack',
                 '_probe_fetch_response', '_probe_send_abort')
    LOW_LEVEL = ('low_level_raw_write', 'low_level_raw_read',
                 'low_level_command_write', 'low_level_ready_read',
                 'low_level_ack_read')

    def test_every_contract_method_is_implemented(self):
        drv, _port, _clock = driver()
        for name in self.TRANSPORT + self.LOW_LEVEL:
            self.assertTrue(callable(getattr(drv, name, None)),
                            'PN532UARTDriver is missing %s()' % name)

    def test_probe_contract_is_advertised(self):
        drv, _port, _clock = driver()
        for name in ('probe_start', 'probe_poll', 'probe_stop'):
            self.assertTrue(callable(getattr(drv, name, None)))

    def test_low_level_ready_read_is_synthesised(self):
        """UART has no status byte; the console step still needs an answer."""
        drv, _port, _clock = driver([PN532_UART_ACK], low_level_debug=True)
        self.assertEqual(drv.low_level_ready_read(), [0x01])
        drv._framer.next_frame()                # consume it
        self.assertEqual(drv.low_level_ready_read(), [0x00])

    def test_low_level_guard_still_applies(self):
        drv, _port, _clock = driver()           # low_level_debug off
        with self.assertRaises(RuntimeError):
            drv.low_level_raw_read(4)


class TestPortLifecycle(unittest.TestCase):

    def test_construction_does_no_io(self):
        """
        Readers are built at config time, where no driver does I/O, and a missing USB
        adapter must not stop klippy from starting.
        """
        opened = []

        def factory(port, baud):
            opened.append(port)
            return fake_serial.Serial(port, baud, timeout=0)

        PN532UARTDriver('/dev/nope', name="gate0", debug=0, serial_factory=factory)
        self.assertEqual(opened, [], 'the port must not open until init()')

    def test_open_failure_names_the_port_not_the_wiring(self):
        """
        A missing adapter must not be reported as "did not respond - check wiring".
        _wake_pn532 catches per-attempt exceptions, so without init() opening the
        port up front the real cause (wrong path, no permission, unplugged) would be
        swallowed and retried three times before a generic message.
        """
        def boom(port, baud):
            raise fake_serial.SerialException('no such device')
        drv = PN532UARTDriver('/dev/nope', name="gate0", debug=0,
                              sleep_fn=lambda _s: None,
                              serial_factory=boom)
        with self.assertRaises(RuntimeError) as caught:
            drv.init()
        message = str(caught.exception)
        self.assertIn('/dev/nope', message)
        self.assertIn('cannot open', message.lower())

    def test_unplugged_adapter_closes_the_port(self):
        """
        Without this, the port object stays open around a dead fd: every read
        raises, probe_poll()'s broad except turns that into a plain False, and the
        manager restarts the scan forever with nothing reopening or reporting the
        reader dead.
        """
        drv, port, _clock = driver()
        drv._open()
        port.read = _raise_io
        drv.probe_start()
        drv.probe_poll()
        self.assertEqual(port.closes, 1, 'a read failure must close the port')
        self.assertIsNone(drv._serial)

    def test_faulted_port_fails_fast_instead_of_retrying(self):
        """
        A homing poll ticks every 20ms. Retrying open() each tick would mean a
        syscall and a log line per tick for the rest of the print.
        """
        drv, port, _clock = driver()
        drv._open()
        port.read = _raise_io
        with self.assertRaises(RuntimeError):
            drv._read_nonblocking()

        opens = []
        drv._serial_factory = lambda p, b: opens.append(p)
        for _ in range(5):
            with self.assertRaises(RuntimeError) as caught:
                drv._read_nonblocking()
        self.assertEqual(opens, [], 'a faulted port must not be reopened')
        self.assertIn('MMU_RFID_INIT', str(caught.exception),
                      'the error should name the recovery step')

    def test_init_clears_the_fault_and_reopens(self):
        """Replug then MMU_RFID_INIT is the documented recovery."""
        chip = PN532UartChip()
        clock = Clock()
        ports = []

        def factory(port, baud):
            new = fake_serial.Serial(port, baud, timeout=0)
            new.on_write = chip.on_write
            ports.append(new)
            return new

        drv = PN532UARTDriver('/dev/fake-nfc', name="gate0", debug=0,
                              sleep_fn=clock.sleep, time_fn=clock.now,
                              serial_factory=factory)
        drv.init()
        ports[0].read = _raise_io
        with self.assertRaises(RuntimeError):
            drv._read_nonblocking()
        self.assertTrue(drv._faulted)

        drv.init()                      # adapter is back
        self.assertFalse(drv._faulted)
        self.assertEqual(len(ports), 2, 'init() must reopen after a fault')

    def test_abort_after_a_read_failure_does_not_flush_the_port(self):
        """
        On a transient read error, flushing would drop bytes still in transit - the
        ordering hazard _probe_abort's own comment warns about.
        """
        drv, port, _clock = driver()
        drv._open()
        resets_before = port.resets
        port.read = _raise_io
        drv._probe_abort()
        self.assertEqual(port.resets, resets_before,
                         'a failed drain must not reach reset_input_buffer()')
        self.assertEqual(drv._framer.buffered(), b'',
                         'our own accumulator must still be cleared')

    def test_close_allows_a_reopen(self):
        chip = PN532UartChip()
        clock = Clock()
        ports = []

        def factory(port, baud):
            new = fake_serial.Serial(port, baud, timeout=0)
            new.on_write = chip.on_write
            ports.append(new)
            return new

        drv = PN532UARTDriver('/dev/fake-nfc', name="gate0", debug=0,
                              sleep_fn=clock.sleep, time_fn=clock.now,
                              serial_factory=factory)
        drv.init()
        drv.close()
        self.assertEqual(ports[0].closes, 1)
        drv.init()
        self.assertEqual(len(ports), 2, 'init() after close() must reopen the port')


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Config plumbing for the 'interface' key
# ═══════════════════════════════════════════════════════════════════════════════

class FakeConfigError(Exception):
    pass


class FakePrinter:
    """An attribute bag - the serial-port registry hangs off the printer."""


class FakeConfig:
    """Just enough ConfigWrapper for interface_from_config() and create_reader()."""

    error = FakeConfigError

    def __init__(self, name='mmu_nfc_reader gate0', printer=None, **options):
        self._name = name
        self._options = options
        self._printer = printer if printer is not None else FakePrinter()

    def get_name(self):
        return self._name

    def get(self, key, default=None):
        return self._options.get(key, default)

    def getint(self, key, default=None, minval=None, maxval=None):
        value = self._options.get(key, default)
        if value is None:
            return None
        value = int(value)
        if minval is not None and value < minval:
            raise self.error('%s below minval' % key)
        if maxval is not None and value > maxval:
            raise self.error('%s above maxval' % key)
        return value

    def getfloat(self, key, default=None, minval=None, maxval=None):
        value = self._options.get(key, default)
        return None if value is None else float(value)

    def get_printer(self):
        return self._printer


class TestInterfaceOption(unittest.TestCase):

    def setUp(self):
        from extras.mmu.unit.nfc import reader_factory
        self.factory = reader_factory

    def test_defaults_preserve_todays_behaviour(self):
        for reader_type, expected in (('pn532', 'i2c'), ('pn7160', 'i2c'),
                                      ('rc522', 'spi'), ('pn5180', 'spi')):
            self.assertEqual(
                self.factory.interface_from_config(FakeConfig(), reader_type),
                expected,
                '%s must keep the transport it has always used when "interface" '
                'is absent' % reader_type)

    def test_explicit_interface_is_honoured(self):
        for interface in ('i2c', 'spi', 'uart'):
            config = FakeConfig(interface=interface)
            self.assertEqual(
                self.factory.interface_from_config(config, 'pn532'), interface)

    def test_case_and_whitespace_are_tolerated(self):
        config = FakeConfig(interface='  UART ')
        self.assertEqual(self.factory.interface_from_config(config, 'pn532'), 'uart')

    def test_unimplemented_interface_says_not_integrated(self):
        """
        A PN7160 speaks UART in silicon, so "not supported" would send people
        hunting for a wiring fault. The message must name the real reason.
        """
        config = FakeConfig(interface='uart')
        with self.assertRaises(FakeConfigError) as caught:
            self.factory.interface_from_config(config, 'pn7160')
        self.assertIn('not integrated', str(caught.exception).lower())

    def test_nonsense_interface_is_rejected(self):
        config = FakeConfig(interface='bogus')
        with self.assertRaises(FakeConfigError) as caught:
            self.factory.interface_from_config(config, 'pn532')
        self.assertIn('invalid interface', str(caught.exception).lower())

    def test_pn532_uart_is_the_only_uart_driver(self):
        for reader_type, interfaces in self.factory.SUPPORTED_INTERFACES.items():
            if 'uart' in interfaces:
                self.assertEqual(reader_type, 'pn532')


class TestCreateReaderUart(unittest.TestCase):
    """
    The factory path, which every test above bypasses by injecting a driver
    directly. Notably this proves the UART branch never reaches MCU_I2C_from_config
    or the software-I2C validator: FakeConfig would not survive either.
    """

    BY_ID = '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'

    def setUp(self):
        from extras.mmu.unit.nfc import reader_factory
        self.factory = reader_factory
        self.printer = FakePrinter()

    def build(self, **options):
        options.setdefault('serial', self.BY_ID)
        config = FakeConfig(printer=self.printer, **options)
        return self.factory.create_reader(
            config, None, 'pn532', debug=0, interface='uart')

    def test_builds_a_uart_driver_with_the_configured_port(self):
        driver_obj = self.build()
        self.assertIsInstance(driver_obj, PN532UARTDriver)
        self.assertEqual(driver_obj._port, self.BY_ID)
        self.assertEqual(driver_obj._baud, 115200,
                         'the PN532 powers up at 115200')

    def test_baud_is_configurable(self):
        self.assertEqual(self.build(baud=230400)._baud, 230400)

    def test_missing_serial_is_a_config_error(self):
        config = FakeConfig(printer=self.printer)
        with self.assertRaises(FakeConfigError) as caught:
            self.factory.create_reader(config, None, 'pn532', debug=0,
                                       interface='uart')
        self.assertIn('serial', str(caught.exception))

    def test_unedited_menuconfig_placeholder_is_rejected(self):
        """
        menuconfig's manual-entry field defaults to "-enter-device-path-". Catching it
        here names the section at config load; letting the open fail instead happens a
        couple of seconds after bootup and only marks the reader not-alive.
        """
        for bad in ('-enter-device-path-', 'ttyUSB0', 'dev/ttyUSB0'):
            with self.assertRaises(FakeConfigError) as caught:
                self.build(serial=bad)
            self.assertIn('absolute', str(caught.exception).lower(), bad)

    def test_two_readers_cannot_share_one_port(self):
        """
        A shared tty interleaves frames on one stream with no error at all, showing
        up as intermittent read failures rather than a config problem. Same class of
        silent collision as two PN532s on one software I2C bus.
        """
        self.build()
        second = FakeConfig(name='mmu_nfc_reader gate1', printer=self.printer,
                            serial=self.BY_ID)
        with self.assertRaises(FakeConfigError) as caught:
            self.factory.create_reader(second, None, 'pn532', debug=0,
                                       interface='uart')
        self.assertIn('gate0', str(caught.exception),
                      'the error should name the reader already holding the port')

    def test_distinct_ports_are_fine(self):
        self.build()
        self.build(serial=self.BY_ID.replace('if00-port0', 'if01-port0'))

    def test_unstable_device_path_is_allowed_but_warned(self):
        """/dev/ttyUSB0 works until something else claims it first - warn, not fail."""
        with self.assertLogs(READER_CHANNEL, level='WARNING') as logged:
            driver_obj = self.build(serial='/dev/ttyUSB0')
        self.assertEqual(driver_obj._port, '/dev/ttyUSB0')
        self.assertTrue(any('by-id' in line for line in logged.output),
                        'the warning should point at the stable path: %r'
                        % logged.output)

    def test_by_id_path_does_not_warn(self):
        handler, logger_obj = capture_warnings()
        original_level = logger_obj.level
        logger_obj.addHandler(handler)
        logger_obj.setLevel(logging.WARNING)
        try:
            self.build()
        finally:
            logger_obj.removeHandler(handler)
            logger_obj.setLevel(original_level)
        self.assertEqual(handler.records, [],
                         'a by-id path is the recommended form and must be silent')

    def test_pn532_spi_warns_that_it_is_untested(self):
        """
        interface: spi wires up a driver that has never run against hardware. Without
        the warning, "no longer dead code" reads as "now usable".
        """
        sentinel = RuntimeError('spi bus not built in this test')

        def refuse(*_args, **_kwargs):
            raise sentinel

        original = self.factory.bus_module.MCU_SPI_from_config
        self.factory.bus_module.MCU_SPI_from_config = refuse
        try:
            config = FakeConfig(printer=self.printer)
            with self.assertLogs(READER_CHANNEL, level='WARNING') as logged:
                with self.assertRaises(RuntimeError):
                    self.factory.create_reader(config, None, 'pn532',
                                               debug=0, interface='spi')
        finally:
            self.factory.bus_module.MCU_SPI_from_config = original
        self.assertTrue(any('UNTESTED' in line for line in logged.output),
                        'the warning must fire before the bus is built: %r'
                        % logged.output)

    def test_registry_is_scoped_to_the_printer(self):
        """A Klipper restart builds a new printer and must not inherit collisions."""
        self.build()
        self.printer = FakePrinter()
        self.build()        # same port, fresh printer - must not raise


class TestFakeSerialRegistry(unittest.TestCase):
    """
    The registry path used by prime_uart_reader(), for the round-trip case where a
    test cannot inject a port because the driver opens its own.
    """

    def setUp(self):
        fake_serial.reset_all()

    def tearDown(self):
        fake_serial.reset_all()

    def test_preset_chunks_reach_a_lazily_opened_port(self):
        fake_serial.preset('/dev/preset-nfc', [PN532_UART_ACK])
        drv = PN532UARTDriver('/dev/preset-nfc', name="gate0", debug=0,
                              sleep_fn=lambda _s: None)
        self.assertTrue(drv._probe_status_ready(),
                        'chunks queued before the open must survive it')

    def test_fail_open_models_a_missing_adapter(self):
        fake_serial.fail_open('/dev/gone-nfc')
        drv = PN532UARTDriver('/dev/gone-nfc', name="gate0", debug=0,
                              sleep_fn=lambda _s: None)
        with self.assertRaises(RuntimeError):
            drv.init()

    def test_prime_uart_reader_seeds_by_port_name(self):
        from test.hh.nfc_fixtures import prime_uart_reader

        class StubReader:
            pass

        reader = StubReader()
        reader.reader = PN532UARTDriver('/dev/primed-nfc', name="gate0", debug=0,
                                        sleep_fn=lambda _s: None)
        self.assertTrue(prime_uart_reader(reader, [PN532_UART_ACK]))
        self.assertTrue(reader.reader._probe_status_ready())

    def test_prime_uart_reader_declines_a_non_uart_driver(self):
        from test.hh.nfc_fixtures import prime_uart_reader

        class StubReader:
            reader = object()

        self.assertFalse(prime_uart_reader(StubReader(), [PN532_UART_ACK]))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. What the installer actually writes
# ═══════════════════════════════════════════════════════════════════════════════

class TestRenderedConfig(unittest.TestCase):
    """
    The template used to choose SPI-vs-I2C by asking "is reader_type rc522 or
    pn5180?". That stopped being a transport question the moment pn532 gained a
    second one - a UART reader would have rendered a full I2C block, pins and all.
    Dispatch is on PARAM_NFC_READER_INTERFACE now, and these tests pin both halves:
    UART renders correctly, and nothing else changed.
    """

    def reader_sections(self, profile_name):
        from test.hh import cfg, profiles
        hardware = cfg.render(
            profiles.get(profile_name))['config/base/mmu_hardware.cfg']
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

    def test_common_uart_reader_renders_serial_not_pins(self):
        sections = self.reader_sections('nfc_pn532_uart')
        self.assertEqual(len(sections), 1, sections)
        keys = list(sections.values())[0]
        self.assertEqual(keys['reader_type'], 'pn532')
        self.assertEqual(keys['interface'], 'uart')
        self.assertTrue(keys['serial'].startswith('/dev/serial/by-id/'),
                        'the installer should write the stable path: %r'
                        % keys.get('serial'))
        self.assertEqual(keys['baud'], '115200')

    def test_common_uart_reader_emits_no_bus_keys(self):
        """
        THE REGRESSION THIS PINS. A pn532 that reached the I2C branch would render
        i2c_address/i2c_mcu/i2c_speed, and Klipper would then try to build an MCU I2C
        bus for a reader that has no MCU involvement at all.
        """
        keys = list(self.reader_sections('nfc_pn532_uart').values())[0]
        for forbidden in ('i2c_address', 'i2c_mcu', 'i2c_speed', 'i2c_bus',
                          'i2c_software_scl_pin', 'i2c_software_sda_pin',
                          'cs_pin', 'spi_bus', 'spi_speed'):
            self.assertNotIn(forbidden, keys,
                             'a UART reader has no bus and no pins')

    def test_per_gate_mixed_uart_and_spi(self):
        """
        Gate 0 UART, gates 1+ RC522 - mixed on purpose. The per-gate params come from
        lists built by symbol-name suffix, so a type chosen for ONE gate is where a
        missing list entry or an off-by-one shows up.
        """
        sections = self.reader_sections('nfc_pn532_uart_per_gate')
        self.assertEqual(len(sections), 4, sections)
        gate0 = sections['unit0_nfc0']
        self.assertEqual((gate0['reader_type'], gate0['interface']),
                         ('pn532', 'uart'))
        self.assertIn('serial', gate0)
        self.assertNotIn('cs_pin', gate0)
        for name in ('unit0_nfc1', 'unit0_nfc2', 'unit0_nfc3'):
            self.assertEqual(sections[name]['reader_type'], 'rc522', name)
            self.assertIn('cs_pin', sections[name], name)
            self.assertNotIn('interface', sections[name],
                             '%s uses its default transport, so the key is noise'
                             % name)

    def test_default_transports_emit_no_interface_key(self):
        """
        Every chip keeps the transport it has always had, and the rendered config
        stays byte-identical for those - `interface:` is written only when it differs
        from the chip default. Guards against churn in everyone else's config.
        """
        for profile in ('nfc_single', 'nfc_pn5180', 'nfc_pn532',
                        'nfc_pn532_sw_i2c', 'nfc_per_gate',
                        'nfc_pn5180_per_gate'):
            for name, keys in self.reader_sections(profile).items():
                self.assertNotIn('interface', keys,
                                 '%s/%s should not have gained an interface key'
                                 % (profile, name))

    def test_existing_transports_still_render_their_blocks(self):
        """The other half of the dispatch change: SPI and I2C still work."""
        spi = list(self.reader_sections('nfc_pn5180').values())[0]
        for key in ('cs_pin', 'spi_speed', 'busy_pin', 'reset_pin'):
            self.assertIn(key, spi, 'pn5180 lost %s' % key)
        i2c = list(self.reader_sections('nfc_pn532').values())[0]
        for key in ('i2c_mcu', 'i2c_address', 'i2c_speed', 'i2c_bus'):
            self.assertIn(key, i2c, 'pn532/i2c lost %s' % key)


class TestUartReaderBoots(unittest.TestCase):
    """
    End to end: rendered config -> reader_factory -> PN532UARTDriver -> alive.

    Everything above either injects a driver or calls the factory with a fake config.
    This is the only test that proves the whole chain, including that the harness's
    fake pyserial is reachable from a lazily-opened port via prime_uart_reader().
    """

    def test_uart_profile_boots_a_live_reader(self):
        from test.hh.bootstrap import Session
        session = Session(profile='nfc_pn532_uart')
        try:
            session.boot()
            unit = session.printer.lookup_object('mmu_machine').units[0]
            reader = unit.nfc_manager.shared_reader
            self.assertIsNotNone(reader, 'the shared reader should be wired up')
            self.assertEqual(reader.reader_type, 'pn532')
            self.assertEqual(reader.interface, 'uart')
            self.assertIsInstance(reader.reader, PN532UARTDriver)
            self.assertTrue(reader.alive,
                            'the reader should initialise against the scripted port')
            self.assertEqual(reader.get_status()['interface'], 'uart',
                             'macros should be able to see the transport')
        finally:
            session.close()

    def test_mixed_per_gate_profile_boots(self):
        """
        Gate 0 on UART alongside RC522 gates. A different code path from the shared
        reader: per-gate readers go through gate_readers and get an MmuNfcEndstop
        each, and prime_reader has to dispatch UART-vs-SPI per gate.
        """
        from test.hh.bootstrap import Session
        session = Session(profile='nfc_pn532_uart_per_gate')
        try:
            session.boot()
            unit = session.printer.lookup_object('mmu_machine').units[0]
            readers = unit.nfc_manager.gate_readers
            self.assertEqual(len(readers), 4, 'one reader per gate')
            self.assertEqual(readers[0].interface, 'uart')
            self.assertIsInstance(readers[0].reader, PN532UARTDriver)
            for gate in (1, 2, 3):
                self.assertEqual(readers[gate].reader_type, 'rc522', gate)
                self.assertEqual(readers[gate].interface, 'spi', gate)
            # Both transports must come up live off their scripted buses: the UART gate
            # against its preset port, the RC522 gates against their SPI scripts.
            self.assertTrue(all(r.alive for r in readers),
                            'every reader should initialise against its scripted bus, '
                            'got alive=%r' % ([r.alive for r in readers],))
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
