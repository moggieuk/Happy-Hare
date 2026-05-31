# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_PRELOAD command
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


class MmuPreloadCommand(BaseCommand):

    CMD = "MMU_PRELOAD"

    HELP_BRIEF = "Preloads filament at specified or current gate"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE = #(int)\n"
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
        if self.check_if_printing(): return

        current_gate = mmu.gate_selected
        active_unit = mmu.mmu_unit()

        # Special hidden use case for preload buttons where only local gate is known
        lgate = gcmd.get_int('LGATE', None)
        if lgate is not None:
            preload_unit = self.get_unit(gcmd, mode="optional")
            if eject_unit is None:
                raise gcmd.error("UNIT parameter is required with LGATE")
            lgate = gcmd.get_int('LGATE', 0, minval=0, maxval=eject_unit.num_gates - 1)
            gate = preload_unit.logical_gate(lgate) # Convert to global logical gate index

        else:
            gate = gcmd.get_int('GATE', current_gate, minval=0, maxval=mmu.num_gates - 1)
            preload_unit = mmu.mmu_unit(gate)

# PAUL
#        gate = gcmd.get_int('GATE', mmu.gate_selected, minval=0, maxval=mmu.num_gates - 1)
#        current_gate = mmu.gate_selected
#        preload_unit = mmu.mmu_unit(gate)
#        active_unit = mmu.mmu_unit()

        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate], mmu_unit=preload_unit): return

        filament_pos = mmu.filament_pos

        can_crossload = (
            active_unit is not preload_unit or
            (
                active_unit.can_crossload and
                (
                    mmu.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate) or
                    filament_pos == FILAMENT_POS_UNLOADED or
                    (
                        filament_pos != FILAMENT_POS_UNLOADED and
                        not preload_unit.manages_gate(current_gate)
                    )
                )
            )
        )
        if not can_crossload:
            if self.check_if_bypass(): return
            if self.check_if_loaded(): return

        mmu.log_always("Preloading filament in %s..." % ("current gate" if gate == current_gate else "gate %d" % gate))

        mmu.log_error("PAUL: command would run") # PAUL
        return # PAUL
        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_suppress_visual_log():
                    with mmu.wrap_action(ACTION_CHECKING):
                        if gate != current_gate:
                            mmu.select_gate(gate)

                        try:
                            mmu._preload_gate()

                        finally:
                            if mmu.gate_selected != current_gate:
                                # If necessary or easy restore previous gate
                                if mmu.is_in_print() or active_unit.multigear or filament_pos != FILAMENT_POS_UNLOADED:
                                    mmu.select_gate(current_gate)
                                else:
                                    # Lazy gate reselection means we have side effect of changed tool/gate
                                    mmu.gate_maps.ensure_ttg_match()
                                    mmu.initialize_encoder() # Encoder 0000

        except MmuError as ee:
            mmu.handle_mmu_error("Filament preload for gate %d failed: %s" % (gate, str(ee)))
