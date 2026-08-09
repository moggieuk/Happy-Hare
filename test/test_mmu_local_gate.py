# Happy Hare test harness - machine gate to unit-local gate conversion.
#
# On a multi-unit machine gate numbers are machine-wide and contiguous (unit0 owns 0-8, unit1 owns
# 9-12), while every per-unit array - steppers, drives, TMCs, offsets, drying state - is indexed
# locally. MmuUnit.local_gate() is the only bridge between the two, so a mistake there does not
# raise: it hands back a plausible index and the caller quietly operates the wrong hardware.
#
# THE TWO CASES ARE NOT THE SAME. Bypass (-2) and unknown (-1) are passed through deliberately, and
# with force_physical they resolve to local gate 0 so array lookups work - that is a documented
# default several callers rely on. A POSITIVE gate belonging to another unit is a different thing
# entirely: there is no correct local index, so demanding physical hardware for one now raises
# instead of silently clamping to this unit's gate 0.
#
# The conversion refuses a foreign gate in BOTH modes. It used to return -1 in default mode, and
# roughly sixteen callers branched on that as a "not mine, use a default" signal - several of them
# on a reactor timer, in get_status, or in a toolhead lookahead callback where raising would be far
# worse than declining. Those now test owns_gate() before converting, so the decision about what a
# foreign gate means is made by the caller that knows, rather than smuggled through a return value
# indistinguishable from a legitimate bypass.
#
# Uses 'ercf_vvd', the only multi-unit profile - on a single-unit machine a foreign gate cannot be
# expressed at all, so none of this is testable.
#
#   ./venv/bin/python -m unittest test.test_mmu_local_gate
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

TOOL_GATE_UNKNOWN = -1
TOOL_GATE_BYPASS = -2

# The four accessors that resolve real hardware, and so pass force_physical=True
HARDWARE_ACCESSORS = ('gear_name', 'drive_obj', 'gear_tmc_obj', 'gear_default_current')


class MultiUnitTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session('ercf_vvd')
        # Imported after the session reroutes 'extras' to the harness tree
        from extras.mmu.mmu_utils import MmuError
        self.MmuError = MmuError

        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit0 = self.mmu.mmu_machine.get_mmu_unit_by_index(0)
        self.unit1 = self.mmu.mmu_machine.get_mmu_unit_by_index(1)

        self.assertEqual(self.unit0.gate_bounds(), (0, 8))
        self.assertEqual(self.unit1.gate_bounds(), (9, 12))
        self.foreign = 9  # Owned by unit1, so foreign to unit0

    def tearDown(self):
        self.hh.close()


class TestForeignGate(MultiUnitTestCase):

    def test_hardware_accessors_refuse_a_gate_the_unit_does_not_own(self):
        """
        The whole point. Clamping a foreign gate to local 0 handed the caller another gate's
        stepper, which is how a mis-addressed request turned into silently driving the wrong lane.
        """
        for name in HARDWARE_ACCESSORS:
            with self.subTest(accessor=name):
                with self.assertRaises(self.MmuError) as ctx:
                    getattr(self.unit0, name)(self.foreign)
                self.assertIn('not managed by unit0', str(ctx.exception))
                self.assertIn('range=0-8', str(ctx.exception))

    def test_each_unit_still_resolves_its_own_gates(self):
        self.assertEqual(self.unit0.gear_name(0), 'unit0_gear')
        self.assertEqual(self.unit1.gear_name(self.foreign), 'unit1_gear')
        self.assertIsNot(self.unit0.drive_obj(0), self.unit1.drive_obj(self.foreign))

    def test_conversion_refuses_a_foreign_gate_in_both_modes(self):
        """
        Callers that can legitimately be handed another unit's gate now test owns_gate() up
        front, so there is no longer a mode in which the conversion quietly returns something.
        """
        with self.assertRaises(self.MmuError):
            self.unit0.local_gate(self.foreign)
        with self.assertRaises(self.MmuError):
            self.unit0.local_gate(self.foreign, force_physical=True)

    def test_a_non_gate_is_rejected_as_such(self):
        with self.assertRaises(self.MmuError):
            self.unit0.local_gate(None)


class TestBypassAndUnknownPassThrough(MultiUnitTestCase):
    """The negative sentinels are a supported input, not an error - several callers depend on it."""

    def test_sentinels_pass_through_in_default_mode(self):
        self.assertEqual(self.unit0.local_gate(TOOL_GATE_BYPASS), TOOL_GATE_BYPASS)
        self.assertEqual(self.unit0.local_gate(TOOL_GATE_UNKNOWN), TOOL_GATE_UNKNOWN)

    def test_sentinels_still_resolve_to_local_gate_zero_for_hardware(self):
        for gate in (TOOL_GATE_BYPASS, TOOL_GATE_UNKNOWN):
            with self.subTest(gate=gate):
                self.assertEqual(self.unit0.gear_name(gate), self.unit0.gear_name(0))
                self.assertIs(self.unit0.drive_obj(gate), self.unit0.drive_obj(0))


class TestCallersDeclineRatherThanConvert(MultiUnitTestCase):
    """
    Now that the conversion refuses a foreign gate, every caller that can be handed one has to
    decide for itself. These are the paths that previously leaned on the -1 return, several of
    them on a reactor timer, in get_status, or in a lookahead callback - so a raise escaping any
    of them is worse than the bug being fixed.
    """

    def calibrator(self):
        return self.unit0.calibrator

    def test_calibrator_queries_fall_back_instead_of_raising(self):
        cal = self.calibrator()
        self.assertEqual(cal.get_bowden_length(self.foreign), cal.get_bowden_length(0))
        self.assertEqual(cal.get_default_gear_rd(self.foreign), cal.get_default_gear_rd(0))
        self.assertTrue(cal.is_bowden_length_calibrated(self.foreign))
        self.assertTrue(cal.is_gear_rd_calibrated(self.foreign))
        self.assertGreater(cal.get_gear_rd(self.foreign), 0)

    def test_calibrator_mutations_decline_quietly(self):
        cal = self.calibrator()
        bowden_before = list(cal._bowden_lengths)
        rd_before = list(cal.rotation_distances)

        cal.update_bowden_length(123.4, gate=self.foreign)
        cal.update_gear_rd(22.5, gate=self.foreign)
        cal.apply_gear_rd(22.5, gate=self.foreign)
        cal.restore_gear_rd(gate=self.foreign)

        self.assertEqual(cal._bowden_lengths, bowden_before, 'wrote a foreign gate\'s bowden length')
        self.assertEqual(cal.rotation_distances, rd_before, 'wrote a foreign gate\'s rotation distance')
        self.assertEqual(self.hh.errors, [])

    def test_nfc_predicates_answer_no_rather_than_throwing(self):
        nfc = self.unit0.nfc_manager
        self.assertFalse(nfc.has_gate_nfc_reader(self.foreign))
        self.assertIsNone(nfc._local_index(self.foreign))
        self.assertIsNone(nfc._local_index(None))

    def test_environment_lookups_report_nothing_configured(self):
        env = self.unit0.environment_manager
        self.assertEqual(env._get_environment_status(self.foreign), (None, None))
        self.assertEqual(env._get_heater_status(self.foreign), (None, None))
        self.assertEqual(self.hh.errors, [])


class TestRoundTrip(MultiUnitTestCase):
    """
    logical_gate/local_gate are inverses, and code holding a local index has to convert back before
    calling a machine-gate API. Only visible on a unit that doesn't start at gate 0, where the two
    numbering schemes actually differ.
    """

    def test_conversion_is_reversible_on_a_later_unit(self):
        for lgate in range(self.unit1.num_gates):
            with self.subTest(lgate=lgate):
                gate = self.unit1.logical_gate(lgate)
                self.assertEqual(gate, lgate + 9)
                self.assertEqual(self.unit1.local_gate(gate), lgate)

    def test_a_local_index_used_as_a_machine_gate_is_caught(self):
        """
        The failure mode behind the rotary selector fix: local index 2 on unit1 means machine gate
        11, but passed straight through it reads as gate 2 - another unit's, and it used to resolve
        to this unit's gate 0 hardware instead of failing.
        """
        lgate = 2
        with self.assertRaises(self.MmuError):
            self.unit1.drive_obj(lgate)
        self.assertIs(self.unit1.drive_obj(self.unit1.logical_gate(lgate)),
                      self.unit1.drive_obj(11))


class TestDryingState(MultiUnitTestCase):
    """
    Drying state is a per-unit array read and written from the environment timer. A negative index
    is valid Python, so a foreign gate used to read and overwrite the last gate's slot in silence -
    and the surrounding try/except never fired because nothing was raised.
    """

    def env(self):
        return self.unit0.environment_manager

    def test_a_foreign_gate_does_not_read_another_gates_slot(self):
        self.assertEqual(self.env()._state_get(self.foreign), '')

    def test_a_foreign_gate_does_not_overwrite_another_gates_slot(self):
        before = list(self.env()._drying_state)
        self.env()._state_set(self.foreign, 'active')
        self.assertEqual(self.env()._drying_state, before,
                         'writing a foreign gate corrupted this unit\'s state')

    def test_owned_gates_still_round_trip(self):
        self.env()._state_set(3, 'active')
        self.assertEqual(self.env()._state_get(3), 'active')


class TestHotPathsStayQuiet(MultiUnitTestCase):
    """
    The change only raises for hardware, precisely so the paths that cannot tolerate an exception
    keep declining. This is the regression guard for that.
    """

    def test_status_polling_survives_a_foreign_selected_gate(self):
        self.mmu.gate_selected = self.foreign     # unit1's gate, while unit0 objects still poll
        eventtime = self.hh.reactor.monotonic()

        self.mmu.get_status(eventtime)
        self.unit0.get_status(eventtime)
        self.unit0.environment_manager.get_status(eventtime)
        self.unit0.sync_feedback.get_status(eventtime)

        self.hh.settle(1.0)
        self.assertEqual(self.hh.errors, [])

    def test_a_deferred_rd_update_declines_instead_of_raising(self):
        """
        apply_gear_rd runs from a toolhead lookahead callback. It checks lgate >= 0 before touching
        the stepper, so a foreign gate must still reach it as -1 rather than as an exception.
        """
        self.mmu.gate_selected = self.foreign
        self.unit0.calibrator.apply_gear_rd(22.5)   # Must not raise
        self.hh.settle(0.)
        self.assertEqual(self.hh.errors, [])


if __name__ == '__main__':
    unittest.main()
