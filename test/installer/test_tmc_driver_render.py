# Every rendered TMC section must contain only options that chip accepts.
#
# Klipper rejects an unknown option in a driver section outright, so the MMU
# does not come back until the line is deleted by hand. The option sets below
# were read off klippy/extras/tmc*.py, not off our own templates - checking a
# template against itself would prove nothing.
#
# This is the invariant the shared component exists to hold. Before it, the
# gear and selector templates emitted uart_pin and sense_resistor
# unconditionally, so a TMC5160, TMC2130 or TMC2660 rendered a section that
# could not boot, and a TMC2240 got sense_resistor when it takes rref.

import unittest

from test.hh import cfg, profiles

HARDWARE = 'config/base/mmu_hardware.cfg'
MMU = 'config/base/mmu.cfg'

# Options each Klipper driver module accepts, limited to the ones these
# templates can emit.
ACCEPTED = {
    'tmc2209': {'uart_pin', 'uart_address', 'run_current', 'hold_current',
                'interpolate', 'sense_resistor', 'stealthchop_threshold',
                'diag_pin', 'driver_SGTHRS'},
    'tmc2208': {'uart_pin', 'uart_address', 'run_current', 'hold_current',
                'interpolate', 'sense_resistor', 'stealthchop_threshold'},
    'tmc2130': {'cs_pin', 'spi_bus', 'spi_software_sclk_pin',
                'spi_software_mosi_pin', 'spi_software_miso_pin',
                'run_current', 'hold_current', 'interpolate',
                'sense_resistor', 'stealthchop_threshold', 'diag_pin',
                'driver_SGT'},
    # No hold_current and no stealthchop: spreadCycle only, idles via
    # idle_current_percent. No virtual endstop either, so no touch homing.
    'tmc2660': {'cs_pin', 'spi_bus', 'spi_software_sclk_pin',
                'spi_software_mosi_pin', 'spi_software_miso_pin',
                'run_current', 'interpolate', 'sense_resistor'},
    'tmc5160': {'cs_pin', 'spi_bus', 'spi_software_sclk_pin',
                'spi_software_mosi_pin', 'spi_software_miso_pin',
                'run_current', 'hold_current', 'interpolate',
                'sense_resistor', 'stealthchop_threshold', 'diag_pin',
                'driver_SGT'},
    # The only chip that takes either bus - tmc2240.py:352 picks UART when
    # uart_pin is set and SPI otherwise. rref, not sense_resistor: it sizes
    # current from a reference resistor.
    'tmc2240': {'cs_pin', 'spi_bus', 'spi_software_sclk_pin',
                'spi_software_mosi_pin', 'spi_software_miso_pin',
                'uart_pin', 'uart_address',
                'run_current', 'hold_current', 'interpolate', 'rref',
                'stealthchop_threshold', 'diag_pin', 'driver_SG4_THRS',
                'driver_SLOPE_CONTROL'},
}

# The bus is part of the chip choice, so the dual-bus TMC2240 has two.
CHIPS = ('TMC2209', 'TMC2226', 'TMC2208', 'TMC2130', 'TMC2660', 'TMC5160',
         'TMC2240_SPI', 'TMC2240_UART')

# stepper -> (profile, symbol prefix, rendered file, section suffix)
STEPPERS = {
    'gear': ('boxturtle', 'GEAR', HARDWARE, '_gear'),
    'selector': ('tradrack', 'SELECTOR', HARDWARE, '_selector'),
    'blobifier': ('boxturtle', 'BLOBIFIER', MMU, 'stepper_blobifier'),
}


class TestRenderedDriverSectionsAreValid(unittest.TestCase):

    def _sections(self, stepper, chip):
        profile_name, prefix, path, suffix = STEPPERS[stepper]
        syms = {'CHOICE_%s_%s' % (prefix, chip): True,
                'PIN_%s_CS' % prefix: 'mmu:PC14'}
        if stepper == 'blobifier':
            syms.update({'MMU_HAS_BLOBIFIER': True,
                         'CHOICE_BLOBIFIER_TYPE_STEPPER': True})
        rendered = cfg.render(profiles.get(profile_name).derive(
            'driver_%s_%s' % (stepper, chip), syms=syms))[path]
        parsed = cfg.assemble({path: rendered})
        return [(name, dict(parsed.items(name)))
                for name in cfg.sections(rendered)
                if name.startswith('tmc') and name.endswith(suffix)]

    def test_no_section_carries_an_option_its_chip_rejects(self):
        for stepper in sorted(STEPPERS):
            for chip in CHIPS:
                with self.subTest(stepper=stepper, chip=chip):
                    sections = self._sections(stepper, chip)
                    self.assertTrue(
                        sections, 'no driver section rendered at all')
                    for name, options in sections:
                        module = name.split()[0]
                        rejected = set(options) - ACCEPTED[module]
                        self.assertEqual(
                            rejected, set(),
                            '[%s] would fail at Klipper boot on: %s'
                            % (name, ', '.join(sorted(rejected))))

    def test_the_bus_matches_the_chip(self):
        """A UART-only chip must never get cs_pin, and vice versa."""
        uart_only = {'TMC2209', 'TMC2226', 'TMC2208', 'TMC2240_UART'}
        spi_only = {'TMC2130', 'TMC2660', 'TMC5160', 'TMC2240_SPI'}
        for stepper in sorted(STEPPERS):
            for chip in CHIPS:
                with self.subTest(stepper=stepper, chip=chip):
                    for name, options in self._sections(stepper, chip):
                        if chip in uart_only:
                            self.assertIn('uart_pin', options)
                            self.assertNotIn('cs_pin', options)
                        elif chip in spi_only:
                            self.assertIn('cs_pin', options)
                            self.assertNotIn('uart_pin', options)

    def test_the_stallguard_field_matches_the_chip(self):
        """The field name differs per family, and so does its scale."""
        expected = {'TMC2209': 'driver_SGTHRS', 'TMC2226': 'driver_SGTHRS',
                    'TMC2130': 'driver_SGT', 'TMC5160': 'driver_SGT',
                    'TMC2240_SPI': 'driver_SG4_THRS',
                    'TMC2240_UART': 'driver_SG4_THRS'}
        for stepper in ('gear', 'selector'):
            _, prefix, path, suffix = STEPPERS[stepper]
            for chip in CHIPS:
                with self.subTest(stepper=stepper, chip=chip):
                    profile_name = STEPPERS[stepper][0]
                    syms = {'CHOICE_%s_%s' % (prefix, chip): True,
                            'PIN_%s_CS' % prefix: 'mmu:PC14',
                            'PIN_%s_DIAG' % prefix: 'mmu:PD2'}
                    rendered = cfg.render(profiles.get(profile_name).derive(
                        'sg_%s_%s' % (stepper, chip), syms=syms))[path]
                    parsed = cfg.assemble({path: rendered})
                    name = [n for n in cfg.sections(rendered)
                            if n.startswith('tmc') and n.endswith(suffix)][0]
                    options = dict(parsed.items(name))
                    field = expected.get(chip)
                    if field is None:
                        # No stallguard on this chip, so no diag pin either
                        self.assertNotIn('diag_pin', options)
                    else:
                        self.assertIn(field, options)


class TestNoDriverMeansNoDriverSection(unittest.TestCase):
    """A stepper set to no TMC at all must leave nothing behind.

    The gear template wraps its whole driver block in PARAM_GEAR_TMC != "";
    the selector one did not, so it emitted a section header with an empty
    type and a touch endstop pointing at a driver that was never written.
    Klipper rejects both.
    """

    HARDWARE = 'config/base/mmu_hardware.cfg'

    CASES = {
        'gear': ('boxturtle', 'GEAR', 'mmu_gear_touch'),
        'selector': ('tradrack', 'SELECTOR', 'mmu_sel_touch'),
    }

    def test_nothing_references_a_driver_that_was_not_written(self):
        for stepper, (profile_name, prefix, touch) in sorted(self.CASES.items()):
            with self.subTest(stepper=stepper):
                rendered = cfg.render(profiles.get(profile_name).derive(
                    'no_tmc_' + stepper,
                    syms={'CHOICE_%s_TMC_NONE' % prefix: True,
                          'PIN_%s_DIAG' % prefix: 'mmu:PD2'}))[self.HARDWARE]
                for name in cfg.sections(rendered):
                    self.assertFalse(
                        name.startswith(' '),
                        'section [%s] has an empty driver type' % name)
                live = [ln for ln in rendered.splitlines()
                        if touch in ln and not ln.lstrip().startswith('#')]
                self.assertEqual(
                    live, [],
                    '%s points at a driver section that is not rendered: %s'
                    % (touch, live))


if __name__ == '__main__':
    unittest.main()
