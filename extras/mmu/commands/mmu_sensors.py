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

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


def _format_sensor_report(mmu, sm, sensor_states, detail, mmu_unit=None, header=True):
    """
    Render a sensor_states dict (as returned by MmuSensorManager.get_sensor_states(), or a
    single-entry dict built by MMU_SENSORS' own SENSOR= path) into the MMU_SENSORS report text.
    """
    if all(v[0] is None for v in sensor_states.values()) and not detail:
        return "No active sensors. Use DETAIL=1 to see all including disabled"

    summary = ""
    pad = 21
    if header and mmu.mmu_machine.num_units > 1:
        if mmu_unit is None:
            pad = 27
            unit_str = "all units"
        else:
            unit_str = mmu_unit.name
        summary += f"Sensors configured for {unit_str}:\n"

    for name in sorted(sensor_states):
        state, sensor = sensor_states[name]

        if state is None and not detail:
            continue # Sensor disabled

        if sm.get_unprefixed_sensor_name(name) in [SENSOR_PROPORTIONAL]:
            # Special case analog sensor
            st = sensor.get_status(0) or {}
            value = st.get('value', 0.)
            value_raw = st.get('value_raw', 0.)

            if state is None:
                value_str = f"{value:.2f} (disabled)"
            else:
                value_str = f"{value:.2f}"

            summary += f"{name:<{pad}} --> {value_str}"
            summary += f" (raw: {value_raw:.4f})"

        else:
            trig = "TRIGGERED" if sensor.runout_helper.filament_present else "Open"

            value_str = f"{trig} (disabled)" if state is None else trig
            summary += f"{name:<{pad}} --> {value_str}"

            if sensor.__class__.__name__ == "MmuVirtualEndstopSensor":
                summary += f" (virtual)"


            if (
                detail and
                state is not None and
                sensor.runout_helper.runout_suspended is False
            ):
                summary += ", Runout enabled"

        summary += "\n"

    return summary


class MmuSensorsCommand(BaseCommand):

    CMD = "MMU_SENSORS"

    HELP_BRIEF = "Query, or enable/disable, sensors fitted to mmu"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "UNIT   = #(int) Specify unit else unit with active gate will be assumed\n"
        + "DETAIL = [0|1]  Set to also see disabled sensors\n"
        + "SENSOR = _sensor_name_ Target one sensor (qualified, e.g. 'unit0:mmu_shared_exit',\n"
        + "         or bare, e.g. 'mmu_exit_0'). Given alone, reports just that sensor\n"
        + "ENABLE = [0|1]  Persistently enable/disable the sensor named by SENSOR (requires SENSOR)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} DETAIL=1 ...report state of all sensors on all units (even disabled ones)\n"
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
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
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
                    "or use the fully-qualified name (see MMU_SENSORS DETAIL=1)" % sensor_param)
                return
            if sensor is None:
                mmu.log_error(
                    "Unknown sensor '%s'. Use MMU_SENSORS DETAIL=1 to see valid sensor names" % sensor_param)
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
            summary = _format_sensor_report(mmu, sm, sensor_states, detail=True, header=False)
        else:
            sensor_states = (
                sm.get_sensor_states(all_sensors=True)
                if mmu_unit is None
                else sm.get_sensor_states(unit=mmu_unit.unit_index)
            )
            summary = _format_sensor_report(mmu, sm, sensor_states, detail=detail, mmu_unit=mmu_unit)

        mmu.log_always(summary or "No sensors found")
