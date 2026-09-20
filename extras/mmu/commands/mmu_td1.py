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
from ..mmu_td1         import MmuTd1BridgeError
from .mmu_base_command import *


class MmuTd1Command(BaseCommand):

    CMD = "MMU_TD1"

    HELP_BRIEF = "Inspect TD-1 scanners or capture filament TD and measured color"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE         = g (the scanner serving this gate)\n"
        + "GATES        = comma,separated,gates (for ENABLE/AUTO across several)\n"
        + "SERIAL       = # Address one physical scanner by USB serial\n"
        + "UNIT         = Restrict/validate the gate selection to one unit\n"

        + "REGISTER     = [0|1] Apply the scanner's cached measurement to GATE\n"
        + "ENABLE       = [0|1] Enable/disable Happy Hare's use of the device\n"
        + "AUTO         = [0|1] Enable/disable passive metadata updates\n"
        + "INIT         = [0|1] Reboot the device through Moonraker and await recovery\n"

        + "REFRESH      = [0|1] Poll Moonraker before reporting, instead of using the cache\n"
        + "DETAILS      = [0|1] Include attribution and per-gate measurements\n"
        + "QUIET        = [0|1] Don't report non-essential status\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                           ...Status of every known scanner\n"
        + f"{CMD} GATE=2                    ...Just the scanner serving gate 2\n"
        + f"{CMD} GATE=2 REGISTER=1         ...Apply its cached measurement to gate 2\n"
        + f"{CMD} GATES=0,1 ENABLE=0        ...Stop using those gates' scanners\n"
        + f"{CMD} SERIAL=ABC123 INIT=1      ...Reboot a scanner that came up with an error\n"
        + "\n"
        + "This command never moves filament. To measure gates, use MMU_CHECK_GATE TD1=1,\n"
        + "which runs filament down its normal path past the scanner (TD1_UPDATE=1 to\n"
        + "re-read gates that already have a measurement).\n"
        + "REGISTER is for a scanner filament does not pass through - present filament to\n"
        + "it by hand, then attribute the reading to a gate. Do that LAST, after the gate's\n"
        + "spool/tag is assigned, since assigning one clears the gate's measurement.\n"
        + "Measured color is kept separate from filament_color - select 'td1_color' in the\n"
        + "LED effect options to display it. Unmeasured gates fall back to filament_color."
    )

    # Operation flags, in the order they are reported when the user asks for too many
    OPERATIONS = ('REGISTER', 'INIT')

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

        Gates only - deliberately no TOOL/TOOLS, and no bypass. A scanner belongs to a
        physical filament path, and remapping a tool doesn't move it, so a tool selector
        here would only add a layer of indirection to reason about. Tool-driven work goes
        through MMU_CHECK_GATE TD1=1, which already owns tool semantics and EndlessSpool
        remapping. The bypass is excluded because scanner assignment is gate-scoped
        (td1_device / td1_devices) and has no bypass equivalent - bypass filament identity
        lives in 'active_filament' rather than the gate map.

        Contradictory selectors are rejected rather than resolved by precedence (which is
        what MMU_CHECK_GATE does): every operation here either moves filament or writes
        gate metadata, so silently picking one of two selectors the user typed is the
        wrong kind of helpful.
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

        Operations are mutually exclusive, so a device reboot can never be bundled with
        anything else. REFRESH/DETAILS/QUIET are modifiers and combine freely.
        """
        flags = {key: gcmd.get_int(key, 0, minval=0, maxval=1) for key in self.OPERATIONS}
        operations = [key for key in self.OPERATIONS if flags[key]]
        operations += [key for key in ('ENABLE', 'AUTO') if gcmd.get(key, None) is not None]
        if len(operations) > 1:
            raise gcmd.error("Specify only one operation at a time (%s)" % ", ".join(operations))
        return operations[0] if operations else ""


    def _device_report(self, manager, serials, details):
        """
        Build the console status block, as one message in the order requested.

        This is the reporting surface for TD-1: little of it is published in printer.mmu,
        because the live device data is Moonraker's own and the gate assignment is already
        in printer.mmu_machine.
        """
        lines = []
        for serial in serials:
            device = manager.devices[serial]
            owner = manager.owners.get(serial)
            device_gates = manager.gates_for(serial)
            gates = ",".join(str(g) for g in device_gates) or "none"
            state = "connected" if device['connected'] else "DISCONNECTED"
            if not device['enabled']:
                state += ", disabled"
            if device['auto']:
                state += ", auto-update"
            lines.append("TD-1 %s: %s" % (serial, state))
            lines.append("  Gates: %s%s" % (
                gates, " (capturing)" if manager.active_serial == serial else ""))
            if device['td'] is not None:
                lines.append("  Latest: TD %.2f, color %s at %s" % (
                    device['td'], device['color'], device['scan_time']))
            else:
                lines.append("  Latest: no measurement yet")
            if device['error']:
                lines.append("  Note: %s" % device['error'])
            if details:
                if owner is not None:
                    lines.append("  Attributable gate: %d" % owner['gate'])
                lines.append("  Last outcome: %s" % (device['last_outcome'] or "none"))
                for gate in device_gates:
                    lines.append("  Gate %d: TD %s, measured color %s" % (
                        gate,
                        self.mmu.gate_td[gate] if self.mmu.gate_td[gate] is not None else "none",
                        self.mmu.gate_td1_color[gate] or "none"))
        return "\n".join(lines) if lines else "TD-1: no scanners known"


    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        if self.check_if_disabled(): return

        operation = self._resolve_operation(gcmd)
        serial = gcmd.get('SERIAL', "")
        gates = self._resolve_gates(gcmd, default=False)
        if gates and serial:
            raise gcmd.error("Specify gate selection or SERIAL=<serial>, not both")
        if operation == 'REGISTER' and gcmd.get('GATE', None) is None:
            raise gcmd.error("REGISTER=1 requires exactly one explicit GATE - "
                             "you are asserting which filament produced the cached reading")

        if gcmd.get('UNIT', None) is not None:
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

        A plain status report reads the poller's cache, so it still works - and is still
        worth running - when Moonraker's [td1] component is missing. REFRESH=1 asks for a
        live poll first, which REGISTER does anyway since it is about to commit a value.
        """
        mmu = self.mmu
        manager = mmu.td1

        if gcmd.get_int('REFRESH', 0, minval=0, maxval=1) or operation == 'REGISTER':
            manager.refresh()

        if serial:
            targets = [serial]
        else:
            targets = [s for s in dict.fromkeys(manager.paths[g]['serial'] for g in gates) if s]
            unassigned = [g for g in gates if not manager.paths[g]['serial']]
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
                manager.owners.pop(device_id, None)
                manager.refresh(device_id, reset=True)
                mmu.log_always("TD-1 %s: rebooted and rediscovered" % device_id)

            elif operation == 'REGISTER':
                gate = gates[0]
                manager.apply(gate, manager.reading(gate))
                scanned = datetime.fromisoformat(device['scan_time'])
                age = max(0., (datetime.now(timezone.utc) - scanned).total_seconds())
                mmu.log_always("TD-1: applied cached measurement to gate %d from %s (%.0fs old)"
                               % (gate, device['scan_time'], age))
                # Establishing the gate's filament identity clears its measurement, so
                # registering before a spool or tag is assigned silently loses this
                if mmu.gate_spool_id[gate] <= 0 and not mmu.gate_spool_rfid[gate]:
                    mmu.log_warning(
                        "Gate %d has no spool or tag assigned yet - assigning one will clear "
                        "this measurement. Assign it first, or register again afterwards" % gate)

        if not gcmd.get_int('QUIET', 0, minval=0, maxval=1):
            details = bool(gcmd.get_int('DETAILS', 0, minval=0, maxval=1))
            mmu.log_always(self._device_report(manager, targets, details))
