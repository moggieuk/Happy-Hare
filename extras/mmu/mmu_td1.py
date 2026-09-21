# Happy Hare MMU Software
# TD-1 Moonraker bridge
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Carry Moonraker's [td1] readings into Happy Hare, and nothing else
#
# The scanners are USB devices owned by Moonraker. This bridge polls them over a
# klipper webhook (never the g-code queue) and refreshes the per-serial MmuTd1Device
# caches. Deciding which gate a reading belongs to, and moving filament to produce
# one, is the per-unit MmuTd1Manager's job.
#
# Machine level rather than per unit because it registers a single webhook endpoint
# ('mmu/td1') and pairs with one 'mmu_td1_request' remote method - registering either
# twice is an error. It sits beside the per-unit managers the same way
# MmuNfcFieldArbiter sits beside the per-unit NFC managers.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import math, re
from datetime import datetime, timezone

# Happy Hare imports
from .mmu_constants import *
from .mmu_utils     import MmuError


# Reactor timings. All bounded: nothing here may stall the printer indefinitely
TD1_READY_DELAY      = 2.0    # Settle after klippy:ready before the first poll
TD1_POLL_ACTIVE      = 1.0    # Poll interval while a reading is actually wanted
TD1_POLL_IDLE        = 10.0   # Poll interval when nothing is consuming readings
TD1_POLL_DEADLINE    = 5.0    # Give up on an unanswered background poll
TD1_REQUEST_TIMEOUT  = 6.0    # Bounded wait for an explicitly requested refresh
TD1_REBOOT_TIMEOUT   = 35.0   # MMU_TD1 INIT=1 - device reboot plus USB rediscovery
TD1_WAIT_GRANULARITY = 0.1    # Reactor pause while waiting on the bridge

# Structured reasons for a device being unusable, so control flow never parses messages
TD1_ERR_BRIDGE       = 'bridge'      # Moonraker bridge/transport failed
TD1_ERR_DEVICE       = 'device'      # Moonraker reported a device-level error
TD1_ERR_INVALID      = 'invalid'     # Malformed measurement record
TD1_ERR_NO_READING   = 'no_reading'  # Connected but hasn't measured yet - normal before first insert


class MmuTd1Error(MmuError):
    """Base for every TD-1 failure. MmuError, so existing recovery paths catch it."""

class MmuTd1BridgeError(MmuTd1Error):
    """Bridge unavailable or request rejected. Waiting won't fix it, so callers propagate."""

class MmuTd1BridgeTimeout(MmuTd1BridgeError):
    """One round trip ran out of time. Recoverable - the next may answer."""

class MmuTd1NoReading(MmuTd1Error):
    """No fresh measurement in time. The scanner failed, not the gate."""


def measurement(data):
    """
    Validate and normalize a Moonraker measurement.

    Raises ValueError for anything incomplete or implausible. Use has_reading() to tell
    "not measured yet" from "measured badly".
    """
    if not isinstance(data, dict):
        raise ValueError("TD-1 returned an invalid record")
    if data.get('error'):
        raise ValueError(str(data['error']))
    td = data.get('td')
    if isinstance(td, bool) or not isinstance(td, (int, float)):
        raise ValueError("TD-1 has no numeric measurement")
    if not math.isfinite(td) or td <= 0:
        raise ValueError("TD-1 transmission distance must be finite and positive")
    color = data.get('color')
    if not isinstance(color, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        raise ValueError("TD-1 has no valid RGB measurement")
    stamp = data.get('scan_time')
    if not isinstance(stamp, str):
        raise ValueError("TD-1 has no scan timestamp")
    # Moonraker has shipped both plain 'Z' and a doubled '+00:00Z' suffix
    if stamp.endswith("+00:00Z"):
        stamp = stamp[:-1]
    elif stamp.endswith("Z"):
        stamp = stamp[:-1] + "+00:00"
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        raise ValueError("TD-1 scan timestamp needs a timezone")
    return {'td': float(td), 'color': color.lower(),
            'scan_time': when.astimezone(timezone.utc).isoformat()}


def has_reading(data):
    """True if the record carries a measurement at all, right or wrong."""
    return isinstance(data, dict) and data.get('td') is not None


class MmuTd1Bridge:
    """
    The one Moonraker transport for every TD-1 scanner on the machine.

    Owns the webhook endpoint, poll cadence and per-serial device cache. No policy:
    which gate a reading belongs to is the owning unit's manager.
    """

    def __init__(self, mmu):
        self.mmu = mmu
        self.reactor = mmu.reactor
        self.webhooks = mmu.printer.lookup_object('webhooks')

        # serial -> MmuTd1Device, seeded from the managers so a configured device is the
        # same object here. Moonraker may report unreferenced ones too
        self.devices = {}
        for manager in self.managers():
            for device in manager.devices():
                self.devices[device.serial] = device

        self.pending = {}
        self.sequence = 0
        self.last_response = 0
        self.busy = False
        self.active_serial = ""
        self.connected = False

        self.webhooks.register_endpoint("mmu/td1", self._callback)
        mmu.printer.register_event_handler("klippy:ready", self._ready)
        mmu.printer.register_event_handler("klippy:disconnect", self._disconnect)


    def managers(self):
        """Every unit's TD-1 manager, in unit order."""
        return [unit.td1_manager for unit in self.mmu.mmu_machine.units
                if getattr(unit, 'td1_manager', None) is not None]


    def manager_for(self, gate):
        """The manager owning 'gate', or None."""
        for manager in self.managers():
            if manager._local_index(gate) is not None:
                return manager
        return None


    def release(self, gate=None):
        """Drop armed attribution across every unit, recording any debt."""
        for manager in self.managers():
            manager.release(gate=gate)


    def set_device_state(self, serial, enabled=None, auto=None):
        """
        Change a scanner's runtime enable/auto state. Lasts until restart.

        Attribution armed under the previous policy is dropped.
        """
        device = self.devices[serial]
        if enabled is not None:
            device.enabled = bool(enabled)
        if auto is not None:
            device.auto_override = bool(auto)
        device.release()


# -----------------------------------------------------------------------------------------------------------
# MOONRAKER BRIDGE
# -----------------------------------------------------------------------------------------------------------

    def _ready(self):
        """Start polling once Klipper is ready. Nothing polls if no scanner is configured."""
        self.connected = True
        if self.devices:
            self.reactor.register_timer(self._poll, self.reactor.monotonic() + TD1_READY_DELAY)


    def _disconnect(self):
        """
        Invalidate ownership and pending transactions on disconnect.

        Nothing observed beforehand describes what is in a gate afterwards.
        """
        self.connected = False
        self.pending.clear()
        for device in self.devices.values():
            device.disconnected()


    def _wants_readings(self):
        """
        True while a reading would actually be consumed.

        Polling feeds automatic updates and in-flight captures. With neither, readings
        are only wanted for status, so the interval backs right off.
        """
        if self.busy or any(d.owner is not None for d in self.devices.values()):
            return True
        if any(m.auto_wanted() for m in self.managers()):
            return True
        # An off-path scanner exists to produce readings to stage, so one is always
        # wanted. Nothing arms for it, so otherwise it would poll at the idle interval
        # while the user stands there waiting
        return any(m.shared_device is not None and m.shared_device.enabled
                   for m in self.managers())


    def _poll(self, eventtime):
        """
        Poll every device without blocking Klipper's reactor.

        One request services all scanners; the interval follows _wants_readings().
        """
        if not self.connected:
            return self.reactor.NEVER
        if not self.pending:
            self.sequence += 1
            request_id = self.sequence
            self.pending[request_id] = {'deadline': eventtime + TD1_POLL_DEADLINE, 'done': False}
            try:
                self.webhooks.call_remote_method("mmu_td1_request", request_id=request_id)
            except self.mmu.printer.command_error:
                self.pending.pop(request_id, None)
                self.update_devices({}, "TD-1 bridge unavailable", TD1_ERR_BRIDGE)
        for key, request in list(self.pending.items()):
            if request['deadline'] < eventtime:
                self.pending.pop(key, None)
                self.update_devices({}, "TD-1 response timed out", TD1_ERR_BRIDGE)
        return eventtime + (TD1_POLL_ACTIVE if self._wants_readings() else TD1_POLL_IDLE)


    def _callback(self, request):
        """
        Deliver a Moonraker response without entering the G-code queue.

        Replies can overtake each other, so a stale one is acknowledged but not applied.
        """
        request_id = request.get_int('request_id')
        pending = self.pending.get(request_id)
        if pending is None:
            request.send({})
            return
        data = request.get('devices', {})
        error = request.get('error', None)
        pending.update(done=True, error=error)
        # Responses can overtake each other; never let a stale one clobber newer state
        if request_id > self.last_response:
            self.last_response = request_id
            self.update_devices(data, error, TD1_ERR_BRIDGE if error else None)
        if not pending.get('wait'):
            self.pending.pop(request_id, None)
        request.send({})


    def refresh(self, serial="", reset=False, timeout=None):
        """
        Wait cooperatively for a bounded Moonraker response.

        'timeout' caps the round trip to the time the caller actually has. The filament
        load path never calls this - see MmuTd1Manager.begin_load.
        """
        if timeout is None:
            timeout = TD1_REBOOT_TIMEOUT if reset else TD1_REQUEST_TIMEOUT
        self.sequence += 1
        key = self.sequence
        pending = {'deadline': self.reactor.monotonic() + timeout, 'done': False, 'wait': True}
        self.pending[key] = pending
        try:
            self.webhooks.call_remote_method(
                "mmu_td1_request", request_id=key, serial=serial, reset=reset)
            while not pending['done']:
                now = self.reactor.monotonic()
                if now >= pending['deadline'] or not self.connected:
                    raise MmuTd1BridgeTimeout("TD-1: Moonraker response timed out")
                self.reactor.pause(min(now + TD1_WAIT_GRANULARITY, pending['deadline']))
            if pending.get('error'):
                raise MmuTd1BridgeError("TD-1: %s" % pending['error'])
        except self.mmu.printer.command_error as exc:
            raise MmuTd1BridgeError("TD-1: Moonraker bridge unavailable: %s" % exc) from exc
        finally:
            self.pending.pop(key, None)


    def _adopt(self, serial):
        """
        Start tracking a scanner Moonraker reports that no gate references.

        MMU_TD1 listing it with "Gates: none" is how you find a serial to configure.
        """
        from .unit.td1.mmu_td1_device import MmuTd1Device
        device = MmuTd1Device(serial)
        self.devices[serial] = device
        return device


    def update_devices(self, data, error=None, error_kind=None):
        """
        Refresh the scanner caches from one Moonraker response.

        Cache only - MmuTd1Manager.consider() decides what may be written to a gate.
        """
        if not isinstance(data, dict):
            data, error, error_kind = {}, "Invalid TD-1 device list", TD1_ERR_BRIDGE
        for serial in sorted(set(self.devices) | set(data)):
            device = self.devices.get(serial) or self._adopt(serial)
            previous = device.scan_time
            was_connected = device.connected
            device.connected = serial in data and error is None
            device.error = error
            device.error_kind = error_kind
            if not device.connected:
                device.last_outcome = 'disconnected'
                device.disconnected()
                continue
            record = data[serial]
            reported = record.get('error') if isinstance(record, dict) else None
            if not reported and not has_reading(record):
                # Normal before the first insertion - not a fault, just nothing to report
                if previous is not None:
                    # Had a reading, now reports none. On an off-path scanner that is
                    # filament being taken out - see MmuTd1Manager.removed()
                    for manager in self.managers():
                        if manager.shared_device is device:
                            manager.removed(device)
                            break
                device.forget_reading()
                device.error = "TD-1 has not measured anything yet"
                device.error_kind = TD1_ERR_NO_READING
                device.last_outcome = 'no measurement yet'
                continue
            try:
                valid = measurement(record)
            except (ValueError, TypeError) as exc:
                device.error = str(exc)
                device.error_kind = TD1_ERR_DEVICE if reported else TD1_ERR_INVALID
                device.last_outcome = 'invalid measurement'
                continue
            device.cache(valid)
            device.last_outcome = 'status_only: no attributable loaded gate'
            token = device.owner
            if token is not None:
                manager = self.manager_for(token['gate'])
                if manager is not None:
                    manager.consider(device, valid, previous, was_connected)
                continue
            # Nobody armed for this reading. From an off-path scanner it is staged as
            # pending instead. Only the first unit naming it stages - 'pending' is
            # machine level
            for manager in self.managers():
                if manager.shared_device is device:
                    manager.stage(device, valid, previous)
                    break


# -----------------------------------------------------------------------------------------------------------
# STATUS
# -----------------------------------------------------------------------------------------------------------

    def get_status(self, eventtime=None):
        """
        Report Happy Hare's own policy toward each gate's scanner, indexed by gate.

        A flat per-gate list aggregated across units, like 'espooler' and 'drying_state'.
        NFC publishes a per-unit dict instead: its shared reader serves the unit rather
        than a gate, so it has no per-gate answer to give. Every TD-1 gate does.
        """
        states = [TD1_STATE_NONE] * self.mmu.num_gates
        for manager in self.managers():
            first = manager.mmu_unit.first_gate
            for local, state in enumerate(manager.gate_states()):
                states[first + local] = state
        return {'td1': states}


    def gates_for(self, serial):
        """
        Global gates served by one physical scanner.

        A serial may be named by several gates and several units, so this is what
        answers "what does disabling this device affect?".
        """
        gates = []
        for manager in self.managers():
            first = manager.mmu_unit.first_gate
            for local, device in enumerate(manager.gate_devices):
                if device is not None and device.serial == serial:
                    gates.append(first + local)
        return sorted(gates)
