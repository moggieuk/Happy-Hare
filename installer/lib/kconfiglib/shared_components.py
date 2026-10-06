"""
Components one unit of a multi-unit MMU can share with another (sync-feedback buffer, encoder),
and the printer-level capabilities (toolhead sensors) every unit takes from the top level.

A component is identified by its name (the name symbol, e.g. PARAM_ENCODER_NAME). A unit owns
one when it has the component and doesn't share it, and renders its section under that name.
A sharer names a section it doesn't render: another unit's, or one in the user's own config.

Each unit is configured by its own Kconfig parse, which can't see the other units' parses,
so a sharing unit reads what the other units SAVED: the parent .mmu_config (for MMU_UNITS)
and each sibling .mmu_config_<unit>. A sharer naming a sibling owner's component takes the
owner's real capabilities (e.g. which buffer sensors it has), which its own defaults
depend on.

Reading only happens when KCONFIG_PARENT is set, which install.sh does for unit parses
alone. Every other parse (the build's pickle, verify_pickle) sees no other units and keeps
the values saved by the last unit parse.

Values are only read from OWNERS, never from another sharer, so one refresh pass after all
units are saved converges.
"""

import collections
import io
import os
import re
import sys

HH_DEFAULT_TOKEN = " #~DEFAULT~#"
_STRING_VALUE_RE = re.compile(r'^"((?:[^\\"]|\\.)*)"$')
_UNESCAPE_RE = re.compile(r"\\(.)")

Kind = collections.namedtuple("Kind", "has shared name exports")

# What a sharer takes from its owner, and its value when the owner's file doesn't set it
Export = collections.namedtuple("Export", "symbol absent")

KINDS = {
    "buffer": Kind(
        has="MMU_HAS_SYNC_FEEDBACK_BUFFER",
        shared="MMU_SHARED_SYNC_FEEDBACK_BUFFER",
        name="PARAM_SYNC_FEEDBACK_BUFFER_NAME",
        exports=(Export("MMU_HAS_SENSOR_BUFFER_COMPRESSION", "n"),
                 Export("MMU_HAS_SENSOR_BUFFER_TENSION", "n"),
                 Export("MMU_HAS_SENSOR_BUFFER_PROPORTIONAL", "n"),
                 Export("PARAM_BUFFER_SPRING_STATE", "none")),
    ),
    "encoder": Kind(
        has="MMU_HAS_ENCODER",
        shared="MMU_SHARED_ENCODER",
        name="PARAM_ENCODER_NAME",
        exports=(),
    ),
}

# Printer-level capabilities every unit takes from the top-level config (the printer owns them)
PRINTER_FLAGS = ("MMU_HAS_SENSOR_TOOLHEAD", "MMU_HAS_SENSOR_EXTRUDER", "MMU_HAS_TOOLHEAD_CUTTER")

Owner = collections.namedtuple("Owner", "unit name values")

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


def export_value(values, export):
    return values.get(export.symbol) or export.absent


def is_owner(values, kind):
    return values.get(kind.has) == "y" and values.get(kind.shared) != "y"


def owned_name(unit, values, kind):
    """The name an owner's section is rendered under (its unit's name by default)."""
    return values.get(kind.name, unit)


def _parent():
    parent = os.environ.get("KCONFIG_PARENT", "")
    return parent if parent and os.path.isfile(parent) else ""


def owners(kind_name, parent=None, unit=None):
    """The other units that own the component, in MMU_UNITS order."""
    kind = KINDS[kind_name]
    parent = _parent() if parent is None else parent
    if not parent:
        return []
    unit = os.environ.get("UNIT_NAME", "") if unit is None else unit
    result = []
    for other in split_units(read_values(parent).get("MMU_UNITS", "")):
        values = read_values(unit_file(parent, other))
        if other != unit and is_owner(values, kind):
            result.append(Owner(other, owned_name(other, values, kind), values))
    return result


def _saved():
    path = os.environ.get("KCONFIG_CONFIG", "")
    return read_values(path) if path else {}


def context_key():
    """
    Everything a parse reads from the saved configs: what the functions below return, and the
    unit's own saved shared flag and name (read with saved-config-value).
    """
    saved = _saved()
    parent = _parent()
    if not saved and not parent:
        return ()
    top = read_values(parent) if parent else {}
    key = [tuple(split_units(top.get("MMU_UNITS", ""))),
           tuple(top.get(symbol) for symbol in PRINTER_FLAGS)]
    for kind_name, kind in sorted(KINDS.items()):
        key.append((kind_name, saved.get(kind.shared), saved.get(kind.name),
                    tuple((o.unit, o.name, tuple(export_value(o.values, e) for e in kind.exports))
                          for o in owners(kind_name))))
    return tuple(key)


def stale(config, parent):
    """
    True when a sharer's saved capabilities no longer match those of the owner it names, e.g.
    the owner was configured after it, or changed its buffer since. Units that share nothing
    never are.
    """
    saved = read_values(config)
    for kind_name, kind in KINDS.items():
        name = saved.get(kind.name, "")
        if saved.get(kind.shared) != "y" or not name:
            continue
        owner = next((o for o in owners(kind_name, parent, unit="") if o.name == name), None)
        if owner and any(export_value(owner.values, e) != export_value(saved, e)
                         for e in kind.exports):
            return True
    return False


# -------------------------------------------------------------------------------------------
# Kconfig preprocessor functions (registered by kconfigfunctions.py)
# -------------------------------------------------------------------------------------------

def _yn(value):
    return "y" if value else "n"


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _owner(kind, index):
    found = owners(kind)
    i = int(index)
    return found[i] if i < len(found) else None


def _export(kind, symbol):
    return next(e for e in KINDS[kind].exports if e.symbol == symbol)


def owner_max(_kconf, _name, kind):
    """The last index into the owners, for an @repeat over them (0 without any)."""
    return str(max(len(owners(kind)) - 1, 0))


def owner_name(_kconf, _name, kind, index):
    """The owner's name at 'index', or "" past the end."""
    owner = _owner(kind, index)
    return _escape(owner.name) if owner else ""


def owner_names(_kconf, _name, kind):
    """The other units' names for their own component, for the menu ("" without any)."""
    names = []
    for owner in owners(kind):
        if owner.name and owner.name not in names:
            names.append(owner.name)
    return _escape(", ".join(names))


def owner_export(_kconf, _name, kind, index, symbol):
    owner = _owner(kind, index)
    return export_value(owner.values if owner else {}, _export(kind, symbol))


def printer_flag(_kconf, _name, symbol):
    """A printer-level flag for a unit parse, from the top-level config (n without one)."""
    parent = _parent()
    return _yn(parent and read_values(parent).get(symbol) == "y")


FUNCTIONS = {
    "owner-export": (owner_export, 3, 3),
    "owner-max": (owner_max, 1, 1),
    "owner-name": (owner_name, 2, 2),
    "owner-names": (owner_names, 1, 1),
    "printer-flag": (printer_flag, 1, 1),
}


def main(argv):
    if len(argv) == 3 and argv[0] == "stale":
        print("y" if stale(argv[1], argv[2]) else "n")
        return 0
    print("usage: python -m shared_components stale <unit config> <parent config>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
