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
        parser = cfg.assemble(cfg.render(profiles.get('emu')))
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

    def test_shared_fan_can_select_mcu_temperature(self):
        mcu_profile = _single_fan_profile().derive(
            'qidi_mcu_fan_source',
            syms={'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU': True})
        mcu_parser = cfg.assemble(cfg.render(mcu_profile))
        self.assertEqual(
            dict(mcu_parser.items('mmu_unit_parameters unit0'))[
                'default_fan_temperature_source'],
            'mcu')

    def test_shared_fan_has_no_source_when_none_is_available(self):
        profile = _single_fan_profile().derive(
            'qidi_fan_without_temperature_source',
            syms={
                'MMU_HAS_ENVIRONMENT_SENSOR': False,
                'BOOL_CREATE_MCU_ENVIRONMENT_SENSORS': False,
                'MMU_HAS_HEATER': False,
            })
        parser = cfg.assemble(cfg.render(profile))
        self.assertEqual(
            dict(parser.items('mmu_unit_parameters unit0'))[
                'default_fan_temperature_source'],
            '')

    def test_per_gate_fans_use_one_configured_default_source(self):
        profile = profiles.get('emu').derive(
            'emu_mcu_fan_source',
            syms={'CHOICE_DEFAULT_FAN_TEMPERATURE_SOURCE_MCU': True})
        parser = cfg.assemble(cfg.render(profile))
        params = dict(parser.items('mmu_unit_parameters unit0'))
        self.assertEqual(params['default_fan_temperature_source'], 'mcu')
        self.assertNotIn('fan_temperature_sources', params)

    def test_per_gate_mcu_design_can_select_one_shared_fan(self):
        profile = profiles.get('emu').derive(
            'emu_shared_fan',
            syms={
                'MMU_HAS_PER_GATE_FANS': False,
                'PIN_FAN': 'unit0_gate0:PA15',
            })
        parser = cfg.assemble(cfg.render(profile))
        unit = dict(parser.items('mmu_unit unit0'))

        self.assertEqual(unit['fan'], '_unit0_fan')
        self.assertNotIn('fans', unit)
        fan_sections = [section for section in parser.sections()
                        if section.startswith('fan_generic ')]
        self.assertEqual(fan_sections, ['fan_generic _unit0_fan'])

    def test_legacy_fan_macro_configuration_is_not_rendered(self):
        rendered = cfg.render(_single_fan_profile())
        self.assertNotIn('gcode_macro _MMU_FAN_VARS',
                         cfg.sections(rendered[MACRO_VARS]))


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
            }, ('PIN_FAN',), ('PIN_FAN_0',)),
            ('per_gate', {
                'MMU_TYPE_EMU_1_0': True,
                'MMU_HAS_HEATER': True,
            }, ('PIN_FAN_0',), ('PIN_FAN',)),
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
        self.assertEqual(kconfig.syms['PIN_FAN'].visibility, 0)
        self.assertEqual(kconfig.syms['PIN_FAN_0'].visibility, 0)
        self.assertEqual(kconfig.named_choices['PARAM_FAN_FORCED_MODE'].visibility, 0)

    def test_mmu_fan_reports_no_manageable_fans(self):
        unit = next(unit for unit in self.hh.mmu.mmu_machine.units
                    if unit.name == 'unit1')
        self.assertEqual(unit.fan_manager.fans, [])
        with self.assertRaisesRegex(Exception, '^No manageable fans on this unit$'):
            self.hh.run_gcode('MMU_FAN UNIT=unit1')


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


if __name__ == '__main__':
    unittest.main()
