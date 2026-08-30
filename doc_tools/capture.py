# Happy Hare menuconfig screenshot tool.
#
# Runs the real installer/Kconfig tree through the real menuconfig, headlessly,
# and renders any screen it can reach to a PNG for the documentation:
#
#   make shots ARGS='--dump'                                  # what is on screen now
#   make shots ARGS='--keys "select:MMU Type,enter" --dump'   # navigate, then look
#   make shots ARGS='--keys "select:Purging,enter" --out doc/images/purging.png'
#
# No screen recorder, no window manager, no cropping by hand. menuconfig is a
# curses app, so it is given a pty, its escape stream is fed to a terminal
# emulator (pyte), and the resulting character grid - text plus per-cell colour -
# is drawn with Pillow. That grid is also what the assertions read, so a shot can
# state which screen it expects to be on and fail loudly when it is not.
#
# THREE THINGS ABOUT DRIVING menuconfig THAT ARE NOT GUESSABLE:
#
#  1. ARROW KEYS MUST BE SENT IN APPLICATION MODE. menuconfig calls keypad(True),
#     which emits DECCKM (ESC [ ? 1 h). After that the cursor keys are ESC O A/B,
#     not ESC [ A/B. The CSI forms are accepted by the pty and silently ignored by
#     the app - keys vanish, the screen never moves, and nothing reports an error.
#
#  2. THE SELECTED ROW IS A PARTIAL-WIDTH BLUE RUN. It is not reverse video, and
#     it is not the '>' in the left margin - that marks a parameter needing a
#     value and never moves. selection() therefore finds the row carrying the most
#     blue-background cells. A restyled menuconfig (see MENUCONFIG_STYLE below)
#     would need _SELECTED_BG changed to match.
#
#  3. STARTUP IS SLOW AND VARIABLE. Parsing the ~40 Kconfig files takes several
#     seconds on a dev box and considerably longer on a Pi, so every wait here is
#     on a predicate about the screen, never on a sleep. START_TIMEOUT is generous
#     for that reason.
#
# REPRODUCIBILITY. Kconfig:107 globs /dev/serial/by-id/* and Kconfig:110-118 shells
# out to canbus_query.py under $KLIPPER_HOME to populate the MCU connection menus.
# Both read the machine doing the capture. KLIPPER_HOME is pointed at a path that
# is plausible in a screenshot but absent on a dev box (see DOC_KLIPPER_HOME) so
# the canbus list comes back empty; the /dev/serial glob cannot be overridden, so
# regenerating these images on a machine with a printer attached WILL produce
# different MCU screens. Capture on a machine with nothing plugged in.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
from collections import Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO_ROOT, 'installer')
KCONFIGLIB = os.path.join(INSTALLER, 'lib', 'kconfiglib')
DOC = os.path.join(REPO_ROOT, 'doc')
IMAGES = os.path.join(DOC, 'images')

# Where a screenshot pretends Klipper lives. Deliberately NOT $HOME/klipper: a real
# checkout makes Kconfig's canbus_query.py actually run and the MCU menus fill with
# whatever is on the capture machine. A stock Pi path is absent on a dev box (so the
# query fails and the list is empty) and reads correctly on the Paths & Services
# screen, which is the one place users see this value.
DOC_KLIPPER_HOME = '/home/pi/klipper'

# What the Paths & Services screen shows as the Happy Hare directory. Kconfig
# .paths_services:23 defaults that field to $(SRC), which on a dev box is wherever
# the repo happens to be cloned - a screenshot would otherwise publish the
# maintainer's home directory. srctree still points at the real installer, so the
# parse is unaffected; this is the only place Kconfig reads $(SRC).
DOC_SRC = '/home/pi/Happy-Hare'

START_TIMEOUT = 180                 # Kconfig parse on a slow machine
STEP_TIMEOUT = 10                   # a redraw after one keypress
SETTLE = 0.4                        # quiet period accepted as "the redraw finished"

# autofit() bounds.
#
# MIN_ROWS is a floor on the finished image rather than a technical limit - menuconfig
# lays out happily in about 15 rows. A documentation set reads badly when its
# screenshots are wildly different heights, and a two-item menu shrunk to fit looks
# like a cropped fragment rather than the installer, so short screens are padded to a
# consistent size. Override per session ('min_rows') or per command (--min-rows).
#
# MAX_ROWS is a backstop for a menu genuinely longer than any sane screenshot - it is
# reported rather than silently accepted, because the image will contain scroll arrows.
#
# GAP_ROWS is height autofit is NOT allowed to hand back: at least this many blank rows
# stay between the last menu item and the separator bar above the help text, so that
# text never reads as though it's crowding the menu above it. menu_slack() reclaims
# only what's left over past this gap, not all of it.
MIN_ROWS, MAX_ROWS, GROW_STEP, GAP_ROWS = 30, 96, 10, 2

# Terminal width. 100 columns is menuconfig's traditional default; wider gives long
# items (board names, pin lists) room to sit on one line instead of wrapping.
DEFAULT_COLS = 110

# menuconfig draws _N_SCROLL_ARROWS (14) of ACS_UARROW/ACS_DARROW on the separator
# bars when a window has content off-screen (menuconfig.py:1376,1422). pyte resolves
# the VT100 graphics charset, so they arrive as these glyphs. Three in a row is
# unambiguous - nothing else in the installer draws them.
SCROLL_ARROWS = ('↓', '↑')            # down, up

# Key encodings. The cursor keys are the SS3 forms - see note 1 in the header.
DOWN, UP, LEFT, RIGHT = b'\x1bOB', b'\x1bOA', b'\x1bOD', b'\x1bOC'
ENTER, ESC, SPACE, TAB = b'\r', b'\x1b', b' ', b'\t'
PGDN, PGUP = b'\x1b[6~', b'\x1b[5~'
HELP, QUIT = b'?', b'q'
# menuconfig binds 'g' to _select_first_menu_entry (menuconfig.py:869), which is the
# only way in to reset a menu's scroll offset from outside
HOME = b'g'

_SELECTED_BG = 'blue'               # see note 2 in the header


def hh_version():
    """
    The Happy Hare version, read the way install.sh:32 does.

    Kconfig:91 takes it from $HH_VERSION and renders it into the title bar of every
    screen; left unset the title reads 'Happy Hare v Configuration', which is what a
    screenshot would then immortalise. Deriving it rather than hardcoding keeps that
    from going stale. There are two other copies of this regex - install.sh:32 and
    test/hh/cfg.py:90 - and they must agree.
    """
    path = os.path.join(REPO_ROOT, 'extras', 'mmu', 'mmu_constants.py')
    with open(path, encoding='utf-8') as f:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    assert match, 'could not find VERSION in %s' % (path,)
    return match.group(1)


def doc_env(unit_name='unit0', multi_unit=False, entry_point=False, unit_index=0,
            capabilities=None):
    """
    The environment a documentation capture parses Kconfig under.

    Built from what install.sh and the Makefile actually export, NOT imported from
    test/hh/cfg.py: that env is shaped for test determinism and points KLIPPER_HOME
    at /nonexistent/klipper, which would land verbatim in a Paths & Services
    screenshot. Everything here is either a real default (Makefile:137-138) or a
    value chosen to look right to a reader.

    The three HAS_* capabilities exist only on a per-unit parse. install.sh:439-442
    reads them out of the TOP config and passes them down, because they are printer
    level rather than unit level (Kconfig:100-102); `capabilities` is that dict.
    """
    env = {
        'srctree': INSTALLER,
        'SRC': DOC_SRC,
        'HH_VERSION': hh_version(),
        'KLIPPER_HOME': DOC_KLIPPER_HOME,
        'UNIT_NAME': unit_name,
        'MCU_NAME': unit_name,
        'UNIT_INDEX': str(unit_index),
        'F_MULTI_UNIT': 'y' if multi_unit else '',
        'F_MULTI_UNIT_ENTRY_POINT': 'y' if entry_point else '',
        # Makefile:620-623 switches style for the multi-unit entry point, so a
        # capture of that screen gets the palette a user would really see.
        'MENUCONFIG_STYLE': 'aquatic' if entry_point else 'default',
    }
    env.update(capabilities or {})
    return env


# ---------------------------------------------------------------------------
# Seeds
#
# A seed is an existing .mmu_config - what the reader's machine would really be
# configured as. Without one every screenshot shows Custom Design / Not listed /
# Other and three config warnings, which is the least representative machine there
# is. Seeds are inputs and are never written back: Menuconfig copies one into a
# temporary directory and points KCONFIG_CONFIG at the copy.
# ---------------------------------------------------------------------------

# Named seeds, generated rather than committed. A checked-in .mmu_config would go
# stale silently as Kconfig gains options; generating means the seed always matches
# the tree being documented. The symbol is the one test/hh/profiles.py:111 uses for
# the same machine, so the docs and the harness describe the same Box Turtle.
BUILTIN_SEEDS = {'boxturtle': 'MMU_TYPE_BOX_TURTLE_1_0'}
DEFAULT_SEED = 'boxturtle'

_seed_cache = {}


def generate_seed(symbol, path, env):
    """Write a .mmu_config with `symbol` selected and everything else defaulted."""
    sys.path.insert(0, KCONFIGLIB)
    import kconfiglib

    saved = {key: os.environ.get(key) for key in env}
    os.environ.update({key: str(value) for key, value in env.items()})
    try:
        kconfig = kconfiglib.Kconfig(os.path.join(INSTALLER, 'Kconfig'), warn=False)
        sym = kconfig.syms.get(symbol)
        if sym is None:
            raise ScreenError('no such Kconfig symbol: %s' % symbol)
        if not sym.set_value(2):                     # 2 == y
            raise ScreenError('could not select %s' % symbol)
        kconfig.write_config(path)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return path


def resolve_seed(spec, env):
    """
    Turn a --seed value into a config file path.

    A name in BUILTIN_SEEDS is generated once per process (parsing Kconfig takes a
    few seconds and a run captures many screens); anything else is a path to an
    existing config, which must exist - silently falling back to defaults would
    produce a whole set of screenshots of the wrong machine.

    'none' means no seed, i.e. bare Kconfig defaults. It is handled here rather than
    at each entry point so that doc_tools.shots and doc_tools.capture cannot disagree
    about it.
    """
    if spec is None or spec == 'none':
        return None
    if spec in BUILTIN_SEEDS:
        if spec not in _seed_cache:
            directory = tempfile.mkdtemp(prefix='hh-seed-')
            _seed_cache[spec] = generate_seed(BUILTIN_SEEDS[spec],
                                              os.path.join(directory, '.mmu_config'), env)
        return _seed_cache[spec]
    if not os.path.isfile(spec):
        raise ScreenError('seed %r does not exist (built-in seeds: %s)'
                          % (spec, ', '.join(sorted(BUILTIN_SEEDS))))
    return spec


def read_config(path):
    """The CONFIG_* assignments in a .mmu_config, as a dict of strings."""
    values = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.split('#')[0].strip()
            if line.startswith('CONFIG_') and '=' in line:
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"')
    return values


def unit_context(path):
    """
    Work out which parse a seed belongs to, from its name and its sibling.

    install.sh runs menuconfig once per config: the top one over .mmu_config, then
    one per unit over .mmu_config_<name> with a different environment
    (install.sh:435-442). A seed called .mmu_config_gru is therefore a UNIT parse for
    'gru', and getting that wrong does not error - it silently renders the wrong half
    of the Kconfig, because whole symbol sets appear and disappear behind
    `if MULTI_UNIT_ENTRY_POINT` (Kconfig:158-186).

    Returns the keyword arguments doc_env() needs, or {} for a plain single-unit
    config. UNIT_INDEX and the printer-level capabilities come from the sibling top
    config, exactly as install.sh:433-442 passes them down.
    """
    directory, name = os.path.split(os.path.abspath(path))
    match = re.match(r'(\.?mmu_config)_(.+)$', name)
    if not match:
        top = read_config(path) if os.path.isfile(path) else {}
        if top.get('CONFIG_MULTI_UNIT') == 'y':      # the shared-config entry point
            return {'multi_unit': True, 'entry_point': True}
        return {}

    unit = match.group(2)
    context = {'unit_name': unit, 'multi_unit': True}
    top_path = os.path.join(directory, match.group(1))
    if os.path.isfile(top_path):
        top = read_config(top_path)
        units = [u.strip() for u in top.get('CONFIG_MMU_UNITS', '').split(',') if u.strip()]
        if unit in units:
            context['unit_index'] = units.index(unit)
        context['capabilities'] = {
            'HAS_SENSOR_TOOLHEAD': top.get('CONFIG_MMU_HAS_SENSOR_TOOLHEAD', ''),
            'HAS_SENSOR_EXTRUDER': top.get('CONFIG_MMU_HAS_SENSOR_EXTRUDER', ''),
            'HAS_SENSOR_TOOLHEAD_CUTTER': top.get('CONFIG_MMU_HAS_TOOLHEAD_CUTTER', ''),
        }
    return context


class ScreenError(RuntimeError):
    """A step did not land where the shot said it would."""


class Menuconfig:
    """
    A live menuconfig in a pty, plus everything needed to ask what it is showing.

    Use as a context manager - the child is SIGKILLed on exit, which is deliberate:
    a clean quit prompts to save, and a documentation run must never write a config
    (see the seed handling in __init__).
    """

    def __init__(self, cols=DEFAULT_COLS, rows=40, seed=DEFAULT_SEED, style=None,
                 min_rows=MIN_ROWS, **context):
        """
        `seed` is a built-in name, a path to an existing .mmu_config, or None for
        Kconfig defaults. `min_rows` is the shortest a fitted screenshot may be.
        `context` overrides what unit_context() infers from the seed (unit_name,
        multi_unit, entry_point, unit_index, capabilities).
        """
        import pyte                                  # kept local: see doc_tools/requirements.txt

        self.cols, self.rows, self.min_rows = cols, rows, min_rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)

        # The environment has to be settled before the seed is generated - a built-in
        # seed is produced by parsing the same Kconfig this session will show, and a
        # multi-unit parse is a different shape of tree.
        self.context = dict(context)
        seed_path = resolve_seed(seed, doc_env(**self.context))
        if seed_path:
            inferred = unit_context(seed_path)
            inferred.update(context)                 # explicit arguments always win
            self.context = inferred

        # KCONFIG_CONFIG is what menuconfig reads AND writes. Makefile:27 defaults it
        # to .mmu_config, i.e. the maintainer's working configuration, so it is always
        # pointed at a throwaway copy. A seed is an input and stays untouched, which
        # also means a session can quit through the save prompt without consequence.
        self._tmpdir = tempfile.mkdtemp(prefix='hh-shots-')
        self._config = os.path.join(self._tmpdir, os.path.basename(seed_path)
                                    if seed_path else '.mmu_config')
        if seed_path:
            shutil.copyfile(seed_path, self._config)

        env = dict(os.environ)
        env.update(doc_env(**self.context))
        # NO LINES/COLUMNS HERE. ncurses honours $LINES/$COLUMNS over the terminal's
        # actual size, so setting them pins the layout for the life of the process:
        # the pty resizes, the child gets SIGWINCH, and menuconfig re-reads the
        # environment and decides nothing changed. Leaving them unset is what makes
        # autofit() possible - the size then comes from the ioctl below.
        env.update(
            PYTHONPATH=KCONFIGLIB,
            KCONFIG_CONFIG=self._config,
            TERM='xterm-256color',
        )
        env.pop('LINES', None)
        env.pop('COLUMNS', None)
        if style:
            env['MENUCONFIG_STYLE'] = style
        self.style = env['MENUCONFIG_STYLE']

        self.pid, self.fd = pty.fork()
        if self.pid == 0:                            # child
            # Size the terminal BEFORE exec: curses reads it at initscr, and a window
            # it believes is 0x0 draws nothing at all.
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            os.chdir(REPO_ROOT)
            os.execve(sys.executable, [sys.executable, '-m', 'menuconfig',
                                       os.path.join(INSTALLER, 'Kconfig')], env)
            os._exit(127)                            # unreachable unless execve fails

    # -- context manager ------------------------------------------------------

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- reading the terminal -------------------------------------------------

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            readable, _, _ = select.select([self.fd], [], [], 0.1)
            if not readable:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:                          # child gone; pty collapsed
                return
            if not data:
                return
            self.stream.feed(data)

    def _snapshot(self):
        return self.selection(), tuple(self.lines)

    def _quiesce(self, timeout=STEP_TIMEOUT):
        """
        Wait until the screen stops changing AND is showing a highlight.

        Reading a curses app mid-redraw is the subtle failure here: while menuconfig
        scrolls a menu there are instants with the highlight not yet repainted, so
        selection() answers -1 and a caller that trusts it walks straight past the
        item it was looking for. Requiring two identical reads a settle apart, with a
        highlight present, is what makes select() land where it says it does.
        """
        end = time.time() + timeout
        last = None
        while time.time() < end:
            now = self._snapshot()
            if now == last and now[0][0] != -1:
                return True
            last = now
            self._pump(SETTLE)
        return False

    def _wait(self, predicate, timeout, what):
        """Poll the screen until `predicate` holds, then let the redraw settle."""
        end = time.time() + timeout
        while time.time() < end:
            self._pump(0.2)
            if predicate():
                self._quiesce(max(1.0, end - time.time()))
                return True
        return False

    @property
    def lines(self):
        return list(self.screen.display)

    @property
    def text(self):
        return '\n'.join(line.rstrip() for line in self.lines)

    @property
    def breadcrumb(self):
        """Row 0, e.g. '(Top)' or '(Top) -> MMU Type'."""
        return self.lines[0].strip()

    def _blue_run(self, y):
        row = self.screen.buffer[y]
        return sum(1 for x in range(self.cols) if row[x].bg == _SELECTED_BG)

    def selection(self):
        """(row, text) of the highlighted item - see note 2 in the header."""
        best, width = -1, 0
        for y in range(self.rows):
            run = self._blue_run(y)
            if run > width:
                best, width = y, run
        return (best, self.lines[best].strip()) if width else (-1, '')

    @property
    def selected(self):
        return self.selection()[1]

    def has(self, text):
        return any(text in line for line in self.lines)

    # -- geometry, for autofit ------------------------------------------------

    def scroll_arrows(self):
        """
        Rows showing menuconfig's "there is more off-screen" indicator.

        This is the thing a screenshot must never contain: a row of arrows tells the
        reader the menu is cut off, which is an artefact of the capture height rather
        than anything about their machine.
        """
        return [(y, arrow) for y, line in enumerate(self.lines)
                for arrow in SCROLL_ARROWS if arrow * 3 in line]

    def _bars(self):
        """Full-width coloured rows: the title bar, and the separator below the menu."""
        page = Counter(self.screen.buffer[y][x].bg
                       for y in range(self.rows)
                       for x in range(self.cols)).most_common(1)[0][0]
        found = []
        for y in range(self.rows):
            row = self.screen.buffer[y]
            colours = Counter(row[x].bg for x in range(self.cols))
            colour, count = colours.most_common(1)[0]
            if count == self.cols and colour != page:
                found.append(y)
        return found

    def _trailing_blank(self):
        """
        Blank rows directly above the separator bar, or None if the geometry can't be
        read (a dialog is up, or nothing is drawn yet). The menu window ends where
        these begin; see menu_slack() and autofit() for what that's used for.
        """
        bars = self._bars()
        if len(bars) < 2:
            return None
        top, separator = bars[0], bars[-1]
        blank = 0
        for y in range(separator - 1, top, -1):
            if self.lines[y].strip():
                break
            blank += 1
        return blank

    def menu_slack(self):
        """
        Height that can be given back without crowding the help text against the menu.

        The layout is: breadcrumb, title bar, menu window, separator bar, a help pane
        of a FIXED seven rows (menuconfig.py:251 _SHOW_HELP_HEIGHT), then two rows of
        key hints. Shrinking the terminal takes rows off the menu window only, so the
        blank space inside the help pane is overhead that cannot be reclaimed and must
        not be counted here - subtracting it would cut into the menu and bring the
        scroll arrows straight back.

        Of the blank rows directly above the separator, GAP_ROWS are reserved rather
        than reclaimed - the whole point of a gap is that it exists whether or not the
        menu itself happened to have trailing blank rows of its own, so it comes off
        the total rather than being conditional on it. autofit() separately GROWS the
        terminal first if there weren't even GAP_ROWS to begin with; this only ever
        shrinks, so it can't undo that.
        """
        blank = self._trailing_blank()
        return None if blank is None else max(blank - GAP_ROWS, 0)

    def state(self):
        """One line naming where we are - used in errors and by --dump."""
        return 'breadcrumb=%r selected=%r' % (self.breadcrumb, self.selected)

    # -- driving it -----------------------------------------------------------

    def start(self):
        if not self._wait(lambda: self.has('(Top)') and self.selection()[0] != -1,
                          START_TIMEOUT, 'startup'):
            raise ScreenError(
                'menuconfig never drew its first screen within %ds.\n'
                'Screen so far:\n%s' % (START_TIMEOUT, self.text))
        return self

    def key(self, keys, timeout=STEP_TIMEOUT):
        """
        Send keys and wait for the screen to change.

        Tolerant by design: DOWN at the end of a list, ESC at the top menu and a
        toggle that is already set all legitimately redraw to the same thing. Use
        step() when the shot depends on the outcome.
        """
        before = self._snapshot()
        os.write(self.fd, keys)
        if not self._wait(lambda: self._snapshot() != before, timeout, repr(keys)):
            self._quiesce(timeout)                   # nothing moved; that may be fine
        return self

    def step(self, keys, expect, timeout=STEP_TIMEOUT):
        """
        Send keys and REQUIRE the screen to satisfy `expect` (a substring, or a
        callable taking this object).

        This is the one that shots should use. A swallowed keypress otherwise
        produces a perfectly plausible screenshot of the wrong screen, and nobody
        reviewing a PNG can tell.
        """
        test = expect if callable(expect) else (lambda mc: mc.has(expect))
        os.write(self.fd, keys)
        if not self._wait(lambda: test(self), timeout, repr(expect)):
            raise ScreenError('after %r the screen never satisfied %r (%s)\n%s'
                              % (keys, expect, self.state(), self.text))
        return self

    def _walk(self, text, direction, limit):
        """
        Step the highlight until it lands on `text` or stops moving.

        "Stopped moving" is the whole screen being identical, not the selected text
        being identical: menus contain blank separator lines, the highlight lands on
        them, and two of those in a row would otherwise look like the end of the list
        and abandon the search halfway down.
        """
        for _ in range(limit):
            before = self._snapshot()
            self.key(direction)
            if text in self.selected:
                return True
            if self._snapshot() == before:
                return False                         # end of the menu; it does not wrap
        return False

    def select(self, text, limit=200):
        """
        Put the highlight on the first item containing `text`.

        Searches down, then back up, because menuconfig does not wrap and the item
        may be above where the highlight starts - or off-screen entirely in a menu
        longer than the window, which is why this cannot just look at what is
        currently displayed.
        """
        if text in self.selected:
            return self
        if self._walk(text, DOWN, limit) or self._walk(text, UP, limit):
            return self
        raise ScreenError('could not put the highlight on %r (%s)\n%s'
                          % (text, self.state(), self.text))

    def enter(self, item=None, expect=None):
        """Select `item` if given, then open it. Asserts the breadcrumb moved."""
        if item:
            self.select(item)
        before = self.breadcrumb
        return self.step(ENTER, expect or (lambda mc: mc.breadcrumb != before))

    def back(self, expect=None):
        before = self.breadcrumb
        return self.step(ESC, expect or (lambda mc: mc.breadcrumb != before))

    def help(self):
        """Open the help pane for the highlighted item."""
        return self.step(HELP, lambda mc: mc.has('Symbol:') or mc.has('information'))

    def toggle(self):
        return self.key(SPACE)

    def repaint(self):
        """
        Make menuconfig rewrite the screen, healing any stale fragment before a shot.

        WHAT GOES WRONG WITHOUT THIS. ncurses writes only the cells it believes
        changed, so it is entitled to skip an erase-to-end-of-line whenever its model
        already says the tail of that row is blank. pyte's model is not identical, and
        where the two disagree a fragment of the PREVIOUS menu survives in this
        pipeline's copy of the screen - e.g. the tail of 'Toolhead (Other/Unknown
        requiring calibration) --->' still sitting after 'Moonraker config file
        (moonraker.conf)' on the Paths & Services screen. The physical terminal is
        correct; only the capture is wrong, which is the worst kind of wrong because
        the resulting PNG looks entirely plausible.

        WHY A DIALOG AND NOT A RESIZE. Resizing looks like the obvious answer and does
        not work: ncurses raises KEY_RESIZE only on a real dimension change, and even
        bounced to a different height and back it emits nothing but cursor motion,
        because its model still matches what it thinks is on screen. Opening a dialog
        genuinely overwrites the middle of the display, so closing it forces those
        cells to be written again for real - and that is what re-syncs the two models.

        Safe when there is nothing to open: if '?' changes nothing, no ESC is sent
        (ESC in a menu would back out a level, quietly capturing the wrong screen).
        """
        crumb, before = self.breadcrumb, self._snapshot()
        self.key(HELP)
        if self._snapshot() == before:
            return self                              # no dialog appeared; leave it alone
        self.key(ESC)
        if self.breadcrumb != crumb:
            raise ScreenError('repaint left the screen on %r, expected %r'
                              % (self.breadcrumb, crumb))
        return self

    # The input dialog titles itself '<prompt> (string)' - or (int)/(hex) - which is
    # both how the tool knows an editor opened and the only reliable way to tell an
    # editor from a submenu, since Enter opens whichever the item happens to be.
    _EDITOR = ('(string)', '(int)', '(hex)')

    def in_editor(self):
        return any(self.has(kind) for kind in self._EDITOR)

    def in_dialog(self):
        """
        Anything floating above the menu - an editor, or a help pane.

        repaint() opens a dialog to force a redraw, so it cannot tidy up a screen that
        IS one; shot() asks this before healing.
        """
        return self.in_editor() or self.breadcrumb.endswith('information')

    def edit(self, item=None):
        """
        Open the value editor for a string/int parameter.

        Asserts an editor actually appeared: on a menu item the same Enter opens a
        submenu, and a screenshot captioned 'editing the display name' that in fact
        shows a submenu is exactly the failure this tool exists to prevent.
        """
        if item:
            self.select(item)
        self.autofit()          # size the menu now; it cannot be done with the box up
        return self.step(ENTER, lambda mc: mc.in_editor())

    def cancel(self):
        """Close the editor WITHOUT applying it."""
        return self.step(ESC, lambda mc: not mc.in_editor())

    def resize(self, rows):
        """
        Change the terminal height under the running menuconfig.

        Works only because the child's environment has no $LINES - see __init__.
        menuconfig relayouts from the SIGWINCH the pty raises, so there is nothing to
        send and no key to press; both this object's pyte screen and the pty are
        resized together so the two never disagree about the geometry.
        """
        if rows == self.rows:
            return self
        before = tuple(self.lines)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, self.cols, 0, 0))
        self.rows = rows
        self.screen.resize(rows, self.cols)
        self._wait(lambda: tuple(self.lines) != before, STEP_TIMEOUT, 'resize')
        return self

    def _unscroll(self):
        """
        Send a scrolled menu back to the top, keeping the highlight where it is.

        A menu keeps its scroll offset across a resize: coming back from a submenu on a
        short terminal leaves the list scrolled, and the up-arrows that go with it stay
        put however tall the terminal then gets. Growing cannot fix an offset, so it
        has to be reset - 'g' does that and moves the highlight to the first entry,
        which select() then puts back. With the whole menu now fitting, walking back
        down does not scroll it again.
        """
        if not any(arrow == '↑' for _, arrow in self.scroll_arrows()):
            return self
        wanted = self.selected
        self.key(HOME)
        if wanted and wanted not in self.selected:
            self.select(wanted)
        return self.repaint()

    def autofit(self, quiet=False):
        """
        Pick the shortest height that shows the whole screen without scroll arrows.

        Grows first - a screen with arrows is cut off and no amount of measuring will
        tell us by how much, since what is hidden is not drawn - then hands back the
        slack the menu window is not using. Two or three resizes, not a search.

        The order matters: measuring slack on a screen that still has arrows would
        read zero and stop, which is precisely the state being fixed.

        Does nothing while the small value editor is open. Resizing under it does not
        relayout the menu behind it, so the arrows never clear and this would grow to
        the cap for nothing; and the arrows the edit box draws itself (menuconfig.py:
        2722) mean the VALUE is wider than the field, which no terminal height fixes.
        edit() fits the menu before opening the box instead, so the screen a dialog is
        photographed on has already been sized.
        """
        if self.in_editor():
            return self

        # Measure only on a freshly painted screen. A row of arrows from an earlier,
        # shorter layout can survive in this pipeline's copy of the screen after the
        # real one has moved on (see repaint() for why), and autofit would then grow
        # all the way to the cap chasing arrows that are not on the terminal at all.
        self.repaint()
        self._unscroll()
        while self.scroll_arrows() and self.rows < MAX_ROWS:
            self.resize(min(self.rows + GROW_STEP, MAX_ROWS))
            self.repaint()
            self._unscroll()

        if self.scroll_arrows():
            if not quiet:
                print('    WARNING: %s still shows scroll arrows at the %d-row cap'
                      % (self.breadcrumb or 'screen', MAX_ROWS), file=sys.stderr)
            return self

        # A menu that exactly fills its window has zero trailing blank rows, and
        # menu_slack() only ever shrinks - it would never grow the terminal to CREATE
        # the gap that isn't there yet. So that case is handled first, separately: grow
        # by the shortfall and re-measure, the same coarse-then-settle shape as the
        # scroll-arrow loop above, because growing can itself shift where the help pane
        # starts rounding and needs a row of slack in either direction.
        while (self._trailing_blank() or 0) < GAP_ROWS and self.rows < MAX_ROWS:
            deficit = GAP_ROWS - (self._trailing_blank() or 0)
            self.resize(min(self.rows + deficit, MAX_ROWS))
            self.repaint()

        # Hand back the slack, but never go under the floor - and note this can resize
        # UPWARDS, when a screen small enough to need no trimming is still shorter than
        # a screenshot should be.
        target = max(self.rows - (self.menu_slack() or 0), self.min_rows)
        if target != self.rows:
            self.resize(target)
            self.repaint()
            # One row of rounding is possible - the help pane can round differently at
            # the new height - so give a row back rather than ship arrows.
            while self.scroll_arrows() and self.rows < MAX_ROWS:
                self.resize(self.rows + 1)
                self.repaint()
        return self

    def write(self, text):
        """
        Type into an open editor, replacing what is there (^U clears the field first).

        Nothing is committed unless the caller then sends ENTER, and even then only to
        the throwaway copy of the config - but cancel() is the honest way to leave a
        screenshot of an edit, since the following shots then still show the machine
        the seed described.
        """
        if not self.in_editor():
            raise ScreenError('no editor is open (%s)' % self.state())
        os.write(self.fd, b'\x15' + text.encode())   # \x15 = ^U, kill line
        if not self._wait(lambda: self.has(text), STEP_TIMEOUT, 'typed text'):
            raise ScreenError('%r never appeared in the editor\n%s' % (text, self.text))
        return self

    # -- output ---------------------------------------------------------------

    def shot(self, path, trim=True, scale=2, heal=None, fit=True):
        """
        Render the current screen.

        `fit` sizes the terminal to the screen first, so no image ever contains the
        scroll arrows that mean "cut off here" - pass False to keep whatever height
        the session was started with. `heal` defaults to "unless this is a dialog" -
        see repaint(), which opens one and so cannot be used to tidy up another.
        """
        from .render import render                   # deferred: needs Pillow
        if fit:
            self.autofit()
        if heal or (heal is None and not self.in_dialog()):
            self.repaint()
        # Post-condition, not an assumption: the whole point of autofit is that no
        # published image says "cut off here", and healing happens after it.
        arrows = self.scroll_arrows()
        if arrows and not self.in_editor():
            print('    WARNING: %s is being captured with scroll arrows on row %d'
                  % (os.path.basename(path), arrows[0][0]), file=sys.stderr)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        render(self.screen, path, trim=trim, scale=scale)
        return path

    def dump(self, stream=sys.stdout):
        """Print the screen as text. The fast way to find out where you are."""
        print('-' * self.cols, file=stream)
        print(self.text, file=stream)
        print('-' * self.cols, file=stream)
        print(self.state(), file=stream)
        return self


# -- the ad-hoc key language -------------------------------------------------
#
# Lets a whole navigation be written on a command line, which is what makes the
# tool driveable without editing a file first:
#
#   --keys 'select:Purging,enter,down,down,help'
#
_WORDS = {'down': DOWN, 'up': UP, 'left': LEFT, 'right': RIGHT, 'enter': ENTER,
          'esc': ESC, 'back': ESC, 'space': SPACE, 'tab': TAB, 'pgdn': PGDN,
          'pgup': PGUP, 'help': HELP}


def run_keys(mc, spec, on_shot=None):
    """
    Apply a comma-separated key spec.

    'shot:PATH' captures mid-sequence, so one command can walk a config and collect
    several images before the session closes - the same thing doc_tools/shots.py does,
    only written on a command line.
    """
    for token in [t.strip() for t in spec.split(',') if t.strip()]:
        verb, _, arg = token.partition(':')
        verb = verb.lower()
        if verb == 'select':
            mc.select(arg)
        elif verb == 'enter' and arg:
            mc.enter(arg)
        elif verb == 'edit':
            mc.edit(arg or None)
        elif verb == 'cancel':
            mc.cancel()
        elif verb == 'type':
            mc.write(arg)
        elif verb == 'shot':
            (on_shot or mc.shot)(arg)
        elif verb == 'repeat':                       # e.g. 'repeat:down*5'
            what, _, count = arg.partition('*')
            for _ in range(int(count or 1)):
                mc.key(_WORDS[what.strip().lower()])
        elif verb in _WORDS:
            mc.key(_WORDS[verb])
        else:
            raise SystemExit('unknown key token %r in --keys' % (token,))
    return mc


def add_common_args(parser):
    parser.add_argument('--cols', type=int, default=DEFAULT_COLS,
                        help='terminal width (default %d)' % DEFAULT_COLS)
    parser.add_argument('--rows', type=int, default=40,
                        help='starting terminal height (default 40); each shot then '
                             'autofits, so this only matters with --no-fit')
    parser.add_argument('--min-rows', type=int, default=MIN_ROWS,
                        help='shortest a fitted screenshot may be (default %d)' % MIN_ROWS)
    parser.add_argument('--no-fit', action='store_true',
                        help='keep --rows as-is instead of fitting each screen')
    parser.add_argument('--seed', default=DEFAULT_SEED,
                        help='.mmu_config (or .mmu_config_<unit>) to start from, or a '
                             'built-in name: %s. Copied, never written back. '
                             "'none' for Kconfig defaults" % ', '.join(sorted(BUILTIN_SEEDS)))
    parser.add_argument('--unit', help='UNIT_NAME; inferred from a .mmu_config_<unit> seed')
    parser.add_argument('--multi-unit', action='store_true', help='parse as a multi-unit setup')
    parser.add_argument('--entry-point', action='store_true',
                        help='the multi-unit shared-config entry point (aquatic style)')
    parser.add_argument('--style', help='override MENUCONFIG_STYLE')
    parser.add_argument('--scale', type=int, default=2, help='pixel scale (default 2)')
    parser.add_argument('--no-trim', action='store_true', help='keep trailing blank rows')
    return parser


def context_from_args(args):
    """Only pass what was actually asked for - the rest is inferred from the seed."""
    context = {}
    if args.unit:
        context['unit_name'] = args.unit
    if args.multi_unit:
        context['multi_unit'] = True
    if args.entry_point:
        context['multi_unit'] = True
        context['entry_point'] = True
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m doc_tools.capture',
        description='Drive menuconfig headlessly; dump a screen or save it as a PNG.')
    add_common_args(parser)
    parser.add_argument('--keys', default='',
                        help="e.g. 'select:Purging,enter,shot:/tmp/a.png,back,help'")
    parser.add_argument('--expect', help='fail unless this text is on the final screen')
    parser.add_argument('--dump', action='store_true', help='print the final screen as text')
    parser.add_argument('--out', help='write the final screen here (PNG)')
    args = parser.parse_args(argv)

    if not args.dump and not args.out and 'shot:' not in args.keys:
        args.dump = True                             # looking around is the common case

    def shot(path):
        mc.shot(path, trim=not args.no_trim, scale=args.scale, fit=not args.no_fit)
        print('wrote %s (%dx%d, %s)' % (path, mc.cols, mc.rows, mc.state()))

    with Menuconfig(cols=args.cols, rows=args.rows, seed=args.seed, style=args.style,
                    min_rows=args.min_rows, **context_from_args(args)) as mc:
        run_keys(mc, args.keys, on_shot=shot)
        if args.expect and not mc.has(args.expect):
            print('EXPECTED %r on screen (%s)' % (args.expect, mc.state()), file=sys.stderr)
            mc.dump(sys.stderr)
            return 1
        if args.dump:
            mc.dump()
        if args.out:
            shot(args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
