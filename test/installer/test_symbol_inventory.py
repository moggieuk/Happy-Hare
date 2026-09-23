# A snapshot of what the Kconfig tree declares.
#
# Refactors that move declarations between files - factoring a shared fragment
# out, sourcing it once per consumer - are invisible to value-level tests when
# they go right, and nearly invisible when they go subtly wrong. A symbol that
# gains a third definition site, or loses one, or changes type, still resolves
# to the same value in the configuration you happened to test.
#
# So this records structure, not values: name, type, how many nodes declare it,
# and which files those nodes live in. Node COUNT is the load-bearing column.
# Kconfig.purging is sourced twice (installer/Kconfig:251 and :294), so its
# symbols legitimately have two nodes; a fragment sourced from the wrong place
# turns that into one, three or four, and no name-only list would show it.
#
# Line numbers are deliberately NOT recorded. They churn on every unrelated
# edit, and a golden that churns is a golden people regenerate without reading.
# Filenames change only when a definition actually moves, which is the signal.
#
# Regenerate after an intended change:
#     HH_REGEN_GOLDEN=1 make test UT='test_symbol_inventory.py'
# and read the diff before committing it - that diff IS the review.

import difflib
import os
import unittest

import kconfiglib

from test.hh import cfg

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory')
SYMBOLS_GOLDEN = os.path.join(GOLDEN_DIR, 'symbols.txt')
CHOICES_GOLDEN = os.path.join(GOLDEN_DIR, 'choices.txt')

_TYPE_NAME = {
    kconfiglib.BOOL: 'bool',
    kconfiglib.TRISTATE: 'tristate',
    kconfiglib.STRING: 'string',
    kconfiglib.INT: 'int',
    kconfiglib.HEX: 'hex',
    kconfiglib.FLOAT: 'float',
    kconfiglib.BOOLINT: 'boolint',
    kconfiglib.UNKNOWN: 'unknown',
}

# The three shapes install.sh parses the tree in. `if MULTI_UNIT_ENTRY_POINT`
# attaches a dependency rather than skipping a source, so all three declare the
# same symbols - only which defaults are satisfiable differs. That makes one
# snapshot sufficient, and makes the equality itself worth asserting.
_ENTRY_ENV = {'UNIT_NAME': 'unit0,unit1', 'MCU_NAME': 'unit0,unit1',
              'UNIT_INDEX': '0', 'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': 'y'}
_PER_UNIT_ENV = {'UNIT_NAME': 'unit0', 'MCU_NAME': 'unit0', 'UNIT_INDEX': '0',
                 'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': ''}


def _inventory(kc):
    lines = []
    for sym in sorted(kc.unique_defined_syms, key=lambda s: s.name):
        files = sorted({node.filename for node in sym.nodes})
        lines.append('%s\t%s\t%d\t%s' % (
            sym.name, _TYPE_NAME.get(sym.orig_type, '?'),
            len(sym.nodes), ','.join(files)))
    return lines


def _choice_inventory(kc):
    lines = []
    for choice in sorted(kc.unique_choices, key=lambda c: c.name or ''):
        # A twice-sourced file lists every member twice; the established idiom
        # for this is at test/test_mmu_config.py:879.
        members = list(dict.fromkeys(sym.name for sym in choice.syms))
        lines.append('%s\t%s' % (choice.name or '<unnamed>', ','.join(members)))
    return lines


def _compare(testcase, actual, path, what):
    if os.environ.get('HH_REGEN_GOLDEN'):
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(actual) + '\n')
        testcase.skipTest('regenerated %s' % os.path.basename(path))

    with open(path, encoding='utf-8') as handle:
        expected = handle.read().splitlines()

    if actual == expected:
        return
    diff = '\n'.join(list(difflib.unified_diff(
        expected, actual, 'golden', 'current', lineterm=''))[:60])
    testcase.fail(
        'the %s changed:\n%s\n\nIf this is intended, regenerate with\n'
        "    HH_REGEN_GOLDEN=1 make test UT='test_symbol_inventory.py'\n"
        'and read the diff before committing it.' % (what, diff))


class TestSymbolInventory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kconfig = cfg._new_kconfig('symbol_inventory')

    def test_symbol_inventory_is_unchanged(self):
        _compare(self, _inventory(self.kconfig), SYMBOLS_GOLDEN, 'symbol inventory')

    def test_choice_inventory_is_unchanged(self):
        _compare(self, _choice_inventory(self.kconfig), CHOICES_GOLDEN,
                 'choice inventory')

    def test_component_symbols_follow_the_naming_contract(self):
        """Generated names still have to earn their modifiable-default handling.

        The `#~DEFAULT~#` marker, the `(NOT DEFAULT)` display and the `r`
        reset key are all driven off a symbol's PREFIX. A fragment that
        generated, say, TMC_BLOBIFIER_UART would parse and render fine and
        silently lose all three.
        """
        allowed = ('PARAM_', 'PIN_', 'BOOL_', 'CHOICE_', 'MMU_HAS_',
                   'UNSELECT_', 'VAR_')
        generated = [
            sym.name for sym in self.kconfig.unique_defined_syms
            if any('components/' in node.filename for node in sym.nodes)]
        self.assertTrue(generated, 'no component-generated symbols found at all')
        self.assertEqual(
            [name for name in generated if not name.startswith(allowed)], [])

    def test_the_tmc_component_is_sourced_once_per_consumer(self):
        """Node counts here are a product, and both factors matter.

        The component is split in two - declarations and prompts - and its
        only consumer, Kconfig.purging, is itself sourced twice (root
        Kconfig:251 and :294). So a symbol appears twice per fragment that
        mentions it: 2 for declaration-only, 2 for prompt-only, 4 for both.
        Anything else means a stray or missing source, which is exactly what
        goes wrong when a second stepper starts consuming the same fragment.
        """
        expected = {
            'PARAM_BLOBIFIER_TMC': 2,          # declared, never prompted
            'CHOICE_BLOBIFIER_TMC2209': 2,     # a choice member, menu only
            'BOOL_BLOBIFIER_TMC_SPI': 2,       # derived, never prompted
            'PIN_BLOBIFIER_UART': 4,           # declared and prompted
            'PARAM_BLOBIFIER_RREF': 4,         # declared and prompted
        }
        for name, nodes in sorted(expected.items()):
            with self.subTest(symbol=name):
                sym = self.kconfig.syms[name]
                files = {node.filename for node in sym.nodes}
                self.assertEqual(len(sym.nodes), nodes)
                self.assertTrue(
                    all('components/Kconfig.tmc_driver' in f for f in files),
                    '%s is declared outside the component: %s' % (name, files))

    def test_all_three_parse_shapes_declare_the_same_tree(self):
        baseline = (_inventory(self.kconfig), _choice_inventory(self.kconfig))
        for label, env in (('multi-unit entry point', _ENTRY_ENV),
                           ('multi-unit per unit', _PER_UNIT_ENV)):
            with self.subTest(shape=label):
                with cfg._env(env):
                    other = cfg._new_kconfig('symbol_inventory_' + label)
                self.assertEqual(
                    baseline, (_inventory(other), _choice_inventory(other)),
                    'the %s parse declares a different tree than the single-unit '
                    'parse. A source was moved under an `if` in a way that changes '
                    'structure rather than dependency.' % label)


if __name__ == '__main__':
    unittest.main()
