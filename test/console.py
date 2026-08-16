# Happy Hare interactive console - a Mainsail-like console for the test harness.
#
# Type real MMU commands, see what the real MmuController prints back. No printer, no
# Klipper, no hardware. Everything under test/hh/ that the unit tests drive, driven by
# hand instead:
#
#   make console
#   mmu> MMU_STATUS
#   mmu> MMU_CHANGE_TOOL TOOL=1
#
# WHY THIS IS THIN. The harness already had the three pieces that would otherwise be the
# whole job: Session.run_gcode() parses and dispatches a raw command string, GCodeDispatch
# accumulates console/errors/raw, and session().boot() reaches a live MMU in two lines.
# This module is plumbing plus a renderer.
#
# THE CLOCK IS VIRTUAL AND FROZEN WHILE YOU TYPE. VirtualReactor.monotonic() only moves
# inside a dispatch or an explicit advance() (test/hh/klippy_root/reactor.py:204,253,296),
# so no timer fires at the prompt and nothing races stdin. That is why blocking on input()
# is safe, and why '/advance N' exists: without it you never see anything time-driven
# (the 8s boot LED rainbow, the 20s pending-spool timeout).
#
# COMMANDS ARE DISPATCHED INSIDE THE REACTOR, not at top level - see _dispatch().
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import argparse
import collections
import contextlib
import logging
import os
import re
import select
import sys
import time

# The fakes log at debug/warning through the root logger and every test file quiets it at
# import (e.g. test/test_mmu_bootup.py:30). A module that imports test.hh directly does
# not, and the output sprays across the prompt. Must happen before test.hh is imported.
logging.getLogger().setLevel(logging.CRITICAL)

if __package__ in (None, ''):                       # allow `python test/console.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.hh import session as hh_session           # noqa: E402

# -- optional readline, mirroring utils/simulator.py -------------------------------
HAVE_READLINE = False
try:
    import readline
    HAVE_READLINE = True
except Exception:                                   # pragma: no cover - Windows
    try:
        import pyreadline as readline               # type: ignore
        HAVE_READLINE = True
    except Exception:
        readline = None

HISTORY_FILE = os.path.expanduser('~/.hh_console_history')

# -- optional raw keyboard, for the scrollback pager -------------------------------
HAVE_RAWKEY = False
try:
    import termios
    import tty
    HAVE_RAWKEY = True
except Exception:                                   # pragma: no cover - Windows
    termios = tty = None

# Where a preloaded filament tip is placed: past the entry switch at -50, which is the
# precondition Happy Hare requires before a preload can start (test/README.md section 5).
TIP_AT_GATE = -40.0
DEFAULT_TEMP = 220
# Gate availability values accepted by MMU_GATE_MAP. These stay local because importing
# production `extras` before hh_session installs fake Klipper contaminates module resolution.
GATE_EMPTY = 0
GATE_AVAILABLE = 1
GATE_AVAILABLE_FROM_BUFFER = 2


####################
##### Renderer #####
####################

# MmuLogger._color_message emits HTML, not ANSI (extras/mmu/mmu_logger.py:96-118) because
# console_show_colored_text defaults to 1. Translating it is what makes the output look
# like Mainsail rather than a wall of tags.
#
# 3 to 8 hex digits, NOT 6: that is what HH's own CONSOLE_COLOR_SPAN_RE accepts
# (mmu_logger.py:22), so a {{F0A}} or {{RRGGBBAA}} token really does reach us. Requiring
# exactly 6 left the literal tag sitting in the output.
_SPAN_OPEN = re.compile(r'<span style="color:#([0-9A-Fa-f]{3,8})"\s*>')
# Tolerant of attributes, so the plain path strips any span form rather than only a bare one
_TAG = re.compile(r'</?span[^>]*>|</?b\s*>', re.I)
RESET = '\033[0m'
UI_SPACE = ' '                                 # extras/mmu/mmu_constants.py:323


def _hex_to_rgb(digits):
    """HH allows 3-8 hex digits. Expand shorthand, ignore any alpha, never crash."""
    if len(digits) in (3, 4):                       # #RGB / #RGBA
        digits = ''.join(c * 2 for c in digits)
    digits = (digits + '000000')[:6]                # 5 and 7 are malformed; tolerate them
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_256(r, g, b):
    """Nearest xterm-256 index: the 6x6x6 cube, or the greyscale ramp when near-grey."""
    if abs(r - g) < 11 and abs(g - b) < 11:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 23)
    cube = tuple(0 if v < 48 else 1 if v < 115 else (v - 35) // 40 for v in (r, g, b))
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def truecolor_supported():
    """
    COLORTERM is the de facto signal. Absent it, assume 256-colour.

    This matters more than it looks. A terminal WITHOUT truecolor support does not ignore
    ESC[38;2;R;G;Bm - it parses the channels as independent SGR codes. Happy Hare's warning
    colour #FF69B4 has green 0x69 = 105, and SGR 105 is "bright magenta background", so the
    whole warning came out on a pink background. Any channel landing in 100-107 (or 40-47)
    does that, so truecolor must be opt-in on evidence, not the default.
    """
    return os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')


# The 16 ANSI colours, in SGR order (30-37 then 90-97). Used for nearest-match in '16'
# mode; a per-channel threshold collapsed every pastel to white.
_ANSI16 = ((0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
           (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
           (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
           (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))


def _rgb_to_16(r, g, b):
    """
    Nearest of the 16, weighted toward hue.

    Plain Euclidean distance sends any light colour to white, because #90EE90 really is
    closer to #C0C0C0 than to #00FF00 in RGB space. Scaling both sides to full saturation
    first compares hue instead, which is what a reader actually distinguishes.
    """
    span = max(r, g, b) - min(r, g, b)
    if span < 40:                                   # genuinely grey: pick by lightness
        return 0 if max(r, g, b) < 64 else 8 if max(r, g, b) < 160 else 7 if max(r, g, b) < 224 else 15
    def norm(c):
        lo, hi = min(c), max(c)
        return tuple(255 * (v - lo) // (hi - lo) for v in c)
    want = norm((r, g, b))
    best, bestd = 7, None
    for i, cand in enumerate(_ANSI16):
        if max(cand) - min(cand) < 40:              # skip the greys as hue candidates
            continue
        d = sum((a - b2) ** 2 for a, b2 in zip(want, norm(cand)))
        # Prefer the bright half when the source is bright, so pastels stay legible.
        d += 0 if (max(r, g, b) > 170) == (i >= 8) else 4000
        if bestd is None or d < bestd:
            best, bestd = i, d
    return best


def fg(r, g, b, mode='256'):
    """Foreground SGR for an RGB triple in the requested colour mode."""
    if mode == 'truecolor':
        return '\033[38;2;%d;%d;%dm' % (r, g, b)
    if mode == '16':
        # 30-37 / 90-97 only, so nothing can be misparsed as a background colour.
        idx = _rgb_to_16(r, g, b)
        return '\033[%dm' % ((30 + idx) if idx < 8 else (90 + idx - 8))
    return '\033[38;5;%dm' % _rgb_to_256(r, g, b)


_SGR = re.compile(r'\033\[([0-9;]*)m')
# Any escape sequence, SGR or not. Used by wrap_ansi() to step over one without counting it
# as a column; cursor control and SGR both have to survive a wrap intact.
_CSI = re.compile(r'\033\[[0-9;?]*[ -/]*[@-~]|\033[@-Z\\-_]')


def _sgr_state(text, cur_fg='', bold=False):
    """
    Fold the SGR codes in `text` into the (foreground, bold) state they leave behind.

    Only these two are tracked because they are the only two Happy Hare emits - see
    html_to_ansi(), which maps <span style="color:#..."> and <b> and nothing else.
    """
    for code in _SGR.findall(text):
        if code in ('0', ''):
            cur_fg, bold = '', False
        elif code == '39':
            cur_fg = ''
        elif code == '22':
            bold = False
        elif code == '1':
            bold = True
        elif code.startswith('38;') or (code.isdigit() and (
                30 <= int(code) <= 37 or 90 <= int(code) <= 97)):
            cur_fg = '\033[%sm' % code
    return cur_fg, bold


def wrap_ansi(line, width):
    """
    Split one logical line into display rows of at most `width` VISIBLE columns.

    Escape sequences cost no columns and must never be cut in half, and each continuation row
    has to re-open whatever colour was still active - otherwise a wrapped warning is pink for
    its first row and default for the rest. Same reasoning as _close_open_attributes(), and
    the same state machine, which is why both call _sgr_state().
    """
    if width < 1:
        return [line]
    rows, cur, used = [], [], 0
    cur_fg, bold = '', False
    pos, end = 0, len(line)
    while pos < end:
        if line[pos] == '\033':
            match = _CSI.match(line, pos)
            seq = match.group(0) if match else line[pos]
            cur.append(seq)
            cur_fg, bold = _sgr_state(seq, cur_fg, bold)
            pos += len(seq)
            continue
        if used == width:
            rows.append(''.join(cur) + (RESET if (cur_fg or bold) else ''))
            cur = [cur_fg + ('\033[1m' if bold else '')] if (cur_fg or bold) else []
            used = 0
        cur.append(line[pos])
        used += 1
        pos += 1
    rows.append(''.join(cur) + (RESET if (cur_fg or bold) else ''))
    return rows


def _close_open_attributes(msg):
    """
    Terminate every LINE with a reset and re-open whatever was still active on the next.

    Happy Hare's warnings are single MULTI-LINE messages whose colour span opens before the
    first newline and closes after the last, e.g.

        <span style="color:#FF69B4">Warning: Calibration steps are not complete...
        - Use MMU_CALIBRATE_BOWDEN ...</span>

    That is balanced per message but leaves the colour open across each intermediate line
    break. A bare foreground colour crossing a newline is what turns the terminal pink:
    ESC[2K and scrolling both act with the *current* attributes, so the pinned header and
    the erased rows get repainted in it. Resetting at each line end and re-establishing the
    state afterwards keeps the colour without ever letting it cross a boundary.
    """
    cur_fg, bold, out = '', False, []
    for line in msg.split('\n'):
        prefix = cur_fg + ('\033[1m' if bold else '')
        cur_fg, bold = _sgr_state(line, cur_fg, bold)
        out.append(prefix + line + (RESET if (cur_fg or bold or prefix) else ''))
    return '\n'.join(out)


def html_to_ansi(msg, color=True, mode='256'):
    """HH's console HTML -> ANSI. With color=False, strip the markup instead."""
    msg = msg.replace(UI_SPACE, ' ')
    if not color:
        return _TAG.sub('', msg)
    # 39m/22m rather than a blanket 0m *inside* a line, so a colour nested in a bold span
    # (or the reverse) does not reset its partner.
    msg = _SPAN_OPEN.sub(lambda m: fg(*_hex_to_rgb(m.group(1)), mode=mode), msg)
    msg = (msg.replace('</span>', '\033[39m')
              .replace('<b>', '\033[1m')
              .replace('</b>', '\033[22m'))
    return _close_open_attributes(msg) if '\033[' in msg else msg


def paint(text, code, enabled=True):
    return '\033[%sm%s%s' % (code, text, RESET) if enabled else text


# Marks a line as coming from the SIMULATOR rather than from Happy Hare.
#
# NOT '!'. This console already spells a command that raised '!! ...' and one that does not
# exist '?? ...', so a lone '!' would read as a quieter error rather than as a note. '#' is
# the comment marker in every config and G-code file the reader is already looking at, which
# carries exactly the "not an instruction, just a remark" sense wanted here.
INFO_PREFIX = '# '
# Grey rather than SGR 2 (faint) on purpose: _sgr_state() tracks foreground colours and
# bold, so a grey line re-opens its colour correctly when the pager wraps it. Faint would be
# dropped at the wrap and the continuation rows would come back at full brightness.
INFO_COLOUR = '90'

# How /timestamp renders the virtual clock. Fixed width, so continuation lines can be
# indented to line up under the first - a ragged left edge is worse than no stamp at all.
# Seconds matter here: the virtual clock usually moves in fractions of a second, so at
# minute resolution a whole session reads as one instant.
TIME_FORMAT = '%H:%M:%S'
TIME_COLOUR = '90'

# -- live mode ---------------------------------------------------------------------
#
# How often the virtual clock is nudged forward while you sit at the prompt, and how often
# the header is therefore repainted. Halving this does NOT double the reactor's work: _tick
# advances by the REAL time measured since the last one, not by this constant, so virtual
# seconds per wall second is invariant and only the per-tick overhead (chiefly repaint)
# scales. 0.5s because that is what makes an led_effect animation legible - at frame_rate 24
# a one-second sample showed every 24th frame, which reads as a jump rather than a fade.
LIVE_INTERVAL = 0.5
# Never advance more than this in one tick. A laptop that slept, or a SIGSTOP, would
# otherwise come back to a catch-up of hours - which is both slow and, past about seven
# virtual minutes, over the reactor's iteration cap.
LIVE_MAX_CATCHUP = 5.0
# advance() resets its iteration counter per call, so a long jump that would blow the cap
# in one go succeeds when it is fed in slices. Measured on the default profile: advance(600)
# dies at 444s, the same 600s in 60s slices completes.
ADVANCE_SLICE = 60.0


def raw_stdout():
    """
    The real stream underneath any ScrollbackTee.

    Cursor control must not reach the scrollback buffer - it is not log content, and for the
    pager, which repaints a whole pane per keypress, feeding its own frames back into the
    buffer it is displaying would make the thing grow as you scroll it.

    Resolved at CALL time rather than captured once, so that with no tee installed this is
    simply whatever sys.stdout currently is - including a StringIO under redirect_stdout,
    which is how the header is tested.
    """
    return getattr(sys.stdout, 'raw_stream', sys.stdout)


class ScrollbackTee:
    r"""
    Stands in for sys.stdout while the prompt is running, keeping a copy of every line.

    Needed because a DECSTBM scroll region is NOT backed by the terminal's own scrollback:
    rows that scroll off the top of the region are discarded, not saved, so with a pinned
    header there is nothing to scroll back to. The console has to keep the lines itself.

    print() writes the text and the '\n' as two separate calls, so lines are reassembled here
    rather than assumed to arrive whole.
    """

    def __init__(self, stream, buffer):
        self.raw_stream = stream                    # what raw_stdout() hands back
        self.buffer = buffer
        self._partial = ''

    def write(self, text):
        written = self.raw_stream.write(text)
        self.write_capture(text)
        return written

    def write_capture(self, text):
        """
        Buffer only, no forwarding. Console.echo() uses this for the prompt and the echoed
        command, which the terminal showed but which never came through write().
        """
        if self.buffer is None or not text:
            return
        self._partial += text
        if '\n' in self._partial:
            *done, self._partial = self._partial.split('\n')
            self.buffer.extend(line.rstrip('\r') for line in done)

    def flush(self):
        self.raw_stream.flush()

    def __getattr__(self, name):                    # isatty, encoding, fileno, ...
        # NOT tidiness. input() only takes the readline path when sys.stdout has a real
        # fileno(), isatty() and str encoding/errors. Spell those out as a fixed handful of
        # methods instead and history, tab-completion and the Shift-Up binding all vanish -
        # silently, only on a real terminal, and never in the test suite.
        return getattr(object.__getattribute__(self, 'raw_stream'), name)


# -- opening the pager from the prompt ---------------------------------------------
#
# \001 is ctrl-a and \005 ctrl-e, so the macro jumps to the start of the line, inserts
# '/scroll ', jumps back to the end and submits. That is what lets a half-typed line survive
# as trailing arguments instead of being clobbered - and it has to survive somehow, because
# readline's own set_startup_hook()+insert_text() does not restore it on libedit.
_SCROLL_MACRO = r'\001/scroll \005\n'
# Shift-Up, Shift-PgUp and plain PgUp. Plain Up is deliberately NOT bound: it is history.
_SCROLL_KEYS = (r'\e[1;2A', r'\e[5;2~', r'\e[5~')


def readline_backend():
    """
    'editline' (libedit) or 'readline' (GNU).

    readline.backend only exists on 3.13+, and `make console` picks whichever interpreter it
    finds - klippy-env's or the venv's - so the docstring probe is the working path on older
    ones, not redundant defence.
    """
    if not HAVE_READLINE:
        return 'readline'
    backend = getattr(readline, 'backend', None)
    if backend:
        return backend
    return 'editline' if 'libedit' in (getattr(readline, '__doc__', '') or '') else 'readline'


def scroll_binding_lines(backend, keys=_SCROLL_KEYS, macro=_SCROLL_MACRO):
    """
    parse_and_bind() statements that open the pager, in the dialect the backend speaks.

    The two are not interchangeable and the wrong one is worse than none at all: libedit does
    not understand GNU's '"key": "macro"' form and inserts the tail of the escape sequence as
    literal text, so Shift-Up types ';2A' at the prompt. GNU has no 'bind -s'. Both were
    measured, not assumed.
    """
    if backend == 'editline':
        return ['bind -s "%s" "%s"' % (key, macro) for key in keys]
    return ['"%s": "%s"' % (key, macro) for key in keys]


def header_groups(text, known):
    """
    A --header / '/header' value into the list of groups it names.

    'all' and 'off' are the two ends, so neither has to be typed out as a list. Shared by the
    flag and the meta-command deliberately: they used to parse their own, which is how you
    end up with '/header all' working and '--header all' not.
    """
    text = (text or '').strip().lower()
    if text in ('off', 'none', ''):
        return []
    if text == 'all':
        return list(known)
    groups = [g for g in text.split(',') if g]
    bad = [g for g in groups if g not in known]
    if bad:
        raise ValueError('unknown group(s) %s; known: %s, or all/off'
                         % (','.join(bad), ','.join(known)))
    return groups


def parse_scroll_args(text):
    """
    Split /scroll's arguments into (rows back, recovered text).

    '/scroll 5' is an offset, never a recovery of the literal text '5' - ambiguous by
    construction, resolved in favour of the documented argument.
    """
    text = (text or '').strip()
    match = re.match(r'(\d+)(?:\s+|$)', text)
    if match is None:
        return 0, text
    return int(match.group(1)), text[match.end():].strip()




class Console:
    def __init__(self, args):
        self.args = args
        self.color = not args.plain and sys.stdout.isatty()
        # See truecolor_supported(): guessing truecolor on a terminal without it turns
        # Happy Hare's pink warning into a pink BACKGROUND.
        self.mode = (args.color if args.color != 'auto'
                     else ('truecolor' if truecolor_supported() else '256'))
        self.sink = []                              # ordered (index -> rendered line)
        self.sink_stamp = []                        # ... and the clock when HH said it
        self._printed = 0                           # how much of it has reached the screen
        # Print Happy Hare's output as it arrives rather than after the command. Enabled ONLY
        # around run_command - the path that presents to a user. Startup output belongs to
        # banner(), and _dispatch() is the raw path used by setup and by the tests, which must
        # stay silent.
        self.streaming = False
        self.startup_output = []                    # bootup, incl. the Happy Hare welcome
        self.pinned = None                          # set by interact() when pinning
        self.interactive = False                    # ... and True once its loop is running
        self._can_pin = False                       # ... and whether it may re-pin later
        self.meta_line = ''                         # the current meta-command, unsplit
        self.scroll_keys = False                    # whether Shift-Up/PgUp could be bound
        # Both default ON at a real prompt and OFF otherwise. --script has to stay
        # byte-for-byte reproducible to be usable as a regression tool, and a clock in the
        # output is the one thing guaranteed to differ between two runs of it.
        interactive = sys.stdout.isatty() and not args.script
        self.timestamps = interactive if args.timestamp is None else args.timestamp
        self.live = interactive if args.live is None else args.live
        self.wall_start = time.time()               # what virtual t=0 is called in real time
        self.clock_epoch = None                     # reactor.monotonic() at that moment
        self._ticking = False                       # re-entry guard for the live tick
        self._at_prompt = False                     # ... and the only place a tick may run
        self._last_tick = None
        # Every line that reached the terminal, for the pager. Rendered, not raw: this is
        # what was displayed, which is not the same as self.sink (MMU responses only - no
        # banner, no meta-command output, no prompts).
        self.scrollback = (collections.deque(maxlen=args.scrollback)
                           if args.scrollback else None)
        self.tee = None                             # the ScrollbackTee, while interacting
        self.hh = None
        self.fil = None
        self.running = True
        # Commands that raised. Counted separately from hh.errors because an exception out
        # of the dispatcher never reaches respond_raw, so a parse failure would otherwise
        # leave --script exiting 0.
        self.failures = 0

    # -- lifecycle ------------------------------------------------------------
    def boot(self):
        a = self.args
        log_dir = None if a.no_log else a.log_dir
        if log_dir:
            # A fresh log per run, as asked. Remove rather than truncate so
            # TimedRotatingFileHandler opens a brand new file.
            try:
                os.makedirs(log_dir, exist_ok=True)
                if os.path.exists(os.path.join(log_dir, 'mmu.log')):
                    os.unlink(os.path.join(log_dir, 'mmu.log'))
            except OSError as exc:
                print('!! could not clear the log in %s: %s' % (log_dir, exc))
        self.hh = hh_session(a.profile, virtual_nfc=a.virtual_nfc, log_dir=log_dir)

        # build() BEFORE registering the handler, boot() after. Bootup is where Happy Hare
        # prints its welcome and its calibration warnings, all through cmd_MMU_BOOTUP - so
        # a handler registered after boot() misses the entire startup, which is exactly what
        # it used to do. build() is what creates the gcode object to register on.
        self.hh.build()
        self.hh.gcode.register_output_handler(self._on_output)

        if a.trace:
            self.hh.mmu.p.log_level = a.trace
        if a.plain:
            self.hh.mmu.p.console_show_colored_text = 0

        # Seed calibration INSIDE boot(), before __MMU_BOOTUP runs. Calibrating afterwards
        # (which is what this used to do) left the banner warning "Calibration steps are not
        # complete" about a machine that was calibrated a millisecond later.
        # gates_loaded_at for the same reason as calibrate: __MMU_BOOTUP prints the gate
        # table, and _preload_all() runs AFTER boot() returns - so the banner said every gate
        # was unknown (ERCF) or empty (ViViD, which has per-gate switches) about a machine
        # that is fully preloaded by the time the prompt appears, and that banner is the last
        # thing on screen. Only seeded when the preload is actually going to happen.
        preloading = not (a.no_preload or a.no_calibrate)
        # home= for the same reason as the other two: bootup renders the selector and filament
        # rows, and homing afterwards left the banner showing 'Selct: XXXX' and '[T?]' about a
        # machine a later MMU_STATUS reported as homed with a gate selected.
        self.hh.boot(calibrate=not a.no_calibrate,
                     gates_loaded_at=TIP_AT_GATE if preloading else None,
                     prime=not a.no_prime, seed=a.seed,
                     pre_bootup=self._home_before_bootup)
        self.startup_output = list(self.sink)        # the welcome, shown by banner()
        self._clear_sink()

        # MANDATORY, and the easiest thing to get wrong: the filament model is created
        # lazily and only then installs the move observer (test/hh/bootstrap.py:336).
        # Without it every motion command dies with a misleading "No trigger on ... after
        # full movement" from the fake HomingMove. seed_loaded_gates() has already built it
        # on the preloading path, so this is a plain fetch there - but NOT under
        # --no-preload/--no-calibrate, which is why it stays unconditional.
        self.fil = self.hh.filament()

        # Without this HH auto-heats and reports it through log_error, which lands in the
        # error list and makes a clean session look dirty (bootstrap.py:464).
        self.hh.heat_extruder(a.temp)

        # A PHYSICAL selector needs calibrating and homing before it can select a gate, and an
        # uncalibrated one refuses with "Selector is not clibrated". No-op on a VirtualSelector
        # machine, so this costs the older profiles nothing.
        if preloading:
            self._preload_all()

        # A live fake Moonraker + Spoolman, seeded to agree with the primed gate map. Without
        # it every call Happy Hare makes to Moonraker goes into the void, so an NFC read ends
        # in "Automatic assignment of id timed out" 20s later instead of resolving a spool.
        if not a.no_moonraker:
            self.hh.attach_moonraker(spools=self.hh.spools_for_gate_map())

        # LAST, so startup itself stays instant no matter what pacing is asked for - preloading
        # 13 gates at --pace 1 would otherwise cost minutes of virtual time before the first
        # prompt. /pace changes it live.
        if a.pace:
            self.hh.set_pacing(a.pace, wall=self._wall_pacing())

        # Walk past effect_initialized, the 8s unit-wide flash bootup leaves running - UNLESS
        # the clock is about to run live, in which case let it play out for real instead: that
        # is what a printer's own user sees at power-on, transient flashes are correctly
        # DROPPED for that window on real hardware too (mmu_led_manager.py:473), and skipping
        # it here is why the effect was never visible at the prompt. Without --live (a frozen
        # clock - the reproducible mode, and every script/pipe) nothing else ever advances the
        # clock, so that session would sit in the window for good without this.
        if not self.live:
            self.hh.settle_leds()

        # Anchor the virtual clock LAST, so /timestamp reads "now" at the first prompt
        # rather than a few virtual seconds into the past. Read from the reactor rather
        # than assuming its start value, which is the reactor's business, not ours.
        self.wall_start = time.time()
        self.clock_epoch = self.hh.reactor.monotonic()
        return self

    def _preload_all(self):
        """Gates start empty (TIP_ABSENT), so a bare MMU_LOAD on a fresh session fails."""
        # The preload walks every gate and would otherwise leave the LAST one selected, which
        # is the one thing about the machine the bootup banner cannot be made to agree with:
        # it renders before this runs. Preloading from the pre_bootup seam instead is not a
        # free swap - today the preload runs after _settle_nfc_init() and after bootup sets
        # the print state to 'initialized', and moving it ahead of bootup changes that
        # ordering. So restore the selection bootup reported instead, and the banner's
        # 'Selct:'/'T' row still describes the machine the first MMU_STATUS sees. Read from
        # the machine rather than hardcoding gate 0, so a persisted selection is honoured too.
        selected = self.hh.mmu.gate_selected
        for gate in range(self.hh.mmu.num_gates):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self._dispatch('MMU_PRELOAD GATE=%d' % gate)
            # Settle between gates. Without it the preload does not finish and the gate is
            # left EMPTY, which then fails every load with "Gate N is empty".
            self.hh.reactor.advance(0.)
        if selected >= 0 and self.hh.mmu.gate_selected != selected:
            self._dispatch('MMU_SELECT GATE=%d' % selected)
            self.hh.reactor.advance(0.)
        self._clear_sink()  # setup noise is not console history

    def close(self):
        if self.hh is not None:
            # Not optional: MmuLogger leaks an atexit handler and a QueueListener thread
            # otherwise (test/hh/bootstrap.py:111).
            self.hh.close()
            self.hh = None

    # -- output ---------------------------------------------------------------
    @contextlib.contextmanager
    def scrollback_stdout(self, enabled):
        """
        Tee sys.stdout into self.scrollback for the duration, and always put it back.

        `enabled` is a parameter rather than an isatty() call so the seam can be driven from
        a test. Restoring in a finally is not optional - a Console that left a dead tee
        behind would keep appending to a deque nobody reads for the rest of the process.
        """
        if not (enabled and self.scrollback is not None):
            yield None
            return
        tee = ScrollbackTee(sys.stdout, self.scrollback)
        sys.stdout = self.tee = tee
        try:
            yield tee
        finally:
            if sys.stdout is tee:
                sys.stdout = tee.raw_stream
            self.tee = None

    def info(self, text):
        """
        Print a line that came from the SIMULATOR, not from Happy Hare.

        Worth distinguishing because the banner lands directly underneath cmd_MMU_BOOTUP's
        real output and reads as more of it - a reader has no way to tell that "All 13 gates
        preloaded" is the harness talking while the line above it came off the MMU.

        Multi-line text is marked per line, so a wrapped message cannot leak past the mark.
        """
        for line in str(text).split('\n'):
            # flush: the startup notice is printed before a long silent boot, and block
            # buffering would hold it back until exactly the point it stops being useful.
            print(paint(INFO_PREFIX + line, INFO_COLOUR, self.color), flush=True)

    def echo(self, text):
        """
        Record a line the terminal showed but stdout never carried.

        readline writes the prompt and echoes what is typed at the C level, so neither
        reaches the tee. Without this the scrollback is a list of answers with no questions.
        """
        if self.tee is not None:
            self.tee.write_capture(text + '\n')

    def _on_output(self, msg):
        self.sink.append(msg)
        # Stamp NOW, not when it is printed. _drain() runs after the command returns, so
        # stamping there gave every line of an operation the same reading - the clock as it
        # stood at the END - which hid the very progression /timestamp exists to show. Under
        # /pace a load reported eight identical stamps while the clock had moved 11s.
        self.sink_stamp.append(self.sim_time())
        # And PRINT it now too, once a session is live. Buffering until the command returned
        # meant a paced load printed correct-looking timestamps all at once at the end - the
        # timings said one thing and the screen said another. Streaming makes the pauses land
        # BETWEEN lines, where they belong. The prompt is unavailable while a command runs,
        # which is what a printer does anyway.
        if self.streaming:
            self._emit_pending()

    def _emit_pending(self):
        """Print whatever Happy Hare has said that has not been printed yet."""
        while self._printed < len(self.sink):
            index = self._printed
            self._printed += 1
            self.emit(html_to_ansi(self.sink[index], self.color, self.mode),
                      stamp=self.sink_stamp[index])

    def _clear_sink(self):
        """All three together - the lists are indexed in lockstep by _drain()."""
        del self.sink[:]
        del self.sink_stamp[:]
        self._printed = 0

    def sim_time(self):
        """
        The virtual clock as a time of day: when the simulator started, plus however far the
        reactor has been advanced since. So '/advance 3600' really does move it an hour, and
        a session that never advances stays at the minute it booted.
        """
        elapsed = 0.
        if self.clock_epoch is not None:
            elapsed = self.hh.reactor.monotonic() - self.clock_epoch
        return time.strftime(TIME_FORMAT, time.localtime(self.wall_start + elapsed))

    def emit(self, text, stamp=None):
        """
        Print a message from the MMU, stamped with the virtual clock if /timestamp is on.

        `stamp` is the clock as it stood when Happy Hare produced the line (recorded by
        _on_output); without one, now. Only the FIRST line carries the stamp; the rest are
        indented to sit under it, so a multi-line reply still reads as one block rather than
        as one stamped line followed by loose text.
        """
        if not self.timestamps:
            print(text)
            return
        stamp = stamp or self.sim_time()
        pad = ' ' * (len(stamp) + 1)
        lines = text.split('\n')
        # Blank lines stay blank rather than becoming nine spaces: the indent is there to
        # line text up, and padding an empty line only leaves trailing whitespace behind.
        print('\n'.join([paint(stamp, TIME_COLOUR, self.color) + ' ' + lines[0]]
                        + [pad + line if line else '' for line in lines[1:]]))

    def _drain(self, mark):
        """
        Print from `mark`, skipping anything streaming already printed.

        Still needed even with streaming on: boot() runs with it off (banner() shows that
        output), and _printed is the authority on what has reached the screen either way.
        """
        self._printed = max(self._printed, mark)
        self._emit_pending()

    # -- dispatch -------------------------------------------------------------
    def _dispatch(self, line):
        """
        Dispatch at top level, exactly as the test suite does. DO NOT "improve" this by
        running it inside the reactor - that was tried and it is wrong here.

        The theory for reactor dispatch is sound: at top level reactor.pause() takes the
        _sys_pause branch, so the clock teleports without running timers and a
        ReactorCompletion.wait() returns None immediately instead of waiting
        (reactor.py:60,198). Happy Hare really does wait on completions and pause on
        command paths - MmuCompoundEndstop (mmu_sensor_utils.py:588), the NFC drivers,
        MmuSyncFeedback's settle loops - so in principle those get wrong answers here.

        In practice reactor dispatch breaks MMU_PRELOAD outright: it leaves every gate
        GATE_EMPTY instead of GATE_AVAILABLE, silently, with no error. Measured, four
        gates, identical setup:

            direct   gate_status=[1, 1, 1, 1]
            reactor  gate_status=[0, 0, 0, 0]

        Pauses that genuinely yield let the sensor timers fire mid-preload in a different
        order, and HH's own preload tail then concludes the gate is not loaded (the
        "entry sensor still triggered after preloading" rule, test/README.md section 5).
        The whole filament/sensor model was built and validated against top-level
        dispatch, which is what all of the tests use.

        So: proven beats faithful. The completion.wait() caveat is real but hypothetical,
        and it is documented in test/README.md rather than traded for a broken preload.
        """
        self.hh.gcode.run_script(line)

    def run_command(self, line):
        mark = len(self.sink)
        unhandled_mark = len(self.hh.gcode.unhandled)
        gate_status_before = None
        if (re.match(r'^\s*MMU_GATE_MAP(?:\s|$)', line, re.I)
                and re.search(r'(?:^|\s)AVAILABLE\s*=', line, re.I)):
            gate_status_before = list(self.hh.mmu.gate_maps.gate_status)
        # Stream Happy Hare's output as it happens rather than after the command returns.
        # Scoped to here, not global: _dispatch() is also the raw path for setup and for the
        # tests, and both need it silent.
        self.streaming = True
        try:
            self._dispatch(line)
        except Exception as exc:                    # noqa: BLE001
            # The fake GCodeDispatch calls handlers bare where real Klipper catches
            # gcode.error and responds (gcode.py:220), so without this the first bad
            # parameter would end the session.
            print(paint('!! %s' % exc, '1;31', self.color))
            self.failures += 1
        if gate_status_before is not None:
            self._sync_filament_to_gate_map(gate_status_before)
        # Settle whatever the command armed. Re-run unconditionally: a failed advance
        # skips its clock assignment, so this also repairs a mid-flight clock.
        try:
            self.hh.reactor.advance(0.)
        except Exception as exc:                    # noqa: BLE001
            print(paint('!! reactor: %s' % exc, '1;31', self.color))
        self._settle_moonraker()
        self._drain(mark)                           # anything streaming did not already print
        self.streaming = False
        self._warn_unhandled(line, unhandled_mark)
        self._warn_silent_macro(line, mark)

    def _sync_filament_to_gate_map(self, before):
        """Make explicit MMU_GATE_MAP availability changes physical in the simulator."""
        after = self.hh.mmu.gate_maps.gate_status
        with self.hh.quiet_sensors():
            for gate, (old, new) in enumerate(zip(before, after)):
                if old == new:
                    continue
                if new == GATE_EMPTY:
                    self.fil.remove(gate)
                elif new in (GATE_AVAILABLE, GATE_AVAILABLE_FROM_BUFFER):
                    # An available gate has filament running back towards its source and
                    # parked through the entry switch, ready for the MMU to pick up.
                    self.fil.refill(gate, sync=False)
                    self.fil.park(gate)
                # GATE_UNKNOWN describes knowledge, not a physical state: preserve it.

    def _home_before_bootup(self):
        """
        Home every physical selector while bootup can still see it, and keep the chatter out
        of the banner.

        Homing has to precede __MMU_BOOTUP or the banner renders an unhomed machine - 'Selct:
        XXXX', tool 'T?' - which a later MMU_STATUS contradicts. But "Homing MMU unit0... /
        Homed" is setup, not bootup, and startup_output is printed under the welcome, so
        leaving it in put three lines of it ABOVE the rabbit. Dropped the same way the
        preload's noise is (see _preload_all).

        Skipped under --no-calibrate, which boots the machine cold on purpose.
        """
        if self.args.no_calibrate:
            return
        mark = len(self.sink)
        self.hh.home_selectors()
        # Then pick a gate, so bootup renders a machine that knows where it is rather than
        # '[T?] ... 0.0mm'. A real printer arrives here already knowing, because the selector
        # restored its saved position at klippy:ready - but a console session starts with a
        # fresh vars file, so there is nothing to restore and the selection has to be made for
        # real, right here. The guard honours a restored selection if one ever does exist.
        if self.hh.mmu.gate_selected < 0:
            self.hh.gcode.run_script('MMU_SELECT GATE=0')
        del self.sink[mark:]
        del self.sink_stamp[mark:]
        self._printed = min(self._printed, mark)

    def _settle_moonraker(self):
        """
        Run the Klipper <-> Moonraker contract to quiescence after a command.

        Both directions are fire-and-forget in production, so a command that calls out to
        Moonraker returns before anything answers. settle() alternately delivers queued calls
        each way until neither side has work - which is what turns a spool lookup into a gate
        map update within the same prompt.

        Errors are reported, not raised: an unanswerable remote method is a finding about the
        machine, not a reason to end the session.
        """
        link = self.hh.moonraker_link
        if link is None:
            return
        try:
            link.settle()
        except Exception as exc:                    # noqa: BLE001
            print(paint('!! moonraker: %s' % exc, '1;31', self.color))

    def _warn_silent_macro(self, line, mark):
        """
        The fake gcode_macro records a macro call but never renders or runs its BODY
        (pinned by test_mmu_toolchange.py). So T1, print start/end and the park/cut/purge
        sequences are REGISTERED - they do not show up as unknown commands - and simply do
        nothing. Silence is the whole symptom, so key off "produced no output at all".
        """
        if len(self.sink) > mark:
            return
        word = line.strip().split(None, 1)[0].upper() if line.strip() else ''
        if re.fullmatch(r'T\d+', word):
            print(paint('   (%s is a macro, and the harness does not run macro bodies - '
                        'use MMU_CHANGE_TOOL TOOL=%s)' % (word, word[1:]), '33', self.color))

    def _warn_unhandled(self, line, mark):
        """
        Unknown commands are recorded and ignored (gcode.py:230), so a typo produces no
        output whatsoever. Only warn about the line the user actually typed: Happy Hare
        legitimately emits M104/M117 into the same list, which is why strict mode is not
        the answer here.
        """
        typed = line.strip().split(None, 1)[0].upper() if line.strip() else ''
        for raw in self.hh.gcode.unhandled[mark:]:
            if raw.split(None, 1)[0].upper() == typed:
                print(paint("?? no such command %r" % typed, '33', self.color))
                if re.fullmatch(r'T\d+', typed):
                    print('   (the fake gcode_macro records macros but does not run '
                          'their bodies - use MMU_CHANGE_TOOL TOOL=%s)' % typed[1:])
                return

    ##################
    ##### Header #####
    ##################

    # get_status() is a pure read with no remote calls, and nothing changes while the user
    # types, so the whole header is rebuilt per prompt rather than diffed or cached.
    GROUPS = ('machine', 'sensors', 'filament', 'selector', 'gates', 'leds')

    def _status(self):
        return self.hh.mmu.get_status(self.hh.reactor.monotonic())

    @property
    def units(self):
        """Every unit, in gate order. One entry on a single-unit machine."""
        machine = getattr(self.hh.mmu, 'mmu_machine', None)
        return list(getattr(machine, 'units', []) or [])

    @property
    def num_units(self):
        return len(self.units) or 1

    def unit_of(self, gate):
        """The unit owning an absolute gate number, or None."""
        for unit in self.units:
            if unit.first_gate <= gate < unit.first_gate + unit.num_gates:
                return unit
        return None

    def header_lines(self):
        st = self._status()
        out = []
        for group in self.args.header:
            out.extend(getattr(self, '_hdr_' + group)(st))
        return out

    def _hdr_machine(self, st):
        # HH's own rendering table rather than a hand-copied one. Imported lazily: the
        # fake klippy tree is only on sys.path after Session.install() has run.
        from extras.mmu.mmu_constants import FILAMENT_POS_NAME_MAP
        mmu = self.hh.mmu
        pos = FILAMENT_POS_NAME_MAP.get(mmu.filament_pos, '?')
        # status['gate'] reports _next_gate mid-toolchange, i.e. the target - so read
        # gate_selected directly and show the target separately (mmu_controller.py:632).
        gate = mmu.gate_selected
        target = '' if mmu._next_gate is None else ' -> gate %s' % mmu._next_gate
        bowden = st.get('bowden_progress', -1)
        line = ('T%-3s gate %-3s%s  %-22s %8.1fmm  %s'
                % (mmu.tool_selected, gate, target, pos,
                   st.get('filament_position', 0.), st.get('action', '?')))
        out = [line]
        extra = ['print=%s' % st.get('print_state', '?')]
        if st.get('sync_drive'):
            extra.append('SYNCED')
        if bowden >= 0:
            extra.append('bowden=%d%%' % bowden)
        extra.append('t=%+.2fs' % (self.hh.reactor.monotonic() - 1000.))
        extra.extend(self._current_cells(gate))
        # Only when pacing is ON. 0 is the default and means "moves are instant", which is
        # the absence of a mode rather than a mode - and a permanent 'realtime=0%' would just
        # be a row of noise on every prompt. Shown next to the clock because that is the
        # field it explains: with this present, t= moves during an operation.
        if self.hh.pacing:
            extra.append('realtime=%g%%' % (self.hh.pacing * 100.))
        out.append('  ' + '  '.join(extra))
        if mmu.is_mmu_paused():
            # Read the reason from status, not psm.reason_for_pause, which persists after
            # a resume and would show stale text.
            out.append(paint('  PAUSED: %s  (MMU_UNLOCK / MMU_RECOVER)'
                             % (st.get('reason_for_pause') or 'unknown'), '1;31', self.color))
        return out

    def _current_cells(self, gate):
        """
        Stepper run current, as HH believes it and as the modelled driver holds it. Shown
        always: watching the number move is the point, and a cell that only appears when
        off-default hides the resting state. Amps come from the TMC so a divergence between
        HH's percentage and the driver is visible rather than inferred.
        """
        mmu = self.hh.mmu
        cells = []
        for label, pct, tmc in (
            ('gear', mmu.gear_run_current(gate), self._tmc_for_gate(gate)),
            ('ext', mmu.extruder_run_current(), self._extruder_tmc()),
        ):
            amps = tmc.get_status().get('run_current') if tmc else None
            cells.append('%s=%d%%' % (label, pct) if amps is None
                         else '%s=%d%% %.2fA' % (label, pct, amps))
        return cells

    def _tmc_for_gate(self, gate):
        try:
            return self.hh.mmu.mmu_unit(gate).gear_tmc_obj(gate)
        except Exception:
            return None # No TMC on this gate, or too early to resolve one

    def _extruder_tmc(self):
        try:
            return self.hh.mmu.mmu_unit().extruder_tmc_obj()
        except Exception:
            return None

    def _hdr_sensors(self, st):
        # NOT status['sensors']: that is only the selected gate's sensors and it carries
        # v3 alias duplicates (mmu_pre_gate IS mmu_entry, mmu_gear IS mmu_exit,
        # mmu_gate IS mmu_shared_exit). get_sensor_states(all_sensors=True) is the real set.
        try:
            states = self.hh.mmu.sensor_manager.get_sensor_states(all_sensors=True)
        except Exception:                            # noqa: BLE001 - pre-ready
            return ['  sensors: (not ready)']
        cells = []
        multi = self.num_units > 1
        for name in sorted(states):
            state = states[name][0]
            # Only drop the unit prefix on a single-unit machine. With two units,
            # unit0:mmu_shared_exit and unit1:mmu_shared_exit would both read as
            # 'mmu_shared_exit' and the panel would show two identical, ambiguous cells.
            short = name if multi else name.split(':')[-1]
            if state is None:                        # a real third state: disabled
                cells.append((short, '-', '90'))
            elif state:
                cells.append((short, '1', '1;32'))
            else:
                cells.append((short, '0', None))
        return self._wrap_cells(cells)

    def _wrap_cells(self, cells, indent='  '):
        """Wrap name=value cells to the terminal width; 11 sensors overflow one line."""
        import shutil
        width = max(40, shutil.get_terminal_size((100, 24)).columns - len(indent))
        lines, cur, used = [], [], 0
        for name, value, code in cells:
            text = '%s=%s' % (name, value)
            if cur and used + len(text) + 2 > width:
                lines.append(indent + '  '.join(cur))
                cur, used = [], 0
            cur.append(paint(text, code, self.color) if code else text)
            used += len(text) + 2
        if cur:
            lines.append(indent + '  '.join(cur))
        return lines

    # Short labels for the path landmarks, in path order. A LINEAR mm scale is useless
    # here: park/entry/gate/encoder all sit within 120mm while the extruder is at 700, so
    # everything interesting collapses into the leftmost few characters. One slot per
    # landmark instead, which reads as "how far along the path is this filament".
    _LANDMARK_LABEL = {
        'mmu_nfc': 'nfc', 'mmu_pre_gate': 'pre', 'mmu_entry': 'ent', 'mmu_gate': 'gate',
        'mmu_exit': 'exit', 'mmu_shared_exit': 'shex', 'mmu_encoder': 'enc',
        'extruder_entry': 'extr', 'filament_compression': 'comp', 'toolhead': 'nozl',
    }

    def _landmarks(self):
        """[(position, label)] in path order, one entry per distinct position."""
        by_pos = {}
        for name, pos in self.fil.layout.items():
            by_pos.setdefault(pos, []).append(self._LANDMARK_LABEL.get(name, name))
        return [(pos, '/'.join(sorted(by_pos[pos]))) for pos in sorted(by_pos)]

    def _hdr_filament(self, st):
        """Per-gate tip position on the modelled path - what a real printer cannot show."""
        from test.hh.filament import TIP_ABSENT
        marks = self._landmarks()
        widths = [max(3, len(label)) for _, label in marks]
        head = ' '.join(label.center(w) for (_, label), w in zip(marks, widths))
        out = ['   %-8s %s' % ('', head)]
        selected = self.hh.mmu.gate_selected
        multi = self.num_units > 1
        last_unit = None
        for gate in range(self.fil.num_gates):
            if multi:
                # Gate numbering is absolute across units, so without a unit break an
                # 8-gate two-unit machine reads as one undifferentiated stack.
                unit = self.unit_of(gate)
                if unit is not None and unit is not last_unit:
                    out.append('   %s' % paint(unit.name, '1', self.color))
                    last_unit = unit
            tip, tail = self.fil.tip[gate], self.fil.tail[gate]
            flag = '*' if gate == selected else ' '
            if tip <= TIP_ABSENT / 2:               # a sentinel, not a position
                out.append('  %sgate %-3d %s' % (flag, gate,
                                                 paint('(empty)', '90', self.color)))
                continue
            cells = []
            for (pos, _), w in zip(marks, widths):
                covered = tail <= pos <= tip        # filament occupies [tail, tip]
                cells.append(paint('##'.center(w), '1;32', self.color) if covered
                             else '..'.center(w))
            out.append('  %sgate %-3d %s  %+8.1f' % (flag, gate, ' '.join(cells), tip))
        return out

    def _hdr_selector(self, st):
        """
        Where each physical selector carriage is, and what its servo is doing.

        carriage is the harness's PHYSICAL truth; cmd is what Happy Hare thinks. They agree
        except while a homing move is in flight, when MmuGenericRail.home() has rebased the
        frame to `forcepos` - so a lasting disagreement means something has gone wrong.

        Servo state is NOT in mmu.get_status(); it lives only on the selector object
        (LinearServoSelector -> 'servo', MmuServoSelector -> 'grip'), so read it there.
        Empty on a VirtualSelector machine, which has no carriage and no servo.
        """
        out = []
        for axis in (getattr(self.hh.printer, 'harness_selectors', None) or ()):
            selector = axis.selector
            bits = ['carriage=%7.2f' % axis.carriage, 'cmd=%7.2f' % axis.commanded,
                    'home=%.2f' % axis.home_position()]
            homed = getattr(selector, 'is_homed', None)
            bits.append(paint('HOMED', '32', self.color) if homed
                        else paint('NOT HOMED', '33', self.color))
            status = selector.get_status(self.hh.reactor.monotonic()) or {}
            for key in ('servo', 'grip'):
                if status.get(key) is not None:
                    bits.append('%s=%s' % (key, status[key]))
            out.append('  %-8s %s' % (axis.unit.name, '  '.join(bits)))
        return out

    def _hdr_gates(self, st):
        # These are LIVE list references from gate_maps, not copies - read only.
        status = st.get('gate_status')
        if status is None:
            return ['  gates: (not ready)']
        names = {-1: '?', 0: 'empty', 1: 'avail', 2: 'buffer'}
        mats = st.get('gate_material') or []
        spools = st.get('gate_spool_id') or []
        cells = []
        for gate, gs in enumerate(status):
            bits = [names.get(gs, str(gs))]
            if gate < len(mats) and mats[gate]:
                bits.append(str(mats[gate]))
            if gate < len(spools) and spools[gate] not in (None, -1):
                bits.append('#%s' % spools[gate])
            cells.append('%d:%s' % (gate, '/'.join(bits)))
        return ['  ' + '  '.join(cells)]

    # A lit LED is a SOLID BLOCK, not '##'. The old glyph was painted in the LED's own
    # colour, which made a white or grey LED - mmu_breathing_white_fast (0.2,0.2,0.2) on
    # 'selecting', mmu_sparkle on 'complete', white_light (1,1,1) for an uncoloured gate
    # under filament_color - indistinguishable from ordinary text, because the terminal's
    # default foreground IS white/grey. A block in that same colour still reads as a block.
    # Foreground only: _sgr_state() tracks fg and bold, so a background colour would not
    # survive a wrap in the pager.
    #
    # LED_DIM exists because "lit" and "visible" are not the same thing. black_light is
    # (0.01, 0, 0.02) - which is what an idle status segment under filament_color shows, and
    # what any BLACK filament shows - and that paints to xterm 16, i.e. pure black: less
    # visible than the grey '··' used for OFF, which inverts the whole mapping. So anything
    # below DIM_FLOOR is painted at DIM_FLOOR with its hue preserved and marked with a
    # lighter glyph. The brightness is a display floor, not a reading; the glyph is what says
    # so, which is why the colour is not simply left alone.
    LED_ON, LED_DIM, LED_OFF = '██', '▓▓', '··'
    DIM_FLOOR = 64                                  # of 255, ~25%

    def _hdr_leds(self, st):
        """
        effect_state is per-SEGMENT, so it cannot show a per-gate colour. The harness keeps
        real (r,g,b,w) data per LED, so read the virtual chain instead.
        """
        # Every unit, not just the selected one, and each unit's own effect_state index -
        # this used to read mmu_unit() and effect_state[0], so on a multi-unit machine it
        # showed unit 0's effects against whichever unit happened to be selected.
        from extras.mmu.unit.mmu_leds import MmuLeds
        out = []
        for index, unit in enumerate(self.units):
            leds = getattr(unit, 'leds', None)
            if leds is None:
                continue
            # All four, not just the per-gate pair: a configured 'status' or 'logo' segment
            # was invisible here, which reads as "not working" rather than "not shown".
            for segment in MmuLeds.SEGMENTS:
                chain = getattr(leds, 'virtual_chains', {}).get(segment)
                if chain is None:
                    continue
                try:
                    data = chain.get_status()['color_data']
                except Exception:                    # noqa: BLE001
                    continue
                if not data:
                    # A configured segment with no LEDs on it. Rendering it gives an empty
                    # row and a '?' effect, which reads as breakage rather than absence.
                    continue
                effect = self.hh.mmu.led_manager.effect_state.get(index, {}).get(segment, '?')
                label = ('%s %s' % (unit.name, segment)) if self.num_units > 1 else segment
                # status and logo are not indexed by gate, so grouping them would be a lie.
                # For the per-gate pair mmu_leds.py:101-102 has already guaranteed that the
                # division is exact.
                per_gate = len(data)
                if segment in MmuLeds.PER_GATE_SEGMENTS and unit.num_gates:
                    per_gate = max(1, len(data) // unit.num_gates)
                out.append('  led %-14s %s  [%s]'
                           % (label, self._swatches(data, per_gate), effect))
        return out

    def _swatches(self, data, per_gate):
        """
        One block per LED, `per_gate` of them run together and a space between groups.

        Ungrouped, ViViD's exit segment - 28 LEDs over 4 gates - renders a 117-column row,
        which soft-wraps and scribbles over the row below it (PinnedHeader.repaint writes
        header rows by absolute position and does not wrap). Grouped it is 59, and you can
        actually see which LEDs belong to which gate.
        """
        cells = []
        for rgbw in data:
            # Fold the white channel in rather than dropping it: an RGBW chain lit only on
            # W would otherwise read as off. Zero with every shipped effect, but free.
            white = rgbw[3] if len(rgbw) > 3 else 0.
            r, g, b = (min(255, int(round((c + white) * 255))) for c in rgbw[:3])
            peak = max(r, g, b)
            if not peak:
                cells.append(paint(self.LED_OFF, '90', self.color))
                continue
            glyph = self.LED_ON
            if peak < self.DIM_FLOOR:               # see LED_DIM
                scale = self.DIM_FLOOR / float(peak)
                r, g, b = (min(255, int(round(c * scale))) for c in (r, g, b))
                glyph = self.LED_DIM
            cells.append(paint(glyph, fg(r, g, b, self.mode)[2:-1], self.color))
        per_gate = max(1, per_gate)                  # or the slice below is empty and loops
        return ' '.join(''.join(cells[i:i + per_gate])
                        for i in range(0, len(cells), per_gate))

    # A HEAVY rule marks the boundary between the status section and the log window - that
    # is the one line a reader uses to tell "state" from "output" at a glance. The top edge
    # stays light so it does not compete with it.
    def rule(self, heavy=False):
        import shutil
        width = min(100, max(40, shutil.get_terminal_size((100, 24)).columns))
        char, colour = ('━', '1;36') if heavy else ('─', '90')
        return paint(char * width, colour, self.color)

    def header_block(self):
        """The header lines plus its heavy bottom edge, or [] when there is no header."""
        lines = self.header_lines()
        return lines + [self.rule(heavy=True)] if lines else []

    def draw_header(self):
        """Inline: reprint above the prompt. Used for --script, non-TTY and --inline-header."""
        lines = self.header_lines()
        if not lines:
            return
        print(self.rule())
        for line in lines:
            print(line)
        print(self.rule(heavy=True))

    def clear_log(self):
        """
        Wipe the output area AND repair the status section.

        Repair, not just wipe. Anything that scribbles on the terminal - a stray escape
        sequence in some output, a resize the terminal handled badly, a program that left a
        mode set - can leave the pinned band showing nonsense, and nothing else ever fixes
        it: repaint() rewrites the rows it knows about but only re-reserves the scroll region
        when the header CHANGES HEIGHT, so a corrupted band just stays corrupted.

        Clearing the whole screen and forcing the band to be re-reserved and redrawn makes
        /clear the one command that always gets the display back, which is the thing a user
        actually wants from it. Erasing only below the band, which is what this used to do,
        left them with no way out short of restarting.
        """
        if not sys.stdout.isatty():
            return
        out = raw_stdout()
        # The modes first, before any erase: ESC[2K and ESC[J both act with the CURRENT
        # attributes, so a colour left open would have the "clear" paint the screen in it.
        # Autowrap and the cursor are here because a pager killed mid-frame turns them off.
        out.write(RESET + '\033[?7h\033[?25h')
        if self.pinned is not None and self.pinned.active:
            # Drop the region before wiping so ESC[2J is not fighting it, then rebuild it.
            out.write('\033[r\033[2J\033[H')
            self.pinned.repaint(force=True)         # force= is what re-reserves the band
        else:
            out.write('\033[r\033[2J\033[H')
        out.flush()

    def redraw(self):
        """
        Put the whole screen back: the status band, then the log underneath it.

        The difference from clear_log() is the history. /clear throws it away; this repaints
        it from the scrollback, so it is the one to reach for when something has scribbled
        on the terminal but you still want to read what was on it.
        """
        if not sys.stdout.isatty():
            return
        self.clear_log()                            # modes, region, screen, status band
        if self.scrollback:
            LogPager(self).paint_tail()



    #########################
    ##### Meta-commands #####
    #########################

    META_HELP = (
        ('/advance N', 'advance virtual time N seconds (alias /wait)'),
        ('/live [on|off]', 'let the clock run while you sit at the prompt; off is the '
                           'reproducible mode (no argument toggles)'),
        ('/vars [mmu|machine|file]',
         'get_status() of the mmu and mmu_machine objects, or the saved mmu_vars.cfg'),
        ('/s [N], /scroll [N]', 'scroll back through the log, opening N rows up. '
                                'Arrows or j/k a line, b/f a page, g/G the ends, esc to exit'),
        ('/redraw', 'repaint the whole screen, log and all - the display is corrupted'),
        ('/clear', 'as /redraw, but empty the log rather than repaint it'),
        ('/sensors', 'sensor table (also: MMU_SENSORS)'),
        ('/sensor NAME on|off|enable|disable',
         'on/off drives the switch; enable/disable makes HH ignore it entirely'),
        ('/place GATE [POS]', 'put a filament tip at POS mm (default %g)' % TIP_AT_GATE),
        ('/preload GATE', 'place then MMU_PRELOAD that gate'),
        ('/exhaust GATE', 'give the filament a finite tail - this is what a runout IS'),
        ('/filament', 'per-gate tip/tail description'),
        ('/selector [POS|gate N|home|end] [UNIT=n]',
         'where the selector carriage physically is - set it as you would by hand'),
        ('/heat [TEMP]', 'set the extruder temperature'),
        ('/pace [FACTOR]', 'how much of each move\'s real duration to spend: 0=instant '
                           '(default), 0.5=twice as fast as real, 1=real time '
                           '(alias /realtime)'),
        ('/timestamp [on|off]', 'stamp MMU output with the virtual clock (no argument '
                                'toggles)'),
        ('/trace 0-4', "Happy Hare's own log_level, 4 = full narration"),
        ('/tag UID [GATE]', 'attach an NFC tag (needs --virtual-nfc)'),
        ('/header [GROUPS]', 'set header groups: %s, or "all"/"off"' % ','.join(GROUPS)),
        ('/log [N]', 'path to mmu.log and its last N lines (default 20)'),
        ('/errors', 'every !! message this session'),
        ('/help', 'this list'),
        ('/quit', 'exit (also Ctrl-D)'),
    )

    def meta(self, line):
        self.meta_line = line                       # /scroll needs its arguments unsplit
        parts = line[1:].split()
        if not parts:
            return
        name, rest = parts[0].lower(), parts[1:]
        fn = getattr(self, '_meta_' + name, None)
        if fn is None:
            alias = {'wait': '_meta_advance', 'q': '_meta_quit', 'h': '_meta_help',
                     's': '_meta_scroll', 'realtime': '_meta_pace'}.get(name)
            fn = getattr(self, alias) if alias else None
        if fn is None:
            print(paint('?? unknown meta-command /%s (try /help)' % name, '33', self.color))
            return
        try:
            fn(rest)
        except Exception as exc:                    # noqa: BLE001
            print(paint('!! /%s: %s' % (name, exc), '1;31', self.color))

    def _meta_help(self, args):
        print('Meta-commands:')
        for name, desc in self.META_HELP:
            print('  %-22s %s' % (name, desc))
        # The one part of the header that is not self-describing: a coloured block is not
        # obviously "one LED" until someone says so, and the gate grouping is invisible on a
        # one-LED-per-gate machine.
        print("\nIn the 'leds' header group, one block is one physical LED painted in its "
              'own\ncolour - %s lit, %s lit but too dim to see honestly (shown brighter), '
              '%s off -\nwith a gate\'s LEDs run together and a space between gates. [...] '
              "is the\nsegment's current effect."
              % (self.LED_ON, self.LED_DIM, self.LED_OFF))
        print('\nEverything else is sent to the MMU as G-code. MMU_HELP lists Happy Hare\'s\n'
              'commands, and any of them accepts HELP=1 for its own parameters.')

    def _meta_quit(self, args):
        self.running = False

    def advance(self, dt):
        """
        Move the virtual clock, in slices.

        One advance() call has an iteration cap, and the LED effects animate at 24fps, so on
        the default profile a single call dies partway through the seventh virtual minute -
        '/advance 600' really did stop at 444s and raise. The counter resets per call, so
        feeding the same span in slices gets there; the timers fire in the same order either
        way, since each slice still runs everything due within it.
        """
        left = float(dt)
        while left > 0:
            step = min(ADVANCE_SLICE, left)
            self.hh.reactor.advance(step)
            left -= step

    def _meta_advance(self, args):
        mark = len(self.sink)
        self.advance(float(args[0]) if args else 1.0)
        self._drain(mark)

    def _meta_clear(self, args):
        """Wipe the log window and repair the status section - see clear_log()."""
        self._clear_sink()
        if self.scrollback is not None:
            self.scrollback.clear()
        self.clear_log()

    def _meta_redraw(self, args):
        """Repaint everything, log included. /clear is the same thing minus the history."""
        self.redraw()

    def _meta_scroll(self, args):
        """
        Open the scrollback viewer. Shift-Up and PgUp are bound to this.

        A leading integer is where to open, counted in WRAPPED rows back from the end - what
        the reader sees, not logical lines. Anything after it is the half-typed line the key
        macro swept up (see _install_scroll_bindings), which is handed back through the
        history rather than thrown away.
        """
        back, typed = parse_scroll_args(self.meta_line.partition(' ')[2])
        self._recover_typed(typed)
        if self.scrollback is None:
            print(paint('   (scrollback is off - drop --scrollback 0)', '33', self.color))
            return
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            # Not a terminal, so there is nothing to page over. Printing the tail keeps
            # /scroll meaningful under --script instead of silently doing nothing.
            for line in list(self.scrollback)[-(back or 40):]:
                print(line)
            return
        LogPager(self).run(back)
        if typed:
            print(paint('   (press Up to get "%s" back)' % typed, '90', self.color))

    @staticmethod
    def _recover_typed(typed):
        """
        Tidy the history after the key macro fired.

        The macro genuinely types and submits '/scroll ...', so that line lands in the
        history the user is about to scroll through - drop it. Then push back whatever they
        were half-way through typing, so a single Up-arrow restores it.

        Best-effort throughout, like every other readline call here: get_history_item is
        1-based and remove_history_item 0-based on both GNU readline and the libedit build
        Python uses on macOS (measured), but pyreadline has neither.
        """
        if not HAVE_READLINE:
            return
        try:
            count = readline.get_current_history_length()
            if count > 0 and (readline.get_history_item(count) or '').startswith('/scroll'):
                readline.remove_history_item(count - 1)
        except Exception:                           # noqa: BLE001
            pass
        if typed:
            try:
                readline.add_history(typed)
            except Exception:                       # noqa: BLE001
                pass

    def _install_scroll_bindings(self):
        """
        Bind Shift-Up and PgUp to /scroll, where that can actually work. Returns whether it
        did, so the banner only advertises a key that exists.

        NOT on libedit, which is what Python's readline is on macOS. Measured: libedit
        delivers exactly the FIRST character of a macro immediately and holds the rest until
        the next input event. A one-character macro fires at once; '\\001/scroll \\005\\n'
        puts a lone '/' on the line and stops. Worse, the remainder is then flushed by
        whatever the user types next, so a stray Shift-Up turns their next command into
        '/scroll MMU_STATUS'. Better to have no key than one that corrupts the next line.

        Not an escape-sequence problem to be routed around: bare control keys bound with
        'bind -s' (^O, ^X, ^_) lag identically, so it is the macro machinery, not the CSI
        lookahead. And neither flavour of readline can bind a key to a Python callable, so
        there is nothing else to reach for. GNU readline runs macros properly, so Linux and
        the printers do get the binding.
        """
        if not HAVE_READLINE or readline_backend() == 'editline':
            return False
        bound = False
        for statement in scroll_binding_lines(readline_backend()):
            try:
                readline.parse_and_bind(statement)
                bound = True
            except Exception:                       # noqa: BLE001
                pass
        return bound

    def _meta_vars(self, args):
        """
        Both status dicts. mmu is live; mmu_machine is a config-load SNAPSHOT built once
        during config load (extras/mmu_machine.py:74) and never refreshed - its is_homed in
        particular is frozen at the pre-homing value. Labelled rather than hidden, because
        it is still the only place the per-unit config summary appears.
        """
        want = args[0].lower() if args else None
        if want not in (None, 'mmu', 'machine', 'file'):
            raise ValueError("usage: /vars [mmu|machine|file]")
        if want == 'file':
            return self._dump_mmu_vars()
        if want in (None, 'mmu'):
            print(paint('  [mmu] live', '1', self.color))
            st = self._status()
            for key in sorted(st):
                print('    %-30s %s' % (key, st[key]))
        if want in (None, 'machine'):
            print(paint('  [mmu_machine] config-load snapshot, NOT live', '1', self.color))
            machine = self.hh.printer.lookup_object('mmu_machine', None)
            if machine is None:
                print('    (no mmu_machine object)')
                return
            st = machine.get_status(self.hh.reactor.monotonic())
            for key in sorted(st):
                value = st[key]
                if isinstance(value, dict):         # the per-unit sub-dicts
                    print('    %-30s' % key)
                    for k2 in sorted(value):
                        print('      %-28s %s' % (k2, value[k2]))
                else:
                    print('    %-30s %s' % (key, value))

    def _dump_mmu_vars(self):
        """
        The session's mmu_vars.cfg, on disk, as MMU_CALIBRATE_* leaves it.

        It is a per-session copy of the repo's config/mmu_vars.cfg living in the harness
        tempdir (test/hh/bootstrap.py:_mmu_vars_copy) and it goes away with the session -
        deliberately, so a run can never touch a real install's calibration.
        """
        save_vars = self.hh.printer.lookup_object('save_variables', None)
        path = getattr(save_vars, 'filename', None)
        if not path or not os.path.exists(path):
            print('    (no mmu_vars.cfg for this session)')
            return
        print(paint('  [mmu_vars.cfg] %s (per-session copy, discarded on exit)'
                    % path, '1', self.color))
        with open(path) as handle:
            for line in handle:
                print('    %s' % line.rstrip())

    def _meta_selector(self, args):
        """
        Report or place the selector carriage.

        Placing it is the simulator's stand-in for physically sliding the carriage, which is
        exactly what MMU_CALIBRATE_SELECTOR AUTO=1 asks for: "the user has manually
        positioned the selector aligned with gate 0 before calling"
        (mmu_linear_selector.py:_calibrate_selector_auto).
        """
        axes = list(getattr(self.hh.printer, 'harness_selectors', None) or ())
        if not axes:
            print('    (this machine has no physical selector)')
            return

        rest, unit = [], 0
        for arg in args:
            if arg.upper().startswith('UNIT='):
                unit = int(arg.split('=', 1)[1])
            else:
                rest.append(arg)
        if not 0 <= unit < len(axes):
            raise ValueError('no selector on unit %d (have 0-%d)' % (unit, len(axes) - 1))

        if not rest:
            for axis in axes:
                print('    %s' % axis.describe())
            return

        axis, word = axes[unit], rest[0].lower()
        if word == 'gate':
            offsets = axis.nominal_gate_offsets()
            gate = int(rest[1])
            if not offsets or not 0 <= gate < len(offsets):
                raise ValueError('no nominal position for gate %d' % gate)
            target = offsets[gate]
        elif word == 'home':
            target = axis.travel_min
        elif word == 'end':
            if axis.travel_max is None:
                raise ValueError('this selector has no known end of travel')
            target = axis.travel_max
        else:
            target = float(word)
        axis.place(target)
        print('    %s' % axis.describe())

    def _meta_sensors(self, args):
        self.run_command('MMU_SENSORS')

    _SENSOR_ACTIONS = ('on', 'off', 'enable', 'disable')

    def _meta_sensor(self, args):
        """
        on/off drives the switch through its real button callback. enable/disable flips
        runout_helper.sensor_enabled, which is exactly what Happy Hare's own ENABLE= does
        (extras/mmu/mmu_sensor_utils.py:267): a disabled sensor reports None instead of
        True/False and HH stops acting on it - the closest thing to "not fitted".
        """
        if not args:
            raise ValueError('usage: /sensor NAME on|off|enable|disable')
        name = args[0]
        action = args[1].lower() if len(args) > 1 else 'on'
        action = {'1': 'on', 'true': 'on', '0': 'off', 'false': 'off'}.get(action, action)
        if action not in self._SENSOR_ACTIONS:
            raise ValueError('unknown action %r; use %s'
                             % (action, '|'.join(self._SENSOR_ACTIONS)))
        handle = self.hh.sensor(name)       # raises helpfully if unknown or ambiguous
        mark = len(self.sink)
        if action in ('on', 'off'):
            handle.set(action == 'on')
        else:
            handle.sensor.runout_helper.sensor_enabled = (action == 'enable')
            self.hh.reactor.advance(0.)
            print('  %s %sd' % (handle.name, action))
        self._drain(mark)

    def _meta_place(self, args):
        gate = int(args[0])
        pos = float(args[1]) if len(args) > 1 else TIP_AT_GATE
        self.hh.place_filament(gate, position=pos)

    def _meta_preload(self, args):
        gate = int(args[0])
        self.hh.place_filament(gate, position=TIP_AT_GATE)
        self.run_command('MMU_PRELOAD GATE=%d' % gate)

    def _meta_exhaust(self, args):
        self.fil.exhaust(int(args[0]))
        self.hh.reactor.advance(0.)

    def _meta_filament(self, args):
        for gate in range(self.fil.num_gates):
            print('  ' + self.fil.describe(gate))

    def _meta_heat(self, args):
        self.hh.heat_extruder(float(args[0]) if args else self.args.temp)

    def _meta_pace(self, args):
        """
        Trade speed for observability. Without a factor, just report the current one.

        At 0 an MMU_LOAD finishes with the clock untouched: fast, but nothing time-driven ever
        happens - the LED effect never reaches a second frame and every action transition lands
        in the same instant. Above 0 each move spends that fraction of its real duration in
        virtual time, so the operation plays out and `/header leds` shows it changing.

        Costs real seconds too: the reactor has to run every timer in the window it skips.
        """
        if not args:
            factor = self.hh.pacing
        else:
            factor = self.hh.set_pacing(args[0], wall=self._wall_pacing())
        if not factor:
            self.info('pace=0 - moves are instant, the clock does not move')
            return
        self.info('pace=%g - each move spends %g%% of its real duration '
                  '(%.4gx real time)' % (factor, factor * 100., 1. / factor))
        if self.hh.pacing_wall:
            self.info('  operations will take that long for real - Ctrl-C to interrupt one')
        else:
            self.info('  the virtual clock moves but nothing sleeps, so operations still '
                      'complete instantly (--wall to sleep)')

    def _wall_pacing(self):
        """
        Whether a paced move should also sleep in REAL time.

        On for an interactive session, because "make it take as long as the machine would" is
        the only reason to ask for pacing by hand - you cannot watch an LED effect that is over
        before the command returns. Off for a script or a pipe, where the point of the harness
        is that it does not wait for anything; --wall/--no-wall overrides either way.

        Requires interact() to have actually started, not merely a tty: a --script run has
        nobody watching it, and a Console driven straight from a test would otherwise sleep for
        real whenever the suite happens to run attached to a terminal.
        """
        if self.args.wall is not None:
            return 1. if self.args.wall else 0.
        return 1. if (self.interactive and sys.stdout.isatty()) else 0.

    ####################
    ##### Live mode ####
    ####################

    # WHY A SIGNAL AND NOT A THREAD. The reactor is greenlet-based, and greenlets belong to
    # the thread that made them: pumping it from a worker thread fails outright with
    # "greenlet.error: Cannot switch to a different thread". A setitimer handler runs on the
    # MAIN thread, so the greenlets stay consistent, and it does fire while blocked inside
    # readline's input() - which is the only moment we want it to.
    #
    # The timer is armed only around input() and disarmed for the whole of a dispatch, so a
    # tick can never land inside one. advance() asserts if it re-enters a callback, and the
    # tee's partial-line buffer would corrupt if a handler printed through the middle of a
    # write. _ticking guards the remaining case: Python runs a pending handler at the next
    # bytecode boundary, including one inside the handler itself.

    def _arm_tick(self, on):
        """
        Start or stop the idle clock. A no-op where signals are unavailable.

        Disarming is NOT gated on self.live. It has to run unconditionally or turning live
        off would leave the itimer running, and every path that stops the clock sets the
        flag first.
        """
        import signal
        try:
            if not on:
                signal.setitimer(signal.ITIMER_REAL, 0)
                return False
            # Only ever armed around a real prompt. script() has no arm/disarm discipline
            # of its own, so a '/live on' in a command file would leave a timer running and
            # the next tick would land inside a dispatch, where advance() asserts.
            if not (self.live and sys.stdout.isatty() and sys.stdin.isatty()):
                return False
            self._last_tick = time.monotonic()
            signal.signal(signal.SIGALRM, self._tick)
            signal.setitimer(signal.ITIMER_REAL, LIVE_INTERVAL, LIVE_INTERVAL)
        except (AttributeError, ValueError, OSError):    # not POSIX, or not the main thread
            return False
        return True

    def _tick(self, *_args):
        """
        A LIVE_INTERVAL slice of virtual time, while you are doing nothing.

        Anything the MMU says is printed above the prompt and the prompt is then put back
        with whatever was half-typed still on it, so this cannot eat a command in progress.
        """
        # _at_prompt, not just the itimer, because DISARMING DOES NOT UNDO A DELIVERED
        # SIGNAL. setitimer(0) stops future ones, but a SIGALRM already taken by the C
        # handler is still flagged, and Python runs its Python-level handler at the next
        # bytecode boundary - which by then is inside the command that just started. The
        # tick would then call advance() from inside a dispatch, where the reactor asserts
        # "advance() called from inside a callback". Checked HERE, in the handler, so a late
        # delivery is a no-op rather than a race.
        if self._ticking or not self.live or self.hh is None or not self._at_prompt:
            return
        if getattr(self.hh.reactor, '_g_dispatch', None) is not None:
            return                                  # belt and braces: mid-dispatch anyway
        self._ticking = True
        try:
            now = time.monotonic()
            # 'is None', not 'or': _last_tick is a float and 0.0 is a legitimate reading,
            # which 'or' would silently treat as never-ticked and skip the advance.
            last = now if self._last_tick is None else self._last_tick
            dt = min(now - last, LIVE_MAX_CATCHUP)
            self._last_tick = now
            if dt <= 0:
                return
            mark = len(self.sink)
            try:
                self.advance(dt)
            except Exception as exc:                # noqa: BLE001
                # Stop rather than reprint the same failure on every tick.
                self.live = False
                self._arm_tick(False)
                self._reprint(lambda: self.info('live clock stopped: %s' % exc))
                return
            if len(self.sink) > mark:
                self._reprint(lambda: self._drain(mark))
            if self.pinned is not None:
                self.pinned.repaint()               # the clock and t=+Ns moved
        finally:
            self._ticking = False

    def _reprint(self, emit_output):
        """
        Print from under the prompt: wipe the prompt line, emit, put the prompt back.

        readline believes it still owns that line, so it has to be erased first and rebuilt
        afterwards from get_line_buffer() - otherwise the output lands on top of what the
        user is typing and the typing is lost.
        """
        out = raw_stdout()
        out.write('\r\033[2K')
        out.flush()
        emit_output()
        pending = ''
        if HAVE_READLINE:
            try:
                pending = readline.get_line_buffer()
            except Exception:                       # noqa: BLE001
                pending = ''
        out.write(self.prompt() + pending)
        out.flush()

    def _meta_live(self, args):
        """
        Let the virtual clock run while you sit at the prompt. On by default at a terminal.

        Off is the reproducible mode: with the clock frozen the same commands always give
        the same transcript, however long you took over them. On is the realistic one -
        timeouts expire and the NFC poll loop runs without being asked.
        """
        if args:
            self.live = args[0].lower() in ('1', 'on', 'true', 'yes')
        else:
            self.live = not self.live
        if self.live and not self._arm_tick(True):
            self.live = False
            self.info('live clock unavailable here (needs a POSIX terminal)')
            return
        if not self.live:
            self._arm_tick(False)
        self.info('live clock %s' % ('on' if self.live else 'off - /advance moves it'))

    def _meta_timestamp(self, args):
        """
        Stamp MMU output with the virtual clock. No argument toggles.

        Worth having because the clock here is not real: it is frozen while you type and
        only moves inside a dispatch or an explicit /advance, so the stamps show what the
        MMU thinks the time is rather than how long you spent reading.
        """
        if args:
            self.timestamps = args[0].lower() in ('1', 'on', 'true', 'yes')
        else:
            self.timestamps = not self.timestamps
        self.info('timestamps %s (clock reads %s)'
                  % ('on' if self.timestamps else 'off', self.sim_time()))

    def _meta_trace(self, args):
        self.hh.mmu.p.log_level = int(args[0]) if args else 1
        print('  log_level = %s' % self.hh.mmu.p.log_level)

    def _meta_tag(self, args):
        uid = args[0]
        gate = int(args[1]) if len(args) > 1 else self.hh.mmu.gate_selected
        self.fil.attach_tag(gate, uid)
        self.hh.reactor.advance(0.)

    def _meta_header(self, args):
        if not args:
            print('  header = %s' % (','.join(self.args.header) or 'off'))
            return
        self.args.header = header_groups(args[0], self.GROUPS)
        self._resync_pin()

    def _resync_pin(self):
        """
        Put the screen back together after the header groups change.

        Two things go wrong without it. '/header off' leaves the reserved rows AND the
        shrunken scroll region in place with a stale header frozen in them, because
        repaint() has nothing to draw and returns early. And any change of HEIGHT moves the
        top of the scroll region over rows that were holding log output, so the band lands
        on top of old text and the prompt reappears at the top of the region instead of the
        bottom - the corruption that /redraw was being used to clear up.

        Redrawing is the same work /redraw does, and it is the only thing that fixes both:
        re-reserve the band, repaint it, and repaint the log underneath from the scrollback.
        """
        if not self._can_pin:
            return
        if self.args.header and self.pinned is None:
            self.pinned = PinnedHeader(self).install()
        elif not self.args.header and self.pinned is not None:
            self.pinned.restore()
            self.pinned = None
        self._install_pace_observer()
        self.redraw()

    def _install_pace_observer(self):
        """
        Let a PACED operation redraw the header while it is still running.

        Pacing spends virtual time on each move, but a command still owns the terminal until
        it returns - so without this the clock advances and nothing is rendered until the end,
        which defeats the point. The pacer calls this between moves, from top level (never
        from inside a reactor callback), so it is the same context an ordinary repaint runs in.

        Only ever a PINNED header: repaint() saves and restores the cursor and rewrites the
        reserved band, leaving the log flow alone. Reprinting an inline header mid-command
        would interleave with Happy Hare's own output.
        """
        pinned = self.pinned
        self.hh.printer.harness_pace_observer = (
            pinned.repaint if (pinned is not None and pinned.active) else None)

    def _meta_log(self, args):
        """Where mmu.log is, and its tail. It is live - `tail -f` it in another window."""
        path = self.hh.mmu_log
        if not os.path.exists(path):
            print('  no log at %s (--no-log, or log_file_level is negative)' % path)
            return
        n = int(args[0]) if args else 20
        with open(path, encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
        print('  %s  (%d lines, %d bytes)' % (path, len(lines), os.path.getsize(path)))
        for line in lines[-n:]:
            print('  | ' + line)

    def _meta_errors(self, args):
        errors = self.hh.errors
        if not errors:
            print('  none')
        for err in errors:
            print('  ' + html_to_ansi(err, self.color, self.mode))

    ######################
    ##### The prompt #####
    ######################

    def prompt(self):
        """
        Just '> '. The tool, the gate and the paused state are all on the first line of the
        status section, so spelling them out again here was duplication that cost a dozen
        columns on every line of the transcript.
        """
        return '> '

    def completer(self, text, state):
        if not hasattr(self, '_words'):
            # Every /name in the entry, not just the first token: an entry that lists an
            # alias ('/s [N], /scroll [N]') has two, and taking token[0] silently dropped
            # the long form out of completion.
            self._words = sorted(list(self.hh.gcode.gcode_help)
                                 + list(self.hh.gcode.base_commands)
                                 + [w for n, _ in self.META_HELP
                                    for w in re.findall(r'/[a-z]+', n)])
        matches = [w for w in self._words if w.upper().startswith(text.upper())]
        return matches[state] if state < len(matches) else None

    def banner(self):
        # Happy Hare's own bootup output first - the welcome, the unit summary and the
        # calibration warnings are exactly what you would see on a real printer starting up.
        for msg in self.startup_output:
            print(html_to_ansi(msg, self.color, self.mode))
        mmu = self.hh.mmu
        print()
        # Everything below here is the SIMULATOR, so it is marked and dimmed - see info().
        # It used to be plain text sitting under the real bootup output, indistinguishable
        # from it. No bold on the title any more: the mark is the signal now, and bold on a
        # deliberately dimmed line just fights it.
        self.info('Happy Hare console  profile=%s  gates=%d'
                  % (self.args.profile, mmu.num_gates))
        if self.args.no_calibrate:
            self.info('Uncalibrated and unhomed, as a fresh install would be. Run '
                      'MMU_CALIBRATE_SELECTOR / MMU_CALIBRATE_BOWDEN;\n'
                      '/selector places the carriage the way you would by hand.')
        elif not self.args.no_preload:
            self.info('All %d gates preloaded, extruder at %g C.'
                      % (mmu.num_gates, self.args.temp))
        if not self.args.no_log:
            self.info('Log: %s' % self.hh.mmu_log)
        self.info('/help for meta-commands, MMU_HELP for Happy Hare commands, '
                  'Ctrl-D to quit.')
        if self.scrollback is not None and sys.stdout.isatty():
            self.info('%s scrolls back through the log.'
                      % ('Shift-Up, PgUp or /s' if self.scroll_keys else '/s (or /scroll)'))
        if self.live:
            self.info('Clock is live: it runs while you sit here, so timers fire on their '
                      'own. /live off freezes it.')

    def interact(self):
        # Now there is a human watching, which is the condition wall-clock pacing waits on -
        # re-applied because boot() computed it before this was true. See _wall_pacing.
        self.interactive = True
        self.hh.set_pacing(self.hh.pacing, wall=self._wall_pacing())
        # The tee first, so the banner is in the scrollback too. isatty() and not the pinning
        # test: --inline-header keeps its history in the terminal's own buffer, but /scroll
        # still ought to work there.
        with self.scrollback_stdout(sys.stdout.isatty()):
            self._interact()

    def _interact(self):
        if HAVE_READLINE:
            try:
                readline.read_history_file(HISTORY_FILE)
            except Exception:                       # noqa: BLE001 - first run
                pass
            readline.set_history_length(1000)
            readline.set_completer(self.completer)
            readline.parse_and_bind('tab: complete')
            self.scroll_keys = self._install_scroll_bindings()
        if sys.stdout.isatty():
            self._can_pin = not self.args.inline_header
            if self.args.header and self._can_pin:
                # Clears the screen and reserves the band, so it must precede the banner
                self.pinned = PinnedHeader(self).install()
                self._install_pace_observer()
            else:
                out = raw_stdout()
                out.write(RESET + '\033[2J\033[H')
                out.flush()
        self.banner()
        try:
            while self.running:
                if self.pinned is not None:
                    self.pinned.repaint()
                else:
                    self.draw_header()
                prompt = self.prompt()
                # Armed ONLY around input(). Everything below runs with it off, so a tick
                # can never land inside a dispatch, inside the pager, or in the middle of
                # the tee reassembling a line.
                self._at_prompt = True
                self._arm_tick(True)
                try:
                    typed = input(prompt)
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    self.echo(prompt + '^C')
                    print('\n(interrupted - /quit or Ctrl-D to exit)')
                    continue
                finally:
                    # Order matters: clear the flag FIRST. Between disarming and here, a
                    # signal taken microseconds ago can still run its handler.
                    self._at_prompt = False
                    self._arm_tick(False)
                line = typed.strip()
                # readline wrote both the prompt and the echo at the C level, so the tee
                # never saw them. Not for the '/scroll' the key macro submits, though - that
                # is a keystroke, not something the user typed into the log.
                if not line.startswith('/scroll'):
                    self.echo(prompt + typed)
                if not line:
                    continue
                if line.startswith('/'):
                    self.meta(line)
                else:
                    self.run_command(line)
        finally:
            # Unconditional: a crash that skipped this would leave the user's terminal
            # with a shrunken scrolling region and no obvious way to notice why. Same for
            # the itimer, which would otherwise keep signalling a console that has gone.
            self._arm_tick(False)
            if self.pinned is not None:
                self.pinned.restore()
                self.pinned = None
        if HAVE_READLINE:
            try:
                readline.write_history_file(HISTORY_FILE)
            except Exception:                       # noqa: BLE001
                pass

    def script(self, stream):
        """Non-interactive: same handling, echoed, so the tool is testable."""
        self.banner()
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            print('%s%s' % (self.prompt(), line))
            if line.startswith('/'):
                self.meta(line)
            else:
                self.run_command(line)
            if self.args.header:
                self.draw_header()
            if not self.running:
                break


class PinnedHeader:
    """
    A header pinned to the top of the terminal while output scrolls underneath, using a
    DECSTBM scroll region.

    Nothing in the harness changes on its own - the virtual clock only moves during a
    dispatch or an explicit advance() - so this repaints on demand rather than on a timer.
    There is no background thread and nothing to race the prompt.

    ESC[r MUST be restored on the way out, including on a crash, or the terminal is left
    with a permanently shrunken scrolling region. install() therefore pairs with a
    restore() in a finally.
    """

    def __init__(self, console):
        self.console = console
        self.height = 0                             # rows actually reserved
        self.wanted = 0                             # rows the header asked for
        self.active = False

    # -- terminal plumbing ----------------------------------------------------
    @staticmethod
    def _rows():
        import shutil
        return max(8, shutil.get_terminal_size((100, 24)).lines)

    def _set_region(self, height):
        # raw_stdout() throughout, not sys.stdout: cursor control is not log content and must
        # not reach the scrollback buffer. With no tee installed the two are the same thing.
        out = raw_stdout()
        rows = self._rows()
        self.wanted = height
        height = min(height, rows - 4)              # always leave room to type
        out.write('\0337')                          # save cursor
        out.write('\033[%d;%dr' % (height + 1, rows))
        out.write('\0338')
        # If the cursor is still up in the reserved band, get it into the scroll region.
        out.write('\033[%d;1H' % (height + 1))
        out.flush()
        self.height = height

    def install(self):
        """
        Clear the screen and reserve the band BEFORE anything is printed. Ordering matters:
        reserve first and the banner lands below the band; print first and the header
        overwrites it on the very first repaint.
        """
        import signal
        raw_stdout().write(RESET + '\033[2J\033[H')  # clean slate, no inherited attributes
        self.active = True
        self._set_region(len(self.console.header_block()))
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (AttributeError, ValueError):        # not POSIX, or not the main thread
            pass
        return self

    def _on_resize(self, *_args):
        if self.active:
            self.repaint(force=True)

    def restore(self):
        if not self.active:
            return
        self.active = False
        out = raw_stdout()
        out.write('\033[r')                         # full-screen scrolling again
        out.write('\033[%d;1H' % self._rows())
        out.flush()

    # -- painting -------------------------------------------------------------
    def repaint(self, force=False):
        block = self.console.header_block()
        if not block:
            return
        # Against `wanted`, not `height`: on a terminal too short for the whole header the
        # band is capped, so height stays below len(block) permanently and comparing against
        # it would re-run _set_region on every repaint - which moves the cursor, twice a
        # second under a live clock, right out from under the prompt.
        if force or len(block) != self.wanted:
            self._set_region(len(block))
        if len(block) > self.height:
            # The band is capped at rows-4 so there is always room to type. Painting the
            # whole block regardless would write through the bottom of the band and into the
            # scroll region, over the log. '/header all' on a 13-gate machine wants 28 rows.
            block = block[:self.height - 1] + [
                paint('  ... %d more header rows: terminal too short, or use fewer /header '
                      'groups' % (len(block) - self.height + 1), '90', self.console.color)]
        out = raw_stdout()
        out.write('\0337')                          # save cursor + attrs
        for i, text in enumerate(block, start=1):
            # RESET before the erase, not after: ESC[2K clears using the CURRENT attributes,
            # so a colour still open from earlier output would repaint the whole row in it.
            out.write('\033[%d;1H%s\033[2K' % (i, RESET))
            out.write(text + RESET)
        out.write('\0338')                          # restore cursor
        out.flush()


class LogPager:
    """
    A scrollback viewer for the log area, under the pinned header.

    WHY THIS HAS TO EXIST. A terminal only pushes a row into its own scrollback buffer when
    that row scrolls off the top of the FULL screen. Rows that scroll out of a DECSTBM region
    - which is exactly what PinnedHeader sets up - are discarded. So with a pinned header
    there is nothing behind you: the terminal's scrollbar and Cmd-Up show the session before
    the header was installed and nothing since. Keeping the lines ourselves (ScrollbackTee)
    and painting them is the only way back.

    IT RUNS BETWEEN input() CALLS, which is the whole reason it is modal. readline is not
    active here, so the terminal is entirely ours: plain Up/Down scroll, and they still mean
    "previous command" at the prompt because the prompt never sees them.
    """

    # Bare keys. Plain arrows work here because readline is not competing for them.
    PLAIN = {'k': 'up', 'j': 'down', 'b': 'pgup', 'f': 'pgdn', ' ': 'pgdn',
             'g': 'home', 'G': 'end',
             'q': 'quit', '\r': 'quit', '\n': 'quit', '\x03': 'quit', '\x04': 'quit'}
    # Escape sequences, minus the leading ESC. Both the normal and the application-cursor
    # forms of the arrows, because which one arrives depends on the terminal's keypad mode.
    SEQS = {'[A': 'up', 'OA': 'up', '[B': 'down', 'OB': 'down',
            '[5~': 'pgup', '[6~': 'pgdn',
            '[H': 'home', 'OH': 'home', '[1~': 'home', '[7~': 'home',
            '[F': 'end', 'OF': 'end', '[4~': 'end', '[8~': 'end'}

    # How long to wait for the rest of an escape sequence before calling it a bare Esc. Not
    # shorter: at 50ms a real arrow key arriving over ssh reads as Esc and the pager closes
    # when the user presses Up.
    ESC_WAIT = 0.15
    POLL = 0.5                                      # so a SIGWINCH is noticed within half a second

    def __init__(self, console):
        self.console = console
        self.offset = 0                             # rows back from the live tail
        self._rows = []
        self._resized = False

    # -- pure bits, so they can be tested without a terminal ------------------
    @staticmethod
    def move(offset, key, total, page):
        """Where a key takes the view. offset counts rows BACK from the live tail."""
        top = max(0, total - page)
        offset += {'up': 1, 'down': -1, 'pgup': page, 'pgdn': -page}.get(key, 0)
        if key == 'home':
            offset = top
        elif key == 'end':
            offset = 0
        return max(0, min(offset, top))

    def geometry(self):
        """(first row, last row, width) of the area this may paint, 1-based and inclusive."""
        import shutil
        size = shutil.get_terminal_size((100, 24))
        pinned = self.console.pinned
        top = (pinned.height + 1) if (pinned is not None and pinned.active) else 1
        return top, max(top + 1, size.lines), max(20, size.columns)

    def _wrap(self, width):
        rows = []
        for line in self.console.scrollback:
            rows.extend(wrap_ansi(line, width))
        return rows

    def _status(self, page, width):
        first = max(0, len(self._rows) - page - self.offset) + 1
        last = min(len(self._rows), first + page - 1)
        where = 'END' if self.offset == 0 else '%d back' % self.offset
        # The LETTER keys are advertised alongside the arrows on purpose. PgUp/PgDn are
        # listed last because a terminal emulator often keeps them for its own scrollback
        # and they never reach us at all - Terminal.app does exactly that with fn-Up.
        text = (' scrollback  %d-%d of %d (%s)   up/down or j/k   b/f page   g/G ends   '
                'q to return ' % (first, last, len(self._rows), where))
        return paint(text[:width], '7', self.console.color)

    # -- painting -------------------------------------------------------------
    def _paint(self):
        top, bottom, width = self.geometry()
        page = bottom - top                         # last row is the status bar
        start = max(0, len(self._rows) - page - self.offset)
        view = self._rows[start:start + page]
        out = raw_stdout()
        # Autowrap off: writing the last cell of a row would otherwise wrap the cursor and,
        # on the bottom row, scroll the region out from under the frame being painted.
        out.write('\033[?7l')
        for i in range(page):
            text = view[i] if i < len(view) else ''
            out.write('\033[%d;1H%s\033[2K%s' % (top + i, RESET, text))
        out.write('\033[%d;1H%s\033[2K%s' % (bottom, RESET, self._status(page, width)))
        out.write(RESET + '\033[?7h')
        out.flush()

    def paint_tail(self):
        """
        Put the live tail back and park the cursor on an empty bottom row.

        Not cosmetic: the next thing to happen is input() drawing the prompt wherever the
        cursor is, so it has to be somewhere a prompt belongs. Used on the way out of the
        pager and by Console.redraw(), which needs exactly the same picture.
        """
        top, bottom, width = self.geometry()
        page = bottom - top
        rows = self._wrap(width)
        view = rows[max(0, len(rows) - page):]
        out = raw_stdout()
        out.write('\033[?7l')
        for i in range(page):
            text = view[i] if i < len(view) else ''
            out.write('\033[%d;1H%s\033[2K%s' % (top + i, RESET, text))
        out.write('\033[%d;1H%s\033[2K' % (bottom, RESET))
        # Park just after the last line, not always on the bottom row: a log shorter than
        # the pane would otherwise get a band of blank rows between it and the prompt. Once
        # the log fills the pane - the usual case, and always on the way out of the pager -
        # this is the bottom row anyway.
        out.write('\033[%d;1H' % min(bottom, top + len(view)))
        out.write('\033[?7h')
        out.flush()

    # -- input ----------------------------------------------------------------
    def _getch(self, timeout=None):
        """
        One character off the raw fd. os.read rather than sys.stdin.read: the TextIOWrapper
        buffers, and a buffered read here would swallow the rest of an escape sequence.
        """
        fd = sys.stdin.fileno()
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return ''
        try:
            return os.read(fd, 1).decode('utf-8', 'replace')
        except (OSError, ValueError):
            return ''

    def _read_key(self):
        """
        The next key as a name, or None for one we do not handle.

        ESC is both the prefix of every arrow key AND the quit key, so it cannot simply block
        waiting for what follows. A short poll settles it: a real arrow arrives as one burst,
        a bare Esc has nothing behind it.
        """
        ch = self._getch(self.POLL)
        if not ch:
            return None                             # timed out - lets a resize be noticed
        if ch != '\033':
            return self.PLAIN.get(ch)
        seq = ''
        while len(seq) < 8:
            nxt = self._getch(self.ESC_WAIT)
            if not nxt:
                break
            seq += nxt
            if seq in self.SEQS:
                return self.SEQS[seq]
            # ESC[1;2A (shift-up) and friends: give up on an exact match and take the final
            # byte, so a modified arrow still scrolls rather than doing nothing. len > 1 or
            # the introducer itself qualifies - 'O' is a letter, and ESC O B is a real arrow.
            if len(seq) > 1 and seq[0] in '[O' and seq[-1].isalpha():
                return self.SEQS.get(seq[0] + seq[-1])
            if seq[-1] == '~':
                return None
        return 'quit' if not seq else None          # bare Esc

    def _on_resize(self, *_args):
        self._resized = True

    # -- lifecycle ------------------------------------------------------------
    def run(self, start=0):
        """
        Scroll the log until the user quits. `start` is an initial offset in rows.

        Every terminal mode this touches is undone in the finally - cbreak most of all.
        Leaving the terminal in cbreak is the one failure here a user cannot see and cannot
        easily undo.
        """
        import signal
        if not (HAVE_RAWKEY and sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        fd = sys.stdin.fileno()
        try:
            saved = termios.tcgetattr(fd)
        except Exception:                           # noqa: BLE001 - not a real terminal
            return False

        out = raw_stdout()
        top, bottom, width = self.geometry()
        self._rows = self._wrap(width)
        self.offset = self.move(start, None, len(self._rows), bottom - top)

        prev_winch = None
        try:
            tty.setcbreak(fd)
            try:
                # PinnedHeader's own SIGWINCH handler repaints the header AND moves the
                # cursor into the region (_set_region), which would land in the middle of a
                # frame. Ours just marks the view dirty; the header is repainted on the way
                # back out of the prompt loop anyway.
                prev_winch = signal.signal(signal.SIGWINCH, self._on_resize)
            except (AttributeError, ValueError):    # not POSIX, or not the main thread
                prev_winch = None
            out.write('\033[?25l')                  # hide the cursor; nothing to point at
            self._paint()
            self._loop()
        except KeyboardInterrupt:
            # cbreak leaves ISIG on, so Ctrl-C arrives as a signal and never reaches
            # _read_key's '\x03' mapping. Treat it as the quit it was meant to be:
            # unhandled it escapes meta(), which only catches Exception, and takes the
            # whole console down with a traceback.
            pass
        finally:
            if prev_winch is not None:
                try:
                    signal.signal(signal.SIGWINCH, prev_winch)
                except (AttributeError, ValueError):
                    pass
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:                       # noqa: BLE001
                pass
            out.write('\033[?7h\033[?25h' + RESET)
            self.paint_tail()
        return True

    def _loop(self):
        while True:
            key = self._read_key()
            if key == 'quit':
                return
            if self._resized:
                self._resized = False
                _, _, width = self.geometry()
                self._rows = self._wrap(width)      # a new width is a whole new set of rows
            elif key is None:
                continue                            # nothing to do; just a poll timing out
            top, bottom, _ = self.geometry()
            self.offset = self.move(self.offset, key, len(self._rows), bottom - top)
            self._paint()


##################
##### Driver #####
##################

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog='make console',
        description='Interactive MMU console on the Happy Hare test harness.')
    p.add_argument('--profile', default='ercf_vvd',
                   help='harness profile name. Default ercf_vvd is a real 2-unit machine '
                        '(ERCF 1.1sb + ViViD 1.0, 13 gates). Others: boxturtle, tradrack, '
                        'emu, encoder, nfc_single, nfc_per_gate, nfc_spoolman, ...')
    p.add_argument('--temp', type=float, default=DEFAULT_TEMP,
                   help='extruder temperature to set at startup (default %(default)s)')
    p.add_argument('--no-preload', action='store_true',
                   help='leave every gate empty (gates start empty, so MMU_LOAD will fail)')
    p.add_argument('--no-calibrate', action='store_true',
                   help='boot cold: no seeded calibration, no homing, no preload - the state '
                        'a fresh install is in, so MMU_CALIBRATE_* can be driven for real')
    p.add_argument('--no-prime', action='store_true',
                   help='leave the gate map blank instead of giving every gate a vendor, '
                        'material, colour and temperature')
    p.add_argument('--seed', type=int, default=0,
                   help='seed for the primed gate map, so a session is reproducible '
                        '(default %(default)s)')
    p.add_argument('--no-moonraker', action='store_true',
                   help='do not attach the fake Moonraker/Spoolman, so calls out to it go '
                        'unanswered - which is what a printer with Moonraker down looks like')
    p.add_argument('--wall', dest='wall', action='store_true', default=None,
                   help='with --pace, sleep in real time so an operation can be watched '
                        '(the default at an interactive prompt, off for a script or pipe)')
    p.add_argument('--no-wall', dest='wall', action='store_false',
                   help='with --pace, move the virtual clock but never sleep')
    p.add_argument('--pace', type=float, default=0.5, metavar='FACTOR',
                   help='how much of each move\'s real duration to spend in virtual time: '
                        '0 is instant, 0.5 (default) is twice as fast as real, 1 is real '
                        'time. Also settable live with /pace')
    p.add_argument('--trace', type=int, default=0, metavar='0-4',
                   help="Happy Hare log_level; 4 is full narration")
    p.add_argument('--virtual-nfc', dest='virtual_nfc', action='store_true', default=True,
                   help='virtualise NFC readers so /tag works (default: on)')
    p.add_argument('--no-virtual-nfc', dest='virtual_nfc', action='store_false',
                   help='use the real reader driver instead, against a fake bus scripted '
                        'with a finite number of init cycles (test/hh/nfc_fixtures.py) - '
                        'ordinary shared-reader polling drains it too, so a long session '
                        'or a few MMU_ENABLE cycles will eventually make the real driver '
                        'genuinely report "did not respond - check wiring". Useful for '
                        'exercising the actual byte-level protocol, not for everyday use')
    p.add_argument('--plain', action='store_true',
                   help='strip colour instead of translating it to ANSI')
    p.add_argument('--color', choices=('auto', 'truecolor', '256', '16'), default='auto',
                   help='colour depth. auto uses truecolor only when $COLORTERM says so, '
                        'else 256 - a terminal without truecolor parses the channels of '
                        'ESC[38;2;R;G;Bm as separate SGR codes, and any channel in 100-107 '
                        'becomes a bright BACKGROUND colour (default: %(default)s)')
    p.add_argument('--header', default='all',
                   help='header groups, comma separated, or "all"/"off" (%s). All of them '
                        'wants a tall terminal - 28 rows on the default 13-gate machine - '
                        'and the band is capped at four rows short of the screen, so a '
                        'shorter one gets a truncation notice (default: %%(default)s)'
                        % ','.join(Console.GROUPS))
    p.add_argument('--scrollback', type=int, default=5000, metavar='N',
                   help='how many log lines to keep for /scroll and Shift-Up. A pinned '
                        'header lives in a DECSTBM scroll region, and rows that scroll out '
                        'of one never reach the terminal\'s own scrollback, so this is the '
                        'only copy. 0 disables it (default: %(default)s)')
    p.add_argument('--inline-header', action='store_true',
                   help='reprint the header above each prompt instead of pinning it to '
                        'the top of the terminal (automatic when not a TTY)')
    p.add_argument('--log-dir', default='/tmp', metavar='DIR',
                   help="where Happy Hare's mmu.log is written, replaced fresh each run. "
                        'The harness otherwise puts it in a temp dir it then deletes '
                        '(default: %(default)s)')
    p.add_argument('--no-log', action='store_true',
                   help='leave the log in the session temp dir, discarded on exit')
    # Tri-state: None means "on at a real prompt, off otherwise". --script has to stay
    # reproducible, and both of these put wall-clock-derived text into the output.
    p.add_argument('--live', dest='live', action='store_true', default=None,
                   help='let the virtual clock run while you sit at the prompt, so timers '
                        'fire and the NFC poll loop turns without being asked (default: on '
                        'when interactive)')
    p.add_argument('--no-live', dest='live', action='store_false',
                   help='freeze the clock unless /advance moves it - the reproducible mode')
    p.add_argument('--timestamp', dest='timestamp', action='store_true', default=None,
                   help='stamp MMU output with the virtual clock (default: on when '
                        'interactive)')
    p.add_argument('--no-timestamp', dest='timestamp', action='store_false',
                   help='no clock in the output')
    p.add_argument('--script', metavar='FILE',
                   help="read commands from FILE ('-' for stdin) instead of prompting")
    args = p.parse_args(argv)

    try:
        args.header = header_groups(args.header, Console.GROUPS)
    except ValueError as exc:
        p.error(str(exc))
    return args


def main(argv=None):
    args = parse_args(argv)
    console = Console(args)
    if not args.script:
        # boot() builds a whole fake printer and preloads every gate, which is fifteen to
        # twenty seconds of complete silence. Say something first, or it looks hung.
        # Transient: interact() clears the screen before the banner, so this does not stay.
        console.info('Starting simulator on profile %r, this takes a moment...'
                     % args.profile)
    try:
        console.boot()
    except Exception as exc:                        # noqa: BLE001
        print('Failed to boot the harness: %s: %s' % (type(exc).__name__, exc),
              file=sys.stderr)
        console.close()
        return 1
    failed = False
    try:
        if args.script:
            if args.script == '-':
                console.script(sys.stdin)
            else:
                with open(args.script, encoding='utf-8') as fh:
                    console.script(fh)
        else:
            console.interact()
        # Read before close(), which tears the session down. Script mode reports a
        # non-zero exit when anything went wrong - either Happy Hare emitted a '!!' or a
        # command raised - so it is usable as a test.
        failed = bool(args.script) and bool(console.hh.errors or console.failures)
    finally:
        console.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
