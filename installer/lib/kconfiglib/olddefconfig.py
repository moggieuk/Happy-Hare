#!/usr/bin/env python3

# Copyright (c) 2018-2019, Ulf Magnusson
# SPDX-License-Identifier: ISC

"""
Updates an old .config file or creates a new one, by filling in default values
for all new symbols. This is the same as picking the default selection for all
symbols in oldconfig, or entering the menuconfig interface and immediately
saving.

The default input/output filename is '.config'. A different filename can be
passed in the KCONFIG_CONFIG environment variable.

When overwriting a configuration file, the old version is saved to
<filename>.old (e.g. .config.old).
"""
import re
import sys

import kconfiglib

# Happy Hare: only settings that carry HH_DEFAULT_TOKEN; other symbols are plumbing for these
_SETTING_PREFIXES = ('PARAM_', 'VAR_', 'PIN_', 'BOOL_', 'MMU_HAS_', 'CHOICE_', 'UNSELECT_')

# Happy Hare: a *_PREVIOUS member re-offers the saved value, so moving onto it changes nothing
_PREVIOUS_MEMBER = re.compile(r'_PREVIOUS(_\d+)?$')


def main():
    kconf = kconfiglib.standard_kconfig(__doc__)
    filename = kconfiglib.standard_config_filename()
    before = read_values(kconf, filename) # Happy Hare: Added
    print(kconf.load_config())
    print(kconf.write_config())
    # Happy Hare: stdout is discarded by the Makefile, so the report goes to stderr
    if before is not None:
        for line in change_report(kconf, before, read_values(kconf, filename) or {}):
            print(line, file=sys.stderr)


# Happy Hare: Added - report values the refresh changed without the user asking

def read_values(kconf, filename):
    """Map each assigned symbol name to (raw value, saved-as-default), or None if unreadable."""
    try:
        with open(filename, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    values = {}
    for line in lines:
        match = kconf._set_match(line)
        if match:
            name, val, default = match.groups()
            values[name] = (val, default is not None)
            continue
        match = kconf._unset_match(line)
        if match:
            name, default = match.groups()
            values[name] = ("n", default is not None)
    return values


def change_report(kconf, before, after):
    """Lines describing how 'after' differs from 'before' (empty when nothing changed)."""
    before = _renamed_to_current(before)
    changed, dropped, choices_seen = [], [], set()
    for name, (old, was_default) in before.items():
        sym = kconf.syms.get(name)
        if sym is None or not sym.nodes:
            dropped.append(name)
            continue

        if sym.choice is not None:
            choice = sym.choice
            if choice in choices_seen:
                continue
            choices_seen.add(choice)
            old_sel = _choice_selection(choice, before)
            new_sel = _choice_selection(choice, after)
            if old_sel != new_sel and (choice.name or "").startswith("CHOICE_") and \
               not (new_sel and _PREVIOUS_MEMBER.search(new_sel)):
                was_default = before.get(old_sel, (None, True))[1]
                changed.append((choice.name, _member(choice, old_sel), _member(choice, new_sel), was_default))
            continue

        new = after.get(name, (None,))[0]
        if _canonical(sym, new) != _canonical(sym, old) and name.startswith(_SETTING_PREFIXES):
            changed.append((name, old, new if new is not None else "(no longer applies)", was_default))

    added = [name for name in after if name not in before and
             not (kconf.syms.get(name) and kconf.syms[name].choice is not None)]

    lines = []
    for heading, want_default in (("Defaults recomputed:", True),
                                  ("Explicit settings that no longer apply:", False)):
        rows = [c for c in changed if c[3] == want_default]
        if rows:
            lines.append("  " + heading)
            lines.extend("    {}: {} -> {}".format(n, o, v) for n, o, v, _ in rows)
    if dropped:
        lines.append("  No longer defined, dropped: " + ", ".join(dropped))
    if added:
        lines.append("  {} new option(s) set to their defaults".format(len(added)))
    return lines


def _renamed_to_current(values):
    """File a value saved under a renamed symbol under its successor, as load_config migrates it."""
    renamed = {}
    for name, value in values.items():
        new_name = kconfiglib.HH_RENAMED_SYMBOLS.get(name)
        if new_name and new_name not in values:
            renamed[new_name] = value
        elif name not in kconfiglib.HH_RENAMED_SYMBOLS:
            renamed.setdefault(name, value)
    return renamed


def _canonical(sym, raw):
    """A raw value in a form comparable across a type change (a migrated "0.45" is 0.45)."""
    if raw is None:
        return None
    match = kconfiglib._conf_string_match(raw)
    if match:
        raw = kconfiglib.unescape(match.group(1))
    if sym.orig_type in (kconfiglib.BOOL, kconfiglib.TRISTATE, kconfiglib.BOOLINT):
        raw = "y" if raw in ("y", "1") else "m" if raw == "m" else "n"
    return raw


def _choice_selection(choice, values):
    for member in choice.syms:
        if values.get(member.name, ("n",))[0] == "y":
            return member.name
    return None


def _member(choice, name):
    if name is None:
        return "(none)"
    prefix = choice.name + "_"
    return name[len(prefix):] if name.startswith(prefix) else name


if __name__ == "__main__":
    main()
