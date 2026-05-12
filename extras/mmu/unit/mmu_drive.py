# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal:
# Wrapper around mmu_stepper to provides for different drive states of MMU gear and the printer extruder
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ...mmu_stepper  import MmuStepper
from ..mmu_constants import *


class MmuDrive(MmuStepper):

    def __init__(self, config, mmu_unit, mmu_extruder_stepper):
        self.config = config
        self._config_name = config.get_name()
        self.mmu_unit = mmu_unit                         # This physical MMU unit
        self.mmu_extruder_stepper = mmu_extruder_stepper # ExtruderStepper connected to this mmu drive (gear)

        MmuStepper.__init__(self, config, default_mode='manual')

        # Initially setup as controlling the unsynced gear stepper
        self._sync_mode = DRIVE_UNSYNCED
        self._driving_stepper = self


    def sync_mode(self, mode):
        if mode == self._sync_mode:
            return False

        logging.info(f"PAUL: sync_mode({mode})")

        if mode not in DRIVE_MODE_NAMES:
            raise self.printer.command_error(f"Invalid MMU drive sync mode: {mode}")

        current_pos = self._driving_stepper.get_mode_position()

        # --------------------------------------------------------------
        # DRIVE_UNSYNCED
        # --------------------------------------------------------------
        if mode == DRIVE_UNSYNCED:
            self.switch_to_manual_mode()
            self.do_set_position(current_pos)
            self.mmu_extruder_stepper.switch_to_extruder_mode()
            self._driving_stepper = self

        # --------------------------------------------------------------
        # DRIVE_EXTRUDER_SYNCED_TO_GEAR (gear leading, extruder following)
        # --------------------------------------------------------------
        elif mode == DRIVE_EXTRUDER_SYNCED_TO_GEAR:
            self.switch_to_manual_mode()
            self.do_set_position(current_pos)
            self.mmu_extruder_stepper.switch_to_manual_mode()
            self.mmu_extruder_stepper.sync_to_manual_stepper(self._config_name)
            self._driving_stepper = self

        # --------------------------------------------------------------
        # DRIVE_EXTRUDER_ONLY
        # --------------------------------------------------------------
        elif mode == DRIVE_EXTRUDER_ONLY:
            self.switch_to_manual_mode()
            self.mmu_extruder_stepper.switch_to_manual_mode()
            self.mmu_extruder_stepper.do_set_position(current_pos)
            self._driving_stepper = self.mmu_extruder_stepper

        # --------------------------------------------------------------
        # DRIVE_GEAR_SYNCED_TO_EXTRUDER (extruder leading, gear following)
        # --------------------------------------------------------------
        elif mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
            self.mmu_extruder_stepper.switch_to_extruder_mode()
            self.switch_to_extruder_mode()
            self.sync_to_extruder(self.mmu_extruder_stepper.get_name())
            self._driving_stepper = self.mmu_extruder_stepper

        self._sync_mode = mode
        return True


    def is_synced_to_extruder(self):
        return (self._sync_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER)


    def set_filament_position(self, pos):
        self._driving_stepper.do_set_position(pos)


    def get_filament_position(self):
        return self._driving_stepper.get_mode_position()


    def get_live_filament_position(self):
        """
        Return the approximate live filament position for dynamic feedback of position
        """
        mcu_stepper = self._driving_stepper.stepper
        mcu_pos = mcu_stepper.get_mcu_position()
        return mcu_pos * mcu_stepper.get_step_dist()


    # Replace get_status for succinct info pertinent to control of filament movement
    def get_status(self, eventtime):
        return {
            "sync_mode": self._sync_mode,
            "sync_mode_name": DRIVE_MODE_NAMES[self._sync_mode],
            "drive_stepper": self._driving_stepper.full_name,
            "filament_position": self._driving_stepper.get_mode_position(),
        }
