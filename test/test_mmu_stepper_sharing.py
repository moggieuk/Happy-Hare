# Happy Hare test harness - config shared from the base gear to the extra gears.
#
# The rendered mmu_hardware.cfg is deliberately incomplete: gates 1..N get only
# what is genuinely per-device, and mmu_unit.py copies the rest out of the base
# gear's sections before Klipper loads them (SHAREABLE_STEPPER_PARAMS and
# SHAREABLE_TMC_PARAMS in mmu_constants.py).
#
# Both halves fail silently. Real Klipper would at least refuse to boot a
# driver with no run_current (tmc2240.py reads it with no default), but the
# harness defaults it to 0.5 and stepper.py defaults rotation_distance to 40 -
# so a gate would move filament at roughly nine times the intended distance
# with nothing in the log. Hence assertions on the resulting geometry and
# current rather than on a clean boot.
#
#   ./venv/bin/python -m unittest test.test_mmu_stepper_sharing
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import re
import unittest

from test.hh import cfg, profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

HARDWARE = 'config/base/mmu_hardware.cfg'
PROFILE = 'boxturtle'


class SharedGearConfigTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session(PROFILE)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.unit = self.hh.mmu.mmu_unit()
        self.assertTrue(self.unit.multigear, '%s is not multigear' % PROFILE)
        self.assertGreater(self.unit.num_gates, 1)

    def tearDown(self):
        self.hh.close()

    def _extra_gear_sections(self):
        rendered = cfg.render(profiles.get(PROFILE))[HARDWARE]
        parsed = cfg.assemble({HARDWARE: rendered})
        base, extra = None, []
        for name in cfg.sections(rendered):
            if re.fullmatch(r'mmu_stepper \S+_gear', name):
                base = dict(parsed.items(name))
            elif re.fullmatch(r'mmu_stepper \S+_gear_\d+', name):
                extra.append((name, dict(parsed.items(name))))
        return base, extra

    def test_the_rendered_config_leaves_the_shared_options_out(self):
        """Without this the boot assertion below could pass on its own."""
        from extras.mmu.mmu_constants import SHAREABLE_STEPPER_PARAMS
        base, extra = self._extra_gear_sections()
        self.assertTrue(base, 'no base gear stepper section rendered')
        self.assertTrue(extra, 'no extra gear stepper sections rendered')
        for key in SHAREABLE_STEPPER_PARAMS:
            with self.subTest(option=key):
                self.assertIn(key, base, 'the base gear must supply it')
                for name, options in extra:
                    self.assertNotIn(
                        key, options,
                        '[%s] carries %s itself, so sharing it is untested'
                        % (name, key))

    def test_every_gear_stepper_ends_up_with_the_base_geometry(self):
        """rotation_distance, gear_ratio, microsteps and full_steps at once.

        get_rotation_distance returns the distance after the gear ratio is
        applied, and microsteps x full_steps_per_rotation - so all four
        shared options have to arrive for both numbers to match.
        """
        drives = self.unit.drives_unique
        self.assertEqual(len(drives), self.unit.num_gates)
        want = drives[0].mmu_gear_stepper.steppers[0].get_rotation_distance()
        self.assertNotEqual(
            want[0], 40.0,
            'the base gear is at Klipper\'s own default, so this proves nothing')
        for i, drive in enumerate(drives[1:], start=1):
            with self.subTest(gear=self.unit.mmu_gear_names[i]):
                self.assertEqual(
                    drive.mmu_gear_stepper.steppers[0].get_rotation_distance(),
                    want)

    def test_every_gear_driver_ends_up_with_the_base_current(self):
        base = self.unit.gear_default_current(0)
        self.assertTrue(base, 'profile has no TMC on gate 0 - nothing to share')
        for gate in range(1, self.unit.num_gates):
            with self.subTest(gate=gate):
                self.assertEqual(self.unit.gear_default_current(gate), base)


if __name__ == '__main__':
    unittest.main()
