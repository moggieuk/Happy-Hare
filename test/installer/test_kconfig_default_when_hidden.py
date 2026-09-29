# The 'default_when_hidden' keyword.
#
# kconfiglib writes a symbol whose prompt is hidden with its default value, but
# as an explicit assignment when the user had set it. Reloaded, that frozen
# default counts as a user value: it shows as NOT DEFAULT and no longer follows
# later default changes. Under a node marked 'default_when_hidden' the saved
# value carries the #~DEFAULT~# token instead, so it stays a real default.

import os
import tempfile
import unittest

import kconfiglib

from test.hh import cfg, profiles

KCONFIG = '''
menuconfig PARAM_TEST_CUSTOMIZE
  bool "Customize"
  default n
  {keyword}

config PARAM_TEST_VALUE
  string
  prompt "Value" if PARAM_TEST_CUSTOMIZE
  default "stock"
'''


class TestDefaultWhenHidden(unittest.TestCase):

    def _saved_line(self, keyword, customize):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'Kconfig')
            with open(path, 'w') as handle:
                handle.write(KCONFIG.format(keyword=keyword))
            kc = kconfiglib.Kconfig(path, warn=False)
            kc.syms['PARAM_TEST_CUSTOMIZE'].set_value('y')
            kc.syms['PARAM_TEST_VALUE'].set_value('custom')
            kc.syms['PARAM_TEST_CUSTOMIZE'].set_value(customize)
            out = os.path.join(tmp, '.config')
            kc.write_config(out)
            with open(out) as handle:
                return [line.strip() for line in handle if 'PARAM_TEST_VALUE' in line]

    def test_hidden_user_value_is_saved_as_the_default(self):
        self.assertEqual(self._saved_line('default_when_hidden', 'n'),
                         ['CONFIG_PARAM_TEST_VALUE="stock"' + kconfiglib.HH_DEFAULT_TOKEN])

    def test_without_the_keyword_the_default_is_frozen_as_before(self):
        self.assertEqual(self._saved_line('', 'n'), ['CONFIG_PARAM_TEST_VALUE="stock"'])

    def test_a_visible_user_value_is_kept(self):
        self.assertEqual(self._saved_line('default_when_hidden', 'y'),
                         ['CONFIG_PARAM_TEST_VALUE="custom"'])

    def test_led_effects_revert_to_defaults_when_customizing_is_turned_off(self):
        with cfg._env(cfg._SINGLE_UNIT_ENV), tempfile.TemporaryDirectory() as tmp:
            kc = cfg._kconfig('led_effects_hidden', dict(
                profiles.get('boxturtle').syms, BOOL_CUSTOMIZE_LED_EFFECTS=True,
                PARAM_EFFECT_ERROR='mmu_sparkle, (1, 0, 0), 5',
                PARAM_WHITE_LIGHT='(0.8, 0.8, 0.8)', PARAM_FILAMENT_COLOR_INTENSITY='0.9'))
            kc.syms['BOOL_CUSTOMIZE_LED_EFFECTS'].set_value('n')
            path = os.path.join(tmp, '.mmu_config')
            kc.write_config(path)
            reloaded = cfg._new_kconfig('led_effects_hidden_reload')
            reloaded.load_config(path, filter_defaults=True)
        self.assertIsNone(reloaded.syms['PARAM_EFFECT_ERROR'].user_value)
        self.assertEqual(reloaded.syms['PARAM_EFFECT_ERROR'].str_value.split(',')[0],
                         'mmu_red_strobe')
        self.assertIsNone(reloaded.syms['PARAM_WHITE_LIGHT'].user_value)
        self.assertEqual(reloaded.syms['PARAM_WHITE_LIGHT'].str_value, '(1, 1, 1)')
        self.assertIsNone(reloaded.syms['PARAM_FILAMENT_COLOR_INTENSITY'].user_value)
        self.assertEqual(reloaded.syms['PARAM_FILAMENT_COLOR_INTENSITY'].str_value, '0.5')


if __name__ == '__main__':
    unittest.main()
