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
from ..mmu_constants import *
from ..mmu_utils     import MmuError


class MmuCalibrator:

    def __init__(self, config, mmu_unit, params):
        logging.info("PAUL: ==== init() for MmuCalibrator")
        self.config = config
        self.mmu_unit = mmu_unit                # This physical MMU unit
        self.mmu_machine = mmu_unit.mmu_machine # Entire Logical combined MMU
        self.p = params                         # mmu_unit_parameters
        self.printer = config.get_printer()

        self.calibration_status = 0b0

        # Event handlers
        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler('klippy:ready',   self.handle_ready)


    def handle_connect(self):
        logging.info("PAUL: ==== handle_connect: MmuCalibrator for %s" % self.mmu_unit.name)
        self.mmu = self.mmu_machine.mmu_controller      # Shared MMU controller class
        self.var_manager = self.mmu_machine.var_manager # Quick access to save variables manager


    def handle_ready(self):
        logging.info("PAUL: ==== handle_ready: MmuCalibrator for %s" % self.mmu_unit.name)

        u = self.mmu_unit

        def ensure_list_size(lst, size, default_value=-1):
            lst = lst[:size]
            lst.extend([default_value] * (size - len(lst)))
            return lst

        # Load bowden length configuration (calibration set with MMU_CALIBRATE_BOWDEN) --------------------------

        bowden_lengths = self.var_manager.get(VARS_MMU_BOWDEN_LENGTHS, None, namespace=u.name)
        bowden_home = self.var_manager.get(VARS_MMU_BOWDEN_HOME, u.p.gate_homing_endstop, namespace=u.name)
        if u.require_bowden_move:
            if bowden_lengths and bowden_home in GATE_ENDSTOPS:
                bowden_lengths = [-1 if x < 0 else x for x in bowden_lengths] # Ensure -1 value for uncalibrated
                # Ensure list size
                if len(bowden_lengths) == u.num_gates:
                    self.mmu.log_debug("Loaded saved bowden lengths for %s: %s" % (u.name, bowden_lengths))
                else:
                    var = self.var_manager.namespace(VARS_MMU_BOWDEN_LENGTHS, namespace=u.name)
                    self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length to %d gates" % (var, u.num_gates))
                    bowden_lengths = ensure_list_size(bowden_lengths, u.num_gates)

                # Ensure values are identical (just for optics) if variable_bowden_lengths is False
                if not u.variable_bowden_lengths:
                    bowden_lengths = [bowden_lengths[0]] * u.num_gates

                if not any(x == -1 for x in bowden_lengths):
                    self.mark_calibrated(CALIBRATED_BOWDENS)
            else:
                self.mmu.log_warning("Warning: Bowden lengths for %s not found in mmu_vars.cfg. Probably not calibrated yet" % u.name)
                bowden_lengths = [-1] * u.num_gates
        else:
            bowden_lengths = [0] * u.num_gates
            self.mark_calibrated(CALIBRATED_BOWDENS)

        self.bowden_lengths = bowden_lengths

        # Ensure the gate endstop is what was calibrated against. If not adjust
        if self.check_calibrated(CALIBRATED_BOWDENS):
            self.adjust_bowden_lengths_on_homing_change()

        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, bowden_lengths, namespace=u.name)


        # Load gear rotation distance configuration (calibration set with MMU_CALIBRATE_GEAR/GATE) -------------------

        gear_steppers = u.mmu_toolhead.get_kinematics().rails[1].steppers

        rds = (
            [s.get_rotation_distance()[0] for s in gear_steppers[:u.num_gates]]
            if len(gear_steppers) >= u.num_gates
            else [gear_steppers[0].get_rotation_distance()[0]] * u.num_gates
        )
        self.default_rotation_distances = rds

        rotation_distances = self.var_manager.get(VARS_MMU_GEAR_ROTATION_DISTANCES, None, namespace=u.name)
        if rotation_distances:
            rotation_distances = [-1 if x == 0 else x for x in rotation_distances] # Ensure -1 value for uncalibrated
            # Ensure list size
            if len(rotation_distances) == u.num_gates:
                self.mmu.log_debug("Loaded saved gear rotation distances for unit %s: %s" % (u.name, rotation_distances))
            else:
                self.mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.var_manager.namespace(VARS_MMU_GEAR_ROTATION_DISTANCES, namespace=u.name))
                rotation_distances = ensure_list_size(rotation_distances, u.num_gates)

            # Ensure values are identical (just for optics) if variable_rotation_distances is False
            if not u.variable_rotation_distances:
                rotation_distances = [rotation_distances[0]] * u.num_gates

            if rotation_distances[0] != -1:
                self.mark_calibrated(CALIBRATED_GEAR_0)
            if not any(x == -1 for x in rotation_distances):
                self.mark_calibrated(CALIBRATED_GEAR_RDS)
        else:
            self.mmu.log_warning("Warning: Gear rotation distances for unit %s not found in mmu_vars.cfg. Probably not calibrated yet" % u.name)
            rotation_distances = [-1] * u.num_gates

        self.var_manager.set(VARS_MMU_GEAR_ROTATION_DISTANCES, rotation_distances, namespace=u.name)
        self.rotation_distances = rotation_distances

        self.var_manager.write() # Save any updates immediately

        self.mmu.log_warning("PAUL: self.bowden_lengths=%s" % self.bowden_lengths)
        self.mmu.log_warning("PAUL: self.default_rotation_distances=%s" % self.default_rotation_distances)
        self.mmu.log_warning("PAUL: self.rotation_distances=%s" % self.rotation_distances)


    def mark_calibrated(self, step):
        self.calibration_status |= step


    def mark_not_calibrated(self, step):
        self.calibration_status &= ~step


    def check_calibrated(self, step):
        return self.calibration_status & step == step



    # -------------------- Bowden length manipulation --------------------
    # Notes:
    #  - The bowden length is the distance between the current choice of endstops.
    #    If those endstops change the bowden length must be adjusted
    #  - A calibrated bowden length must also be updated if the rotation_distance for
    #    that gate is updated
    #  - Testing has shown that the encoder based clog detection length is generally
    #    proportional to the bowden length

    # Returns the currently calibrated bowden length or the default for gate 0 if not calibrated
    def get_bowden_length(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        ref_gate = lgate if lgate >= 0 and self.mmu_unit.variable_bowden_lengths else 0
        return self.bowden_lengths[ref_gate]


    # Update bowden calibration for current gate and clog_detection if not yet calibrated
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def update_bowden_length(self, length, gate=None, console_msg=False):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate < 0:
            self.mmu.log_debug("Assertion failure: cannot save bowden length for gate: %s" % self.mmu.selected_gate_string(gate))
            return

        all_gates = not self.mmu_unit.variable_bowden_lengths

        if length < 0: # Reset
            action = "reset"
            if all_gates:
                self.bowden_lengths = [-1] * self.mmu_unit.num_gates
            else:
                self.bowden_lengths[lgate] = -1

        else:
            length = round(length, 1)
            action = "saved"
            if all_gates:
                self.bowden_lengths = [length] * self.mmu_unit.num_gates
            else:
                self.bowden_lengths[lgate] = length

        msg = "Calibrated bowden length %.1fmm has been %s %s" % (length, action, ("for all gates" if all_gates else "gate %d" % gate))
        if console_msg:
            self.mmu.log_always(msg)
        else:
            self.mmu.log_debug(msg)

        # Update calibration status
        if not any(x == -1 for x in self.bowden_lengths):
            self.calibration_status |= CALIBRATED_BOWDENS

        # Persist
        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, self.bowden_lengths, namespace=self.mmu_unit.name)
        self.var_manager.write()


    # Adjust all bowden lengths if endstop is changed (e.g. from MMU_TEST_CONFIG)
    def adjust_bowden_lengths_on_homing_change(self):
        current_home = self.var_manager.get(VARS_MMU_BOWDEN_HOME, None, namespace=self.mmu_unit.name)
        if self.mmu_unit.p.gate_homing_endstop == current_home:
            return

        adjustment = 0
        if current_home == SENSOR_ENCODER:
            adjustment = self.mmu_unit.p.gate_endstop_to_encoder
        elif self.mmu_unit.p.gate_homing_endstop == SENSOR_ENCODER:
            adjustment = -self.mmu_unit.p.gate_endstop_to_encoder
        self.bowden_lengths = [length + adjustment if length != -1 else length for length in self.bowden_lengths]
        self.mmu.log_debug("Adjusted bowden lengths by %.1f: %s because of gate_homing_endstop change" % (adjustment, self.bowden_lengths))

        # Persist
        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, self.bowden_lengths, namespace=self.mmu_unit.name)
        self.var_manager.set(VARS_MMU_BOWDEN_HOME, self.mmu_unit.p.gate_homing_endstop, namespace=self.mmu_unit.name)
        self.var_manager.write()


    def is_bowden_length_calibrated(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate >= 0:
            return self.bowden_lengths[lgate] >= 0
        return True


    # -------------------- Encoder based runout/clog/tangle length manipulation --------------------

    def get_clog_detection_length(self):
        return self.var_manager.get(VARS_MMU_ENCODER_CLOG_LENGTH, None, namespace=self.mmu_unit.encoder.name)


    def calc_clog_detection_length(self, bowden_length):
        cal_min = round((bowden_length * 2) / 100., 1) # 2% of bowden length seems to be good starting point
        return max(cal_min, 8.)                        # Never less than 8mm


    def update_clog_detection_length(self, cdl, push=False):
        """
        Persist the calibrated encoder clog detection length and notify the encoder of change if in auto mode
        If not forced then save if auto but don't update the encoder
        """
        if not self.mmu_unit.has_encoder(): return
        if not cdl: return

        self.var_manager.set(VARS_MMU_ENCODER_CLOG_LENGTH, cdl, namespace=self.mmu_unit.encoder.name, write=push)
        if push:
            self.mmu_unit.encoder.set_clog_detection_length(cdl)

# PAUL not needed
#        auto = (self.mmu.sync_feedback_manager.mmu_unit.p.flowguard_encoder_mode == RUNOUT_AUTOMATIC)
#
#        if auto or force:
#            self.var_manager.set(VARS_MMU_ENCODER_CLOG_LENGTH, cdl, namespace=self.mmu_unit.encoder.name, write=bool(force))
#
#        if auto and not force:
#            self.mmu_unit.encoder.set_clog_detection_length(cdl)



    # -------------------- Gear stepper rotation distance manipulation --------------------
    # Notes:
    #  - If the rotation distance is changed for gate with calibrated bowden length then adjust bowden length

    # Return current calibrated gear rotation_distance or sensible default
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def get_gear_rd(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate < 0:
            rd = self.default_rotation_distances[0]
        else:
            rd = self.rotation_distances[lgate if lgate >= 0 and self.mmu_unit.variable_rotation_distances else 0]

        if rd <= 0:
            rd = self.default_rotation_distances[lgate]
            self.mmu.log_debug("Gate %d not calibrated, falling back to default rotation_distance: %.4f" % (gate, rd))

        return rd


    # Set the active gear stepper rotation distance
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def set_gear_rd(self, rd, gate=None):
        logging.info("PAUL: set_gear_rd(gate=%s)" % gate)
        if gate == None: gate = self.mmu.gate_selected
        logging.info("PAUL: set_gear_rd now gate=%d" % gate)
        lgate = self.mmu_unit.local_gate(gate)

        if rd and lgate >= 0:
            self.mmu.log_trace("Setting gate %d gear motor rotation distance: %.4f" % (gate, rd))
            self.mmu_unit.gear_stepper_obj(gate).set_rotation_distance(rd)
# PAUL replaced this with line above. Modify elsewhere in file if it works
#            if self.gear_rail.steppers:
#                self.gear_rail.steppers[0].set_rotation_distance(rd)


    # Save rotation_distance for gate (and associated gates) adjusting any calibrated bowden length
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def update_gear_rd(self, rd, gate=None, console_msg=False):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if gate < 0:
            self.mmu.log_debug("Assertion failure: cannot save gear rotation_distance for gate: %d" % gate)
            return

        # Initial calibration on gate 0 also sets all gates as auto calibration starting point
        all_gates = (
            not self.mmu.mmu_unit().variable_rotation_distances
            or (gate == 0 and self.rotation_distances[0] == 0.)
        )

        if rd < 0:
            if all_gates:
                self.rotation_distances = [-1] * self.mmu_unit.num_gates
            else:
                self.rotation_distances[gate] = -1

            self.mmu.log_always("Gear rotation distance calibration has been reset for %s" % ("all gates" if all_gates else "gate %d" % gate))

        else:
            prev_rd = self.get_gear_rd(gate)
            rd = round(rd, 4)

            if all_gates:
                self.rotation_distances = [rd] * self.mmu_unit.num_gates
                updated_gates = list(range(self.mmu_unit.num_gates))
            else:
                self.rotation_distances[lgate] = rd
                updated_gates = [lgate]

            # Adjust calibrated bowden lengths
            for g in updated_gates if self.mmu_unit.variable_bowden_lengths else [lgate]:
                prev_bowden = self.bowden_lengths[g] # Must get raw value
                if prev_bowden > 0: # Is calibrated
                    new_bl = prev_bowden * (prev_rd / rd) # Adjust for same effective calibrated distance
                    self.update_bowden_length(new_bl, g)

            msg = "Rotation distance calibration (%.4f) has been saved for %s" % (rd, ("all gates" if all_gates else "gate %d" % gate))
            if console_msg:
                self.mmu.log_always(msg)
            else:
                self.mmu.log_debug(msg)

        # Update calibration status
        if self.rotation_distances[0] != -1:
            self.calibration_status |= CALIBRATED_GEAR_0
        if not any(x == -1 for x in self.rotation_distances):
            self.calibration_status |= CALIBRATED_GEAR_RDS

        # Persist
        self.var_manager.set(VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, namespace=self.mmu_unit.name, write=True)


    def is_gear_rd_calibrated(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate >= 0:
            return self.rotation_distances[lgate] >= 0
        return True


    #
    # Calibration implementations...
    #

    # Bowden calibration - Method 1
    # This method of bowden calibration is done in reverse and is a fallback. The user inserts filament to the
    # actual extruder and we measure the distance necessary to home to the defined gate homing position
    def calibrate_bowden_length_manual(self, approx_bowden_length):
        try:
            self.mmu.log_always("Calibrating bowden length on gate %d (manual method) using %s as gate reference point" % (self.mmu.gate_selected, self.mmu._gate_homing_string()))
            self.mmu._set_filament_direction(DIRECTION_UNLOAD)
            self.mmu.selector.filament_drive()
            self.mmu.log_always("Finding %s endstop position..." % self.mmu_unit.p.gate_homing_endstop)
            homed = False

            if self.mmu_unit.p.gate_homing_endstop == SENSOR_ENCODER:
                with self.mmu._require_encoder():
                    success = self.mmu._reverse_home_to_encoder(approx_bowden_length)
                    if success:
                        actual,_,_ = success
                        homed = True

            else: # Gate sensor... SENSOR_SHARED_EXIT is shared, but SENSOR_EXIT_PREFIX is specific
                actual, homed, measured, _ = self.mmu.trace_filament_move(
                    "Reverse homing off gate sensor",
                    -approx_bowden_length,
                    motor="gear",
                    homing_move=-1,
                    endstop_name=self.mmu_unit.p.gate_homing_endstop,
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
                    self.mmu_unit.p.extruder_homing_endstop
                )
            )
            self.mmu._initialize_filament_position(dwell=True)
            overshoot = self.mmu._load_gate(allow_retry=False)

            if self.mmu_unit.p.extruder_homing_endstop in [SENSOR_EXTRUDER_ENTRY, SENSOR_COMPRESSION]:
                if self.mmu.sensor_manager.check_sensor(self.mmu_unit.p.extruder_homing_endstop):
                    raise MmuError("The %s sensor triggered before homing. Check filament and sensor operation" % self.mmu_unit.p.extruder_homing_endstop)

            actual, extra = self.mmu._home_to_extruder(extruder_homing_max) # PAUL check this
            measured = self.mmu.get_encoder_distance(dwell=True) + self.mmu._get_encoder_dead_space()
            calibrated_length = round(overshoot + actual + extra, 1)

            msg = "Filament homed to extruder after %.1fmm movement" % actual
            if self.mmu.has_encoder():
                msg += "\n(encoder measured %.1fmm)" % (measured - self.mmu_unit.p.gate_parking_distance)
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
        orig_endstop = self.mmu_unit.p.extruder_homing_endstop
        try:
            # Can't allow "none" endstop during calibration so temporarily change it
            self.mmu_unit.p.extruder_homing_endstop = SENSOR_EXTRUDER_COLLISION

            self.mmu.log_always("Calibrating bowden length on gate %d using %s as gate reference point and encoder collision detection" % (self.mmu.gate_selected, self.mmu._gate_homing_string()))
            reference_sum = spring_max = 0.
            successes = 0

            for i in range(repeats):
                self.mmu._initialize_filament_position(dwell=True)
                overshoot = self.mmu._load_gate(allow_retry=False)
                self.mmu._load_bowden(approximate_length, start_pos=overshoot) # Get close to extruder homing point

                self.mmu.log_info("Finding extruder gear position (try #%d of %d)..." % (i+1, repeats))
                _,_ = self.mmu._home_to_extruder(extruder_homing_max)
                actual = self.mmu._get_filament_position() - self.mmu_unit.p.gate_parking_distance
                measured = self.mmu.get_encoder_distance(dwell=True) + self.mmu._get_encoder_dead_space()
                spring = self.mmu.selector.filament_release(measure=True) if self.mmu.has_encoder() else 0.
                reference = actual - spring

                # When homing using collision, we expect the filament to spring back.
                if spring != 0:
                    msg = "Pass #%d: Filament homed to extruder after %.1fmm movement" % (i+1, actual)
                    if self.mmu.has_encoder():
                        msg += "\n(encoder measured %.1fmm, filament sprung back %.1fmm)" % (measured - self.mmu_unit.p.gate_parking_distance, spring)
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
            self.mmu_unit.p.extruder_homing_endstop = orig_endstop


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
            old_result = mean * self.mmu_unit.encoder.get_resolution()

            msg = "Load direction:   mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self.mmu._sample_stats(pos_values)
            msg += "\nUnload direction: mean=%(mean).2f stdev=%(stdev).2f min=%(min)d max=%(max)d range=%(range)d" % self.mmu._sample_stats(neg_values)
            self.mmu.log_always(msg)

            # Sanity check to ensure all teeth are reflecting / being counted. 20% tolerance
            if (abs(resolution - self.mmu_unit.encoder.get_resolution()) / self.mmu_unit.encoder.get_resolution()) > 0.2:
                self.mmu.log_warning("Warning: Encoder is not detecting the expected number of counts based on CAD parameters which may indicate an issue")

            msg = "Before calibration measured length: %.2fmm" % old_result
            msg += "\nCalculated resolution of the encoder: %.4f (currently: %.4f)" % (resolution, self.mmu_unit.encoder.get_resolution())
            self.mmu.log_always(msg)

            if save:
                self.mmu_unit.encoder.set_resolution(resolution)
                self.var_manager.set(VARS_MMU_ENCODER_RESOLUTION, round(resolution, 4), namespace=self.mmu_unit.encoder.name, write=True)
                self.mmu.log_always("Encoder calibration has been saved")
                self.calibration_status |= CALIBRATED_ENCODER

        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration of encoder failed. Aborting, because:\n%s" % str(ee))

        finally:
            if mean == 0:
                self.mmu._set_filament_pos_state(FILAMENT_POS_UNKNOWN)


    # Automatically calibrate the rotation_distance for gate>0 using encoder measurements and gate 0 as reference
    # Gate 0 is always calibrated with MMU_CALILBRATE_GEAR
    def calibrate_gate(self, gate, length, repeats, save=True):
        lgate = self.mmu_unit.local_gate(gate)

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
            current_rd = self.mmu_unit.gear_stepper_obj(gate).get_rotation_distance()[0]
            new_rd = round(ratio * current_rd, 4)

            self.mmu.log_always("Calibration move of %d x %.1fmm, average encoder measurement: %.1fmm - Ratio is %.4f" % (repeats * 2, length, mean, ratio))
            self.mmu.log_always("Calculated gate %d rotation_distance: %.4f (currently: %.4f)" % (gate, new_rd, self.rotation_distances[gate]))
            if gate != 0: # Gate 0 is not calibrated, it is the reference and set with MMU_CALIBRATE_GEAR
                gate0_rd = self.rotation_distances[0]
                tolerance_range = (gate0_rd - gate0_rd * 0.2, gate0_rd + gate0_rd * 0.2) # Allow 20% variation from gate 0
                if tolerance_range[0] <= new_rd < tolerance_range[1]:
                    if save:
                        self.set_gear_rd(new_rd)
                        self.update_gear_rd(new_rd, console_msg=True)
                else:
                    self.mmu.log_always("Calibration ignored because it is not considered valid (>20% difference from gate 0)")
            self.mmu._unload_gate()
            self.mmu._set_filament_pos_state(FILAMENT_POS_UNLOADED)
        except MmuError as ee:
            # Add some more context to the error and re-raise
            raise MmuError("Calibration for gate %d failed. Aborting, because:\n%s" % (gate, str(ee)))



    # ------------------ Autotuning from telemetry data -------------------

    def note_load_telemetry(self, bowden_length, bowden_move_ratio, bowden_travel):
        self.mmu.log_error(f"note_load_telemetry(bowden_length={bowden_length}, bowden_move_ratio={bowden_move_ratio}, bowden_travel={bowden_travel})")
        return

# PAUL TODO...
#        homing_delta = None
#        if homing_movement is not None:
#            homing_delta = homing_movement - expected_homing
#PAUL            homing_movement -= deficit
#        self._autotune(DIRECTION_LOAD, bowden_move_ratio, homing_delta) # PAUL check autotune


    def note_unload_telemetry(self, bowden_length, bowden_move_ratio, bowden_travel):
        self.mmu.log_error(f"note_unload_telemetry(bowden_length={bowden_length}, bowden_move_ratio={bowden_move_ratio}, bowden_travel={bowden_travel})")
        return

# PAUL TODO...
#        homing_delta = None
#        if homing_movement is not None:
#            homing_delta = homing_movement - expected_homing
#PAUL            homing_movement -= deficit
#        self._autotune(DIRECTION_UNLOAD, bowden_move_ratio, homing_delta) # PAUL check autotune


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
        lgate = self.mmu_unit.local_gate(self.mmu.gate_selected)

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
                self.mmu_unit.p.autotune_rotation_distance and
                self.mmu_unit.variable_rotation_distances and
                self.mmu.gate_selected > 0 and
                bowden_move_ratio > 0 and
                homing_movement > 0
            ):
                if direction in [DIRECTION_LOAD, DIRECTION_UNLOAD]:
                    current_rd = self.mmu_unit.gear_stepper_obj(gate).get_rotation_distance()[0]
                    new_rd = round(bowden_move_ratio * current_rd, 4)
                    gate0_rd = self.rotation_distances[0]

                    # Allow max 10% variation from gate 0 for autotune
                    if math.isclose(new_rd, gate0_rd, rel_tol=0.1):
                        if not self.mmu.calibrating and self.rotation_distances[self.mmu.gate_selected] > 0:
                            # Tuning existing calibration
                            new_rd = round((self.rotation_distances[self.mmu.gate_selected] * 5 + new_rd) / 6, 4) # Moving average
                            msg += ". Autotuned rotation_distance: %.4f for gate %d" % (new_rd, self.mmu.gate_selected)
                        if not math.isclose(current_rd, new_rd):
                            _ = self.mmu.update_gear_rd(new_rd, self.mmu.gate_selected)
                    else:
                        msg += ". Calculated rotation_distance: %.4f for gate %d failed sanity check and has been ignored" % (new_rd, self.mmu.gate_selected)


            # Automatic calibration of bowden length based on actual homing movement telemetry
            # TODO Currently only works with gate 0. Could work with other gates if variable_bowden_lengths is True and rotation distance is calibrated
            if (
                self.mmu_unit.p.autotune_bowden_length and
                self.mmu_unit.require_bowden_move and
                self.mmu.gate_selected == 0 and
                (
                    0.9 < bowden_move_ratio < 1.1 or
                    not self.mmu.has_encoder()
                )
            ):
                if direction in [DIRECTION_LOAD, DIRECTION_UNLOAD]:
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


    # ------------------- Toolhead calibration helpers --------------------

    def _probe_toolhead(self, cold_temp=70, probe_depth=100, sensor_homing=80):
        """
        Helper for MMU_CALIBRATATE_TOOLHEAD that probes toolhead to measure three key dimensions:

          toolhead_extruder_to_nozzle
          toolhead_sensor_to_nozzle
          toolhead_entry_to_extruder

        Filament is assumed to be at the extruder and will be at extruder again when complete
        """
        selector = self.mmu_unit.selector
        extruder_name = self.mmu_unit.extruder_name

        # Ensure extruder is COLD
        self.mmu.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=0" % extruder_name)
        current_temp = self.printer.lookup_object(extruder_name).get_status(0)['temperature']
        if current_temp > cold_temp:
            self.mmu.log_always("Waiting for extruder to cool")
            self.mmu.gcode.run_script_from_command("TEMPERATURE_WAIT SENSOR=%s MINIMUM=0 MAXIMUM=%d" % (extruder_name, cold_temp))

        # Enable the extruder stepper
        stepper_enable = self.printer.lookup_object('stepper_enable')
        extruder_stepper = self.mmu_unit.extruder_stepper_obj().stepper
        ge = stepper_enable.lookup_enable(extruder_stepper.get_name())
        ge.motor_enable(self.mmu.toolhead.get_last_move_time())

        # Reliably force filament to the nozzle
        selector.filament_drive()
        actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to toolhead sensor", self.mmu_unit.p.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
        if not fhomed:
            raise MmuError("Failed to reach toolhead sensor after moving %.1fmm" % self.mmu_unit.p.toolhead_homing_max)
        selector.filament_release()
        actual,_,_,_ = self.mmu.trace_filament_move("Forcing filament to nozzle", probe_depth, motor="extruder")

        # Measure 'toolhead_sensor_to_nozzle'
        selector.filament_drive()
        actual,fhomed,_,_ = self.mmu.trace_filament_move("Reverse homing off toolhead sensor", -probe_depth, motor="gear+extruder", homing_move=-1, endstop_name=SENSOR_TOOLHEAD)
        if fhomed:
            toolhead_sensor_to_nozzle = -actual
            self.mmu.log_always("Measured toolhead_sensor_to_nozzle: %.1f" % toolhead_sensor_to_nozzle)
        else:
            raise MmuError("Failed to reverse home to toolhead sensor")

        # Move to extruder extrance again
        selector.filament_release()
        actual,_,_,_ = self.mmu.trace_filament_move("Moving to extruder entrance", -(probe_depth - toolhead_sensor_to_nozzle), motor="extruder")

        # Measure 'toolhead_extruder_to_nozzle'
        selector.filament_drive()
        actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to toolhead sensor", self.mmu_unit.p.toolhead_homing_max, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
        if fhomed:
            toolhead_extruder_to_nozzle = actual + toolhead_sensor_to_nozzle
            self.mmu.log_always("Measured toolhead_extruder_to_nozzle: %.1f" % toolhead_extruder_to_nozzle)
        else:
            raise MmuError("Failed to home to toolhead sensor")

        toolhead_entry_to_extruder = 0.
        if self.mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
            # Retract clear of extruder sensor and then home in "extrude" direction
            actual,fhomed,_,_ = self.mmu.trace_filament_move("Reverse homing off extruder entry sensor", -(sensor_homing + toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle), motor="gear+extruder", homing_move=-1, endstop_name=SENSOR_EXTRUDER_ENTRY)
            actual,_,_,_ = self.mmu.trace_filament_move("Moving before extruder entry sensor", -20, motor="gear+extruder")
            actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to extruder entry sensor", 40, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_EXTRUDER_ENTRY)

            # Measure to toolhead sensor and thus derive 'toolhead_entry_to_extruder'
            if fhomed:
                actual,fhomed,_,_ = self.mmu.trace_filament_move("Homing to toolhead sensor", sensor_homing, motor="gear+extruder", homing_move=1, endstop_name=SENSOR_TOOLHEAD)
                if fhomed:
                    toolhead_entry_to_extruder = actual - (toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle)
                    self.mmu.log_always("Measured toolhead_entry_to_extruder: %.1f" % toolhead_entry_to_extruder)
            else:
                raise MmuError("Failed to reverse home to toolhead sensor")

        # Unload and re-park filament
        selector.filament_release()
        actual,_,_,_ = self.mmu.trace_filament_move("Moving to extruder entrance", -sensor_homing, motor="extruder")

        return toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle, toolhead_entry_to_extruder


# -----------------------------------------------------------------------------------------------------------
# Static method for easy access from mmu controller, commands and various other unit components
# -----------------------------------------------------------------------------------------------------------

    @staticmethod
    def check_if_not_calibrated(mmu, required, silent=False, check_gates=None, use_autotune=True):
        calibrated = True

        if check_gates is None:
            check_gates = list(range(mmu.num_gates))

        # What mmu_units are involved with check_gates? (retaining logical order)
        mmu_units = list(dict.fromkeys(mmu.mmu_unit(g) for g in check_gates))

        # Iterate over mmu_units with separate calibration message for each
        for u in mmu_units:
            if not u.calibrator.check_calibrated(required):
                rmsg = omsg = ""

                if (
                    (not use_autotune or not u.p.autocal_selector) and
                    (required & CALIBRATED_SELECTOR) and
                    not u.calibrator.check_calibrated(CALIBRATED_SELECTOR)
                ):
                    unit_check_gates = [g for g in check_gates if u.manages_gate(g)]
                    uncalibrated = u.selector.get_uncalibrated_gates(unit_check_gates)
                    if uncalibrated:
                        info = "\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: %s" % ",".join(map(str, uncalibrated))
                        if u.p.autocal_selector:
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not u.p.skip_cal_rotation_distance) and
                    (required & CALIBRATED_GEAR_0) and
                    not u.calibrator.check_calibrated(CALIBRATED_GEAR_0)
                ):
                    uncalibrated = not u.calibrator.is_gear_rd_calibrated(u.first_gate)
                    if uncalibrated:
                        info = "\n- Use MMU_CALIBRATE_GEAR (on first gate)"
                        info += " to calibrate gear rotation_distance for first gate of unit"
                        if u.p.skip_cal_rotation_distance:
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not u.p.skip_cal_encoder) and
                    (required & CALIBRATED_ENCODER) and
                    not u.calibrator.check_calibrated(CALIBRATED_ENCODER)
                ):
                    info = "\n- Use MMU_CALIBRATE_ENCODER (with first gate of unit selected)"
                    if u.p.skip_cal_encoder:
                        omsg += info
                    else:
                        rmsg += info

                if (
                    u.variable_rotation_distances and
                    (not use_autotune or not (u.p.skip_cal_rotation_distance or u.p.autotune_rotation_distance)) and
                    (required & CALIBRATED_GEAR_RDS) and
                    not u.calibrator.check_calibrated(CALIBRATED_GEAR_RDS)
                ):
                    uncalibrated = [
                        g
                        for g in range(u.first_gate + 1, u.first_gate + u.num_gates)
                        if not u.calibrator.is_gear_rd_calibrated(g) and g in check_gates
                    ]
                    if uncalibrated:
                        if u.encoder:
                            info = "\n- Use MMU_CALIBRATE_GEAR (with appropriate gate selected) or MMU_CALIBRATE_GATES GATE=xx"
                            info += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                        else:
                            info = "\n- Use MMU_CALIBRATE_GEAR (with appropriate gate selected)"
                            info += " to calibrate gear rotation_distance on gates: %s" % ",".join(map(str, uncalibrated))
                        if (u.p.skip_cal_rotation_distance or u.p.autotune_rotation_distance):
                            omsg += info
                        else:
                            rmsg += info

                if (
                    (not use_autotune or not u.p.autocal_bowden_length) and
                    (required & CALIBRATED_BOWDENS) and
                    not u.calibrator.check_calibrated(CALIBRATED_BOWDENS)
                ):
                    if u.variable_bowden_lengths:
                        uncalibrated = [
                            g
                            for g in range(u.first_gate + 1, u.first_gate + u.num_gates)
                            if not u.calibrator.is_bowden_length_calibrated(g) and g in check_gates
                        ]
                        if uncalibrated:
                            info = "\n- Use MMU_CALIBRATE_BOWDEN (with appropriate gate selected)"
                            info += " to calibrate bowden length gates: %s" % ",".join(map(str, uncalibrated))
                            if u.p.autocal_bowden_length:
                                omsg += info
                            else:
                                rmsg += info
                    else:
                        uncalibrated = not u.calibrator.is_bowden_length_calibrated(u.first_gate)
                        if uncalibrated:
                            info = "\n- Use MMU_CALIBRATE_BOWDEN (with first gate of unit selected) to calibrate bowden length"
                            if u.p.autocal_bowden_length:
                                omsg += info
                            else:
                                rmsg += info

                if rmsg or omsg:
                    msg = "Warning: Calibration steps are not complete for MMU %s:" % u.name
                    if rmsg:
                        msg += "\nRequired:%s" % rmsg
                    if omsg:
                        msg += "\nOptional (handled by autocal/autotune):%s" % omsg
                    if not silent:
                        if silent is None:  # Bootup/status use case to avoid looking like error
                            mmu.log_always("{2}%s{0}" % msg, color=True)
                        else:
                            mmu.log_error(msg)
                    calibrated = False

        return not calibrated


# -----------------------------------------------------------------------------------------------------------
# Calibration commands are defined here to keep close to helper logic
# -----------------------------------------------------------------------------------------------------------

from ..commands                  import register_command
from ..commands.mmu_base_command import *


# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_GEAR command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibrateGearCommand(BaseCommand):
    """
    Gear rotation distance calibration command.

    Note that because it operates on the current gate selected it is not a per-unit command
    """

    CMD = "MMU_CALIBRATE_GEAR"

    HELP_BRIEF = "Calibration routine for gear stepper rotational distance of selected gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "MEASURED = #(mm) Measured moved distance\n"
        + "LENGTH   = #(mm) Commanded distance (default: 100, min: 50)\n"
        + "SAVE     = [0|1] Save calculated rotation_distance (default: 1)\n"
        + "RESET    = [0|1] Reset rotation_distance to default for selected gate (default: 0)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_invalid_gate(): return

        mmu_unit = mmu.mmu_unit() # For gate selected
        calibrator = mmu_unit.calibrator

# PAUL add GATE parameter, defaulting to first_gate on unit. Set local lgate
        length = gcmd.get_float('LENGTH', 100., above=50.)
        measured = gcmd.get_float('MEASURED', -1, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        gate = mmu.gate_selected if mmu.gate_selected >= 0 else 0

        with mmu.wrap_sync_gear_to_extruder():
            if reset:
                calibrator.set_gear_rd(calibrator.default_rotation_distances[lgate]) # PAUL need to set gate
                calibrator.update_gear_rd(-1)
                return

            if measured > 0:
                current_rd = mmu_unit.gear_stepper_obj(self.mmu.gate_selected).get_rotation_distance()[0]
                new_rd = round(current_rd * measured / length, 4)
                mmu.log_always("MMU gear stepper 'rotation_distance' calculated to be %.4f (currently: %.4f)" % (new_rd, current_rd))
                if save:
                    calibrator.set_gear_rd(new_rd)
                    calibrator.update_gear_rd(new_rd, console_msg=True)
                return

            raise gcmd.error("Must specify 'MEASURED=' and optionally 'LENGTH='")



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_GATES command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibrateGatesCommand(BaseCommand):
    """
    Start: Will home selector, select gate 0 or required gate
    End: Filament will unload
    """

    CMD = "MMU_CALIBRATE_GATES"

    HELP_BRIEF = "Optional calibration of individual MMU gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT    = #(int) Optional if only one unit fitted to printer\n"
        + "LENGTH  = #(mm) Commanded distance (default: 400)\n"
        + "REPEATS = #(count) Number of repetitions (default: 3, min: 1, max: 10)\n"
        + "ALL     = [0|1] Calibrate all gates (default: 0)\n"
        + "GATE    = #(index) Gate index to calibrate (0 to num_gates-1)\n"
        + "SAVE    = [0|1] Save calibration (default: 1)\n"
        + "RESET   = [0|1] Reset gate rotation_distance (default: 0)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True,
        )

# PAUL this IS per-UNIT ..
    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        calibrator = mmu_unit.calibrator

        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', -1, minval=0, maxval=mmu.num_gates - 1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if gate == -1 and not all_gates:
            raise gcmd.error("Must specify 'GATE=' or 'ALL=1' for all gates")

        if reset:
            if all_gates:
                calibrator.set_gear_rd(calibrator.default_rotation_distance) # PAUL FIX ME ... "distances" per gate
                for lgate in range(mmu_unit.num_gates - 1):
                    calibrator.update_gear_rd(-1, lgate + 1)
            else:
                calibrator.update_gear_rd(-1, lgate)
            return

# PAUL fixme gate and lgate
        if self.check_if_not_calibrated(mmu,
            CALIBRATED_GEAR_0 | CALIBRATED_ENCODER | CALIBRATED_SELECTOR,
            check_gates=[gate] if gate != -1 else None
        ): return

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu._unload_tool()
                mmu.calibrating = True
                with mmu._require_encoder():
                    if all_gates:
                        mmu.log_always("Start the complete calibration of ancillary gates...")
                        for gate in range(mmu.num_gates - 1):
                            calibrator.calibrate_gate(gate + 1, length, repeats, save=save)
                        mmu.log_always("Phew! End of auto gate calibration")
                    else:
                        calibrator.calibrate_gate(gate, length, repeats, save=(save and gate != 0))
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_BOWDEN command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibrateBowdenCommand(BaseCommand):
    """
    Calibrated bowden length is always from chosen gate homing point to the entruder gears
    Start: With desired gate selected
    End: Filament will be unloaded

    Note that because it operates on the current gate selected it is not a per-unit command
    """

    CMD = "MMU_CALIBRATE_BOWDEN"

    HELP_BRIEF = "Calibration of reference bowden length for selected gate"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "REPEATS       = #(count) Number of repetitions (default: 3, min: 1, max: 10)\n"
        + "SAVE          = [0|1] Save calibration (default: 1)\n"
        + "MANUAL        = [0|1] Use manual calibration method (default: 0)\n"
        + "COLLISION     = [0|1] Force collision method (requires encoder) (default: 0)\n"
        + "RESET         = [0|1] Clear saved bowden length (default: 0)\n"
        + "HOMING_MAX    = #(mm) Extruder homing maximum (default: 150)\n"
        + "BOWDEN_LENGTH = #(mm) Approx bowden length used for calibration\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if self.check_if_disabled(): return
        if self.check_if_no_bowden_move(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_invalid_gate(): return

        mmu_unit = mmu.mmu_unit() # For gate selected
        calibrator = mmu_unit.calibrator

        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        manual = bool(gcmd.get_int('MANUAL', 0, minval=0, maxval=1))
        collision = bool(gcmd.get_int('COLLISION', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            calibrator.update_bowden_length(-1, console_msg=True)
            return

        if manual:
            if self.check_if_not_calibrated(mmu, CALIBRATED_GEAR_0 | CALIBRATED_SELECTOR, check_gates=[mmu.gate_selected]): return
        else:
            if self.check_if_not_calibrated(mmu, CALIBRATED_GEAR_0 | CALIBRATED_ENCODER | CALIBRATED_SELECTOR, check_gates=[mmu.gate_selected]): return

        can_use_sensor = (
            mmu_unit.p.extruder_homing_endstop in [
                SENSOR_EXTRUDER_ENTRY,
                SENSOR_COMPRESSION,
                SENSOR_GEAR_TOUCH
            ] and (
                mmu_unit.sensor_manager.has_sensor(mmu_unit.p.extruder_homing_endstop) or
                mmu_unit.gear_stepper_obj(mmu.gate_selected).is_endstop_virtual(mmu_unit.p.extruder_homing_endstop)
            )
        )
        can_auto_calibrate = mmu_unit.has_encoder() or can_use_sensor

        if not can_auto_calibrate and not manual:
            mmu.log_always("No encoder or extruder entry sensor available. Use manual calibration method:\nWith gate selected, manually load filament all the way to the extruder gear\nThen run 'MMU_CALIBRATE_BOWDEN MANUAL=1 BOWDEN_LENGTH=xxx'\nWhere BOWDEN_LENGTH is greater than your real length")
            return

        extruder_homing_max = gcmd.get_float('HOMING_MAX', 150, above=0.)
        approx_bowden_length = gcmd.get_float('BOWDEN_LENGTH', mmu_unit.p.bowden_homing_max if (manual or can_use_sensor) else None, above=0.)
        if not approx_bowden_length:
            raise gcmd.error("Must specify 'BOWDEN_LENGTH=x' where x is slightly LESS than your estimated bowden length to give room for homing")

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu._wrap_suspend_filament_monitoring():
                    mmu.calibrating = True
                    if manual:
                        # Method 1: Manual (reverse homing to gate) method
                        length = calibrator.calibrate_bowden_length_manual(approx_bowden_length)

                    elif can_use_sensor and not collision:
                        # Method 2: Automatic one-shot method with homing sensor (BEST)
                        mmu._unload_tool()
                        length = calibrator.calibrate_bowden_length_sensor(approx_bowden_length)

                    elif mmu_unit.has_encoder():
                        # Method 3: Automatic averaging method with encoder and extruder collision. Uses repeats for accuracy
                        mmu._unload_tool()
                        length = calibrator.calibrate_bowden_length_collision(approx_bowden_length, extruder_homing_max, repeats)

                    else:
                        raise gcmd.error("Invalid configuration or options provided. Perhaps you tried COLLISION=1 without encoder or don't have extruder_homing_endstop set?")

                    cdl = None
                    msg = "Calibrated bowden length is %.1fmm" % length
                    if mmu.has_encoder():
                        cdl = calibrator.calc_clog_detection_length(length)
                        msg += ". Recommended flowguard_encoder_max_motion (clog detection length): %.1fmm" % cdl
                    mmu.log_always(msg)

                    if save:
                        calibrator.update_bowden_length(length, console_msg=True)
                        if cdl is not None:
                            calibrator.update_clog_detection_length(length, push=True)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_TOOLHEAD command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibrateToolheadCommand(BaseCommand):
    """
    Start: Test gate should already be selected
    End: Filament will unload
    """

    CMD = "MMU_CALIBRATE_TOOLHEAD"

    HELP_BRIEF = "Automated measurement of key toolhead parameters"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT  = #(int) Optional if only one unit fitted to printer\n"
        + "CLEAN = [0|1] Measure clean nozzle dimensions (after cold pull)\n"
        + "DIRTY = [0|1] Measure residual filament (dirty nozzle)\n"
        + "CUT   = [0|1] Measure blade position (hold cutter closed)\n"
        + "SAVE  = [0|1] Persist results in active config (default: 1)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        calibrator = mmu_unit.calibrator

        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_not_calibrated(mmu,
            CALIBRATED_GEAR_0 | CALIBRATED_ENCODER | CALIBRATED_SELECTOR | CALIBRATED_BOWDENS,
            check_gates=[mmu.gate_selected]
        ): return
        if not mmu_unit.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
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
            mmu.log_always(msg)
            return

        if cut:
            gcode_macro = self.printer.lookup_object("gcode_macro %s" % mmu.p.form_tip_macro, None)
            if gcode_macro is None:
                raise gcmd.error("Filament tip forming macro '%s' not found" % mmu.p.form_tip_macro)
            gcode_vars = self.printer.lookup_object("gcode_macro %s_VARS" % mmu.p.form_tip_macro, gcode_macro)
            if not ('blade_pos' in gcode_vars.variables and 'retract_length' in gcode_vars.variables):
                raise gcmd.error("Filament tip forming macro '%s' does not look like a cutting macro!" % mmu.p.form_tip_macro)

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu.calibrating = True
                mmu._initialize_filament_position(dwell=True)
                overshoot = mmu._load_gate(allow_retry=False)
                _,_ = mmu._load_bowden(start_pos=overshoot)
                _,_ = mmu._home_to_extruder(mmu_unit.p.extruder_homing_max)

                if cut:
                    mmu.log_always("Measuring blade cutter postion (with filament fragment)...")
                    tetn, tstn, tete = calibrator._probe_toolhead()
                    # Blade position is the difference between empty and extruder with full cut measurements for sensor to nozzle
                    vbp = mmu.p.toolhead_sensor_to_nozzle - tstn
                    msg = line
                    if abs(vbp - mmu.p.toolhead_residual_filament) < 5:
                        mmu.log_error("Measurements did not make sense. Looks like probing went past the blade pos!\nAre you holding the blade closed or have cut filament in the extruder?")
                    else:
                        msg += "Calibration Results (cut tip):\n"
                        msg += "> variable_blade_pos: %.1f (currently: %.1f)\n" % (vbp, gcode_vars.variables['blade_pos'])
                        msg += "> variable_retract_length: %.1f-%.1f, recommend: %.1f (currently: %.1f)\n" % (mmu.p.toolhead_residual_filament + mmu.toolchange_retract, vbp, vbp - 5., gcode_vars.variables['retract_length'])
                        msg += line
                        mmu.log_always(msg)
                        if save:
                            mmu.log_always("New calibrated blade_pos and retract_length active until restart. Update mmu_macro_vars.cfg to persist")
                            gcode_vars.variables['blade_pos'] = vbp
                            gcode_vars.variables['retract_length'] = vbp - 5.

                elif clean:
                    mmu.log_always("Measuring clean toolhead dimensions after cold pull...")
                    tetn, tstn, tete = calibrator._probe_toolhead()
                    msg = line
                    msg += "Calibration Results (clean nozzle):\n"
                    msg += "> toolhead_extruder_to_nozzle: %.1f (currently: %.1f)\n" % (tetn, mmu.p.toolhead_extruder_to_nozzle)
                    msg += "> toolhead_sensor_to_nozzle: %.1f (currently: %.1f)\n" % (tstn, mmu.p.toolhead_sensor_to_nozzle)
                    if mmu_unit.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, mmu.p.toolhead_entry_to_extruder)
                    msg += line
                    mmu.log_always(msg)
                    if save:
                        mmu.log_always("New toolhead calibration active until restart. Update mmu_parameters_%s.cfg to persist settings" % mmu_unit.name) # PAUL these are currently (incorrectly) in mmu.cfg
                        mmu.p.toolhead_extruder_to_nozzle = round(tetn, 1)
                        mmu.p.toolhead_sensor_to_nozzle = round(tstn, 1)
                        mmu.p.toolhead_entry_to_extruder = round(tete, 1)

                elif dirty:
                    mmu.log_always("Measuring dirty toolhead dimensions (with filament residue)...")
                    tetn, tstn, tete = calibrator._probe_toolhead()
                    # Ooze reduction is the difference between empty and dirty measurements for sensor to nozzle
                    tor = mmu.p.toolhead_sensor_to_nozzle - tstn
                    msg = line
                    msg += "Calibration Results (dirty nozzle):\n"
                    msg += "> toolhead_residual_filament: %.1f (currently: %.1f)\n" % (tor, mmu.p.toolhead_residual_filament)
                    if mmu_unit.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, mmu.p.toolhead_entry_to_extruder)
                    msg += line
                    mmu.log_always(msg)
                    if save:
                        mmu.log_always("New calibrated ooze reduction active until restart. Update mmu_parameters_%s.cfg to persist" % mmu_unit.name) # PAUL these are currently (incorrectly) in mmu.cfg
                        mmu.p.toolhead_residual_filament = round(tor, 1)
                        mmu.p.toolhead_entry_to_extruder = round(tete, 1)

                # Unload and park filament
                mmu._unload_bowden()
                mmu._unload_gate()

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_ENCODER command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibrateEncoderCommand(BaseCommand):
    """
    Start: Assumes filament is loaded through encoder\n"
    End: Does not eject filament at end (filament same as start)\n"
    """

    CMD = "MMU_CALIBRATE_ENCODER"

    HELP_BRIEF = "Calibration routine for the MMU encoder"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT     = #(int) Optional if only one unit fitted to printer\n"
        + "LENGTH   = #(mm) Commanded distance (default: 400)\n"
        + "REPEATS  = #(count) Number of repetitions (default: 3, min: 1, max: 10)\n"
        + "SPEED    = #(mm/s) Move speed (default: gear_from_buffer_speed, min: 10)\n"
        + "ACCEL    = #(mm/s^2) Move accel (default: gear_from_buffer_accel, min: 10)\n"
        + "MINSPEED = #(mm/s) Minimum speed (default: SPEED)\n"
        + "MAXSPEED = #(mm/s) Maximum speed (default: SPEED)\n"
        + "SAVE     = [0|1] Save calibration (default: 1)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # examples / supplement if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        calibrator = mmu_unit.calibrator

        if self.check_if_disabled(): return
        if self.check_has_encoder(): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(mmu, CALIBRATED_GEAR_0, check_gates=[mmu.gate_selected]): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        speed = gcmd.get_float('SPEED', mmu_unit.p.gear_from_buffer_speed, minval=10.)
        accel = gcmd.get_float('ACCEL', mmu_unit.p.gear_from_buffer_accel, minval=10.)
        min_speed = gcmd.get_float('MINSPEED', speed, above=0.)
        max_speed = gcmd.get_float('MAXSPEED', speed, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        advance = 60. # Ensure filament is in encoder even if not loaded by user

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu._require_encoder():
                    mmu_unit.selector.filament_drive()
                    mmu.calibrating = True
                    _,_,measured,_ = mmu.trace_filament_move("Checking for filament", advance)
                    if measured < mmu_unit.encoder.encoder_min:
                        raise MmuError("Filament not detected in encoder. Ensure filament is available and try again")
                    mmu._unload_tool()
                    calibrator.calibrate_encoder(length, repeats, speed, min_speed, max_speed, accel, save)
                    _,_,_,_ = mmu.trace_filament_move("Parking filament", -advance)
        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_PSENSOR command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

@register_command
class MmuCalibratePsensorCommand(BaseCommand):
    """
    Start: Filament must be loaded in extruder
    """

    CMD = "MMU_CALIBRATE_PSENSOR"

    HELP_BRIEF = "Calibrate analog proprotional sync-feedback sensor"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT = #(int) Optional if only one unit fitted to printer\n"
        + "MOVE = #(mm) Movement range used to search limits (default: sync_feedback_buffer_maxrange, min: 1, max: 100)\n"
    )
    HELP_SUPPLEMENT = (
        "# Start: Filament must be loaded in extruder\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING,
            per_unit=True,
        )

    def _run(self, gcmd, mmu_unit):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if not mmu_unit.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            raise gcmd.error("Proportional (analog sync-feedback) sensor not found\n" + usage)

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_loaded(): return

        SD_THRESHOLD = 0.02
        MAX_MOVE_MULTIPLIER = 1.8
        STEP_SIZE = 2.0
        MOVE_SPEED = 8.0

        move = gcmd.get_float('MOVE', mmu_unit.p.sync_feedback_buffer_maxrange, minval=1, maxval=100)
        steps = math.ceil(move * MAX_MOVE_MULTIPLIER / STEP_SIZE)

        usage = (
            "Ensure your sensor is configured by setting sync_feedback_analog_pin in [mmu_sensors].\n"
            "The other settings (sync_feedback_analog_max_compression, sync_feedback_analog_max_tension "
            "and sync_feedback_analog_neutral_point) will be determined by this calibration."
        )

        if not mmu_unit.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            raise gcmd.error("Proportional (analog sync-feedback) sensor not found\n" + usage)

        def _avg_raw(n=10, dwell_s=0.1):
            """
            Sample sensor.get_status(0)['value_raw'] n times with dwell between reads
            and return moving average
            """
            sensor = mmu_unit.sensor_manager.all_sensors.get(SENSOR_PROPORTIONAL)

            k = 0.1 # 1st order,low pass filter coefficient, 0.1 for 10 samples
            avg = sensor.get_status(0).get('value_raw', None)

            for _ in range(int(max(1, n-1))):
                mmu.movequeues_dwell(dwell_s)
                raw = sensor.get_status(0).get('value_raw', None)
                if raw is None or not isinstance(raw, float):
                    return None
                avg += k * (raw - avg) # 1st order low pass filter
            return (avg)

        def _seek_limit(msg, steps, step_size, prev_val, ramp, log_label):
            mmu.log_always(msg)
            for i in range(steps):
                _ = mmu.trace_filament_move(msg, step_size, motor="gear", speed=MOVE_SPEED, wait=True)
                val = _avg_raw()

                delta = val - prev_val

                if ramp is None:
                    if delta == 0:
                        mmu.log_always("No sensor change. Retrying")
                        continue
                    ramp = (delta > 0)

                if (ramp and val >= prev_val) or (not ramp and val <= prev_val):
                    prev_val = val
                    mmu.log_always("Seeking ... ADC %s limit: %.4f" % (log_label, val))
                else:
                    # Limit found
                    return prev_val, ramp, True

            # Ran out of steps without detecting a clear limit
            return prev_val, ramp, False
        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu.wrap_gear_current(percent=mmu_unit.p.sync_gear_current, reason="while calibrating sync_feedback psensor"):
                    mmu_unit.selector.filament_drive()
                    mmu.calibrating = True

                    raw0 = _avg_raw()
                    if raw0 is None:
                        raise gcmd.error("Sensor malfunction. Could not read valid ADC output\nAre you sure you configured in [mmu_sensors]?")

                    msg = "Finding compression limit stepping up to %.2fmm\n" % (steps * STEP_SIZE)
                    c_prev = raw0
                    ramp = None
                    c_prev, ramp, found_c_limit = _seek_limit(msg, steps, STEP_SIZE, c_prev, ramp, "compressed")

                    # Back off compressed extreme
                    msg = "Backing off compressed limit"
                    mmu.log_always(msg)
                    _ = mmu.trace_filament_move(msg, -(steps * STEP_SIZE / 2.0), motor="gear", speed=MOVE_SPEED, wait=True)

                    msg = "Finding tension limit stepping up to %.2fmm\n" % (steps * STEP_SIZE)
                    t_prev = _avg_raw()
                    ramp = (not ramp) if found_c_limit else None # If compression succeeded, inverse ramp; otherwise re-detect
                    t_prev, ramp, found_t_limit = _seek_limit(msg, steps, -STEP_SIZE, t_prev, ramp, "tension")

                    # Back off tension extreme
                    msg = "Backing off tension limit"
                    mmu.log_always(msg)
                    _ = mmu.trace_filament_move(msg, (steps * STEP_SIZE / 2.0), motor="gear", speed=MOVE_SPEED, wait=True)

            if (found_c_limit and found_t_limit):
                msg =  "Calibration Results:\n"
                msg += "As wired, recommended settings (in mmu_hardware_%s.cfg) are:\n" % mmu_unit.name
                msg += "[mmu_sensors]\n"
                msg += "sync_feedback_analog_max_compression: %.4f\n" % c_prev
                msg += "sync_feedback_analog_max_tension:     %.4f\n" % t_prev
                msg += "sync_feedback_analog_neutral_point:   %.4f\n" % ((c_prev + t_prev) / 2.0)
                msg += "After updating, don't forget to restart klipper!"
                mmu.log_always(msg)
            else:
                msg = "Warning: calibration did not find both compression and tension "
                msg += "limits (compression=%s, tension=%s)\n" % (found_c_limit, found_t_limit)
                msg += "Perhaps sync_feedback_buffer_maxrange parameter is incorrect?\n"
                msg += "Alternatively with bigger movement range by running with MOVE="
                mmu.log_warning(msg)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))
        finally:
            mmu.calibrating = False
        if self.check_if_disabled(): return
