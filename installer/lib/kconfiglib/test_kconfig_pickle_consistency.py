#!/usr/bin/env python3
"""
Regression test: verify that every explicit CONFIG_* assignment in a raw
.mmu_config-style file survives correctly into the pickled dict produced by
KConfig.as_dict() / pre_parse_kconfig().

Why this exists
---------------
Several past bugs in as_dict() looked fine on the surface but silently
dropped or mis-typed values that were explicitly set in a raw .mmu_config
file -- e.g.:
  - An invisible/promptless STRING symbol's assigned value being discarded
    in favor of its unconditional default (str_value ignores user_value
    when the symbol isn't visible).
  - A BOOL/TRISTATE symbol's user_value being an int tri-state (0/1/2)
    rather than "n"/"m"/"y", silently comparing unequal to "y" and being
    read as disabled even when explicitly enabled.
  - The same visibility trap for choice selections (Choice.selection vs.
    Choice.user_selection).

This test catches that whole class of bug automatically, for any Kconfig
project, without needing to already know which symbols are affected.

How it works
------------
It independently reloads the raw .mmu_config-style file with a bare
kconfiglib.Kconfig (no Happy-Hare-specific code at all -- deliberately NOT
using KConfig.as_dict(), since comparing that function's output against
itself would never catch a bug in it). For every symbol the file explicitly
assigns (sym.user_value is not None), it computes what as_dict() *should*
produce for that symbol from first principles, and compares that against
what's actually sitting in the pickle's "values" dict.

Usage
-----
    python3 test_kconfig_pickle_consistency.py <raw_config_file> <pickle_file>

Exit code is 0 if everything matches, 1 otherwise (with a mismatch report
printed to stdout). Can also be imported and used as a pytest-style
assertion via verify_pickle_matches_config().
"""

import argparse
import re
import sys

import kconfiglib

# Same array-grouping pattern and cutoff as KConfig.as_dict(). This is
# intentionally duplicated (not imported) since it's simple, mechanical
# index math -- not the type-normalization logic we're actually trying to
# validate independently.
_ARRAY_SUFFIX_RE = re.compile(r"^(.+_)(\d+)$")
_MAX_ARRAY_INDEX = 12


def _expected_value(sym_obj):
    """
    Independently compute what as_dict() should produce for this symbol,
    from first principles -- NOT by calling as_dict() itself.

    Prefers the symbol's raw user_value (what was actually written in the
    .config file) over the visibility-gated str_value, since that's the
    behavior being validated. Falls back to str_value for symbols with no
    explicit assignment (not exercised by this test, since we only ever
    call this for symbols where user_value is not None -- see below).
    """
    raw = sym_obj.user_value if sym_obj.user_value is not None else sym_obj.str_value

    # BOOL/BOOLINT/TRISTATE user_value is stored as an internal tristate integer.
    if sym_obj.type == kconfiglib.BOOLINT:
        return "1" if raw in (1, 2, "1", "y") else "0"

    if sym_obj.type in (kconfiglib.BOOL, kconfiglib.TRISTATE) and isinstance(raw, int):
        raw = kconfiglib.TRI_TO_STR[raw]

    if sym_obj.type == kconfiglib.BOOL:
        return raw == "y"
    elif sym_obj.type == kconfiglib.TRISTATE:
        return raw
    else:
        return raw.replace("\\n", "\n")


def verify_pickle_matches_config(config_file, pickle_values, base_kconfig="Kconfig"):
    """
    Returns a list of (symbol_name, expected, actual) mismatches between the
    raw config_file's explicit assignments and pickle_values (the "values"
    dict from a pickle, i.e. KConfig.as_dict()'s output).

    An empty list means everything explicitly assigned in config_file is
    correctly reflected in pickle_values.
    """
    kc = kconfiglib.Kconfig(base_kconfig)
    kc.load_config(config_file, filter_defaults=False)

    mismatches = []

    for name, sym in kc.syms.items():
        if sym.type == kconfiglib.UNKNOWN:
            continue
        if sym.user_value is None:
            # Never explicitly assigned in this file -- nothing to check
            # (as_dict() is free to fall back to its own default here, and
            # we have no independent "ground truth" for it from this file
            # alone).
            continue

        expected = _expected_value(sym)

        if sym.choice is not None:
            # Choice members: as_dict() only inserts an entry for the
            # selected (True) member -- an unselected member being absent
            # is correct, not a mismatch. But if the selected member is
            # missing, or an unselected one is present with the wrong
            # value, that's a real bug.
            if expected is True:
                if pickle_values.get(name) is not True:
                    mismatches.append((name, expected, pickle_values.get(name, "<missing>")))
            else:
                if name in pickle_values and pickle_values[name] is not False:
                    mismatches.append((name, expected, pickle_values[name]))
            continue

        m = _ARRAY_SUFFIX_RE.match(name)
        if m and int(m.group(2)) <= _MAX_ARRAY_INDEX:
            key, idx = m.group(1), int(m.group(2))
            group = pickle_values.get(key)
            if not isinstance(group, list) or idx >= len(group) or group[idx] != expected:
                got = group[idx] if isinstance(group, list) and idx < len(group) else "<missing>"
                mismatches.append((name, expected, f"pickle_values[{key!r}][{idx}] = {got!r}"))
            continue

        if name not in pickle_values:
            mismatches.append((name, expected, "<missing from pickle>"))
        elif pickle_values[name] != expected:
            mismatches.append((name, expected, pickle_values[name]))

    return mismatches


def main():
    import pickle

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config_file", help="Raw .mmu_config-style value file, e.g. .mmu_config_unit0")
    ap.add_argument("pickle_file", help="Pickle produced by pre_parse_kconfig(), e.g. out/.mmu_config_unit0.pickle")
    ap.add_argument("--kconfig", default="Kconfig", help="Base Kconfig menu file (default: Kconfig)")
    args = ap.parse_args()

    with open(args.pickle_file, "rb") as f:
        data = pickle.load(f)

    mismatches = verify_pickle_matches_config(args.config_file, data["values"], args.kconfig)

    if mismatches:
        print(f"FAILED -- {len(mismatches)} mismatch(es) between "
              f"{args.config_file} and {args.pickle_file}:")
        for name, expected, got in mismatches:
            print(f"  {name}: file says {expected!r}, pickle has {got!r}")
        sys.exit(1)

    print(f"OK -- {args.config_file} matches {args.pickle_file}")


if __name__ == "__main__":
    main()
