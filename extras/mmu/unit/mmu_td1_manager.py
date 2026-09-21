# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to coordinate the TD-1 scanners associated with an MMU unit
#
# Modelled on MmuNfcManager. The scanners themselves are USB devices owned by
# Moonraker, so unlike the NFC readers there is no chip to drive here - this manager
# owns the *policy*: which gate a scanner serves, whether a reading may be attributed
# to it, and when filament movement should wait for one.
#
# The Moonraker transport is deliberately NOT here. It registers a single klipper
# webhook endpoint and pairs with one remote method, so it lives once at machine level
# in extras/mmu/mmu_td1.py (MmuTd1Bridge), the same way MmuNfcFieldArbiter sits beside
# the per-unit NFC managers. Device objects are shared through the klipper object
# registry, so a serial named by two units is one MmuTd1Device.
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
from ..mmu_td1           import (
    measurement, MmuTd1Error, MmuTd1BridgeError, MmuTd1BridgeTimeout, MmuTd1NoReading,
    TD1_ERR_NO_READING, TD1_REQUEST_TIMEOUT, TD1_WAIT_GRANULARITY,
)
from .td1.mmu_td1_device import MmuTd1Device


class MmuTd1Manager:
    """
    Coordinate this unit's TD-1 scanners and the gate metadata they produce.

    Gate arguments are GLOBAL gate numbers throughout the public API, matching every
    other caller in the codebase; _local_index() converts.
    """

    def __init__(self, config, mmu_unit, params):
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.mmu = None                         # Set at klippy:connect

        # Device objects. Looked up before being created so a serial repeated across
        # gates - or named by another unit - is one object, which is what keeps "one
        # scanner, one debt" true by construction.
        self.shared_device = None   # Off-path: filament is presented to it by hand
        self.gate_devices = []      # Per-local-gate, in the filament path (or None)
        self._setup_devices()

        # Bumped when a gate's filament identity changes, so a capture already in flight
        # discards its own result rather than applying it to a new spool
        self.revisions = [0] * mmu_unit.num_gates

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("mmu:gate_filament_changed", self.filament_changed)


    def reinit(self):
        # State reset on (re)initialization. Called by mmu_unit.reinit().
        self.release()


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def _setup_devices(self):
        """
        Build (or look up) the device objects this unit references.

        The two are independent, as with the NFC readers: a unit may have per-gate
        scanners, an off-path one, both, or neither.
        """
        self.shared_device = self._lookup_or_create_device(self.mmu_unit.td1_device)
        serials = self.mmu_unit.td1_devices or [''] * self.mmu_unit.num_gates
        self.gate_devices = [self._lookup_or_create_device(serial) for serial in serials]


    def _lookup_or_create_device(self, serial):
        if not serial:
            return None
        section = 'mmu_td1_device %s' % serial
        obj = self.printer.lookup_object(section, None)
        auto = bool(self.p.td1_auto_update)
        if obj is not None:
            # Shared between gates, or with another unit. One physical scanner cannot
            # be auto-updating for one unit and not another
            if obj.auto != auto:
                raise self.config.error(
                    "Units sharing TD-1 scanner '%s' must agree on td1_auto_update" % serial)
            return obj
        obj = MmuTd1Device(serial, auto=auto)
        self.printer.add_object(section, obj)
        logging.info("MMU: Created: [%s]" % section)
        return obj


    #
    # Public access -------------------------------------------------------------
    #

    def has_td1(self):
        return bool(self.shared_device) or any(self.gate_devices)


    def has_shared_td1(self):
        return self.shared_device is not None


    def has_gate_td1(self, gate):
        return self.device_for(gate) is not None


    def serial_for(self, gate):
        device = self.device_for(gate)
        return device.serial if device is not None else ""


    def addressed_device(self, gate):
        """
        The device MMU_TD1 acts on when the user names a gate.

        The gate's own in-path scanner, else this unit's off-path one - the same order
        register_reading() resolves in, so reporting and acting agree.
        """
        return self.device_for(gate) or self.shared_device


    def addressed_serial(self, gate):
        device = self.addressed_device(gate)
        return device.serial if device is not None else ""


    def device_for(self, gate):
        """The device serving 'gate', without any health check. None if unassigned."""
        local = self._local_index(gate)
        if local is None:
            return None
        return self.gate_devices[local]


    def devices(self):
        """Every distinct device this unit references."""
        seen = {}
        for device in [self.shared_device] + self.gate_devices:
            if device is not None:
                seen.setdefault(device.serial, device)
        return list(seen.values())


    def _local_index(self, gate):
        local = gate - self.mmu_unit.first_gate
        if 0 <= local < len(self.gate_devices):
            return local
        return None


    def _owns(self, gate):
        return self._local_index(gate) is not None


    @property
    def bridge(self):
        return self.mmu.td1


# -----------------------------------------------------------------------------------------------------------
# GATE METADATA
# -----------------------------------------------------------------------------------------------------------

    def filament_changed(self, gate):
        """
        Drop attribution for a gate whose filament identity has changed.

        Handles 'mmu:gate_filament_changed' for this unit's gates only. The measurements
        themselves are gate-map fields and the gate map clears them; what goes here is
        Happy Hare's belief about which filament a scanner is looking at. Bumping the
        revision makes any capture already in flight discard its own result rather than
        apply it to a new spool.
        """
        local = self._local_index(gate)
        if local is None:
            return
        self.revisions[local] += 1
        self.release(gate=gate)


    def release(self, gate=None, device=None):
        """Drop armed attribution on this unit's devices, recording any debt."""
        for candidate in self.devices():
            if device is None or candidate is device:
                candidate.release(gate=gate)


    def revision(self, gate):
        local = self._local_index(gate)
        return self.revisions[local] if local is not None else -1


    def apply(self, gate, record):
        """
        Apply a measured TD and its separate measured color to one gate.

        Both are ordinary gate-map fields, so one write persists them together and
        notifies LEDs, macros and the lane-data push exactly once. A reading identical to
        what the gate already holds is dropped rather than churning any of that.
        """
        try:
            valid = measurement(record)
        except (ValueError, TypeError) as exc:
            # Callers reach here from g-code handlers, where anything that isn't an
            # MmuError becomes a Klipper shutdown rather than a reported error
            raise MmuTd1NoReading("TD-1: %s" % exc) from exc
        maps = self.mmu.gate_maps
        if maps.gate_td[gate] == valid['td'] and maps.gate_td1_color[gate] == valid['color']:
            return
        maps.renew_gate_map()
        maps.gate_td[gate] = valid['td']
        maps.gate_td1_color[gate] = valid['color']
        self.adopt_color(gate)
        maps.update_gate_color_rgb()
        maps.persist_gate_map(changed_gate=gate)


    def adopt_color(self, gate, force=False):
        """
        Use the measured color as the gate's filament color.

        Only when nothing else has claimed that field. Spoolman owns filament_color
        whenever it has an opinion, and a manually set color is the user's, so a
        non-empty value is left alone - a scanner's guess should not overwrite a
        answer that came from the spool itself.

        'force' is the explicit override (MMU_TD1 SET_COLOR=1). Note that on a gate
        with a Spoolman spool the next refresh will put Spoolman's color back.

        Returns True if the gate's color changed; the caller persists.
        """
        maps = self.mmu.gate_maps
        # Carries an alpha channel derived from the TD, so a translucent filament
        # renders as one rather than as flat color
        measured = maps.td1_rgba(gate)
        if not measured or maps.gate_color[gate] == measured:
            return False
        if maps.gate_color[gate] and not force:
            return False
        maps.gate_color[gate] = measured
        return True


    def healthy(self, device):
        """
        Raise unless this scanner is enabled, connected and fault free.

        A scanner that has simply not measured anything yet counts as healthy - that is
        the normal state before filament first reaches it.
        """
        if not device.enabled:
            raise MmuTd1Error("TD-1: scanner %s is disabled" % device.serial)
        if not device.connected:
            raise MmuTd1BridgeError("TD-1: scanner %s is disconnected" % device.serial)
        # A device that simply hasn't measured yet is healthy - that's the normal state
        # before filament first reaches it, and the whole point of scanning
        if device.error and device.error_kind != TD1_ERR_NO_READING:
            raise MmuTd1Error("TD-1: %s" % device.error)
        return device


    def device(self, gate):
        """Resolve an enabled, connected and healthy in-path scanner for a gate."""
        device = self.device_for(gate)
        if device is None:
            raise MmuTd1Error("TD-1: no scanner configured for gate %d" % gate)
        return self.healthy(device)


    def validated(self, device):
        """
        A healthy scanner's latest measurement, as a normalized record.

        healthy() lets a scanner that has simply not measured yet through, so this is
        where "healthy but nothing to report" becomes a proper MmuError rather than the
        ValueError that measurement() raises.
        """
        self.healthy(device)
        try:
            return measurement(device.record())
        except (ValueError, TypeError) as exc:
            raise MmuTd1NoReading("TD-1: %s" % exc) from exc


    def reading(self, gate):
        """The latest valid measurement from the in-path scanner serving 'gate'."""
        return self.validated(self.device(gate))


    def register_reading(self, gate):
        """
        The measurement MMU_TD1 REGISTER should attribute to 'gate'.

        A gate's own in-path scanner first, since it is the one filament actually
        crossed. Then this unit's off-path scanner, which is the whole point of
        REGISTER - you presented filament to it by hand. Then anything already staged
        as pending, so a reading taken before the gate had an identity is not lost.
        """
        if self.has_gate_td1(gate):
            return self.reading(gate)
        if self.shared_device is not None:
            return self.validated(self.shared_device)
        pending = self.mmu.pending_measurement
        if pending is not None:
            return pending
        raise MmuTd1Error("TD-1: no scanner configured for gate %d" % gate)


    def wait_measurement(self, gate, baseline, timeout):
        """
        Wait for a measurement newer than 'baseline', for at most 'timeout' seconds.

        Every round trip is capped to the time actually remaining, so the caller's
        timeout means what it says. A transport stall inside that window is just another
        reason we don't have a reading yet, and is reported as one - only a bridge that
        is genuinely unusable propagates, because no amount of waiting will fix it.
        """
        deadline = self.reactor.monotonic() + timeout
        bridge = self.bridge
        bridge.busy = True
        bridge.active_serial = self.serial_for(gate)
        try:
            while True:
                device = self.device(gate)
                stamp = device.scan_time
                if stamp is not None and (baseline is None or stamp > baseline):
                    if device.claimable(gate):
                        device.settle()
                        return self.reading(gate)
                    # Owed to a gate that already gave up waiting. Take it off the
                    # scanner's books and keep waiting for one that is ours
                    self.mmu.log_debug(
                        "TD-1: discarded a reading still owed to gate %d" % device.unclaimed)
                    device.settle()
                    baseline = stamp
                    continue
                remaining = deadline - self.reactor.monotonic()
                if remaining <= 0:
                    # Filament crossed the scanner and we stopped waiting for what it
                    # measured, so whatever turns up next is not the next gate's
                    device = self.device_for(gate)
                    if device is not None:
                        device.owe(gate)
                    raise MmuTd1NoReading("TD-1: no fresh measurement for gate %d" % gate)
                try:
                    bridge.refresh(timeout=min(remaining, TD1_REQUEST_TIMEOUT))
                except MmuTd1BridgeTimeout:
                    continue
                if self.reactor.monotonic() < deadline:
                    self.reactor.pause(self.reactor.monotonic() + TD1_WAIT_GRANULARITY)
        finally:
            # Must not leak: a stuck 'busy' silences the passive path and pins polling
            # at its active interval for the rest of the session
            bridge.busy = False
            bridge.active_serial = ""


    def needs_measurement(self, gate):
        """
        True when this gate has a scanner but nothing measured to show for it.

        "Unmeasured" is the whole staleness rule, and it costs nothing to maintain:
        gate_td is cleared whenever a gate's filament identity changes - spool swap,
        RFID change, gate emptied, manual TD edit - so a gate that still holds a value
        holds one that describes the filament actually in it.
        """
        return self.has_gate_td1(gate) and self.mmu.gate_td[gate] is None


    def baseline(self, gate):
        """
        The scanner's latest reading time, sampled before filament is moved past it.

        Raises if the scanner is unusable, letting a caller decide not to move at all
        rather than discovering the problem after a bowden's worth of travel.
        """
        return self.device(gate).scan_time


    def capture(self, gate, baseline):
        """
        Wait for a reading newer than 'baseline' and apply it to the gate.

        Called once filament has traversed the scanner. Raises MmuTd1NoReading when
        nothing arrives in time; the filament is left exactly where the caller put it,
        so unloading and recovery remain the caller's business.
        """
        record = self.wait_measurement(gate, baseline, self.p.td1_capture_timeout)
        self.apply(gate, record)


# -----------------------------------------------------------------------------------------------------------
# AUTOMATIC CAPTURE DURING NORMAL LOADING
# -----------------------------------------------------------------------------------------------------------

    def begin_load(self, gate):
        """
        Establish ownership before a normal load crosses the scanner.

        Deliberately passive: it reads the poller's cache and never waits on Moonraker,
        because this runs inside every tool change. Arming ownership here is what lets a
        reading that arrives later be attributed to the right gate without guessing.
        """
        device = self.device_for(gate) if gate >= 0 else None
        if device is None:
            return None
        capture = bool(self.p.td1_capture_on_load)
        if not device.enabled or not (device.auto or capture):
            return None
        # Whatever was armed before is finished with either way, and if it never
        # produced a reading the scanner still owes that gate one
        device.release()
        try:
            self.device(gate)
        except MmuError as exc:
            self.mmu.log_debug(str(exc))
            return None
        token = {'gate': gate, 'revision': self.revision(gate),
                 'baseline': device.scan_time, 'capture': capture,
                 'satisfied': False}
        device.arm(token)
        return token


    def end_load(self, token, success):
        """
        Capture after loading while preserving explicit ownership.

        While printing we never wait: ownership stays armed and the bridge's poll applies
        whatever it delivers, so opting into capture costs no tool-change time.
        """
        if token is None:
            return
        gate = token['gate']
        device = self.device_for(gate)
        if device is None:
            return
        if (not success
            or self.revision(gate) != token['revision']
            or self.mmu.gate_selected != gate
            or device.owner is not token):
            device.release()
            return
        if not token['capture'] or self.mmu.is_printing():
            return
        try:
            record = self.wait_measurement(gate, token['baseline'], self.p.td1_capture_timeout)
            if (self.revision(gate) != token['revision']
                or self.mmu.gate_selected != gate
                or device.owner is not token):
                device.release()
                return
            self.apply(gate, record)
            token['satisfied'] = True
        except MmuError as exc:
            # A measurement failure never invalidates an otherwise successful load
            self.mmu.log_warning(str(exc))


# -----------------------------------------------------------------------------------------------------------
# PASSIVE ATTRIBUTION
# -----------------------------------------------------------------------------------------------------------

    def stage(self, device, valid, previous):
        """
        Stage an off-path reading as pending, for the gate preloaded next.

        The mirror of a shared NFC tag read: the scanner serves no filament path, so
        there is no gate to attribute to yet and guessing at the loaded one would be
        exactly the misattribution the in-path rules exist to prevent. The user says
        which gate it was by preloading one (or by MMU_TD1 GATE=n REGISTER=1).
        """
        if device is not self.shared_device or not device.enabled:
            return
        # Filament left in the reader is reported on every poll, with the scan_time it
        # was first read at. Staging only a reading newer than the cached one is the
        # dedupe - MmuNfcManager does the same job by UID.
        #
        # Deliberately no was_connected guard, unlike consider(): the cached reading
        # survives a disconnect, so this same test already rejects a stale re-report
        # after one. The guard would only throw away the first genuine reading after a
        # Moonraker hiccup, silently, leaving the user to present filament twice
        if previous is not None and valid['scan_time'] <= previous:
            return
        self.mmu.stage_pending_measurement(valid)
        device.last_outcome = 'staged as pending for the next gate'


    def consider(self, device, valid, previous, was_connected):
        """
        Decide whether a reading the bridge just cached belongs to a gate.

        Called for a device this unit's armed token belongs to. A reading is only
        written when this manager can say which filament produced it.
        """
        token = device.owner
        if token is None:
            return
        if not (device.enabled
                and (device.auto or token['capture'])
                and was_connected
                and (previous is None or valid['scan_time'] > previous)
                and not self.bridge.busy):
            return
        gate = token['gate']
        if not device.claimable(gate):
            # Owed to a gate that has already stopped waiting; this reading settles
            # that debt and is attributed to nobody
            device.last_outcome = ("status_only: settled an unclaimed reading from gate %d"
                                   % device.unclaimed)
            device.settle()
        elif (self.revision(gate) == token['revision']
              and self.mmu.gate_selected == gate
              and self.mmu.filament_pos == FILAMENT_POS_LOADED):
            self.apply(gate, valid)
            token['satisfied'] = True
            device.settle()
            device.last_outcome = "applied to gate %d" % gate


# -----------------------------------------------------------------------------------------------------------
# STATUS
# -----------------------------------------------------------------------------------------------------------

    def gate_states(self):
        """
        This unit's per-gate policy, in local gate order.

        Policy only. Connectivity, the latest reading and any device fault come from
        Moonraker's own [td1] endpoint, and the gate/serial assignment is already in
        printer.mmu_machine - none of that is repeated here.
        """
        states = []
        for device in self.gate_devices:
            if device is None:
                states.append(TD1_STATE_NONE)
            elif not device.enabled:
                states.append(TD1_STATE_DISABLED)
            elif device.auto:
                states.append(TD1_STATE_AUTO)
            else:
                states.append(TD1_STATE_ENABLED)
        return states
