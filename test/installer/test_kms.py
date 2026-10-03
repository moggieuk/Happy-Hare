# KMS board misc hardware: the heater and heater fans stay custom, each CUSTOM_* flag
# set in the board file beside its misc_hardware_<group>; the environment sensor
# comes from the generic feature with board and type defaults.

import re
import unittest

from test.hh import cfg, profiles

SECTION = re.compile(r'^\[([^\]]+)\]', re.M)


class TestKmsBoardOwnsCustomHardware(unittest.TestCase):

    FLAGS = ('CUSTOM_HEATER_SETUP', 'CUSTOM_HEATER_FAN_SETUP')

    def test_each_custom_flag_is_set_in_the_board_file(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('kms_custom_flags', profiles.get('kms').syms)
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                self.assertTrue(kconfig.is_enabled(flag))
                self.assertTrue(any(node.filename.endswith('boards/custom/Kconfig.kms')
                                    for node in kconfig.syms[flag].nodes))
        for flag in ('CUSTOM_LED_SETUP', 'CUSTOM_ENVIRONMENT_SENSOR_SETUP',
                     'CUSTOM_FAN_SETUP', 'CUSTOM_NFC_READER_SETUP'):
            self.assertFalse(kconfig.is_enabled(flag))

    def test_misc_hardware_sections_render_once(self):
        rendered = cfg.render(profiles.get('kms'))
        sections = [s for text in rendered.values() for s in SECTION.findall(text)]
        for section in ('temperature_sensor unit0_environment', 'heater_generic unit0_heater',
                        'verify_heater unit0_heater', 'heater_fan unit0_fan_left',
                        'heater_fan unit0_fan_right'):
            with self.subTest(section=section):
                self.assertEqual(sections.count(section), 1)
        self.assertFalse([s for s in sections if s.startswith('heater_fan _')])

    def test_environment_sensor_comes_from_the_generic_feature(self):
        parser = cfg.assemble(cfg.render(profiles.get('kms')), macros=False)
        sensor = dict(parser.items('temperature_sensor unit0_environment'))
        self.assertEqual(sensor['sensor_type'], 'HTU21D')
        self.assertEqual(sensor['i2c_address'], '64')
        self.assertEqual(sensor['htu21d_report_time'], '10')
        self.assertEqual(sensor['i2c_software_scl_pin'], 'unit0:PB10')
        self.assertEqual(sensor['i2c_software_sda_pin'], 'unit0:PB11')
        self.assertEqual(dict(parser.items('mmu_unit unit0'))['environment_sensor'],
                         'unit0_environment')


if __name__ == '__main__':
    unittest.main()
