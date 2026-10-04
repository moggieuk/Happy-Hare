# Merge-mode upgrades let Kconfig win for macro variables by mapping each VAR_<prefix>_<name>
# symbol to "variable_<name>" in a [gcode_macro ..._VARS] section (installer/build.py
# VAR_SECTION_MAP). A VAR_ symbol whose prefix is missing from the map, or maps to the wrong
# section, silently keeps the old value on upgrade.

import glob, os, re, unittest

from test.hh import cfg

REPO = cfg.REPO_ROOT
SYMBOL = re.compile(r'^\s*config (VAR_[A-Z0-9_]+)\s*$', re.M)
SECTION = re.compile(r'^\[gcode_macro ([^\]]+)\]')


def _template_locations():
    """VAR_ symbol -> (gcode_macro section, option) where mmu_macro_vars.cfg renders it."""
    found, section = {}, None
    with open(os.path.join(REPO, 'config', 'base', 'mmu_macro_vars.cfg')) as f:
        for line in f:
            match = SECTION.match(line)
            if match:
                section = 'gcode_macro ' + match.group(1)
            for symbol in re.findall(r'\[\[(VAR_[A-Z0-9_]+)\]\]', line):
                found[symbol] = (section, line.split(':', 1)[0].strip())
    return found


# Known bugs, not fixed here: "var_form_tip_" maps to _MMU_CUT_TIP_VARS instead of
# _MMU_FORM_TIP_VARS, and two symbols are named differently from their variable
# (variable_blobifier_type, variable_cut_iterations). Merge-mode upgrades keep the old value
# for these instead of the Kconfig one.
KNOWN_MISMAPPED = {'VAR_BLOBIFIER_TYPE', 'VAR_CUT_TIP_ITERATIONS'} | {
    'VAR_FORM_TIP_' + name for name in (
        'COOLING_MOVES', 'COOLING_TUBE_LENGTH', 'COOLING_TUBE_POSITION', 'COOLING_ZONE_PAUSE',
        'DIP_EXTRACTION_SPEED', 'DIP_INSERTION_SPEED', 'EXTRUDER_EJECT_SPEED',
        'FINAL_COOLING_SPEED', 'INITIAL_COOLING_SPEED', 'MELT_ZONE_PAUSE', 'PARKING_DISTANCE',
        'RAMMING_VOLUME', 'RAMMING_VOLUME_STANDALONE', 'SKINNYDIP_DISTANCE',
        'TOOLCHANGE_FAN_ASSIST', 'TOOLCHANGE_FAN_SPEED', 'TOOLCHANGE_TEMP', 'UNLOADING_SPEED',
        'UNLOADING_SPEED_START', 'USE_FAST_SKINNYDIP', 'USE_SKINNYDIP')}


def _mappings():
    """[(symbol, mapped (section, option), rendered (section, option))] for rendered VAR_ symbols."""
    cfg._prepare_imports()
    from installer.build import VAR_SECTION_MAP
    symbols = set()
    for path in glob.glob(os.path.join(REPO, 'installer', 'macro_vars', 'Kconfig*')):
        with open(path) as f:
            symbols.update(SYMBOL.findall(f.read()))
    locations = _template_locations()
    result = []
    for symbol in sorted(symbols & set(locations)):
        key = symbol.lower()
        mapped = next(((section, 'variable_' + key[len(prefix):])
                       for prefix, section in VAR_SECTION_MAP.items() if key.startswith(prefix)),
                      None)
        result.append((symbol, mapped, locations[symbol]))
    return result


class TestMacroVarsSectionMap(unittest.TestCase):

    def test_every_macro_var_maps_to_where_it_renders(self):
        mappings = [m for m in _mappings() if m[0] not in KNOWN_MISMAPPED]
        self.assertTrue(any(symbol.startswith('VAR_VENT_') for symbol, _, _ in mappings))
        for symbol, mapped, rendered in mappings:
            with self.subTest(symbol=symbol):
                self.assertEqual(mapped, rendered)

    @unittest.expectedFailure
    def test_known_mismapped_vars(self):
        mappings = [m for m in _mappings() if m[0] in KNOWN_MISMAPPED]
        self.assertEqual(len(mappings), len(KNOWN_MISMAPPED))
        self.assertEqual([m for m in mappings if m[1] != m[2]], [])


if __name__ == '__main__':
    unittest.main()
