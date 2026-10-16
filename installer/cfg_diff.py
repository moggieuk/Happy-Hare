# Happy Hare MMU Software
#
# Summarize what an install changed in the Happy Hare .cfg files, comparing the backup the
# install made (mmu.old-<timestamp>) with the newly installed mmu/base files. Values are
# compared, not text, so comments, layout and template wording don't show
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import argparse
import glob
import json
import os
import re
import subprocess
import sys

from .parser import ConfigBuilder

_BACKUP = re.compile(r"^mmu\.old-(\d{8}-\d{6})(-\d+)?$")


def backup_since(config_home, since):
    """The newest mmu.old-<timestamp> backup made at or after 'since', or None"""
    found = []
    for path in glob.glob(os.path.join(config_home, "mmu.old-*")):
        m = _BACKUP.match(os.path.basename(path))
        if m and os.path.isdir(path) and m.group(1) >= since:
            found.append((m.group(1), int((m.group(2) or "-0")[1:]), path))
    return max(found)[2] if found else None


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


def show_lines(heading, rows, out):
    print(heading, file=out)
    current = None
    for section, line in rows:
        if line is None:
            print("[%s]" % section, file=out)
            current = section
            continue
        if section != current:
            print("[%s]" % section, file=out)
            current = section
        print(line, file=out)


def summary(backup_dir, install_dir, verbose=False, out=None, replaced=()):
    """Print the changes. False if nothing changed"""
    out = out or sys.stdout
    names = sorted(set(os.listdir(os.path.join(backup_dir, "base"))) | set(os.listdir(os.path.join(install_dir, "base"))))
    shown = False
    for name in [n for n in names if n.endswith(".cfg")]:
        old_path, new_path = (os.path.join(d, "base", name) for d in (backup_dir, install_dir))
        old = options(old_path) if os.path.exists(old_path) else {}
        new = options(new_path) if os.path.exists(new_path) else {}
        changed, removed, added = file_changes(old, new, {(s, o) for f, s, o in replaced if f == name})
        if not (changed or removed or added):
            continue
        if not shown:
            print("Changes to your Happy Hare .cfg files (previous files are in '%s'):" % os.path.basename(backup_dir),
                  file=out)
            shown = True
        print("From %s:" % name, file=out)
        if changed:
            show_lines("Changed:", changed, out)
        if removed:
            show_lines("Removed:", removed, out)
        if added and verbose:
            show_lines("Added:", added, out)
        elif added:
            sections = sum(1 for _, line in added if line is None)
            print("Added: %d new option(s)%s" % (len(added) - sections,
                  " and %d new section(s)" % sections if sections else ""), file=out)
        print(file=out)
    if not shown:
        print("No changes to the values in your Happy Hare .cfg files", file=out)
    elif verbose:
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
    parser.add_argument("config_home")
    parser.add_argument("--since", required=True, help="YYYYMMDD-HHMMSS the install started")
    parser.add_argument("--replaced", metavar="FILE", help="hand edits the build replaced (HH_REPLACED_REPORT)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config_home = os.path.expanduser(args.config_home)
    install_dir = os.path.join(config_home, "mmu")
    backup_dir = backup_since(config_home, args.since)
    if backup_dir is None or not os.path.isdir(os.path.join(backup_dir, "base")) or \
            not os.path.isdir(os.path.join(install_dir, "base")):
        return 0
    try:
        summary(backup_dir, install_dir, args.verbose, replaced=read_replaced(args.replaced))
    except Exception as e:
        print("WARNING: couldn't summarize the .cfg changes (%s)" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
