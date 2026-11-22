# Happy Hare MMU Software
#
# TigerTag registry schema: the single description of every TigerTag registry table the tag
# parser uses, and the rules a row must pass to enter one. Everything that touches the
# registry consumes this module, so the three can't drift apart:
#
#   extras/mmu/unit/nfc/tag_parser.py  built-in tables, and the startup load of downloaded copies
#   components/mmu_server.py           Moonraker's download (loads this file by path: it runs
#                                      in Moonraker, outside the Klipper package)
#   utils/update_tigertag_tables.py    regenerates the built-in tables (loads it by path)
#
# Registry: TigerTag-RFID-Guide database (CC0), https://github.com/TigerTag-Project/TigerTag-RFID-Guide
# The product catalogue and container models aren't used; every ID table that is, is here.
#
# Standard library only and no Klipper imports - keep it that way, Moonraker loads it too.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from collections import namedtuple

REGISTRY_URL = "https://raw.githubusercontent.com/TigerTag-Project/TigerTag-RFID-Guide/main/database"
STAMPS_FILE = "last_update"         # Per-table change stamps (ms since epoch), keyed by Table.stamp_key

# Chip layout tag_parser._try_tigertag decodes: the major of the registry's version "version"
# field. A version with another major may lay its bytes out differently, so it is refused
LAYOUT_MAJOR = "1"

# Names reach the gate map and G-code (MMU_GATE_MAP NAME=...): short printable text without
# the characters G-code treats as quotes, escapes or comments
NAME_MAX = 48
NAME_FORBIDDEN = frozenset('"\'\\;#')

# key        - short name
# file       - registry file, without .json; also the cached file name
# field      - the field holding the name
# stamp_key  - its key in last_update.json
# attr       - the tag_parser table it fills
# id_bits    - width of the chip field holding the ID: an ID must fit it
# check      - extra rule beyond id + name: "version", "diameter" or None
Table = namedtuple("Table", "key file field stamp_key attr id_bits check")

TABLES = (
    Table("versions",  "id_version",      "name",  "versions",           "_TIGERTAG_MAGIC",     32, "version"),
    Table("types",     "id_type",         "label", "types",              "_TIGERTAG_TYPES",      8, None),
    Table("diameters", "id_diameter",     "label", "filament_diameters", "_TIGERTAG_DIAMETERS",  8, "diameter"),
    Table("materials", "id_material",     "label", "filament_materials", "_TIGERTAG_MATERIALS", 16, None),
    Table("brands",    "id_brand",        "name",  "brands",             "_TIGERTAG_BRANDS",    16, None),
    Table("aspects",   "id_aspect",       "label", "aspects",            "_TIGERTAG_ASPECTS",    8, None),
    Table("units",     "id_measure_unit", "label", "measure_units",      "_TIGERTAG_UNITS",      8, None),
)
TABLES_BY_KEY = {t.key: t for t in TABLES}

# Material names Happy Hare shipped before the tables were generated, under a different
# spelling from TigerTag's. Kept so the MATERIAL a gate shows (and what Spoolman auto-create
# receives) doesn't change under existing users; TigerTag's label is noted beside each
KEEP_MATERIAL_NAMES = {
    7649: "PETG-HS",    # TigerTag: PETG HS
    10478: "Cast Fil",  # TigerTag: Castable Filament
    11506: "PLA-LW",    # TigerTag: PLA-AERO
    24629: "PLA-HS",    # TigerTag: PLA High Speed
    31011: "ASA-LW",    # TigerTag: ASA-AERO
    48047: "TPU-HS",    # TigerTag: TPU High Speed
    56527: "PEI",       # TigerTag: PEI-1010
    57469: "PETG-HF",   # TigerTag: PETG HF
}


def name_ok(name) -> bool:
    return (isinstance(name, str) and 0 < len(name.strip()) <= NAME_MAX
            and name.isprintable() and not NAME_FORBIDDEN.intersection(name))


def diameter_mm(label):
    """A diameter label ("1.75") as millimetres, or None if it isn't a plausible one"""
    try:
        value = float(label)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 10 else None


def is_placeholder(table, row) -> bool:
    """Rows the registry carries that are never table entries: version 0 is the empty chip"""
    return table.check == "version" and isinstance(row, dict) and row.get("id") == 0


def row_ok(table, row) -> bool:
    """The one rule every row must pass to enter a table, whichever way it arrives"""
    if not isinstance(row, dict):
        return False
    row_id = row.get("id")
    # type() rather than isinstance(): JSON true would otherwise pass as the ID 1
    if type(row_id) is not int or not 0 <= row_id < (1 << table.id_bits):
        return False
    if not name_ok(row.get(table.field)):
        return False
    if table.check == "version":
        version = row.get("version")
        return row_id != 0 and isinstance(version, str) and version.split(".")[0] == LAYOUT_MAJOR
    if table.check == "diameter":
        return diameter_mm(row[table.field]) is not None
    return True


def clean_rows(table, rows):
    """(rows that pass row_ok, how many failed, placeholders not counted), or (None, 0) when
    'rows' isn't a registry table at all"""
    if not isinstance(rows, list):
        return None, 0
    good, rejected = [], 0
    for row in rows:
        if row_ok(table, row):
            good.append(row)
        elif not is_placeholder(table, row):
            rejected += 1
    return good, rejected
