# klippy/extras/mmu/unit/nfc/log.py
#
# The one logging surface for the NFC/RFID layer. Every driver, mmu_nfc_reader and
# tag_parser go through here, so channel names and levels are decided in one place.
#
# Channels
# ────────
#   mmu_rfid.reader      readers and their chip drivers
#   mmu_rfid.tag_parser  tag_parser's pycryptodome availability notice. Decode detail
#                        reaches mmu_rfid.reader via the reader's parse trace callback
#
# Klipper runs the root logger at INFO (queuelogger.setup_bg_logging), so all
# logger calls are info or above.
#
# Every line is prefixed with its channel name - see _ChannelPrefix for why that is
# done here rather than with a formatter.
#
# Which to call
# ─────────────
#   info()     events worth keeping unconditionally: a port opened, a chip woke.
#   warning()  something is wrong but the reader carries on.
#   error()    the operation failed.
#
# Per-transaction detail (frame hex, poll results, state transitions) is info() behind the
# calling driver's own level: `if self._debug >= 4: logger.info(...)`.
#
# Every driver line carries "[<name> <chip>] " - the reader's [mmu_nfc_reader <name>]
# section name, which is stable for the life of the printer. The channel name above is
# stamped on separately by _ChannelPrefix, so do not repeat it in a format string.
#
# Consequence worth knowing when picking a level: a failure logged at warning is
# ungated and reaches every user, so reserve it for something they can act on. A
# failure the driver recovers from on the next line belongs at info behind a guard -
# a warning that only appears at debug: 3 is neither a warning nor a trace.
#
# Note the MANAGER (mmu_nfc_manager) deliberately does NOT use this module. It logs
# through self.mmu.log_* so its messages reach mmu.log with the rest of the MMU's
# narrative; this module is for the hardware layer, which belongs in klippy.log.

import logging

READER_CHANNEL     = 'mmu_rfid.reader'
TAG_PARSER_CHANNEL = 'mmu_rfid.tag_parser'

logger     = logging.getLogger(READER_CHANNEL)
tag_logger = logging.getLogger(TAG_PARSER_CHANNEL)


class _ChannelPrefix(logging.Filter):
    """
    Stamp the channel name onto every record logged on it.

    Needed because klipper owns the handler and strips the formatter

    Rewrites record.msg (the format string) and leaves record.args alone, so %-style
    formatting still happens at handler time. Guarded so a record cannot be prefixed
    twice if it is ever passed through more than once.
    """

    def __init__(self, channel):
        logging.Filter.__init__(self)
        self._prefix = '%s: ' % channel

    def filter(self, record):
        if not getattr(record, '_mmu_rfid_prefixed', False):
            record.msg = self._prefix + str(record.msg)
            record._mmu_rfid_prefixed = True
        return True


logger.addFilter(_ChannelPrefix(READER_CHANNEL))
tag_logger.addFilter(_ChannelPrefix(TAG_PARSER_CHANNEL))


def info(msg, *args, **kwargs):
    logger.info(msg, *args, **kwargs)


def warning(msg, *args, **kwargs):
    logger.warning(msg, *args, **kwargs)


def error(msg, *args, **kwargs):
    logger.error(msg, *args, **kwargs)
