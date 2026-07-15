# Happy Hare MMU Software
#
# Implements the NFC_DOCTOR command (check NFC setup and configuration).
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


class MmuNfcDoctorCommand(BaseCommand):
    """
    Check NFC reader setup and configuration.
    """

    CMD = "NFC_DOCTOR"

    HELP_BRIEF = "Check NFC reader setup and configuration"
    HELP_PARAMS = f"{CMD}: {HELP_BRIEF}"
    HELP_SUPPLEMENT = (
        "Checks NFC config, readers, Spoolman, and Happy Hare hooks."
    )

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
        gcmd.respond_info(nfc_manager.color_console_tags(
            '\n'.join(nfc_manager._doctor_lines(self.printer))))
