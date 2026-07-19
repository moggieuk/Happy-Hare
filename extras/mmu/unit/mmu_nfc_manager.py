# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to coordinate the NFC/RFID readers associated with an MMU unit
#
# Modelled on MmuEnvironmentManager. Two reader topologies are supported (and may
# co-exist on a single unit):
#   1. A single 'shared' reader (mmu_unit.nfc_reader) that the user presents tags
#      to manually. This manager polls it periodically and, on a tag read, asks
#      Spoolman (via the mmu_controller -> Moonraker) to resolve the tag UID to a
#      spool. The result is applied asynchronously as the "pending" spool id.
#   2. A set of per-gate readers (mmu_unit.nfc_readers, indexed by local gate).
#      These are not polled here - they are intended to be read on demand (e.g.
#      during a gate load) and resolved to a specific gate. This manager just
#      owns/inits them and offers a helper to resolve a per-gate read.
#
# On a read the manager fires a fire-and-forget lookup and then holds off further
# reads for a short cooldown (and dedupes by UID) so a tag left on the reader
# doesn't hammer Spoolman. If a lookup fails for a recoverable reason (e.g. a
# Spoolman communication error) the controller can call allow_reread() to drop the
# hold immediately so the same tag can be re-read without waiting.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging

# Happy Hare imports
from ..mmu_constants     import *
from ..mmu_utils         import MmuError
from .nfc.mmu_nfc_reader import MmuNfcReader

NFC_CHECK_INTERVAL = 2.0   # How often to poll the shared NFC reader (seconds)
NFC_READ_TIMEOUT   = 0.1   # Per-poll reader read timeout (seconds) - keep small; runs on reactor thread
NFC_TAG_HOLD_TIME  = 5.0   # Cooldown after acting on a tag before reading again (seconds)


class MmuNfcManager:

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.mmu = None                         # Set at klippy:connect

        # Reader objects. NFC readers are NOT regular auto-loaded Klipper objects:
        # each must be constructed with the owning mmu_unit (which the normal
        # load_object path can't supply). We look one up first (it may be shared
        # between gates) and otherwise build it from its config section. Done here
        # at config time so a missing/invalid section surfaces early.
        self.shared_reader = None               # Shared reader object, or None
        self.gate_readers = []                  # Per-local-gate reader objects (or None per slot)
        self._setup_readers()

        # Per-reader control flags:
        #   enabled - top-level on/off. If disabled a reader is never read (poll,
        #             auto read_gate, or manual READ). Re-enabling re-initializes it.
        #   active  - runtime guard against *accidental* reads. Blocks only the
        #             automatic paths (poll, auto-dispatch); a manual READ overrides
        #             it. Meant to be toggled frequently (e.g. off while a NEXT_SPOOLID
        #             is pending, or off on all gates during a print).
        self.shared_enabled = True
        self.shared_active = True
        self.gate_enabled = [True] * len(self.gate_readers)
        self.gate_active = [True] * len(self.gate_readers)

        # Shared-reader polling / debounce state
        self._poll_timer = self.reactor.register_timer(self._poll_shared_reader)
        self._polling = False
        self.reinit()

        # Listen for important mmu events
        self.printer.register_event_handler("mmu:enabled",  self._handle_mmu_enabled)
        self.printer.register_event_handler("mmu:disabled", self._handle_mmu_disabled)
        self.printer.register_event_handler("mmu:bootup",   self._handle_mmu_bootup)
        self.printer.register_event_handler("mmu:printing", self._handle_printing)
        self.printer.register_event_handler("mmu:not_printing", self._handle_not_printing)
        self.printer.register_event_handler("mmu:spoolid_pending", self._handle_spoolid_pending)
        self.printer.register_event_handler("mmu:spoolid_not_pending", self._handle_spoolid_not_pending)
        self.printer.register_event_handler("klippy:connect", self._handle_connect)


    def reinit(self):
        # State reset on (re)initialization. Called by mmu_unit.reinit().
        self._last_uid = None   # UID currently "held" (deduped)
        self._hold_until = 0.0  # Monotonic time until which reads are ignored (cooldown)


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def _setup_readers(self):
        """
        Build (or look up) the reader objects this unit controls. Readers are
        constructed with the owning mmu_unit and registered as printer objects.
        """
        self.shared_reader = self._lookup_or_create_reader(self.mmu_unit.nfc_reader)
        self.gate_readers = [self._lookup_or_create_reader(name) for name in self.mmu_unit.nfc_readers]


    def _lookup_or_create_reader(self, reader_name):
        if not reader_name:
            return None
        section = 'mmu_nfc_reader %s' % reader_name
        obj = self.printer.lookup_object(section, None)
        if obj is not None:
            return obj # Already created (e.g. shared between gates)
        if not self.config.has_section(section):
            raise self.config.error("MMU NFC reader section [%s] not found!" % section)
        c = self.config.getsection(section)
        obj = MmuNfcReader(c, self.mmu_unit)
        self.printer.add_object(section, obj)
        logging.info("MMU: Created: [%s]" % section)
        return obj


    #
    # Public access -------------------------------------------------------------
    #

    def has_shared_nfc_reader(self):
        return bool(self.mmu_unit.nfc_reader)


    def has_gate_nfc_reader(self, gate):
        nfc_readers = self.mmu_unit.nfc_readers
        if not nfc_readers:
            return False
        lgate = self.mmu_unit.local_gate(gate)
        return 0 <= lgate < len(nfc_readers) and bool(nfc_readers[lgate])


    def allow_reread(self):
        """
        Drop the read cooldown/dedupe so the currently presented tag can be read
        again immediately. Intended to be called after a *recoverable* lookup
        failure (e.g. Spoolman communication error) so the user doesn't have to
        remove and re-present the tag. A definitive "unknown tag" result should
        NOT call this (re-reading won't help).
        """
        self._last_uid = None
        self._hold_until = 0.0


    def read_gate(self, gate):
        """
        On-demand read of the per-gate reader for 'gate' and (if a tag is present)
        initiate a Spoolman lookup targeting that gate. Returns the raw UID read
        (or None). This is an *automatic* path so it honors both the enabled and
        active flags. Safe to call with no per-gate reader configured.
        """
        if not self.has_gate_nfc_reader(gate):
            return None
        if not self.is_enabled(gate=gate) or not self.is_active(gate=gate):
            return None
        reader = self._reader_for(gate=gate)
        if reader is None:
            return None
        uid, metadata = self._read_reader(reader, deep=self._want_metadata())
        if uid:
            self._dispatch_lookup(uid, gate=gate, metadata=metadata)
        return uid


    #
    # Per-reader enable/active control and manual operations (used by MMU_NFC) ---
    #

    def has_reader(self, shared=False, gate=None):
        return self._reader_for(shared=shared, gate=gate) is not None


    def is_enabled(self, shared=False, gate=None):
        if shared:
            return self.shared_enabled
        lg = self._local_index(gate)
        return self.gate_enabled[lg] if lg is not None else False


    def is_active(self, shared=False, gate=None):
        if shared:
            return self.shared_active
        lg = self._local_index(gate)
        return self.gate_active[lg] if lg is not None else False


    def set_enabled(self, value, shared=False, gate=None):
        """
        Set a reader's top-level enabled flag. Re-initializes on a transition
        to enabled, since a reader may have been powered down/idled while off.
        """
        value = bool(value)
        if shared:
            was, self.shared_enabled = self.shared_enabled, value
        else:
            lg = self._local_index(gate)
            if lg is None:
                return
            was, self.gate_enabled[lg] = self.gate_enabled[lg], value
        if value and not was:
            self.init_reader(shared=shared, gate=gate)


    def set_active(self, value, shared=False, gate=None):
        """
        Set a reader's runtime active flag (guards automatic reads only).
        """
        value = bool(value)
        if shared:
            self.shared_active = value
        else:
            lg = self._local_index(gate)
            if lg is not None:
                self.gate_active[lg] = value


    def init_reader(self, shared=False, gate=None):
        """
        (Re)initialize a single addressed reader. Returns alive bool, or None
        if no such reader is configured.
        """
        reader = self._reader_for(shared=shared, gate=gate)
        if reader is None:
            return None
        self._init_reader(reader, self._label_for(shared=shared, gate=gate))
        return reader.alive


    def init_all(self):
        """
        (Re)initialize every reader this unit controls.
        """
        self._init_all_readers()


    def read_reader(self, shared=False, gate=None):
        """
        Manual one-shot read of an addressed reader, returning the UID or None.

        Intended for the MMU_NFC command. Overrides the 'active' guard (an explicit
        request is not an accidental read); callers should honor 'enabled' first.
        Does not dispatch a Spoolman lookup - it just reports the tag.
        """
        reader = self._reader_for(shared=shared, gate=gate)
        if reader is None:
            return None
        uid, _metadata = self._read_reader(reader)
        return uid


    def release_reader(self, shared=False, gate=None):
        """
        Release the current target on an addressed reader.
        """
        reader = self._reader_for(shared=shared, gate=gate)
        if reader is None:
            return False
        try:
            return reader.release(reason="mmu_nfc_command")
        except Exception as e:
            self.mmu.log_error("NFC: release error on reader '%s': %s" % (getattr(reader, 'name', '?'), str(e)))
            return False


    def get_status(self, eventtime=None):
        """
        Pure status snapshot (no reader I/O) for printer variables.

        Shape: {'unit', 'polling', 'shared': {..}|None, 'gates': {global_gate: {..}}}
        where each reader dict reports enabled/active/alive/present/uid (the cached
        tag from the last read, not a live scan).
        """
        def reader_status(reader, enabled, active):
            return {
                'enabled': bool(enabled),
                'active': bool(active),
                'alive': bool(getattr(reader, 'alive', False)),
                'present': bool(getattr(reader, 'present', False)),
                'uid': getattr(reader, 'last_uid', None),
            }

        status = {'unit': self.mmu_unit.name, 'polling': self._polling}
        status['shared'] = (reader_status(self.shared_reader, self.shared_enabled, self.shared_active)
                            if self.shared_reader is not None else None)
        gates = {}
        for lg, reader in enumerate(self.gate_readers):
            if reader is not None:
                gates[self.mmu_unit.first_gate + lg] = reader_status(reader, self.gate_enabled[lg], self.gate_active[lg])
        status['gates'] = gates
        return status


    #
    # Internal implementation --------------------------------------------------
    #

    def _handle_mmu_enabled(self):
        """
        Event indicating that the MMU unit was enabled. (Re)arm shared-reader
        polling - enable/disable can cycle during a session.
        """
        self._start_polling()


    def _handle_mmu_disabled(self):
        """
        Event indicating that the MMU unit was disabled. Stop polling.
        """
        self._stop_polling()


    def _handle_mmu_bootup(self):
        """
        Delayed event fired once after MMU bootup. Initialize every reader we
        control and arm shared-reader polling.
        """
        self._init_all_readers()
        self._start_polling()


    def _handle_printing(self, print_time):
        """
        Deactivate all readers while actively printing so no NFC transaction runs
        on the reactor thread mid-print. The shared poll and auto read_gate honor
        the active flag; a manual MMU_NFC READ still overrides it if needed.
        """
        self._set_all_active(False)


    def _handle_not_printing(self, print_time):
        """
        Re-activate all readers once printing stops.
        """
        self._set_all_active(True)


    def _handle_spoolid_pending(self):
        """
        A shared-reader spool lookup is in flight (broadcast by the controller).
        Deactivate the shared reader so no competing lookup is dispatched until it
        resolves. Per-gate readers are unaffected.
        """
        self.shared_active = False


    def _handle_spoolid_not_pending(self, reread=False):
        """
        The in-flight shared lookup resolved. Re-activate the shared reader;
        on a recoverable failure (reread) also drop the dedup/cooldown so the same
        tag can be re-read immediately.
        """
        self.shared_active = True
        if reread:
            self.allow_reread()


    def _set_all_active(self, value):
        value = bool(value)
        self.shared_active = value
        self.gate_active = [value] * len(self.gate_active)


    def _local_index(self, gate):
        """
        Map a global gate number to a per-gate reader slot index, or None.
        """
        if gate is None:
            return None
        lgate = self.mmu_unit.local_gate(gate)
        return lgate if 0 <= lgate < len(self.gate_readers) else None


    def _reader_for(self, shared=False, gate=None):
        """
        Resolve the reader object addressed by (shared | gate), or None.
        """
        if shared:
            return self.shared_reader
        lg = self._local_index(gate)
        return self.gate_readers[lg] if lg is not None else None


    def _label_for(self, shared=False, gate=None):
        """
        Driver logging label for an addressed reader: unit name (shared) or
        logical gate number (per-gate).
        """
        if shared:
            return self.mmu_unit.name
        lg = self._local_index(gate)
        return self.mmu_unit.first_gate + lg if lg is not None else '?'


    def _all_readers(self):
        readers = []
        if self.shared_reader is not None:
            readers.append(self.shared_reader)
        readers.extend(r for r in self.gate_readers if r is not None)
        return readers


    def _init_all_readers(self):
        # The shared reader is labelled with the unit name; per-gate readers with
        # their logical gate number (local index + first_gate).
        if self.shared_reader is not None:
            self._init_reader(self.shared_reader, self.mmu_unit.name)
        for lgate, reader in enumerate(self.gate_readers):
            if reader is not None:
                self._init_reader(reader, self.mmu_unit.first_gate + lgate)


    def _init_reader(self, reader, gate):
        name = getattr(reader, 'name', '?')
        try:
            alive = reader.init(gate)
            if alive:
                self.mmu.log_debug("NFC: reader '%s' initialized (gate=%s)" % (name, gate))
            else:
                self.mmu.log_warning("NFC: reader '%s' did not respond during init" % name)
        except Exception as e:
            self.mmu.log_error("NFC: error initializing reader '%s': %s" % (name, str(e)))


    def _start_polling(self):
        if self.shared_reader is None:
            return # Nothing to poll
        if not self._polling:
            self.mmu.log_info("NFC: shared reader '%s' listening for tags" % self.mmu_unit.nfc_reader)
        self._polling = True
        self.reinit()
        self.reactor.update_timer(self._poll_timer, self.reactor.NOW)


    def _stop_polling(self):
        self._polling = False
        self.reactor.update_timer(self._poll_timer, self.reactor.NEVER)


    def _poll_shared_reader(self, eventtime):
        """
        Reactor callback to periodically read the shared reader.
        Note: this performs a live NFC transaction, so the read timeout is kept
        small. Reads are suppressed during printing via the active flag (cleared
        on mmu:printing) to keep the reactor responsive.
        """
        if not self._polling or self.mmu is None or self.shared_reader is None:
            return self.reactor.NEVER

        # Respect the reader's enabled (hard off) and active (soft guard) flags.
        # Keep ticking so a later re-enable/re-activate resumes automatically.
        if not self.shared_enabled or not self.shared_active:
            return eventtime + NFC_CHECK_INTERVAL

        now = self.reactor.monotonic()

        # Cooldown after a recent read
        if now < self._hold_until:
            return eventtime + NFC_CHECK_INTERVAL

        uid, metadata = self._read_reader(self.shared_reader, deep=self._want_metadata())

        if uid:
            # Only act on a newly presented tag; a tag left on the reader keeps
            # the same UID and must not re-trigger a Spoolman lookup
            if uid != self._last_uid:
                self._last_uid = uid
                self._hold_until = now + NFC_TAG_HOLD_TIME
                self.mmu.log_debug("NFC: shared reader read tag UID=%s" % uid)
                self._dispatch_lookup(uid, gate=None, metadata=metadata)
        else:
            # No tag present - forget the held UID so re-presentation is honored
            self._last_uid = None

        return eventtime + NFC_CHECK_INTERVAL


    def _read_reader(self, reader, deep=False):
        """
        Read a tag from 'reader', returning (uid, metadata).

        With deep=False (default) reads just the UID via read_uid() - the tag
        contents are never parsed and metadata is None. With deep=True (used only
        when Spoolman auto-create is enabled) performs a structured read and
        returns the parsed tag metadata dict alongside the UID. Both paths
        auto-release the target, so no separate release step is required.
        """
        try:
            if deep:
                uid, metadata = reader.read_tag_data(timeout=NFC_READ_TIMEOUT)
            else:
                uid = reader.read_uid(timeout=NFC_READ_TIMEOUT)
                metadata = None
        except Exception as e:
            self.mmu.log_error("NFC: read error on reader '%s': %s" % (getattr(reader, 'name', '?'), str(e)))
            return None, None
        return (str(uid) if uid else None), metadata


    def _want_metadata(self):
        """
        True when reads should be deep (parse the full tag contents). Gated by
        nfc_deep_read so that with nfc_deep_read=0 the tag is never parsed - only
        the UID is read - and no metadata is produced, applied, or sent. When on,
        metadata populates the local gate map (and, if further enabled, drives
        Spoolman auto-create) independently of whether Spoolman is configured.
        """
        return self.mmu is not None and self.mmu.nfc_deep_read_enabled()


    def _dispatch_lookup(self, uid, gate=None, metadata=None):
        """
        Hand a tag read to the controller (fire-and-forget). The controller
        applies deep-read 'metadata' to the local gate map and, if Spoolman is
        active, initiates the async UID->spool resolution (whose result returns
        later as an MMU_GATE_MAP command from Moonraker). Nothing is awaited here.

        'metadata' is the parsed tag payload from a deep read (dict), or None for
        a UID-only read.
        """
        try:
            self.mmu._nfc_tag_read(uid, gate=gate, metadata=metadata)
        except Exception as e:
            self.mmu.log_error("NFC: error initiating tag lookup for UID=%s: %s" % (uid, str(e)))
