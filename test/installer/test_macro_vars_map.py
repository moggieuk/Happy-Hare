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


def _mappings():
    """[(symbol, mapped (section, option), rendered (section, option))] for rendered VAR_ symbols."""
    cfg._prepare_imports()
    from installer.build import macro_var_option
    symbols = set()
    for path in glob.glob(os.path.join(REPO, 'installer', 'macro_vars', 'Kconfig*')):
        with open(path) as f:
            symbols.update(SYMBOL.findall(f.read()))
    locations = _template_locations()
    return [(symbol, macro_var_option(symbol.lower()), locations[symbol])
            for symbol in sorted(symbols & set(locations))]


class TestMacroVarsSectionMap(unittest.TestCase):

    def test_every_macro_var_maps_to_where_it_renders(self):
        mappings = _mappings()
        self.assertTrue(any(symbol.startswith('VAR_VENT_') for symbol, _, _ in mappings))
        for symbol, mapped, rendered in mappings:
            with self.subTest(symbol=symbol):
                self.assertEqual(mapped, rendered)


if __name__ == '__main__':
    unittest.main()
