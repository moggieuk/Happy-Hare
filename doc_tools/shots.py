# The screenshots the documentation needs.
#
#   make shots                        # regenerate every session's images
#   make shots ARGS='--list'          # the sessions, and what each one covers
#   make shots ARGS='--only getting-started-boxturtle'
#   make shots ARGS='--seed ~/printer_data/.mmu_config'   # against a real machine
#
# A SESSION IS ONE menuconfig, MANY IMAGES. Parsing the Kconfig tree costs several
# seconds, so a session starts the installer once, walks it, and captures along the
# way - `shot('name')` writes name.png under the session's 'outdir' and carries on
# from where it is. Group screens that belong to the same walkthrough into one
# session; start a new one when the seed or the unit has to change.
#
# EVERY SESSION NAMES A REAL PAGE. There is no shared demo pool: a session exists
# because doc/Something.md embeds its images, and its 'outdir' is that page's own
# folder (see doc_tools/README.md). Falling back to the shared doc/images/ default
# is for CAPTURE=1 exploration only - don't add a session that writes there, or
# `make shots` starts regenerating pictures nothing reads.
#
# HEIGHT LOOKS AFTER ITSELF. Each shot() fits the terminal to the screen in front of
# it, so no image contains menuconfig's row of scroll arrows and none carries a band
# of dead space either - subject to a 30-row floor, so a two-item menu still looks like
# the installer rather than a cropped fragment. Sessions do not set 'rows'; pass
# 'min_rows' to change the floor, or 'fit': False and a 'rows' to pin a height.
#
# ALWAYS ASSERT THE LANDING SCREEN. Use mc.enter()/mc.edit()/mc.step(), which raise
# when the expected screen does not arrive, rather than mc.key(), which tolerates a
# keypress that changed nothing. A missed key produces a perfectly plausible PNG of
# the WRONG screen, and nobody reviewing an image can tell that is what happened.
#
# EDITS ARE CANCELLED, NOT APPLIED. mc.edit() opens a parameter's editor so it can be
# photographed; mc.cancel() closes it without changing anything, so the screens after
# it still show the machine the seed described. (Applying would be harmless to the
# real config - the session works on a copy - but not to the rest of the session.)
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import argparse
import os
import sys
import traceback

from .capture import DEFAULT_COLS, DEFAULT_SEED, DOC, IMAGES, MIN_ROWS, Menuconfig, ScreenError

# ---------------------------------------------------------------------------
# The sessions. Extend these; the runner needs no changes.
#
#   name     --only key, and the prefix for anything the session does not name
#   caption  what the session covers, for whoever writes the prose
#   scenes   f(mc, shot) - navigate, calling shot('image-name') at each screen
#   outdir   where this session's images go, relative to doc/ - name it after the
#            page (e.g. 'GettingStartedWithBoxTurtle'). Every session should set
#            this; see the header above.
#   seed     a config to start from - a built-in name or a path (default: boxturtle)
#   min_rows shortest a fitted screenshot may be (default 30, for a consistent set)
#   fit      False to stop autofitting and honour 'rows' instead
#   rows     starting height; only meaningful with 'fit': False
#   unit_name / multi_unit / entry_point - inferred from the seed, override here
# ---------------------------------------------------------------------------


def _getting_started_boxturtle(mc, shot):
    """
    For doc/GettingStartedWithBoxTurtle.md - the installer screens a first-time Box
    Turtle owner walks through, in that order. Runs from a bare Kconfig ('seed': None)
    rather than the boxturtle seed used elsewhere, because the page is about DRIVING
    menuconfig - selecting MMU Type is the first real thing a reader does with it,
    and the root-warnings screen is only informative if the warnings visibly clear as
    a result of that choice, which requires starting before it happens.
    """
    mc.select('MMU Type')
    shot('01-first-run')                            # every field still a placeholder

    mc.enter('MMU Type')
    mc.select('Box Turtle')
    mc.toggle()
    shot('02-mmu-type-boxturtle')                   # (X) Box Turtle; Turtle Neck now offered

    mc.enter('Turtle Neck')
    shot('03-turtleneck-buffer')                    # v2 is the default - nothing to change
    mc.back()
    mc.back()                                       # -> (Top)

    mc.select('MMU Type')
    shot('04-root-warnings')                        # only the (later-page) toolhead warning remains

    mc.enter('Board type')
    shot('05-board-type')                           # AFC Lite v1.0 - the board this MMU shipped with
    mc.back()

    mc.enter('MCU connection')
    shot('06-mcu-connection')                        # Serial - already right for a USB-attached board
    mc.back()

    mc.enter('MMU Features / Additions')
    shot('07-mmu-features')                          # LEDs/eSpooler/buffer already on; nothing to add
    mc.back()

    mc.enter('Pins / TMC')
    mc.enter('Gear pins')
    shot('08-gear-pins')                             # every gate's step/dir/enable/diag pin

    mc.edit('Gear dir pin')
    shot('09-gear-dir-editor')                       # the pin nobody can predict from a drawing
    mc.write('!unit0:PD3')
    shot('10-gear-dir-inverted')                     # '!' reverses it - no rewiring, no cfg edits
    mc.cancel()                                      # this page only shows the move; it does not make it
    mc.back()
    mc.back()                                        # -> (Top)

    mc.enter('Software Options')
    mc.enter('Select spoolman spool manager support')
    mc.select('Read-only')
    mc.toggle()
    shot('11-spoolman-readonly')                     # the one setting this page actually changes


SESSIONS = [
    {
        'name': 'getting-started-boxturtle',
        'caption': 'doc/GettingStartedWithBoxTurtle.md - first menuconfig pass for a Box Turtle',
        'scenes': _getting_started_boxturtle,
        'outdir': 'GettingStartedWithBoxTurtle',
        'seed': 'none',
    },
]


def run_session(session, outdir, scale=2, seed=None, min_rows=None, verbose=False):
    """Run one session, returning the images it produced."""
    written = []
    context = {key: session[key] for key in ('unit_name', 'multi_unit', 'entry_point')
               if key in session}
    # A session with its own 'outdir' (a getting-started page's image folder) always
    # goes there; --outdir only redirects sessions that did not ask for a home.
    outdir = os.path.join(DOC, session['outdir']) if 'outdir' in session else outdir

    with Menuconfig(cols=session.get('cols', DEFAULT_COLS), rows=session.get('rows', 40),
                    seed=seed or session.get('seed', DEFAULT_SEED),
                    style=session.get('style'),
                    min_rows=min_rows or session.get('min_rows', MIN_ROWS),
                    **context) as mc:

        def shot(name):
            path = os.path.join(outdir, name + '.png')
            mc.shot(path, trim=session.get('trim', True), scale=scale,
                    fit=session.get('fit', True))
            if verbose:
                mc.dump()
            print('    %-24s %2dx%-3d %s' % (name + '.png', mc.cols, mc.rows, mc.state()))
            written.append(path)

        session['scenes'](mc, shot)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m doc_tools.shots',
        description='Regenerate the menuconfig screenshots used by the documentation.')
    parser.add_argument('--only', action='append', default=[], metavar='NAME',
                        help='just this session; repeatable')
    parser.add_argument('--outdir', default=IMAGES, help='where the PNGs go')
    parser.add_argument('--seed', help='override every session\'s seed: a built-in name, '
                                       'or a path to a .mmu_config / .mmu_config_<unit>')
    parser.add_argument('--scale', type=int, default=2, help='pixel scale (default 2)')
    parser.add_argument('--min-rows', type=int,
                        help='override every session\'s height floor (default %d)' % MIN_ROWS)
    parser.add_argument('--list', action='store_true', help='list the sessions and exit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='dump each captured screen as text too')
    args = parser.parse_args(argv)

    if args.list:
        width = max(len(session['name']) for session in SESSIONS)
        for session in SESSIONS:
            print('  %-*s  %s' % (width, session['name'], session['caption']))
        return 0

    known = {session['name'] for session in SESSIONS}
    unknown = [name for name in args.only if name not in known]
    if unknown:
        parser.error('no such session: %s (try --list)' % ', '.join(unknown))
    wanted = [s for s in SESSIONS if not args.only or s['name'] in args.only]

    # No pre-creation of args.outdir here: shot() (doc_tools/capture.py) already
    # makes whatever directory a PNG needs, and args.outdir is only the fallback
    # for a session with no 'outdir' of its own - creating it eagerly would recreate
    # exactly the unused doc/images/ this file's header says not to write to.
    failed, written = [], []
    for index, session in enumerate(wanted, 1):
        print('[%d/%d] %s' % (index, len(wanted), session['name']))
        try:
            written += run_session(session, args.outdir, args.scale,
                                   args.seed, args.min_rows, args.verbose)
        except (ScreenError, OSError) as exc:
            failed.append(session['name'])
            print(traceback.format_exc() if args.verbose else '    FAILED: %s' % exc,
                  file=sys.stderr)

    if failed:
        print('\n%d of %d sessions failed: %s' % (len(failed), len(wanted), ', '.join(failed)),
              file=sys.stderr)
        return 1
    # Sessions each name their own 'outdir' (see the header above), so a run can
    # easily span several folders - naming just one, as if there were a single
    # shared pool, would be as misleading as recreating that pool would be.
    dirs = sorted({os.path.relpath(os.path.dirname(path)) for path in written})
    print('\n%d screenshot%s in %s' % (len(written), '' if len(written) == 1 else 's',
                                       ', '.join(dirs) if dirs else '(nothing written)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
