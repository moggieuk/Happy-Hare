# Chameleon X5: a generic five-channel board. It supplies defaults for the features it
# supports (and its heater as misc hardware) but never turns a feature on; the machine
# type does that. QuattroBox v2 turns them on when the X5 is its board.

import re
import unittest

from test.hh import cfg, profiles

SECTION = re.compile(r'^\[([^\]]+)\]', re.M)
QB2 = {'MMU_FAMILY_QUATTRO_BOX': True, 'MMU_TYPE_QUATTRO_BOX_2_0': True}
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

    def test_quattro_box_wiring(self):
        self.assertEqual(self.item('temperature_sensor unit0_Outside')['i2c_address'], '119')
        self.assertEqual(self.item('mmu_servo exhaust')['pin'], 'unit0:PB0')
        board_fan = self.item('controller_fan unit0_fan')
        self.assertEqual(board_fan['pin'], 'unit0:PB4')
        for stepper in board_fan['stepper'].split(','):
            with self.subTest(stepper=stepper.strip()):
                self.assertIn(stepper.strip(), self.parser.sections())

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
        self.assertIn('controller_fan unit0_fan', parser.sections())
        self.assertIn('mmu_servo exhaust', parser.sections())


class TestX5UnderAnotherMachine(unittest.TestCase):

    def test_board_turns_nothing_on(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('x5_boxturtle_flags', X5_BOXTURTLE)
        for flag in ('MMU_HAS_ENVIRONMENT_SENSOR', 'MMU_HAS_HEATER', 'MMU_HAS_FANS'):
            with self.subTest(flag=flag):
                self.assertFalse(kconfig.is_enabled(flag))
        parser, _ = _render('x5_boxturtle', X5_BOXTURTLE)
        for kind in ('heater_generic', 'heater_fan', 'multi_pin', 'fan_generic', 'controller_fan', 'mmu_servo'):
            with self.subTest(kind=kind):
                self.assertFalse(_sections(parser, kind))
        self.assertNotIn('temperature_sensor unit0_Outside', parser.sections())

    def test_enabled_features_take_the_board_defaults(self):
        parser, _ = _render('x5_boxturtle_dryer', dict(
            X5_BOXTURTLE, MMU_HAS_HEATER=True, MMU_HAS_ENVIRONMENT_SENSOR=True))
        heater = dict(parser.items('heater_generic unit0_heater'))
        self.assertEqual((heater['heater_pin'], heater['sensor_pin']), ('unit0:PC6', 'unit0:PC0'))
        self.assertNotIn('temperature_sensor unit0_Heater_B', parser.sections())
        self.assertEqual(_heater_fan_pins(parser)[1], ['unit0:PB8', 'unit0:PB9'])
        sensor = dict(parser.items('temperature_sensor unit0_Env'))
        self.assertEqual(sensor['i2c_software_scl_pin'], 'unit0:PA8')
        self.assertEqual(sensor['i2c_software_sda_pin'], 'unit0:PC9')
        self.assertNotIn('temperature_sensor unit0_Outside', parser.sections())


class TestQuattroBoxV2OnAnotherBoard(unittest.TestCase):

    def test_nothing_from_the_x5(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('qb2_mmb_flags', dict(QB2, BOARD_TYPE_MMB_2_0=True))
        for flag in ('MMU_HAS_ENVIRONMENT_SENSOR', 'MMU_HAS_HEATER', 'MMU_HAS_FANS'):
            with self.subTest(flag=flag):
                self.assertFalse(kconfig.is_enabled(flag))
        self.assertEqual(kconfig.get('PARAM_MISC_HARDWARE'), '')


if __name__ == '__main__':
    unittest.main()
