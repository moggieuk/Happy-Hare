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
import logging
import os
import re
import sys

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

# Where a preloaded filament tip is placed: past the entry switch at -50, which is the
# precondition Happy Hare requires before a preload can start (test/README.md section 5).
TIP_AT_GATE = -40.0
DEFAULT_TEMP = 220


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
        for code in _SGR.findall(line):
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




class Console:
    def __init__(self, args):
        self.args = args
        self.color = not args.plain and sys.stdout.isatty()
        # See truecolor_supported(): guessing truecolor on a terminal without it turns
        # Happy Hare's pink warning into a pink BACKGROUND.
        self.mode = (args.color if args.color != 'auto'
                     else ('truecolor' if truecolor_supported() else '256'))
        self.sink = []                              # ordered (index -> rendered line)
        self.startup_output = []                    # bootup, incl. the Happy Hare welcome
        self.pinned = None                          # set by interact() when pinning
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
        self.hh.boot(calibrate=not a.no_calibrate)
        self.startup_output = list(self.sink)        # the welcome, shown by banner()
        del self.sink[:]

        # MANDATORY, and the easiest thing to get wrong: the filament model is created
        # lazily and only then installs the move observer (test/hh/bootstrap.py:336).
        # boot() does not do it. Without it every motion command dies with a misleading
        # "No trigger on ... after full movement" from the fake HomingMove.
        self.fil = self.hh.filament()

        # Without this HH auto-heats and reports it through log_error, which lands in the
        # error list and makes a clean session look dirty (bootstrap.py:464).
        self.hh.heat_extruder(a.temp)

        # A PHYSICAL selector needs calibrating and homing before it can select a gate, and an
        # uncalibrated one refuses with "Selector is not clibrated". No-op on a VirtualSelector
        # machine, so this costs the older profiles nothing.
        self._prepare_selectors()

        if not (a.no_preload or a.no_calibrate):
            self._preload_all()
        return self

    def _prepare_selectors(self):
        """
        Home each physical selector. Calibration itself is seeded inside boot() - see the
        note there - but homing stays here on purpose: doing it before __MMU_BOOTUP sends
        bootup down a different recovery branch.

        MMU_HOME must name its unit on a multi-unit machine.

        Skipped entirely under --no-calibrate, which boots the machine cold so the real
        MMU_CALIBRATE_* flow can be driven by hand.
        """
        if self.args.no_calibrate:
            return
        if not getattr(self.hh.printer, 'harness_selectors', None):
            return
        for index, unit in enumerate(self.hh.mmu.mmu_machine.units):
            if getattr(unit.selector, 'selector_stepper', None) is not None:
                self._dispatch('MMU_HOME UNIT=%d' % index)

    def _preload_all(self):
        """Gates start empty (TIP_ABSENT), so a bare MMU_LOAD on a fresh session fails."""
        for gate in range(self.hh.mmu.num_gates):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self._dispatch('MMU_PRELOAD GATE=%d' % gate)
            # Settle between gates. Without it the preload does not finish and the gate is
            # left EMPTY, which then fails every load with "Gate N is empty".
            self.hh.reactor.advance(0.)
        self.sink.clear()                           # setup noise is not console history

    def close(self):
        if self.hh is not None:
            # Not optional: MmuLogger leaks an atexit handler and a QueueListener thread
            # otherwise (test/hh/bootstrap.py:111).
            self.hh.close()
            self.hh = None

    # -- output ---------------------------------------------------------------
    def _on_output(self, msg):
        self.sink.append(msg)

    def _drain(self, mark):
        for msg in self.sink[mark:]:
            print(html_to_ansi(msg, self.color, self.mode))

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
        try:
            self._dispatch(line)
        except Exception as exc:                    # noqa: BLE001
            # The fake GCodeDispatch calls handlers bare where real Klipper catches
            # gcode.error and responds (gcode.py:220), so without this the first bad
            # parameter would end the session.
            print(paint('!! %s' % exc, '1;31', self.color))
            self.failures += 1
        # Settle whatever the command armed. Re-run unconditionally: a failed advance
        # skips its clock assignment, so this also repairs a mid-flight clock.
        try:
            self.hh.reactor.advance(0.)
        except Exception as exc:                    # noqa: BLE001
            print(paint('!! reactor: %s' % exc, '1;31', self.color))
        self._drain(mark)
        self._warn_unhandled(line, unhandled_mark)
        self._warn_silent_macro(line, mark)

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
        legitimately emits M104/M117/SET_TMC_CURRENT into the same list, which is why
        strict mode is not the answer here.
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
        out.append('  ' + '  '.join(extra))
        if mmu.is_mmu_paused():
            # Read the reason from status, not psm.reason_for_pause, which persists after
            # a resume and would show stale text.
            out.append(paint('  PAUSED: %s  (MMU_UNLOCK / MMU_RECOVER)'
                             % (st.get('reason_for_pause') or 'unknown'), '1;31', self.color))
        return out

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

    def _hdr_leds(self, st):
        """
        effect_state is per-SEGMENT, so it cannot show a per-gate colour. The harness keeps
        real (r,g,b,w) data per LED, so read the virtual chain instead.
        """
        # Every unit, not just the selected one, and each unit's own effect_state index -
        # this used to read mmu_unit() and effect_state[0], so on a multi-unit machine it
        # showed unit 0's effects against whichever unit happened to be selected.
        out = []
        for index, unit in enumerate(self.units):
            leds = getattr(unit, 'leds', None)
            if leds is None:
                continue
            for segment in ('exit', 'entry'):
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
                swatches = []
                for rgbw in data:
                    r, g, b = (int(round(c * 255)) for c in rgbw[:3])
                    swatches.append(paint('##', fg(r, g, b, self.mode)[2:-1], self.color)
                                    if (r or g or b) else paint('..', '90', self.color))
                effect = self.hh.mmu.led_manager.effect_state.get(index, {}).get(segment, '?')
                label = ('%s %s' % (unit.name, segment)) if self.num_units > 1 else segment
                out.append('  led %-14s %s  [%s]' % (label, ' '.join(swatches), effect))
        return out

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
        Wipe the output area, leaving the status section alone.

        With a pinned header that means erasing only from the top of the scroll region
        downwards; without one there is nothing to preserve, so clear the lot.
        """
        if not sys.stdout.isatty():
            return
        if self.pinned is not None and self.pinned.active:
            sys.stdout.write(RESET + '\033[%d;1H\033[J' % (self.pinned.height + 1))
        else:
            sys.stdout.write(RESET + '\033[2J\033[H')
        sys.stdout.flush()



    #########################
    ##### Meta-commands #####
    #########################

    META_HELP = (
        ('/advance N', 'advance virtual time N seconds (alias /wait)'),
        ('/vars [mmu|machine|file]',
         'get_status() of the mmu and mmu_machine objects, or the saved mmu_vars.cfg'),
        ('/clear', 'clear the log window, keeping the status section'),
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
        ('/trace 0-4', "Happy Hare's own log_level, 4 = full narration"),
        ('/tag UID [GATE]', 'attach an NFC tag (needs --virtual-nfc)'),
        ('/header [GROUPS]', 'set header groups: %s, or "off"' % ','.join(GROUPS)),
        ('/log [N]', 'path to mmu.log and its last N lines (default 20)'),
        ('/errors', 'every !! message this session'),
        ('/help', 'this list'),
        ('/quit', 'exit (also Ctrl-D)'),
    )

    def meta(self, line):
        parts = line[1:].split()
        if not parts:
            return
        name, rest = parts[0].lower(), parts[1:]
        fn = getattr(self, '_meta_' + name, None)
        if fn is None:
            alias = {'wait': '_meta_advance', 'q': '_meta_quit', 'h': '_meta_help'}.get(name)
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
        print('\nEverything else is sent to the MMU as G-code. MMU_HELP lists Happy Hare\'s\n'
              'commands, and any of them accepts HELP=1 for its own parameters.')

    def _meta_quit(self, args):
        self.running = False

    def _meta_advance(self, args):
        dt = float(args[0]) if args else 1.0
        mark = len(self.sink)
        self.hh.reactor.advance(dt)
        self._drain(mark)

    def _meta_clear(self, args):
        """Wipe the log window. The status section is state, not scrollback - it stays."""
        del self.sink[:]
        self.clear_log()

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
        if args[0].lower() == 'off':
            self.args.header = []
            return
        groups = [g for g in args[0].split(',') if g]
        bad = [g for g in groups if g not in self.GROUPS]
        if bad:
            raise ValueError('unknown group(s) %s; known: %s'
                             % (','.join(bad), ','.join(self.GROUPS)))
        self.args.header = groups

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
        mmu = self.hh.mmu
        tag = 'PAUSED' if mmu.is_mmu_paused() else 'T%s g%s' % (mmu.tool_selected,
                                                                mmu.gate_selected)
        return 'mmu[%s]> ' % tag

    def completer(self, text, state):
        if not hasattr(self, '_words'):
            self._words = sorted(list(self.hh.gcode.gcode_help)
                                 + list(self.hh.gcode.base_commands)
                                 + ['/' + n.split()[0][1:] for n, _ in self.META_HELP])
        matches = [w for w in self._words if w.upper().startswith(text.upper())]
        return matches[state] if state < len(matches) else None

    def banner(self):
        # Happy Hare's own bootup output first - the welcome, the unit summary and the
        # calibration warnings are exactly what you would see on a real printer starting up.
        for msg in self.startup_output:
            print(html_to_ansi(msg, self.color, self.mode))
        mmu = self.hh.mmu
        print()
        print(paint('Happy Hare console', '1', self.color)
              + '  profile=%s  gates=%d' % (self.args.profile, mmu.num_gates))
        if self.args.no_calibrate:
            print('Uncalibrated and unhomed, as a fresh install would be. Run '
                  'MMU_CALIBRATE_SELECTOR / MMU_CALIBRATE_BOWDEN;\n'
                  '/selector places the carriage the way you would by hand.')
        elif not self.args.no_preload:
            print('All %d gates preloaded, extruder at %g C.'
                  % (mmu.num_gates, self.args.temp))
        if not self.args.no_log:
            print('Log: %s' % self.hh.mmu_log)
        print('/help for meta-commands, MMU_HELP for Happy Hare commands, Ctrl-D to quit.')

    def interact(self):
        if HAVE_READLINE:
            try:
                readline.read_history_file(HISTORY_FILE)
            except Exception:                       # noqa: BLE001 - first run
                pass
            readline.set_history_length(1000)
            readline.set_completer(self.completer)
            readline.parse_and_bind('tab: complete')
        if sys.stdout.isatty():
            if self.args.header and not self.args.inline_header:
                # Clears the screen and reserves the band, so it must precede the banner
                self.pinned = PinnedHeader(self).install()
            else:
                sys.stdout.write(RESET + '\033[2J\033[H')
                sys.stdout.flush()
        self.banner()
        try:
            while self.running:
                if self.pinned is not None:
                    self.pinned.repaint()
                else:
                    self.draw_header()
                try:
                    line = input(self.prompt()).strip()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print('\n(interrupted - /quit or Ctrl-D to exit)')
                    continue
                if not line:
                    continue
                if line.startswith('/'):
                    self.meta(line)
                else:
                    self.run_command(line)
        finally:
            # Unconditional: a crash that skipped this would leave the user's terminal
            # with a shrunken scrolling region and no obvious way to notice why.
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
        self.height = 0
        self.active = False

    # -- terminal plumbing ----------------------------------------------------
    @staticmethod
    def _rows():
        import shutil
        return max(8, shutil.get_terminal_size((100, 24)).lines)

    def _set_region(self, height):
        rows = self._rows()
        height = min(height, rows - 4)              # always leave room to type
        sys.stdout.write('\0337')                   # save cursor
        sys.stdout.write('\033[%d;%dr' % (height + 1, rows))
        sys.stdout.write('\0338')
        # If the cursor is still up in the reserved band, get it into the scroll region.
        sys.stdout.write('\033[%d;1H' % (height + 1))
        sys.stdout.flush()
        self.height = height

    def install(self):
        """
        Clear the screen and reserve the band BEFORE anything is printed. Ordering matters:
        reserve first and the banner lands below the band; print first and the header
        overwrites it on the very first repaint.
        """
        import signal
        sys.stdout.write(RESET + '\033[2J\033[H')   # clean slate, no inherited attributes
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
        sys.stdout.write('\033[r')                  # full-screen scrolling again
        sys.stdout.write('\033[%d;1H' % self._rows())
        sys.stdout.flush()

    # -- painting -------------------------------------------------------------
    def repaint(self, force=False):
        block = self.console.header_block()
        if not block:
            return
        if force or len(block) != self.height:
            self._set_region(len(block))
        sys.stdout.write('\0337')                   # save cursor + attrs
        for i, text in enumerate(block, start=1):
            # RESET before the erase, not after: ESC[2K clears using the CURRENT attributes,
            # so a colour still open from earlier output would repaint the whole row in it.
            sys.stdout.write('\033[%d;1H%s\033[2K' % (i, RESET))
            sys.stdout.write(text + RESET)
        sys.stdout.write('\0338')                   # restore cursor
        sys.stdout.flush()


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
    p.add_argument('--trace', type=int, default=0, metavar='0-4',
                   help="Happy Hare log_level; 4 is full narration")
    p.add_argument('--virtual-nfc', action='store_true',
                   help='virtualise NFC readers so /tag works')
    p.add_argument('--plain', action='store_true',
                   help='strip colour instead of translating it to ANSI')
    p.add_argument('--color', choices=('auto', 'truecolor', '256', '16'), default='auto',
                   help='colour depth. auto uses truecolor only when $COLORTERM says so, '
                        'else 256 - a terminal without truecolor parses the channels of '
                        'ESC[38;2;R;G;Bm as separate SGR codes, and any channel in 100-107 '
                        'becomes a bright BACKGROUND colour (default: %(default)s)')
    p.add_argument('--header', default='machine,sensors,filament',
                   help='header groups, comma separated, or "off" (%s)'
                        % ','.join(Console.GROUPS))
    p.add_argument('--inline-header', action='store_true',
                   help='reprint the header above each prompt instead of pinning it to '
                        'the top of the terminal (automatic when not a TTY)')
    p.add_argument('--log-dir', default='/tmp', metavar='DIR',
                   help="where Happy Hare's mmu.log is written, replaced fresh each run. "
                        'The harness otherwise puts it in a temp dir it then deletes '
                        '(default: %(default)s)')
    p.add_argument('--no-log', action='store_true',
                   help='leave the log in the session temp dir, discarded on exit')
    p.add_argument('--script', metavar='FILE',
                   help="read commands from FILE ('-' for stdin) instead of prompting")
    args = p.parse_args(argv)

    if args.header.lower() in ('off', 'none', ''):
        args.header = []
    else:
        args.header = [g for g in args.header.split(',') if g]
        bad = [g for g in args.header if g not in Console.GROUPS]
        if bad:
            p.error('unknown header group(s) %s; known: %s'
                    % (','.join(bad), ','.join(Console.GROUPS)))
    return args


def main(argv=None):
    args = parse_args(argv)
    console = Console(args)
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
