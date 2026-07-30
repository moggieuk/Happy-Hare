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
# Every driver line carries "[<name> <chip>] " - the reader's [mmu_nfc_reader <name>]
# section name, which is stable for the life of the printer. The channel name above is
# stamped on separately by _ChannelPrefix, so do not repeat it in a format string.
#
# Two gating traps - both of these look redundant and are not
# ──────────────────────────────────────────────────────────
# 1. Drivers wrap trace() calls in `if self._debug >= 4:` even though trace() checks a
#    flag itself. Not redundant: enable_trace() below is a MODULE-GLOBAL one-way latch
#    that ANY reader at debug: 4 opens for every reader. The per-instance guard is what
#    keeps a reader at debug: 0 quiet while another is being traced. Keep both.
# 2. `if self._debug >= 3: logger.info(...)` sites are NOT trace() sites waiting to be
#    converted. debug: 3 must show them, and trace() needs 4 - and needs it from some
#    reader on the machine. Converting one loses the message entirely at debug: 3.
#    Same for PN7160Handler._debug(), whose flag is `debug >= 4 or pn7160_debug`:
#    routing it through trace() would silently break pn7160_debug on its own.
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
    The trace latch matters more here than in the drivers: these calls have no
    per-instance debug level to guard them (tag_parser has no reader to ask), and
    several sit inside per-block loops. Promoted to unconditional INFO they would log
    a dozen-plus lines every time a tag failed one vendor's format on the way to
    matching another.
    """
    if _trace:
        tag_logger.info(msg, *args, **kwargs)


def tag_info(msg, *args, **kwargs):
    tag_logger.info(msg, *args, **kwargs)


def tag_warning(msg, *args, **kwargs):
    tag_logger.warning(msg, *args, **kwargs)


def tag_error(msg, *args, **kwargs):
    tag_logger.error(msg, *args, **kwargs)
