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
#   spool ... entry(-150) [GEAR] park(-100) ... exit(0) shared_exit(+10) ... extruder(+700)
#                   |                  |            |
#   tip ----------->                                    (moves right when loading)
#
#   Parked at -100 the entry switch is COVERED - the filament runs back through it to the
#   spool, which is the only way the gear can still grip it. Pushing filament past -150 is
#   an insert, and only a user can do that: the gear is downstream of the switch.
#
# SENSOR SEMANTICS. Filament occupies the span [tail, tip], so a switch at position P
# reads triggered exactly when tail <= P <= tip. Loading trips sensors in ascending order,
# unloading clears them in descending order.
#
# The TAIL is normally -infinity: filament runs back to an attached spool, so anything
# behind the tip is filament. exhaust() gives a gate a finite tail, which is what a
# RUNOUT physically is - the end of the filament passes the gate and the sensors behind
# the tip go clear while the tip is still downstream. Without a tail every runout looks
# like a clog to Happy Hare, because the gate sensor never releases.
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
# BoxTurtle defaults: gate_homing_endstop is mmu_exit, gate_parking_distance is -100 and
# gate_final_eject_distance is 100 (installer/mmu_types/Kconfig.box_turtle:97-104).
#
# THE ENTRY SWITCH IS UPSTREAM OF THE GEAR, and those two shipped distances pin where it
# has to go to within 100mm:
#
#   entry < -100  A parked filament must still be gripped, so it necessarily runs back
#                 through the switch to the spool - parked means entry COVERED. Happy
#                 Hare relies on this: mmu_gate_maps.validate_gate_status forces a
#                 non-EMPTY gate to GATE_EMPTY when entry reads clear, so a layout where
#                 parking uncovers it demotes every gate the moment that runs.
#   entry > -200  Eject retracts 100mm PAST park precisely to release the filament from
#                 the gear, and homes against the entry switch going clear
#                 (_eject_from_gate, mmu_filament_movement.py:920-929). Put the switch
#                 outside that reach and the homing move can never trigger.
#
# -150 sits in the middle. The consequence that matters for the rest of the suite: no
# MMU-commanded move ever crosses this switch. Loading, preloading and the NFC jog all
# live between -100 and the extruder. Only a user insertion (or a spool running out, via
# exhaust()) can change its state - which is exactly the real machine.
#
# This was previously inverted - entry at -50, i.e. between park and the gate - on the
# reasoning that HH's preload tail marks a gate GATE_UNKNOWN when entry is still covered
# afterwards. That tail is inside `except MmuError` (mmu_filament_movement.py:180-189)
# and cannot fire on a successful preload, so it never justified the geometry. What the
# inverted layout did produce was a stream of phantom insert events from the MMU's own
# moves, and the nested preloads that followed them.
DEFAULT_LAYOUT = {
    'mmu_pre_gate': -150.0,     # v3 alias: the SAME switch as mmu_entry, so same position
    'mmu_entry': -150.0,        # covered when parked at -100; upstream of the gear
    'mmu_gate': 0.0,            # alternative gate sensor name
    'mmu_exit': 0.0,            # BoxTurtle's gate_homing_endstop
    'mmu_nfc': -80.0,           # per-gate reader, reachable from park within the jog window
    'mmu_shared_exit': 10.0,
    # The encoder wheel, just past the gate - where ERCF-style machines put it. It is
    # NOT a switch: nothing in sensor_names() ever resolves to it (Happy Hare registers
    # the encoder's derived sensor as 'encoder', which position() deliberately does not
    # match), so the model never drives it. It is here purely as the point whose
    # COVERAGE decides whether a move turns the encoder wheel - see travel_over().
    'mmu_encoder': 20.0,
    'extruder_entry': 700.0,
    # Happy Hare registers the extruder-entry switch as plain 'extruder' (it arrives as
    # 'default:extruder'), so WITHOUT this alias position() returns None for it, bind() leaves
    # it out, and the sensor is never driven by the model - it just reads empty forever. That
    # reads to HH as a contradiction the moment the toolhead sensor trips:
    #
    #   "Toolhead or extruder sensor failure. Extruder sensor reports no filament but
    #    toolhead sensor is still triggered."
    #
    # Same point on the path as extruder_entry, which is why this is an alias rather than a
    # second position - the same idiom as mmu_gate/mmu_exit above. Invisible until a profile
    # set MMU_HAS_SENSOR_EXTRUDER; none did before ercf_vvd.
    'extruder': 700.0,
    'toolhead': 740.0,
    # Fallback for machines without the dynamically modelled, tension-sprung switch
    # buffer. configure_buffers() moves this landmark forward by 70% of buffer_maxrange
    # for the common two-switch design.
    'filament_compression': 700.0,
}

# Where a filament tip sits in each notional state
TIP_ABSENT = -10000.0           # no filament anywhere near this gate
TIP_PARKED = -100.0             # gate_parking_distance for BoxTurtle
TIP_PRESENTED = -180.0          # offered to the MMU, not yet past the entry switch (-150)

# A tag travels with the filament this far behind the tip.
DEFAULT_TAG_OFFSET = 0.0
# Half-width of the NFC read zone: a tag is detectable within +/- this of the reader.
DEFAULT_TAG_WINDOW = 15.0

# A switch-style buffer normally reaches its compression switch before the physical
# end stop. There is no more precise geometry in mmu_hardware.cfg, so keep the model's
# deliberately approximate switch point in one named place.
BUFFER_COMPRESSION_FRACTION = 0.7


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
        # -inf: filament runs back to an attached spool. exhaust() makes it finite.
        self.tail = [float('-inf')] * num_gates
        # {unit name: (first_gate, num_gates)}, filled in by the Session. Empty means
        # single-unit, in which case gates_visible_to() falls back to every gate as before.
        self.units = {}
        self.tags = {}                  # gate -> Tag
        self.tag_window = tag_window
        self._set_sensor = set_sensor
        self.history = []               # [(gate, delta, reason)] for debugging
        # Called as obs(gate, delta, start_tip, start_tail) after every move. Used by
        # the Session to turn filament travel into encoder pulses; a switch cannot
        # express that, because an encoder reports MOTION rather than presence.
        self.observers = []
        # Unit-qualified switch-buffer geometry, populated from the real MmuBuffer
        # objects by Session.filament(). Only tension-sprung, two-switch buffers are
        # modelled dynamically; other buffer types retain their configured resting state.
        self.buffers = {}
        self._selected_gate = None
        self._drive_mode = None

    def configure_buffers(self, units, selected_gate=None, drive_mode=None):
        """Import the physical buffer travel configured for each MMU unit."""
        self._selected_gate = selected_gate
        self._drive_mode = drive_mode
        for unit in units:
            buffer = getattr(unit, 'buffer', None)
            if (buffer is None
                    or getattr(buffer, 'buffer_spring_state', 'none') != 'tension'
                    or getattr(buffer, 'compression_sensor', None) is None
                    or getattr(buffer, 'tension_sensor', None) is None):
                continue
            maxrange = float(getattr(buffer, 'buffer_maxrange', 0.))
            if maxrange <= 0.:
                continue
            prefix = getattr(buffer, 'name', unit.name)
            if prefix in self.buffers:
                continue
            gates = tuple(
                gate
                for connected in getattr(buffer, 'connected_units', (unit,))
                for gate in range(connected.first_gate,
                                  connected.first_gate + connected.num_gates)
            )
            entry = self.layout['extruder_entry']
            self.buffers[prefix] = {
                'gates': gates,
                'entry': entry,
                'compression': entry + maxrange * BUFFER_COMPRESSION_FRACTION,
                'compression_travel': maxrange * BUFFER_COMPRESSION_FRACTION,
                'maxrange': maxrange,
                'travel': {gate: 0. for gate in gates},
                'contact': {gate: False for gate in gates},
            }

        # The console's path legend reads the unqualified layout. A shared path can
        # only draw one landmark, so use the first configured buffer as its representative.
        if self.buffers:
            first = next(iter(self.buffers.values()))
            self.layout['filament_compression'] = first['compression']
        return self

    # -- setup -------------------------------------------------------------
    def place(self, gate, position, sync=True):
        """Put a gate's filament tip at an absolute path position."""
        self.tip[gate] = float(position)
        for geometry in self.buffers.values():
            if gate in geometry['travel']:
                # Absolute placement says where the filament is along the path, not
                # whether it is pushing against the extruder. Only a compression-home
                # establishes that contact and stores subsequent relative motion.
                geometry['travel'][gate] = 0.
                geometry['contact'][gate] = False
        if sync:
            self.sync(gate)
        return self

    def exhaust(self, gate, at=None, sync=True):
        """
        The spool has run out: give this gate's filament a finite tail so the sensors
        behind the tip go clear.

        Defaults to just past the gate sensor, which is the moment Happy Hare can tell a
        runout from a clog - the gate sensor releases while filament is still gripped
        downstream. Without this, _runout() sees filament still present and reports
        "a clog/tangle has been detected and requires manual intervention".
        """
        if at is None:
            at = self.layout.get('mmu_exit', 0.0) + 1.0
        self.tail[gate] = float(at)
        if sync:
            self.sync(gate)
        return self

    def refill(self, gate, sync=True):
        """Undo exhaust(): filament runs back to a spool again."""
        self.tail[gate] = float('-inf')
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
        geometry = self._buffer_geometry(name)
        if geometry is not None:
            bare = name.split(':')[-1]
            if bare == 'filament_tension':
                return geometry['entry']
            if bare == 'filament_compression':
                return geometry['compression']
        bare = name.split(':')[-1]
        # Strip a trailing per-gate index: mmu_exit_2 -> mmu_exit
        if bare in self.layout:
            return self.layout[bare]
        head = bare.rsplit('_', 1)[0]
        if head in self.layout:
            return self.layout[head]
        return None

    def models_sensor(self, name):
        """Whether a registered sensor is owned by the dynamic buffer model."""
        return (name.split(':')[-1] in ('filament_tension', 'filament_compression')
                and self._buffer_geometry(name) is not None)

    def _buffer_geometry(self, name):
        bare = name.split(':')[-1]
        if bare not in ('filament_tension', 'filament_compression'):
            return None
        if ':' in name:
            return self.buffers.get(name.split(':')[0])
        if len(self.buffers) == 1:
            return next(iter(self.buffers.values()))
        return None

    def _buffer_gate(self, geometry, gate=None):
        gates = geometry['gates']
        if gate is not None and gate in gates:
            return gate
        if self._selected_gate is not None:
            selected = self._selected_gate()
            if selected in gates:
                return selected
        # At startup no gate need be selected. Prefer a non-empty lane if one exists;
        # otherwise the resting spring state is the same whichever lane represents it.
        return max(gates, key=lambda g: self.tip[g])

    def _buffer_triggered(self, name, gate=None):
        geometry = self._buffer_geometry(name)
        if geometry is None:
            return None
        gate = self._buffer_gate(geometry, gate)
        tip = self.tip[gate]
        travel = geometry['travel'][gate]
        if name.split(':')[-1] == 'filament_tension':
            return tip < geometry['entry'] and travel <= 0.
        return travel + 1e-9 >= geometry['compression_travel']

    def gate_of(self, name):
        """Gate index encoded in a per-gate sensor name, or None."""
        tail = name.split(':')[-1].rsplit('_', 1)
        if len(tail) == 2 and tail[1].isdigit():
            return int(tail[1])
        return None

    def gates_visible_to(self, name):
        """
        Which gates a sensor can possibly see.

        Three cases, narrowest first:

        1. A per-gate name (mmu_exit_7) sees that gate alone.
        2. A UNIT-QUALIFIED name (unit0:mmu_shared_exit) sees only that unit's gates. Without
           this a unit-scoped sensor fell through to "every gate on the machine", so on a
           multi-unit printer unit0's shared-exit switch read TRIGGERED whenever unit1 had
           filament loaded - while every one of unit0's own gates was empty. Harmless on a
           one-unit machine, which is why it went unnoticed; `units` is populated by the
           Session and empty for a single-unit session, so behaviour there is unchanged.
        3. Anything else (default:toolhead, and the extruder/compression sensors) is genuinely
           printer-wide and sees every gate.
        """
        gate = self.gate_of(name)
        if gate is not None:
            return (gate,)
        unit = name.split(':')[0] if ':' in name else None
        span = self.units.get(unit)
        if span is not None:
            first, count = span
            return range(first, first + count)
        return range(self.num_gates)

    def triggered(self, name, gate=None):
        """Would this sensor read triggered right now?"""
        if self.models_sensor(name):
            return self._buffer_triggered(name, gate)
        position = self.position(name)
        if position is None:
            return None
        target_gate = self.gate_of(name)
        if target_gate is None:
            target_gate = gate
        if target_gate is None:
            # A shared sensor sees any gate IT CAN SEE whose filament spans it
            return any(self.tail[g] <= position <= self.tip[g]
                       for g in self.gates_visible_to(name))
        return self.tail[target_gate] <= position <= self.tip[target_gate]

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
            if self.models_sensor(name):
                travel = self._buffer_trip_distance(gate, delta, name, sought)
                if travel is not None and (best is None or travel < best[1]):
                    best = (name, travel)
                continue
            position = self.position(name)
            if position is None:
                continue
            owner = self.gate_of(name)
            if owner is not None and owner != gate:
                continue    # another gate's sensor cannot see this filament
            start = self.tip[gate]
            currently = self.tail[gate] <= position <= start
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

    def _buffer_trip_distance(self, gate, delta, name, sought):
        """Distance to a switch transition in a tension-sprung buffer."""
        current = self._buffer_triggered(name, gate)
        if current == bool(sought):
            return 0.
        geometry = self._buffer_geometry(name)
        start = self.tip[gate]
        forward = delta > 0.
        bare = name.split(':')[-1]
        epsilon = 1e-6
        if bare == 'filament_tension':
            if forward and not sought:
                travel = geometry['entry'] - start
            elif not forward and sought:
                travel = start - geometry['entry'] + epsilon
            else:
                return None
        else:
            buffer_travel = geometry['travel'][gate]
            mode = self._drive_mode(gate) if self._drive_mode is not None else 'gear'
            if mode == 'gear':
                if forward and sought:
                    travel = max(0., geometry['entry'] - start) + (
                        geometry['compression_travel'] - buffer_travel)
                elif not forward and not sought:
                    travel = buffer_travel - geometry['compression_travel'] + epsilon
                else:
                    return None
            elif mode == 'extruder':
                if forward and not sought:
                    travel = buffer_travel - geometry['compression_travel'] + epsilon
                elif not forward and sought:
                    travel = geometry['compression_travel'] - buffer_travel
                else:
                    return None
            else:
                return None
        return travel if 0. <= travel <= abs(delta) else None

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

    def travel_over(self, position, start_tip, start_tail, delta):
        """
        How much of a `delta` move happens while filament COVERS `position`.

        This is what an encoder measures: the wheel only turns while filament is
        under it, so a move that starts short of the encoder contributes only the
        part after the filament arrives. Modelling it as "moved at all" instead
        would make Happy Hare's `measured > 6.0mm` motion test pass for a gate with
        no filament in it, which is exactly the check under test.

        Direction-blind, like the real hardware: a pulse counter has no quadrature,
        so a retraction produces positive counts too and get_distance() only ever
        grows. Returns a non-negative distance.
        """
        # Filament covers `position` at travel u (signed, along the move) whenever
        # start_tail + u <= position <= start_tip + u, i.e. u in [lo, hi].
        lo = position - start_tip
        hi = position - start_tail          # +inf while a spool is attached
        overlap = min(hi, max(0.0, delta)) - max(lo, min(0.0, delta))
        return max(0.0, overlap)

    def advance(self, gate, delta, reason=''):
        """Move a gate's filament and push any resulting sensor changes into HH."""
        if not delta:
            return self.tip[gate]
        start_tip, start_tail = self.tip[gate], self.tail[gate]
        self._advance_buffer(gate, delta, start_tip, reason)
        self.tip[gate] += delta
        self.tail[gate] += delta        # filament moves as one piece
        self.history.append((gate, delta, reason))
        logging.debug('filament: gate %d %+.2f -> %.2f (%s)',
                      gate, delta, self.tip[gate], reason)
        self.sync(gate)
        for observe in self.observers:
            observe(gate, delta, start_tip, start_tail)
        return self.tip[gate]

    def _advance_buffer(self, gate, delta, start_tip, reason):
        """Store relative gear/extruder travel in a tension-sprung buffer."""
        for geometry in self.buffers.values():
            if gate not in geometry['travel']:
                continue
            mode = self._drive_mode(gate) if self._drive_mode is not None else 'gear'
            travel = geometry['travel'][gate]
            contact = geometry['contact'][gate]
            compression_home = ('homing' in reason
                                and 'filament_compression' in reason)
            if mode == 'gear':
                if delta > 0.:
                    # Passing the nominal entry coordinate on an ordinary Bowden move
                    # is not proof of contact: calibration and flex can overshoot it.
                    # A compression homing move is the explicit collision with the
                    # extruder; after that, relative gear feed expands the buffer.
                    if contact or compression_home:
                        travel += max(
                            0., start_tip + delta - max(start_tip, geometry['entry']))
                        contact = start_tip + delta >= geometry['entry']
                elif contact:
                    travel += delta
            elif mode == 'extruder' and contact:
                travel -= delta
            # Equal gear/extruder motion transports filament without changing the buffer.
            geometry['travel'][gate] = min(geometry['maxrange'], max(0., travel))
            if start_tip + delta < geometry['entry'] and geometry['travel'][gate] <= 0.:
                contact = False
            geometry['contact'][gate] = contact

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
