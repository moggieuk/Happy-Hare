# Happy Hare test harness - fake pyserial.
#
# Lands at <klippy>/serial.py in the overlay (test/hh/root.py symlinks every .py
# under klippy_root/ preserving its relative path), and the overlay sits at
# sys.path[0]. So PN532UARTDriver's lazy `import serial` resolves here.
#
# It SHADOWS pyserial process-wide for the whole test session, and wins from sys.path[0]
# even where a real pyserial exists - which it does when the tests run under klipper's
# klippy-env (klippy-requirements.txt pins pyserial 3.4), the default on a printer. That is
# intended: nothing here wants the real one. But it does mean the shadow is load-bearing
# rather than merely unopposed, so if pyserial ever becomes a test dependency this file has
# to go and the constructor seam below becomes the only injection route.
#
# Only the surface PN532UARTDriver actually touches is implemented: read, write,
# reset_input_buffer, in_waiting, close.
#
# SCRIPT-DRIVEN, NOT TIME-DRIVEN, and that is the whole trick
# ───────────────────────────────────────────────────────────
# The driver's deadlines run on a clock (time.time, or an injected fake) while the
# harness reactor's clock is virtual. Anything here that consulted a clock would
# couple the two and make tests depend on real elapsed time. So nothing does.
#
# Instead, ONE QUEUED CHUNK becomes readable per read()/in_waiting call. Chunk
# boundaries are how a test models a frame that straddles reactor ticks, which is
# exactly the case _HSUFrameReader exists to handle, and it makes the number of
# poll iterations a driver needs deterministic and assertable. An exhausted script
# returns b'' forever - "no bytes yet", not an error.
#
# PREFER THE CONSTRUCTOR SEAM. PN532UARTDriver takes serial_factory=, so most
# tests should hand it a Serial() directly (see test_mmu_nfc_uart.py) and never
# touch this module's registry. The registry exists for the reader_factory ->
# Session round-trip, where nothing can inject. Its state is module-level and
# survives the whole session, so call reset_all() in setUp if you use it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from collections import deque


class SerialException(IOError):
    """Mirrors serial.SerialException."""


class SerialTimeoutException(SerialException):
    """Mirrors serial.SerialTimeoutException."""


# port name -> live Serial instance, so a round-trip test can find the port the
# driver opened for itself and inspect or feed it.
_PORTS = {}
# port name -> chunks queued BEFORE anything opened that port.
_PRESET = {}
# port names whose open() must raise, for the missing-adapter path.
_FAIL = set()


def preset(port, chunks):
    """Queue read chunks for a port that has not been opened yet."""
    _PRESET.setdefault(port, deque()).extend(bytes(c) for c in chunks)


def fail_open(port):
    """Make the next Serial(port) raise SerialException."""
    _FAIL.add(port)


def allow_open(port):
    _FAIL.discard(port)


def get_port(port):
    """The live Serial for a port, or None if nothing has opened it."""
    return _PORTS.get(port)


def reset_all():
    """Drop all module state. Call from setUp when using the registry."""
    _PORTS.clear()
    _PRESET.clear()
    _FAIL.clear()


class Serial:
    """A scripted, non-blocking stand-in for serial.Serial."""

    def __init__(self, port=None, baudrate=9600, timeout=None,
                 write_timeout=None, exclusive=None, **kwargs):
        if port in _FAIL:
            raise SerialException("could not open port %s" % port)
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.exclusive = exclusive
        self.is_open = True

        # Assertion surface.
        self.writes = []        # every frame written, in order
        self.reads = 0          # read() call count
        self.resets = 0         # reset_input_buffer() call count
        self.dropped = []       # bytes each reset_input_buffer() threw away
        self.closes = 0

        # Reactive chip hook: callable(self, data) invoked on every write, so a
        # model can queue its reply. Preferred over a flat script when the test is
        # about command ORDER rather than byte boundaries.
        self.on_write = None

        self.script = _PRESET.pop(port, None) or deque()
        self._pending = bytearray()
        _PORTS[port] = self

    # -- internals ----------------------------------------------------------

    def _fill(self):
        """Make the next queued chunk readable, if nothing is pending.

        Only pops when _pending is empty, so in_waiting followed by read() still
        consumes exactly one chunk. That is what gives "one chunk per pump()".
        """
        if not self._pending and self.script:
            self._pending.extend(self.script.popleft())

    # -- pyserial surface ---------------------------------------------------

    @property
    def in_waiting(self):
        self._fill()
        return len(self._pending)

    def read(self, size=1):
        self.reads += 1
        self._fill()
        out = bytes(self._pending[:size])
        del self._pending[:size]
        return out

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if self.on_write is not None:
            self.on_write(self, data)
        return len(data)

    def reset_input_buffer(self):
        self.resets += 1
        self.dropped.append(bytes(self._pending))
        self._pending = bytearray()

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        self.closes += 1
        self.is_open = False

    # -- test-facing --------------------------------------------------------

    def feed(self, *chunks):
        """Append read chunks. One becomes readable per read() call."""
        for chunk in chunks:
            self.script.append(bytes(chunk))
        return self

    def pending(self):
        """Bytes readable right now, without consuming them."""
        return bytes(self._pending)
