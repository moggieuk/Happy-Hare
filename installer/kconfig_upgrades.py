"""One-time saved Kconfig syntax upgrades, before loading the current tree."""
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


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
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_ASSIGNMENT = re.compile(
    r'^(CONFIG_(' + '|'.join(sorted(NUMERIC_OPTIONS)) + r')=)"('
    + _NUMBER + r')"([ \t]*(?:#[^\r\n]*)?)(?=\r?$)', re.MULTILINE)


def upgrade(path):
    """Unquote legacy numbers, preserving precision, comments and default markers.

    Invalid strings remain for the normal loader to diagnose. Already upgraded
    files are not written or backed up again (including their timestamps).
    """
    path = Path(path)
    if not path.exists():
        return 0
    before = path.read_bytes()
    text = before.decode("utf-8")
    count = 0

    def unquote(match):
        nonlocal count
        if not math.isfinite(float(match[3])):
            return match[0]
        count += 1
        return match[1] + match[3] + match[4]

    updated = _ASSIGNMENT.sub(unquote, text)
    if not count:
        return 0

    backup = Path(str(path) + ".pre-numeric-upgrade")
    if not backup.exists():
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
    print("Upgraded %d numeric Kconfig settings in '%s'; values preserved "
          "(backup: %s)" % (count, path, backup), file=sys.stderr)
    return count


if __name__ == "__main__":
    upgrade(sys.argv[1])
