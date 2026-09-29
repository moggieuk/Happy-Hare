# The 'validator' keyword.
#
# A STRING symbol can carry a regexp that menuconfig applies to an edited
# value: to the whole string, or to each element of an 'array_editor' string.

import os
import re
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles


def _parse(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'Kconfig')
        with open(path, 'w') as handle:
            handle.write(text)
        return kconfiglib.Kconfig(path, warn=False)


class TestValidatorParsing(unittest.TestCase):

    def test_pattern_is_compiled_onto_the_symbol(self):
        kc = _parse('config PARAM_TEST\n  string "Test"\n  validator "[(][a-z]+[)]"\n')
        self.assertEqual(kc.syms['PARAM_TEST'].validator.pattern, '[(][a-z]+[)]')

    def test_kconfig_string_escaping_applies_to_the_pattern(self):
        kc = _parse('config PARAM_TEST\n  string "Test"\n  validator "\\\\d+"\n')
        self.assertEqual(kc.syms['PARAM_TEST'].validator.pattern, r'\d+')

    def test_macro_expands_into_the_pattern(self):
        kc = _parse('num := [0-9]+\nconfig PARAM_TEST\n  string "Test"\n'
                    '  validator "$(num)(,$(num))*"\n')
        self.assertEqual(kc.syms['PARAM_TEST'].validator.pattern, '[0-9]+(,[0-9]+)*')

    def test_redeclaration_keeps_the_validator(self):
        kc = _parse('config PARAM_TEST\n  string "Test"\n  validator "[a-z]+"\n'
                    'config PARAM_TEST\n  default "abc"\n')
        self.assertEqual(kc.syms['PARAM_TEST'].validator.pattern, '[a-z]+')

    def test_symbols_without_the_keyword_have_none(self):
        kc = _parse('config PARAM_TEST\n  string "Test"\n')
        self.assertIsNone(kc.syms['PARAM_TEST'].validator)

    def test_invalid_regexp_is_a_parse_error(self):
        with self.assertRaisesRegex(kconfiglib.KconfigError, 'invalid validator regexp'):
            _parse('config PARAM_TEST\n  string "Test"\n  validator "[a-z"\n')

    def test_trailing_tokens_are_a_parse_error(self):
        with self.assertRaises(kconfiglib.KconfigError):
            _parse('config PARAM_TEST\n  string "Test"\n  validator "[a-z]+" "extra"\n')


LED_RGB_SYMS = ('PARAM_WHITE_LIGHT', 'PARAM_BLACK_LIGHT', 'PARAM_EMPTY_LIGHT')


class TestLedEffectValidators(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.trees = {}
        for name in ('boxturtle', 'emu'):
            with cfg._env(cfg._SINGLE_UNIT_ENV):
                cls.trees[name] = cfg._kconfig('led_validators_' + name, dict(
                    profiles.get(name).syms, MMU_HAS_LEDS=True))

    @staticmethod
    def _effect_syms(kc):
        return sorted(name for name in kc.syms if name.startswith('PARAM_EFFECT_'))

    def test_every_customizable_color_and_effect_has_a_validator(self):
        kc = self.trees['boxturtle']
        names = LED_RGB_SYMS + tuple(self._effect_syms(kc))
        self.assertEqual(len(self._effect_syms(kc)), 25)
        self.assertEqual([n for n in names if kc.syms[n].validator is None], [])

    def test_emu_tree_carries_its_effect_overrides(self):
        self.assertTrue(self.trees['emu'].syms['PARAM_EFFECT_GATE_AVAILABLE'].str_value
                        .startswith('mmu_static_white_dim_unit0'))

    def test_shipped_defaults_pass_their_validator(self):
        for profile, kc in self.trees.items():
            for name in LED_RGB_SYMS + tuple(self._effect_syms(kc)):
                sym = kc.syms[name]
                with self.subTest(profile=profile, symbol=name):
                    self.assertIsNotNone(sym.validator.fullmatch(sym.str_value.strip()),
                                         sym.str_value)

    def test_effect_format(self):
        validator = self.trees['boxturtle'].syms['PARAM_EFFECT_LOADING'].validator
        for good in ('mmu_blue, (0, 0, 0.4)', 'Mmu-Blue_2,(1,1,1)',
                     'mmu_rainbow,   (0.5, 0.2, 0),   8', 'mmu_x, (.01, 0, 1.0), 0.8'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('', 'mmu_blue', 'mmu blue, (0, 0, 1)', 'mmu_blue, (0, 0)',
                    'mmu_blue, (0, 0, 1.5)', 'mmu_blue, (0, 0, -1)', 'mmu_blue, 0, 0, 1',
                    'mmu_blue, (0, 0, 1), 8, 9', 'mmu_blue, (0, 0, 1), fast'):
            with self.subTest(value=bad):
                self.assertIsNone(validator.fullmatch(bad))

    def test_rgb_format(self):
        validator = self.trees['boxturtle'].syms['PARAM_WHITE_LIGHT'].validator
        for good in ('(1, 1, 1)', '(.01,0,.02)', '( 0.5 , 1.0 , 0. )'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('1, 1, 1', '(1, 1)', '(2, 0, 0)', '(1, 1, 1), 5', 'white'):
            with self.subTest(value=bad):
                self.assertIsNone(validator.fullmatch(bad))

    def test_color_order_format(self):
        validator = self.trees['boxturtle'].syms['PARAM_COLOR_ORDER'].validator
        for good in ('GRBW', 'grb', 'RGB, GRBW,   RBG,RGB', 'GRB GRBW', 'GRB , GRB'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('', 'GRBX', 'GRB,,GRB', 'GRB,', ',GRB', 'GRB;GRB', 'G-R-B'):
            with self.subTest(value=bad):
                self.assertIsNone(validator.fullmatch(bad))

    def test_every_type_color_order_default_passes(self):
        validator = self.trees['boxturtle'].syms['PARAM_COLOR_ORDER'].validator
        root = os.path.join(os.path.dirname(__file__), '..', '..', 'installer')
        default = re.compile(r'config PARAM_COLOR_ORDER\n(?:[^\n]*\n)*?\s*default "([^"]*)"')
        found = []
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.startswith('Kconfig'):
                    with open(os.path.join(dirpath, name)) as handle:
                        found += default.findall(handle.read())
        self.assertGreaterEqual(len(found), 5)
        for value in found:
            with self.subTest(value=value):
                self.assertIsNotNone(validator.fullmatch(value))


class TestMmuUnitsValidator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')
        with cfg._env(entry_env):
            cls.sym = cfg._kconfig('mmu_units_validator', {}).syms['MMU_UNITS']

    def test_is_an_array_with_a_validator(self):
        self.assertEqual(self.sym.array_editor, ',')
        self.assertIsNotNone(self.sym.validator)

    def test_default_units_pass(self):
        for name in self.sym.str_value.split(','):
            with self.subTest(name=name):
                self.assertIsNotNone(self.sym.validator.fullmatch(name.strip()))

    def test_unit_name_format(self):
        for good in ('unit0', 'u', '_box', 'box-turtle_2', 'a1-b2'):
            with self.subTest(name=good):
                self.assertIsNotNone(self.sym.validator.fullmatch(good))
        for bad in ('', '0unit', '-unit', 'Unit0', 'unit 0', 'unit.0', 'unit:0'):
            with self.subTest(name=bad):
                self.assertIsNone(self.sym.validator.fullmatch(bad))


if __name__ == '__main__':
    unittest.main()
