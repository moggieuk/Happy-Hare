#!/usr/bin/env python3
"""
sub.py

Recursively find *.py files under the current directory and replace a literal
string with another literal string.

Examples:
  python sub.py "old" "new" --dry-run
  python sub.py "foo(" "bar(" --backup
  python sub.py "x" "y" --exclude "*/venv/*" "*/.git/*"
"""

from __future__ import annotations

import argparse
import fnmatch
from pathlib import Path


def _matches_any(path_str: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path_str, pat) for pat in patterns)


def iter_py_files(root: Path, include: list[str], exclude: list[str]):
    for p in root.rglob("*.py"):
        if not p.is_file():
            continue

        rel = p.relative_to(root).as_posix()

        # Exclude first (so you can exclude even if include matches)
        if exclude and _matches_any(rel, exclude):
            continue

        # If include is provided, require a match
        if include and not _matches_any(rel, include):
            continue

        yield p


def process_file(
    path: Path,
    old: str,
    new: str,
    *,
    dry_run: bool,
    count_only: bool,
    backup: bool,
    encoding: str,
) -> tuple[int, bool]:
    """
    Returns (num_replacements, changed)
    """
    try:
        text = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        # Fallback: try utf-8-sig or skip
        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception:
            return (0, False)
    except Exception:
        return (0, False)

    occurrences = text.count(old)
    if occurrences == 0:
        return (0, False)

    if count_only:
        return (occurrences, False)

    new_text = text.replace(old, new)
    changed = new_text != text

    if changed and not dry_run:
        if backup:
            bak_path = path.with_suffix(path.suffix + ".bak")
            # Avoid overwriting an existing backup accidentally
            if not bak_path.exists():
                bak_path.write_text(text, encoding=encoding)
        path.write_text(new_text, encoding=encoding)

    return (occurrences, changed)


def main():
    parser = argparse.ArgumentParser(
        description="Recursively replace a literal string in all *.py files under the current directory."
    )
    parser.add_argument("old", help="Literal string to search for")
    parser.add_argument("new", help="Literal string to replace with")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing files")
    parser.add_argument("--count-only", action="store_true", help="Only count occurrences; do not write")
    parser.add_argument("--backup", action="store_true", help="Write a .bak file before modifying")
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding to use for read/write (default: utf-8)",
    )
    parser.add_argument(
        "--include",
        nargs="*",
        default=[],
        help='Optional glob(s) to include (relative paths), e.g. "src/*" "tests/*"',
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=["*/.git/*", "*/__pycache__/*", "*/venv/*", "*/.venv/*", "*/site-packages/*"],
        help='Glob(s) to exclude (relative paths). Defaults exclude venv/.git/etc.',
    )

    args = parser.parse_args()
    root = Path.cwd()

    total_files = 0
    changed_files = 0
    total_replacements = 0

    for py_file in iter_py_files(root, args.include, args.exclude):
        total_files += 1
        n, changed = process_file(
            py_file,
            args.old,
            args.new,
            dry_run=args.dry_run,
            count_only=args.count_only,
            backup=args.backup,
            encoding=args.encoding,
        )
        if n:
            rel = py_file.relative_to(root).as_posix()
            total_replacements += n
            if args.count_only:
                print(f"{rel}: {n}")
            else:
                action = "WOULD CHANGE" if args.dry_run else "CHANGED"
                print(f"{action} {rel}: {n} replacement(s)")
            if changed:
                changed_files += 1

    if args.count_only:
        print(f"\nScanned {total_files} *.py file(s). Found {total_replacements} occurrence(s).")
    else:
        if args.dry_run:
            print(
                f"\nScanned {total_files} *.py file(s). "
                f"Would change {changed_files} file(s), {total_replacements} replacement(s)."
            )
        else:
            print(
                f"\nScanned {total_files} *.py file(s). "
                f"Changed {changed_files} file(s), {total_replacements} replacement(s)."
            )


if __name__ == "__main__":
    main()
