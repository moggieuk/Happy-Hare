# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Migrate saved MMU state at startup using explicit installer intent
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Shared by startup and the installer; no Klipper or installer dependencies.
# Compute the complete migration before staging any writes.

import ast
import copy
import re
import uuid

from . import mmu_constants as C

GATE_SENSOR_KEY = re.compile(r"^(mmu_[a-z_]+?)_(\d+)$")
INTENT_FIELDS = ("from", "to", "origin", "old_gates", "new_gates", "encoders")

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

def namespaced(variable, namespace):
    return variable.replace("mmu_", "mmu_%s_" % namespace)


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


def selection_changed(variables, mapping, new_total, units_removed=False):
    """Keep selection only when its gate identity/index and tool still exist."""
    gate = variables.get(C.VARS_MMU_GATE_SELECTED, C.TOOL_GATE_UNKNOWN)
    tool = variables.get(C.VARS_MMU_TOOL_SELECTED, C.TOOL_GATE_UNKNOWN)
    if gate == C.TOOL_GATE_UNKNOWN and tool == C.TOOL_GATE_UNKNOWN:
        return False
    if mapping is None:
        return True
    if gate == C.TOOL_GATE_BYPASS:
        # Saved state does not identify the bypass owner. Any removed unit may
        # have owned it, even when the first unit's gates have not moved.
        return units_removed or mapping.get(0) != 0
    return (type(gate) is not int or gate < 0 or mapping.get(gate) != gate
            or (tool != C.TOOL_GATE_UNKNOWN and (type(tool) is not int or not 0 <= tool < new_total)))


def startup_layout(variables, install, target, identity):
    """
    The layout the saved gate lists still have, under the new unit names (target: old name ->
    new name or None), or None when none is needed (no gate moved) or it can't be known. A
    removed unit is named "" so no unit ever matches its gates.

    A saved layout naming only installed units, each once, is kept in its own order: it may be
    pending from an earlier install that Klipper hasn't started on yet, so the lists still
    have that order. Units added since are missing from it because they have no saved gates.
    """
    saved = valid_layout(variables.get(C.VARS_MMU_GATE_MAP_UNITS))
    if saved is not None:
        names = [name for name, _ in saved if name]
        if not set(names) <= set(install["from"]):
            saved = None
    if saved is None:
        if identity or any(install["old_gates"].get(o) is None for o in install["from"]):
            return None
        saved = [[o, install["old_gates"][o]] for o in install["from"]]
    layout = []
    for name, count in saved:
        new = target.get(name)
        if not new:
            layout.append(["", count])
            continue
        # Preserve the original list offsets but tombstone gates removed at any
        # intermediate install, so later growth cannot resurrect their state.
        limits = [count, install["old_gates"].get(name), install["new_gates"].get(new)]
        kept = min(n for n in limits if n is not None)
        layout.append([new, kept])
        if kept < count:
            layout.append(["", count - kept])
    return layout


def migrate_unit_state(variables, install):
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

    # The gate lists are left as they are. Happy Hare matches them to the units at startup using
    # this layout: the order and gate counts they were saved with, under the new unit names
    layout = startup_layout(variables, install, target, identity)
    if layout is None:
        result.pop(C.VARS_MMU_GATE_MAP_UNITS, None)
    else:
        result[C.VARS_MMU_GATE_MAP_UNITS] = layout
    if not identity:
        notes.append("Gate maps matched using the original unit layout" if layout is not None
                     else "Gate counts unknown so gate maps were not migrated")

    # Selection still refers to the original saved-list offsets, even when
    # earlier pending installs have changed the configured gate numbering.
    selection_mapping = match_by_unit(layout, [[name, install["new_gates"][name]] for name in install["to"]]) \
        if layout is not None and all(install["new_gates"].get(name) is not None for name in install["to"]) else mapping
    if selection_changed(variables, selection_mapping, new_total,
                         units_removed=any(name is None for name in target.values())):
        reset = False
        for var in (C.VARS_MMU_GATE_SELECTED, C.VARS_MMU_TOOL_SELECTED):
            if var in variables and variables[var] != C.TOOL_GATE_UNKNOWN:
                result[var] = C.TOOL_GATE_UNKNOWN
                reset = True
        if reset:
            notes.append("Selected gate and tool reset to unknown")

    # Unit-scoped sensor keys follow their unit's name. Per-gate keys are renumbered at startup,
    # with the gate lists
    sensors = variables.get(C.VARS_MMU_SENSOR_ENABLED)
    if isinstance(sensors, dict):
        new = {}
        for key, value in sensors.items():
            prefix, sep, rest = key.partition(":")
            if sep and prefix in target:
                if target[prefix] is not None:
                    new["%s:%s" % (target[prefix], rest)] = value
                continue
            new[key] = value
        result[C.VARS_MMU_SENSOR_ENABLED] = new

    return result, notes


def _names(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(n, str) and n for n in value) and len(set(value)) == len(value))


def validate_history(history):
    """Reject an incomplete/unknown journal before changing any live variables."""
    if not isinstance(history, dict) or history.get("version") != 1:
        raise ValueError("Unsupported history MMU state migration version")
    steps = history.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Configured MMU state migration has no steps")
    ids, previous = set(), None
    for step in steps:
        if not isinstance(step, dict) or not _names(step.get("from")) or not _names(step.get("to")):
            raise ValueError("Invalid unit names in history MMU state migration")
        ident = step.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("Invalid or duplicate history MMU migration ID")
        ids.add(ident)
        if previous is not None and step["from"] != previous:
            raise ValueError("Configured MMU state migrations do not form a continuous unit history")
        previous = step["to"]
        origin = step.get("origin")
        if not isinstance(origin, dict) or set(origin) != set(step["to"]):
            raise ValueError("Invalid history MMU unit origins")
        sources = [v for v in origin.values() if v is not None]
        if any(not isinstance(v, str) or v not in step["from"] for v in sources) or len(set(sources)) != len(sources):
            raise ValueError("Invalid history MMU unit origins")
        for field, names in (("old_gates", step["from"]), ("new_gates", step["to"])):
            counts = step.get(field)
            if not isinstance(counts, dict) or set(counts) != set(names) or any(
                    n is not None and (type(n) is not int or n < 0) for n in counts.values()):
                raise ValueError("Invalid gate counts in history MMU state migration")
        encoders = step.get("encoders")
        if not isinstance(encoders, dict) or any(
                not isinstance(k, str) or not k or (v is not None and (not isinstance(v, str) or not v))
                for k, v in encoders.items()):
            raise ValueError("Invalid encoder names in history MMU state migration")
    return steps


def parse_history(value):
    try:
        history = ast.literal_eval(value) if isinstance(value, str) else copy.deepcopy(value)
    except (ValueError, SyntaxError) as e:
        raise ValueError("Invalid MMU state_migration history: %s" % e)
    if history in (None, {}):
        return None
    validate_history(history)
    return history


def append_history(history, install):
    """Append one installer operation; retrying the same ID is idempotent."""
    updated = parse_history(history) or {"version": 1, "steps": []}
    step = {key: copy.deepcopy(install.get(key, {})) for key in INTENT_FIELDS}
    step["id"] = install.get("migration_id") or uuid.uuid4().hex
    for existing in updated["steps"]:
        if existing["id"] == step["id"]:
            if existing != step:
                raise ValueError("MMU migration ID was reused with different changes")
            return updated
    updated["steps"].append(step)
    validate_history(updated)
    return updated


def apply_history(variables, current_names, history):
    """Apply only config records after the checkpoint saved with the migrated state."""
    history = parse_history(history)
    checkpoint = variables.get(C.VARS_MMU_MIGRATION_APPLIED)
    if checkpoint is not None and (not history or checkpoint not in [s["id"] for s in history["steps"]]):
        raise ValueError("Saved MMU migration checkpoint is missing from state_migration history; restore matching configuration and saved state")
    result = copy.deepcopy(variables)
    notes = []
    if not history:
        return result, notes
    steps = history["steps"]
    start = next((i + 1 for i, s in enumerate(steps) if s["id"] == checkpoint), 0)
    if start < len(steps):
        if steps[-1]["to"] != current_names:
            raise ValueError("Pending MMU migration does not match configured units; finish the installation before starting Klipper")
        saved_layout = valid_layout(result.get(C.VARS_MMU_GATE_MAP_UNITS))
        if saved_layout:
            require_known_unit_changes([name for name, _ in saved_layout], steps[start]["from"])
        for step in steps[start:]:
            result, messages = migrate_unit_state(result, step)
            notes.extend(messages)
    result[C.VARS_MMU_MIGRATION_APPLIED] = steps[-1]["id"]
    return result, notes


def valid_layout(layout):
    if not isinstance(layout, list) or not all(
            isinstance(u, (list, tuple)) and len(u) == 2 and isinstance(u[0], str)
            and type(u[1]) is int and u[1] >= 0 for u in layout):
        return None
    names = [name for name, _ in layout if name]
    return [list(u) for u in layout] if len(names) == len(set(names)) else None


def require_known_unit_changes(saved_names, current_names):
    """A missing name plus a new name may be a rename; never guess its identity.

    Empty names mark explicitly removed units and must not be paired with new ones.
    Recorded intent has already renamed retained units and tombstoned removals.
    """
    removed = sorted(set(saved_names) - set(current_names) - {""})
    added = sorted(set(current_names) - set(saved_names))
    if removed and added:
        raise ValueError(
            "Unrecorded MMU unit change: previous unit(s) %s, new unit(s) %s. "
            "Cannot distinguish a rename from replacement; saved state has not been migrated. "
            "Restore the previous unit names, then run './install.sh -i' in Replace mode "
            "and use the unit editor to rename, or explicitly remove and add units."
            % (", ".join(removed), ", ".join(added)))


def match_by_unit(saved_layout, current_layout):
    old_first, first = {}, 0
    for name, count in saved_layout:
        old_first[name] = (first, count)
        first += count
    mapping, first = {}, 0
    for name, count in current_layout:
        if name in old_first:
            start, old_count = old_first[name]
            for local in range(min(count, old_count)):
                mapping[start + local] = first + local
        first += count
    return mapping


def migrate_state(variables, current_layout, defaults, history=None):
    """One startup transaction: unit keys, selection, sensors, lists, layout, and receipt."""
    result, notes = apply_history(variables, [name for name, _ in current_layout], history)
    saved_layout = valid_layout(result.get(C.VARS_MMU_GATE_MAP_UNITS))
    if saved_layout:
        require_known_unit_changes([name for name, _ in saved_layout],
                                   [name for name, _ in current_layout])
    num_gates = sum(n for _, n in current_layout)
    mapping = match_by_unit(saved_layout, current_layout) if saved_layout else None
    errors = []
    if mapping is not None and gates_moved(mapping, sum(n for _, n in saved_layout)):
        if selection_changed(result, mapping, num_gates,
                             units_removed=any(name and name not in {n for n, _ in current_layout}
                                               for name, _ in saved_layout)):
            errors.append("Persisted selection dropped because its gate or tool was removed or renumbered")
            result[C.VARS_MMU_GATE_SELECTED] = result[C.VARS_MMU_TOOL_SELECTED] = C.TOOL_GATE_UNKNOWN
        sensors = result.get(C.VARS_MMU_SENSOR_ENABLED, {})
        if isinstance(sensors, dict):
            renamed = {}
            for key, value in sensors.items():
                m = GATE_SENSOR_KEY.match(key)
                if m is None:
                    renamed[key] = value
                elif int(m.group(2)) in mapping:
                    renamed["%s_%d" % (m.group(1), mapping[int(m.group(2))])] = value
            result[C.VARS_MMU_SENSOR_ENABLED] = renamed

    fitted = {}
    for var, default in defaults.items():
        saved = result.get(var)
        if saved is None:
            continue
        fitted[var] = list(default)
        if not isinstance(saved, list):
            errors.append("Invalid value specified with %s" % var)
            continue
        if saved_layout and sum(n for _, n in saved_layout) == len(saved):
            positions = mapping
            how = "matched to its units by name, any new gates use defaults" if saved_layout != current_layout else ''
        elif len(saved) == num_gates:
            positions, how = {g: g for g in range(num_gates)}, ''
        else:
            positions, how = None, "ignored because no saved unit layout matches it, using defaults"
        if how:
            notes.append("%s (%d entries): %s" % (var, len(saved), how))
        if positions is None:
            continue
        if var == C.VARS_MMU_TOOL_TO_GATE_MAP:
            fitted[var] = [positions.get(saved[t], default[t]) if t < len(saved) and isinstance(saved[t], int)
                           else default[t] for t in range(num_gates)]
        else:
            for old, new in positions.items():
                fitted[var][new] = saved[old]
            if var == C.VARS_MMU_ENDLESS_SPOOL_GROUPS:
                groups = [saved[old] for old in positions]
                next_group = max([g for g in groups if isinstance(g, int)] + [-1]) + 1
                for gate in range(num_gates):
                    if gate not in positions.values() or fitted[var][gate] is None:
                        fitted[var][gate], next_group = next_group, next_group + 1
    # Save the checkpoint with the migrated values, so a retry cannot apply a
    # completed config history twice.
    result.update(fitted)
    result[C.VARS_MMU_GATE_MAP_UNITS] = current_layout
    return result, errors, notes


def stage_migration(machine, history=None):
    """Called once at config load, before any unit's connect/ready consumers."""
    manager = machine.var_manager
    original = manager.save_variables.allVariables
    maps = machine.mmu_controller.gate_maps
    defaults = {var: getattr(maps, attr) for var, attr, _ in maps._gate_map_vars}
    defaults[C.VARS_MMU_TOOL_TO_GATE_MAP] = maps.ttg_map
    defaults[C.VARS_MMU_ENDLESS_SPOOL_GROUPS] = maps.endless_spool_groups
    layout = [[unit.name, unit.num_gates] for unit in machine.units]
    migrated, errors, notes = migrate_state(original, layout, defaults, history)
    # No mutation until the entire migration has validated and completed.
    for key in set(original) - set(migrated):
        manager.delete(key)
    for key, value in migrated.items():
        if key not in original or original[key] != value:
            manager.set(key, value)
    maps._migration_errors = errors
    maps._migration_notes = notes
