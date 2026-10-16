# Happy Hare MMU Software
#
# Summarize what an install changed in the Happy Hare .cfg files, comparing the backup make
# install made (mmu.old-<timestamp>) with the newly installed mmu/base files. Values are
# compared, not text, so comments, layout and template wording don't show
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import argparse
import json
import os
import subprocess
import sys

from .parser import ConfigBuilder

RULE = "-" * 106


def coloured(colour, text):
    return "%s%s%s" % (os.getenv(colour, ""), text, os.getenv("C_OFF", "")) if text else ""


def options(path):
    """{section: {option: value}} of a .cfg file, outside its EXCLUDE FROM CONFIG BUILDER block"""
    config = ConfigBuilder(path)
    return {section: {option: (config.get(section, option) or "").strip()
                      for option in config.options(section)}
            for section in config.sections(scope="included")}


def read_replaced(path):
    """{(file, section, option)} of hand edits the build replaced with a menuconfig change"""
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {tuple(json.loads(line)) for line in f if line.strip()}


def file_changes(old, new, replaced=()):
    """(changed, removed, added) as [(section, line)] for one file's {section: {option: value}}"""
    changed, removed, added = [], [], []
    for section, values in old.items():
        if section not in new:
            removed.append((section, None))
            continue
        for option, value in values.items():
            if option not in new[section]:
                removed.append((section, "%s: %s" % (option, value)))
            elif new[section][option] != value and option != "gcode":
                note = " (your .cfg edit, replaced by a menuconfig change)" if (section, option) in replaced else ""
                changed.append((section, "%s: %s -> %s%s" % (option, value, new[section][option], note)))
    for section, values in new.items():
        if section not in old:
            added.append((section, None))
        for option, value in values.items():
            if option not in old.get(section, {}):
                added.append((section, "%s: %s" % (option, value)))
    return changed, removed, added


def show_lines(heading, rows, lines):
    lines.append(heading)
    current = None
    for section, line in rows:
        if line is None or section != current:
            lines.append("[%s]" % section)
            current = section
        if line is not None:
            lines.append(line)


def summary(backup_dir, install_dir, verbose=False, out=None, replaced=()):
    """Print the changes. False if nothing changed"""
    out = out or sys.stdout
    names = sorted(set(os.listdir(os.path.join(backup_dir, "base"))) | set(os.listdir(os.path.join(install_dir, "base"))))
    lines = []
    for name in [n for n in names if n.endswith(".cfg")]:
        old_path, new_path = (os.path.join(d, "base", name) for d in (backup_dir, install_dir))
        old = options(old_path) if os.path.exists(old_path) else {}
        new = options(new_path) if os.path.exists(new_path) else {}
        changed, removed, added = file_changes(old, new, {(s, o) for f, s, o in replaced if f == name})
        if not (changed or removed or added):
            continue
        lines += ["", "> %s:" % name]
        if changed:
            show_lines("Changed:", changed, lines)
        if removed:
            show_lines("Removed:", removed, lines)
        if added and verbose:
            show_lines("Added:", added, lines)
        elif added:
            sections = sum(1 for _, line in added if line is None)
            lines.append("Added: %d new option(s)%s" % (len(added) - sections,
                         " and %d new section(s)" % sections if sections else ""))

    shown = bool(lines)
    if shown:
        heading = "Summary of changes to your Happy Hare .cfg files (previous files are in '%s'):" \
            % os.path.basename(backup_dir)
    else:
        heading = "No changes to the values in your Happy Hare .cfg files"
    # Set apart as a block from the install output around it: the frame in NOTICE, the changes in INFO
    block = [("C_NOTICE", line) for line in ("", RULE, "", heading)] + [("C_INFO", line) for line in lines] + \
        [("C_NOTICE", line) for line in ("", RULE, "")]
    for colour, line in block:
        print(coloured(colour, line), file=out)
    if shown and verbose:
        word_diff(os.path.join(backup_dir, "base"), os.path.join(install_dir, "base"), out)
    return shown


def word_diff(old_dir, new_dir, out):
    """The text diff 'make diff' shows, when git is available"""
    try:
        result = subprocess.run(["git", "diff", "-U2", "--color", "--minimal", "--word-diff=color", "--no-index",
                                 "--", old_dir, new_dir], capture_output=True, text=True)
    except OSError:
        return
    print(result.stdout, file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarize what an install changed in the Happy Hare .cfg files")
    parser.add_argument("backup_dir", help="the mmu.old-<timestamp> backup this install made")
    parser.add_argument("install_dir", help="the installed mmu directory")
    parser.add_argument("--replaced", metavar="FILE", help="hand edits the build replaced (HH_REPLACED_REPORT)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not all(os.path.isdir(os.path.join(d, "base")) for d in (args.backup_dir, args.install_dir)):
        return 0
    try:
        summary(args.backup_dir, args.install_dir, args.verbose, replaced=read_replaced(args.replaced))
    except Exception as e:
        print("WARNING: couldn't summarize the .cfg changes (%s)" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
