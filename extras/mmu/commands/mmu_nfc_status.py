# Happy Hare MMU Software
#
# Implements the NFC_STATUS command (report spool state for all NFC gates).
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


class MmuNfcStatusCommand(BaseCommand):
    """
    Report spool state for all configured NFC gates.
    """

    CMD = "NFC_STATUS"

    HELP_BRIEF = "Report spool state for all configured NFC gates"
    HELP_PARAMS = f"{CMD}: {HELP_BRIEF}"
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
        gcmd.respond_info('\n'.join(
            nfc_manager._lane_status_lines(self.printer)))
