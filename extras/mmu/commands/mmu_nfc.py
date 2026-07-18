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
        + "UNLOADED    = 1       Reset local read state (called by Happy Hare's post-unload hook)\n"
        + "POLL_DISABLE = 1      Manually pause polling (normally automatic on dispatch)\n"
        + "POLL_ENABLE = 1       Manually resume polling (normally automatic on unload/removal)\n"
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
        if nfc_manager._flag_param(gcmd, 'STATUS'):
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
        elif gcmd.get_int('UNLOADED', 0):
            gate._handle_hh_unload(gcmd)
        elif gcmd.get_int('POLL_DISABLE', 0):
            gate._set_poll_enabled(gcmd, False)
        elif gcmd.get_int('POLL_ENABLE', 0):
            gate._set_poll_enabled(gcmd, True)
        elif gcmd.get_int('POLL', 0):
            polled = gate._poll()
            status = gate.status_line().strip()
            if polled is None:
                # _poll_hh_pause_check() skipped the read -- this gate
                # already dispatched a spool/uid-only event and hasn't been
                # unloaded since, so nothing was actually read from the tag.
                # Say so rather than claiming a poll ran.
                msg = ('one poll skipped; already reported to Happy Hare '
                       'and not yet unloaded; %s' % status)
            else:
                msg = 'one poll complete; %s' % status
            nfc_manager.logger.info('[%s]: %s', gate._name, msg)
            gcmd.respond_info(nfc_manager.color_console_tags(
                'NFC[%s]: %s' % (gate._name, msg)))
        elif gcmd.get_int('APPLY', 0):
            gate._apply_current_spool(gcmd)
        else:
            gate._cmd_help(gcmd)
