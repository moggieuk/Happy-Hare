# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_NFC_SCAN command - read a gate's NFC/RFID spool tag by jogging
# the filament to the gate's reader (homing against the reader-as-endstop).
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


class MmuNfcScanCommand(BaseCommand):

    CMD = "MMU_NFC_SCAN"

    HELP_BRIEF = "Read the NFC/RFID spool tag for a gate by jogging filament to its reader"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE = #(int) Gate to scan (default: current gate)\n"
    )
    HELP_SUPPLEMENT = (
        "Jogs the filament within the unit's 'nfc_read_window' until the spool's\n"
        "RFID tag reaches the gate's reader, reads it, then re-parks the filament.\n"
        "Examples:\n"
        + f"{CMD}        ...Scan the RFID/NFC tag on the current gate\n"
        + f"{CMD} GATE=2 ...Scan the RFID/NFC tag on gate 2\n"
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
        # BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_printing(): return

        current_gate = mmu.gate_selected
        active_unit = mmu.mmu_unit()

        gate = gcmd.get_int('GATE', current_gate, minval=0, maxval=mmu.num_gates - 1)
        scan_unit = mmu.mmu_unit(gate)

        if self.check_if_not_calibrated(CALIBRATED_ESSENTIAL, check_gates=[gate], mmu_unit=scan_unit): return

        filament_pos = mmu.filament_pos
        is_unloaded = filament_pos == FILAMENT_POS_UNLOADED

        # Selecting a different gate with filament loaded requires a crossload-capable MMU
        can_continue = (
            is_unloaded
            or scan_unit is not active_unit
            or active_unit.can_crossload
        )
        if not can_continue:
            if self.check_if_loaded(): return
            self.mmu.log_error("Operation not possible: Can't crossload on this mmu type")
            return

        mmu.log_always("Scanning NFC tag in %s..." % ("current gate" if gate == current_gate else "gate %d" % gate))
        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_suppress_visual_log():
                    with mmu.wrap_action(ACTION_CHECKING):
                        if gate != current_gate:
                            mmu.select_gate(gate)

                        try:
                            mmu._jog_scan()
                            # Type-B: disable idle gear stepper after the scan
                            mmu.disable_idle_gear_stepper(gate)

                        finally:
                            if mmu.gate_selected != current_gate:
                                # Restore previous gate if necessary or easy
                                if mmu.is_in_print() or active_unit.multigear or filament_pos != FILAMENT_POS_UNLOADED:
                                    mmu.select_gate(current_gate)
                                else:
                                    # Lazy gate reselection - side effect of changed tool/gate
                                    mmu.gate_maps.ensure_ttg_match()
                                    mmu.initialize_encoder() # Encoder 0000

        except MmuError as ee:
            mmu.handle_mmu_error("NFC scan for gate %d failed: %s" % (gate, str(ee)))
