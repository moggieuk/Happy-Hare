# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle all aspects of MMU calibration and autotuning. In
#       paricular manage persistence of bowden lengths and gear rotation distances.
#
# Implements commands:
#   MMU_SET_LED
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, math

# Happy Hare imports

# MMU subcomponent clases
from .mmu_shared import *


class MmuCalibrationManager:

    def __init__(self, mmu):
        self.mmu = mmu


    # -------------------- Bowden length manipulation --------------------
    # Notes:
    #  - The bowden length is the distance between the current choice of endstops.
    #    If those endstops change the bowden length must be adjusted
    #  - A calibrated bowden length must also be updated if the rotation_distance for
    #    that gate is updated
    #  - Testing has shown that the encoder based clog detection length is generally
    #    proportional to the bowden length

    # Returns the currently calibrated bowden length or the default for gate 0 if not
    def get_bowden_length(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected

        ref_gate = gate if gate >= 0 and self.mmu.mmu_machine.variable_bowden_lengths else 0
        return self.mmu.bowden_lengths[ref_gate]


    # Update bowden calibration for current gate and clog_detection if not yet calibrated
    def update_bowden_length(self, length, gate=None, console_msg=False):
        if gate == None: gate = self.mmu.gate_selected
        if gate < 0:
            self.mmu.log_debug("Assertion failure: cannot save bowden length for gate: %s" % self.mmu.selected_gate_string(gate))
            return

        all_gates = not self.mmu.mmu_machine.variable_bowden_lengths

        if length < 0: # Reset
            action = "reset"
            if all_gates:
                self.mmu.bowden_lengths = [-1] * self.mmu.num_gates
            else:
                self.mmu.bowden_lengths[gate] = -1

        else:
            length = round(length, 1)
            action = "saved"
            if all_gates:
                self.mmu.bowden_lengths = [length] * self.mmu.num_gates
            else:
                self.mmu.bowden_lengths[gate] = length

        msg = "Calibrated bowden length %.1fmm has been %s %s" % (length, action, ("for all gates" if all_gates else "gate %d" % gate))
        if console_msg:
            self.mmu.log_always(msg)
        else:
            self.mmu.log_debug(msg)

        # Update calibration status
        if not any(x == -1 for x in self.mmu.bowden_lengths):
            self.mmu.calibration_status |= self.mmu.CALIBRATED_BOWDENS

        # Persist
        self.mmu.save_variable(self.mmu.VARS_MMU_CALIB_BOWDEN_LENGTHS, self.mmu.bowden_lengths)
        self.mmu.write_variables()


    # Adjust all bowden lengths if endstops are changed (e.g. from MMU_TEST_CONFIG)
    def adjust_bowden_lengths_on_homing_change(self):
        current_home = self.mmu.save_variables.allVariables.get(self.mmu.VARS_MMU_CALIB_BOWDEN_HOME, None)
        if self.mmu.gate_homing_endstop == current_home:
            return

        adjustment = 0
        if current_home == self.mmu.SENSOR_ENCODER:
            adjustment = self.mmu.gate_endstop_to_encoder
        elif self.mmu.gate_homing_endstop == self.mmu.SENSOR_ENCODER:
            adjustment = -self.mmu.gate_endstop_to_encoder
        self.mmu.bowden_lengths = [length + adjustment if length != -1 else length for length in self.mmu.bowden_lengths]
        self.mmu.log_debug("Adjusted bowden lengths by %.1f: %s because of gate_homing_endstop change" % (adjustment, self.mmu.bowden_lengths))

        # Persist
        self.mmu.save_variable(self.mmu.VARS_MMU_CALIB_BOWDEN_LENGTHS, self.mmu.bowden_lengths)
        self.mmu.save_variable(self.mmu.VARS_MMU_CALIB_BOWDEN_HOME, self.mmu.gate_homing_endstop)
        self.mmu.write_variables()


    # -------------------- Encoder based runout/clog/tangle length manipulation --------------------

    def calc_clog_detection_length(self, bowden_length):
        cal_min = round((bowden_length * 2) / 100., 1) # 2% of bowden length seems to be good starting point
        return max(cal_min, 8.)                        # Never less than 8mm


    def update_clog_detection_length(self, cdl, force=False):
        """
        Persist the calibrated encoder clog detection length and notify the encoder of change if in auto mode
        If not forced then save if auto but don't update the encoder
        """
        if not self.mmu.has_encoder(): return
        if not cdl: return

        auto = (self.mmu.sync_feedback_manager.flowguard_encoder_mode == self.mmu.encoder_sensor.RUNOUT_AUTOMATIC)

        if auto or force:
            self.mmu.save_variable(self.mmu.VARS_MMU_CALIB_CLOG_LENGTH, cdl, write=bool(force))

        if auto and not force:
            self.mmu.encoder_sensor.set_clog_detection_length(cdl)


    # -------------------- Gear stepper rotation distance manipulation --------------------
    # Notes:
    #  - If the rotation distance is changed for gate with calibrated bowden length then adjust bowden length

    # Return current calibrated gear rotation_distance or sensible default
    def get_gear_rd(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected

        if gate < 0:
            rd = self.mmu.default_rotation_distance
        else:
            rd = self.mmu.rotation_distances[gate if gate >= 0 and self.mmu.mmu_machine.variable_rotation_distances else 0]

        if rd <= 0:
            rd = self.mmu.default_rotation_distance
            self.mmu.log_debug("Gate %d not calibrated, falling back to default rotation_distance: %.4f" % (gate, rd))

        return rd


    # Save rotation_distance for gate (and associated gates) adjusting any calibrated bowden length
    def update_gear_rd(self, rd, gate=None, console_msg=False):
        if gate == None: gate = self.mmu.gate_selected
        if gate < 0:
            self.mmu.log_debug("Assertion failure: cannot save gear rotation_distance for gate: %d" % gate)
            return

        # Initial calibration on gate 0 also sets all gates as auto calibration starting point
        all_gates = (
            not self.mmu.mmu_machine.variable_rotation_distances
            or (gate == 0 and self.mmu.rotation_distances[0] == 0.)
        )

        if rd < 0:
            if all_gates:
                self.mmu.rotation_distances = [-1] * self.mmu.num_gates
            else:
                self.mmu.rotation_distances[gate] = -1

            self.mmu.log_always("Gear rotation distance calibration has been reset for %s" % ("all gates" if all_gates else "gate %d" % gate))

        else:
            prev_rd = self.get_gear_rd(gate)
            rd = round(rd, 4)

            if all_gates:
                self.mmu.rotation_distances = [rd] * self.mmu.num_gates
                updated_gates = list(range(self.mmu.num_gates))
            else:
                self.mmu.rotation_distances[gate] = rd
                updated_gates = [gate]

            # Adjust calibrated bowden lengths
            for g in updated_gates if self.mmu.mmu_machine.variable_bowden_lengths else [gate]:
                prev_bowden = self.mmu.bowden_lengths[g] # Must get raw value
                if prev_bowden > 0: # Is calibrated
                    new_bl = prev_bowden * (prev_rd / rd) # Adjust for same effective calibrated distance
                    self.update_bowden_length(new_bl, g)

            msg = "Rotation distance calibration (%.4f) has been saved for %s" % (rd, ("all gates" if all_gates else "gate %d" % gate))
            if console_msg:
                self.mmu.log_always(msg)
            else:
                self.mmu.log_debug(msg)

        # Update calibration status
        if self.mmu.rotation_distances[0] != -1:
            self.mmu.calibration_status |= self.mmu.CALIBRATED_GEAR_0
        if not any(x == -1 for x in self.mmu.rotation_distances):
            self.mmu.calibration_status |= self.mmu.CALIBRATED_GEAR_RDS

        # Persist
        self.mmu.save_variable(self.mmu.VARS_MMU_GEAR_ROTATION_DISTANCES, self.mmu.rotation_distances, write=True)


    #
    # Calibration implementations...
    #

    # Bowden calibration - Method 1
    # This method of bowden calibration is done in reverse and is a fallback. The user inserts filament to the
    # actual extruder and we measure the distance necessary to home to the defined gate homing position
    def calibrate_bowden_length_manual(self, approx_bowden_length):
        try:
            self.mmu.log_always("Calibrating bowden length on gate %d (manual method) using %s as gate reference point" % (self.mmu.gate_selected, self.mmu._gate_homing_string()))
            self.mmu._set_filament_direction(self.mmu.DIRECTION_UNLOAD)
            self.mmu.selector.filament_drive()
            self.mmu.log_always("Finding %s endstop position..." % self.mmu.gate_homing_endstop)
            homed = False

            if self.mmu.gate_homing_endstop == self.mmu.SENSOR_ENCODER:
                with self.mmu._require_encoder():
                    success = self.mmu._reverse_home_to_encoder(approx_bowden_length)
                    if success:
                        actual,_,_ = success
                        homed = True

            else: # Gate sensor... SENSOR_GATE is shared, but SENSOR_GEAR_PREFIX is specific
                actual, homed, measured, _ = self.mmu.trace_filament_move(
                    "Reverse homing off gate sensor",
                    -approx_bowden_length,
                    motor="gear",
                    homing_move=-1,
                    endstop_name=self.mmu.gate_homing_endstop,
                )

            if not homed:
                raise MmuError("Did not home to gate sensor after moving %.1fmm" % approx_bowden_length)

            actual = abs(actual)
            self.mmu.log_always("Filament homed back to gate after %.1fmm movement" % actual)
            self.mmu._unload_gate()
            return actual

        except MmuError as ee:
            raise MmuError("Calibration of bowden length on gate %d failed. Aborting because:\n%s" % (self.mmu.gate_selected, str(ee)))


    # Bowden calibration - Method 2
    # Automatic one-shot homing calibration from gate to endstop
    #   bowden_length = actual_moved + toolhead_entry_to_extruder
    def calibrate_bowden_length_sensor(self, extruder_homing_max):
        try:
            self.mmu.log_always(
                "Calibrating bowden length for gate %d using %s as gate reference point and %s as extruder homing point" %
                (
                    self.mmu.gate_selected,
                    self.mmu._gate_homing_string(),
                    self.mmu.extruder_homing_endstop
                )
            )
            self.mmu._initialize_filament_position(dwell=True)
            overshoot = self.mmu._load_gate(allow_retry=False)

            if self.mmu.extruder_homing_endstop in [self.mmu.SENSOR_EXTRUDER_ENTRY, self.mmu.SENSOR_COMPRESSION]:
                if self.mmu.sensor_manager.check_sensor(self.mmu.extruder_homing_endstop):
                    raise MmuError("The %s sensor triggered before homing. Check filament and sensor operation" % self.mmu.extruder_homing_endstop)

            actual, extra = self.mmu._home_to_extruder(extruder_homing_max)
            measured = self.mmu.get_encoder_distance(dwell=True) + self.mmu._get_encoder_dead_space()
            calibrated_length = round(overshoot + actual + extra, 1)

            msg = "Filament homed to extruder after %.1fmm movement" % actual
            if self.mmu.has_encoder():
                msg += "\n(encoder measured %.1fmm)" % (measured - self.mmu.gate_parking_distance)
            self.mmu.log_always(msg)

            self.mmu._unload_bowden(calibrated_length) # Fast move
            self.mmu._unload_gate()
            return calibrated_length

        except MmuError as ee:
            raise MmuError("Calibration of bowden length on gate %d failed. Aborting because:\n%s" % (self.mmu.gate_selected, str(ee)))


    # Bowden calibration - Method 3
    # Automatic calibration from gate to extruder entry sensor or collision with extruder gear (requires encoder)
    # Allows for repeats to average restult which is essential with encoder collision detection
    def calibrate_bowden_length_collision(self, approximate_length, extruder_homing_max, repeats):
        orig_endstop = self.mmu.extruder_homing_endstop
        try:
            # Can't allow "none" endstop during calibration so temporarily change it
            self.mmu.extruder_homing_endstop = self.mmu.SENSOR_EXTRUDER_COLLISION

            self.mmu.log_always("Calibrating bowden length on gate %d using %s as gate reference point and encoder collision detection" % (self.mmu.gate_selected, self.mmu._gate_homing_string()))
            reference_sum = spring_max = 0.
            successes = 0

            for i in range(repeats):
                self.mmu._initialize_filament_position(dwell=True)
                overshoot = self.mmu._load_gate(allow_retry=False)
                self.mmu._load_bowden(approximate_length, start_pos=overshoot) # Get close to extruder homing point

                self.mmu.log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                _,_ = self.mmu._home_to_extruder(extruder_homing_max)
                actual = self.mmu._get_filament_position() - self.mmu.gate_parking_distance
                measured = self.mmu.get_encoder_distance(dwell=True) + self.mmu._get_encoder_dead_space()
                spring = self.mmu.selector.filament_release(measure=True) if self.mmu.has_encoder() else 0.
                reference = actual - spring

                # When homing using collision, we expect the filament to spring back.
                if spring != 0:
                    msg = "Pass #%d: Filament homed to extruder after %.1fmm movement" % (i+1, actual)
                    if self.mmu.has_encoder():
                        msg += "\n(encoder measured %.1fmm, filament sprung back %.1fmm)" % (measured - self.mmu.gate_parking_distance, spring)
                    self.mmu.log_always(msg)
                    reference_sum += reference
                    spring_max = max(spring, spring_max)
                    successes += 1
                else:
                    # No spring means we haven't reliably homed
                    self.mmu.log_always("Failed to detect a reliable home position on this attempt")

                self.mmu._initialize_filament_position(True)
                self.mmu._unload_bowden(reference)
                self.mmu._unload_gate()

            if successes == 0:
                raise MmuError("All %d attempts at homing failed. MMU needs some adjustments!" % repeats)

            return (reference_sum / successes)

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of bowden length on gate %d failed. Aborting because:\n%s" % (self.mmu.gate_selected, str(ee)))
        finally:
            self.mmu.extruder_homing_endstop = orig_endstop


    def calibrate_encoder(self, length, repeats, speed, min_speed, max_speed, accel, save=True):
        pos_values, neg_values = [], []
        self.mmu.log_always("%s over %.1fmm..." % ("Calibrating" if save else "Validating calibration", length))
        speed_incr = (max_speed - min_speed) / repeats
        test_speed = min_speed
        mean = 0

        try:
            for x in range(repeats):
                if speed_incr > 0.:
                    self.mmu.log_always("Test run #%d, Speed=%.1f mm/s" % (x, test_speed))

                # Move forward
                self.mmu._initialize_filament_position(dwell=True)
                self.mmu.trace_filament_move(None, length, speed=test_speed, accel=accel, wait=True)
                counts = self.mmu._get_encoder_counts(dwell=True)
                pos_values.append(counts)
                self.mmu.log_always("%s+ counts: %d" % (UI_SPACE*2, counts))

                # Move backward
                self.mmu._initialize_filament_position(dwell=True)
                self.mmu.trace_filament_move(None, -length, speed=test_speed, accel=accel, wait=True)
                counts = self.mmu._get_encoder_counts(dwell=True)
                neg_values.append(counts)
                self.mmu.log_always("%s- counts: %d" % (UI_SPACE*2, counts))

                if counts == 0: break
                test_speed += speed_incr

            mean_pos = self.mmu._sample_stats(pos_values)['mean']
            mean_neg = self.mmu._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2

            if mean == 0:
                self.mmu.log_always("No counts measured. Ensure a tool was selected with filament gripped before running calibration and that your encoder is working properly")
                return

            resolution = length / mean
            old_result = mean * self.mmu.encoder_sensor.get_resolution()

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self.mmu._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self.mmu._sample_stats(neg_values)
            self.mmu.log_always(msg)

            # Sanity check to ensure all teeth are reflecting / being counted. 20% tolerance
            if (abs(resolution - self.mmu.encoder_sensor.get_resolution()) / self.mmu.encoder_sensor.get_resolution()) > 0.2:
                self.mmu.log_warning("Warning: Encoder is not detecting the expected number of counts based on CAD parameters which may indicate an issue")

            msg = "Before calibration measured length: %.2fmm" % old_result
            msg += "\nCalculated resolution of the encoder: %.4f (currently: %.4f)" % (resolution, self.mmu.encoder_sensor.get_resolution())
            self.mmu.log_always(msg)

            if save:
                self.mmu.encoder_sensor.set_resolution(resolution)
                self.mmu.save_variable(self.mmu.VARS_MMU_ENCODER_RESOLUTION, round(resolution, 4), write=True)
                self.mmu.log_always("Encoder calibration has been saved")
                self.mmu.calibration_status |= self.mmu.CALIBRATED_ENCODER

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of encoder failed. Aborting, because:\n%s" % str(ee))

        finally:
            if mean == 0:
                self.mmu._set_filament_pos_state(self.mmu.FILAMENT_POS_UNKNOWN)


    # Automatically calibrate the rotation_distance for gate>0 using encoder measurements and gate 0 as reference
    # Gate 0 is always calibrated with MMU_CALILBRATE_GEAR
    def calibrate_gate(self, gate, length, repeats, save=True):
        try:
            pos_values, neg_values = [], []
            self.mmu.select_gate(gate)
            self.mmu._load_gate(allow_retry=False)
            self.mmu.log_always("%s gate %d over %.1fmm..." % ("Calibrating" if (gate > 0 and save) else "Validating calibration of", gate, length))

            if gate == 0:
                self.mmu.log_always("Gate 0 is calibrated with MMU_CALIBRATE_GEAR and manual measurement, so this will run as a validation that encoder is calibrated correctly")

            for _ in range(repeats):
                self.mmu._initialize_filament_position(dwell=True)
                _,_,measured,delta = self.mmu.trace_filament_move("Calibration load movement", length, encoder_dwell=True)
                pos_values.append(measured)
                self.mmu.log_always("%s+ measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), self.mmu._get_encoder_counts(dwell=None)))
                self.mmu._initialize_filament_position(dwell=True)
                _,_,measured,delta = self.mmu.trace_filament_move("Calibration unload movement", -length, encoder_dwell=True)
                neg_values.append(measured)
                self.mmu.log_always("%s- measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), self.mmu._get_encoder_counts(dwell=None)))

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min).2f max=%(max).2f range=%(range).2f" % self.mmu._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min).2f max=%(max).2f range=%(range).2f" % self.mmu._sample_stats(neg_values)
            self.mmu.log_always(msg)

            mean_pos = self.mmu._sample_stats(pos_values)['mean']
            mean_neg = self.mmu._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2
            ratio = mean / length
            current_rd = self.mmu.gear_rail.steppers[0].get_rotation_distance()[0]
            new_rd = round(ratio * current_rd, 4)

            self.mmu.log_always("Calibration move of %d x %.1fmm, average encoder measurement: %.1fmm - Ratio is %.4f" % (repeats * 2, length, mean, ratio))
            self.mmu.log_always("Calculated gate %d rotation_distance: %.4f (currently: %.4f)" % (gate, new_rd, self.mmu.rotation_distances[gate]))
            if gate != 0: # Gate 0 is not calibrated, it is the reference and set with MMU_CALIBRATE_GEAR
                gate0_rd = self.mmu.rotation_distances[0]
                tolerance_range = (gate0_rd - gate0_rd * 0.2, gate0_rd + gate0_rd * 0.2) # Allow 20% variation from gate 0
                if tolerance_range[0] <= new_rd < tolerance_range[1]:
                    if save:
                        self.mmu.set_gear_rotation_distance(new_rd)
                        self.update_gear_rd(new_rd, console_msg=True)
                else:
                    self.mmu.log_always("Calibration ignored because it is not considered valid (>20% difference from gate 0)")
            self.mmu._unload_gate()
            self.mmu._set_filament_pos_state(self.mmu.FILAMENT_POS_UNLOADED)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for gate %d failed. Aborting, because:\n%s" % (gate, str(ee)))


    def note_load_telemetry(self, bowden_move_ratio, homing_movement, deficit):
        if homing_movement is not None:
            homing_movement -= deficit
        self._autotune(self.mmu.DIRECTION_LOAD, bowden_move_ratio, homing_movement)


    def note_unload_telemetry(self, bowden_move_ratio, homing_movement, deficit):
        if homing_movement is not None:
            homing_movement -= deficit
        self._autotune(self.mmu.DIRECTION_UNLOAD, bowden_move_ratio, homing_movement)


    # Use data from load or unload operation to auto-calibrate / auto-tune
    #
    # Data we can use:
    #  - ratio of large bowden move to that measured by encoder (0 if it can't be relied on)
    #  - the amount of unexpected homing necessary to reach endstop. We want some homing
    #    movement but we can use excessive numbers for tuning (None indicates not available)
    #  - the direction of filament movement
    #
    # Things we could possibly tune from this infomation:
    #  - If gate 0, use the bowden move ratio to update encoder calibration ("encoder calibration"). Not reliable so not currently done!
    #  - If gate 0, use excess homing move to tune the calibrated bowden length ("bowden calibration")
    #    but only do this if bowden move ratio is reasonable. Can be done in both directions
    #  - If gate >0, use the bowden move ratio to set/tune the gear rotation_distance ("gate calibration")
    #    but only do this if homing movement data tells us we haven't overshot. Can be done in both directions
    #
    # Calibration replaces the previous value. Autotuning applies a moving average
    def _autotune(self, direction, bowden_move_ratio, homing_movement):
        msg = "Autotune: bowden move ratio: %.4f, Extra homing movement: %s" % (bowden_move_ratio, "n/a" if homing_movement is None else "%.1fmm" % homing_movement)
        if homing_movement is not None:

            # If sync-feedback is available it provides a better way to autotune rotation distance. This is retained for legacy cases
            has_tension, has_compression, has_proportional = self.mmu.sync_feedback_manager.get_active_sensors()

            # Encoder based automatic calibration of gate's gear rotation_distance
            # TODO Currently only works with gate >0. Could work with gate 0 if variable_rotation_distance is True
            # TODO and bowden is calibrated and we don't tune bowden below
            if (
                False and # TODO Temporarily disabled based on user's feedback until tested further
                not any([has_tension, has_compression, has_proportional]) and
                self.mmu.autotune_rotation_distance and
                self.mmu.mmu_machine.variable_rotation_distances and
                self.mmu.gate_selected > 0 and
                bowden_move_ratio > 0 and
                homing_movement > 0
            ):
                if direction in [self.mmu.DIRECTION_LOAD, self.mmu.DIRECTION_UNLOAD]:
                    current_rd = self.mmu.gear_rail.steppers[0].get_rotation_distance()[0]
                    new_rd = round(bowden_move_ratio * current_rd, 4)
                    gate0_rd = self.mmu.rotation_distances[0]

                    # Allow max 10% variation from gate 0 for autotune
                    if math.isclose(new_rd, gate0_rd, rel_tol=0.1):
                        if not self.mmu.calibrating and self.mmu.rotation_distances[self.mmu.gate_selected] > 0:
                            # Tuning existing calibration
                            new_rd = round((self.mmu.rotation_distances[self.mmu.gate_selected] * 5 + new_rd) / 6, 4) # Moving average
                            msg += ". Autotuned rotation_distance: %.4f for gate %d" % (new_rd, self.mmu.gate_selected)
                        if not math.isclose(current_rd, new_rd):
                            _ = self.mmu.update_gear_rd(new_rd, self.mmu.gate_selected)
                    else:
                        msg += ". Calculated rotation_distance: %.4f for gate %d failed sanity check and has been ignored" % (new_rd, self.mmu.gate_selected)


            # Automatic calibration of bowden length based on actual homing movement telemetry
            # TODO Currently only works with gate 0. Could work with other gates if variable_bowden_lengths is True and rotation distance is calibrated
            if (
                self.mmu.autotune_bowden_length and
                self.mmu.mmu_machine.require_bowden_move and
                self.mmu.gate_selected == 0 and
                (
                    0.9 < bowden_move_ratio < 1.1 or
                    not self.mmu.has_encoder()
                )
            ):
                if direction in [self.mmu.DIRECTION_LOAD, self.mmu.DIRECTION_UNLOAD]:
                    bowden_length = self.get_bowden_length()
                    # We expect homing_movement to be 0 if perfectly calibrated and perfect movement
                    # Note that we only change calibrated bowden length if extra homing is >1% of bowden length
                    error_tolerance = bowden_length * 0.01 # 1% of bowden length
                    if abs(homing_movement) > error_tolerance:
                        if homing_movement > 0:
                            new_bl = bowden_length + error_tolerance
                        else:
                            new_bl = bowden_length - error_tolerance
                    else:
                        new_bl = bowden_length
                    new_bl = round((bowden_length * 5 + new_bl) / 6, 1) # Still perform moving average to smooth changes
                    if not math.isclose(bowden_length, new_bl):
                        self.update_bowden_length(new_bl)
                        msg += " Autotuned bowden length: %.1f" % new_bl

            if self.mmu.gate_selected == 0 and homing_movement > 0 and bowden_move_ratio > 0:
                # Bowden movement based warning of encoder calibration aka MMU_CALIBRATE_ENCODER
                if not 0.95 < bowden_move_ratio < 1.05:
                    msg += ". Encoder measurement on gate 0 was outside of desired calibration range. You may want to check function or recalibrate"
        else:
            msg += ". Tuning not possible"

        self.mmu.log_debug(msg)

