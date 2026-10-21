# Happy Hare installer: where menuconfig shows each stepper's stallguard threshold.
#
# The threshold is a driver tuning value, so it belongs with that stepper's currents in
# MMU Kinematics and Speeds, for the selector as for the gear, not with the pins.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import cfg, profiles


class TestStallguardMenu(unittest.TestCase):

    def setUp(self):
        syms = dict(profiles.get("tradrack").syms, PIN_SELECTOR_DIAG="unit0:PA1", PIN_GEAR_DIAG="unit0:PA2")
        with cfg._env(cfg._SINGLE_UNIT_ENV):
            self.kc = cfg._kconfig("stallguard-menu", syms)

    def menu_of(self, name):
        from kconfiglib import expr_value
        nodes = [n for n in self.kc.syms[name].nodes if n.prompt and expr_value(n.prompt[1])]
        self.assertEqual(len(nodes), 1, "%s should have one visible prompt" % name)
        return nodes[0].parent.prompt[0].strip()

    def test_selector_threshold_is_with_the_selector_stepper(self):
        self.assertEqual(self.menu_of("PARAM_SELECTOR_STALLGUARD_THRESHOLD"), "Selector stepper")

    def test_gear_threshold_is_with_the_gear_stepper(self):
        self.assertEqual(self.menu_of("PARAM_GEAR_STALLGUARD_THRESHOLD"), "Gear stepper")


if __name__ == "__main__":
    unittest.main()
