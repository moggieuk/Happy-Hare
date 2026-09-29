# Happy Hare test harness - encoder FlowGuard on an encoder shared by two units.
#
# Units that name the same [mmu_encoder] share one MmuEncoder object, so arming FlowGuard
# on the selected unit and disarming it on the others must respect which unit owns the
# encoder (encoder.active_mmu_unit), whatever order the units are visited in.
#
#   ./venv/bin/python -m unittest test.test_mmu_flowguard
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

UNIT0, UNIT1 = 'unit0', 'unit1'   # the 'encoder_shared' profile's two Tradracks


class SharedEncoderFlowGuardTestCase(unittest.TestCase):
    """FlowGuard arming on an encoder shared by two units."""

    def setUp(self):
        # calibrate=True so select_gate() is legal on a Tradrack
        self.hh = session('encoder_shared')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.units = self.mmu.mmu_machine.units
        self.unit0, self.unit1 = self.units
        self.encoder = self.unit0.encoder

    def tearDown(self):
        self.hh.close()

    def flowguard_messages(self, action):
        del self.hh.gcode.console[:]
        action()
        return [l for l in self.hh.gcode.console if 'FlowGuard monitoring' in l]

    def arm_for_selected_unit(self):
        """What print start does: enable monitoring for the selected unit."""
        return self.flowguard_messages(self.mmu._enable_filament_monitoring)

    def assert_owned_by(self, owner, other, messages):
        self.assertTrue(self.encoder.is_flowguard_enabled(),
                        "encoder FlowGuard should be armed for %s: %s" % (owner.name, messages))
        self.assertIs(self.encoder.active_mmu_unit, owner)
        self.assertTrue(owner.sync_feedback.flowguard_active)
        self.assertFalse(other.sync_feedback.flowguard_active)

    def test_the_profile_really_is_two_units_on_one_encoder(self):
        self.assertEqual([u.name for u in self.units], [UNIT0, UNIT1])
        self.assertIs(self.unit0.encoder, self.unit1.encoder)
        self.assertFalse(self.unit0.has_buffer(), 'this profile is encoder-only on purpose')
        self.assertFalse(self.unit1.has_buffer())
        self.assertEqual(self.unit0.p.flowguard_encoder_mode, 2)
        self.assertEqual(self.unit1.p.flowguard_encoder_mode, 2)

    def test_a_non_owning_unit_does_not_disarm_the_encoder(self):
        messages = self.arm_for_selected_unit()

        self.assert_owned_by(self.unit0, self.unit1, messages)
        self.assertEqual([m for m in messages if 'deactivated' in m], [],
                         "a unit that does not own the shared encoder must not disarm it: %s" % messages)

    def test_arming_with_the_second_unit_selected(self):
        self.mmu.select_gate(self.unit1.first_gate)
        messages = self.arm_for_selected_unit()

        self.assert_owned_by(self.unit1, self.unit0, messages)

    def test_switching_unit0_to_unit1_while_armed_hands_over_the_encoder(self):
        self.arm_for_selected_unit()
        messages = self.flowguard_messages(lambda: self.mmu.select_gate(self.unit1.first_gate))

        self.assert_owned_by(self.unit1, self.unit0, messages)

    def test_switching_unit1_to_unit0_while_armed_hands_over_the_encoder(self):
        # The selected unit is visited before the owner here, so it must take over an encoder
        # that is already armed by the other unit
        self.mmu.select_gate(self.unit1.first_gate)
        self.arm_for_selected_unit()
        messages = self.flowguard_messages(lambda: self.mmu.select_gate(self.unit0.first_gate))

        self.assert_owned_by(self.unit0, self.unit1, messages)

    def test_disabling_monitoring_releases_the_encoder(self):
        self.arm_for_selected_unit()
        self.encoder.detection_length = 999.   # as if autotune had moved the live length
        self.mmu._disable_filament_monitoring()

        self.assertFalse(self.encoder.is_flowguard_enabled())
        self.assertFalse(self.unit0.sync_feedback.flowguard_active)
        self.assertFalse(self.unit1.sync_feedback.flowguard_active)

        # Once disarmed, the live length is no longer reported to either unit
        for unit in self.units:
            self.assertNotEqual(self.encoder.get_effective_clog_detection_length(unit), 999.)
