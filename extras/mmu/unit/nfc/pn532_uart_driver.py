# klippy/extras/mmu/unit/nfc/pn532_uart_driver.py
#
# PN532 HSU (High Speed UART) transport, over a host serial port.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# PN532 HSU protocol overview
# ───────────────────────────
# Plain asynchronous serial, 8N1, 115200 baud by default. The breakout board's
# SEL0/SEL1 pads select HSU (SEL0=0, SEL1=1) — see pn532_driver.py for the full
# table. There is no direction byte and no status byte: the host writes a command
# frame and the chip writes back an ACK frame, then (later) a response frame.
#
#   Write frame: [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, params..., DCS, 0x00]
#   Read  frame: [0x00, 0x00, 0xFF, LEN, LCS, TFI, CMD, data...,   DCS, 0x00]
#   ACK  frame:  [0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00]
#   NACK frame:  [0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00]
#
# That is the same shape as SPI minus the direction byte and the LSB-first bit
# reversal, so check_preambled_frame() is shared with PN532SPIDriver.
#
# Why this transport is structurally different
# ────────────────────────────────────────────
# I2C and SPI are QUERY-THEN-READ: the host asks "ready?", then reads exactly the
# number of bytes it wants. UART has neither half. Bytes arrive when they arrive,
# a frame can straddle several reactor ticks, and a response the host never asked
# for still lands in the OS receive buffer. So a naive port of the I2C transport
# has three latent bugs, and _HSUFrameReader below exists to close all three:
#
#   1. "Bytes available" is NOT "a frame is ready". At 115200 a 32-byte response
#      takes ~2.8ms to arrive. Acting on a partial frame yields a driver that
#      works on a fast host and fails on a slow one.
#   2. A second read gets nothing. On I2C, _probe_fetch_ack() is its own bus
#      transaction; here the bytes were already consumed by the pump.
#   3. The stream needs resync. The chip emits leading 0x00 padding, and an
#      abandoned exchange leaves a whole late frame in the buffer.
#
# NOT MCU-mediated
# ────────────────
# Every other reader transport in Happy Hare goes through Klipper's bus.py and is
# therefore driven by the MCU. This one is not: it opens a host serial port inside
# the klippy process, because Klipper has no generic host-accessible UART
# primitive for arbitrary devices. The precedent is mainline Klipper's
# extras/palette2.py, the only other host-I/O extras module: a non-blocking port
# (timeout=0) polled from reactor timers.
#
# Consequences, all load-bearing:
#   - The port must be non-blocking and every wait must yield via self._sleep
#     (reactor.pause). A blocking read here stalls the printer, not just the poll.
#   - Each reader needs its own tty, so HSU suits the shared-reader / low-gate
#     case. Software I2C is the answer for a reader-per-gate build.
#   - pyserial is imported lazily, inside _open(). It is not a Happy Hare
#     dependency (it IS in Klipper's own klippy-requirements.txt, so a real
#     install has it) and it is absent from the test venv.

from .log import logger
from .pn532_driver import (
    _PN532Base,
    _MAX_RESPONSE_BYTES,
    PN532_ACK,
    check_preambled_frame,
    _hex,
)

# Frame start code, after any leading padding.
_START_CODE = b'\x00\xFF'

# The two bytes that follow the start code, which classify the frame.
_TAIL_ACK  = b'\x00\xFF'
_TAIL_NACK = b'\xFF\x00'
_TAIL_EXT  = b'\xFF\xFF'

FRAME_ACK  = 'ack'
FRAME_NACK = 'nack'
FRAME_INFO = 'info'

DEFAULT_BAUD = 115200


class _HSUFrameReader:
    """
    Byte-stream to PN532-frame reassembler for HSU/UART.

    This class is the whole difference between UART and the query-then-read
    transports. It owns an accumulator, discards leading padding and any garbage
    before a start code, holds partial frames across reactor ticks, verifies both
    checksums, and hands out only COMPLETE frames — normalised into the
    preamble-first shape so check_preambled_frame()'s fixed offsets apply.

    It never blocks, never sleeps and never consults a clock. pump() is the ONLY
    method that touches the port; every extraction path works out of the
    accumulator alone. That split is what makes the two probe invariants hold
    (see PN532UARTDriver._probe_status_ready / _probe_fetch_ack).
    """

    MAX_BUFFER = 512    # Past this we are desynced; drop the oldest bytes

    def __init__(self, read_fn, log_fn=None, max_buffer=MAX_BUFFER):
        self._read = read_fn            # callable(max_bytes) -> bytes, non-blocking
        self._log  = log_fn if log_fn is not None else (lambda *a: None)
        self._buf  = bytearray()
        self._max  = max_buffer
        # Diagnostics, and the assertion surface for the framer tests. Growth in
        # these on a healthy link means the wiring or baud rate is wrong.
        self.discarded  = 0             # Bytes thrown away as padding/garbage
        self.bad_frames = 0             # LCS/DCS/extended-frame rejects
        self.frames     = 0             # Complete frames handed out

    # ── The only method that reads the port ──────────────────────────────────

    def pump(self, max_bytes=256):
        """
        Pull whatever bytes are already available into the accumulator.

        Returns the number of bytes added; 0 is normal and not an error.
        """
        data = self._read(max_bytes) or b''
        if data:
            self._buf.extend(data)
            if len(self._buf) > self._max:
                drop = len(self._buf) - self._max
                del self._buf[:drop]
                self.discarded += drop
                self._log("framer overflow: dropped %d byte(s)", drop)
        return len(data)

    # ── Extraction: accumulator only, never the port ─────────────────────────

    def peek_frame(self):
        """
        Return (kind, frame_bytes, consumed) for the frame at the head of the
        accumulator, or None if no COMPLETE frame is buffered yet.

        Does not consume the frame. Bytes ahead of it (padding, garbage, rejected
        frames) ARE dropped — that is resync, not consumption, and it is what
        guarantees forward progress.
        """
        while True:
            if not self._sync():
                return None
            buf = self._buf
            if len(buf) < 5:            # 00 FF + 2 classifier bytes + postamble
                return None
            tail = bytes(buf[2:4])
            if tail == _TAIL_ACK or tail == _TAIL_NACK:
                kind = FRAME_ACK if tail == _TAIL_ACK else FRAME_NACK
                return (kind, b'\x00' + bytes(buf[:5]), 5)
            if tail == _TAIL_EXT:
                # No command this driver issues can answer with more than 255
                # bytes, so an extended frame is garbage by definition. Drop the
                # start code and classifier: guaranteed forward progress, and no
                # second checksum path to get wrong.
                self.bad_frames += 1
                self._log("framer: extended frame rejected")
                self._drop(4)
                continue
            length, lcs = buf[2], buf[3]
            if (length + lcs) & 0xFF:
                # Bad length checksum, so this was not really a frame start.
                self.bad_frames += 1
                self._drop(2)
                continue
            need = length + 5           # 00 FF | LEN | LCS | data... | DCS
            if len(buf) < need:
                return None             # Partial frame; try again after the next pump
            data = bytes(buf[4:4 + length])
            if (sum(data) + buf[4 + length]) & 0xFF:
                self.bad_frames += 1
                self._drop(2)
                continue
            # The postamble is treated as OPTIONAL. A real chip always sends it,
            # but it can lag the DCS by a byte time, and waiting for it would cost
            # every exchange one whole poll interval. If it has not arrived, the
            # stray 0x00 is simply discarded as padding ahead of the next frame.
            consumed = need
            if len(buf) > need and buf[need] == 0x00:
                consumed += 1
            return (FRAME_INFO, b'\x00' + bytes(buf[:need]), consumed)

    def consume(self, n):
        if n > 0:
            del self._buf[:n]

    def next_frame(self):
        """peek_frame() then consume it. Returns (kind, frame_bytes) or None."""
        got = self.peek_frame()
        if got is None:
            return None
        kind, frame, consumed = got
        self.consume(consumed)
        self.frames += 1
        return kind, frame

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _sync(self):
        """
        Drop everything before the first start code. True if one is present.

        find() on the two-byte start code lands correctly however much 0x00
        padding preceded it: in 00 00 00 FF it matches at the LAST zero.
        """
        i = self._buf.find(_START_CODE)
        if i < 0:
            # Keep a trailing lone 0x00 - it may be the first half of a start
            # code split across two reads.
            keep = 1 if self._buf[-1:] == b'\x00' else 0
            drop = len(self._buf) - keep
            if drop > 0:
                self.discarded += drop
                del self._buf[:drop]
            return False
        if i:
            self.discarded += i
            del self._buf[:i]
        return True

    def _drop(self, n):
        self.discarded += n
        del self._buf[:n]

    def take_raw(self, n):
        """Consume up to n unparsed bytes. For low_level_raw_read() only."""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def reset(self):
        """Discard the accumulator. Logs what was thrown away - a non-empty
        buffer at a reset point is a real diagnostic signal, not noise."""
        if self._buf:
            self._log("framer reset: dropping %s", _hex(self._buf, ' '))
            self.discarded += len(self._buf)
        self._buf = bytearray()

    def buffered(self):
        return bytes(self._buf)


class PN532UARTDriver(_PN532Base):
    """
    Driver for one PN532 NFC reader connected over HSU/UART on a host serial port.

    The public interface is identical to PN532Driver (I2C).

    Parameters
    ----------
    serial_port : str
        Device path, e.g. /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0.
        Prefer a by-id path: /dev/ttyUSB0 is not stable across reboots.
    name : str
        The reader's config section name ([mmu_nfc_reader <name>]), used only
        as a logging label.
    baud : int
        Port speed. The PN532 powers up at 115200; anything faster needs a
        SetSerialBaudRate command this driver does not issue.
    transceive_delay : float
        Seconds to wait for an InListPassiveTarget result.
    crc_delay : float
        Seconds to wait after InRelease.
    debug : int
        0 = silent, 1 = major events, 4 = full trace.
    serial_factory : callable or None
        Test seam: callable(port, baud) returning a pyserial-like object. When
        None (production) the port is opened with pyserial in _open().
    """

    # Bound the "discard a frame that isn't the one I want and keep looking" path
    # in _await(), so a chip babbling valid-but-wrong frames cannot spin forever
    # inside one call. The timeout bounds it too; this bounds it per-poll.
    _MAX_SKIPPED_FRAMES = 8

    # _probe_abort() budget. See the method for why these are this small.
    _ABORT_QUIET_TIME = 0.005   # No new bytes for this long means the stream is idle
    _ABORT_MAX_TIME   = 0.020   # Hard ceiling, matching the I2C abort cost

    # HSU wake burst. The PN532's UART receiver needs a dummy 0x55 preamble to
    # resynchronise after power-up or a low-VBAT idle before it will see a frame.
    _WAKE_PREAMBLE = bytes([0x55, 0x55] + [0x00] * 14)
    _WAKE_SETTLE   = 0.020      # Chip needs ~2ms; 20ms is cheap insurance

    def __init__(self, serial_port, name,
                 baud=DEFAULT_BAUD,
                 transceive_delay=0.250,
                 crc_delay=0.050,
                 debug=1,
                 low_level_debug=False,
                 sleep_fn=None,
                 time_fn=None,
                 serial_factory=None):
        self._port           = serial_port
        self._baud           = baud
        self._serial         = None
        self._serial_factory = serial_factory
        # Set when an I/O error takes the port down (adapter unplugged, USB reset).
        # Blocks reopening until init() runs, so a homing poll cannot hammer open()
        # every 20ms for the rest of the print - see _on_io_error().
        self._faulted        = False
        self._fault_reason   = ''
        self._transport_name = 'pn532/uart'
        self._framer = _HSUFrameReader(self._read_nonblocking,
                                       log_fn=self._framer_log)
        super().__init__(name, transceive_delay, crc_delay, debug, low_level_debug,
                         sleep_fn=sleep_fn, time_fn=time_fn)

    # UART frames start at the preamble with no status or direction byte, exactly
    # like SPI. Note the two-party invariant documented on check_preambled_frame:
    # it verifies no checksums, and relies on the framer having done so.
    _check_frame = staticmethod(check_preambled_frame)

    def _framer_log(self, fmt, *args):
        if self._debug >= 4:
            logger.info("[%s %s] framer: " + fmt,
                        *((self._name, self._transport_name) + args))

    # ─────────────────────────────────────────────────────────────────────────
    # Port lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _open(self):
        """
        Open the serial port if it is not already open. Idempotent.

        Deliberately lazy, and deliberately NOT called from __init__: reader
        construction happens at config time, where no driver does I/O, and a
        missing USB adapter must not stop klippy from starting. The first real
        open therefore happens inside init(), whose RuntimeError the NFC manager
        already handles (it initialises readers a couple of seconds after MMU
        bootup, not at klippy:connect).
        """
        if self._serial is not None:
            return self._serial
        if self._faulted:
            # Fail fast rather than retrying the open. A homing poll runs every
            # 20ms, and an unplugged adapter would otherwise mean an open() syscall
            # and a log line per tick for the rest of the print.
            raise RuntimeError(
                "[%s pn532/uart] %s went away (%s). Reconnect it and run "
                "MMU_RFID_INIT to reopen."
                % (self._name, self._port, self._fault_reason))

        # The factory shares this error wrapping deliberately: a test seam that
        # reported failures differently from production would be testing something
        # other than what ships.
        try:
            self._serial = (self._serial_factory(self._port, self._baud)
                            if self._serial_factory is not None
                            else self._open_pyserial())
        except Exception as e:
            self._serial = None
            raise RuntimeError(
                "[%s pn532/uart] cannot open %s at %d baud: %s"
                % (self._name, self._port, self._baud, e))

        logger.info("[%s %s] _open: opened %s at %d baud",
                 self._name, self._transport_name, self._port, self._baud)
        # Opening a USB-CDC port asserts DTR/RTS, which resets some breakout
        # boards and can leave junk in the buffer. Start from a clean stream.
        self._flush_input()
        return self._serial

    def _open_pyserial(self):
        """Open the real port. Only reached in production, never under test."""
        # Lazy import: pyserial is not a Happy Hare dependency, so a module-scope
        # import would break every install that does not use a UART reader (and the
        # whole test suite, whose venv has no pyserial). Same pattern as
        # mmu_nfc_reader's deferred tag_parser import.
        import serial

        # timeout=0 is what makes read() non-blocking, which the whole design rests
        # on - see the concurrency note at the top of this file.
        kwargs = {'timeout': 0, 'write_timeout': 0.5}
        try:
            # exclusive=True stops a second klippy or a stray minicom from
            # interleaving frames on the same tty. Added in pyserial 3.3.
            return serial.Serial(self._port, self._baud, exclusive=True, **kwargs)
        except TypeError:
            return serial.Serial(self._port, self._baud, **kwargs)

    def init(self):
        """Open the port, then run the standard wake + SAMConfiguration sequence.

        The explicit _open() matters for the error message, not the timing: _write
        and the framer's read hook both open lazily, but they are reached from
        inside _wake_pn532's retry loop, which catches per-attempt exceptions. A
        genuinely missing or unopenable port would then surface as the generic
        "did not respond - check wiring" after three attempts, hiding the actual
        cause (wrong path, no permission, adapter unplugged). Opening here lets
        that error propagate verbatim.
        """
        # Clear a previous fault so a reconnected adapter can be picked up. This is
        # the ONLY thing that clears the latch, which is what makes MMU_RFID_INIT
        # the documented recovery step rather than a per-tick retry.
        self._faulted = False
        self._open()
        return super(PN532UARTDriver, self).init()

    def close(self):
        """Close the port so a later init() can reopen it. Safe to call twice."""
        port, self._serial = self._serial, None
        self._framer.reset()
        if port is None:
            return
        try:
            port.close()
        except Exception as e:
            if self._debug >= 4:
                logger.info("[%s %s] close: close failed: %s",
                            self._name, self._transport_name, e)

    def _flush_input(self):
        """Drop both buffered layers: our accumulator and the OS receive buffer."""
        self._framer.reset()
        if self._serial is None:
            return
        try:
            self._serial.reset_input_buffer()
        except Exception as e:
            if self._debug >= 4:
                logger.info("[%s %s] _flush_input: failed: %s",
                            self._name, self._transport_name, e)

    def _on_io_error(self, operation, exc):
        """
        Drop the port after an I/O failure and re-raise as RuntimeError.

        This is what makes an unplugged adapter recoverable. Without it the port
        object stays open around a dead file descriptor: every read raises, the
        error surfaces through probe_poll()'s broad except as a plain False, and
        MmuNfcManager restarts the scan forever with nothing ever reopening or
        reporting the reader dead.

        Closing here plus the _faulted latch in _open() turns that into: one clear
        message, then cheap failures, then a clean recovery on MMU_RFID_INIT (which
        clears the latch) once the adapter is back.
        """
        self._faulted = True
        self._fault_reason = str(exc)
        logger.error("[%s %s] _on_io_error: %s failed on %s: %s - closing the port. "
                  "Reconnect and run MMU_RFID_INIT to reopen.",
                  self._name, self._transport_name, operation, self._port, exc)
        self.close()
        raise RuntimeError(
            "[%s pn532/uart] %s failed on %s: %s"
            % (self._name, operation, self._port, exc))

    def _read_nonblocking(self, max_bytes=256):
        """The framer's read hook. timeout=0 makes read() return immediately."""
        port = self._open()
        try:
            return port.read(max_bytes)
        except (OSError, IOError) as e:
            # pyserial's SerialException subclasses IOError (== OSError on py3), so
            # this catches both a raw fd error and pyserial's own wrapper without
            # importing pyserial to name the class.
            self._on_io_error('read', e)

    def _write(self, data):
        # _open() here too, not just in _send: _probe_send_abort and the wake
        # preamble write without going through _send.
        port = self._open()
        try:
            port.write(bytes(data))
        except (OSError, IOError) as e:
            self._on_io_error('write', e)

    # ─────────────────────────────────────────────────────────────────────────
    # UART transport
    # ─────────────────────────────────────────────────────────────────────────

    def _send(self, cmd_and_params):
        """Write a command frame to the PN532."""
        frame = bytes(self._build_frame(cmd_and_params))
        if self._debug >= 4:
            logger.info("[%s %s] _send: TX  cmd=0x%02X  frame=%s",
                        self._name, self._transport_name, cmd_and_params[0],
                        _hex(frame, ' '))
        # A new command starts a new exchange, so anything still buffered belongs
        # to the previous one and can only be mistaken for this command's ACK. On
        # I2C an unread response waits inside the chip and cannot do this; here it
        # is already in the OS buffer, which is why this flush has no I2C analogue.
        self._open()
        self._flush_input()
        self._write(frame)

    def _await(self, match_fn, timeout, poll_interval, what):
        """
        Yield the reactor until match_fn accepts a frame. Returns whatever
        match_fn returned, or None on timeout.

        match_fn(kind, frame) returns None to mean "not the frame I want, keep
        looking"; anything else ends the wait.

        Frames match_fn rejects are DISCARDED rather than left in place, and that
        is the one deliberate behavioural difference from the I2C and SPI
        transports. There, a stale response sits in the chip and _recv() can fail
        fast the moment _check_frame() rejects a read. Here the OS buffer can
        legitimately hold a late frame from an abandoned exchange, so skipping it
        and continuing is the correct move. Bounded twice: by timeout, and by
        _MAX_SKIPPED_FRAMES.
        """
        deadline = self._now() + timeout
        skipped = 0
        while True:
            try:
                self._framer.pump()
            except Exception as e:
                logger.error("[%s %s] _await(%s): read failed: %s",
                          self._name, self._transport_name, what, e)
                return None
            while skipped <= self._MAX_SKIPPED_FRAMES:
                got = self._framer.next_frame()
                if got is None:
                    break
                result = match_fn(*got)
                if result is not None:
                    return result
                skipped += 1
                if self._debug >= 3:
                    logger.info("[%s %s] _await(%s): discarded a %s frame: %s",
                                self._name, self._transport_name, what,
                                got[0], _hex(got[1], ' '))
            if self._now() >= deadline:
                if self._debug >= 4:
                    logger.info("[%s %s] _await(%s): timeout after %.3fs "
                                "(buffered=%s)", self._name,
                                self._transport_name, what, timeout,
                                _hex(self._framer.buffered(), ' ') or '(empty)')
                return None
            self._sleep(poll_interval)

    def _read_ack(self, timeout=1.0, poll_interval=0.005):
        """Wait for the ACK frame that follows a command write."""
        def match(kind, frame):
            if kind == FRAME_ACK:
                return True
            if kind == FRAME_NACK:
                # A definite answer: the chip rejected the frame. Return False so
                # the wait ends here instead of burning the whole timeout.
                logger.warning("[%s %s] _read_ack: chip sent NACK",
                            self._name, self._transport_name)
                return False
            return None     # An info frame: left over from an earlier exchange
        return bool(self._await(match, timeout, poll_interval, 'ack'))

    def _recv(self, expected_cmd_resp, read_len=_MAX_RESPONSE_BYTES,
              timeout=1.0, poll_interval=0.005):
        """
        Wait for the response frame matching expected_cmd_resp.

        read_len is accepted for interface compatibility and IGNORED: a UART frame
        carries its own length, so there is no host-chosen read size to honour.
        Do not "fix" this by truncating to read_len - that would corrupt any
        response longer than the caller's guess.
        """
        def match(kind, frame):
            if kind != FRAME_INFO:
                return None
            payload = self._check_frame(frame, expected_cmd_resp)
            if payload is None and self._debug >= 3:
                # A PN532 application error frame (TFI 0x7F) lands here too, and
                # is diagnostically very different from silence - log the bytes.
                logger.info("[%s %s] _recv: frame did not match "
                            "expect=0x%02X raw=%s", self._name,
                            self._transport_name, expected_cmd_resp,
                            _hex(frame, ' '))
            return payload
        return self._await(match, timeout, poll_interval, 'recv')

    # ── Non-blocking probe primitives (see _PN532Base) ───────────────────────

    def _probe_status_ready(self):
        """
        True only when a COMPLETE frame is buffered.

        INVARIANT: on I2C this reads the chip's status byte, but on UART "bytes
        are available" is not the same question. A 32-byte response takes ~2.8ms
        to arrive at 115200 and straddles several reactor ticks, so a naive
        in_waiting > 0 would let probe_poll() run _probe_fetch_ack() against half
        a frame, fail the ACK check and fire _probe_abort(). This pumps the port -
        the only read on the probe path - and asks the framer whether a whole
        frame has landed. It consumes nothing.
        """
        self._framer.pump()
        return self._framer.peek_frame() is not None

    def _probe_fetch_ack(self):
        """
        Read and validate the ACK frame.

        INVARIANT: consumes from the accumulator and never re-reads the port. On
        I2C this is its own bus transaction; here the bytes are already in hand,
        and a second read would return nothing. probe_poll() only calls this after
        _probe_status_ready() returned True, so a complete frame is present.
        """
        got = self._framer.next_frame()
        if got is None:
            return False
        kind, frame = got
        return kind == FRAME_ACK and list(frame) == PN532_ACK

    def _probe_fetch_response(self, expected_cmd_resp, read_len):
        """Parse a buffered response frame. Consumes; never re-reads the port."""
        got = self._framer.next_frame()
        if got is None or got[0] != FRAME_INFO:
            return None
        return self._check_frame(got[1], expected_cmd_resp)

    def _probe_send_abort(self):
        """Write a bare ACK frame to cancel the command in flight."""
        self._write(PN532_ACK)

    def _probe_abort(self):
        """
        Cancel a command in flight and drain the late response it may leave.

        UART override because the hazard is different: on I2C an abandoned
        InListPassiveTarget response waits unread inside the chip, but here the
        chip pushes it into the OS receive buffer whether we want it or not.

        BUDGET. This runs on a reactor tick during a drip-homing move, and on an
        empty gate it runs about every 2 seconds for the whole move: no response
        ever arrives (MxRtyPassiveActivation retries forever), so _PROBE_WATCHDOG
        fires, and MmuNfcManager._homing_poll restarts the scan on False. The I2C
        abort is capped at 4 x 5ms; this matches that, because generosity here
        would reintroduce exactly the reactor-hogging the probe split removed.

        It can afford to be cheap because correctness rests on FRAME-KIND
        FILTERING, not on the drain: anything that survives is classified and
        discarded for free by _probe_fetch_response and _await. The drain is an
        optimisation, not the guarantee.
        """
        self._probe_stage = None
        try:
            self._probe_send_abort()
        except Exception as e:
            if self._debug >= 4:
                logger.info("[%s %s] _probe_abort: abort write failed: %s",
                            self._name, self._transport_name, e)
            # Fall through and still drain: the stale bytes are the real hazard.

        deadline   = self._now() + self._ABORT_MAX_TIME
        idle_until = self._now() + self._ABORT_QUIET_TIME
        while self._now() < deadline:
            try:
                arrived = self._framer.pump()
            except Exception as e:
                # The read failed, so there is nothing to drain and the port may
                # already be closed by _on_io_error. Drop our own accumulator and
                # return WITHOUT touching reset_input_buffer(): on a transient error
                # that would flush mid-transit, which is the very ordering hazard
                # the comment below warns about.
                if self._debug >= 4:
                    logger.info("[%s %s] _probe_abort: drain read failed: %s",
                                self._name, self._transport_name, e)
                self._framer.reset()
                return
            if arrived:
                idle_until = self._now() + self._ABORT_QUIET_TIME
            elif self._now() >= idle_until:
                break
            self._sleep(0.002)

        # ORDER MATTERS: flush only AFTER the quiet window. reset_input_buffer()
        # drops what the OS already holds, so flushing first just moves the
        # garbage - a 20-byte frame is still ~1.7ms in transit at 115200.
        self._flush_input()

    # ─────────────────────────────────────────────────────────────────────────
    # Wake
    # ─────────────────────────────────────────────────────────────────────────

    def _transport_wake_preamble(self):
        """Send the HSU 0x55 resync burst before a GetFirmwareVersion attempt."""
        self._open()
        if self._debug >= 4:
            logger.info("[%s %s] _transport_wake_preamble: TX %s",
                        self._name, self._transport_name,
                        _hex(self._WAKE_PREAMBLE, ' '))
        self._write(self._WAKE_PREAMBLE)
        self._sleep(self._WAKE_SETTLE)
        # The burst can bounce bytes back; clear them so they cannot be read as
        # the GetFirmwareVersion ACK.
        self._flush_input()

    # ─────────────────────────────────────────────────────────────────────────
    # Low-level UART debug tools
    # ─────────────────────────────────────────────────────────────────────────
    #
    # All five exist because the transport contract is prose, not an ABC: a
    # partial transport dies with AttributeError at GCode time rather than at
    # construction. Two UART-specific caveats for anyone driving these from the
    # console:
    #
    #   - init() returns early when low_level_debug is set, so _wake_pn532 never
    #     runs and the 0x55 burst is never sent. STEP=WAKEUP writes the I2C [0x00]
    #     instead. Send the burst by hand first:
    #       MMU_RFID_RAW_WRITE DATA="55 55 00 00 00 00 00 00"
    #   - low_level_raw_read returns bytes UNPARSED, including the chip's leading
    #     0x00 padding. The response pretty-printer auto-detects an I2C status
    #     byte by testing whether the first three bytes are 00 00 FF, which two or
    #     more padding zeros defeats. Read the hex, not the decode.

    def low_level_raw_write(self, data):
        """Write raw bytes to the port, bypassing framing and ACK handling."""
        self._require_low_level_debug()
        payload = [b & 0xFF for b in data]
        self._write(payload)
        return payload

    def low_level_raw_read(self, length):
        """Read up to length buffered bytes, unparsed and unframed."""
        self._require_low_level_debug()
        self._framer.pump()
        return list(self._framer.take_raw(length))

    def low_level_command_write(self, cmd_and_params):
        """Build and write a PN532 command frame without reading ACK/response."""
        self._require_low_level_debug()
        frame = self.low_level_command_frame(cmd_and_params)
        self._write(frame)
        return frame

    def low_level_ready_read(self):
        """
        Synthesised ready/busy byte: [0x01] ready, [0x00] busy.

        UART has no status byte at all. This reports whether a COMPLETE frame is
        buffered, so the console's "ready?" step reads the same as on I2C/SPI.
        """
        self._require_low_level_debug()
        return [0x01] if self._probe_status_ready() else [0x00]

    def low_level_ack_read(self, length=6):
        """Console-style ACK probe: check ready, then read the ACK bytes."""
        self._require_low_level_debug()
        ready = self.low_level_ready_read()
        if not ready or ready[0] != 0x01:
            return ready, []
        return ready, self.low_level_raw_read(length)
