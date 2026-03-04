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

from .mmu_base_command import *


class MmuHelpCommand(BaseCommand):

    CMD = "MMU_HELP"

    HELP_BRIEF = "Display the complete set of MMU commands and function"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "ALL       = [0|1] Show all commands (excluding aliases)\n"
        + "GENERAL   = [0|1] Regular MMU commands (DEFAULT ON)\n"
        + "TESTING   = [0|1] Calibration and testing commands\n"
        + "SLICER    = [0|1] Print start/end or slicer macros (defined in mmu_software.cfg)\n"
        + "CALLBACKS = [0|1] Callbacks macros (defined in mmu_sequence.cfg, mmu_state.cfg)\n"
        + "STEPS     = [0|1] Advanced load/unload sequence and steps commands\n"
        + "ALIAS     = [0|1] Show legacy aliased commands\n"
        + "CMD       = _cmd_ Show help on command (same as _cmd_ HELP=1)\n"
        + "SHOWPARMS = [0|1] Show parameter help\n"
        + "EXAMPLES  = [0|1] Show supplemental examples\n"
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
        def flag(name, default=0):
            return bool(gcmd.get_int(name, default, minval=0, maxval=1))

        show_all      = flag("ALL", 0)
        show_params   = flag("SHOWPARAMS", 0)
        show_examples = flag("EXAMPLES", 0)
        slicer        = flag("SLICER", 0)
        callbacks     = flag("CALLBACKS", 0)
        cmd = gcmd.get("CMD", None)

        # Which categories exist + which flag controls showing them
        categories = [
            (CATEGORY_GENERAL,   flag("GENERAL", 1)),
            (CATEGORY_TESTING,   flag("TESTING", 0)),
            (CATEGORY_STEPS,     flag("STEPS", 0)),
            (CATEGORY_ALIAS,     flag("ALIAS", 0)),
        ]

        if show_all:
            categories = [(cat, True) for cat, _enabled in categories]

        commands_by_category = {}
        for c in BaseCommand._registered_commands:
            commands_by_category.setdefault(c["category"], []).append(c)

        lines = ["Happy Hare MMU commands (add HELP=1 for options):\n"]

        for category, enabled in categories:
            if not enabled:
                continue

            cmds = commands_by_category.get(category, [])
            cmds.sort(key=lambda c: c["name"])
            if not cmds:
                continue

            lines.append("\nCategory: %s (%d)\n" % (category, len(cmds)))
            for c in cmds:
                if show_params:
                    supplement = c["help_supplement"] if show_examples else None
                    lines.append(self.format_help(c['help_params'], supplement))
                    lines.append("\n")
                else:
                    lines.append(self.format_help("%s : %s" % (c['name'], c['help_brief'])))

# PAUL All this before needs sorting out after all commands are migrated
        # Command / macros that are not registered
        cmds = list(self.mmu.gcode.ready_gcode_handlers.keys())
        slicer_lines = []
        steps_lines = []
        callbacks_lines = []
        misc_lines = []

        # Logic to partition commands:
        for c in cmds:
            cu = c.upper()
            d = self.mmu.gcode.gcode_help.get(c, "n/a")

            if any(rc["name"] == c for rc in BaseCommand._registered_commands):
                continue

            if slicer and (c.startswith("MMU_START") or c.startswith("MMU_END") or c in ["MMU_UPDATE_HEIGHT"]):
                 slicer_lines.append(self.format_help("%s : %s\n" % (cu, d))) # Print start/end macros

            elif c.startswith("_MMU"):
                if c in ["_MMU_M400", "_MMU_LOAD_SEQUENCE", "_MMU_UNLOAD_SEQUENCE"]:
                    steps_lines.append(self.format_help("%s : %s\n" % (cu, d))) # Invidual sequence step commands
                elif c.startswith("_MMU_PRE_") or c.startswith("_MMU_POST_") or c in ["_MMU_ACTION_CHANGED", "_MMU_EVENT", "_MMU_PRINT_STATE_CHANGED"]:
                    callbacks_lines.append(self.format_help("%s: %s\n" % (cu, d))) # Callbacks

            elif c.startswith("MMU") and not c.startswith("MMU__"):
                misc_lines.append(self.format_help("%s : %s" % (cu, d))) # Base command

        lines.append("\nCategory: %s (%d)\n" % ('SLICER', len(slicer_lines)))
        lines.extend(slicer_lines)
        lines.append("\nCategory: %s (%d)\n" % ('STEPS', len(steps_lines)))
        lines.extend(steps_lines)
        lines.append("\nCategory: %s (%d)\n" % ('CALLBACKS', len(callbacks_lines)))
        lines.extend(callbacks_lines)
        lines.append("\nCategory: %s (%d)\n" % ('MISC', len(misc_lines)))
        lines.extend(misc_lines)

        msg = "".join(lines)
        self.mmu.log_always(msg, color=True)
