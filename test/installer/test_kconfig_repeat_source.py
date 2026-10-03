# `source` inside an @repeat block.
#
# @repeat expands its body into a line queue that is read ahead of the current
# file. Each sourced file starts with an empty queue and the caller's queue
# resumes when it ends, so a `source` in the body parses the file once per
# iteration and the rest of the body follows it. $(i) is substituted only in
# the body, so the iteration reaches the sourced file through a variable
# assigned on the line before, e.g. `suffix := _$(i)`.

import os
import tempfile
import unittest

import kconfiglib

from test.hh import cfg


class TestSourceInsideRepeat(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, name, text):
        path = os.path.join(self.tmpdir.name, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def _parse(self, top):
        path = self._write('Kconfig', top)
        # srctree must point at the temp dir or `source` resolves into installer/
        env = {'srctree': self.tmpdir.name,
               'KCONFIG_CONFIG': os.path.join(self.tmpdir.name, '.config')}
        with cfg._env(env):
            return kconfiglib.Kconfig(path, warn=False)

    def test_each_iteration_sources_the_fragment_with_its_own_suffix(self):
        self._write('Kconfig.fragment', 'config SLOT$(sfx)\n  bool\n')
        kc = self._parse('mainmenu "t"\n'
                         'sfx :=\n'
                         'source "Kconfig.fragment"\n'
                         '@repeat var=i min=0 max=2@\n'
                         'sfx := _$(i)\n'
                         'source "Kconfig.fragment"\n'
                         'config AFTER_$(i)\n  bool\n'
                         '@endrepeat@\n'
                         'config LAST\n  bool\n')

        def files(name):
            return [os.path.basename(n.filename) for n in kc.syms[name].nodes]

        for name in ('SLOT', 'SLOT_0', 'SLOT_1', 'SLOT_2'):
            with self.subTest(name=name):
                self.assertEqual(files(name), ['Kconfig.fragment'])
        for name in ('AFTER_0', 'AFTER_1', 'AFTER_2', 'LAST'):
            with self.subTest(name=name):
                self.assertEqual(files(name), ['Kconfig'])

        order = [n.item.name for n in kc.node_iter()]
        self.assertEqual(order, ['SLOT', 'SLOT_0', 'AFTER_0', 'SLOT_1', 'AFTER_1',
                                 'SLOT_2', 'AFTER_2', 'LAST'])

    def test_a_sourced_fragment_can_use_its_own_line_macros(self):
        self._write('Kconfig.fragment',
                    '@repeat var=j min=1 max=2@\n'
                    'config SLOT$(sfx)_SUB$(j)\n  bool\n'
                    '@endrepeat@\n'
                    '@if HH_TEST_REPEAT_SOURCE@\n'
                    'config SLOT$(sfx)_FLAG\n  bool\n'
                    '@endif@\n')
        with cfg._env({'HH_TEST_REPEAT_SOURCE': 'y'}):
            kc = self._parse('mainmenu "t"\n'
                             '@repeat var=i min=0 max=1@\n'
                             'sfx := _$(i)\n'
                             'source "Kconfig.fragment"\n'
                             'config AFTER_$(i)\n  bool\n'
                             '@endrepeat@\n')

        order = [n.item.name for n in kc.node_iter()]
        self.assertEqual(order, ['SLOT_0_SUB1', 'SLOT_0_SUB2', 'SLOT_0_FLAG', 'AFTER_0',
                                 'SLOT_1_SUB1', 'SLOT_1_SUB2', 'SLOT_1_FLAG', 'AFTER_1'])


if __name__ == '__main__':
    unittest.main()
