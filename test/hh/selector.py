# Happy Hare test harness - where a unit's selector endstops sit.
#
# Deliberately NOT a motion model, and deliberately not part of filament.py. The filament
# model is "one scalar per gate: the position of the filament's leading edge"; a selector
# carriage is a different axis entirely, and conflating them was how the harness ended up
# routing selector endstops through gate-filament trip_distance() and failing every physical
# selector with "No trigger on mmu_sel_home after full movement".
#
# It is also much smaller than the filament model, because there is nothing to integrate:
# MmuStepper already tracks the carriage, and a plain selector move needs no help from us. The
# ONLY question a homing move asks is:
#
#     "at what axis position does endstop X trigger?"
#
# COORDINATES. The selector axis is in real config coordinates - unlike the filament path,
# where HH resets the gear axis to `forcepos` on every homing move so only displacements mean
# anything. Here position_endstop/position_min/position_max come straight from the rail config
# (extras/mmu_stepper.py:895-924), so the endstops are defined in absolute terms and a homing
# move that reaches the target coordinate is exactly a switch being made.
#
# TWO GEOMETRIES, because the shipped physical-selector families disagree about endstops:
#
#   LinearSelector family (ERCF, Tradrack)  ONE home switch at position_endstop; gates are
#                                           reached by plain moves to calibrated offsets.
#   IndexedSelector (ViViD)                 NO home switch at all; one index switch PER GATE,
#                                           laid out in selector_gate_order.
#
# RotarySelector (3D Chameleon) is a THIRD family but not a third geometry: it homes and selects
# exactly like the linear one, and nothing it does differently (grip expressed as a carriage
# position, gear direction per gate) is about where a switch sits. So it needs nothing here -
# cad_gate0_pos and cad_gate_width, which the offsets below are built from, are parameters it
# already has. Its own behaviour is covered in test_mmu_selector.TestRotarySelector.
#
# THE CARRIAGE IS TRACKED, not read off the stepper. This file used to be stateless with
# respect to position - `carriage` was just `stepper.commanded_pos` - and that is what made
# MMU_CALIBRATE_SELECTOR unusable: MmuGenericRail.home() teleports the axis to `forcepos`
# immediately before every homing move (extras/mmu_stepper.py:424), so a trip resolved against
# the stepper coordinate always measured |position_endstop - forcepos|, i.e. the homing SEARCH
# distance. Every gate reported the same number.
#
# So the harness carries the physical truth, exactly as filament.py does for the filament, and
# the fake motion layer keeps the two meanings of "position" apart:
#
#   set_position()        redefine the coordinate frame; no motion   (klippy_root/stepper.py)
#   harness_note_motion() real travel; moves the mcu step count      (klippy_root/stepper.py)
#
# Motion reaches us from exactly two places - the fake HomingMove for homing moves, and
# Session._on_manual_move for plain ones. Both must call advance(); a plain move that is not
# observed leaves the carriage on the home switch through the retract inside rail.home(), and
# the second homing move then measures zero.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


class SelectorAxis:
    """
    The endstop geometry of one unit's selector, and where its carriage physically is.

    The geometry half is stateless - it reads the rail and the selector each time. The
    carriage is tracked (see the note at the top of this file), so an axis must be built once
    and kept, not rebuilt mid-session.
    """

    def __init__(self, unit, selector, stepper):
        self.unit = unit
        self.selector = selector
        self.stepper = stepper

        # The physical limits of travel. `travel_min` is the home switch, which on every
        # shipped LinearSelector doubles as a hard stop; `travel_max` is the far end, which
        # _calibrate_selector_auto deliberately drives into to discover the selector length
        # (mmu_linear_selector.py:_calibrate_selector_auto step 2), so a plain move HAS to
        # stop there or step 3 measures a number the machine does not have.
        self.travel_min = self.home_position()
        offsets = self.nominal_gate_offsets()
        positions = self.gate_positions()
        # An IndexedSelector is a rotary ring, not a rail. Its last published index is not
        # a hard stop: moving forward from it wraps through zero to the first indexes again.
        # Keep the circumference separate from travel_max, which remains useful diagnostic
        # output and is still a real hard limit for every linear-selector family.
        self.circumference = None
        if callable(getattr(self.selector, '_get_gate_endstop_name', None)):
            spacing = self._cad('cad_gate_width') or 1.0
            self.circumference = self.unit.num_gates * spacing
        if offsets:
            self.travel_max = offsets[-1] + (self._cad('cad_last_gate_offset') or 0.)
        elif positions:
            self.travel_max = max(positions.values())
        elif self.circumference is not None:
            self.travel_max = self.circumference - spacing
        else:
            self.travel_max = None

        # WHERE THE CARRIAGE POWERS ON. Nominal gate 0, NOT the home switch: a carriage
        # sitting on its own switch makes the first homing move travel zero, which trips
        # HomingMove.check_no_movement() and fails MMU_HOME with "Endstop still triggered
        # after retract". Gate 0 is also where MMU_CALIBRATE_SELECTOR AUTO=1 asks the user to
        # put it by hand, so a cold session is ready for the real calibration flow.
        self.carriage = offsets[0] if offsets else (self.travel_min or 0.)

    # -- motion ------------------------------------------------------------------

    def place(self, position):
        """Put the carriage somewhere, as a user sliding it by hand would. Clamped."""
        self.carriage = self._clamp(position)
        return self.carriage

    def advance(self, delta):
        """Move the carriage by `delta`, and return the distance it ACTUALLY travelled.

        The return value is what the caller must feed to harness_note_motion: a move that
        runs into either end of travel moves the mcu position by less than it asked for.
        """
        if not delta:
            return 0.
        if self.circumference is not None:
            self.carriage = self._clamp(self.carriage + delta)
            return delta
        target = self._clamp(self.carriage + delta)
        moved = target - self.carriage
        self.carriage = target
        return moved

    def _clamp(self, position):
        if self.circumference is not None:
            return position % self.circumference
        if self.travel_min is not None:
            position = max(self.travel_min, position)
        if self.travel_max is not None:
            position = min(self.travel_max, position)
        return position

    # -- geometry ----------------------------------------------------------------

    @property
    def commanded(self):
        """
        What Happy Hare THINKS the position is. Equal to `carriage` except while a homing
        move is in flight, when rail.home() has rebased the frame to `forcepos`.
        """
        return self.stepper.commanded_pos

    def home_position(self):
        """Where the default (home) endstop sits: the rail's position_endstop."""
        return self.stepper.rail.get_homing_info().position_endstop

    def _cad(self, name, default=None):
        value = getattr(self.selector.p, name, None)
        return default if value is None else value

    def _home_endstop_names(self):
        """
        What the rail calls its default endstop. Klipper names a rail's own endstop after the
        owning section, so this arrives as 'mmu_stepper unit0_selector' rather than the
        friendly 'mmu_sel_home' - match both, plus the bare stepper name.
        """
        name = getattr(self.stepper, 'name', None)
        if not name:
            return ()
        return ('mmu_stepper %s' % name, name,
                getattr(self.unit.p, 'selector_endstop_name', None) or 'mmu_sel_home')

    def gate_positions(self):
        """
        {endstop name: axis position} for a per-gate INDEX switch design.

        Empty for the LinearSelector family, which has no such switches. For IndexedSelector
        the switches are evenly spaced and visited in `selector_gate_order`, so physical slot
        i carries gate order[i] - a permutation, which is exactly where an off-by-one would
        hide, hence a direct test for it.
        """
        get_name = getattr(self.selector, '_get_gate_endstop_name', None)
        order = getattr(self.selector, 'gate_sequence', None) or getattr(
            self.unit.p, 'selector_gate_order', None)
        if get_name is None or not order:
            return {}

        spacing = self._cad('cad_gate_width') or 1.0
        out = {}
        for slot, lgate in enumerate(order):
            try:
                out[get_name(int(lgate))] = slot * spacing
            except Exception:                       # pragma: no cover - defensive
                logging.debug('selector: no endstop name for gate %r', lgate)
        return out

    def nominal_gate_offsets(self):
        """
        Gate offsets by HH's OWN published quick method - the formula it uses itself when a
        selector has no endstop to measure against (mmu_rotary_selector.py:556-557, and the
        theoretical branch at mmu_linear_selector.py:478):

            cad_gate0_pos + i * cad_gate_width

        Taken from HH rather than invented so a change to a vendor's CAD table flows through
        without anyone having to remember this file exists. On ERCF v1.1 the real spacing is
        not uniform (bearing blocks every third gate, mmu_linear_selector.py:485-500); the
        harness is exercising HH's gate SELECTION, not validating ERCF's geometry, so the
        uniform approximation is the honest simplification and HH's own fallback besides.
        """
        gate0 = self._cad('cad_gate0_pos')
        width = self._cad('cad_gate_width')
        if gate0 is None or width is None:
            return []
        return [round(gate0 + i * width, 1) for i in range(self.unit.num_gates)]

    def position(self, name):
        """Axis position at which `name` triggers, or None if this axis does not own it."""
        if name in self._home_endstop_names():
            return self.home_position()
        return self.gate_positions().get(name)

    def owns(self, name):
        return self.position(name) is not None

    # -- what a homing move needs ------------------------------------------------

    def trip_distance(self, name, start, delta):
        """
        How far a move of `delta` from axis position `start` travels before `name` triggers,
        or None if it never does. Sign-aware: a switch behind the direction of travel is
        unreachable, and one past the end of the move is not reached.

        Returns 0.0 when the switch is already made, which is a real outcome - HH queries an
        index endstop before moving (mmu_indexed_selector.py:_select_gate) precisely so it can
        skip a move it does not need.
        """
        target = self.position(name)
        if target is None or not delta:
            return None
        if self.circumference is not None:
            start %= self.circumference
            if delta > 0:
                travel = (target - start) % self.circumference
            else:
                travel = -((start - target) % self.circumference)
        else:
            travel = target - start
        if travel and (travel > 0) != (delta > 0):
            return None                             # switch is behind us
        if abs(travel) > abs(delta):
            return None                             # move ends short of it
        return abs(travel)

    def describe(self):
        parts = ['%s selector carriage=%.2f cmd=%.2f home=%.2f max=%s'
                 % (self.unit.name, self.carriage, self.commanded, self.home_position(),
                    '-' if self.travel_max is None else '%.2f' % self.travel_max)]
        for name, pos in sorted(self.gate_positions().items(), key=lambda kv: kv[1]):
            parts.append('%s=%.2f' % (name.split(':')[-1], pos))
        return ' '.join(parts)

    def __repr__(self):
        return 'SelectorAxis(%r)' % (self.unit.name,)


def axes_for(printer):
    """
    One SelectorAxis per unit that HAS a physical selector, published by the Session as
    printer.harness_selectors.

    Returned as a list rather than a dict because the consumer (the fake HomingMove) matches
    on the STEPPER, and by object identity: MmuGenericRail.home() passes the MmuStepper being
    homed as its 'toolhead' (extras/mmu_stepper.py:414-459), so identity is exact and survives
    renaming. Units with a VirtualSelector have no selector_stepper and are skipped, which is
    why a BoxTurtle session gets an empty list and behaves exactly as before.

    The fake klippy tree never imports from test.hh - it communicates only through
    printer.harness_* attributes (see harness_filament, harness_counters) - so this is called
    from the harness side and the result handed over.
    """
    mmu = printer.lookup_object('mmu', None)
    machine = getattr(mmu, 'mmu_machine', None) if mmu is not None else None
    out = []
    for unit in (getattr(machine, 'units', None) or ()):
        selector = getattr(unit, 'selector', None)
        stepper = getattr(selector, 'selector_stepper', None)
        if stepper is not None:
            out.append(SelectorAxis(unit, selector, stepper))
    return out
