# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_STATUS command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import re

# Happy Hare imports
from ..mmu_constants    import *
from ..mmu_utils        import MmuError
from .mmu_base_command  import *


class MmuStatusCommand(BaseCommand):

    CMD = "MMU_STATUS"

    HELP_BRIEF = "Complete dump of current MMU state and important configuration"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "SHOWCONFIG = [0|1]\n"
        + "DETAIL     = [0|1]\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )


    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)
        on_off = lambda x: "ON" if x else "OFF"

        lines = []

        lines.append(
            f"MMU: Happy Hare v{mmu.mmu_machine.happy_hare_version} "
            f"controlling {mmu.mmu_machine.num_units} units:\n"
        )

        status_suffix = (
            " (DISABLED)"
            if not mmu.is_enabled
            else " (PAUSED)"
            if mmu.is_mmu_paused()
            else ""
        )
        if status_suffix:
            lines.append(status_suffix)

        for i in range(mmu.mmu_machine.num_units):
            unit = mmu.mmu_machine.get_mmu_unit_by_index(i)

            if unit.mmu_vendor != unit.display_name:
                lines.append(f"\n{UI_SOLID_CIRCLE} {unit.display_name}, a {unit.mmu_vendor} v{unit.mmu_version_string}")
            else:
                lines.append(f"\n{UI_SOLID_CIRCLE} {unit.mmu_vendor} v{unit.mmu_version_string}")

            first, last = unit.gate_bounds()
            active = " ACTIVE" if (mmu.mmu_machine.num_units > 1 and i == self.mmu.unit_selected) else ""
            lines.append(f" (gates {first}-{last}){active}\n")
            lines.append(f"{UI_CASCADE} Connected to extruder: {unit.extruder_name()}\n")
            lines.append(f"{UI_CASCADE} {unit.selector.get_mmu_status_config()}\n")
            if unit.has_encoder():
                lines.append(f"{UI_CASCADE} Encoder reads {mmu.get_encoder_distance():.1f}mm\n")


        # This is all now the currently active unit ---------

        mmu_unit = mmu.mmu_unit()

        lines.append(f"\nPrint state is {mmu.psm.print_state.upper()}")

        lines.append(
            f". Tool {mmu.selected_tool_string()} selected on gate "
            f"{mmu.selected_gate_string()}{mmu.selected_unit_string()}"
        )

        if mmu.saved_toolhead_operation:
            lines.append(". Toolhead position saved")

        lines.append(
            f"\nMMU gear stepper at {mmu.gear_run_current_percent}% current and is "
            f"{'SYNCED' if mmu.drive().is_synced_to_extruder() else 'not synced'} "
            f"to extruder"
        )

        if mmu._standalone_sync:
            lines.append(". Standalone sync mode is ENABLED")

        if mmu_unit.sync_feedback.is_enabled():
            lines.append(
                "\nSync feedback indicates filament in bowden is: "
                f"{mmu_unit.sync_feedback.get_sync_feedback_string(detail=True).upper()}"
            )

            if not mmu_unit.sync_feedback.is_active():
                lines.append(" (not currently active)")
        else:
            lines.append("\nSync feedback is disabled/unavailable")

        if mmu.gate_selected >= 0:
            fil_pos_str = FILAMENT_POS_NAME_MAP.get(mmu.filament_pos, "INVALID")
            lines.append(f"\nFilament positon believed to be: {fil_pos_str}")

        if config:

            # Temp scalar pulled for _f_calc() use
            self.calibrated_bowden_length = mmu_unit.calibrator.get_bowden_length()
            self.encoder_clog_detection_length = mmu.encoder().get_clog_detection_length()
            self.toolchange_retract = mmu.toolchange_retract
            self.filament_remaining = mmu_unit.extruder_wrapper.filament_remaining

            lines.append("\n\nLOAD SEQUENCE:")

            # Gate loading -------------------------------------------------------

            lines.append(
                "\n- Filament loads into gate from parked position by homing a maximum of "
                f"{self._f_calc('gate_homing_max')} to {mmu._gate_homing_string()}"
            )

            # Bowden loading -----------------------------------------------------

            if mmu_unit.require_bowden_move:
                correction = (
                    " CORRECTED"
                    if mmu_unit.p.bowden_apply_correction
                    else ""
                )

                if self.calibrated_bowden_length > 0:
                    if mmu._must_home_to_extruder():
                        if mmu_unit.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                            move = self._f_calc("calibrated_bowden_length - bowden_load_homing_buffer - toolhead_entry_to_extruder")
                        else:
                            move = self._f_calc("calibrated_bowden_length - bowden_load_homing_buffer")
                        lines.append(f"\n- Bowden is loaded with a fast{correction} {move} move ")

                    elif mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                        move = self._f_calc("calibrated_bowden_length - bowden_load_homing_buffer")
                        lines.append(f"\n- Bowden is loaded with a fast{correction} {move} move ")

                    else:
                        lines.append(f"\n- Bowden is loaded with a full fast{correction} {self._f_calc('calibrated_bowden_length')} move")

                else:
                    lines.append(
                        "\n- Bowden is not yet calibrated so will perform "
                        f"homing move of up to {self._f_calc('bowden_homing_max')}"
                    )
            else:
                lines.append("\n- No fast bowden move is required")

            # Extruder homing ----------------------------------------------------

            if mmu._must_home_to_extruder():
                if mmu_unit.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENCODER:
                    lines.append(
                        f"\n- Filament finds extruder entrance by homing a maximum of {self._f_calc('bowden_load_homing_buffer + extruder_homing_max')} "
                        f"to extruder using ENCODER COLLISION detection "
                        f"(at {mmu_unit.p.extruder_collision_homing_current}% current)"
                    )
                elif mmu_unit.p.extruder_homing_endstop == SENSOR_GEAR_TOUCH:
                    lines.append(
                        f"\n- Filament finds extruder entrance by homing homes a maximum of {self._f_calc('bowden_load_homing_buffer + extruder_homing_max')} "
                        f"to extruder using 'touch' (stallguard) detection "
                        f"(at ((5)){mmu_unit.p.extruder_collision_homing_current}%{{6}} current)"
                    )
                else:
                    lines.append(
                        f"\n- Filament finds extruder entrance by homing a maximum of {self._f_calc('bowden_load_homing_buffer + extruder_homing_max')} "
                        f"to {mmu_unit.p.extruder_homing_endstop.upper()} sensor"
                    )

                if mmu_unit.p.extruder_homing_endstop == SENSOR_EXTRUDER_ENTRY:
                    lines.append(f" and then moving {self._f_calc('toolhead_entry_to_extruder')}")
                elif mmu_unit.p.extruder_homing_endstop == SENSOR_COMPRESSION:
                    lines.append(f" and then moving -{self._f_calc('sync_feedback_buffer_range / 2')} to center sync-feedback buffer")
            else:

                if mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                    lines.append("\n- No extruder homing is necessary because will home to toolhead sensor")
                else:
                    lines.append("\n- WARNING: no extruder homing is performed - extruder loading cannot be precise")

            # Extruder loading ---------------------------------------------------

            if mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                if mmu._must_home_to_extruder():
                    move = self._f_calc('toolhead_homing_max') # Already consumed bowden homing buffer
                else:
                    move = self._f_calc('bowden_load_homing_buffer + toolhead_homing_max')

                lines.append(
                    f"\n- Extruder (synced) loads by homing a maximum of {self._f_calc('bowden_load_homing_buffer + toolhead_homing_max')} "
                    "to TOOLHEAD sensor before moving the last "
                    f"{self._f_calc('toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining')} "
                    "to the nozzle"
                )

            else:
                lines.append(
                    "\n- Extruder (synced) loads by moving "
                    f"{self._f_calc('toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract - filament_remaining')} "
                    "to the nozzle"
                )

            # Purging ------------------------------------------------------------

            if mmu.p.force_purge_standalone:
                if mmu.p.purge_macro:
                    lines.append(
                        "\n- Purging is always managed by Happy Hare using "
                        f"'{mmu.p.purge_macro}' macro with extruder purging current of "
                        f"{{5}}{mmu.p.extruder_purge_current}%{{6}}"
                    )
                else:
                    lines.append("\n- No purging is performed!")
            else:
                if mmu.p.purge_macro:
                    lines.append(
                        "\n- Purging is managed by slicer when printing. "
                        "Otherwise by Happy Hare using "
                        f"'{mmu.p.purge_macro}' macro with extruder purging current of "
                        f"{{5}}{mmu.p.extruder_purge_current}%{{6}} when not printing"
                    )
                else:
                    lines.append("\n- Purging is managed by slicer only when printing")

            # Tightening ---------------------------------------------------------

            has_tension = mmu.sensor_manager.has_sensor(SENSOR_TENSION)
            has_compression = mmu.sensor_manager.has_sensor(SENSOR_COMPRESSION)
            has_proportional = mmu.sensor_manager.has_sensor(SENSOR_PROPORTIONAL)

            if (
                mmu_unit.p.toolhead_post_load_tighten
                and not mmu_unit.p.sync_to_extruder
                and mmu.can_use_encoder()
                and mmu_unit.p.flowguard_encoder_mode
            ):
                lines.append(
                    "\n- Filament in bowden is tightened by "
                    f"{self._f_calc('toolhead_post_load_tighten% * encoder_clog_detection_length')} at reduced gear "
                    "current to prevent false encoder flowguard detection"
                )

            elif (
                mmu_unit.p.toolhead_post_load_tension_adjust
                and (
                    mmu_unit.p.sync_to_extruder
                    or mmu_unit.p.sync_purge
                )
                and (has_tension or has_compression or has_proportional)
                and mmu_unit.sync_feedback.is_enabled()
            ):
                lines.append(
                    "\n- Filament in bowden will be adjusted a maximum of "
                    f"{(mmu_unit.p.sync_feedback_buffer_range or mmu_unit.p.sync_feedback_buffer_maxrange):.1f}mm "
                    "to neutralize tension"
                )

            lines.append("\n\nUNLOAD SEQUENCE:")

            # Tip forming --------------------------------------------------------

            if mmu.p.force_form_tip_standalone:
                if mmu.p.form_tip_macro:
                    lines.append(
                        "\n- Tip is always formed by Happy Hare using "
                        f"'{mmu.p.form_tip_macro}' macro after initial retract of "
                        f"{self._f_calc('toolchange_retract')} with extruder current of "
                        f"{{5}}{mmu.p.extruder_form_tip_current}%{{6}}"
                    )
                else:
                    lines.append("\n- No tip forming is performed!")
            else:
                if mmu.p.form_tip_macro:
                    lines.append(
                        "\n- Tip is formed by slicer when printing. "
                        "Otherwise by Happy Hare using "
                        f"'{mmu.p.form_tip_macro}' macro after initial retract of "
                        f"{self._f_calc('toolchange_retract')} with extruder current of "
                        f"{{5}}{mmu.p.extruder_form_tip_current}%{{6}}"
                    )
                else:
                    lines.append("\n- Tip is formed by slicer only when printing")

            # Extruder unloading -------------------------------------------------

            if mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                lines.append(
                    "\n- Extruder (synced) unloads by reverse homing a maximum of "
                    f"{self._f_calc('toolhead_entry_to_extruder + toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin')} "
                    "to EXTRUDER sensor"
                )
            elif mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                lines.append(
                    "\n- Extruder (optionally synced) unloads by reverse homing a maximum "
                    f"{self._f_calc('toolhead_sensor_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin')} "
                    "to TOOLHEAD sensor"
                )
                lines.append(
                    ", then unloads by moving "
                    f"{self._f_calc('toolhead_extruder_to_nozzle - toolhead_sensor_to_nozzle + toolhead_unload_safety_margin')} "
                    "to exit extruder"
                )
            else:
                lines.append(
                    "\n- Extruder (optionally synced) unloads by moving "
                    f"{self._f_calc('toolhead_extruder_to_nozzle + toolhead_unload_safety_margin')} "
                    "less tip-cutting reported park position to exit extruder"
                )

            # Bowden unloading ---------------------------------------------------

            if mmu_unit.require_bowden_move:
                if self.calibrated_bowden_length >= 0:
                    if (
                        mmu.has_encoder()
                        and mmu_unit.p.bowden_pre_unload_test
                        and not mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY)
                    ):
                        lines.append(
                            "\n- Bowden is unloaded with a short "
                            f"{self._f_calc('encoder_move_step_size')} validation move before "
                            f"{self._f_calc('calibrated_bowden_length - bowden_unload_homing_buffer - encoder_move_step_size')} "
                            "fast move"
                        )
                    else:
                        lines.append(
                            "\n- Bowden is unloaded with a fast "
                            f"{self._f_calc('calibrated_bowden_length - bowden_unload_homing_buffer')} move"
                        )
                else:
                    lines.append(
                        "\n- Bowden is not yet calibrated so will perform "
                        f"homing move of up to {self._f_calc('bowden_homing_max')}"
                    )
            else:
                lines.append("\n- No fast bowden move is required")

            # Gate homing --------------------------------------------------------

            using = "using" if mmu_unit.p.gate_homing_endstop == SENSOR_ENCODER else "to"
            lines.append(
                "\n- Filament finds gate by homing a maximum of "
                f"{self._f_calc('bowden_unload_homing_buffer + gate_homing_max')} {using} {mmu._gate_homing_string()} "
            )

            # Gate parking -------------------------------------------------------

            lines.append(
                f"\n- Filament is parked by moving {self._f_calc('gate_parking_distance')} from the gate endstop ({mmu._gate_homing_string()})"
            )

        if not detail:
            lines.append("\n\nFor details on TTG and EndlessSpool groups add 'DETAIL=1'")
            if not config:
                lines.append(", for configuration summary add 'SHOWCONFIG=1'")

        lines.append(f"\n\n{mmu._mmu_visual_to_string()}")
        lines.append(f"\n{mmu._state_to_string()}")

        if detail:
            lines.append(f"\n\n{mmu.gate_maps.ttg_map_to_string()}")
            if mmu.endless_spool_enabled:
                lines.append(f"\n\n{mmu.gate_maps.es_groups_to_string()}")
            lines.append(f"\n\n{mmu.gate_maps.gate_map_to_string()}")

        msg = "".join(lines)

        mmu.log_always(msg, color=True)

        # Always warn if not fully calibrated or needs recovery
        mmu.report_necessary_recovery(use_autotune=False)


    def _f_calc(self, formula):
        """
        Display property-based calculation with property names and values
        to make it easier for users to understand the calculation.
        """
        mmu = self.mmu
        mmu_unit = mmu.mmu_unit()
        ns = {}

        def _merge_obj(o):
            for k, v in vars(o).items():
                ns[k] = v
                ns[k.lower()] = v

        # Namespace priority (last wins)
        _merge_obj(self)                        # Local (special variables)
        _merge_obj(mmu_unit.p)                  # Unit parameters
        _merge_obj(mmu_unit.toolhead_wrapper.p) # Unit toolhead parameters
        _merge_obj(mmu.p)                       # Machine parameters

        # Replace percent syntax for evaluation:
        #   foo%  ->  (foo/100.0)
        eval_formula = re.sub(r'([A-Za-z_]\w*)%', r'(\1/100.0)', formula)

        def fmt_num(x):
            return f"{float(x):.1f}"

        def fmt_pct(x):
            x = float(x)
            return f"{int(round(x))}" if abs(x - round(x)) < 1e-9 else f"{x:.1f}"

        # Calculate numeric result
        result = eval(eval_formula, {"__builtins__": {}}, ns)

        is_single_var = re.fullmatch(r'[A-Za-z_]\w*', formula.strip())

        # Annotate variables in the original formula, preserving operators
        token_pat = re.compile(r'\b([A-Za-z_]\w*)(%)?')

        def annotate_token(match):
            name = match.group(1)
            is_percent = match.group(2) == '%'
            val = ns.get(name.lower(), 0.0)

            if is_percent:
                return f"{name}:{fmt_pct(val)}%" if not is_single_var else f"{name}%"

            return f"{name}:{fmt_num(val)}" if not is_single_var else name

        annotated_formula = token_pat.sub(annotate_token, formula)

        return f"{{5}}{result:.1f}mm{{6}} {{1}}({annotated_formula}){{0}}"
