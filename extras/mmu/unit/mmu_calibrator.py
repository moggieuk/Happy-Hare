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
#   MMU_CALIBRATE_GATE
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

        mmu = self.mmu
        u = self.mmu_unit

        def ensure_list_size(lst, size, default_value=UNCALIBRATED):
            lst = lst[:size]
            lst.extend([default_value] * (size - len(lst)))
            return lst

        # -------------------------------------------------------------------------------------------------------
        # Load bowden length configuration (calibration set with MMU_CALIBRATE_BOWDEN)
        # -------------------------------------------------------------------------------------------------------

        bowden_lengths = self.var_manager.get(VARS_MMU_BOWDEN_LENGTHS, None, namespace=u.name)
        bowden_home = self.var_manager.get(VARS_MMU_BOWDEN_HOME, u.p.gate_homing_endstop, namespace=u.name)
        if u.require_bowden_move:
            if bowden_lengths and bowden_home in GATE_ENDSTOPS:
                bowden_lengths = [UNCALIBRATED if x < 0 else x for x in bowden_lengths] # Ensure -1 value for uncalibrated
                # Ensure list size
                if len(bowden_lengths) == u.num_gates:
                    mmu.log_debug("Loaded saved bowden lengths for %s: %s" % (u.name, bowden_lengths))
                else:
                    var = self.var_manager.namespace(VARS_MMU_BOWDEN_LENGTHS, namespace=u.name)
                    mmu.log_error("Incorrect number of gates specified in %s. Adjusted length to %d gates" % (var, u.num_gates))
                    bowden_lengths = ensure_list_size(bowden_lengths, u.num_gates)

                # Ensure values are identical (just for optics) if variable_bowden_lengths is False
                if not u.variable_bowden_lengths:
                    bowden_lengths = [bowden_lengths[0]] * u.num_gates

                if not any(x == UNCALIBRATED for x in bowden_lengths):
                    self.mark_calibrated(CALIBRATED_BOWDENS)
            else:
                mmu.log_warning("Warning: Bowden lengths for %s not found in mmu_vars.cfg. Probably not calibrated yet" % u.name)
                bowden_lengths = [UNCALIBRATED] * u.num_gates
        else:
            bowden_lengths = [0] * u.num_gates
            self.mark_calibrated(CALIBRATED_BOWDENS)

        self._bowden_lengths = bowden_lengths

        # Ensure the gate endstop is what was calibrated against. If not adjust
        if self.check_calibrated(CALIBRATED_BOWDENS):
            self.adjust_bowden_lengths_on_homing_change()

        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, bowden_lengths, namespace=u.name)


        # -------------------------------------------------------------------------------------------------------
        # Load gear rotation distance configuration (calibration set with MMU_CALIBRATE_GEAR/GATE)
        # -------------------------------------------------------------------------------------------------------

        gear_steppers = u.mmu_toolhead.get_kinematics().rails[1].steppers

        rds = (
            [s.get_rotation_distance()[0] for s in gear_steppers[:u.num_gates]]
            if len(gear_steppers) >= u.num_gates
            else [gear_steppers[0].get_rotation_distance()[0]] * u.num_gates
        )
        self._default_rotation_distances = rds

        rotation_distances = self.var_manager.get(VARS_MMU_GEAR_ROTATION_DISTANCES, None, namespace=u.name)
        if rotation_distances:
            rotation_distances = [UNCALIBRATED if x == 0 else x for x in rotation_distances] # Ensure -1 value for uncalibrated
            # Ensure list size
            if len(rotation_distances) == u.num_gates:
                mmu.log_debug("Loaded saved gear rotation distances for unit %s: %s" % (u.name, rotation_distances))
            else:
                mmu.log_error("Incorrect number of gates specified in %s. Adjusted length" % self.var_manager.namespace(VARS_MMU_GEAR_ROTATION_DISTANCES, namespace=u.name))
                rotation_distances = ensure_list_size(rotation_distances, u.num_gates)

            # Ensure values are identical (just for optics) if variable_rotation_distances is False
            if not u.variable_rotation_distances:
                rotation_distances = [rotation_distances[0]] * u.num_gates

            if rotation_distances[0] != UNCALIBRATED:
                self.mark_calibrated(CALIBRATED_GEAR_0)
            if not any(x == UNCALIBRATED for x in rotation_distances):
                self.mark_calibrated(CALIBRATED_GEAR_RDS)
        else:
            mmu.log_warning("Warning: Gear rotation distances for unit %s not found in mmu_vars.cfg. Probably not calibrated yet" % u.name)
            rotation_distances = [UNCALIBRATED] * u.num_gates

        self.var_manager.set(VARS_MMU_GEAR_ROTATION_DISTANCES, rotation_distances, namespace=u.name)
        self.rotation_distances = rotation_distances

        self.var_manager.write() # Save any updates immediately


    def mark_calibrated(self, step):
        self.calibration_status |= step


    def mark_not_calibrated(self, step):
        self.calibration_status &= ~step


    def check_calibrated(self, step):
        return self.calibration_status & step == step


    # -----------------------------------------------------------------------------------------------------------
    # Bowden length manipulation
    #
    # Notes:
    #  - The bowden length is the distance between the current choice of endstops.
    #    If those endstops change the bowden length must be adjusted
    #  - A calibrated bowden length must also be updated if the rotation_distance for
    #    that gate is updated
    #  - Testing has shown that the encoder based clog detection length is generally
    #    proportional to the bowden length
    # -----------------------------------------------------------------------------------------------------------

    def get_bowden_length(self, gate=None):
        """
        Returns the currently calibrated bowden length or the default for gate 0 if not calibrated
        """
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        ref_gate = lgate if lgate >= 0 and self.mmu_unit.variable_bowden_lengths else 0
        return self._bowden_lengths[ref_gate]


    def update_bowden_length(self, length, gate=None, console_msg=False):
        """
        Update bowden calibration for current gate and clog_detection if not yet calibrated
        Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
        """
        if gate == None: gate = self.mmu.gate_selected
        mmu = self.mmu
        mmu_unit = self.mmu_unit
        lgate = mmu_unit.local_gate(gate)

        if lgate < 0:
            mmu.log_debug("Assertion failure: cannot save bowden length for gate: %s" % mmu.selected_gate_string(gate))
            return

        all_gates = not mmu_unit.variable_bowden_lengths

        if length < 0: # Reset
            action = "reset"
            if all_gates:
                self._bowden_lengths = [UNCALIBRATED] * mmu_unit.num_gates
            else:
                self._bowden_lengths[lgate] = UNCALIBRATED

        else:
            length = round(length, 1)
            action = "saved"
            if all_gates:
                self._bowden_lengths = [length] * mmu_unit.num_gates
            else:
                self._bowden_lengths[lgate] = length

        msg = "Calibrated bowden length (%.1fmm) has been %s %s" % (length, action, ("for all gates" if all_gates else "gate %d" % gate))
        if console_msg:
            mmu.log_always(msg)
        else:
            mmu.log_debug(msg)

        # Update calibration status
        if not any(x == UNCALIBRATED for x in self._bowden_lengths):
            self.calibration_status |= CALIBRATED_BOWDENS

        # Persist
        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, self._bowden_lengths, namespace=mmu_unit.name)
        self.var_manager.write()


    def adjust_bowden_lengths_on_homing_change(self):
        """
        Adjust all bowden lengths if endstop is changed (e.g. from MMU_TEST_CONFIG)
        """
        mmu = self.mmu
        mmu_unit = self.mmu_unit

        current_home = self.var_manager.get(VARS_MMU_BOWDEN_HOME, None, namespace=mmu_unit.name)
        if mmu_unit.p.gate_homing_endstop == current_home:
            return

        adjustment = 0
        if current_home == SENSOR_ENCODER:
            adjustment = mmu_unit.p.gate_endstop_to_encoder
        elif mmu_unit.p.gate_homing_endstop == SENSOR_ENCODER:
            adjustment = -mmu_unit.p.gate_endstop_to_encoder
        self._bowden_lengths = [length + adjustment if length != UNCALIBRATED else length for length in self._bowden_lengths]
        mmu.log_debug("Adjusted bowden lengths by %.1f: %s because of gate_homing_endstop change" % (adjustment, self._bowden_lengths))

        # Persist
        self.var_manager.set(VARS_MMU_BOWDEN_LENGTHS, self._bowden_lengths, namespace=mmu_unit.name)
        self.var_manager.set(VARS_MMU_BOWDEN_HOME, mmu_unit.p.gate_homing_endstop, namespace=mmu_unit.name)
        self.var_manager.write()


    def is_bowden_length_calibrated(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate >= 0:
            return self._bowden_lengths[lgate] >= 0
        return True


    # -----------------------------------------------------------------------------------------------------------
    # Encoder based runout/clog/tangle length manipulation
    # -----------------------------------------------------------------------------------------------------------

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


    # -----------------------------------------------------------------------------------------------------------
    # Gear stepper rotation distance manipulation
    # Notes:
    #  - If the rotation distance is changed for gate with calibrated bowden length then adjust bowden length
    # -----------------------------------------------------------------------------------------------------------

    def get_gear_rd(self, gate=None):
        """
        Return current calibrated gear rotation_distance or sensible default
        Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
        """
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        rd = self.rotation_distances[lgate] if lgate >= 0 else self._default_rotation_distances[0]

        if rd <= 0:
            rd = self._default_rotation_distances[lgate]
            self.mmu.log_debug("Gate %d not calibrated, falling back to default rotation_distance: %.4f" % (gate, rd))

        return rd

    # Return the default gear rotation_distance for gate
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def get_default_gear_rd(self, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        lgate = max(0, lgate)
        return self._default_rotation_distances[lgate]


    # Set the active gear stepper rotation distance
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def set_gear_rd(self, rd, gate=None):
        if gate == None: gate = self.mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if rd and lgate >= 0:
            self.mmu.log_trace("Setting gate %d gear motor rotation distance: %.4f" % (gate, rd))
            self.mmu_unit.gear_stepper_obj(gate).set_rotation_distance(rd)


    # Save rotation_distance for gate (and associated gates) adjusting any calibrated bowden length
    # Note: gate is the logical gate so important to convert to local per-unit lgate but report gate in messages
    def update_gear_rd(self, rd, gate=None, console_msg=False):
        mmu = self.mmu
        mmu_unit = self.mmu_unit

        if gate == None: gate = mmu.gate_selected
        lgate = mmu_unit.local_gate(gate)

        if gate < 0:
            mmu.log_debug("Assertion failure: cannot save gear rotation_distance for gate: %d" % gate)
            return

        all_gates = not mmu_unit.variable_rotation_distances

        if rd < 0: # Reset
            if all_gates:
                self.rotation_distances = [UNCALIBRATED] * mmu_unit.num_gates
            else:
                self.rotation_distances[lgate] = UNCALIBRATED

            mmu.log_always("Gear rotation distance calibration has been reset for %s" % ("all gates" if all_gates else "gate %d" % gate))

        else:
            prev_rd = self.get_gear_rd(gate)
            rd = round(rd, 4)

            if all_gates:
                self.rotation_distances = [rd] * mmu_unit.num_gates
                updated_gates = mmu_unit.gate_range()
            else:
                self.rotation_distances[lgate] = rd
                updated_gates = [gate]

            msg = "Calibrated rotation distance (%.4f) has been saved for %s" % (rd, ("all gates" if all_gates else "gate %d" % gate))
            if console_msg:
                mmu.log_always(msg)
            else:
                mmu.log_debug(msg)

            # Now adjust effected calibrated bowden lengths
            update_bowdens = updated_gates if mmu_unit.variable_bowden_lengths else [gate]
            for g in update_bowdens:
                prev_bowden = self.get_bowden_length(g)
                if prev_bowden != UNCALIBRATED:
                    new_bl = prev_bowden * (prev_rd / rd) # Adjust for same effective calibrated distance
                    self.update_bowden_length(new_bl, g, console_msg=console_msg)

        # Update calibration status
        if self.rotation_distances[0] != UNCALIBRATED:
            self.calibration_status |= CALIBRATED_GEAR_0
        if not any(x == UNCALIBRATED for x in self.rotation_distances):
            self.calibration_status |= CALIBRATED_GEAR_RDS

        # Persist
        self.var_manager.set(VARS_MMU_GEAR_ROTATION_DISTANCES, self.rotation_distances, namespace=mmu_unit.name, write=True)


    def is_gear_rd_calibrated(self, gate=None):
        if gate == None: gate = mmu.gate_selected
        lgate = self.mmu_unit.local_gate(gate)

        if lgate >= 0:
            return self.rotation_distances[lgate] >= 0
        return True


    # -----------------------------------------------------------------------------------------------------------
    # Autotuning from load/unload telemetry data
    # -----------------------------------------------------------------------------------------------------------

    def note_load_telemetry(self, bowden_length, bowden_move_ratio, bowden_travel):
        self.mmu.log_error(f"note_load_telemetry(bowden_length={bowden_length}, bowden_move_ratio={bowden_move_ratio}, bowden_travel={bowden_travel})")
        return
# PAUL IMPORTANT: bowden_move_ratio can now be None. 

# PAUL TODO...
#        homing_delta = None
#        if homing_movement is not None:
#            homing_delta = homing_movement - expected_homing
#PAUL            homing_movement -= deficit
#        self._autotune(DIRECTION_LOAD, bowden_move_ratio, homing_delta) # PAUL check autotune


    def note_unload_telemetry(self, bowden_length, bowden_move_ratio, bowden_travel):
        self.mmu.log_error(f"note_unload_telemetry(bowden_length={bowden_length}, bowden_move_ratio={bowden_move_ratio}, bowden_travel={bowden_travel})")
        return
 # PAUL ratio, homing_buffer

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
                    gate0_rd = self.rotation_distances[0] # PAUL NO needs mingate for unit

                    # Allow max 10% variation from gate 0 for autotune
                    if math.isclose(new_rd, gate0_rd, rel_tol=0.1):
                        if not self.mmu.calibrating and self.rotation_distances[self.mmu.gate_selected] > 0: # PAUL NO
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


    # -----------------------------------------------------------------------------------------------------------
    # Used by mmu controller, commands and various other unit components to validate and report on calibration
    # -----------------------------------------------------------------------------------------------------------

    def check_if_not_calibrated(self, required, silent=False, check_gates=None, use_autotune=True):
        mmu = self.mmu
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
