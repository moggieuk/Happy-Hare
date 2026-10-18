# The per-unit "MMU Kinematics", "Speeds" and "Other Settings" menus, and where the
# gate count is set.
#
# All three are per-unit, so a single unit and every unit of a multi-unit setup show
# them, Speeds straight after MMU Kinematics. A multi-unit entry point shows neither
# MMU Kinematics nor Other Settings, and has an "Extruder speeds" menu instead of Speeds.
# The gate count is under MMU Type -> Design attributes for every machine type.
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

KINEMATICS = ['Gear stepper', 'Selector stepper', 'Selector servo']
OTHER = ['PARAM_EXTRUDER_NAME', 'Filament sensors', 'MMU/Extruder sync', 'Calibration/Autotuning']


def _kconfig(env, label, profile='tradrack'):
    with cfg._env(env):
        return cfg._kconfig(label, profiles.get(profile).syms)


def _top_menus(env, label):
    kc = _kconfig(env, label)
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

    def test_each_unit_has_kinematics_then_speeds_and_other_settings(self):
        for label, env in sorted(ENVS.items()):
            with self.subTest(env=label):
                menus = _top_menus(env, 'kinematics_' + label)
                self.assertEqual(menus['MMU Kinematics'], KINEMATICS)
                self.assertEqual(menus['Other Settings'], OTHER)
                self.assertIn('PARAM_GEAR_LOAD_SPEED', menus['Speeds'])
                names = list(menus)
                self.assertEqual(names.index('MMU Kinematics') + 1, names.index('Speeds'))

    def test_single_unit_speeds_include_the_extruder_speeds(self):
        self.assertIn('PARAM_EXTRUDER_LOAD_SPEED', _top_menus(cfg._SINGLE_UNIT_ENV, 'speeds_single')['Speeds'])

    def test_multi_unit_unit_speeds_leave_out_the_extruder_speeds(self):
        self.assertNotIn('PARAM_EXTRUDER_LOAD_SPEED', _top_menus(ENVS['multi-unit unit'], 'speeds_unit')['Speeds'])

    def test_multi_unit_entry_point_has_only_extruder_speeds(self):
        menus = _top_menus(ENTRY_POINT_ENV, 'kinematics_entry')
        self.assertNotIn('MMU Kinematics', menus)
        self.assertNotIn('Other Settings', menus)
        self.assertNotIn('Speeds', menus)
        self.assertIn('PARAM_EXTRUDER_LOAD_SPEED', menus['Extruder speeds'])
        self.assertNotIn('PARAM_GEAR_LOAD_SPEED', menus['Extruder speeds'])


class TestGateCountPrompt(unittest.TestCase):

    def test_under_design_attributes_for_every_machine_type(self):
        for name, profile in sorted(profiles.PROFILES.items()):
            if getattr(profile, 'units', None) or getattr(profile, 'install_dir', False):
                continue
            with self.subTest(profile=name):
                kc = _kconfig(cfg._SINGLE_UNIT_ENV, 'num_gates_' + name, name)
                menus = [node.parent.prompt[0] for node in kc.syms['PARAM_NUM_GATES'].nodes
                         if node.prompt and kconfiglib.expr_value(node.prompt[1])
                         and node.parent.item is kconfiglib.MENU]
                self.assertEqual(menus, ['Design attributes'])


if __name__ == '__main__':
    unittest.main()
