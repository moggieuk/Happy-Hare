# Fake Klipper `klippy/reactor.py` for the Happy Hare test harness.
#
# Two deliberate differences from real Klipper, everything else ported closely:
#
#  1. VIRTUAL CLOCK. monotonic() returns a harness-controlled float advanced by
#     advance(dt). Happy Hare is full of multi-second timers - the pending spool_id
#     timeout (20s), PENDING_LED_WARN_WINDOW (5s), the NFC lookup window (10s), LED
#     flash durations, and BOOT_DELAY (2.5s, extras/mmu/mmu_constants.py:69) - and
#     real Klipper's reactor binds monotonic straight to wall clock with no
#     fast-forward anywhere. Sleeping through those would make the suite unusable.
#  2. No fd polling and no cross-thread async queue: the harness is single-threaded
#     and has no sockets.
#
# The GREENLET DISPATCH IS NOT SIMPLIFIED, and that is the important part.
# MmuCompoundEndstop.home_start (extras/mmu/mmu_sensor_utils.py:565-580) registers
# one reactor callback per child endstop, each of which blocks on
# child_completion.wait() (:586). That is a blocking wait *inside* a reactor
# callback, several concurrently outstanding. A single-threaded fake reactor
# deadlocks there; a re-entrant pump would silently change ordering in exactly the
# code path the NFC compound endstop lives in. So we use greenlets, like Klipper.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import time as _wall

import greenlet

_NOW = 0.
_NEVER = 9999999999999999.

# advance() safety net. A hung completion.wait() must be a fast, legible failure
# with the pending work attached, never a hang.
MAX_ITERATIONS = 100000
MAX_WALL_SECONDS = 10.


class ReactorTimer:
    def __init__(self, callback, waketime):
        self.callback = callback
        self.waketime = waketime


class ReactorCompletion:
    class sentinel:
        pass

    def __init__(self, reactor):
        self.reactor = reactor
        self.result = self.sentinel
        self.waiting = []

    def test(self):
        return self.result is not self.sentinel

    def complete(self, result):
        self.result = result
        for wait in self.waiting:
            self.reactor.update_timer(wait.timer, self.reactor.NOW)

    def wait(self, waketime=_NEVER, waketime_result=None):
        if self.result is self.sentinel:
            wait = greenlet.getcurrent()
            self.waiting.append(wait)
            self.reactor.pause(waketime)
            self.waiting.remove(wait)
            if self.result is self.sentinel:
                return waketime_result
        return self.result


class ReactorCallback:
    def __init__(self, reactor, callback, waketime):
        self.reactor = reactor
        self.timer = reactor.register_timer(self.invoke, waketime)
        self.callback = callback
        self.completion = ReactorCompletion(reactor)

    def invoke(self, eventtime):
        self.reactor.unregister_timer(self.timer)
        res = self.callback(eventtime)
        self.completion.complete(res)
        return self.reactor.NEVER


class ReactorGreenlet(greenlet.greenlet):
    def __init__(self, run):
        greenlet.greenlet.__init__(self, run=run)
        self.timer = None


class ReactorMutex:
    def __init__(self, reactor, is_locked):
        self.reactor = reactor
        self.is_locked = is_locked
        self.next_pending = False
        self.queue = []
        self.lock = self.__enter__
        self.unlock = self.__exit__

    def test(self):
        return self.is_locked

    def __enter__(self):
        if not self.is_locked:
            self.is_locked = True
            return
        g = greenlet.getcurrent()
        self.queue.append(g)
        while 1:
            self.reactor.pause(self.reactor.NEVER)
            if self.next_pending and self.queue[0] is g:
                self.next_pending = False
                self.queue.pop(0)
                return

    def __exit__(self, type=None, value=None, tb=None):
        if not self.queue:
            self.is_locked = False
            return
        self.next_pending = True
        self.reactor.update_timer(self.queue[0].timer, self.reactor.NOW)


class VirtualReactor:
    NOW = _NOW
    NEVER = _NEVER

    def __init__(self, start_time=1000.):
        self._now = float(start_time)
        self._target = self._now
        self._timers = []
        self._next_timer = self.NEVER
        self._g_dispatch = None
        self._greenlets = []
        self._all_greenlets = []
        self._process = False
        self.iterations = 0
        self._pending_error = None

    # -- clock -------------------------------------------------------------
    def monotonic(self):
        return self._now

    # -- timers ------------------------------------------------------------
    def update_timer(self, timer_handler, waketime):
        timer_handler.waketime = waketime
        self._next_timer = min(self._next_timer, waketime)

    def register_timer(self, callback, waketime=NEVER):
        timer_handler = ReactorTimer(callback, waketime)
        timers = list(self._timers)
        timers.append(timer_handler)
        self._timers = timers
        self._next_timer = min(self._next_timer, waketime)
        return timer_handler

    def unregister_timer(self, timer_handler):
        timer_handler.waketime = self.NEVER
        timers = list(self._timers)
        if timer_handler in timers:
            timers.pop(timers.index(timer_handler))
        self._timers = timers

    def _check_timers(self, eventtime):
        """Ported from Klipper's SelectReactor._check_timers (minus gc/idle logic)."""
        self._next_timer = self.NEVER
        g_dispatch = self._g_dispatch
        for t in self._timers:
            waketime = t.waketime
            if eventtime >= waketime:
                t.waketime = self.NEVER
                t.waketime = waketime = t.callback(eventtime)
                if g_dispatch is not self._g_dispatch:
                    # The callback paused; a new dispatch greenlet took over.
                    self._next_timer = min(self._next_timer, waketime)
                    self._end_greenlet(g_dispatch)
                    return
            self._next_timer = min(self._next_timer, waketime)

    # -- callbacks and completions ----------------------------------------
    def completion(self):
        return ReactorCompletion(self)

    def register_callback(self, callback, waketime=NOW):
        return ReactorCallback(self, callback, waketime).completion

    def register_async_callback(self, callback, waketime=NOW):
        # Harness is single-threaded, so this is just a callback.
        return ReactorCallback(self, callback, waketime).completion

    def async_complete(self, completion, result):
        completion.complete(result)

    def mutex(self, is_locked=False):
        return ReactorMutex(self, is_locked)

    # -- greenlets ---------------------------------------------------------
    def _sys_pause(self, waketime):
        """
        pause() outside a running dispatch. Real Klipper time.sleep()s here; we jump
        the virtual clock instead so a pause during config load or bootup is free.
        """
        if waketime > self._now and waketime < self.NEVER:
            self._now = waketime
        return self._now

    def pause(self, waketime):
        g = greenlet.getcurrent()
        if g is not self._g_dispatch:
            if self._g_dispatch is None:
                return self._sys_pause(waketime)
            return self._g_dispatch.switch(waketime)
        # Pausing the dispatch greenlet - hand dispatch to another greenlet
        if self._greenlets:
            g_next = self._greenlets.pop()
        else:
            g_next = ReactorGreenlet(run=self._dispatch_loop)
            self._all_greenlets.append(g_next)
        g_next.parent = g.parent
        g.timer = self.register_timer(g.switch, waketime)
        self._next_timer = self.NOW
        eventtime = g_next.switch()
        return eventtime

    def _end_greenlet(self, g_old):
        self._greenlets.append(g_old)
        self.unregister_timer(g_old.timer)
        g_old.timer = None
        self._g_dispatch.switch(self.NEVER)
        self._g_dispatch = g_old

    def in_dispatch(self):
        """
        True while a reactor callback is executing. Callers that would otherwise pump
        the reactor must check this: advance() is not reentrant, and calling it from
        inside a callback is a bug.
        """
        return self._g_dispatch is not None

    def _dispatch_loop(self):
        self._g_dispatch = greenlet.getcurrent()
        wall_deadline = _wall.monotonic() + MAX_WALL_SECONDS
        try:
            while self._process:
                if self._next_timer > self._target:
                    break                   # nothing more due within this advance()
                self.iterations += 1
                if self.iterations > MAX_ITERATIONS:
                    raise AssertionError(self._watchdog_msg('iteration cap'))
                if _wall.monotonic() > wall_deadline:
                    raise AssertionError(self._watchdog_msg('wall-clock budget'))
                eventtime = max(self._now, min(self._next_timer, self._target))
                self._now = eventtime
                self._check_timers(eventtime)
        except BaseException as e:
            # NEVER let a callback exception vanish. greenlet delivers an exception to
            # the greenlet's PARENT, and with dispatch handed between greenlets that
            # can mean it is reported to stderr and dropped - which is exactly how a
            # harness bug once masqueraded as Happy Hare quietly declining to finish an
            # operation. Stash it and re-raise from advance().
            self._pending_error = e
        finally:
            self._g_dispatch = None

    def _watchdog_msg(self, why):
        pending = [(getattr(t.callback, '__qualname__', repr(t.callback)), t.waketime)
                   for t in self._timers if t.waketime < self.NEVER]
        return ("VirtualReactor.advance() exceeded its %s at t=%.3f. This normally "
                "means a completion.wait() will never be completed, or a timer "
                "re-arms itself immediately.\nPending timers: %r"
                % (why, self._now, pending))

    # -- test-facing pump --------------------------------------------------
    def advance(self, dt=0.):
        """
        Run every timer/callback due within the next `dt` seconds of virtual time,
        in time order, then leave the clock at now+dt. advance(0) drains work that
        is already due without moving the clock ("settle").
        """
        assert dt >= 0., 'cannot advance time backwards'
        assert self._g_dispatch is None, 'advance() called from inside a callback'
        self._target = self._now + dt
        self._process = True
        self.iterations = 0
        self._pending_error = None
        try:
            g = ReactorGreenlet(run=self._dispatch_loop)
            self._all_greenlets.append(g)
            g.switch()
        finally:
            self._process = False
            self._g_dispatch = None
        if self._pending_error is not None:
            error, self._pending_error = self._pending_error, None
            raise error
        self._now = self._target
        return self._now

    def run_until(self, predicate, timeout=60., step=0.05):
        """Advance in small steps until `predicate()` is true; assert on timeout."""
        elapsed = 0.
        while elapsed < timeout:
            if predicate():
                return True
            self.advance(step)
            elapsed += step
        raise AssertionError('run_until: predicate never became true within %.1fs '
                             'of virtual time' % (timeout,))

    def finalize(self):
        self._process = False


# Klipper's module exposes SelectReactor / EPollReactor; HH only ever receives the
# instance via printer.get_reactor(), but alias for anything doing isinstance.
SelectReactor = VirtualReactor
Reactor = VirtualReactor
