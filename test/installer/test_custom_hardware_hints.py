# A feature whose hardware a board supplies itself (a CUSTOM_* flag) hides its
# h/w menu; menuconfig shows a hint in its place so the fixed prompt isn't left
# with nothing behind it.

import unittest

from test.hh import cfg, profiles

HINT = "Defined by the mcu board's misc hardware"
X5 = {'BOARD_TYPE_CHAMELEON_X5_1_0': True}


def _visible_hints(syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        kconfig = cfg._kconfig('custom_hints_' + '_'.join(sorted(syms)), syms)
    kconf = kconfig.kconf if hasattr(kconfig, 'kconf') else kconfig
    import kconfiglib
    hints = []
    node = kconf.top_node
    stack = [node.list]
    while stack:
        node = stack.pop()
        while node:
            if (node.item == kconfiglib.COMMENT and node.prompt[0] == HINT
                    and kconfiglib.expr_value(node.prompt[1])):
                hints.append('%s:%d' % (node.filename.rsplit('/', 1)[-1], node.linenr))
            if node.list:
                stack.append(node.list)
            node = node.next
    return sorted(hints)


class TestCustomHardwareHints(unittest.TestCase):

    def test_hint_shows_for_each_custom_feature(self):
        cases = {
            'vvd': ({'MMU_TYPE_VVD_1_0': True, 'BOARD_TYPE_VVD_1_0': True},
                    ['Kconfig.environment_sensor', 'Kconfig.fans', 'Kconfig.heater',
                     'Kconfig.heater', 'Kconfig.leds', 'Kconfig.nfc_reader']),
            'kms': (profiles.get('kms').syms, ['Kconfig.heater', 'Kconfig.heater']),
            'qidi': (profiles.get('qidi').syms, ['Kconfig.nfc_reader']),
            'boxturtle': (profiles.get('boxturtle').syms, []),
            'x5_qb2': (dict(X5, MMU_FAMILY_QUATTRO_BOX=True, MMU_TYPE_QUATTRO_BOX_2_0=True),
                       ['Kconfig.heater']),
            'x5_boxturtle': (dict(X5, MMU_TYPE_BOX_TURTLE_1_0=True), []),
            'x5_boxturtle_heater': (dict(X5, MMU_TYPE_BOX_TURTLE_1_0=True, MMU_HAS_HEATER=True),
                                    ['Kconfig.heater']),
            'x5_per_gate_heater': (dict(X5, MMU_CUSTOM=True, MMU_HAS_HEATER=True,
                                        MMU_HAS_PER_GATE_CONFIG=True), []),
        }
        for name, (syms, files) in cases.items():
            with self.subTest(machine=name):
                hints = _visible_hints(syms)
                self.assertEqual(sorted(h.split(':')[0] for h in hints), files, hints)

    def test_no_hint_is_indented_under_the_prompt_it_follows(self):
        """A comment right after a symbol it depends on, in the same block,
        becomes that symbol's implicit child and menuconfig indents it one
        level deeper than the other hints."""
        import kconfiglib
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconf = cfg._new_kconfig('custom_hint_parents')
        nested = ['%s:%d under %s' % (node.filename.rsplit('/', 1)[-1], node.linenr,
                                      node.parent.item.name)
                  for node in kconf.node_iter()
                  if node.item == kconfiglib.COMMENT and node.prompt[0] == HINT
                  and isinstance(node.parent.item, kconfiglib.Symbol)]
        self.assertEqual(nested, [])


if __name__ == '__main__':
    unittest.main()
