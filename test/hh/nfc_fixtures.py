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


class VirtualNfcChip:
    """
    A reader chip standing in for RC522Driver / PN532Driver / PN7160Driver, driven by
    the filament path model.

    Faked at the DRIVER boundary rather than the bus, because a real UID read is a full
    ISO14443 exchange (REQA, anticollision, select, ...) and scripting that faithfully is
    chip simulation, not filament simulation. The bus-level fixtures above still exercise
    the real RC522 init path; this replaces the chip for tests about what happens once a
    tag is found.

    Interface MmuNfcReader actually depends on (verified against
    extras/mmu/unit/nfc/mmu_nfc_reader.py): init, is_alive, read_tag, read_target,
    _release_current_target, plus the _gate label that init(gate) writes. Optionally
    also the non-blocking presence-probe contract - see probe_support below.

    Targets report protocol='uid_only', so _classify_target returns 'uid_only' and
    read_tag_data yields (uid, None) - a UID read with no metadata. That is deliberate:
    metadata already has full end-to-end coverage through _MMU_TEST NFC_READ=1, which
    injects at _dispatch_lookup, and returning real parseable NDEF bytes here would mean
    building tag images - properly the job of direct tag_parser tests.

    PROBE SUPPORT. Real drivers may implement probe_start/probe_poll/probe_stop so a
    homing move can tick a presence check without blocking the reactor; those that
    don't get MmuNfcReader's blocking shim instead. Both paths must keep working, so
    probe_support defaults to False (shim - what most tests exercise) and is opted
    into per chip. probe_supported() is honoured by MmuNfcReader.has_probe_support(),
    which is also how the real PN7160 declines when its IRQ line isn't wired.

    probe_latency_ticks models a scan that spans several reactor ticks: probe_poll()
    returns None that many times before answering. Straddling ticks is the whole point
    of the contract, so it needs to be reachable in a test.
    """

    def __init__(self, model=None, gate=None, label=None, probe_support=False):
        self._gate = gate if gate is not None else label
        self.model = model
        self.presented = None       # explicit override, for shared/common readers
        self.reads = 0              # assertion surface
        self.releases = 0
        self.inits = 0
        self._held = None
        # Presence-probe surface
        self.probe_support = probe_support
        self.probe_starts = 0
        self.probe_polls = 0
        self.probe_stops = 0
        self.probe_latency_ticks = 0
        self._probe_in_flight = False
        self._probe_ticks = 0
        # Ordered operation log: 'read', 'probe_start', 'probe_poll', 'probe_stop',
        # 'release'. Counters alone can't express "no read happened *while* a probe was
        # running", which is the invariant that matters - see reads_during_probe().
        self.events = []

    # -- driver interface ---------------------------------------------------
    def init(self):
        self.inits += 1
        self._held = None
        return True

    def is_alive(self):
        return True

    def read_tag(self, timeout=0.5):
        """UID-only read. Auto-releases, like the real PN532/PN7160 path."""
        self.reads += 1
        self.events.append('read')
        tag = self._visible_tag()
        self._held = None
        return tag.uid if tag is not None else None

    def read_target(self, timeout=0.5):
        self.reads += 1
        self.events.append('read')
        tag = self._visible_tag()
        if tag is None:
            self._held = None
            return None
        self._held = tag
        return {'uid': tag.uid, 'protocol': 'uid_only',
                'protocol_name': 'uid_only', 'sak': 0x00,
                'uid_length': len(tag.uid) // 2}

    def _release_current_target(self, reason=None):
        self.releases += 1
        self.events.append('release')
        self._held = None
        return True

    # -- non-blocking presence probe ----------------------------------------
    #
    # Deliberately does NOT bump self.reads: a probe reports presence, it does not
    # read the tag. Tests assert exactly that - during a homing move the chip must
    # see probes and no reads.

    def probe_supported(self):
        return self.probe_support

    def probe_start(self):
        self.probe_starts += 1
        self.events.append('probe_start')
        self._probe_in_flight = True
        self._probe_ticks = 0
        return True

    def probe_poll(self):
        """True = tag present, False = scan finished empty, None = still scanning."""
        self.probe_polls += 1
        self.events.append('probe_poll')
        if not self._probe_in_flight:
            return False
        self._probe_ticks += 1
        if self._probe_ticks <= self.probe_latency_ticks:
            return None
        self._probe_in_flight = False
        tag = self._visible_tag()
        if tag is None:
            return False
        self._held = tag        # A real chip leaves a detected target selected
        return True

    def probe_stop(self):
        self.probe_stops += 1
        self.events.append('probe_stop')
        self._probe_in_flight = False
        self._held = None
        return True

    # -- test-facing --------------------------------------------------------

    def reads_during_probe(self):
        """
        How many tag reads happened while a presence probe was running, i.e. between a
        'probe_start' and its matching 'probe_stop'.

        This is THE homing invariant: a homing move ticks a presence probe and must
        never read the tag, because a read (especially a deep one) is slow enough to
        wreck homing accuracy and risk a Klipper "Timer too close". Reads before and
        after a probe window are fine and expected - _jog_scan pre-reads before it
        jogs, and read_gate_after_home() reads once the move has stopped - so a plain
        read count can't distinguish correct from broken. This can.
        """
        count = 0
        in_probe = False
        for event in self.events:
            if event == 'probe_start':
                in_probe = True
            elif event == 'probe_stop':
                in_probe = False
            elif event == 'read' and in_probe:
                count += 1
        return count
    def present(self, uid, metadata=None):
        """Hold a tag on this reader regardless of filament position."""
        from .filament import Tag
        self.presented = Tag(uid, metadata)
        return self

    def clear(self):
        self.presented = None
        return self

    def _visible_tag(self):
        if self.presented is not None:
            return self.presented
        # A per-gate reader sees its own gate's filament; a common reader has an
        # integer-less label, so it only ever reports an explicitly presented tag.
        if self.model is not None and isinstance(self._gate, int):
            return self.model.tag_detected(self._gate)
        return None

    def __repr__(self):
        return 'VirtualNfcChip(gate=%r, tag=%r)' % (
            self._gate, self._visible_tag())


def virtualise(printer, model=None, probe_support=False):
    """
    Swap every reader's chip driver for a VirtualNfcChip. Must run BEFORE the readers
    are initialised, which is when MmuNfcReader.init() first talks to the chip - these
    days that is MmuNfcManager's delayed post-bootup init, not klippy:connect (see
    Session._settle_nfc_init), so anywhere before boot() is early enough.

    probe_support selects which homing path the chips exercise: False (default) is
    MmuNfcReader's blocking shim, True is the non-blocking probe contract. Tests that
    care set it explicitly; a chip's flag can also be flipped afterwards, since
    has_probe_support() consults probe_supported() on each call.

    Returns {reader_name: VirtualNfcChip}.
    """
    machine = printer.lookup_object('mmu_machine', None)
    chips = {}
    if machine is None:
        return chips
    for unit in machine.units:
        manager = getattr(unit, 'nfc_manager', None)
        if manager is None:
            continue
        readers = [(gate, r) for gate, r in enumerate(
            getattr(manager, 'gate_readers', ()) or ()) if r is not None]
        shared = getattr(manager, 'shared_reader', None)
        if shared is not None:
            readers.append((None, shared))
        for gate, reader in readers:
            chip = VirtualNfcChip(model=model, gate=gate, label=reader.name,
                                  probe_support=probe_support)
            reader.reader = chip
            chips[reader.name] = chip
    return chips


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
