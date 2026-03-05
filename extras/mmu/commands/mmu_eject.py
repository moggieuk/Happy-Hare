# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_EJECT command
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


class MmuEjectCommand(BaseCommand):

    CMD = "MMU_EJECT"

    HELP_BRIEF = "Alias for MMU_UNLOAD if filament is loaded but will fully eject filament from MMU (release from gear) if already in unloaded state"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "GATE          = #(int)\n"
        + "FORCE         = [0|1]\n"
        + "EXTRUDER_ONLY = [0|1]\n"
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

        gate = gcmd.get_int('GATE', self.mmu.gate_selected, minval=0, maxval=self.mmu.num_gates - 1)
        force = bool(gcmd.get_int('FORCE', 0, minval=0, maxval=1))
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate]): return
        self.mmu._fix_started_state()

        can_crossload = (
            (self.mmu.mmu_unit().can_crossload or self.mmu.mmu_unit().multigear)
            and self.mmu.sensor_manager.has_gate_sensor(SENSOR_GEAR_PREFIX, gate)
        )
        if not can_crossload and gate != self.mmu.gate_selected:
            if self.mmu.check_if_loaded(): return

        # Determine if full eject_from_gate is necessary
        in_bypass = self.mmu.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        can_eject_from_gate = (
            not extruder_only
            and not (in_bypass and self.mmu.filament_pos != FILAMENT_POS_UNLOADED and gate >= 0)
            and (
                (self.mmu.mmu_unit(gate).multigear and gate != self.mmu.gate_selected)
                or self.mmu.filament_pos == FILAMENT_POS_UNLOADED
                or force
            )
        )

        if not can_eject_from_gate and self.mmu.filament_pos == FILAMENT_POS_UNLOADED:
            self.mmu.log_always("Filament not loaded")
            return

        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament eject

                    current_gate = self.mmu.gate_selected
                    if gate != current_gate:
                        self.mmu.select_gate(gate)

                    try:
                        self.mmu._mmu_unload_eject(gcmd)
                        if can_eject_from_gate:
                            self.mmu.log_always("Ejecting filament out of %s" % ("current gate" if gate == current_gate else "gate %d" % gate))
                            self.mmu._eject_from_gate()

                    finally:
                        if self.mmu.gate_selected != current_gate:
                            # If necessary or easy restore previous gate
                            if self.mmu.is_in_print() or self.mmu.mmu_unit().multigear or self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                self.mmu.select_gate(current_gate)
                            else:
                                # Lazy movement means we have side effect of changed tool/gate
                                self.mmu._ensure_ttg_match()
                                self.mmu._initialize_encoder() # Encoder 0000

                    self.mmu._persist_swap_statistics()

        except MmuError as ee:
            self.mmu.handle_mmu_error("Filament eject for gate %d failed: %s" % (gate, str(ee)))
