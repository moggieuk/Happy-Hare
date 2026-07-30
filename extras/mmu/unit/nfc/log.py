# klippy/extras/mmu/unit/nfc/log.py
#
# The one logging surface for the NFC/RFID layer. Every driver, mmu_nfc_reader and
# tag_parser go through here, so channel names and levels are decided in one place.
#
# Channels
# ────────
#   mmu_rfid.reader      readers and their chip drivers
#   mmu_rfid.tag_parser  tag decoding
#
# Klipper runs the root logger at INFO (queuelogger.setup_bg_logging), so all
# logger calls are info or above.
#
# Every line is prefixed with its channel name - see _ChannelPrefix for why that is
# done here rather than with a formatter.
#
# Which to call
# ─────────────
#   trace()    per-transaction detail; needs debug: 4. Frame hex, poll results, state
#              transitions - the things you want when a reader will not talk.
#   info()     events worth keeping unconditionally: a port opened, a chip woke.
#   warning()  something is wrong but the reader carries on.
#   error()    the operation failed.
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

_trace = False


def enable_trace():
    """
    Turn on trace() output. Called when any reader is configured debug: 4.

    One-way on purpose. Readers share these channels, so a reader left at debug: 0
    must not switch off tracing that another reader asked for. A restart clears it,
    which is the only reset that matters.
    """
    global _trace
    _trace = True


def trace_enabled():
    return _trace


def trace(msg, *args, **kwargs):
    """
    Per-transaction detail, at INFO. Silent unless a reader set debug: 4.
    """
    if _trace:
        logger.info(msg, *args, **kwargs)


def info(msg, *args, **kwargs):
    logger.info(msg, *args, **kwargs)


def warning(msg, *args, **kwargs):
    logger.warning(msg, *args, **kwargs)


def error(msg, *args, **kwargs):
    logger.error(msg, *args, **kwargs)


# -- tag_parser channel -------------------------------------------------------

def tag_trace(msg, *args, **kwargs):
    """
    The gate matters more here than in the drivers: these calls are not guarded by a
    per-instance debug level, and several sit inside per-block loops. Promoted to
    unconditional INFO they would log a dozen-plus lines every time a tag failed one
    vendor's format on the way to matching another.
    """
    if _trace:
        tag_logger.info(msg, *args, **kwargs)


def tag_info(msg, *args, **kwargs):
    tag_logger.info(msg, *args, **kwargs)


def tag_warning(msg, *args, **kwargs):
    tag_logger.warning(msg, *args, **kwargs)


def tag_error(msg, *args, **kwargs):
    tag_logger.error(msg, *args, **kwargs)
