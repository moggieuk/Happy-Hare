# -*- coding: utf-8 -*-
# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Command class to implement MMU heater control and basic filament drying functionality
#
# Implements commands:
#   MMU_HEATER
#
# The environment manager will support automatic spool rotation if equiped with eSpooler and the
# dry cycle is initiated with this option. IMPORTANT: filament must be removed from the MMU inlet and
# fastened to the spool. Also, the GATES parameter must be supplied to this command.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuHeaterCommand(BaseCommand):

    CMD = "MMU_HEATER"

    HELP_BRIEF = "Control MMU heater(s) and filament drying cycle"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT            = #(int) Optional if only one unit fitted to printer\n"
        + "STOP            = [0|1] Turn off heater and drying cycle\n"
        + "DRYING_DATA     = [0|1] Dump configured drying data for filament types\n"
        + "DRY             = [0|1] Disable/enable filament heater for filament drying cycle\n"
        + "TIMER           = #(mins) Force drying time\n"
        + "TEMP            = #(degrees) Force temperature\n"
        + "HUMIDITY        = % Terminate drying when humidty goal is reached\n"
        + "GATES           = g1,g2 Gates to control ONLY IF MMU has per-gate heaters/dryers\n"
        + "ROTATE          = [0|1] Rotate spool (requires eSpooler and explicit GATES)\n"
        + "ROTATE_INTERVAL = #(mins) How often to rotate spools when drying (requires eSpooler)\n"
        + "VENT_INTERVAL   = #(mins) How often to call 'vent' macro in drying cycle\n"
        + "(no parameters for status report)"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} TEMP=50                             ...Set heater temperature or adjusts if in drying cycle\n"
        + f"{CMD} DRY=1                               ...Dry with intelligent temperature/time recommended from 'drying_data'\n"
        + f"{CMD} DRY=1 TEMP=50 TIMER=240 HUMIDITY=12 ...Initiate drying cycle at 50{UI_DEGREE}C for 240 minutes with 12% humidity goal\n"
        + f"{CMD} STOP=1                              ...Stop current drying cycle\n"
        + f"{CMD} DRY=1 ROTATE=1 GATES=1,3            ...Start drying cycle on gates 1 & 3 periodically rotating them (requires espooler)\n"
        + f"{CMD} DRYING_DATA=1                       ...List the current drying data database\n"
        + f"{CMD} DRY=1 VENT_INTERVAL=10              ...Initiate drying cycle calling vent macro every 10 minutes\n"
        + f"With per-gate heaters:\n"
        + f"{CMD} DRY=1 GATES=0,2,3                   ...Drying cycle on gates 0,2 & 3 (subject to max simultaneous heaters)\n"
        + f"{CMD} TEMP=45 GATES=0,1                   ...Turn heaters on for gates 0 & 1\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            per_unit=True,
        )

    def _format_minutes(self, minutes):
        hours, mins = divmod(int(minutes), 60)
        parts = []
        if hours:
            parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
        if mins:
            parts.append("%d minute%s" % (mins, "" if mins == 1 else "s"))
        if not (hours or mins):
            parts.append("<1 minute")
        return " ".join(parts)

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.

        if self.check_if_disabled(): return

        em = mmu_unit.environment_manager

        if not em.has_heater():
            raise gcmd.error("No MMU heater configured on %s" % mmu_unit.name)

        drying_data = gcmd.get_int('DRYING_DATA', 0, minval=0, maxval=1)
        stop = gcmd.get_int('STOP', None, minval=0, maxval=1)
        dry = gcmd.get_int('DRY', None, minval=0, maxval=1)
        timer = gcmd.get_float('TIMER', None, minval=0.)
        temp = gcmd.get_float('TEMP', None, minval=0., maxval=mmu_unit.p.heater_max_temp)
        humidity = gcmd.get_float('HUMIDITY', mmu_unit.p.heater_default_dry_humidity, minval=0.)
        vent_interval = gcmd.get_float('VENT_INTERVAL', mmu_unit.p.heater_vent_interval, minval=0.)
        rotate = gcmd.get_int('ROTATE', 0, minval=0, maxval=1)
        rotate_interval = gcmd.get_float('ROTATE_INTERVAL', mmu_unit.p.heater_rotate_interval, minval=1.)

        # GATE is a common user mistake so interpret as GATES of one element
        gate = gcmd.get_int('GATE', None, minval=0, maxval=self.mmu.num_gates - 1)
        if gate is not None:
            gates_str = str(gate)
        else:
            gates_str = gcmd.get('GATES', "!")

        gates = []
        if gates_str != "!":
            # Supplied list of gates
            gates_param = True
            try:
                for gate in gates_str.split(','):
                    gate = int(gate)
                    if not 0 <= gate < self.mmu.num_gates:
                        raise gcmd.error("Invalid gate: %d" % gate)
                    gates.append(gate)
                self.validate_gates(mmu_unit, gates)
            except ValueError:
                raise gcmd.error("Invalid GATES parameter: %s" % gates_str)
            except MmuError as e:
                raise gcmd.error(str(e))
        else:
            gates_param = False

        # Display drying data table ---------------------------------------------
        if drying_data:
            msg = "Drying data:\n"
            for material in sorted(self.mmu.p.drying_data.keys()):
                t, minutes = self.mmu.p.drying_data[material]
                # Avoid format() on unicode with alignment in Py2 edge-cases; keep it simple
                msg += "%s %s°C for %s\n" % (material + ":", int(t), self._format_minutes(minutes))
            self.mmu.log_always(msg)
            return

        # Cancel drying cycle / Heater off --------------------------------------
        if stop or temp == 0:
            try:
                msg = em.stop_or_cancel(gates if gates_param else None)
            except MmuError as e:
                raise gcmd.error(str(e))
            self.mmu.log_info(msg)
            return

        # Raw heater control ----------------------------------------------------
        if not dry and temp is not None:
            if em.has_per_gate_heaters():
                if not gates_param:
                    gates = self.get_default_gates(mmu_unit, empty=False) # Default to all non empty gates

                if not gates:
                    self.mmu.log_always("No gates selected for raw heater control")
                    return

                try:
                    em.set_heater_target(temp, gates)
                except MmuError as e:
                    self.mmu.log_error(str(e))
                return

            # Single heater mode
            try:
                em.set_heater_target(temp)
            except MmuError as e:
                self.mmu.log_error(str(e))
            return

        # Initiate drying cycle -------------------------------------------------
        if dry:
            if not em.has_env_sensor():
                self.mmu.log_warning("MMU environment sensor not found. Check 'environment_sensor' configuration")
                return

            if em.is_drying():
                self.mmu.log_always("MMU already in filament drying cycle. Stop current cycle first")
                return

            # Optional spool rotation (requires eSpooler and explicit gates)
            # (BTT ViViD is allowed if not in print)
            if rotate and not (mmu_unit.has_espooler() or mmu_unit.mmu_vendor == VENDOR_VVD):
                self.mmu.log_warning("Rotation requested but eSpooler not fitted - ignoring")
                rotate = 0

            if rotate and not gates_param:
                raise gcmd.error("ROTATE requires explicit GATES parameter")

            if not rotate and not gates_param:
                gates = self.get_default_gates(mmu_unit, empty=False) # Default to all non empty gates

            if rotate:
                for gate in gates:
                    if self.mmu.gate_status[gate] != GATE_EMPTY:
                        self.mmu.log_warning("Gate %d is not empty so cannot rotate (filament end must be removed from the gate and secured to the spool for rotation)" % gate)

            try:
                warnings, info = em.start_drying_cycle(
                    gates,
                    timer=timer,
                    temp=temp,
                    humidity=humidity,
                    vent_interval=vent_interval,
                    rotate=rotate,
                    rotate_interval=rotate_interval,
                )
            except MmuError as e:
                raise gcmd.error(str(e))

            for line in warnings:
                self.mmu.log_warning(line)
            for line in info:
                self.mmu.log_info(line)

            msg = "MMU filament drying cycle started:"

        elif em.is_drying():
            msg = "MMU is in filament drying cycle:"

        else: # Not in drying cycle, but let's check heaters
            hs = em.get_heater_snapshot()

            if hs.get('per_gate'):
                # Per-gate heaters
                heaters_on = hs.get('heaters_on', [])
                heaters_off = hs.get('heaters_off', [])

                if heaters_on:
                    msg = "Not in drying cycle but one or more gate heaters are on:"
                    for h in heaters_on:
                        msg += "\nGate %d: Target temperature %.1f°C (current: %.1f°C)" % (
                            h.get('gate'), h.get('target'), h.get('temperature')
                        )
                    if heaters_off:
                        msg += "\nGate heaters off: %s" % ",".join([str(g) for g in heaters_off])
                else:
                    msg = "Not in drying cycle and all gate heaters are off"

            else:
                # Single shared heater
                cur_temp = hs.get('temperature')
                cur_target = hs.get('target')
                if cur_target is None:
                    msg = "Heater is not found / misconfigured"
                elif cur_target != 0:
                    msg = "Not in drying cycle but heater is on. Target temperature: %.1f°C (current: %.1f°C)" % (cur_target, cur_temp)
                else:
                    msg = "Not in drying cycle and heater is off"

        # Display status report of drying cycle ---------------------------------
        if em.is_drying():
            ds = em.get_drying_snapshot()

            if ds.get('gates'):
                msg += "\nDrying filaments in gates: %s" % ",".join(str(g) for g in ds.get('gates'))

            if not ds.get('per_gate'):
                # Single heater status report
                remaining_mins = self._format_minutes(ds.get('remaining', 0) // 60)
                cur_temp = ds.get('environment_temp')
                cur_humidity = ds.get('environment_humidity')

                msg += "\nCycle time: %s (remaining: %s)" % (
                    self._format_minutes(ds.get('time', 0)),
                    remaining_mins,
                )
                if cur_temp is not None:
                    msg += "\nTarget humidity: %.1f%%" % ds.get('humidity_target')
                    if cur_humidity is not None:
                        msg += " (current: %.1f%%)" % cur_humidity
                else:
                    msg += "\nEnvironment sensor not available / misconfigured"
                    cur_temp = ds.get('heater_temp', -1)
                    if cur_temp is None: cur_temp = -1 # Saftey, should not be possible to get here

                msg += "\nDrying temp: %.1f°C (current: %.1f°C)" % (
                    ds.get('temp'),
                    cur_temp,
                )

            else:
                # Per-gate status report
                msg += "\nPer-gate dryer mode (max concurrent heaters: %d). Humidty target %.1f%%" % (
                    mmu_unit.max_concurrent_heaters,
                    ds.get('humidity_target'),
                )
                for gd in ds.get('gate_drying', []):
                    gate = gd.get('gate')
                    state = gd.get('state', DRYING_STATE_NONE)
                    material = gd.get('material', None)
                    t = gd.get('temp', None)
                    last_t = gd.get('last_temp', None)
                    last_h = gd.get('last_humidity', None)
                    remaining = gd.get('remaining', None)
                    rem_txt = self._format_minutes(remaining // 60) if remaining is not None else None

                    line = "\nGate %d: " % gate
                    if state == DRYING_STATE_ACTIVE:
                        if last_t is not None:
                            line += "Drying %s %.1f°C (target %.1f°C)" % (material, last_t, t)
                        if last_h is not None:
                            line += ", humidity %.1f%%" % last_h
                        if rem_txt is not None:
                            line += ", %s remaining" % rem_txt

                    elif state == DRYING_STATE_QUEUED:
                        line += "(queued waiting for heater slot, target %.1f°C)" % t

                    elif state in [DRYING_STATE_COMPLETE, DRYING_STATE_CANCELLED]:
                        reason = gd.get('done_reason', 'complete' if state == DRYING_STATE_COMPLETE else 'cancelled')
                        line += "(%s" % reason
                        if last_h is not None:
                            line += ", final humidity: %.1f%%" % last_h
                        line += ")"

                    msg += line

            # Venting status
            if ds.get('vent_timer') is not None:
                msg += "\nVenting operational (running macro %s every %s, next in %s)" % (
                    mmu_unit.p.heater_vent_macro,
                    self._format_minutes(ds.get('vent_interval')),
                    self._format_minutes(max(ENV_CHECK_INTERVAL, ds.get('vent_timer')) / 60),
                )
            else:
                if not mmu_unit.p.heater_vent_macro:
                    vent_reason = "heater_vent_macro not set"
                else:
                    vent_reason = "heater_vent_interval is 0"
                msg += "\nVenting not operational (%s)" % vent_reason

            # Rotation status (eSpooler)
            if ds.get('rotate_enabled'):
                msg += "\nSpool rotation enabled (running every %s, next in %s)" % (
                    self._format_minutes(ds.get('rotate_interval')),
                    self._format_minutes(max(ENV_CHECK_INTERVAL, ds.get('rotate_timer')) / 60),
                )
            elif mmu_unit.has_espooler():
                msg += "\nSpool rotation not enabled"

        # Report status
        self.mmu.log_always(msg)


    def get_default_gates(self, mmu_unit, empty=False):
        if empty:
            return [g for g in mmu_unit.gate_range()
                    if self.mmu.gate_status[g] == GATE_EMPTY]
        return [g for g in mmu_unit.gate_range()
                if self.mmu.gate_status[g] != GATE_EMPTY]


    def validate_gates(self, mmu_unit, gates):
        for gate in gates:
            if not mmu_unit.manages_gate(gate):
                raise MmuError("Gate %d is not managed by %s" % (gate, mmu_unit.name))
        return gates
