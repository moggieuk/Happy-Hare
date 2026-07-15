# Happy Hare MMU Software
#
# Implements the NFC command (per-gate reader control/test).
#
# NFC gate objects own reader and spool state; this command owns all Klipper
# GCode registration and routing for one configured per-gate NFC reader.
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


class MmuNfcCommand(NfcMixin, BaseCommand):
    """
    Control or test one configured per-gate NFC reader.
    """

    CMD = "NFC"

    HELP_BRIEF = "Control or test one configured per-gate NFC reader"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "GATE        = <#>     Per-lane reader to act on (required)\n"
        + "STATUS      = 1       Show this gate state\n"
        + "INIT        = 1       Re-run reader hardware init\n"
        + "SCAN        = 1       Scan hardware once, no Spoolman/Happy Hare dispatch\n"
        + "JOG_SCAN    = 1       Start scan-jog to find tag on a loaded spool\n"
        + "LED_TEST    = 1       Test configured lane tag-read LED effect\n"
        + "CYCLES      = <n>     LED test cycles (used with LED_TEST=1)\n"
        + "POLL        = 1       Run one full read/resolve cycle\n"
        + "APPLY       = 1       Send cached spool to Happy Hare now\n"
        + "CLEAR_CACHE = 1       Clear cached spool/UID, no Happy Hare dispatch\n"
        + "HH_SYNC     = 1       Seed lane cache from Happy Hare gate map (with SPOOL_ID=<n>)\n"
        + "READ        = [0|1]   Stop (0) or start (1) timer polling\n"
    )
    HELP_SUPPLEMENT = (
        "Run 'NFC GATE=<#>' with no action to show gate-specific help.\n"
        "See NFC_HELP for the complete NFC command set."
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
        # BaseCommand wrapper already handles HELP=1.
        gate = self._lane(gcmd) # From NfcMixin
        if gate._cmd_low_level_debug(gcmd):
            return
        read_value = gcmd.get('READ', None)
        if read_value is not None:
            gate._set_reading(
                gcmd, gcmd.get_int('READ', minval=0, maxval=1) == 1)
        elif nfc_manager._flag_param(gcmd, 'STATUS'):
            gcmd.respond_info(gate.status_line())
        elif gcmd.get_int('INIT', 0):
            gate._manual_init(gcmd)
        elif gcmd.get_int('SCAN', 0):
            gate._manual_scan(gcmd)
        elif gcmd.get_int('LED_TEST', 0):
            gate._lane_led_test(gcmd)
        elif gcmd.get_int('JOG_SCAN', 0):
            gate._manual_jog_scan(gcmd)
        elif gcmd.get_int('CLEAR_CACHE', 0) or gcmd.get_int('CLEAR', 0):
            gate._clear_spool_cache(gcmd)
        elif gcmd.get_int('POLL', 0):
            gate._poll()
            status = gate.status_line().strip()
            nfc_manager.logger.info(
                '[%s]: one poll complete; %s', gate._name, status)
            gcmd.respond_info(nfc_manager.color_console_tags(
                'NFC[%s]: one poll complete; %s' % (gate._name, status)))
        elif gcmd.get_int('APPLY', 0):
            gate._apply_current_spool(gcmd)
        elif gcmd.get_int('HH_SYNC', 0):
            gate._hh_sync(gcmd)
        else:
            gate._cmd_help(gcmd)
