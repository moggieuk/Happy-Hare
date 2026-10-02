# Printer-level capabilities (toolhead sensor, extruder sensor, cutter) are set once in the
# top-level config, and every unit parse reads them from there through KCONFIG_PARENT
# ($(printer-flag,SYM), shared_components.PRINTER_FLAGS) - the only source, so an env var
# of the same name changes nothing.

import os
import tempfile
import unittest
from pathlib import Path

from test.hh import cfg, profiles

cfg._prepare_imports()
import shared_components  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPrinterCapabilities(unittest.TestCase):

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

    def test_every_flag_is_read_from_the_top_level_config(self):
        kconfig = (REPO_ROOT / 'installer' / 'Kconfig').read_text(encoding='utf-8')
        for sym in shared_components.PRINTER_FLAGS:
            with self.subTest(sym=sym):
                self.assertIn('$(printer-flag,%s)' % sym, kconfig)

    def test_a_unit_reads_printer_capabilities_from_the_top_level_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = self._install(tmp, {'MMU_HAS_SENSOR_TOOLHEAD': True,
                                         'MMU_HAS_TOOLHEAD_CUTTER': True})
            unit = self._unit(parent)
            self.assertTrue(unit.is_enabled('MMU_HAS_SENSOR_TOOLHEAD'))
            self.assertTrue(unit.is_enabled('MMU_HAS_TOOLHEAD_CUTTER'))
            self.assertFalse(unit.is_enabled('MMU_HAS_SENSOR_EXTRUDER'))

    def test_the_old_env_vars_are_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = self._install(tmp, {'MMU_HAS_SENSOR_EXTRUDER': True})
            unit = self._unit(parent, HAS_SENSOR_EXTRUDER='', HAS_SENSOR_TOOLHEAD='y')
            self.assertTrue(unit.is_enabled('MMU_HAS_SENSOR_EXTRUDER'))
            self.assertFalse(unit.is_enabled('MMU_HAS_SENSOR_TOOLHEAD'))
            unit = self._unit('', HAS_SENSOR_TOOLHEAD='y')                # no top-level config
            self.assertFalse(unit.is_enabled('MMU_HAS_SENSOR_TOOLHEAD'))

    def test_a_render_sees_its_own_top_level_flags_after_another_render(self):
        # The harness reuses a parsed tree for an identical parse: the top-level flags read
        # at parse time must be part of what makes it identical
        units = profiles.clone_across_units('printer_flags_unset', profiles.get('boxturtle'),
                                            ('unit0', 'unit1')).units
        with_sensor = profiles.Profile('printer_flags_toolhead', units=units, syms={
            'MMU_HAS_SENSOR_TOOLHEAD': True, 'PIN_TOOLHEAD_SENSOR': 'PG13'})
        without = profiles.Profile('printer_flags_none', units=units)
        params = 'config/base/mmu_parameters_unit1.cfg'
        self.assertIn('toolhead_homing_max', cfg.render(with_sensor)[params])
        self.assertNotIn('toolhead_homing_max', cfg.render(without)[params])

    def test_install_sh_no_longer_hands_them_down(self):
        install_sh = (REPO_ROOT / 'install.sh').read_text(encoding='utf-8')
        self.assertNotRegex(install_sh, r'\bHAS_(SENSOR_TOOLHEAD|SENSOR_EXTRUDER|TOOLHEAD_CUTTER)=')
        self.assertIn('KCONFIG_PARENT="${KCONFIG_CONFIG}"', install_sh)


if __name__ == "__main__":
    unittest.main()
