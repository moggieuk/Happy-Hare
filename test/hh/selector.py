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
# TWO GEOMETRIES, because the two shipped physical-selector families disagree:
#
#   LinearSelector family (ERCF, Tradrack)  ONE home switch at position_endstop; gates are
#                                           reached by plain moves to calibrated offsets.
#   IndexedSelector (ViViD)                 NO home switch at all; one index switch PER GATE,
#                                           laid out in selector_gate_order.
#
# KNOWN LIMIT: HH's own MMU_CALIBRATE_SELECTOR AUTO=1 cannot run here, so offsets are seeded
# instead (Session.calibrate_selectors). Auto-calibration measures travel as
# (trig_mcu_pos - init_mcu_pos) * step_dist (extras/mmu_stepper.py:414-459), which needs the
# mcu position to survive the set_position(forcepos) that precedes every homing move. Real
# Klipper gets that from step generation, with set_position preserving the mcu position
# (klippy/stepper.py:158-177). The fake has no step generation - set_position IS how it
# effects motion - so making it preserve the mcu position instead makes travel measure 0 and
# homing die with "Endstop still triggered after retract". Verified by experiment. Decoupling
# the two would mean reworking motion semantics for every stepper in the fake, which is a
# bigger change than this needs; seeding costs one call and duplicates no HH logic beyond the
# published quick-method formula.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


class SelectorAxis:
    """
    The endstop geometry of one unit's selector.

    Stateless with respect to position - it reads the rail and the selector each time - so it
    can be rebuilt at any point without going out of step with the machine.
    """

    def __init__(self, unit, selector, stepper):
        self.unit = unit
        self.selector = selector
        self.stepper = stepper

    # -- geometry ----------------------------------------------------------------

    @property
    def carriage(self):
        """
        Where the carriage is, in axis coordinates. Read from the stepper rather than tracked:
        homing leaves the axis at position_endstop and selecting a gate is a plain move to
        that gate's offset, so the stepper's own commanded position is already the answer.
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
        travel = target - start
        if travel and (travel > 0) != (delta > 0):
            return None                             # switch is behind us
        if abs(travel) > abs(delta):
            return None                             # move ends short of it
        return abs(travel)

    def describe(self):
        parts = ['%s selector carriage=%.2f home=%.2f'
                 % (self.unit.name, self.carriage, self.home_position())]
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
