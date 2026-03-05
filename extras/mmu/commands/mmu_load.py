# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_LOAD command
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
from .mmu_base_command import BaseCommand


class MmuLoadCommand(BaseCommand):

    CMD = "MMU_LOAD"

    HELP_BRIEF = "Loads filament on current tool/gate or optionally loads just the extruder for bypass or recovery usage (EXTRUDER_ONLY=1)"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "EXTRUDER_ONLY = [0|1]\n"
        + "SKIP_PURGE    = [0|1]\n"
        + "RESTORE       = [0|1]\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
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

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_not_homed(): return
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[self.mmu.gate_selected]): return
        self.mmu._fix_started_state()

        in_bypass = self.mmu.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1) or in_bypass)
        skip_purge = bool(gcmd.get_int('SKIP_PURGE', 0, minval=0, maxval=1))
        restore = bool(gcmd.get_int('RESTORE', 1, minval=0, maxval=1))
        do_purge = PURGE_STANDALONE if not skip_purge else PURGE_NONE

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament load
                    if self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                        self.mmu.log_always("Filament already loaded")
                        return

                    self.mmu._note_toolchange("> %s" % self.mmu.selected_tool_string())

                    if extruder_only:
                        self.mmu.load_sequence(bowden_move=0., extruder_only=True, purge=do_purge)

                    else:
                        self.mmu._next_tool = self.mmu.tool_selected # Valid only during the load process - cleared in _continue_after()
                        self.mmu.last_statistics = {}
                        self.mmu._save_toolhead_position_and_park('load')
                        if self.mmu.tool_selected == TOOL_GATE_UNKNOWN:
                            self.mmu.log_error("Selected gate is not mapped to any tool. Will load filament but be sure to use MMU_TTG_MAP to assign tool")

                        self.mmu._select_and_load_tool(self.mmu.tool_selected, purge=do_purge)
                        self.mmu._persist_gate_statistics()
                        self.mmu._continue_after('load', restore=restore)

                    self.mmu._persist_swap_statistics()

        except MmuError as ee:
            self.mmu.handle_mmu_error("%s.\nOccured when loading tool: %s" % (str(ee), self.mmu._last_toolchange))
            if self.mmu.tool_selected == TOOL_GATE_BYPASS:
                self.mmu._set_filament_pos_state(FILAMENT_POS_UNKNOWN)
