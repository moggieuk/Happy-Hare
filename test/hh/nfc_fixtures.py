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

    STILL FINITE, which now matters on a SHARED reader. Its poll genuinely runs - it
    only started arming once reader init stopped being abandoned mid-pause (see
    Session._settle_nfc_init) - and each poll cycle spends ~9 entries on a tag scan.
    So roughly 7 seconds of virtual time drains the default script, after which reads
    log ScriptExhausted warnings and the reader reports not-alive. A test that
    advances a shared-reader profile that far needs a larger cycles=.
    """
    return list(RC522_INIT) + [list(_TX_CONTROL_ON) for _ in range(2 * cycles)]


def prime_reader(reader, cycles=DEFAULT_CYCLES):
    """
    Give one MmuNfcReader's transport enough scripted responses to initialise.
    Returns True if a bus was found and primed.

    RC522 (SPI) and PN532 over HSU/UART are scripted. PN532/PN7160 over I2C are
    I2C-framed and their init sequences have not been captured yet - deliberately
    left unprimed so a test using them fails loudly with the transcript rather than
    appearing to work.
    """
    driver = getattr(reader, 'reader', None)
    # UART first: a UART driver has neither _spi nor _i2c, so the ordering only
    # matters for clarity, not correctness.
    if getattr(driver, '_port', None) is not None:
        return prime_uart_reader(reader, pn532_uart_script(cycles))
    spi = getattr(driver, '_spi', None)
    if spi is None or not hasattr(spi, 'script'):
        return False
    spi.script.extend(rc522_script(cycles))
    return True


# ── PN532 HSU (UART) byte-stream fixtures ─────────────────────────────────────
#
# The RC522 script above is one canned answer per spi_transfer - query then read.
# A UART script is a list of BYTE CHUNKS instead, because HSU is a push stream:
# the fake port makes one chunk readable per read(), so CHUNK BOUNDARIES are how a
# test models a frame that straddles reactor ticks. That is the case
# _HSUFrameReader exists for, so it has to be reachable here.
#
# Frames are BUILT rather than typed out, so LCS/DCS can never drift from what
# pn532_driver._build_frame produces.

PN532_UART_ACK  = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])
PN532_UART_NACK = bytes([0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00])
# Application error frame: TFI 0x7F. Parses as a well-formed info frame and is then
# rejected on the TFI check - diagnostically very different from silence.
PN532_UART_ERROR = bytes([0x00, 0x00, 0xFF, 0x01, 0xFF, 0x7F, 0x81, 0x00])
# Leading zeros a real chip emits ahead of a frame, and junk for the resync path.
PN532_HSU_PADDING = bytes([0x00] * 3)
PN532_UART_GARBAGE = bytes([0xA5, 0x5A, 0x13])


def pn532_frame(cmd_resp, payload=()):
    """A PN532 -> host information frame (TFI 0xD5) with checksums computed."""
    data = bytes([0xD5, cmd_resp]) + bytes(payload)
    return (bytes([0x00, 0x00, 0xFF, len(data), (-len(data)) & 0xFF])
            + data + bytes([(-sum(data)) & 0xFF, 0x00]))


PN532_FIRMWARE_RESP = pn532_frame(0x03, [0x32, 0x01, 0x06, 0x07])   # PN532 v1.6
PN532_SAM_RESP = pn532_frame(0x15)
PN532_RELEASE_RESP = pn532_frame(0x53, [0x00])
PN532_NO_TARGET = pn532_frame(0x4B, [0x00])                         # NbTg = 0


def pn532_inlist_resp(uid_bytes, sak=0x00, atqa=(0x00, 0x44), tg=0x01):
    """
    InListPassiveTarget response for one ISO14443A target.

    Payload shape is what _parse_inlist_payload expects: NbTg, Tg, SENS_RES(2),
    SAK, UIDLen, UID...
    """
    return pn532_frame(0x4B, [0x01, tg, atqa[0], atqa[1], sak,
                              len(uid_bytes)] + list(uid_bytes))


def pn532_uart_init_cycle():
    """One successful init(): GetFirmwareVersion, then SAMConfiguration."""
    return [PN532_UART_ACK, PN532_FIRMWARE_RESP, PN532_UART_ACK, PN532_SAM_RESP]


def pn532_uart_script(cycles=DEFAULT_CYCLES):
    """
    Enough init cycles for repeated init()/is_alive() calls.

    Same reasoning as rc522_script's long tail: init() is reached from several
    places (bootup, MMU_RFID_INIT, the ENABLE re-init) and is_alive() adds another
    GetFirmwareVersion exchange each time. An over-long script is harmless because
    unconsumed chunks are simply never read.
    """
    script = []
    for _ in range(max(1, cycles)):
        script.extend(pn532_uart_init_cycle())
        script.extend([PN532_UART_ACK, PN532_FIRMWARE_RESP])   # is_alive()
    return script


def pn532_uart_probe_script(uid_bytes=None, split_ack=False, pad=True,
                            garbage=False):
    """
    One probe cycle's worth of chunks: the ACK, then the scan result.

    split_ack feeds the ACK in two pieces so the framer MUST hold a partial frame
    across a tick - that is the invariant behind _probe_status_ready() meaning
    "a complete frame is buffered" rather than "bytes are available". pad prefixes
    the leading zeros a real chip emits; garbage prefixes junk so resync runs.
    """
    ack = PN532_UART_ACK
    if pad:
        ack = PN532_HSU_PADDING + ack
    if garbage:
        ack = PN532_UART_GARBAGE + ack
    chunks = [ack[:4], ack[4:]] if split_ack else [ack]
    chunks.append(pn532_inlist_resp(uid_bytes) if uid_bytes else PN532_NO_TARGET)
    return chunks


def prime_uart_reader(reader, chunks):
    """
    UART sibling of prime_reader(). Returns True if a UART driver was primed.

    The port opens LAZILY, inside init(), so at priming time it usually does not
    exist yet. In that case seed the fake serial module's registry by port name and
    Serial.__init__ picks the script up when the driver finally opens it.
    """
    driver = getattr(reader, 'reader', None)
    port_name = getattr(driver, '_port', None)
    if port_name is None:
        return False
    port = getattr(driver, '_serial', None)
    if port is not None and hasattr(port, 'feed'):
        port.feed(*chunks)
        return True
    import serial
    if not hasattr(serial, 'preset'):
        return False    # Real pyserial, not the harness fake - nothing to script
    serial.preset(port_name, chunks)
    return True


class PN532UartChip:
    """
    Reactive HSU chip: answers each command frame written to a fake serial port
    with an ACK plus the right response, driven by a presented tag.

    Wire it up with `fake_port.on_write = chip.on_write`.

    Preferred over a flat chunk script for anything about command ORDER, because a
    flat script silently desynchronises the moment the driver issues one more or
    one fewer command than expected. Flat scripts stay the right tool for framer
    edge cases (partial frames, garbage, NACK, extended frames) where the exact
    byte boundaries ARE the test.

    response_delay_reads defers the InListPassiveTarget answer by N read() calls -
    SCRIPT-driven, not clock-driven - so a probe straddling reactor ticks is
    reachable. Same role probe_latency_ticks plays for VirtualNfcChip.
    """

    def __init__(self, uid=None):
        self.uid = uid                  # hex string, or None for an empty field
        self.response_delay_reads = 0
        self.commands = []              # command bytes seen, in order
        self.aborts = 0                 # bare ACK frames written by the driver

    def present(self, uid):
        self.uid = uid
        return self

    def clear(self):
        self.uid = None
        return self

    def _uid_bytes(self):
        if not self.uid:
            return None
        return [int(self.uid[i:i + 2], 16) for i in range(0, len(self.uid), 2)]

    def on_write(self, port, data):
        data = bytes(data)
        if data == PN532_UART_ACK:
            # The driver aborting a command in flight, not sending one.
            self.aborts += 1
            return
        if len(data) < 8 or data[:3] != b'\x00\x00\xFF':
            return                      # Wake preamble or raw debug bytes
        cmd = data[6]
        self.commands.append(cmd)
        reply = self._reply_for(cmd)
        if reply is None:
            return
        port.feed(PN532_UART_ACK)
        for _ in range(self.response_delay_reads):
            port.feed(b'')              # A read that yields nothing yet
        port.feed(reply)

    def _reply_for(self, cmd):
        if cmd == 0x02:                 # GetFirmwareVersion
            return PN532_FIRMWARE_RESP
        if cmd == 0x14:                 # SAMConfiguration
            return PN532_SAM_RESP
        if cmd == 0x32:                 # RFConfiguration
            return pn532_frame(0x33)
        if cmd == 0x52:                 # InRelease
            return PN532_RELEASE_RESP
        if cmd == 0x4A:                 # InListPassiveTarget
            uid = self._uid_bytes()
            return pn532_inlist_resp(uid) if uid else PN532_NO_TARGET
        return None


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
    _release_current_target. Optionally also the non-blocking presence-probe contract
    - see probe_support below. Note the chip's own _gate is set by virtualise() and is
    functional (it selects the tag to show); MmuNfcReader no longer writes to it.
    _gates (plural) is the full list a chip is bound to - more than one entry when
    the chip is shared between neighboring gates (see virtualise()); _visible_tag()
    checks each in order and returns the first tag found, an arbitrary tie-break for
    a chip serving two gates at once.

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

    def __init__(self, model=None, gate=None, gates=None, label=None, probe_support=False):
        self._gate = gate if gate is not None else label
        self._gates = ([g for g in gates if isinstance(g, int)] if gates
                       else ([self._gate] if isinstance(self._gate, int) else []))
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
        # A per-gate reader sees its own gate's filament; a reader shared between
        # neighboring gates checks each of _gates in order (first tag found wins -
        # tests should present one tag at a time). A common reader has no int gate
        # at all, so it only ever reports an explicitly presented tag.
        if self.model is not None:
            for gate in self._gates:
                tag = self.model.tag_detected(gate)
                if tag is not None:
                    return tag
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

    A reader shared between neighboring gates (mmu_unit.py's 'nfc_readers' repeating a
    name) appears more than once in gate_readers by object identity; it gets exactly
    ONE chip serving every gate it's bound to (VirtualNfcChip._gates), not one chip
    per slot - which would silently drop all but the last-processed gate.

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
        # enumerate() gives the gate's index WITHIN the unit; _visible_tag() indexes a
        # printer-global FilamentPath, so offset it. MmuNfcReader.init() used to patch
        # the chip's label with the global gate and paper over the difference; it no
        # longer does, and every NFC profile today is single-unit (first_gate == 0), so
        # this was latent rather than live.
        #
        # Dedupe by reader identity first, so a shared-pair reader collects BOTH gates
        # onto one entry instead of producing two entries that would spawn two chips
        # (the second silently winning as reader.reader, per gate-slot info lost).
        by_id = {}
        entries = []
        for lgate, r in enumerate(getattr(manager, 'gate_readers', ()) or ()):
            if r is None:
                continue
            gate = unit.first_gate + lgate
            idx = by_id.get(id(r))
            if idx is None:
                by_id[id(r)] = len(entries)
                entries.append([r, [gate]])
            else:
                entries[idx][1].append(gate)
        shared = getattr(manager, 'shared_reader', None)
        if shared is not None:
            entries.append([shared, [None]])
        for reader, gates in entries:
            chip = VirtualNfcChip(model=model, gate=gates[0], gates=gates, label=reader.name,
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
