# Happy Hare heater fan configuration tests.

import unittest

from test.hh import cfg, profiles

HARDWARE = 'config/base/mmu_hardware.cfg'


class TestMmuHeaterFanRender(unittest.TestCase):

    def test_shared_heater_creates_one_heater_fan(self):
        profile = profiles.get('nfc_single').derive(
            'generic_shared_heater_fan',
            syms={
                'MMU_HAS_HEATER': True,
                'PARAM_FILAMENT_HEATER': 'unit0_heater',
                'PIN_HEATER_FAN': 'unit0:PA8',
                'PARAM_HEATER_FAN_MAX_POWER': 0.8,
                'PARAM_HEATER_FAN_SHUTDOWN_SPEED': 0.1,
                'PARAM_HEATER_FAN_KICK_START_TIME': 0.7,
            })
        parser = cfg.assemble(cfg.render(profile))

        fan = dict(parser.items('heater_fan unit0_fan'))
        self.assertEqual(fan, {
            'pin': 'unit0:PA8',
            'max_power': '0.8',
            'shutdown_speed': '0.1',
            'kick_start_time': '0.7',
            'heater': 'unit0_heater',
        })

    def test_per_gate_heaters_create_gate_aligned_heater_fans(self):
        fan_pins = {
            'PIN_HEATER_FAN_%d' % gate: 'unit0_gate%d:PA8' % gate
            for gate in range(5)
        }
        profile = profiles.get('emu').derive(
            'emu_generic_heater_fans',
            syms=dict({'MMU_HAS_HEATER': True}, **fan_pins))
        parser = cfg.assemble(cfg.render(profile))

        sections = [
            section for section in parser.sections()
            if section.startswith('heater_fan ')
        ]
        self.assertEqual(
            sections,
            ['heater_fan unit0_fan_%d' % gate for gate in range(5)])
        for gate, section in enumerate(sections):
            fan = dict(parser.items(section))
            self.assertEqual(fan['pin'], 'unit0_gate%d:PA8' % gate)
            self.assertEqual(fan['heater'], 'unit0_heater%d' % gate)

    def test_heater_fan_can_be_disabled(self):
        profile = profiles.get('nfc_single').derive(
            'generic_shared_heater_fan_disabled',
            syms={
                'MMU_HAS_HEATER': True,
                'PARAM_FILAMENT_HEATER': 'unit0_heater',
                'MMU_HAS_HEATER_FAN': False,
                'PIN_HEATER_FAN': 'unit0:PA8',
            })
        parser = cfg.assemble(cfg.render(profile))
        self.assertNotIn('heater_fan unit0_fan', parser.sections())

    def test_custom_heater_fans_suppress_generic_generation(self):
        profile = profiles.get('kms').derive(
            'kms_custom_heater_fans',
            syms={'PIN_HEATER_FAN': 'unit0:PA8'})
        parser = cfg.assemble(cfg.render(profile))

        self.assertNotIn('heater_fan unit0_fan', parser.sections())
        self.assertIn('heater_fan unit0_fan_left', parser.sections())
        self.assertIn('heater_fan unit0_fan_right', parser.sections())

    def test_heater_fan_block_precedes_nfc_readers(self):
        profile = profiles.get('nfc_single').derive(
            'nfc_with_generic_heater_fan',
            syms={
                'MMU_HAS_HEATER': True,
                'PARAM_FILAMENT_HEATER': 'unit0_heater',
                'PIN_HEATER_FAN': 'unit0:PA8',
            })
        hardware = cfg.render(profile)[HARDWARE]
        self.assertLess(
            hardware.index('[heater_fan unit0_fan]'),
            hardware.index('# NFC READER(S)'))


if __name__ == '__main__':
    unittest.main()
