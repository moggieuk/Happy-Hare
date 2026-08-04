# The screenshots the documentation needs.
#
#   make shots                        # regenerate everything into doc/images
#   make shots ARGS='--list'          # the sessions, and what each one covers
#   make shots ARGS='--only boxturtle-walkthrough'
#   make shots ARGS='--seed ~/printer_data/.mmu_config'   # against a real machine
#
# A SESSION IS ONE menuconfig, MANY IMAGES. Parsing the Kconfig tree costs several
# seconds, so a session starts the installer once, walks it, and captures along the
# way - `shot('name')` writes doc/images/name.png and carries on from where it is.
# Group screens that belong to the same walkthrough into one session; start a new one
# when the seed or the unit has to change.
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

from .capture import DEFAULT_SEED, IMAGES, MIN_ROWS, Menuconfig, ScreenError

# ---------------------------------------------------------------------------
# The sessions. Extend these; the runner needs no changes.
#
#   name     --only key, and the prefix for anything the session does not name
#   caption  what the session covers, for whoever writes the prose
#   scenes   f(mc, shot) - navigate, calling shot('image-name') at each screen
#   seed     a config to start from - a built-in name or a path (default: boxturtle)
#   min_rows shortest a fitted screenshot may be (default 30, for a consistent set)
#   fit      False to stop autofitting and honour 'rows' instead
#   rows     starting height; only meaningful with 'fit': False
#   unit_name / multi_unit / entry_point - inferred from the seed, override here
# ---------------------------------------------------------------------------


def _installer_tour(mc, shot):
    """The main screens a first-time installer sees, in the order they see them."""
    mc.select('MMU Type')
    shot('top-menu')

    mc.enter('MMU Type')
    shot('mmu-type')
    mc.back()

    mc.enter('MCU connection')
    shot('mcu-connection')
    mc.back()

    mc.enter('Toolhead sensors/settings')
    shot('toolhead-sensors')
    mc.back()

    mc.enter('Paths & Services')
    shot('paths-services')
    mc.back()


def _help_and_editing(mc, shot):
    """The two interactions worth showing once: per-item help, and editing a value."""
    mc.enter('MMU Type')
    mc.help()
    shot('item-help')
    mc.back()
    mc.back()

    mc.edit('Display name')
    shot('editor')
    mc.write('Turtle Left')
    shot('editor-typed')
    mc.cancel()


SESSIONS = [
    {
        'name': 'installer-tour',
        'caption': 'The main installer screens for a Box Turtle',
        'scenes': _installer_tour,
    },
    {
        'name': 'help-and-editing',
        'caption': 'Per-item help, and editing a parameter value',
        'scenes': _help_and_editing,
    },
]


def run_session(session, outdir, scale=2, seed=None, min_rows=None, verbose=False):
    """Run one session, returning the images it produced."""
    written = []
    context = {key: session[key] for key in ('unit_name', 'multi_unit', 'entry_point')
               if key in session}

    with Menuconfig(cols=session.get('cols', 100), rows=session.get('rows', 40),
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
        prog='python -m doc.shots',
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

    os.makedirs(args.outdir, exist_ok=True)
    failed, total = [], 0
    for index, session in enumerate(wanted, 1):
        print('[%d/%d] %s' % (index, len(wanted), session['name']))
        try:
            total += len(run_session(session, args.outdir, args.scale,
                                     args.seed, args.min_rows, args.verbose))
        except (ScreenError, OSError) as exc:
            failed.append(session['name'])
            print(traceback.format_exc() if args.verbose else '    FAILED: %s' % exc,
                  file=sys.stderr)

    if failed:
        print('\n%d of %d sessions failed: %s' % (len(failed), len(wanted), ', '.join(failed)),
              file=sys.stderr)
        return 1
    print('\n%d screenshot%s in %s' % (total, '' if total == 1 else 's',
                                       os.path.relpath(args.outdir)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
