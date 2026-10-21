# toolhead_unload_test is a [mmu_unit_parameters] option prompted beside
# toolhead_entry_tension_test in the per-unit "MMU/Extruder sync" menu: on a single unit, on
# each unit of a multi-unit setup, and never at the multi-unit entry point.
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

    def test_single_unit_shows_it_beside_the_entry_tension_test(self):
        kc, files = _prompt_files(cfg._SINGLE_UNIT_ENV, 'tradrack', 'unload_test_single')
        self.assertEqual(files, ['Kconfig.motor_sync'])
        self.assertEqual(kc.syms[SYM].str_value, '0')

    def test_each_unit_of_a_multi_unit_setup_shows_it(self):
        self.assertEqual(_prompt_files(UNIT_ENV, 'tradrack', 'unload_test_unit')[1],
                         ['Kconfig.motor_sync'])

    def test_multi_unit_entry_point_does_not(self):
        self.assertEqual(_prompt_files(ENTRY_POINT_ENV, 'tradrack', 'unload_test_entry')[1], [])


class TestUnloadTestRender(unittest.TestCase):

    def test_a_unit_value_reaches_that_units_parameters(self):
        profile = copy.deepcopy(profiles.get('ercf_vvd'))
        profile.name = 'ercf_vvd_unload_test'
        profile.units[1].syms[SYM] = 2
        out = cfg.render_via_files(profile)
        levels = {}
        for path, text in out.items():
            for line in text.splitlines():
                if line.startswith('toolhead_unload_test'):
                    levels[path] = line.split(':', 1)[1].split('#')[0].strip()
        self.assertEqual(levels, {'config/base/mmu_parameters_unit0.cfg': '0',
                                  'config/base/mmu_parameters_unit1.cfg': '2'})


if __name__ == '__main__':
    unittest.main()
