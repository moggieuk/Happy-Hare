"""The existing loader warns when a former string option becomes numeric."""
import os
import tempfile
import unittest

import kconfiglib
from test.hh import cfg


class TestNumericOptions(unittest.TestCase):
    def test_float_options_warn_for_legacy_strings_and_accept_numbers(self):
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
            path = os.path.join(tmp, '.mmu_config')
            for filter_defaults in (True, False):
                for quoted in (True, False):
                    with self.subTest(filter_defaults=filter_defaults, quoted=quoted):
                        kc.warnings.clear()
                        with open(path, 'w') as f:
                            for name, value in values.items():
                                f.write('CONFIG_%s=%s\n' % (name, '"%s"' % value if quoted else value))
                        kc.load_config(path, filter_defaults=filter_defaults)
                        for name, value in values.items():
                            sym = kc.syms[name]
                            self.assertIs(sym.orig_type, kconfiglib.FLOAT)
                            self.assertEqual(sym.user_value, None if quoted else value)
                            if quoted:
                                self.assertTrue(any(name in w and 'invalid' in w and 'ignored' in w
                                                    for w in kc.warnings), name)
                        if not quoted:
                            self.assertEqual(kc.warnings, [])
