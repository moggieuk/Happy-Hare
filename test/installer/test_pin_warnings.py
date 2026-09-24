"""Required motor pins must produce actionable menuconfig warnings."""

import re
import unittest

import kconfiglib

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

    def test_the_bus_warning_follows_the_chip(self):
        """An SPI chip needs a CS pin, not a UART one, and the reverse."""
        for prefix, warning in (('GEAR', 'W23'), ('SELECTOR', 'W25')):
            for chip, wanted, other in (('TMC2209', 'UART', 'CS'),
                                        ('TMC2130', 'CS', 'UART'),
                                        ('TMC5160', 'CS', 'UART'),
                                        ('TMC2240_SPI', 'CS', 'UART'),
                                        ('TMC2240_UART', 'UART', 'CS')):
                with self.subTest(stepper=prefix, chip=chip):
                    self.set_symbols(**{
                        'CHOICE_%s_%s' % (prefix, chip): True,
                        'PIN_%s_STEP' % prefix: 'mcu:PA1',
                        'PIN_%s_DIR' % prefix: 'mcu:PA2',
                        'PIN_%s_%s' % (prefix, wanted): '',
                        'PIN_%s_%s' % (prefix, other): ''})
                    self.assertTrue(self.enabled(warning),
                                    'no %s pin for a %s' % (wanted, chip))
                    self.set_symbols(**{'PIN_%s_%s' % (prefix, wanted): 'mcu:PC14'})
                    self.assertFalse(self.enabled(warning))
                    # The pin for the bus this chip does not speak is irrelevant
                    self.set_symbols(**{'PIN_%s_%s' % (prefix, other): ''})
                    self.assertFalse(self.enabled(warning))

    def test_software_spi_needs_all_three_pins(self):
        """Any one of the trio means software SPI, so all three must be set."""
        for prefix, warning in (('GEAR', 'W27'), ('SELECTOR', 'W28')):
            trio = ['PIN_%s_SPI_%s' % (prefix, p)
                    for p in ('SCLK', 'MOSI', 'MISO')]
            self.set_symbols(**{'CHOICE_%s_TMC5160' % prefix: True,
                                'PIN_%s_CS' % prefix: 'mcu:PC14'})
            self.set_symbols(**{pin: '' for pin in trio})
            self.assertFalse(self.enabled(warning),
                             'all three blank is hardware SPI, not a mistake')
            for missing in trio:
                with self.subTest(stepper=prefix, pin=missing):
                    self.set_symbols(**{pin: 'mcu:PG%d' % i
                                        for i, pin in enumerate(trio)})
                    self.assertFalse(self.enabled(warning))
                    self.set_symbols(**{missing: ''})
                    self.assertTrue(self.enabled(warning))
            self.set_symbols(**{pin: '' for pin in trio})

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


class TestEveryWarningIsReachable(unittest.TestCase):
    """The visibility gate is a @repeat over a fixed range.

    Nothing ties that range to the warnings that exist, so a warning numbered
    past the end is defined, evaluates correctly, and is never displayed -
    which is exactly what happened to W27 and W28.
    """

    def test_the_gate_covers_every_warning_that_exists(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('warning_gate')
        for gate, prefix in (('SHOW_PER_UNIT_WARNINGS', 'W'),
                             ('SHOW_SHARED_WARNINGS', 'SW')):
            with self.subTest(gate=gate):
                gated = {kconfiglib.expr_str(cond).split(' &&')[0]
                         for _value, cond in kc.syms[gate].defaults}
                defined = {name for name in kc.syms
                           if re.fullmatch(prefix + r'\d+', name)
                           and kc.syms[name].nodes}
                missing = sorted(defined - gated,
                                 key=lambda n: int(n[len(prefix):]))
                self.assertEqual(
                    missing, [],
                    'these warnings can fire but %s will not show the block, '
                    'so raise its @repeat max: %s' % (gate, ', '.join(missing)))
