# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_RECOVER command
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


class MmuRecoverCommand(BaseCommand):

    CMD = "MMU_RECOVER"

    HELP_BRIEF = "Recover MMU tool/gate/filament state"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "TOOL   = t Optionally force the assignment of specified tool number\n"
        + "GATE   = g Optionally force the assignment of the specified gate number (fixes TTG map)\n"
        + "BYPASS = 1 Used to force the assignment of the bypass Tool/Gate\n"
        + "LOADED = [0|1] Force unloaded or loaded (in the extruder) state\n"
        + "STRICT = 1 If auto-recovering state, allows extended tests including extruder heating\n"
        + "(no parameters for automatic filament position recovery)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s               ...automatically recover filament position\n" % CMD
        + "%s LOADED=1      ...to indicate filament is in the extruder\n" % CMD
        + "%s TOOL=2 GATE=3 ...to indicate T2 is currently loaded from gate 3\n" % CMD
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
        # BaseCommand already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return

        tool = gcmd.get_int('TOOL', TOOL_GATE_UNKNOWN, minval=-2, maxval=mmu.num_gates - 1)
        mod_gate = gcmd.get_int('GATE', TOOL_GATE_UNKNOWN, minval=-2, maxval=mmu.num_gates - 1)

        if gcmd.get_int('BYPASS', None, minval=0, maxval=1):
            mod_gate = TOOL_GATE_BYPASS
            tool = TOOL_GATE_BYPASS

        loaded = gcmd.get_int('LOADED', -1, minval=0, maxval=1)
        strict = gcmd.get_int('STRICT', 0, minval=0, maxval=1)

        try:
            if tool == TOOL_GATE_BYPASS:
                mmu.selector().restore_gate(TOOL_GATE_BYPASS)
                mmu._set_gate_selected(TOOL_GATE_BYPASS)
                mmu._set_tool_selected(TOOL_GATE_BYPASS)
                mmu._ensure_ttg_match()

            elif tool >= 0:  # If tool is specified then use and optionally override the gate
                mmu._set_tool_selected(tool)
                gate = mmu.ttg_map[tool]
                if mod_gate >= 0:
                    gate = mod_gate
                if gate >= 0:
                    mmu.selector().restore_gate(gate)
                    mmu._set_gate_selected(gate)
                    mmu.log_info("Remapping T%d to gate %d" % (tool, gate))
                    mmu._remap_tool(tool, gate, loaded)

            elif mod_gate >= 0:  # If only gate specified then just reset and ensure tool is correct
                mmu.selector().restore_gate(mod_gate)
                mmu._set_gate_selected(mod_gate)
                mmu._ensure_ttg_match()

            elif tool == TOOL_GATE_UNKNOWN and mmu.tool_selected == TOOL_GATE_BYPASS and loaded == -1:
                # This is to be able to get out of "stuck in bypass" state
                ts = mmu.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
                es = mmu.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
                if ts or es:  # TODO use check_all_sensors() call when sensor_manager is fixed
                    mmu._set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
                else:
                    if es is None and ts is None:
                        mmu.log_warning("Warning: Making assumption that bypass is unloaded because no toolhead sensors are present")
                    mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)
                mmu._set_filament_direction(DIRECTION_UNKNOWN)
                return

            if loaded == 1:
                mmu._set_filament_direction(DIRECTION_LOAD)
                mmu._set_filament_pos_state(FILAMENT_POS_LOADED)
            elif loaded == 0:
                mmu._set_filament_direction(DIRECTION_UNLOAD)
                mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)
            else:
                # Filament position not specified so auto recover
                mmu.recover_filament_pos(strict=strict, message=True)

            # Reset sync state
            mmu.reset_sync_gear_to_extruder(False)

            # Report
            mmu.log_info(f"{mmu._state_to_string()}", color=True)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
