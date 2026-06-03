# aappy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Mixin base class for MMU that handles all filament movement and control
#
# Basic load steps:
#   [preload_gate]
#   load_gate
#   load_bowden
#   [home_to_extruder]
#   load_extruder
#   purge
#
# Basic unload steps:
#   form_tip
#   unload_extruder
#   unload_bowden
#   unload_gate
#   [eject_from_gate
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import contextlib
import logging
import math

# Klipper imports
from ..homing             import HomingMove

# Happy Hare imports
from .mmu_constants       import *
from .mmu_utils           import MmuError


class MmuFilamentMovement:

# -----------------------------------------------------------------------------------------------------------
# MODULAR FILAMENT LOAD / UNLOAD STEPS
# -----------------------------------------------------------------------------------------------------------

    def _preload_gate(self):
        """
        Preload filament at the selected gate using the least invasive strategy available.

        Returns:
            None. Updates gate state in place or raises MmuError when preload fails.
        """
        u = self.mmu_unit()

        gate_exit_sensor = self.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, self.gate_selected)
        if gate_exit_sensor is not None:
            if gate_exit_sensor:
                self.log_always("Filament already preloaded")
                self.gate_maps.set_gate_status(self.gate_selected, GATE_AVAILABLE)
                return
            else:
                # Minimal load to mmu exit sensor
                self.log_always("Preloading...")
                endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, self.gate_selected)
                msg = "Homing to %s sensor" % endstop_name
                with self.wrap_suspend_filament_monitoring():
                    actual, homed, measured, _ = self.move_filament(msg, u.p.gate_preload_homing_max, motor="gear", homing_move=1, endstop_name=endstop_name)
                    if homed:
                        self.move_filament("Parking", u.p.gate_preload_parking_distance)
                        self.gate_maps.set_gate_status(self.gate_selected, GATE_AVAILABLE)
                        self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                        self.log_always("Filament detected and loaded in gate %d" % self.gate_selected)
                        return

            if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, self.gate_selected):
                self.gate_maps.set_gate_status(self.gate_selected, GATE_UNKNOWN)
                self.log_warning(
                    f"Filament detected by entry sensor on gate {self.gate_selected} but didn't reach "
                    "the exit sensor. Perhaps increase 'gate_preload_homing_max'"
                )
                return

        else:
            # Full gate load if no mmu exit sensor
            for _ in range(u.p.gate_preload_attempts):
                self.log_always("Loading...")
                try:
                    self._load_gate(allow_retry=False)
                    self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                    self.log_always("Parking...")
                    self._unload_gate()
                    self.log_always("Filament detected and parked in gate %d" % self.gate_selected)
                    return
                except MmuError as ee:
                    # Exception just means filament is not loaded yet, so continue
                    self.log_trace("Exception on preload: %s" % str(ee))

            if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, self.gate_selected):
                self.gate_maps.set_gate_status(self.gate_selected, GATE_UNKNOWN)
                self.log_warning(f"Filament detected by entry sensor on gate {self.gate_selected} but was not able to complete preload")
                return

        self.gate_maps.set_gate_status(self.gate_selected, GATE_EMPTY)
        raise MmuError("Filament not detected")


    def _eject_from_gate(self):
        """
        Fully eject filament from a gate so it can be removed safely. Note that this operates on
        current gate but it should not change filament_pos state because gate may switch back

        Args:
            gate: Gate index to operate on. When omitted, the current selection is used.

        Returns:
            None. Updates gate and filament state in place.
        """
        u = self.mmu_unit()
        gate = self.gate_selected

        self.log_always("Ejecting...")
        if (
            self.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate) and
            self.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, gate)
        ):
            endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)
            msg = "Reverse homing off %s sensor" % endstop_name
            actual, homed, measured, _ = self.move_filament(msg, -u.p.gate_homing_max, motor="gear", homing_move=-1, endstop_name=endstop_name)
            if homed:
                self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
            else:
                raise MmuError("Filament did not exit gate homing sensor: %s" % endstop_name)

        final_move = abs(u.p.gate_final_eject_distance)
        if final_move > 0:
            msg = "Ejecting filament out of gate"
            if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate) is not None:
                # Use homing move so we don't "over eject"
                self.move_filament(msg, -u.p.gate_final_eject_distance, motor="gear", homing_move=-1, endstop_name=SENSOR_ENTRY_PREFIX, wait=True)
            else:
                self.move_filament(msg, -u.p.gate_final_eject_distance, wait=True)
        else:
            self.log_trace("No final eject, gate_final_eject_distance is 0")

        self.gate_maps.set_gate_status(gate, GATE_EMPTY)
        self.log_always("The filament in gate %d can be removed" % gate)


    def _load_gate(self, allow_retry=True):
        """
        Load filament into gate. This is considered the starting position for the rest of the filament loading
        process. Note that this may overshoot the home position for the "encoder" technique but subsequent
        bowden move will accommodate. Also for systems with gate sensor and encoder with gate sensor first,
        there will be a gap in encoder readings that must be taken into consideration.

        Args:
            allow_retry: Whether retry attempts are allowed when the first load attempt fails.

        Returns:
            float: Overshoot past the gate homing point that later stages should account for.
        """
        u = self.mmu_unit()

        self._validate_gate_config("load")
        self.set_filament_direction(DIRECTION_LOAD)
        retries = u.p.gate_load_retries if allow_retry else 1

        if u.p.gate_homing_endstop == SENSOR_ENCODER:
            with self.require_encoder():
                measured = 0.
                for i in range(retries):
                    msg = "Initial load into encoder" if i == 0 else ("Retry load into encoder (retry #%d)" % i)
                    _, _, m, _ = self.move_filament(msg, u.p.gate_homing_max)
                    measured += m
                    if m > 6.0:
                        self.gate_maps.set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                        self.set_filament_pos_state(FILAMENT_POS_START_BOWDEN)
                        return measured
                    else:
                        self.log_debug("Error loading filament - filament motion was not detected by the encoder. %s" % ("Retrying..." if i < retries - 1 else ""))
                        if i < retries - 1:
                            self.selector().filament_release()

        else:  # Gate sensor... SENSOR_SHARED_EXIT is shared, but SENSOR_EXIT_PREFIX is gate specific (can also be SENSOR_EXTRUDER_ENTRY for no bowden designs)
            for i in range(retries):
                endstop_name = self.sensor_manager.get_qualified_endstop_name(u.p.gate_homing_endstop)
                msg = ("Initial homing to %s sensor" % endstop_name) if i == 0 else ("Retry homing to gate sensor (retry #%d)" % i)
                h_dir = -1 if u.p.gate_parking_distance < 0 and self.sensor_manager.check_sensor(endstop_name) else 1
                actual, homed, measured, _ = self.move_filament(msg, h_dir * u.p.gate_homing_max, motor="gear", homing_move=h_dir, endstop_name=endstop_name)
                if homed:
                    self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
                    self.gate_maps.set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                    self.set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    return 0.
                else:
                    self.log_debug("Error loading filament - filament did not reach gate homing sensor. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self.selector().filament_release()

        self.gate_maps.set_gate_status(self.gate_selected, GATE_EMPTY)
        self.set_filament_pos_state(FILAMENT_POS_UNLOADED)
        msg = "Couldn't pick up filament at gate"
        if u.p.gate_homing_endstop == SENSOR_ENCODER:
            msg += " (encoder didn't report enough movement)"
        else:
            msg += " (gate endstop didn't trigger)"
        msg += "\nGate marked as empty. Use 'MMU_GATE_MAP GATE=%d AVAILABLE=1' to reset" % self.gate_selected
        raise MmuError(msg)


    def _unload_gate(self, extra_homing=0.):
        """
        Unload filament through the gate to the final parked MMU position.

        Args:
            extra_homing: Additional homing distance budget. None indicates recovery mode.

        Returns:
            Actual homing distance required to reach gate homing point (excludes parking movement)
        """
        u = self.mmu_unit()

        self._validate_gate_config("unload")
        self.set_filament_direction(DIRECTION_UNLOAD)

        # Figure out homing buffer
        recovery = False
        if extra_homing is None: # Means recovery operation
            recovery = True
            expected_homing = u.calibrator.get_bowden_length()
        else:
            expected_homing = extra_homing
        homing_max = expected_homing + u.p.gate_homing_max

        if recovery:
            # Safety step because this method is used as a defensive way to unload the entire bowden from unknown position
            # It handles the cases of filament still in extruder with no toolhead sensor or, if toolhead sensor is available,
            # the small window where filament is between extruder entrance and toolhead sensor
            length = u.toolhead_wrapper.p.toolhead_extruder_to_nozzle
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                length -= u.toolhead_wrapper.p.toolhead_sensor_to_nozzle # Can safely reduce the base move distance because starting point in toolhead sensor
            length += u.p.toolhead_unload_safety_margin    # Add safety margin

            self.log_debug("Performing synced pre-unload bowden move of %.1fmm to ensure filament is not trapped in extruder" % length)

            if u.p.gate_homing_endstop == SENSOR_ENCODER:
                _,_,_,_ = self.move_filament("Bowden safety pre-unload move", -length, motor="gear+extruder")

            else:
                endstop_name = self.sensor_manager.get_qualified_endstop_name(u.p.gate_homing_endstop)
                homing_movement, homed, _, _ = self.move_filament(
                    "Bowden safety pre-unload move",
                    -length,
                    motor="gear+extruder",
                    homing_move=-1,
                    endstop_name=endstop_name,
                )

                # In case we ended up homing during the safety pre-unload, lets just do our parking and be done
                # This can easily happen when your parking distance is configured to park the filament past the
                # gate sensor instead of behind the gate sensor and the filament position is determined to be
                # "somewhere in the bowden tube"
                if homed:
                    self.set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    self.move_filament("Final parking", u.p.gate_parking_distance)
                    self.set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return homing_movement

        if u.p.gate_homing_endstop == SENSOR_ENCODER:

            with self.require_encoder():
                if recovery:
                    self.log_info("Slowly unloading bowden because unsure of filament position...")
                else:
                    self.log_trace("Unloading gate using the encoder")

                success = self._reverse_home_to_encoder(homing_max)
                if success:
                    homing_movement, homing_overshoot = success
                    self.log_debug(f"Found encoder endstop after moving {homing_movement:.1f}mm with {homing_overshoot:.1f}mm overshoot")
                    parking_distance = u.p.gate_parking_distance - homing_overshoot # homing_overshoot will always be -ve (retraction)
                    _, _, measured, _ = self.move_filament("Final parking", parking_distance)

                    # We don't expect any movement of the encoder unless it is free-spinning
                    if measured > self.encoder().movement_min(): # We expect 0, but relax the test a little (allow one pulse)
                        self.log_warning("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                    self.set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return homing_movement

                msg = "did not clear the encoder after moving %.1fmm" % homing_max

        else:  # Using mmu_shared_exit or mmu_exit_N sensor

            # Precaution: reverse home off gate sensor
            endstop_name = self.sensor_manager.get_qualified_endstop_name(u.p.gate_homing_endstop)
            homing_movement, homed, _, _ = self.move_filament(
                f"Reverse homing off {endstop_name} sensor",
                -homing_max,
                motor="gear",
                homing_move=-1,
                endstop_name=endstop_name,
            )
            if homed:
                self.set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                self.move_filament("Final parking", u.p.gate_parking_distance)
                self.set_filament_pos_state(FILAMENT_POS_UNLOADED)
                return homing_movement

            msg = "did not home to sensor '{u.p.gate_homing_endstop}' after moving {homing_max:.1f}mm" % (u.p.gate_homing_endstop, homing_max)

        raise MmuError("Failed to unload gate because %s" % msg)


    def _reverse_home_to_encoder(self, homing_max):
        """
        Step filament backward until it clears the encoder or the move budget is exhausted.

        Args:
            homing_max: Maximum distance available for the homing operation.

        Returns:
            tuple[float, float] or None: Actual homing movement, final distance from home (overshoot) or None if the filament didn't clear the encoder
        """
        u = self.mmu_unit()

        step_size = u.p.encoder_move_step_size
        max_steps = int(math.ceil(homing_max / step_size))
        homing = 0.0

        for i in range(max_steps):
            sactual, _, _, sdelta = self.move_filament(f"Unloading step #{i + 1} from encoder", -step_size,)
            homing -= sactual

            # Large enough step delta here means we are out of the encoder
            if sdelta >= step_size * 0.2:  # 20 %
                homing -= sdelta # Reduce homing movement by the overshoot
                return homing, -sdelta

        self.log_debug(f"Filament did not clear encoder even after moving {step_size * max_steps:.1f}mm")
        return None


    def _validate_gate_config(self, direction):
        """
        Validate that the configured gate homing mechanism is supported and available.

        Args:
            direction: Operation direction label used in validation error messages.

        Returns:
            None. Raises MmuError when the gate configuration is invalid.
        """
        u = self.mmu_unit()

        if u.p.gate_homing_endstop == SENSOR_ENCODER:
            if not self.has_encoder():
                raise MmuError("Attempting to %s encoder but encoder is not configured on MMU!" % direction)
        elif u.p.gate_homing_endstop in GATE_ENDSTOPS:
            sensor = u.p.gate_homing_endstop
            if u.p.gate_homing_endstop == SENSOR_EXIT_PREFIX:
                sensor += "_%d" % self.gate_selected
            if not self.sensor_manager.has_sensor(sensor):
                raise MmuError("Attempting to %s gate but sensor '%s' is not configured on MMU!" % (direction, sensor))
        else:
            raise MmuError("Unsupported gate endstop %s" % u.p.gate_homing_endstop)


    def _must_home_to_extruder(self):
        """
        Determine whether a dedicated extruder homing step is required before loading.

        Returns:
            bool: True when a dedicated extruder homing move is required before final loading.
        """
        u = self.mmu_unit()

        has_extruder_endstop = u.p.extruder_homing_endstop != SENSOR_EXTRUDER_NONE
        force_homing = u.p.extruder_force_homing
        toolhead_sensor_missing = not self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)

        return has_extruder_endstop and (force_homing or toolhead_sensor_missing)


    def _load_bowden(self, length=None, start_pos=0.):
        """
        Perform the fast bowden load portion between the gate and the extruder area.
        Usually the full length but if 'full' is False a specific length can be specified
        Note that filament position will be measured from the gate "parking position" and so
        will be the gate_parking_distance plus any overshoot.
        The start of the bowden move is from the parking homing point.

        Args:
            length: Requested bowden travel distance. When omitted, the calibrated bowden length is used.
            start_pos: Distance already consumed before the bowden move begins, such as gate overshoot with encoder

        Returns:
            tuple[float, float]:
              - Ratio of encoder measured load movement to commanded movement, used for calibration and telemetry (None for invalid or no encoder)
              - The distance not yet moved and reserved for homing buffer in later stages.
        """
#        self.log_warning(f"PAUL: _load_bowden(length={length}, start_pos={start_pos})")
#        self.log_warning(f"PAUL: S_BOWDEN filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
        u = self.mmu_unit()

        bowden_length = u.calibrator.get_bowden_length()
        if length is None:
            length = bowden_length

        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance

        full = (length == bowden_length)

        # Compensate for distance already moved for gate homing endstop (e.g. overshoot after encoder based gate homing)
        length -= start_pos

        try:
            # Do we need to reduce by buffer amount to ensure we don't overshoot homing sensor
            extruder_homing_buffer = 0.
            if full:
                # We will need some buffer space if we are intending to home to extruder (or toolhead sensor)
                if self._must_home_to_extruder() or self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                    # Determine how much to reduce the fast move portion to leave room for homing
                    extruder_homing_buffer = u.p.bowden_load_homing_buffer

                    # If homing to extruder entry sensor, Further reduce to compensate for distance from sensor to extruder gear
                    # (because the bowden length is always recorded as distance to extruder gear)
                    if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                        extruder_homing_buffer -= u.toolhead_wrapper.p.toolhead_entry_to_extruder

                length -= extruder_homing_buffer # Reduce fast move distance

            ratio = None
            if length > 0:
                self.log_debug("Loading bowden tube")
                self.set_filament_direction(DIRECTION_LOAD)
                self.selector().filament_drive() # Ensure encoder reading is not distrubed by initial filament gipping
                self.set_filament_pos_state(FILAMENT_POS_START_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = (self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_bowden_position()) - start_pos

                if self.gate_selected > 0 and not u.calibrator.is_gear_rd_calibrated():
                    self.log_warning("Warning: gate %d not calibrated! Using default rotation distance" % self.gate_selected)

                # "Fast" load
                _, _, _, delta = self.move_filament("Fast loading move through bowden", length, track=True, encoder_dwell=bool(u.p.autotune_encoder))
                delta -= self.get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self.can_use_encoder() and delta >= length * (u.p.bowden_move_error_tolerance / 100.) and not self.calibrating:
                    raise MmuError("Failed to load bowden. Perhaps filament is stuck in gate. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                # Encoder based validation test
                if self.can_use_encoder() and delta >= u.p.bowden_allowable_encoder_delta and not self.calibrating:
                    ratio = None # Not considered valid for auto-calibration
                    # Correction attempts to load the filament according to encoder reporting
                    if u.p.bowden_apply_correction:
                        for i in range(2):
                            if delta >= u.p.bowden_allowable_encoder_delta:
                                msg = "Correction load move #%d into bowden" % (i+1)
                                _,_,_,d = self.move_filament(msg, delta, track=True)
                                delta = d
                                self.log_debug("Correction load move was necessary, encoder now measures %.1fmm" % self.get_encoder_distance())
                            else:
                                self.log_debug(
                                    "Correction load complete, "
                                    f"delta {delta:.1f}mm is less than "
                                    f"'bowden_allowable_encoder_delta' ({u.p.bowden_allowable_encoder_delta:.1f}mm)"
                                )
                                break
                        self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                        if delta >= u.p.bowden_allowable_encoder_delta:
                            self.log_warning(
                                "Warning: Excess slippage was detected in bowden tube load after correction moves. "
                                f"Gear moved {length:.1f}mm, Encoder measured {length - delta:.1f}mm. "
                                "See mmu.log for more details"
                            )
                    else:
                        self.log_warning(
                            "Warning: Excess slippage was detected in bowden tube load but "
                            "'bowden_apply_correction' is disabled. "
                            f"Gear moved {length:.1f}mm, Encoder measured {length - delta:.1f}mm. "
                            "See mmu.log for more details"
                        )

                    if delta >= u.p.bowden_allowable_encoder_delta:
                        self.log_debug(
                            "Possible causes of slippage:\n"
                            "Calibration ref length too long (hitting extruder gear before homing)\n"
                            "Calibration ratio for gate is not accurate\n"
                            "MMU gears are not properly gripping filament\n"
                            "Encoder reading is inaccurate\n"
                            "Faulty servo"
                        )

                self._random_failure() # Testing
                self.movequeue_wait()

            if full:
                self.set_filament_pos_state(FILAMENT_POS_END_BOWDEN)

            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = None

#            self.log_warning(f"PAUL: E_BOWDEN filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
#            self.log_warning(f"PAUL: _load_bowden() => (ratio={ratio}, extruder_homing_buffer={extruder_homing_buffer})")
            return ratio, extruder_homing_buffer

        finally:
            self.bowden_start_pos = None


    def _unload_bowden(self, length=None):
        """
        Perform the fast bowden unload portion from the extruder side back toward the MMU.

        Args:
            length: Requested bowden travel distance. When omitted, the calibrated bowden length is used.

        Returns:
            tuple[float, float]:
              - Ratio of measured unload movement to commanded movement, used for calibration and telemetry (None for invalid)
              - The distance not yet moved and reserved for homing buffer in later stages.
        """
#        self.log_warning(f"PAUL: _unload_bowden(lenght={length})")
#        self.log_warning(f"PAUL: S_BOWDEN filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
        u = self.mmu_unit()

        bowden_length = u.calibrator.get_bowden_length()
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Shorten move to provide gate unload buffer used to ensure we don't overshoot homing point
        gate_homing_buffer = u.p.bowden_unload_homing_buffer
        length -= gate_homing_buffer

        try:
            ratio = None
            if length > 0:
                self.log_debug("Unloading bowden tube")
                self.set_filament_direction(DIRECTION_UNLOAD)
                self.selector().filament_drive() # Ensure encoder reading is not distrubed by initial filament gipping

                # Optional pre-unload safety step
                if (full and self.has_encoder() and u.p.bowden_pre_unload_test and
                    self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY) is not False and
                    self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is not False
                ):
                    with self.require_encoder():
                        emss = u.p.encoder_move_step_size
                        self.log_debug("Performing bowden pre-unload test")
                        _, _, _, delta = self.move_filament("Bowden pre-unload test", -emss)
                        if delta > emss * (u.p.bowden_pre_unload_error_tolerance / 100.):
                            self.set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
                            raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder or filament not loaded\nOptionally use MMU_RECOVER to recover filament position")
                        length -= emss
                        self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_bowden_position()

                # Sensor validation
                if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is False:
                    sensors = self.sensor_manager.get_all_sensors()
                    sensor_msg = ''
                    sname = []
                    for name, state in sensors.items():
                        sensor_msg += "%s (%s), " % (name.upper(), "Disabled" if state is None else ("Detected" if state is True else "Empty"))
                        if state is False:
                            sname.append(name)
                    self.log_warning("Warning: Possible sensor malfunction - %s sensor indicated no filament present prior to unloading bowden\nWill ignore and attempt to continue..." % ", ".join(sname))
                    self.log_debug("Sensor state: %s" % sensor_msg)

                # "Fast" unload
                _, _, _, delta = self.move_filament("Fast unloading move through bowden", -length, track=True, encoder_dwell=bool(u.p.autotune_encoder))
                delta -= self.get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self.can_use_encoder() and delta >= u.p.bowden_allowable_encoder_delta and not self.calibrating:
                    ratio = None
                    # Only a warning because _unload_gate() will deal with it
                    self.log_warning("Warning: Excess slippage was detected in bowden tube unload. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                self._random_failure() # Testing
                self.movequeue_wait()

            if full:
                self.set_filament_pos_state(FILAMENT_POS_START_BOWDEN)

            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = None

#            self.log_warning(f"PAUL: E_BOWDEN filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
#            self.log_warning(f"PAUL: _load_bowden() => (ratio={ratio}, gate_homing_buffer={gate_homing_buffer})")
            return ratio, gate_homing_buffer

        finally:
            self.bowden_start_pos = None


    def _home_to_extruder(self, extra_homing=0.):
        """
        Home filament to the extruder gear

        Args:
            extra_homing: Additional homing distance budget. None indicates recovery mode.

        Returns:
            [float or None]
              - Actual homing movement, if applicable required to reach the extruder gear else None
        """
#        self.log_warning(f"PAUL: _home_to_extruder(extra_homing={extra_homing})")
        u = self.mmu_unit()

        self.set_filament_direction(DIRECTION_LOAD)
        measured = 0.
        homing_movement = None
        homing_max = extra_homing + u.p.extruder_homing_max

        if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_NONE:
            # Shouldn't get here
            homed = True

        elif u.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENCODER:
            if self.has_encoder():
                homing_movement, homed, measured, _ = self._home_to_extruder_collision_detection(homing_max)
            else:
                raise MmuError("Cannot home to extruder using 'collision' method because encoder is not configured or disabled!")

        else:
            self.log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (u.p.extruder_homing_endstop, homing_max))
            homing_movement, homed, measured, _ = self.move_filament("Homing filament to extruder endstop", homing_max, motor="gear", homing_move=1, endstop_name=u.p.extruder_homing_endstop)
            if homed:
                self.log_debug("Extruder endstop '%s' reached after %.1fmm (measured %.1fmm)" % (u.p.extruder_homing_endstop, homing_movement, measured))
                self.set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)

                if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                    # Close the fixed gap from the entry sensor to the extruder gear
                    actual, _, measured, _ = self.move_filament("Aligning filament to extruder gear", u.toolhead_wrapper.p.toolhead_entry_to_extruder, motor="gear")
                    homing_movement += actual

                elif u.has_buffer() and u.p.extruder_homing_endstop == SENSOR_COMPRESSION:
                    # Estimate the midpoint of buffer for accurate bowden length determination
                    homing_movement -= (u.buffer.buffer_range / 2.)

        if not homed:
            self.set_filament_pos_state(FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder '%s' endstop after moving %.1fmm" % (u.p.extruder_homing_endstop, homing_max))

        if measured > (homing_max * 0.8):
            self.log_warning("Warning: 80%% of 'extruder_homing_max' was used homing. You may want to increase 'extruder_homing_max'")

        self.set_filament_pos_state(FILAMENT_POS_HOMED_EXTRUDER)

#        self.log_warning(f"PAUL: _home_to_extruder() -> homing_movement={homing_movement}")
        return homing_movement


    def _home_to_extruder_collision_detection(self, homing_max):
        """
        Home to the extruder gear using encoder-based collision detection based on lack of encoder movement

        Args:
            homing_max: Maximum distance available for the homing operation.

        Returns:
            tuple[float, bool, float, float]: Actual move, homing success flag, measured movement, and cumulative encoder delta.
            Note that return Matches move_filament() calls
        """
        u = self.mmu_unit()

        # Lock the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
        ge = stepper_enable.lookup_enable(extruder_stepper.get_name())
        ge.motor_enable(self.toolhead.get_last_move_time())

        step = u.p.extruder_collision_homing_step * math.ceil(self.encoder().get_resolution() * 10) / 10
        self.log_debug("Homing to extruder gear, up to %.1fmm in %.1fmm steps" % (homing_max, step))

        with self.wrap_gear_current(u.p.extruder_collision_homing_current, "for collision detection"):
            homed = False
            measured = delta = 0.
            i = 0
            for i in range(int(homing_max / step)):
                msg = "Homing step #%d" % (i+1)
                _,_,smeasured,sdelta = self.move_filament(msg, step, speed=u.p.gear_homing_speed)
                measured += smeasured
                delta += sdelta
                if sdelta >= self.encoder().movement_min() or abs(delta) > step: # Not enough or strange measured movement means we've hit the extruder
                    homed = True
                    measured -= step # Subtract the last step to improve accuracy
                    break
            self.log_debug("Extruder entrance%s found after %.1fmm move (%d steps), encoder measured %.1fmm (delta %.1fmm)"
                    % (" not" if not homed else "", step*(i+1), i+1, measured, delta))

        if delta > 5.0:
            self.log_warning("Warning: A lot of slippage was detected whilst homing to extruder, you may want to reduce 'extruder_collision_homing_current' and/or ensure a good grip on filament by gear drive")

        self.drive().set_filament_position(self.drive().get_filament_position() - step) # Ignore last step movement
        return step*i, homed, measured, delta


    def _load_extruder(self, extruder_only=False, extra_homing=0.):
        """
        Advance filament from the extruder entrance region to the nozzle.

        Args:
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.
            extra_homing: Additional homing distance budget. None indicates recovery mode.

        Returns:
            float or None: Additional bowden movement inferred from toolhead-sensor homing, or None when not applicable.
        """
#        self.log_warning(f"PAUL: _load_extruder(extruder_only={extruder_only}, extra_homing={extra_homing})")
        u = self.mmu_unit()

        with self.wrap_action(ACTION_LOADING_EXTRUDER):
            self.log_debug("Loading filament into extruder")
            self.set_filament_direction(DIRECTION_LOAD)

            # Important to wait for filaments with wildly different print temps. In practice, the time taken
            # to perform a swap should be adequate to reach the target temp but better safe than sorry
            self._ensure_safe_extruder_temperature(wait=True)
            bowden_extra = None

            has_tension, has_compression, has_proportional = u.sync_feedback.get_active_sensors()
            has_toolhead = self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)

            synced = not extruder_only
            if synced:
                speed = self.p.extruder_sync_load_speed
                motor = "gear+extruder"
            else:
                speed = self.p.extruder_load_speed
                motor = "extruder"

            fhomed = False

            if has_toolhead:
                # With toolhead sensor for best accuracy we always first home to toolhead sensor past the extruder entrance
                # The remaining load distance is then relative to the toolhead sensor not extruder gears/entrance
                if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")

                homing_max = extra_homing + u.p.toolhead_homing_max
                self.log_debug("Homing up to %.1fmm to toolhead sensor%s" % (homing_max, (" (synced)" if synced else "")))
                actual, fhomed, measured, _ = self.move_filament("Homing to toolhead sensor", homing_max, motor=motor, homing_move=1, endstop_name=SENSOR_TOOLHEAD)

                if fhomed:
                    self.set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                    # Bowden part of move is homing distance minus the distance between entrance and toolhead sensor
                    bowden_extra = max(actual - (u.toolhead_wrapper.p.toolhead_extruder_to_nozzle - u.toolhead_wrapper.p.toolhead_sensor_to_nozzle), 0)
                else:
                    if self.gate_selected != TOOL_GATE_BYPASS:
                        self.set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    else:
                        # For bypass its best to assume we didn't enter the extruder at all
                        self.set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % u.p.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure reduction is used only one time
            d = u.toolhead_wrapper.p.toolhead_sensor_to_nozzle if has_toolhead else u.toolhead_wrapper.p.toolhead_extruder_to_nozzle
            length = max(d - u.extruder_wrapper.filament_remaining - u.toolhead_wrapper.p.toolhead_residual_filament - u.toolhead_wrapper.p.toolhead_ooze_reduction - self.toolchange_retract, 0)

            # If we have a compression sensor indicating compression we can detect failure in the critical extruder entrance transition
            # by performing the initial load with just the extruder motor and checking that the sensor un-triggers before continuing
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and u.p.toolhead_entry_tension_test
                and synced
                and not has_toolhead
                and self.sensor_manager.check_sensor(SENSOR_COMPRESSION)
            ):
                max_range = u.buffer.buffer_maxrange * 2 # Arbitary but buffer_maxrange is not enough to overcome bowden slack
                if length > max_range:
                    self.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_range)
                    actual, success = u.sync_feedback.adjust_filament_tension(use_gear_motor=False, max_move=max_range)
                    if success:
                        length -= actual
                    else:
                        self.set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                        raise MmuError("Failed to load filament passed the extruder entrance (sync-feedback buffer didn't detect neutral tension)")

            self.log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self.move_filament("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)

            # Update filament model in extruder
            color = (
                self.gate_color[self.gate_selected]
                if self.gate_selected >= 0
                else UNKNOWN_FILAMENT_COLOR
            )
            u.extruder_wrapper.set_filament_remaining(0., color)

            # Encoder based validation test to validate the filament was picked up by extruder. This runs if we are
            # short of deterministic sensors and test makes sense
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and self.can_use_encoder()
                and not fhomed
                and not extruder_only
            ):
                self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                if measured < self.encoder().movement_min():
                    raise MmuError("Move to nozzle failed (encoder didn't sense any movement). Extruder may not have picked up filament or filament did not find homing sensor")
                elif delta > length * (u.p.toolhead_move_error_tolerance / 100.):
                    self.set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
                    raise MmuError("Move to nozzle failed (encoder didn't sense sufficient movement). Extruder may not have picked up filament or filament did not find homing sensor")

            # Make post load filament tension adjustments for reliability. If encoder is fitted, the "post_load_tighten"
            # will aid reliability in subsequent clog detection (and takes prescedence), else "post_load_tention_adjust" will try
            # to neutralize the filament tension. Don't run on bypass gate.
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and not extruder_only
            ):
                if (
                    u.p.toolhead_post_load_tighten
                    and not u.p.sync_to_extruder
                    and self.can_use_encoder()
                    and u.sync_feedback.p.flowguard_encoder_mode
                ):
                    # Tightening move to prevent erroneous encoder clog detection/runout if gear stepper is not synced with extruder
                    with self.wrap_gear_current(percent=50, reason="to tighten filament in bowden"):
                        # Filament will already be gripped so perform fixed MMU only retract
                        cdl = self.encoder().get_clog_detection_length()
                        pullback = min(cdl * u.p.toolhead_post_load_tighten / 100, 15) # % of current clog detection length or 15mm min
                        _,_,measured,delta = self.move_filament("Tighening filament in bowden", -pullback, motor="gear", wait=True)
                        self.log_info("Filament tightened by %.1fmm to prevent false clog detection" % pullback)
                        self.adjust_encoder_distance(-pullback)

                elif (
                    u.p.toolhead_post_load_tension_adjust
                    and (u.p.sync_to_extruder or u.p.sync_purge)
                    and (has_tension or has_compression or has_proportional)
                    and u.sync_feedback.is_enabled()
                ):
                    actual = self._adjust_filament_tension()
                    self.adjust_encoder_distance(actual)

            self._random_failure() # Testing
            self.movequeue_wait()
            self.set_filament_pos_state(FILAMENT_POS_LOADED)
            self.log_debug("Filament should be loaded to nozzle")

#            self.log_warning(f"PAUL: _load_extruder() => bowden_extra={bowden_extra}")
            return bowden_extra # Will only have value if we have toolhead sensor


    def _adjust_filament_tension(self):
        """
        Helper to put filament in neutral tension using sync feedback.
        Two methods are available based on switch only sensors or proportional feedback

        Returns:
            float: The correction distance moved
        """
        actual, success = self.mmu_unit().sync_feedback.adjust_filament_tension()
        if success:
            self.log_info("Filament tension in bowden successfully relaxed")
        else:
            self.log_warning("Unsuccessful in relaxing filament tension after adjusting %.1fmm" % actual)
        return actual


    def _unload_extruder(self, extruder_only=False, validate=True):
        """
        Extract filament past extruder gear (to end of bowden). Assume that tip has already been formed
        and we are parked somewhere in the extruder either by slicer or by stand alone tip creation
        But be careful:
          A poor tip forming routine or slicer could have popped the filament out of the extruder already
        Ending point is either the exit of the extruder or at the extruder (entry) endstop if fitted

        Args:
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.
            validate: Whether encoder-based validation should be performed during unload.

        Returns:
            bool: True when the unload finished in a synced state; otherwise False.
        """
#        self.log_warning(f"PAUL: _unload_extruder(extruder_only={extruder_only}, validate={validate})")
        u = self.mmu_unit()

        with self.wrap_action(ACTION_UNLOADING_EXTRUDER):
            self.log_debug("Extracting filament from extruder")
            self.set_filament_direction(DIRECTION_UNLOAD)

            self._ensure_safe_extruder_temperature(wait=False)

            synced = self.selector().get_filament_grip_state() == FILAMENT_DRIVE_STATE and not extruder_only
            if synced:
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                speed = self.p.extruder_unload_speed
                motor = "extruder"

            fhomed = False
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY) and not extruder_only:
                # BEST Strategy: Extruder exit movement leveraging extruder entry sensor. Must be synced
                synced = True
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"

                if not self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY):
                    if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                        raise MmuError("Toolhead or extruder sensor failure. Extruder sensor reports no filament but toolhead sensor is still triggered")
                    else:
                        self.log_warning("Warning: Filament was not detected by extruder (entry) sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                else:
                    hlength = u.toolhead_wrapper.p.toolhead_extruder_to_nozzle + u.toolhead_wrapper.p.toolhead_entry_to_extruder + u.p.toolhead_unload_safety_margin - u.toolhead_wrapper.p.toolhead_residual_filament - u.toolhead_wrapper.p.toolhead_ooze_reduction - self.toolchange_retract
                    self.log_debug("Reverse homing up to %.1fmm off extruder sensor (synced) to exit extruder" % hlength)
                    _, fhomed, _, _ = self.move_filament("Reverse homing off extruder sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_EXTRUDER_ENTRY)

                if not fhomed:
                    raise MmuError("Failed to reach extruder entry sensor after moving %.1fmm" % hlength)
                else:
                    validate = False
                    # We know exactly where end of filament is so true up
                    self.set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)
                    self.drive().set_filament_position(-(u.toolhead_wrapper.p.toolhead_extruder_to_nozzle + u.toolhead_wrapper.p.toolhead_entry_to_extruder))

                # TODO There have been reports of this failing, perhaps because of klipper's late update of sensor state? Maybe query_endstop instead
                #      So former MmuError() has been changed to error message
                if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                    self.log_warning("Warning: Toolhead sensor still reports filament is present in toolhead! Possible sensor malfunction\nWill attempt to continue...")

            else:
                if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                    # NEXT BEST: With toolhead sensor we first home to toolhead sensor. Optionally synced
                    if not self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                        self.log_warning("Warning: Filament was not detected in extruder by toolhead sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                    else:
                        hlength = u.toolhead_wrapper.p.toolhead_sensor_to_nozzle + u.p.toolhead_unload_safety_margin - u.toolhead_wrapper.p.toolhead_residual_filament - u.toolhead_wrapper.p.toolhead_ooze_reduction - self.toolchange_retract
                        self.log_debug("Reverse homing up to %.1fmm off toolhead sensor%s" % (hlength, (" (synced)" if synced else "")))
                        _, fhomed, _, _ = self.move_filament("Reverse homing off toolhead sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_TOOLHEAD)
                    if not fhomed:
                        raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % hlength)
                    else:
                        validate = False
                        # We know exactly where end of filament is so true up
                        self.set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                        self.drive().set_filament_position(-u.toolhead_wrapper.p.toolhead_sensor_to_nozzle)

                # Finish up with regular extruder exit movement. Optionally synced
                length = max(0, u.toolhead_wrapper.p.toolhead_extruder_to_nozzle + self.drive().get_filament_position()) + u.p.toolhead_unload_safety_margin
                self.log_debug("Unloading last %.1fmm to exit the extruder%s" % (length, " (synced)" if synced else ""))
                _,_,measured,delta = self.move_filament("Unloading extruder", -length, speed=speed, motor=motor, wait=True)

                # Best guess of filament position is right at extruder entrance or just beyond if synced
                if synced:
                    self.drive().set_filament_position(-(u.toolhead_wrapper.p.toolhead_extruder_to_nozzle + u.p.toolhead_unload_safety_margin))
                else:
                    self.drive().set_filament_position(-u.toolhead_wrapper.p.toolhead_extruder_to_nozzle)

                # Encoder based validation test if it has high chance of being useful
                # NOTE: This check which used to raise MmuError() is tripping many folks up because they have poor tip forming
                #       logic so just log error and continue. This disguises the root cause problem but will make folks happier
                #       Not performed for slicer tip forming (validate=True) because everybody is ejecting the filament!
                if validate and self.can_use_encoder() and length > u.p.encoder_move_step_size and not extruder_only and self.gate_selected != TOOL_GATE_BYPASS:
                    self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                    msg = None
                    if measured < self.encoder().movement_min():
                        msg = "any"
                    elif synced and delta > length * (u.p.toolhead_move_error_tolerance / 100.):
                        msg = "sufficient"
                    if msg:
                        self.log_warning("Warning: Encoder not sensing %s movement during final extruder retraction move\nConcluding filament either stuck in the extruder, tip forming erroneously completely ejected filament or filament was not fully loaded\nWill attempt to continue..." % msg)

                self.set_filament_pos_state(FILAMENT_POS_END_BOWDEN)

            self._random_failure() # Testing
            self.movequeue_wait()
            self.log_debug("Filament should be out of extruder")

#            self.log_warning(f"PAUL: _unload_extruder() => synced={synced}")
            return synced


# -----------------------------------------------------------------------------------------------------------
# LOAD SEQUENCE
# -----------------------------------------------------------------------------------------------------------

    def load_sequence(self, bowden_move=None, skip_extruder=False, purge=None, extruder_only=False):
        """
        Execute the complete filament load sequence for the current gate and tool.

        Args:
            bowden_move: Requested bowden travel for the sequence. When omitted, the full calibrated length is used.
            skip_extruder: Whether to stop after the bowden stage and skip the final extruder load.
            purge: Purge mode selection used after loading completes.
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.

        Returns:
            None. Performs the load workflow, updates state and telemetry, or raises MmuError on failure.
        """
        u = self.mmu_unit()
        self.movequeue_wait()
        u.calibrator.restore_gear_rd()

        bowden_length = u.calibrator.get_bowden_length() # -1 if not calibrated yet
        calibrated = (bowden_length >= 0)
        initial_calibration = not calibrated and not extruder_only

        # Default bowden move if not specified is full length
        if bowden_move is None:
            bowden_move = bowden_length

        if calibrated and bowden_move > bowden_length:
            bowden_move = bowden_length
            self.log_warning(
                f"Warning: Restricting bowden load length to calibrated value of {bowden_length:.1f}mm"
            )

        # Convenience flags
        full = (bowden_move == bowden_length)
        macros_and_track = not extruder_only and full

        self.set_filament_direction(DIRECTION_LOAD)
        self.initialize_filament_position(dwell=None) # Reset measurement to 0
#        self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

        try:
            must_home = False
            if not extruder_only:
                current_action = self._set_action(ACTION_LOADING)
                if full:
                    must_home = self._must_home_to_extruder() or initial_calibration
                else:
                    skip_extruder = True

            if macros_and_track:
                self._track_time_start('load')
                # PRE_LOAD user defined macro
                with self._wrap_track_time('pre_load'):
                    self.wrap_gcode_command(self.p.pre_load_macro, exception=True, wait=True)

            self.log_info("Loading %s..." % ("extruder" if extruder_only else "filament"))
            if not extruder_only:
                self._display_visual_state()

            start_overshoot = 0.     # How far we overshoot the gate homing point (encoder based homing)
            start_filament_pos = self.filament_pos

            # Note: Conditionals deliberately coded this way to match macro alternative
            if self.p.gcode_load_sequence and not initial_calibration:
                self.log_debug("Calling external user defined loading sequence macro")
                self.wrap_gcode_command(
                    f"{self.p.load_sequence_macro} "
                    f"FILAMENT_POS={start_filament_pos} "
                    f"LENGTH={bowden_move:.1f} "
                    f"FULL={int(full)} "
                    f"HOME_EXTRUDER={int(must_home)} "
                    f"SKIP_EXTRUDER={int(skip_extruder)} "
                    f"EXTRUDER_ONLY={int(extruder_only)}",
                    exception=True,
                )

            elif extruder_only:
                if start_filament_pos < FILAMENT_POS_EXTRUDER_ENTRY:
                    self._load_extruder(extruder_only=True)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                else:
                    raise MmuError(
                        f"Cannot load extruder because filament already in extruder "
                        f"(state: {start_filament_pos}). Unload first"
                    )

            elif start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                raise MmuError(
                    f"Cannot load extruder because filament already in extruder "
                    f"(state: {start_filament_pos}). Unload first"
                )

            else:
                if start_filament_pos <= FILAMENT_POS_UNLOADED:
                    start_overshoot = self._load_gate()

                if initial_calibration:
                    if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_NONE:
                        raise MmuError(
                            f"Auto calibration is not possible with "
                            f"'extruder_homing_endstop: {SENSOR_EXTRUDER_NONE}'"
                        )

                    self.log_warning(
                        f"Auto calibrating bowden length on gate {self.gate_selected} "
                        f"using {self._gate_homing_string()} as gate reference point"
                    )

                    if self.sensor_manager.check_sensor(u.p.extruder_homing_endstop):
                        raise MmuError(
                            f"The {u.p.extruder_homing_endstop} sensor triggered before homing. "
                            "Check filament and sensor operation"
                        )

                    # Slow homing move for the max permissible distance
                    homing_max = u.p.bowden_homing_max
                    homing_movement = self._home_to_extruder(homing_max)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                    if homing_movement is None:
                        raise MmuError(
                            "Failed to auto calibrate bowden because unable to home to extruder "
                            f"after moving {homing_max:.1f}mm\n"
                            "If you have a very long bowden you may need to increase "
                            "'bowden_homing_max'"
                        )

                    calibrated_bowden_length = start_overshoot + homing_movement

                    if not skip_extruder:
                        self._load_extruder()

#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                    # Notify calibration manager
                    u.calibrator.update_bowden_calibration(calibrated_bowden_length)

                else:

                    # Normal load (not initial_calibration) ---------------------
                    bowden_travel = 0.
                    extruder_homing_buffer = 0.
                    bowden_move_ratio = None

#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                    if start_filament_pos < FILAMENT_POS_END_BOWDEN:
                        # Homing buffer is the shortfall in desired bowden move
                        bowden_move_ratio, extruder_homing_buffer = self._load_bowden(bowden_move, start_pos=start_overshoot)
                        bowden_travel = bowden_move - extruder_homing_buffer

                    if start_filament_pos < FILAMENT_POS_HOMED_EXTRUDER:
                        if must_home:
                            homing_movement = self._home_to_extruder(extruder_homing_buffer)
                            bowden_travel += homing_movement
                            extruder_homing_buffer = 0. # Don't reuse
#                            self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

                        elif self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                            pass # _load_extruder() will consume the homing buffer in next step

                        else:
                            # We are not homing so will just complete the bowden move with a slower (after movement)
                            speed = u.p.gear_short_move_speed
                            accel = u.p.gear_short_move_accel
                            _, _, _, delta = self.move_filament("Slow move to extruder entrance", extruder_homing_buffer, motor="gear", speed=speed, accel=accel)
                            bowden_travel += extruder_homing_buffer
                            extruder_homing_buffer = 0. # Don't reuse

                    if not skip_extruder:
                        bowden_extra = self._load_extruder(extra_homing=extruder_homing_buffer)
#                        self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                        if bowden_extra is not None:
                            # This means we employed homing to the toolhead sensor so adjust effective bowden move
                            bowden_travel += bowden_extra

                    # Notify calibration manager
                    if full and not skip_extruder:
                        u.calibrator.note_load_telemetry(self.gate_selected, bowden_move, bowden_travel, bowden_move_ratio)

            self.movequeue_wait()
            msg = "Load of %.1fmm filament successful" % self.drive().get_filament_position()
#            self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
            if self.can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = -(u.p.gate_parking_distance) + self.get_encoder_dead_space()
                msg += " {1}(adjusted encoder: %.1fmm){0}" % (final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # Activate loaded spool in Spoolman
            self._spoolman_activate_spool(self.gate_spool_id[self.gate_selected])

            # Deal with purging
            if purge == PURGE_SLICER and not skip_extruder:
                self.log_debug("Purging expected to be performed by slicer")

            elif purge == PURGE_STANDALONE and not skip_extruder:
                with self._wrap_track_time('purge'):
                    self.purge_standalone()

            # POST_LOAD user defined macro
            if macros_and_track:
                with self._wrap_track_time('post_load'):

                    # Restore the expected sync state now before running this macro
                    self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_purge)

                    if self.has_blobifier: # Legacy blobifer integration. purge_macro now preferred
                        with self.wrap_action(ACTION_PURGING):
                            self.wrap_gcode_command(self.p.post_load_macro, exception=True, wait=True)
                    else:
                        self.wrap_gcode_command(self.p.post_load_macro, exception=True, wait=True)

        except MmuError as ee:
            self._track_gate_statistics('load_failures', self.gate_selected)
            raise MmuError("Load sequence failed because:\n%s" % (str(ee)))

        finally:
            self._track_gate_statistics('loads', self.gate_selected)

            if not extruder_only:
                self._set_action(current_action)

            if macros_and_track:
                self._track_time_end('load')


# -----------------------------------------------------------------------------------------------------------
# UNLOAD SEQUENCE
# -----------------------------------------------------------------------------------------------------------

    def unload_sequence(self, bowden_move=None, check_state=False, form_tip=None, extruder_only=False):
        """
        Execute the complete filament unload sequence for the current gate and tool.

        Args:
            bowden_move: Requested bowden travel for the sequence. When omitted, the full calibrated length is used.
            check_state: Whether to recover and normalize filament state before unloading.
            form_tip: Tip-forming mode override. When omitted, standalone tip forming is used.
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.

        Returns:
            None. Performs the unload workflow, updates state and telemetry, or raises MmuError on failure.
        """
        u = self.mmu_unit()
        self.movequeue_wait()
        u.calibrator.restore_gear_rd()

        bowden_length = u.calibrator.get_bowden_length() # -1 if not calibrated yet
        calibrated = (bowden_length >= 0)
        if not calibrated:
            bowden_length = u.p.bowden_homing_max # Special case - if not calibrated then apply the max possible bowden length

        # Default bowden move if not specified is full length
        if bowden_move is None:
            bowden_move = bowden_length

        if calibrated and bowden_move > bowden_length:
            bowden_move = bowden_length
            self.log_warning(
                f"Warning: Restricting bowden unload length to calibrated value of {bowden_length:.1f}mm"
            )

        # Convenience flags
        full = (bowden_move == bowden_length)
        macros_and_track = not extruder_only and full
        runout = self.is_handling_runout

        self.set_filament_direction(DIRECTION_UNLOAD)

        if check_state or self.filament_pos == FILAMENT_POS_UNKNOWN:
            # Let's determine where filament is and reset state before continuing
            self.recover_filament_pos(message=True)

        self.initialize_filament_position(dwell=None)
#        self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

        if self.filament_pos == FILAMENT_POS_UNLOADED:
            self.log_debug("Filament already ejected")
            return

        try:
            if not extruder_only:
                current_action = self._set_action(ACTION_UNLOADING)

            # Deactivate spool immediately before tip forming/cutting
            # Tip forming/cutting macros use the extruder to execute, hence
            # any retraction / de retraction moves are accounted in Spoolman.
            # By de-activating early, the retraction performed from the macro
            # is deliberately not accounted in spoolman
            self._spoolman_activate_spool(0)

            # Run PRE_UNLOAD user defined macro
            if macros_and_track:
                self._track_time_start('unload')
                with self._wrap_track_time('pre_unload'):
                    self.wrap_gcode_command(self.p.pre_unload_macro, exception=True, wait=True)

            self.log_info("Unloading %s..." % ("extruder" if extruder_only else "filament"))
            if not extruder_only:
                self._display_visual_state()

            # Tip forming ---------------------------------
            synced_extruder_unload = False
            park_pos = 0.
            do_form_tip = form_tip if form_tip is not None else FORM_TIP_STANDALONE # Default to standalone
            if do_form_tip == FORM_TIP_SLICER:
                # Slicer was responsible for the tip, but the user must set the slicer_tip_park_pos
                park_pos = self.p.slicer_tip_park_pos
                self.drive().set_filament_position(-park_pos)
                if park_pos == 0.:
                    self.log_error("Tip forming performed by slicer but 'slicer_tip_park_pos' not set")
                else:
                    self.log_debug("Tip forming performed by slicer, park_pos set to %.1fmm" % park_pos)

            elif do_form_tip == FORM_TIP_STANDALONE and (self.filament_pos >= FILAMENT_POS_IN_EXTRUDER or runout):
                with self._wrap_track_time('form_tip'):
                    # Extruder only in runout case to give filament best chance to reach gear
                    detected = self.form_tip_standalone(extruder_only=(extruder_only or runout))
                    park_pos = self.drive().get_filament_position()
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

                    # If handling runout warn if we don't see any filament near the gate
                    if runout and (
                        self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected) is False or
                        (self.has_encoder() and self.get_encoder_distance() == 0)
                    ):
                        self.log_warning("Warning: Filament not seen near gate after tip forming move. Unload may not be possible")

                    self.wrap_gcode_command(self.p.post_form_tip_macro, exception=True, wait=True)

            # Note: Conditionals deliberately coded this way to match macro alternative
            start_filament_pos = self.filament_pos
            unload_to_buffer = (start_filament_pos >= FILAMENT_POS_END_BOWDEN and not extruder_only)

            if self.p.gcode_unload_sequence and calibrated:
                self.log_debug("Calling external user defined unloading sequence macro")
                self.wrap_gcode_command(
                    "%s FILAMENT_POS=%d LENGTH=%.1f EXTRUDER_ONLY=%d PARK_POS=%.1f" % (
                        self.p.unload_sequence_macro,
                        start_filament_pos,
                        bowden_move,
                        extruder_only,
                        park_pos
                    ),
                    exception=True
                )

            elif extruder_only:
                if start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                    synced_extruder_unload = self._unload_extruder(extruder_only=True, validate=do_form_tip == FORM_TIP_STANDALONE)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                else:
                    raise MmuError("Cannot unload extruder because filament not detected in extruder! (state: %s)" % start_filament_pos)

            elif start_filament_pos == FILAMENT_POS_UNLOADED:
                raise MmuError("Cannot unload because already unloaded!")

            else:
#                self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                if start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload to gate
                    synced_extruder_unload = self._unload_extruder(validate=do_form_tip == FORM_TIP_STANDALONE)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

                if (
                    (start_filament_pos >= FILAMENT_POS_END_BOWDEN and calibrated) or
                    (start_filament_pos >= FILAMENT_POS_HOMED_GATE and not full)
                ):
                    # Fast unload of bowden, then unload gate
                    bowden_move_ratio, gate_homing_buffer = self._unload_bowden(bowden_move)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                    homing_movement = self._unload_gate(gate_homing_buffer)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
                    bowden_travel = bowden_move - gate_homing_buffer + homing_movement

                    # Notify autotune manager
                    if full:
                        u.calibrator.note_unload_telemetry(self.gate_selected, bowden_move, bowden_travel, bowden_move_ratio)
#                    self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")

                elif start_filament_pos >= FILAMENT_POS_HOMED_GATE:
                    # We have to do slow unload because we don't know exactly where we are
                    self._unload_gate(bowden_move)

            # Set future "from buffer" flag (also used for faster loading speed)
            if unload_to_buffer and self.gate_status[self.gate_selected] != GATE_EMPTY:
                self.gate_maps.set_gate_status(self.gate_selected, GATE_AVAILABLE_FROM_BUFFER)

            # If runout then over unload to prevent accidental reload
            if runout:
                self._eject_from_gate()

             # Encoder based validation test
             # Currently disabled because it results in servo "flutter" that users don't like
             #if self.can_use_encoder():
             #    movement = self.selector().filament_release(measure=True)
             #    if movement > self.encoder().movement_min():
             #        self.set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
             #        self.log_trace("Encoder moved %.1fmm when filament was released!" % movement)
             #        raise MmuError("Encoder sensed movement when the servo was released\nConcluding filament is stuck somewhere")

            self.movequeue_wait()
            msg = "Unload of %.1fmm filament successful" % self.drive().get_filament_position()
#            self.log_warning(f"PAUL: filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
            if self.can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = -(u.p.gate_parking_distance) + self.get_encoder_dead_space() + (u.p.toolhead_unload_safety_margin if not synced_extruder_unload else 0.)
                msg += " {1}(adjusted encoder: %.1fmm){0}" % (final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # POST_UNLOAD user defined macro
            if macros_and_track:
                with self._wrap_track_time('post_unload'):

                    # Restore the expected sync state now before running this macro
                    self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_to_extruder)

                    if self.has_mmu_cutter:
                        with self.wrap_action(ACTION_CUTTING_FILAMENT):
                            self.wrap_gcode_command(self.p.post_unload_macro, exception=True, wait=True)
                    else:
                        self.wrap_gcode_command(self.p.post_unload_macro, exception=True, wait=True)

        except MmuError as ee:
            self._track_gate_statistics('unload_failures', self.gate_selected)
            raise MmuError("Unload sequence failed because:\n%s" % (str(ee)))

        finally:
            self._track_gate_statistics('unloads', self.gate_selected)

            if not extruder_only:
                self._set_action(current_action)

            if macros_and_track:
                self._track_time_end('unload')


# -----------------------------------------------------------------------------------------------------------
# TIP FORMING AND PURGING
# -----------------------------------------------------------------------------------------------------------

    def form_tip_standalone(self, extruder_only=False):
        """
        Form tip prior to extraction from the extruder. This can take the form of shaping the filament or could simply
        activate a filament cutting mechanism. Sets filament position based on park pos

        Args:
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.

        Returns:
            bool: True when filament is believed to remain detected after tip forming; otherwise False.
        """
        u = self.mmu_unit()

        self.movequeue_wait()

        # Pre check to validate the presence of filament in the extruder and case where we don't need to form tip
        filament_initially_present = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        if filament_initially_present is False:
            self.log_debug("Tip forming skipped because no filament was detected")

            if self.filament_pos == FILAMENT_POS_LOADED:
                self.set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
            else:
                self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

            self.drive().set_filament_position(-u.toolhead_wrapper.p.toolhead_extruder_to_nozzle)
            return False

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.p.form_tip_macro)

        with self.wrap_action(ACTION_CUTTING_TIP if self.has_toolhead_cutter else ACTION_FORMING_TIP):
            self._ensure_safe_extruder_temperature(wait=True)
            sync = self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_form_tip, force_grip=True)

            # Perform the tip forming move and establish park_pos
            initial_encoder_position = self.get_encoder_distance()
            park_pos, remaining, reported = self._do_form_tip()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position

            # Update filament model in extruder
            color = (
                self.gate_color[self.gate_selected]
                if self.gate_selected >= 0
                else UNKNOWN_FILAMENT_COLOR
            )
            u.extruder_wrapper.set_filament_remaining(remaining, color)

            # Encoder based validation test
            detected = True # Start with assumption that filament was present
            if self.can_use_encoder() and not reported:
                # Logic to try to validate success and update presence of filament based on movement
                if filament_initially_present is True:
                    # With encoder we might be able to check for clog now
                    if not measured > self.encoder().movement_min():
                        raise MmuError("No encoder movement: Concluding filament is stuck in extruder")
                else:
                    # Couldn't determine if we initially had filament at start (lack of sensors)
                    if not measured > self.encoder().movement_min():
                        # No movement. We can be confident we are/were empty
                        detected = False
                    elif sync:
                        # A further test is needed to see if the filament is actually in the extruder
                        detected, moved = self.test_filament_still_in_extruder_by_retracting()
                        park_pos += moved

            self.drive().set_filament_position(-park_pos)
            self.set_encoder_distance(initial_encoder_position + park_pos)

            if detected or extruder_only:
                # Definitely in extruder
                self.set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
            else:
                # No detection. Best to assume we are somewhere in bowden for defensive unload
                self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

#            self.log_warning(f"PAUL: AFTER_FF filament_pos={self.drive().get_filament_position():.1f}mm, encoder={self.get_encoder_distance(dwell=None):.1f}mm")
            return detected


    def _do_form_tip(self, test=False):
        """
        Run the configured tip-forming macro and derive park position information from it.

        Args:
            test: Whether the tip-forming macro should run in test or final-eject mode.

        Returns:
            tuple[float, float, bool]: Park position, remaining filament estimate, and whether the park position was explicitly reported by the macro.
        """
        u = self.mmu_unit()

        with self._wrap_extruder_current(self.p.extruder_form_tip_current, "for tip forming move"):
            extruder_stepper = self.toolhead.get_extruder().extruder_stepper.stepper
            initial_mcu_pos = extruder_stepper.get_mcu_position()
            initial_encoder_position = self.get_encoder_distance()

            with self._wrap_pressure_advance(0., "for tip forming"):
                gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, "_MMU_FORM_TIP")
                self.log_info("Forming tip...")
                self.wrap_gcode_command("%s %s" % (self.p.form_tip_macro, "FINAL_EJECT=1" if test else ""), exception=True, wait=True)

            final_mcu_pos = extruder_stepper.get_mcu_position()
            stepper_movement = (initial_mcu_pos - final_mcu_pos) * extruder_stepper.get_step_dist()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            park_pos = gcode_macro.variables.get("output_park_pos", -1)
            try:
                park_pos = float(park_pos)
            except ValueError as e:
                self.log_error("Reported 'output_park_pos: %s' could not be parsed: %s" % (park_pos, str(e)))
                park_pos = -1

            reported = False
            if park_pos < 0:
                # Use stepper movement (tip forming)
                filament_remaining = 0.
                park_pos = stepper_movement + u.toolhead_wrapper.p.toolhead_residual_filament + self.toolchange_retract
                msg = "After tip forming, extruder moved: %.1fmm thus park_pos calculated as %.1fmm (encoder measured %.1fmm total movement)" % (stepper_movement, park_pos, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)
            else:
                # Means the macro reported it (filament cutting)
                if park_pos == 0:
                    self.log_warning("Warning: output_park_pos was reported as 0mm and may not be set correctly\nWill attempt to continue...")
                reported = True
                filament_remaining = park_pos - stepper_movement - u.toolhead_wrapper.p.toolhead_residual_filament - self.toolchange_retract
                msg = "After tip cutting, park_pos reported as: %.1fmm with calculated %.1fmm filament remaining in extruder (extruder moved: %.1fmm, encoder measured %.1fmm total movement)" % (park_pos, filament_remaining, stepper_movement, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)

            if not test:
                # Important sanity checks to spot misconfiguration
                if park_pos > u.toolhead_wrapper.p.toolhead_extruder_to_nozzle:
                    self.log_warning("Warning: park_pos (%.1fmm) cannot be greater than 'toolhead_extruder_to_nozzle' distance of %.1fmm! Assuming fully unloaded from extruder\nWill attempt to continue..." % (park_pos, u.toolhead_wrapper.p.toolhead_extruder_to_nozzle))
                    park_pos = u.toolhead_wrapper.p.toolhead_extruder_to_nozzle
                    filament_remaining = 0.

                if filament_remaining < 0:
                    self.log_warning("Warning: Calculated filament remaining after cut is negative (%.1fmm)! Suspect misconfiguration of output_park_pos (%.1fmm).\nWill attempt to continue assuming no cut filament remaining..." % (filament_remaining, park_pos))
                    park_pos = 0.
                    filament_remaining = 0.

        return park_pos, filament_remaining, reported


    def purge_standalone(self, extruder_only=False):
        """
        Run the configured standalone purge macro, if one is available.
        """
        u = self.mmu_unit()

        if not self.p.purge_macro:
            return

        gcode_macro = self.printer.lookup_object(f"gcode_macro {self.p.purge_macro}", None)
        if gcode_macro is None:
            self.log_warning(f"Purge macro '{self.p.purge_macro}' not found")
            return

        with self.wrap_action(ACTION_PURGING):
            self._ensure_safe_extruder_temperature(wait=True)
            self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_purge, force_grip=True)

            self.log_info("Purging...")

            with self._wrap_extruder_current(self.p.extruder_purge_current, "for filament purge"):
                # The macro to decide on the purge volume, but expect to be based on this.
                msg = (
                    f"Suggested purge volume of {self.toolchange_purge_volume:.1f}mm{UI_CUBE} calculated from:\n"
                    f"- toolhead_residual_filament: {u.toolhead_wrapper.p.toolhead_residual_filament:.1f}mm\n"
                    f"- filament_remaining (previous cut fragment): {u.extruder_wrapper.filament_remaining:.1f}mm\n"
                )
                toolchange_volume_str = f"{self._slicer_purge_volume:.1f}mm{UI_CUBE}"
                msg += (
                    f"- slicer purge volume for toolchange "
                    f"{self.selected_tool_string(self._last_tool)} > "
                    f"{self.selected_tool_string(self._next_tool)}: "
                    f"{toolchange_volume_str}"
                )
                self.log_debug(msg)
                macro = self.p.purge_macro
                if extruder_only:
                    macro += " EXTRUDER_ONLY=1"
                self.wrap_gcode_command(macro, exception=True, wait=True)


# -----------------------------------------------------------------------------------------------------------
# FILAMENT MOVEMENT AND CONTROL
# -----------------------------------------------------------------------------------------------------------

    def _resolve_filament_move_speed(self, dist, motor, homing_move, speed, accel, speed_override=True):
        """
        Determine best speed and accelaration for move type
        """
        u = self.mmu_unit()

        if motor in ["gear"]:
            if homing_move != 0:
                speed = speed or u.p.gear_homing_speed
                accel = accel or min(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel)
            else:
                if abs(dist) > u.p.gear_short_move_threshold:
                    if dist < 0:
                        speed = speed or u.p.gear_unload_speed
                        accel = accel or u.p.gear_unload_accel
                    elif (not u.p.has_filament_buffer or
                            (self.gate_selected >= 0 and self.gate_status[self.gate_selected] != GATE_AVAILABLE_FROM_BUFFER)):
                        speed = speed or u.p.gear_from_spool_speed
                        accel = accel or u.p.gear_from_spool_accel
                    else:
                        speed = speed or u.p.gear_from_filament_buffer_speed
                        accel = accel or u.p.gear_from_filament_buffer_accel
                else:
                    speed = speed or u.p.gear_short_move_speed
                    accel = accel or u.p.gear_short_move_accel

        elif motor in ["gear+extruder", "synced"]:
            if homing_move != 0:
                speed = speed or min(u.p.gear_homing_speed, self.p.extruder_homing_speed)
                accel = accel or min(max(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel), self.p.extruder_accel)
            else:
                speed = speed or (self.p.extruder_sync_load_speed if dist > 0 else self.p.extruder_sync_unload_speed)
                accel = accel or min(max(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel), self.p.extruder_accel)

        elif motor in ["extruder"]:
            if homing_move != 0:
                speed = speed or self.p.extruder_homing_speed
                accel = accel or self.p.extruder_accel
            else:
                speed = speed or (self.p.extruder_load_speed if dist > 0 else self.p.extruder_unload_speed)
                accel = accel or self.p.extruder_accel

        else:
            raise self.printer.command_error("Invalid motor specification '%s'" % (motor,))

        if self.gate_selected >= 0 and speed_override:
            adjust = self.gate_speed_override[self.gate_selected] / 100.
            speed *= adjust
            accel *= adjust

        return speed, accel


    # Used to wrap certain unload moves and activate eSpooler. Ensures eSpooler is always stopped
    @contextlib.contextmanager
    def _wrap_espooler(self, motor, dist, speed, accel, homing_move):
        """
        Activate eSpooler assist or rewind around eligible moves and always stop it afterward.

        Args:
            motor: Motor mode to use for the move, such as gear, extruder, or synced movement.
            dist: Relative move distance in millimeters.
            speed: Requested move speed. When omitted, a context-appropriate default is chosen.
            accel: Requested acceleration. When omitted, a context-appropriate default is chosen.
            homing_move: Non-zero to perform a homing move; the sign indicates the expected trigger direction.

        Yields:
            self while eSpooler assist or rewind is active for the wrapped move.
        """
        u = self.mmu_unit()

        self._wait_for_espooler = False
        espooler_operation = ESPOOLER_OFF

        if self.has_espooler():
            pwm_value = 0
            if abs(dist) >= u.p.espooler_min_distance and speed > u.p.espooler_min_stepper_speed:
                if dist > 0 and ESPOOLER_ASSIST in u.p.espooler_operations:
                    espooler_operation = ESPOOLER_ASSIST
                elif dist < 0 and ESPOOLER_REWIND in u.p.espooler_operations:
                    espooler_operation = ESPOOLER_REWIND

                if espooler_operation == ESPOOLER_OFF:
                    pwm_value = 0
                elif speed >= u.p.espooler_max_stepper_speed:
                    pwm_value = 1
                else:
                    pwm_value = (speed / u.p.espooler_max_stepper_speed) ** u.p.espooler_speed_exponent

            # Reduce assist speed compared to rewind but also apply the "print" minimum
            # We want rewind to be faster than assist but never non-functional
            if espooler_operation == ESPOOLER_ASSIST:
                pwm_value = max(pwm_value * (u.p.espooler_assist_reduced_speed / 100), u.p.espooler_printing_power / 100)

            if espooler_operation != ESPOOLER_OFF:
                self._wait_for_espooler = not homing_move
                self.espooler().set_operation(self.gate_selected, pwm_value, espooler_operation)
        try:
            # Note gate_selected doesn't change in this use case, it's just filament movement
            yield self

        finally:
            self._wait_for_espooler = False
            if espooler_operation != ESPOOLER_OFF:
                self.espooler().set_operation(self.gate_selected, 0, ESPOOLER_OFF)


    def move_filament(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0,
                      endstop_name="default", track=False, wait=False, encoder_dwell=False,
                      speed_override=True, suppress_grip_change=False):
        """
        Execute a traced filament move and report actual motion, homing result, and encoder data.

        Convenience wrapper around all gear and extruder motor movement that retains sync state, tracks movement and creates trace log
        motor = "gear"             - gear motor(s) only in manual mode
                "gear+extruder"    - gear and extruder in manual mode
                "extruder"         - extruder only in manual mode
                "synced"           - gear synced with extruder in extruder mode (as in print, homing move not possible)

        If homing move then endstop name can be specified.
                "mmu_shared_exit"  - at the gate on MMU (when motor includes "gear")
                "mmu_exit_N"       - post past the filament drive gear
                "extruder"         - just before extruder entrance (motor includes "gear" or "extruder")
                "toolhead"         - after extruder entrance (motor includes "gear" or "extruder")
                "mmu_gear_touch"   - stallguard on gear (when motor includes "gear", only useful for motor="gear")
                "mmu_ext_touch"    - stallguard on nozzle (when motor includes "extruder", only useful for motor="extruder")

        All move distances are interpreted as relative

        Args:
            trace_str: Human-readable trace prefix to log after the move completes.
            dist: Relative move distance in millimeters.
            speed: Requested move speed. When omitted, a context-appropriate default is chosen.
            accel: Requested acceleration. When omitted, a context-appropriate default is chosen.
            motor: Motor mode to use for the move, such as gear, extruder, or synced movement.
            homing_move: Non-zero to perform a homing move; the sign indicates the expected trigger direction.
            endstop_name: Endstop to use for homing moves.
            track: Whether the move should update per-gate tracking statistics.
            wait: Whether to wait for motion queues to drain after the move.
            encoder_dwell: Whether to add dwell time when sampling encoder position.
            speed_override: Whether per-gate speed overrides should be applied.
            suppress_grip_change: Prevents explicit calling to correct grip (buzz gear on down case)

        Returns:
            tuple[float, bool, float, float]: Actual movement, homing success flag, encoder-measured movement, and the difference between commanded and measured travel.
        """
        u = self.mmu_unit()
        drive = self.drive()
        extruder_name = self.mmu_unit().extruder_name()
        ext_pos = self.toolhead.get_position()

        encoder_start = self.get_encoder_distance(dwell=encoder_dwell)
        homed = False
        actual = dist
        delta = 0.
        null_rtn = (0., False, 0., 0.)

        if motor not in ["gear", "gear+extruder", "extruder", "synced"]:
            self.log_assertion("Invalid motor specification '%s'" % motor)
            return null_rtn

            if homing_move != 0:
                # Check for valid endstop
                endstop_name = self.sensor_manager.get_qualified_endstop_name(endstop_name)
                if not drive.has_endstop(endstop_name):
                    self.log_error(f"Endstop '{endstop_name}' not found")
                    return null_rtn

        # Determine speed
        speed, accel = self._resolve_filament_move_speed(dist, motor, homing_move, speed, accel, speed_override=speed_override)

        with self._wrap_espooler(motor, dist, speed, accel, homing_move):
            wait = wait or self._wait_for_espooler

            try:
                if motor == "gear":
                    # normal gear-only movement
                    if not suppress_grip_change:
                        self.selector().filament_drive()
                    drive.sync_mode(DRIVE_UNSYNCED)
                    self._restore_gear_current()

                elif motor == "gear+extruder":
                    # gear leads, extruder follows manually
                    if not suppress_grip_change:
                        self.selector().filament_drive()
                    drive.sync_mode(DRIVE_EXTRUDER_SYNCED_TO_GEAR)
                    self._restore_gear_current()

                elif motor == "extruder":
                    # extruder-only-on-gear semantics
                    if not suppress_grip_change:
                        self.selector().filament_release()
                    drive.sync_mode(DRIVE_EXTRUDER_ONLY)
                    self._restore_gear_current()

                elif motor == "synced":
                    # extruder leads, gear follows extruder
                    if not suppress_grip_change:
                        self.selector().filament_drive()
                    drive.sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
                    self._adjust_gear_current(percent=u.p.sync_gear_current, reason="for extruder synced move")

                else:
                    raise self.printer.command_error("Invalid motor specification '%s'" % (motor,))

                start_pos = drive.get_filament_position()

                # Manual stepper move authority
                if motor in ["gear", "gear+extruder", "extruder"]:

                    if homing_move != 0:
                        # Check for valid endstop
                        endstop_name = self.sensor_manager.get_qualified_endstop_name(endstop_name)
                        if not drive.has_endstop(endstop_name):
                            self.log_error(f"Endstop '{endstop_name}' not found")
                            return null_rtn

                    if homing_move != 0:
                        self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s" % (motor.upper(), dist, speed, accel, endstop_name, wait))
                    else:
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))

                    actual, homed = drive.move(dist, speed, accel, homing_move=homing_move, endstop_name=endstop_name)

                # Normal extruder-side move authority
                elif motor == "synced":
                    if homing_move != 0:
                        self.log_error("Not possible to perform homing move while synced")
                        return null_rtn

                    if self.log_enabled(LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))

                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)
                    actual = dist

                if wait:
                    self.movequeue_wait()

            except self.printer.command_error as e:
                if homing_move != 0:
                    self.log_stepper("Did not complete homing move: %s" % str(e))
                    try:
                        actual = drive.get_filament_position() - start_pos
                    except Exception:
                        actual = 0.
                    homed = False
                else:
                    return null_rtn

        encoder_end = self.get_encoder_distance(dwell=encoder_dwell)
        measured = encoder_end - encoder_start
        delta = abs(actual) - measured # +ve means measured less than moved, -ve means measured more than moved

        if trace_str:
            if homing_move != 0:
                trace_str += ". Stepper: '%s' %s after moving %.1fmm (of max %.1fmm), encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, ("homed" if homed else "did not home"), actual, dist, measured, delta)
            else:
                trace_str += ". Stepper: '%s' moved %.1fmm, encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, dist, measured, delta)

            trace_str += " --> Pos: @%.1f, (e: %.1fmm)" % (drive.get_filament_position(), encoder_end)
            self.log_trace(trace_str)

        if motor == "gear" and track and self.can_use_encoder():
            if dist > 0:
                self._track_gate_statistics('load_distance', self.gate_selected, dist)
                self._track_gate_statistics('load_delta', self.gate_selected, delta)
            else:
                self._track_gate_statistics('unload_distance', self.gate_selected, -dist)
                self._track_gate_statistics('unload_delta', self.gate_selected, delta)
            if dist != 0:
                quality = abs(1. - delta / dist)
                cur_quality = self.gate_statistics[self.gate_selected]['quality']
                if cur_quality < 0:
                    self.gate_statistics[self.gate_selected]['quality'] = quality
                else:
                    # Average (EMA) down over 10 swaps
                    self.gate_statistics[self.gate_selected]['quality'] = (cur_quality * 9 + quality) / 10

        return actual, homed, measured, delta


# -----------------------------------------------------------------------------------------------------------
# GENERAL FILAMENT RECOVERY AND MOVE HELPERS
# -----------------------------------------------------------------------------------------------------------

    def get_encoder_dead_space(self):
        """
        If loading filament from the gate, return the "unseen" porition of the move
        """
        if self.has_encoder() and self.mmu_unit().p.gate_homing_endstop in [SENSOR_SHARED_EXIT, SENSOR_EXIT_PREFIX]:
            return self.mmu_unit().p.gate_endstop_to_encoder
        else:
            return 0.


    def report_necessary_recovery(self, use_autotune=True):
        """
        Report whether recovery or calibration is required or manual recovery is necessary

        Args:
            use_autotune: Whether autotune-aware calibration status should be considered.

        Returns:
            None. Logs recovery guidance based on current calibration and filament state.
        """
        # Iterate over mmu_units with separate calibration message for each
        for u in self.mmu_machine.units:
            u.calibrator.check_if_not_calibrated(CALIBRATED_ALL, silent=None, use_autotune=use_autotune)

        # Report of filament position state
        if self.filament_pos != FILAMENT_POS_UNLOADED and TOOL_GATE_UNKNOWN in [self.gate_selected, self.tool_selected]:
            self.log_error("Filament detected but tool/gate is unknown. Please use MMU_RECOVER GATE=xx to correct state")

        elif self.filament_pos not in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]:
           self.log_error(
               "Filament not detected as either unloaded or fully loaded.\n"
               "Please check and use MMU_RECOVER to correct state or fix before continuing"
           )


    def recover_filament_pos(self, strict=False, can_heat=True, message=False, silent=False):
        """
        Infer the most conservative filament position from sensors and available checks (for unload purposes)
        Also, ensures that the filament availabilty is updated if filament is found

        Args:
            strict: Whether recovery should use stricter assumptions when inferring filament position.
            can_heat: Whether checks that may require a heated extruder are allowed.
            message: Whether to emit a user-facing recovery status message.
            silent: Whether state updates should suppress normal status messaging.

        Returns:
            None. Updates the inferred filament position and gate availability in place.
        """
        u = self.mmu_unit()

        if message:
            self.log_info("Attempting to recover filament position...")

        ts = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        es = self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
        gs = self.sensor_manager.check_sensor(self.sensor_manager.get_qualified_endstop_name(u.p.gate_homing_endstop))

        filament_detected = self.sensor_manager.check_any_sensors_in_path()
        looks_loaded = self.sensor_manager.check_all_sensors_in_path()
        if not filament_detected:
            filament_detected = self.check_filament_in_mmu() # Include encoder detection method

        # Definitely loaded
        if ts:
            self.set_filament_pos_state(FILAMENT_POS_LOADED, silent=silent)

        # Probably loaded: Unless strict we will continue to assume loaded in the absence of sensors to say otherwise
        elif not strict and self.filament_pos == FILAMENT_POS_LOADED and looks_loaded:
            pass

        # Somewhere in extruder
        elif filament_detected and can_heat and self.check_filament_in_extruder(): # Encoder based
            self.set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming on unload
        elif ts is False and filament_detected and (self.p.strict_filament_recovery or strict) and can_heat and self.check_filament_in_extruder():
            # This case adds an additional encoder based test to see if filament is still being gripped by extruder
            # even though TS doesn't see it. It's a pedantic option so on turned on by strict flag
            self.set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming

        # At extruder entry
        elif es:
            self.set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY, silent=silent) # Allows for fast bowden unload move

        # Parked at gate (when parking distance is not a retract i.e. gs sensor expected to be triggered)
        elif gs and filament_detected and u.p.gate_parking_distance <= 0:
            self.set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # Somewhere in bowden
        elif gs or filament_detected:
            self.set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=silent) # Prevents fast bowden unload move

            # Sensor sanity check
            if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False) is False:
                sensors = self.sensor_manager.get_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False)
                malfunction = ", ".join(sorted(k for k, v in sensors.items() if v is False))
                self.log_warning(
                    (
                        f"Filament appears to be in the bowden, but these sensors are not triggered: {malfunction}\n"
                        "Check sensors using MMU_SENSORS.\n"
                        "Verify the correct gate is selected.\n"
                        "Re-run MMU_RECOVER when ready."
                    )
                )

        # Unloaded
        else:
            self.set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # If filament is detected then ensure gate status is correct
        if self.gate_selected != TOOL_GATE_UNKNOWN and filament_detected:
            gate_status = self.gate_status[self.gate_selected]
            if self.filament_pos >= FILAMENT_POS_START_BOWDEN and gate_status < GATE_AVAILABLE:
                self.gate_maps.set_gate_status(self.gate_selected, GATE_AVAILABLE)
            elif gate_status == GATE_EMPTY:
                self.gate_maps.set_gate_status(self.gate_selected, GATE_UNKNOWN)


    def check_filament_in_mmu(self):
        """
        Check whether filament is present anywhere in the MMU path.

        Returns:
            bool or None: True when filament is detected in the MMU path, False when not detected, or None when no test is possible.
        """
        self.log_debug("Checking for filament in MMU...")
        detected = self.sensor_manager.check_any_sensors_in_path()
        if not detected and self.has_encoder():
            self.selector().filament_drive() # Prevent accidental encoder movement by griping early
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected


    def check_filament_in_gate(self):
        """
        Check whether filament is present at the currently selected gate.

        Returns:
            bool or None: True when filament is detected at the selected gate, False when not detected, or None when no test is possible.
        """
        self.log_debug("Checking for filament at gate...")
        detected = self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected)
        if not detected and self.has_encoder():
            self.selector().filament_drive() # Prevent accidental encoder movement by griping early
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected


    def check_filament_runout(self):
        """
        Check whether filament runout has been detected.

        Returns:
            bool or None: True when runout is detected, False when filament appears present, or None when no test is possible.
        """
        self.log_debug("Checking for runout...")
        runout = self.sensor_manager.check_for_runout()
        if runout is None and self.has_encoder():
            self.selector().filament_drive() # Prevent accidental encoder movement by griping early
            detected = not self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
            runout = not detected
        if runout is None:
            self.log_debug("No sensors configured!")
        return runout


    def check_filament_in_extruder(self):
        """
        Check whether filament is still present in the extruder path.

        Returns:
            bool or None: True when filament is believed to be in the extruder path, False when not present, or None when no test is possible.
        """
        es = self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY)
        if es is not None:
            return es

        # Now toolhead if fitted
        ts = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        if ts is True:
            return True

        # Finally resort to movement test with encoder
        detected, _ = self.test_filament_still_in_extruder_by_retracting()
        return detected


    def test_filament_still_in_extruder_by_retracting(self):
        """
        Probe for filament in the extruder by retracting and observing encoder motion.
        Even with toolhead sensor this can happen if the filament is in the short
        distance from sensor to gears. Requires encoder
        Return None if test not possible

        Returns:
            tuple[bool or None, float]: Detection result and encoder-measured movement from the probe retract.
        """
        u = self.mmu_unit()

        detected = None
        measured = 0
        if self.has_encoder() and not u.filament_always_gripped:
            with self.require_encoder(): # Force quality measurement
                self.log_info("Checking for possibility of filament still in extruder gears...")
                self._ensure_safe_extruder_temperature(wait=False)
                self.selector().filament_release() # Prevent accidental encoder movement by releasing early
                move = u.p.encoder_move_step_size
                _, _, measured, _ = self.move_filament("Checking extruder", -move, speed=self.p.extruder_unload_speed, motor="extruder")
                detected = measured > self.encoder().movement_min()
                self.log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
        return detected, measured


    def buzz_gear_motor(self):
        """
        Buzz the gear motor briefly to infer filament presence from encoder movement.

        Returns:
            bool or None: Encoder-based filament detection result, or None when the test is unavailable.
        """
        u = self.mmu_unit()

        if self.has_encoder():
            with self.require_encoder(): # Force quality measurement
                initial_encoder_position = self.get_encoder_distance()
                self.move_filament(None, 2.5 * self.encoder().get_resolution(), accel=u.p.gear_buzz_accel, encoder_dwell=None)
                self.move_filament(None, -2.5 * self.encoder().get_resolution(), accel=u.p.gear_buzz_accel, encoder_dwell=None)
                measured = self.get_encoder_distance() - initial_encoder_position
                self.log_trace("After buzzing gear motor, encoder measured %.2f" % measured)
                self.set_encoder_distance(initial_encoder_position, dwell=None)
                return measured > self.encoder().movement_min()
        else:
            self.move_filament(None, 5, accel=u.p.gear_buzz_accel)
            self.move_filament(None, -5, accel=u.p.gear_buzz_accel)
        return None


# -----------------------------------------------------------------------------------------------------------
# MMU/Extruder Synchronization
# -----------------------------------------------------------------------------------------------------------

    def _sync_gear_to_extruder(self, sync):
        """
        Apply or remove direct synchronization between the gear motor and
        extruder and adjust stepper currents.

        Args:
            sync: Whether to sync the gear motor to the extruder.
        """

        u = self.mmu_unit()
        self.log_stepper(f"_sync_gear_to_extruder(sync={sync})")

        # Protect cases where we should not sync (note type-B always has a homed selector).
        bypass_or_unknown_gate = self.gate_selected < 0
        selector_ready = self.selector().is_homed
        if bypass_or_unknown_gate or not selector_ready:
            sync = False
            self._standalone_sync = False

        # Sync to / unsync from extruder
        if sync:
            self.drive().sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
        else:
            self.drive().sync_mode(DRIVE_UNSYNCED)

        # Current control:
        # - While synced, optionally reduce current for the active gear stepper.
        # - On multigear systems, restore current on the previously-used gear stepper if gate differs.
        # - When unsynced, restore current to 100%.
        if sync:
            if u.multigear and gate != self.gate_selected:
                self._restore_gear_current()  # Restore previous gear stepper to 100%

            self._adjust_gear_current(percent=u.p.sync_gear_current, reason="for extruder syncing")
        else:
            self._restore_gear_current()  # 100%


    def reset_sync_gear_to_extruder(self, sync_intention=None, force_grip=False, skip_extruder_check=False, force_in_print=False):
        """
        Reconcile the desired gear-to-extruder sync state with current context and safety rules.

        Args:
            sync_intention:      Requested sync intent from the caller for the current operation.
                                 None=figure out based on context.
            force_grip:          Whether filament grip or release should be forced to change even when suppression is active.
            skip_extruder_check: Whether to bypass the normal check that filament is past the extruder entry point.
                                 This is only currently used to support the MMU_SYNC_GEAR_MOTOR command

        Returns:
            bool: Final sync state that was applied.
        """
        u = self.mmu_unit()

        bypass_selected = self.gate_selected == TOOL_GATE_BYPASS
        extruder_check_enabled = not skip_extruder_check
        filament_in_extruder = self.filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY

        must_sync = (
            extruder_check_enabled
            and filament_in_extruder
            and u.filament_always_gripped
        )

        out_of_print_sync = (
            bool(sync_intention)
            or must_sync
            or self._standalone_sync
        )

        sync = sync_intention

        # Override caller intent for states where sync is impossible or required.
        if bypass_selected:
            sync = False
        elif extruder_check_enabled and not filament_in_extruder:
            sync = False
        elif must_sync:
            sync = True

        # Caller doesn't know so let's figure it out
        if sync is None:
           if self.is_in_print(force_in_print):
               if self.is_printing(force_in_print):
                   # Actively printing
                   if not filament_in_extruder:
                       sync = False
                   else:
                       # Respect the mmu design or print-time sync setting.
                       sync = u.p.sync_to_extruder or u.filament_always_gripped
               else:
                   # In print context but not actively printing (e.g., paused)
                   sync = out_of_print_sync
           else:
               # Not in a print
               sync = out_of_print_sync

        self.log_stepper(
            f"reset_sync_gear_to_extruder("
            f"sync_intention={sync_intention}, "
            f"force_grip={force_grip}, "
            f"skip_extruder_check={skip_extruder_check}, "
            f"force_in_print={force_in_print}), "
            f"must_sync={must_sync}, "
            f"out_of_print_sync={out_of_print_sync} "
            f"--> sync={sync}"
        )

        # Filament grip handling. This is always lazy to spare unecessary movement.
        # Any filament move with correct the grip if necessary
        if force_grip:
            if sync:
                self.selector().filament_drive()
            else:
                self.selector().filament_release()

        self._sync_gear_to_extruder(sync)
        return sync


    @contextlib.contextmanager
    def wrap_sync_gear_to_extruder(self):
        """
        Preserve sync state across a block. Grip restoration is only performed by
        the outermost wrapper, because it is expensive.

        Yields:
            self while nested operations may temporarily alter sync and grip state.
        """
        # Capture current sync state so it can be restored on exit
#        previous_sync = self.drive().is_synced_to_extruder()      # PAUL don't think we require now?
#        previous_grip = self.selector().get_filament_grip_state() # PAUL don't think we require now?

        if not hasattr(self, "_sync_gear_wrap_depth"):
            self._sync_gear_wrap_depth = 0
        self._sync_gear_wrap_depth += 1

        try:
            yield self

        finally:
            self._sync_gear_wrap_depth -= 1
            outermost = self._sync_gear_wrap_depth == 0
            self.reset_sync_gear_to_extruder(force_grip=outermost)


# -----------------------------------------------------------------------------------------------------------
# TMC Stepper Current Control
# -----------------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def wrap_gear_current(self, percent=100, reason=""):
        """
        Temporarily adjust gear-stepper run current for the duration of a block.

        Args:
            percent: Target current percentage relative to the configured default current.
            reason: Reason text included in diagnostic logging.

        Yields:
            self while the temporary gear-current override is active.
        """
        prev_percent = self._adjust_gear_current(percent=percent, reason=reason)
        self._gear_run_current_locked = True
        try:
            yield self
        finally:
            self._gear_run_current_locked = False
            self._restore_gear_current(percent=prev_percent)


    def _adjust_gear_current(self, gate=None, percent=100, reason="", restore=False):
        """
        Apply a gear-stepper run-current percentage change when allowed.

        Args:
            gate: Gate index to operate on. When omitted, the current selection is used.
            percent: Target current percentage relative to the configured default current.
            reason: Reason text included in diagnostic logging.
            restore: Whether the current-change log message should be phrased as a restore operation.

        Returns:
            int or float: Previously active or currently retained gear-current percentage.
        """
        u = self.mmu_unit(gate)

        current_percent = self.gear_run_current_percent

        if self._gear_run_current_locked:
            return current_percent
        if gate is None:
            gate = self.gate_selected
        if gate < 0:
            return current_percent
        if not (0 < percent < 200):
            return current_percent
        if u.gear_tmc_obj(gate) is None:
            return current_percent
        if percent == self.gear_run_current_percent:
            return current_percent

        sname = u.gear_name(gate)
        if restore:
            msg = "Restoring MMU %s run current to %d%% ({}A)" % (sname, percent)
        else:
            msg = "Modifying MMU %s run current to %d%% ({}A) %s" % (sname, percent, reason)
        target_current = (u.gear_default_current(gate) * percent) / 100.0
        self._set_tmc_current(sname, target_current, msg)
        self.gear_run_current_percent = percent # Update global record of current %
        return percent


    def _restore_gear_current(self, gate=None, percent=100):
        """
        Restore gear-stepper run current to a specified percentage.

        Args:
            gate: Gate index to operate on. When omitted, the current selection is used.
            percent: Target current percentage relative to the configured default current.

        Returns:
            None. Delegates to _adjust_gear_current in restore mode.
        """
        self._adjust_gear_current(gate=gate, percent=percent, restore=True)


    @contextlib.contextmanager
    def _wrap_extruder_current(self, percent=100, reason=""):
        """
        Temporarily adjust extruder run current for the duration of a block.

        Args:
            percent: Target current percentage relative to the configured default current.
            reason: Reason text included in diagnostic logging.

        Yields:
            self while the temporary extruder-current override is active.
        """
        prev_percent = self._adjust_extruder_current(percent, reason)
        try:
            yield self
        finally:
            self._restore_extruder_current(percent=prev_percent)


    def _adjust_extruder_current(self, percent=100, reason="", restore=False):
        """
        Apply an extruder run-current percentage change when allowed.

        Args:
            percent: Target current percentage relative to the configured default current.
            reason: Reason text included in diagnostic logging.
            restore: Whether the current-change log message should be phrased as a restore operation.

        Returns:
            int or float: Previously active or currently retained extruder-current percentage.
        """
        u = self.mmu_unit()

        current_percent = self.extruder_run_current_percent

        if not (0 < percent < 200):
            return current_percent
        if u.extruder_tmc_obj() is None:
            return current_percent
        if percent == self.extruder_run_current_percent:
            return current_percent

        sname = u.extruder_name()
        if restore:
            msg = "Restoring extruder stepper %s run current to %d%% ({}A)" % (sname, percent)
        else:
            msg = "Modifying extruder stepper %s run current to %d%% ({}A) %s" % (sname, percent, reason)
        target_current = (u.extruder_default_current() * percent) / 100.0
        self._set_tmc_current(sname, target_current, msg)
        self.extruder_run_current_percent = percent # Update global record of current %
        return percent


    def _restore_extruder_current(self, percent=100):
        """
        Restore extruder run current to a specified percentage.

        Args:
            percent: Target current percentage relative to the configured default current.

        Returns:
            None. Delegates to _adjust_extruder_current in restore mode.
        """
        self._adjust_extruder_current(percent=percent, restore=True)


    def _set_tmc_current(self, stepper, run_current, msg):
        """
        Send the low-level command that updates a stepper driver current.

        Args:
            stepper: Stepper name to pass to the low-level current command.
            run_current: Absolute current value to apply to the driver.
            msg: Format string used when logging the current change.

        Returns:
            None. Logs and dispatches the low-level current command.
        """
        self.log_info(msg.format("%.2f" % run_current))
        self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (stepper, run_current))


# -----------------------------------------------------------------------------------------------------------
# Pressure Advance Control
# -----------------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _wrap_pressure_advance(self, pa=0, reason=""):
        """
        Temporarily override pressure advance for a block and restore it afterward.

        Args:
            pa: Pressure advance value to apply temporarily or permanently.
            reason: Reason text included in diagnostic logging.

        Yields:
            self while the temporary pressure-advance override is active.
        """
        extruder = self.toolhead.get_extruder()
        initial_pa = extruder.get_status(0).get('pressure_advance')

        if initial_pa is None:
            yield self
            return

        try:
            if reason:
                self.log_debug("Setting pressure advance %s: %.4f" % (reason, pa))
            self._set_pressure_advance(pa)

            yield self

        finally:
            if reason:
                self.log_debug("Restoring pressure advance: %.4f" % initial_pa)
            self._set_pressure_advance(initial_pa)


    def _set_pressure_advance(self, pa):
        """
        Send the low-level command that updates pressure advance.

        Args:
            pa: Pressure advance value to apply temporarily or permanently.

        Returns:
            None. Dispatches the low-level pressure-advance command.
        """
        self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.4f QUIET=1" % pa)
