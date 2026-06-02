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
        if self.check_if_printing(): return

        current_gate = mmu.gate_selected
        active_unit = mmu.mmu_unit()

        # Special hidden use case for eject buttons where only local gate is known
        lgate = gcmd.get_int('LGATE', None)
        if lgate is not None:
            eject_unit = self.get_unit(gcmd, mode="optional")
            if eject_unit is None:
                raise gcmd.error("UNIT parameter is required with LGATE")
            lgate = gcmd.get_int('LGATE', 0, minval=0, maxval=eject_unit.num_gates - 1)
            gate = eject_unit.logical_gate(lgate) # Convert to global logical gate index

        else:
            gate = gcmd.get_int('GATE', current_gate, minval=0, maxval=mmu.num_gates - 1)
            eject_unit = mmu.mmu_unit(gate)

        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate], mmu_unit=eject_unit): return
        mmu.fix_started_state()

        force = bool(gcmd.get_int('FORCE', 0, minval=0, maxval=1))
        in_bypass = (mmu.gate_selected == TOOL_GATE_BYPASS)
        extruder_only = bool(gcmd.get_int('EXTRUDER_ONLY', 0)) or in_bypass

        filament_pos = mmu.filament_pos
        is_unloaded = filament_pos == FILAMENT_POS_UNLOADED

        can_continue = (
            is_unloaded
            or eject_unit is not active_unit
            or active_unit.can_crossload
            or gate == current_gate
        )

        if not can_continue:
            # If being loaded is preventing the eject give specific error
            if gate != current_gate and self.check_if_loaded(): return
            self.mmu.log_error("Operation not possible: Can't crossload on this mmu type")
            return

        # Techincally possible, now determine if we can fully eject_from_gate
        # rather than be an alias for UNLOAD
        can_eject_from_gate = (
            not extruder_only
            and gate >= 0
            and (is_unloaded or force or gate != current_gate)
        )

        if not can_eject_from_gate and filament_pos == FILAMENT_POS_UNLOADED:
            mmu.log_always("Filament not loaded")
            return

        mmu.log_always("Ejecting filament out of %s" % ("current gate" if gate == current_gate else "gate %d" % gate))
        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_suspend_filament_monitoring(): # Don't want runout accidently triggering during unload

                    # Same as MMU_UNLOAD logic
                    if gate == current_gate and filament_pos != FILAMENT_POS_UNLOADED:
                        self._handle_unload(gcmd)

                    if can_eject_from_gate:
                        try:
                            if gate != current_gate:
                                mmu.select_gate(gate)

                            mmu._eject_from_gate()

                        finally:
                            if mmu.gate_selected != current_gate:
                                # If necessary or easy restore previous gate
                                if mmu.is_in_print() or mmu.mmu_unit().multigear or filament_pos != FILAMENT_POS_UNLOADED:
                                    mmu.select_gate(current_gate)
                                else:
                                    # Lazy movement means we have side effect of changed tool/gate
                                    mmu.gate_maps.ensure_ttg_match()
                                    mmu.initialize_encoder() # Encoder 0000

        except MmuError as ee:
            mmu.handle_mmu_error("Filament eject for gate %d failed: %s" % (gate, str(ee)))
