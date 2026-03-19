# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_TEST_CONFIG command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Happy Hare imports
from ..mmu_constants       import *
from ..unit.mmu_calibrator import MmuCalibrator

CATEGORY_GENERAL   = "GENERAL"
CATEGORY_TESTING   = "CALIBRATION/TESTING"
CATEGORY_STEPS     = "STEPS"             # Individual loading/unloading steps
CATEGORY_MACROS    = "MACROS"
CATEGORY_CALLBACKS = "CALLBACKS/HOOKS"
CATEGORY_INTERNAL  = "INTERNAL (CAUTION!)" # Hidden from user
CATEGORY_OTHER     = "OTHER/ALIAS"

ALL_UNITS          = "ALL"               # Token meaning all units on some commands


class BaseCommand:
    """
    Base class for Happy Hare-style gcode commands.
    Standardizes: registration, HELP=1 handling, and optional logging.
    """

    # Shared across ALL BaseCommand instances
    _registered_commands = []

    def __init__(self, mmu):
        self.mmu = mmu

    def register(
        self,
        name,
        handler,
        help_brief,
        help_params,
        help_supplement=None,
        category=CATEGORY_OTHER,
        per_unit=False,
    ):
        """
        Register a gcode command with shared help behavior.

        handler signature:
          - per_unit=False: handler(gcmd)
          - per_unit=True : handler(gcmd, mmu_unit)
        """
        def wrapped(gcmd):
            mmu = self.mmu

            mmu.log_to_file(gcmd.get_commandline())

            if gcmd.get_int('HELP', 0, minval=0, maxval=1):
                mmu.log_always(self.format_help(help_params, help_supplement or "", per_unit), color=True)
                return

            # We don't use klipper's register_mux_command() because it isn't flexible really enough
            # Instead provide flexible "UNIT" processing and pass the mmu_unit to the command handler
            # Allow unit to be the name, index, or optional (implied) if only one unit configured
            if per_unit:
                unit_param = gcmd.get("UNIT", None)
                mmu_machine = mmu.mmu_machine

                mmu_unit = self.get_unit(gcmd)

                if mmu_unit is not None:
                    return handler(gcmd, mmu_unit)

                elif unit_param == ALL_UNITS:
                    # Repeat for all units
                    for mmu_unit in mmu_machine.units:
                        handler(gcmd, mmu_unit)
                    return

                elif mmu_machine.num_units == 1:
                    # Default to unit 0
                    mmu_unit = mmu_machine.get_mmu_mmu_unit_by_index(0)
                    return handler(gcmd, unit)

                raise gcmd.error("UNIT parameter is required because you have more than one!")

            return handler(gcmd)
      

        # Record metadata for this command for help subsystem
        metadata = {
            "name": name.upper(),
            "help_brief": help_brief,
            "help_params": help_params,
            "help_supplement": help_supplement or "",
            "category": category,
            "per_unit": per_unit,
        }

        # Register command with klipper
        self.mmu.gcode.register_command(name, wrapped, desc=help_brief)

        # Record metadata in the category (global registry)
        # This checks for duplicates and replaces in case of duplicate registration
        for i, cmd in enumerate(BaseCommand._registered_commands):
            if cmd["name"] == metadata["name"]:
                BaseCommand._registered_commands[i] = metadata
                break
        else:
            BaseCommand._registered_commands.append(metadata)


    def get_unit(self, gcmd):
        """
        Helper to process the UNIT parameter and return the selected unit.
        For commands that don't want to be "per-unit" but still accept UNIT parameter.
        """
        mmu = self.mmu
        mmu_machine = mmu.mmu_machine
        unit_param = gcmd.get("UNIT", None)

        mmu_unit = None
        if unit_param is not None:
            # Try lookup by name first
            mmu_unit = mmu_machine.get_mmu_unit_by_name(unit_param)

            # If not found, try as unit index
            if mmu_unit is None:
                try:
                    unit_index = int(unit_param)
                    mmu_unit = mmu_machine.get_mmu_unit_by_index(unit_index)
                except (ValueError, TypeError):
                    pass
        return mmu_unit


    def format_help(self, msg, supplement=None, per_unit=False, not_registered=False):
        """
        Format a help message and optional supplement into a nicely aligned block.

        The input `msg` is expected to be multi-line with the first line containing
        either "command: description" or just a single heading line. Subsequent
        lines may contain parameter definitions in the form "name = value".

        This function:
          - Keeps the heading (and highlights the command using UI markers "{5}" / "{6}").
          - Aligns parameter names into a column (minimum width 10).
          - Prefixes parameter lines with a cascade/UI marker using `UI_CASCADE`.
          - Uses `UI_SPACE` as the fill character when padding parameter names.
          - Optionally appends a supplement block (if provided) wrapped with UI markers.

        Args:
            msg: The main help message (multi-line).
            supplement: Optional supplemental text (multi-line) appended after the main block.

        Returns:
            The formatted help string.
        """
        if not msg:
            return ""

        lines = msg.splitlines()
        if not lines:
            return msg

        # Format the heading (first line). If the heading contains ":", split into
        # command and description and wrap the command in UI markers.

        first_line = lines[0].rstrip()
        if ":" in first_line:
            cmd, helpstr = first_line.split(":", 1)
            cmd_text = cmd.strip()
            if per_unit:
                cmd_text += UI_SUPERSCRIPT_1
            elif not_registered:
                cmd_text += UI_SUPERSCRIPT_2
            formatted_help = "{5}" + cmd_text + "{6} : " + helpstr.strip()
        else:
            formatted_help = first_line


        # Compute parameter name column width: minimum 10, else longest name+1.
        param_lines = [ln for ln in lines[1:] if "=" in ln]
        def param_name_length(ln):
            name = ln.split("=", 1)[0].strip()
            return len(name) + 1

        param_width = max(10, max((param_name_length(ln) for ln in param_lines), default=0))

        # Build formatted parameter lines
        formatted_params: list[str] = []
        for ln in lines[1:]:
            if "=" in ln:
                key, value = ln.split("=", 1)
                key_str = key.strip()
                value_str = value.strip()
                padded_key = key_str.ljust(param_width, UI_SPACE)
                padded = f"{padded_key}= {value_str}"
                formatted_line = f"{{4}}{UI_CASCADE} {padded}{{0}}"
            else:
                formatted_line = f"{{4}}{UI_CASCADE} {ln.rstrip()}{{0}}"
            formatted_params.append(formatted_line)

        # Handle supplement if provided
        formatted_supplement = ""
        if supplement is not None:
            supp_lines = supplement.splitlines()
            if supp_lines:
                first = supp_lines[0].strip()
                formatted_supplement = "{3}{5}" + first + "{6}"
                if len(supp_lines) > 1:
                    formatted_supplement += "\n" + "\n".join(line.rstrip() for line in supp_lines[1:])
                formatted_supplement += "{0}"

        main_block = "\n".join([formatted_help] + formatted_params) if formatted_params else formatted_help
        return main_block + (
            ("\n" + formatted_supplement + "\n") if formatted_supplement else "\n"
        )


# Common "checker" methods used to guard commands -----------------------------------------------------------

    def check_if_disabled(self):
        if not self.mmu.is_enabled:
            self.mmu.log_error("Operation not possible. MMU is disabled. Please use MMU ENABLE=1 to use")
            return True
        self.mmu._wakeup()
        return False

    def check_if_printing(self):
        if self.mmu.is_printing():
            self.mmu.log_error("Operation not possible. Printer is actively printing")
            return True
        return False

    def check_if_bypass(self):
        if self.mmu.tool_selected == TOOL_GATE_BYPASS and self.mmu.filament_pos not in [FILAMENT_POS_UNLOADED]:
            self.mmu.log_error("Operation not possible. MMU is currently using bypass. Unload or select a different gate first")
            return True
        return False

    def check_if_not_homed(self, gate=None):
        if not self.mmu.selector().is_homed:
            self.mmu.log_error("Operation not possible. MMU selector is not homed")
            return True
        return False

    def check_if_loaded(self):
        if self.mmu.filament_pos not in [FILAMENT_POS_UNLOADED, FILAMENT_POS_UNKNOWN]:
            self.mmu.log_error("Operation not possible. Filament is loaded")
            return True
        return False

    def check_if_not_loaded(self):
        if self.mmu.filament_pos != FILAMENT_POS_LOADED:
            self.mmu.log_error("Operation not possible. Filament is not loaded")
            return True
        return False

    def check_if_invalid_gate(self):
        if self.mmu.gate_selected < 0:
            self.mmu.log_error("Operation not possible. No MMU gate selected")
            return True
        return False

    def check_if_always_gripped(self):
        if self.mmu.mmu_unit().filament_always_gripped:
            self.mmu.log_error("Operation not possible. MMU design doesn't allow for manual override of syncing state.\nSyncing will be enabled if filament is inside the extruder.\nUse `MMU_RECOVER` to correct filament position if necessary.")
            return True
        return False

    def check_if_no_bowden_move(self):
        if not self.mmu.mmu_unit().require_bowden_move:
            self.mmu.log_error("Operation not possible. MMU design does not require bowden move/calibration")
            return True
        return False

    def check_has_encoder(self):
        if not self.mmu.has_encoder():
            self.mmu.log_error("No encoder fitted to this MMU unit")
            return True
        return False

    def check_has_espooler(self):
        if any(self.mmu.mmu.has_espooler(gate) for gate in range(self.mmu.num_gates)):
            return False
        self.mmu.log_error("No espoolers fitted to this MMU unit")
        return True

    def check_if_spoolman_enabled(self):
        if self.mmu.p.spoolman_support == SPOOLMAN_OFF:
            self.mmu.log_error("Spoolman support is currently disabled")
            return True
        return False

    def check_if_not_calibrated(self, required, silent=False, check_gates=None, use_autotune=True):
        calibrator = self.mmu.mmu_unit().calibrator
        return calibrator.check_if_not_calibrated(required, silent=silent, check_gates=check_gates, use_autotune=use_autotune)
