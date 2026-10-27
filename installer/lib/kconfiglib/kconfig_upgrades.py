"""One-time saved Kconfig syntax upgrades, before loading the current tree."""
import math
import os
from pathlib import Path
import re
import sys


# These options used to be strings. Do not reinterpret other string settings.
NUMERIC_OPTIONS = {
    "PARAM_HEATER_MAX_TEMP",
    "PARAM_HEATER_DEFAULT_DRY_TEMP",
    "PARAM_HEATER_DEFAULT_DRY_TIME",
    "PARAM_HEATER_DEFAULT_DRY_HUMIDITY",
    "PARAM_HEATER_PAUSE_TEMP",
    "PARAM_HEATER_ROTATE_INTERVAL",
    "PARAM_HEATER_VENT_INTERVAL",
    "PARAM_TD1_CAPTURE_TIMEOUT",
    "PARAM_SLICER_TIP_PARK_POS",
}
_QUOTED_PREFIXES = tuple(("CONFIG_" + name + '="').encode("ascii")
                         for name in NUMERIC_OPTIONS)
_ASSIGNMENT = re.compile(
    r'^(CONFIG_(' + '|'.join(sorted(NUMERIC_OPTIONS)) + r')=)"([^"\\\r\n]*)"'
    r'([ \t]*(?:#[^\r\n]*)?)(?=\r?$)', re.MULTILINE)


def _convert(before):
    # Normal configurations need no decoding, regex scan, or migration imports.
    if not any(prefix in before for prefix in _QUOTED_PREFIXES):
        return before, 0, 0
    text = before.decode("utf-8")
    count = 0
    empty = 0

    def unquote(match):
        nonlocal count, empty
        value = match[3].strip()
        if not value:
            count += 1
            empty += 1
            return ""
        try:
            if not math.isfinite(float(value)):
                return match[0]
        except ValueError:
            return match[0]
        count += 1
        # Only the exact trailing default marker has meaning to the loader.
        # Ordinary inline comments were accepted after strings, but would become
        # part of a float value and cause the customization to be discarded.
        default = " #~DEFAULT~#" if match[4].rstrip().endswith(" #~DEFAULT~#") else ""
        return match[1] + value + default

    updated = _ASSIGNMENT.sub(unquote, text)
    return updated.encode("utf-8"), count, empty


def upgrade(path):
    """Migrate a saved config once, serializing concurrent builders on its backup.

    The backup inode stays stable so waiting builders share the same lock. They
    reread the source under that lock and skip a completed migration. The normal
    no-op path never opens the backup or imports the locking/writing helpers.
    """
    path = Path(path)
    if not path.exists() or not _convert(path.read_bytes())[1]:
        return 0

    import fcntl
    import shutil
    import tempfile

    backup = Path(str(path) + ".pre-numeric-upgrade")
    with backup.open("a+b") as saved:
        fcntl.flock(saved, fcntl.LOCK_EX)
        before = path.read_bytes()
        updated, count, empty = _convert(before)
        if not count:
            return 0
        # Back up exactly the bytes converted, never a later version on disk.
        saved.seek(0)
        saved.truncate()
        saved.write(before)
        saved.flush()
        shutil.copystat(path, backup)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(updated)
            shutil.copymode(path, temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        detail = "; %d empty settings reset to defaults" % empty if empty else ""
        print("Upgraded %d numeric Kconfig settings in '%s'; numeric values preserved%s "
              "(backup: %s)" % (count, path, detail, backup), file=sys.stderr)
        return count


def load_legacy_config(kconf, filename):
    """Load a menu dialog selection without changing its source or printing notices."""
    path = Path(filename)
    if not path.exists():
        path = Path(kconf.srctree) / filename
    updated, count, _ = _convert(path.read_bytes())
    if not count:
        return kconf.load_config(filename)

    import tempfile

    # Kconfiglib accepts filenames, not streams. A temporary converted copy lets
    # users read legacy files from read-only locations and save only by choice.
    with tempfile.NamedTemporaryFile() as converted:
        converted.write(updated)
        converted.flush()
        return kconf.load_config(converted.name)
