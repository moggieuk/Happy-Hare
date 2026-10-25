# The 'keep_when_hidden' keyword.
#
# kconfiglib ignores a symbol's user value while its prompt is hidden, so saving
# writes the default instead: a frozen assignment when only the prompt is
# conditional, or nothing when an enclosing 'if' disables the defaults too. Under
# a node marked 'keep_when_hidden' the user value is saved instead, so turning a
# parent off doesn't lose the customization.

import os
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles

KCONFIG = '''
menuconfig PARAM_TEST_PARENT
  bool "Parent"
  default y

config PARAM_TEST_INT
  int
  prompt "Int" if PARAM_TEST_PARENT
  default 50
  {keyword}

config PARAM_TEST_FLOAT
  float
  prompt "Float" if PARAM_TEST_PARENT
  default 1.5
  {keyword}

config PARAM_TEST_STRING
  string
  prompt "String" if PARAM_TEST_PARENT
  default "stock"
  {keyword}

config PARAM_TEST_BOOL
  bool
  prompt "Bool" if PARAM_TEST_PARENT
  default y
  {keyword}

config PARAM_TEST_BOOLINT
  boolint
  prompt "Boolint" if PARAM_TEST_PARENT
  default 1
  {keyword}

if PARAM_TEST_PARENT
config PARAM_TEST_DROPPED
  int "Dropped"
  default 7
  {keyword}
endif
'''

CUSTOM = {
    'PARAM_TEST_INT': '90',
    'PARAM_TEST_FLOAT': '2.25',
    'PARAM_TEST_STRING': 'custom',
    'PARAM_TEST_BOOL': 'n',
    'PARAM_TEST_BOOLINT': 0,
    'PARAM_TEST_DROPPED': '9',
}

KEPT_LINES = {
    'PARAM_TEST_INT': 'CONFIG_PARAM_TEST_INT=90',
    'PARAM_TEST_FLOAT': 'CONFIG_PARAM_TEST_FLOAT=2.25',
    'PARAM_TEST_STRING': 'CONFIG_PARAM_TEST_STRING="custom"',
    'PARAM_TEST_BOOL': '# CONFIG_PARAM_TEST_BOOL is not set',
    'PARAM_TEST_BOOLINT': 'CONFIG_PARAM_TEST_BOOLINT=0',
    'PARAM_TEST_DROPPED': 'CONFIG_PARAM_TEST_DROPPED=9',
}


class TestKeepWhenHidden(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def kconfig(self, text):
        path = os.path.join(self.tmp.name, 'Kconfig')
        with open(path, 'w') as handle:
            handle.write(text)
        return kconfiglib.Kconfig(path, warn=False)

    def saved(self, kc):
        out = os.path.join(self.tmp.name, '.config')
        kc.write_config(out)
        with open(out) as handle:
            return out, [line.strip() for line in handle if 'PARAM_TEST_' in line]

    def customize_then_hide(self, keyword, custom=CUSTOM):
        kc = self.kconfig(KCONFIG.format(keyword=keyword))
        for name, value in custom.items():
            kc.syms[name].set_value(value)
        kc.syms['PARAM_TEST_PARENT'].set_value('n')
        return kc

    def test_hidden_user_values_are_saved(self):
        _, lines = self.saved(self.customize_then_hide('keep_when_hidden'))
        for name, line in KEPT_LINES.items():
            with self.subTest(symbol=name):
                self.assertIn(line, lines)
        self.assertFalse([line for line in lines if kconfiglib.HH_DEFAULT_TOKEN in line
                          and 'PARENT' not in line])

    def test_without_the_keyword_the_default_is_frozen_or_dropped(self):
        _, lines = self.saved(self.customize_then_hide(''))
        self.assertIn('CONFIG_PARAM_TEST_INT=50', lines)
        self.assertIn('CONFIG_PARAM_TEST_STRING="stock"', lines)
        self.assertIn('CONFIG_PARAM_TEST_BOOLINT=1', lines)
        self.assertFalse([line for line in lines if 'PARAM_TEST_DROPPED' in line])

    def test_without_a_user_value_the_default_is_saved_as_before(self):
        _, lines = self.saved(self.customize_then_hide('keep_when_hidden', custom={}))
        self.assertIn('CONFIG_PARAM_TEST_INT=50' + kconfiglib.HH_DEFAULT_TOKEN, lines)
        self.assertFalse([line for line in lines if 'PARAM_TEST_DROPPED' in line])

    def test_kept_values_come_back_when_the_parent_is_shown(self):
        out, _ = self.saved(self.customize_then_hide('keep_when_hidden'))
        for filter_defaults in (True, False):
            with self.subTest(filter_defaults=filter_defaults):
                kc = self.kconfig(KCONFIG.format(keyword='keep_when_hidden'))
                kc.load_config(out, filter_defaults=filter_defaults)
                kc.syms['PARAM_TEST_PARENT'].set_value('y')
                self.assertEqual(kc.syms['PARAM_TEST_INT'].str_value, '90')
                self.assertEqual(kc.syms['PARAM_TEST_FLOAT'].str_value, '2.25')
                self.assertEqual(kc.syms['PARAM_TEST_STRING'].str_value, 'custom')
                self.assertEqual(kc.syms['PARAM_TEST_BOOL'].str_value, 'n')
                self.assertEqual(kc.syms['PARAM_TEST_BOOLINT'].str_value, '0')
                self.assertEqual(kc.syms['PARAM_TEST_DROPPED'].str_value, '9')

    def test_a_menu_passes_the_keyword_to_its_symbols(self):
        kc = self.kconfig('''
menuconfig PARAM_TEST_PARENT
  bool "Parent"
  default y

menu "Settings"
  keep_when_hidden

config PARAM_TEST_INT
  int
  prompt "Int" if PARAM_TEST_PARENT
  default 50
endmenu
''')
        kc.syms['PARAM_TEST_INT'].set_value('90')
        kc.syms['PARAM_TEST_PARENT'].set_value('n')
        self.assertIn('CONFIG_PARAM_TEST_INT=90', self.saved(kc)[1])

    def test_the_nearest_keyword_wins(self):
        menu = '''
menuconfig PARAM_TEST_PARENT
  bool "Parent"
  default y

menu "Settings"
  {outer}

config PARAM_TEST_INT
  int
  prompt "Int" if PARAM_TEST_PARENT
  default 50
  {inner}
endmenu
'''
        for outer, inner, expected in (
                ('default_when_hidden', 'keep_when_hidden', 'CONFIG_PARAM_TEST_INT=90'),
                ('keep_when_hidden', 'default_when_hidden',
                 'CONFIG_PARAM_TEST_INT=50' + kconfiglib.HH_DEFAULT_TOKEN)):
            with self.subTest(inner=inner):
                kc = self.kconfig(menu.format(outer=outer, inner=inner))
                kc.syms['PARAM_TEST_INT'].set_value('90')
                kc.syms['PARAM_TEST_PARENT'].set_value('n')
                self.assertIn(expected, self.saved(kc)[1])

    def test_promptless_values_survive_save_reload(self):
        for inherited in (False, True):
            with self.subTest(inherited=inherited):
                text = KCONFIG.format(keyword='' if inherited else 'keep_when_hidden')
                text = '\n'.join(line for line in text.splitlines()
                                 if not line.strip().startswith('prompt '))
                text = text.replace('int "Dropped"', 'int')
                if inherited:
                    text = 'menu "Settings"\n  keep_when_hidden\n' + text + '\nendmenu\n'
                kc = self.kconfig(text)
                for name, value in CUSTOM.items():
                    kc.syms[name].set_value(value)
                kc.syms['PARAM_TEST_PARENT'].set_value('n')
                out, lines = self.saved(kc)
                for line in KEPT_LINES.values():
                    self.assertIn(line, lines)
                for filter_defaults in (False, True):
                    reloaded = self.kconfig(text)
                    reloaded.load_config(out, filter_defaults=filter_defaults)
                    for name in CUSTOM:
                        self.assertEqual(reloaded.syms[name].user_value, kc.syms[name].user_value)
                    self.assertEqual(self.saved(reloaded)[1], lines)

    def test_promptless_defaults_without_keep_are_unchanged(self):
        for keyword in ('', 'default_when_hidden', 'keep_when_hidden'):
            with self.subTest(keyword=keyword):
                kc = self.kconfig('config PARAM_TEST_INT\n  int\n  default 50\n  ' + keyword + '\n')
                self.assertIn('CONFIG_PARAM_TEST_INT=50' + kconfiglib.HH_DEFAULT_TOKEN,
                              self.saved(kc)[1])
                if keyword != 'keep_when_hidden':
                    kc.syms['PARAM_TEST_INT'].set_value('90')
                    self.assertIn('CONFIG_PARAM_TEST_INT=50', self.saved(kc)[1])

    def test_both_keywords_on_one_node_is_an_error(self):
        with self.assertRaisesRegex(kconfiglib.KconfigError, "can't both be set"):
            self.kconfig('''
config PARAM_TEST_INT
  int "Int"
  default 50
  keep_when_hidden
  default_when_hidden
''')

    def test_a_choice_is_an_error(self):
        with self.assertRaisesRegex(kconfiglib.KconfigError, 'only valid for symbols and menus'):
            self.kconfig('''
choice CHOICE_TEST
  prompt "Choice"
  keep_when_hidden

config CHOICE_TEST_A
  bool "A"
endchoice
''')


class TestKeepWhenHiddenInTheTree(unittest.TestCase):

    def save_and_reload(self, label, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV), tempfile.TemporaryDirectory() as tmp:
            kc = cfg._kconfig(label, syms)
            path = os.path.join(tmp, '.mmu_config')
            kc.write_config(path)
            with open(path) as handle:
                saved = handle.read().splitlines()
            reloaded = cfg._new_kconfig(label + '_reload')
            reloaded.load_config(path, filter_defaults=True)
        return reloaded, saved

    def test_installer_migration_history_survives_hidden_save_reload(self):
        history = "{'version': 1, 'steps': [{'id': 'example'}]}"
        reloaded, _ = self.save_and_reload('migration_history', {'PARAM_STATE_MIGRATION': history})
        self.assertEqual(reloaded.syms['PARAM_STATE_MIGRATION'].user_value, history)
        self.assertEqual(reloaded.as_dict()['PARAM_STATE_MIGRATION'], history)

    def render_parameters(self, kc):
        extra = {'PARAM_TOTAL_NUM_GATES': kc.getint('PARAM_NUM_GATES')}
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            rendered = cfg._render_templates(['config/base/mmu_parameters.cfg'], kc, extra)
        return rendered['config/base/mmu_parameters.cfg']

    def test_a_tolerance_survives_turning_move_validation_off(self):
        kc, _ = self.save_and_reload('keep_tolerance', dict(
            profiles.get('encoder').syms,
            PARAM_TOOLHEAD_MOVE_ERROR_TOLERANCE='90', PARAM_ENCODER_MOVE_VALIDATION='0'))
        self.assertEqual(kc.syms['PARAM_TOOLHEAD_MOVE_ERROR_TOLERANCE'].user_value, '90')
        lines = [line.split('#')[0].split(':', 1) for line in self.render_parameters(kc).splitlines()
                 if line.startswith('toolhead_move_error_tolerance')]
        self.assertEqual([value.strip() for _, value in lines], ['90'])

    def test_a_forced_value_still_reverts(self):
        # Hidden sync options are forced on when the filament is always gripped
        _, saved = self.save_and_reload('forced_sync', dict(
            profiles.get('tradrack').syms,
            PARAM_SYNC_TO_EXTRUDER='0', PARAM_FILAMENT_ALWAYS_GRIPPED='1'))
        self.assertIn('CONFIG_PARAM_SYNC_TO_EXTRUDER=1', [line.strip() for line in saved])


if __name__ == '__main__':
    unittest.main()
