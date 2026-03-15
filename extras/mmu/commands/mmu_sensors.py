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
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT   = #(int)\n"
        + "DETAIL = [0|1]\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
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

        if mmu.check_if_disabled(): return

        mmu_unit = self.get_unit(gcmd)
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))

        summary = ""

        sensors = (
            sm.get_active_sensors(all_sensors=True)
            if mmu_unit is None
            else sm.get_unit_sensors(mmu_unit.unit_index)
        )

        if all(v is None for v in sensors.values()) and not detail:
            summary += "No active sensors. Use DETAIL=1 to see all"
        else:
            for name in sorted(sensors):
                state = sensors[name]
                if state is None and not detail:
                    continue

                sensor = (
                    sm.all_sensors_map.get(name)
                    if mmu_unit is None
                    else sm.unit_sensors[mmu_unit.unit_index].get(name)
                )
                if sensor is None:
                    # Defensive: should not happen, but avoid attribute errors
                    summary += "%s: (sensor missing)\n" % name
                    continue

                if name in [SENSOR_PROPORTIONAL]:
                    # Special case analog sensor
                    st = sensor.get_status(0) or {}
                    value = st.get('value', 0.)
                    value_raw = st.get('value_raw', 0.)

                    summary += "%s: %.2f" % (
                        name,
                        ("(%.2f, currently disabled)" % value) if state is None else value
                    )
                    if detail:
                        summary += " (raw: %.2f)" % value_raw

                else:
                    trig = "%s" % ('TRIGGERED' if sensor.runout_helper.filament_present else 'Open')
                    summary += "%s: %s" % (
                        name,
                        ("(%s, currently disabled)" % trig) if state is None else trig
                    )
                    if detail and sensor.runout_helper.runout_suspended is not None and state is not None:
                        summary += "%s" % (", Runout enabled" if not sensor.runout_helper.runout_suspended else "")

                summary += "\n"

        mmu.log_always(summary)
