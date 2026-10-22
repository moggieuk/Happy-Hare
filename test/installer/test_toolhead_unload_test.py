# toolhead_unload_test is a printer-level [mmu_parameters] option prompted on the shared
# "Tip Forming / Cutting" page: on a single unit and at the multi-unit entry point, but never in
# a unit's own parse, and it renders once into mmu.cfg rather than into each unit's file.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import copy
import unittest

from test.hh import cfg, profiles
from test.installer.test_toolhead_unload_margin import ENTRY_POINT_ENV, UNIT_ENV

import kconfiglib

SYM = 'PARAM_TOOLHEAD_UNLOAD_TEST'


def _prompt_files(env, profile, label):
    with cfg._env(env):
        kc = cfg._kconfig(label, profiles.get(profile).syms)
    files = [node.filename for node in kc.syms[SYM].nodes
             if node.prompt and kconfiglib.expr_value(node.prompt[1])]
    return kc, files


class TestUnloadTestPrompt(unittest.TestCase):

    def test_single_unit_shows_it_on_the_tip_forming_page(self):
        kc, files = _prompt_files(cfg._SINGLE_UNIT_ENV, 'tradrack', 'unload_test_single')
        self.assertEqual(files, ['Kconfig.tip_shaping'])
        self.assertEqual(kc.syms[SYM].str_value, '0')

    def test_multi_unit_entry_point_shows_it(self):
        self.assertEqual(_prompt_files(ENTRY_POINT_ENV, 'tradrack', 'unload_test_entry')[1],
                         ['Kconfig.tip_shaping'])

    def test_each_unit_of_a_multi_unit_setup_does_not(self):
        self.assertEqual(_prompt_files(UNIT_ENV, 'tradrack', 'unload_test_unit')[1], [])


class TestUnloadTestRender(unittest.TestCase):

    def test_the_value_renders_once_for_the_printer(self):
        profile = copy.deepcopy(profiles.get('ercf_vvd'))
        profile.name = 'ercf_vvd_unload_test'
        profile.syms[SYM] = 2
        out = cfg.render_via_files(profile)
        levels = {}
        for path, text in out.items():
            for line in text.splitlines():
                if line.startswith('toolhead_unload_test'):
                    levels[path] = line.split(':', 1)[1].split('#')[0].strip()
        self.assertEqual(levels, {'config/base/mmu.cfg': '2'})


if __name__ == '__main__':
    unittest.main()
