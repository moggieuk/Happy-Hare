# Happy Hare test harness - per-gear overrides on multigear units.
#
# Gear 1..N inherit stepper and TMC settings from gear 0 at boot (mmu_unit.py only fills an
# option that is absent). BOOL_GEAR_OVERRIDE_<n> lets Kconfig render an explicit value on one
# gear instead. Nothing enabled must render exactly what shipped before.
#
#   ./venv/bin/python -m unittest test.test_mmu_gear_overrides
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import cfg, profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

HARDWARE = 'config/base/mmu_hardware.cfg'

STEPPER_KEYS = ('rotation_distance', 'gear_ratio', 'microsteps', 'full_steps_per_rotation')
TMC_KEYS = ('run_current', 'hold_current', 'sense_resistor', 'rref')

DIAG_PINS = {
    'PIN_GEAR_DIAG': 'unit0:PA1',
    'PIN_GEAR_DIAG_1': 'unit0:PA2',
    'PIN_GEAR_DIAG_2': 'unit0:PA3',
    'PIN_GEAR_DIAG_3': 'unit0:PA4',
}

GEAR_2 = {
    'BOOL_GEAR_OVERRIDE_2': True,
    'PARAM_GEAR_RUN_CURRENT_2': '0.6',
    'PARAM_GEAR_GEAR_RATIO_2': '50:17',
    'PARAM_GEAR_MICROSTEPS_2': '32',
}


def _sections(name, syms):
    profile = profiles.get('boxturtle').derive(name, syms=syms)
    parser = cfg.assemble(cfg.render(profile))

    def gear(i):
        return (dict(parser.items('mmu_stepper unit0_gear_%d' % i)),
                dict(parser.items('tmc2209 mmu_stepper unit0_gear_%d' % i)))
    return gear


class TestGearOverrideRender(unittest.TestCase):

    def test_nothing_enabled_renders_only_pins(self):
        gear = _sections('gear_override_none', {})
        for i in (1, 2, 3):
            stepper, tmc = gear(i)
            for key in STEPPER_KEYS:
                self.assertNotIn(key, stepper, 'gear_%d' % i)
            for key in TMC_KEYS:
                self.assertNotIn(key, tmc, 'gear_%d' % i)

    def test_override_lands_on_its_own_gear_only(self):
        gear = _sections('gear_override_gear2', GEAR_2)
        stepper, tmc = gear(2)
        self.assertEqual(tmc['run_current'], '0.6')
        self.assertEqual(stepper['gear_ratio'], '50:17')
        self.assertEqual(stepper['microsteps'], '32')
        for i in (1, 3):
            stepper, tmc = gear(i)
            self.assertNotIn('run_current', tmc)
            self.assertNotIn('gear_ratio', stepper)
            self.assertNotIn('microsteps', stepper)

    def test_values_are_ignored_while_the_toggle_is_off(self):
        # build.py reads a hidden symbol's raw user value, so the template guard is what
        # stops a saved value from rendering after the toggle is switched off
        syms = dict(GEAR_2, BOOL_GEAR_OVERRIDE_2=False)
        stepper, tmc = _sections('gear_override_off', syms)(2)
        self.assertNotIn('run_current', tmc)
        self.assertNotIn('gear_ratio', stepper)
        self.assertNotIn('microsteps', stepper)

    def test_an_empty_field_keeps_inheriting(self):
        stepper, tmc = _sections('gear_override_partial', {
            'BOOL_GEAR_OVERRIDE_2': True,
            'PARAM_GEAR_RUN_CURRENT_2': '0.6',
        })(2)
        self.assertEqual(tmc['run_current'], '0.6')
        for key in STEPPER_KEYS:
            self.assertNotIn(key, stepper)
        for key in ('hold_current', 'sense_resistor', 'rref'):
            self.assertNotIn(key, tmc)

    def test_stallguard_override_replaces_the_inherited_threshold(self):
        gear = _sections('gear_override_stallguard', dict(DIAG_PINS, **{
            'BOOL_GEAR_OVERRIDE_2': True,
            'PARAM_GEAR_STALLGUARD_THRESHOLD_2': '77',
        }))
        base = gear(1)[1]['driver_SGTHRS']
        self.assertNotEqual(base, '77')
        self.assertEqual(gear(2)[1]['driver_SGTHRS'], '77')
        self.assertEqual(gear(3)[1]['driver_SGTHRS'], base)


class TestGearOverridePrompts(unittest.TestCase):

    def _kconfig(self, name, profile, syms):
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            return cfg._kconfig(name, dict(profiles.get(profile).syms, **syms))

    def test_prompts_follow_the_toggle_and_the_gate_count(self):
        kc = self._kconfig('gear_override_prompts', 'boxturtle', {'BOOL_GEAR_OVERRIDE_2': True})
        self.assertTrue(kc.syms['BOOL_GEAR_OVERRIDE_3'].visibility)
        self.assertFalse(kc.syms['BOOL_GEAR_OVERRIDE_4'].visibility)
        self.assertTrue(kc.syms['PARAM_GEAR_RUN_CURRENT_2'].visibility)
        self.assertFalse(kc.syms['PARAM_GEAR_RUN_CURRENT_3'].visibility)

    def test_single_gear_units_have_no_override_menu(self):
        kc = self._kconfig('gear_override_single_gear', 'tradrack', {})
        self.assertFalse(kc.syms['BOOL_GEAR_OVERRIDE_1'].visibility)


class TestGearOverrideBoot(unittest.TestCase):

    def setUp(self):
        self.hh = session(profiles.get('boxturtle').derive('gear_override_boot', syms=GEAR_2))
        self.addCleanup(self.hh.close)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit()

    def test_override_survives_inheritance(self):
        base = self.unit.gear_default_current(0)
        self.assertNotAlmostEqual(base, 0.6)
        self.assertAlmostEqual(self.unit.gear_default_current(1), base)
        self.assertAlmostEqual(self.unit.gear_default_current(2), 0.6)

        # Klipper folds gear_ratio into the rotation distance it reports
        def rotation(name):
            obj = self.hh.printer.lookup_object('mmu_stepper %s' % name)
            return obj.get_steppers()[0].get_rotation_distance()

        base_rd, base_steps = rotation('unit0_gear')
        self.assertEqual(rotation('unit0_gear_1'), (base_rd, base_steps))
        rd, steps = rotation('unit0_gear_2')
        self.assertEqual(steps, base_steps * 2)
        configured = float(dict(cfg.assemble(cfg.render(self.hh.profile)).items(
            'mmu_stepper unit0_gear'))['rotation_distance'])
        self.assertAlmostEqual(rd, configured * 17 / 50, places=5)

        # An uncalibrated gate falls back to its own drive's configured value on select
        self.hh.run_gcode('MMU_SELECT GATE=2')
        self.assertEqual(self.mmu.gate_selected, 2)
        self.assertEqual(rotation('unit0_gear_2'), (rd, steps))

    def test_current_restore_returns_to_the_override(self):
        self.hh.run_gcode('MMU_SELECT GATE=2')
        self.assertEqual(self.mmu.gate_selected, 2)
        tmc = self.unit.gear_tmc_obj(2)
        changes = len(tmc.current_changes)

        with self.mmu.wrap_gear_current(percent=50, reason="test"):
            pass

        applied = [run for _t, run, _h in tmc.current_changes[changes:]]
        self.assertEqual(len(applied), 2)
        self.assertAlmostEqual(applied[0], 0.3, places=3)
        self.assertAlmostEqual(applied[1], 0.6, places=3)


if __name__ == '__main__':
    unittest.main()
