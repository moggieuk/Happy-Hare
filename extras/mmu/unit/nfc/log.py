# klippy/extras/mmu/unit/nfc/log.py
#
# NFC Gate Reader — logging bridge to the standard MMU logger
# Version 1.0.0
# Copyright (C) 2026  WoodWorker
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ─────────────────────────────────────────────────────────────────────────────
# NFC logging is routed through the standard Happy Hare MMU logging layer
# (the `mmu` object's log_debug/log_info/log_warning/log_error). All NFC output
# therefore lands in mmu.log — owned and rotated by MmuLogger — and on the
# Klipper console, exactly like every other MMU component. There is no separate
# nfc_reader.log and no second rotating file handler.
#
# The log file location is not rediscovered here: MmuLogger derives mmu.log from
# printer.start_args['log_file'] and owns the handler; this module simply
# discovers the active `mmu` object and forwards records to it. Every message is
# prefixed "NFC" so it is identifiable in the shared log.
#
# The module-level `logger` is an adapter with the usual .debug/.info/.warning/
# .error/.exception surface, so existing call sites are unchanged.

import logging
import re
import traceback


# ─────────────────────────────────────────────────────────────────────────────
# MMU discovery + logging adapter
# ─────────────────────────────────────────────────────────────────────────────

_NFC_PREFIX = 'NFC'
_printer = None
_mmu = None

# NFC-specific log levels — independent of Happy Hare's global log_level /
# log_file_level so the reader/tag can be debugged without turning on all MMU
# debug output. Set from the reader/gate config via configure():
#   _file_level      ← 'debug'             (what NFC writes to mmu.log)
#   _console_enabled ← 'console_output'    (whether NFC echoes to the console)
#   _console_level   ← 'console_log_level' (console verbosity cap)
# Levels: 0=off, 1=error, 2=warning, 3=info, 4=debug.
_file_level = 2
_console_enabled = False
_console_level = 2

_LEVELS = {
    'off': 0, 'none': 0, '0': 0,
    'error': 1, '1': 1,
    'warn': 2, 'warning': 2, '2': 2,
    'info': 3, '3': 3,
    'debug': 4, 'trace': 4, '4': 4,
}
# Severity rank per level name (lower rank = more severe = shown at lower levels).
_SEVERITY = {'error': 1, 'exception': 1, 'warning': 2, 'info': 3, 'debug': 4}


def _normalise_level(value, default):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, min(value, 4))
    return _LEVELS.get(str(value).strip().lower(), default)


def bind_printer(printer):
    """Remember the printer so the active `mmu` object can be discovered."""
    global _printer
    if printer is not None:
        _printer = printer
    _resolve_mmu()


def _resolve_mmu():
    """Discover the MMU controller whose MmuLogger owns mmu.log (lazy)."""
    global _mmu
    if _mmu is None and _printer is not None:
        _mmu = _printer.lookup_object('mmu', None)
    return _mmu


def _format(msg, args):
    if not args:
        return str(msg)
    try:
        return str(msg) % args
    except Exception:
        return str(msg)


def _prefixed(text):
    text = str(text)
    if text.lstrip().upper().startswith(_NFC_PREFIX):
        return text
    return '%s %s' % (_NFC_PREFIX, text)


class _MmuLogAdapter:
    """Route NFC log calls to mmu.log and the Klipper console.

    Messages are gated on the NFC-specific levels (_file_level / _console_*),
    then written straight to the MMU log file (mmu.log_to_file) and console
    (mmu.gcode.respond_info) — deliberately NOT through mmu.log_debug/info/...,
    which would re-gate on Happy Hare's global levels. This keeps NFC output in
    the shared mmu.log while letting its verbosity be controlled independently.

    Console policy mirrors Happy Hare: errors always show; warning/info show when
    console_output is on and within console_log_level; debug never hits console.
    Before the `mmu` object exists (early config load) file records fall back to
    the shared 'mmu' Python logger, which MmuLogger wires to mmu.log.

    Accepts and ignores logging kwargs (e.g. extra=) for call-site compatibility.
    """

    def _emit(self, level, msg, args, exc=False):
        rank = _SEVERITY[level]
        show_file = 0 < rank <= _file_level
        if rank <= 1:            # error / exception
            show_console = True
        elif rank >= 4:          # debug
            show_console = False
        else:                    # warning / info
            show_console = _console_enabled and rank <= _console_level
        if not (show_file or show_console):
            return

        text = _prefixed(_format(msg, args))
        if exc:
            text = '%s\n%s' % (text, traceback.format_exc())

        mmu = _resolve_mmu()
        if mmu is None:
            # Pre-init fallback: shared 'mmu' logger -> mmu.log (or klippy.log).
            if show_file:
                getattr(logging.getLogger('mmu'),
                        'error' if exc else level)(text)
            return

        if show_file:
            mmu.log_to_file(text)
        if show_console:
            try:
                mmu.gcode.respond_info(color_console_tags(text))
            except Exception:
                pass

    def debug(self, msg, *args, **kwargs):
        self._emit('debug', msg, args)

    def info(self, msg, *args, **kwargs):
        self._emit('info', msg, args)

    def warning(self, msg, *args, **kwargs):
        self._emit('warning', msg, args)

    def error(self, msg, *args, **kwargs):
        self._emit('error', msg, args)

    def exception(self, msg, *args, **kwargs):
        self._emit('exception', msg, args, exc=True)


# Module-level singleton — imported by every nfc gate module.
logger = _MmuLogAdapter()


def configure(printer=None, console_output=None, console_log_level=None,
              file_level=None):
    """Bind the MMU object and set the NFC-specific log levels.

    NFC output is written to mmu.log and the Klipper console, but gated on these
    NFC levels rather than Happy Hare's global log_level / log_file_level, so a
    reader/tag can be debugged without enabling all MMU debug output.
      printer           discovers the active `mmu` object
      file_level        ← reader/gate 'debug'   (0..4, what reaches mmu.log)
      console_output    ← 'console_output'      (console echo on/off)
      console_log_level ← 'console_log_level'   (console verbosity cap)
    """
    global _file_level, _console_enabled, _console_level
    bind_printer(printer)
    if file_level is not None:
        _file_level = _normalise_level(file_level, _file_level)
    if console_output is not None:
        _console_enabled = bool(console_output)
    if console_log_level is not None:
        _console_level = _normalise_level(console_log_level, _console_level)


def configure_console(printer=None, enabled=None, level=None):
    """Set just the NFC console output preferences (and bind the printer)."""
    configure(printer=printer, console_output=enabled, console_log_level=level)


def info_both(msg, *args):
    """Compatibility alias for an INFO record."""
    logger.info(msg, *args)


# ─────────────────────────────────────────────────────────────────────────────
# Console color tags — precompiled NFC bracket-tag substitutions
#
# NFC console messages use bracket tags ([WARN], [OK], NFC[...], ...), not Happy
# Hare's {0}..{6}/{{RRGGBB}} markers, so only these are handled. Coloring honors
# the MMU's console_show_colored_text preference when the mmu object is available.
# ─────────────────────────────────────────────────────────────────────────────

# NFC bracket tags, compiled once.
_NFC_TAG_SUBS = (
    (re.compile(r'\bNFC(?=\[)'), '<span style="color:#4FC3F7">NFC</span>'),
    (re.compile(r'^NFC '),       '<span style="color:#4FC3F7">NFC</span> '),
    (re.compile(r'\[WARN\]'),    '<span style="color:#FFFF00">[WARN]</span>'),
    (re.compile(r'\[OK\]'),      '<span style="color:#90EE90">[OK]</span>'),
    (re.compile(r'\[ERROR\]'),   '<span style="color:#FF6060">[ERROR]</span>'),
    (re.compile(r'\[SCAN\]'),    '<span style="color:#FFA040">[SCAN]</span>'),
    (re.compile(r'\[MOVE\]'),    '<span style="color:#FFA040">[MOVE]</span>'),
    (re.compile(r'\[REWIND\]'),  '<span style="color:#90EE90">[REWIND]</span>'),
)


def color_console_tags(text):
    """Wrap NFC bracket tags in HTML color spans for the Klipper UI.

    Applies the precompiled NFC tag patterns. When the MMU has colored console
    text disabled, the tags are left as plain text.
    """
    text = str(text)

    mmu = _resolve_mmu()
    if mmu is not None and not getattr(mmu.p, 'console_show_colored_text', True):
        return text

    for pattern, repl in _NFC_TAG_SUBS:
        text = pattern.sub(repl, text)

    return text
