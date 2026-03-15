# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_HELP command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging
from collections       import defaultdict

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuHelpCommand(BaseCommand):

    CMD = "MMU_HELP"

    HELP_BRIEF = "Display the complete set of MMU commands and function"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "ALL       = [0|1] Show all user commands types\n"
        + "GENERAL   = [0|1] Regular MMU commands (DEFAULT ON)\n"
        + "TESTING   = [0|1] Calibration and testing commands\n"
        + "STEPS     = [0|1] Advanced load/unload sequence and steps commands\n"
        + "MACROS    = [0|1] Print start/end or slicer macros (defined in mmu_software.cfg)\n"
        + "CALLBACKS = [0|1] Callbacks macros (defined in mmu_sequence.cfg, mmu_state.cfg)\n"
        + "INTERNAL  = [0|1] Internal commands/macros (Caution!)\n" # Hidden from user unless explcitily asked for
        + "OTHER     = [0|1] Alias or not categorised\n"
        + "CMD       = _cmd_ Show help on command (same as _cmd_ HELP=1)\n"
        + "PARAMS    = [0|1] Show parameter help\n"
        + "EXAMPLES  = [0|1] Show supplemental examples\n"
        + "(without parameters it will summarize all major commands)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + "%s ALL=1                                   ...Summerize all user commands\n" % CMD
        + "%s PARAMS=1                                ...Summerize basic commands showing parameters\n" % CMD
        + "%s GENERAL=0 TESTING=1 PARAMS=1 EXAMPLES=1 ...Provide details help on all testing/calibration commands\n" % CMD
        + "%s INTERNAL=1 PARAMS=1                     ...You are a developer? Caution!\n" % CMD
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

        def flag(name, default=0):
            return bool(gcmd.get_int(name, default, minval=0, maxval=1))

        show_all      = flag("ALL", 0)
        show_params   = flag("PARAMS", 0)
        show_examples = flag("EXAMPLES", 0)
        internal      = gcmd.get_int("INTERNAL", 0)
        cmd           = gcmd.get("CMD", None)

        # First handle specific command if specified
        if cmd is not None:
            cmd = cmd.upper()
            registered = {rc["name"].upper() for rc in BaseCommand._registered_commands}
            if cmd in registered:
                mmu.gcode.run_script_from_command("%s HELP=1" % cmd)
            else:
                mmu.log_warning("Sorry, command %s doesn't exist or has no parameter help" % cmd)
            return

        # Which categories exist + which flag controls showing them
        default = 1 if not internal else 0
        categories = [
            (CATEGORY_GENERAL,   flag("GENERAL", default)),
            (CATEGORY_TESTING,   flag("TESTING", 0)),
            (CATEGORY_STEPS,     flag("STEPS", 0)),
            (CATEGORY_MACROS,    flag("MACROS", 0)),
            (CATEGORY_CALLBACKS, flag("CALLBACKS", 0)),
            (CATEGORY_INTERNAL,  flag("INTERNAL", internal)),
            (CATEGORY_OTHER,     flag("OTHER", default)),
        ]

        if show_all:
            categories = [
                (cat, True if cat != CATEGORY_INTERNAL else flag("INTERNAL", 0))
                for cat, _enabled in categories
            ]

        # Build combined list of registered + non-registered commands
        combined = list(BaseCommand._registered_commands) + list(self.non_registered_commands())

        # Group commands by category (robust to missing keys assumed not needed per your note)
        commands_by_category = defaultdict(list)
        for c in combined:
            commands_by_category[c["category"]].append(c)

        lines = ["Happy Hare MMU commands (add HELP=1 for options to see more):\n"]
        lines.append("{5}%s{6}Per-unit command, {5}%s{6}Defined by gcode macro\n" % (UI_SUPERSCRIPT_1, UI_SUPERSCRIPT_2))

        for category, enabled in categories:
            if not enabled:
                continue

            cmds = commands_by_category.get(category, [])
            cmds.sort(key=lambda c: c["name"])
            if not cmds:
                continue

            lines.append("\nCategory: %s (%d)\n" % (category, len(cmds)))
            for c in cmds:
                # ensure per_unit exists for both branches
                per_unit = bool(c.get("per_unit", False))
                not_registered = bool(c.get("not_registered", False))

                if show_params:
                    supplement = c.get("help_supplement") if show_examples else None
                    # help_params may be empty string; fallback to one-line brief if you prefer
                    help_params = c.get("help_params", "")
                    if not help_params:
                        help_params = "%s : %s" % (c.get("name", ""), c.get("help_brief", ""))
                    lines.append(self.format_help(help_params, supplement, per_unit, not_registered))
                    lines.append("\n")
                else:
                    lines.append(self.format_help("%s : %s" % (c.get("name", ""), c.get("help_brief", "")), None, per_unit, not_registered))

        msg = "".join(lines)
        mmu.log_always(msg, color=True)


    def non_registered_commands(self):
        """
        Return a list of klipper commands (not in BaseCommand._registered_commands).
        Unknown commands are categorized and returned as metadata dicts.
        """
        mmu = self.mmu

        # All klipper gcode commands
        k_cmds = list(mmu.gcode.ready_gcode_handlers.keys())

        # Fast lookup of registered names
        registered = {rc["name"].upper() for rc in BaseCommand._registered_commands}

        def categorize(c):
            cu = c.upper()
            if cu in ["MMU_COLD_PULL"]:
                return CATEGORY_GENERAL

            if cu in ["MMU_QUERY_PSENSOR"]:
                return CATEGORY_TESTING

            if (cu.startswith("MMU_START") or cu.startswith("MMU_END") or cu in ["MMU_UPDATE_HEIGHT", "MMU_CHANGE_TOOL_STANDALONE"]):
                return CATEGORY_MACROS

            if cu.startswith("_MMU"):
                if cu in ["_MMU_M400", "_MMU_LOAD_SEQUENCE", "_MMU_UNLOAD_SEQUENCE"]:
                    return CATEGORY_STEPS
                if (
                    cu.startswith("_MMU_PRE_")
                    or cu.startswith("_MMU_POST_")
                    or cu in ["_MMU_ACTION_CHANGED", "_MMU_EVENT", "_MMU_PRINT_STATE_CHANGED"]
                ):
                    return CATEGORY_CALLBACKS
                return CATEGORY_INTERNAL

            if cu.startswith("MMU__"):
                return CATEGORY_INTERNAL

            if cu.startswith("MMU"):
                return CATEGORY_OTHER

            return None


        other_cmds = []
        for cmd in k_cmds:
            name = cmd.upper()
            if name in registered:
                continue

            help_brief = mmu.gcode.gcode_help.get(cmd, "n/a")
            category = categorize(cmd)

            if category is not None:
                metadata = {
                    "name": name,
                    "help_brief": help_brief,
                    "help_params": "",
                    "help_supplement": "",
                    "category": category,
                    "per_unit": False,
                    "not_registered": True,
                }
                other_cmds.append(metadata)

        return other_cmds
