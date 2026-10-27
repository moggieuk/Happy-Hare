"""Legacy numeric settings survive the one-time saved-config upgrade."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import menuconfig

import kconfiglib
import kconfig_upgrades
from kconfig_upgrades import upgrade
from installer import build
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
            # Both single-unit and per-unit files use the same migration helper.
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
            # The builder/importer path must migrate before loading too, without
            # relying on a make prerequisite or an earlier interactive install.
            path = Path(tmp) / 'builder'
            original = ('CONFIG_PARAM_HEATER_PAUSE_TEMP=" 30 "\n'
                        'CONFIG_PARAM_HEATER_MAX_TEMP="7.25e1"\n'
                        'CONFIG_PARAM_HEATER_ROTATE_INTERVAL=""\n')
            path.write_text(original)
            with contextlib.redirect_stderr(io.StringIO()):
                loaded = build.KConfig(str(path))
            self.assertEqual(loaded.syms['PARAM_HEATER_PAUSE_TEMP'].user_value, '30')
            self.assertEqual(loaded.syms['PARAM_HEATER_MAX_TEMP'].user_value, '7.25e1')
            self.assertIsNone(loaded.syms['PARAM_HEATER_ROTATE_INTERVAL'].user_value)
            self.assertEqual(loaded.warnings, [])
            # Restoring another legacy config must refresh the advertised backup.
            restored = 'CONFIG_PARAM_HEATER_PAUSE_TEMP=" 32 "\n'
            path.write_text(restored)
            with contextlib.redirect_stderr(io.StringIO()):
                loaded.load_unit(str(path))
            self.assertEqual(loaded.syms['PARAM_HEATER_PAUSE_TEMP'].user_value, '32')
            self.assertEqual(Path(str(path) + '.pre-numeric-upgrade').read_text(), restored)
            # Force every worker to see the legacy file before any can replace
            # it: exactly one may migrate/report, and the backup must be legacy.
            concurrent = Path(tmp) / 'concurrent'
            original = b'CONFIG_PARAM_HEATER_PAUSE_TEMP=" 30 "\n'
            concurrent.write_bytes(original)
            barrier = threading.Barrier(8, timeout=10)
            first_read = threading.local()
            convert = kconfig_upgrades._convert

            def synchronized_convert(data):
                if not getattr(first_read, 'done', False):
                    first_read.done = True
                    barrier.wait()
                return convert(data)

            report = io.StringIO()
            with mock.patch.object(kconfig_upgrades, '_convert', synchronized_convert), \
                 contextlib.redirect_stderr(report), ThreadPoolExecutor(max_workers=8) as workers:
                counts = list(workers.map(upgrade, [concurrent] * 8))
            self.assertEqual(sorted(counts), [0] * 7 + [1])
            self.assertEqual(report.getvalue().count('Upgraded'), 1)
            self.assertEqual(Path(str(concurrent) + '.pre-numeric-upgrade').read_bytes(), original)
            # Loading a legacy file in the curses dialog is read-only and quiet.
            directory = Path(tmp) / 'read-only'
            directory.mkdir()
            source = directory / 'legacy'
            source.write_text('CONFIG_PARAM_HEATER_PAUSE_TEMP=" 30 "\n')
            original = source.read_bytes()
            source.chmod(0o444)
            directory.chmod(0o555)
            report = io.StringIO()
            try:
                with mock.patch.object(menuconfig, '_kconf', kc, create=True), \
                     contextlib.redirect_stderr(report):
                    self.assertTrue(menuconfig._try_load(str(source)))
                self.assertEqual(kc.syms['PARAM_HEATER_PAUSE_TEMP'].user_value, '30')
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(list(directory.iterdir()), [source])
                self.assertEqual(report.getvalue(), '')
            finally:
                directory.chmod(0o755)
                source.chmod(0o644)
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
