# Printer-level capabilities (toolhead sensors and cutter) reach each unit parse from the
# top-level config through KCONFIG_PARENT, falling back to the env install.sh
# run_kconfig_units also passes, which the harness mirrors. An env name the Kconfig never
# reads is silently ignored, so the unit just sees the capability as absent.

import os
import re
import tempfile
import unittest
from pathlib import Path

from test.hh import cfg

cfg._prepare_imports()
import shared_components  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestHandedDownEnv(unittest.TestCase):

    def test_names_match_kconfig_and_install_sh(self):
        kconfig = (REPO_ROOT / 'installer' / 'Kconfig').read_text(encoding='utf-8')
        install_sh = (REPO_ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertEqual({env: sym for sym, env in shared_components.PRINTER_FLAGS},
                         cfg.HANDED_DOWN_ENV)
        for env, sym in cfg.HANDED_DOWN_ENV.items():
            with self.subTest(env=env):
                self.assertIn('$(printer-flag,%s,%s)' % (sym, env), kconfig)
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

    def _install(self, tmp, printer_syms):
        """A top-level config with printer_syms, saved where a unit parse can read it."""
        parent = os.path.join(tmp, '.mmu_config')
        entry_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='y',
                         UNIT_NAME='unit0,unit1', MCU_NAME='unit0,unit1')
        with cfg._env(entry_env):
            cfg._kconfig('printer_flags', printer_syms).write_config(parent)
        return parent

    def _unit(self, parent, **env):
        unit_env = dict(cfg._SINGLE_UNIT_ENV, F_MULTI_UNIT='y', F_MULTI_UNIT_ENTRY_POINT='',
                        UNIT_NAME='unit1', MCU_NAME='unit1', UNIT_INDEX='1',
                        KCONFIG_PARENT=parent, **env)
        with cfg._env(unit_env):
            return cfg._kconfig('printer_flags:unit1', {})

    def test_a_unit_reads_printer_capabilities_from_the_top_level_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = self._install(tmp, {'MMU_HAS_SENSOR_TOOLHEAD': True,
                                         'MMU_HAS_TOOLHEAD_CUTTER': True})
            unit = self._unit(parent)                          # no env at all
            self.assertTrue(unit.is_enabled('MMU_HAS_SENSOR_TOOLHEAD'))
            self.assertTrue(unit.is_enabled('MMU_HAS_TOOLHEAD_CUTTER'))
            self.assertFalse(unit.is_enabled('MMU_HAS_SENSOR_EXTRUDER'))

    def test_the_top_level_config_wins_over_the_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = self._install(tmp, {'MMU_HAS_SENSOR_EXTRUDER': True})
            unit = self._unit(parent, HAS_SENSOR_EXTRUDER='', HAS_SENSOR_TOOLHEAD='y')
            self.assertTrue(unit.is_enabled('MMU_HAS_SENSOR_EXTRUDER'))
            self.assertFalse(unit.is_enabled('MMU_HAS_SENSOR_TOOLHEAD'))
