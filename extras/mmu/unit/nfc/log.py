# klippy/extras/mmu/mmu_nfc_log.py
#
# EMU NFC Gate Reader — dedicated logger
# Version 1.0.0  |  2026-04-14
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ─────────────────────────────────────────────────────────────────────────────
# Dedicated logger for all NFC gate modules.
#
# All MMU NFC output goes to nfc_reader.log (same directory as
# klippy.log).  WARNING and ERROR records automatically also appear in
# klippy.log via _KlippyForwardHandler.  INFO and DEBUG stay in nfc_reader.log
# only.  Optional UI console output is configured by NFC_manager after reading
# printer.cfg.
#
# Debug levels (set via debug = N in printer.cfg):
#   0  none           — nothing logged anywhere
#   1  errors         — ERROR  → nfc_reader.log + klippy.log
#   2  warnings       — WARNING+ → nfc_reader.log + klippy.log  (default)
#   3  integration    — Spoolman / Happy Hare events → nfc_reader.log only
#   4  trace          — full poll + driver detail → nfc_reader.log only
#
# Usage (from any module in this package):
#   from .mmu_nfc_log import logger

import datetime
import logging
import os
import re

_LOGGER_NAME = 'nfc_gate'
_LOG_FILENAME = 'nfc_reader.log'
_CONSOLE_HANDLER_NAME = 'nfc_gate_console'
_LEVELS = {
    'off': logging.CRITICAL + 1,
    '0': logging.CRITICAL + 1,  # 0 = no logging
    'error': logging.ERROR,
    '1': logging.ERROR,          # 1 = errors only
    'warning': logging.WARNING,
    'warn': logging.WARNING,
    '2': logging.WARNING,        # 2 = warnings and errors
    'info': logging.INFO,
    '3': logging.INFO,           # 3 = info, warnings, and errors
    'debug': logging.DEBUG,
    '4': logging.DEBUG,          # 4 = everything (verbose)
}

_console_gcode = None
_console_reactor = None
_console_enabled = False
_console_level = logging.WARNING


def _normalise_level(level, default=logging.WARNING):
    if isinstance(level, int):
        return {
            0: logging.CRITICAL + 1,  # 0 = no logging
            1: logging.ERROR,          # 1 = errors only
            2: logging.WARNING,        # 2 = warnings and errors
            3: logging.INFO,           # 3 = info, warnings, and errors
            4: logging.DEBUG,          # 4 = everything (verbose)
        }.get(level, default)
    return _LEVELS.get(str(level).strip().lower(), default)


def _format_record_message(record):
    try:
        return record.getMessage()
    except Exception:
        return str(record.msg)


def _quote_respond_value(value):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return value.replace('\n', '\\n')


def color_console_tags(text):
    """Wrap console bracket tags with HTML color spans for the Klipper UI."""
    text = str(text)
    text = re.sub(r'\bNFC(?=\[)', '<span style="color:#4FC3F7">NFC</span>', text)
    text = re.sub(r'^NFC ', '<span style="color:#4FC3F7">NFC</span> ', text)
    text = text.replace('[WARN]',   '<span style="color:#FFFF00">[WARN]</span>')
    text = text.replace('[OK]',     '<span style="color:#90EE90">[OK]</span>')
    text = text.replace('[ERROR]',  '<span style="color:#FF6060">[ERROR]</span>')
    text = text.replace('[SCAN]',   '<span style="color:#FFA040">[SCAN]</span>')
    text = text.replace('[MOVE]',   '<span style="color:#FFA040">[MOVE]</span>')
    text = text.replace('[REWIND]', '<span style="color:#90EE90">[REWIND]</span>')
    return text


def _nfc_console_message(message, level_marker=None):
    message = str(message)
    if level_marker and message.startswith(level_marker):
        return color_console_tags(message)
    match = re.match(r'^\[([^\]]+)\]:\s*(.*)$', message)
    if match:
        message = "NFC[%s]: %s" % (match.group(1), match.group(2))
    if level_marker:
        message = "%s %s" % (level_marker, message)
    return color_console_tags(message)


def _warn_console_message(message):
    return _nfc_console_message(message, "[WARN]")


def _error_console_message(message):
    return _nfc_console_message(message, "[ERROR]")


def _respond_prefixed(message, levelno):
    if levelno >= logging.ERROR:
        rendered = _error_console_message(message)
    elif levelno >= logging.WARNING:
        rendered = _warn_console_message(message)
    else:
        rendered = _nfc_console_message(message)
    if hasattr(_console_gcode, 'respond_info'):
        _console_gcode.respond_info(rendered)
        return
    if levelno >= logging.ERROR:
        if hasattr(_console_gcode, 'respond'):
            _console_gcode.respond(rendered)
        elif hasattr(_console_gcode, 'respond_raw'):
            _console_gcode.respond_raw(rendered)


def _respond_to_console(record):
    """
    Send selected NFC log messages to the Klipper console.

    Info/warning output is controlled by console_output + console_log_level.
    Errors are always sent once a gcode object is configured, matching the
    troubleshooting behavior we want during hardware bring-up.
    """
    global _console_gcode, _console_reactor
    if getattr(record, 'nfc_no_console', False):
        return
    if _console_gcode is None:
        return
    if record.levelno < logging.INFO:
        return
    if record.levelno < logging.ERROR:
        if not _console_enabled or record.levelno < _console_level:
            return

    msg = _format_record_message(record)

    def _send(_eventtime=None, message=msg, levelno=record.levelno):
        try:
            _respond_prefixed(message, levelno)
        except Exception:
            # Never allow UI notification failure to recurse through logging.
            pass

    if _console_reactor is not None:
        try:
            _console_reactor.register_callback(_send)
            return
        except Exception:
            pass
    _send()


class _GCodeConsoleHandler(logging.Handler):
    name = _CONSOLE_HANDLER_NAME

    def emit(self, record):
        _respond_to_console(record)


_ARCHIVE_RE = re.compile(r'nfc_reader\.log\.(\d{4}-\d{2}-\d{2})$')
_MAX_ARCHIVES = 7
_MAX_ARCHIVE_DAYS = 7


def _get_log_date(path):
    """Return the date of the first log entry in *path*, or None.

    Reads the first non-empty line and parses the leading YYYY-MM-DD stamp
    written by our formatter.  Using the log content (rather than filesystem
    mtime/ctime) is reliable on Linux where ctime is not a creation timestamp.
    Returns None if the file is empty, unreadable, or has an unexpected format.
    """
    try:
        with open(path, 'rb') as f:
            for _ in range(10):          # skip any leading blank lines
                raw = f.readline()
                if not raw:
                    return None          # EOF — empty file
                line = raw.decode('utf-8', errors='replace').strip()
                if line:
                    # format: "YYYY-MM-DD HH:MM:SS LEVEL    message"
                    return datetime.date.fromisoformat(line[:10])
    except Exception:
        pass
    return None


def _prune_old_archives(log_dir):
    """Delete archives beyond _MAX_ARCHIVES or older than _MAX_ARCHIVE_DAYS.

    Scans *log_dir* for files matching nfc_reader.log.YYYY-MM-DD, sorts them
    newest-first, and removes any that are either past position _MAX_ARCHIVES
    in that list or whose date is more than _MAX_ARCHIVE_DAYS days ago.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=_MAX_ARCHIVE_DAYS)
    archives = []
    try:
        for name in os.listdir(log_dir):
            m = _ARCHIVE_RE.match(name)
            if m:
                try:
                    d = datetime.date.fromisoformat(m.group(1))
                    archives.append((d, os.path.join(log_dir, name)))
                except ValueError:
                    pass
    except OSError:
        return

    archives.sort(key=lambda x: x[0], reverse=True)   # newest first

    for i, (d, path) in enumerate(archives):
        if i >= _MAX_ARCHIVES or d < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def _find_klipper_log_dir():
    """
    Return the directory that klippy.log lives in by inspecting the root
    logger's FileHandler(s).  Falls back to ~/printer_data/logs if none found.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return os.path.dirname(os.path.abspath(handler.baseFilename))
    return os.path.expanduser('~/printer_data/logs')


_LOG_FORMATTER = logging.Formatter(
    '%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')


class _DateRotatingFileHandler(logging.FileHandler):
    """FileHandler that rotates nfc_reader.log at the first write after midnight.

    On every emit() the current date is compared against the date of the first
    log entry.  When the day has advanced the open file is closed, renamed to
    nfc_reader.log.YYYY-MM-DD (using the date from its first entry), old
    archives are pruned, and a fresh file is opened.

    Rotation happens on day advance only — not on restart.  If Klipper runs
    continuously across midnight the first write after midnight triggers the
    rename.  If Klipper restarts on the same day the existing file is reused.
    """

    def __init__(self, path):
        # Perform a startup rotation if the file already contains stale entries
        # (Klipper restarted the same day we already have a previous-day file).
        self._do_rotate_if_stale(path)
        # Prune at every startup so archives don't accumulate when Klipper
        # restarts before midnight (which skips the midnight-crossing _rotate
        # path that was the only previous prune trigger).
        _prune_old_archives(os.path.dirname(os.path.abspath(path)))
        super(_DateRotatingFileHandler, self).__init__(path, mode='a',
                                                       encoding='utf-8',
                                                       delay=False)
        self._log_path = path
        self._current_day = datetime.date.today()

    # ── public ────────────────────────────────────────────────────────────────

    def emit(self, record):
        today = datetime.date.today()
        if today != self._current_day:
            self._rotate(self._current_day)
            self._current_day = today
        super(_DateRotatingFileHandler, self).emit(record)

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _do_rotate_if_stale(log_path):
        """Rename *log_path* at startup if its first entry is from a prior day."""
        if not os.path.exists(log_path):
            return
        log_date = _get_log_date(log_path)
        if log_date is None or log_date >= datetime.date.today():
            return
        archive = '{}.{}'.format(log_path, log_date.isoformat())
        try:
            os.rename(log_path, archive)
        except OSError:
            pass  # Cannot rotate — keep writing to the existing file

    def _rotate(self, rotate_date):
        """Close the current stream, rename the file, prune archives, reopen."""
        try:
            self.stream.flush()
            self.stream.close()
        except Exception:
            pass
        self.stream = None

        archive = '{}.{}'.format(self._log_path, rotate_date.isoformat())
        try:
            os.rename(self._log_path, archive)
        except OSError:
            pass  # Cannot rename — new writes will append to the existing file

        _prune_old_archives(os.path.dirname(self._log_path) or '.')

        try:
            self.stream = self._open()
        except Exception:
            pass


class _KlippyForwardHandler(logging.Handler):
    """Forward WARNING and ERROR records to klippy.log (the root logger).

    Attached to the nfc_gate logger so that any logger.warning() or
    logger.error() call anywhere in the MMU NFC package automatically
    appears in klippy.log without requiring explicit log_both() calls.
    INFO and DEBUG records stay in nfc_reader.log only.
    """

    def emit(self, record):
        if record.levelno < logging.WARNING:
            return
        try:
            logging.getLogger().handle(record)
        except Exception:
            self.handleError(record)


def _build_logger():
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger  # Already configured (e.g. reloaded config)

    log_path = os.path.join(_find_klipper_log_dir(), _LOG_FILENAME)

    fh = _DateRotatingFileHandler(log_path)
    fh.setFormatter(_LOG_FORMATTER)

    logger.addHandler(fh)
    logger.addHandler(_KlippyForwardHandler())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    return logger


def configure(path='', printer=None, console_output=None, console_log_level=None):
    """
    Redirect the NFC logger to *path*.

    Called from NFCGate.__init__ after reading log_file from config.
    Replaces the existing FileHandler so the configured path takes effect
    even though the logger was created at import time.
    Expands ~ automatically.  If *path* is a bare filename (no directory
    component), it is placed in the same directory as klippy.log.
    """
    if path:
        expanded = os.path.expanduser(path)
        if not os.path.dirname(expanded):
            expanded = os.path.join(_find_klipper_log_dir(), expanded)
        _lg = logging.getLogger(_LOGGER_NAME)
        for h in _lg.handlers[:]:
            if isinstance(h, logging.FileHandler):
                _lg.removeHandler(h)
                h.close()
        _lg.propagate = False
        fh = _DateRotatingFileHandler(expanded)
        fh.setFormatter(_LOG_FORMATTER)
        _lg.addHandler(fh)

    if (printer is not None or console_output is not None or
            console_log_level is not None):
        configure_console(printer, console_output, console_log_level)


def configure_console(printer=None, enabled=None, level=None):
    """
    Configure optional Fluidd/Mainsail console output.

    Errors are always sent to the console once *printer* is available.  Info
    and warning records are sent only when *enabled* is true and the record
    level is at or above *level*.
    """
    global _console_gcode, _console_reactor, _console_enabled, _console_level

    if printer is not None:
        try:
            _console_gcode = printer.lookup_object('gcode')
        except Exception:
            _console_gcode = None
        try:
            _console_reactor = printer.get_reactor()
        except Exception:
            _console_reactor = None
    if enabled is not None:
        _console_enabled = bool(enabled)
    if level is not None:
        _console_level = _normalise_level(level, _console_level)

    _lg = logging.getLogger(_LOGGER_NAME)
    for h in _lg.handlers:
        if isinstance(h, _GCodeConsoleHandler):
            return
    _lg.addHandler(_GCodeConsoleHandler())


# Thin wrappers kept for call-site compatibility.
# WARNING and ERROR automatically reach klippy.log via _KlippyForwardHandler.
# INFO stays in nfc_reader.log only unless a call site uses info_both().

def log_both(level, msg, *args, **kwargs):
    getattr(logger, level)(msg, *args, **kwargs)


def info(msg, *args, **kwargs):
    logger.info(msg, *args, **kwargs)


def warning(msg, *args, **kwargs):
    logger.warning(msg, *args, **kwargs)


def error(msg, *args, **kwargs):
    logger.error(msg, *args, **kwargs)


# Module-level singleton — imported by every nfc_gate* module.
logger = _build_logger()


def info_both(msg, *args):
    """Log an INFO event to nfc_reader.log and klippy.log."""
    logger.info(msg, *args)
    try:
        if args:
            msg = msg % args
        record = logger.makeRecord(_LOGGER_NAME, logging.INFO, __file__, 0,
                                   msg, (), None)
        logging.getLogger().handle(record)
    except Exception:
        pass
