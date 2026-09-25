# The harness hands printer-level capabilities to each unit parse the way
# install.sh run_kconfig_units does. An env name the Kconfig never reads is
# silently ignored, so the unit just sees the capability as absent.

import re
import unittest
from pathlib import Path

from test.hh import cfg

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestHandedDownEnv(unittest.TestCase):

    def test_names_match_kconfig_and_install_sh(self):
        kconfig = (REPO_ROOT / 'installer' / 'Kconfig').read_text(encoding='utf-8')
        install_sh = (REPO_ROOT / 'install.sh').read_text(encoding='utf-8')
        for env, sym in cfg.HANDED_DOWN_ENV.items():
            with self.subTest(env=env):
                self.assertIn('$(env-is-y,%s)' % env, kconfig)
                self.assertRegex(install_sh, r'\b%s="\$CONFIG_%s"' % (re.escape(env), sym))

    def test_toolhead_cutter_reaches_unit_parse(self):
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')
        with cfg._env(entry_env):
            entry = cfg._kconfig('handed_down', {'MMU_HAS_TOOLHEAD_CUTTER': True})

        unit_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='',
                        UNIT_NAME='unit0', MCU_NAME='unit0', UNIT_INDEX='0',
                        **cfg.handed_down_env(entry))
        with cfg._env(unit_env):
            unit = cfg._kconfig('handed_down:unit0', {})
        self.assertTrue(unit.is_enabled('MMU_HAS_TOOLHEAD_CUTTER'))
