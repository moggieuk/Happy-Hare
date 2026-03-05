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
from ..unit.mmu_encoder import (RUNOUT_DISABLED,
                                RUNOUT_STATIC,
                                RUNOUT_AUTOMATIC)
from ..mmu_utils        import MmuError
from .mmu_base_command  import *


class MmuStatusCommand(BaseCommand):

    CMD = "MMU_STATUS"

    HELP_BRIEF = "Complete dump of current MMU state and important configuration"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
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

        config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
        detail = gcmd.get_int('DETAIL', 0, minval=0, maxval=1)
        on_off = lambda x: "ON" if x else "OFF"

        lines = []

        lines.append(
            f"MMU: Happy Hare {self.mmu._fversion(self.mmu.mmu_machine.happy_hare_version)} "
            f"controlling {self.mmu.mmu_machine.num_units} units:"
        )

        status_suffix = (
            " (DISABLED)"
            if not self.mmu.is_enabled
            else " (PAUSED)"
            if self.mmu.is_mmu_paused()
            else ""
        )
        if status_suffix:
            lines.append(status_suffix)

        for i in range(self.mmu.mmu_machine.num_units):
            unit = self.mmu.mmu_machine.get_mmu_unit_by_index(i)

            if unit.mmu_vendor != unit.display_name:
                lines.append(
                    f"\n+ {unit.display_name}, a {unit.mmu_vendor} "
                    f"v{unit.mmu_version_string}"
                )
            else:
                lines.append(f"\n+ {unit.mmu_vendor} v{unit.mmu_version_string}")

            lines.append(f" (gates {unit.first_gate}-{unit.first_gate + unit.num_gates})")

            lines.append(f"\n{UI_CASCADE} {self.mmu.selector().get_mmu_status_config()}")

            if self.mmu.has_encoder():
                lines.append(f". Encoder reads {self.mmu.get_encoder_distance():.1f}mm")

        lines.append(f"\nPrint state is {self.mmu.print_state.upper()}")

        lines.append(
            f". Tool {self.mmu.selected_tool_string()} selected on gate "
            f"{self.mmu.selected_gate_string()}{self.mmu.selected_unit_string()}"
        )

        if self.mmu.saved_toolhead_operation:
            lines.append(". Toolhead position saved")

        lines.append(
            f"\nMMU gear stepper at {self.mmu.gear_run_current_percent}% current and is "
            f"{'SYNCED' if self.mmu.mmu_toolhead().is_gear_synced_to_extruder() else 'not synced'} "
            f"to extruder"
        )

        if self.mmu._standalone_sync:
            lines.append(". Standalone sync mode is ENABLED")

        if self.mmu.mmu_unit().sync_feedback.is_enabled():
            lines.append(
                "\nSync feedback indicates filament in bowden is: "
                f"{self.mmu.mmu_unit().sync_feedback.get_sync_feedback_string(detail=True).upper()}"
            )

            if not self.mmu.mmu_unit().sync_feedback.is_active():
                lines.append(" (not currently active)")
        else:
            lines.append("\nSync feedback is disabled")

        if config:
            # Temp scalar pulled for _f_calc() use
            self.calibrated_bowden_length = (
                self.mmu.mmu_unit().calibrator.get_bowden_length()
            )
            self.toolchange_retract = self.mmu.toolchange_retract
            self.filament_remaining = self.mmu.filament_remaining

            lines.append("\n\nLOAD SEQUENCE:")

            # Gate loading -------------------------------------------------------
            lines.append(
                "\n- Filament loads into gate by homing a maximum of "
                f"{self._f_calc('gate_homing_max')} to {self.mmu._gate_homing_string()}"
            )

            # Bowden loading -----------------------------------------------------
            if self.mmu.mmu_unit().require_bowden_move:

                correction = (
                    " CORRECTED"
                    if self.mmu.mmu_unit().p.bowden_apply_correction
                    else ""
                )

                if self.calibrated_bowden_length >= 0:
                    if self.mmu._must_buffer_extruder_homing():

                        if (
                            self.mmu.mmu_unit().p.extruder_homing_endstop
                            == SENSOR_EXTRUDER_ENTRY
                        ):
                            move = self._f_calc(
                                "bowden_fast_load_portion% * "
                                "calibrated_bowden_length - "
                                "toolhead_entry_to_extruder"
                            )
                        else:
                            move = self._f_calc(
                                "bowden_fast_load_proportion% * "
                                "calibrated_bowden_length"
                            )

                        lines.append(
                            f"\n- Bowden is loaded with a fast{correction} {move} move"
                        )
                    else:
                        lines.append(
                            f"\n- Bowden is loaded with a full fast{correction} "
                            f"{self._f_calc('calibrated_bowden_length')} move"
                        )
                else:
                    lines.append(
                        "\n- Bowden is not yet calibrated so will perform "
                        f"homing move of up to {self._f_calc('bowden_homing_max')}"
                    )
            else:
                lines.append("\n- No fast bowden move is required")

            # Extruder homing ----------------------------------------------------
            if self.mmu._must_home_to_extruder():
                if (
                    self.mmu.mmu_unit().p.extruder_homing_endstop
                    == SENSOR_EXTRUDER_COLLISION
                ):
                    lines.append(
                        f", then homes a maximum of {self._f_calc('extruder_homing_max')} "
                        f"to extruder using COLLISION detection "
                        f"(at {self.mmu.mmu_unit().p.extruder_collision_homing_current}% current)"
                    )
                elif (
                    self.mmu.mmu_unit().p.extruder_homing_endstop
                    == SENSOR_GEAR_TOUCH
                ):
                    lines.append(
                        f", then homes a maxium of {self._f_calc('extruder_homing_max')} "
                        "to extruder using 'touch' (stallguard) detection"
                    )
                else:
                    lines.append(
                        f", then homes a maximum of {self._f_calc('extruder_homing_max')} "
                        f"to {self.mmu.extruder_homing_endstop.upper()} sensor"
                    )

                if (
                    self.mmu.mmu_unit().p.extruder_homing_endstop
                    == SENSOR_EXTRUDER_ENTRY
                ):
                    lines.append(
                        f" and then moves {self._f_calc('toolhead_entry_to_extruder')} "
                        "to extruder extrance"
                    )
            else:
                if (
                    self.mmu.mmu_unit().p.extruder_homing_endstop
                    == SENSOR_EXTRUDER_NONE
                    and not self.mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD)
                ):
                    lines.append(
                        ". WARNING: no extruder homing is performed - "
                        "extruder loading cannot be precise"
                    )
                else:
                    lines.append(", no extruder homing is necessary")

            # Extruder loading ---------------------------------------------------
            if self.mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
                lines.append(
                    f"\n- Extruder (synced) loads by homing a maximum of {self._f_calc('toolhead_homing_max')} "
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
            if self.mmu.p.force_purge_standalone:
                if self.mmu.p.purge_macro:
                    lines.append(
                        "\n- Purging is always managed by Happy Hare using "
                        f"'{self.mmu.p.purge_macro}' macro with extruder purging current of "
                        f"{self.mmu.p.extruder_purge_current}%"
                    )
                else:
                    lines.append("\n- No purging is performed!")
            else:
                if self.mmu.p.purge_macro:
                    lines.append(
                        "\n- Purging is managed by slicer when printing. "
                        "Otherwise by Happy Hare using "
                        f"'{self.mmu.p.purge_macro}' macro with extruder purging current of "
                        f"{self.mmu.p.extruder_purge_current}% when not printing"
                    )
                else:
                    lines.append("\n- Purging is managed by slicer only when printing")

            # Tightening ---------------------------------------------------------
            has_tension = self.mmu.sensor_manager.has_sensor(SENSOR_TENSION)
            has_compression = self.mmu.sensor_manager.has_sensor(SENSOR_COMPRESSION)
            has_proportional = self.mmu.sensor_manager.has_sensor(SENSOR_PROPORTIONAL)

            if (
                self.mmu.mmu_unit().p.toolhead_post_load_tighten
                and not self.mmu.mmu_unit().p.sync_to_extruder
                and self.mmu._can_use_encoder()
                and self.mmu.mmu_unit().sync_feedback.p.flowguard_encoder_mode
            ):
                lines.append(
                    "\n- Filament in bowden is tightened by "
                    f"{min(self.mmu.encoder().get_clog_detection_length() * self.mmu.mmu_unit().p.toolhead_post_load_tighten / 100, 15):.1f}mm "
                    f"({self.mmu.mmu_unit().p.toolhead_post_load_tighten}% of clog detection length) at reduced gear "
                    "current to prevent false clog detection"
                )

            elif (
                self.mmu.mmu_unit().p.toolhead_post_load_tension_adjust
                and (
                    self.mmu.mmu_unit().p.sync_to_extruder
                    or self.mmu.mmu_unit().p.sync_purge
                )
                and (has_tension or has_compression or has_proportional)
                and self.mmu.mmu_unit().sync_feedback.is_enabled()
            ):
                lines.append(
                    "\n- Filament in bowden will be adjusted a maximum of "
                    f"{(self.mmu.mmu_unit().sync_feedback.p.sync_feedback_buffer_range or self.mmu.mmu_unit().sync_feedback.p.sync_feedback_buffer_maxrange):.1f}mm "
                    "to neutralize tension"
                )

            lines.append("\n\nUNLOAD SEQUENCE:")

            # Tip forming --------------------------------------------------------
            if self.mmu.p.force_form_tip_standalone:
                if self.mmu.p.form_tip_macro:
                    lines.append(
                        "\n- Tip is always formed by Happy Hare using "
                        f"'{self.mmu.p.form_tip_macro}' macro after initial retract of "
                        f"{self._f_calc('toolchange_retract')} with extruder current of "
                        f"{self.mmu.p.extruder_form_tip_current}%"
                    )
                else:
                    lines.append("\n- No tip forming is performed!")
            else:
                if self.mmu.p.form_tip_macro:
                    lines.append(
                        "\n- Tip is formed by slicer when printing. "
                        "Otherwise by Happy Hare using "
                        f"'{self.mmu.p.form_tip_macro}' macro after initial retract of "
                        f"{self._f_calc('toolchange_retract')} with extruder current of "
                        f"{self.mmu.p.extruder_form_tip_current}%"
                    )
                else:
                    lines.append("\n- Tip is formed by slicer only when printing")

            # Extruder unloading -------------------------------------------------
            if self.mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY):
                lines.append(
                    "\n- Extruder (synced) unloads by reverse homing a maximum of "
                    f"{self._f_calc('toolhead_entry_to_extruder + toolhead_extruder_to_nozzle - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract + toolhead_unload_safety_margin')} "
                    "to EXTRUDER sensor"
                )
            elif self.mmu.sensor_manager.has_sensor(SENSOR_TOOLHEAD):
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
            if self.mmu.mmu_unit().require_bowden_move:
                if self.calibrated_bowden_length >= 0:
                    if (
                        self.mmu.has_encoder()
                        and self.mmu.mmu_unit().p.bowden_pre_unload_test
                        and not self.mmu.sensor_manager.has_sensor(SENSOR_EXTRUDER_ENTRY)
                    ):
                        lines.append(
                            "\n- Bowden is unloaded with a short "
                            f"{self._f_calc('encoder_move_step_size')} validation move before "
                            f"{self._f_calc('bowden_fast_unload_portion% * calibrated_bowden_length - encoder_move_step_size')} "
                            "fast move"
                        )
                    else:
                        lines.append(
                            "\n- Bowden is unloaded with a fast "
                            f"{self._f_calc('bowden_fast_unload_portion% * calibrated_bowden_length')} move"
                        )
                else:
                    lines.append(
                        "\n- Bowden is not yet calibrated so will perform "
                        f"homing move of up to {self._f_calc('bowden_homing_max')}"
                    )
            else:
                lines.append("\n- No fast bowden move is required")

            # Gate parking -------------------------------------------------------
            lines.append(
                "\n- Filament is stored by homing a maximum of "
                f"{self._f_calc('gate_homing_max')} to {self.mmu._gate_homing_string()} "
                f"and parking {self._f_calc('gate_parking_distance')} in the gate\n"
            )

        if not detail:
            lines.append("\n\nFor details on TTG and EndlessSpool groups add 'DETAIL=1'")
            if not config:
                lines.append(", for configuration add 'SHOWCONFIG=1'")

        lines.append(f"\n\n{self.mmu._mmu_visual_to_string()}")
        lines.append(f"\n{self.mmu._state_to_string()}")

        if detail:
            lines.append(f"\n\n{self.mmu._ttg_map_to_string()}")
            if self.mmu.endless_spool_enabled:
                lines.append(f"\n\n{self.mmu._es_groups_to_string()}")
            lines.append(f"\n\n{self.mmu._gate_map_to_string()}")

        msg = "".join(lines)

        self.mmu.log_always(msg, color=True)

        # Always warn if not fully calibrated or needs recovery
        self.mmu.report_necessary_recovery(use_autotune=False)


    def _f_calc(self, formula):
        """
        Display property based calculation with property name and value
        to make it easier for users to understand application
        """
        ns = {}

        def _merge_obj(o):
            items = vars(o).items()
            for k, v in items:
                ns[k] = v
                ns[k.lower()] = v

        # Namespace priority (last wins)
        _merge_obj(self)                  # Local
        _merge_obj(self.mmu.mmu_unit().p) # Unit parameters
        _merge_obj(self.mmu.p)            # Machine parameters

        # Rewrite percent syntax for evaluation
        # Replace:   foo%
        # With:      (foo/100.0)
        percent_pat = re.compile(r'([A-Za-z_]\w*)%')

        eval_formula = percent_pat.sub(r'(\1/100.0)', formula)

        # Formatting helpers

        def fmt_num(x):
            return "%.1f" % float(x)

        def fmt_pct(x):
            x = float(x)
            return "%.0f" % x if abs(x - round(x)) < 1e-9 else "%.1f" % x

        def format_var(p):
            return p + ':' + fmt_num(ns.get(p.lower(), 0.0))

        def format_percent_term(term):
            if term.endswith('%'):
                name = term[:-1]
                val = ns.get(name.lower(), 0.0)
                return f"{name}:{fmt_pct(val)}%"
            return None

        # Evaluate
        result = eval(eval_formula, {"__builtins__": {}}, ns)

        # Build display string
        terms = re.split(r'(\+|\-)', formula)

        formatted_formula = "%.0fmm (" % result

        for term in terms:
            term = term.strip()

            if term in ('+', '-'):
                formatted_formula += " " + term + " "
            elif len(terms) > 1:
                pct_fmt = format_percent_term(term)
                if pct_fmt:
                    formatted_formula += pct_fmt
                else:
                    formatted_formula += format_var(term)
            else:
                formatted_formula += term

        formatted_formula += ")"
        return formatted_formula

