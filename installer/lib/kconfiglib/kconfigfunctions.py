"""Happy Hare Kconfig preprocessor functions."""

import io
import os
import re


HH_DEFAULT_TOKEN = " #~DEFAULT~#"
_STRING_VALUE_RE = re.compile(r'^"((?:[^\\"]|\\.)*)"$')
_UNESCAPE_RE = re.compile(r"\\(.)")
_config_cache = {}


def _config_path(kconf):
    filename = os.environ.get("KCONFIG_CONFIG", ".config")
    if os.path.exists(filename):
        return filename

    srctree_filename = os.path.join(kconf.srctree, filename)
    if os.path.exists(srctree_filename):
        return srctree_filename

    return filename


def _saved_values(kconf):
    filename = _config_path(kconf)
    try:
        stat = os.stat(filename)
    except OSError:
        return {}

    stamp = (getattr(stat, "st_mtime_ns", stat.st_mtime), stat.st_size)
    cache_key = (os.path.realpath(filename), stamp)
    cached = _config_cache.get(cache_key)
    if cached is not None:
        return cached

    prefix = os.environ.get("CONFIG_", "CONFIG_")
    values = {}
    with io.open(filename, "r", encoding=kconf._encoding or "utf-8") as config:
        for line in config:
            line = line.rstrip()
            if not line.startswith(prefix) or "=" not in line:
                continue

            name, value = line.split("=", 1)
            if value.endswith(HH_DEFAULT_TOKEN):
                value = value[:-len(HH_DEFAULT_TOKEN)]

            match = _STRING_VALUE_RE.match(value)
            if match:
                value = _UNESCAPE_RE.sub(r"\1", match.group(1))

            values[name[len(prefix):]] = value

    _config_cache.clear()
    _config_cache[cache_key] = values
    return values


def _escape_kconfig_string(value):
    return value.replace("\\", r"\\").replace('"', r'\"')


def saved_config_value(kconf, _, symbol):
    """Return SYMBOL's last assignment, including #~DEFAULT~# assignments."""
    return _escape_kconfig_string(_saved_values(kconf).get(symbol, ""))


functions = {
    "saved-config-value": (saved_config_value, 1, 1),
}
