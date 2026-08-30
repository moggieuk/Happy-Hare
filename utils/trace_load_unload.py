#!/usr/bin/env python3
"""
trace_load_unload.py

Filter Happy Hare MMU logs for TRACE ENTRY / TRACE EXIT method markers and
print an indented call tree.

Also includes command lines like:
    16:07:49 > MMU_STATS COUNTER=servo_down INCR=1

Usage:
    python3 trace_load_unload.py mmu.log
    python3 trace_load_unload.py < mmu.log
    cat mmu.log | python3 trace_load_unload.py

Save output:
    python3 trace_load_unload.py mmu.log > flow.txt
"""

import re
import sys


ENTRY_RE = re.compile(
    r"^(?P<time>\d\d:\d\d:\d\d)\s+TRACE:\s+ENTRY\s+"
    r"(?P<method>[A-Za-z_]\w*)\((?P<args>.*?)\)"
    r"(?P<rest>.*)$"
)

EXIT_RE = re.compile(
    r"^(?P<time>\d\d:\d\d:\d\d)\s+TRACE:\s+EXIT\s+"
    r"(?P<method>[A-Za-z_]\w*)\(\)"
    r"(?:\s*=>\s*(?P<result>.*)|(?P<rest>\s+\|.*))?$"
)

COMMAND_RE = re.compile(
    r"^(?P<time>\d\d:\d\d:\d\d)\s+>\s+(?P<cmd>.*)$"
)

ERROR_RE = re.compile(
    r"(ERROR|Error|Exception|Traceback|failed because|MmuError|failed|aborted)",
    re.IGNORECASE,
)

FILAMENT_RE = re.compile(r"filament_pos=([-+]?\d+(?:\.\d+)?)mm")
ENCODER_RE = re.compile(r"encoder=([-+]?\d+(?:\.\d+)?)mm")


def normalize(line):
    return re.sub(r"^(\d\d:\d\d:\d\d)\s+", r"\1 ", line.rstrip())


def extract_positions(text):
    filament = ""
    encoder = ""

    m = FILAMENT_RE.search(text)
    if m:
        filament = f"{float(m.group(1)):7.1f}"

    m = ENCODER_RE.search(text)
    if m:
        encoder = f"{float(m.group(1)):7.1f}"

    return filament, encoder


def clean_state_text(text):
    text = re.sub(r"\s*\|\s*filament_pos=[-+]?\d+(?:\.\d+)?mm", "", text)
    text = re.sub(r"\s*\|\s*encoder=[-+]?\d+(?:\.\d+)?mm", "", text)
    return text.strip()


def prefix(time, filament="", encoder=""):
    return f"{time} {filament:>7} {encoder:>7} "


def print_header():
    print("TIME      FILPOS ENCODER FLOW")
    print("-------- ------- ------- ----------------------------------------------")


def process_lines(lines):
    stack = []
    print_header()

    for raw in lines:
        line = normalize(raw)
        if not line:
            continue

        entry = ENTRY_RE.match(line)
        if entry:
            method = entry.group("method")
            args = entry.group("args")
            rest = entry.group("rest").strip()

            filament, encoder = extract_positions(rest)
            rest = clean_state_text(rest)

            indent = "  " * len(stack)
            suffix = f" {rest}" if rest else ""
            print(f"{prefix(entry.group('time'), filament, encoder)}{indent} ▶ {method}({args}){suffix}")

            stack.append(method)
            continue

        exit_ = EXIT_RE.match(line)
        if exit_:
            method = exit_.group("method")
            result = exit_.group("result")
            rest = exit_.group("rest")

            result = result if result is not None else (rest.strip() if rest else "")
            filament, encoder = extract_positions(result)
            result = clean_state_text(result)

            if method in stack:
                while stack and stack[-1] != method:
                    missing = stack.pop()
                    indent = "  " * len(stack)
                    print(f"{prefix(exit_.group('time'))}{indent} ✖ {missing} ABORTED / missing EXIT")

                stack.pop()
                indent = "  " * len(stack)
            else:
                indent = "  " * len(stack)
                print(f"{prefix(exit_.group('time'))}{indent}!⚠ EXIT for {method} without matching ENTRY")

            if result:
                print(f"{prefix(exit_.group('time'), filament, encoder)}{indent} ◀ {method} => {result}")
            else:
                print(f"{prefix(exit_.group('time'), filament, encoder)}{indent} ◀ {method}")
            continue

        command = COMMAND_RE.match(line)
        if command:
            indent = "  " * len(stack)
            print(f"{prefix(command.group('time'))}{indent} □ {command.group('cmd')}")
            continue

        if ERROR_RE.search(line):
            indent = "  " * len(stack)
            msg = line[9:] if len(line) > 9 else line
            print(f"{prefix(line[:8])}{indent} ⚠ {msg}")

    while stack:
        method = stack.pop()
        indent = "  " * len(stack)
        print(f"{prefix('??:??:??')}{indent} ✖ {method} ABORTED / missing EXIT")


def main():
    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            with open(path, "r", errors="replace") as f:
                process_lines(f)
    else:
        process_lines(sys.stdin)


if __name__ == "__main__":
    main()
