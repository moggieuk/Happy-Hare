# Happy Hare test harness - encoder FlowGuard on a SHARED encoder.
#
# WHY THIS FILE EXISTS SEPARATELY FROM test_mmu_encoder.py
#
# test_mmu_encoder.py covers the encoder as gate-homing hardware and as a runout/clog
# detector for a single unit. The `encoder` key in [mmu_unit] is documented as shareable
# (config/base/mmu_hardware.cfg:151) and mmu_unit.py hands the SAME MmuEncoder object to
# every unit that names it, so two units can sit on one coder - a real shape on a machine
# with one encoder per bowden feeding two MMU heads. Nothing covered that.
#
# THE BUG
#
# MmuController._apply_flowguard_scope() (mmu_controller.py:2530) arms the FlowGuard of
# the selected unit and deactivates it on every OTHER unit - per unit, because FlowGuard
# is per unit. On a shared encoder those are the same object, and the deactivate branch
# tests `u.encoder.is_flowguard_enabled()`, i.e. the shared flag. So one pass armed the
# encoder for unit0 and then switched it straight back off while "deactivating" unit1:
#
#     FlowGuard monitoring with encoder activated
#     FlowGuard monitoring with encoder deactivated
#     ... unit0[act=True enc=False] unit1[act=False enc=False]
#
# Both lines in the same second, on every suspend/resume of every load/unload/toolchange,
# and encoder FlowGuard dead for the whole print. It was also ORDER DEPENDENT: with the
# later unit selected, the deactivate ran while the encoder was still off (a no-op) and
# the machine appeared to work - which is what made it survive on a real printer.
#
# The unit's own `flowguard_active` flag kept saying True, so MMU_FLOWGUARD reported
# "enabled and currently active" for a detector that was not running.
#
# THE FIX
#
# enable_flowguard() already records its caller in encoder.active_mmu_unit, so ownership
# is already known - deactivate_flowguard() just has to honour it. A unit that does not own
# the shared encoder now clears only its own (never-valid) flag.
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
        # calibrate=True so select_gate() is legal (Tradrack's LinearServoSelector refuses to
        # select on an uncalibrated machine, and these tests only need the SELECTION to move)
        self.hh = session('encoder_shared')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.units = self.mmu.mmu_machine.units
        self.unit0, self.unit1 = self.units
        self.encoder = self.unit0.encoder

    def tearDown(self):
        self.hh.close()

    def arm_for_selected_unit(self):
        """What print start does: apply the FlowGuard scope with unit0 selected."""
        del self.hh.gcode.console[:]
        self.mmu._enable_filament_monitoring()
        return [l for l in self.hh.gcode.console if 'FlowGuard monitoring' in l]

    def test_the_profile_really_is_two_units_on_one_encoder(self):
        """Otherwise the rest of this file would be testing nothing."""
        self.assertEqual([u.name for u in self.units], [UNIT0, UNIT1])
        self.assertIs(self.unit0.encoder, self.unit1.encoder)
        self.assertFalse(self.unit0.has_buffer(), 'this profile is encoder-only on purpose')
        self.assertFalse(self.unit1.has_buffer())
        self.assertEqual(self.unit0.p.flowguard_encoder_mode, 2)
        self.assertEqual(self.unit1.p.flowguard_encoder_mode, 2)

    def test_arm_and_disarm_in_one_pass_would_kill_a_shared_encoder(self):
        """
        The bug, stated as a unit test: the scope pass arms unit0's encoder and then
        disarms the same object as "unit1". The two console lines are the fingerprint a
        real printer shows - same second, activated immediately followed by deactivated.
        """
        messages = self.arm_for_selected_unit()

        self.assertTrue(
            self.encoder.is_flowguard_enabled(),
            "encoder FlowGuard was armed and then disarmed inside one scope pass: %s" % messages)
        self.assertEqual(
            [m for m in messages if 'deactivated' in m], [],
            "a unit that does not own the shared encoder must not disarm it: %s" % messages)

    def test_the_owning_unit_keeps_its_flag_and_the_other_never_claims_one(self):
        """`flowguard_active` is what MMU_FLOWGUARD and get_status() report."""
        self.arm_for_selected_unit()

        self.assertTrue(self.unit0.sync_feedback.flowguard_active)
        self.assertFalse(
            self.unit1.sync_feedback.flowguard_active,
            "unit1 must not report itself active while unit0 owns the encoder")
        self.assertEqual(self.encoder.active_mmu_unit, self.unit0)

    def test_the_owning_unit_still_releases_the_encoder_when_its_turn_is_over(self):
        """
        Ownership must not become a one-way latch: switching the selected unit to the other
        head has to disarm the first and arm the second, in whichever order the scope loop
        happens to visit them.
        """
        self.arm_for_selected_unit()
        self.mmu.select_gate(self.unit1.first_gate)
        self.assertEqual(self.mmu.mmu_unit(), self.unit1)

        del self.hh.gcode.console[:]
        self.mmu._apply_flowguard_scope()
        messages = [l for l in self.hh.gcode.console if 'FlowGuard monitoring' in l]

        self.assertTrue(self.encoder.is_flowguard_enabled(),
                        "the newly selected unit should own the encoder: %s" % messages)
        self.assertEqual(self.encoder.active_mmu_unit, self.unit1)
        self.assertFalse(self.unit0.sync_feedback.flowguard_active)
        self.assertTrue(self.unit1.sync_feedback.flowguard_active)

    def test_the_result_does_not_depend_on_which_unit_the_loop_reaches_first(self):
        """
        The pass is order dependent by construction (activate the selected unit, deactivate
        the rest), so drive it backwards as well: with unit1 selected the deactivate of unit0
        runs FIRST. That ordering used to be the only one that worked, which is why the bug
        survived - a machine that selects the second unit looks fine.
        """
        self.mmu.select_gate(self.unit1.first_gate)
        messages = self.arm_for_selected_unit()

        self.assertTrue(self.encoder.is_flowguard_enabled(),
                        "unit1 selected, unit0 deactivated first: %s" % messages)
        self.assertEqual(self.encoder.active_mmu_unit, self.unit1)
        self.assertTrue(self.unit1.sync_feedback.flowguard_active)
        self.assertFalse(self.unit0.sync_feedback.flowguard_active)

    def test_disabling_monitoring_releases_the_encoder(self):
        """
        The other direction: nothing may keep the encoder armed once the print that wanted
        it is over, or a stale arming would sit on the encoder between prints and the next
        print would start with a detection length that is nobody's.
        """
        self.arm_for_selected_unit()
        self.mmu._disable_filament_monitoring()

        self.assertFalse(self.encoder.is_flowguard_enabled())
        self.assertFalse(self.unit0.sync_feedback.flowguard_active)
        self.assertFalse(self.unit1.sync_feedback.flowguard_active)

        # A disarmed encoder answers callers with the length a fresh enable would settle on
        # rather than the live one, which only the owner may have moved (see
        # get_effective_clog_detection_length and its test in test_mmu_encoder.py)
        self.assertEqual(
            self.encoder.get_effective_clog_detection_length(self.unit0),
            self.encoder.get_effective_clog_detection_length(self.unit1))
