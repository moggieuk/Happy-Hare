# Happy Hare MMU Software
#
# Implements the NFC_REGISTER command (assign an NFC UID to a Spoolman spool).
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from .mmu_base_command import *
from .mmu_nfc_mixins   import NfcMixin
from ..unit.nfc import manager as nfc_manager


class MmuNfcRegisterCommand(NfcMixin, BaseCommand):
    """
    Assign an NFC UID to an existing Spoolman spool.
    """

    CMD = "NFC_REGISTER"

    HELP_BRIEF = "Assign an NFC UID to an existing Spoolman spool"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "UID      = <tag_uid>   NFC tag UID to assign (required)\n"
        + "SPOOL_ID = <n>         Existing Spoolman spool id (required)\n"
    )
    HELP_SUPPLEMENT = (
        "Example:\n"
        "NFC_REGISTER UID=04A1B2C3 SPOOL_ID=12 ...Bind tag 04A1B2C3 to spool 12"
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
        nfc_manager._cmd_register_uid_to_spool(
            self.mmu.gcode, self._spoolman(), gcmd) # _spoolman from NfcMixin
