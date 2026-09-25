"""Happy Hare Kconfig preprocessor functions."""

import io
import os
import re


HH_DEFAULT_TOKEN = " #~DEFAULT~#"
_STRING_VALUE_RE = re.compile(r'^"((?:[^\\"]|\\.)*)"$')
_UNESCAPE_RE = re.compile(r"\\(.)")
_config_cache = {}


def env_default(_kconf, _name, variable, default=""):
    """Return an environment variable, falling back only when it is unset."""
    return os.environ.get(variable, default)


def nonempty(_kconf, _name, value):
    return "y" if value else "n"


def env_is_y(_kconf, _name, variable):
    return "y" if os.environ.get(variable) == "y" else "n"


def path_exists(_kconf, _name, path):
    return "y" if os.path.exists(path) else "n"


def path_is_dir(_kconf, _name, path):
    return "y" if os.path.isdir(path) else "n"


def _unquote_kconfig_string(value):
    """Decode a string copied from Kconfig's ``CONFIG_FOO="..."`` format."""
    match = _STRING_VALUE_RE.match(value)
    return _UNESCAPE_RE.sub(r"\1", match.group(1)) if match else value


def _escape_kconfig_string(value):
    return value.replace("\\", r"\\").replace('"', r'\"')


def path_join(_kconf, _name, base, suffix):
    """Join paths after removing quotes inherited from a serialized Kconfig value."""
    return _escape_kconfig_string(os.path.join(
        _unquote_kconfig_string(base), suffix))


def word_count(_kconf, _name, value):
    return str(len(value.split()))


def word_at(_kconf, _name, value, index):
    try:
        position = int(index)
        return value.split()[position - 1] if position > 0 else ""
    except (IndexError, ValueError):
        return ""


def first_nonempty(_kconf, _name, first, second):
    return first or second


def serial_device(_kconf, _name, devices, index, chip=""):
    """Mirror the old grep + sed selection of a discovered Klipper device."""
    matches = [device for device in devices.split() if "Klipper_" + chip in device]
    try:
        return matches[int(index) - 1]
    except (IndexError, ValueError):
        return ""


def _device_choice(device, empty, prefix, suffix=""):
    basename = os.path.basename(device.rstrip("/"))
    if not basename or basename == ".":
        return empty
    return prefix + basename.replace("-", "_").upper() + suffix


def mmu_serial_choice(_kconf, _name, device):
    return _device_choice(device, "CHOICE_MMU_SERIAL_DEVICE_NONE",
                          "CHOICE_MMU_SERIAL_DEVICE_")


def mmu_serial_gate_choice(_kconf, _name, gate, device):
    return _device_choice(device, "CHOICE_MMU_SERIAL_DEVICE_NONE_GATE_" + gate,
                          "CHOICE_MMU_SERIAL_DEVICE_", "_GATE_" + gate)


def buffer_serial_choice(_kconf, _name, device):
    return _device_choice(device, "CHOICE_BUFFER_SERIAL_DEVICE_NONE",
                          "CHOICE_BUFFER_SERIAL_DEVICE_")


def connection_interface(_kconf, _name, connection):
    return connection.partition(":")[0]


def connection_uuid(_kconf, _name, connection):
    before, separator, after = connection.partition(":")
    return after if separator else before


def saved_interface(_kconf, _name, value):
    return value or "can0"


def _canbus_choice(connection, empty, prefix, suffix=""):
    if not connection:
        return empty
    identifier = re.sub(r"[^A-Z0-9_]", "_", connection.replace(":", "_").upper())
    return prefix + identifier + suffix


def mmu_canbus_choice(_kconf, _name, connection):
    return _canbus_choice(connection, "CHOICE_MMU_CANBUS_UUID_NONE",
                          "CHOICE_MMU_CANBUS_UUID_")


def mmu_canbus_gate_choice(_kconf, _name, gate, connection):
    return _canbus_choice(connection, "CHOICE_MMU_CANBUS_UUID_NONE_GATE_" + gate,
                          "CHOICE_MMU_CANBUS_UUID_", "_GATE_" + gate)


def buffer_canbus_choice(_kconf, _name, connection):
    return _canbus_choice(connection, "CHOICE_BUFFER_CANBUS_UUID_NONE",
                          "CHOICE_BUFFER_CANBUS_UUID_")


def pad(_kconf, _name, width, value):
    try:
        return value.ljust(int(width))
    except ValueError:
        return value


def multiline(_kconf, _name, value):
    """Replace physical newlines with the literal ``\n`` form Klipper expects."""
    if not value:
        return ""
    return r"\n".join(value.splitlines()) + r"\n"


def unit_suffix(_kconf, _name, unit):
    return "Unit: [[B]]{}[[/B]]".format(unit) if unit else ""


def menu_title(_kconf, _name, multi_unit, message, suffix):
    if multi_unit == "y":
        return message + " Configuration for [[B]]Multi Unit Setup[[/B]]"
    return message + " Configuration - " + suffix


def menu_caption(_kconf, _name, multi_unit, message, suffix):
    if multi_unit == "y":
        return message + " - [[B]]Shared Config[[/B]]"
    return "Configuration - " + suffix


_LED_THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "..", "config", "led_theme")
_LED_THEME_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LED_THEME_HEADER_RE = re.compile(r"^\[#\s*LED THEME:\s*(.*?)\s*(#\])?\s*$")


def led_themes(_kconf, _name, max_themes):
    """Space separated, sorted theme names found in config/led_theme/."""
    import kconfiglib
    names = sorted(f[:-4] for f in os.listdir(_LED_THEME_DIR) if f.endswith(".cfg"))
    bad = [n for n in names if not _LED_THEME_NAME_RE.match(n)]
    if bad:
        raise kconfiglib.KconfigError(
            "LED theme file names must be lowercase [a-z0-9_]: %s" % ", ".join(bad))
    if len(names) > int(max_themes):
        raise kconfiglib.KconfigError(
            "%d LED themes found but only %s are supported" % (len(names), max_themes))
    return " ".join(names)


def led_theme_choice(_kconf, _name, theme):
    return "CHOICE_LED_THEME_" + theme.upper() if theme else "CHOICE_LED_THEME_NONE"


def _led_theme_header(theme):
    """(label, help) from a theme file's leading Jinja comment:
    '[# LED THEME: <label>' then optional help lines, closed by '#]'."""
    try:
        with open(os.path.join(_LED_THEME_DIR, theme + ".cfg"), encoding="utf-8") as f:
            match = _LED_THEME_HEADER_RE.match(f.readline())
            if not match:
                return theme, ""
            help_lines = []
            if not match.group(2):
                for line in f:
                    if line.strip().endswith("#]"):
                        help_lines.append(line.rstrip()[:-2])
                        break
                    help_lines.append(line.rstrip())
    except (OSError, ValueError):
        return theme, ""
    return match.group(1), "\n".join(line.strip() for line in help_lines).strip()


def led_theme_label(_kconf, _name, theme):
    return _led_theme_header(theme)[0]


def led_theme_help(_kconf, _name, theme):
    return _led_theme_header(theme)[1] or "LED effects from config/led_theme/%s.cfg" % theme


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

            value = _unquote_kconfig_string(value)

            values[name[len(prefix):]] = value

    _config_cache.clear()
    _config_cache[cache_key] = values
    return values


def saved_config_value(kconf, _, symbol):
    """Return SYMBOL's last assignment, including #~DEFAULT~# assignments."""
    return _escape_kconfig_string(_saved_values(kconf).get(symbol, ""))


functions = {
    "buffer-canbus-choice": (buffer_canbus_choice, 1, 1),
    "buffer-serial-choice": (buffer_serial_choice, 1, 1),
    "connection-interface": (connection_interface, 1, 1),
    "connection-uuid": (connection_uuid, 1, 1),
    "env-default": (env_default, 1, 2),
    "env-is-y": (env_is_y, 1, 1),
    "first-nonempty": (first_nonempty, 2, 2),
    "menu-caption": (menu_caption, 3, 3),
    "menu-title": (menu_title, 3, 3),
    "mmu-canbus-choice": (mmu_canbus_choice, 1, 1),
    "mmu-canbus-gate-choice": (mmu_canbus_gate_choice, 2, 2),
    "mmu-serial-choice": (mmu_serial_choice, 1, 1),
    "mmu-serial-gate-choice": (mmu_serial_gate_choice, 2, 2),
    "hh-multiline": (multiline, 1, 1),
    "nonempty": (nonempty, 1, 1),
    "hh-pad": (pad, 2, 2),
    "led-theme-choice": (led_theme_choice, 1, 1),
    "led-theme-help": (led_theme_help, 1, 1),
    "led-theme-label": (led_theme_label, 1, 1),
    "led-themes": (led_themes, 1, 1),
    "path-exists": (path_exists, 1, 1),
    "path-is-dir": (path_is_dir, 1, 1),
    "path-join": (path_join, 2, 2),
    "saved-config-value": (saved_config_value, 1, 1),
    "saved-interface": (saved_interface, 1, 1),
    "serial-device": (serial_device, 2, 3),
    "unit-suffix": (unit_suffix, 1, 1),
    "word-at": (word_at, 2, 2),
    "word-count": (word_count, 1, 1),
}
