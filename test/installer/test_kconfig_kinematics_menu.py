# The per-unit "MMU Kinematics" and "Other Settings" menus.
#
# Both are per-unit, so a single unit and every unit of a multi-unit setup show
# them, with Speeds nested under MMU Kinematics. A multi-unit entry point shows
# neither, and keeps its own top-level Speeds for the extruder speeds.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest
from unittest.mock import patch

import kconfiglib
import menuconfig

from test.hh import cfg, profiles

ENVS = {
    'single': cfg._SINGLE_UNIT_ENV,
    'multi-unit unit': dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', UNIT_NAME='u1',
                            MCU_NAME='u1', UNIT_INDEX='1'),
}
ENTRY_POINT_ENV = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                       UNIT_NAME='u0,u1', MCU_NAME='u0,u1')

KINEMATICS = ['PARAM_NUM_GATES', 'Gear stepper', 'Selector stepper', 'Selector servo', 'Speeds']
OTHER = ['PARAM_EXTRUDER_NAME', 'Filament sensors', 'MMU/Extruder sync', 'Calibration/Autotuning']


def _top_menus(env, label):
    with cfg._env(env):
        kc = cfg._kconfig(label, profiles.get('tradrack').syms)
    with patch.multiple(menuconfig, create=True, _kconf=kc, _show_name=False, _show_all=False):
        menus = {}
        for node in menuconfig._shown_nodes(kc.top_node):
            if node.item is kconfiglib.MENU:
                menus[node.prompt[0]] = [
                    child.prompt[0].strip() if child.item is kconfiglib.MENU else child.item.name
                    for child in menuconfig._shown_nodes(node)
                    if child.parent is node and child.item is not kconfiglib.COMMENT]
        return menus


class TestKinematicsMenu(unittest.TestCase):

    def test_each_unit_splits_kinematics_from_other_settings(self):
        for label, env in sorted(ENVS.items()):
            with self.subTest(env=label):
                menus = _top_menus(env, 'kinematics_' + label)
                self.assertEqual(menus['MMU Kinematics'], KINEMATICS)
                self.assertEqual(menus['Other Settings'], OTHER)
                self.assertNotIn('Speeds', menus)

    def test_multi_unit_entry_point_keeps_a_top_level_speeds(self):
        menus = _top_menus(ENTRY_POINT_ENV, 'kinematics_entry')
        self.assertNotIn('MMU Kinematics', menus)
        self.assertNotIn('Other Settings', menus)
        self.assertIn('PARAM_EXTRUDER_LOAD_SPEED', menus['Speeds'])
        self.assertNotIn('PARAM_GEAR_LOAD_SPEED', menus['Speeds'])


if __name__ == '__main__':
    unittest.main()
