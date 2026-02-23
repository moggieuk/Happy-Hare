#!/usr/bin/env python3
"""
unmember_constants.py

Replace occurrences like:
  self.FILAMENT_POS_HOMED_GATE
  self.mmu.FILAMENT_POS_HOMED_GATE
  mmu.FILAMENT_POS_HOMED_GATE
with:
  FILAMENT_POS_HOMED_GATE

Usage:
  # Dry-run on a file
  python unmember_constants.py --dry-run path/to/file.py

  # Apply changes recursively in a directory
  python unmember_constants.py -r path/to/project_dir

  # Add extra allowed prefixes:
  python unmember_constants.py --prefix my_instance --prefix printer.mm --dry-run file.py
"""
from __future__ import annotations
import argparse
import io
import os
import sys
import tokenize
from typing import List, Tuple, Iterable, Set

# ---- Replace / extend this constant list if needed ----
CONSTANTS = [
"BOOT_DELAY",
"TOOL_GATE_UNKNOWN",
"TOOL_GATE_BYPASS",
"GATE_UNKNOWN",
"GATE_EMPTY",
"GATE_AVAILABLE",
"GATE_AVAILABLE_FROM_BUFFER",
"FILAMENT_POS_UNKNOWN",
"FILAMENT_POS_UNLOADED",
"FILAMENT_POS_HOMED_GATE",
"FILAMENT_POS_START_BOWDEN",
"FILAMENT_POS_IN_BOWDEN",
"FILAMENT_POS_END_BOWDEN",
"FILAMENT_POS_HOMED_ENTRY",
"FILAMENT_POS_HOMED_EXTRUDER",
"FILAMENT_POS_EXTRUDER_ENTRY",
"FILAMENT_POS_HOMED_TS",
"FILAMENT_POS_IN_EXTRUDER",
"FILAMENT_POS_LOADED",
"DIRECTION_LOAD",
"DIRECTION_UNKNOWN",
"DIRECTION_UNLOAD",
"FORM_TIP_NONE",
"FORM_TIP_SLICER",
"FORM_TIP_STANDALONE",
"PURGE_NONE",
"PURGE_SLICER",
"PURGE_STANDALONE",
"ACTION_IDLE",
"ACTION_LOADING",
"ACTION_LOADING_EXTRUDER",
"ACTION_UNLOADING",
"ACTION_UNLOADING_EXTRUDER",
"ACTION_FORMING_TIP",
"ACTION_HEATING",
"ACTION_CHECKING",
"ACTION_HOMING",
"ACTION_SELECTING",
"ACTION_CUTTING_TIP",
"ACTION_CUTTING_FILAMENT",
"ACTION_PURGING",
"MACRO_EVENT_RESTART",
"MACRO_EVENT_GATE_MAP_CHANGED",
"MACRO_EVENT_FILAMENT_GRIPPED",
"SENSOR_ENCODER",
"SENSOR_GATE",
"SENSOR_GEAR_PREFIX",
"SENSOR_EXTRUDER_NONE",
"SENSOR_EXTRUDER_COLLISION",
"SENSOR_EXTRUDER_ENTRY",
"SENSOR_GEAR_TOUCH",
"SENSOR_COMPRESSION",
"SENSOR_TENSION",
"SENSOR_PROPORTIONAL",
"SENSOR_TOOLHEAD",
"SENSOR_EXTRUDER_TOUCH",
"SENSOR_SELECTOR_TOUCH",
"SENSOR_SELECTOR_HOME",
"SENSOR_PRE_GATE_PREFIX",
"EXTRUDER_ENDSTOPS",
"GATE_ENDSTOPS",
"GATE_STATS_STRING",
"GATE_STATS_PERCENTAGE",
"GATE_STATS_EMOTICON",
"GATE_STATS_TYPES",
"LOG_ESSENTIAL",
"LOG_INFO",
"LOG_DEBUG",
"LOG_TRACE",
"LOG_STEPPER",
"LOG_LEVELS",
"ESPOOLER_OFF",
"ESPOOLER_REWIND",
"ESPOOLER_ASSIST",
"ESPOOLER_PRINT",
"ESPOOLER_OPERATIONS",
"TOOLHEAD_POSITION_STATE",
"FILAMENT_UNKNOWN_STATE",
"FILAMENT_RELEASE_STATE",
"FILAMENT_DRIVE_STATE",
"FILAMENT_HOLD_STATE",
"VARS_MMU_REVISION",
"VARS_MMU_ENABLE_ENDLESS_SPOOL",
"VARS_MMU_ENDLESS_SPOOL_GROUPS",
"VARS_MMU_TOOL_TO_GATE_MAP",
"VARS_MMU_GATE_STATUS",
"VARS_MMU_GATE_MATERIAL",
"VARS_MMU_GATE_COLOR",
"VARS_MMU_GATE_FILAMENT_NAME",
"VARS_MMU_GATE_TEMPERATURE",
"VARS_MMU_GATE_SPOOL_ID",
"VARS_MMU_GATE_SPEED_OVERRIDE",
"VARS_MMU_GATE_SELECTED",
"VARS_MMU_TOOL_SELECTED",
"VARS_MMU_LAST_TOOL",
"VARS_MMU_FILAMENT_POS",
"VARS_MMU_FILAMENT_REMAINING",
"VARS_MMU_FILAMENT_REMAINING_COLOR",
"VARS_MMU_SWAP_STATISTICS",
"VARS_MMU_COUNTERS",
"VARS_MMU_ENCODER_RESOLUTION",
"VARS_MMU_CALIB_CLOG_LENGTH",
"VARS_MMU_GATE_STATISTICS_PREFIX",
"VARS_MMU_GEAR_ROTATION_DISTANCES",
"VARS_MMU_CALIB_BOWDEN_LENGTHS",
"VARS_MMU_CALIB_BOWDEN_HOME",
"VARS_MMU_CALIB_BOWDEN_LENGTH",
"VARS_MMU_GEAR_ROTATION_DISTANCE",
"VARS_MMU_CALIB_PREFIX",
"T_MACRO_COLOR_ALLGATES",
"T_MACRO_COLOR_GATEMAP",
"T_MACRO_COLOR_SLICER",
"T_MACRO_COLOR_OFF",
"T_MACRO_COLOR_OPTIONS",
"SPOOLMAN_OFF",
"SPOOLMAN_READONLY",
"SPOOLMAN_PUSH",
"SPOOLMAN_PULL",
"SPOOLMAN_OPTIONS",
"SPOOLMAN_CONFIG_ERROR",
"AUTOMAP_NONE",
"AUTOMAP_FILAMENT_NAME",
"AUTOMAP_SPOOL_ID",
"AUTOMAP_MATERIAL",
"AUTOMAP_CLOSEST_COLOR",
"AUTOMAP_COLOR",
"AUTOMAP_OPTIONS",
"EMPTY_GATE_STATS_ENTRY",
"W3C_COLORS",
"UPGRADE_REMINDER",
]
# ------------------------------------------------------

DEFAULT_PREFIXES = ["self", "self.mmu", "mmu", "cls", "self._mmu"]


def build_constants_set(constants: Iterable[str]) -> Set[str]:
    return set(constants)


def process_tokens(source_bytes: bytes, constants: Set[str], prefixes: List[str]) -> Tuple[bytes, int]:
    """
    Token-wise processing: look for dotted attribute sequences matching one of prefixes
    followed by a dot and a constant name. Replace the whole dotted attribute with the
    bare constant name (single NAME token). Returns new source bytes and number of replacements.
    """
    src_io = io.BytesIO(source_bytes)
    tok_gen = tokenize.tokenize(src_io.readline)

    out_tokens = []
    replacements = 0

    # normalize prefixes into lists of name components for quick compare
    prefix_components = [tuple(p.split('.')) for p in prefixes]

    # Convert generator to a list for lookahead-friendly processing
    tokens = [t for t in tok_gen]  # includes ENDMARKER
    i = 0
    N = len(tokens)

    while i < N:
        tok = tokens[i]
        # If this token starts a dotted chain of NAME (NAME '.' NAME '.' ... '.' NAME)
        # and the final NAME is one of the CONSTANTS, check whether the preceding chain
        # (one or more NAME parts) matches one of the allowed prefixes.
        if tok.type == tokenize.NAME:
            # try to parse a chain of NAME ( '.' NAME )* optionally followed by '.' CONSTANT_NAME
            j = i
            name_chain = []
            # collect at least one NAME
            if tokens[j].type == tokenize.NAME:
                name_chain.append(tokens[j].string)
                j += 1
                # collect repeated (. NAME)
                while j + 1 < N and tokens[j].type == tokenize.OP and tokens[j].string == '.' and tokens[j + 1].type == tokenize.NAME:
                    # append dot and next name to the chain
                    # we keep the chain as sequence of names only
                    name_chain.append(tokens[j + 1].string)
                    j += 2
                # After this, j points to the token after the last NAME in the chain.
                # We need at least a chain of length >=1 and we want to check if the final
                # name in chain is a CONSTANT with a preceding prefix (i.e. chain length >= 2),
                # or the chain can be prefix + constant where constant is the last name.
                # Approach: We will test possible splits of name_chain into (prefix_names, constant_name),
                # where prefix_names length >=1 (the instance) and constant_name present in constants.
                matched = False
                # Try splits where prefix length >=1 and constant is the last name
                if len(name_chain) >= 2:
                    prefix_names = tuple(name_chain[:-1])
                    candidate_const = name_chain[-1]
                    if candidate_const in constants and prefix_names in prefix_components:
                        # We matched: replace tokens from i up to j (exclusive) with a single NAME candidate_const
                        # Note: tokens i .. j-1 correspond to the chain and dots.
                        # Build a new token
                        new_token = tokenize.TokenInfo(tokenize.NAME, candidate_const, tok.start, tokens[j - 1].end, tok.line)
                        out_tokens.append(new_token)
                        replacements += 1
                        i = j
                        matched = True
                if matched:
                    continue
            # If not matched, just append current token and advance 1
            out_tokens.append(tok)
            i += 1
        else:
            out_tokens.append(tok)
            i += 1

    # Untokenize back to bytes
    new_src = tokenize.untokenize(out_tokens)
    return new_src, replacements


def process_file(path: str, constants: Set[str], prefixes: List[str], dry_run: bool = True) -> int:
    with open(path, 'rb') as f:
        src = f.read()
    new_src, replacements = process_tokens(src, constants, prefixes)
    if replacements:
        if dry_run:
            print(f"[DRY] {path}: {replacements} replacement(s)")
        else:
            # create backup
            bak = path + ".bak"
            with open(bak, 'wb') as bf:
                bf.write(src)
            with open(path, 'wb') as f:
                if isinstance(new_src, str):
                    f.write(new_src.encode('utf-8'))
                else:
                    f.write(new_src)
            print(f"[OK ] {path}: {replacements} replacement(s) (backup -> {bak})")
    else:
        print(f"[   ] {path}: no matches")
    return replacements


def iter_py_files(paths: List[str], recursive: bool) -> Iterable[str]:
    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    for fn in files:
                        if fn.endswith('.py'):
                            yield os.path.join(root, fn)
            else:
                for fn in os.listdir(p):
                    if fn.endswith('.py'):
                        yield os.path.join(p, fn)
        elif os.path.isfile(p):
            yield p
        else:
            print(f"Warning: {p} not found", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replace class-member constant uses like 'self.FOO' or 'self.mmu.FOO' with 'FOO'.")
    parser.add_argument('paths', nargs='+', help="File(s) or directory(ies) to process")
    parser.add_argument('-r', '--recursive', action='store_true', help="Recursively process directories")
    parser.add_argument('-p', '--prefix', action='append', default=[], help="Allowed instance prefix (e.g. 'self', 'self.mmu'). Can be used multiple times.")
    parser.add_argument('--dry-run', action='store_true', default=True, help="Don't write files; only show what would change (default). Use --no-dry-run to apply.")
    parser.add_argument('--no-dry-run', dest='dry_run', action='store_false', help="Apply changes to files (will create .bak backups).")
    parser.add_argument('--list-constants', action='store_true', help="Print built-in constant list and exit.")
    args = parser.parse_args(argv)

    constants_set = build_constants_set(CONSTANTS)
    prefixes = DEFAULT_PREFIXES + args.prefix

    if args.list_constants:
        for c in sorted(constants_set):
            print(c)
        return 0

    total = 0
    total_repl = 0
    for file_path in iter_py_files(args.paths, args.recursive):
        total += 1
        try:
            repl = process_file(file_path, constants_set, prefixes, dry_run=args.dry_run)
            total_repl += repl
        except Exception as exc:
            print(f"[ERR] {file_path}: {exc}", file=sys.stderr)
    print(f"\nProcessed {total} file(s). Total replacements: {total_repl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

