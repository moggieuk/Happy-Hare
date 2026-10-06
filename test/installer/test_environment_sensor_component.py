# The shared environment sensor definition, components/Kconfig.environment_sensor.
#
# One fragment serves the shared sensor and every per-gate sensor. The layouts
# differ only in the i2c bus list: the shared sensor's fixed list is added by its
# caller re-opening the bus choice, and only per-gate sensors offer "Custom bus
# name". Pinned here:
#
# - both layouts ask the same questions in the same order
# - each layout's bus choice offers its own buses and no others
# - a user's bus choice survives a save and reload

import os
import re
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles

SHARED = {'MMU_HAS_ENVIRONMENT_SENSOR': True}

HARDWARE = ['PARAM_ENVIRONMENT_SENSOR', 'CHOICE_ENVIRONMENT_SENSOR_TYPE',
            'PARAM_ENVIRONMENT_SENSOR_REPORT_TIME', 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_TYPE',
            'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS', 'PARAM_ENVIRONMENT_SENSOR_I2C_ADDRESS']
SOFTWARE = ['PARAM_ENVIRONMENT_SENSOR', 'CHOICE_ENVIRONMENT_SENSOR_TYPE',
            'PARAM_ENVIRONMENT_SENSOR_REPORT_TIME', 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_TYPE',
            'PARAM_ENVIRONMENT_SENSOR_I2C_ADDRESS', 'PIN_ENVIRONMENT_SENSOR_SCL',
            'PIN_ENVIRONMENT_SENSOR_SDA']


def _kconfig(label, base, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, dict(profiles.get(base).syms, **syms))


def _shown(node):
    return node.prompt and kconfiglib.expr_value(node.prompt[1])


def _prompts(parent, gate=None):
    """Names of the shown prompts under `parent`, in menu order, without the gate suffix."""
    names = []
    node = parent.list
    while node:
        if _shown(node):
            name = node.item.name
            if gate is not None:
                name = re.sub(r'_%d$' % gate, '', name)
            names.append(name)
        if node.list and not isinstance(node.item, kconfiglib.Choice):
            names += _prompts(node, gate)
        node = node.next
    return names


def _shared_menu(kc):
    return next(node for node in kc.node_iter()
                if node.item is kconfiglib.MENU and _shown(node)
                and node.prompt[0] == 'Environment sensor h/w config')


def _gate_toggle(kc, gate):
    return next(node for node in kc.syms['PARAM_ENVIRONMENT_SENSOR_GATE_%d' % gate].nodes
                if node.prompt)


def _buses(kc, suffix=''):
    return [sym.name for sym in kc.named_choices['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS' + suffix].syms
            if sym.visibility]


class TestEnvironmentSensorPrompts(unittest.TestCase):

    def test_shared_and_per_gate_ask_in_the_same_order(self):
        for bus, expected in (('HARDWARE', HARDWARE), ('SOFTWARE', SOFTWARE)):
            with self.subTest(bus=bus):
                choice = 'CHOICE_ENVIRONMENT_SENSOR_I2C_%s' % bus
                shared = _kconfig('env_order_shared_' + bus, 'boxturtle', dict(SHARED, **{choice: True}))
                per_gate = _kconfig('env_order_gate_' + bus, 'emu', {choice + '_1': True})
                self.assertEqual(_prompts(_shared_menu(shared)), expected)
                self.assertEqual(_prompts(_gate_toggle(per_gate, 1), gate=1), expected)

    def test_shared_bus_choice_offers_only_its_fixed_buses(self):
        kc = _kconfig('env_buses_shared', 'boxturtle', SHARED)
        self.assertEqual(_buses(kc), ['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C2',
                                      'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C3'])
        self.assertEqual(kc.named_choices['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS'].selection.name,
                         'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C2')
        self.assertFalse(kc.syms['PARAM_ENVIRONMENT_SENSOR_I2C_BUS'].visibility)

    def test_per_gate_bus_choice_offers_the_boards_buses_and_a_custom_name(self):
        kc = _kconfig('env_buses_gate', 'emu', {'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER_2': True})
        self.assertEqual(_buses(kc, '_2'), ['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_SLB_I2C2_PB10_PB11_2',
                                            'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER_2'])
        self.assertTrue(kc.syms['PARAM_ENVIRONMENT_SENSOR_I2C_BUS_2'].visibility)

    def test_only_per_gate_bus_help_mentions_a_custom_name(self):
        shared = _kconfig('env_help_shared', 'boxturtle', SHARED)
        per_gate = _kconfig('env_help_gate', 'emu', {})
        for kc, suffix, custom in ((shared, '', False), (per_gate, '_1', True)):
            with self.subTest(suffix=suffix):
                choice = kc.named_choices['CHOICE_ENVIRONMENT_SENSOR_I2C_BUS' + suffix]
                help_text = next(node.help for node in choice.nodes if node.prompt)
                self.assertEqual('Custom bus name' in help_text, custom)
                self.assertEqual(help_text, help_text.strip())
                self.assertLessEqual(len(help_text.split('\n')), 7)


class TestEnvironmentSensorValues(unittest.TestCase):

    def test_a_bus_choice_survives_a_save_and_reload(self):
        for base, syms in (('boxturtle', dict(SHARED, CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_I2C3=True)),
                           ('emu', {'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER_1': True,
                                    'PARAM_ENVIRONMENT_SENSOR_I2C_BUS_1': 'i2c1'})):
            with self.subTest(profile=base):
                kc = _kconfig('env_saved_' + base, base, syms)
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, '.mmu_config')
                    kc.write_config(path, header='')
                    with cfg._env(cfg._SINGLE_UNIT_ENV):
                        reloaded = cfg._new_kconfig('env_reloaded_' + base)
                        reloaded.load_config(path, filter_defaults=True)
                for name, value in syms.items():
                    self.assertEqual(reloaded.syms[name].str_value, 'y' if value is True else value, name)


if __name__ == '__main__':
    unittest.main()
