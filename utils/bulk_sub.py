#!/usr/bin/env python3
"""
bulk_sub.py — recursively apply regex substitutions to *.py and *.cfg files.

Features
- Dry-run (default): report what would change.
- Apply mode: write changes, backing up originals to "<file>.orig".
- Optional unified diff output for each changed file.
- Loads (pattern, replacement) pairs from:
    1) Built-in DEFAULT_PATTERNS (edit below), or
    2) --rules FILE where FILE is .json (list of [pattern, repl] pairs)
       or .py file that defines PATTERNS = [(pattern, repl), ...].

Usage examples
--------------
# Report what would change and show diffs
python bulk_sub.py --diff

# Actually apply changes (still can show diffs)
python bulk_sub.py --apply --diff

# Use a JSON rules file
python bulk_sub.py --rules rules.json --apply

# Use a Python rules file that defines PATTERNS
python bulk_sub.py --rules rules.py --diff

# Preview revert changes
python bulk_sub.py --restore --diff

# Revert changes and clean up last backup
python bulk_sub.py --restore --apply --restore-clean

#
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import fnmatch
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

# ---- Edit these if you want in-script rules ---------------------------------
# Important: these are applied in order listed

# Set A - sensor names (mmu_pre_gate -> mmu_entry, mmu_gear -> mmu_exit, mmu_gate --> mmu_shared_exit)
DEFAULT_PATTERNS: List[Tuple[str, str]] = [
    # Example:
    # (r"(?<!_)mmu_gear(?!_)", "mmu_gear"),
    ("PARAM_MMU_VENDOR",  "PARAM_VENDOR"),
    ("PARAM_MMU_VERSION", "PARAM_VERSION"),

#    ("SENSOR_PRE_GATE_PREFIX", "SENSOR_ENTRY_PREFIX"),
#    ("SENSOR_GEAR_PREFIX", "SENSOR_EXIT_PREFIX"),
#    ("SENSOR_GATE", "SENSOR_SHARED_EXIT"),
#    ("mmu_pre_gate", "mmu_entry"),
#    ("pre_gate_switch_pin", "mmu_entry_switch_pin"),
#    ("post_gear_switch_pin", "mmu_exit_switch_pin"),
#    ("gear_switch_pin", "mmu_shared_exit_switch_pin"),
#    ("(?<!_)mmu_gear(?!_)", "mmu_exit"),
#    ("mmu_gate(?!_map)", "mmu_shared_exit"),
#    ("(?<!stepper_)mmu_gear(?!_touch|_rotation)", "mmu_exit"),
#    ("pre_gate_sensors", "entry_sensors"),
#    ("gear sensor", "mmu exit sensor"),
#    ("pre-gate", "mmu entry"),
#    ("PRE_GATE", "ENTRY"),
#    ("POST_GEAR", "EXIT"),


#    ("(?<!CALIBRATE_)GEAR(?!_(?:0|RDS|STEPPER)\b)", "EXIT"),
#    ("gates/lanes", "lanes"),
#    ("GATE", "LANE"),
#    ("Gate", "Lane"),
#    ("gate", "lane"),
#
#    ("TTG", "TTL"),
#    ("ttg", "ttl"),
]

# Set B - Gate >> Lane
#DEFAULT_PATTERNS: List[Tuple[str, str]] = [
#    ("gates/lanes", "lanes"),
#    ("GATE", "LANE"),
#    ("Gate", "Lane"),
#    ("gate", "lane"),
#]

# Set C - TTG >> TTL
#DEFAULT_PATTERNS: List[Tuple[str, str]] = [
#    ("TTG", "TTL"),
#    ("ttg", "ttl"),
#]

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    "build", "dist", ".mypy_cache", ".ruff_cache", ".pytest_cache", "utils"
}
# PAUL TARGET_EXTS = (".py", ".cfg", "Kconfig*")
TARGETS = ("*.py", "*.cfg", "Kconfig*", "Makefile", "*.sh")


# ---- Diff colorization -------------------------------------------------------
def _want_color(choice: str) -> bool:
    if choice == "always":
        return True
    if choice == "never":
        return False
    # auto
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()

def _enable_windows_ansi():
    # On Windows, try to enable ANSI; no hard dependency.
    if os.name == "nt":
        try:
            import colorama  # type: ignore
            colorama.just_fix_windows_console()
        except Exception:
            pass

class _C:
    reset  = "\033[0m"
    bold   = "\033[1m"
    dim    = "\033[2m"
    red    = "\033[31m"
    green  = "\033[32m"
    yellow = "\033[33m"
    magenta= "\033[35m"
    gray   = "\033[90m"

def colorize_unified_diff_line(line: str, use_color: bool) -> str:
    if not use_color:
        return line
    # File headers
    if line.startswith("+++ ") or line.startswith("--- "):
        return f"{_C.gray}{_C.bold}{line}{_C.reset}"
    # Hunk headers
    if line.startswith("@@"):
        return f"{_C.magenta}{_C.bold}{line}{_C.reset}"
    # Additions (but not +++ header)
    if line.startswith("+") and not line.startswith("+++"):
        return f"{_C.green}{line}{_C.reset}"
    # Deletions (but not --- header)
    if line.startswith("-") and not line.startswith("---"):
        return f"{_C.red}{line}{_C.reset}"
    # “No newline at end of file” marker
    if line.startswith("\\"):
        return f"{_C.yellow}{_C.dim}{line}{_C.reset}"
    return line

def load_patterns(path: str | None) -> List[Tuple[str, str]]:
    if not path:
        return DEFAULT_PATTERNS
    p = Path(path)
    if not p.exists():
        sys.exit(f"[error] Rules file not found: {p}")
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            sys.exit(f"[error] Failed to parse JSON rules: {e}")
        if not isinstance(data, list):
            sys.exit("[error] JSON rules must be [[pattern, repl], ...]")
        pairs = []
        for i, item in enumerate(data):
            if not (isinstance(item, list) and len(item) == 2 and
                    all(isinstance(x, str) for x in item)):
                sys.exit(f"[error] JSON rules item #{i+1} must be [pattern, repl]")
            pairs.append((item[0], item[1]))
        return pairs
    elif p.suffix.lower() == ".py":
        ns: dict = {}
        code = p.read_text(encoding="utf-8")
        exec(compile(code, str(p), "exec"), ns, ns)  # nosec
        if "PATTERNS" not in ns:
            sys.exit("[error] Python rules file must define PATTERNS")
        pairs = ns["PATTERNS"]
        if not (isinstance(pairs, list) and all(isinstance(t, tuple) and len(t) == 2 for t in pairs)):
            sys.exit("[error] PATTERNS must be a list of (pattern, repl)")
        if not all(isinstance(a, str) and isinstance(b, str) for a, b in pairs):
            sys.exit("[error] Each (pattern, repl) must be strings")
        return pairs
    else:
        sys.exit("[error] Unsupported rules file type. Use .json or .py")


def iter_target_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            if name.endswith(".orig"):
                continue
            if any(fnmatch.fnmatchcase(name, pat) for pat in TARGETS):
# PAUL            if name.lower().endswith(TARGET_EXTS):
                yield Path(dirpath) / name


def compile_patterns(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[re.Pattern, str]]:
    compiled = []
    for i, (pat, repl) in enumerate(pairs, start=1):
        try:
            compiled.append((re.compile(pat, flags=re.MULTILINE), repl))
        except re.error as e:
            sys.exit(f"[error] Invalid regex in rule #{i}: {e}\n    pattern: {pat}")
    return compiled


def backup_path_for(original: Path) -> Path:
    base = Path(str(original) + ".orig")
    if not base.exists():
        return base
    ts = time.strftime("%Y%m%d-%H%M%S")
    return Path(f"{original}.orig.{ts}")


def apply_substitutions(text: str, rules: List[Tuple[re.Pattern, str]]):
    new_text = text
    details = []
    for rx, repl in rules:
        new_text, n = rx.subn(repl, new_text)
        if n:
            details.append((rx.pattern, repl, n))
    return new_text, details


# -------------------------- Revert helpers ------------------------------------
_BACKUP_SUFFIX_RE = re.compile(r"(.*)\.orig(?:\..+)?$")

def _collect_backups(root: Path) -> dict[Path, Path]:
    """
    Find backups under root. Returns a mapping of original_path -> chosen_backup_path.
    If multiple backups exist for an original, pick the most-recent by mtime.
    """
    mapping: dict[Path, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            if name.endswith(".orig") or ".orig." in name:
                bpath = Path(dirpath) / name
                m = _BACKUP_SUFFIX_RE.match(str(bpath))
                if not m:
                    continue
                orig = Path(m.group(1))
                # Only revert backups for targeted file types
# PAUL                if orig.suffix.lower() not in TARGET_EXTS:
                if not any(fnmatch.fnmatchcase(orig.suffix, pat) for pat in TARGETS):
                    continue
                prev = mapping.get(orig)
                if not prev or bpath.stat().st_mtime > prev.stat().st_mtime:
                    mapping[orig] = bpath
    return mapping


def _list_all_backups_for(orig: Path) -> list[Path]:
    """Return all backups for 'orig' in the same directory: .orig and .orig.*"""
    d = orig.parent
    base = orig.name
    # Only files, ignore dirs
    results = [p for p in d.glob(base + ".orig") if p.is_file()]
    results += [p for p in d.glob(base + ".orig.*") if p.is_file()]
    return results


def _revert_backups(root: Path, apply: bool, diff: bool, encoding: str,
                    use_color: bool = False, clean: bool = False, clean_all: bool = False) -> int:
    backups = _collect_backups(root)
    if not backups:
        print("[info] No backups found (.orig or .orig.TIMESTAMP) under", root)
        return 0

    restored = 0
    deleted_backups = 0

    for orig, bpath in sorted(backups.items()):
        try:
            backup_text = bpath.read_text(encoding=encoding, errors="surrogateescape")
        except Exception as e:
            print(f"[warn] Skipping unreadable backup: {bpath} ({e})", file=sys.stderr)
            continue

        current_text = ""
        exists = orig.exists()
        if exists:
            try:
                current_text = orig.read_text(encoding=encoding, errors="surrogateescape")
            except Exception as e:
                print(f"[warn] Cannot read current file {orig}: {e}", file=sys.stderr)

        print(f"\n=== REVERT {orig} ===")
        print(f"  backup: {bpath}")
        if diff:
            a = current_text.splitlines(keepends=True) if exists else []
            b = backup_text.splitlines(keepends=True)
            for line in difflib.unified_diff(
                a, b,
                fromfile=str(orig),
                tofile=str(orig) + " (from backup)",
                lineterm=""
            ):
                # if not using colors, replace with: print(line, end="")
                print(colorize_unified_diff_line(line, use_color), end="")

        if apply:
            try:
                orig.write_text(backup_text, encoding=encoding)
                restored += 1
                print(f"[apply] Restored from backup: {bpath} -> {orig}")

                # Decide which backups to remove
                to_delete: list[Path] = []
                if clean_all:
                    to_delete = _list_all_backups_for(orig)
                elif clean:
                    to_delete = [bpath]

                if to_delete:
                    # Avoid deleting the same file twice if lists overlap
                    seen = set()
                    for bp in to_delete:
                        if bp in seen:
                            continue
                        seen.add(bp)
                        try:
                            bp.unlink()
                            deleted_backups += 1
                            print(f"[apply] Deleted backup: {bp}")
                        except Exception as e:
                            print(f"[warn] Restore succeeded, but failed to delete backup {bp}: {e}",
                                  file=sys.stderr)
            except Exception as e:
                print(f"[error] Failed to restore {orig}: {e}", file=sys.stderr)
        else:
            print("[dry-run] Would restore from backup")
            if clean_all:
                cands = _list_all_backups_for(orig)
                for bp in cands:
                    print(f"[dry-run] Would delete backup: {bp}")
            elif clean:
                print(f"[dry-run] Would delete backup: {bpath}")

    mode = "APPLY" if apply else "DRY-RUN"
    clean_mode = "all" if clean_all else ("used" if clean else "off")
    print("\nRevert summary:")
    print(f"  Backups found  : {len(backups)}")
    print(f"  Files restored : {restored} (mode: {mode}, diff={'on' if diff else 'off'})")
    print(f"  Backups deleted: {deleted_backups} (clean={clean_mode})")
    return 0

# -----------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Recursively apply regex substitutions to *.py and *.cfg files, with backup & revert."
    )
    ap.add_argument("--apply", action="store_true",
                    help="Write changes to files (default is dry-run).")
    ap.add_argument("--diff", action="store_true",
                    help="Show unified diff for each changed/restored file.")
    ap.add_argument("--revert", "--restore", dest="revert", action="store_true",
                    help="Restore files from .orig/.orig.TIMESTAMP backups. Honors --apply.")
    ap.add_argument("--revert-clean", "--restore-clean", dest="revert_clean", action="store_true",
                    help="After a successful restore, delete the backup file that was used.")
    ap.add_argument("--revert-clean-all", "--restore-clean-all", dest="revert_clean_all", action="store_true",
                    help="After a successful restore, delete ALL backups for that file.")
    ap.add_argument("--rules", metavar="FILE", default=None,
                    help="Rules file (.json with [[pattern, repl], ...] or .py defining PATTERNS).")
    ap.add_argument("--root", metavar="DIR", default=".",
                    help="Root directory to start from (default: .)")
    ap.add_argument("--encoding", default="utf-8",
                    help="Text encoding for reading/writing files (default: utf-8).")
    ap.add_argument(
        "--color", choices=["auto", "always", "never"], default="auto",
        help="Colorize diff output (default: auto)"
    )

    args = ap.parse_args(argv)

    root = Path(args.root).resolve()

    use_color = _want_color(args.color)
    if use_color:
        _enable_windows_ansi()

    # Revert mode takes precedence and ignores substitution logic.
    if args.revert:
        # If both flags set, favor 'all'
        clean_all = args.revert_clean_all
        clean_used = args.revert_clean and not clean_all

        return _revert_backups(
            root=root,
            apply=args.apply,
            diff=args.diff,
            encoding=args.encoding,
            use_color=use_color,
            clean=clean_used,
            clean_all=clean_all
        )


    patterns = load_patterns(args.rules)
    if not patterns:
        print("[warn] No patterns specified; nothing to do. Add to DEFAULT_PATTERNS or use --rules.", file=sys.stderr)
        return 0

    rules = compile_patterns(patterns)

    total_files = 0
    changed_files = 0
    total_subs = 0

    for path in iter_target_files(root):
        total_files += 1
        try:
            text = path.read_text(encoding=args.encoding, errors="surrogateescape")
        except Exception as e:
            print(f"[warn] Skipping unreadable file: {path} ({e})", file=sys.stderr)
            continue

        new_text, details = apply_substitutions(text, rules)
        if not details:
            continue

        changed_files += 1
        subs_for_file = sum(n for _, _, n in details)
        total_subs += subs_for_file

        print(f"\n=== {path} ===")
        for pat, repl, n in details:
            print(f"  {n:>6} × {pat!r}  ->  {repl!r}")

        if args.diff:
            a = text.splitlines(keepends=True)
            b = new_text.splitlines(keepends=True)
            for line in difflib.unified_diff(
                a, b,
                fromfile=str(path) + ".orig",
                tofile=str(path),
                lineterm=""
            ):
                print(colorize_unified_diff_line(line, use_color), end="")

        if args.apply:
            try:
                bpath = backup_path_for(path)
                bpath.write_text(text, encoding=args.encoding)
                path.write_text(new_text, encoding=args.encoding)
                print(f"[apply] Wrote changes. Backup saved as: {bpath}")
            except Exception as e:
                print(f"[error] Failed to write changes for {path}: {e}", file=sys.stderr)

    print("\nSummary:")
    print(f"  Scanned files : {total_files}")
    print(f"  Changed files : {changed_files}")
    print(f"  Total subs    : {total_subs}")
    print(f"  Mode          : {'APPLY' if args.apply else 'DRY-RUN'} (diff={'on' if args.diff else 'off'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

