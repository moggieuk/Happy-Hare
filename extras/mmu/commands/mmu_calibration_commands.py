# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements commands:
#   MMU_CALIBRATE_GEAR
#   MMU_CALIBRATE_ENCODER
#   MMU_CALIBRATE_GATE
#   MMU_CALIBRATE_BOWDEN
#   MMU_CALIBRATE_TOOLHEAD (per-unit)
#   MMU_CALIBRATE_PSENSOR  (per-unit)
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
from ..mmu_constants         import *
from ..mmu_utils             import MmuError
from .mmu_base_command       import *
from .mmu_calibration_mixins import CalibrationMixin


# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_GEAR command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

class MmuCalibrateGearCommand(CalibrationMixin):
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
        "Examples:\n"
        + f"{CMD} MEASURED=96.5           ...measured 96.5mm on default 100mm move\n"
        + f"{CMD} LENGTH=200 MEASURED=202 ...moved 200mm and measured 202mm\n"
        + f"{CMD} RESET=1                 ...reset rotation distance for current gate to default\n"
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
        mmu_unit = self.mmu_unit = mmu.mmu_unit()
        calibrator = mmu_unit.calibrator
        gate = mmu.gate_selected

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_invalid_gate(): return

        length = gcmd.get_float('LENGTH', 100., above=50.)
        measured = gcmd.get_float('MEASURED', -1, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)

        with mmu.wrap_sync_gear_to_extruder():
            if reset:
                default_rd = calibrator.get_default_gear_rd(gate)
                calibrator.set_gear_rd(default_rd)
                calibrator.update_gear_rd(UNCALIBRATED, console_msg=True)
                return

            if measured > 0:
                current_rd = mmu_unit.gear_stepper_obj(gate).get_rotation_distance()[0]
                new_rd = round(current_rd * measured / length, 4)
                mmu.log_always(
                    f"MMU gear stepper for gate {gate} 'rotation_distance' calculated to be {new_rd:.4f} (currently: {current_rd:.4f})"
                )
                if save:
                    calibrator.set_gear_rd(new_rd)
                    calibrator.update_gear_rd(new_rd, console_msg=True)
                return

            raise gcmd.error("Must specify 'MEASURED=' and optionally 'LENGTH='")



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_ENCODER command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

class MmuCalibrateEncoderCommand(CalibrationMixin):
    """
    Start: Assumes filament is loaded through encoder\n"
    End: Does not eject filament at end (filament same as start)\n"
    """

    CMD = "MMU_CALIBRATE_ENCODER"

    HELP_BRIEF = "Calibration routine for the MMU encoder"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT     = #(int)|_name_ Specify unit by name, number (optional if single unit)\n"
        + "LENGTH   = #(mm) Commanded distance (default: 400)\n"
        + "REPEATS  = #(count) Number of repetitions (default: 3, min: 1, max: 10)\n"
        + "SPEED    = #(mm/s) Move speed\n"
        + "ACCEL    = #(mm/s^2) Move accel\n"
        + "MINSPEED = #(mm/s) Minimum speed, speed of first repeat (default: SPEED)\n"
        + "MAXSPEED = #(mm/s) Maximum speed, speed of last repeat (default: SPEED)\n"
        + "SAVE     = [0|1] Save calibration (default: 1)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD} LENGTH=200 REPEATS=5      ...average over 5 repetitions with a move length of 200mm\n"
        + f"{CMD} SAVE=0                    ...perform default calibration but don't save result\n"
        + f"{CMD} MINSPEED=100 MAXSPEED=300 ...calibrate over default three moves of increasing speeds\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_TESTING
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu
        mmu_unit = self.mmu_unit = mmu.mmu_unit()
        calibrator = mmu_unit.calibrator
        gate = mmu.gate_selected

        if self.check_if_disabled(): return
        if self.check_if_no_encoder(mmu_unit): return
        if self.check_if_bypass(): return
        if self.check_if_not_calibrated(
            CALIBRATED_GEAR_RDS,
            check_gates=[gate]
        ): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        speed = gcmd.get_float('SPEED', mmu_unit.p.gear_from_filament_buffer_speed, minval=10.)
        accel = gcmd.get_float('ACCEL', mmu_unit.p.gear_from_filament_buffer_accel, minval=10.)
        min_speed = gcmd.get_float('MINSPEED', speed, above=0.)
        max_speed = gcmd.get_float('MAXSPEED', speed, above=0.)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        advance = 60. # Ensure filament is in encoder even if not loaded by user

        try:
            mmu.calibrating = True

            with mmu.wrap_sync_gear_to_extruder():
                with mmu._require_encoder():
                    mmu_unit.selector.filament_drive()
                    _,_,measured,_ = mmu.trace_filament_move("Checking for filament", advance)

                    if measured < mmu_unit.encoder.encoder_min:
                        raise MmuError("Filament not detected in encoder. Ensure filament is available and try again")

                    self._calibrate_encoder(length, repeats, min_speed, max_speed, accel, save)
                    _,_,_,_ = mmu.trace_filament_move("Parking filament", -advance)

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))

        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_GATE command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

class MmuCalibrateGateCommand(CalibrationMixin):
    """
    Start: Will home selector, select gate 0 or required gate
    End: Filament will unload
    """

    CMD = "MMU_CALIBRATE_GATE"

    HELP_BRIEF = "Optional calibration of rotational distance using calibrated encoder and gate 0 reference"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT    = #(int)|_name_ Specify unit by name, number (only required if ALL=1 and multi-unit)\n"
        + "LENGTH  = #(mm) Commanded distance (default: 400)\n"
        + "REPEATS = #(count) Number of repetitions (default: 3, min: 1, max: 10)\n"
        + "ALL     = [0|1] Calibrate all gates (same as MMU_CALIBRATE_GATES alias)\n"
        + "GATE    = #(index) Gate to calibrate (defaults to current gate unless ALL=1)\n"
        + "SAVE    = [0|1] Save calibration (default: 1)\n"
        + "RESET   = [0|1] Reset gate rotation_distance\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}                         ...default calibration procedure of rd for current gate\n"
        + f"{CMD} GATE=2 LENGTH=200       ...calibrate rd for gate 2 using a shorter than default 200mm movement\n"
        + f"{CMD} ALL=1 LENGTH=200 SAVE=0 ...calibrate all gates unit in sequence, report but don't save results\n"
        + f"{CMD} RESET=1                 ...reset the rotation distance for gate (or current gate) to default\n"
        + f"{CMD} RESET=1 ALL=1           ...reset rd on all gates except first (reference) gate\n"
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
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_invalid_gate(): return

        length = gcmd.get_float('LENGTH', 400., above=0.)
        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        all_gates = gcmd.get_int('ALL', 0, minval=0, maxval=1)
        gate = gcmd.get_int('GATE', None, minval=0, maxval=mmu.num_gates - 1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if all_gates:
            mmu_unit = self.get_unit(gcmd)
            gate_range = mmu_unit.gate_range()
        elif gate is not None:
            mmu_unit = mmu.mmu_unit(gate)
            gate_range = [gate]
        else:
            gate = mmu.gate_selected
            mmu_unit = mmu.mmu_unit(mmu.gate_selected)
            gate_range = [gate]

        if self.check_if_no_encoder(mmu_unit): return
        
        self.mmu_unit = mmu_unit
        calibrator = mmu_unit.calibrator

        if self.check_if_not_calibrated(
            CALIBRATED_ENCODER | CALIBRATED_SELECTOR,
            check_gates=gate_range
        ): return

        if reset:
            for g in gate_range:
                if g != mmu_unit.first_gate: # Don't allow reset of initial gate with this command
                    default_rd = calibrator.get_default_gear_rd(g)
                    calibrator.set_gear_rd(default_rd, g)
                    calibrator.update_gear_rd(UNCALIBRATED, g)
            return

        try:
            with mmu.wrap_sync_gear_to_extruder():
                mmu._unload_tool()
                mmu.calibrating = True

                with mmu._require_encoder():
                    if all_gates:
                        mmu.log_always("Start the complete calibration of ancillary gates...")
                    for g in gate_range:
                        self._calibrate_gate(g, length, repeats, save=bool((save and g != mmu_unit.first_gate)))
                    if all_gates:
                        mmu.log_always("Phew! End of auto gate calibration")

        except MmuError as ee:
            mmu.handle_mmu_error(str(ee))

        finally:
            mmu.calibrating = False



# -----------------------------------------------------------------------------------------------------------
# MMU_CALIBRATE_BOWDEN command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

class MmuCalibrateBowdenCommand(CalibrationMixin):
    """
    Calibrated bowden length is always from chosen gate homing point to the extruder gears
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
        + "BOWDEN_LENGTH = #(mm) Approx bowden length, normally >actual (but slightly <actual if using COLLISION)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}             ...calibrate bowden in current gate\n"
        + f"{CMD} MANUAL=1    ...calibrate bowden in reverse from manually placed filament at extruder gear\n"
        + f"{CMD} SAVE=0      ...measure bowden using default scheme but don't save the results\n"
        + f"{CMD} RESET=1     ...reset calibrated bowden for current gate. (allows first-time auto calibration)\n"
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
        mmu_unit = self.mmu_unit = mmu.mmu_unit() # For gate selected
        calibrator = mmu_unit.calibrator
        gate = mmu.gate_selected

        if self.check_if_disabled(): return
        if self.check_if_no_bowden_move(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return
        if self.check_if_invalid_gate(): return

        repeats = gcmd.get_int('REPEATS', 3, minval=1, maxval=10)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        manual = bool(gcmd.get_int('MANUAL', 0, minval=0, maxval=1))
        collision = bool(gcmd.get_int('COLLISION', 0, minval=0, maxval=1))
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))

        if reset:
            calibrator.update_bowden_length(UNCALIBRATED, console_msg=True)
            return

        if manual:
            if self.check_if_not_calibrated(
                CALIBRATED_GEAR_0 | CALIBRATED_GEAR_RDS | CALIBRATED_SELECTOR,
                check_gates=[gate]
            ): return
        else:
            if self.check_if_not_calibrated(
                CALIBRATED_GEAR_0 | CALIBRATED_GEAR_RDS | CALIBRATED_ENCODER | CALIBRATED_SELECTOR,
                check_gates=[gate]
            ): return

        can_use_sensor = (
            mmu_unit.p.extruder_homing_endstop in [
                SENSOR_EXTRUDER_ENTRY,
                SENSOR_COMPRESSION,
                SENSOR_GEAR_TOUCH
            ] and (
                mmu_unit.sensor_manager.has_sensor(mmu_unit.p.extruder_homing_endstop) or
                mmu_unit.gear_stepper_obj(gate).is_endstop_virtual(mmu_unit.p.extruder_homing_endstop)
            )
        )
        can_auto_calibrate = mmu_unit.has_encoder() or can_use_sensor

        if not can_auto_calibrate and not manual:
            mmu.log_always(
                "No encoder or extruder entry sensor available.\n"
                "Use manual calibration method:\n"
                "- With gate selected, manually load filament all the way to the extruder gear\n"
                "- Then run 'MMU_CALIBRATE_BOWDEN MANUAL=1 BOWDEN_LENGTH=xxx' where BOWDEN_LENGTH is GREATER than your real length"
            )
            return

        extruder_homing_max = gcmd.get_float('HOMING_MAX', 150, above=0.)
        approx_bowden_length = gcmd.get_float('BOWDEN_LENGTH', mmu_unit.p.bowden_homing_max if (manual or can_use_sensor) else None, above=0.)
        if approx_bowden_length is None:
            raise gcmd.error("Must specify 'BOWDEN_LENGTH=x' where x is slightly LESS than your estimated bowden length to give room for homing")

        try:
            with mmu.wrap_sync_gear_to_extruder():
                with mmu._wrap_suspend_filament_monitoring():
                    mmu.calibrating = True
                    if manual:
                        # Method 1: Manual (reverse homing to gate) method
                        length = self._calibrate_bowden_length_manual(approx_bowden_length)

                    elif can_use_sensor and not collision:
                        # Method 2: Automatic one-shot method with homing sensor (BEST)
                        mmu._unload_tool()
                        length = self._calibrate_bowden_length_sensor(approx_bowden_length)

                    elif mmu_unit.has_encoder():
                        # Method 3: Automatic averaging method with encoder and extruder collision. Uses repeats for accuracy
                        mmu._unload_tool()
                        length = self._calibrate_bowden_length_collision(approx_bowden_length, extruder_homing_max, repeats)

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

class MmuCalibrateToolheadCommand(CalibrationMixin):
    """
    Start: Test gate should already be selected
    End: Filament will unload
    """

    CMD = "MMU_CALIBRATE_TOOLHEAD"

    HELP_BRIEF = "Automated measurement of key toolhead parameters"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT  = #(int)|_name_ Specify unit by name, number (optional if single unit)\n"
        + "CLEAN = [0|1] Measure clean nozzle dimensions (after cold pull)\n"
        + "DIRTY = [0|1] Measure residual filament (dirty nozzle)\n"
        + "CUT   = [0|1] Measure blade position (hold cutter closed)\n"
        + "SAVE  = [0|1] Persist results in active config (default: 1)\n"
    )
    HELP_SUPPLEMENT = """Reminder - run with this sequence of options:
    1) CLEAN=1 with clean extruder for: toolhead_extruder_to_nozzle, toolhead_sensor_to_nozzle (and toolhead_entry_to_extruder)
    2) DIRTY=1 with dirty extruder (uncut tip fragment) for: toolhead_residual_filament (and toolhead_entry_to_extruder)
    3) CUT=1 holding blade in for: variable_blade_pos
    Desired gate should be selected but the filament unloaded
    (SAVE=0 to run without persisting results)
    Note: On Type-B MMUs you might experience noise/grinding as movement limits are explored
          (select bypass or reduce gear stepper current if a problem)
    """

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
        self.mmu_unit = mmu_unit
        mmu = self.mmu
        calibrator = mmu_unit.calibrator
        gate = mmu.gate_selected

        if self.check_if_disabled(): return
        if self.check_if_not_homed(): return
        if self.check_if_bypass(): return
        if self.check_if_loaded(): return

        if self.check_if_not_calibrated(
            CALIBRATED_GEAR_RDS| CALIBRATED_ENCODER | CALIBRATED_SELECTOR | CALIBRATED_BOWDENS,
            check_gates=[gate]
        ): return

        if not mmu_unit.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
            raise gcmd.error("Sorry this feature requires a toolhead sensor")

        clean = gcmd.get_int('CLEAN', 0, minval=0, maxval=1)
        dirty = gcmd.get_int('DIRTY', 0, minval=0, maxval=1)
        cut = gcmd.get_int('CUT', 0, minval=0, maxval=1)
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        line = "-----------------------------------------------\n"

        if not (clean or cut or dirty):
            mmu.log_always(self.HELP_SUPPLEMENT)
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
                mmu._load_bowden(start_pos=overshoot)
                mmu._home_to_extruder(mmu_unit.p.extruder_homing_max)

                if cut:
                    mmu.log_always("Measuring blade cutter position (with filament fragment)...")
                    tetn, tstn, tete = self._probe_toolhead()
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
                    tetn, tstn, tete = self._probe_toolhead()
                    msg = line
                    msg += "Calibration Results (clean nozzle):\n"
                    msg += "> toolhead_extruder_to_nozzle: %.1f (currently: %.1f)\n" % (tetn, mmu.p.toolhead_extruder_to_nozzle)
                    msg += "> toolhead_sensor_to_nozzle: %.1f (currently: %.1f)\n" % (tstn, mmu.p.toolhead_sensor_to_nozzle)
                    if mmu_unit.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                        msg += "> toolhead_entry_to_extruder: %.1f (currently: %.1f)\n" % (tete, mmu.p.toolhead_entry_to_extruder)
                    msg += line
                    mmu.log_always(msg)
                    if save:
# PAUL these params are currently (incorrectly) in mmu.cfg
                        mmu.log_always("New toolhead calibration active until restart. Update mmu_parameters_%s.cfg to persist settings" % mmu_unit.name)
                        mmu.p.toolhead_extruder_to_nozzle = round(tetn, 1)
                        mmu.p.toolhead_sensor_to_nozzle = round(tstn, 1)
                        mmu.p.toolhead_entry_to_extruder = round(tete, 1)

                elif dirty:
                    mmu.log_always("Measuring dirty toolhead dimensions (with filament residue)...")
                    tetn, tstn, tete = self._probe_toolhead()
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
# PAUL these params are currently (incorrectly) in mmu.cfg
                        mmu.log_always("New calibrated ooze reduction active until restart. Update mmu_parameters_%s.cfg to persist" % mmu_unit.name)
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
# MMU_CALIBRATE_PSENSOR command
#  This "registered command" will be instantiated later by the main mmu_controller module
# -----------------------------------------------------------------------------------------------------------

class MmuCalibratePsensorCommand(CalibrationMixin):
    """
    Start: Filament must be loaded in extruder
    """

    CMD = "MMU_CALIBRATE_PSENSOR"

    HELP_BRIEF = "Calibrate analog proportional sync-feedback sensor"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "UNIT = #(int)|_name_ Specify unit by name, number (optional if single unit)\n"
        + "MOVE = #(mm) Movement range used to search limits (default: sync_feedback_buffer_maxrange, min: 1, max: 100)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f"{CMD}         ...perform calibration using default movement\n"
        + f"{CMD} MOVE=30 ...calibrate using a longer filament movement - for larger buffers\n"
        "(filament must be loaded in extruder before running)\n"
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
        self.mmu_unit = mmu_unit
        mmu = self.mmu

        usage = (
            "Ensure your sensor is configured by setting sync_feedback_analog_pin in [mmu_sensors].\n"
            "The other settings (sync_feedback_analog_max_compression, sync_feedback_analog_max_tension "
            "and sync_feedback_analog_neutral_point) will be determined by this calibration."
        )

        if not mmu_unit.sensor_manager.has_sensor(SENSOR_PROPORTIONAL):
            raise gcmd.error("Proportional (analog sync-feedback) sensor not found\n" + usage)

        if self.check_if_disabled(): return
        if self.check_if_bypass(): return
        if self.check_if_not_loaded(): return

        MAX_MOVE_MULTIPLIER = 1.8
        STEP_SIZE = 2.0
        MOVE_SPEED = 8.0

        move = gcmd.get_float('MOVE', mmu_unit.p.sync_feedback_buffer_maxrange, minval=1, maxval=100)
        steps = math.ceil(move * MAX_MOVE_MULTIPLIER / STEP_SIZE)

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
