# Happy Hare MMU Software
#
# Implements the NFC_HELP command (overview of the NFC command set).
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from .mmu_base_command import *
from ..unit.nfc import manager as nfc_manager


class MmuNfcHelpCommand(BaseCommand):
    """
    Show NFC reader command help.
    """

    CMD = "NFC_HELP"

    HELP_BRIEF = "Show NFC reader command help"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "ADVANCED  = 1   Include advanced shared-reader commands\n"
        + "CALLBACKS = 1   Include callback/macro command names\n"
        + "LOW_LEVEL = 1   Include low-level reader debug commands\n"
    )
    HELP_SUPPLEMENT = ""

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL,
            log=False
        )

    def _run(self, gcmd):
        gcmd.respond_info('\n'.join(nfc_manager._nfc_help(gcmd)))
