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
        "%s: %s\n" % (CMD, HELP_BRIEF)
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

        if self.mmu.check_if_disabled(): return
        if self.mmu.check_if_printing(): return
        if self.mmu.check_if_not_homed(): return

        gate = gcmd.get_int('GATE', self.mmu.gate_selected, minval=0, maxval=self.mmu.num_gates - 1)
        if self.mmu.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate]): return

        can_crossload = (
            (self.mmu.mmu_unit().can_crossload or self.mmu.mmu_unit().multigear)
            and self.mmu.sensor_manager.has_gate_sensor(SENSOR_GEAR_PREFIX, gate)
        )
        if not can_crossload:
            if self.mmu.check_if_bypass(): return
            if self.mmu.check_if_loaded(): return

        self.mmu.log_always("Preloading filament in %s..." % ("current gate" if gate == self.mmu.gate_selected else "gate %d" % gate))
        try:
            with self.mmu.wrap_sync_gear_to_extruder():
                with self.mmu.wrap_suppress_visual_log():
                    with self.mmu.wrap_action(ACTION_CHECKING):

                        current_gate = self.mmu.gate_selected
                        if gate != current_gate:
                            self.mmu.select_gate(gate)

                        try:
                            self.mmu._preload_gate()

                        finally:
                            if self.mmu.gate_selected != current_gate:
                                # If necessary or easy restore previous gate
                                if self.mmu.is_in_print() or self.mmu.mmu_unit().multigear or self.mmu.filament_pos != FILAMENT_POS_UNLOADED:
                                    self.mmu.select_gate(current_gate)
                                else:
                                    # Lazy movement means we have side effect of changed tool/gate
                                    self.mmu._ensure_ttg_match()
                                    self.mmu._initialize_encoder() # Encoder 0000
        except MmuError as ee:
            self.mmu.handle_mmu_error("Filament preload for gate %d failed: %s" % (gate, str(ee)))
