# Happy Hare test harness - driver-level tests for the non-blocking presence probe.
#
# test_mmu_nfc_scan.py covers the probe through the manager, against a VirtualNfcChip
# faked at the DRIVER boundary. That proves the orchestration but says nothing about the
# register sequences the real chips need - and those are where this change is riskiest,
# because a homing probe has to talk to the chip in halves.
#
# Two chips are covered here, each with a different way of getting the same answer.
#
# RC522 (below): the REAL driver against the scripted SPI bus
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
# PN7160 (TestPn7160* at the bottom): riskier still, because in polled mode its "nothing
# there" answer IS an I2C error - the chip NACKs a read when it has nothing to say. Get
# that taxonomy wrong in either direction and the feature is worse than useless: treat a
# NACK as a fault and every empty tick kills the reader mid-home; treat a real fault as a
# NACK and a wedged chip is never noticed. Those tests pin the mapping, and pin that
# silence alone never triggers a discovery restart.
#
#   ./venv/bin/python -m unittest test.test_mmu_nfc_probe
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from collections import deque

from test.hh.bootstrap import install

install()

from extras.mmu.unit.nfc import rc522_driver
from extras.mmu.unit.nfc.rc522_driver import RC522Driver
from extras.mmu.unit.nfc import pn7160_driver
from extras.mmu.unit.nfc.pn7160_driver import PN7160Driver

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


# =============================================================================
# PN7160 - the polled (no irq_pin) probe
# =============================================================================
#
# The harness fake (test/hh/klippy_root/extras/bus.py) can't drive these: its
# _I2CTransferCmd.send() takes no 'retry' kwarg and its reply carries no
# i2c_bus_status, which is the very field the polled probe reads. A local fake also lets
# a test OMIT i2c_transfer_cmd, which is how the "old firmware" gate is exercised.

NACK = object()      # Script marker: the NFCC NACKed this read (nothing to say)
NOSTATUS = object()  # Script marker: SUCCESS-shaped reply carrying no status key

# Frames as they arrive from the chip
RF_DISCOVER_NTF = [0x61, 0x03, 0x06, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00]
RF_INTF_ACTIVATED_NTF = [0x61, 0x05, 0x04, 0x01, 0x01, 0x01, 0x00]
CORE_GENERIC_ERROR_NTF = [0x60, 0x07, 0x01, 0x00]
RF_DISCOVER_SELECT_CMD = (0x21, 0x04)   # Must NEVER be sent by a probe tick


class _FakeReactor:
    NOW, NEVER = 0.0, 9e9

    def __init__(self):
        self.now = 100.0
        self.pauses = []
        self.strict = False     # True: any pause is a test failure

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        if self.strict:
            raise AssertionError('probe path paused the reactor - that is the whole '
                                 'thing a probe tick must not do')
        self.pauses.append(max(0.0, waketime - self.now))
        self.now = max(self.now, waketime)
        return self.now


class _FakePrinter:
    def __init__(self, reactor):
        self._reactor = reactor

    def get_reactor(self):
        return self._reactor


class _FakeConfig:
    """Minimal ConfigWrapper for a [mmu_nfc_reader] section with no pins."""

    class error(Exception):
        pass

    _sentinel = object()

    def __init__(self, printer, **opts):
        self._opts = opts
        self._printer = printer

    def get(self, option, default=_sentinel):
        if option in self._opts:
            return self._opts[option]
        if default is self._sentinel:
            raise self.error("Option '%s' must be specified" % (option,))
        return default

    def getint(self, option, default=_sentinel, minval=None, maxval=None):
        return int(self.get(option, default))

    def getfloat(self, option, default=_sentinel, minval=None, maxval=None):
        return float(self.get(option, default))

    def getboolean(self, option, default=_sentinel):
        return bool(self.get(option, default))

    def get_printer(self):
        return self._printer

    def get_name(self):
        return 'mmu_nfc_reader gate0'


class _FakeTransferCmd:
    def __init__(self, owner):
        self._owner = owner

    def send(self, args, minclock=0, reqclock=0, retry=True):
        # 'retry' MUST be accepted - the driver passes retry=False
        _oid, write, read_len = args
        return self._owner.serve(list(write), read_len)


class FakeI2c:
    """Scripted I2C bus. 'script' is consumed one entry per READ."""

    def __init__(self, script=(), status_support=True, raises=None):
        self.oid = 1                    # the driver passes i2c.oid to the raw command
        self.transcript = []            # [('write', [bytes]) | ('read', len), ...]
        self.script = deque(script)
        self.raises = raises            # Exception to raise instead of answering
        if status_support:
            # Absent on Klipper <= v0.13.0, which is the gate
            self.i2c_transfer_cmd = _FakeTransferCmd(self)

    def get_i2c_address(self):
        return 0x28

    def serve(self, write, read_len):
        if self.raises is not None:
            raise self.raises
        if write:
            self.transcript.append(('write', write))
            return {'i2c_bus_status': 'SUCCESS', 'response': []}
        self.transcript.append(('read', read_len))
        item = self.script.popleft() if self.script else NACK
        if item is NACK:
            return {'i2c_bus_status': 'NACK', 'response': []}
        if item is NOSTATUS:
            return {'response': []}
        return {'i2c_bus_status': 'SUCCESS', 'response': list(item)}

    # Fallback path (no i2c_transfer_cmd), only reached by the gating tests
    def i2c_write(self, data, **kwargs):
        self.transcript.append(('write', list(data)))

    def i2c_read(self, write, read_len, **kwargs):
        return self.serve(list(write), read_len)

    # -- assertion helpers --------------------------------------------------

    def reads(self):
        return [n for op, n in self.transcript if op == 'read']

    def writes(self):
        return [tuple(payload) for op, payload in self.transcript if op == 'write']


def pn7160(script=(), status_support=True, irq=False, raises=None, **opts):
    """A PN7160Driver with a probe already armed, as probe_start() would leave it.

    Built directly rather than through probe_start(), because probe_start()'s full NCI
    setup transcript is a separate concern - the TICK is what the homing probe makes
    load-bearing.
    """
    reactor = _FakeReactor()
    i2c = FakeI2c(script, status_support=status_support, raises=raises)
    drv = PN7160Driver(_FakeConfig(_FakePrinter(reactor), **opts), i2c,
                       name="gate0", debug=0)
    drv._handler.irq_enabled = irq
    drv._handler.irq_state = 1 if irq else None
    drv._handler.initialized = True
    drv._alive = True
    drv._needs_full_setup = False        # _setup_for_read() cleared it
    drv._probe_active = True
    drv._discovery_active = True
    i2c.transcript.clear()
    reactor.strict = True
    return drv, i2c, reactor


class TestPn7160ProbeGating(unittest.TestCase):
    """
    probe_supported() decides whether MmuNfcReader uses the probe or the blocking shim.
    In polled mode it must also refuse where the firmware cannot REPORT an I2C NACK,
    because there a speculative read is an MCU shutdown, not an answer.
    """

    def supported(self, **kwargs):
        drv, i2c, _reactor = pn7160(**kwargs)
        return drv, i2c

    def test_polled_probe_is_available_when_the_mcu_reports_status(self):
        drv, _i2c = self.supported()
        self.assertTrue(drv.probe_supported(),
                        'a polled PN7160 on firmware that reports i2c_bus_status can '
                        'probe by reading on spec - declining it forces the blocking '
                        'shim and loses tag-homing accuracy')
        self.assertIn('polled', drv._probe_mode_text(),
                      'the init() log line is what a user greps klippy.log for to '
                      'confirm the probe is live')

    def test_polled_probe_is_refused_without_status_support(self):
        """THE SAFETY GATE. Without i2c_transfer, Klipper's command_i2c_read calls
        i2c_shutdown_on_err(), so an empty-buffer NACK is an MCU shutdown mid-print."""
        drv, _i2c = self.supported(status_support=False)
        self.assertFalse(drv.probe_supported())
        self.assertIn('NACK', drv._probe_mode_text())

    def test_probe_polled_false_disables_it(self):
        drv, _i2c = self.supported(probe_polled=False)
        self.assertFalse(drv.probe_supported())
        self.assertIn('probe_polled', drv._probe_mode_text(),
                      'the two "unavailable" reasons must be distinguishable: one is '
                      'the user\'s choice, the other means update Klipper')

    def test_irq_mode_never_depends_on_status_support(self):
        """The IRQ path asks a cached pin state and never reads on spec."""
        drv, _i2c = self.supported(irq=True, status_support=False, probe_polled=False)
        self.assertTrue(drv.probe_supported())
        self.assertIn('irq_pin', drv._probe_mode_text())

    def test_probe_supported_touches_no_bus(self):
        """_homing_poll_interval() calls this ~50 times a second."""
        drv, i2c = self.supported()
        drv.probe_supported()
        self.assertEqual(i2c.transcript, [])


class TestPn7160SilentTick(unittest.TestCase):
    """A NACK means "nothing pending", which is the answer on nearly every tick."""

    def test_nack_reports_none_and_leaves_the_reader_alive(self):
        """
        THE most load-bearing assertion here. If an empty tick escalates, the reader is
        marked dead and a full re-init forced - on the very first miss of every home.
        """
        drv, _i2c, _reactor = pn7160([NACK])
        self.assertIsNone(drv.probe_poll())
        self.assertTrue(drv.is_alive())
        self.assertFalse(drv._needs_full_setup)
        self.assertTrue(drv._probe_active)

    def test_silent_tick_costs_one_header_read_and_no_pause(self):
        drv, i2c, _reactor = pn7160([NACK])
        drv.probe_poll()                    # reactor.strict makes any pause fail
        self.assertEqual(i2c.reads(), [3],
                         'a silent tick must read the 3-byte NCI header and stop')

    def test_silence_never_triggers_a_discovery_restart(self):
        """
        Silence is "no tag yet", not a stall, and must never tear discovery down. A
        teardown (RF_DEACTIVATE) plus probe_start() mid-move is blocking NCI work -
        worse than the shim this replaces. 4s here is longer than any watchdog this
        driver used to carry.
        """
        drv, i2c, reactor = pn7160([NACK] * 200)
        for _tick in range(200):
            reactor.now += 0.020
            self.assertIsNone(drv.probe_poll())
        self.assertEqual(i2c.writes(), [],
                         'a write during a run of silence means silence tore discovery '
                         'down - the probe has no time-based watchdog')

    def test_irq_silence_never_triggers_a_discovery_restart(self):
        """
        THE 500mm SWEEP REGRESSION. A 2s watchdog here used to answer False on normal
        silence, so MmuNfcManager restarted discovery (_homing_poll) partway through
        every sweep longer than 2s - a 500mm window at 100mm/s is 5s of legitimate
        silence. Each restart blinded the reader for probe_start()'s full NCI bring-up
        (~124ms, 12mm of travel), and the repeated hardware resets provoked I2C
        START_NACKs that made recovery slower still. Silence must stay None forever.
        """
        drv, i2c, reactor = pn7160([], irq=True)
        drv._handler.irq_state = 0                  # IRQ low: nothing pending
        for _tick in range(500):                    # 10s at a 20ms tick
            reactor.now += 0.020
            self.assertIsNone(drv.probe_poll(),
                              'IRQ-mode silence must answer None - False restarts '
                              'discovery mid-sweep and loses the tag')
        self.assertTrue(drv._probe_active,
                        'a silent sweep must leave the probe armed')
        self.assertEqual(i2c.writes(), [],
                         'an IRQ-low tick must cost no bus traffic at all')

    def test_status_less_reply_is_silence_not_an_error(self):
        """
        Pins the "read the header via _i2c_transfer_safe, not _read_exact" decision: via
        _read_exact this raises PN7160Error("short I2C read"), which would count as a
        torn frame and restart discovery every third tick.
        """
        drv, _i2c, _reactor = pn7160([NOSTATUS])
        self.assertIsNone(drv.probe_poll())
        self.assertEqual(drv._probe_errors, 0)
        self.assertTrue(drv.is_alive())

    def test_all_zero_header_is_not_a_frame(self):
        """Some MCUs hand back a zero-filled buffer for a NACKed read."""
        drv, i2c, _reactor = pn7160([[0x00, 0x00, 0x00]])
        self.assertIsNone(drv.probe_poll())
        self.assertEqual(i2c.reads(), [3],
                         'a garbage header must not be followed by a payload read')


class TestPn7160TransportErrors(unittest.TestCase):
    """
    A Klipper/MCU comms fault is not a dead PN7160. On a CAN toolhead a 20ms polled
    tick competes with step data, so "Unable to obtain 'i2c_response' response" happens
    under load and is transient. Escalating one straight to not-alive forced a full
    re-init (VEN toggle + NCI bring-up, ~124ms blind, 12mm of travel at 100mm/s) whose
    repeated hardware resets then provoked I2C START_NACKs costing 1.7-2.1s each.
    """

    KLIPPER_TIMEOUT = Exception("Unable to obtain 'i2c_response' response")

    def test_one_transport_error_is_tolerated(self):
        """THE regression. One hiccup must not cost the scan."""
        drv, _i2c, _reactor = pn7160(raises=self.KLIPPER_TIMEOUT)
        self.assertIsNone(drv.probe_poll(),
                          'a transient MCU timeout must answer None, not False - '
                          'False makes MmuNfcManager rebuild discovery mid-sweep')
        self.assertTrue(drv.is_alive(), 'the reader is fine; the host bus was busy')
        self.assertFalse(drv._needs_full_setup,
                         'forcing full setup is what made the next restart cost 124ms')
        self.assertTrue(drv._probe_active, 'the probe must stay armed')

    def test_a_sustained_run_still_escalates(self):
        """Tolerance is not blindness - a reader that has really gone must be caught."""
        drv, _i2c, _reactor = pn7160(raises=self.KLIPPER_TIMEOUT)
        results = [drv.probe_poll() for _ in range(drv._PROBE_MAX_TRANSPORT_ERRORS)]
        self.assertEqual(results[:-1], [None] * (drv._PROBE_MAX_TRANSPORT_ERRORS - 1))
        self.assertIs(results[-1], False, 'the cap must still escalate')
        self.assertFalse(drv.is_alive())
        self.assertTrue(drv._needs_full_setup)

    def test_a_good_frame_clears_the_transport_count(self):
        """
        Consecutive is the whole point: scattered hiccups across a long sweep must
        never accumulate into a teardown.
        """
        drv, _i2c, _reactor = pn7160([RF_DISCOVER_NTF[:3], RF_DISCOVER_NTF[3:]])
        drv._probe_transport_errors = drv._PROBE_MAX_TRANSPORT_ERRORS - 1
        self.assertTrue(drv.probe_poll(), 'a real detection must still report True')
        self.assertEqual(drv._probe_transport_errors, 0,
                         'a good frame proves the transport recovered')


class TestPn7160TotalDuration(unittest.TestCase):
    """
    TOTAL_DURATION is the NCI discovery period: the NFCC interrogates the field once
    per period then idles. Never sent before, so the firmware default (hundreds of ms)
    stood, making detection of a moving tag probabilistic - P(catch) ~ dwell / period.
    """

    def test_the_set_frame_encodes_the_period_little_endian(self):
        """
        Hand-encoded NCI. A byte wrong here is a silently wrong period, which shows up
        only as flaky detection on hardware - exactly the bug being fixed.
        """
        cmd = pn7160_driver.NCI_CORE_SET_TOTAL_DURATION_CMD
        self.assertEqual(cmd[:3], [0x20, 0x02, 0x05],
                         'CORE_SET_CONFIG (GID CORE, OID 0x02) with 5 payload octets')
        self.assertEqual(cmd[3:6], [0x01, 0x00, 0x02],
                         'one parameter: TOTAL_DURATION (0x00), 2 octets long')
        self.assertEqual(len(cmd), 3 + cmd[2],
                         'the length octet must match the real payload length')
        ms = cmd[6] | (cmd[7] << 8)
        self.assertEqual(ms, pn7160_driver.PN7160_TOTAL_DURATION_MS,
                         'NCI multi-octet values are little-endian - a byte-swapped '
                         '50ms becomes 12800ms, which is worse than the default')

    def test_the_period_is_short_enough_for_a_moving_tag(self):
        """
        Pins the REASON for the value. At 100mm/s the readable zone is ~15-20mm, so a
        period much above ~100ms makes detection a coin flip rather than a certainty.
        """
        self.assertLessEqual(pn7160_driver.PN7160_TOTAL_DURATION_MS, 100)
        self.assertGreater(pn7160_driver.PN7160_TOTAL_DURATION_MS, 0)

    def test_the_get_frame_asks_for_total_duration(self):
        cmd = pn7160_driver.NCI_CORE_GET_TOTAL_DURATION_CMD
        self.assertEqual(cmd, [0x20, 0x03, 0x02, 0x01, 0x00],
                         'CORE_GET_CONFIG for one parameter, TOTAL_DURATION')


class TestPn7160Detection(unittest.TestCase):
    """Presence only: the probe must never select or activate the tag."""

    def check_detects(self, frame, irq):
        drv, i2c, reactor = pn7160([frame[:3], frame[3:]], irq=irq)
        # In IRQ mode the detection tick ends in _wait_for_irq_release(), a bounded 50ms
        # wait that is deliberate: the move is stopping anyway. Polled mode has no such
        # step, and must not pause at all - asserted below.
        reactor.strict = False
        self.assertTrue(drv.probe_poll(), 'a presence NTF must report True')
        self.assertFalse(drv._probe_active, 'a detection ends the scan')
        if not irq:
            self.assertEqual(reactor.pauses, [],
                             'a polled detection tick must not pause the reactor')
        self.assertNotIn(RF_DISCOVER_SELECT_CMD,
                         [w[:2] for w in i2c.writes()],
                         'the probe must not select the tag - that NCI command blocks '
                         'for up to a second, mid-move. read_target() does it later, '
                         'once the machine is stationary')

    def test_activation_ntf_is_a_detection(self):
        for irq in (False, True):
            self.check_detects(RF_INTF_ACTIVATED_NTF, irq)

    def test_discover_ntf_is_a_detection(self):
        for irq in (False, True):
            self.check_detects(RF_DISCOVER_NTF, irq)

    def test_unrelated_notification_keeps_listening(self):
        drv, _i2c, _reactor = pn7160([CORE_GENERIC_ERROR_NTF[:3],
                                      CORE_GENERIC_ERROR_NTF[3:]])
        self.assertIsNone(drv.probe_poll())
        self.assertTrue(drv._probe_active)
        self.assertTrue(drv.is_alive())

    def test_poll_without_an_armed_probe_is_false(self):
        drv, _i2c, _reactor = pn7160([RF_DISCOVER_NTF[:3], RF_DISCOVER_NTF[3:]])
        drv._probe_active = False
        self.assertFalse(drv.probe_poll())


class TestPn7160ErrorTaxonomy(unittest.TestCase):
    """Torn frame vs dead reader: the two must not be confused in either direction."""

    def test_torn_frame_is_tolerated_then_resyncs(self):
        """
        Header read OK, payload NACKed: the header is already consumed so the stream is
        out of step. Transport noise, not a dead chip - tolerate a couple, then hand
        back False so the caller restarts on a chip connect_nci() has reset.
        """
        header = RF_DISCOVER_NTF[:3]
        drv, _i2c, reactor = pn7160([header, NACK, header, NACK, header, NACK])
        reactor.strict = False              # the teardown path legitimately pauses
        self.assertIsNone(drv.probe_poll())
        self.assertIsNone(drv.probe_poll())
        self.assertFalse(drv.probe_poll(),
                         'after _PROBE_MAX_FRAME_ERRORS the caller must be told to '
                         'restart the scan')
        self.assertTrue(drv.is_alive(),
                        'a torn frame is noise on the wire, not a dead reader')

    def test_a_nack_while_irq_is_asserted_is_not_silence(self):
        """
        Polled mode's "nothing pending" answer must NOT be borrowed by the IRQ path.
        There the IRQ line said a frame was waiting, and irq_state stays high - so the
        silence watchdog never runs again and a reader NACKing every read would answer
        None for the entire homing move, with nothing in the log.
        """
        drv, _i2c, reactor = pn7160([NACK] * 5, irq=True)
        reactor.strict = False              # the teardown path legitimately pauses
        self.assertIsNone(drv.probe_poll())
        self.assertIsNone(drv.probe_poll())
        self.assertFalse(drv.probe_poll(),
                         'a reader that never delivers the frame its IRQ promised must '
                         'eventually be reported, not silently polled forever')
        self.assertTrue(drv.is_alive())

    def test_a_good_frame_resets_the_error_count(self):
        drv, _i2c, _reactor = pn7160([RF_DISCOVER_NTF[:3], NACK,
                                      CORE_GENERIC_ERROR_NTF[:3],
                                      CORE_GENERIC_ERROR_NTF[3:]])
        self.assertIsNone(drv.probe_poll())         # torn
        self.assertEqual(drv._probe_errors, 1)
        self.assertIsNone(drv.probe_poll())         # clean, unrelated NTF
        self.assertEqual(drv._probe_errors, 0)

    def test_a_real_transport_failure_still_escalates(self):
        """
        A SUSTAINED transport failure must still be reported. Escalation is now on the
        consecutive count rather than the first fault, because a single one is usually
        just a busy host bus (see TestPn7160TransportErrors) - but a reader that has
        genuinely gone must not be polled forever.
        """
        drv, _i2c, _reactor = pn7160(raises=RuntimeError('mcu is gone'))
        for _ in range(drv._PROBE_MAX_TRANSPORT_ERRORS - 1):
            self.assertIsNone(drv.probe_poll(), 'below the cap: tolerated')
        self.assertFalse(drv.probe_poll())
        self.assertFalse(drv.is_alive())
        self.assertTrue(drv._needs_full_setup,
                        'a genuine failure must force a full re-init, or the reader '
                        'stays broken until klippy restarts')


class TestPn7160FastPolledFrames(unittest.TestCase):
    """
    wait_frame()'s polled retry spacing. The 100ms no_irq_read_delay settle is what made
    a polled NCI command cost >=120ms, and probe_start() - which runs inside
    home_start(), before the drip move - pays for three of them.
    """

    def build(self, script, status_support=True, **opts):
        reactor = _FakeReactor()
        i2c = FakeI2c(script, status_support=status_support)
        drv = PN7160Driver(_FakeConfig(_FakePrinter(reactor), **opts), i2c,
                           name="gate0", debug=0)
        drv._handler.irq_enabled = False
        return drv._handler, reactor

    def test_polled_retry_uses_the_tight_interval(self):
        handler, reactor = self.build([NACK, RF_DISCOVER_NTF[:3], RF_DISCOVER_NTF[3:]])
        self.assertTrue(handler.no_irq_fast_poll)
        frame = handler.wait_frame(timeout=0.5, poll_interval=0.020)
        self.assertEqual(frame, RF_DISCOVER_NTF)
        self.assertLess(sum(reactor.pauses), 0.050,
                        'a too-early read costs only a reported NACK now, so retries '
                        'must not settle for no_irq_read_delay each time: pauses were '
                        '%s' % (reactor.pauses,))

    def test_status_less_firmware_keeps_the_conservative_settle(self):
        """No way to tell a NACK from data there, so minimise speculative reads."""
        handler, reactor = self.build([RF_DISCOVER_NTF[:3], RF_DISCOVER_NTF[3:]],
                                      status_support=False)
        self.assertFalse(handler.no_irq_fast_poll)
        handler.wait_frame(timeout=0.5, poll_interval=0.020)
        self.assertAlmostEqual(max(reactor.pauses), handler.no_irq_read_delay, places=6,
                               msg='without status support the full no_irq_read_delay '
                                   'settle must stay - reading early there risks an '
                                   'MCU shutdown')


class TestPn7160ProbeStartCost(unittest.TestCase):
    """
    probe_start() runs inside MmuNfcEndstop.home_start() - after Klipper has computed the
    homing print_time and before the drip move launches - so its NCI setup sits on the
    homing critical path. With one 100ms settle per command attempt it cost ~0.5s; the
    point of no_irq_fast_poll is to bring that down to something a move can absorb.
    """

    # RSPs the setup sequence waits for, in order: CORE_RESET, CORE_INIT,
    # CORE_SET_CONFIG (TOTAL_DURATION), RF_DISCOVER_MAP, RF_DISCOVER
    SETUP_RSPS = ([0x40, 0x00, 0x03, 0x00, 0x11, 0x01],
                  [0x40, 0x01, 0x01, 0x00],
                  [0x40, 0x02, 0x02, 0x00, 0x00],
                  [0x41, 0x00, 0x01, 0x00],
                  [0x41, 0x03, 0x01, 0x00])

    def script(self):
        out = []
        for frame in self.SETUP_RSPS:
            out.extend([frame[:3], frame[3:]])
        # The optional post-reset frame read: nothing there, which is a NACK
        out.insert(2, NACK)
        return out

    def test_polled_setup_is_affordable(self):
        reactor = _FakeReactor()
        i2c = FakeI2c(self.script())
        drv = PN7160Driver(_FakeConfig(_FakePrinter(reactor)), i2c, name="gate0", debug=0)
        drv._handler.irq_enabled = False
        self.assertTrue(drv.probe_start())
        self.assertTrue(drv._probe_active)
        self.assertIn(tuple(pn7160_driver.NCI_RF_DISCOVER_NFCA_NFCV_CMD), i2c.writes(),
                      'probe_start must actually start RF discovery')
        self.assertLess(sum(reactor.pauses), 0.200,
                        'polled probe setup blocked for %.3fs inside home_start(); it '
                        'was ~0.5s before no_irq_fast_poll and must not regress there'
                        % sum(reactor.pauses))


class TestPn7160HandlerFollowsReaderDebug(unittest.TestCase):
    """The handler's NCI frame detail is level 4 on this reader, nothing else."""

    def handler(self, **opts):
        reactor = _FakeReactor()
        drv = PN7160Driver(_FakeConfig(_FakePrinter(reactor), **opts), FakeI2c(),
                           name="gate0", debug=opts.get('debug', 2))
        return drv._handler

    def test_debug_level_reaches_the_handler(self):
        self.assertEqual(self.handler(debug=4).debug, 4)
        self.assertEqual(self.handler(debug=2).debug, 2)

    def test_handler_detail_needs_level_four(self):
        with self.assertLogs('mmu_rfid.reader', level='INFO') as captured:
            self.handler(debug=4)._debug("hello")
        self.assertTrue(any('hello' in line for line in captured.output))

        handler = self.handler(debug=3)
        records = []
        logger_obj = logging.getLogger('mmu_rfid.reader')
        collector = logging.Handler(level=logging.INFO)
        collector.emit = lambda record: records.append(record.getMessage())
        logger_obj.addHandler(collector)
        previous, propagate = logger_obj.level, logger_obj.propagate
        logger_obj.setLevel(logging.INFO)
        logger_obj.propagate = False
        try:
            handler._debug("hello")
        finally:
            logger_obj.setLevel(previous)
            logger_obj.propagate = propagate
            logger_obj.removeHandler(collector)
        self.assertEqual(records, [])


class TestPn7160ProbeContractShape(unittest.TestCase):
    def test_driver_exposes_the_full_contract(self):
        drv, _i2c, _reactor = pn7160()
        for name in ('probe_supported', 'probe_start', 'probe_poll', 'probe_stop'):
            self.assertTrue(callable(getattr(drv, name, None)),
                            'PN7160Driver.%s missing - the reader would silently fall '
                            'back to the blocking shim' % name)


if __name__ == '__main__':
    unittest.main()
