# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_SENSORS command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import re

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *

_TRAILING_NUMBER = re.compile(r'^(.*?)(\d+)$')


def _sensor_sort_key(name):
    """
    Natural sort: a numeric suffix (e.g. the gate number in 'mmu_entry_9') sorts
    numerically rather than lexicographically, so gate 9 lists before gate 10.
    """
    m = _TRAILING_NUMBER.match(name)
    if m:
        return (m.group(1), int(m.group(2)))
    return (name, -1)


def _format_sensor_report(mmu, sm, sensor_states, mmu_unit=None, header=True):
    """
    Render a sensor_states dict (as returned by MmuSensorManager.get_sensor_states(), or a
    single-entry dict built by MMU_SENSORS' own SENSOR= path) into the MMU_SENSORS report text.
    Every sensor is listed, disabled or not - a disabled one is tagged "(DISABLE)" regardless
    of sensor type (switch, virtual, or the analog proportional sensor).
    """
    lines = []
    pad = 21
    header_line = None
    if header and mmu.mmu_machine.num_units > 1:
        if mmu_unit is None:
            pad = 27
            unit_str = "all units"
        else:
            unit_str = mmu_unit.name
        header_line = f"Sensors configured for {unit_str}:"

    for name in sorted(sensor_states, key=_sensor_sort_key):
        state, sensor = sensor_states[name]
        line = ""

        if sm.get_unprefixed_sensor_name(name) in [SENSOR_PROPORTIONAL]:
            # Special case analog sensor
            st = sensor.get_status(0) or {}
            value = st.get('value', 0.)
            value_raw = st.get('value_raw', 0.)

            value_str = f"{value:.2f} (DISABLE)" if state is None else f"{value:.2f}"
            line += f"{name:<{pad}} --> {value_str}"
            line += f" (raw: {value_raw:.4f})"

        else:
            trig = "TRIGGERED" if sensor.runout_helper.filament_present else "Open"

            value_str = f"{trig} (DISABLE)" if state is None else trig
            line += f"{name:<{pad}} --> {value_str}"

            if sensor.__class__.__name__ == "MmuVirtualEndstopSensor":
                line += f" (virtual)"

            if state is not None and sensor.runout_helper.runout_suspended is False:
                line += ", Runout enabled"

        lines.append(line)

    if header_line is not None:
        lines.insert(0, header_line)

    return "\n".join(lines)


class MmuSensorsCommand(BaseCommand):

    CMD = "MMU_SENSORS"

    HELP_BRIEF = "Query, or enable/disable, sensors fitted to mmu"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "UNIT   = #(int) Specify unit else unit with active gate will be assumed\n"
        + "SENSOR = _sensor_name_ Target one sensor by name; alone, reports just that sensor\n"
        + "ENABLE = [0|1]  Persistently enable/disable the sensor named by SENSOR\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}          ...report state of every sensor on all units, including disabled ones\n"
        + f"{CMD} UNIT=1   ...report state of active sensors on unit index 1\n"
        + f"{CMD} SENSOR=mmu_exit_0 ...report state of just that one sensor, even if disabled\n"
        + f"{CMD} SENSOR=unit0:mmu_shared_exit ENABLE=0 ...persistently disable that sensor (sticky across restarts)\n"
        + f"{CMD} SENSOR=mmu_exit_0 ENABLE=1 ...persistently re-enable it\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        sm = mmu.sensor_manager

        if self.check_if_disabled(): return

        mmu_unit = self.get_unit(gcmd, mode="optional")
        sensor_param = gcmd.get('SENSOR', None)
        enable_param = gcmd.get_int('ENABLE', None, minval=0, maxval=1)

        if enable_param is not None and sensor_param is None:
            mmu.log_error("ENABLE= requires SENSOR=<name> naming the sensor to enable/disable")
            return

        single = None
        if sensor_param is not None:
            qualified, sensor, err = sm.resolve_sensor(sensor_param, mmu_unit=mmu_unit)
            if err == 'ambiguous':
                mmu.log_error(
                    "SENSOR='%s' matches more than one unit's sensor. Specify UNIT= "
                    "or use the fully-qualified name (see MMU_SENSORS)" % sensor_param)
                return
            if sensor is None:
                mmu.log_error(
                    "Unknown sensor '%s'. Use MMU_SENSORS to see valid sensor names" % sensor_param)
                return
            single = (qualified, sensor)

            if enable_param is not None:
                enabled = bool(enable_param)
                changed = sm.set_sensor_enabled(qualified, enabled, write=True)
                mmu.log_always("Sensor '%s' %s%s" % (
                    qualified, "enabled" if enabled else "disabled", "" if changed else " (no change)"))
                if changed and not enabled and sm.get_unprefixed_sensor_name(qualified) in SHARED_GATE_ENDSTOPS:
                    mmu.log_warning(
                        "'%s' is a shared-gate endstop. Disabling it persistently defeats the safety "
                        "check that stops one gate's filament being driven into another's at the hub "
                        "during crossload/preload/NFC-scan until it is re-enabled" % qualified)

        if single is not None:
            qualified, sensor = single
            sensor_states = {qualified: (
                bool(sensor.runout_helper.filament_present) if sensor.runout_helper.sensor_enabled else None,
                sensor)}
            summary = _format_sensor_report(mmu, sm, sensor_states, header=False)
        else:
            sensor_states = (
                sm.get_sensor_states(all_sensors=True)
                if mmu_unit is None
                else sm.get_sensor_states(unit=mmu_unit.unit_index)
            )
            summary = _format_sensor_report(mmu, sm, sensor_states, mmu_unit=mmu_unit)

        mmu.log_always(summary or "No sensors found")
