# Happy Hare MMU Software
#
# Implements the NFC_LED_TEST command (test configured lane LED effects).
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


class MmuNfcLedTestCommand(BaseCommand):
    """
    Test configured NFC lane LED effects.
    """

    CMD = "NFC_LED_TEST"

    HELP_BRIEF = "Test configured NFC lane LED effects"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "ALL    = 1       Test tag-read effect on every enabled lane\n"
        + "DELAY  = <secs>  Chase delay between lanes (default 0.20)\n"
        + "CYCLES = <n>     Number of effect cycles per lane\n"
    )
    HELP_SUPPLEMENT = (
        "Example:\n"
        "NFC_LED_TEST ALL=1 DELAY=0.20 CYCLES=2 ...Chase every enabled lane"
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
        nfc_manager._cmd_led_test_all(gcmd)
