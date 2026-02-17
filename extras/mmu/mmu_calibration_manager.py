# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Manager class to handle all aspects of MMU unit calibration and autotuning. In
#       paricular manage persistence of bowden lengths and gear rotation distances.
#
# Implements commands:
#   MMU_CALIBRATE_GEAR
#   MMU_CALIBRATE_ENCODER
#   MMU_CALIBRATE_BOWDEN
#   MMU_CALIBRATE_GATES
#   MMU_CALIBRATE_TOOLHEAD
#   MMU_CALIBRATE_PSENSOR
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

    # Calibration steps
    CALIBRATED_GEAR_0    = 0b00001 # Specifically rotation_distance for gate 0
    CALIBRATED_ENCODER   = 0b00010
    CALIBRATED_SELECTOR  = 0b00100 # Defaults true with VirtualSelector
    CALIBRATED_BOWDENS   = 0b01000 # Bowden length for all gates
    CALIBRATED_GEAR_RDS  = 0b10000 # rotation_distance for other gates (optional)
    CALIBRATED_ESSENTIAL = 0b01111
    CALIBRATED_ALL       = 0b11111

    # mmu_vars.cfg parameters
    VARS_MMU_GEAR_ROTATION_DISTANCES = "mmu_gear_rotation_distances"
    VARS_MMU_BOWDEN_LENGTHS          = "mmu_bowden_lengths"      # Per-gate calibrated bowden lengths
    VARS_MMU_BOWDEN_HOME             = "mmu_bowden_home"         # The endstop used as reference point: encoder, gate or gear sensor
    VARS_MMU_ENCODER_RESOLUTION      = "mmu_encoder_resolution"  # Calibrated encoder resolution (overrides config default)
    VARS_MMU_ENCODER_CLOG_LENGTH     = "mmu_encoder_clog_length" # Autotuned clog detection length if in automatic mode


    def __init__(self, mmu_unit):
        self.mmu_unit = mmu_unit                # This physical MMU
        self.mmu = unit.mmu			# Shared MMU operation class
        self.mmu_machine = self.mmu.mmu_machine # Entire Logical MMU

# PAUL why not have a calibration manger per unit?
#        mmu_unit = self.mmu_unit(gate)
#        first_gate = mmu_unit.first_gate
#        last_gate = mmu_unit.first_gate + mmu_unit.num_gates - 1

        self.calibration_status = 0b0

        # Register GCODE commands ---------------------------------------------------------------------------
        self.gcode.register_command('MMU_CALIBRATE_GEAR', self.cmd_MMU_CALIBRATE_GEAR, desc=self.cmd_MMU_CALIBRATE_GEAR_help)
        self.gcode.register_command('MMU_CALIBRATE_ENCODER', self.cmd_MMU_CALIBRATE_ENCODER, desc=self.cmd_MMU_CALIBRATE_ENCODER_help)
        self.gcode.register_command('MMU_CALIBRATE_BOWDEN', self.cmd_MMU_CALIBRATE_BOWDEN, desc = self.cmd_MMU_CALIBRATE_BOWDEN_help)
        self.gcode.register_command('MMU_CALIBRATE_GATES', self.cmd_MMU_CALIBRATE_GATES, desc = self.cmd_MMU_CALIBRATE_GATES_help)
        self.gcode.register_command('MMU_CALIBRATE_TOOLHEAD', self.cmd_MMU_CALIBRATE_TOOLHEAD, desc = self.cmd_MMU_CALIBRATE_TOOLHEAD_help)
        self.gcode.register_command('MMU_CALIBRATE_PSENSOR', self.cmd_MMU_CALIBRATE_PSENSOR, desc = self.cmd_MMU_CALIBRATE_PSENSOR_help)


    def handle_connect():
        # Load bowden length configuration (calibration set with MMU_CALIBRATE_BOWDEN) ----------------------
        bowden_lengths = self.mmu.var_manager.get(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, None, namespace=mmu_unit.name)
        bowden_home = self.mmu.var_manager.get(self.VARS_MMU_CALIB_BOWDEN_HOME, self.gate_homing_endstop, namespace=mmu_unit.name)
        if mmu_unit.require_bowden_move:
            if bowden_lengths and bowden_home in self.GATE_ENDSTOPS:
                bowden_lengths = [-1 if x < 0 else x for x in bowden_lengths] # Ensure -1 value for uncalibrated
                # Ensure list size
                if len(bowden_lengths) == mmu_unit.num_gates:
                    self.log_debug("Loaded saved bowden lengths for unit %s: %s" % (mmu_unit.name, bowden_lengths))
                else:
                    self.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.var_manager.namespace(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, namespace=mmu_unit.name))
                    bowden_lengths = self._ensure_list_size(bowden_lengths, mmu_unit.num_gates)

                # Ensure they are identical (just for optics) if variable_bowden_lengths is False
                if not mmu_unit.variable_bowden_lengths:
                    bowden_lengths = [bowden_lengths[0]] * mmu_unit.num_gates

                self.calibration_manager.adjust_bowden_lengths_on_homing_change(mmu_unit, bowden_home)
                if not any(x == -1 for x in bowden_lengths):
                    self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_BOWDENS)
            else:
                self.log_warning("Warning: Bowden lengths for unit %s not found in mmu_vars.cfg. Probably not calibrated yet" % mmu_unit.name)
                bowden_lengths = [-1] * mmu_unit.num_gates
        else:
            bowden_lengths = [0] * mmu_unit.num_gates
            self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_BOWDENS)

        # Upgrade step 2: Separate per mmu_unit ---------------------------------------------------------------------
        # Assume non-namespaced variables pertain to first mmu_unit or first encoder. This isn't perfect but it
        # should make it easier for most users upgrading from v3
        first_unit = self.mmu_machine.units[0]
        for var in [
            self.VARS_MMU_GEAR_ROTATION_DISTANCES,
            self.VARS_MMU_CALIB_BOWDEN_LENGTHS,
            self.VARS_MMU_CALIB_BOWDEN_HOME
        ]:
            self.var_manager.upgrade(var, first_unit.name)

        for g in range(first_unit.num_gates):
            self.var_manager.upgrade("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, g), first_unit.name)

        # We don't expect more but cleanup if we have any (backup will contain old values anyway)
        for g in range(first_unit.num_gates, 24):
            self.var_manager.delete("%s%d" % (self.VARS_MMU_GATE_STATISTICS_PREFIX, g))

        # Now handle encoder
        first_encoder = next((unit.encoder for unit in self.mmu_machine.units if unit.encoder is not None), None)
        if first_encoder is not None:
            self.var_manager.upgrade(self.VARS_MMU_ENCODER_RESOLUTION, first_encoder.name)
            self.var_manager.upgrade(self.VARS_MMU_CALIB_CLOG_LENGTH, first_encoder.name)

        # These arrays are per gate on the logical (combined) MMU
        self.bowden_lengths = []
        self.default_rotation_distances = []
        self.rotation_distances = []

        units_with_encoder = []
        for i, mmu_unit in enumerate(self.mmu_machine.units):

            # Load bowden length configuration (calibration set with MMU_CALIBRATE_BOWDEN) ----------------------
            bowden_lengths = self.mmu.var_manager.get(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, None, namespace=mmu_unit.name)
            bowden_home = self.mmu.var_manager.get(self.VARS_MMU_CALIB_BOWDEN_HOME, self.gate_homing_endstop, namespace=mmu_unit.name)
            if mmu_unit.require_bowden_move:
                if bowden_lengths and bowden_home in self.GATE_ENDSTOPS:
                    bowden_lengths = [-1 if x < 0 else x for x in bowden_lengths] # Ensure -1 value for uncalibrated
                    # Ensure list size
                    if len(bowden_lengths) == mmu_unit.num_gates:
                        self.log_debug("Loaded saved bowden lengths for unit %s: %s" % (mmu_unit.name, bowden_lengths))
                    else:
                        self.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.var_manager.namespace(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, namespace=mmu_unit.name))
                        bowden_lengths = self._ensure_list_size(bowden_lengths, mmu_unit.num_gates)

                    # Ensure they are identical (just for optics) if variable_bowden_lengths is False
                    if not mmu_unit.variable_bowden_lengths:
                        bowden_lengths = [bowden_lengths[0]] * mmu_unit.num_gates

                    self.calibration_manager.adjust_bowden_lengths_on_homing_change(mmu_unit, bowden_home)
                    if not any(x == -1 for x in bowden_lengths):
                        self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_BOWDENS)
                else:
                    self.log_warning("Warning: Bowden lengths for unit %s not found in mmu_vars.cfg. Probably not calibrated yet" % mmu_unit.name)
                    bowden_lengths = [-1] * mmu_unit.num_gates
            else:
                bowden_lengths = [0] * mmu_unit.num_gates
                self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_BOWDENS)

            self.var_manager.set(self.VARS_MMU_CALIB_BOWDEN_LENGTHS, bowden_lengths, namespace=mmu_unit.name)
            self.var_manager.set(self.VARS_MMU_CALIB_BOWDEN_HOME, bowden_home, namespace=mmu_unit.name)
            self.bowden_lengths.extend(bowden_lengths)

            # Load gear rotation distance configuration (calibration set with MMU_CALIBRATE_GEAR) ---------------
            gear_steppers = mmu_unit.mmu_toolhead.get_kinematics().rails[1].steppers
            rds = [s.get_rotation_distance()[0] for s in gear_steppers[:mmu_unit.num_gates]] if len(gear_steppers) >= mmu_unit.num_gates else [gear_steppers[0].get_rotation_distance()[0]] * mmu_unit.num_gates
            self.default_rotation_distances.extend(rds)
            rotation_distances = self.var_manager.get(self.VARS_MMU_GEAR_ROTATION_DISTANCES, None, namespace=mmu_unit.name)
            if rotation_distances:
                rotation_distances = [-1 if x == 0 else x for x in rotation_distances] # Ensure -1 value for uncalibrated
                # Ensure list size
                if len(rotation_distances) == mmu_unit.num_gates:
                    self.log_debug("Loaded saved gear rotation distances for unit %s: %s" % (mmu_unit.name, rotation_distances))
                else:
                    self.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.var_manager.namespace(self.VARS_MMU_GEAR_ROTATION_DISTANCES, namespace=mmu_unit.name))
                    rotation_distances = self._ensure_list_size(rotation_distances, mmu_unit.num_gates)

                # Ensure they are identical (just for optics) if variable_rotation_distances is False
                if not self.mmu_unit().variable_rotation_distances:
                    rotation_distances = [rotation_distances[0]] * mmu_unit.num_gates

                if rotation_distances[0] != -1:
                    self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_0)
                if not any(x == -1 for x in rotation_distances):
                    self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_RDS)
            else:
                self.log_warning("Warning: Gear rotation distances for unit %s not found in mmu_vars.cfg. Probably not calibrated yet" % mmu_unit.name)
                rotation_distances = [-1] * mmu_unit.num_gates

            self.var_manager.set(self.VARS_MMU_GEAR_ROTATION_DISTANCES, rotation_distances, namespace=mmu_unit.name)
            self.rotation_distances.extend(rotation_distances)

            # Gather mmu_units with encoder (generally 0 or all of them)
            if mmu_unit.encoder is not None:
                units_with_encoder.append(mmu_unit)
            else:
                self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_ENCODER) # Pretend we are calibrated to avoid warnings

        self.log_warning("PAUL: self.bowden_lengths=%s" % self.bowden_lengths)
        self.log_warning("PAUL: self.default_rotation_distances=%s" % self.default_rotation_distances)
        self.log_warning("PAUL: self.rotation_distances=%s" % self.rotation_distances)





    def _unit(self, mmu_unit):
        if mmu_unit is None:
            mmu_unit = self.mmu_unit()
        return mmu_unit

    def mark_calibrated(self, mmu_unit, step):
        self.calibration_status[self._unit(mmu_unit).unit_index] |= step

    def mark_not_calibrated(self, mmu_unit, step):
        self.calibration_status[self._unit(mmu_unit).unit_index] &= ~step

    def check_calibrated(self, mmu_unit, step):
        return self.calibration_status[self._unit(mmu_unit).unit_index] & step == step



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
            self.mmu.calibration_status |= self.CALIBRATED_BOWDENS

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
            self.mmu.calibration_status |= self.CALIBRATED_GEAR_0
        if not any(x == -1 for x in self.mmu.rotation_distances):
            self.mmu.calibration_status |= self.CALIBRATED_GEAR_RDS

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
                self.mmu.calibration_status |= self.CALIBRATED_ENCODER

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



    # ------------------ Autotuning from telemetry data -------------------

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
                self.mmu.mmu_unit().variable_rotation_distances and
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
                self.mmu.mmu_unit().require_bowden_move and
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



    # ------------------ Calibration Gcode Commands -------------------------

    cmd_MMU_CALIBRATE_GEAR_help = "Calibration routine for gear stepper rotational distance"
    def cmd_MMU_CALIBRATE_GEAR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_gate_not_valid(): return
        length = gcmd.get_float('LENGTH', 100., above=50.)
        measured = gcmd.get_float('MEASURED', -1, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        gate = self.gate_selected if self.gate_selected >= 0 else 0

        with self.wrap_sync_gear_to_extruder():
            if reset:
                self.set_gear_rotation_distance(self.default_rotation_distance)
                self.calibration_manager.update_gear_rd(-1)
                return

            if measured > 0:
                current_rd = self.gear_rail().steppers[0].get_rotation_distance()[0]
                new_rd = round(current_rd * measured / length, 4)
                self.log_always("MMU gear stepper 'rotation_distance' calculated to be %.4f (currently: %.4f)" % (new_rd, current_rd))
                if save:
                    self.set_gear_rotation_distance(new_rd)
                    self.calibration_manager.update_gear_rd(new_rd, console_msg=True)
                return

            raise gcmd.error("Must specify 'MEASURED=' and optionally 'LENGTH='")

# PAUL old logic
#        with self.wrap_sync_gear_to_extruder():
#            mmu_unit = self.mmu_unit()
#            first_gate = self.mmu_unit().first_gate
#            last_gate = self.mmu_unit().first_gate + self.mmu_unit().num_gates - 1
#
#            if reset:
#                self.set_rotation_distance(self.default_rotation_distances[self.gate_selected])
#                self.rotation_distances[first_gate:last_gate] = self.default_rotation_distances[first_gate:last_gate]
#                self.var_manager.set(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances[first_gate:last_gate], write=True, namespace=mmu_unit.name)
#                self.log_always("Gear calibration for all gates on unit %s has been reset" % mmu_unit.name)
#
#                self.calibration_manager.mark_not_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_0) # TODO ***********************
#                self.calibration_manager.mark_not_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_RDS) # TODO ***********************
#
#            elif measured > 0:
#                current_rd = self.gear_rail().steppers[0].get_rotation_distance()[0]
#                new_rd = round(current_rd * measured / length, 4)
#                self.log_always("Gear stepper 'rotation_distance' calculated to be %.4f (currently: %.4f)" % (new_rd, current_rd))
#                if save:
#                    self.set_rotation_distance(new_rd)
#
#                    all_gates = False
#                    if not self.mmu_unit.variable_rotation_distances or (gate == first_gate and self.rotation_distances[first_gate] == 0.):
#                        # Initial calibration on gate 0 sets all gates as auto calibration starting point
#                        self.rotation_distances[first_gate:last_gate] = [new_rd] * mmu_unit.num_gates
#                        all_gates = True
#                    else:
#                        self.rotation_distances[gate] = new_rd
#
#                    self.var_manager.set(self.VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances[first_gate:last_gate], write=True, namespace=mmu_unit.name)
#                    self.log_always("Gear calibration for %s on unit %s has been saved" % (("all gates" if all_gates else "gate %d" % gate), mmu_unit.name))
#
#                    # This feature can be used to calibrate any gate gear but gate 0 on unit is mandatory
#                    if self.rotation_distances[0] != -1:
#                        self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_0) # TODO ***********
#                    if not any(x == -1 for x in self.rotation_distances):
#                        self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_RDS) # TODO ***********
#            else:
#                raise gcmd.error("Must specify 'MEASURED=' and optionally 'LENGTH='")

    # Start: Assumes filament is loaded through encoder
    # End: Does not eject filament at end (filament same as start)
    cmd_MMU_CALIBRATE_ENCODER_help = "Calibration routine for the MMU encoder"
    def cmd_MMU_CALIBRATE_ENCODER(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self._check_has_encoder(): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0, check_gates=[self.gate_selected]): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        speed = gcmd.get_float('SPEED', self.gear_from_buffer_speed, minval=10.)
        accel = gcmd.get_float('ACCEL', self.gear_from_buffer_accel, minval=10.)
        min_speed = gcmd.get_float('MINSPEED', speed, above=0.)
        max_speed = gcmd.get_float('MAXSPEED', speed, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        advance = 60. # Ensure filament is in encoder even if not loaded by user

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._require_encoder():
                    self.selector.filament_drive()
                    self.calibrating = True
                    _,_,measured,_ = self.trace_filament_move("Checking for filament", advance)
                    if measured < self.encoder_min:
                        raise MmuError("Filament not detected in encoder. Ensure filament is available and try again")
                    self._unload_tool()
                    self.calibration_manager.calibrate_encoder(length, repeats, speed, min_speed, max_speed, accel, save)
                    _,_,_,_ = self.trace_filament_move("Parking filament", -advance)
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

    # Calibrated bowden length is always from chosen gate homing point to the entruder gears
    # Start: With desired gate selected
    # End: Filament will be unloaded
    cmd_MMU_CALIBRATE_BOWDEN_help = "Calibration of reference bowden length for selected gate"
    def cmd_MMU_CALIBRATE_BOWDEN(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_no_bowden_move(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_gate_not_valid(): return

        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        manual = bool(gcmd.get_int('MANUAL', 0, minval=0, maxval=1))
        collision = bool(gcmd.get_int('COLLISION', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            self.calibration_manager.update_bowden_length(-1, console_msg=True)
            return

        if manual:
            if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_SELECTOR, check_gates=[self.gate_selected]): return
        else:
            if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR, check_gates=[self.gate_selected]): return

        can_use_sensor = (
            self.extruder_homing_endstop in [
                self.SENSOR_EXTRUDER_ENTRY,
                self.SENSOR_COMPRESSION,
                self.SENSOR_GEAR_TOUCH
            ] and (
                self.sensor_manager.has_sensor(self.extruder_homing_endstop) or
                self.gear_rail.is_endstop_virtual(self.extruder_homing_endstop)
            )
        )
        can_auto_calibrate = self.has_encoder() or can_use_sensor

        if not can_auto_calibrate and not manual:
            self.log_always("No encoder or extruder entry sensor available. Use manual calibration method:\nWith gate selected, manually load filament all the way to the extruder gear\nThen run 'MMU_CALIBRATE_BOWDEN MANUAL=1 BOWDEN_LENGTH=xxx'\nWhere BOWDEN_LENGTH is greater than your real length")
            return

        extruder_homing_max = gcmd.get_float('HOMING_MAX', 150, above=0.)
        approx_bowden_length = gcmd.get_float('BOWDEN_LENGTH', self.bowden_homing_max if (manual or can_use_sensor) else None, above=0.)
        if not approx_bowden_length:
            raise gcmd.error("Must specify 'BOWDEN_LENGTH=x' where x is slightly LESS than your estimated bowden length to give room for homing")

        try:
            with self.wrap_sync_gear_to_extruder():
                with self._wrap_suspend_filament_monitoring():
                    self.calibrating = True
                    if manual:
                        # Method 1: Manual (reverse homing to gate) method
                        length = self.calibration_manager.calibrate_bowden_length_manual(approx_bowden_length)

                    elif can_use_sensor and not collision:
                        # Method 2: Automatic one-shot method with homing sensor (BEST)
                        self._unload_tool()
                        length = self.calibration_manager.calibrate_bowden_length_sensor(approx_bowden_length)

                    elif self.has_encoder():
                        # Method 3: Automatic averaging method with encoder and extruder collision. Uses repeats for accuracy
                        self._unload_tool()
                        length = self.calibration_manager.calibrate_bowden_length_collision(approx_bowden_length, extruder_homing_max, repeats)

                    else:
                        raise gcmd.error("Invalid configuration or options provided. Perhaps you tried COLLISION=1 without encoder or don't have extruder_homing_endstop set?")

                    cdl = None
                    msg = "Calibrated bowden length is %.1fmm" % length
                    if self.has_encoder():
                        cdl = self.calibration_manager.calc_clog_detection_length(length)
                        msg += ". Recommended flowguard_encoder_max_motion (clog detection length): %.1fmm" % cdl
                    self.log_always(msg)

                    if save:
                        self.calibration_manager.update_bowden_length(length, console_msg=True)
                        if cdl is not None:
                            self.calibration_manager.update_clog_detection_length(length, force=True)

        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False


    # Start: Will home selector, select gate 0 or required gate
    # End: Filament will unload
    cmd_MMU_CALIBRATE_GATES_help = "Optional calibration of individual MMU gate"
    def cmd_MMU_CALIBRATE_GATES(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if gate == -1 and not all_gates:
            raise gcmd.error("Must specify 'GATE=' or 'ALL=1' for all gates")

        if reset:
            if all_gates:
                self.set_gear_rotation_distance(self.default_rotation_distance)
                for gate in range(self.num_gates - 1):
                    self.calibration_manager.update_gear_rd(-1, gate + 1)
            else:
                self.calibration_manager.update_gear_rd(-1, gate)
            return

        if self.check_if_not_calibrated(
            self.CALIBRATED_GEAR_0 | self.CALIBRATED_ENCODER | self.CALIBRATED_SELECTOR,
            check_gates=[gate] if gate != -1 else None
        ): return

        try:
            with self.wrap_sync_gear_to_extruder():
                self._unload_tool()
                self.calibrating = True
                with self._require_encoder():
                    if all_gates:
                        self.log_always("Start the complete calibration of ancillary gates...")
                        for gate in range(self.num_gates - 1):
                            self.calibration_manager.calibrate_gate(gate + 1, length, repeats, save=save)
                        self.log_always("Phew! End of auto gate calibration")
                    else:
                        self.calibration_manager.calibrate_gate(gate, length, repeats, save=(save and gate != 0))
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

# PAUL old logic
#    # Start: Will home selector, select gate 0 or required gate
#    # End: Filament will unload
#    cmd_MMU_CALIBRATE_GATES_help = "Optional calibration of individual MMU gate(s)"
#    def cmd_MMU_CALIBRATE_GATES(self, gcmd):
#        self.log_to_file(gcmd.get_commandline())
#        if self.check_if_disabled(): return
#        if self.check_if_not_homed(): return
#        if self.check_if_bypass(): return
#        length = gcmd.get_float('LENGTH', 400., above=0.)
#        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
#        auto = gcmd.get_int('ALL', 0, minval=0, maxval=1)
#        gate = gcmd.get_int('GATE', -1, minval=0, maxval=self.num_gates - 1)
#        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
#        if gate == -1 and not auto:
#            raise gcmd.error("Must specify 'GATE=' or 'ALL=1' for all gates")
#
#        if self.check_if_not_calibrated(
#            CalibrationManager.CALIBRATED_GEAR_0 | CalibrationManager.CALIBRATED_ENCODER | CalibrationManager.CALIBRATED_SELECTOR,
#            check_gates=[gate] if gate != -1 else None
#        ): return
#
#        try:
#            with self.wrap_sync_gear_to_extruder():
##                mmu_unit = self.mmu_unit()
##                first_gate = self.mmu_unit().first_gate
##                last_gate = self.mmu_unit().first_gate + self.mmu_unit().num_gates - 1
#
#                self._unload_tool()
#                self.calibrating = True
#                with self._require_encoder():
#                    if gate == -1:
#                        self.log_always("Start the complete calibration of ancillary gates...")
#                        for gate in range(self.num_gates - 1):
#                            self.calibration_manager.calibrate_gate(gate + 1, length, repeats, save=save)
#                        self.log_always("Phew! End of auto gate calibration")
#                    else:
#                        self.calibration_manager.calibrate_gate(gate, length, repeats, save=(save and gate != 0))
## PAUL FIXME .. this command must be per unit and this check on just slice of gates for that unit
#                if not any(x == -1 for x in self.rotation_distances[1:]):
#                    self.calibration_manager.mark_calibrated(mmu_unit, CalibrationManager.CALIBRATED_GEAR_RDS) # TODO ***********
#        except MmuError as ee:
#            self.handle_mmu_error(str(ee))
#        finally:
#            self.calibrating = False


    # Start: Test gate should already be selected
    # End: Filament will unload
    cmd_MMU_CALIBRATE_TOOLHEAD_help = "Automated measurement of key toolhead parameters"
    def cmd_MMU_CALIBRATE_TOOLHEAD(self, gcmd):
        self.log_to_file(gcmd.get_commandline())
        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(self.CALIBRATED_GEAR_0|self.CALIBRATED_ENCODER|self.CALIBRATED_SELECTOR|self.CALIBRATED_BOWDENS, check_gates=[self.gate_selected]): return
        if not self.sensor_manager.has_sensor(self.SENSOR_TOOLHEAD):
            raise gcmd.error("Sorry this feature requires a toolhead sensor")
        clean = gcmd.get_int('CLEAN', 0, minval=0, maxval=1)
        dirty = gcmd.get_int('DIRTY', 0, minval=0, maxval=1)
        cut = gcmd.get_int('CUT', 0, minval=0, maxval=1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        line = "-----------------------------------------------\n"

        if not (clean or cut or dirty):
            msg = "Reminder - run with this sequence of options:\n"
            msg += "1) 'CLEAN=1' with clean extruder for: toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle (and toolhead_entry_to_extruder)\n"
            msg += "2) 'DIRTY=1' with dirty extruder (no not cut tip fragment) for: toolhead_residual_filament (and toolhead_entry_to_extruder)\n"
            msg += "3) 'CUT=1' holding blade in for: variable_blade_pos\n"
            msg += "Desired gate should be selected but the filament unloaded\n"
            msg += "('SAVE=0' to run without persisting results)\n"
            msg += "Note: On Type-B MMU's you might experience noise/grinding as movement limits are explored (select bypass or reduce gear stepper current if a problem)\n"
            self.log_always(msg)
            return

        if cut:
            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
            if gcode_macro is None:
                raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)
            gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.form_tip_macro, gcode_macro)
            if not ('blade_pos' in gcode_vars.variables and 'retract_length' in gcode_vars.variables):
                raise gcmd.error("Filament tip forming macro '%s' does not look like a cutting macro!" % self.form_tip_macro)
        try:
            with self.wrap_sync_gear_to_extruder():
                self.calibrating = True
                self._initialize_filament_position(dwell=True)
                overshoot = self._load_gate(allow_retry=False)
                _,_ = self._load_bowden(start_pos=overshoot)
                _,_ = self._home_to_extruder(self.extruder_homing_max)

                if cut:
                    self.log_always("Measuring blade cutter postion (with filament fragment)...")
                    tetn, tstn, tete = self._probe_toolhead()
                    # Blade position is the difference between empty and extruder with full cut measurements for sensor to nozzle
                    vbp = self.toolhead_sensor_to_nozzle - tstn
                    msg = line
                    if abs(vbp - self.toolhead_residual_filament) < 5:
                        self.log_error("Measurements did not make sense. Looks like probing went past the blade pos!\nAre you holding the blade closed or have cut filament in the extruder?")
                    else:
                        msg += "Calibration Results (cut tip):\n"
                        msg += "> variable_blade_pos: %.1f (currently: %.1f)\n" % (vbp, gcode_vars.variables['blade_pos'])
                        msg += "> variable_retract_length: %.1f-%.1f, recommend: %.1f (currently: %.1f)\n" % (self.toolhead_residual_filament + self.toolchange_retract, vbp, vbp - 5., gcode_vars.variables['retract_length'])
                        msg += line
                        self.log_always(msg)
                        if save:
                            self.log_always("New calibrated blade_pos and retract_length active until restart. Update mmu_macro_vars.cfg to persist")
                            gcode_vars.variables['blade_pos'] = vbp
                            gcode_vars.variables['retract_length'] = vbp - 5.

                elif clean:
                    self.log_always("Measuring clean toolhead dimensions after cold pull...")
                    tetn, tstn, tete = self._probe_toolhead()
                    msg = line
                    msg += "Calibration Results (clean nozzle):\n"
                    msg += "> toolhead_extruder_to_nozzle: %.1f (currently: %.1f)\n" % (tetn, self.toolhead_extruder_to_nozzle)
                    msg += "> toolhead_sensor_to_nozzle: %.1f (currently: %.1f)\n" % (tstn, self.toolhead_sensor_to_nozzle)
                    if self.sensor_manager.has_sensor(self.SENSOR_EXTRUDER_ENTRY):
                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
                    msg += line
                    self.log_always(msg)
                    if save:
                        self.log_always("New toolhead calibration active until restart. Update mmu_parameters.cfg to persist settings")
                        self.toolhead_extruder_to_nozzle = round(tetn, 1)
                        self.toolhead_sensor_to_nozzle = round(tstn, 1)
                        self.toolhead_entry_to_extruder = round(tete, 1)

                elif dirty:
                    self.log_always("Measuring dirty toolhead dimensions (with filament residue)...")
                    tetn, tstn, tete = self._probe_toolhead()
                    # Ooze reduction is the difference between empty and dirty measurements for sensor to nozzle
                    tor = self.toolhead_sensor_to_nozzle - tstn
                    msg = line
                    msg += "Calibration Results (dirty nozzle):\n"
                    msg += "> toolhead_residual_filament: %.1f (currently: %.1f)\n" % (tor, self.toolhead_residual_filament)
                    if self.sensor_manager.has_sensor(self.SENSOR_EXTRUDER_ENTRY):
                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
                    msg += line
                    self.log_always(msg)
                    if save:
                        self.toolhead_residual_filament = round(tor, 1)
                        self.toolhead_entry_to_extruder = round(tete, 1)

                # Unload and park filament
                _ = self._unload_bowden()
                _,_ = self._unload_gate()
        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

# PAUL old logic
#    # Start: Test gate should already be selected
#    # End: Filament will unload
#    cmd_MMU_CALIBRATE_TOOLHEAD_help = "Automated measurement of key toolhead parameters"
#    def cmd_MMU_CALIBRATE_TOOLHEAD(self, gcmd):
#        self.log_to_file(gcmd.get_commandline())
#        if self.check_if_disabled(): return
#        if self.check_if_not_homed(): return
#        if self.check_if_bypass(): return
#        if self.check_if_loaded(): return
#        if self.check_if_not_calibrated(CalibrationManager.CALIBRATED_GEAR_0|CalibrationManager.CALIBRATED_ENCODER|CalibrationManager.CALIBRATED_SELECTOR|CalibrationManager.CALIBRATED_BOWDENS, check_gates=[self.gate_selected]): return
#        if not self.sensor_manager.has_sensor(self.SENSOR_TOOLHEAD):
#            raise gcmd.error("Sorry this feature requires a toolhead sensor")
#        clean = gcmd.get_int('CLEAN', 0, minval=0, maxval=1)
#        dirty = gcmd.get_int('DIRTY', 0, minval=0, maxval=1)
#        cut = gcmd.get_int('CUT', 0, minval=0, maxval=1)
#        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
#        line = "-----------------------------------------------\n"
#
#        if not (clean or cut or dirty):
#            msg = "Reminder - run with this sequence of options:\n"
#            msg += "1) 'CLEAN=1' with clean extruder for: toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle (and toolhead_entry_to_extruder)\n"
#            msg += "2) 'DIRTY=1' with dirty extruder (no not cut tip fragment) for: toolhead_residual_filament (and toolhead_entry_to_extruder)\n"
#            msg += "3) 'CUT=1' holding blade in for: variable_blade_pos\n"
#            msg += "Desired gate should be selected but the filament unloaded\n"
#            msg += "('SAVE=0' to run without persisting results)\n"
#            self.log_always(msg)
#            return
#
#        if cut:
#            gcode_macro = self.printer.lookup_object("gcode_macro %s" % self.form_tip_macro, None)
#            if gcode_macro is None:
#                raise gcmd.error("Filament tip forming macro '%s' not found" % self.form_tip_macro)
#            gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % self.form_tip_macro, gcode_macro)
#            if not ('blade_pos' in gcode_vars.variables and 'retract_length' in gcode_vars.variables):
#                raise gcmd.error("Filament tip forming macro '%s' does not look like a cutting macro!" % self.form_tip_macro)
#
#        try:
#            with self.wrap_sync_gear_to_extruder():
#                self.calibrating = True
#                self._initialize_filament_position(dwell=True)
#                overshoot = self._load_gate(allow_retry=False)
#                _,_ = self._load_bowden(start_pos=overshoot)
#                _,_ = self._home_to_extruder(self.extruder_homing_max)
#
#                if cut:
#                    self.log_always("Measuring blade cutter postion (with filament fragment)...")
#                    tetn, tstn, tete = self._probe_toolhead()
#                    # Blade position is the difference between empty and extruder with full cut measurements for sensor to nozzle
#                    vbp = self.toolhead_sensor_to_nozzle - tstn
#                    msg = line
#                    if abs(vbp - self.toolhead_residual_filament) < 5:
#                        self.log_error("Measurements did not make sense. Looks like probing went past the blade pos!\nAre you holding the blade closed or have cut filament in the extruder?")
#                    else:
#                        msg += "Calibration Results (cut tip):\n"
#                        msg += "> variable_blade_pos: %.1f (currently: %.1f)\n" % (vbp, gcode_vars.variables['blade_pos'])
#                        msg += "> variable_retract_length: %.1f-%.1f, recommend: %.1f (currently: %.1f)\n" % (self.toolhead_residual_filament + self.toolchange_retract, vbp, vbp - 5., gcode_vars.variables['retract_length'])
#                        msg += line
#                        self.log_always(msg)
#                        if save:
#                            self.log_always("New calibrated blade_pos and retract_length active until restart. Update mmu_macro_vars.cfg to persist")
#                            gcode_vars.variables['blade_pos'] = vbp
#                            gcode_vars.variables['retract_length'] = vbp - 5.
#
#                elif clean:
#                    self.log_always("Measuring clean toolhead dimensions after cold pull...")
#                    tetn, tstn, tete = self._probe_toolhead()
#                    msg = line
#                    msg += "Calibration Results (clean nozzle):\n"
#                    msg += "> toolhead_extruder_to_nozzle: %.1f (currently: %.1f)\n" % (tetn, self.toolhead_extruder_to_nozzle)
#                    msg += "> toolhead_sensor_to_nozzle: %.1f (currently: %.1f)\n" % (tstn, self.toolhead_sensor_to_nozzle)
#                    if self.sensor_manager.has_sensor(self.SENSOR_EXTRUDER_ENTRY):
#                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
#                    msg += line
#                    self.log_always(msg)
#                    if save:
#                        self.log_always("New toolhead calibration active until restart. Update mmu_parameters.cfg to persist settings")
#                        self.toolhead_extruder_to_nozzle = round(tetn, 1)
#                        self.toolhead_sensor_to_nozzle = round(tstn, 1)
#                        self.toolhead_entry_to_extruder = round(tete, 1)
#
#                elif dirty:
#                    self.log_always("Measuring dirty toolhead dimensions (with filament residue)...")
#                    tetn, tstn, tete = self._probe_toolhead()
#                    # Ooze reduction is the difference between empty and dirty measurements for sensor to nozzle
#                    tor = self.toolhead_sensor_to_nozzle - tstn
#                    msg = line
#                    msg += "Calibration Results (dirty nozzle):\n"
#                    msg += "> toolhead_residual_filament: %.1f (currently: %.1f)\n" % (tor, self.toolhead_residual_filament)
#                    if self.sensor_manager.has_sensor(self.SENSOR_EXTRUDER_ENTRY):
#                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, self.toolhead_entry_to_extruder)
#                    msg += line
#                    self.log_always(msg)
#                    if save:
#                        self.log_always("New calibrated ooze reduction active until restart. Update mmu_parameters.cfg to persist")
#                        self.toolhead_residual_filament = round(tor, 1)
#                        self.toolhead_entry_to_extruder = round(tete, 1)
#
#                # Unload and park filament
#                _ = self._unload_bowden()
#                _,_ = self._unload_gate()
#        except MmuError as ee:
#            self.handle_mmu_error(str(ee))
#        finally:
#            self.calibrating = False

    # Start: Filament must be loaded in extruder
    cmd_MMU_CALIBRATE_PSENSOR_help = "Calibrate analog proprotional sync-feedback sensor"
    def cmd_MMU_CALIBRATE_PSENSOR(self, gcmd):
        self.log_to_file(gcmd.get_commandline())

        if not self.sensor_manager.has_sensor(self.SENSOR_PROPORTIONAL):
            raise gcmd.error("Proportional (analog sync-feedback) sensor not found\n" + usage)

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_loaded(): return

        SD_THRESHOLD = 0.02
        MAX_MOVE_MULTIPLIER = 1.8
        STEP_SIZE = 2.0
        MOVE_SPEED = 8.0

        move = gcmd.get_float('MOVE', self.sync_feedback_manager.sync_feedback_buffer_maxrange, minval=1, maxval=100)
        steps = math.ceil(move * MAX_MOVE_MULTIPLIER / STEP_SIZE)

        usage = (
            "Ensure your sensor is configured by setting sync_feedback_analog_pin in [mmu_sensors].\n"
            "The other settings (sync_feedback_analog_max_compression, sync_feedback_analog_max_tension "
            "and sync_feedback_analog_neutral_point) will be determined by this calibration."
        )

        if not self.sensor_manager.has_sensor(self.SENSOR_PROPORTIONAL):
            raise gcmd.error("Proportional (analog sync-feedback) sensor not found\n" + usage)

        def _avg_raw(n=10, dwell_s=0.1):
            """
            Sample sensor.get_status(0)['value_raw'] n times with dwell between reads
            and return moving average
            """
            sensor = self.sensor_manager.all_sensors.get(self.SENSOR_PROPORTIONAL)

            k = 0.1 # 1st order,low pass filter coefficient, 0.1 for 10 samples
            avg = sensor.get_status(0).get('value_raw', None)

            for _ in range(int(max(1, n-1))):
                self.movequeues_dwell(dwell_s)
                raw = sensor.get_status(0).get('value_raw', None)
                if raw is None or not isinstance(raw, float):
                    return None
                avg += k * (raw - avg) # 1st order low pass filter
            return (avg)

        def _seek_limit(msg, steps, step_size, prev_val, ramp, log_label):
            self.log_always(msg)
            for i in range(steps):
                _ = self.trace_filament_move(msg, step_size, motor="gear", speed=MOVE_SPEED, wait=True)
                val = _avg_raw()

                delta = val - prev_val

                if ramp is None:
                    if delta == 0:
                        self.log_always("No sensor change. Retrying")
                        continue
                    ramp = (delta > 0)

                if (ramp and val >= prev_val) or (not ramp and val <= prev_val):
                    prev_val = val
                    self.log_always("Seeking ... ADC %s limit: %.4f" % (log_label, val))
                else:
                    # Limit found
                    return prev_val, ramp, True

            # Ran out of steps without detecting a clear limit
            return prev_val, ramp, False
        try:
            with self.wrap_sync_gear_to_extruder():
                with self.wrap_gear_current(percent=self.sync_gear_current, reason="while calibrating sync_feedback psensor"):
                    self.selector.filament_drive()
                    self.calibrating = True

                    raw0 = _avg_raw()
                    if raw0 is None:
                        raise gcmd.error("Sensor malfunction. Could not read valid ADC output\nAre you sure you configured in [mmu_sensors]?")

                    msg = "Finding compression limit stepping up to %.2fmm\n" % (steps * STEP_SIZE)
                    c_prev = raw0
                    ramp = None
                    c_prev, ramp, found_c_limit = _seek_limit(msg, steps, STEP_SIZE, c_prev, ramp, "compressed")

                    # Back off compressed extreme
                    msg = "Backing off compressed limit"
                    self.log_always(msg)
                    _ = self.trace_filament_move(msg, -(steps * STEP_SIZE / 2.0), motor="gear", speed=MOVE_SPEED, wait=True)

                    msg = "Finding tension limit stepping up to %.2fmm\n" % (steps * STEP_SIZE)
                    t_prev = _avg_raw()
                    ramp = (not ramp) if found_c_limit else None # If compression succeeded, inverse ramp; otherwise re-detect
                    t_prev, ramp, found_t_limit = _seek_limit(msg, steps, -STEP_SIZE, t_prev, ramp, "tension")

                    # Back off tension extreme
                    msg = "Backing off tension limit"
                    self.log_always(msg)
                    _ = self.trace_filament_move(msg, (steps * STEP_SIZE / 2.0), motor="gear", speed=MOVE_SPEED, wait=True)

            if (found_c_limit and found_t_limit):
                msg =  "Calibration Results:\n"
                msg += "As wired, recommended settings (in mmu_hardware.cfg) are:\n"
                msg += "[mmu_sensors]\n"
                msg += "sync_feedback_analog_max_compression: %.4f\n" % c_prev
                msg += "sync_feedback_analog_max_tension:     %.4f\n" % t_prev
                msg += "sync_feedback_analog_neutral_point:   %.4f\n" % ((c_prev + t_prev) / 2.0)
                msg += "After updating, don't forget to restart klipper!"
                self.log_always(msg)
            else:
                msg = "Warning: calibration did not find both compression and tension "
                msg += "limits (compression=%s, tension=%s)\n" % (found_c_limit, found_t_limit)
                msg += "Perhaps sync_feedback_buffer_maxrange parameter is incorrect?\n"
                msg += "Alternatively with bigger movement range by running with MOVE="
                self.log_warning(msg)

        except MmuError as ee:
            self.handle_mmu_error(str(ee))
        finally:
            self.calibrating = False

