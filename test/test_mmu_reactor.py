# Happy Hare test harness - self-test of the fake reactor.
#
# The reactor is the one fake with real, subtle behaviour (greenlet dispatch +
# virtual clock), so it is verified INDEPENDENTLY of Happy Hare. Without this, a
# greenlet deadlock would surface as an inscrutable hang somewhere deep in a bootup
# test and there would be no way to tell whose fault it was.
#
# The headline case is test_compound_endstop_pattern, which reproduces
# MmuCompoundEndstop.home_start (extras/mmu/mmu_sensor_utils.py:565-598) exactly:
# N reactor callbacks, each blocking on its own child completion, first trigger wins.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install

install()  # put the fake klippy tree on sys.path before importing reactor

import reactor as reactor_mod  # noqa: E402


class TestVirtualReactor(unittest.TestCase):

    def setUp(self):
        self.reactor = reactor_mod.VirtualReactor(start_time=1000.)

    def test_clock_only_moves_when_advanced(self):
        self.assertEqual(self.reactor.monotonic(), 1000.)
        self.reactor.advance(5.)
        self.assertEqual(self.reactor.monotonic(), 1005.)

    def test_timers_fire_in_time_order(self):
        fired = []

        def make(name):
            def cb(eventtime):
                fired.append((name, round(eventtime - 1000., 3)))
                return self.reactor.NEVER
            return cb

        now = self.reactor.monotonic()
        self.reactor.register_timer(make('c'), now + 3.)
        self.reactor.register_timer(make('a'), now + 1.)
        self.reactor.register_timer(make('b'), now + 2.)
        self.reactor.advance(5.)
        self.assertEqual(fired, [('a', 1.), ('b', 2.), ('c', 3.)])

    def test_timer_not_yet_due_does_not_fire(self):
        fired = []
        t = self.reactor.register_timer(
            lambda et: (fired.append(et), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 10.)
        self.reactor.advance(5.)
        self.assertEqual(fired, [])
        self.reactor.advance(6.)
        self.assertEqual(len(fired), 1)
        self.assertIsNotNone(t)

    def test_repeating_timer_rearms(self):
        ticks = []

        def cb(eventtime):
            ticks.append(eventtime)
            return eventtime + 1.

        self.reactor.register_timer(cb, self.reactor.monotonic() + 1.)
        self.reactor.advance(5.5)
        self.assertEqual(len(ticks), 5)

    def test_update_timer_reschedules(self):
        fired = []
        t = self.reactor.register_timer(
            lambda et: (fired.append(et), self.reactor.NEVER)[1], self.reactor.NEVER)
        self.reactor.advance(10.)
        self.assertEqual(fired, [])
        self.reactor.update_timer(t, self.reactor.monotonic() + 1.)
        self.reactor.advance(2.)
        self.assertEqual(len(fired), 1)

    def test_register_callback_runs_at_next_advance(self):
        seen = []
        self.reactor.register_callback(lambda et: seen.append('ran'))
        self.assertEqual(seen, [])          # nothing runs until the reactor is pumped
        self.reactor.advance(0.)            # advance(0) == settle
        self.assertEqual(seen, ['ran'])

    def test_completion_already_complete(self):
        c = self.reactor.completion()
        c.complete('done')
        self.assertTrue(c.test())
        result = []
        self.reactor.register_callback(lambda et: result.append(c.wait()))
        self.reactor.advance(0.)
        self.assertEqual(result, ['done'])

    def test_completion_waited_then_completed_by_timer(self):
        """A blocking wait inside a reactor callback, released by a later timer."""
        c = self.reactor.completion()
        result = []
        self.reactor.register_callback(lambda et: result.append(c.wait()))
        self.reactor.register_timer(
            lambda et: (c.complete('trigger'), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 2.)
        self.reactor.advance(3.)
        self.assertEqual(result, ['trigger'])

    def test_completion_wait_with_timeout(self):
        c = self.reactor.completion()
        result = []

        def waiter(eventtime):
            result.append(c.wait(self.reactor.monotonic() + 1., 'timed_out'))

        self.reactor.register_callback(waiter)
        self.reactor.advance(3.)
        self.assertEqual(result, ['timed_out'])

    def test_compound_endstop_pattern(self):
        """
        Reproduces MmuCompoundEndstop.home_start / _wait_for_child_endstop
        (extras/mmu/mmu_sensor_utils.py:565-598): one callback per child endstop,
        each blocking on its own completion, first to trigger wins. This is the
        pattern that forces greenlets - and the NFC compound endstop depends on it.
        """
        children = [self.reactor.completion() for _ in range(3)]
        parent = self.reactor.completion()
        state = {'winner': None, 'pending': len(children)}

        def wait_for_child(index, completion):
            def cb(eventtime):
                triggered = completion.wait()
                state['pending'] -= 1
                if triggered and state['winner'] is None:
                    state['winner'] = index
                    parent.complete(True)
                elif state['pending'] == 0 and state['winner'] is None:
                    parent.complete(False)
            return cb

        for i, c in enumerate(children):
            self.reactor.register_callback(wait_for_child(i, c))

        # Child 1 trips first (at t+1); child 0 would have tripped at t+2.
        self.reactor.register_timer(
            lambda et: (children[1].complete(True), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 1.)
        self.reactor.register_timer(
            lambda et: (children[0].complete(True), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 2.)

        self.reactor.advance(3.)
        self.assertTrue(parent.test())
        self.assertTrue(parent.result)
        self.assertEqual(state['winner'], 1, 'first endstop to trigger must win')

    def test_compound_endstop_all_children_fail(self):
        """No child triggers -> parent completes False once all have been resolved."""
        children = [self.reactor.completion() for _ in range(2)]
        parent = self.reactor.completion()
        state = {'winner': None, 'pending': len(children)}

        def wait_for_child(index, completion):
            def cb(eventtime):
                triggered = completion.wait(self.reactor.monotonic() + 1., False)
                state['pending'] -= 1
                if triggered and state['winner'] is None:
                    state['winner'] = index
                    parent.complete(True)
                elif state['pending'] == 0 and state['winner'] is None:
                    parent.complete(False)
            return cb

        for i, c in enumerate(children):
            self.reactor.register_callback(wait_for_child(i, c))
        self.reactor.advance(3.)
        self.assertTrue(parent.test())
        self.assertFalse(parent.result)

    def test_pause_outside_dispatch_jumps_the_clock(self):
        """
        HH calls reactor.pause() during config load / bootup, outside any dispatch.
        Real Klipper time.sleep()s; we jump the virtual clock so it is free.
        """
        start = self.reactor.monotonic()
        got = self.reactor.pause(start + 2.5)
        self.assertEqual(got, start + 2.5)
        self.assertEqual(self.reactor.monotonic(), start + 2.5)

    def test_pause_inside_callback_yields_to_other_work(self):
        order = []

        def sleeper(eventtime):
            order.append('sleep-start')
            self.reactor.pause(self.reactor.monotonic() + 2.)
            order.append('sleep-end')

        self.reactor.register_callback(sleeper)
        self.reactor.register_timer(
            lambda et: (order.append('other'), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 1.)
        self.reactor.advance(3.)
        self.assertEqual(order, ['sleep-start', 'other', 'sleep-end'],
                         'a paused callback must not block unrelated timers')

    def test_never_completed_wait_parks_instead_of_hanging(self):
        """
        A wait that is never completed parks its greenlet on a NEVER waketime, so
        advance() simply runs out of due work and returns - it does NOT spin or hang.

        Note what this does and does not buy us: advance() staying responsive means
        the *calling test* fails on its own assertion (e.g. "homing never
        triggered") rather than the suite wedging. It is not detected as an error
        here, because an outstanding completion is legitimate - HH parks on one
        whenever it is waiting for an endstop that has not tripped yet.
        """
        c = self.reactor.completion()
        self.reactor.register_callback(lambda et: c.wait())   # never completed
        self.reactor.advance(1.)
        self.assertFalse(c.test())
        self.assertEqual(self.reactor.monotonic(), 1001.)

    def test_watchdog_catches_a_runaway_timer(self):
        """A timer that re-arms immediately must trip the iteration cap, not spin."""
        self.reactor.register_timer(lambda et: self.reactor.NOW,
                                    self.reactor.monotonic())
        saved = reactor_mod.MAX_ITERATIONS
        reactor_mod.MAX_ITERATIONS = 500
        try:
            with self.assertRaises(AssertionError) as ctx:
                self.reactor.advance(1.)
            self.assertIn('iteration cap', str(ctx.exception))
            self.assertIn('Pending timers', str(ctx.exception))
        finally:
            reactor_mod.MAX_ITERATIONS = saved

    def test_run_until(self):
        flag = {'set': False}
        self.reactor.register_timer(
            lambda et: (flag.__setitem__('set', True), self.reactor.NEVER)[1],
            self.reactor.monotonic() + 1.5)
        self.assertTrue(self.reactor.run_until(lambda: flag['set'], timeout=5.))

    def test_run_until_times_out(self):
        with self.assertRaises(AssertionError):
            self.reactor.run_until(lambda: False, timeout=0.2, step=0.05)

    # -- parked callbacks --------------------------------------------------
    #
    # advance() has an EDGE that real Klipper's reactor does not: it stops once the
    # next timer falls past its target. A callback that pauses across that edge is
    # abandoned half-run and advance() returns as if all was well. This bit the NFC
    # reader init pass - every SPI reader booted dead because the first driver sleep
    # outran the pump window (see Session._settle_nfc_init) - so the detection and
    # the drain that fix it are pinned here.

    def _sleeper(self, log, naps, nap=0.05):
        """A callback that pauses `naps` times, logging how far it got."""
        def napping_callback(eventtime):
            for i in range(naps):
                self.reactor.pause(self.reactor.monotonic() + nap)
                log.append(i)
            return self.reactor.NEVER
        return napping_callback

    def test_callback_pausing_past_the_target_is_left_parked(self):
        """The hazard itself: advance() returns having run only part of a callback."""
        log = []
        self.reactor.register_callback(self._sleeper(log, naps=3))
        self.reactor.advance(0.01)      # far less than 3 x 0.05 of sleeping
        self.assertEqual(log, [], 'the callback should not have finished a nap yet')
        self.assertEqual(len(self.reactor.suspended_callbacks()), 1,
                         'a half-run callback must be visible as parked')

    def test_drain_suspended_finishes_a_parked_callback(self):
        log = []
        self.reactor.register_callback(self._sleeper(log, naps=3))
        self.reactor.advance(0.01)
        spent = self.reactor.drain_suspended()
        self.assertEqual(log, [0, 1, 2], 'every nap should have been waited out')
        self.assertEqual(self.reactor.suspended_callbacks(), [])
        self.assertGreater(spent, 0.)

    def test_drain_suspended_reports_a_callback_it_cannot_finish(self):
        """Budget exhaustion must name the parked work, not spin or pass quietly."""
        log = []
        self.reactor.register_callback(self._sleeper(log, naps=100))
        self.reactor.advance(0.01)
        with self.assertRaises(AssertionError) as ctx:
            self.reactor.drain_suspended(budget=0.2)
        self.assertIn('still parked', str(ctx.exception))
        self.assertIn('Pending timers', str(ctx.exception))

    def test_drain_suspended_scoped_to_one_callback_ignores_a_pausing_poller(self):
        """
        Why `inside` exists. A repeating timer that pauses inside its own callback -
        the shared NFC reader poll does exactly this - is parked again as fast as it
        clears, so an unfiltered drain never observes "nothing parked" and just burns
        its budget. Naming the callback actually being waited for fixes that.

        The poller here naps longer than the drain's step so it is reliably mid-pause
        at every step boundary; a nap shorter than the step would clear inside a single
        advance() and the trap would not reproduce.
        """
        log = []

        def poller(eventtime):
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            return eventtime + 0.01     # re-arms for ever

        self.reactor.register_timer(poller, self.reactor.monotonic())
        self.reactor.register_callback(self._sleeper(log, naps=3))
        self.reactor.advance(0.01)

        # Scoped to the sleeper, the drain terminates even with the poller parked.
        self.reactor.drain_suspended(inside='napping_callback', budget=1.)
        self.assertEqual(log, [0, 1, 2])
        self.assertTrue(self.reactor.suspended_callbacks(),
                        'the poller should still be parked - that is the point')
        # Unfiltered, the ever-re-parking poller keeps it from ever settling.
        with self.assertRaises(AssertionError):
            self.reactor.drain_suspended(budget=0.3)


if __name__ == '__main__':
    unittest.main()


class TestWallClockBudget(unittest.TestCase):
    """
    The wall-clock safety net must time the ADVANCE, not the greenlet.

    A dispatch greenlet parks inside pause() and is pooled, so its _dispatch_loop call
    can be suspended for unbounded real time - across later advance() calls, or while a
    user sits at the console prompt. A deadline captured when that invocation first
    started is long expired by the time the greenlet resumes, and trips on its next
    iteration having done no work. That produced a bogus "exceeded its wall-clock
    budget" on the first command after any pause, with iterations=2 and every timer
    healthy - see the console's _MMU_TEST / MMU_GATE_MAP reports.
    """

    def setUp(self):
        self.reactor = reactor_mod.VirtualReactor(start_time=1000.)

    def test_a_parked_greenlet_does_not_carry_a_stale_deadline(self):
        # A callback that parks the dispatch greenlet, exactly as a driver sleep or
        # save_variables' zero-length aio pause does.
        def sleeper(eventtime):
            self.reactor.pause(self.reactor.monotonic() + 0.01)
            return self.reactor.NEVER

        self.reactor.register_timer(sleeper, 1000.5)
        self.reactor.advance(1.)

        # Real time passes with the greenlet pooled - the user reading the screen.
        self.reactor._wall_deadline = (reactor_mod._wall.monotonic()
                                       - reactor_mod.MAX_WALL_SECONDS * 2)
        # The next advance must set its own deadline rather than inherit that one.
        self.reactor.advance(0.)
        self.reactor.advance(1.)
        self.assertGreater(self.reactor._wall_deadline, reactor_mod._wall.monotonic(),
                           'advance() reused an expired deadline')

    def test_the_budget_still_catches_a_genuine_hang(self):
        """The net must still work: a timer re-arming at the same instant forever."""
        def stuck(eventtime):
            return eventtime                    # due again immediately, forever

        self.reactor.register_timer(stuck, 1000.5)
        with self.assertRaises(AssertionError) as caught:
            self.reactor.advance(1.)
        self.assertIn('exceeded its', str(caught.exception))

    def test_the_watchdog_reports_the_clock_state(self):
        """now/target/next_timer is what distinguishes a hang from a stopped clock."""
        msg = self.reactor._watchdog_msg('iteration cap')
        for field in ('now=', 'target=', 'next_timer=', 'iterations=', 'Pending timers:'):
            self.assertIn(field, msg)
