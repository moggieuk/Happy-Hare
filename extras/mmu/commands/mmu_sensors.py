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


class MmuSensorsCommand(BaseCommand):

    CMD = "MMU_SENSORS"

    HELP_BRIEF = "Query state of sensors fitted to mmu"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "UNIT   = #(int) Specify unit else unit with active gate will be assumed\n"
        + "DETAIL = [0|1]  Set to see disabled sensors\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} DETAIL=1 ...report state of all sensors on all units (even disabled ones)\n"
        + f"{CMD} UNIT=1   ...report state of active sensors on unit index 1\n"
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

        summary = ""

        sensor_states = (
            sm.get_sensor_states(all_sensors=True)
            if mmu_unit is None
            else sm.get_sensor_states(unit=mmu_unit.unit_index)
        )

        if all(v[0] is None for v in sensor_states.values()) and not detail:
            summary += "No active sensors. Use DETAIL=1 to see all"

        else:
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

                    summary += f"{name:<16} --> {value_str}"

                    if detail:
                        summary += f" (raw: {value_raw:.2f})"

                else:
                    trig = "TRIGGERED" if sensor.runout_helper.filament_present else "Open"

                    value_str = f"{trig} (disabled)" if state is None else trig
                    summary += f"{name:<16} --> {value_str}"

                    if (
                        detail and
                        state is not None and
                        sensor.runout_helper.runout_suspended is False
                    ):
                        summary += ", Runout enabled"

                summary += "\n"

        mmu.log_always(summary)
