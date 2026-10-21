# Board files record every fan header: each is given a fan role (managed, heater or
# controller fan) as a default, or listed as a spare. The defaults only take effect when
# that fan type is enabled.

import unittest

from test.hh import cfg, profiles

BOXTURTLE = {'MMU_TYPE_BOX_TURTLE_1_0': True}


def _kconfig(label, syms):
    with cfg._env(cfg._SINGLE_UNIT_ENV):
        return cfg._kconfig(label, syms)


class TestSingleBoardFanHeaders(unittest.TestCase):

    BOARDS = {
        'BOARD_TYPE_SKR_PICO_1': {'PIN_FAN': 'unit0:gpio17', 'PIN_HEATER_FAN': 'unit0:gpio18',
                                  'PIN_CONTROLLER_FAN': 'unit0:gpio20'},
        'BOARD_TYPE_EBB_GEN1': {'PIN_FAN': 'unit0:PA0', 'PIN_HEATER_FAN': '',
                                'PIN_CONTROLLER_FAN': 'unit0:PA1'},
        'BOARD_TYPE_WGB_3_0': {'PIN_FAN': 'unit0:PE9', 'PIN_HEATER_FAN': '',
                               'PIN_CONTROLLER_FAN': 'unit0:PB3'},
        'BOARD_TYPE_AFC_PRO_1_0': {'PIN_FAN': 'unit0:PA10', 'PIN_HEATER_FAN': '',
                                   'PIN_CONTROLLER_FAN': ''},
    }

    def test_fan_headers_default_to_their_roles(self):
        for board, expected in self.BOARDS.items():
            with self.subTest(board=board):
                kconfig = _kconfig('fan_pins_' + board.lower(), dict(BOXTURTLE, **{board: True}))
                self.assertTrue(kconfig.is_enabled(board))
                self.assertEqual({sym: kconfig.syms[sym].str_value for sym in expected}, expected)
                self.assertEqual(kconfig.syms['PIN_FAN_SPARE'].str_value, '')

    def test_enabled_fans_render_on_the_board_headers(self):
        parser = cfg.assemble(cfg.render(profiles.Profile('skr_pico_fans', syms=dict(
            BOXTURTLE, BOARD_TYPE_SKR_PICO_1=True, MMU_HAS_ENVIRONMENT_SENSOR=True,
            MMU_HAS_FANS=True, MMU_HAS_CONTROLLER_FAN=True))), macros=False)
        self.assertEqual(dict(parser.items('fan_generic _unit0_fan'))['pin'], 'unit0:gpio17')
        self.assertEqual(dict(parser.items('controller_fan _unit0_controller_fan'))['pin'], 'unit0:gpio20')

    def test_unused_fan_pins_render_nothing(self):
        text = cfg.render(profiles.Profile('skr_pico_no_fans', syms=dict(
            BOXTURTLE, BOARD_TYPE_SKR_PICO_1=True)))['config/base/mmu_hardware.cfg']
        for pin in ('gpio17', 'gpio18', 'gpio20'):
            self.assertNotIn(pin, text)


class TestPerGateBoardFanHeaders(unittest.TestCase):

    def test_ebb_second_header_is_each_gates_controller_fan(self):
        kconfig = _kconfig('ebb_per_gate_fans', profiles.get('emu_ebb').syms)
        self.assertEqual([kconfig.syms['PIN_FAN_%d' % g].str_value for g in range(5)],
                         ['unit0_gate%d:PA0' % g for g in range(5)])
        self.assertEqual([kconfig.syms['PIN_CONTROLLER_FAN_%d' % g].str_value for g in range(5)],
                         ['unit0_gate%d:PA1' % g for g in range(5)])

    def test_slb_second_header_is_each_gates_heater_fan(self):
        parser = cfg.assemble(cfg.render(profiles.get('emu').derive(
            'emu_heater_fans', syms={'MMU_HAS_HEATER': True})), macros=False)
        heater_fans = sorted(s for s in parser.sections() if s.startswith('heater_fan '))
        self.assertEqual([dict(parser.items(s))['pin'] for s in heater_fans],
                         ['unit0_gate%d:PA9' % g for g in range(5)])
        self.assertEqual([dict(parser.items(s))['heater'] for s in heater_fans],
                         ['unit0_heater%d' % g for g in range(5)])


if __name__ == '__main__':
    unittest.main()
