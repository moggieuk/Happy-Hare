# Chameleon X5: a generic five-channel board. It supplies defaults for the features it
# supports (and its heater as misc hardware) but never turns a feature on; the machine
# type does that. QuattroBox v2 turns them on when the X5 is its board.

import re
import unittest

from test.hh import cfg, profiles

SECTION = re.compile(r'^\[([^\]]+)\]', re.M)
QB2 = {'MMU_FAMILY_QUATTRO_BOX': True, 'MMU_TYPE_QUATTRO_BOX_2_0': True}
X5 = {'BOARD_TYPE_CHAMELEON_X5_1_0': True}
CUSTOM_FLAGS = ('CUSTOM_HEATER_SETUP', 'CUSTOM_MISC_SETUP')
X5_BOXTURTLE = {'MMU_TYPE_BOX_TURTLE_1_0': True, 'BOARD_TYPE_CHAMELEON_X5_1_0': True}


def _render(name, syms):
    rendered = cfg.render(profiles.Profile(name, syms=syms))
    text = rendered['config/base/mmu_hardware.cfg']
    return cfg.assemble(rendered, macros=False), text


def _sections(parser, kind):
    return [s for s in parser.sections() if s.split(' ', 1)[0] == kind]


def _heater_fan_pins(parser):
    """The one shared heater fan and the physical pins it drives (through a multi_pin)."""
    fan = dict(parser.items('heater_fan _unit0_heater_fan'))
    if not fan['pin'].startswith('multi_pin:'):
        return fan, [fan['pin']]
    alias = dict(parser.items('multi_pin ' + fan['pin'].split(':', 1)[1]))
    return fan, [pin.strip() for pin in alias['pins'].split(',')]


class TestQuattroBoxV2OnX5(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser, cls.text = _render('x5_qb2', QB2)

    def item(self, section):
        return dict(self.parser.items(section))

    def test_unit_uses_the_feature_sensor_heater_and_fan(self):
        unit = self.item('mmu_unit unit0')
        self.assertEqual(unit['environment_sensor'], 'unit0_Chamber')
        self.assertEqual(unit['filament_heater'], 'unit0_heater')
        self.assertEqual(unit['fan'], '_unit0_fan')

    def test_chamber_sensor_from_the_feature(self):
        sensor = self.item('temperature_sensor unit0_Chamber')
        self.assertEqual(sensor['sensor_type'], 'BME280')
        self.assertEqual(sensor['i2c_address'], '118')
        self.assertEqual(sensor['i2c_software_scl_pin'], 'unit0:PA8')
        self.assertEqual(sensor['i2c_software_sda_pin'], 'unit0:PC9')

    def test_heater_from_the_board(self):
        heater = self.item('heater_generic unit0_heater')
        self.assertEqual(heater['heater_pin'], 'unit0:PC6')
        self.assertEqual(heater['sensor_list'],
                         'temperature_sensor unit0_Heater_A, temperature_sensor unit0_Heater_B')

    def test_custom_flags_are_set_by_the_machine_type(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('x5_qb2_flags', QB2)
        for flag in CUSTOM_FLAGS:
            with self.subTest(flag=flag):
                self.assertTrue(kconfig.is_enabled(flag))
                self.assertTrue(any(node.filename.endswith('mmu_types/Kconfig.quattro_box')
                                    for node in kconfig.syms[flag].nodes))
        # No block matches these: CUSTOM_FAN_SETUP and CUSTOM_ENVIRONMENT_SENSOR_SETUP
        # would drop the managed fan and Chamber sensor
        for flag in ('CUSTOM_LED_SETUP', 'CUSTOM_ENVIRONMENT_SENSOR_SETUP', 'CUSTOM_FAN_SETUP',
                     'CUSTOM_HEATER_FAN_SETUP', 'CUSTOM_CONTROLLER_FAN_SETUP',
                     'CUSTOM_NFC_READER_SETUP'):
            with self.subTest(flag=flag):
                self.assertFalse(kconfig.is_enabled(flag))

    def test_heater_fans_from_the_feature(self):
        self.assertEqual(_sections(self.parser, 'heater_fan'), ['heater_fan _unit0_heater_fan'])
        fan, pins = _heater_fan_pins(self.parser)
        self.assertEqual(pins, ['unit0:PB8', 'unit0:PB9'])
        self.assertEqual(fan['heater'], 'unit0_heater')
        self.assertEqual(fan['heater_temp'], '40.0')
        self.assertEqual(fan['shutdown_speed'], '0.0')
        # Klipper resolves multi_pin:<name> while loading the [heater_fan], in file order
        sections = SECTION.findall(self.text)
        alias = 'multi_pin ' + fan['pin'].split(':', 1)[1]
        self.assertLess(sections.index(alias), sections.index('heater_fan _unit0_heater_fan'))

    def test_exhaust_fan_is_the_managed_fan(self):
        self.assertEqual(self.item('fan_generic _unit0_fan')['pin'], 'unit0:PB5')

    def test_vent_servo_from_the_feature(self):
        self.assertEqual(self.item('mmu_servo unit0_vent_servo')['pin'], 'unit0:PB0')
        self.assertEqual(_sections(self.parser, 'mmu_servo'), ['mmu_servo unit0_vent_servo'])
        self.assertEqual(self.item('mmu_unit unit0')['vent_servo'], 'unit0_vent_servo')
        params = self.item('mmu_unit_parameters unit0')
        self.assertEqual({k: params[k] for k in (
            'heater_vent_interval', 'heater_vent_duration', 'heater_vent_open_angle', 'heater_vent_macro')},
            {'heater_vent_interval': '20', 'heater_vent_duration': '240',
             'heater_vent_open_angle': '150', 'heater_vent_macro': ''})

    def test_quattro_box_wiring(self):
        self.assertEqual(self.item('temperature_sensor unit0_Outside')['i2c_address'], '119')

    def test_controller_fan_from_the_feature(self):
        self.assertEqual(_sections(self.parser, 'controller_fan'), ['controller_fan _unit0_controller_fan'])
        board_fan = self.item('controller_fan _unit0_controller_fan')
        self.assertEqual((board_fan['pin'], board_fan['max_power'], board_fan['fan_speed']),
                         ('unit0:PB4', '0.8', '1.0'))
        self.assertEqual(board_fan['stepper'], ', '.join(
            'mmu_stepper unit0_gear' + suffix for suffix in ('', '_1', '_2', '_3')))

    def test_gear_directions_are_left_to_the_user(self):
        for gate, suffix in enumerate(('', '_1', '_2', '_3')):
            stepper = self.item('mmu_stepper unit0_gear' + suffix)
            with self.subTest(gate=gate):
                self.assertFalse(stepper['step_pin'].startswith('!'), stepper['step_pin'])
                self.assertFalse(stepper['dir_pin'].startswith('!'), stepper['dir_pin'])

    def test_eject_buttons_follow_the_channel_order(self):
        pins = [self.item('gcode_button unit0_eject%d' % gate)['pin'] for gate in range(4)]
        self.assertEqual(pins, ['^unit0:PB14', '^unit0:PB12', '^unit0:PD8', '^unit0:PE14'])

    def test_gate_sensors_and_shared_exit_homing(self):
        sensors = self.item('mmu_sensors unit0')
        self.assertEqual(sensors['mmu_entry_switch_pin_0'], '^unit0:PE11')
        self.assertEqual(sensors['mmu_exit_switch_pin_0'], '^unit0:PE10')
        self.assertEqual(sensors['mmu_shared_exit_switch_pin'], '^unit0:PA6')
        params = self.item('mmu_unit_parameters unit0')
        self.assertEqual(params['gate_homing_endstop'], 'mmu_shared_exit')
        self.assertEqual(params['gate_parking_distance'], '-30')
        self.assertEqual(params['gate_preload_endstop'], 'mmu_exit')
        self.assertEqual(params['gate_preload_parking_distance'], '10')

    def test_proportional_buffer_on_t2_with_default_calibration(self):
        self.assertEqual(self.item('mmu_unit unit0')['buffer'], 'unit0')
        buffer = self.item('mmu_buffer unit0')
        self.assertEqual(buffer['analog_pin'], 'unit0:PC2')
        self.assertNotIn('tension_pin', buffer)
        self.assertNotIn('compression_pin', buffer)
        # Calibration is the user's, not the machine's
        self.assertEqual((buffer['analog_max_compression'], buffer['analog_max_tension'],
                          buffer['analog_neutral_point']), ('1.0', '0.0', '0.5'))

    def test_led_segments(self):
        leds = self.item('mmu_leds unit0')
        self.assertEqual(leds['exit_leds'], 'neopixel:_unit0_leds (1-4)')
        self.assertEqual(leds['entry_leds'], 'neopixel:_unit0_leds (8-5)')
        self.assertEqual(leds['status_leds'], 'neopixel:_unit0_leds (9-38)')
        self.assertEqual(leds['logo_leds'], '')
        self.assertEqual(leds['entry_effect'], 'gate_status')

    def test_machine_tuning(self):
        params = self.item('mmu_unit_parameters unit0')
        expected = {
            'gear_load_speed': '350', 'gear_load_accel': '500',
            'gear_unload_speed': '350', 'gear_unload_accel': '500',
            'gear_homing_speed': '50', 'sync_gear_current': '40',
            'bowden_unload_homing_buffer': '50',
        }
        self.assertEqual({k: params[k] for k in expected}, expected)
        self.assertEqual(self.item('tmc2209 mmu_stepper unit0_gear')['run_current'], '0.9')

    def test_no_section_or_pin_is_defined_twice(self):
        sections = SECTION.findall(self.text)
        self.assertEqual(sorted({s for s in sections if sections.count(s) > 1}), [])
        pins = re.findall(r'^(?:pin|heater_pin|sensor_pin)\s*:\s*(\S+)', self.text, re.M)
        pins += [p.strip() for line in re.findall(r'^pins\s*:\s*(.+)$', self.text, re.M)
                 for p in line.split(',')]
        self.assertEqual(sorted({p for p in pins if pins.count(p) > 1}), [])

    def test_without_heater_keeps_the_quattro_box_wiring(self):
        parser, _ = _render('x5_qb2_no_heater', dict(QB2, MMU_HAS_HEATER=False))
        self.assertFalse(_sections(parser, 'heater_generic') + _sections(parser, 'heater_fan')
                         + _sections(parser, 'multi_pin'))
        self.assertIn('controller_fan _unit0_controller_fan', parser.sections())
        self.assertIn('mmu_servo unit0_vent_servo', parser.sections())


class TestX5UnderAnotherMachine(unittest.TestCase):

    def test_board_turns_nothing_on(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('x5_boxturtle_flags', X5_BOXTURTLE)
        for flag in ('MMU_HAS_ENVIRONMENT_SENSOR', 'MMU_HAS_HEATER', 'MMU_HAS_FANS',
                     'MMU_HAS_VENT_SERVO', 'MMU_HAS_CONTROLLER_FAN') + CUSTOM_FLAGS:
            with self.subTest(flag=flag):
                self.assertFalse(kconfig.is_enabled(flag))
        parser, _ = _render('x5_boxturtle', X5_BOXTURTLE)
        for kind in ('heater_generic', 'heater_fan', 'multi_pin', 'fan_generic', 'controller_fan', 'mmu_servo'):
            with self.subTest(kind=kind):
                self.assertFalse(_sections(parser, kind))
        self.assertNotIn('temperature_sensor unit0_Outside', parser.sections())
        # PC2 is the T2 thermistor except on QuattroBox v2
        parser, _ = _render('x5_custom_buffer', dict(
            X5, MMU_CUSTOM=True, MMU_HAS_SYNC_FEEDBACK_BUFFER=True,
            MMU_HAS_SENSOR_BUFFER_PROPORTIONAL=True))
        self.assertNotIn('PC2', dict(parser.items('mmu_buffer unit0')).get('analog_pin', ''))
        # PB0 is CH4's LED pin except on QuattroBox v2
        parser, _ = _render('x5_boxturtle_vent', dict(X5_BOXTURTLE, MMU_HAS_VENT_SERVO=True))
        self.assertNotIn('mmu_servo unit0_vent_servo', parser.sections())

    def test_enabled_features_take_the_board_pins(self):
        parser, _ = _render('x5_boxturtle_dryer', dict(
            X5_BOXTURTLE, MMU_HAS_HEATER=True, MMU_HAS_ENVIRONMENT_SENSOR=True,
            PARAM_FILAMENT_HEATER='my_heater'))
        # The heater itself is the user's, as on any other board
        self.assertFalse(_sections(parser, 'heater_generic'))
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['filament_heater'], 'my_heater')
        fan, pins = _heater_fan_pins(parser)
        self.assertEqual(pins, ['unit0:PB8', 'unit0:PB9'])
        self.assertEqual(fan['heater'], 'my_heater')
        sensor = dict(parser.items('temperature_sensor unit0_Env'))
        self.assertEqual(sensor['i2c_software_scl_pin'], 'unit0:PA8')
        self.assertEqual(sensor['i2c_software_sda_pin'], 'unit0:PC9')
        self.assertNotIn('temperature_sensor unit0_Outside', parser.sections())

    def test_channel_sensors_follow_the_channel_order(self):
        # FYSETC schematic: channel i's IN/OUT connectors sit beside motor i
        parser, _ = _render('x5_boxturtle_sensors', dict(
            X5_BOXTURTLE, MMU_HAS_SENSOR_ENTRY=True, MMU_HAS_SENSOR_EXIT=True))
        sensors = dict(parser.items('mmu_sensors unit0'))
        self.assertEqual([sensors['mmu_entry_switch_pin_%d' % g] for g in range(4)],
                         ['^unit0:PE11', '^unit0:PB1', '^unit0:PA0', '^unit0:PA4'])
        self.assertEqual([sensors['mmu_exit_switch_pin_%d' % g] for g in range(4)],
                         ['^unit0:PE10', '^unit0:PE7', '^unit0:PA1', '^unit0:PA3'])


    def test_per_gate_fans_use_the_channel_fans(self):
        parser, _ = _render('x5_per_gate_fans', dict(
            X5, MMU_CUSTOM=True, MMU_HAS_ENVIRONMENT_SENSOR=True, MMU_HAS_FANS=True,
            MMU_HAS_PER_GATE_CONFIG=True))
        self.assertEqual([dict(parser.items('fan_generic _unit0_fan%d' % g))['pin'] for g in range(4)],
                         ['unit0:PB3', 'unit0:PB4', 'unit0:PB8', 'unit0:PB9'])

    def test_nfc_readers_default_to_the_i2c_0_header(self):
        _, text = _render('x5_nfc_common', dict(
            X5, MMU_CUSTOM=True, MMU_HAS_NFC_READER=True, MMU_HAS_COMMON_NFC_READER=True,
            CHOICE_NFC_READER_TYPE_PN532=True))
        self.assertRegex(text, r'(?m)^i2c_bus\s*:\s*i2c1\s*$')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('x5_nfc_per_gate', dict(
                X5, MMU_CUSTOM=True, MMU_HAS_NFC_READER=True, MMU_HAS_PER_GATE_NFC_READERS=True,
                CHOICE_NFC_READER_TYPE_PN7160_0=True, CHOICE_NFC_READER_TYPE_PN7160_1=True))
        self.assertEqual([kconfig.get('PARAM_NFC_READER_I2C_BUS_%d' % g) for g in range(2)],
                         ['i2c1', 'i2c1'])


class TestQuattroBoxV2OnAnotherBoard(unittest.TestCase):

    def test_every_version_forces_the_filament_buffer_off(self):
        for version in ('MMU_TYPE_QUATTRO_BOX_1_0', 'MMU_TYPE_QUATTRO_BOX_1_1', 'MMU_TYPE_QUATTRO_BOX_2_0'):
            with self.subTest(version=version):
                with cfg._env(cfg._SINGLE_UNIT_ENV):
                    kconfig = cfg._kconfig('qb_no_filament_buffer_' + version, {
                        'MMU_FAMILY_QUATTRO_BOX': True, version: True, 'MMU_HAS_FILAMENT_BUFFER': True})
                # Evaluated value: the forced assignment above is overridden by the select
                self.assertEqual(kconfig.syms['MMU_HAS_FILAMENT_BUFFER'].str_value, 'n')
                self.assertEqual(kconfig.syms['MMU_HAS_FILAMENT_BUFFER'].visibility, 0)

    def test_machine_features_without_the_x5_hardware(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('qb2_mmb_flags', dict(QB2, BOARD_TYPE_MMB_2_0=True))
        # The machine has these whatever the board
        for flag in ('MMU_HAS_ENVIRONMENT_SENSOR', 'MMU_HAS_SENSOR_ENTRY', 'MMU_HAS_SENSOR_EXIT',
                     'MMU_HAS_SENSOR_SHARED_EXIT'):
            with self.subTest(flag=flag):
                self.assertTrue(kconfig.is_enabled(flag))
        self.assertEqual(kconfig.get('PARAM_GATE_HOMING_ENDSTOP'), 'mmu_shared_exit')
        self.assertEqual(kconfig.get('PARAM_GATE_PRELOAD_PARKING_DISTANCE'), '10')
        # The X5 supplies the pins and hardware for these
        for flag in ('MMU_HAS_HEATER', 'MMU_HAS_FANS', 'MMU_HAS_SYNC_FEEDBACK_BUFFER',
                     'MMU_HAS_VENT_SERVO', 'MMU_HAS_CONTROLLER_FAN') + CUSTOM_FLAGS:
            with self.subTest(flag=flag):
                self.assertFalse(kconfig.is_enabled(flag))
        self.assertEqual(kconfig.get('PARAM_MISC_HARDWARE'), '')

    def test_preload_parks_behind_the_sensor_without_exit_sensors(self):
        # Forward preload parking is only valid on per-gate exit sensors
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('qb2_mmb_no_exit', dict(QB2, BOARD_TYPE_MMB_2_0=True,
                                                           MMU_HAS_SENSOR_EXIT=False))
        self.assertEqual(kconfig.get('PARAM_GATE_PRELOAD_PARKING_DISTANCE'), '-20')

if __name__ == '__main__':
    unittest.main()
