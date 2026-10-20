"""TMC policy parsing and refresh behavior, including inherited gear options."""
import os
import tempfile
import unittest

from installer import build
from installer.parser import ConfigBuilder
from test.hh import cfg, profiles
from test.installer.test_build import _HardwareRefresh


class TestTmcPolicies(unittest.TestCase):
    def policies(self, path='config/base/mmu_hardware.cfg'):
        with cfg._chdir(cfg.REPO_ROOT):
            return build.template_managed_options(path)

    def test_both_templates_share_the_same_explicit_driver_policies(self):
        hardware = {k: v for k, v in self.policies().items() if k.startswith('tmc')}
        self.assertEqual(set(hardware), {'tmc2208', 'tmc2209', 'tmc2130',
                                        'tmc5160', 'tmc2660', 'tmc2240'})
        self.assertEqual(hardware, self.policies('config/base/mmu.cfg'))
        for driver in ('tmc2660', 'tmc2240'):
            self.assertNotIn('driver_SGT', hardware[driver])
        self.assertNotIn('driver_SLOPE_CONTROL', hardware['tmc2240'])

    def test_managed_lists_cover_all_generated_driver_options(self):
        from test.installer.test_tmc_driver_render import ACCEPTED
        policies = self.policies()
        for driver, options in ACCEPTED.items():
            with self.subTest(driver=driver):
                self.assertEqual(policies[driver], options - {'driver_SLOPE_CONTROL'})

    def refresh(self, section, extra, emitted='uart_pin: PA1\n', changes=()):
        old = build.HHConfig([])
        old.read_buf('[%s]\n%s%s' % (section, emitted, extra))
        new = ConfigBuilder()
        new.read_buf('[%s]\n%s' % (section, emitted))
        old.update_builder(new, kconfig_keys=changes, managed_options=self.policies())
        return new

    def test_custom_registers_survive_but_managed_thresholds_do_not(self):
        for driver, field in [('tmc2209', 'driver_SGTHRS'), ('tmc2130', 'driver_SGT'),
                              ('tmc5160', 'driver_SGT'), ('tmc2240', 'driver_SG4_THRS')]:
            for motor in ('mmu_stepper unit0_gear', 'manual_stepper stepper_blobifier'):
                section = driver + ' ' + motor
                with self.subTest(section=section):
                    new = self.refresh(section, '%s: 42\ndriver_TOFF: 4\n' % field)
                    self.assertFalse(new.has_option(section, field))
                    self.assertEqual(new.get(section, 'driver_TOFF'), '4')

    def test_unmanaged_sgt_and_slope_survive(self):
        for driver in ('tmc2660', 'tmc2240'):
            section = driver + ' manual_stepper stepper_blobifier'
            new = self.refresh(section, 'driver_SGT: 5\n')
            self.assertEqual(new.get(section, 'driver_SGT'), '5')
        new = self.refresh(section, 'driver_SLOPE_CONTROL: 2\n')
        self.assertEqual(new.get(section, 'driver_SLOPE_CONTROL'), '2')

    def test_additional_gears_keep_manual_overrides_except_explicit_removals(self):
        section = 'tmc2209 mmu_stepper unit0_gear_1'
        extra = 'run_current: 0.8\nstealthchop_threshold: 12\ndriver_SGTHRS: 42\n'
        new = self.refresh(section, extra, changes={(section, 'run_current')})
        self.assertFalse(new.has_option(section, 'run_current'))
        self.assertEqual(new.get(section, 'stealthchop_threshold'), '12')
        self.assertEqual(new.get(section, 'driver_SGTHRS'), '42')

    def test_unlisted_driver_and_removed_sections_are_not_opted_in(self):
        section = 'tmc9999 mmu_stepper unit0_gear'
        self.assertFalse(self.refresh(section, 'custom: 1\n').has_option(section, 'custom'))
        old = build.HHConfig([])
        old.read_buf('[tmc2209 mmu_stepper unit0_gear]\nuart_pin: PA1\ncustom: 1\n')
        new = ConfigBuilder()
        old.update_builder(new, managed_options=self.policies())
        self.assertEqual(list(new.sections()), [])


class TestGroupedDeclarations(unittest.TestCase):
    def parse(self, source, component=''):
        with tempfile.TemporaryDirectory() as root:
            for name, text in [('template.cfg', source), ('component.cfg', component)]:
                with open(os.path.join(root, name), 'w') as f:
                    f.write(text)
            with cfg._chdir(root):
                return build.template_managed_options('template.cfg')

    def test_multiple_selectors_and_exact_multiword_name(self):
        policies = self.parse('[# @managed-options tmc2130, tmc5160: cs_pin, driver_SGT #]\n'
                              '[# @managed-options tmc2130 mmu_stepper unit0_gear: cs_pin #]')
        self.assertEqual(policies['tmc2130'], policies['tmc5160'])
        self.assertEqual(policies['tmc2130 mmu_stepper unit0_gear'], {'cs_pin'})

    def test_static_includes_are_read_even_in_hidden_branches(self):
        policies = self.parse('[% if false %][% include "component.cfg" %][% endif %]',
                              '[# @managed-options tmc2130, tmc5160: cs_pin #]')
        self.assertEqual(set(policies), {'tmc2130', 'tmc5160'})

    def test_repeated_includes_are_read_once(self):
        self.assertEqual(self.parse('[% include "component.cfg" %]' * 2,
                                    '[# @managed-options tmc2130: cs_pin #]'),
                         {'tmc2130': {'cs_pin'}})

    def test_duplicate_or_invalid_selectors_are_rejected(self):
        for source in ('[# @managed-options tmc2130, tmc2130: cs_pin #]',
                       '[# @managed-options tmc*: cs_pin #]',
                       '[# @managed-options tmc2130,: cs_pin #]',
                       '[# @managed-options tmc2130: cs_pin #][% include "component.cfg" %]'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.parse(source, '[# @managed-options tmc2130: cs_pin #]')


class TestTmcRefresh(_HardwareRefresh):
    def test_manual_gate_overrides_survive_refresh_and_merge(self):
        installed = self.build('boxturtle', {}, 'replace', [])
        section = 'tmc2209 mmu_stepper unit0_gear_1'
        old = ConfigBuilder(installed)
        old.set(section, 'run_current', '0.8')
        old.set(section, 'driver_SGTHRS', '42')
        old.set(section, 'driver_TOFF', '4')
        with open(installed, 'w') as f:
            f.write(old.write())
        for mode in ('refresh', 'merge'):
            with self.subTest(mode=mode):
                new = ConfigBuilder(self.build('boxturtle', {}, mode, [installed]))
                for option in ('run_current', 'driver_SGTHRS', 'driver_TOFF'):
                    self.assertEqual(new.get(section, option), old.get(section, option))

    def test_resetting_a_gate_override_restores_inheritance(self):
        syms = {'BOOL_GEAR_OVERRIDE_1': True, 'PARAM_GEAR_RUN_CURRENT_1': '0.8'}
        installed = self.build('boxturtle', syms, 'replace', [])
        section = 'tmc2209 mmu_stepper unit0_gear_1'
        self.assertEqual(ConfigBuilder(installed).get(section, 'run_current'), '0.8')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            baseline = cfg._kconfig('gate-before', dict(profiles.get('boxturtle').syms, **syms))
        new = ConfigBuilder(self.build('boxturtle', {'BOOL_GEAR_OVERRIDE_1': False},
                                       'refresh', [installed], baseline))
        self.assertFalse(new.has_option(section, 'run_current'))

    def test_blobifier_slope_survives_refresh_but_is_not_generated(self):
        syms = dict(profiles.get('boxturtle').syms, MMU_HAS_BLOBIFIER=True,
                    CHOICE_BLOBIFIER_TYPE_STEPPER=True, CHOICE_BLOBIFIER_TMC2240_SPI=True,
                    PIN_BLOBIFIER_CS='unit0:PC14')
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            kconfig = cfg._kconfig('blobifier-slope', syms)
        section = 'tmc2240 manual_stepper stepper_blobifier'
        source = os.path.join(self.tmp.name, 'mmu.cfg')
        with open(source, 'w') as f:
            f.write('[%s]\ndriver_SLOPE_CONTROL: 2\ndriver_SG4_THRS: 42\n' % section)
        for mode in ('replace', 'refresh', 'merge'):
            out = tempfile.mkdtemp(dir=self.tmp.name)
            dest = os.path.join(out, 'mmu.cfg')
            with cfg._env(dict(cfg._SINGLE_UNIT_ENV, OUT=out, F_CFG_UPGRADE_MODE=mode)), \
                    cfg._chdir(cfg.REPO_ROOT):
                build.build_config_file('config/base/mmu.cfg', dest, kconfig, [source],
                                        {'PARAM_TOTAL_NUM_GATES': kconfig.getint('PARAM_NUM_GATES')})
            new = ConfigBuilder(dest)
            with self.subTest(mode=mode):
                self.assertFalse(new.has_option(section, 'driver_SG4_THRS'))
                if mode == 'replace':
                    self.assertFalse(new.has_option(section, 'driver_SLOPE_CONTROL'))
                else:
                    self.assertEqual(new.get(section, 'driver_SLOPE_CONTROL'), '2')
