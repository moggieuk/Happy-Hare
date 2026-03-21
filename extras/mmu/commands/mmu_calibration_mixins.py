# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Mixins (base class) for calibration commands
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


class CalibrationMixin(BaseCommand):
    """
    Base class for all calibration commands
    """

    def _calibrate_bowden_length_manual(self, gate_homing_max):
        """
        Bowden calibration - Method 1 (MANUAL=1)
        This method of bowden calibration is done in reverse and is a fallback. The user inserts filament to the
        actual extruder gear and we measure the distance necessary to home to the defined gate homing position
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        selector = mmu_unit.selector
        gate = mmu.gate_selected

        try:
            endstop = mmu_unit.p.gate_homing_endstop
            endstop_str = mmu._gate_homing_string()
            mmu.log_always(f"Calibrating bowden length on gate {gate} (manual method) using {endstop_str} as gate reference point")
            mmu._set_filament_direction(DIRECTION_UNLOAD)
            selector.filament_drive()
            mmu.log_always(f"Finding {endstop} endstop position...")
            homed = False

            if endstop == SENSOR_ENCODER:
                with mmu._require_encoder():
                    success = mmu._reverse_home_to_encoder(gate_homing_max)
                    if success:
                        homed = True
                        homing_movement, remaining_park = success

            else: # Gate sensor
                if not mmu.sensor_manager.check_sensor(endstop):
                    raise MmuError(
                        f"The {mmu_unit.p.gate_homing_endstop} sensor was not triggered at start of movement. "
                        f"Check sensor operation and that the filament was manually loaded to the extruder gear "
                        f"before starting the calibration"
                    )

                homing_movement, homed, measured, _ = mmu.trace_filament_move(
                    f"Reverse homing off gate endstop {endstop}",
                    -gate_homing_max,
                    motor="gear",
                    homing_move=-1,
                    endstop_name=endstop,
                )
                remaining_park = mmu_unit.p.gate_parking_distance

            self.trace_filament_move("Final parking", remaining_park)
            mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)

            if not homed:
                raise MmuError("Did not home to gate sensor after moving %.1fmm" % gate_homing_max)

            homing_movement = abs(homing_movement)
            mmu.log_always("Filament homed back to gate after %.1fmm movement" % homing_movement)
            return homing_movement

        except MmuError as ee:
            raise MmuError(
                f"Calibration of bowden length on gate {gate} failed. "
                f"Aborting because:\n{ee}"
            )


    def _calibrate_bowden_length_sensor(self, extruder_homing_max):
        """
        Bowden calibration - Method 2
        Automatic one-shot homing calibration from gate to endstop.
        Supports extruder (entry) sensor and sync-feedback compression sensor
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        gate = mmu.gate_selected

        try:
            mmu.log_always(
                f"Calibrating bowden length for gate {mmu.gate_selected} "
                f"using {mmu._gate_homing_string()} as gate reference point "
                f"and {mmu_unit.p.extruder_homing_endstop} as extruder homing point"
            )

            mmu._initialize_filament_position(dwell=True)
            overshoot = mmu._load_gate(allow_retry=False)

            if mmu_unit.p.extruder_homing_endstop in [SENSOR_EXTRUDER_ENTRY, SENSOR_COMPRESSION]:
                if mmu.sensor_manager.check_sensor(mmu_unit.p.extruder_homing_endstop):
                    raise MmuError(
                        f"The {mmu_unit.p.extruder_homing_endstop} sensor triggered before homing. "
                        "Check filament and sensor operation"
                    )

            homing_distance = mmu._home_to_extruder(extruder_homing_max)
            measured = mmu.get_encoder_distance(dwell=True) + mmu.get_encoder_dead_space()

            calibrated_length = round(overshoot + homing_distance, 1)

            msg = f"Filament homed to extruder after {homing_distance:.1f}mm movement"
            if mmu.has_encoder():
                msg += f"\n(encoder measured {(measured - mmu_unit.p.gate_parking_distance):.1f}mm)"
            mmu.log_always(msg)

            mmu._unload_bowden(calibrated_length) # Fast move
            mmu._unload_gate()
            return calibrated_length

        except MmuError as ee:
            raise MmuError(
                f"Calibration of bowden length on gate {gate} failed. "
                f"Aborting because:\n{ee}"
            )


    def _calibrate_bowden_length_collision(self, approximate_length, extruder_homing_max, repeats):
        """
        Bowden calibration - Method 3 (ENCODER based)
        Automatic calibration from gate to extruder entry sensor or collision with extruder gear (requires encoder)
        Allows for repeats to average result which is essential with encoder collision detection
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        selector = mmu_unit.selector
        gate = mmu.gate_selected

        orig_endstop = mmu_unit.p.extruder_homing_endstop
        try:
            # Can't allow "none" endstop during calibration so temporarily change it
            mmu_unit.p.extruder_homing_endstop = SENSOR_EXTRUDER_COLLISION

            mmu.log_always(
                f"Calibrating bowden length on gate {gate} using "
                f"{mmu._gate_homing_string()} as gate reference point "
                f"and encoder collision detection"
            )
            reference_sum = 0.
            successes = 0

            for i in range(repeats):
                mmu._initialize_filament_position(dwell=True)
                overshoot = mmu._load_gate(allow_retry=False)

                # Get close to extruder homing point based on user guidance
                mmu._load_bowden(approximate_length, start_pos=overshoot)

                mmu.log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                mmu._home_to_extruder(extruder_homing_max)
                actual = mmu._get_filament_position() - mmu_unit.p.gate_parking_distance
                measured = mmu.get_encoder_distance(dwell=True) + mmu.get_encoder_dead_space()
                spring = selector.filament_release(measure=True) if mmu.has_encoder() else 0.
                reference = actual - spring

                # When homing using collision, we expect the filament to spring back.
                if spring != 0:
                    msg = (
                        "-------------------------------------------------\n"
                        f"Pass #{i + 1}: Filament homed to extruder after {actual:.1f}mm movement\n"
                        "-------------------------------------------------\n"
                    )
                    if mmu.has_encoder():
                        msg += "\n(encoder measured %.1fmm, filament sprung back %.1fmm)" % (measured - mmu_unit.p.gate_parking_distance, spring)
                    mmu.log_always(msg)
                    reference_sum += reference
                    successes += 1
                else:
                    # No spring means we haven't reliably homed
                    mmu.log_always("Failed to detect a reliable home position on this attempt")

                mmu._initialize_filament_position(True)
                mmu._unload_bowden(reference)
                mmu._unload_gate()

            if successes == 0:
                raise MmuError("All %d attempts at homing failed. MMU needs some adjustments!" % repeats)

            return (reference_sum / successes)

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of bowden length on gate %d failed. Aborting because:\n%s" % (gate, str(ee)))

        finally:
            mmu_unit.p.extruder_homing_endstop = orig_endstop


    def _calibrate_encoder(self, length, repeats, min_speed, max_speed, accel, save=True):
        """
        Start: filament at start of bowden just past the encoder
        End: filament in the same spot in bowden
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        calibrator = mmu_unit.calibrator
        encoder = mmu_unit.encoder

        pos_values, neg_values = [], []
        mmu.log_always("%s over %.1fmm..." % ("Calibrating" if save else "Validating calibration", length))
        speed_incr = (max_speed - min_speed) / max(repeats - 1, 1)
        test_speed = min_speed
        mean = 0

        try:
            for x in range(repeats):
                if speed_incr > 0.:
                    mmu.log_always("Test run #%d, Speed=%.1f mm/s" % (x + 1, test_speed))

                # Move forward
                mmu._initialize_filament_position(dwell=True)
                mmu.trace_filament_move(None, length, speed=test_speed, accel=accel, wait=True)
                pos_counts = mmu._get_encoder_counts(dwell=True)
                pos_values.append(pos_counts)
                mmu.log_always("%s+ counts: %d" % (UI_SPACE*2, pos_counts))

                # Move backward
                mmu._initialize_filament_position(dwell=True)
                mmu.trace_filament_move(None, -length, speed=test_speed, accel=accel, wait=True)
                neg_counts = mmu._get_encoder_counts(dwell=True)
                neg_values.append(neg_counts)
                mmu.log_always("%s- counts: %d" % (UI_SPACE*2, neg_counts))

                if pos_counts == 0 or neg_counts == 0: break

                test_speed += speed_incr

            mean_pos = mmu._sample_stats(pos_values)['mean']
            mean_neg = mmu._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2

            if mean == 0:
                mmu.log_always("No counts measured. Ensure a tool was selected with filament gripped before running calibration and that your encoder is working properly")
                return

            resolution = length / mean
            prev_measured_length = mean * encoder.get_resolution()

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % mmu._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % mmu._sample_stats(neg_values)
            mmu.log_always(msg)

            # Sanity check to ensure all teeth are reflecting / being counted. 20% tolerance
            if (abs(resolution - encoder.get_resolution()) / encoder.get_resolution()) > 0.2:
                mmu.log_warning("Warning: Encoder is not detecting the expected number of counts based on CAD parameters which may indicate an issue")

            msg = "Before calibration measured length: %.2fmm" % prev_measured_length
            msg += "\nCalculated resolution of the encoder: %.4f (currently: %.4f)" % (resolution, encoder.get_resolution())
            mmu.log_always(msg)

            if save:
                encoder.set_resolution(resolution)
                mmu.var_manager.set(VARS_MMU_ENCODER_RESOLUTION, round(resolution, 4), namespace=encoder.name, write=True)
                mmu.log_always("Encoder calibration has been saved")
                calibrator.calibration_status |= CALIBRATED_ENCODER

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of encoder failed. Aborting, because:\n%s" % str(ee))

        finally:
            if mean == 0:
                mmu._set_filament_pos_state(FILAMENT_POS_UNKNOWN)


    def _calibrate_gate(self, gate, length, repeats, save=True):
        """
        Automatically calibrate the rotation_distance for gate>0 using encoder measurements and gate 0 as reference
        Gate 0 is always calibrated with MMU_CALILBRATE_GEAR
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        calibrator = mmu_unit.calibrator
        lgate = mmu_unit.local_gate(gate)

        try:
            pos_values, neg_values = [], []
            mmu.select_gate(gate)
            mmu._load_gate(allow_retry=False)
            mmu.log_always("%s gate %d over %.1fmm..." % ("Calibrating" if (gate > 0 and save) else "Validating calibration of", gate, length))

            if lgate == 0:
                mmu.log_always(
                    f"Gate 0 on {mmu_unit.name} is calibrated with MMU_CALIBRATE_GEAR "
                    f"and manual measurement, so this will run as a validation that "
                    f"encoder is calibrated correctly"
                )

            for _ in range(repeats):
                mmu._initialize_filament_position(dwell=True)
                _,_,measured,delta = mmu.trace_filament_move("Calibration load movement", length, encoder_dwell=True)
                pos_values.append(measured)
                mmu.log_always("%s+ measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), mmu._get_encoder_counts(dwell=None)))

                mmu._initialize_filament_position(dwell=True)
                _,_,measured,delta = mmu.trace_filament_move("Calibration unload movement", -length, encoder_dwell=True)
                neg_values.append(measured)
                mmu.log_always("%s- measured: %.1fmm (counts: %d)" % (UI_SPACE*2, (length - delta), mmu._get_encoder_counts(dwell=None)))

            pos_stats = mmu._sample_stats(pos_values)
            neg_stats = mmu._sample_stats(neg_values)
            msg = (
                f"Load direction:   mean={pos_stats['mean']:.2f} "
                f"stdev={pos_stats['stdev']:.2f} min={pos_stats['min']:.2f} "
                f"max={pos_stats['max']:.2f} range={pos_stats['range']:.2f}\n"
                f"Unload direction: mean={neg_stats['mean']:.2f} "
                f"stdev={neg_stats['stdev']:.2f} min={neg_stats['min']:.2f} "
                f"max={neg_stats['max']:.2f} range={neg_stats['range']:.2f}"
            )
            mmu.log_always(msg)

            mean_pos = mmu._sample_stats(pos_values)['mean']
            mean_neg = mmu._sample_stats(neg_values)['mean']
            mean = (float(mean_pos) + float(mean_neg)) / 2
            ratio = mean / length

            current_rd = mmu_unit.gear_stepper_obj(gate).get_rotation_distance()[0]
            new_rd = round(ratio * current_rd, 4)

            mmu.log_always(
                f"Calibration move of {repeats * 2} x {length:.1f}mm, "
                f"average encoder measurement: {mean:.1f}mm - Ratio is {ratio:.4f}\n"
                f"Calculated gate {gate} rotation_distance: {new_rd:.4f} "
                f"(currently: {calibrator.get_gear_rd(gate):.4f})"
            )

            if lgate != 0: # Local gate 0 is not calibrated, it is the reference and set with MMU_CALIBRATE_GEAR
                gate0_rd = calibrator.get_gear_rd(mmu_unit.first_gate)
                tolerance_range = (gate0_rd - gate0_rd * 0.2, gate0_rd + gate0_rd * 0.2) # Allow 20% variation from gate 0
                if tolerance_range[0] <= new_rd < tolerance_range[1]:
                    if save:
                        calibrator.set_gear_rd(new_rd)
                        calibrator.update_gear_rd(new_rd, console_msg=True)
                else:
                    mmu.log_always(
                        f"Calibration ignored because it is not considered valid "
                        f"(>20% difference from reference gate {mmu_unit.first_gate})"
                    )

            mmu._unload_gate()
            mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for gate %d failed. Aborting, because:\n%s" % (gate, str(ee)))


    def _probe_toolhead(self, cold_temp=70, probe_depth=100, sensor_homing=80):
        """
        Helper for MMU_CALIBRATE_TOOLHEAD that probes toolhead to measure three key dimensions:

          toolhead_extruder_to_nozzle
          toolhead_sensor_to_nozzle
          toolhead_entry_to_extruder

        Filament is assumed to be at the extruder and will be at extruder again when complete
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        selector = mmu_unit.selector
        extruder_name = mmu_unit.extruder_name

        # -------------------------------------------------------------------------
        # Ensure extruder is COLD
        # -------------------------------------------------------------------------
        mmu.gcode.run_script_from_command(
            "SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % extruder_name
        )

        current_temp = self.printer.lookup_object(extruder_name).get_status(0)["temperature"]

        if current_temp > cold_temp:
            mmu.log_always("Waiting for extruder to cool")
            mmu.gcode.run_script_from_command(
                "TEMPERATURE_WAIT SENSOR=%s MINIMUM=0 MAXIMUM=%d"
                % (extruder_name, cold_temp)
            )

        # -------------------------------------------------------------------------
        # Enable the extruder stepper
        # -------------------------------------------------------------------------
        stepper_enable = self.printer.lookup_object("stepper_enable")
        extruder_stepper = mmu_unit.extruder_stepper_obj().stepper

        ge = stepper_enable.lookup_enable(extruder_stepper.get_name())
        ge.motor_enable(mmu.toolhead.get_last_move_time())

        # -------------------------------------------------------------------------
        # Force filament to nozzle
        # -------------------------------------------------------------------------
        selector.filament_drive()

        actual, fhomed, _, _ = mmu.trace_filament_move(
            "Homing to toolhead sensor",
            mmu_unit.p.toolhead_homing_max,
            motor="gear+extruder",
            homing_move=1,
            endstop_name=SENSOR_TOOLHEAD,
        )

        if not fhomed:
            raise MmuError(
                "Failed to reach toolhead sensor after moving %.1fmm"
                % mmu_unit.p.toolhead_homing_max
            )

        selector.filament_release()

        actual, _, _, _ = mmu.trace_filament_move(
            "Forcing filament to nozzle",
            probe_depth,
            motor="extruder",
        )

        # -------------------------------------------------------------------------
        # Measure: toolhead_sensor_to_nozzle
        # -------------------------------------------------------------------------
        selector.filament_drive()

        actual, fhomed, _, _ = mmu.trace_filament_move(
            "Reverse homing off toolhead sensor",
            -probe_depth,
            motor="gear+extruder",
            homing_move=-1,
            endstop_name=SENSOR_TOOLHEAD,
        )

        if not fhomed:
            raise MmuError("Failed to reverse home to toolhead sensor")

        toolhead_sensor_to_nozzle = -actual
        mmu.log_always(
            "Measured toolhead_sensor_to_nozzle: %.1f"
            % toolhead_sensor_to_nozzle
        )

        # -------------------------------------------------------------------------
        # Move back to extruder entrance
        # -------------------------------------------------------------------------
        selector.filament_release()

        actual, _, _, _ = mmu.trace_filament_move(
            "Moving to extruder entrance",
            -(probe_depth - toolhead_sensor_to_nozzle),
            motor="extruder",
        )

        # -------------------------------------------------------------------------
        # Measure: toolhead_extruder_to_nozzle
        # -------------------------------------------------------------------------
        selector.filament_drive()

        actual, fhomed, _, _ = mmu.trace_filament_move(
            "Homing to toolhead sensor",
            mmu_unit.p.toolhead_homing_max,
            motor="gear+extruder",
            homing_move=1,
            endstop_name=SENSOR_TOOLHEAD,
        )

        if not fhomed:
            raise MmuError("Failed to home to toolhead sensor")

        toolhead_extruder_to_nozzle = actual + toolhead_sensor_to_nozzle

        mmu.log_always(
            "Measured toolhead_extruder_to_nozzle: %.1f"
            % toolhead_extruder_to_nozzle
        )

        # -------------------------------------------------------------------------
        # Measure: toolhead_entry_to_extruder (if sensor exists)
        # -------------------------------------------------------------------------
        toolhead_entry_to_extruder = 0.0

        if mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
            # Retract and home to extruder entry sensor
            actual, fhomed, _, _ = mmu.trace_filament_move(
                "Reverse homing off extruder entry sensor",
                -(sensor_homing + toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle),
                motor="gear+extruder",
                homing_move=-1,
                endstop_name=SENSOR_EXTRUDER_ENTRY,
            )

            actual, _, _, _ = mmu.trace_filament_move(
                "Moving before extruder entry sensor",
                -20,
                motor="gear+extruder",
            )

            actual, fhomed, _, _ = mmu.trace_filament_move(
                "Homing to extruder entry sensor",
                40,
                motor="gear+extruder",
                homing_move=1,
                endstop_name=SENSOR_EXTRUDER_ENTRY,
            )

            if not fhomed:
                raise MmuError("Failed to reverse home to extruder entry sensor")

            # Measure relative to toolhead sensor
            actual, fhomed, _, _ = mmu.trace_filament_move(
                "Homing to toolhead sensor",
                sensor_homing,
                motor="gear+extruder",
                homing_move=1,
                endstop_name=SENSOR_TOOLHEAD,
            )

            if fhomed:
                toolhead_entry_to_extruder = actual - (
                    toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle
                )

                mmu.log_always(
                    "Measured toolhead_entry_to_extruder: %.1f"
                    % toolhead_entry_to_extruder
                )

        # -------------------------------------------------------------------------
        # Unload and re-park filament
        # -------------------------------------------------------------------------
        selector.filament_release()

        actual, _, _, _ = mmu.trace_filament_move(
            "Moving to extruder entrance",
            -sensor_homing,
            motor="extruder",
        )

        return (
            toolhead_extruder_to_nozzle,
            toolhead_sensor_to_nozzle,
            toolhead_entry_to_extruder,
        )
