# Happy Hare test harness - a 1-D filament path model.
#
# Enough physics to make Happy Hare's LOADING/UNLOADING SEQUENCING run: gate homing,
# parking, preload, and the NFC jog scan. Deliberately NOT a motion simulator - there
# is no acceleration, no step generation and no trapq. Those belong to the optional
# real-Klipper tier; what is under test here is HH's choreography.
#
# COORDINATES. One scalar per gate: the position of the filament's leading edge (the
# "tip") along that gate's path, in mm, measured so that 0 is the gate's homing
# sensor. Negative is back toward the spool, positive is forward toward the extruder.
#
#   spool ... park(-100) ... entry(-50) ... exit(0) shared_exit(+10) ... extruder(+700)
#                    |             |            |
#   tip ------------>                               (moves right when loading)
#
#   Parked at -100 the entry switch is CLEAR; pushing filament past -50 is an insert.
#
# SENSOR SEMANTICS. Filament occupies everything behind its tip, so a switch at
# position P reads triggered exactly when tip >= P. That single rule gives correct
# behaviour for both directions: loading trips sensors in ascending order, unloading
# clears them in descending order.
#
# AXIS vs PATH. A homing move is expressed on the gear axis, and HH resets that axis
# to `forcepos` at the start of every homing move (extras/mmu_stepper.py:424), so axis
# positions are RELATIVE and cannot be used as a path coordinate. The model therefore
# only ever consumes displacements (target - start), which is direction-correct and
# immune to the axis being re-zeroed.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

# Logical sensor name -> path position (mm). Chosen to be consistent with the shipped
# BoxTurtle defaults: gate_homing_endstop is mmu_exit and gate_parking_distance is
# -100, so a parked/preloaded filament sits at -100 with the entry switch covered.
# The entry switch MUST sit between the park position and the gate sensor, i.e. a
# parked filament leaves it CLEAR. That is not an arbitrary choice - Happy Hare's own
# preload failure tail marks a gate GATE_UNKNOWN when the entry sensor is still
# triggered after preloading, so a layout where parking leaves it covered makes every
# preload "fail". Getting this backwards was the first thing the harness caught about
# its own geometry.
DEFAULT_LAYOUT = {
    'mmu_pre_gate': -70.0,
    'mmu_entry': -50.0,         # cleared when parked at -100
    'mmu_gate': 0.0,            # alternative gate sensor name
    'mmu_exit': 0.0,            # BoxTurtle's gate_homing_endstop
    'mmu_nfc': -80.0,           # per-gate reader, reachable from park within the jog window
    'mmu_shared_exit': 10.0,
    'extruder_entry': 700.0,
    'toolhead': 740.0,
}

# Where a filament tip sits in each notional state
TIP_ABSENT = -10000.0           # no filament anywhere near this gate
TIP_PARKED = -100.0             # gate_parking_distance for BoxTurtle
TIP_PRESENTED = -60.0           # offered to the gate, not yet past the entry switch (-50)

# A tag travels with the filament this far behind the tip.
DEFAULT_TAG_OFFSET = 0.0
# Half-width of the NFC read zone: a tag is detectable within +/- this of the reader.
DEFAULT_TAG_WINDOW = 15.0


class Tag:
    """An RFID tag riding on a gate's filament."""

    def __init__(self, uid, metadata=None, offset=DEFAULT_TAG_OFFSET):
        self.uid = uid
        self.metadata = dict(metadata or {})
        self.offset = offset

    def __repr__(self):
        return 'Tag(%r)' % (self.uid,)


class FilamentPath:
    """
    Per-gate filament positions plus the sensor geometry, with a hook to push sensor
    state changes back into Happy Hare through its real callbacks.
    """

    def __init__(self, num_gates, layout=None, set_sensor=None,
                 tag_window=DEFAULT_TAG_WINDOW):
        self.num_gates = num_gates
        self.layout = dict(DEFAULT_LAYOUT)
        if layout:
            self.layout.update(layout)
        self.tip = [TIP_ABSENT] * num_gates
        self.tags = {}                  # gate -> Tag
        self.tag_window = tag_window
        self._set_sensor = set_sensor
        self.history = []               # [(gate, delta, reason)] for debugging

    # -- setup -------------------------------------------------------------
    def place(self, gate, position, sync=True):
        """Put a gate's filament tip at an absolute path position."""
        self.tip[gate] = float(position)
        if sync:
            self.sync(gate)
        return self

    def present(self, gate, sync=True):
        """Offer filament to a gate (not yet past the entry switch)."""
        return self.place(gate, TIP_PRESENTED, sync=sync)

    def park(self, gate, sync=True):
        """Filament parked at the gate, as after a successful preload."""
        return self.place(gate, TIP_PARKED, sync=sync)

    def remove(self, gate, sync=True):
        return self.place(gate, TIP_ABSENT, sync=sync)

    def attach_tag(self, gate, uid, metadata=None, offset=DEFAULT_TAG_OFFSET):
        self.tags[gate] = Tag(uid, metadata, offset)
        return self

    # -- queries -----------------------------------------------------------
    def position(self, name):
        """Path position of a logical sensor name, accepting qualified names."""
        bare = name.split(':')[-1]
        # Strip a trailing per-gate index: mmu_exit_2 -> mmu_exit
        if bare in self.layout:
            return self.layout[bare]
        head = bare.rsplit('_', 1)[0]
        if head in self.layout:
            return self.layout[head]
        return None

    def gate_of(self, name):
        """Gate index encoded in a per-gate sensor name, or None."""
        tail = name.split(':')[-1].rsplit('_', 1)
        if len(tail) == 2 and tail[1].isdigit():
            return int(tail[1])
        return None

    def triggered(self, name, gate=None):
        """Would this sensor read triggered right now?"""
        position = self.position(name)
        if position is None:
            return None
        target_gate = self.gate_of(name)
        if target_gate is None:
            target_gate = gate
        if target_gate is None:
            # A shared sensor sees whichever gate's filament is furthest forward
            return any(t >= position for t in self.tip)
        return self.tip[target_gate] >= position

    def tag_detected(self, gate):
        reader_pos = self.layout.get('mmu_nfc')
        tag = self.tags.get(gate)
        if tag is None or reader_pos is None:
            return None
        tag_pos = self.tip[gate] - tag.offset
        if abs(tag_pos - reader_pos) <= self.tag_window:
            return tag
        return None

    # -- motion ------------------------------------------------------------
    def trip_distance(self, gate, delta, names, sought=True):
        """
        How far this gate's filament can travel through `delta` before one of `names`
        reaches its SOUGHT state (`sought` mirrors Klipper's `triggered` argument to
        home_start: True = home until the switch closes, False = until it opens).

        Returns (name, distance) with distance >= 0, or None if nothing would trip
        within the move.

        A sensor ALREADY in the sought state trips at distance 0. That is not a
        shortcut - it is what real hardware does, and what the fake MCU_endstop models
        by completing home_start immediately. Skipping such sensors instead made a
        re-home against an already-triggered switch run its full length, which in the
        model looked like shoving 200mm of filament into the bowden.
        """
        if not delta:
            return None
        direction = 1.0 if delta > 0 else -1.0
        best = None
        for name in names:
            position = self.position(name)
            if position is None:
                continue
            owner = self.gate_of(name)
            if owner is not None and owner != gate:
                continue    # another gate's sensor cannot see this filament
            start = self.tip[gate]
            currently = start >= position
            if currently == bool(sought):
                travel = 0.0                    # already there: completes at once
            elif sought:
                if direction < 0:
                    continue                    # retracting cannot close a switch ahead
                travel = position - start
            else:
                if direction > 0:
                    continue                    # advancing cannot open a switch behind
                # The switch opens as the tip passes back below it; a hair beyond so
                # the `tip >= position` comparison genuinely flips.
                travel = start - position + 1e-6
            if travel <= abs(delta) and (best is None or travel < best[1]):
                best = (name, travel)
        return best

    def nfc_trip_distance(self, gate, delta):
        """Distance until this gate's tag enters the reader window, or None."""
        reader_pos = self.layout.get('mmu_nfc')
        tag = self.tags.get(gate)
        if not delta or tag is None or reader_pos is None:
            return None
        if self.tag_detected(gate) is not None:
            return 0.0                          # already under the reader
        direction = 1.0 if delta > 0 else -1.0
        tag_pos = self.tip[gate] - tag.offset
        # The near edge of the window, approached from whichever side we are on
        edge = reader_pos - self.tag_window if direction > 0 else reader_pos + self.tag_window
        travel = (edge - tag_pos) * direction
        if 0.0 <= travel <= abs(delta):
            return travel
        return None

    def advance(self, gate, delta, reason=''):
        """Move a gate's filament and push any resulting sensor changes into HH."""
        if not delta:
            return self.tip[gate]
        self.tip[gate] += delta
        self.history.append((gate, delta, reason))
        logging.debug('filament: gate %d %+.2f -> %.2f (%s)',
                      gate, delta, self.tip[gate], reason)
        self.sync(gate)
        return self.tip[gate]

    # -- pushing state into Happy Hare -------------------------------------
    def sync(self, gate=None):
        """
        Drive every sensor whose modelled state differs from what HH believes, through
        HH's real button callbacks (never by poking filament_present).
        """
        if self._set_sensor is None:
            return
        gates = range(self.num_gates) if gate is None else [gate]
        for name in self.sensor_names():
            owner = self.gate_of(name)
            if owner is not None and owner not in gates:
                continue
            state = self.triggered(name)
            if state is not None:
                self._set_sensor(name, bool(state))

    def sensor_names(self):
        """Overridden by the Session to the sensors HH actually registered."""
        return list(self._registered or ())

    _registered = ()

    def bind(self, sensor_names, set_sensor):
        self._registered = list(sensor_names)
        self._set_sensor = set_sensor
        return self

    def describe(self, gate):
        parts = ['gate %d tip=%.1f' % (gate, self.tip[gate])]
        for name in sorted(self._registered or (), key=lambda n: self.position(n) or 0):
            if self.gate_of(name) in (None, gate):
                parts.append('%s=%d' % (name.split(':')[-1],
                                        1 if self.triggered(name) else 0))
        tag = self.tag_detected(gate)
        if tag is not None:
            parts.append('tag=%s' % tag.uid)
        return ' '.join(parts)
