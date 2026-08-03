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


class ReactorError(Exception):
    pass


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


class ReactorPreventPause:
    """Ported from klipper's reactor.py:96-102. A nesting counter, not a flag."""
    def __init__(self, reactor):
        self.reactor = reactor

    def __enter__(self):
        self.reactor._prevent_pause_count += 1

    def __exit__(self, type=None, value=None, tb=None):
        self.reactor._prevent_pause_count -= 1


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

    def _g_dispatch_is_none(self):
        return self.reactor._g_dispatch is None

    def __enter__(self):
        if not self.is_locked:
            self.is_locked = True
            return
        if self._g_dispatch_is_none():
            # Waiting here would spin, not block: pause() outside a running dispatch
            # goes to _sys_pause, which returns immediately for NEVER, so the loop
            # below never yields and nothing can ever release the mutex. Real klipper
            # cannot reach this - its reactor never stops - so it is always a harness
            # bug (usually: pump the reactor before issuing gcode). Fail legibly
            # instead of hanging the suite with no diagnostic.
            raise AssertionError(
                'gcode mutex is held and there is no running dispatch to release it. '
                'A reactor callback is probably parked holding or queued on it - call '
                'reactor.advance(0.) before issuing more gcode.'
            )
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
        self._prevent_pause_count = 0
        self._greenlets = []
        self._all_greenlets = []
        self._process = False
        self.iterations = 0
        self._wall_deadline = None
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

    # -- pause guard -------------------------------------------------------
    # Ported from klipper's reactor.py:270-275. Klipper wraps contexts where a
    # greenlet switch would be unsafe - the whole klippy:ready handler loop
    # (klippy.py:161), invoke_shutdown (:210), every get_status() (gcode_macro.py:30,
    # webhooks.py:495) - and anything that pauses inside one raises.
    #
    # Only Session.ready() enters such a block today, so in practice the guard only
    # sees SAVE_VARIABLE. It sits at the top of pause() though, so it covers every
    # pause path - ReactorMutex.__enter__ and ReactorCompletion.wait included. A
    # future harness caller that wraps get_status() or shutdown dispatch the way
    # klipper does will widen its reach accordingly.
    def assert_no_pause(self):
        return ReactorPreventPause(self)

    def verify_can_pause(self):
        if self._prevent_pause_count:
            raise ReactorError("Internal error - reactor pause disabled")

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
        # Guard FIRST, unlike klipper's reactor.py:236-240 which checks _g_dispatch
        # before _prevent_pause_count. Real klipper dispatches klippy:ready from
        # _connect, which IS a reactor callback, so _g_dispatch is never None inside
        # its assert_no_pause block. The harness fires ready from the main greenlet
        # (bootstrap.py Session.ready), so deferring to klipper's ordering would send
        # every ready-time pause down _sys_pause and make the assertion unreachable.
        self.verify_can_pause()
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

    @staticmethod
    def _parked_inside(g, name):
        """True if greenlet `g` is parked with `name` somewhere on its call stack."""
        frame = g.gr_frame
        while frame is not None:
            if frame.f_code.co_name == name:
                return True
            frame = frame.f_back
        return False

    def suspended_callbacks(self, inside=None):
        """
        Timers that exist only to resume a callback parked in pause(), i.e. callbacks
        that are half-run right now. `inside` narrows the list to those parked with
        that function name on the stack.

        THE HAZARD THIS EXPOSES. advance(dt) stops at `_next_timer > _target`, so a
        callback that pauses for longer than the remaining window is simply left
        parked - advance() returns normally, having run only PART of it, and nothing
        anywhere says so. Real Klipper never has this problem because its reactor
        keeps running; here the pump has an edge, and pausing across it silently
        truncates the callback. That is not hypothetical: it is why every SPI NFC
        reader used to boot dead (see Session._settle_nfc_init).

        pause() parks a greenlet by registering `g.switch` as the resume timer
        (:220), and _end_greenlet unregisters it once the callback finally returns
        (:227). So a live bound-method-of-a-greenlet timer means "still parked".
        Ordinary self-re-arming timers are bound to plain functions and never match.
        """
        found = []
        for t in self._timers:
            if t.waketime >= self.NEVER:
                continue
            g = getattr(t.callback, '__self__', None)
            if not isinstance(g, greenlet.greenlet):
                continue
            if inside is not None and not self._parked_inside(g, inside):
                continue
            found.append(t)
        return found

    def drain_suspended(self, inside=None, budget=2., step=0.05):
        """
        Advance in small steps until nothing is parked mid-pause, and return the
        virtual time spent. Raises if `budget` runs out.

        Use this after pumping to a point where a callback is EXPECTED to have run to
        completion but sleeps its way through several pauses - the reader-init pass is
        the motivating case. Stepping rather than advancing `budget` in one go keeps
        the overshoot to only what the parked callbacks actually needed, which matters
        because overshoot on a live reactor means extra poll cycles.

        PASS `inside` WHENEVER A REPEATING TIMER MIGHT ALSO PAUSE, or this will not
        terminate. Draining unfiltered means "wait for the reactor to hold no parked
        callback at all", and a 1 Hz poll whose own callback pauses (the shared NFC
        reader does exactly this once init arms it) keeps re-parking, so the condition
        is never observed true and the budget always blows. Naming the callback being
        waited for makes the loop watch only that one.

        NOT a general "settle" either way: a callback blocked on completion.wait() is
        also parked, and one waiting on an event that only a later test action triggers
        (a homing completion, say) would never clear. Reach for this only where the
        pause being waited out is a driver sleep.
        """
        spent = 0.
        while self.suspended_callbacks(inside):
            if spent >= budget:
                raise AssertionError(
                    'drain_suspended(inside=%r): %d callback(s) still parked after '
                    '%.2fs of virtual time. Pending timers: %r'
                    % (inside, len(self.suspended_callbacks(inside)), spent,
                       [(getattr(t.callback, '__qualname__', repr(t.callback)),
                         t.waketime) for t in self._timers
                        if t.waketime < self.NEVER]))
            self.advance(step)
            spent += step
        return spent

    def _dispatch_loop(self):
        self._g_dispatch = greenlet.getcurrent()
        # The deadline belongs to the ADVANCE, not to this greenlet's invocation of the
        # loop, and reading it from the reactor is what makes that true.
        #
        # A dispatch greenlet parks inside pause() and is pooled, so its _dispatch_loop
        # call can be suspended for unbounded REAL time - across later advance() calls,
        # or simply while the user sits at the console prompt. A deadline captured in a
        # local when that invocation first started is then long past by the time the
        # greenlet is resumed, and trips on its very next iteration having done no work
        # at all. That is not a hang; it is a stopwatch nobody stopped, and it produced
        # a bogus "wall-clock budget exceeded" on the first command after any pause -
        # reported with iterations=2 and every timer perfectly healthy.
        try:
            while self._process:
                if self._next_timer > self._target:
                    break                   # nothing more due within this advance()
                self.iterations += 1
                if self.iterations > MAX_ITERATIONS:
                    raise AssertionError(self._watchdog_msg('iteration cap'))
                if _wall.monotonic() > self._wall_deadline:
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
        # The clock state is in here because without it these reports are unreadable:
        # whether _now has reached _target is the difference between "waiting for time
        # that will never come" and "genuinely stuck", and _next_timer says which.
        return ("VirtualReactor.advance() exceeded its %s at t=%.6f. This normally "
                "means a completion.wait() will never be completed, or a timer "
                "re-arms itself immediately.\n"
                "now=%.6f target=%.6f next_timer=%.6f iterations=%d\n"
                "Pending timers: %r"
                % (why, self._now, self._now, self._target, self._next_timer,
                   self.iterations, pending))

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
        self._wall_deadline = _wall.monotonic() + MAX_WALL_SECONDS
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
