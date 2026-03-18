# Happy Hare MMU Software
# Main module
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Main control class for any Klipper based MMU (includes filament driver/gear control)
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
from .unit.mmu_calibrator import MmuCalibrator # For check_if_not_calibrated()


class MmuFilamentWorkflow:

# -----------------------------------------------------------------------------------------------------------
# MODULAR FILAMENT LOAD / UNLOAD STEPS
# -----------------------------------------------------------------------------------------------------------

    # Preload selected gate as little as possible. If a full gate load is the only option
    # this will then park correctly after pre-load
    def _preload_gate(self):
        """
        Preload filament at the selected gate using the least invasive strategy available.

        Args:
            None.

        Returns:
            None. Updates gate state in place or raises MmuError when preload fails.
        """
        u = self.mmu_unit()

        gate_sensor = self.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, self.gate_selected)
        if gate_sensor is not None:
            if gate_sensor:
                self.log_always("Filament already preloaded")
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
                return
            else:
                # Minimal load to mmu exit sensor if fitted
                endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, self.gate_selected)
                self.log_always("Preloading...")
                msg = "Homing to %s sensor" % endstop_name
                with self._wrap_suspend_filament_monitoring():
                    actual, homed, measured, _ = self.trace_filament_move(msg, u.p.gate_preload_homing_max, motor="gear", homing_move=1, endstop_name=endstop_name)
                    if homed:
                        self.trace_filament_move("Final parking", -u.p.gate_preload_parking_distance)
                        self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
                        self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                        self.log_always("Filament detected and loaded in gate %d" % self.gate_selected)
                        return
        else:
            # Full gate load if no mmu exit sensor
            for _ in range(u.p.gate_preload_attempts):
                self.log_always("Loading...")
                try:
                    self._load_gate(allow_retry=False)
                    self._check_pending_spool_id(self.gate_selected) # Have spool_id ready?
                    self.log_always("Parking...")
                    _,_ = self._unload_gate()
                    self.log_always("Filament detected and parked in gate %d" % self.gate_selected)
                    return
                except MmuError as ee:
                    # Exception just means filament is not loaded yet, so continue
                    self.log_trace("Exception on preload: %s" % str(ee))

        if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, self.gate_selected):
            self._set_gate_status(self.gate_selected, GATE_UNKNOWN)
            self.log_warning("Filament detected by mmu entry %d sensor but did not complete preload" % self.gate_selected)
        else:
            self._set_gate_status(self.gate_selected, GATE_EMPTY)
            raise MmuError("Filament not detected")


    # Eject final clear of gate. Important for MMU's where filament is always gripped (e.g. most type-B)
    def _eject_from_gate(self, gate=None):
        """
        Fully eject filament from a gate so it can be removed safely.

        Args:
            gate: Gate index to operate on. When omitted, the current selection is used.

        Returns:
            None. Updates gate and filament state in place.
        """
        u = self.mmu_unit()

        # If gate not specified assume current gate
        if gate is None:
            gate = self.gate_selected
        else:
            self.select_gate(gate)

        self.selector().filament_drive()

        self.log_always("Ejecting...")
        if self.sensor_manager.has_gate_sensor(SENSOR_EXIT_PREFIX, gate):
            endstop_name = self.sensor_manager.get_gate_sensor_name(SENSOR_EXIT_PREFIX, gate)
            msg = "Reverse homing off %s sensor" % endstop_name
            actual, homed, measured, _ = self.trace_filament_move(msg, -u.p.gate_homing_max, motor="gear", homing_move=-1, endstop_name=endstop_name)
            if homed:
                self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
            else:
                raise MmuError("Filament did not exit gate homing sensor: %s" % endstop_name)

        if u.p.gate_final_eject_distance > 0:
            msg = "Ejecting filament out of gate"
            if self.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, gate) is not None:
                # Use homing move so we don't "over eject"
                self.trace_filament_move(msg, -u.p.gate_final_eject_distance, motor="gear", homing_move=-1, endstop_name=SENSOR_ENTRY_PREFIX, wait=True)
            else:
                self.trace_filament_move(msg, -u.p.gate_final_eject_distance, wait=True)

        self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True) # Should already be in this position
        self._set_gate_status(gate, GATE_EMPTY)
        self.log_always("The filament in gate %d can be removed" % gate)


    # Load filament into gate. This is considered the starting position for the rest of the filament loading
    # process. Note that this may overshoot the home position for the "encoder" technique but subsequent
    # bowden move will accommodate. Also for systems with gate sensor and encoder with gate sensor first,
    # there will be a gap in encoder readings that must be taken into consideration.
    # Return the overshoot past homing point
    def _load_gate(self, allow_retry=True):
        """
        Load filament into the selected gate and establish the gate starting position.

        Args:
            allow_retry: Whether retry attempts are allowed when the first load attempt fails.

        Returns:
            float: Overshoot past the gate homing point that later stages should account for.
        """
        u = self.mmu_unit()

        self._validate_gate_config("load")
        self._set_filament_direction(DIRECTION_LOAD)
        self.selector().filament_drive()
        retries = u.p.gate_load_retries if allow_retry else 1

        if u.p.gate_homing_endstop == SENSOR_ENCODER:
            with self._require_encoder():
                measured = 0.
                for i in range(retries):
                    msg = "Initial load into encoder" if i == 0 else ("Retry load into encoder (retry #%d)" % i)
                    _, _, m, _ = self.trace_filament_move(msg, u.p.gate_homing_max)
                    measured += m
                    if m > 6.0:
                        self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                        self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)
                        return measured
                    else:
                        self.log_debug("Error loading filament - filament motion was not detected by the encoder. %s" % ("Retrying..." if i < retries - 1 else ""))
                        if i < retries - 1:
                            self.selector().filament_release()
                            self.selector().filament_drive()

        else:  # Gate sensor... SENSOR_SHARED_EXIT is shared, but SENSOR_EXIT_PREFIX is gate specific
            for i in range(retries):
                endstop_name = self.sensor_manager.get_mapped_endstop_name(u.p.gate_homing_endstop)
                msg = ("Initial homing to %s sensor" % endstop_name) if i == 0 else ("Retry homing to gate sensor (retry #%d)" % i)
                h_dir = -1 if u.p.gate_parking_distance < 0 and self.sensor_manager.check_sensor(endstop_name) else 1 # Reverse home?
                actual, homed, measured, _ = self.trace_filament_move(msg, h_dir * u.p.gate_homing_max, motor="gear", homing_move=h_dir, endstop_name=endstop_name)
                if homed:
                    self.log_debug("Endstop %s reached after %.1fmm (measured %.1fmm)" % (endstop_name, actual, measured))
                    self._set_gate_status(self.gate_selected, max(self.gate_status[self.gate_selected], GATE_AVAILABLE)) # Don't reset if filament is buffered
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    return 0.
                else:
                    self.log_debug("Error loading filament - filament did not reach gate homing sensor. %s" % ("Retrying..." if i < retries - 1 else ""))
                    if i < retries - 1:
                        self.selector().filament_release()
                        self.selector().filament_drive()

        self._set_gate_status(self.gate_selected, GATE_EMPTY)
        self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
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
            tuple[float, float]: Actual homing distance performed and expected homing distance.
        """
        u = self.mmu_unit()

        self._validate_gate_config("unload")
        self._set_filament_direction(DIRECTION_UNLOAD)
        self.selector().filament_drive()

        # Figure out homing buffer
        recovery = False
        if extra_homing is None:
            recovery = True
            expected_homing = u.calibrator.get_bowden_length()
        else:
            expected_homing = extra_homing
        homing_max = expected_homing + u.p.gate_homing_max

        if recovery: # Means recovery operation

            # Safety step because this method is used as a defensive way to unload the entire bowden from unknown position
            # It handles the cases of filament still in extruder with no toolhead sensor or, if toolhead sensor is available,
            # the small window where filament is between extruder entrance and toolhead sensor
            length = self.p.toolhead_extruder_to_nozzle
            if self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                length -= self.p.toolhead_sensor_to_nozzle # Can safely reduce the base move distance because starting point in toolhead sensor
            length += u.p.toolhead_unload_safety_margin # Add safety margin

            self.log_debug("Performing synced pre-unload bowden move of %.1fmm to ensure filament is not trapped in extruder" % length)

            if u.p.gate_homing_endstop == SENSOR_ENCODER:
                _,_,_,_ = self.trace_filament_move("Bowden safety pre-unload move", -length, motor="gear+extruder")

            else:
                endstop_name = self.sensor_manager.get_mapped_endstop_name(u.p.gate_homing_endstop)
                actual, homed, _, _ = self.trace_filament_move("Bowden safety pre-unload move", -length, motor="gear+extruder", homing_move=-1, endstop_name=endstop_name)

                # In case we ended up homing during the safety pre-unload, lets just do our parking and be done
                # This can easily happen when your parking distance is configured to park the filament past the
                # gate sensor instead of behind the gate sensor and the filament position is determined to be
                # "somewhere in the bowden tube"
                if homed:
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                    self.trace_filament_move("Final parking", -u.p.gate_parking_distance) # PAUL TODO reverse sign
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return actual, expected_homing

        if u.p.gate_homing_endstop == SENSOR_ENCODER:

            with self._require_encoder():
                if recovery:
                    self.log_info("Slowly unloading bowden because unsure of filament position...")
                else:
                    self.log_trace("Unloading gate using the encoder")

                success = self._reverse_home_to_encoder(homing_max)
                if success:
                    actual, park, _ = success
                    _, _, measured, _ = self.trace_filament_move("Final parking", -park) # PAUL TODO reverse sign

                    # We don't expect any movement of the encoder unless it is free-spinning
                    if measured > self.encoder().movement_min(): # We expect 0, but relax the test a little (allow one pulse)
                        self.log_warning("Warning: Possible encoder malfunction (free-spinning) during final filament parking")
                    self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    return actual, expected_homing

                msg = "did not clear the encoder after moving %.1fmm" % homing_max

        else:  # Using mmu_shared_exit or mmu_exit_N sensor

            endstop_name = self.sensor_manager.get_mapped_endstop_name(u.p.gate_homing_endstop)
            actual, homed, _, _ = self.trace_filament_move("Reverse homing off %s sensor" % endstop_name, -homing_max, motor="gear", homing_move=-1, endstop_name=endstop_name)
            if homed:
                self._set_filament_pos_state(FILAMENT_POS_HOMED_GATE)
                self.trace_filament_move("Final parking", -u.p.gate_parking_distance) # PAUL TODO reverse sign
                self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                return actual, expected_homing

            msg = "did not home to sensor '%s' after moving %1.fmm" % (u.p.gate_homing_endstop, homing_max)

        raise MmuError("Failed to unload gate because %s" % msg)


    # Shared with manual bowden calibration routine
    def _reverse_home_to_encoder(self, homing_max):
        """
        Step filament backward until it clears the encoder or the move budget is exhausted.

        Args:
            homing_max: Maximum distance available for the homing operation.

        Returns:
            tuple[float, float, float] or None: Actual reverse travel, final parking distance, and cumulative encoder delta, or None if the encoder was not cleared.
        """
        u = self.mmu_unit()

        emss = u.p.encoder_move_step_size
        max_steps = int(math.ceil(homing_max / emss))
        delta = 0.
        actual = 0.
        for i in range(max_steps):
            msg = "Unloading step #%d from encoder" % (i+1)
            sactual, _, _, sdelta = self.trace_filament_move(msg, -emss)
            delta += sdelta
            actual -= sactual
            # Large enough delta here means we are out of the encoder
            if sdelta >= emss * 0.2: # 20 %
                actual -= sdelta
                park = u.p.gate_parking_distance - sdelta # will be between 8 and 20mm (for 23mm gate_parking_distance, 15mm step)
                return actual, park, delta
        self.log_debug("Filament did not clear encoder even after moving %.1fmm" % (emss * max_steps))
        return None


    # Shared gate functions to deduplicate logic
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


    # Fast load of filament in bowden, usually the full length but if 'full' is False a specific length can be specified
    # Note that filament position will be measured from the gate "parking position" and so will be the gate_parking_distance
    # plus any overshoot. The start of the bowden move is from the parking homing point.
    # Returns ratio of measured movement to real movement IF it is "clean" and could be used for auto-calibration else 0
    def _load_bowden(self, length=None, start_pos=0.):
        """
        Perform the fast bowden load portion between the gate and the extruder area.

        Args:
            length: Requested bowden travel distance. When omitted, the calibrated bowden length is used.
            start_pos: Distance already consumed before the bowden move begins, such as gate overshoot.

        Returns:
            tuple[float, float]: Usable move-to-measurement ratio and any homing buffer reserved for later stages.
        """
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
            homing_buffer = 0.
            if full:
                # We will need some buffer space if we are intending to home
                if self._must_buffer_extruder_homing():
                    # Calculate the anticipated homing distance to the extruder
                    # (how much to reduce the fast move portion)
                    homing_buffer = bowden_length * ((100 - u.p.bowden_fast_load_portion) / 100)

                    # Further reduce to compensate for distance from extruder entry sensor to extruder gear
                    # because the bowden length is always recorded as distance to extruder gear
                    if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                        homing_buffer -= self.p.toolhead_entry_to_extruder

                length -= homing_buffer # Reduce fast move distance

            if length > 0:
                self.log_debug("Loading bowden tube")
                self._set_filament_direction(DIRECTION_LOAD)
                self.selector().filament_drive()
                self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = (self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()) - start_pos

                if self.gate_selected > 0 and not u.is_gear_rd_calibrated():
                    self.log_warning("Warning: gate %d not calibrated! Using default rotation distance" % self.gate_selected)

                # "Fast" load
                _, _, _, delta = self.trace_filament_move("Fast loading move through bowden", length, track=True, encoder_dwell=bool(u.p.autotune_encoder))
                delta -= self._get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self._can_use_encoder() and delta >= length * (u.p.bowden_move_error_tolerance / 100.) and not self.calibrating:
                    raise MmuError("Failed to load bowden. Perhaps filament is stuck in gate. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                # Encoder based validation test
                if self._can_use_encoder() and delta >= u.p.bowden_allowable_load_delta and not self.calibrating:
                    ratio = 0. # Not considered valid for auto-calibration
                    # Correction attempts to load the filament according to encoder reporting
                    if u.p.bowden_apply_correction:
                        for i in range(2):
                            if delta >= u.p.bowden_allowable_load_delta:
                                msg = "Correction load move #%d into bowden" % (i+1)
                                _,_,_,d = self.trace_filament_move(msg, delta, track=True)
                                delta = d
                                self.log_debug("Correction load move was necessary, encoder now measures %.1fmm" % self.get_encoder_distance())
                            else:
                                self.log_debug("Correction load complete, delta %.1fmm is less than 'bowden_allowable_unload_delta' (%.1fmm)" % (delta, u.p.bowden_allowable_load_delta))
                                break
                        self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                        if delta >= u.p.bowden_allowable_load_delta:
                            self.log_warning("Warning: Excess slippage was detected in bowden tube load afer correction moves. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details"% (length, length - delta))
                    else:
                        self.log_warning("Warning: Excess slippage was detected in bowden tube load but 'bowden_apply_correction' is disabled. Gear moved %.1fmm, Encoder measured %.1fmm. See mmu.log for more details" % (length, length - delta))

                    if delta >= u.p.bowden_allowable_load_delta:
                        self.log_debug("Possible causes of slippage:\nCalibration ref length too long (hitting extruder gear before homing)\nCalibration ratio for gate is not accurate\nMMU gears are not properly gripping filament\nEncoder reading is inaccurate\nFaulty servo")

                self._random_failure() # Testing
                self.movequeues_wait()
            else:
                # No bowden movement required
                ratio = 1.

            if full:
                self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)
            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = 0.

            # Return the ratio of move vs encoder measurement and the amount of buffer space (under load)
            # This is used for auto-calibration and for determining how far left to extruder
            return ratio, homing_buffer

        finally:
            self.bowden_start_pos = None


    def _unload_bowden(self, length=None):
        """
        Perform the fast bowden unload portion from the extruder side back toward the MMU.

        Args:
            length: Requested bowden travel distance. When omitted, the calibrated bowden length is used.

        Returns:
            float: Ratio of measured unload movement to commanded movement, used for calibration and telemetry.
        """
        u = self.mmu_unit()

        bowden_length = u.calibrator.get_bowden_length()
        if length is None:
            length = bowden_length
        if bowden_length > 0 and not self.calibrating:
            length = min(length, bowden_length) # Cannot exceed calibrated distance
        full = length == bowden_length

        # Shorten move by % to provide gate unload buffer used to ensure we don't overshoot homing point
        gate_homing_buffer = bowden_length * ((100 - u.p.bowden_fast_unload_portion) / 100)
        length -= gate_homing_buffer

        try:
            if length > 0:
                self.log_debug("Unloading bowden tube")
                self._set_filament_direction(DIRECTION_UNLOAD)
                self.selector().filament_drive()

                # Optional pre-unload safety step
                if (full and self.has_encoder() and u.p.bowden_pre_unload_test and
                    self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY) is not False and
                    self.sensor_manager.check_all_sensors_before(FILAMENT_POS_START_BOWDEN, self.gate_selected, loading=False) is not False
                ):
                    with self._require_encoder():
                        emss = u.p.encoder_move_step_size
                        self.log_debug("Performing bowden pre-unload test")
                        _, _, _, delta = self.trace_filament_move("Bowden pre-unload test", -emss)
                        if delta > emss * (u.p.bowden_pre_unload_error_tolerance / 100.):
                            self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
                            raise MmuError("Bowden pre-unload test failed. Filament seems to be stuck in the extruder or filament not loaded\nOptionally use MMU_RECOVER to recover filament position")
                        length -= emss
                        self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

                # Record starting position for bowden progress tracking. Prefer encoder if available
                self.bowden_start_pos = self.get_encoder_distance(dwell=None) if self.has_encoder() else self._get_live_filament_position()

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
                _, _, _, delta = self.trace_filament_move("Fast unloading move through bowden", -length, track=True, encoder_dwell=bool(u.p.autotune_encoder))
                delta -= self._get_encoder_dead_space()
                ratio = (length - delta) / length

                # Encoder based validation test
                if self._can_use_encoder() and delta >= u.p.bowden_allowable_unload_delta and not self.calibrating:
                    ratio = 0.
                    # Only a warning because _unload_gate() will deal with it
                    self.log_warning("Warning: Excess slippage was detected in bowden tube unload. Gear moved %.1fmm, Encoder measured %.1fmm" % (length, length - delta))

                self._random_failure() # Testing
                self.movequeues_wait()
            else:
                # No bowden movement required
                ratio = 1.

            if full:
                self._set_filament_pos_state(FILAMENT_POS_START_BOWDEN)
            elif self.filament_pos != FILAMENT_POS_IN_BOWDEN:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)
                ratio = 0.
            return ratio # For auto-calibration

        finally:
            self.bowden_start_pos = None


    def _home_to_extruder(self, homing_max):
        """
        Home filament to the configured extruder-side reference point when required.

        Args:
            homing_max: Maximum distance available for the homing operation.

        Returns:
            tuple[float or None, float]: Actual homing movement, if applicable, and any calibration adjustment that should be applied.
        """
        u = self.mmu_unit()

        self._set_filament_direction(DIRECTION_LOAD)
        self.selector().filament_drive()
        measured = adjustment = 0.
        homing_movement = None

        if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_NONE:
            homed = True

        elif u.p.extruder_homing_endstop == SENSOR_EXTRUDER_COLLISION:
            if self.has_encoder():
                actual, homed, measured, _ = self._home_to_extruder_collision_detection(homing_max)
                homing_movement = actual
            else:
                raise MmuError("Cannot home to extruder using 'collision' method because encoder is not configured or disabled!")

        else:
            self.log_debug("Homing to extruder '%s' endstop, up to %.1fmm" % (u.p.extruder_homing_endstop, homing_max))
            actual, homed, measured, _ = self.trace_filament_move("Homing filament to extruder endstop", homing_max, motor="gear", homing_move=1, endstop_name=u.p.extruder_homing_endstop)
            if homed:
                self.log_debug("Extruder endstop '%s' reached after %.1fmm (measured %.1fmm)" % (u.p.extruder_homing_endstop, actual, measured))
                self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)

                if u.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                    # Close the fixed gap from the entry sensor to the extruder gear
                    _, _, measured, _ = self.trace_filament_move("Aligning filament to extruder gear", self.p.toolhead_entry_to_extruder, motor="gear")

                elif u.p.extruder_homing_endstop == SENSOR_COMPRESSION:
                    # Estimate the midpoint of buffer for accurate bowden length determination
                    adjustment = -(u.sync_feedback.p.sync_feedback_buffer_range / 2.)

            homing_movement = actual

        if not homed:
            self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)
            raise MmuError("Failed to reach extruder '%s' endstop after moving %.1fmm" % (u.p.extruder_homing_endstop, homing_max))

        if measured > (homing_max * 0.8):
            self.log_warning("Warning: 80%% of 'extruder_homing_max' was used homing. You may want to increase 'extruder_homing_max'")

        self._set_filament_pos_state(FILAMENT_POS_HOMED_EXTRUDER)

        return homing_movement, adjustment


    # Special extruder homing option for detecting the collision base on lack of encoder movement
    def _home_to_extruder_collision_detection(self, homing_max):
        """
        Home to the extruder gear using encoder-based collision detection.

        Args:
            homing_max: Maximum distance available for the homing operation.

        Returns:
            tuple[float, bool, float, float]: Actual move, homing success flag, measured movement, and cumulative encoder delta.
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
                _,_,smeasured,sdelta = self.trace_filament_move(msg, step, speed=u.p.gear_homing_speed)
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

        self._set_filament_position(self._get_filament_position() - step) # Ignore last step movement
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
        u = self.mmu_unit()

        with self.wrap_action(ACTION_LOADING_EXTRUDER):
            self.log_debug("Loading filament into extruder")
            self._set_filament_direction(DIRECTION_LOAD)

            # Important to wait for filaments with wildly different print temps. In practice, the time taken
            # to perform a swap should be adequate to reach the target temp but better safe than sorry
            self._ensure_safe_extruder_temperature(wait=True)
            bowden_extra = None

            has_tension = self.sensor_manager.has_sensor(SENSOR_TENSION)
            has_compression = self.sensor_manager.has_sensor(SENSOR_COMPRESSION)
            has_proportional = self.sensor_manager.has_sensor(SENSOR_PROPORTIONAL)
            has_toolhead = self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)

            synced = not extruder_only
            if synced:
                self.selector().filament_drive()
                speed = self.p.extruder_sync_load_speed
                motor = "gear+extruder"
            else:
                self.selector().filament_release()
                speed = self.p.extruder_load_speed
                motor = "extruder"

            fhomed = False
            if has_toolhead:
                # With toolhead sensor for accuracy we always first home to toolhead sensor past the extruder entrance
                # The remaining load distance is relative to the toolhead sensor
                if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                    raise MmuError("Possible toolhead sensor malfunction - filament detected before it entered extruder")
                homing_max = extra_homing + u.p.toolhead_homing_max
                self.log_debug("Homing up to %.1fmm to toolhead sensor%s" % (homing_max, (" (synced)" if synced else "")))
                actual, fhomed, measured, _ = self.trace_filament_move("Homing to toolhead sensor", homing_max, motor=motor, homing_move=1, endstop_name=SENSOR_TOOLHEAD)
                if fhomed:
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                    # Bowden part of move is homing distance minus the distance between entrance and toolhead sensor
                    bowden_extra = max(actual - (self.p.toolhead_extruder_to_nozzle - self.p.toolhead_sensor_to_nozzle), 0)
                else:
                    if self.gate_selected != TOOL_GATE_BYPASS:
                        self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                    else:
                        # For bypass its best to assume we didn't enter the extruder at all
                        self._set_filament_pos_state(FILAMENT_POS_UNLOADED)
                    raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % u.p.toolhead_homing_max)

            # Length may be reduced by previous unload in filament cutting use case. Ensure reduction is used only one time
            d = self.p.toolhead_sensor_to_nozzle if has_toolhead else self.p.toolhead_extruder_to_nozzle
            length = max(d - self.filament_remaining - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract, 0)

            # If we have a compression sensor indicating compression we can detect failure in the critical extruder entrance transition
            # by performing the initial load with just the extruder motor and checking that the sensor un-triggers before continuing
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and u.p.toolhead_entry_tension_test
                and synced
                and not has_toolhead
                and self.sensor_manager.check_sensor(SENSOR_COMPRESSION)
            ):
                max_range = u.sync_feedback.p.sync_feedback_buffer_maxrange * 2 # Arbitary but buffer_maxrange is not enough to overcome bowden slack
                if length > max_range:
                    self.log_debug("Monitoring extruder entrance transition for up to %.1fmm..." % max_range)
                    actual, success = u.sync_feedback.adjust_filament_tension(use_gear_motor=False, max_move=max_range)
                    if success:
                        length -= actual
                    else:
                        self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY) # But could also still be POS_IN_BOWDEN!
                        raise MmuError("Failed to load filament passed the extruder entrance (sync-feedback buffer didn't detect neutral tension)")

            self.log_debug("Loading last %.1fmm to the nozzle..." % length)
            _,_,measured,delta = self.trace_filament_move("Loading filament to nozzle", length, speed=speed, motor=motor, wait=True)
            self._set_filament_remaining(0.)

            # Encoder based validation test to validate the filament was picked up by extruder. This runs if we are
            # short of deterministic sensors and test makes sense
            if (
                self.gate_selected != TOOL_GATE_BYPASS
                and self._can_use_encoder()
                and not fhomed
                and not extruder_only
            ):
                self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                if measured < self.encoder().movement_min():
                    raise MmuError("Move to nozzle failed (encoder didn't sense any movement). Extruder may not have picked up filament or filament did not find homing sensor")
                elif delta > length * (u.p.toolhead_move_error_tolerance / 100.):
                    self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
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
                    and self._can_use_encoder()
                    and u.sync_feedback.p.flowguard_encoder_mode
                ):
                    # Tightening move to prevent erroneous encoder clog detection/runout if gear stepper is not synced with extruder
                    with self.wrap_gear_current(percent=50, reason="to tighten filament in bowden"):
                        # Filament will already be gripped so perform fixed MMU only retract
                        pullback = min(self.encoder().get_clog_detection_length() * u.p.toolhead_post_load_tighten / 100, 15) # % of current clog detection length or 15mm min
                        _,_,measured,delta = self.trace_filament_move("Tighening filament in bowden", -pullback, motor="gear", wait=True)
                        self.log_info("Filament tightened by %.1fmm to prevent false clog detection" % pullback)

                elif (
                    u.p.toolhead_post_load_tension_adjust
                    and (u.p.sync_to_extruder or u.p.sync_purge)
                    and (has_tension or has_compression or has_proportional)
                    and u.sync_feedback.is_enabled()
                ):
                    # Try to put filament in neutral tension by centering between sensors
                    # Two methods are available based on switch only sensors or proportional feedback
                    actual, success = u.sync_feedback.adjust_filament_tension()
                    if success:
                        self.log_info("Filament tension in bowden successfully relaxed")
                    else:
                        self.log_warning("Unsuccessful in relaxing filament tension after adjusting %.1fmm" % actual)

            self._random_failure() # Testing
            self.movequeues_wait()
            self._set_filament_pos_state(FILAMENT_POS_LOADED)
            self.log_debug("Filament should be loaded to nozzle")
            return bowden_extra # Will only have value if we have toolhead sensor


    # Extract filament past extruder gear (to end of bowden). Assume that tip has already been formed
    # and we are parked somewhere in the extruder either by slicer or by stand alone tip creation
    # But be careful:
    #   A poor tip forming routine or slicer could have popped the filament out of the extruder already
    # Ending point is either the exit of the extruder or at the extruder (entry) endstop if fitted
    # Return True if we were synced
    def _unload_extruder(self, extruder_only=False, validate=True):
        """
        Extract filament from the extruder back to the bowden entrance region.

        Args:
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.
            validate: Whether encoder-based validation should be performed during unload.

        Returns:
            bool: True when the unload finished in a synced state; otherwise False.
        """
        u = self.mmu_unit()

        with self.wrap_action(ACTION_UNLOADING_EXTRUDER):
            self.log_debug("Extracting filament from extruder")
            self._set_filament_direction(DIRECTION_UNLOAD)

            self._ensure_safe_extruder_temperature(wait=False)

            synced = self.selector().get_filament_grip_state() == FILAMENT_DRIVE_STATE and not extruder_only
            if synced:
                self.selector().filament_drive()
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"
            else:
                self.selector().filament_release()
                speed = self.p.extruder_unload_speed
                motor = "extruder"

            fhomed = False
            if self.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY) and not extruder_only:
                # BEST Strategy: Extruder exit movement leveraging extruder entry sensor. Must be synced
                synced = True
                self.selector().filament_drive()
                speed = self.p.extruder_sync_unload_speed
                motor = "gear+extruder"

                if not self.sensor_manager.check_sensor(SENSOR_EXTRUDER_ENTRY):
                    if self.sensor_manager.check_sensor(SENSOR_TOOLHEAD):
                        raise MmuError("Toolhead or extruder sensor failure. Extruder sensor reports no filament but toolhead sensor is still triggered")
                    else:
                        self.log_warning("Warning: Filament was not detected by extruder (entry) sensor at start of extruder unload\nWill attempt to continue...")
                        fhomed = True # Assumption
                else:
                    hlength = self.p.toolhead_extruder_to_nozzle + self.p.toolhead_entry_to_extruder + u.p.toolhead_unload_safety_margin - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract
                    self.log_debug("Reverse homing up to %.1fmm off extruder sensor (synced) to exit extruder" % hlength)
                    _, fhomed, _, _ = self.trace_filament_move("Reverse homing off extruder sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_EXTRUDER_ENTRY)

                if not fhomed:
                    raise MmuError("Failed to reach extruder entry sensor after moving %.1fmm" % hlength)
                else:
                    validate = False
                    # We know exactly where end of filament is so true up
                    self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY)
                    self._set_filament_position(-(self.p.toolhead_extruder_to_nozzle + self.p.toolhead_entry_to_extruder))

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
                        hlength = self.p.toolhead_sensor_to_nozzle + u.p.toolhead_unload_safety_margin - self.p.toolhead_residual_filament - self.p.toolhead_ooze_reduction - self.toolchange_retract
                        self.log_debug("Reverse homing up to %.1fmm off toolhead sensor%s" % (hlength, (" (synced)" if synced else "")))
                        _, fhomed, _, _ = self.trace_filament_move("Reverse homing off toolhead sensor", -hlength, motor=motor, homing_move=-1, endstop_name=SENSOR_TOOLHEAD)
                    if not fhomed:
                        raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % hlength)
                    else:
                        validate = False
                        # We know exactly where end of filament is so true up
                        self._set_filament_pos_state(FILAMENT_POS_HOMED_TS)
                        self._set_filament_position(-self.p.toolhead_sensor_to_nozzle)

                # Finish up with regular extruder exit movement. Optionally synced
                length = max(0, self.p.toolhead_extruder_to_nozzle + self._get_filament_position()) + u.p.toolhead_unload_safety_margin
                self.log_debug("Unloading last %.1fmm to exit the extruder%s" % (length, " (synced)" if synced else ""))
                _,_,measured,delta = self.trace_filament_move("Unloading extruder", -length, speed=speed, motor=motor, wait=True)

                # Best guess of filament position is right at extruder entrance or just beyond if synced
                if synced:
                    self._set_filament_position(-(self.p.toolhead_extruder_to_nozzle + u.p.toolhead_unload_safety_margin))
                else:
                    self._set_filament_position(-self.p.toolhead_extruder_to_nozzle)

                # Encoder based validation test if it has high chance of being useful
                # NOTE: This check which used to raise MmuError() is tripping many folks up because they have poor tip forming
                #       logic so just log error and continue. This disguises the root cause problem but will make folks happier
                #       Not performed for slicer tip forming (validate=True) because everybody is ejecting the filament!
                if validate and self._can_use_encoder() and length > u.p.encoder_move_step_size and not extruder_only and self.gate_selected != TOOL_GATE_BYPASS:
                    self.log_debug("Total measured movement: %.1fmm, total delta: %.1fmm" % (measured, delta))
                    msg = None
                    if measured < self.encoder().movement_min():
                        msg = "any"
                    elif synced and delta > length * (u.p.toolhead_move_error_tolerance / 100.):
                        msg = "sufficient"
                    if msg:
                        self.log_warning("Warning: Encoder not sensing %s movement during final extruder retraction move\nConcluding filament either stuck in the extruder, tip forming erroneously completely ejected filament or filament was not fully loaded\nWill attempt to continue..." % msg)

                self._set_filament_pos_state(FILAMENT_POS_END_BOWDEN)

            self._random_failure() # Testing
            self.movequeues_wait()
            self.log_debug("Filament should be out of extruder")
            return synced


# -----------------------------------------------------------------------------------------------------------
# LOAD / UNLOAD SEQUENCES
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
        self.movequeues_wait()

        bowden_length = u.calibrator.get_bowden_length() # -1 if not calibrated yet
        calibrated = (bowden_length >= 0)
        initial_calibration = not calibrated and not extruder_only

        # Default bowden move if not specified is full length
        if bowden_move is None:
            bowden_move = bowden_length

        if calibrated and bowden_move > bowden_length:
            bowden_move = bowden_length
            self.log_warning("Warning: Restricting bowden unload length to calibrated value of %.1fmm" % bowden_length)

        # Convenience flags
        full = (bowden_move == bowden_length)
        macros_and_track = not extruder_only and full

        self._set_filament_direction(DIRECTION_LOAD)
        self._initialize_filament_position(dwell=None) # Reset measurement to 0

        try:
            must_home_extruder = False
            if not extruder_only:
                current_action = self._set_action(ACTION_LOADING)
                if full:
                    must_home_extruder = self._must_home_to_extruder() or initial_calibration
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

            homing_expected = 0.   # Amount of homing that would be expected (because bowden load is shortened)
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
            start_overshoot = 0.   # How far we overshoot the gate homing point (encoder based homing)
            calibrated_bowden_length = None
            start_filament_pos = self.filament_pos

            # Note: Conditionals deliberately coded this way to match macro alternative
            if self.p.gcode_load_sequence and not initial_calibration:
                self.log_debug("Calling external user defined loading sequence macro")
                self.wrap_gcode_command("%s FILAMENT_POS=%d LENGTH=%.1f FULL=%d HOME_EXTRUDER=%d SKIP_EXTRUDER=%d EXTRUDER_ONLY=%d" % (self.p.load_sequence_macro, start_filament_pos, bowden_move, int(full), int(must_home_extruder), int(skip_extruder), int(extruder_only)), exception=True)

            elif extruder_only:
                if start_filament_pos < FILAMENT_POS_EXTRUDER_ENTRY:
                    _ = self._load_extruder(extruder_only=True)
                else:
                    raise MmuError("Cannot load extruder because filament already in extruder (state: %s). Unload first" % start_filament_pos)

            elif start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                raise MmuError("Cannot load because filament already in extruder (state: %s). Unload first" % start_filament_pos)

            else:
                if start_filament_pos <= FILAMENT_POS_UNLOADED:
                    start_overshoot = self._load_gate()

                if initial_calibration:
                    if u.p.extruder_homing_endstop in [SENSOR_EXTRUDER_NONE, SENSOR_EXTRUDER_COLLISION]:
                        raise MmuError("Auto calibration is not possible with 'extruder_homing_endstop: %s'" % SENSOR_EXTRUDER_NONE)

                    self.log_warning("Auto calibrating bowden length on gate %d using %s as gate reference point" % (self.gate_selected, self._gate_homing_string()))

                    if self.sensor_manager.check_sensor(u.p.extruder_homing_endstop):
                        raise MmuError("The %s sensor triggered before homing. Check filament and sensor operation" % u.p.extruder_homing_endstop)

                    # Slow homing move for the max permissible distance
                    homing_max = u.p.bowden_homing_max
                    actual_homing, adjustment = self._home_to_extruder(homing_max)
                    if actual_homing is None:
                        raise MmuError("Failed to auto calibrate bowden because unable to home to extruder after moving %.1fmm\nIf you have a very long bowden you may need to increase 'bowden_homing_max'" % homing_max)

                    calibrated_bowden_length = start_overshoot + actual_homing + adjustment

                    if not skip_extruder:
                        self._load_extruder()

                    # Notify calibration manager
                    u.calibrator.update_bowden_calibration(calibrated_bowden_length)

                else:

                    # Normal non initial_calibration load ---------------------
                    homing_buffer = 0.
                    adjustment = 0.
                    bowden_start = self._get_filament_position()

                    if start_filament_pos < FILAMENT_POS_END_BOWDEN:
                        # Homing buffer is the shortfall in desired bowden move
                        bowden_move_ratio, homing_buffer = self._load_bowden(bowden_move, start_pos=start_overshoot)

                    if start_filament_pos < FILAMENT_POS_HOMED_EXTRUDER:
                        if must_home_extruder:
                            actual_homing, adjustment = self._home_to_extruder(homing_buffer + u.p.extruder_homing_max)
                            homing_buffer = 0. # Don't reuse

                        elif self.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                            pass # _load_extruder() will consume the homing buffer in next step

                        else:
                            # We are not homing so will just complete the bowden move with a slower (after movement)
                            speed = u.p.gear_short_move_speed
                            accel = u.p.gear_short_move_accel
                            _, _, _, delta = self.trace_filament_move("Slow move to extruder entrance", homing_buffer, motor="gear")
                            homing_buffer = 0. # Don't reuse

                    bowden_travel = self._get_filament_position() - bowden_start + adjustment

                    if not skip_extruder:
                        bowden_extra = self._load_extruder(extra_homing=homing_buffer)
                        if bowden_extra is not None:
                            # This means we employed homing to the toolhead sensor so adjust effective bowden move
                            bowden_travel += bowden_extra

                    # Notify calibration manager
                    if full and not extruder_only and not self.p.gcode_load_sequence:
                        u.calibrator.load_telemetry(bowden_move, bowden_move_ratio, bowden_travel)

            self.movequeues_wait()
            msg = "Load of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = u.p.gate_parking_distance + self._get_encoder_dead_space()
                msg += " {1}(adjusted encoder: %.1fmm){0}" % (final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # Activate loaded spool in Spoolman
            self._spoolman_activate_spool(self.gate_spool_id[self.gate_selected])

            # Deal with purging
            if purge == PURGE_SLICER and not skip_extruder:
                self.log_debug("Purging expected to be performed by slicer")

            elif purge == PURGE_STANDALONE and not skip_extruder:
                with self._wrap_track_time('purge'):

                    # Restore the expected sync state now before running this macro
                    # (we also must force correction of filament grip for old blobifer/unsynced functionality)
                    self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_purge, force_grip=True)

                    with self.wrap_action(ACTION_PURGING):
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


    def _must_home_to_extruder(self):
        """
        Determine whether a dedicated extruder homing step is required before loading.

        Args:
            None.

        Returns:
            bool: True when a dedicated extruder homing move is required before final loading.
        """
        u = self.mmu_unit()

        has_extruder_endstop = u.p.extruder_homing_endstop != SENSOR_EXTRUDER_NONE
        force_homing = u.p.extruder_force_homing
        toolhead_sensor_missing = not self.sensor_manager.has_sensor(SENSOR_TOOLHEAD)

        return has_extruder_endstop and (force_homing or toolhead_sensor_missing)


    def _must_buffer_extruder_homing(self):
        """
        Determine whether fast bowden loading must reserve distance for extruder homing.

        Args:
            None.

        Returns:
            bool: True when fast bowden loading must leave room for a later homing move.
        """
        u = self.mmu_unit()

        if not self._must_home_to_extruder():
            return False

        return u.p.extruder_homing_endstop != SENSOR_EXTRUDER_COLLISION


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

        self.movequeues_wait()

        bowden_length = u.calibrator.get_bowden_length() # -1 if not calibrated yet
        calibrated = (bowden_length >= 0)
        if bowden_length < 0:
            bowden_length = u.p.bowden_homing_max # Special case - if not calibrated then apply the max possible bowden length

        # Default bowden move if not specified is full length
        if bowden_move is None:
            bowden_move = bowden_length

        if calibrated and bowden_move > bowden_length:
            bowden_move = bowden_length
            self.log_warning("Warning: Restricting bowden unload length to calibrated value of %.1fmm" % bowden_length)

        # How much to reduce the fast move portion
        gate_homing_buffer = bowden_move * ((100 - u.p.bowden_fast_unload_portion) / 100)

        # Convenience flags
        full = (bowden_move == bowden_length)
        macros_and_track = not extruder_only and full
        runout = self.is_handling_runout

        self._set_filament_direction(DIRECTION_UNLOAD)
        self._initialize_filament_position(dwell=None)

        if check_state or self.filament_pos == FILAMENT_POS_UNKNOWN:
            # Let's determine where filament is and reset state before continuing
            self.recover_filament_pos(message=True)

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
                self._set_filament_position(-park_pos)
                if park_pos == 0.:
                    self.log_error("Tip forming performed by slicer but 'slicer_tip_park_pos' not set")
                else:
                    self.log_debug("Tip forming performed by slicer, park_pos set to %.1fmm" % park_pos)

            elif do_form_tip == FORM_TIP_STANDALONE and (self.filament_pos >= FILAMENT_POS_IN_EXTRUDER or runout):
                with self._wrap_track_time('form_tip'):
                    # Extruder only in runout case to give filament best chance to reach gear
                    detected = self.form_tip_standalone(extruder_only=(extruder_only or runout))
                    park_pos = self._get_filament_position()

                    # If handling runout warn if we don't see any filament near the gate
                    if runout and (
                        self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected) is False or
                        (self.has_encoder() and self.get_encoder_distance() == 0)
                    ):
                        self.log_warning("Warning: Filament not seen near gate after tip forming move. Unload may not be possible")

                    self.wrap_gcode_command(self.p.post_form_tip_macro, exception=True, wait=True)

            # Note: Conditionals deliberately coded this way to match macro alternative
            #PAUL homing_total = None    # Track how much homing is done for calibrated bowden length optimization
            expected_homing = 0.   # Amount of homing that would be expected (because bowden load is shortened)
            bowden_move_ratio = 0. # Track mismatch in moved vs measured bowden distance
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
                else:
                    raise MmuError("Cannot unload extruder because filament not detected in extruder! (state: %s)" % start_filament_pos)

            elif start_filament_pos == FILAMENT_POS_UNLOADED:
                raise MmuError("Cannot unload because already unloaded!")

            else:
                if start_filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
                    # Exit extruder, fast unload of bowden, then slow unload to gate
                    synced_extruder_unload = self._unload_extruder(validate=do_form_tip == FORM_TIP_STANDALONE)

                if (
                    (start_filament_pos >= FILAMENT_POS_END_BOWDEN and calibrated) or
                    (start_filament_pos >= FILAMENT_POS_HOMED_GATE and not full)
                ):
                    # Fast unload of bowden, then unload gate
                    bowden_move_ratio = self._unload_bowden(bowden_move - gate_homing_buffer)
                    actual_homing, expected_homing = self._unload_gate(gate_homing_buffer)
                    # PAUL homing_total += actual_homing

                elif start_filament_pos >= FILAMENT_POS_HOMED_GATE:
                    # We have to do slow unload because we don't know exactly where we are
                    _,_ = self._unload_gate(bowden_move)

            # Set future "from buffer" flag (also used for faster loading speed)
            if unload_to_buffer and self.gate_status[self.gate_selected] != GATE_EMPTY:
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE_FROM_BUFFER)

            # If runout then over unload to prevent accidental reload
            if runout:
                self._eject_from_gate()

             # Encoder based validation test
             # Currently disabled because it results in servo "flutter" that users don't like
             #if self._can_use_encoder():
             #    movement = self.selector().filament_release(measure=True)
             #    if movement > self.encoder().movement_min():
             #        self._set_filament_pos_state(self.FILAMENT_POS_UNKNOWN)
             #        self.log_trace("Encoder moved %.1fmm when filament was released!" % movement)
             #        raise MmuError("Encoder sensed movement when the servo was released\nConcluding filament is stuck somewhere")

            self.movequeues_wait()
            msg = "Unload of %.1fmm filament successful" % self._get_filament_position()
            if self._can_use_encoder():
                final_encoder_pos = self.get_encoder_distance(dwell=None)
                not_seen = u.p.gate_parking_distance + self._get_encoder_dead_space() + (u.p.toolhead_unload_safety_margin if not synced_extruder_unload else 0.)
                msg += " {1}(adjusted encoder: %.1fmm){0}" % -(final_encoder_pos + not_seen)
            self.log_info(msg, color=True)

            # Notify autotune manager
            if full and not extruder_only and not self.p.gcode_unload_sequence:
                # PAUL TODO u.calibrator.note_unload_telemetry(bowden_move_ratio, homing_total, expected_homing)
                pass

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


    # Form tip prior to extraction from the extruder. This can take the form of shaping the filament or could simply
    # activate a filament cutting mechanism. Sets filament position based on park pos
    # Returns True if filament is detected
    def form_tip_standalone(self, extruder_only=False):
        """
        Form or cut a filament tip using the configured standalone macro.

        Args:
            extruder_only: Whether only the extruder path should be moved without normal MMU synchronization.

        Returns:
            bool: True when filament is believed to remain detected after tip forming; otherwise False.
        """
        u = self.mmu_unit()

        self.movequeues_wait()

        # Pre check to validate the presence of filament in the extruder and case where we don't need to form tip
        filament_initially_present = self.sensor_manager.check_sensor(SENSOR_TOOLHEAD)
        if filament_initially_present is False:
            self.log_debug("Tip forming skipped because no filament was detected")

            if self.filament_pos == FILAMENT_POS_LOADED:
                self._set_filament_pos_state(FILAMENT_POS_EXTRUDER_ENTRY)
            else:
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

            self._set_filament_position(-self.p.toolhead_extruder_to_nozzle)
            return False

        gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.form_tip_macro, None)
        if gcode_macro is None:
            raise MmuError("Filament tip forming macro '%s' not found" % self.p.form_tip_macro)

        with self.wrap_action(ACTION_CUTTING_TIP if self.has_toolhead_cutter else ACTION_FORMING_TIP):
            sync = self.reset_sync_gear_to_extruder(not extruder_only and u.p.sync_form_tip)
            self._ensure_safe_extruder_temperature(wait=True)

            # Perform the tip forming move and establish park_pos
            initial_encoder_position = self.get_encoder_distance()
            park_pos, remaining, reported = self._do_form_tip()
            measured = self.get_encoder_distance(dwell=None) - initial_encoder_position
            self._set_filament_remaining(remaining, self.gate_color[self.gate_selected] if self.gate_selected != TOOL_GATE_UNKNOWN else '')

            # Encoder based validation test
            detected = True # Start with assumption that filament was present
            if self._can_use_encoder() and not reported:
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

            self._set_filament_position(-park_pos)
            self.set_encoder_distance(initial_encoder_position + park_pos)

            if detected or extruder_only:
                # Definitely in extruder
                self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER)
            else:
                # No detection. Best to assume we are somewhere in bowden for defensive unload
                self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN)

            return detected


    def _do_form_tip(self, test=False):
        """
        Run the configured tip-forming macro and derive park position information from it.

        Args:
            test: Whether the tip-forming macro should run in test or final-eject mode.

        Returns:
            tuple[float, float, bool]: Park position, remaining filament estimate, and whether the park position was explicitly reported by the macro.
        """
        with self.wrap_extruder_current(self.p.extruder_form_tip_current, "for tip forming move"):
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
                park_pos = stepper_movement + self.p.toolhead_residual_filament + self.toolchange_retract
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
                filament_remaining = park_pos - stepper_movement - self.p.toolhead_residual_filament - self.toolchange_retract
                msg = "After tip cutting, park_pos reported as: %.1fmm with calculated %.1fmm filament remaining in extruder (extruder moved: %.1fmm, encoder measured %.1fmm total movement)" % (park_pos, filament_remaining, stepper_movement, measured)
                if test:
                    self.log_always(msg)
                else:
                    self.log_trace(msg)

            if not test:
                # Important sanity checks to spot misconfiguration
                if park_pos > self.p.toolhead_extruder_to_nozzle:
                    self.log_warning("Warning: park_pos (%.1fmm) cannot be greater than 'toolhead_extruder_to_nozzle' distance of %.1fmm! Assuming fully unloaded from extruder\nWill attempt to continue..." % (park_pos, self.p.toolhead_extruder_to_nozzle))
                    park_pos = self.p.toolhead_extruder_to_nozzle
                    filament_remaining = 0.

                if filament_remaining < 0:
                    self.log_warning("Warning: Calculated filament remaining after cut is negative (%.1fmm)! Suspect misconfiguration of output_park_pos (%.1fmm).\nWill attempt to continue assuming no cut filament remaining..." % (filament_remaining, park_pos))
                    park_pos = 0.
                    filament_remaining = 0.

        return park_pos, filament_remaining, reported


    def purge_standalone(self):
        """
        Run the configured standalone purge macro, if one is available.

        Args:
            None.

        Returns:
            None. Runs the purge macro when configured and available.
        """
        u = self.mmu_unit()

        if self.p.purge_macro:
            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.p.purge_macro, None)
            if gcode_macro:
                self.log_info("Purging...")
                with self._wrap_extruder_current(self.p.extruder_purge_current, "for filament purge"):
                    # The macro to decide on the purge volume, but expect to be based on this.
                    msg = "Suggested purge volume of %.1fmm%s calculated from:\n" % (self.toolchange_purge_volume, UI_CUBE)
                    msg += "- toolhead_residual_filament: %.1fmm\n" % self.p.toolhead_residual_filament
                    msg += "- filament_remaining (previous cut fragment): %.1fmm\n" % self.filament_remaining
                    msg += "- slicer purge volume for toolchange %s > %s" % (self.selected_tool_string(self._last_tool), self.selected_tool_string(self._next_tool))
                    self.log_debug(msg)
                    self.wrap_gcode_command(self.p.purge_macro, exception=True, wait=True)
            else:
                self.log_warning("Purge macro %s not found" % self.p.purge_macro)


# -----------------------------------------------------------------------------------------------------------
# FILAMENT MOVEMENT AND CONTROL
# -----------------------------------------------------------------------------------------------------------

    # Convenience wrapper around all gear and extruder motor movement that retains sync state, tracks movement and creates trace log
    # motor = "gear"           - gear motor(s) only on rail
    #         "gear+extruder"  - gear and extruder included on rail
    #         "extruder"       - extruder only on gear rail
    #         "synced"         - gear synced with extruder as in print (homing move not possible)
    #
    # If homing move then endstop name can be specified.
    #         "mmu_shared_exit"       - at the gate on MMU (when motor includes "gear")
    #         "mmu_exit_N"     - post past the filament drive gear
    #         "extruder"       - just before extruder entrance (motor includes "gear" or "extruder")
    #         "toolhead"       - after extruder entrance (motor includes "gear" or "extruder")
    #         "mmu_gear_touch" - stallguard on gear (when motor includes "gear", only useful for motor="gear")
    #         "mmu_ext_touch"  - stallguard on nozzle (when motor includes "extruder", only useful for motor="extruder")
    #
    # All move distances are interpreted as relative
    # 'wait' will wait on appropriate move queue(s) after completion of move (forced to True if need encoder reading)
    # 'measure' whether we need to wait and measure encoder for movement
    # 'encoder_dwell' delay some additional time to ensure we have accurate encoder reading (if encoder fitted and required for measuring)
    #
    # All moves return: actual (relative), homed, measured, delta; mmu_toolhead().get_position[1] holds absolute position
    #
    def trace_filament_move(self, trace_str, dist, speed=None, accel=None, motor="gear", homing_move=0, endstop_name="default", track=False, wait=False, encoder_dwell=False, speed_override=True):
        """
        Execute a traced filament move and report actual motion, homing result, and encoder data.

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

        Returns:
            tuple[float, bool, float, float]: Actual movement, homing success flag, encoder-measured movement, and the difference between commanded and measured travel.
        """
        u = self.mmu_unit()

        encoder_start = self.get_encoder_distance(dwell=encoder_dwell)
        pos = self.mmu_toolhead().get_position()
        ext_pos = self.toolhead.get_position()
        homed = False
        actual = dist
        delta = 0.
        null_rtn = (0., False, 0., 0.)

        if homing_move != 0:
            # Check for valid endstop
            if endstop_name is None:
                endstops = self.gear_rail().get_endstops()
            else:
                endstop_name = self.sensor_manager.get_mapped_endstop_name(endstop_name)
                endstops = self.gear_rail().get_extra_endstop(endstop_name)
                if endstops is None:
                    self.log_error("Endstop '%s' not found" % endstop_name)
                    return null_rtn

        # Set sensible speeds and accelaration if not supplied
        if motor in ["gear"]:
            if homing_move != 0:
                speed = speed or u.p.gear_homing_speed
                accel = accel or min(u.p.gear_from_filament_buffer_accel, u.p.gear_from_spool_accel)
            else:
                if abs(dist) > u.p.gear_short_move_threshold:
                    if dist < 0:
                        speed = speed or u.p.gear_unload_speed
                        accel = accel or u.p.gear_unload_accel
                    elif (not u.p.has_filament_buffer or (self.gate_selected >= 0 and self.gate_status[self.gate_selected] != GATE_AVAILABLE_FROM_BUFFER)):
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
            self.log_assertion("Invalid motor specification '%s'" % motor)
            return null_rtn

        # Apply per-gate speed override
        if self.gate_selected >= 0 and speed_override:
            adjust = self.gate_speed_override[self.gate_selected] / 100.
            speed *= adjust
            accel *= adjust

        def _set_sync_mode(sync_mode):
            self.mmu_toolhead().sync(sync_mode)
            if sync_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER:
                self._adjust_gear_current(percent=u.p.sync_gear_current, reason="for extruder synced move")
            else:
                self._restore_gear_current() # 100%

        with self._wrap_espooler(motor, dist, speed, accel, homing_move):
            wait = wait or self._wait_for_espooler # Allow eSpooler wrapper to force wait

            # Gear rail is driving the filament
            start_pos = self.mmu_toolhead().get_position()[1]
            if motor in ["gear", "gear+extruder", "extruder"]:
                _set_sync_mode(DRIVE_EXTRUDER_SYNCED_TO_GEAR if motor == "gear+extruder" else DRIVE_EXTRUDER_ONLY_ON_GEAR if motor == "extruder" else DRIVE_GEAR_ONLY)
                if homing_move != 0:
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstops, self.mmu_toolhead())
                    init_ext_mcu_pos = self.mmu_toolhead().extruder_stepper_obj().stepper.get_mcu_position() # For non-homing extruder or if extruder not on gear rail
                    init_pos = pos[1]
                    pos[1] += dist
                    for _ in range(self.p.canbus_comms_retries):  # HACK: We can repeat because homing move
                        got_comms_timeout = False # HACK: Logic to try to mask CANbus timeout issues
                        try:
                            #initial_mcu_pos = self.mmu_toolhead().extruder_stepper_obj().stepper.get_mcu_position()
                            #init_pos = pos[1]
                            #pos[1] += dist
                            with self.wrap_accel(accel):
                                trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                            homed = True
                            if self.gear_rail().is_endstop_virtual(endstop_name):
                                # Stallguard doesn't do well at slow speed. Try to infer move completion
                                if abs(trig_pos[1] - dist) < 1.0:
                                    homed = False
                        except self.printer.command_error as e:
                            # CANbus mcu's often seen to exhibit "Communication timeout" so surface errors to user
                            if abs(trig_pos[1] - dist) > 0. and "after full movement" not in str(e):
                                if 'communication timeout' in str(e).lower():
                                    got_comms_timeout = True
                                    speed *= 0.8 # Reduce speed by 20%
                                self.log_error("Did not complete homing move: %s" % str(e))
                            else:
                                if self.log_enabled(LOG_STEPPER):
                                    self.log_stepper("Did not home: %s" % str(e))
                            homed = False
                        finally:
                            halt_pos = self.mmu_toolhead().get_position()
                            ext_actual = (self.mmu_toolhead().extruder_stepper_obj().stepper.get_mcu_position() - init_ext_mcu_pos) * self.mmu_toolhead().extruder_stepper_obj().stepper.get_step_dist()

                            # Support setup where a non-homing extruder is being used
                            if motor == "extruder" and not u.homing_extruder:
                                # This isn't super accurate if extruder isn't (homing) MmuExtruder because doesn't have required endstop, thus this will
                                # overrun and even move slightly even if already homed. We can only correct the actual gear rail position.
                                halt_pos[1] += ext_actual
                                self.mmu_toolhead().set_position(halt_pos) # Correct the gear rail position

                            actual = halt_pos[1] - init_pos
                            if self.log_enabled(LOG_STEPPER):
                                self.log_stepper("%s HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s >> %s" % (
                                        motor.upper(), dist, speed, accel, endstop_name, wait,
                                        (
                                            "%s halt_pos=%.1f (rail moved=%.1f, extruder moved=%.1f), "
                                            "start_pos=%.1f, trig_pos=%.1f"
                                            % (
                                                "HOMED" if homed else "DID NOT HOMED",
                                                halt_pos[1], actual, ext_actual, start_pos, trig_pos[1],
                                            )
                                        ),
                                    )
                                )

                        if not got_comms_timeout:
                            break
                else:
                    if self.log_enabled(LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
                    pos[1] += dist
                    with self.wrap_accel(accel):
                        self.mmu_toolhead().move(pos, speed)

            # Extruder is driving, gear rail is following
            elif motor in ["synced"]:
                _set_sync_mode(DRIVE_GEAR_SYNCED_TO_EXTRUDER)
                if homing_move != 0:
                    self.log_error("Not possible to perform homing move while synced")
                else:
                    if self.log_enabled(LOG_STEPPER):
                        self.log_stepper("%s MOVE: dist=%.1f, speed=%.1f, accel=%.1f, wait=%s" % (motor.upper(), dist, speed, accel, wait))
                    ext_pos[3] += dist
                    self.toolhead.move(ext_pos, speed)

            self.mmu_toolhead().flush_step_generation() # TTC mitigation (TODO still required?)
            self.toolhead.flush_step_generation()     # TTC mitigation (TODO still required?)
            if wait:
                self.movequeues_wait()

        encoder_end = self.get_encoder_distance(dwell=encoder_dwell)
        measured = encoder_end - encoder_start
        delta = abs(actual) - measured # +ve means measured less than moved, -ve means measured more than moved
        if trace_str:
            if homing_move != 0:
                trace_str += ". Stepper: '%s' %s after moving %.1fmm (of max %.1fmm), encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, ("homed" if homed else "did not home"), actual, dist, measured, delta)
                trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead().get_position()[1], encoder_end)
            else:
                trace_str += ". Stepper: '%s' moved %.1fmm, encoder measured %.1fmm (delta %.1fmm)"
                trace_str = trace_str % (motor, dist, measured, delta)
            trace_str += ". Pos: @%.1f, (%.1fmm)" % (self.mmu_toolhead().get_position()[1], encoder_end)
            self.log_trace(trace_str)

        if self._can_use_encoder() and motor == "gear" and track:
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
                    # Average down over 10 swaps
                    self.gate_statistics[self.gate_selected]['quality'] = (cur_quality * 9 + quality) / 10

        return actual, homed, measured, delta


    # Used to force accelaration override for homing moves
    @contextlib.contextmanager
    def wrap_accel(self, accel):
        """
        Temporarily override the acceleration limit used by the MMU toolhead.

        Args:
            accel: Requested acceleration. When omitted, a context-appropriate default is chosen.

        Yields:
            self while the temporary acceleration limit is active.
        """
        self.mmu_toolhead().get_kinematics().set_accel_limit(accel)
        try:
            yield self
        finally:
            self.mmu_toolhead().get_kinematics().set_accel_limit(None)

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
                self.espooler.set_operation(self.gate_selected, pwm_value, espooler_operation)
        try:
            # Note gate_selected doesn't change in this use case, it's just filament movement
            yield self

        finally:
            self._wait_for_espooler = False
            if espooler_operation != ESPOOLER_OFF:
                self.espooler.set_operation(self.gate_selected, 0, ESPOOLER_OFF)


# -----------------------------------------------------------------------------------------------------------
# GENERAL FILAMENT RECOVERY AND MOVE HELPERS
# -----------------------------------------------------------------------------------------------------------

    # Report on need to recover and necessary calibration
    def report_necessary_recovery(self, use_autotune=True):
        """
        Report whether recovery or calibration is required before continuing.

        Args:
            use_autotune: Whether autotune-aware calibration status should be considered.

        Returns:
            None. Logs recovery guidance based on current calibration and filament state.
        """
        if not MmuCalibrator.check_if_not_calibrated(self, CALIBRATED_ALL, silent=None, use_autotune=use_autotune):
            if self.filament_pos != FILAMENT_POS_UNLOADED and TOOL_GATE_UNKNOWN in [self.gate_selected, self.tool_selected]:
                self.log_error("Filament detected but tool/gate is unknown. Please use MMU_RECOVER GATE=xx to correct state")
            elif self.filament_pos not in [FILAMENT_POS_LOADED, FILAMENT_POS_UNLOADED]:
                self.log_error("Filament not detected as either unloaded or fully loaded. Please check and use MMU_RECOVER to correct state or fix before continuing")


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
        gs = self.sensor_manager.check_sensor(self.sensor_manager.get_mapped_endstop_name(u.p.gate_homing_endstop))

        filament_detected = self.sensor_manager.check_any_sensors_in_path()
        looks_loaded = self.sensor_manager.check_all_sensors_in_path()
        if not filament_detected:
            filament_detected = self.check_filament_in_mmu() # Include encoder detection method

        # Definitely loaded
        if ts:
            self._set_filament_pos_state(FILAMENT_POS_LOADED, silent=silent)

        # Probably loaded: Unless strict we will continue to assume loaded in the absence of sensors to say otherwise
        elif not strict and self.filament_pos == FILAMENT_POS_LOADED and looks_loaded:
            pass

        # Somewhere in extruder
        elif filament_detected and can_heat and self.check_filament_in_extruder(): # Encoder based
            self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming on unload
        elif ts is False and filament_detected and (self.p.strict_filament_recovery or strict) and can_heat and self.check_filament_in_extruder():
            # This case adds an additional encoder based test to see if filament is still being gripped by extruder
            # even though TS doesn't see it. It's a pedantic option so on turned on by strict flag
            self._set_filament_pos_state(FILAMENT_POS_IN_EXTRUDER, silent=silent) # Will start from tip forming

        # At extruder entry
        elif es:
            self._set_filament_pos_state(FILAMENT_POS_HOMED_ENTRY, silent=silent) # Allows for fast bowden unload move

        # Parked at gate (when parking distance is not a retract i.e. gs sensor expected to be triggered)
        elif gs and filament_detected and u.p.gate_parking_distance <= 0:
            self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # Somewhere in bowden
        elif gs or filament_detected:
            self._set_filament_pos_state(FILAMENT_POS_IN_BOWDEN, silent=silent) # Prevents fast bowden unload move

            # Sensor sanity check
            if self.sensor_manager.check_all_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False) is False:
                sensors = self.sensor_manager.get_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected, loading=False)
                malfunction = ", ".join(sorted(k for k, v in sensors.items() if v is False))
                self.log_warning("Filament determined to be somewhere in bowden but the following sensors are unexpectedly not triggered: %s\nCheck for further sensor malfunction with MMU_SENSORS command. Also validate the correct gate is selected.\nRe-run MMU_RECOVER when ready" % malfunction)

        # Unloaded
        else:
            self._set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=silent)

        # If filament is detected then ensure gate status is correct
        if self.gate_selected != TOOL_GATE_UNKNOWN and filament_detected:
            gate_status = self.gate_status[self.gate_selected]
            if self.filament_pos >= FILAMENT_POS_START_BOWDEN and gate_status < GATE_AVAILABLE:
                self._set_gate_status(self.gate_selected, GATE_AVAILABLE)
            elif gate_status == GATE_EMPTY:
                self._set_gate_status(self.gate_selected, GATE_UNKNOWN)


    def check_filament_in_mmu(self):
        """
        Check whether filament is present anywhere in the MMU path.

        Args:
            None.

        Returns:
            bool or None: True when filament is detected in the MMU path, False when not detected, or None when no test is possible.
        """
        self.log_debug("Checking for filament in MMU...")
        detected = self.sensor_manager.check_any_sensors_in_path()
        if not detected and self.has_encoder():
            self.selector().filament_drive()
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected


    def check_filament_in_gate(self):
        """
        Check whether filament is present at the currently selected gate.

        Args:
            None.

        Returns:
            bool or None: True when filament is detected at the selected gate, False when not detected, or None when no test is possible.
        """
        self.log_debug("Checking for filament at gate...")
        detected = self.sensor_manager.check_any_sensors_before(FILAMENT_POS_HOMED_GATE, self.gate_selected)
        if not detected and self.has_encoder():
            self.selector().filament_drive()
            detected = self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
        if detected is None:
            self.log_debug("No sensors configured!")
        return detected


    def check_filament_runout(self):
        """
        Check whether filament runout has been detected.

        Args:
            None.

        Returns:
            bool or None: True when runout is detected, False when filament appears present, or None when no test is possible.
        """
        self.log_debug("Checking for runout...")
        runout = self.sensor_manager.check_for_runout()
        if runout is None and self.has_encoder():
            self.selector().filament_drive()
            detected = not self.buzz_gear_motor()
            self.log_debug("Filament %s in encoder after buzzing gear motor" % ("detected" if detected else "not detected"))
            runout = not detected
        if runout is None:
            self.log_debug("No sensors configured!")
        return runout


    def check_filament_in_extruder(self):
        # First double check extruder entry sensor if fitted
        """
        Check whether filament is still present in the extruder path.

        Args:
            None.

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

        Args:
            None.

        Returns:
            tuple[bool or None, float]: Detection result and encoder-measured movement from the probe retract.
        """
        u = self.mmu_unit()

        detected = None
        measured = 0
        if self.has_encoder() and not u.filament_always_gripped:
            with self._require_encoder(): # Force quality measurement
                self.log_info("Checking for possibility of filament still in extruder gears...")
                self._ensure_safe_extruder_temperature(wait=False)
                self.selector().filament_release()
                move = u.p.encoder_move_step_size
                _, _, measured, _ = self.trace_filament_move("Checking extruder", -move, speed=self.p.extruder_unload_speed, motor="extruder")
                detected = measured > self.encoder().movement_min()
                self.log_debug("Filament %s in extruder" % ("detected" if detected else "not detected"))
        return detected, measured


    def buzz_gear_motor(self):
        """
        Buzz the gear motor briefly to infer filament presence from encoder movement.

        Args:
            None.

        Returns:
            bool or None: Encoder-based filament detection result, or None when the test is unavailable.
        """
        u = self.mmu_unit()

        if self.has_encoder():
            with self._require_encoder(): # Force quality measurement
                initial_encoder_position = self.get_encoder_distance()
                self.trace_filament_move(None, 2.5 * self.encoder().get_resolution(), accel=u.p.gear_buzz_accel, encoder_dwell=None)
                self.trace_filament_move(None, -2.5 * self.encoder().get_resolution(), accel=u.p.gear_buzz_accel, encoder_dwell=None)
                measured = self.get_encoder_distance() - initial_encoder_position
                self.log_trace("After buzzing gear motor, encoder measured %.2f" % measured)
                self.set_encoder_distance(initial_encoder_position, dwell=None)
                return measured > self.encoder().movement_min()
        else:
            self.trace_filament_move(None, 5, accel=u.p.gear_buzz_accel)
            self.trace_filament_move(None, -5, accel=u.p.gear_buzz_accel)
        return None


# -----------------------------------------------------------------------------------------------------------
# MMU/Extruder Synchronization
# -----------------------------------------------------------------------------------------------------------

    def reset_sync_gear_to_extruder(self, sync_intention, force_grip=False, force_in_print=False, skip_extruder_check=False):
        """
        Reconcile the desired gear-to-extruder sync state with current context and safety rules.

        Args:
            sync_intention: Requested sync intent from the caller for the current operation.
            force_grip: Whether filament grip or release should be forced even when suppression is active.
            force_in_print: Whether print-context checks should behave as though a print is active.
            skip_extruder_check: Whether to bypass the normal check that filament is past the extruder entry point.

        Returns:
            bool: Final sync state that was applied.
        """
        u = self.mmu_unit()

        bypass_selected = (self.gate_selected == TOOL_GATE_BYPASS)
        in_print_context = self.is_in_print(force_in_print)
        actively_printing = self.is_printing(force_in_print)

        filament_past_entry = self.filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY
        extruder_check_ok = filament_past_entry or skip_extruder_check

        always_gripped = u.filament_always_gripped
        standalone_sync_requested = self._standalone_sync

        # In a non-print context we also honor the caller's explicit intention.
        wants_sync_out_of_print = always_gripped or standalone_sync_requested or sync_intention

        if bypass_selected:
            sync = False

        elif in_print_context:
            if actively_printing:
                # During active printing, respect the print-time sync setting.
                sync = bool(u.p.sync_to_extruder)
            else:
                # In print context but not actively printing (e.g., paused/warming),
                # sync only if filament is present (or overridden) and sync is needed/requested
                sync = extruder_check_ok and (always_gripped or standalone_sync_requested)

        else:
            # Not in a print: sync if filament is present (or overridden) and any
            # condition requires/requests syncing.
            sync = extruder_check_ok and wants_sync_out_of_print

        self.sync_gear_to_extruder(sync, force_grip=force_grip)
        return sync


    def sync_gear_to_extruder(self, sync, gate=None, force_grip=False):
        """
        Apply or remove direct synchronization between the gear motor and the extruder.

        Args:
            sync: Whether to sync the gear motor to the extruder.
            gate: Gate index to operate on. When omitted, the current selection is used.
            force_grip: Whether filament grip or release should be forced even when suppression is active.

        Returns:
            None. Applies sync state, grip state, and current changes in place.
        """
        u = self.mmu_unit()

        # Default to current selection; some designs call this before gate selection is finalized.
        if gate is None:
            gate = self.gate_selected

        bypass_or_unknown_gate = gate < 0
        selector_ready = self.selector().is_homed

        # Protect cases where we should not sync (type-B always has a homed selector).
        if bypass_or_unknown_gate or not selector_ready:
            sync = False
            self._standalone_sync = False

        # Filament grip handling (do this before syncing to avoid "buzz" movement on type-A MMUs).
        if sync:
            self.selector().filament_drive()
        else:
            # There are situations where we want to be lazy to avoid servo "flutter":
            #   - `_suppress_release_grip` is True unless we are the outermost caller.
            #   - `force_grip` can override that suppression.
            should_release = force_grip or not self._suppress_release_grip
            if should_release:
                self.selector().filament_release()

        # Sync / unsync toolhead mode (avoid redundant calls).
        desired_sync_mode = DRIVE_GEAR_SYNCED_TO_EXTRUDER if sync else None
        if desired_sync_mode != self.mmu_toolhead().sync_mode:
            self.movequeues_wait()  # Safety: likely unnecessary but ensures no queued moves conflict.
            self.mmu_toolhead().sync(desired_sync_mode)

        # Current control:
        # - While synced, optionally reduce current for the active gear stepper.
        # - On multigear systems, restore current on the previously-used gear stepper if gate differs.
        # - When unsynced, restore current to 100%.
        if sync:
            if u.multigear and gate != self.gate_selected:
                self._restore_gear_current()  # Restore previous gear stepper to 100%

            self._adjust_gear_current(
                gate=gate,
                percent=u.p.sync_gear_current,
                reason="for extruder syncing",
            )
        else:
            self._restore_gear_current()  # 100%


    @contextlib.contextmanager
    def wrap_sync_gear_to_extruder(self):
        """
        Preserve sync and grip state across a block that temporarily changes them.

        Args:
            None.

        Yields:
            self while nested operations may temporarily alter sync and grip state.
        """
        # Capture current sync state so it can be restored on exit.
        previous_sync = (
            self.mmu_toolhead().sync_mode == DRIVE_GEAR_SYNCED_TO_EXTRUDER
        )

        # Suppress grip release only at the outermost level.
        outermost_wrapper = not self._suppress_release_grip
        self._suppress_release_grip = True

        try:
            yield self
        finally:
            # Only the outermost wrapper clears suppression.
            if outermost_wrapper:
                self._suppress_release_grip = False

            # Restore prior sync state. Logic inside reset_sync_gear_to_extruder
            # may consult the global suppression flag when reconciling grip.
            self.reset_sync_gear_to_extruder(previous_sync)


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
        current_percent = self.gear_run_current_percent

        if self._gear_run_current_locked:
            return current_percent
        if gate is None:
            gate = self.gate_selected
        if gate < 0:
            return current_percent
        if not (0 < percent < 200):
            return current_percent
        if self.mmu_unit(gate).gear_tmc_obj(gate) is None:
            return current_percent
        if percent == self.gear_run_current_percent:
            return current_percent

        sname = self.mmu_unit(gate).gear_name(gate)
        if restore:
            msg = "Restoring MMU %s run current to %d%% ({}A)" % (sname, percent)
        else:
            msg = "Modifying MMU %s run current to %d%% ({}A) %s" % (sname, percent, reason)
        target_current = (self.mmu_unit(gate).gear_default_current(gate) * percent) / 100.0
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
        target_current = (u.extruder_default_current * percent) / 100.0
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
