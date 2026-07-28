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


if __name__ == '__main__':
    unittest.main()
