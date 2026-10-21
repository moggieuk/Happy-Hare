# The 'validator' keyword.
#
# A STRING symbol can carry a regexp that menuconfig applies to an edited
# value: to the whole string, or to each element of an 'array_editor' string.

import glob
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

    def test_macros_expand_in_help_text(self):
        kc = _parse('syntax := Syntax: x\n'
                    'config PARAM_TEST\n  string "Test"\n  help\n    $(syntax)\n')
        self.assertEqual(kc.syms['PARAM_TEST'].nodes[0].help, 'Syntax: x')

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
        for good in ('', 'mmu_blue', 'Mmu-Blue_2', 'mmu_blue, (0, 0, 0.4)', 'Mmu-Blue_2,(1,1,1)',
                     'mmu_rainbow,   (0.5, 0.2, 0),   8', 'mmu_x, (.01, 0, 1.0), 0.8'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('mmu blue', 'mmu_blue,', 'mmu_blue, 8', 'mmu blue, (0, 0, 1)', ', (0, 0, 1)',
                    'mmu_blue, (0, 0)',
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


class TestUnitNamePrompt(unittest.TestCase):
    """The Klipper object name is editable for a single unit only."""

    def parse(self, **env):
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV, **env)):
            return cfg._kconfig('unit_name_prompt', {})

    def test_single_unit_can_name_itself_when_restructuring_is_allowed(self):
        # install.sh allows it in Replace mode or before anything is installed
        sym = self.parse(F_UNITS_RESTRUCTURE='y').syms['UNIT_NAME']
        self.assertEqual(sym.visibility, 2)
        self.assertEqual(sym.reparse_env, ['UNIT_NAME', 'MCU_NAME'])
        self.assertIsNotNone(sym.validator)
        self.assertIsNotNone(sym.validator.fullmatch('box'))
        self.assertIsNone(sym.validator.fullmatch('Box'))

    def test_installed_single_unit_name_is_read_only_outside_replace(self):
        import kconfiglib
        kc = self.parse(UNIT_NAME='box', MCU_NAME='box')
        self.assertEqual(kc.syms['UNIT_NAME'].visibility, 0)
        self.assertEqual(kc.syms['UNIT_NAME'].str_value, 'box')
        shown = [n.prompt[0] for n in kc.node_iter()
                 if n.item == kconfiglib.COMMENT and n.prompt
                 and kconfiglib.expr_value(n.prompt[1]) and 'Klipper object name' in n.prompt[0]]
        self.assertEqual(shown, ["Klipper object name: box  (rename with './install.sh -i', REPLACE mode)"])

    def test_multi_unit_name_comes_from_the_unit_list(self):
        import kconfiglib
        kc = self.parse(F_MULTI_UNIT='y', UNIT_NAME='box', MCU_NAME='box', UNIT_INDEX='1')
        self.assertEqual(kc.syms['UNIT_NAME'].visibility, 0)
        self.assertEqual(kc.syms['UNIT_NAME'].str_value, 'box')
        shown = [n.prompt[0] for n in kc.node_iter()
                 if n.item == kconfiglib.COMMENT and n.prompt
                 and kconfiglib.expr_value(n.prompt[1]) and 'Klipper object name' in n.prompt[0]]
        self.assertEqual(shown, ['Klipper object name: box  (set in the MMU units list)'])


class TestMmuUnitsValidator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')
        with cfg._env(entry_env):
            cls.sym = cfg._kconfig('mmu_units_validator', {}).syms['MMU_UNITS']

    def test_restructuring_follows_the_install_mode(self):
        import kconfiglib
        self.assertEqual(kconfiglib.expr_value(self.sym.append_only_unless), 0)
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1', F_UNITS_RESTRUCTURE='y')
        with cfg._env(entry_env):
            sym = cfg._kconfig('mmu_units_restructure', {}).syms['MMU_UNITS']
        self.assertEqual(kconfiglib.expr_value(sym.append_only_unless), 2)

    def test_is_a_sequence_with_a_validator(self):
        self.assertEqual(self.sym.sequence_editor, ',')
        self.assertIsNone(self.sym.array_editor)
        self.assertIsNotNone(self.sym.append_only_unless)
        self.assertIsNotNone(self.sym.validator)

    def test_default_units_pass(self):
        for name in self.sym.str_value.split(','):
            with self.subTest(name=name):
                self.assertIsNotNone(self.sym.validator.fullmatch(name.strip()))

    def test_unit_name_format(self):
        for good in ('unit0', 'u', 'box-turtle_2', 'a1-b2', 'a_'):
            with self.subTest(name=good):
                self.assertIsNotNone(self.sym.validator.fullmatch(good))
        for bad in ('', '0unit', '-unit', '_box', 'Unit0', 'unit 0', 'unit.0', 'unit:0'):
            with self.subTest(name=bad):
                self.assertIsNone(self.sym.validator.fullmatch(bad))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPinValidator(unittest.TestCase):

    # Pins that take a comma-separated list, validated by pin_list_validator
    FAN_PINS = ('PIN_HEATER_FAN', 'PIN_FAN', 'PIN_CONTROLLER_FAN')
    LIST_PINS = ({'PIN_FAN_SPARE'} | set(FAN_PINS)
                 | {'%s_%d' % (pin, gate) for pin in FAN_PINS for gate in range(12)})

    @classmethod
    def setUpClass(cls):
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')
        cls.trees = []
        for label, env in (('unit', cfg._SINGLE_UNIT_ENV), ('entry', entry_env)):
            with cfg._env(env):
                cls.trees.append(cfg._kconfig('pin_validator_' + label, {}))
        cls.kc = cls.trees[0]
        cls.pattern = cls.kc.variables['pin_validator'].value
        cls.validator = re.compile(cls.pattern)
        cls.list_pattern = cls.kc.variables['pin_list_validator'].value
        cls.list_validator = re.compile(cls.list_pattern)

    def _prompted_pins(self):
        return [sym for kc in self.trees for sym in kc.unique_defined_syms
                if sym.name.startswith('PIN_') and any(node.prompt for node in sym.nodes)]

    def test_every_prompted_pin_has_the_shared_validator(self):
        pins = self._prompted_pins()
        self.assertGreater(len(pins), 100)
        expected = lambda sym: self.list_pattern if sym.name in self.LIST_PINS else self.pattern
        self.assertEqual([sym.name for sym in pins
                          if sym.validator is None or sym.validator.pattern != expected(sym)], [])

    def test_every_pin_prompt_has_help(self):
        missing = ['%s:%d' % (node.filename, node.linenr)
                   for sym in self._prompted_pins() for node in sym.nodes
                   if node.prompt and not node.help]
        self.assertEqual(missing, [])

    def test_every_pin_help_fits_the_help_window(self):
        too_long = {'%s:%d' % (node.filename, node.linenr)
                    for sym in self._prompted_pins() for node in sym.nodes
                    if node.prompt and node.help and len(node.help.split('\n')) > 7}
        self.assertEqual(sorted(too_long), [])

    def test_shared_help_is_expanded(self):
        help_text = self.kc.syms['PIN_GEAR_STEP'].nodes[-1].help
        self.assertEqual(help_text.split('\n'), [self.kc.variables['pin_syntax'].value,
                                                  self.kc.variables['pin_example'].value])
        self.assertTrue(help_text.startswith('Syntax:'), help_text)

    def test_examples_in_help_pass_the_validator(self):
        # [[VALUE:UNIT_NAME]] is the live unit name in menuconfig
        example = self.kc.variables['pin_example'].value.replace('[[VALUE:UNIT_NAME]]', 'box')
        examples = example.split(':', 1)[1].split(',')
        self.assertGreater(len(examples), 0)
        for example in examples:
            with self.subTest(example=example):
                self.assertIsNotNone(self.validator.fullmatch(example.strip()))

    def test_pin_format(self):
        for good in ('', 'PA1', 'unit0:PA1', '^unit0:PB7', '~PA1', '!PA1', '^!unit0_gate0:PB6',
                     'mmu-1:gpio12', 'P1.29', 'host:gpiochip0/gpio4', 'EXP1_1'):
            with self.subTest(pin=good):
                self.assertIsNotNone(self.validator.fullmatch(good))
        for bad in ('unit0:^PA1', '!^PA1', '^~PA1', '^^PA1', 'unit0:', ':PA1', 'a:b:PA1',
                    'unit0 PA1', '^(MCU_NAME):PB7', 'PA1,PA2'):
            with self.subTest(pin=bad):
                self.assertIsNone(self.validator.fullmatch(bad))

    def test_every_shipped_literal_pin_default_passes(self):
        macros = {'MCU_NAME': 'unit0', 'UNIT_NAME': 'unit0', 'i': '0', 'gate': '0'}
        failures = []
        checked = 0
        for path in glob.glob(os.path.join(REPO_ROOT, 'installer', '**', 'Kconfig*'), recursive=True):
            sym = None
            with open(path) as handle:
                for linenr, line in enumerate(handle, 1):
                    match = re.match(r'\s*config (PIN_\S+)', line)
                    if match:
                        sym = match.group(1)
                        continue
                    if re.match(r'\s*(config|menuconfig|menu|choice|if|endif|endmenu|comment)\b', line):
                        sym = None
                    match = sym and re.match(r'\s*default\s+"([^"]*)"', line)
                    if match:
                        checked += 1
                        value = re.sub(r'\$\((\w+)\)', lambda m: macros.get(m.group(1), m.group(0)),
                                       match.group(1))
                        validator = self.list_validator if sym in self.LIST_PINS else self.validator
                        if not validator.fullmatch(value.strip()):
                            failures.append('%s:%d %s = "%s"' % (
                                os.path.relpath(path, REPO_ROOT), linenr, sym, match.group(1)))
        self.assertGreater(checked, 500)
        self.assertEqual(failures, [])

    def test_every_profile_pin_value_passes(self):
        failures = set()
        for name, profile in sorted(profiles.PROFILES.items()):
            with cfg._env(cfg._SINGLE_UNIT_ENV):
                kc = cfg._kconfig('pin_validator_' + name, profile.syms)
            for sym in kc.unique_defined_syms:
                validator = self.list_validator if sym.name in self.LIST_PINS else self.validator
                if sym.name.startswith('PIN_') and not validator.fullmatch(sym.str_value.strip()):
                    failures.add((name, sym.name, sym.str_value))
        self.assertEqual(sorted(failures), [])


class TestConsoleStatValidators(unittest.TestCase):

    COLUMNS = ('pre_unload', 'form_tip', 'unload', 'post_unload', 'pre_load', 'load', 'purge',
               'post_load', 'total')
    ROWS = ('total', 'total_average', 'job', 'job_average', 'last')

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kc = cfg._kconfig('console_stat_validators', profiles.get('boxturtle').syms)

    def _check(self, name, good, bad):
        sym = self.kc.syms[name]
        self.assertEqual(sym.array_editor, ',')
        for value in good:
            with self.subTest(symbol=name, value=value):
                self.assertIsNotNone(sym.validator.fullmatch(value))
        for value in bad:
            with self.subTest(symbol=name, value=value):
                self.assertIsNone(sym.validator.fullmatch(value))

    def test_columns(self):
        self._check('PARAM_CONSOLE_STAT_COLUMNS', self.COLUMNS,
                    ('', 'swap', 'Total', 'total_average', 'unload load', 'loads'))

    def test_rows(self):
        self._check('PARAM_CONSOLE_STAT_ROWS', self.ROWS,
                    ('', 'unload', 'Last', 'job average', 'jobs'))

    def test_menuconfig_rejects_an_unknown_token_in_the_list(self):
        import menuconfig
        from unittest.mock import patch
        sym = self.kc.syms['PARAM_CONSOLE_STAT_COLUMNS']
        with patch.object(menuconfig, '_error') as error:
            self.assertTrue(menuconfig._check_valid(sym, 'unload, load'))
            error.assert_not_called()
            self.assertFalse(menuconfig._check_valid(sym, 'unload, swap'))
        self.assertIn("Element 2 'swap'", error.call_args.args[0])

    def test_shipped_defaults_pass_per_element(self):
        for name in ('PARAM_CONSOLE_STAT_COLUMNS', 'PARAM_CONSOLE_STAT_ROWS'):
            sym = self.kc.syms[name]
            for element in sym.str_value.split(sym.array_editor):
                with self.subTest(symbol=name, element=element):
                    self.assertIsNotNone(sym.validator.fullmatch(element.strip()))


class TestSequencePositionValidators(unittest.TestCase):

    PARKS = tuple('VAR_SEQUENCE_PARK_' + n for n in
                  ('TOOLCHANGE', 'RUNOUT', 'PAUSE', 'CANCEL', 'COMPLETE'))
    POSITIONS = tuple('VAR_SEQUENCE_%s_POSITION' % n for n in
                      ('PRE_UNLOAD', 'POST_FORM_TIP', 'PRE_LOAD'))

    @classmethod
    def setUpClass(cls):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            cls.kc = cfg._kconfig('sequence_validators', profiles.get('boxturtle').syms)

    def _validator(self, name):
        return self.kc.syms[name].validator

    def test_every_symbol_has_a_plain_string_validator(self):
        for name in self.PARKS + self.POSITIONS:
            with self.subTest(symbol=name):
                self.assertIsNotNone(self._validator(name))
                self.assertIsNone(self.kc.syms[name].array_editor)

    def test_park_format(self):
        validator = self._validator('VAR_SEQUENCE_PARK_PAUSE')
        for good in ('-999,-999,1,5,2', '-999, -999, 1,  5, 2', '1.5, .5, 0, 0, 0',
                     '+10, 20., -1, 0, 2', '(50, 50, 5, 0, 2)', '( 50,50,5,0,2 )'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('', '1, 2, 3, 4', '1, 2, 3, 4, 5, 6', 'x, 2, 3, 4, 5', '1,,2,3,4',
                    '1, 2, 3, 4, 5,', '1 2 3 4 5', '(1, 2, 3, 4, 5', '[1, 2, 3, 4, 5]',
                    "'1, 2, 3, 4, 5'", '1, 2, 3, 4, -'):
            with self.subTest(value=bad):
                self.assertIsNone(validator.fullmatch(bad))

    def test_position_format(self):
        validator = self._validator('VAR_SEQUENCE_PRE_LOAD_POSITION')
        for good in ('-999, -999, 0', '100,200,1.5', '(.5, -.5, 0)'):
            with self.subTest(value=good):
                self.assertIsNotNone(validator.fullmatch(good))
        for bad in ('', '1, 2', '1, 2, 3, 4', '-999, -999, 0, 0, 0', '1, 2, z', '1, 2, 3,'):
            with self.subTest(value=bad):
                self.assertIsNone(validator.fullmatch(bad))

    def test_every_declared_default_passes(self):
        names = set(self.PARKS + self.POSITIONS)
        found = []
        for path in glob.glob(os.path.join(REPO_ROOT, 'installer', '**', 'Kconfig*'), recursive=True):
            sym = None
            with open(path) as handle:
                for line in handle:
                    match = re.match(r'\s*config\s+(\w+)', line)
                    if match:
                        sym = match.group(1) if match.group(1) in names else None
                        continue
                    match = sym and re.match(r'\s*default\s+"([^"]*)"', line)
                    if match:
                        found.append((sym, match.group(1)))
        self.assertGreaterEqual(len(found), len(names))
        for sym, value in found:
            with self.subTest(symbol=sym, value=value):
                self.assertIsNotNone(self._validator(sym).fullmatch(value.strip()))


if __name__ == '__main__':
    unittest.main()
