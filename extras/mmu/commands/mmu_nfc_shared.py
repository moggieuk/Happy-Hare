# Happy Hare MMU Software
#
# Implements the NFC_SHARED command (control the shared NFC reader).
#
# The shared NFC gate object owns reader and spool state; this command owns all
# Klipper GCode registration and routing for the single shared reader.
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


class MmuNfcSharedCommand(NfcMixin, BaseCommand):
    """
    Control the configured shared NFC reader.
    """

    CMD = "NFC_SHARED"

    HELP_BRIEF = "Control the configured shared NFC reader"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "STATUS  = 1       Show detailed shared reader state\n"
        + "SUMMARY = 1       Show one-line shared reader state\n"
        + "READ    = [0|1]   Stop (0) or start (1) shared polling\n"
        + "CANCEL  = 1       Cancel a staged shared spool\n"
        + "REPLACE = 1       Discard a staged spool and scan another\n"
        + "RESET   = 1       Clear shared state, restore LEDs, and poll\n"
        + "CLEAR   = 1       Clear pending state and stop polling\n"
        + "LED_TEST    = 1   Test configured shared tag-read LED effect\n"
        + "POLL        = 1   Run one full read/resolve cycle\n"
        + "SCAN        = 1   Raw hardware scan only\n"
        + "INIT        = 1   Re-run NFC reader init\n"
        + "CLEAR_CACHE = 1   Clear tag cache, keeping pending spool\n"
    )
    HELP_SUPPLEMENT = (
        "Run 'NFC_SHARED' with no action to show shared reader help.\n"
        "See NFC_HELP ADVANCED=1 for Happy Hare pre-load hook commands."
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
        shared = self._shared(gcmd) # From NfcMixin
        flag = nfc_manager._flag_param
        color = nfc_manager.color_console_tags
        logger = nfc_manager.logger

        if shared._cmd_low_level_debug(gcmd):
            return
        read_value = gcmd.get('READ', None)
        if read_value is not None:
            shared._set_reading(
                gcmd, gcmd.get_int('READ', minval=0, maxval=1) == 1)
        elif flag(gcmd, 'STATUS'):
            gcmd.respond_info(color(
                'NFC %s' % shared.shared_status_detail()))
        elif flag(gcmd, 'SUMMARY'):
            gcmd.respond_info(color(
                'NFC %s' % shared.shared_summary_line()))
        elif flag(gcmd, 'REPLACE'):
            shared._shared_replace_pending(gcmd)
        elif flag(gcmd, 'RESET'):
            shared._shared_reset_and_poll(gcmd)
        elif flag(gcmd, 'CLEAR'):
            shared._shared_clear_pending()
            shared._shared_last_error = None
            shared._shared_last_action = 'shared state cleared'
            shared._polling = False
            shared._shared_read_deadline = 0.0
            shared.reactor.update_timer(
                shared._poll_timer, shared.reactor.NEVER)
            shared._state.current_uid = None
            shared._state.current_spool = None
            logger.info('[%s]: shared state cleared', shared._name)
            gcmd.respond_info(color(
                'NFC[%s]: shared state cleared' % shared._name))
        elif flag(gcmd, 'PRELOAD_CHECK'):
            shared._shared_preload_check(gcmd)
        elif flag(gcmd, 'PRELOAD_COMMIT'):
            shared._shared_preload_commit(gcmd)
        elif flag(gcmd, 'PRELOAD_CLEAR_ASSIGNED'):
            shared._shared_preload_clear_assigned(gcmd)
        elif flag(gcmd, 'CANCEL'):
            shared._shared_clear_pending()
            shared._shared_last_error = None
            shared._shared_last_action = 'pending spool canceled'
            shared._polling = False
            shared._shared_read_deadline = 0.0
            shared.reactor.update_timer(
                shared._poll_timer, shared.reactor.NEVER)
            logger.info('[%s]: pending spool canceled', shared._name)
            gcmd.respond_info(color(
                'NFC[%s]: pending spool canceled' % shared._name))
        elif flag(gcmd, 'POLL'):
            if shared._is_printing():
                logger.warning(
                    '[%s]: shared poll skipped while printing', shared._name)
                gcmd.respond_info(
                    '[WARN] NFC[%s]: shared poll skipped while printing'
                    % shared._name)
                return
            shared._poll()
            status = shared.shared_status_line().strip()
            logger.info(
                '[%s]: shared POLL=1 complete — %s', shared._name, status)
            gcmd.respond_info(color(
                'NFC[%s]: one poll complete; %s' % (shared._name, status)))
        elif flag(gcmd, 'SCAN'):
            shared._manual_scan(gcmd)
        elif flag(gcmd, 'INIT'):
            shared._manual_init(gcmd)
        elif flag(gcmd, 'LED_TEST'):
            shared._shared_play_tag_read_effect(
                gcmd, duration=shared._shared_read_effect_duration)
        elif flag(gcmd, 'CLEAR_CACHE'):
            shared._shared_clear_cache(gcmd)
        else:
            shared._shared_help(gcmd)
