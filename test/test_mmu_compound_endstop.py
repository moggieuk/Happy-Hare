# Happy Hare test harness - MmuCompoundEndstop winner resolution.
#
# A compound endstop homes several endstops at once, first wins, and callers then ask
# get_triggered_endstop_name() WHICH one stopped the move. Two callers act on the answer:
# _home_to_gate_with_nfc decides whether the tag or the gate switch arrived first, and
# _jog_scan tells a datum trip from a tag detection. Getting it wrong is not cosmetic -
# a mis-named gate win has the scan believe it is sitting on the gate datum when it is
# actually at the reader.
#
# WHY THESE TESTS EXIST AS UNIT TESTS. The integration tests cannot see this: in the
# harness geometry the scan finds the tag and returns to park whether the winner is named
# correctly or not (it just finds it on a later leg), so they stay green under a mutation
# that stops recording the winner entirely. Only asserting the class contract directly
# catches it.
#
# THE DISCRIMINATOR. home_wait judges each child by what its own home_wait() returned:
#
#   Klipper MCU_endstop        hit -> print_time | no trigger -> exactly 0. | timeout -> raises
#   MmuVirtualEndstopSensor    hit -> trigger time | no trigger -> raises
#   harness MCU_endstop        hit -> print_time | no trigger -> raises
#
# so "triggered" is `no exception and value != 0.` - deliberately NOT Klipper's own
# `> 0.` (klippy/extras/homing.py). Print times are not necessarily positive: this
# harness offsets them by HOST_OFFSET=1234.5 against a reactor starting at 1000.0, so a
# perfectly good trigger time here is about -234.5. See test_a_negative_trigger_time_counts.
#
#   ./venv/bin/python -m unittest test.test_mmu_compound_endstop
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install

install()   # Put the fake klippy root on sys.path before importing MMU modules

import mcu                                                          # noqa: E402
from extras.mmu.mmu_sensor_utils import MmuCompoundEndstop           # noqa: E402


class _Error(Exception):
    pass


class _FakePrinter:
    command_error = _Error

    def get_reactor(self):
        raise AssertionError('home_wait must not need the reactor')


class _StubVirtual:
    """Stands in for MmuVirtualEndstopSensor: raises when it did not trigger."""

    def __init__(self, trigger_time=None, error=None):
        self.trigger_time = trigger_time
        self.error = error
        self.home_waits = 0

    def home_wait(self, home_end_time):
        self.home_waits += 1
        if self.error is not None:
            raise self.error
        if self.trigger_time is None:
            raise _Error('No trigger')
        return self.trigger_time


class _StubMcu(mcu.MCU_endstop):
    """
    Stands in for a real Klipper MCU_endstop. Must pass isinstance(x, mcu.MCU_endstop)
    because that is how the compound decides which child is the hardware one, so it
    subclasses rather than duck-types - and skips the parent __init__ deliberately.
    """

    def __init__(self, trigger_time=0., error=None):
        self.trigger_time = trigger_time
        self.error = error
        self.home_waits = 0

    def home_wait(self, home_end_time):
        self.home_waits += 1
        if self.error is not None:
            raise self.error
        return self.trigger_time      # real Klipper returns 0. for "no trigger"


def compound(*children):
    """children: (endstop, name) pairs, in the order the compound should see them."""
    return MmuCompoundEndstop(_FakePrinter(), name='test_compound', endstops=list(children))


HOME_END = 500.0


class TestWinnerIsAlwaysNamed(unittest.TestCase):

    def test_the_virtual_child_wins(self):
        gate, nfc = _StubMcu(trigger_time=0.), _StubVirtual(trigger_time=120.0)
        c = compound((gate, 'mmu_exit_0'), (nfc, 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), 120.0)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_nfc_0')

    def test_the_mcu_child_wins(self):
        gate, nfc = _StubMcu(trigger_time=90.0), _StubVirtual(trigger_time=None)
        c = compound((gate, 'mmu_exit_0'), (nfc, 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), 90.0)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_exit_0')

    def test_the_name_is_never_none_after_a_successful_home(self):
        """
        The whole point. Callers compare the name against an endstop name, so a None
        silently reads as "the other one" - which is how a tag detection got
        mis-classified as a datum trip.
        """
        for gate_t, nfc_t in ((0., 120.0), (90.0, None), (90.0, 120.0)):
            with self.subTest(gate=gate_t, nfc=nfc_t):
                c = compound((_StubMcu(trigger_time=gate_t), 'mmu_exit_0'),
                             (_StubVirtual(trigger_time=nfc_t), 'mmu_nfc_0'))
                c.home_wait(HOME_END)
                self.assertIsNotNone(c.get_triggered_endstop_name())

    def test_every_child_is_closed_out_exactly_once(self):
        """
        Children are not re-callable: MCU_endstop.home_wait disarms via _dispatch.stop()
        and MmuNfcEndstop.home_wait stops the presence poll. So the compound must call
        each one once, including the loser.
        """
        gate, nfc = _StubMcu(trigger_time=90.0), _StubVirtual(trigger_time=None)
        c = compound((gate, 'mmu_exit_0'), (nfc, 'mmu_nfc_0'))
        c.home_wait(HOME_END)
        self.assertEqual((gate.home_waits, nfc.home_waits), (1, 1))


class TestTheDiscriminator(unittest.TestCase):

    def test_zero_does_not_count_as_a_trigger(self):
        """0. is Klipper's documented "no trigger" sentinel for MCU_endstop."""
        c = compound((_StubMcu(trigger_time=0.), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=None), 'mmu_nfc_0'))
        with self.assertRaises(_Error):
            c.home_wait(HOME_END)

    def test_a_negative_trigger_time_counts(self):
        """
        Pins `!= 0.` against Klipper's `> 0.`. Harness print_time is
        reactor(1000.) - HOST_OFFSET(1234.5) = about -234.5, so negative times are the
        NORMAL case in tests. A `> 0.` discriminator would call every one a miss and
        break the suite wholesale.
        """
        c = compound((_StubMcu(trigger_time=0.), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=-234.5), 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), -234.5)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_nfc_0')


class TestTieBreaking(unittest.TestCase):

    def test_the_earliest_trigger_wins(self):
        c = compound((_StubMcu(trigger_time=150.0), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=100.0), 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), 100.0)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_nfc_0')

    def test_an_exact_tie_falls_to_insertion_order(self):
        """
        Gate first in the NFC compound, and gate-first is the safe direction: callers
        read a gate win as "we are on the datum", and a genuine tie means the switch
        really did trigger.
        """
        c = compound((_StubMcu(trigger_time=100.0), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=100.0), 'mmu_nfc_0'))
        c.home_wait(HOME_END)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_exit_0')

    def test_a_pre_triggered_child_beats_a_later_real_trip(self):
        """
        A child already in its sought state when armed completes immediately and reports
        the ARM print_time, earlier than any later trip. It wins, and that is right: a
        pre-triggered endstop is exactly why the move could not go anywhere.
        """
        arm_time = 10.0
        c = compound((_StubMcu(trigger_time=180.0), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=arm_time), 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), arm_time)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_nfc_0')

    def test_two_virtual_children_are_still_deterministic(self):
        """mmu_misc_mixins builds compounds from N arbitrary endstops, possibly with NO
        MCU child at all, so resolution must not assume one-MCU-plus-one-virtual."""
        c = compound((_StubVirtual(trigger_time=100.0), 'compression'),
                     (_StubVirtual(trigger_time=100.0), 'tension'))
        c.home_wait(HOME_END)
        self.assertEqual(c.get_triggered_endstop_name(), 'compression')


class TestFailurePaths(unittest.TestCase):

    def test_no_trigger_reraises_the_childs_own_message(self):
        """
        The virtual child's error names the endstop that failed to trigger, which is more
        use than the compound's generic wording, so it is preferred.
        """
        gate = _StubMcu(trigger_time=0.)
        nfc = _StubVirtual(error=_Error('No trigger on mmu_nfc_0 after full movement'))
        c = compound((gate, 'mmu_exit_0'), (nfc, 'mmu_nfc_0'))
        with self.assertRaises(_Error) as ctx:
            c.home_wait(HOME_END)
        self.assertIn('mmu_nfc_0', str(ctx.exception))

    def test_no_trigger_with_no_child_error_uses_the_compound_message(self):
        """All-MCU compound: every child returns 0. and none raises."""
        c = compound((_StubMcu(trigger_time=0.), 'mmu_exit_0'))
        with self.assertRaises(_Error) as ctx:
            c.home_wait(HOME_END)
        self.assertIn('test_compound', str(ctx.exception))

    def test_a_child_exception_propagates_rather_than_being_swallowed(self):
        """
        THE SILENT-FAILURE FIX. A comms timeout used to be reported as a clean home: the
        callback crowned the MCU child off its failure completion, so _triggered_endstop
        was set, and home_wait returned home_end_time while discarding the child's
        "Communication timeout during homing".

        Also pins the ranking. The virtual child raises here too - that is just its
        ordinary no-trigger signal - and it must not mask the hardware fault. A real
        MCU_endstop RETURNS 0. for a plain no-trigger, so if it raised at all something
        actually broke.
        """
        boom = _Error('Communication timeout during homing')
        c = compound((_StubMcu(error=boom), 'mmu_exit_0'),
                     (_StubVirtual(error=_Error('No trigger on mmu_nfc_0')), 'mmu_nfc_0'))
        with self.assertRaises(_Error) as ctx:
            c.home_wait(HOME_END)
        self.assertIn('Communication timeout', str(ctx.exception))

    def test_the_mcu_fault_wins_even_when_it_is_not_first(self):
        """Ranking must not depend on insertion order."""
        boom = _Error('Communication timeout during homing')
        c = compound((_StubVirtual(error=_Error('No trigger on mmu_nfc_0')), 'mmu_nfc_0'),
                     (_StubMcu(error=boom), 'mmu_exit_0'))
        with self.assertRaises(_Error) as ctx:
            c.home_wait(HOME_END)
        self.assertIn('Communication timeout', str(ctx.exception))

    def test_a_raising_child_never_wins(self):
        """Even when the other child legitimately triggered."""
        c = compound((_StubMcu(error=_Error('boom')), 'mmu_exit_0'),
                     (_StubVirtual(trigger_time=120.0), 'mmu_nfc_0'))
        self.assertEqual(c.home_wait(HOME_END), 120.0)
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_nfc_0')


class TestLateChildCallback(unittest.TestCase):
    """
    home_wait sets _trigger_completion to None, so a child callback arriving afterwards
    must not touch it. The `if not self._homing` guard is what protects that, and it has
    to stay ahead of any other check - hence no _trigger_completion.test() in there.
    """

    class _StubCompletion:
        def wait(self):
            return True

    def test_a_callback_after_home_wait_is_harmless(self):
        gate, nfc = _StubMcu(trigger_time=90.0), _StubVirtual(trigger_time=None)
        c = compound((gate, 'mmu_exit_0'), (nfc, 'mmu_nfc_0'))
        c.home_wait(HOME_END)
        self.assertIsNone(c._trigger_completion)
        c._wait_for_child_endstop(nfc, self._StubCompletion())   # must not raise
        self.assertEqual(c.get_triggered_endstop_name(), 'mmu_exit_0',
                         'a late callback must not change the resolved winner')


if __name__ == '__main__':
    unittest.main()
