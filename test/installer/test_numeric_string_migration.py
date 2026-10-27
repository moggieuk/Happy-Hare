"""Numeric Kconfig edits must retain values saved by the former string type."""
import os
import tempfile
import unittest
from unittest import mock

import kconfiglib
import menuconfig
from test.hh import cfg, profiles

# Explicit expectations, independent of the loader's migration allowlist.
VALUES = {
    'PARAM_HEATER_MAX_TEMP': '72.50',
    'PARAM_HEATER_DEFAULT_DRY_TEMP': '47.25',
    'PARAM_HEATER_DEFAULT_DRY_TIME': '120.5',
    'PARAM_HEATER_DEFAULT_DRY_HUMIDITY': '18.75',
    'PARAM_HEATER_PAUSE_TEMP': '-1',
    'PARAM_HEATER_ROTATE_INTERVAL': '7.5',
    'PARAM_HEATER_VENT_INTERVAL': '0',
    'PARAM_TD1_CAPTURE_TIMEOUT': '2.75',
    'PARAM_SLICER_TIP_PARK_POS': '12.5',
}


class TestQuotedFloatMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, '.mmu_config')
        tree = os.path.join(self.tmp.name, 'Kconfig')
        with open(tree, 'w') as f:
            for name in list(VALUES) + ['PARAM_UNRELATED_FLOAT']:
                f.write('config %s\n  float "%s"\n  default 3.5\n' % (name, name))
        self.tree = tree
        self.kc = kconfiglib.Kconfig(tree, warn_to_stderr=False)

    def load(self, values=VALUES, marker='', filter_defaults=False, replace=True):
        with open(self.path, 'w') as f:
            for name, value in values.items():
                f.write('CONFIG_%s="%s"%s\n' % (name, value, marker))
        self.kc.load_config(self.path, filter_defaults=filter_defaults, replace=replace)

    def test_quoted_customizations_survive_both_load_modes(self):
        for filtering in (False, True):
            for replace in (False, True):
                with self.subTest(filtering=filtering, replace=replace):
                    self.kc = kconfiglib.Kconfig(self.tree, warn_to_stderr=False)
                    self.load(filter_defaults=filtering, replace=replace)
                    self.assertEqual({n: self.kc.syms[n].user_value for n in VALUES}, VALUES)
                    self.assertEqual(self.kc.warnings, [])

    def test_recorded_defaults_remain_resettable(self):
        self.load(marker=' #~DEFAULT~#', filter_defaults=True)
        for name in VALUES:
            self.assertIsNone(self.kc.syms[name].user_value)
            self.assertTrue(self.kc.syms[name]._was_default)
            self.assertEqual(self.kc.syms[name].str_value, '3.5')
        self.load(marker=' #~DEFAULT~#', filter_defaults=False)
        self.assertEqual({n: self.kc.syms[n].user_value for n in VALUES}, VALUES)

    def test_saved_float_format_reloads_without_changing_values(self):
        self.load()
        self.kc.write_config(self.path)
        with open(self.path) as f:
            saved = f.read()
        for name, value in VALUES.items():
            self.assertIn('CONFIG_%s=%s\n' % (name, value), saved)
        self.kc.load_config(self.path, filter_defaults=True)
        self.assertEqual({n: self.kc.syms[n].user_value for n in VALUES}, VALUES)

    def test_numeric_spellings_keep_their_precision_and_sentinels(self):
        for value in ('-1', '-0.5', '0', '0.125', '7.2500', '1e3', '+4.5', ' 45.5 '):
            with self.subTest(value=value):
                self.load({'PARAM_HEATER_PAUSE_TEMP': value})
                self.assertEqual(self.kc.syms['PARAM_HEATER_PAUSE_TEMP'].user_value, value)

    def test_invalid_legacy_values_warn_instead_of_bypassing_float_validation(self):
        for value in ('warm', '', 'nan', 'inf'):
            with self.subTest(value=value):
                self.load({'PARAM_HEATER_PAUSE_TEMP': value})
                self.assertIsNone(self.kc.syms['PARAM_HEATER_PAUSE_TEMP'].user_value)
                self.assertTrue(self.kc.warnings)

    def test_unrelated_floats_do_not_gain_quoted_input(self):
        self.load({'PARAM_UNRELATED_FLOAT': '9.5'})
        self.assertIsNone(self.kc.syms['PARAM_UNRELATED_FLOAT'].user_value)
        self.assertTrue(self.kc.warnings)


class TestShippedNumericOptions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kc = cfg._kconfig('numeric_options', dict(profiles.get('qidi').syms,
                MMU_HAS_ESPOOLER=True, MMU_HAS_VENT_SERVO=True,
                MMU_HAS_TD1=True, PARAM_FORCE_FORM_TIP_STANDALONE=0))

    def test_numeric_options_reject_text_and_accept_fractional_values(self):
        for name, value in VALUES.items():
            with self.subTest(name=name):
                sym = self.kc.syms[name]
                self.assertIs(sym.orig_type, kconfiglib.FLOAT)
                with mock.patch.object(menuconfig, '_error'):
                    self.assertFalse(menuconfig._check_valid(sym, 'warm'))
                    self.assertTrue(menuconfig._check_valid(sym, value))

    def test_vent_angles_match_existing_runtime_bounds(self):
        for name in ('PARAM_HEATER_VENT_OPEN_ANGLE', 'PARAM_HEATER_VENT_CLOSE_ANGLE'):
            with self.subTest(name=name), mock.patch.object(menuconfig, '_error'):
                sym = self.kc.syms[name]
                for value in ('0', '90.5', '360'):
                    self.assertTrue(menuconfig._check_valid(sym, value))
                for value in ('-0.1', '360.1'):
                    self.assertFalse(menuconfig._check_valid(sym, value))

    def test_legacy_file_survives_real_builder_dictionary_and_render(self):
        with tempfile.TemporaryDirectory() as tmp, cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._kconfig('numeric_legacy', profiles.get('qidi').syms)
            path = os.path.join(tmp, '.mmu_config')
            kc.write_config(path)
            with open(path, 'a') as f:
                for name, value in VALUES.items():
                    f.write('CONFIG_%s="%s"\n' % (name, value))
            kc.load_config(path, filter_defaults=False)
            for name, value in VALUES.items():
                self.assertEqual(kc.as_dict()[name], value)
            rendered = cfg._render_templates(cfg.BASE_TEMPLATES, kc, {'PARAM_TOTAL_NUM_GATES': 4})
            parsed = cfg.assemble(rendered, macros=False)
            for name, value in VALUES.items():
                if name.startswith('PARAM_HEATER_'):
                    self.assertEqual(parsed.get('mmu_unit_parameters unit0', name[6:].lower()), value)

    def test_custom_heater_values_render_and_boot(self):
        from test.hh import session
        profile = profiles.get('qidi').derive('numeric_heater', syms={
            name: value for name, value in VALUES.items() if name.startswith('PARAM_HEATER_')})
        with session(profile) as hh:
            hh.boot()
            params = hh.mmu.mmu_unit(0).p
            for name, value in VALUES.items():
                if name.startswith('PARAM_HEATER_'):
                    self.assertEqual(getattr(params, name[6:].lower()), float(value))
            self.assertEqual(hh.errors, [])
