"""
Components one unit of a multi-unit MMU can share with another (sync-feedback buffer, encoder).

Each unit is configured by its own Kconfig parse, which can't see the other units' parses,
so a sharing unit reads what the other units SAVED: the parent .mmu_config (for MMU_UNITS)
and each sibling .mmu_config_<unit>. That gives it a pick list of the units that own the
component and the owner's real capabilities (e.g. which buffer sensors it has), which the
sharer's own defaults depend on.

Reading only happens when KCONFIG_PARENT is set, which install.sh does for unit parses
alone. Every other parse (the build's pickle, verify_pickle) sees no other units and keeps
the values saved by the last unit parse.

Values are only read from OWNERS (has the component and isn't sharing it), never from
another sharer, so one refresh pass after all units are saved converges.
"""

import collections
import io
import os
import re
import sys

# The preprocessor variable (root Kconfig) holding how many units a pick list offers
SLOTS_VARIABLE = "shared_slots"

HH_DEFAULT_TOKEN = " #~DEFAULT~#"
_STRING_VALUE_RE = re.compile(r'^"((?:[^\\"]|\\.)*)"$')
_UNESCAPE_RE = re.compile(r"\\(.)")

Kind = collections.namedtuple("Kind", "has shared name choice exports")

# exports: (symbol, word used in the pick list label) the sharer takes from its owner
KINDS = {
    "buffer": Kind(
        has="MMU_HAS_SYNC_FEEDBACK_BUFFER",
        shared="MMU_SHARED_SYNC_FEEDBACK_BUFFER",
        name="PARAM_SYNC_FEEDBACK_BUFFER_NAME",
        choice="CHOICE_SHARED_BUFFER",
        exports=(("MMU_HAS_SENSOR_BUFFER_COMPRESSION", "compression"),
                 ("MMU_HAS_SENSOR_BUFFER_TENSION", "tension"),
                 ("MMU_HAS_SENSOR_BUFFER_PROPORTIONAL", "proportional")),
    ),
    "encoder": Kind(
        has="MMU_HAS_ENCODER",
        shared="MMU_SHARED_ENCODER",
        name="PARAM_ENCODER_NAME",
        choice="CHOICE_SHARED_ENCODER",
        exports=(),
    ),
}

Slot = collections.namedtuple("Slot", "unit member owner values")

_cache = {}


def read_values(path):
    """CONFIG_ assignments of a value file, #~DEFAULT~# ones included; {} if unreadable."""
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = (os.path.realpath(path), getattr(stat, "st_mtime_ns", stat.st_mtime), stat.st_size)
    if key not in _cache:
        values = {}
        with io.open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if not line.startswith("CONFIG_") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if value.endswith(HH_DEFAULT_TOKEN):
                    value = value[:-len(HH_DEFAULT_TOKEN)]
                m = _STRING_VALUE_RE.match(value)
                values[name[len("CONFIG_"):]] = _UNESCAPE_RE.sub(r"\1", m.group(1)) if m else value
        _cache[key] = values
    return _cache[key]


def split_units(raw):
    return [u.strip() for u in raw.split(",") if u.strip()]


def unit_file(parent, unit):
    return "%s_%s" % (parent, unit)


def is_owner(values, kind):
    return values.get(kind.has) == "y" and values.get(kind.shared) != "y"


def _symbol(unit):
    return re.sub(r"[^A-Z0-9_]", "_", unit.upper())


def _parent():
    parent = os.environ.get("KCONFIG_PARENT", "")
    return parent if parent and os.path.isfile(parent) else ""


def slots(kind_name, parent=None, unit=None, limit=None):
    """The pick list for this unit: other units that own the component, or have no file yet."""
    kind = KINDS[kind_name]
    parent = _parent() if parent is None else parent
    if not parent:
        return []
    unit = os.environ.get("UNIT_NAME", "") if unit is None else unit
    result, members = [], set()
    for other in split_units(read_values(parent).get("MMU_UNITS", "")):
        if other == unit:
            continue
        path = unit_file(parent, other)
        configured = os.path.isfile(path)
        values = read_values(path) if configured else {}
        if configured and not is_owner(values, kind):
            continue
        member = base = "%s_%s" % (kind.choice, _symbol(other))
        n = 2
        while member in members:           # 'box-1' and 'box_1' map to the same symbol
            member = "%s_%d" % (base, n)
            n += 1
        members.add(member)
        result.append(Slot(other, member, configured, values))
        if len(result) == limit:
            break
    return result


def _limit(kconf):
    return int(kconf.variables[SLOTS_VARIABLE].expanded_value)


def _slot(kconf, kind_name, index):
    found = slots(kind_name, limit=_limit(kconf))
    try:
        return found[int(index) - 1]
    except (IndexError, ValueError):
        return None


def _saved():
    path = os.environ.get("KCONFIG_CONFIG", "")
    return read_values(path) if path else {}


def unresolved(kind_name, limit=None):
    """This unit was saved sharing a name that isn't in its pick list."""
    kind = KINDS[kind_name]
    if not _parent():
        return False
    saved = _saved()
    name = saved.get(kind.name, "")
    return (saved.get(kind.shared) == "y" and bool(name)
            and name not in [s.unit for s in slots(kind_name, limit=limit)])


def label(slot, kind):
    if not slot.owner:
        return "%s (not configured yet)" % slot.unit
    if not kind.exports:
        return slot.unit
    words = [word for sym, word in kind.exports if slot.values.get(sym) == "y"]
    return "%s (%s)" % (slot.unit, " + ".join(words) if words else "no sensors")


def context_key():
    """
    Everything the functions below can return for the current environment (a superset:
    slots beyond the pick list's length are included).
    """
    parent = _parent()
    if not parent:
        return ()
    saved = _saved()
    key = [tuple(split_units(read_values(parent).get("MMU_UNITS", "")))]
    for kind_name, kind in sorted(KINDS.items()):
        key.append((kind_name, saved.get(kind.shared), saved.get(kind.name),
                    tuple((s.unit, s.member, s.owner,
                           tuple(s.values.get(sym) for sym, _ in kind.exports))
                          for s in slots(kind_name))))
    return tuple(key)


def stale(config, parent):
    """
    True when a sharer's saved capabilities no longer match its owner's, e.g. the owner was
    configured after it, or changed its buffer since. Units that share nothing never are.
    """
    saved = read_values(config)
    for kind in KINDS.values():
        if saved.get(kind.shared) != "y":
            continue
        owner = read_values(unit_file(parent, saved.get(kind.name, "")))
        if not is_owner(owner, kind):
            continue
        if any((owner.get(sym) == "y") != (saved.get(sym) == "y") for sym, _ in kind.exports):
            return True
    return False


# -------------------------------------------------------------------------------------------
# Kconfig preprocessor functions (registered by kconfigfunctions.py)
# -------------------------------------------------------------------------------------------

def _yn(value):
    return "y" if value else "n"


def shared_active(_kconf, _name):
    return _yn(_parent())


def shared_any(kconf, _name, kind):
    return _yn(slots(kind, limit=_limit(kconf)))


def shared_slot_used(kconf, _name, kind, index):
    return _yn(_slot(kconf, kind, index))


def shared_owner(kconf, _name, kind, index):
    slot = _slot(kconf, kind, index)
    return _yn(slot and slot.owner)


def shared_member(kconf, _name, kind, index):
    slot = _slot(kconf, kind, index)
    return slot.member if slot else KINDS[kind].choice + "_NONE"


def shared_name(kconf, _name, kind, index):
    slot = _slot(kconf, kind, index)
    return slot.unit if slot else ""


def shared_label(kconf, _name, kind, index):
    slot = _slot(kconf, kind, index)
    return label(slot, KINDS[kind]) if slot else ""


def shared_export(kconf, _name, kind, index, symbol):
    slot = _slot(kconf, kind, index)
    return _yn(slot and slot.values.get(symbol) == "y")


def shared_saved_member(kconf, _name, kind):
    """The member to select by default: the one for the saved name, else none."""
    choice = KINDS[kind].choice
    if unresolved(kind, _limit(kconf)):
        return choice + "_UNRESOLVED"
    name = _saved().get(KINDS[kind].name, "")
    for slot in slots(kind, limit=_limit(kconf)):
        if slot.unit == name:
            return slot.member
    return choice + "_NONE"


def shared_unresolved(kconf, _name, kind):
    return _yn(unresolved(kind, _limit(kconf)))


def shared_unresolved_label(_kconf, _name, kind):
    return "%s (not an owner)" % (_saved().get(KINDS[kind].name) or "-")


FUNCTIONS = {
    "shared-active": (shared_active, 0, 0),
    "shared-any": (shared_any, 1, 1),
    "shared-export": (shared_export, 3, 3),
    "shared-label": (shared_label, 2, 2),
    "shared-member": (shared_member, 2, 2),
    "shared-name": (shared_name, 2, 2),
    "shared-owner": (shared_owner, 2, 2),
    "shared-saved-member": (shared_saved_member, 1, 1),
    "shared-slot-used": (shared_slot_used, 2, 2),
    "shared-unresolved": (shared_unresolved, 1, 1),
    "shared-unresolved-label": (shared_unresolved_label, 1, 1),
}


def main(argv):
    if len(argv) == 3 and argv[0] == "stale":
        print("y" if stale(argv[1], argv[2]) else "n")
        return 0
    print("usage: python -m shared_components stale <unit config> <parent config>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
