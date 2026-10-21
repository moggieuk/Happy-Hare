# Happy Hare MMU Software
#
# Migration of renamed, removed and reordered multi-unit MMU units
#
# The unit list (CONFIG_MMU_UNITS) is edited with menuconfig's sequence_editor,
# which records where each unit came from relative to the installed list in a
# '<config>.MMU_UNITS.changes' sidecar. This module turns that into:
#
#   check    - summarize the change and refuse it when it is not allowed
#   kconfig  - rename/rewrite the per-unit '.mmu_config_<unit>' files (before
#              the per-unit menuconfig so a renamed unit keeps its settings)
#   prepare  - capture what the install step needs from the still-installed
#              config (gate counts, save_variables file, manual-edit warnings)
#   apply    - with Klipper stopped: remove stale unit files and migrate the
#              save_variables file (unit namespaced keys and global gate lists)
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import ast
import configparser
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import traceback

import sequence_edit
import shared_components

from .parser import ConfigBuilder, MAGIC_EXCLUSION_COMMENT

SYMBOL = "MMU_UNITS"
SEPARATOR = ","

EXIT_NONE = 0
EXIT_REFUSED = 2
EXIT_FAILED = 3
EXIT_APPEND = 10
EXIT_STRUCTURAL = 11
EXIT_UNTRACKED = 12

DEFAULT_TOKEN = "#~DEFAULT~#"

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_constants():
    path = os.path.join(SRC, "extras", "mmu", "mmu_constants.py")
    spec = importlib.util.spec_from_file_location("hh_mmu_constants", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _load_constants()

# Variables namespaced by unit name (SaveVariableManager.namespace)
UNIT_VARS = [
    C.VARS_MMU_GEAR_ROTATION_DISTANCES,
    C.VARS_MMU_BOWDEN_LENGTHS,
    C.VARS_MMU_BOWDEN_HOME,
    C.VARS_MMU_SELECTOR_OFFSETS,
    C.VARS_MMU_SELECTOR_BYPASS_OFFSET,
    C.VARS_MMU_SELECTOR_LAST_POS,
    C.VARS_MMU_SELECTOR_ANGLES,
    C.VARS_MMU_SELECTOR_BYPASS_ANGLE,
    C.VARS_MMU_SELECTOR_RELEASE_ANGLE,
    C.VARS_MMU_SELECTOR_SERVO_ANGLES,
    C.VARS_MMU_SELECTOR_GATE_SEQUENCE,
    C.VARS_MMU_SELECTOR_ENDSTOP_WIDTHS,
]

# Variables namespaced by encoder name, which defaults to the unit name
ENCODER_VARS = [
    C.VARS_MMU_ENCODER_RESOLUTION,
    C.VARS_MMU_ENCODER_CLOG_LENGTH,
]

# Global per-gate lists and the value a new gate starts with (mmu_gate_maps.py)
GATE_LIST_VARS = [
    (C.VARS_MMU_GATE_STATUS, C.GATE_UNKNOWN),
    (C.VARS_MMU_GATE_FILAMENT_NAME, ""),
    (C.VARS_MMU_GATE_MATERIAL, ""),
    (C.VARS_MMU_GATE_VENDOR, ""),
    (C.VARS_MMU_GATE_TD, None),
    (C.VARS_MMU_GATE_TD1_COLOR, ""),
    (C.VARS_MMU_GATE_COLOR, ""),
    (C.VARS_MMU_GATE_TEMPERATURE, None), # default_extruder_temp
    (C.VARS_MMU_GATE_SPOOL_ID, -1),
    (C.VARS_MMU_GATE_SPEED_OVERRIDE, 100),
    (C.VARS_MMU_GATE_SPOOL_RFID, ""),
]

# Per-gate list options in [mmu_machine] that must match the total gate count
GATE_LIST_OPTIONS = [
    "default_ttg_map",
    "default_gate_status",
    "default_gate_filament_name",
    "default_gate_material",
    "default_gate_vendor",
    "default_gate_td",
    "default_gate_td1_color",
    "default_gate_color",
    "default_gate_temperature",
    "default_gate_spool_id",
    "default_gate_spool_rfid",
    "default_gate_speed_override",
    "default_endless_spool_groups",
]

GATE_SENSOR_KEY = re.compile(r"^(mmu_[a-z_]+?)_(\d+)$")


def namespaced(variable, namespace):
    return variable.replace("mmu_", "mmu_%s_" % namespace)


# -----------------------------------------------------------------------------
# Kconfig value files
# -----------------------------------------------------------------------------

# The names of encoders and buffers, which only follow a rename of the unit that owns one
SHARED_KINDS = {kind.name: kind for kind in shared_components.KINDS.values()}
KCONFIG_LINE = re.compile(r'^(CONFIG_[A-Za-z0-9_]+)=(.*?)(\s+' + DEFAULT_TOKEN + r')?\s*$')


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r'\\(.)', r'\1', value[1:-1])
    return value


def read_kconfig(path):
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = KCONFIG_LINE.match(line.rstrip("\n"))
                if m:
                    values[m.group(1)[len("CONFIG_"):]] = _unquote(m.group(2))
    except FileNotFoundError:
        pass
    return values


def unit_file(kconfig, name):
    return "%s_%s" % (kconfig, name)


def removed_file(kconfig, name):
    return "%s_%s.removed" % (kconfig, name)


def _names_pattern(names):
    # Only a whole unit name at the start of a token ("unit0", "unit0:PA3",
    # "unit0_gear", "_unit0_leds") is a reference to the unit
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r'(?:^|(?<=[\s,:(;=!^~"]))(_?)(' + alternatives + r')(?=$|[\s,:;()_"])')


def _loose_pattern(names):
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(r'(?<![A-Za-z0-9-])(' + alternatives + r')(?![A-Za-z0-9-])')


def rewrite_kconfig(path, renames, known_names, identity=None, own=None):
    """
    Rewrite unit names in the explicit values of a Kconfig value file.
    Values saved with #~DEFAULT~# are left alone since they are recomputed on
    the next load. 'identity' forces UNIT_NAME/MCU_NAME/UNIT_INDEX.

    An encoder or buffer name is only renamed by 'own' (this file's old unit name -> new),
    when the unit owns the component and the name is exactly its old unit name. A shared
    one names another unit's or the user's own section, which the user has to keep right.

    Returns (rewritten lines, lines that may still need a manual edit).
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")
    values = read_kconfig(path)

    renames = {old: new for old, new in renames.items() if old != new}
    pattern = _names_pattern(set(renames) | set(known_names)) if renames else None
    gone = set(renames) - set(renames.values())
    loose = _loose_pattern(gone) if gone else None

    rewritten, manual = [], []
    for i, line in enumerate(lines):
        m = KCONFIG_LINE.match(line)
        if not m:
            continue
        key, value, default = m.group(1), m.group(2), m.group(3) or ""
        sym = key[len("CONFIG_"):]

        if sym in SHARED_KINDS:
            name = _unquote(value)
            if not own or name not in own or values.get(SHARED_KINDS[sym].shared) == "y":
                continue
            new_value = _quote(own[name])
        elif identity and sym in identity:
            new_value = identity[sym]
        elif default or pattern is None or sym == SYMBOL:
            continue
        else:
            new_value = pattern.sub(lambda mm: mm.group(1) + renames.get(mm.group(2), mm.group(2)), value)

        if new_value != value:
            lines[i] = "%s=%s%s" % (key, new_value, default)
            rewritten.append("%s: %s -> %s" % (sym, value, new_value))
        if loose and not default and sym not in (SYMBOL,) + tuple(SHARED_KINDS) \
                and loose.search(new_value):
            manual.append("%s:%d: %s" % (path, i + 1, lines[i]))

    if rewritten:
        _write_text(path, "\n".join(lines))
    return rewritten, manual


def _write_text(path, text):
    tmp = path + ".unit-migration-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _quote(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


# -----------------------------------------------------------------------------
# Installed configuration
# -----------------------------------------------------------------------------

def _base(config_home, name):
    return os.path.join(config_home, "mmu", "base", name)


def installed_units(config_home):
    if not config_home:
        return None
    path = _base(config_home, "mmu.cfg")
    if not os.path.exists(path):
        return None
    units = ConfigBuilder(path).get("mmu_machine", "units")
    if units is None:
        return None
    return sequence_edit.split_sequence(units, SEPARATOR)


def installed_num_gates(config_home, unit):
    path = _base(config_home, "mmu_hardware_%s.cfg" % unit)
    if not os.path.exists(path):
        return None
    value = ConfigBuilder(path).get("mmu_unit %s" % unit, "num_gates")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def installed_option(config_home, filename, section, option):
    path = _base(config_home, filename)
    if not os.path.exists(path):
        return None
    return ConfigBuilder(path).get(section, option)


def vars_file(config_home, kconfig):
    """
    The save_variables file: the installed [save_variables] filename, else the
    menuconfig 'save_variables path'. Overrides made elsewhere aren't followed.
    """
    path = installed_option(config_home, "mmu_macro_vars.cfg", "save_variables", "filename") \
        or read_kconfig(kconfig).get("PARAM_MMU_VARS_CFG")
    return os.path.expanduser(path) if path else None


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

def state_path(kconfig):
    return kconfig + ".unit_migration"


def read_state(kconfig):
    return sequence_edit.read_changes(state_path(kconfig))


def write_state(kconfig, state):
    with open(state_path(kconfig), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def clear_state(kconfig):
    for path in (state_path(kconfig), sequence_edit.changes_path(kconfig, SYMBOL)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def baseline(kconfig, config_home):
    """The unit list that the installed configuration (and save_variables) reflects."""
    state = read_state(kconfig)
    if state and isinstance(state.get("baseline"), list):
        return state["baseline"]
    units = installed_units(config_home)
    if units is not None:
        return units
    values = read_kconfig(kconfig)
    if values.get("MULTI_UNIT") == "y":
        return sequence_edit.split_sequence(values.get(SYMBOL, ""), SEPARATOR)
    return [values.get("UNIT_NAME") or "unit0"] if os.path.exists(kconfig) else []


class Plan:
    def __init__(self, kconfig, base):
        values = read_kconfig(kconfig)
        self.kconfig = kconfig
        self.baseline = list(base)
        self.single = values.get("MULTI_UNIT") != "y"
        if self.single:
            # A single unit is named by its own menuconfig, so any change is a rename
            self.current = [values.get("UNIT_NAME") or "unit0"]
            origins = {self.current[0]: self.baseline[0]} if len(self.baseline) == 1 else None
        else:
            self.current = sequence_edit.split_sequence(values.get(SYMBOL, ""), SEPARATOR)
            changes = sequence_edit.read_changes(sequence_edit.changes_path(kconfig, SYMBOL))
            origins = sequence_edit.origins_from_changes(changes, self.baseline, self.current)
        self.captured = origins is not None
        self.model = sequence_edit.SequenceModel(self.baseline, self.current, origins)

    @property
    def origin(self):
        return self.model.origins()

    @property
    def removed(self):
        return self.model.removed

    @property
    def target(self):
        """Baseline name -> new name, or None for a removed unit."""
        by_origin = {o: n for n, o in self.origin.items() if o is not None}
        return {o: by_origin.get(o) for o in self.baseline}

    def structural(self):
        return self.model.structural()

    def changed(self):
        # Nothing is installed or configured yet on a fresh install
        return bool(self.baseline) and self.model.is_changed()

    def errors(self):
        errors = []
        if len(set(self.current)) != len(self.current):
            errors.append("Unit names in %s must be unique: %s" % (SYMBOL, ", ".join(self.current)))
        if not self.current:
            errors.append("%s cannot be empty" % SYMBOL)
        # A unit sharing an object (NFC reader, ...) of a removed unit
        applied = _applied(self.kconfig, self.baseline)
        removed = {applied.get(old) or old: old for old in self.removed}
        if removed:
            pattern = _names_pattern(set(removed) | set(self.current))
            for unit in self.current:
                origin = self.origin.get(unit)
                path = unit_file(self.kconfig, applied.get(origin) or unit) if origin else None
                if not path or not os.path.exists(path):
                    continue
                # Once migrated, a reused name in the file means the unit that now has it
                reused = set(self.current) if applied.get(origin) != origin else set()
                for sym, value in read_kconfig(path).items():
                    if sym in ("UNIT_NAME", "MCU_NAME") or sym in SHARED_KINDS:
                        continue
                    for m in pattern.finditer(value):
                        if m.group(2) in removed and m.group(2) not in reused:
                            errors.append("Unit '%s' still uses '%s' from removed unit '%s' (%s)"
                                          % (unit, value, removed[m.group(2)], sym))
                            break
        return errors


def _applied(kconfig, base):
    state = read_state(kconfig) or {}
    applied = state.get("applied") if state.get("baseline") == base else None
    if not isinstance(applied, dict):
        applied = {}
    return {o: applied.get(o, o) for o in base}


def _unit_values(kconfig, plan, applied, unit, baseline=True):
    """The saved values of a baseline unit (or, with baseline=False, a current one)."""
    if plan.single:
        return read_kconfig(kconfig)
    if baseline:
        have = applied.get(unit)
        return read_kconfig(unit_file(kconfig, have) if have else removed_file(kconfig, unit))
    origin = plan.origin.get(unit)
    return read_kconfig(unit_file(kconfig, (applied.get(origin) if origin else None) or unit))


def component_names(kconfig, plan, kind):
    """
    Baseline unit -> (name before, name after) of the component it owns (its name, which
    defaults to the unit's), None after for a removed unit. A name that is the file's unit
    name follows the rename, whether or not the file is migrated yet.
    """
    applied = _applied(kconfig, plan.baseline)
    names = {}
    for old, new in plan.target.items():
        values = _unit_values(kconfig, plan, applied, old)
        if not shared_components.is_owner(values, kind):
            continue
        name = values.get(kind.name)
        if name is None or name in (old, values.get("UNIT_NAME")):
            names[old] = (old, new)
        else:
            names[old] = (name, name if new else None)
    return names


def shared_name_warnings(kconfig, plan):
    """Sharers still naming an encoder/buffer that the change renames or removes."""
    if plan.single:
        return []
    applied = _applied(kconfig, plan.baseline)
    warnings = []
    for kind_name, kind in sorted(shared_components.KINDS.items()):
        names = component_names(kconfig, plan, kind).values()
        after = {new for _old, new in names}
        changed = {old: new for old, new in names if old not in after}
        for unit in plan.current:
            values = _unit_values(kconfig, plan, applied, unit, baseline=False)
            name = values.get(kind.name)
            if values.get(kind.has) != "y" or values.get(kind.shared) != "y" or name not in changed:
                continue
            fate = ("is renamed to '%s'" % changed[name] if changed[name]
                    else "no unit owns after this change")
            warnings.append("Unit '%s' shares %s '%s', which %s. Change its shared %s name in"
                            " its configuration" % (unit, kind_name, name, fate, kind_name))
    return warnings


# -----------------------------------------------------------------------------
# check
# -----------------------------------------------------------------------------

def check(kconfig, config_home, base, mode, out=sys.stdout):
    plan = Plan(kconfig, base)
    if not plan.changed():
        return EXIT_NONE

    for error in plan.errors():
        print("ERROR: %s" % error, file=out)
    if plan.errors():
        return EXIT_REFUSED

    print("Changes to MMU units (%s -> %s):" % (", ".join(plan.baseline) or "none",
                                               ", ".join(plan.current)), file=out)
    for line in plan.model.summary():
        print("  %s" % line, file=out)
    if not plan.captured and (plan.removed or any(o is None for o in plan.origin.values())):
        print("WARNING: No renames were recorded for this list so any unit not in the previous list is"
              " treated as new and its settings and saved state start from defaults", file=out)

    if not plan.structural():
        return EXIT_APPEND

    if mode != "replace":
        if not plan.captured and not read_state(kconfig):
            # Not made with the unit editor (e.g. the list was edited before it existed)
            print("WARNING: The installed units do not match %s but no change was recorded, so nothing"
                  " is migrated. Use './install.sh -i' (Replace mode) to rename, remove or reorder"
                  " units" % SYMBOL, file=out)
            return EXIT_UNTRACKED
        undo = ("set 'Klipper object name' back to '%s'" % plan.baseline[0] if plan.single
                else "open 'MMU units' and press [u]")
        print("ERROR: Renaming, removing or reordering units is only allowed in Replace upgrade mode."
              " Re-run './install.sh -i' and choose option 2, or %s to undo the pending change"
              % undo, file=out)
        return EXIT_REFUSED

    for warning in shared_name_warnings(kconfig, plan):
        print("WARNING: %s" % warning, file=out)

    if config_home and installed_units(config_home) is not None:
        if gates_moved(*_check_layout(plan, config_home)):
            print("Gates of units after the first moved or removed unit are renumbered. Calibration, gate"
                  " maps and saved state move with each unit", file=out)
            print("The selected gate and tool will be reset to unknown, so select or home again after"
                  " the install", file=out)
        else:
            print("Calibration, gate maps and saved state move with each unit", file=out)
        path = vars_file(config_home, kconfig)
        if path:
            print("Saved state will be migrated in %s" % path, file=out)
        for warning in _loaded_filament_warnings(plan, config_home, kconfig):
            print("WARNING: %s" % warning, file=out)
        for warning in manual_edit_warnings(plan, config_home, kconfig):
            print("WARNING: %s" % warning, file=out)
    return EXIT_STRUCTURAL


# -----------------------------------------------------------------------------
# kconfig (phase A)
# -----------------------------------------------------------------------------

def migrate_kconfig(kconfig, base, out=sys.stdout):
    """Bring the per-unit Kconfig files in line with the current unit list. Idempotent."""
    plan = Plan(kconfig, base)
    if plan.single:
        _rename_single_unit(kconfig, plan.baseline[:1], out)
        return
    if not plan.changed() and not read_state(kconfig):
        return
    applied = _applied(kconfig, plan.baseline)
    previous = dict(applied)
    state = read_state(kconfig) or {}
    if "generated" not in state:
        state["generated"] = generated_hardware_lines(kconfig, [n for n in previous.values() if n])
    target = plan.target
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    moves = []
    for old in plan.baseline:
        have, want = applied[old], target[old]
        if have == want:
            continue
        src = unit_file(kconfig, have) if have else removed_file(kconfig, old)
        dst = unit_file(kconfig, want) if want else removed_file(kconfig, old)
        if os.path.exists(src):
            moves.append((old, src, dst))
        applied[old] = want

    # Two steps so that swapping names never overwrites a file still to be moved
    staged = []
    for old, src, dst in moves:
        tmp = "%s.unit-migration-%s" % (src, stamp)
        os.replace(src, tmp)
        staged.append((old, tmp, dst))
    for old, tmp, dst in staged:
        if os.path.exists(dst):
            os.replace(dst, "%s.old-%s" % (dst, stamp))
        os.replace(tmp, dst)
        print("Moved %s -> %s" % (os.path.basename(tmp.rsplit(".unit-migration-", 1)[0]),
                                  os.path.basename(dst)), file=out)

    renames = {previous[o]: target[o] for o in plan.baseline
               if target[o] and previous[o] and previous[o] != target[o]}
    known = set(plan.current) | set(renames)

    manual = []
    for index, name in enumerate(plan.current):
        path = unit_file(kconfig, name)
        if not os.path.exists(path):
            continue
        identity = {"UNIT_NAME": _quote(name), "MCU_NAME": _quote(name), "UNIT_INDEX": str(index)}
        was = previous.get(plan.origin.get(name))
        own = {was: name} if was and was != name else None
        rewritten, flagged = rewrite_kconfig(path, renames, known, identity, own)
        manual.extend(flagged)
        for line in rewritten:
            print("  %s: %s" % (os.path.basename(path), line), file=out)
    if os.path.exists(kconfig) and renames:
        rewritten, flagged = rewrite_kconfig(kconfig, renames, known)
        manual.extend(flagged)
        for line in rewritten:
            print("  %s: %s" % (os.path.basename(kconfig), line), file=out)
    for line in manual:
        print("WARNING: Check for an old unit name: %s" % line, file=out)

    state.update(baseline=plan.baseline, applied=applied)
    write_state(kconfig, state)


def _rename_single_unit(kconfig, installed, out):
    # The old name: the installed one, the one install.sh saw before menuconfig, or
    # MCU_NAME, which keeps the name the last parse ran with unless menuconfig
    # re-parsed after the rename
    values = read_kconfig(kconfig)
    new = values.get("UNIT_NAME")
    olds = {values.get("MCU_NAME"), os.getenv("F_UNIT_NAME_BEFORE")} | set(installed)
    olds = {o for o in olds if o and o != new}
    if not new or not olds:
        return
    state = read_state(kconfig) or {}
    if "generated" not in state:
        state["generated"] = generated_hardware_lines(kconfig, [])
        write_state(kconfig, state)
    renames = {o: new for o in olds}
    rewritten, manual = rewrite_kconfig(kconfig, renames, olds | {new}, own=renames)
    for line in rewritten:
        print("  %s: %s" % (os.path.basename(kconfig), line), file=out)
    for line in manual:
        print("WARNING: Check for an old unit name: %s" % line, file=out)


# -----------------------------------------------------------------------------
# prepare
# -----------------------------------------------------------------------------

def prepare(kconfig, config_home, base, out=sys.stdout):
    """Record, from the still-installed config, what 'apply' will need after install."""
    plan = Plan(kconfig, base)
    state = read_state(kconfig) or {"baseline": plan.baseline, "applied": _applied(kconfig, plan.baseline)}
    state.pop("install", None)

    if plan.changed() and config_home and installed_units(config_home) is not None:
        values = read_kconfig(kconfig)
        temp = installed_option(config_home, "mmu.cfg", "mmu_parameters", "default_extruder_temp") \
            or values.get("PARAM_DEFAULT_EXTRUDER_TEMP") or 200
        new_gates = {}
        for name in plan.current:
            gates = read_kconfig(kconfig if plan.single else unit_file(kconfig, name)).get("PARAM_NUM_GATES")
            new_gates[name] = int(gates) if gates and gates.isdigit() else None
        state["install"] = {
            "config_home": config_home,
            "from": plan.baseline,
            "to": plan.current,
            "origin": plan.origin,
            "old_gates": {o: installed_num_gates(config_home, o) for o in plan.baseline},
            "new_gates": new_gates,
            "vars_file": vars_file(config_home, kconfig),
            "default_extruder_temp": int(float(temp)),
            "warnings": manual_edit_warnings(plan, config_home, kconfig),
            "encoders": {before: after for before, after in
                         component_names(kconfig, plan, shared_components.KINDS["encoder"]).values()
                         if before != after},
        }
    elif not plan.changed() and not read_state(kconfig):
        return
    write_state(kconfig, state)


def generated_hardware_lines(kconfig, names):
    """
    Lines of the generated PARAM_MISC_HARDWARE blocks, which Replace mode
    regenerates below each file's EXCLUDE marker and so never need a manual edit.
    """
    lines = set()
    for path in [kconfig] + [unit_file(kconfig, n) for n in names]:
        value = read_kconfig(path).get("PARAM_MISC_HARDWARE", "")
        lines.update(line.strip() for line in value.split("\\n") if line.strip())
    return sorted(lines)


def _renamed_or_removed(plan):
    return {o for o, n in plan.target.items() if n != o}


def manual_edit_warnings(plan, config_home, kconfig):
    """Places outside Kconfig that still mention a renamed or removed unit."""
    names = _renamed_or_removed(plan)
    if not names or not config_home:
        return []
    loose = _loose_pattern(names)
    warnings = []
    state = read_state(kconfig) or {}
    generated = set(state.get("generated") or
                    generated_hardware_lines(kconfig, _applied(kconfig, plan.baseline).values()))

    def scan(path, excluded_only=False):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        except (OSError, UnicodeDecodeError):
            return
        in_excluded = not excluded_only
        for i, line in enumerate(lines):
            if MAGIC_EXCLUSION_COMMENT.match(line):
                in_excluded = True
                continue
            if in_excluded and not line.lstrip().startswith("#") and loose.search(line) \
                    and line.strip() not in generated:
                warnings.append("%s:%d: %s" % (path, i + 1, line.strip()))

    mmu_dir = os.path.join(config_home, "mmu")
    for unit in plan.baseline:
        for name in ("mmu_hardware_%s.cfg" % unit, "mmu_parameters_%s.cfg" % unit):
            scan(_base(config_home, name), excluded_only=True)
    for name in ("mmu.cfg", "mmu_macro_vars.cfg"):
        scan(_base(config_home, name), excluded_only=True)

    values = read_kconfig(kconfig)
    printer_cfg = values.get("PRINTER_CONFIG_FILE") or "printer.cfg"
    scan(os.path.join(config_home, printer_cfg))
    for root, _dirs, files in os.walk(mmu_dir):
        if os.path.abspath(root) == os.path.abspath(os.path.join(mmu_dir, "base")):
            continue
        for name in sorted(files):
            path = os.path.join(root, name)
            if name.endswith(".cfg") and not os.path.islink(path) and name != "mmu_vars.cfg":
                scan(path)
    return warnings


def _check_layout(plan, config_home):
    # Gate counts before the per-unit menuconfig: installed for kept units, 0 for new ones
    old_gates = {o: installed_num_gates(config_home, o) for o in plan.baseline}
    layout = {
        "from": plan.baseline,
        "to": plan.current,
        "origin": plan.origin,
        "old_gates": old_gates,
        "new_gates": {n: (old_gates.get(o) if o else 0) for n, o in plan.origin.items()},
    }
    return gate_map(layout), sum(n or 0 for n in old_gates.values())


def _loaded_filament_warnings(plan, config_home, kconfig):
    path = vars_file(config_home, kconfig)
    variables = read_vars(path) if path else {}
    if variables.get(C.VARS_MMU_FILAMENT_POS, C.FILAMENT_POS_UNLOADED) == C.FILAMENT_POS_UNLOADED:
        return []
    gate = variables.get(C.VARS_MMU_GATE_SELECTED, C.TOOL_GATE_UNKNOWN)
    if gate == C.TOOL_GATE_UNKNOWN or not gates_moved(*_check_layout(plan, config_home)):
        return []
    return ["Filament is loaded (gate %d) but the selected gate and tool will be reset to unknown."
            " Unload it before installing" % gate]


# -----------------------------------------------------------------------------
# apply (phase B)
# -----------------------------------------------------------------------------

def read_vars(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if not parser.read(path):
            return {}
    except configparser.Error:
        return {}
    if not parser.has_section("Variables"):
        return {}
    result = {}
    for name, value in parser.items("Variables"):
        try:
            result[name] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            result[name] = value
    return result


def write_vars(path, variables):
    parser = configparser.ConfigParser(interpolation=None)
    parser.add_section("Variables")
    for name, value in sorted(variables.items()):
        parser.set("Variables", name, repr(value))
    tmp = path + ".unit-migration-tmp"
    with open(tmp, "w") as f:
        parser.write(f)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def gate_map(install):
    """Old global gate -> new global gate, for gates whose unit is kept."""
    old_gates, new_gates = install["old_gates"], install["new_gates"]
    if any(old_gates.get(o) is None for o in install["from"]) or \
       any(new_gates.get(n) is None for n in install["to"]):
        return None

    old_first, first = {}, 0
    for unit in install["from"]:
        old_first[unit] = first
        first += old_gates[unit]
    mapping, first = {}, 0
    for unit in install["to"]:
        origin = install["origin"].get(unit)
        if origin is not None:
            for local in range(min(old_gates[origin], new_gates[unit])):
                mapping[old_first[origin] + local] = first + local
        first += new_gates[unit]
    return mapping


def gates_moved(mapping, old_total):
    """True if any existing gate is renumbered or dropped (unknown counts count as moved)."""
    return mapping is None or any(mapping.get(g) != g for g in range(old_total))


def migrate_vars(variables, install):
    """Return (migrated copy of 'variables', notes)."""
    result = dict(variables)
    notes = []
    target = {o: None for o in install["from"]}
    for new, old in install["origin"].items():
        if old is not None:
            target[old] = new

    # Unit namespaced variables, by exact name
    moves = {}
    for old, new in target.items():
        if old == new:
            continue
        keys = [namespaced(v, old) for v in UNIT_VARS]
        stats = namespaced(C.VARS_MMU_GATE_STATISTICS_PREFIX, old)
        keys += [k for k in variables if k.startswith(stats) and k[len(stats):].isdigit()]
        for key in keys:
            if key in variables:
                moves[key] = None if new is None else namespaced("mmu_", new) + key[len(namespaced("mmu_", old)):]
    # Encoder variables, by the encoder's name (prepare: before -> after, None if removed)
    for old, new in install.get("encoders", {}).items():
        for key in (namespaced(v, old) for v in ENCODER_VARS):
            if key in variables:
                moves[key] = None if new is None else namespaced("mmu_", new) + key[len(namespaced("mmu_", old)):]
    for key in moves:
        result.pop(key, None)
    for key, new_key in moves.items():
        if new_key is not None:
            result[new_key] = variables[key]
    renamed = sum(1 for k in moves.values() if k)
    if moves:
        notes.append("Renamed %d and removed %d unit variables" % (renamed, len(moves) - renamed))

    mapping = gate_map(install)
    old_total = sum(install["old_gates"].get(o) or 0 for o in install["from"])
    new_total = sum(install["new_gates"].get(n) or 0 for n in install["to"])
    identity = mapping is not None and old_total == new_total and \
        all(mapping.get(g) == g for g in range(old_total))

    if mapping is None:
        notes.append("Gate counts unknown so gate maps were not migrated")
    elif not identity:
        for var, default in GATE_LIST_VARS:
            old = variables.get(var)
            if not isinstance(old, list) or len(old) != old_total:
                continue
            if default is None and var == C.VARS_MMU_GATE_TEMPERATURE:
                default = install.get("default_extruder_temp", 200)
            new = [default] * new_total
            for og, ng in mapping.items():
                new[ng] = old[og]
            result[var] = new

        groups = variables.get(C.VARS_MMU_ENDLESS_SPOOL_GROUPS)
        if isinstance(groups, list) and len(groups) == old_total:
            new = [None] * new_total
            for og, ng in mapping.items():
                new[ng] = groups[og]
            next_group = max([g for g in new if isinstance(g, int)] + [-1]) + 1
            for i, g in enumerate(new):
                if g is None:
                    new[i], next_group = next_group, next_group + 1
            result[C.VARS_MMU_ENDLESS_SPOOL_GROUPS] = new

        ttg = variables.get(C.VARS_MMU_TOOL_TO_GATE_MAP)
        if isinstance(ttg, list) and len(ttg) == old_total:
            result[C.VARS_MMU_TOOL_TO_GATE_MAP] = [
                mapping.get(ttg[t], t) if t < old_total else t for t in range(new_total)
            ]

        notes.append("Gate maps moved with their units (%d -> %d gates)" % (old_total, new_total))

    # The selection drives the selector position and active unit at startup
    if gates_moved(mapping, old_total):
        reset = False
        for var in (C.VARS_MMU_GATE_SELECTED, C.VARS_MMU_TOOL_SELECTED):
            if var in variables and variables[var] != C.TOOL_GATE_UNKNOWN:
                result[var] = C.TOOL_GATE_UNKNOWN
                reset = True
        if reset:
            notes.append("Selected gate and tool reset to unknown")

    sensors = variables.get(C.VARS_MMU_SENSOR_ENABLED)
    if isinstance(sensors, dict):
        new = {}
        for key, value in sensors.items():
            prefix, sep, rest = key.partition(":")
            if sep and prefix in target:
                if target[prefix] is not None:
                    new["%s:%s" % (target[prefix], rest)] = value
                continue
            m = GATE_SENSOR_KEY.match(key)
            if m and mapping is not None and not identity:
                gate = int(m.group(2))
                if gate in mapping:
                    new["%s_%d" % (m.group(1), mapping[gate])] = value
                continue
            new[key] = value
        result[C.VARS_MMU_SENSOR_ENABLED] = new

    return result, notes


def stop_klipper(kconfig, out=sys.stdout):
    values = read_kconfig(kconfig)
    service = values.get("SERVICE_KLIPPER")
    if not service:
        print("WARNING: No Klipper service configured. Make sure Klipper is stopped", file=out)
        return
    print("Stopping Klipper so saved state can be migrated...", file=out)
    if values.get("INIT_SYSTEMD") == "y":
        if not service.endswith(".service"):
            service += ".service"
        if subprocess.call("systemctl list-unit-files '%s'" % service, shell=True,
                           stdout=subprocess.DEVNULL) == 0:
            subprocess.call("sudo systemctl stop '%s'" % service, shell=True)
            return
    elif os.path.exists("/etc/init.d/" + service):
        subprocess.call("/etc/init.d/%s stop" % service, shell=True)
        return
    print("WARNING: Klipper service '%s' not found. Make sure Klipper is stopped" % service, file=out)


def apply(kconfig, no_service=False, out=sys.stdout):
    state = read_state(kconfig)
    install = (state or {}).get("install")
    if install:
        if no_service:
            print("WARNING: Service restarts are skipped. Klipper must already be stopped or it may"
                  " overwrite the migrated saved state", file=out)
        else:
            stop_klipper(kconfig, out)

        # 'make install' has already backed up the whole mmu directory
        config_home = install["config_home"]
        mmu_dir = os.path.join(config_home, "mmu")
        for unit in sorted(set(install["from"]) - set(install["to"])):
            for path in (_base(config_home, "mmu_hardware_%s.cfg" % unit),
                         _base(config_home, "mmu_parameters_%s.cfg" % unit),
                         os.path.join(mmu_dir, ".mmu_config_%s" % unit)):
                if os.path.exists(path):
                    os.remove(path)
                    print("Removed %s" % path, file=out)

        path = install.get("vars_file")
        if path and os.path.exists(path):
            variables = read_vars(path)
            migrated, notes = migrate_vars(variables, install)
            if migrated != variables:
                if os.path.commonpath([os.path.abspath(path), os.path.abspath(mmu_dir)]) != os.path.abspath(mmu_dir):
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    shutil.copy2(path, "%s.old-%s" % (path, stamp))
                write_vars(path, migrated)
                print("Migrated saved state in %s" % path, file=out)
            for note in notes:
                print("  %s" % note, file=out)

        for warning in install.get("warnings", []):
            print("WARNING: May need a manual edit for the new unit names: %s" % warning, file=out)

    clear_state(kconfig)


# -----------------------------------------------------------------------------
# Post-build check
# -----------------------------------------------------------------------------

def total_gates(kconfig):
    values = read_kconfig(kconfig)
    if values.get("MULTI_UNIT") == "y":
        units = sequence_edit.split_sequence(values.get(SYMBOL, ""), SEPARATOR)
        counts = [read_kconfig(unit_file(kconfig, u)).get("PARAM_NUM_GATES") for u in units]
    else:
        counts = [values.get("PARAM_NUM_GATES")]
    if not all(c and c.isdigit() for c in counts):
        return None
    return sum(int(c) for c in counts)


def gate_list_warnings(mmu_cfg, total_gates):
    """[mmu_machine] per-gate lists whose length no longer matches the total gate count."""
    if not os.path.exists(mmu_cfg):
        return []
    builder = ConfigBuilder(mmu_cfg)
    warnings = []
    for section in ("mmu_machine", "mmu_parameters"):
        if not builder.has_section(section):
            continue
        for option in GATE_LIST_OPTIONS:
            value = builder.get(section, option)
            if not value:
                continue
            n = len([v for v in value.split(",")])
            if n != total_gates:
                warnings.append("[%s] %s has %d entries but there are %d gates" % (section, option, n, total_gates))
    return warnings


# -----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Happy Hare MMU unit list migration")
    parser.add_argument("action", choices=["baseline", "current", "check", "kconfig", "prepare", "apply",
                                           "gate-lists"])
    parser.add_argument("--kconfig", default=os.getenv("KCONFIG_CONFIG", ".mmu_config"))
    parser.add_argument("--config-home", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--mode", default=os.getenv("F_CFG_UPGRADE_MODE", "refresh"))
    parser.add_argument("--no-service", action="store_true", default=bool(os.getenv("F_NO_SERVICE")))
    parser.add_argument("--mmu-cfg", default=None)
    parser.add_argument("--total-gates", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except Exception as e:
        traceback.print_exc()
        print("WARNING: MMU unit migration '%s' failed: %s" % (args.action, e), file=sys.stderr)
        return EXIT_FAILED


def _run(args):
    config_home = args.config_home or read_kconfig(args.kconfig).get("KLIPPER_CONFIG_HOME")
    config_home = os.path.expanduser(config_home) if config_home else None

    if args.baseline is not None:
        base = sequence_edit.split_sequence(args.baseline, SEPARATOR)
        state = read_state(args.kconfig)
        if state and isinstance(state.get("baseline"), list):
            base = state["baseline"]
    else:
        base = baseline(args.kconfig, config_home)

    if args.action == "baseline":
        print(SEPARATOR.join(base))
        return EXIT_NONE
    if args.action == "current":
        print(SEPARATOR.join(Plan(args.kconfig, base).current))
        return EXIT_NONE
    if args.action == "check":
        return check(args.kconfig, config_home, base, args.mode)
    if args.action == "kconfig":
        migrate_kconfig(args.kconfig, base)
    elif args.action == "prepare":
        prepare(args.kconfig, config_home, base)
    elif args.action == "apply":
        apply(args.kconfig, args.no_service)
    elif args.action == "gate-lists":
        # Advisory only: must never stop an install
        try:
            mmu_cfg = args.mmu_cfg or (_base(config_home, "mmu.cfg") if config_home else "")
            total = args.total_gates if args.total_gates is not None else total_gates(args.kconfig)
            for warning in gate_list_warnings(mmu_cfg, total) if total else []:
                print("WARNING: %s. Klipper will not start until it is fixed" % warning)
        except Exception as e:
            print("WARNING: Could not check per-gate list lengths: %s" % e)
    return EXIT_NONE


if __name__ == "__main__":
    sys.exit(main())
