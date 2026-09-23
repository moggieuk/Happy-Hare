# Every symbol a Kconfig expression names must actually be defined.
#
# Kconfig does not complain about a reference to a symbol that is declared
# nowhere. It creates a placeholder with no nodes, which evaluates false, so
# `select X` does nothing, `if X` is dead and `if !X` is always taken. The
# feature the author was gating simply never happens, silently and forever.
#
# That is not hypothetical: five such references had accumulated, including
# one where a warning had stopped being able to fire, and one left behind by
# a symbol rename that a clean textual merge could not have flagged.

import re
import unittest

from test.hh import cfg

# kconfiglib's own placeholder for the modules symbol, which this tree does
# not use. Everything else must resolve.
BUILTIN_PLACEHOLDERS = {'MODULES'}

# A comparison against a literal - `PARAM_NUM_GATES > 4` - registers the
# literal as a constant symbol with no nodes, which is not a dangling name.
_LITERAL = re.compile(r'^-?\d+(\.\d+)?$')

# The three parse shapes install.sh uses. A reference can be live in one and
# absent from another, so all three are checked.
_SHAPES = {
    'single unit': cfg._SINGLE_UNIT_ENV,
    'multi-unit entry point': {
        'UNIT_NAME': 'unit0,unit1', 'MCU_NAME': 'unit0,unit1', 'UNIT_INDEX': '0',
        'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': 'y'},
    'multi-unit per unit': {
        'UNIT_NAME': 'unit0', 'MCU_NAME': 'unit0', 'UNIT_INDEX': '0',
        'F_MULTI_UNIT': 'y', 'F_MULTI_UNIT_ENTRY_POINT': ''},
}


class TestNoDanglingSymbolReferences(unittest.TestCase):

    def test_every_referenced_symbol_is_defined(self):
        for shape, env in sorted(_SHAPES.items()):
            with self.subTest(shape=shape):
                with cfg._env(env):
                    kconfig = cfg._new_kconfig('references_' + shape)
                dangling = sorted(
                    name for name, sym in kconfig.syms.items()
                    if not sym.nodes
                    and name not in BUILTIN_PLACEHOLDERS
                    and not _LITERAL.match(name))
                self.assertEqual(
                    dangling, [],
                    'these names are used in an expression but declared '
                    'nowhere, so they evaluate false and whatever they gate '
                    'never happens: %s' % ', '.join(dangling))


if __name__ == '__main__':
    unittest.main()
