# Happy Hare managed fan tests.

import unittest

from test.hh import cfg, profiles, session


HARDWARE = 'config/base/mmu_hardware.cfg'
PARAMS = 'config/base/mmu_parameters.cfg'
MACRO_VARS = 'config/base/mmu_macro_vars.cfg'


def _controller_fan_profile(base, **syms):
    return profiles.get(base).derive(
        base + '_controller_fan',
        syms=dict({
            'MMU_HAS_CONTROLLER_FAN': True,
            'PIN_CONTROLLER_FAN': 'unit0:PA3',
        }, **syms))


def _per_gate_controller_fan_profile(base='emu', **syms):
    return profiles.get(base).derive(
        base + '_per_gate_controller_fan',
        syms=dict({
            'MMU_HAS_CONTROLLER_FAN': True,
            'PIN_CONTROLLER_FAN_0': 'unit0_gate0:PB8',
            'PIN_CONTROLLER_FAN_2': 'unit0_gate2:PB8',
            'PIN_CONTROLLER_FAN_4': 'unit0_gate4:PB8',
            'PARAM_CONTROLLER_FAN_GATE_4': False,
        }, **syms))


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
        self.assertEqual(params['default_fan_temperature_source'], 'mcu')
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

    def test_emu_defaults_fan_source_to_mcu_on_both_boards(self):
        # Neither profile sets a temperature source symbol, so this pins the
        # computed Kconfig default for the EMU on its two supported boards.
        for profile_name in ('emu', 'emu_ebb'):
            profile = profiles.get(profile_name)
            with self.subTest(profile=profile_name):
                self.assertNotIn(
                    'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU', profile.syms)
                parser = cfg.assemble(cfg.render(profile))
                params = dict(parser.items('mmu_unit_parameters unit0'))
                self.assertEqual(params['default_fan_temperature_source'], 'mcu')

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
            'Controller fan h/w config',
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

    def test_shared_heater_fan_defaults_heater_temp(self):
        profile = profiles.get('qidi').derive(
            'qidi_heater_fan_temp',
            syms={
                'PARAM_FILAMENT_HEATER': 'qidi_heater',
                'PIN_HEATER_FAN': 'unit0:PA8',
            })
        self.assertNotIn('PARAM_HEATER_FAN_HEATER_TEMP', profile.syms)
        parser = cfg.assemble(cfg.render(profile))
        fan = dict(parser.items('heater_fan _unit0_heater_fan'))
        self.assertEqual(fan['heater_temp'], '45.0')
        self.assertEqual(fan['pin'], 'unit0:PA8')
        self.assertFalse([s for s in parser.sections() if s.startswith('multi_pin ')])

    def test_shared_heater_fan_accepts_a_list_of_pins(self):
        profile = profiles.get('qidi').derive(
            'qidi_heater_fan_pins',
            syms={
                'PARAM_FILAMENT_HEATER': 'qidi_heater',
                'PIN_HEATER_FAN': 'unit0:PA4,  !unit0:PA5',
                'PARAM_HEATER_FAN_HEATER_TEMP': 40.0,
            })
        parser = cfg.assemble(cfg.render(profile))
        # Generated sections only: the QIDI board's misc hardware adds its own heater fans
        sections = [s for s in parser.sections() if s.startswith(('heater_fan _', 'multi_pin _'))]
        # Klipper resolves multi_pin: when the fan loads, so the alias must come first
        self.assertEqual(sections, ['multi_pin _unit0_heater_fan_pins', 'heater_fan _unit0_heater_fan'])
        self.assertEqual(parser.get('multi_pin _unit0_heater_fan_pins', 'pins'), 'unit0:PA4, !unit0:PA5')
        fan = dict(parser.items('heater_fan _unit0_heater_fan'))
        self.assertEqual(fan['pin'], 'multi_pin:_unit0_heater_fan_pins')
        self.assertEqual(fan['heater'], 'qidi_heater')
        self.assertEqual(fan['heater_temp'], '40.0')

    def test_heater_fan_pin_validator_accepts_lists(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('heater_fan_pin_validator', profiles.get('qidi').syms)
        validator = kconfig.syms['PIN_HEATER_FAN'].validator
        for good in ('', 'unit0:PA4', '!unit0:PA4', 'unit0:PA4,unit0:PA5', 'PA4, PB8 ,  PB9'):
            with self.subTest(good=good):
                self.assertTrue(validator.fullmatch(good))
        for bad in (',', 'unit0:PA4,', ', unit0:PA4', 'unit0:PA4,,unit0:PA5', 'PA4 PB8'):
            with self.subTest(bad=bad):
                self.assertFalse(validator.fullmatch(bad))

    def test_per_gate_heater_fans_render_gate_aligned_heater_temp(self):
        profile = profiles.get('emu').derive(
            'emu_heater_fan_temps', syms=dict(
                {
                    'MMU_HAS_HEATER': True,
                    'PARAM_HEATER_FAN_HEATER_TEMP_1': 60.0,
                },
                **{'PIN_HEATER_FAN_%d' % gate: 'unit0_gate%d:PA14' % gate
                   for gate in range(5)}))
        parser = cfg.assemble(cfg.render(profile))
        for gate in range(5):
            fan = dict(parser.items('heater_fan _unit0_heater_fan%d' % gate))
            with self.subTest(gate=gate):
                self.assertEqual(fan['heater_temp'],
                                 '60.0' if gate == 1 else '45.0')

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
            ('controller_shared', {
                'MMU_TYPE_HTLF_1_0': True,
                'MMU_HAS_CONTROLLER_FAN': True,
            }, ('PIN_CONTROLLER_FAN',), ('PIN_CONTROLLER_FAN_0',)),
            ('controller_per_gate', {
                'MMU_TYPE_EMU_1_0': True,
                'MMU_HAS_CONTROLLER_FAN': True,
            }, ('PIN_CONTROLLER_FAN_0',), ('PIN_CONTROLLER_FAN',)),
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
        self.assertNotIn('controller_fan _unit1_controller_fan', self.parser.sections())
        # The fake controller_fan checked the custom fan's stepper names at connect
        fan = self.hh.printer.lookup_object('controller_fan unit1_mcu_fan')
        self.assertEqual(fan.stepper_names,
                         ('mmu_stepper unit1_selector', 'mmu_stepper unit1_gear'))

    def test_vivid_custom_fans_do_not_enable_managed_controls(self):
        syms = next(unit.syms for unit in self.profile.units if unit.name == 'unit1')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('vivid_custom_fans', syms)
        self.assertFalse(kconfig.is_enabled('MMU_HAS_FANS'))
        self.assertEqual(kconfig.syms['MMU_HAS_FANS'].visibility, 0)
        self.assertTrue(kconfig.is_enabled('CUSTOM_FAN_SETUP'))
        self.assertTrue(kconfig.is_enabled('MMU_HAS_HEATER_FANS'))
        self.assertEqual(kconfig.syms['MMU_HAS_HEATER_FANS'].assignable, (2,))
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

    def test_vivid_fixes_its_custom_hardware_capabilities(self):
        syms = next(unit.syms for unit in profiles.get('ercf_vvd').units
                    if unit.name == 'unit1')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('vvd_custom_fans', syms)
        self.assertTrue(kconfig.is_enabled('CUSTOM_ENVIRONMENT_SENSOR_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_FAN_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_FAN_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_CONTROLLER_FAN_SETUP'))
        self.assertFalse(kconfig.is_enabled('MMU_HAS_FANS'))
        self.assertEqual(kconfig.syms['MMU_HAS_FANS'].visibility, 0)
        self.assertFalse(kconfig.is_enabled('MMU_HAS_CONTROLLER_FAN'))
        self.assertEqual(kconfig.syms['MMU_HAS_CONTROLLER_FAN'].visibility, 0)
        self.assertTrue(kconfig.is_enabled('MMU_HAS_HEATER_FANS'))
        self.assertEqual(kconfig.syms['MMU_HAS_HEATER_FANS'].assignable, (2,))

    def test_kms_offers_the_generic_sensor_and_managed_fan(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('kms_custom_fans', profiles.get('kms').syms)
        self.assertFalse(kconfig.is_enabled('CUSTOM_ENVIRONMENT_SENSOR_SETUP'))
        self.assertFalse(kconfig.is_enabled('CUSTOM_FAN_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_SETUP'))
        self.assertTrue(kconfig.is_enabled('CUSTOM_HEATER_FAN_SETUP'))
        # Offered, but off unless the user adds a fan
        self.assertFalse(kconfig.is_enabled('MMU_HAS_FANS'))
        self.assertNotEqual(kconfig.syms['MMU_HAS_FANS'].visibility, 0)
        self.assertTrue(kconfig.is_enabled('MMU_HAS_HEATER_FANS'))
        self.assertEqual(kconfig.syms['MMU_HAS_HEATER_FANS'].assignable, (2,))

    def test_kms_custom_environment_sensor_is_not_duplicated(self):
        rendered = '\n'.join(cfg.render(profiles.get('kms')).values())

        self.assertEqual(
            rendered.count('[temperature_sensor unit0_environment]'), 1)
        self.assertEqual(rendered.count('[heater_generic unit0_heater]'), 1)
        self.assertNotIn('[fan_generic ', rendered)
        self.assertIn('[heater_fan unit0_fan_left]', rendered)
        self.assertIn('[heater_fan unit0_fan_right]', rendered)


class TestControllerFanRender(unittest.TestCase):

    @staticmethod
    def _section(profile):
        parser = cfg.assemble(cfg.render(profile))
        sections = [s for s in parser.sections() if s.startswith('controller_fan ')]
        return sections, parser

    def test_selector_machine_lists_gear_and_selector(self):
        sections, parser = self._section(_controller_fan_profile('tradrack'))
        self.assertEqual(sections, ['controller_fan _unit0_controller_fan'])
        fan = dict(parser.items(sections[0]))
        self.assertEqual(fan['pin'], 'unit0:PA3')
        self.assertEqual(fan['stepper'],
                         'mmu_stepper unit0_gear, mmu_stepper unit0_selector')
        self.assertEqual(fan['max_power'], '1.0')
        self.assertEqual(fan['kick_start_time'], '0.5')
        self.assertEqual(fan['fan_speed'], '1.0')
        self.assertEqual(fan['idle_timeout'], '30')
        # Present but empty, so Klipper's extruder heater default does not apply
        self.assertEqual(fan['heater'], '')

    def test_multigear_machine_lists_every_gear(self):
        sections, parser = self._section(_controller_fan_profile('boxturtle'))
        fan = dict(parser.items(sections[0]))
        self.assertEqual(
            fan['stepper'],
            'mmu_stepper unit0_gear, mmu_stepper unit0_gear_1, '
            'mmu_stepper unit0_gear_2, mmu_stepper unit0_gear_3')

    def test_hardware_parameters_are_rendered(self):
        sections, parser = self._section(_controller_fan_profile(
            'boxturtle',
            PARAM_CONTROLLER_FAN_MAX_POWER=0.8,
            PARAM_CONTROLLER_FAN_KICK_START_TIME=1.0,
            PARAM_CONTROLLER_FAN_SPEED=0.6,
            PARAM_CONTROLLER_FAN_IDLE_TIMEOUT=120))
        fan = dict(parser.items(sections[0]))
        self.assertEqual(fan['max_power'], '0.8')
        self.assertEqual(fan['kick_start_time'], '1.0')
        self.assertEqual(fan['fan_speed'], '0.6')
        self.assertEqual(fan['idle_timeout'], '120')

    def test_off_by_default(self):
        sections, _ = self._section(profiles.get('boxturtle'))
        self.assertEqual(sections, [])

    def test_empty_pin_renders_nothing_and_warns(self):
        profile = _controller_fan_profile('boxturtle', PIN_CONTROLLER_FAN='')
        sections, _ = self._section(profile)
        self.assertEqual(sections, [])
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('controller_fan_no_pin', profile.syms)
        self.assertTrue(kconfig.is_enabled('W32'))
        self.assertTrue(kconfig.is_enabled('SHOW_PER_UNIT_WARNINGS'))

    def test_is_independent_of_the_managed_fan(self):
        profile = _single_fan_profile().derive(
            'qidi_managed_and_controller_fan',
            syms={'MMU_HAS_CONTROLLER_FAN': True, 'PIN_CONTROLLER_FAN': 'unit0:PA3'})
        parser = cfg.assemble(cfg.render(profile))
        self.assertIn('fan_generic _unit0_fan', parser.sections())
        self.assertIn('controller_fan _unit0_controller_fan', parser.sections())
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['fan'], '_unit0_fan')

    def test_per_gate_fans_follow_each_gate_gear(self):
        profile = _per_gate_controller_fan_profile(
            PARAM_CONTROLLER_FAN_SPEED_2=0.7,
            PARAM_CONTROLLER_FAN_IDLE_TIMEOUT_2=90,
            PIN_CONTROLLER_FAN='unit0_gate0:PB9')
        sections, parser = self._section(profile)
        # Gate 1 and 3 have no pin, gate 4 is switched off; no shared fan in per-gate config
        self.assertEqual(sections, ['controller_fan _unit0_controller_fan0',
                                    'controller_fan _unit0_controller_fan2'])
        fan0 = dict(parser.items(sections[0]))
        fan2 = dict(parser.items(sections[1]))
        self.assertEqual(fan0['stepper'], 'mmu_stepper unit0_gear')
        self.assertEqual(fan2['stepper'], 'mmu_stepper unit0_gear_2')
        self.assertEqual(fan2['pin'], 'unit0_gate2:PB8')
        self.assertEqual(fan2['fan_speed'], '0.7')
        self.assertEqual(fan2['idle_timeout'], '90')
        self.assertEqual(fan0['fan_speed'], '1.0')
        self.assertEqual(fan0['heater'], '')

    def test_per_gate_missing_pin_is_warned(self):
        profile = _per_gate_controller_fan_profile()
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('per_gate_controller_fan_no_pin', profile.syms)
        self.assertTrue(kconfig.is_enabled('W32'))
        self.assertEqual(kconfig.syms['PIN_CONTROLLER_FAN'].visibility, 0)

        all_pins = dict(profile.syms, PIN_CONTROLLER_FAN_1='unit0_gate1:PB8',
                        PIN_CONTROLLER_FAN_3='unit0_gate3:PB8')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('per_gate_controller_fan_pins', all_pins)
        self.assertFalse(kconfig.is_enabled('W32'))

    def test_option_and_raw_pin_have_help_text(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('controller_fan_help', profiles.get('boxturtle').syms)
        for name in ('MMU_HAS_CONTROLLER_FAN', 'PARAM_CONTROLLER_FAN_MAX_POWER',
                     'PARAM_CONTROLLER_FAN_KICK_START_TIME', 'PARAM_CONTROLLER_FAN_SPEED',
                     'PARAM_CONTROLLER_FAN_IDLE_TIMEOUT', 'PIN_CONTROLLER_FAN',
                     'PARAM_CONTROLLER_FAN_GATE_0', 'PARAM_CONTROLLER_FAN_MAX_POWER_0',
                     'PARAM_CONTROLLER_FAN_KICK_START_TIME_0', 'PARAM_CONTROLLER_FAN_SPEED_0',
                     'PARAM_CONTROLLER_FAN_IDLE_TIMEOUT_0', 'PIN_CONTROLLER_FAN_0'):
            with self.subTest(option=name):
                self.assertTrue(all(node.help for node in kconfig.syms[name].nodes
                                    if node.prompt))


class TestControllerFanBoot(unittest.TestCase):
    """The fake controller_fan checks stepper names at klippy:connect, as Klipper does"""

    def _boot(self, base):
        hh = session(_controller_fan_profile(base))
        self.addCleanup(hh.close)
        hh.boot()
        return hh.printer.lookup_object('controller_fan _unit0_controller_fan')

    def test_selector_machine_boots(self):
        fan = self._boot('tradrack')
        self.assertEqual(fan.stepper_names,
                         ('mmu_stepper unit0_gear', 'mmu_stepper unit0_selector'))
        self.assertEqual(fan.heater_names, ())

    def test_multigear_machine_boots(self):
        fan = self._boot('boxturtle')
        self.assertEqual(len(fan.stepper_names), 4)

    def test_per_gate_mcu_machine_boots(self):
        for base in ('emu', 'emu_ebb'):
            with self.subTest(profile=base):
                hh = session(_per_gate_controller_fan_profile(base))
                self.addCleanup(hh.close)
                hh.boot()
                fan = hh.printer.lookup_object('controller_fan _unit0_controller_fan2')
                self.assertEqual(fan.stepper_names, ('mmu_stepper unit0_gear_2',))

    def test_multi_unit_fan_lists_only_its_own_unit(self):
        base = profiles.get('ercf_vvd')
        units = [unit.derive(syms={'MMU_HAS_CONTROLLER_FAN': True,
                                   'PIN_CONTROLLER_FAN': 'unit0:PA3'})
                 if unit.name == 'unit0' else unit
                 for unit in base.units]
        profile = base.derive('ercf_vvd_controller_fan', units=units)
        parser = cfg.assemble(cfg.render(profile))
        self.assertEqual(
            sorted(s for s in parser.sections() if s.startswith('controller_fan ')),
            ['controller_fan _unit0_controller_fan', 'controller_fan unit1_mcu_fan'])

        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot()
        fan = hh.printer.lookup_object('controller_fan _unit0_controller_fan')
        self.assertTrue(fan.stepper_names)
        self.assertTrue(all(name.startswith('mmu_stepper unit0_')
                            for name in fan.stepper_names), fan.stepper_names)

    def test_unknown_stepper_is_rejected(self):
        fan = self._boot('boxturtle')
        fan.stepper_names = ['mmu_stepper unit0_gaer']
        with self.assertRaisesRegex(Exception, 'steppers are unknown'):
            fan.handle_connect()


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

    def test_status_reports_the_runtime_fan_mode(self):
        def fans():
            return self.hh.mmu.get_status(self.hh.reactor.monotonic())['fans']
        self.assertEqual(fans(), [{'unit': 'unit0', 'first_gate': 0, 'per_gate': False,
                                   'enabled': True, 'modes': [2]}])
        self.hh.run_gcode('MMU_FAN FAN_FORCED=1')
        self.assertEqual(fans()[0]['modes'], [1])
        self.hh.run_gcode('MMU_FAN ENABLE=0 FAN_FORCED=0')
        self.assertEqual((fans()[0]['enabled'], fans()[0]['modes']), (False, [0]))

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
            self.hh.run_gcode('MMU_FAN SOURCE=environment GATE=1')
        # The EMU default source is mcu, and gate 1's MCU sensor exists.
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

        # The EMU defaults every fan to its MCU sensor; gate 0 is explicitly
        # switched to its environment sensor to exercise both sources.
        self.hh.run_gcode('MMU_FAN SOURCE=environment GATE=0')
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
            'mcu')
        self.assertEqual(self.hh.errors, [])

    def test_per_gate_command_targets_only_selected_fans(self):
        self.hh.close()
        self.hh = session('emu')
        self.addCleanup(self.hh.close)
        self.hh.boot()
        unit = self.hh.mmu.mmu_unit(0)
        fans = [self.hh.printer.lookup_object(name) for name in unit.fans]
        mcu_sensor = self.hh.printer.lookup_object('temperature_sensor _unit0_mcu1')

        mcu_sensor.feed(50.)
        self.hh.reactor.advance(unit.p.fan_polling_time)
        self.assertEqual([fan.get_status(0)['speed'] for fan in fans],
                         [0., 1., 0., 0., 0.])
        mcu_sensor.feed(47.)
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
