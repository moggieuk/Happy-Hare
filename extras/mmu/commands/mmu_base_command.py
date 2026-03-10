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
from ..mmu_constants import UI_SPACE, UI_CASCADE

CATEGORY_GENERAL   = "GENERAL"
CATEGORY_TESTING   = "CALIBRATION/TESTING"
CATEGORY_STEPS     = "STEPS"     # Individual loading/unloading steps
CATEGORY_ALIAS     = "ALIAS"
CATEGORY_MACROS    = "MACROS"
CATEGORY_CALLBACKS = "CALLBACKS"
CATEGORY_INTERNAL  = "INTERNAL"  # Hidden from user
CATEGORY_OTHER     = "OTHER"     # If not explicitly assigned (should be empty)

ALL_UNITS          = "ALL"


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
            self.mmu.log_to_file(gcmd.get_commandline())

            if gcmd.get_int('HELP', 0, minval=0, maxval=1):
                self.mmu.log_always(self.format_help(help_params, help_supplement or ""), color=True)
                return

            # We don't use klipper's register_mux_command() because it isn't flexible really enough
            # Instead provide flexible "UNIT" processing and pass the mmu_unit to the command handler
            # Allow unit to be the name, index, or optional (implied) if only one unit configured
            if per_unit:
                unit_param = gcmd.get("UNIT", None)
                machine = self.mmu.mmu_machine
                if unit_param is not None:
                    # Try lookup by name first
                    unit = machine.get_mmu_unit_by_name(unit_param)

                    if unit is None and unit_param == ALL_UNITS:
                        # Repeat for all units
                        for unit in machine.units:
                            handler(gcmd, unit)
                        return

                    # If not found, try as unit index
                    elif unit is None:
                        try:
                            unit_index = int(unit_param)
                            unit = machine.get_mmu_unit_by_index(unit_index)
                        except (ValueError, TypeError):
                            pass

                    if unit is None:
                        raise gcmd.error("Invalid UNIT '%s'. Must be a valid unit name or unit index" % unit_param)

                elif machine.num_units == 1:
                    # Default to unit 0
                    unit = machine.get_mmu_unit_by_index(0)
                else:
                    raise gcmd.error("UNIT parameter is required")

                return handler(gcmd, unit)

            return handler(gcmd)

        # Record metadata for this command for help subsystem
        metadata = {
            "name": name.upper(),
            "help_brief": help_brief,
            "help_params": help_params,
            "help_supplement": help_supplement or "",
            "category": category,
            "per_unit": per_unit,
#            "instance": self, # Don't really need this
        }

        # Register command with klipper
        self.mmu.gcode.register_command(name, wrapped, desc=help_brief)

        # Record metadata in the category (global registry)
        BaseCommand._registered_commands.append(metadata)


    def format_help(self, msg, supplement=None):
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
            formatted_help = "{5}" + cmd.strip() + "{6} : " + helpstr.strip()
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
        return main_block + (("\n" + formatted_supplement) if formatted_supplement else "\n")
