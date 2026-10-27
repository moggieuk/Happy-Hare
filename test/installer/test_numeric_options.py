"""Quoted legacy numbers load without rewriting saved configurations."""
from pathlib import Path
import tempfile
import unittest

import kconfiglib
from test.hh import cfg


class TestNumericOptions(unittest.TestCase):
    def test_quoted_floats_preserve_values_and_normalize_on_save(self):
        names = (
            'PARAM_HEATER_MAX_TEMP', 'PARAM_HEATER_DEFAULT_DRY_TEMP',
            'PARAM_HEATER_DEFAULT_DRY_TIME', 'PARAM_HEATER_DEFAULT_DRY_HUMIDITY',
            'PARAM_HEATER_PAUSE_TEMP', 'PARAM_HEATER_ROTATE_INTERVAL',
            'PARAM_HEATER_VENT_INTERVAL', 'PARAM_TD1_CAPTURE_TIMEOUT',
            'PARAM_SLICER_TIP_PARK_POS',
        )
        with tempfile.TemporaryDirectory() as tmp, cfg._env(cfg._SINGLE_UNIT_ENV):
            shipped = cfg._new_kconfig('numeric_options')
            for name in names:
                self.assertIs(shipped.syms[name].orig_type, kconfiglib.FLOAT)
            # Visible symbols isolate parsing/saving from hardware dependencies.
            tree = Path(tmp) / 'Kconfig'
            tree.write_text('config NUMBER\n    float "Number"\n    default -1\n'
                            'config TEXT\n    string "Text"\n    default "42"\n')
            kc = kconfiglib.Kconfig(str(tree), warn_to_stderr=False)
            path = Path(tmp) / '.mmu_config'
            for value in ('72.50', '0', '-1', '7.25e1', ' 30 '):
                for marker in ('', ' #~DEFAULT~#', ' # ordinary comment'):
                    with self.subTest(value=value, marker=marker):
                        original = ('# standalone comment\nCONFIG_NUMBER="%s"%s\n'
                                    'CONFIG_TEXT="42"\n' % (value, marker)).encode()
                        path.write_bytes(original)
                        stamp = path.stat().st_mtime_ns
                        kc.load_config(str(path), filter_defaults=False)
                        self.assertEqual(kc.warnings, [])
                        self.assertEqual(kc.syms['NUMBER'].user_value, value.strip())
                        self.assertEqual(kc.syms['TEXT'].user_value, '42')
                        self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(path.stat().st_mtime_ns, stamp)
                        self.assertFalse(Path(str(path) + '.pre-numeric-upgrade').exists())
                        kc.load_config(str(path), filter_defaults=True)
                        self.assertEqual(kc.syms['NUMBER'].user_value,
                                         None if marker == ' #~DEFAULT~#' else value.strip())
                        kc.load_config(str(path), filter_defaults=False)
                        kc.write_config(str(path))
                        saved = path.read_bytes()
                        self.assertIn(('CONFIG_NUMBER=%s' % value.strip()).encode(), saved)
                        self.assertIn(b'CONFIG_TEXT="42"', saved)
                        stamp = path.stat().st_mtime_ns
                        kc.load_config(str(path), filter_defaults=False)
                        kc.write_config(str(path))
                        self.assertEqual(kc.warnings, [])
                        self.assertEqual(path.read_bytes(), saved)
                        self.assertEqual(path.stat().st_mtime_ns, stamp)
            for value in ('', ' ', 'invalid', 'nan', 'inf', '1e999'):
                with self.subTest(invalid=value):
                    path.write_text('CONFIG_NUMBER="%s"\n' % value)
                    kc.load_config(str(path), filter_defaults=False)
                    self.assertTrue(any('invalid' in warning for warning in kc.warnings))
                    self.assertIsNone(kc.syms['NUMBER'].user_value)
                    self.assertEqual(kc.syms['NUMBER'].str_value, '-1')
