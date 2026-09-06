# Happy Hare managed fan tests.

import unittest

from test.hh import cfg, profiles, session


HARDWARE = 'config/base/mmu_hardware.cfg'
PARAMS = 'config/base/mmu_parameters.cfg'
MACRO_VARS = 'config/base/mmu_macro_vars.cfg'


def _single_fan_profile():
    return profiles.get('qidi').derive(
        'qidi_managed_fan',
        syms={
            'MMU_HAS_FANS': True,
            'PIN_FAN': 'unit0:PA8',
        })


class TestMmuFanRender(unittest.TestCase):

    def test_shared_fan_uses_scalar_unit_property(self):
        rendered = cfg.render(_single_fan_profile())
        parser = cfg.assemble(rendered)
        unit = dict(parser.items('mmu_unit unit0'))
        params = dict(parser.items('mmu_unit_parameters unit0'))

        self.assertEqual(unit['fan'], '_unit0_fan')
        self.assertNotIn('fans', unit)
        self.assertIn('fan_generic _unit0_fan', parser.sections())
        self.assertEqual(params['default_fan_on_temp'], '49.0')
        self.assertEqual(params['default_fan_off_temp'], '47.0')
        self.assertEqual(params['fan_polling_time'], '5.0')
        self.assertEqual(params['default_fan_temperature_source'], 'environment')
        self.assertEqual(params['fan_control_enabled'], '1')
        self.assertEqual(params['fan_forced'], '2')

    def test_emu_fans_use_gate_aligned_list(self):
        profile = profiles.get('emu').derive(
            'emu_per_gate_fan_hardware',
            syms={
                'PARAM_FAN_MAX_POWER_1': 0.65,
                'PARAM_FAN_KICK_START_TIME_1': 1.25,
            })
        parser = cfg.assemble(cfg.render(profile))
        unit = dict(parser.items('mmu_unit unit0'))

        self.assertNotIn('fan', unit)
        self.assertEqual(
            [name.strip() for name in unit['fans'].split(',')],
            ['_unit0_fan0', '_unit0_fan1', '_unit0_fan2',
             '_unit0_fan3', '_unit0_fan4'])
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertEqual(params['default_fan_temperature_source'], 'environment')
        self.assertNotIn('fan_temperature_sources', params)
        for gate in range(5):
            self.assertIn('fan_generic _unit0_fan%d' % gate, parser.sections())
        gate_fan = dict(parser.items('fan_generic _unit0_fan1'))
        self.assertEqual(gate_fan['max_power'], '0.65')
        self.assertEqual(gate_fan['kick_start_time'], '1.25')

    def test_shared_fan_can_select_mcu_temperature(self):
        mcu_profile = _single_fan_profile().derive(
            'qidi_mcu_fan_source',
            syms={'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU': True})
        mcu_parser = cfg.assemble(cfg.render(mcu_profile))
        self.assertEqual(
            dict(mcu_parser.items('mmu_unit_parameters unit0'))[
                'default_fan_temperature_source'],
            'mcu')

    def test_managed_fan_is_suppressed_without_a_temperature_source(self):
        profile = _single_fan_profile().derive(
            'qidi_fan_without_temperature_source',
            syms={
                'MMU_HAS_ENVIRONMENT_SENSOR': False,
                'BOOL_CREATE_MCU_ENVIRONMENT_SENSORS': False,
                'MMU_HAS_HEATER': False,
            })
        parser = cfg.assemble(cfg.render(profile))
        unit = dict(parser.items('mmu_unit unit0'))
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertNotIn('fan', unit)
        self.assertNotIn('fans', unit)
        self.assertNotIn('default_fan_temperature_source', params)
        self.assertNotIn('fan_generic _unit0_fan', parser.sections())

    def test_per_gate_fans_use_one_configured_default_source(self):
        profile = profiles.get('emu').derive(
            'emu_mcu_fan_source',
            syms={'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU': True})
        parser = cfg.assemble(cfg.render(profile))
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertEqual(params['default_fan_temperature_source'], 'mcu')
        self.assertNotIn('fan_temperature_sources', params)

    def test_legacy_fan_macro_configuration_is_not_rendered(self):
        rendered = cfg.render(_single_fan_profile())
        self.assertNotIn('gcode_macro _MMU_FAN_VARS',
                         cfg.sections(rendered[MACRO_VARS]))


class TestMmuFanConfiguration(unittest.TestCase):

    @staticmethod
    def _kconfig(name, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig(name, syms)

    def test_managed_and_heater_fans_are_independent_options(self):
        syms = dict(profiles.get('qidi').syms, MMU_HAS_FANS=True)
        kconfig = self._kconfig('qidi_independent_fans', syms)

        managed_prompts = {
            node.prompt[0] for node in kconfig.syms['MMU_HAS_FANS'].nodes
            if node.prompt
        }
        heater_prompts = {
            node.prompt[0] for node in kconfig.syms['MMU_HAS_HEATER_FANS'].nodes
            if node.prompt
        }
        self.assertEqual(managed_prompts, {'Enable managed fan(s)?'})
        self.assertEqual(heater_prompts, {'Configure heater fan(s)?'})
        self.assertTrue(kconfig.is_enabled('MMU_HAS_FANS'))
        self.assertTrue(kconfig.is_enabled('MMU_HAS_HEATER_FANS'))
        self.assertGreater(
            kconfig.named_choices['PARAM_FAN_FORCED_MODE'].visibility, 0)

    def test_managed_fan_is_fixed_off_without_a_temperature_source(self):
        for profile_name in ('qidi', 'emu'):
            syms = dict(
                profiles.get(profile_name).syms,
                MMU_HAS_FANS=True,
                MMU_HAS_ENVIRONMENT_SENSOR=False,
                BOOL_CREATE_MCU_ENVIRONMENT_SENSORS=False)
            kconfig = self._kconfig(
                'managed_fan_without_source_' + profile_name, syms)

            with self.subTest(profile=profile_name):
                self.assertEqual(kconfig.syms['MMU_HAS_FANS'].str_value, 'n')
                self.assertEqual(kconfig.syms['MMU_HAS_FANS'].visibility, 0)
                self.assertEqual(kconfig.syms['PIN_FAN'].visibility, 0)
                self.assertEqual(
                    kconfig.named_choices['PARAM_FAN_FORCED_MODE'].visibility,
                    0)

    def test_fan_control_choices_and_options_have_help_text(self):
        syms = dict(profiles.get('qidi').syms, MMU_HAS_FANS=True)
        kconfig = self._kconfig('fan_control_help', syms)

        choices = (
            'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE',
            'PARAM_FAN_FORCED_MODE',
        )
        options = (
            'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_ENVIRONMENT',
            'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU',
            'PARAM_FAN_FORCED_MODE_AUTO',
            'PARAM_FAN_FORCED_MODE_OFF',
            'PARAM_FAN_FORCED_MODE_ON',
        )
        for name in choices:
            with self.subTest(choice=name):
                self.assertTrue(any(node.help for node in
                                    kconfig.named_choices[name].nodes))
        for name in options:
            with self.subTest(option=name):
                self.assertTrue(any(node.help for node in
                                    kconfig.syms[name].nodes))

    def test_new_configuration_menus_and_choices_have_help_text(self):
        kconfig = self._kconfig(
            'new_configuration_help',
            dict(profiles.get('emu').syms, MMU_HAS_HEATER=True))

        menu_prompts = (
            'Environment sensor h/w config',
            'Fan h/w config',
            'Managed fan defaults',
            'Heater h/w config',
            'Heater and humidity control',
            'Heater fan h/w config',
            'Gate 0 config',
        )
        for prompt in menu_prompts:
            nodes = [
                node for node in kconfig.node_iter()
                if node.prompt and node.prompt[0] == prompt
            ]
            with self.subTest(menu=prompt):
                self.assertTrue(nodes)
                self.assertTrue(all(node.help for node in nodes))

        heater_control = next(
            node for node in kconfig.node_iter()
            if node.prompt and
            node.prompt[0] == 'Heater and humidity control')
        self.assertIn('MMU_HEATER', heater_control.help)

        for choice in ('CHOICE_ENVIRONMENT_SENSOR_TYPE_0',
                       'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_TYPE_0',
                       'CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_0'):
            with self.subTest(choice=choice):
                self.assertTrue(any(
                    node.help for node in kconfig.named_choices[choice].nodes))

    def test_per_gate_config_is_independent_of_per_gate_mcu(self):
        config_only = {
            'MMU_CUSTOM': True,
            'MMU_HAS_PER_GATE_CONFIG': True,
            'MMU_HAS_PER_GATE_MCU': False,
        }
        kconfig = self._kconfig('per_gate_config_only', config_only)
        self.assertTrue(kconfig.is_enabled('MMU_HAS_PER_GATE_CONFIG'))
        self.assertFalse(kconfig.is_enabled('MMU_HAS_PER_GATE_MCU'))

        mcu_only = dict(config_only,
                        MMU_HAS_PER_GATE_CONFIG=False,
                        MMU_HAS_PER_GATE_MCU=True)
        kconfig = self._kconfig('per_gate_mcu_only', mcu_only)
        self.assertFalse(kconfig.is_enabled('MMU_HAS_PER_GATE_CONFIG'))
        self.assertTrue(kconfig.is_enabled('MMU_HAS_PER_GATE_MCU'))

    def test_emu_uses_both_per_gate_flags(self):
        kconfig = self._kconfig('emu_per_gate_topology', profiles.get('emu').syms)
        self.assertTrue(kconfig.is_enabled('MMU_HAS_PER_GATE_CONFIG'))
        self.assertTrue(kconfig.is_enabled('MMU_HAS_PER_GATE_MCU'))
        self.assertGreater(kconfig.syms['PARAM_ENVIRONMENT_SENSOR_GATE_0'].visibility, 0)
        self.assertGreater(kconfig.syms['PARAM_FAN_GATE_0'].visibility, 0)

    def test_per_gate_heater_fans_do_not_use_the_shared_enable(self):
        kconfig = self._kconfig(
            'emu_per_gate_heater_fans',
            dict(profiles.get('emu').syms, MMU_HAS_HEATER=True))

        self.assertEqual(kconfig.syms['MMU_HAS_HEATER_FANS'].str_value, 'n')
        self.assertEqual(kconfig.syms['MMU_HAS_HEATER_FANS'].visibility, 0)
        self.assertGreater(
            kconfig.syms['PARAM_HEATER_FAN_GATE_0'].visibility, 0)

    def test_fan_hardware_parameters_follow_the_selected_layout(self):
        shared = self._kconfig(
            'shared_fan_hardware',
            dict(profiles.get('qidi').syms, MMU_HAS_FANS=True))
        per_gate = self._kconfig(
            'per_gate_fan_hardware',
            dict(profiles.get('emu').syms, MMU_HAS_HEATER=True))

        for scalar in ('PARAM_FAN_MAX_POWER',
                       'PARAM_HEATER_FAN_SPEED'):
            with self.subTest(layout='shared', symbol=scalar):
                self.assertGreater(shared.syms[scalar].visibility, 0)
            with self.subTest(layout='per_gate', symbol=scalar):
                self.assertEqual(per_gate.syms[scalar].visibility, 0)

        for indexed in ('PARAM_FAN_MAX_POWER_0',
                        'PARAM_HEATER_FAN_SPEED_0'):
            with self.subTest(layout='shared', symbol=indexed):
                self.assertEqual(shared.syms[indexed].visibility, 0)
            with self.subTest(layout='per_gate', symbol=indexed):
                self.assertGreater(per_gate.syms[indexed].visibility, 0)

        fan_parent = per_gate.syms['PARAM_FAN_MAX_POWER_0'].nodes[0].parent
        heater_fan_parent = \
            per_gate.syms['PARAM_HEATER_FAN_SPEED_0'].nodes[0].parent
        self.assertEqual(fan_parent.prompt[0], 'Fan h/w config')
        self.assertEqual(heater_fan_parent.prompt[0],
                         'Heater fan h/w config')

        heater_fan_toggle = next(
            node for node in shared.syms['MMU_HAS_HEATER_FANS'].nodes
            if node.filename.endswith('Kconfig.heater'))
        heater_fan_menu = next(
            node for node in shared.node_iter()
            if node.filename.endswith('Kconfig.heater') and
            node.prompt and node.prompt[0] == 'Heater fan h/w config')
        self.assertIs(heater_fan_menu.parent, heater_fan_toggle.parent)

        for toggle in ('PARAM_FAN_GATE_0', 'PARAM_HEATER_FAN_GATE_0'):
            nodes = [
                node for node in per_gate.syms[toggle].nodes
                if node.filename.endswith('Kconfig.per_gate')
            ]
            with self.subTest(toggle=toggle):
                self.assertTrue(nodes)
                self.assertTrue(all(not node.is_menuconfig for node in nodes))

    def test_shared_heater_fan_is_rendered_with_safety_settings(self):
        profile = profiles.get('qidi').derive(
            'qidi_heater_fan',
            syms={
                'PARAM_FILAMENT_HEATER': 'qidi_heater',
                'PIN_HEATER_FAN': 'unit0:PA8',
                'PARAM_HEATER_FAN_SPEED': 0.75,
                'PARAM_HEATER_FAN_SHUTDOWN_SPEED': 0.8,
            })
        parser = cfg.assemble(cfg.render(profile))
        unit = dict(parser.items('mmu_unit unit0'))
        fan = dict(parser.items('heater_fan _unit0_heater_fan'))
        params = dict(parser.items('mmu_unit_parameters unit0'))

        self.assertNotIn('fan', unit)
        self.assertNotIn('fans', unit)
        self.assertNotIn('fan_generic _unit0_fan', parser.sections())
        self.assertEqual(fan['heater'], 'qidi_heater')
        self.assertEqual(fan['fan_speed'], '0.75')
        self.assertEqual(fan['shutdown_speed'], '0.8')
        self.assertNotIn('fan_control_enabled', params)

    def test_per_gate_heater_fans_use_gate_aligned_heaters(self):
        profile = profiles.get('emu').derive(
            'emu_heater_fans', syms=dict(
                {
                    'MMU_HAS_HEATER': True,
                    'PARAM_HEATER_FAN_MAX_POWER_1': 0.7,
                    'PARAM_HEATER_FAN_KICK_START_TIME_1': 1.5,
                    'PARAM_HEATER_FAN_SPEED_1': 0.8,
                    'PARAM_HEATER_FAN_SHUTDOWN_SPEED_1': 0.9,
                },
                **{'PIN_HEATER_FAN_%d' % gate: 'unit0_gate%d:PA14' % gate
                   for gate in range(5)}))
        parser = cfg.assemble(cfg.render(profile))
        unit = dict(parser.items('mmu_unit unit0'))

        self.assertNotIn('fan', unit)
        self.assertIn('fans', unit)
        for gate in range(5):
            section = 'heater_fan _unit0_heater_fan%d' % gate
            self.assertIn(section, parser.sections())
            self.assertEqual(
                dict(parser.items(section))['heater'],
                'unit0_heater%d' % gate)
            self.assertIn('fan_generic _unit0_fan%d' % gate, parser.sections())

        gate_fan = dict(parser.items('heater_fan _unit0_heater_fan1'))
        self.assertEqual(gate_fan['max_power'], '0.7')
        self.assertEqual(gate_fan['kick_start_time'], '1.5')
        self.assertEqual(gate_fan['fan_speed'], '0.8')
        self.assertEqual(gate_fan['shutdown_speed'], '0.9')

    def test_heater_without_fixed_fan_is_warned(self):
        base = dict(profiles.get('qidi').syms)
        self.assertTrue(
            self._kconfig('qidi_without_fan', base).is_enabled('W21'))

        managed = dict(base, MMU_HAS_FANS=True, PIN_FAN='unit0:PA8')
        self.assertTrue(
            self._kconfig('qidi_managed_only', managed).is_enabled('W21'))

        fixed = dict(base, PIN_HEATER_FAN='unit0:PA9')
        self.assertFalse(
            self._kconfig('qidi_fixed_fan', fixed).is_enabled('W21'))


class TestMmuFanPinConfiguration(unittest.TestCase):

    @staticmethod
    def _pins_visibility(kconfig, symbol):
        from kconfiglib import expr_value
        nodes = [
            node for node in kconfig.syms[symbol].nodes
            if node.filename.endswith('Kconfig.pins')
        ]
        return max(
            expr_value(node.prompt[1]) if node.prompt else 0
            for node in nodes
        )

    def test_raw_pin_editors_follow_fan_layout(self):
        cases = (
            ('managed_shared', {
                'MMU_TYPE_HTLF_1_0': True,
                'MMU_HAS_FANS': True,
            }, ('PIN_FAN',), ('PIN_FAN_0', 'PIN_HEATER_FAN')),
            ('per_gate', {
                'MMU_TYPE_EMU_1_0': True,
                'MMU_HAS_HEATER': True,
            }, ('PIN_FAN_0', 'PIN_HEATER_FAN_0'),
               ('PIN_FAN', 'PIN_HEATER_FAN')),
        )

        with cfg._env(cfg._SINGLE_UNIT_ENV):
            for label, syms, visible, hidden in cases:
                kconfig = cfg._kconfig(label, syms)
                for symbol in visible:
                    with self.subTest(case=label, symbol=symbol):
                        self.assertGreater(
                            self._pins_visibility(kconfig, symbol), 0)
                for symbol in hidden:
                    with self.subTest(case=label, symbol=symbol):
                        self.assertEqual(
                            self._pins_visibility(kconfig, symbol), 0)


class TestVividCustomFans(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.profile = profiles.get('ercf_vvd')
        cls.parser = cfg.assemble(cfg.render(cls.profile))
        cls.hh = session(cls.profile)
        cls.hh.boot()

    @classmethod
    def tearDownClass(cls):
        cls.hh.close()

    def test_custom_fans_suppress_managed_fan_configuration(self):
        unit = dict(self.parser.items('mmu_unit unit1'))
        self.assertNotIn('fan', unit)
        self.assertNotIn('fans', unit)
        self.assertEqual(
            [section for section in self.parser.sections()
             if section.startswith('fan_generic ') and 'unit1' in section],
            [])
        self.assertIn('heater_fan unit1_fan', self.parser.sections())
        self.assertIn('controller_fan unit1_mcu_fan', self.parser.sections())

    def test_vivid_forces_fan_capability_but_hides_managed_controls(self):
        syms = next(unit.syms for unit in self.profile.units if unit.name == 'unit1')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('vivid_custom_fans', syms)
        self.assertTrue(kconfig.is_enabled('MMU_HAS_FANS'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_FAN_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_FAN_SETUP'))
        self.assertFalse(kconfig.is_enabled('W21'))
        self.assertEqual(kconfig.syms['PIN_FAN'].visibility, 0)
        self.assertEqual(kconfig.syms['PIN_FAN_0'].visibility, 0)
        self.assertEqual(kconfig.named_choices['PARAM_FAN_FORCED_MODE'].visibility, 0)

    def test_mmu_fan_reports_no_manageable_fans(self):
        unit = next(unit for unit in self.hh.mmu.mmu_machine.units
                    if unit.name == 'unit1')
        self.assertEqual(unit.fan_manager.fans, [])
        with self.assertRaisesRegex(Exception, '^No manageable fans on this unit$'):
            self.hh.run_gcode('MMU_FAN UNIT=unit1')

    def test_kms_and_vivid_split_both_custom_fan_families(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            for profile_name in ('kms', 'vvd'):
                syms = (profiles.get('kms').syms if profile_name == 'kms'
                        else next(unit.syms for unit in profiles.get('ercf_vvd').units
                                  if unit.name == 'unit1'))
                kconfig = cfg._kconfig('%s_custom_fans' % profile_name, syms)
                with self.subTest(profile=profile_name):
                    self.assertTrue(kconfig.is_enabled('CUSTOM_FAN_SETUP'))
                    self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_FAN_SETUP'))


class TestMmuFanRuntime(unittest.TestCase):

    def setUp(self):
        self.hh = session(_single_fan_profile())
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.manager = self.unit.fan_manager
        self.fan = self.hh.printer.lookup_object(self.unit.fan)
        self.sensor = self.hh.printer.lookup_object(self.unit.environment_sensor)

    def _speed(self, fan=None):
        return (fan or self.fan).get_status(self.hh.reactor.monotonic())['speed']

    def test_automatic_hysteresis(self):
        self.assertEqual(self._speed(), 0.)

        self.sensor.feed(50.)
        self.hh.reactor.advance(self.unit.p.fan_polling_time)
        self.assertEqual(self._speed(), 1.)

        self.sensor.feed(48.)
        self.hh.reactor.advance(self.unit.p.fan_polling_time)
        self.assertEqual(self._speed(), 1.)

        self.sensor.feed(47.)
        self.hh.reactor.advance(self.unit.p.fan_polling_time)
        self.assertEqual(self._speed(), 0.)

    def test_command_force_enable_and_status(self):
        self.hh.run_gcode('MMU_FAN FAN_FORCED=1')
        self.assertEqual(self._speed(), 1.)

        self.hh.run_gcode('MMU_FAN ENABLE=0')
        self.assertEqual(self._speed(), 0.)
        self.assertFalse(self.manager.is_enabled())

        # Forced control remains useful when automatic monitoring is disabled.
        self.hh.run_gcode('MMU_FAN FAN_FORCED=1')
        self.assertEqual(self._speed(), 1.)

        self.hh.run_gcode('MMU_FAN ENABLE=1 FAN_FORCED=2')
        self.assertTrue(self.manager.is_enabled())
        self.hh.reactor.advance(0.)

        at = len(self.hh.console)
        self.hh.run_gcode('MMU_FAN')
        status = '\n'.join(self.hh.console[at:])
        self.assertIn('MMU fan control for unit0: ENABLED', status)
        self.assertIn('Fan (_unit0_fan): AUTO', status)
        self.assertEqual(self.hh.errors, [])

    def test_command_adjusts_and_reports_auto_temperature_range(self):
        self.hh.run_gcode('MMU_FAN ON_TEMP=60 OFF_TEMP=58')
        snapshot = self.manager.get_snapshot()
        self.assertEqual(snapshot[0]['on_temp'], 60.)
        self.assertEqual(snapshot[0]['off_temp'], 58.)
        self.assertEqual(self.unit.p.default_fan_on_temp, 49.)
        self.assertEqual(self.unit.p.default_fan_off_temp, 47.)

        at = len(self.hh.console)
        self.hh.run_gcode('MMU_FAN')
        status = '\n'.join(self.hh.console[at:])
        self.assertIn(
            'AUTO range in force: OFF <= 58.0°C, ON >= 60.0°C; polling 5.0s',
            status)
        self.assertEqual(self.hh.errors, [])

    def test_command_rejects_an_inverted_temperature_range_atomically(self):
        with self.assertRaisesRegex(
                Exception, 'ON_TEMP must be greater than or equal to OFF_TEMP'):
            self.hh.run_gcode('MMU_FAN ON_TEMP=45 OFF_TEMP=50')
        snapshot = self.manager.get_snapshot()
        self.assertEqual(snapshot[0]['on_temp'], 49.)
        self.assertEqual(snapshot[0]['off_temp'], 47.)

    def test_command_rejects_heater_as_a_temperature_source(self):
        with self.assertRaisesRegex(
                Exception,
                'SOURCE must be one of: environment, mcu, default'):
            self.hh.run_gcode('MMU_FAN SOURCE=heater')
        self.assertEqual(
            self.manager.get_snapshot()[0]['source'],
            'environment')

    def test_per_gate_source_requires_a_sensor_for_the_selected_gate(self):
        self.hh.close()
        profile = profiles.get('emu').derive(
            'emu_missing_gate_environment_sensor',
            syms={'PARAM_ENVIRONMENT_SENSOR_GATE_1': False})
        self.hh = session(profile)
        self.addCleanup(self.hh.close)
        self.hh.boot()
        manager = self.hh.mmu.mmu_unit(0).fan_manager

        self.hh.run_gcode('MMU_FAN SOURCE=mcu GATE=1')
        with self.assertRaisesRegex(
                Exception,
                "Temperature source 'environment' is not available for gate 1 on unit0"):
            self.hh.run_gcode('MMU_FAN SOURCE=default GATE=1')
        self.assertEqual(
            {item['gate']: item['source'] for item in manager.get_snapshot()}[1],
            'mcu')

    def test_per_gate_command_adjusts_only_selected_auto_range(self):
        self.hh.close()
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        unit = self.hh.mmu.mmu_unit(0)
        manager = unit.fan_manager
        fans = [self.hh.printer.lookup_object(name) for name in unit.fans]
        sensors = [self.hh.printer.lookup_object(name)
                   for name in unit.environment_sensors]
        mcu_sensor = self.hh.printer.lookup_object('temperature_sensor _unit0_mcu1')

        self.hh.run_gcode('MMU_FAN SOURCE=mcu ON_TEMP=60 OFF_TEMP=58 GATE=1')
        snapshots = {item['gate']: item for item in manager.get_snapshot()}
        self.assertEqual(
            [(snapshots[gate]['off_temp'], snapshots[gate]['on_temp'])
             for gate in range(5)],
            [(47., 49.), (58., 60.), (47., 49.), (47., 49.), (47., 49.)])

        sensors[0].feed(50.)
        sensors[1].feed(70.)
        mcu_sensor.feed(50.)
        self.hh.reactor.advance(unit.p.fan_polling_time)
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [1., 0., 0., 0., 0.])

        mcu_sensor.feed(60.)
        self.hh.reactor.advance(unit.p.fan_polling_time)
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [1., 1., 0., 0., 0.])

        at = len(self.hh.console)
        self.hh.run_gcode('MMU_FAN')
        status = '\n'.join(self.hh.console[at:])
        self.assertIn(
            'Gate 1 (_unit0_fan1): AUTO, 100%, source mcu: 60.0°C, '
            'range OFF <= 58.0°C / ON >= 60.0°C', status)

        self.hh.run_gcode('MMU_FAN SOURCE=default GATE=1')
        self.assertEqual(
            {item['gate']: item['source'] for item in manager.get_snapshot()}[1],
            'environment')
        self.assertEqual(self.hh.errors, [])

    def test_per_gate_command_targets_only_selected_fans(self):
        self.hh.close()
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        unit = self.hh.mmu.mmu_unit(0)
        fans = [self.hh.printer.lookup_object(name) for name in unit.fans]
        sensors = [self.hh.printer.lookup_object(name)
                   for name in unit.environment_sensors]

        sensors[1].feed(50.)
        self.hh.reactor.advance(unit.p.fan_polling_time)
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [0., 1., 0., 0., 0.])
        sensors[1].feed(47.)
        self.hh.reactor.advance(unit.p.fan_polling_time)
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [0., 0., 0., 0., 0.])

        self.hh.run_gcode('MMU_FAN FAN_FORCED=1 GATE=2')
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [0., 0., 1., 0., 0.])

        self.hh.run_gcode('MMU_FAN FAN_FORCED=1 GATES=0,4')
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [1., 0., 1., 0., 1.])
        self.assertEqual(self.hh.errors, [])

    def test_sparse_per_gate_targets_are_validated_before_changes(self):
        self.hh.close()
        profile = profiles.get('emu').derive(
            'emu_sparse_managed_fans',
            syms={'PARAM_FAN_GATE_1': False})
        self.hh = session(profile)
        self.addCleanup(self.hh.close)
        self.hh.boot()
        unit = self.hh.mmu.mmu_unit(0)
        manager = unit.fan_manager

        self.assertEqual(unit.fans[1], '')
        with self.assertRaisesRegex(
                Exception,
                'Gate 1 does not have a managed fan on unit0'):
            self.hh.run_gcode('MMU_FAN FAN_FORCED=1 GATE=1')

        # A mixed valid/invalid list is rejected before the valid fan changes.
        with self.assertRaisesRegex(
                Exception,
                'Gate 1 does not have a managed fan on unit0'):
            self.hh.run_gcode('MMU_FAN FAN_FORCED=1 GATES=0,1')
        self.assertEqual(
            {item['gate']: item['mode'] for item in manager.get_snapshot()}[0],
            'AUTO')

        # An unscoped command intentionally applies to every configured
        # managed fan and skips the empty gate-aligned slot.
        self.hh.run_gcode('MMU_FAN FAN_FORCED=1')
        self.assertEqual(
            {item['gate']: item['mode'] for item in manager.get_snapshot()},
            {0: 'ON', 2: 'ON', 3: 'ON', 4: 'ON'})


if __name__ == '__main__':
    unittest.main()
