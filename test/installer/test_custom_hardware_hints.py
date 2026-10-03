# A feature whose hardware a board supplies itself (a CUSTOM_* flag) hides its
# h/w menu; menuconfig shows a hint in its place so the fixed prompt isn't left
# with nothing behind it.

import unittest

from test.hh import cfg, profiles

HINT = "Defined by the board's misc hardware (mmu_hardware.cfg)"


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
        }
        for name, (syms, files) in cases.items():
            with self.subTest(machine=name):
                hints = _visible_hints(syms)
                self.assertEqual(sorted(h.split(':')[0] for h in hints), files, hints)


if __name__ == '__main__':
    unittest.main()
