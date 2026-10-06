# Happy Hare: Model behind menuconfig's 'sequence_editor' dialog.
#
# A sequence_editor symbol holds an ordered list of unique names. The value
# alone cannot tell a rename from a remove plus an add, so this model tracks
# where each entry came from relative to a baseline list and records that in
# a '<config>.<SYMBOL>.changes' JSON sidecar. It has no curses dependency so
# the installer and tests can use it directly.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import os


def split_sequence(value, separator):
    if not value:
        return []
    return [part.strip() for part in value.split(separator) if part.strip()]


def join_sequence(names, separator):
    # Separator plus a space ("a, b"), the spacing the shipped defaults use,
    # unless the separator already ends in one
    joiner = separator if separator[-1:].isspace() else separator + " "
    return joiner.join(names)


def changes_path(config_filename, sym_name):
    return "{}.{}.changes".format(config_filename, sym_name)


def read_changes(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def origins_from_changes(data, baseline, current):
    """
    The recorded origin of each entry in 'current', or None if 'data' was not
    recorded for this baseline and value (e.g. the config was edited by hand).
    """
    if not data or data.get("from") != baseline or data.get("to") != current:
        return None
    origin = data.get("origin")
    if not isinstance(origin, dict) or set(origin) != set(current):
        return None
    sources = [o for o in origin.values() if o is not None]
    if len(sources) != len(set(sources)) or not set(sources) <= set(baseline):
        return None
    return dict(origin)


def origins_by_name(baseline, current):
    return {name: (name if name in baseline else None) for name in current}


class SequenceModel:
    """
    rows:    current entries, in order, as [name, origin] where origin is the
             baseline name the entry came from, or None for a new entry.
    removed: baseline names that no longer appear, as an origin, in any row.
    """

    def __init__(self, baseline, current, origins=None, validator=None,
                 append_only=False):
        self.baseline = list(baseline)
        self.validator = validator
        self.append_only = append_only
        if origins is None:
            origins = origins_by_name(self.baseline, current)
        self.rows = [[name, origins.get(name)] for name in current]

    @classmethod
    def load(cls, baseline, current, changes=None, **kwargs):
        origins = origins_from_changes(changes, baseline, current)
        return cls(baseline, current, origins, **kwargs)

    # State

    def names(self):
        return [name for name, _ in self.rows]

    def origins(self):
        return {name: origin for name, origin in self.rows}

    @property
    def removed(self):
        used = {origin for _, origin in self.rows}
        return [name for name in self.baseline if name not in used]

    def value(self, separator):
        return join_sequence(self.names(), separator)

    def is_changed(self):
        return self.rows != [[name, name] for name in self.baseline]

    def structural(self):
        """True for anything other than appending new entries to the baseline."""
        n = len(self.baseline)
        head, tail = self.rows[:n], self.rows[n:]
        return (head != [[name, name] for name in self.baseline]
                or any(origin is not None for _, origin in tail))

    def to_changes(self):
        return {"from": self.baseline, "to": self.names(), "origin": self.origins()}

    def summary(self):
        lines = []
        for name, origin in self.rows:
            if origin is None:
                lines.append("add {}".format(name))
            elif origin != name:
                lines.append("rename {} -> {}".format(origin, name))
        for name in self.removed:
            lines.append("remove {}".format(name))
        kept = [origin for _, origin in self.rows if origin is not None]
        if kept != [name for name in self.baseline if name in kept]:
            lines.append("reorder")
        return lines

    # Validation

    def check_name(self, name, index=None):
        """An error message for 'name' as the entry at 'index', or None."""
        if not name:
            return "Name cannot be empty"
        for i, (other, _) in enumerate(self.rows):
            if i != index and other == name:
                return "'{}' is already in the list".format(name)
        if self.validator is not None and not self.validator.fullmatch(name):
            return "'{}' is not valid syntax -- see help".format(name)
        return None

    # Operations. Each returns None on success or an error message, and
    # leaves the model unchanged on error.

    def _fixed(self):
        # Everything up to the last entry that came from the baseline, plus
        # what was removed: only what follows may change while append-only
        last = max([i for i, (_, origin) in enumerate(self.rows) if origin is not None] + [-1])
        return [list(row) for row in self.rows[:last + 1]], self.removed

    def _apply(self, change):
        rows = [list(row) for row in self.rows]
        fixed = self._fixed()
        change()
        if self.append_only and self._fixed() != fixed:
            self.rows = rows
            return "Only appending new entries is allowed here -- see help"
        return None

    def add(self, name, index=None):
        error = self.check_name(name)
        if error:
            return error
        index = len(self.rows) if index is None else index
        # A removed baseline name comes back as itself rather than as a new entry
        origin = name if name in self.removed else None
        return self._apply(lambda: self.rows.insert(index, [name, origin]))

    def rename(self, index, name):
        if name == self.rows[index][0]:
            return None
        error = self.check_name(name, index)
        if error:
            return error

        def change():
            self.rows[index][0] = name
            # Renaming back to a removed baseline name simply restores it
            if self.rows[index][1] is None and name in self.removed:
                self.rows[index][1] = name
        return self._apply(change)

    def remove(self, index):
        if len(self.rows) <= 1:
            return "The list must keep at least one entry"
        return self._apply(lambda: self.rows.pop(index))

    def restore(self, name):
        if name not in self.removed:
            return None
        error = self.check_name(name)
        if error:
            return error

        def change():
            # Back to its baseline position, relative to the rows still kept
            position = self.baseline.index(name)
            before = set(self.baseline[:position])
            index = 0
            for i, (_, origin) in enumerate(self.rows):
                if origin in before:
                    index = i + 1
            self.rows.insert(index, [name, name])
        return self._apply(change)

    def move(self, index, delta):
        target = index + delta
        if not 0 <= target < len(self.rows):
            return None

        def change():
            self.rows[index], self.rows[target] = self.rows[target], self.rows[index]
        return self._apply(change)

    def reset(self):
        self.rows = [[name, name] for name in self.baseline]


def write_changes(path, model):
    """Record 'model' in its sidecar, or remove the sidecar when nothing changed."""
    if not model.is_changed():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(model.to_changes(), f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
