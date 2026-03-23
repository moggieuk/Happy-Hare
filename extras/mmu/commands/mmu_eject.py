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
from .mmu_misc_mixins    import UnloadEjectMixin


class MmuEjectCommand(UnloadEjectMixin, BaseCommand):

    CMD = "MMU_EJECT"

    HELP_BRIEF = "Ejects filament from MMU on chosen gate. If current gate then performs unload first if not already unloaded"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE          = #(int)\n"
        + "FORCE         = [0|1]\n"
        + "EXTRUDER_ONLY = [0|1]\n"
        + "SKIP_TIP      = [0|1]\n"
        + "RESTORE       = [0|1]\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}        ...Eject filament from current gate\n"
        + f"{CMD} GATE=5 ...Eject filament on gate 5\n"
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
        current_gate = mmu.gate_selected
        eject_unit = mmu.mmu_machine.get_mmu_unit_by_gate(gate)
        force = bool(gcmd.get_int('FORCE', 0, minval=0, maxval=1))

        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate]): return
        mmu._fix_started_state()

        can_crossload = (
            mmu.mmu_unit().can_crossload or
            eject_unit != mmu.mmu_unit()
        )

        # Does being loaded prevent the eject?
        if not can_crossload and gate != mmu.gate_selected:
            if self.check_if_loaded(): return

        # Determine if we can fully eject_from_gate
        in_bypass = (mmu.gate_selected == TOOL_GATE_BYPASS)
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0)) or in_bypass
        can_eject_from_gate = (
            not extruder_only
            and not (
                in_bypass
                and mmu.filament_pos != FILAMENT_POS_UNLOADED
                and gate >= 0
            )
            and (
                (can_crossload and gate != mmu.gate_selected)
                or mmu.filament_pos == FILAMENT_POS_UNLOADED
                or force
            )
        )

        if not can_eject_from_gate and mmu.filament_pos == FILAMENT_POS_UNLOADED:
            mmu.log_always("Filament not loaded")
            return

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu._wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during unload

                    # Same as MMU_UNLOAD logic
                    if gate == current_gate and mmu.filament_pos != FILAMENT_POS_UNLOADED:
                        self._handle_unload(gcmd)

                    if can_eject_from_gate:
                        try:
                            if gate != current_gate:
                                mmu.select_gate(gate)

                            mmu.log_always("Ejecting filament out of %s" % ("current gate" if gate == current_gate else "gate %d" % gate))
                            mmu._eject_from_gate()

                        finally:
                            if mmu.gate_selected != current_gate:
                                mmu.select_gate(current_gate)

                                # If necessary or easy restore previous gate
                                if mmu.is_in_print() or mmu.mmu_unit().multigear or mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                    mmu.select_gate(current_gate)
                                else:
                                    # Lazy movement means we have side effect of changed tool/gate
                                    mmu._ensure_ttg_match()
                                    mmu._initialize_encoder() # Encoder 0000

        except MmuError as ee:
            mmu.handle_mmu_error("Filament eject for gate %d failed: %s" % (gate, str(ee)))
