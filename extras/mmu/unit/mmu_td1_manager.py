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

        # Looked up before created, so a serial repeated across gates - or named by
        # another unit - resolves to one object
        self.shared_device = None   # Off-path: filament is presented to it by hand
        self.gate_devices = []      # Per-local-gate, in the filament path (or None)
        self._setup_devices()

        # The reading last staged from the off-path scanner - see stage()
        self._staged = None

        # Bumped when a gate's filament identity changes, so a capture already in flight
        # discards its own result rather than applying it to a new spool
        self.revisions = [0] * mmu_unit.num_gates

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("mmu:gate_filament_changed", self.filament_changed)


    def reinit(self):
        # State reset on (re)initialization. Called by mmu_unit.reinit().
        self.release()
        self._staged = None


    def allow_restage(self):
        """
        Let filament still in the off-path scanner be staged again (MmuNfcManager.allow_reread).

        Called when a pending times out, never when one is consumed - a measurement just
        applied must not immediately stage itself for the next gate.
        """
        self._staged = None


    def _handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller


    def _setup_devices(self):
        """Build (or look up) this unit's devices. Per-gate and off-path are independent."""
        self.shared_device = self._lookup_or_create_device(self.mmu_unit.td1_device)
        serials = self.mmu_unit.td1_devices or [''] * self.mmu_unit.num_gates
        self.gate_devices = [self._lookup_or_create_device(serial) for serial in serials]


    def _lookup_or_create_device(self, serial):
        if not serial:
            return None
        section = 'mmu_td1_device %s' % serial
        obj = self.printer.lookup_object(section, None)
        if obj is not None:
            return obj # Shared between gates, or with another unit
        obj = MmuTd1Device(serial)
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

        In-path scanner first, then the off-path one - the order register_reading() uses.
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


    def auto_for(self, device):
        """
        Whether a new reading from this device is applied automatically, for THIS unit.

        Resolved at point of use, so an MMU_TEST_CONFIG edit takes effect immediately and
        two units sharing a scanner can differ. The device carries only the MMU_TD1 AUTO=
        runtime override.
        """
        if device.auto_override is not None:
            return device.auto_override
        return bool(self.p.td1_auto_update)


    def auto_wanted(self):
        """True if any of this unit's scanners is auto-updating right now."""
        return any(d.enabled and self.auto_for(d) for d in self.devices())


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

        Handles 'mmu:gate_filament_changed' for this unit's gates. The measurements are
        gate-map fields and the gate map clears them; the revision bump makes a capture
        already in flight discard its result rather than apply it to a new spool.
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
        Apply a measured TD and measured color to one gate.

        Both are gate-map fields, so one write persists and notifies once. An identical
        reading is dropped rather than churning that.
        """
        try:
            valid = measurement(record)
        except (ValueError, TypeError) as exc:
            # Reached from g-code handlers, where a non-MmuError is a Klipper shutdown
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
        self.mmu._td1_led_on_measure(self.mmu_unit, gate=gate)


    def adopt_color(self, gate, force=False):
        """
        Use the measured color as the gate's filament color, if nothing else claimed it.

        A non-empty filament_color belongs to Spoolman or the user and is left alone.
        'force' (MMU_TD1 SET_COLOR=1) overrides, though a Spoolman refresh will win it
        back. Returns True if the color changed; the caller persists.
        """
        maps = self.mmu.gate_maps
        measured = maps.td1_rgba(gate) # Carries the TD-derived alpha
        if not measured or maps.gate_color[gate] == measured:
            return False
        if maps.gate_color[gate] and not force:
            return False
        maps.gate_color[gate] = measured
        return True


    def healthy(self, device):
        """
        Raise unless this scanner is enabled, connected and fault free.

        Not having measured anything yet is healthy - it is the state before filament
        first reaches the scanner.
        """
        if not device.enabled:
            raise MmuTd1Error("TD-1: scanner %s is disabled" % device.serial)
        if not device.connected:
            raise MmuTd1BridgeError("TD-1: scanner %s is disconnected" % device.serial)
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

        healthy() passes an unmeasured scanner, so this is where "nothing to report"
        becomes an MmuError rather than measurement()'s ValueError.
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

        In-path scanner (filament actually crossed it), then the off-path one, then
        anything already staged as pending.
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

        Each round trip is capped to the time remaining. A stall inside the window is
        reported as "no reading"; only an unusable bridge propagates.
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
                    # Owed to a gate that gave up waiting - settle it and keep waiting
                    self.mmu.log_debug(
                        "TD-1: discarded a reading still owed to gate %d" % device.unclaimed)
                    device.settle()
                    baseline = stamp
                    continue
                remaining = deadline - self.reactor.monotonic()
                if remaining <= 0:
                    # We stopped waiting, so the next reading is not the next gate's
                    device = self.device_for(gate)
                    if device is not None:
                        device.owe(gate)
                    self.mmu._td1_led_on_fail(self.mmu_unit, gate=gate)
                    raise MmuTd1NoReading("TD-1: no fresh measurement for gate %d" % gate)
                try:
                    bridge.refresh(timeout=min(remaining, TD1_REQUEST_TIMEOUT))
                except MmuTd1BridgeTimeout:
                    continue
                if self.reactor.monotonic() < deadline:
                    self.reactor.pause(self.reactor.monotonic() + TD1_WAIT_GRANULARITY)
        finally:
            # A stuck 'busy' silences the passive path and pins polling active
            bridge.busy = False
            bridge.active_serial = ""


    def needs_measurement(self, gate):
        """
        True when this gate has a scanner but nothing measured to show for it.

        "Unmeasured" is the whole staleness rule: gate_td is cleared on any filament
        identity change, so a surviving value describes the filament actually in the gate.
        """
        return self.has_gate_td1(gate) and self.mmu.gate_td[gate] is None


    def baseline(self, gate):
        """
        The scanner's latest reading time, sampled before filament is moved past it.

        Raises if the scanner is unusable, so a caller can decline to move at all.
        """
        return self.device(gate).scan_time


    def capture(self, gate, baseline):
        """
        Wait for a reading newer than 'baseline' and apply it to the gate.

        Called once filament has traversed the scanner. Raises MmuTd1NoReading on
        timeout, leaving the filament where it is - recovery is the caller's business.
        """
        record = self.wait_measurement(gate, baseline, self.p.td1_capture_timeout)
        self.apply(gate, record)


# -----------------------------------------------------------------------------------------------------------
# AUTOMATIC CAPTURE DURING NORMAL LOADING
# -----------------------------------------------------------------------------------------------------------

    def begin_load(self, gate):
        """
        Arm ownership before a normal load crosses the scanner.

        Passive: reads the poller's cache and never waits on Moonraker, because this runs
        inside every tool change.
        """
        device = self.device_for(gate) if gate >= 0 else None
        if device is None:
            return None
        capture = bool(self.p.td1_capture_on_load)
        if not device.enabled or not (self.auto_for(device) or capture):
            return None
        # Anything armed before is finished with - release() records the debt
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

        Never waits while printing: ownership stays armed and the poll applies whatever
        arrives, so capture costs no tool-change time.
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

    def same_filament(self, a, b):
        """
        True if two readings plausibly describe the same filament.

        Tolerance, not equality: an analogue instrument never repeats a reading exactly.
        The thresholds sit outside the quoted accuracy, so jitter reads as unchanged.
        """
        if a is None or b is None:
            return False
        if abs(a['td'] - b['td']) > TD1_SAME_TD_FRACTION * max(a['td'], b['td']):
            return False
        return all(abs(int(a['color'][i:i + 2], 16) - int(b['color'][i:i + 2], 16))
                   <= TD1_SAME_RGB_DISTANCE for i in (0, 2, 4))


    def removed(self, device):
        """
        Filament has left the off-path scanner - restart the pending window from now.

        Optional: only fires if the scanner reports nothing once filament is taken out,
        which Moonraker's API does not promise. Where it doesn't, stage() alone carries
        the feature. Only ever extends a live pending that still describes this reading;
        a consumed one must never be resurrected.
        """
        if device is not self.shared_device or not device.enabled:
            return
        try:
            record = measurement(device.record())
        except (ValueError, TypeError):
            return
        if not self.same_filament(self.mmu.pending_measurement, record):
            return
        self.mmu.stage_pending_measurement(record, removed=True)


    def stage(self, device, valid, previous):
        """
        Stage an off-path reading as pending, for the gate preloaded next.

        The mirror of a shared NFC tag read: the scanner serves no filament path, so
        there is no gate to attribute to. The user names one by preloading it (or with
        MMU_TD1 GATE=n REGISTER=1).
        """
        if device is not self.shared_device or not device.enabled:
            return
        # No was_connected guard, unlike consider(): the cached reading survives a
        # disconnect, so this already rejects a stale re-report after one
        if previous is not None and valid['scan_time'] <= previous:
            return
        # A device re-measuring filament left in it advances scan_time every time, so
        # dedupe on the measurement (MmuNfcManager does the same by UID). allow_restage()
        # lifts this once a pending times out
        if self.same_filament(self._staged, valid):
            device.last_outcome = 'status_only: already staged'
            return
        self._staged = valid
        self.mmu.stage_pending_measurement(valid)
        self.mmu._td1_led_on_measure(self.mmu_unit)
        device.last_outcome = 'staged as pending for the next gate'


    def consider(self, device, valid, previous, was_connected):
        """
        Decide whether a reading the bridge just cached belongs to a gate.

        Only written when this manager can say which filament produced it.
        """
        token = device.owner
        if token is None:
            return
        if not (device.enabled
                and (self.auto_for(device) or token['capture'])
                and was_connected
                and (previous is None or valid['scan_time'] > previous)
                and not self.bridge.busy):
            return
        gate = token['gate']
        if not device.claimable(gate):
            # Settles a debt to a gate that stopped waiting; attributed to nobody
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

        Policy only - connectivity and readings come from Moonraker's [td1] endpoint,
        and the gate/serial assignment from printer.mmu_machine.
        """
        states = []
        for device in self.gate_devices:
            if device is None:
                states.append(TD1_STATE_NONE)
            elif not device.enabled:
                states.append(TD1_STATE_DISABLED)
            elif self.auto_for(device):
                states.append(TD1_STATE_AUTO)
            else:
                states.append(TD1_STATE_ENABLED)
        return states
