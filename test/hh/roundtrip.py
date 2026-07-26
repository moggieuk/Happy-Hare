# Happy Hare test harness - the Klipper <-> Moonraker round trip.
#
# Runs the real MmuController (Klipper, sync/greenlet) and the real MmuServer
# (Moonraker, async/asyncio) in ONE process and closes the bidirectional contract.
#
# WHY TWO QUEUES RATHER THAN DIRECT CALLS. Production is fire-and-forget in both
# directions: Klipper's webhooks.call_remote_method hands off and returns
# immediately, and Moonraker's klippy_apis.run_gcode is awaited on Moonraker's own
# loop. Calling straight through would (a) let a Klipper gcode handler re-enter
# Moonraker mid-await, and (b) require nesting an asyncio loop inside a greenlet
# reactor callback. So each direction gets a queue and settle() alternates draining
# them until both are empty. That reproduces production ordering while staying fully
# deterministic - no sleeps, no nesting, no races.
#
#     Klipper                                    Moonraker
#     -------                                    ---------
#     webhooks.call_remote_method(...)  --->  webhooks.inbox
#                                                  |  settle() drains
#                                                  v
#                                             MmuServer.<method>(**kwargs)
#                                                  |
#     gcode.run_script("MMU_GATE_MAP ...") <---  klippy_apis.queue
#          ^  settle() drains
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

from .bootstrap import Session
from .moonraker import MoonrakerHarness

# Klipper calls this but mmu_server does NOT register it - Moonraker's own built-in
# spoolman component serves it (extras/mmu/mmu_controller.py:3030). Without a stub
# here the round trip would raise on any spool activation.
BUILTIN_METHODS = ('spoolman_set_active_spool',)


class RoundTrip:
    """
    A joined Klipper + Moonraker session. Both halves are REAL Happy Hare code; only
    their surroundings are faked.
    """

    def __init__(self, profile='nfc_spoolman', spools=(), num_gates=4,
                 hostname='testprinter', klipper_kwargs=None,
                 moonraker_kwargs=None):
        self.klipper = Session(profile, **(klipper_kwargs or {}))
        self.moonraker = MoonrakerHarness(
            spools=list(spools), num_gates=num_gates, hostname=hostname,
            **(moonraker_kwargs or {}))
        self.builtin_calls = []     # calls to Moonraker's own components
        self._settling = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        return self.boot()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def boot(self):
        self.klipper.boot()
        self.moonraker.component_init()
        # Bootup itself calls out to Moonraker (_spoolman_sync /
        # _moonraker_sync_lane_data), so settle before handing back a quiet machine.
        self.settle()
        return self

    def close(self):
        self.moonraker.close()
        self.klipper.close()

    # -- the pump ----------------------------------------------------------
    def settle(self, max_rounds=40):
        """
        Run the contract to quiescence: alternately deliver queued Klipper->Moonraker
        calls and queued Moonraker->Klipper gcode until neither side has work. This
        is what lets a test assert on the final state of an inherently async flow.
        """
        assert not self._settling, 'settle() is not reentrant'
        self._settling = True
        try:
            for round_num in range(max_rounds):
                progressed = False

                # Moonraker -> Klipper: gcode callbacks (MMU_GATE_MAP / MMU_LOG)
                for command in self.moonraker.klippy.drain():
                    logging.debug('roundtrip: klipper <- %s', command)
                    self.klipper.gcode.run_script(command)
                    progressed = True
                # Let any timers those handlers armed run (LED flashes, pending warn)
                self.klipper.reactor.advance(0.)

                # Klipper -> Moonraker: remote method calls
                for name, kwargs in self.klipper.webhooks.drain():
                    logging.debug('roundtrip: moonraker <- %s(%r)', name, kwargs)
                    self._dispatch(name, kwargs)
                    progressed = True

                if not progressed:
                    return round_num
            raise AssertionError(
                'settle() did not quiesce in %d rounds - the two sides are ping-'
                'ponging. Last Klipper gcode: %r; last Moonraker calls: %r'
                % (max_rounds, self.moonraker.klippy.gcode[-3:],
                   self.klipper.webhooks.calls[-3:]))
        finally:
            self._settling = False

    def _dispatch(self, name, kwargs):
        if name in BUILTIN_METHODS:
            self.builtin_calls.append((name, kwargs))
            return None
        handler = self.moonraker.server.remote_methods.get(name)
        if handler is None:
            raise AssertionError(
                'Klipper called remote method %r which Moonraker does not register. '
                'Either mmu_server dropped it, or it belongs to a built-in Moonraker '
                'component and should be listed in roundtrip.BUILTIN_METHODS. '
                'Registered: %s'
                % (name, ', '.join(sorted(self.moonraker.server.remote_methods))))
        return self.moonraker.run(handler(**kwargs))

    def advance(self, seconds):
        """
        Advance BOTH clocks together, then settle. Klipper's reactor and Moonraker's
        miss-cache clock are independent, so moving only one would leave the two
        halves seeing an inconsistent world.
        """
        self.klipper.reactor.advance(seconds)
        self.moonraker.advance(seconds)
        self.settle()
        return self

    # -- driving -----------------------------------------------------------
    def run_gcode(self, script):
        self.klipper.gcode.run_script(script)
        self.settle()
        return self

    def present_tag(self, uid, gate=None, unit=0, deep=True, **metadata):
        """
        Inject a tag read at the exact point a real reader hands off, using HH's own
        _MMU_TEST NFC_READ=1 hook (extras/mmu/commands/mmu_dev_test.py:1148 ->
        nfc_manager._dispatch_lookup). Every reader-level guard is bypassed by
        construction, so this needs no reader hardware and no driver.

        gate=None targets the unit's shared reader; gate=N a per-gate reader.
        """
        parts = ['_MMU_TEST', 'NFC_READ=1', 'UID=%s' % uid,
                 'DEEP=%d' % (1 if deep else 0)]
        if gate is not None:
            parts.append('GATE=%d' % gate)
        else:
            parts.append('UNIT=%d' % unit)
        for key, value in metadata.items():
            parts.append('%s=%s' % (key.upper(), value))
        return self.run_gcode(' '.join(parts))

    # -- shortcuts ---------------------------------------------------------
    @property
    def mmu(self):
        return self.klipper.mmu

    @property
    def db(self):
        return self.moonraker.db

    @property
    def errors(self):
        return self.klipper.errors

    def gate_map_commands(self):
        return self.moonraker.klippy.commands('MMU_GATE_MAP')

    def remote_calls(self, name=None):
        if name is None:
            return list(self.klipper.webhooks.calls)
        return self.klipper.webhooks.calls_to(name)

    def led_effects(self, unit=0):
        """The LED manager's recorded per-segment effect state for a unit."""
        return dict(self.klipper.printer.lookup_object('mmu')
                    .led_manager.effect_state.get(unit, {}))


def roundtrip(**kwargs):
    return RoundTrip(**kwargs)
