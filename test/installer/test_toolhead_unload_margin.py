# toolhead_unload_safety_margin is a [mmu_unit_parameters] option, rendered from each
# unit's own parse. A multi-unit setup used to offer it only at the entry point, whose
# value never reached a unit, so each unit asks for it instead.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import copy
import unittest

import kconfiglib

from test.hh import cfg, profiles

SYM = 'PARAM_TOOLHEAD_UNLOAD_SAFETY_MARGIN'
UNIT_ENV = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', UNIT_NAME='unit1', MCU_NAME='unit1',
                UNIT_INDEX='1')
ENTRY_POINT_ENV = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                       UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')


def _prompt_files(env, profile, label):
    with cfg._env(env):
        kc = cfg._kconfig(label, profiles.get(profile).syms)
    files = [node.filename for node in kc.syms[SYM].nodes
             if node.prompt and kconfiglib.expr_value(node.prompt[1])]
    return kc, files


class TestUnloadSafetyMarginPrompt(unittest.TestCase):

    def test_single_unit_shows_it_with_the_toolhead_dimensions(self):
        self.assertEqual(_prompt_files(cfg._SINGLE_UNIT_ENV, 'tradrack', 'margin_single')[1],
                         ['Kconfig.toolhead'])

    def test_each_unit_of_a_multi_unit_setup_shows_it(self):
        self.assertEqual(_prompt_files(UNIT_ENV, 'tradrack', 'margin_unit')[1],
                         ['Kconfig.endstops'])

    def test_multi_unit_entry_point_does_not(self):
        self.assertEqual(_prompt_files(ENTRY_POINT_ENV, 'tradrack', 'margin_entry')[1], [])

    def test_a_unit_keeps_its_machine_type_default(self):
        kc, _ = _prompt_files(UNIT_ENV, 'emu', 'margin_emu_unit')
        self.assertEqual(kc.syms[SYM].str_value, '40')


class TestUnloadSafetyMarginRender(unittest.TestCase):

    def test_a_unit_value_reaches_that_units_parameters(self):
        profile = copy.deepcopy(profiles.get('ercf_vvd'))
        profile.name = 'ercf_vvd_unload_margin'
        profile.units[1].syms[SYM] = 25
        out = cfg.render_via_files(profile)
        margins = {}
        for path, text in out.items():
            for line in text.splitlines():
                if line.startswith('toolhead_unload_safety_margin'):
                    margins[path] = line.split(':', 1)[1].split('#')[0].strip()
        self.assertEqual(margins, {'config/base/mmu_parameters_unit0.cfg': '10',
                                   'config/base/mmu_parameters_unit1.cfg': '25'})


if __name__ == '__main__':
    unittest.main()
