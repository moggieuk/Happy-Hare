# Happy Hare test harness - fan control config rendering.
#
# Pure strings: render the real shipped templates and assert the FAN CONTROL
# block of mmu_macro_vars.cfg is internally consistent with the [fan_generic]
# and [temperature_sensor] sections the hardware template creates. The
# _MMU_FAN_LOOP delayed gcode guards every entry, so a block emitted with a
# missing (or nameless) list does not error: the fan silently never moves
# and nothing in the config or the logs says why.
#
# Requires jinja2 (installer/requirements.txt) - run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_fans
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import unittest

from test.hh import cfg, profiles

HARDWARE = 'config/base/mmu_hardware.cfg'
MACRO_VARS = 'config/base/mmu_macro_vars.cfg'


def _fan_vars(rendered):
    """The _MMU_FAN_VARS macro variables, or None if the block was not emitted."""
    try:
        return dict(cfg.assemble(rendered).items('gcode_macro _MMU_FAN_VARS'))
    except configparser.Error:
        return None


def _literal(value):
    """gcode_macro values are Python string literals in the rendered config."""
    return ast.literal_eval(value)


class TestSingleMcuFans(unittest.TestCase):
    """One fan, driven by the single enclosure environment sensor."""

    def _render(self, **overrides):
        profile = profiles.get('boxturtle').derive(
            'fans_%s' % '_'.join(sorted(overrides)), syms=overrides)
        return cfg.render(profile)

    def test_fitted_fan_is_driven_and_names_exist(self):
        rendered = self._render(
            MMU_HAS_FANS=True,
            MMU_HAS_ENVIRONMENT_SENSOR=True,
            PIN_FAN='PE12')
        cfg.assert_sane(rendered)

        fans = _fan_vars(rendered)
        self.assertIsNotNone(
            fans,
            'fan + environment sensor fitted but _MMU_FAN_VARS was not emitted')
        self.assertEqual(_literal(fans['variable_fans']), '_mmu_fan')
        self.assertEqual(_literal(fans['variable_fan_sensors']), 'unit0_Env')

        # Every named fan and sensor must exist as a section in the hardware config.
        secs = cfg.sections(rendered[HARDWARE])
        self.assertIn('fan_generic _mmu_fan', secs)
        self.assertIn('temperature_sensor unit0_Env', secs)

    def test_custom_fan_names_pass_through(self):
        rendered = self._render(
            MMU_HAS_FANS=True,
            MMU_HAS_ENVIRONMENT_SENSOR=True,
            PIN_FAN='PE12',
            VAR_FAN_FANS='fan_generic fan0, fan_generic fan1')
        fans = _fan_vars(rendered)
        self.assertIsNotNone(fans)
        self.assertEqual(
            _literal(fans['variable_fans']), 'fan_generic fan0, fan_generic fan1')

    def test_fans_enabled_but_no_fan_fitted(self):
        """
        The block must not be emitted at all: _MMU_FAN_VARS would be missing
        its name lists, and the _MMU_FAN_LOOP delayed gcode would then poll
        forever without ever moving a fan. Omitting the block degrades to the
        handled 'configuration is not defined' message instead.
        """
        rendered = self._render(
            MMU_HAS_FANS=True,
            MMU_HAS_ENVIRONMENT_SENSOR=True,
            PIN_FAN='')
        self.assertIsNone(_fan_vars(rendered))
        # the hardware side agrees: no [fan_generic] section either
        self.assertNotIn('fan_generic _mmu_fan', cfg.sections(rendered[HARDWARE]))

    def test_fans_without_environment_sensor(self):
        rendered = self._render(MMU_HAS_FANS=True, PIN_FAN='PE12')
        self.assertIsNone(_fan_vars(rendered))
        self.assertIn('fan_generic _mmu_fan', cfg.sections(rendered[HARDWARE]))


class TestPerGateFans(unittest.TestCase):
    """
    EMU: per-gate MCUs with a fan pin AND an environment sensor name per gate.
    The machine type implies MMU_HAS_FANS and MMU_HAS_ENVIRONMENT_SENSOR, and
    the EBB board defaults give every gate a fan pin and a sensor name, so
    enabling per-gate sensors pairs fan with sensor on every gate out of the
    box; blanking either side on a gate takes that gate out of the control.
    """

    def _render(self, **overrides):
        profile = profiles.get('emu').derive(
            'fans_%s' % '_'.join(sorted(overrides)), syms=overrides)
        return cfg.render(profile)

    def test_fitted_gates_pair_fan_with_their_sensor(self):
        rendered = self._render(
            MMU_HAS_PER_GATE_ENV_SENSORS=True,
            PARAM_ENVIRONMENT_SENSOR_0='unit0_env0')
        cfg.assert_sane(rendered)

        fans = _fan_vars(rendered)
        self.assertIsNotNone(
            fans,
            'per-gate fans + sensors fitted but _MMU_FAN_VARS has no name lists')
        self.assertEqual(
            _literal(fans['variable_fan_sensors']),
            'unit0_env0, unit0_Env1, unit0_Env2, unit0_Env3, unit0_Env4')
        self.assertEqual(
            _literal(fans['variable_fans']),
            '_unit0_fan0, _unit0_fan1, _unit0_fan2, _unit0_fan3, _unit0_fan4')

        secs = cfg.sections(rendered[HARDWARE])
        for i in range(5):
            self.assertIn('fan_generic _unit0_fan%d' % i, secs)
        for name in ('unit0_env0', 'unit0_Env1', 'unit0_Env2',
                     'unit0_Env3', 'unit0_Env4'):
            self.assertIn('temperature_sensor %s' % name, secs)

    def test_sparse_gates_pair_only_fitted_gates(self):
        """
        A gate out of the control when EITHER side is blanked: fan pin or
        sensor name. Here four gates are blanked one way or the other, so
        only gate 4 (both defaulted) is controlled. Lists are built in one
        loop, so position pairing in the macro stays aligned.
        """
        rendered = self._render(
            MMU_HAS_PER_GATE_ENV_SENSORS=True,
            PIN_FAN_0='',                      # fan, but no sensor name
            PARAM_ENVIRONMENT_SENSOR_1='',     # sensor name, but no fan pin
            PIN_FAN_2='',                      # fan, but no sensor name
            PARAM_ENVIRONMENT_SENSOR_3='')     # sensor name, but no fan pin
        fans = _fan_vars(rendered)
        self.assertIsNotNone(fans)
        self.assertEqual(_literal(fans['variable_fan_sensors']), 'unit0_Env4')
        self.assertEqual(_literal(fans['variable_fans']), '_unit0_fan4')

    def test_no_gates_fitted(self):
        rendered = self._render(
            MMU_HAS_PER_GATE_ENV_SENSORS=True,
            **{('PIN_FAN_%d' % i): '' for i in range(5)})
        self.assertIsNone(_fan_vars(rendered))


class TestEnvironmentSensorWarning(unittest.TestCase):
    """
    W14 warns when an environment sensor is enabled but no sensor name is
    given. Same dead-symbol bug class as the fan block: the original condition
    AND-ed in PARAM_ENVIRONMENT_SENSORS, a symbol that does not exist, which
    made the warning unreachable.
    """

    def _w14(self, **overrides):
        syms = dict(profiles.get('boxturtle').syms)
        syms['MMU_HAS_ENVIRONMENT_SENSOR'] = True
        syms.update(overrides)
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('fans_w14_%s' % '_'.join(sorted(overrides)), syms)
        return kc.is_enabled('W14')

    def test_blank_sensor_name_warns(self):
        self.assertTrue(self._w14(PARAM_ENVIRONMENT_SENSOR=''))

    def test_named_sensor_does_not_warn(self):
        self.assertFalse(self._w14(PARAM_ENVIRONMENT_SENSOR='unit0_Env'))
