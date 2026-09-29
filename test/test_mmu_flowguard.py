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

    def test_an_encoder_setting_change_on_a_non_owner_leaves_the_encoder_alone(self):
        """
        _on_encoder_change pushes a live encoder FlowGuard change (MMU_TEST_CONFIG) straight
        into the encoder, so it must only do that for the unit that owns it. A non-owner
        taking it over would retune the detection the selected unit is running.

        No route through _apply_flowguard_scope leaves a non-owner's flowguard_active set, so
        this pins the guard itself: the flag is set directly to stand in for any future way
        into that state. What is asserted is the encoder's, not the flag's, bookkeeping -
        clearing a stale flag is deactivate_flowguard's job, on the next scope pass.
        """
        self.arm_for_selected_unit()
        self.assert_owned_by(self.unit0, self.unit1, [])

        self.unit1.sync_feedback.flowguard_active = True    # a non-owner believing it is active
        del self.hh.gcode.console[:]
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=1 flowguard_encoder_max_motion=45')
        self.hh.settle(0.2)
        self.assertEqual(self.hh.errors, [], 'config change failed')

        self.assertTrue(self.encoder.is_flowguard_enabled(), "a non-owner disarmed the encoder")
        self.assertIs(self.encoder.active_mmu_unit, self.unit0, "a non-owner took ownership")
        self.assertNotEqual(
            self.encoder.detection_length, 45.,
            "unit1's flowguard_encoder_max_motion was applied to an encoder it does not own")

        # The owner is still the one whose settings are in force, and it still reacts
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 flowguard_encoder_max_motion=45')
        self.hh.settle(0.2)
        self.assertEqual(self.encoder.detection_length, 45.)


class SharedEncoderPrintTestCase(unittest.TestCase):
    """Toolchanges between the two units during a print."""

    TIP_AT_GATE = -40.0

    def setUp(self):
        self.hh = session('encoder_shared')
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit0, self.unit1 = self.mmu.mmu_machine.units
        self.encoder = self.unit0.encoder
        self.t0, self.t1 = self.unit0.first_gate, self.unit1.first_gate   # default 1:1 TTG map
        self.hh.heat_extruder(220)
        for gate in (self.t0, self.t1):
            self.hh.place_filament(gate, position=self.TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.assertEqual(self.hh.errors, [], 'preload failed')

    def tearDown(self):
        self.hh.close()

    def gcode(self, script, settle=0.5):
        del self.hh.gcode.console[:]
        self.hh.run_gcode(script)
        self.hh.settle(settle)
        return [l for l in self.hh.gcode.console if 'FlowGuard monitoring' in l]

    def start_print(self, tool):
        self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.gcode('MMU_PRINT_START', settle=1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')

    def change_tool(self, tool):
        messages = self.gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.assertEqual(self.hh.errors, [], 'toolchange to T%d failed' % tool)
        return messages

    def assert_owned_by(self, owner, other, messages=None):
        self.assertTrue(self.encoder.is_flowguard_enabled(),
                        "encoder FlowGuard should be armed for %s: %s" % (owner.name, messages))
        self.assertIs(self.encoder.active_mmu_unit, owner)
        self.assertEqual(self.encoder.extruder.name, owner.extruder_name())
        self.assertTrue(owner.sync_feedback.flowguard_active)
        self.assertFalse(other.sync_feedback.flowguard_active)

    def test_toolchanges_between_units_hand_over_the_encoder(self):
        self.start_print(self.t0)
        self.assert_owned_by(self.unit0, self.unit1)

        for tool, owner, other in ((self.t1, self.unit1, self.unit0),
                                   (self.t0, self.unit0, self.unit1),
                                   (self.t1, self.unit1, self.unit0)):
            messages = self.change_tool(tool)
            self.assert_owned_by(owner, other, messages)
            self.assertEqual(messages, ['FlowGuard monitoring with encoder deactivated',
                                        'FlowGuard monitoring with encoder activated'])

    def test_each_unit_arms_the_encoder_with_its_own_settings(self):
        self.unit0.calibrator.update_clog_detection_length(12.3)
        self.unit1.p.flowguard_encoder_mode = 1        # static
        self.unit1.p.flowguard_encoder_max_motion = 33.

        self.start_print(self.t0)
        self.assertEqual((self.encoder.detection_mode, self.encoder.detection_length), (2, 12.3))
        self.encoder.detection_length = 17.7           # unpersisted drift must not follow unit0

        self.change_tool(self.t1)
        self.assert_owned_by(self.unit1, self.unit0)
        self.assertEqual((self.encoder.detection_mode, self.encoder.detection_length), (1, 33.))

        self.change_tool(self.t0)
        self.assert_owned_by(self.unit0, self.unit1)
        self.assertEqual((self.encoder.detection_mode, self.encoder.detection_length), (2, 12.3))

    def test_a_unit_with_encoder_flowguard_off_leaves_it_off(self):
        self.unit1.p.flowguard_encoder_mode = 0
        self.start_print(self.t0)

        self.change_tool(self.t1)
        self.assertFalse(self.encoder.is_flowguard_enabled())
        self.assertFalse(self.unit0.sync_feedback.flowguard_active)
        self.assertFalse(self.unit1.sync_feedback.flowguard_active)

        self.change_tool(self.t0)
        self.assert_owned_by(self.unit0, self.unit1)

    def test_pause_and_resume_rearm_for_the_selected_unit(self):
        self.start_print(self.t1)
        self.assert_owned_by(self.unit1, self.unit0)

        self.gcode('MMU_PAUSE')
        self.assertFalse(self.encoder.is_flowguard_enabled())
        self.assertFalse(self.unit1.sync_feedback.flowguard_active)

        self.gcode('RESUME')
        self.assert_owned_by(self.unit1, self.unit0)

    def test_a_runout_on_the_second_unit_is_handled_for_its_tool(self):
        self.start_print(self.t0)
        self.change_tool(self.t1)

        emitted = []
        run_script = self.hh.gcode.run_script
        def spy(script):
            if '__MMU_ENCODER_RUNOUT' in script:
                emitted.append(script)
            return run_script(script)
        self.hh.gcode.run_script = spy
        self.hh.printer.lookup_object('idle_timeout').state = 'Printing'

        # Extruder advances past the detection length with no encoder movement
        extruder_pos = [self.encoder._get_extruder_pos()]
        self.encoder._get_extruder_pos = lambda eventtime=None: extruder_pos[0]
        self.hh.settle(3.)
        generation = self.encoder.get_flowguard_generation()
        extruder_pos[0] += self.encoder.detection_length + 50.
        self.hh.settle(3.)

        self.assertEqual(len(emitted), 1, 'no encoder runout was raised: %s' % emitted)
        self.assertIn('GENERATION=%d' % generation, emitted[0])
        self.assertTrue(any('clog/tangle' in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.mmu.tool_selected, self.t1)
