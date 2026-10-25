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
#              config (gate counts, migration history, manual-edit warnings)
#   apply    - verify installed migration history and remove stale unit files
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import argparse
import ast
import configparser
import datetime
import json
import importlib
import importlib.util
import os
import re
import shutil
import sys
import traceback
import uuid

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


def _load_state_migration():
    # Load the dependency-free runtime helpers without importing Klipper's `extras`
    # package into the installer process (or a test harness that installs it later).
    package = "_hh_mmu_state"
    if package not in sys.modules:
        path = os.path.join(SRC, "extras", "mmu", "__init__.py")
        spec = importlib.util.spec_from_file_location(package, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(package + ".mmu_state_migration")


state_migration = _load_state_migration()
gate_map = state_migration.gate_map
gates_moved = state_migration.gates_moved
append_history = state_migration.append_history


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
    "default_gate_remaining_weight",
    "default_gate_initial_weight",
    "default_endless_spool_groups",
]



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
        elif default or pattern is None or sym in (SYMBOL, "PARAM_STATE_MIGRATION"):
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

    installed_values = read_kconfig(os.path.join(config_home, "mmu", os.path.basename(kconfig))) if config_home else {}
    was_multi = installed_values.get("MULTI_UNIT", "n") if installed_values else None
    mode_conversion = (plan.single and len(plan.baseline) > 1) or (
        was_multi in ("y", "n") and (was_multi == "y") == plan.single)
    # An explicit single/multi conversion is not an inferred rename. The normal
    # Replace confirmation warns that unfamiliar names start as new units.
    if not plan.captured and not mode_conversion:
        try:
            state_migration.require_known_unit_changes(plan.baseline, plan.current)
        except ValueError as e:
            print("ERROR: %s" % e, file=out)
            return EXIT_REFUSED

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
            print("ERROR: The installed units do not match %s but no change was recorded. Use './install.sh -i' (Replace mode) to rename, remove or reorder"
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
            print("If the selected gate or tool is removed or the gate is renumbered, selection will be reset to unknown; select or home again after"
                  " the install", file=out)
        else:
            print("Calibration, gate maps and saved state move with each unit", file=out)
        print("Migration intent will be saved in mmu.cfg and applied by Klipper at startup", file=out)
        if gates_moved(*_check_layout(plan, config_home)):
            print("Unload filament before installing changes that renumber gates", file=out)
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
    previous_install = state.pop("install", None)

    if plan.changed() and config_home and installed_units(config_home) is not None:
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
            "warnings": manual_edit_warnings(plan, config_home, kconfig),
            "encoders": {before: after for before, after in
                         component_names(kconfig, plan, shared_components.KINDS["encoder"]).values()
                         if before != after},
        }
        if previous_install and all(previous_install.get(k) == state["install"].get(k)
                                    for k in ("from", "to", "origin", "new_gates")):
            state["install"] = previous_install
        state["install"].setdefault("migration_id", uuid.uuid4().hex)
    history = installed_option(config_home, "mmu.cfg", "mmu_machine", "state_migration") if config_home else None
    history = state_migration.parse_history(history)
    if state.get("install"):
        history = append_history(history, state["install"])
    if not history and not state.get("install") and not read_state(kconfig) and \
            read_kconfig(kconfig).get("PARAM_STATE_MIGRATION", "{}") == "{}":
        return
    history = trim_history(kconfig, history)
    state["history"] = history
    # Save the retry ID and original counts before publishing the template value.
    write_state(kconfig, state)
    write_history(kconfig, history)


def trim_history(kconfig, history):
    """Best-effort pruning using only the variables path recorded in Kconfig."""
    if not history:
        return history
    path = read_kconfig(kconfig).get("PARAM_MMU_VARS_CFG")
    if not path:
        return history
    try:
        parser = configparser.ConfigParser(interpolation=None)
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            parser.read_file(handle)
        variables = {key: ast.literal_eval(value) for key, value in parser.items("Variables")}
    except (OSError, UnicodeError, configparser.Error, ValueError, SyntaxError):
        return history
    checkpoint = variables.get(state_migration.C.VARS_MMU_MIGRATION_APPLIED)
    if not isinstance(checkpoint, str):
        return history
    for index, step in enumerate(history["steps"]):
        if step["id"] == checkpoint:
            # Keep the checkpoint itself: startup validates it before skipping it.
            return dict(history, steps=history["steps"][index:])
    return history


def write_history(kconfig, history):
    """Carry installed history through template regeneration without touching saved state."""
    with open(kconfig, encoding="utf-8") as f:
        lines = [line for line in f if not line.startswith("CONFIG_PARAM_STATE_MIGRATION=")]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append("CONFIG_PARAM_STATE_MIGRATION=%s\n" % _quote(repr(history or {})))
    tmp = kconfig + ".migration-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    shutil.copymode(kconfig, tmp)
    os.replace(tmp, kconfig)


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
            if re.match(r"^state_migration\s*[:=]", line):
                continue  # Installer-owned history is never a manual unit reference.
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


def apply(kconfig, out=sys.stdout):
    state = read_state(kconfig)
    install = (state or {}).get("install")
    if state:
        home = install["config_home"] if install else read_kconfig(kconfig).get("KLIPPER_CONFIG_HOME")
        actual = installed_option(os.path.expanduser(home), "mmu.cfg", "mmu_machine", "state_migration") if home else None
        if state_migration.parse_history(actual) != state.get("history"):
            raise ValueError("Installed mmu.cfg does not contain the prepared migration history; retry install")
    if install:
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
        apply(args.kconfig)
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
