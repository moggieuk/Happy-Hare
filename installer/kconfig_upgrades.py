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


def upgrade(path):
    """Unquote legacy numbers, preserving precision, comments and default markers.

    Invalid strings remain for the normal loader to diagnose. Already upgraded
    files are not written or backed up again (including their timestamps).
    """
    path = Path(path)
    if not path.exists():
        return 0
    before = path.read_bytes()
    # Normal configurations need no decoding, regex scan, or migration imports.
    if not any(prefix in before for prefix in _QUOTED_PREFIXES):
        return 0
    text = before.decode("utf-8")
    count = 0
    empty = 0

    def unquote(match):
        nonlocal count, empty
        value = match[3].strip()
        if not value:
            count += 1
            empty += 1
            return "# %s: empty legacy value; using default" % match[2]
        try:
            if not math.isfinite(float(value)):
                return match[0]
        except ValueError:
            return match[0]
        count += 1
        return match[1] + value + match[4]

    updated = _ASSIGNMENT.sub(unquote, text)
    if not count:
        return 0

    # Keep the normal load path light; these imports are only needed on migration.
    import shutil
    import tempfile

    backup = Path(str(path) + ".pre-numeric-upgrade")
    shutil.copy2(path, backup)
    # Replace atomically so interruption cannot leave a partially written config.
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(updated.encode("utf-8"))
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    detail = "; %d empty settings reset to defaults" % empty if empty else ""
    print("Upgraded %d numeric Kconfig settings in '%s'; numeric values preserved%s "
          "(backup: %s)" % (count, path, detail, backup), file=sys.stderr)
    return count


if __name__ == "__main__":
    upgrade(sys.argv[1])
