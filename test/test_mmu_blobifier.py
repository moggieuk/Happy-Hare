# Happy Hare Blobifier initialization tests.
#
# BLOBIFIER_INIT runs from init_macros() at bootup and again whenever purge_macro is
# changed at runtime. Macro bodies are recorded no-ops in the harness, so these tests
# count calls on the BLOBIFIER_INIT macro and model its one side effect that matters
# here: marking BLOBIFIER as initialized.

import logging
import unittest

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

NOT_INSTALLED = "Blobifier is not correctly installed"


def _profile(name, syms):
    return profiles.get('boxturtle').derive(name, syms=syms)


def _installed(name, **syms):
    return _profile(name, dict({
        'MMU_HAS_BLOBIFIER': True,
        'PIN_BLOBIFIER_SERVO': 'unit0:PA1',
    }, **syms))


def _not_installed(name, purge_macro):
    return _profile(name, {
        'CHOICE_PURGE_MACRO_OTHER': True,
        'PURGE_MACRO_OTHER': purge_macro,
    })


def _mark_initialized(macro, gcmd):
    # SET_GCODE_VARIABLE replaces the variables dict rather than mutating it
    blobifier = macro.printer.lookup_object('gcode_macro BLOBIFIER')
    blobifier.variables = dict(blobifier.variables, initialized=1)


class BlobifierSessionMixin:

    PROFILE = None

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.build()
        self.hh.printer.harness_macro_effects = {'BLOBIFIER_INIT': _mark_initialized}
        self.hh.boot()

    def tearDown(self):
        self.hh.close()

    def init_calls(self):
        return len(self.hh.printer.lookup_object('gcode_macro BLOBIFIER_INIT').calls)

    def set_initialized(self, value):
        blobifier = self.hh.printer.lookup_object('gcode_macro BLOBIFIER')
        blobifier.variables = dict(blobifier.variables, initialized=value)


class TestInstalledWithBlobifierPurge(BlobifierSessionMixin, unittest.TestCase):

    PROFILE = _installed('boxturtle_blobifier_purge')

    def test_initializes_once_at_bootup(self):
        self.assertEqual(self.hh.mmu.p.purge_macro, 'BLOBIFIER')
        self.assertEqual(self.init_calls(), 1)
        self.assertEqual(self.hh.errors, [])

    def test_purge_macro_change_does_not_reinitialize(self):
        self.hh.run_gcode('MMU_TEST_CONFIG purge_macro=_MMU_PURGE')
        self.assertEqual(self.hh.mmu.p.purge_macro, '_MMU_PURGE')
        self.hh.run_gcode('MMU_TEST_CONFIG purge_macro=BLOBIFIER')
        self.assertEqual(self.hh.mmu.p.purge_macro, 'BLOBIFIER')
        self.assertEqual(self.init_calls(), 1)

        # The initialized guard, not a missed on_change, is what suppressed it
        self.set_initialized(0)
        self.hh.run_gcode('MMU_TEST_CONFIG purge_macro=_MMU_PURGE')
        self.assertEqual(self.init_calls(), 2)
        self.assertEqual(self.hh.errors, [])


class TestInstalledWithOtherPurge(BlobifierSessionMixin, unittest.TestCase):

    PROFILE = _installed('boxturtle_blobifier_other_purge', CHOICE_PURGE_MACRO_PURGE=True)

    def test_initializes_at_bootup_without_blobifier_purge_macro(self):
        self.assertEqual(self.hh.mmu.p.purge_macro, '_MMU_PURGE')
        self.assertEqual(self.init_calls(), 1)
        self.assertEqual(self.hh.errors, [])


class TestNotInstalled(unittest.TestCase):

    def _boot(self, name, purge_macro):
        hh = session(_not_installed(name, purge_macro))
        self.addCleanup(hh.close)
        hh.boot()
        return hh

    def assert_rejected(self, hh):
        self.assertIsNone(hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS', None))
        self.assertEqual(hh.printer.lookup_object('gcode_macro BLOBIFIER_INIT').calls, [])
        self.assertEqual(hh.mmu.p.purge_macro, '')
        self.assertEqual(len([e for e in hh.errors if NOT_INSTALLED in e]), 1, hh.errors)

    def test_blobifier_purge_macro_is_rejected(self):
        self.assert_rejected(self._boot('boxturtle_no_blobifier', 'BLOBIFIER'))

    def test_blobifier_purge_macro_with_params_is_rejected(self):
        self.assert_rejected(
            self._boot('boxturtle_no_blobifier_params', 'BLOBIFIER PURGE_LENGTH=20'))

    def test_other_purge_macro_is_left_alone(self):
        hh = session(profiles.get('boxturtle'))
        self.addCleanup(hh.close)
        hh.boot()
        self.assertEqual(hh.mmu.p.purge_macro, '_MMU_PURGE')
        self.assertEqual(hh.errors, [])


if __name__ == '__main__':
    unittest.main()
