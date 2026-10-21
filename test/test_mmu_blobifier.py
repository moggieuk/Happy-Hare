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


class TestPark(BlobifierSessionMixin, unittest.TestCase):
    """BLOBIFIER_PARK runs for real; the stub printer has no [quad_gantry_level]"""

    PROFILE = _installed('boxturtle_blobifier_park')

    def setUp(self):
        super().setUp()
        self.hh.printer.harness_macro_effects['BLOBIFIER_PARK'] = (
            lambda macro, gcmd: macro.run_body(gcmd))
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.bl = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS').variables
        self.safe = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_SAFE_DESCEND')
        self.hh.run_gcode("G90\nG1 X150 Y150 Z20")

    def park(self):
        first = len(self.toolhead.moves)
        self.hh.run_gcode("BLOBIFIER_PARK")
        return [pos[:3] for pos, _ in self.toolhead.moves[first:]]

    def refused(self):
        return any('Please home' in m for m in self.hh.console)

    def add_qgl(self, applied):
        qgl = type('QGL', (), {'get_status': lambda self, eventtime=None: {'applied': applied}})()
        self.hh.printer.add_object('quad_gantry_level', qgl)

    def test_parks_without_qgl(self):
        moves = self.park()
        self.assertFalse(self.refused())
        self.assertAlmostEqual(moves[-1][0], self.bl['purge_x'])

    def test_refuses_when_qgl_not_applied(self):
        self.add_qgl(False)
        self.assertEqual(self.park(), [])
        self.assertTrue(self.refused())

    def test_parks_when_qgl_applied(self):
        self.add_qgl(True)
        moves = self.park()
        self.assertFalse(self.refused())
        self.assertAlmostEqual(moves[-1][0], self.bl['purge_x'])

    def test_tray_unsafe_above_force_height_stays_high(self):
        self.safe.variables = dict(self.safe.variables, tray=False,
                                   print_height=self.bl['force_safe_descend_height_until'] + 5.)
        moves = self.park()
        self.assertTrue(moves)
        self.assertNotIn(self.bl['tray_top'], [z for _, _, z in moves])


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


class TestParkDescent(BlobifierSessionMixin, unittest.TestCase):
    """Exercise the height override independently of the optional-QGL fix."""

    PROFILE = _installed('boxturtle_blobifier_park_descent')

    def setUp(self):
        super().setUp()
        self.hh.printer.harness_macro_effects['BLOBIFIER_PARK'] = (
            lambda macro, gcmd: macro.run_body(gcmd))
        qgl = type('QGL', (), {'get_status': lambda self, eventtime=None: {'applied': True}})()
        self.hh.printer.add_object('quad_gantry_level', qgl)
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.bl = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS').variables
        self.safe = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_SAFE_DESCEND')
        self.hh.run_gcode("G90\nG1 X150 Y150 Z20")

    def park_heights(self, print_height):
        self.safe.variables = dict(self.safe.variables, tray=False, print_height=print_height)
        first = len(self.toolhead.moves)
        self.hh.run_gcode("BLOBIFIER_PARK")
        moves = self.toolhead.moves[first:]
        self.assertTrue(moves)
        return [pos[2] for pos, _ in moves]

    def test_low_print_descends_to_tray_even_if_tray_unsafe(self):
        self.assertIn(self.bl['tray_top'], self.park_heights(0.))

    def test_tray_unsafe_at_force_height_stays_high(self):
        self.assertNotIn(self.bl['tray_top'],
                         self.park_heights(self.bl['force_safe_descend_height_until']))

    def test_tray_unsafe_above_force_height_stays_high(self):
        self.assertNotIn(self.bl['tray_top'],
                         self.park_heights(self.bl['force_safe_descend_height_until'] + 5.))

    def test_zero_force_height_disables_override(self):
        self.bl['force_safe_descend_height_until'] = 0.
        self.assertNotIn(self.bl['tray_top'], self.park_heights(0.))


if __name__ == '__main__':
    unittest.main()


class TestPurgeTemperature(BlobifierSessionMixin, unittest.TestCase):
    """BLOBIFIER and _BLOBIFIER_RESTORE_TEMP run for real; M104/M109 drive the fake heater"""

    PROFILE = _installed('boxturtle_blobifier_purge_temp')

    def setUp(self):
        super().setUp()
        run = lambda macro, gcmd: macro.run_body(gcmd)
        self.hh.printer.harness_macro_effects.update(
            {'BLOBIFIER': run, '_BLOBIFIER_RESTORE_TEMP': run})
        fan = type('Fan', (), {'get_status': lambda self, eventtime=None: {'speed': 0.}})()
        self.hh.printer.add_object('fan', fan)
        self.hh.printer.lookup_object('configfile').status_raw_config = {
            'extruder': {'filament_diameter': '1.75'}}
        self.heater = self.hh.printer.lookup_object('extruder').heater
        gcode = self.hh.printer.lookup_object('gcode')
        gcode.register_command('M104', self._m104)
        gcode.register_command('M109', self._m109)
        self.bl = self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS')
        self.purge_temp = self.bl.variables['purge_temp_min']
        self.waits = []
        self.hh.run_gcode("G28\nG90\nG1 X150 Y150 Z20")

    def _m104(self, gcmd):
        self.heater.target_temp = gcmd.get_float('S', 0.)

    def _m109(self, gcmd):
        self.waits.append(gcmd.get_float('S', 0.))
        self._m104(gcmd)
        self.heater.smoothed_temp = self.heater.target_temp

    def purge(self, temp):
        self.heater.target_temp = self.heater.smoothed_temp = temp
        self.hh.run_gcode("BLOBIFIER PURGE_LENGTH=20")
        return self.heater.target_temp

    def test_restores_target_after_raising_for_purge(self):
        self.assertEqual(self.purge(self.purge_temp - 10), self.purge_temp - 10)
        self.assertEqual(self.waits, [self.purge_temp])

    def test_leaves_hotter_target_alone(self):
        self.assertEqual(self.purge(self.purge_temp + 10), self.purge_temp + 10)

    def test_does_not_override_target_set_during_purge(self):
        self.bl.variables = dict(self.bl.variables, clean_macro='M104 S%d' % (self.purge_temp + 30))
        self.assertEqual(self.purge(self.purge_temp - 10), self.purge_temp + 30)
