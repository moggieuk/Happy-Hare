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
from ..mmu_dialog      import DIALOG_RESUME_HINT
from .mmu_base_command import *


class MmuErrorDialogCommand(BaseCommand):

    CMD = "MMU_ERROR_DIALOG"

    HELP_BRIEF = "Customize the pop-up dialog shown when the MMU pauses (cleared at print start/end)"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "MSG   = _text_ Lines (separated by \\n) shown after the reason, replacing the default resume hint\n"
        + "ABORT = [0|1] Add an ABORT (CANCEL_PRINT) button\n"
        + "RESET = [0|1] Clear MSG and ABORT (applied before them)\n"
        + "(no parameters to report current settings)\n"
    )
    HELP_SUPPLEMENT = (
        "Examples:\n"
        + f'{CMD} ABORT=1                           ...Add an ABORT button for this print\n'
        + f'{CMD} MSG="Ensure T0 is loaded\\nThen RESUME" ...Replace the hint text with two lines\n'
        + f'{CMD} RESET=1                           ...Return to defaults\n'
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
        abort = gcmd.get_int('ABORT', None, minval=0, maxval=1)

        if reset:
            dialog.reset()
        if msg is not None:
            dialog.set(lines=msg.replace("\\n", "\n").split("\n"))
        if abort is not None:
            dialog.set(show_abort=bool(abort))

        if not (reset or msg is not None or abort is not None):
            self.mmu.log_always(
                "Error dialog %s\nText: %s\nButtons: %s" % (
                    "enabled" if self.mmu.p.show_error_dialog else "disabled (show_error_dialog=0)",
                    " / ".join(dialog.extra_lines or [DIALOG_RESUME_HINT]),
                    ", ".join(label for label, _, _ in dialog.buttons()),
                )
            )
