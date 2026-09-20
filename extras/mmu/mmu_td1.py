# Happy Hare MMU Software
# TD-1 filament measurement coordinator
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Bridge Moonraker's [td1] scanners into the gate map - own gate attribution,
#       scan geometry and filament movement, but never the USB devices themselves
#
# The scanners are USB devices owned by Moonraker. Happy Hare polls them over a webhook
# bridge (never the g-code queue), decides which gate a reading belongs to, and applies
# transmission distance + measured color to that gate's map entry.
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
    """
    Base for every TD-1 failure.

    Subclasses MmuError so the existing pause/recovery paths still catch TD-1 problems
    without knowing anything about them.
    """

class MmuTd1BridgeError(MmuTd1Error):
    """
    The Moonraker bridge is unavailable or rejected the request.

    No amount of waiting fixes this, so callers propagate rather than retry.
    """

class MmuTd1BridgeTimeout(MmuTd1BridgeError):
    """
    One bridge round trip ran out of time.

    Recoverable where a longer wait is still running - the next round trip may answer.
    """

class MmuTd1NoReading(MmuTd1Error):
    """
    No fresh measurement arrived in time.

    Raised only once the filament is safely parked, so callers can treat it as "the
    scanner failed" rather than "the gate failed".
    """


def measurement(data):
    """
    Validate and normalize a Moonraker measurement.

    Raises ValueError for anything that isn't a complete, plausible reading. Callers
    that need to distinguish "not measured yet" from "measured badly" use has_reading().
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
    """
    True if the record carries a measurement at all, right or wrong.

    Distinguishes a scanner that has simply not seen filament yet (normal before the
    first insertion) from one that reported something unusable.
    """
    return isinstance(data, dict) and data.get('td') is not None


class MmuTd1:
    """
    Coordinate Moonraker measurements and gate ownership without owning USB devices.

    Machine-level rather than per-unit because one physical scanner can be shared by
    several units: each serial must be owned exactly once so access can be serialized
    and status reported once. Per-unit configuration still lives on the unit - scanner
    serials on [mmu_unit] (like nfc_readers) and tunables on [mmu_unit_parameters].
    """

    def __init__(self, mmu):
        self.mmu = mmu
        self.reactor = mmu.reactor
        self.webhooks = mmu.printer.lookup_object('webhooks')

        self.devices = {}                                   # serial -> live device cache
        self.paths = [{'serial': '', 'unit': None, 'local': gate}  # global gate -> scanner
                      for gate in range(mmu.num_gates)]
        self.owners = {}                                    # serial -> attribution token
        self.revisions = [0] * mmu.num_gates                # bumped when a gate's filament identity changes

        self.pending = {}
        self.sequence = 0
        self.last_response = 0
        self.busy = False
        self.active_serial = ""
        self.connected = False

        # Scanner serials are validated by MmuUnit; here we only index them by global gate
        for unit in mmu.mmu_machine.units:
            serials = unit.td1_devices or [unit.td1_device] * unit.num_gates
            for local, serial in enumerate(serials):
                gate = unit.first_gate + local
                self.paths[gate] = {'serial': serial, 'unit': unit.name, 'local': local}
                if serial:
                    auto = bool(unit.p.td1_auto_update)
                    if serial in self.devices and self.devices[serial]['auto'] != auto:
                        raise mmu.config.error(
                            "Units sharing TD-1 scanner '%s' must agree on td1_auto_update" % serial)
                    self.devices.setdefault(serial, self._new_device(serial, auto=auto))

        self.webhooks.register_endpoint("mmu/td1", self._callback)
        mmu.printer.register_event_handler("klippy:ready", self._ready)
        mmu.printer.register_event_handler("klippy:disconnect", self._disconnect)
        mmu.printer.register_event_handler("mmu:gate_filament_changed", self.filament_changed)


    def _new_device(self, serial, auto=False):
        """
        Build the live cache entry for one physical scanner.

        Everything here is runtime state. 'enabled' and 'auto' deliberately do not
        persist, following the NFC readers rather than the filament sensors - turning a
        scanner off is a "not right now" action, and the configuration is the record of
        what the machine is supposed to do.
        """
        return {'serial': serial, 'connected': False, 'enabled': True, 'auto': auto,
                'td': None, 'color': None, 'scan_time': None,
                'error': None, 'error_kind': None, 'last_outcome': None,
                'unclaimed': None}


    def set_device_state(self, serial, enabled=None, auto=None):
        """
        Change a scanner's runtime enable/auto state.

        Lasts until restart, when the configured td1_auto_update takes over again. Any
        attribution armed under the previous policy is dropped, since a reading arriving
        now was requested under rules that no longer apply.
        """
        device = self.devices[serial]
        if enabled is not None:
            device['enabled'] = bool(enabled)
        if auto is not None:
            device['auto'] = bool(auto)
        self.release(serial=serial)


    def release(self, gate=None, serial=None):
        """
        Drop armed attribution, recording what the scanner is still owed.

        A token that was armed and never produced a reading means filament crossed the
        scanner with nobody left to claim what it measured. Forgetting that is what lets
        the next gate adopt the previous gate's measurement, so the debt is recorded
        here rather than only where an explicit wait times out - during a print nothing
        waits at all, and the token is simply dropped at the next tool change.
        """
        for key, owner in list(self.owners.items()):
            if (gate is None or owner['gate'] == gate) and (serial is None or key == serial):
                self.owners.pop(key)
                if not owner.get('satisfied'):
                    self.owe(key, owner['gate'])


    def owe(self, serial, gate):
        """
        Record that this scanner may still report a reading produced by 'gate'.

        One slot is enough: a second unclaimed traverse only makes the pending reading
        older, and the question a claimant asks is never "how many" but "is the next
        reading certainly mine".
        """
        device = self.devices.get(serial)
        if device is not None:
            device['unclaimed'] = gate


    def claimable(self, serial, gate):
        """
        True when a reading arriving now is certainly attributable to 'gate'.

        False while the scanner owes a different gate. Moonraker timestamps a reading
        when it receives it, not when filament entered the scanner, so a late reading
        from the previous gate is indistinguishable by time from this gate's own - the
        debt is the only thing that separates them.
        """
        device = self.devices.get(serial)
        return device is None or device['unclaimed'] in (None, gate)


    def settle(self, serial):
        """
        Consume the outstanding debt with the reading that just arrived.

        The reading itself is written nowhere: it belongs to a gate that has already
        stopped waiting for it. Assumes the scanner reports in traversal order, which
        holds for one optical sensor on one filament path but is on the list of things
        still to confirm against real hardware - if it turns out a reading can overtake
        an older one, this settles with the wrong one and the claimant loses a
        measurement (it never gains a wrong one).
        """
        device = self.devices.get(serial)
        if device is not None:
            device['unclaimed'] = None


    def settings(self, gate):
        """
        Resolve this gate's tunables from its owning unit's parameters.

        Always read through here rather than cached, so MMU_TEST_CONFIG edits apply live.
        """
        return self.mmu.mmu_unit(gate).p


# -----------------------------------------------------------------------------------------------------------
# MOONRAKER BRIDGE
# -----------------------------------------------------------------------------------------------------------

    def _ready(self):
        """
        Start polling, but only once Klipper is ready.

        Nothing is polled at all when no scanner is configured.
        """
        self.connected = True
        if self.devices:
            self.reactor.register_timer(self._poll, self.reactor.monotonic() + TD1_READY_DELAY)


    def _disconnect(self):
        """
        Invalidate ownership and pending transactions on disconnect.

        Nothing observed before a disconnect can be trusted to describe what is in a
        gate afterwards, so attribution is dropped rather than carried over.
        """
        self.connected = False
        self.owners.clear()
        self.pending.clear()
        for device in self.devices.values():
            device['connected'] = False
            # Nothing observed before a disconnect can be owed to anyone afterwards
            device['unclaimed'] = None


    def _wants_readings(self):
        """
        True while a reading would actually be consumed.

        Polling exists to feed automatic updates and in-flight captures. When no device
        is auto-updating and nothing is capturing, readings are only wanted for status,
        so we back right off rather than talking to Moonraker every second forever.
        """
        if self.busy or self.owners:
            return True
        return any(d['auto'] and d['enabled'] for d in self.devices.values())


    def _poll(self, eventtime):
        """
        Poll every device without blocking Klipper's reactor.

        One request services all scanners, and the interval backs right off when nothing
        is consuming readings - see _wants_readings().
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

        Replies can overtake each other, so anything older than the newest already seen
        is acknowledged but not allowed to overwrite device state.
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

        'timeout' lets a caller cap the round trip to the time it actually has - without
        it a slow bridge would silently overrun the caller's own deadline. The filament
        load path deliberately does not call this at all (see begin_load).
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


    def update_devices(self, data, error=None, error_kind=None):
        """
        Refresh the scanner cache and apply any attributable new measurement.

        A reading is only written to a gate when this manager can say which filament
        produced it - see the ownership token conditions below.
        """
        if not isinstance(data, dict):
            data, error, error_kind = {}, "Invalid TD-1 device list", TD1_ERR_BRIDGE
        for serial in sorted(set(self.devices) | set(data)):
            # Scanners Moonraker reports but nothing here claims are kept deliberately:
            # MMU_TD1 listing them with "Gates: none" is how you find a serial to
            # configure in the first place
            device = self.devices.setdefault(serial, self._new_device(serial))
            previous = device.get('scan_time')
            was_connected = device.get('connected', False)
            device['connected'] = serial in data and error is None
            device['error'] = error
            device['error_kind'] = error_kind
            if not device['connected']:
                device['last_outcome'] = 'disconnected'
                # Dropped rather than released: a disconnect invalidates the model of
                # what is in front of the scanner, so there is nothing left to owe
                self.owners.pop(serial, None)
                device['unclaimed'] = None
                continue
            record = data[serial]
            reported = record.get('error') if isinstance(record, dict) else None
            if not reported and not has_reading(record):
                # Normal before the first insertion - not a fault, just nothing to report.
                # A device that HAD a reading and now reports none has been power cycled
                # or rebooted, so drop the cached one with it: leaving it behind makes
                # status quote a measurement the scanner no longer stands behind, and
                # hands measurement() a record carrying both a value and an error
                device.update(td=None, color=None, scan_time=None)
                device['error'] = "TD-1 has not measured anything yet"
                device['error_kind'] = TD1_ERR_NO_READING
                device['last_outcome'] = 'no measurement yet'
                continue
            try:
                valid = measurement(record)
            except (ValueError, TypeError) as exc:
                device['error'] = str(exc)
                device['error_kind'] = TD1_ERR_DEVICE if reported else TD1_ERR_INVALID
                device['last_outcome'] = 'invalid measurement'
                continue
            device.update(valid)
            device['last_outcome'] = 'status_only: no attributable loaded gate'
            owner = self.owners.get(serial)
            if (owner is not None
                and device['enabled']
                and (device['auto'] or owner['capture'])
                and was_connected
                and (previous is None or valid['scan_time'] > previous)
                and not self.busy):
                gate = owner['gate']
                if not self.claimable(serial, gate):
                    # Owed to a gate that has already stopped waiting; this reading
                    # settles that debt and is attributed to nobody
                    device['last_outcome'] = ("status_only: settled an unclaimed reading from gate %d"
                                              % device['unclaimed'])
                    self.settle(serial)
                elif (self.revisions[gate] == owner['revision']
                      and self.mmu.gate_selected == gate
                      and self.mmu.filament_pos == FILAMENT_POS_LOADED):
                    self.apply(gate, valid)
                    owner['satisfied'] = True
                    self.settle(serial)
                    device['last_outcome'] = "applied to gate %d" % gate


# -----------------------------------------------------------------------------------------------------------
# GATE METADATA
# -----------------------------------------------------------------------------------------------------------

    def filament_changed(self, gate):
        """
        Drop attribution for a gate whose filament identity has changed.

        Handles 'mmu:gate_filament_changed'. The measurements themselves are gate-map
        fields and the gate map clears them; what goes here is Happy Hare's belief about
        which filament a scanner is looking at. Bumping the revision makes any capture
        already in flight discard its own result rather than apply it to a new spool.
        """
        self.revisions[gate] += 1
        self.release(gate=gate)


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
        maps.update_gate_color_rgb()
        maps.persist_gate_map(changed_gate=gate)


    def device(self, gate):
        """
        Resolve an enabled, connected and healthy scanner for a gate.

        A scanner that has simply not measured anything yet counts as healthy - that is
        the normal state before filament first reaches it.
        """
        serial = self.paths[gate]['serial']
        device = self.devices.get(serial)
        if device is None:
            raise MmuTd1Error("TD-1: no scanner configured for gate %d" % gate)
        if not device['enabled']:
            raise MmuTd1Error("TD-1: scanner %s is disabled" % serial)
        if not device['connected']:
            raise MmuTd1BridgeError("TD-1: scanner %s is disconnected" % serial)
        # A device that simply hasn't measured yet is healthy - that's the normal state
        # before filament first reaches it, and the whole point of scanning
        if device['error'] and device['error_kind'] != TD1_ERR_NO_READING:
            raise MmuTd1Error("TD-1: %s" % device['error'])
        return device


    def reading(self, gate):
        """
        The scanner's latest valid measurement for 'gate', as a normalized record.

        device() lets a scanner that has simply not measured yet through as healthy, so
        this is where "healthy but nothing to report" becomes a proper MmuError rather
        than the ValueError that measurement() raises.
        """
        device = self.device(gate)
        try:
            return measurement(device)
        except (ValueError, TypeError) as exc:
            raise MmuTd1NoReading("TD-1: %s" % exc) from exc


    def wait_measurement(self, gate, baseline, timeout):
        """
        Wait for a measurement newer than 'baseline', for at most 'timeout' seconds.

        Every round trip is capped to the time actually remaining, so the caller's
        timeout means what it says. A transport stall inside that window is just another
        reason we don't have a reading yet, and is reported as one - only a bridge that
        is genuinely unusable propagates, because no amount of waiting will fix it.
        """
        deadline = self.reactor.monotonic() + timeout
        serial = self.paths[gate]['serial']
        self.busy = True
        self.active_serial = serial
        try:
            while True:
                device = self.device(gate)
                stamp = device.get('scan_time')
                if stamp is not None and (baseline is None or stamp > baseline):
                    if self.claimable(serial, gate):
                        self.settle(serial)
                        return self.reading(gate)
                    # Owed to a gate that already gave up waiting. Take it off the
                    # scanner's books and keep waiting for one that is ours
                    self.mmu.log_debug(
                        "TD-1: discarded a reading still owed to gate %d" % device['unclaimed'])
                    self.settle(serial)
                    baseline = stamp
                    continue
                remaining = deadline - self.reactor.monotonic()
                if remaining <= 0:
                    # Filament crossed the scanner and we stopped waiting for what it
                    # measured, so whatever turns up next is not the next gate's
                    self.owe(serial, gate)
                    raise MmuTd1NoReading("TD-1: no fresh measurement for gate %d" % gate)
                try:
                    self.refresh(timeout=min(remaining, TD1_REQUEST_TIMEOUT))
                except MmuTd1BridgeTimeout:
                    continue
                if self.reactor.monotonic() < deadline:
                    self.reactor.pause(self.reactor.monotonic() + TD1_WAIT_GRANULARITY)
        finally:
            # Must not leak: a stuck 'busy' silences the passive path and pins polling
            # at its active interval for the rest of the session
            self.busy = False
            self.active_serial = ""


    def needs_measurement(self, gate):
        """
        True when this gate has a scanner but nothing measured to show for it.

        "Unmeasured" is the whole staleness rule, and it costs nothing to maintain:
        gate_td is cleared whenever a gate's filament identity changes - spool swap,
        RFID change, gate emptied, manual TD edit - so a gate that still holds a value
        holds one that describes the filament actually in it.
        """
        return bool(self.paths[gate]['serial']) and self.mmu.gate_td[gate] is None


    def baseline(self, gate):
        """
        The scanner's latest reading time, sampled before filament is moved past it.

        Raises if the scanner is unusable, letting a caller decide not to move at all
        rather than discovering the problem after a bowden's worth of travel.
        """
        return self.device(gate).get('scan_time')


    def capture(self, gate, baseline):
        """
        Wait for a reading newer than 'baseline' and apply it to the gate.

        Called once filament has traversed the scanner. Raises MmuTd1NoReading when
        nothing arrives in time; the filament is left exactly where the caller put it,
        so unloading and recovery remain the caller's business.
        """
        record = self.wait_measurement(gate, baseline, self.settings(gate).td1_capture_timeout)
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
        if gate < 0 or not self.paths[gate]['serial']:
            return None
        serial = self.paths[gate]['serial']
        device = self.devices[serial]
        capture = bool(self.settings(gate).td1_capture_on_load)
        if not device['enabled'] or not (device['auto'] or capture):
            return None
        # Whatever was armed before is finished with either way, and if it never
        # produced a reading the scanner still owes that gate one
        self.release(serial=serial)
        try:
            self.device(gate)
        except MmuError as exc:
            self.mmu.log_debug(str(exc))
            return None
        token = {'gate': gate, 'revision': self.revisions[gate],
                 'baseline': device.get('scan_time'), 'capture': capture,
                 'satisfied': False}
        self.owners[serial] = token
        return token


    def end_load(self, token, success):
        """
        Capture after loading while preserving explicit ownership.

        While printing we never wait: ownership stays armed and update_devices() applies
        whatever the poller delivers, so opting into capture costs no tool-change time.
        """
        if token is None:
            return
        gate = token['gate']
        serial = self.paths[gate]['serial']
        if (not success
            or self.revisions[gate] != token['revision']
            or self.mmu.gate_selected != gate
            or self.owners.get(serial) is not token):
            self.release(serial=serial)
            return
        if not token['capture'] or self.mmu.is_printing():
            return
        try:
            record = self.wait_measurement(gate, token['baseline'], self.settings(gate).td1_capture_timeout)
            if (self.revisions[gate] != token['revision']
                or self.mmu.gate_selected != gate
                or self.owners.get(serial) is not token):
                self.release(serial=serial)
                return
            self.apply(gate, record)
            token['satisfied'] = True
        except MmuError as exc:
            # A measurement failure never invalidates an otherwise successful load
            self.mmu.log_warning(str(exc))


# -----------------------------------------------------------------------------------------------------------
# STATUS
# -----------------------------------------------------------------------------------------------------------

    def get_status(self, eventtime=None):
        """
        Report Happy Hare's own policy toward each gate's scanner, indexed by gate.

        A flat per-gate list aggregated across units, like 'espooler' and 'drying_state'.
        NFC reports a per-unit dict instead because its shared reader serves the unit and
        the bypass rather than any gate, so it has no per-gate answer to give; every TD-1
        gate either has a scanner or doesn't, so the flat list says everything.

        Policy only. Connectivity, the latest reading and any device fault come from
        Moonraker's own [td1] endpoint, and the gate/serial assignment is already in
        printer.mmu_machine - none of that is repeated here.
        """
        states = []
        for gate, path in enumerate(self.paths):
            device = self.devices.get(path['serial']) if path['serial'] else None
            if device is None:
                states.append(TD1_STATE_NONE)
            elif not device['enabled']:
                states.append(TD1_STATE_DISABLED)
            elif device['auto']:
                states.append(TD1_STATE_AUTO)
            else:
                states.append(TD1_STATE_ENABLED)
        return {'td1': states}


    def gates_for(self, serial):
        """
        Global gates served by one physical scanner.

        A serial may be named by several gates, and by several units, so this is the
        only way to answer "what does disabling this device affect?".
        """
        return [gate for gate, path in enumerate(self.paths) if path['serial'] == serial]
