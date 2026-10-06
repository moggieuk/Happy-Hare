# The shared environment sensor definition, components/Kconfig.environment_sensor.
#
# One fragment serves the shared sensor and every per-gate sensor, so both
# layouts ask the same questions in the same order. The i2c bus inside it is
# components/Kconfig.i2c_bus, pinned by test_i2c_bus_component.py.

import re
import unittest

import kconfiglib

from test.hh import cfg, profiles

SHARED = {'MMU_HAS_ENVIRONMENT_SENSOR': True}

HARDWARE = ['PARAM_ENVIRONMENT_SENSOR', 'CHOICE_ENVIRONMENT_SENSOR_TYPE',
            'PARAM_ENVIRONMENT_SENSOR_REPORT_TIME', 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_TYPE',
            'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS', 'PARAM_ENVIRONMENT_SENSOR_I2C_BUS',
            'PARAM_ENVIRONMENT_SENSOR_I2C_ADDRESS']
SOFTWARE = ['PARAM_ENVIRONMENT_SENSOR', 'CHOICE_ENVIRONMENT_SENSOR_TYPE',
            'PARAM_ENVIRONMENT_SENSOR_REPORT_TIME', 'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_TYPE',
            'PIN_ENVIRONMENT_SENSOR_I2C_SCL', 'PIN_ENVIRONMENT_SENSOR_I2C_SDA',
            'PARAM_ENVIRONMENT_SENSOR_I2C_ADDRESS']
# With a custom bus name, so the hardware case shows the same prompts on every board
LAYOUT = {'HARDWARE': {'CHOICE_ENVIRONMENT_SENSOR_I2C_HARDWARE': True,
                       'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_OTHER': True},
          'SOFTWARE': {'CHOICE_ENVIRONMENT_SENSOR_I2C_SOFTWARE': True}}


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


class TestEnvironmentSensorPrompts(unittest.TestCase):

    def test_shared_and_per_gate_ask_in_the_same_order(self):
        for bus, expected in (('HARDWARE', HARDWARE), ('SOFTWARE', SOFTWARE)):
            with self.subTest(bus=bus):
                shared = _kconfig('env_order_shared_' + bus, 'boxturtle', dict(SHARED, **LAYOUT[bus]))
                per_gate = _kconfig('env_order_gate_' + bus, 'emu',
                                    {name + '_1': value for name, value in LAYOUT[bus].items()})
                self.assertEqual(_prompts(_shared_menu(shared)), expected)
                self.assertEqual(_prompts(_gate_toggle(per_gate, 1), gate=1), expected)


if __name__ == '__main__':
    unittest.main()
