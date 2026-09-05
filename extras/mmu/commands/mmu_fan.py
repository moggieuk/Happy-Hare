# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_FAN command.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from ..unit.mmu_fan_manager import (
    FAN_OFF, FAN_ON, FAN_AUTO, FAN_SOURCE_DEFAULT, FAN_TEMPERATURE_SOURCES)
from .mmu_base_command import *


class MmuFanCommand(BaseCommand):

    CMD = "MMU_FAN"

    HELP_BRIEF = "Control MMU fan(s)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT       = #(int/name) Optional if only one unit is fitted\n"
        + "ENABLE     = [0|1] Disable/enable automatic fan management\n"
        + "FAN_FORCED = [0|1|2] Force OFF, force ON, or return to AUTO\n"
        + "SOURCE     = [environment|mcu|default] AUTO temperature source\n"
        + "ON_TEMP    = # (20-80) AUTO mode fan-on temperature\n"
        + "OFF_TEMP   = # (20-80) AUTO mode fan-off temperature\n"
        + "GATE       = # Gate to control (per-gate fans only)\n"
        + "GATES      = g1,g2 Gates to control (per-gate fans only)\n"
        + "(no action parameters for status report)"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                              ...Show fan status\n"
        + f"{CMD} FAN_FORCED=1                 ...Force all unit fans on\n"
        + f"{CMD} FAN_FORCED=0 GATE=2          ...Force gate 2 fan off\n"
        + f"{CMD} FAN_FORCED=2 GATES=1,2       ...Return gate 1 and 2 fans to AUTO\n"
        + f"{CMD} SOURCE=mcu GATE=2            ...Use gate 2 MCU temperature\n"
        + f"{CMD} SOURCE=default GATE=2        ...Restore gate 2 default source\n"
        + f"{CMD} ON_TEMP=55 OFF_TEMP=52       ...Set the unit AUTO temperature range\n"
        + f"{CMD} ON_TEMP=60 OFF_TEMP=58 GATE=2 ...Set gate 2 AUTO temperature range\n"
        + f"{CMD} ENABLE=0                     ...Disable control and turn all unit fans off\n"
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

    def _run(self, gcmd, mmu_unit):
        manager = mmu_unit.fan_manager
        if not manager.has_fans():
            raise gcmd.error("No MMU fan configured on %s" % mmu_unit.name)

        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        forced = gcmd.get_int('FAN_FORCED', None, minval=FAN_OFF, maxval=FAN_AUTO)
        source = gcmd.get('SOURCE', None)
        if source is not None:
            source = str(source).lower()
        on_temp = gcmd.get_float('ON_TEMP', None, minval=20., maxval=80.)
        off_temp = gcmd.get_float('OFF_TEMP', None, minval=20., maxval=80.)
        gates, gates_supplied = self._get_gates(gcmd, mmu_unit)

        if gates_supplied and not manager.has_per_gate_fans():
            raise gcmd.error("GATE/GATES is only available when per-gate fans are configured")
        if gates_supplied and forced is None and source is None and on_temp is None and off_temp is None:
            raise gcmd.error("GATE/GATES requires FAN_FORCED, SOURCE, ON_TEMP, or OFF_TEMP")

        if source is not None:
            allowed_sources = FAN_TEMPERATURE_SOURCES + (FAN_SOURCE_DEFAULT,)
            if source not in allowed_sources:
                raise gcmd.error("SOURCE must be one of: %s" % ", ".join(allowed_sources))
            try:
                manager.set_temperature_source(
                    source, gates if gates_supplied else None)
            except ValueError as e:
                raise gcmd.error(str(e))
            selected = "gates %s" % ",".join(map(str, gates)) if gates_supplied else "all fans"
            sources = manager.get_snapshot(gates if gates_supplied else None)
            description = ", ".join(
                "%s: %s" % (
                    "G%d" % item['gate'] if item['gate'] is not None else "fan",
                    item['source'])
                for item in sources)
            self.mmu.log_info("MMU %s on %s temperature source: %s" % (
                selected, mmu_unit.name, description))

        if on_temp is not None or off_temp is not None:
            try:
                manager.set_thresholds(
                    on_temp, off_temp, gates if gates_supplied else None)
            except ValueError as e:
                raise gcmd.error(str(e))
            selected = "gates %s" % ",".join(map(str, gates)) if gates_supplied else "all fans"
            ranges = manager.get_snapshot(gates if gates_supplied else None)
            description = ", ".join(
                "%s: OFF <= %.1f%sC, ON >= %.1f%sC" % (
                    "G%d" % item['gate'] if item['gate'] is not None else "fan",
                    item['off_temp'], UI_DEGREE, item['on_temp'], UI_DEGREE)
                for item in ranges)
            self.mmu.log_info("MMU %s on %s AUTO range: %s" % (
                selected, mmu_unit.name, description))

        if enable is not None:
            manager.set_enabled(bool(enable))
            self.mmu.log_info("MMU fan control %s for %s" % (
                "enabled" if enable else "disabled; all fans off", mmu_unit.name))

        if forced is not None:
            manager.set_mode(forced, gates if gates_supplied else None)
            selected = "gates %s" % ",".join(map(str, gates)) if gates_supplied else "all fans"
            self.mmu.log_info("MMU %s on %s set to %s" % (
                selected, mmu_unit.name, {FAN_OFF: "OFF", FAN_ON: "ON", FAN_AUTO: "AUTO"}[forced]))

        if enable is None and forced is None and source is None and on_temp is None and off_temp is None:
            self._show_status(mmu_unit)

    def _get_gates(self, gcmd, mmu_unit):
        gate_value = gcmd.get('GATE', None)
        gates_value = gcmd.get('GATES', None)
        if gate_value is not None and gates_value is not None:
            raise gcmd.error("Specify GATE or GATES, not both")

        raw = gate_value if gate_value is not None else gates_value
        if raw is None:
            return [], False

        try:
            gates = [int(value.strip()) for value in str(raw).split(',') if value.strip()]
        except ValueError:
            raise gcmd.error("Invalid GATE/GATES parameter: %s" % raw)
        if not gates:
            raise gcmd.error("GATE/GATES cannot be empty")

        unique = []
        for gate in gates:
            if not mmu_unit.manages_gate(gate):
                raise gcmd.error("Gate %d is not part of %s" % (gate, mmu_unit.name))
            if gate not in unique:
                unique.append(gate)
        return unique, True

    def _show_status(self, mmu_unit):
        manager = mmu_unit.fan_manager
        p = mmu_unit.p
        snapshot = manager.get_snapshot()
        lines = [
            "MMU fan control for %s: %s" % (mmu_unit.name, "ENABLED" if manager.is_enabled() else "DISABLED"),
        ]
        if not manager.has_per_gate_fans():
            item = snapshot[0]
            lines.append("AUTO range in force: OFF <= %.1f%sC, ON >= %.1f%sC; polling %.1fs" % (
                item['off_temp'], UI_DEGREE, item['on_temp'], UI_DEGREE, p.fan_polling_time))
        else:
            lines.append("AUTO ranges in force; polling %.1fs" % p.fan_polling_time)
        for item in snapshot:
            label = "Gate %d" % item['gate'] if item['gate'] is not None else "Fan"
            temperature = "n/a" if item['temperature'] is None else "%.1f%sC" % (item['temperature'], UI_DEGREE)
            auto_range = ""
            if item['gate'] is not None:
                auto_range = ", range OFF <= %.1f%sC / ON >= %.1f%sC" % (
                    item['off_temp'], UI_DEGREE, item['on_temp'], UI_DEGREE)
            source = item['source'] or "none"
            lines.append("%s (%s): %s, %.0f%%, source %s: %s%s" % (
                label, item['name'], item['mode'], item['speed'] * 100.,
                source, temperature, auto_range))
        self.mmu.log_always("\n".join(lines))
