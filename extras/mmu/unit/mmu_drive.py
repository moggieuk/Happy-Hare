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

        # Resolved at connect. None until then, and None for a stepper with no TMC
        self._tmc = None
        self._default_current = None
        self._run_current_percent = 100

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)


    def handle_connect(self):
        self.mmu = self.mmu_machine.mmu_controller # Master MMU controller

        # Our own driver, found by stepper name. Runs before MmuUnit.handle_connect, so the
        # unit's per-gate accessors read back through here rather than resolving separately
        for chip in TMC_CHIPS:
            c = self.printer.lookup_object("%s mmu_stepper %s" % (chip, self.name), None)
            if c is not None:
                self._tmc = c
                self._default_current = c.get_status(0).get("run_current")
                break


    def reinit(self):
        # Record only. Runs during config load, before handle_connect, so nothing here may
        # touch self.mmu. Enable/disable restores the driver to its configured default, so
        # forgetting what we believed is the whole job
        self._run_current_percent = 100


    def tmc_obj(self):
        return self._tmc


    def default_current(self):
        return self._default_current


    def run_current_percent(self):
        return self._run_current_percent


    def set_run_current_percent(self, percent):
        self._run_current_percent = percent


    def sync_mode(self, mode):
        prev_mode = self._sync_mode
        if mode == prev_mode:
            return False

        if mode not in DRIVE_MODE_NAMES:
            raise self.printer.command_error(f"Invalid MMU drive sync mode: {mode}")

        self.mmu.log_stepper(f"sync_mode({DRIVE_MODE_NAMES[mode]}) for {self.name}")

        # Before resetting the driving stepper, capture it's position
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
            self.mmu_extruder_stepper.do_set_position(current_pos) # To allow rebasing mode_pos
            self.mmu_gear_stepper.switch_to_extruder_mode()
            self.mmu_gear_stepper.sync_to_extruder(self.mmu_extruder_stepper.get_name())
            self._driving_stepper = self.mmu_extruder_stepper

        self._sync_mode = mode

        # Send correct sync event
        if mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
            self.printer.send_event("mmu:synced")
        elif prev_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
            self.printer.send_event("mmu:unsynced")

        return True


    def get_sync_mode(self):
        return self._sync_mode


    def get_name(self):
        return self.name


    def is_synced_to_extruder(self):
        return (self._sync_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER)


    def set_filament_position(self, pos):
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
        """
        Check if endstop name exists on driving stepper
        Caution: sync mode needs to be correct before calling this
        """
        return self._driving_stepper.rail.has_endstop(endstop_name)


    def get_endstop(self, endstop_name):
        """
        Returns just the endstop obj. Not (endstop, name) tuple on driving stepper
        Caution: sync mode needs to be correct before calling this
        """
        endstop, _ = self._driving_stepper.rail.get_homing_endstops(endstop_name)[0]
        return endstop


    def get_extra_endstop_names(self):
        """
        Return extra endstops registered on driving stepper
        Caution: sync mode needs to be correct before calling this
        """
        return self._driving_stepper.rail.get_extra_endstop_names()


    def is_endstop_virtual(self, endstop):
        """
        Check if endstop is virtual (stallguard)
        Caution: sync mode needs to be correct before calling this
        """
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
            home_result = self._driving_stepper.do_homing_move(
                target_pos, speed, accel,
                probe_pos=True,
                triggered=(homing_move > 0),
                check_trigger=True,
                endstop_name=endstop_name)

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


