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
from ..mmu_constants     import *
from ..mmu_utils         import MmuError
from .mmu_base_command   import *
from .mmu_command_mixins import UnloadEjectMixin


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
        mmu = self.mmu

        if self.check_if_disabled(): return

        gate = gcmd.get_int('GATE', mmu.gate_selected, minval=0, maxval=mmu.num_gates - 1)
        force = bool(gcmd.get_int('FORCE', 0, minval=0, maxval=1))
        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate]): return
        mmu._fix_started_state()

        can_crossload = (
            (mmu.mmu_unit().can_crossload or mmu.mmu_unit().multigear)
            and mmu.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate)
        )
        if not can_crossload and gate != mmu.gate_selected:
            if self.check_if_loaded(): return

        # Determine if full eject_from_gate is necessary
        in_bypass = mmu.gate_selected == TOOL_GATE_BYPASS
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0, minval=0, maxval=1)) or in_bypass
        can_eject_from_gate = (
            not extruder_only
            and not (in_bypass and mmu.filament_pos != FILAMENT_POS_UNLOADED and gate >= 0)
            and (
                (mmu.mmu_unit(gate).multigear and gate != mmu.gate_selected)
                or mmu.filament_pos == FILAMENT_POS_UNLOADED
                or force
            )
        )

        if not can_eject_from_gate and mmu.filament_pos == FILAMENT_POS_UNLOADED:
            mmu.log_always("Filament not loaded")
            return

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during filament eject

                    current_gate = mmu.gate_selected
                    if gate != current_gate:
                        mmu.select_gate(gate)

                    try:
                        self._handle_unload_eject(gcmd) # From mixin
                        if can_eject_from_gate:
                            mmu.log_always("Ejecting filament out of %s" % ("current gate" if gate == current_gate else "gate %d" % gate))
                            mmu._eject_from_gate()

                    finally:
                        if mmu.gate_selected != current_gate:
                            # If necessary or easy restore previous gate
                            if mmu.is_in_print() or mmu.mmu_unit().multigear or mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                mmu.select_gate(current_gate)
                            else:
                                # Lazy movement means we have side effect of changed tool/gate
                                mmu._ensure_ttg_match()
                                mmu._initialize_encoder() # Encoder 0000

                    mmu._persist_swap_statistics()

        except MmuError as ee:
            mmu.handle_mmu_error("Filament eject for gate %d failed: %s" % (gate, str(ee)))
