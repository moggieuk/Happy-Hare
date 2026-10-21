# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_ERROR_DIALOG command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_dialog      import BUILTIN_BUTTONS, DIALOG_RESUME_HINT
from .mmu_base_command import *


class MmuErrorDialogCommand(BaseCommand):

    CMD = "MMU_ERROR_DIALOG"

    HELP_BRIEF = "Customize the pop-up dialog shown when the MMU pauses (cleared at print start/end)"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "MSG     = _text_ Lines (separated by \\n) shown after the reason, replacing the default resume hint\n"
        + "BUTTONS = _list_ Buttons shown left of 'error_dialog_buttons': %s or LABEL|GCODE[|style]\n" % ",".join(BUILTIN_BUTTONS)
        + "RESET   = [0|1] Clear MSG and BUTTONS (applied before them)\n"
        + "SHOW    = [0|1] Preview the dialog\n"
        + "(no parameters to report current settings)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f'{CMD} BUTTONS=abort                     ...Add an ABORT (CANCEL_PRINT) button for this print\n'
        + f'{CMD} MSG="Ensure T0 is loaded\\nThen RESUME" ...Replace the hint text with two lines\n'
        + f'{CMD} RESET=1                           ...Return to defaults\n'
        + f'{CMD} SHOW=1                            ...Preview the dialog in Mainsail/Fluidd\n'
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_MACROS,
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        dialog = self.mmu.error_dialog

        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        msg = gcmd.get('MSG', None)
        buttons = gcmd.get('BUTTONS', None)
        show = gcmd.get_int('SHOW', 0, minval=0, maxval=1)

        if reset:
            dialog.reset()
        if msg is not None:
            msg = msg.replace("\\n", "\n")
            dialog.set(lines=msg.split("\n"))
        if buttons is not None:
            try:
                dialog.set(buttons=[b for b in buttons.split(',') if b.strip()])
            except ValueError as e:
                raise gcmd.error("Invalid BUTTONS: %s" % e)

        if show:
            dialog.show_error("Dialog preview", "Preview from %s SHOW=1" % self.CMD)
        elif not (reset or msg is not None or buttons is not None):
            self.mmu.log_always(
                "Error dialog %s\nTitle: %s\nText: %s\nButtons: %s" % (
                    "enabled" if self.mmu.p.show_error_dialog else "disabled (show_error_dialog=0)",
                    dialog.title(),
                    " / ".join(dialog.extra_lines or [DIALOG_RESUME_HINT]),
                    ", ".join(label for label, _, _ in dialog.buttons()),
                )
            )
