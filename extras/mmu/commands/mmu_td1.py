# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Command class to inspect TD-1 scanners and capture filament TD / measured color
#
# Implements commands:
#   MMU_TD1
#
# The scanners are USB devices owned by Moonraker's [td1] component. This command resolves
# the gate (or device) being addressed and hands the work to the machine-level MmuTd1
# coordinator, which owns attribution, scan geometry and filament movement.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

from datetime import datetime, timezone

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from ..mmu_td1         import MmuTd1BridgeError, TD1_ERR_NO_READING
from .mmu_base_command import *


class MmuTd1Command(BaseCommand):

    CMD = "MMU_TD1"

    HELP_BRIEF = "Inspect TD-1 scanners or capture filament TD and measured color"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "SHARED   = [0|1] Target the unit's off-path scanner\n"
        + "GATE     = #(int) Target the scanner serving this gate\n"
        + "GATES    = g,g,g Target several gates' scanners\n"
        + "UNIT     = #(int)/name Disambiguate when several units qualify\n"
        + "SERIAL   = # Target one scanner by USB serial\n"
        + "ENABLE   = [0|1] Turn Happy Hare's use of the scanner on/off\n"
        + "AUTO     = [0|1] Override the unit's td1_auto_update until restart\n"
        + "READ     = [0|1] Poll Moonraker now instead of using the cache\n"
        + "REGISTER = [0|1] Apply the scanner's measurement to GATE\n"
        + "SET_COLOR = [0|1] Overwrite filament_color with the measured color\n"
        + "INIT     = [0|1] Reboot the addressed scanner and await recovery\n"
        + "INIT_ALL = [0|1] Reboot every scanner on every unit\n"
        + "CLEAR_PENDING = [0|1] Discard a staged measurement\n"
        + "DETAILS  = [0|1] Add scan time and attribution to the report\n"
        + "QUIET    = [0|1] Don't report non-essential status\n"
        + "(no parameters for status report of all scanners)"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                        ...Report status of all scanners\n"
        + f"{CMD} DETAILS=1              ...As above plus scan time and attribution\n"
        + f"{CMD} SHARED=1 ENABLE=0      ...Disable the off-path scanner\n"
        + f"{CMD} CLEAR_PENDING=1        ...Discard a staged measurement, keep other pending data\n"
        + f"{CMD} GATE=3 READ=1          ...Poll the scanner serving gate 3 and report the result\n"
        + f"{CMD} GATE=2 REGISTER=1      ...Apply a measurement to gate 2 (as if auto-scanned)\n"
        + f"{CMD} GATES=0,1 ENABLE=0     ...Disable selected per-gate scanners\n"
        + f"{CMD} GATES=0,1 SET_COLOR=1  ...Use the measured color as those gates' filament color\n"
        + f"{CMD} UNIT=0 AUTO=1          ...Auto-apply readings on unit 0's scanners\n"
        + f"{CMD} GATE=2 INIT=1          ...Reboot the scanner on gate 2\n"
        + f"{CMD} INIT_ALL=1             ...Reboot every scanner on all units\n"
    )

    # Operation flags, in the order they are reported when the user asks for too many
    OPERATIONS = ('REGISTER', 'SET_COLOR', 'INIT')

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
        )


    def _resolve_gates(self, gcmd, default=False):
        """
        Resolve GATE/GATES to unique gates, in the order requested.

        Gates only - a scanner belongs to a physical filament path, so no TOOL/TOOLS
        (use MMU_CHECK_GATE TD1=1) and no bypass (scanner assignment is gate-scoped).
        Contradictory selectors are rejected rather than resolved by precedence, since
        every operation here writes gate metadata.
        """
        mmu = self.mmu
        selectors = [key for key in ('GATE', 'GATES') if gcmd.get(key, None) is not None]
        if len(selectors) > 1:
            raise gcmd.error("Specify only one of GATE or GATES")

        if not selectors:
            if not default:
                return []
            values = [mmu.gate_selected]
        else:
            key = selectors[0]
            try:
                values = [int(v) for v in gcmd.get(key).split(',')]
            except ValueError:
                raise gcmd.error("Invalid %s parameter: %s" % (key, gcmd.get(key)))
            if key == 'GATE' and len(values) != 1:
                raise gcmd.error("Use GATES for a list of more than one")
            bad = [v for v in values if not (0 <= v < mmu.num_gates)]
            if bad:
                raise gcmd.error("Invalid gate(s) in %s: %s" % (key, ",".join(map(str, bad))))

        return list(dict.fromkeys(values)) # Deduplicate, preserving order


    def _resolve_operation(self, gcmd):
        """
        Resolve exactly one operation, or none for a plain status report.

        Operations are mutually exclusive; READ/DETAILS/QUIET are modifiers.
        """
        flags = {key: gcmd.get_int(key, 0, minval=0, maxval=1) for key in self.OPERATIONS}
        operations = [key for key in self.OPERATIONS if flags[key]]
        operations += [key for key in ('ENABLE', 'AUTO') if gcmd.get(key, None) is not None]
        if len(operations) > 1:
            raise gcmd.error("Specify only one operation at a time (%s)" % ", ".join(operations))
        return operations[0] if operations else ""


    def _reader_line(self, label, manager, device, gate, capturing, details):
        """
        One reader, one line, in MMU_NFC's shape.

        A gate row quotes that gate's recorded measurement; an off-path row has no
        gate, so it quotes the device's own latest. last_measured is always the
        device's - the gate map keeps no timestamp - so a row can read td=None with a
        time beside it. The serial is omitted where the label already is the serial,
        and DETAILS only ever appends to a row, never reorders it.
        """
        if gate is not None:
            td, color = self.mmu.gate_td[gate], self.mmu.gate_td1_color[gate]
        else:
            td, color = device.td, device.color
        mode = "auto" if manager is not None and manager.auto_for(device) else "manual"
        if device.auto_override is not None:
            mode += "(override)"
        line = "%-9s enabled=%d, connected=%d, mode=%s, td=%s, td-color=%s" % (
            label + ":", int(device.enabled), int(device.connected), mode,
            "%.2f" % td if td is not None else "None", color or "None")
        if label != device.serial:
            line += ", serial=%s" % device.serial
        if details:
            line += ", last_measured=%s" % self._measured_at(device)
        return line + (" (capturing)" if capturing else "")


    @staticmethod
    def _measured_at(device):
        """The device's scan time as a local wall clock, or Never."""
        if not device.scan_time:
            return "Never"
        try:
            return datetime.fromisoformat(device.scan_time).astimezone().strftime("%H:%M:%S")
        except ValueError:
            return device.scan_time


    def _device_report(self, bridge, serials, details):
        """
        Build the console status block, grouped by unit exactly as MMU_NFC's is.

        A scanner shared by several gates appears on each of their rows. This is the
        reporting surface for TD-1 - little is published in printer.mmu, since the
        live data is Moonraker's and the assignment is in printer.mmu_machine.
        """
        wanted = list(dict.fromkeys(serials))
        managers = bridge.managers()
        multi = len(managers) > 1
        lines, shown, notes = [], set(), {}

        def row(label, manager, device, gate):
            shown.add(device.serial)
            # Keyed by serial so a scanner serving four gates notes its fault once.
            # "Not measured yet" is normal, and last_measured already says it
            if device.error and device.error_kind != TD1_ERR_NO_READING:
                notes[device.serial] = device.error
            return self._reader_line(label, manager, device, gate,
                                     bridge.active_serial == device.serial, details)

        for manager in managers:
            unit_lines = []
            shared = manager.shared_device
            if shared is not None and shared.serial in wanted:
                unit_lines.append(row("shared", manager, shared, None))
            for local, device in enumerate(manager.gate_devices):
                if device is not None and device.serial in wanted:
                    gate = manager.mmu_unit.first_gate + local
                    unit_lines.append(row("gate %d" % gate, manager, device, gate))
            if unit_lines:
                if multi:
                    lines.append("Unit %s:" % manager.mmu_unit.name)
                lines.extend(unit_lines)

        # Scanners Moonraker reports that nothing references. Listing them is how you
        # find a serial to configure
        spare = [s for s in wanted if s not in shown and s in bridge.devices]
        if spare:
            lines.append("Not assigned to any gate:")
            for serial in spare:
                # Labelled by serial: the only name such a scanner has
                lines.append(row(serial, None, bridge.devices[serial], None))

        lines.extend("Note %s: %s" % (serial, error) for serial, error in notes.items())
        if details:
            for serial in wanted:
                device = bridge.devices.get(serial)
                if device is None:
                    continue
                detail = "Detail %s: last outcome %s" % (serial, device.last_outcome or "none")
                if device.owner is not None:
                    detail += ", attributable to gate %d" % device.owner['gate']
                if device.scan_time:
                    detail += ", scanned at %s" % device.scan_time
                lines.append(detail)
        staged = self.mmu.pending_measurement
        if staged is not None:
            lines.append("Staged for the next gate loaded: td=%.2f, td-color=%s at %s"
                         % (staged['td'], staged['color'], staged['scan_time']))
        if not lines:
            return "No TD-1 scanners configured"
        return "MMU TD-1 readers:\n" + "\n".join(lines)


    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        if self.check_if_disabled(): return

        # Independent of scanner addressing - only the off-path scanner ever stages, and
        # the pending is machine-level - so handle it and fall through
        if gcmd.get_int('CLEAR_PENDING', 0, minval=0, maxval=1):
            if mmu.clear_pending_measurement():
                mmu.log_always("TD-1: discarded the staged measurement")
            else:
                mmu.log_always("TD-1: no staged measurement to discard")

        # INIT_ALL: reboot everything, like MMU_NFC INIT_ALL re-initializes every reader
        if gcmd.get_int('INIT_ALL', 0, minval=0, maxval=1):
            try:
                for serial in sorted(mmu.td1.devices):
                    mmu.td1.devices[serial].owner = None
                    mmu.td1.refresh(serial, reset=True)
            except MmuError as ee:
                mmu.handle_mmu_error(str(ee), recover=False)
                return
            mmu.log_always("TD-1: rebooted all scanners on all units")
            return

        operation = self._resolve_operation(gcmd)
        serial = gcmd.get('SERIAL', "")
        shared = bool(gcmd.get_int('SHARED', 0, minval=0, maxval=1))
        gates = self._resolve_gates(gcmd, default=False)

        if sum([shared, bool(gates), bool(serial)]) > 1:
            raise gcmd.error(
                "Specify only one of SHARED=1, GATE=<n>, GATES=<n,n,...> or SERIAL=<serial>")
        if operation == 'AUTO' and not gates and not shared:
            # An override of a unit's policy has to say whose. GATE/GATES/SHARED already
            # do; a bare SERIAL= does not. Only bites on a multi-unit machine. ENABLE is
            # about the device, not a unit's policy, so it needs none of this
            self.get_unit(gcmd, mode="required")

        if operation == 'SET_COLOR' and not gates:
            raise gcmd.error("SET_COLOR=1 needs GATE=<n> or GATES=<n,n,...>")
        if operation == 'REGISTER' and gcmd.get('GATE', None) is None:
            raise gcmd.error("REGISTER=1 requires exactly one explicit GATE - "
                             "you are asserting which filament produced the reading")

        if shared:
            # Belongs to a unit, not a gate - addressed as MMU_NFC does its shared reader
            unit = self.get_unit(gcmd, mode="required")
            device = unit.td1_manager.shared_device
            if device is None:
                raise gcmd.error("No off-path TD-1 scanner ('td1_device') on unit %s" % unit.name)
            serial = device.serial
        elif gcmd.get('UNIT', None) is not None:
            unit = self.get_unit(gcmd, mode="required")
            if any(mmu.mmu_unit(g) is not unit for g in gates):
                raise gcmd.error("UNIT conflicts with the requested gate selection")

        try:
            self._do_device_action(gcmd, gates, serial, operation)
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee), recover=False)


    def _do_device_action(self, gcmd, gates, serial, operation):
        """
        Everything this command does: status, REGISTER, ENABLE, AUTO and INIT.

        A plain status report reads the poller's cache, so it still works when
        Moonraker's [td1] component is missing. READ=1 polls first, as REGISTER always does.
        """
        mmu = self.mmu
        manager = mmu.td1

        if gcmd.get_int('READ', 0, minval=0, maxval=1) or operation == 'REGISTER':
            manager.refresh()

        if operation == 'SET_COLOR':
            maps = mmu.gate_maps
            maps.renew_gate_map()
            changed, skipped = [], []
            for gate in gates:
                if mmu.mmu_unit(gate).td1_manager.adopt_color(gate, force=True):
                    changed.append(gate)
                elif not maps.gate_td1_color[gate]:
                    skipped.append(gate)
            if changed:
                maps.update_gate_color_rgb()
                maps.persist_gate_map(spoolman_sync=False) # Local only; Spoolman has no TD-1 color
                mmu.log_always("TD-1: filament color set from the measured color on gate(s) %s"
                               % ",".join(str(g) for g in changed))
                spooled = [g for g in changed if mmu.gate_spool_id[g] > 0]
                if spooled:
                    mmu.log_warning(
                        "Gate(s) %s have a Spoolman spool - the next Spoolman refresh will "
                        "put its color back" % ",".join(str(g) for g in spooled))
            if skipped:
                mmu.log_info("No measured color on gate(s) %s"
                             % ",".join(str(g) for g in skipped))
            if not changed and not skipped:
                mmu.log_info("Filament color already matches the measured color")
            return

        if serial:
            targets = [serial]
        else:
            serials = {g: mmu.mmu_unit(g).td1_manager.addressed_serial(g) for g in gates}
            targets = [s for s in dict.fromkeys(serials[g] for g in gates) if s]
            unassigned = [g for g in gates if not serials[g]]
            if unassigned:
                message = "No TD-1 scanner configured for gate(s) %s" % (
                    ",".join(str(g) for g in unassigned))
                if operation:
                    raise gcmd.error(message)
                mmu.log_info(message) # A bare status query just says so and carries on
        if not targets:
            if operation:
                raise gcmd.error("Select a gate or device for %s" % operation)
            targets = list(manager.devices)

        for device_id in targets:
            if device_id not in manager.devices:
                raise gcmd.error("Unknown TD-1 device '%s'" % device_id)
            device = manager.devices[device_id]

            if operation in ('ENABLE', 'AUTO'):
                value = bool(gcmd.get_int(operation, minval=0, maxval=1))
                manager.set_device_state(
                    device_id, **{'enabled' if operation == 'ENABLE' else 'auto': value})
                affected = manager.gates_for(device_id)
                mmu.log_always("TD-1 %s: %s=%d affects gate(s) %s until restart" % (
                    device_id, operation, int(value),
                    ",".join(str(g) for g in affected) or "none"))

            elif operation == 'INIT':
                device.owner = None
                manager.refresh(device_id, reset=True)
                mmu.log_always("TD-1 %s: rebooted and rediscovered" % device_id)

            elif operation == 'REGISTER':
                gate = gates[0]
                gate_manager = mmu.mmu_unit(gate).td1_manager
                # From the record actually applied, which may be a staged pending one
                record = gate_manager.register_reading(gate)
                gate_manager.apply(gate, record)
                scanned = datetime.fromisoformat(record['scan_time'])
                age = max(0., (datetime.now(timezone.utc) - scanned).total_seconds())
                mmu.log_always("TD-1: applied cached measurement to gate %d from %s (%.0fs old)"
                               % (gate, record['scan_time'], age))
                # Establishing identity clears measurements, so this would be lost
                if mmu.gate_spool_id[gate] <= 0 and not mmu.gate_spool_rfid[gate]:
                    mmu.log_warning(
                        "Gate %d has no spool or tag assigned yet - assigning one will clear "
                        "this measurement. Assign it first, or register again afterwards" % gate)

        if not gcmd.get_int('QUIET', 0, minval=0, maxval=1):
            details = bool(gcmd.get_int('DETAILS', 0, minval=0, maxval=1))
            mmu.log_always(self._device_report(manager, targets, details))
