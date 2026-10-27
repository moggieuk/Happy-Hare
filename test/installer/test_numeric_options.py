"""Legacy numeric settings survive the one-time saved-config upgrade."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

import kconfiglib
from installer.kconfig_upgrades import upgrade
from test.hh import cfg


class TestNumericOptions(unittest.TestCase):
    def test_legacy_numbers_upgrade_once_without_changing_values(self):
        values = {
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
        with tempfile.TemporaryDirectory() as tmp, cfg._env(cfg._SINGLE_UNIT_ENV):
            kc = cfg._new_kconfig('numeric_options')
            kc.warn_to_stderr = False
            # Both single-unit and per-unit files use the same upgrade target.
            for filename in ('.mmu_config', '.mmu_config_unit0'):
                for marker in ('', ' #~DEFAULT~#'):
                    with self.subTest(filename=filename, marker=marker):
                        path = Path(tmp) / (filename + ('_defaults' if marker else ''))
                        original = ''.join('CONFIG_%s="%s"%s\r\n' % (name, value, marker)
                                           for name, value in values.items()).encode()
                        path.write_bytes(original)
                        report = io.StringIO()
                        with contextlib.redirect_stderr(report):
                            self.assertEqual(upgrade(path), 9)
                        self.assertIn('values preserved', report.getvalue())
                        backup = Path(str(path) + '.pre-numeric-upgrade')
                        self.assertEqual(backup.read_bytes(), original)
                        self.assertEqual(path.read_bytes(), original.replace(b'"', b''))
                        kc.load_config(str(path), filter_defaults=False)
                        self.assertEqual(kc.warnings, [])
                        for name, value in values.items():
                            self.assertIs(kc.syms[name].orig_type, kconfiglib.FLOAT)
                            self.assertEqual(kc.syms[name].user_value, value)
                        # Default markers must still mean defaults on an editor load.
                        kc.load_config(str(path), filter_defaults=True)
                        if marker:
                            for name in values:
                                self.assertIsNone(kc.syms[name].user_value)
                        # A rerun must neither write nor report an upgrade.
                        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                        snapshot = (path.read_bytes(), path.stat().st_mtime_ns,
                                    backup.read_bytes(), backup.stat().st_mtime_ns)
                        report = io.StringIO()
                        with contextlib.redirect_stderr(report):
                            self.assertEqual(upgrade(path), 0)
                        self.assertEqual(report.getvalue(), '')
                        self.assertEqual(snapshot, (path.read_bytes(), path.stat().st_mtime_ns,
                                                   backup.read_bytes(), backup.stat().st_mtime_ns))
            untouched = Path(tmp) / 'invalid'
            original = (b'CONFIG_PARAM_HEATER_MAX_TEMP="invalid"\n'
                        b'CONFIG_PARAM_HEATER_PAUSE_TEMP="nan"\n'
                        b'CONFIG_PARAM_HEATER_DEFAULT_DRY_TIME="1e999"\n'
                        b'CONFIG_UNRELATED_STRING="42"\n'
                        b'CONFIG_PARAM_TD1_CAPTURE_TIMEOUT=3.5\n')
            untouched.write_bytes(original)
            self.assertEqual(upgrade(untouched), 0)
            self.assertEqual(untouched.read_bytes(), original)
            self.assertFalse(Path(str(untouched) + '.pre-numeric-upgrade').exists())
