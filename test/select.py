# Happy Hare test selector - the interactive front end for `make test`.
#
# The suite is 500+ tests and the cost is wildly uneven, so `make test` opens a file-level
# picker with everything already ticked. Enter runs the lot exactly as before; untick the
# expensive files and you get a focused run instead:
#
#   make test
#   > n            # none
#   > +nfc         # tick every file whose name contains 'nfc'
#   > <Enter>      # run those
#
# WHY THE TIMINGS ARE MEASURED HERE AND NOT WITH A TestResult. The obvious way to time a
# module is startTest/stopTest on a TestResult subclass, and it is wrong: setUpClass runs
# outside that window, and for this harness setUpClass is where nearly all the time is.
# Measured, test_mmu_config run on its own is ~35s wall of which the tests themselves are
# 0.08s - a per-test timer reports the 0.08 and hides the 35. So each module's suite is
# wrapped in a _TimedSuite that times its own run(), bracketing setUpModule/setUpClass too.
#
# WHY IT STILL ENDS UP IN unittest.main(). Only the *selection* happens here; the run is
# handed to unittest's own CLI, so -v/-k/-f/-b and the exit status keep working with no
# code of ours in the way. Everything ticked (or --all) runs the identical
# `discover -p '*'` the Makefile used to run directly.
#
# THE MENU IS SKIPPED whenever it cannot work or nobody asked for it: not a tty, --all,
# --last, --pattern, or a module that failed to import (see _discover). That predicate
# lives here rather than in the Makefile so there is one copy of it.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unittest

if __package__ in (None, ''):                       # allow `python test/select.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Remembered selection and per-file timings. Sibling of .mmu_config, and gitignored the
# same way. Losing it costs nothing: the menu just falls back to REFERENCE_FILE below
STATE_FILE = os.path.join(ROOT, '.mmu_test_state')

# Checked-in reference timings (test/benchmark.json), for anyone who has never run the
# suite on this machine. Unlike STATE_FILE this IS committed - see the file's own
# "_comment" for how it's regenerated. Read-only: a reference number must never be able to
# flow into STATE_FILE (see _load_reference/_save_state), or the next narrowed run would
# fold a fabricated measurement into the user's own history as if they had really run it.
REFERENCE_FILE = os.path.join(ROOT, 'test', 'benchmark.json')


# -- timing ------------------------------------------------------------------------

class _TimedSuite(unittest.TestSuite):
    """One module's tests, timing its own run - class fixtures included.

    Records (seconds, tests actually run). The count matters: a `-k` that matches nothing
    still runs every module's suite in ~0s, and without the count that would overwrite real
    measurements with zeros.
    """

    def __init__(self, tests, name, sink):
        super().__init__(tests)
        self.module_name = name
        self._sink = sink

    def run(self, result, debug=False):
        before = getattr(result, 'testsRun', 0)
        started = time.perf_counter()
        try:
            return super().run(result, debug)
        finally:
            ran = getattr(result, 'testsRun', 0) - before
            if ran > 0:
                elapsed = time.perf_counter() - started
                seconds, count = self._sink.get(self.module_name, (0.0, 0))
                self._sink[self.module_name] = (seconds + elapsed, count + ran)


class _TimingLoader(unittest.TestLoader):
    """Stock discovery, except every module's suite comes back as a _TimedSuite.

    loadTestsFromModule is the one hook that fires for both `discover` and an explicit
    list of dotted module names, which is why the timing hangs off it.
    """

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def loadTestsFromModule(self, module, *args, **kwargs):
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        return _TimedSuite(list(suite), module.__name__, self.sink)


def _collect(suite, out):
    """Flatten a discovered tree into {module name: test count}, module-deep only."""
    if isinstance(suite, _TimedSuite):
        count = suite.countTestCases()
        if count:
            out[suite.module_name] = out.get(suite.module_name, 0) + count
            return                                  # the module is the unit; stop here
    if isinstance(suite, unittest.TestSuite):
        for child in suite:
            _collect(child, out)


def _discover(sink, pattern='*'):
    """Return (loader, [(module, display, count)]) in stable display order.

    A module that fails to import never reaches loadTestsFromModule - unittest builds a
    placeholder suite for it directly - so it would silently vanish from the menu. The
    caller keys off loader.errors to fall back to a full run instead.
    """
    loader = _TimingLoader(sink)
    found = {}
    _collect(loader.discover(ROOT, pattern=pattern), found)
    entries = [(name, _display(name), count) for name, count in sorted(found.items())]
    return loader, entries


def _display(module):
    return module[5:] if module.startswith('test.') else module


# -- state -------------------------------------------------------------------------

def _parse_times(raw):
    """{module: [seconds, count]} (JSON's shape - a list, not a tuple) -> {module: (float, int)}.

    Shared by _load_state and _load_reference: both treat a malformed entry as simply
    unknown rather than a reason to reject the whole file.
    """
    records = {}
    for module, value in (raw or {}).items():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            records[module] = (float(value[0]), int(value[1]))
    return records


def _load_state():
    """Returns (selection, {module: (seconds, tests run)}). Junk in the file is ignored."""
    try:
        with open(STATE_FILE) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return [], {}
    if not isinstance(state, dict):
        return [], {}
    selection = [s for s in state.get('selection') or [] if isinstance(s, str)]
    return selection, _parse_times(state.get('times'))


def _load_reference():
    """{module: seconds} from the checked-in test/benchmark.json, or {} if it is missing or
    unreadable - a first-time user with no reference file just sees no estimate, same as
    today. Metadata keys other than "times" (git_rev, machine, ...) are for a human
    regenerating the file, not read here."""
    try:
        with open(REFERENCE_FILE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {module: seconds for module, (seconds, _) in _parse_times(data.get('times')).items()}


def _save_state(selection, records, filtered=False):
    """Merge into what's already saved, keeping the most complete measurement per file.

    A two-file run must not forget the other twenty - hence merging into `previous`
    rather than replacing it.

    A `-k`-narrowed run of one file must not replace that file's whole-file time with its
    own smaller one - hence `filtered`, which the caller sets from the actual argv.

    That used to be inferred from the test count instead (keep whichever entry has more
    tests), which cannot tell "I ran a subset of this file" from "tests were deleted from
    this file". Once a file's count DROPPED its stored timing froze permanently: every
    later full run measured it and was thrown away for having fewer tests than the stale
    entry. test_mmu_console sat at 143.03s against a real 40.5s that way - and since
    test/benchmark.json is regenerated by copying this file's 'times' block, the stale
    number was one `make ALL=1 test` away from being committed as everyone's reference.
    """
    _, previous = _load_state()
    for module, (seconds, count) in records.items():
        if not filtered or count >= previous.get(module, (0.0, 0))[1]:
            previous[module] = (seconds, count)
    payload = {'selection': list(selection),
               'times': {k: [round(v[0], 2), v[1]] for k, v in previous.items()}}
    try:
        with open(STATE_FILE, 'w') as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write('\n')
    except OSError as exc:                          # read-only checkout, odd permissions
        print('Could not save test selection to %s: %s' % (STATE_FILE, exc), file=sys.stderr)


# -- rendering ---------------------------------------------------------------------

def _colours():
    """The same C_* variables the Makefile exports for the python installer."""
    if not sys.stdout.isatty():
        return {k: '' for k in ('off', 'info', 'notice', 'warn')}
    return {
        'off': os.environ.get('C_OFF', ''),
        'info': os.environ.get('C_INFO', ''),
        'notice': os.environ.get('C_NOTICE', ''),
        'warn': os.environ.get('C_WARNING', ''),
    }


def _fmt_secs(seconds):
    if seconds is None:
        return '-'
    if seconds < 10:
        return '%.1fs' % seconds
    if seconds < 60:
        return '%.0fs' % seconds
    minutes, rest = divmod(int(round(seconds)), 60)
    return '%dm%02ds' % (minutes, rest)


def _render(entries, selected, times, local, order, c):
    """`times` is reference-first, local-second (see main()) so it covers every module the
    user has ever measured OR that test/benchmark.json ships; `local` is just the modules the
    USER has personally timed, needed here only to mark the rest as reference-sourced."""
    total = sum(count for _, _, count in entries)
    width = max(len(display) for _, display, _ in entries)
    reference_shown = any(module in times and module not in local for module, _, _ in entries)

    if local:
        suffix = '        times from last run (~ = reference, never run locally)'
    elif times:
        suffix = '        reference times only - never run locally'
    else:
        suffix = ''
    print()
    print('%sHappy Hare tests%s - %d tests in %d files%s' % (
        c['notice'], c['off'], total, len(entries), suffix))
    print()
    for index in order:
        module, display, count = entries[index]
        ticked = module in selected
        secs = times.get(module)
        shown = _fmt_secs(secs)
        if secs is not None and module not in local:
            shown += '~'
        row = '%4d %s %-*s %5d %9s' % (
            index + 1, '[x]' if ticked else '[ ]', width, display, count, shown)
        print('%s%s%s' % (c['info'] if ticked else '', row, c['off'] if ticked else ''))

    files = sum(1 for module, _, _ in entries if module in selected)
    tests = sum(count for module, _, count in entries if module in selected)
    known = [module for module, _, _ in entries if module in selected and module in times]
    known_local = sum(1 for module in known if module in local)
    missing = files - len(known)
    estimate = ''
    if known:
        total_known = sum(times[module] for module in known)
        marker = '~' if not missing else '>'
        if known_local == len(known):
            estimate = ' - %s%s last time' % (marker, _fmt_secs(total_known))
        elif known_local == 0:
            estimate = ' - %s%s expected (reference, never run locally)' % (
                marker, _fmt_secs(total_known))
        else:
            estimate = ' - %s%s expected (%d/%d files from your last run)' % (
                marker, _fmt_secs(total_known), known_local, len(known))
        if missing:
            estimate += ' (%d never timed)' % missing
    print()
    print('  selected: %d files - %d tests%s' % (files, tests, estimate))
    print()
    print('  %s[Enter]%s run    1 3 5-8 toggle    a all    n none    v invert'
          % (c['notice'], c['off']))
    print('  +TEXT / -TEXT tick by name    p previous selection    s sort by time    q quit')
    if reference_shown:
        print('  ~ marks a checked-in reference time (test/benchmark.json) - '
              'you have not run that file here yet')


# -- the menu ----------------------------------------------------------------------

def _toggle_tokens(line, entries, selected):
    """Apply '1 3 5-8' style toggles. Returns False on anything unparseable."""
    wanted = []
    for token in line.replace(',', ' ').split():
        bounds = token.split('-', 1) if '-' in token else [token, token]
        try:
            low, high = int(bounds[0]), int(bounds[1])
        except ValueError:
            return False
        if not 1 <= low <= high <= len(entries):
            return False
        wanted.extend(range(low - 1, high))
    for index in wanted:
        module = entries[index][0]
        if module in selected:
            selected.discard(module)
        else:
            selected.add(module)
    return True


def _menu(entries, times, local, previous):
    """Returns the chosen module names, or None if the user quit."""
    c = _colours()
    selected = {module for module, _, _ in entries}
    by_name = list(range(len(entries)))
    order = by_name
    message = ''

    while True:
        _render(entries, selected, times, local, order, c)
        if message:
            print('  %s%s%s' % (c['warn'], message, c['off']))
            message = ''
        try:
            line = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not line:
            if not selected:
                message = 'Nothing selected - tick something, or q to quit.'
                continue
            return [module for module, _, _ in entries if module in selected]

        head, rest = line[0], line[1:].strip()
        if head in '+-' and rest:
            matched = {module for module, display, _ in entries if rest.lower() in display.lower()}
            if not matched:
                message = 'No file name contains %r.' % rest
                continue
            if head == '+':
                selected |= matched
            else:
                selected -= matched
            continue

        command = line.lower()
        if command in ('q', 'quit'):
            return None
        if command in ('a', 'all'):
            selected = {module for module, _, _ in entries}
        elif command in ('n', 'none'):
            selected = set()
        elif command in ('v', 'i', 'invert'):
            selected = {module for module, _, _ in entries if module not in selected}
        elif command in ('p', 'prev', 'previous'):
            recalled = {module for module, _, _ in entries if module in previous}
            if not recalled:
                message = 'No previous selection saved.'
            else:
                selected = recalled
        elif command in ('s', 'sort'):
            # Display order only. The numbers stay bound to their files, so '1 3 5' always
            # toggles what it toggled before the sort
            if order is by_name:
                order = sorted(by_name, key=lambda i: -times.get(entries[i][0], -1.0))
            else:
                order = by_name
        elif not _toggle_tokens(line, entries, selected):
            message = 'Not understood: %r' % line


# -- entry point -------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='make test', add_help=False,
        description='Pick which test files to run. Unrecognised flags go to unittest.')
    parser.add_argument('--all', action='store_true', help='run everything, no menu')
    parser.add_argument('--last', action='store_true', help='re-run the last selection, no menu')
    parser.add_argument('--pattern', help='run files matching a glob, no menu (make UT=...)')
    parser.add_argument('--help', '-h', action='store_true', help='show this help')
    args, passthrough = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    if args.help:
        parser.print_help()
        return 0

    sink = {}
    loader, entries = _discover(sink, args.pattern or '*')
    previous, records = _load_state()
    local = {module: seconds for module, (seconds, _) in records.items()}
    times = _load_reference()
    times.update(local)          # the user's own measurement always wins

    if args.pattern:
        return _run(sink, ['discover', '-p', args.pattern] + passthrough, None, entries)

    if not entries:
        print('No tests found under %s' % ROOT, file=sys.stderr)
        return 1

    everything = [module for module, _, _ in entries]

    if loader.errors:
        # A module failed to import. It is not in `entries` and never will be, so offering
        # a menu here would quietly hide it. Run the lot and let it fail as it always has
        print('%d module(s) failed to import - running everything so the error shows.'
              % len(loader.errors), file=sys.stderr)
        return _run(sink, ['discover', '-p', '*'] + passthrough, None, entries)

    if args.last:
        # Ahead of the tty check: --last is an explicit "no menu, run this" and means the
        # same from a script as it does from a terminal
        chosen = [module for module in everything if module in previous]
        if not chosen:
            print('No previous selection saved - running everything.', file=sys.stderr)
            chosen = everything
    elif args.all or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _run(sink, ['discover', '-p', '*'] + passthrough, None, entries)
    else:
        chosen = _menu(entries, times, local, previous)
        if chosen is None:
            print('Nothing run.')
            return 1                                # so `make test && ...` does not proceed

    if set(chosen) == set(everything):
        return _run(sink, ['discover', '-p', '*'] + passthrough, None, entries)
    return _run(sink, list(chosen) + passthrough, chosen, entries)


def _run(sink, argv, selection, entries):
    """Hand off to unittest's own CLI, then persist what we learned.

    `selection` is None for a run that isn't a narrowing (everything, a UT pattern, an import
    failure), which leaves the remembered selection alone so `p` and LAST=1 stay useful after
    a full run.
    """
    program = unittest.main(module=None, argv=['unittest'] + argv,
                            testLoader=_TimingLoader(sink), exit=False)
    # Discovery runs a _TimedSuite for each package __init__ too. Those carry no tests and
    # would just be noise in the saved timings
    bearing = {module for module, _, _ in entries}
    measured = {module: value for module, value in sink.items() if module in bearing}
    narrowed = selection is not None and 0 < len(selection) < len(entries)
    # -k runs a SUBSET of every file it touches, so those timings are not whole-file
    # timings and must not displace one. Nothing else here does that: --pattern (make UT=)
    # narrows which FILES are discovered, and each of those still runs in full.
    filtered = any(arg == '-k' or arg.startswith('-k') for arg in argv)
    _save_state(list(selection) if narrowed else _load_state()[0], measured, filtered=filtered)
    return 0 if program.result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
