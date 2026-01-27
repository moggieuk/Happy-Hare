# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Helper class for callback based extruder monitoring.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

from .mmu_shared import MmuError

class ExtruderMonitor:
    """
    Periodically samples the extruder position and notifies registered callbacks
    when their per-callback movement thresholds are crossed (in either direction).

    - Each callback tracks its own signed movement accumulator since it was registered
      or last triggered. The callback is invoked with the *signed* distance moved.
    - Global enable/disable is controlled by (re)scheduling the reactor timer.
    """

    CHECK_INTERVAL = 0.5 # How often to check extruder movement (seconds)

    def __init__(self, mmu):
        self.mmu = mmu

        self.enabled = True
        self._last_pos = None # Last absolute extruder position

        # Per-callback state:
        #   { callback: {"threshold": float, "accum": float} }
        self._callbacks = {}

        self._timer = self.mmu.reactor.register_timer(self._check_extruder_movement)
        self.enable() # Start now


    def enable(self):
        """
        Globally enable monitoring and start the watchdog immediately.
        """
        self.mmu.reactor.update_timer(self._timer, self.mmu.reactor.NOW)
        self.enabled = True


    def disable(self):
        """
        Globally disable monitoring and stop the watchdog.
        """
        self.mmu.reactor.update_timer(self._timer, self.mmu.reactor.NEVER)
        self.enabled = False


    def register_callback(self, cb, movement_threshold):
        """
        Register a callback to be notified when accumulated movement crosses
        the threshold (in either direction).
          Args:
            cb: Callable taking one positional arg: signed_distance_mm (float).
            movement_threshold: Positive float, in mm. Must be > 0.
          Behavior:
            - Resets this callback's accumulator to 0 on registration.
            - If already registered, updates the threshold and resets accumulator.
        """
        if not callable(cb):
            raise TypeError("cb must be callable")
        if movement_threshold is None or movement_threshold <= 0:
            raise ValueError("movement_threshold must be a positive float")

        self._callbacks[cb] = {"threshold": float(movement_threshold), "accum": 0.0}

        # Ensure the timer is running if globally enabled
        if self.enabled:
            self.mmu.reactor.update_timer(self._timer, self.mmu.reactor.NOW)


    def remove_callback(self, cb):
        """
        Unregister a previously registered callback. Silently ignores unknown cbs.
        """
        self._callbacks.pop(cb, None)


    def get_and_reset_accumulated(self, cb):
        """
        Get the currently accumulated signed distance for a given callback
        and reset its accumulator to zero.
          Args:
            cb: The same callback object that was passed to register_callback().
          Returns:
            float: The signed accumulated distance in mm since registration or
                   the last reset/trigger.
          Raises:
            KeyError: If the callback is not currently registered.
        """
        state = self._callbacks.get(cb)
        if state is None:
            raise KeyError("Callback is not registered with ExtruderMonitor")

        distance = state["accum"]
        state["accum"] = 0.0
        return distance


    # ---- Internal Implementation ----

    def _check_extruder_movement(self, eventtime):
        """
        Reactor timer entrypoint. Returns the next wakeup time.
        """
        if not self.enabled or not self._callbacks:
            return eventtime + self.CHECK_INTERVAL

        mcu = self.mmu.printer.lookup_object('mcu')
        est_print_time = mcu.estimated_print_time(eventtime)
        pos = self.mmu.toolhead.get_extruder().find_past_position(est_print_time)

        # Initialize last position on first successful read
        if self._last_pos is None:
            self._last_pos = pos
            return eventtime + self.CHECK_INTERVAL

        # Compute signed delta since last sample
        delta = pos - self._last_pos
        self._last_pos = pos

        if delta != 0.0:
            # Accumulate per-subscriber and trigger as needed
            to_trigger = []
            for cb, state in self._callbacks.items():
                state["accum"] += delta  # Signed accumulation
                threshold = state["threshold"]

                if abs(state["accum"]) >= threshold:
                    # Capture the actual signed distance since last trigger and reset
                    signed_distance = state["accum"]
                    state["accum"] = 0.0
                    to_trigger.append((cb, signed_distance))

            # Second loop to avoid reentrancy issues if callbacks mutate subscriptions
            for cb, signed_distance in to_trigger:
                try:
                    cb(eventtime, signed_distance)
                except MmuError as ee:
                    self.mmu.log_error("Error calling callback: %s" % str(ee))

        # Reschedule
        return eventtime + self.CHECK_INTERVAL
