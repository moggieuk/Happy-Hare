# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal:
# Wrapper around mmu_stepper to provides for different drive states of MMU gear and the printer extruder
# (This is designed to abstract the mmu_stepper which should not really be accessed directly)
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, time

# Klipper imports
import chelper

# Happy Hare imports
from ..mmu_constants import *


class MmuDrive():

    def __init__(self, config, mmu_unit, mmu_gear_stepper, mmu_extruder_stepper):
        self.printer = config.get_printer()
        self.name = mmu_gear_stepper.get_name()
        self.mmu_unit = mmu_unit                         # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine          # Entire Logical combined MMU
        self.mmu_extruder_stepper = mmu_extruder_stepper # ExtruderStepper connected to this mmu drive
        self.mmu_gear_stepper = mmu_gear_stepper

        # Initially setup as controlling the unsynced gear stepper
        self._sync_mode = DRIVE_UNSYNCED
        self._driving_stepper = self.mmu_gear_stepper

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Master MMU controller


    def sync_mode(self, mode):
        if mode == self._sync_mode:
            return False

        self.mmu.log_stepper(f"sync_mode({mode}) for {self.name}")

        if mode not in DRIVE_MODE_NAMES:
            raise self.printer.command_error(f"Invalid MMU drive sync mode: {mode}")

        current_pos = self._driving_stepper.get_mode_position()

        # ------------------------------------------------------------------
        # DRIVE_UNSYNCED
        # ------------------------------------------------------------------
        if mode == DRIVE_UNSYNCED:
            self.mmu_gear_stepper.switch_to_manual_mode()
            self.mmu_gear_stepper.do_set_position(current_pos)
            self.mmu_extruder_stepper.switch_to_extruder_mode()
            self._driving_stepper = self.mmu_gear_stepper

        # ------------------------------------------------------------------
        # DRIVE_EXTRUDER_SYNCED_TO_GEAR (gear leading, extruder following)
        # ------------------------------------------------------------------
        elif mode == DRIVE_EXTRUDER_SYNCED_TO_GEAR:
            self.mmu_gear_stepper.switch_to_manual_mode()
            self.mmu_gear_stepper.do_set_position(current_pos)
            self.mmu_extruder_stepper.switch_to_manual_mode()
            self.mmu_extruder_stepper.sync_to_manual_stepper(self.mmu_gear_stepper.get_name())
            self._driving_stepper = self.mmu_gear_stepper

        # ------------------------------------------------------------------
        # DRIVE_EXTRUDER_ONLY
        # ------------------------------------------------------------------
        elif mode == DRIVE_EXTRUDER_ONLY:
            self.mmu_gear_stepper.switch_to_manual_mode()
            self.mmu_extruder_stepper.switch_to_manual_mode()
            self.mmu_extruder_stepper.do_set_position(current_pos)
            self._driving_stepper = self.mmu_extruder_stepper

        # ------------------------------------------------------------------
        # DRIVE_GEAR_SYNCED_TO_EXTRUDER (extruder leading, gear following)
        # ------------------------------------------------------------------
        elif mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
            self.mmu_extruder_stepper.switch_to_extruder_mode()
            self.mmu_gear_stepper.switch_to_extruder_mode()
            self.mmu_gear_stepper.sync_to_extruder(self.mmu_extruder_stepper.get_name())
            self._driving_stepper = self.mmu_extruder_stepper

        self._sync_mode = mode
        return True


    def get_name(self):
        return self.name


    def is_synced_to_extruder(self):
        return (self._sync_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER)


    def set_filament_position(self, pos):
        self.mmu.log_warning(f"PAUL: {self._driving_stepper.get_name()}.do_set_position({pos})")
        self._driving_stepper.do_set_position(pos)


    def get_filament_position(self):
        return self._driving_stepper.get_mode_position()


    def get_live_filament_position(self):
        """
        Return the approximate live (non-based) filament position for dynamic feedback of position
        This is a non-based measurement so only useful for relative movement tracking
        """
        mcu_stepper = self._driving_stepper.stepper
        mcu_pos = mcu_stepper.get_mcu_position()
        return mcu_pos * mcu_stepper.get_step_dist()


    def driving_stepper(self):
        return self._driving_stepper


    def has_endstop(self, endstop_name):
        return self._driving_stepper.rail.has_endstop(endstop_name)


    def get_extra_endstop_names(self):
        return self._driving_stepper.rail.get_extra_endstop_names()


    def is_endstop_virtual(self, endstop):
        return self._driving_stepper.rail.is_endstop_virtual(endstop)


    def set_gear_direction(self, direction):
        """
        Changes direction of rail. Useful for some MMU designs like
        3DChameleon or for saved direction calibration
        """
        self.mmu_gear_stepper.stepper.set_dir_inverted(direction)


    def move(self, dist, speed, accel, homing_move=0, endstop_name="default"):
        """
        Execute a relative move on the driving MmuStepper
        Returns: actual, homed
        """
        start_pos = self._driving_stepper.get_mode_position()
        target_pos = start_pos + dist

        if homing_move != 0:
            home_result = self._driving_stepper.do_homing_move(target_pos, speed, accel, probe_pos=True, triggered=(homing_move > 0), check_trigger=True, endstop_name=endstop_name)

            halt_pos = self._driving_stepper.get_mode_position()
            actual = halt_pos - start_pos
            homed = True

            try:
                if self._driving_stepper.rail.is_endstop_virtual(endstop_name):
                    trig_rel = home_result["trig_pos"] - start_pos
                    # Stallguard doesn't do well at slow speed. Try to infer move completion
                    if abs(trig_rel - dist) < 1.0:
                        homed = False
            except Exception:
                pass

            return actual, homed

        self._driving_stepper.do_move(target_pos, speed, accel)
        return dist, False


    # Replace get_status for succinct info pertinent to control of filament movement
    def get_status(self, eventtime):
        return {
            "sync_mode": self._sync_mode,
            "sync_mode_name": DRIVE_MODE_NAMES[self._sync_mode],
            "drive_stepper": self._driving_stepper.full_name,
            "filament_position": self._driving_stepper.get_mode_position(),
        }


