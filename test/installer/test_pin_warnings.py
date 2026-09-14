"""Required motor pins must produce actionable menuconfig warnings."""

import unittest

from test.hh import cfg


class TestPinWarnings(unittest.TestCase):
    def setUp(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kc = cfg._kconfig('pin_warnings', {
                'MMU_TYPE_TRADRACK_1_0': True,
                'BOARD_TYPE_MANUAL': True,
            })

    def set_symbols(self, **values):
        cfg._apply_syms(self.kc, 'pin_warnings', values)

    def enabled(self, name):
        return self.kc.is_enabled(name)

    def test_manual_and_other_boards_warn_about_pin_setup(self):
        self.assertTrue(self.enabled('W2'))
        self.set_symbols(BOARD_TYPE_OTHER=True)
        self.assertTrue(self.enabled('W2'))
        self.set_symbols(BOARD_TYPE_MMB_2_0=True)
        self.assertFalse(self.enabled('W2'))

    def test_each_required_base_motor_pin_warns_and_clears(self):
        for prefix, motion_warning, uart_warning in (
                ('GEAR', 'W22', 'W23'), ('SELECTOR', 'W24', 'W25')):
            pins = ['PIN_' + prefix + '_' + pin for pin in ('STEP', 'DIR', 'UART')]
            self.set_symbols(**{pin: 'mcu:PA%d' % i for i, pin in enumerate(pins)})
            self.assertFalse(self.enabled(motion_warning))
            self.assertFalse(self.enabled(uart_warning))
            for pin in pins:
                with self.subTest(pin=pin):
                    self.set_symbols(**{pin: ''})
                    warning = uart_warning if pin.endswith('UART') else motion_warning
                    self.assertTrue(self.enabled(warning))
                    self.assertTrue(self.enabled('SHOW_PER_UNIT_WARNINGS'))
                    self.set_symbols(**{pin: 'mcu:PA1'})
                    self.assertFalse(self.enabled(warning))

    def test_optional_pins_and_hardware_controlled_gear(self):
        self.set_symbols(PIN_GEAR_STEP='mcu:PA1', PIN_GEAR_DIR='mcu:PA2',
                         CHOICE_GEAR_TMC_NONE=True,
                         PIN_SELECTOR_STEP='mcu:PB1', PIN_SELECTOR_DIR='mcu:PB2',
                         PIN_SELECTOR_UART='mcu:PB3')
        for warning in ('W22', 'W23', 'W24', 'W25'):
            self.assertFalse(self.enabled(warning))
        self.set_symbols(PIN_GEAR_STEP='')
        self.assertTrue(self.enabled('W22'))

    def test_no_selector_stepper_means_no_selector_pin_warnings(self):
        self.set_symbols(MMU_TYPE_BOX_TURTLE_1_0=True)
        self.assertFalse(self.enabled('MMU_HAS_SELECTOR_STEPPER'))
        self.assertFalse(self.enabled('W24'))
        self.assertFalse(self.enabled('W25'))

    def test_all_active_multigear_pins_and_gate_count_boundary(self):
        self.set_symbols(MMU_TYPE_BOX_TURTLE_1_0=True, PARAM_NUM_GATES=12)
        self.assertTrue(self.enabled('MULTIGEAR'))
        for gate in range(12):
            suffix = '_%d' % gate if gate else ''
            self.set_symbols(**{'PIN_GEAR_' + pin + suffix: 'mcu:PA1'
                                for pin in ('STEP', 'DIR', 'UART')})
        self.assertFalse(self.enabled('W22'))
        self.assertFalse(self.enabled('W23'))
        for gate in range(1, 12):
            for pin, warning in (('STEP', 'W22'), ('DIR', 'W22'), ('UART', 'W23')):
                name = 'PIN_GEAR_%s_%d' % (pin, gate)
                with self.subTest(pin=name):
                    self.set_symbols(**{name: ''})
                    self.assertTrue(self.enabled(warning))
                    self.set_symbols(**{name: 'mcu:PA1'})
                    self.assertFalse(self.enabled(warning))
        self.set_symbols(PARAM_NUM_GATES=4, PIN_GEAR_STEP_4='', PIN_GEAR_UART_4='')
        self.assertFalse(self.enabled('W22'))
        self.assertFalse(self.enabled('W23'))
        self.set_symbols(PARAM_NUM_GATES=5)
        self.assertTrue(self.enabled('W22'))
        self.assertTrue(self.enabled('W23'))
        self.set_symbols(CHOICE_GEAR_TMC_NONE=True)
        self.assertFalse(self.enabled('W23'))


if __name__ == '__main__':
    unittest.main()
