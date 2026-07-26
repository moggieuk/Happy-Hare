# Fake Klipper `klippy/extras/homing.py` for the Happy Hare test harness.
#
# This module must EXIST even for configs that never home, because
# extras/mmu/unit/selectors/__init__.py pkgutil-imports every selector module and
# mmu_linear_servo_selector.py:35 does `from ....homing import Homing, HomingMove`
# at module scope.
#
# HomingMove IS IMPLEMENTED, backed by the 1-D filament model
# (test/hh/filament.py). The contract it has to satisfy is set by
# MmuGenericRail.home (extras/mmu_stepper.py:414-459):
#
#     mstepper.set_position([forcepos, 0, 0, 0])
#     init_mcu_pos = mstepper.get_steppers()[0].get_mcu_position()
#     hmove = HomingMove(printer, endstops, mstepper)
#     hmove.homing_move([movepos, 0, 0, 0], speed)
#     ... optional retract + second homing_move, then check_no_movement() ...
#     trig_mcu_pos = <the stepper_positions entry matching this stepper>.trig_pos
#     travelled = (trig_mcu_pos - init_mcu_pos) * step_dist
#
# So we must: arm the endstops, decide where the first one trips, move the model and
# the stepper there, and record trig_pos. `mstepper` stands in for the toolhead and
# implements the ManualStepper-ish interface (get_position / set_position /
# get_steppers / get_last_move_time / drip_move / flush_step_generation).
#
# We do NOT call toolhead.drip_move: with no real motion system nothing would ever
# drive an endstop, so the trigger point is computed from the model instead. Motion
# fidelity (acceleration, step timing, drip pacing) is explicitly out of scope here.
#
# NO TRIGGER is signalled the same way real Klipper does - printer.command_error("No
# trigger on ... after full movement") - so HH's retry and error handling runs for
# real rather than being bypassed.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

import mcu as mcu_mod

HOMING_START_DELAY = 0.001
ENDSTOP_SAMPLE_TIME = .000015
ENDSTOP_SAMPLE_COUNT = 4


class StepperPosition:
    def __init__(self, stepper, endstop_name):
        self.stepper = stepper
        self.endstop_name = endstop_name
        self.stepper_name = stepper.get_name()
        self.start_pos = stepper.get_mcu_position()
        self.start_cmd_pos = stepper.mcu_to_commanded_position(self.start_pos)
        self.halt_pos = self.trig_pos = None

    def note_home_end(self, trigger_time=None):
        self.halt_pos = self.trig_pos = self.stepper.get_mcu_position()


def _expand(endstops):
    """
    Flatten any MmuCompoundEndstop into its children so the model can pick a winner
    among the real leaves. Returns [(endstop_obj, name), ...].

    MmuCompoundEndstop keeps `endstops` plus an `endstop_names` map
    (extras/mmu/mmu_sensor_utils.py:499-530); homing to a compound must be able to
    trip whichever child would physically fire first, which is the whole point of the
    first-wins design the NFC preload relies on.
    """
    flat = []
    for endstop, name in endstops:
        children = getattr(endstop, 'endstops', None)
        names = getattr(endstop, 'endstop_names', None)
        if children and names is not None:
            for child in children:
                flat.append((child, names.get(child, name)))
        else:
            flat.append((endstop, name))
    return flat


def _fire(endstop, print_time, eventtime):
    """Trigger an endstop through whichever entry point its class provides."""
    if isinstance(endstop, mcu_mod.MCU_endstop):
        endstop.trigger(print_time)
        return True
    handler = getattr(endstop, 'trigger_handler', None)
    if handler is not None:
        # Virtual sensors (MmuVirtualEndstopSensor and subclasses, incl. MmuNfcEndstop)
        # are driven by trigger_handler(eventtime, state)
        handler(eventtime, True)
        return True
    return False


class HomingMove:
    def __init__(self, printer, endstops, toolhead=None):
        self.printer = printer
        self.endstops = list(endstops)
        self.toolhead = toolhead
        self.stepper_positions = []

    def get_mcu_endstops(self):
        return [es for es, _name in self.endstops]

    def calc_toolhead_pos(self, kin_spos, offsets):
        raise NotImplementedError('calc_toolhead_pos is not needed by Happy Hare')

    def homing_move(self, movepos, speed, probe_pos=False,
                    triggered=True, check_triggered=True):
        toolhead = self.toolhead
        printer = self.printer
        reactor = printer.get_reactor()
        model = getattr(printer, 'harness_filament', None)

        start_pos = toolhead.get_position()
        start_axis = start_pos[0]
        target_axis = movepos[0]
        delta = target_axis - start_axis
        print_time = toolhead.get_last_move_time()
        eventtime = reactor.monotonic()

        # Arm every endstop, exactly as Klipper does
        leaves = _expand(self.endstops)
        for endstop, _name in self.endstops:
            endstop.home_start(print_time, ENDSTOP_SAMPLE_TIME,
                               ENDSTOP_SAMPLE_COUNT, 1. / speed if speed else 0.001,
                               triggered=triggered)

        self.stepper_positions = [
            StepperPosition(stepper, name)
            for endstop, name in self.endstops
            for stepper in endstop.get_steppers()
        ]

        winner, travel = self._resolve(model, leaves, delta, triggered)

        if winner is None:
            # Full movement with no trigger. Move the model the whole way, leave the
            # axis at the target, then fail the way real Klipper does so HH's own
            # retry/error paths run.
            if model is not None:
                model.advance(self._gate(), delta,
                              'homing MISS [%s]' % ','.join(n for _e, n in leaves))
            toolhead.set_position([target_axis, 0., 0., 0.])
            toolhead.flush_step_generation()
            names = ', '.join(name for _es, name in leaves)
            raise printer.command_error(
                'No trigger on %s after full movement' % (names or 'endstop',))

        endstop, name = winner
        signed = travel if delta > 0 else -travel
        if model is not None:
            model.advance(self._gate(), signed, 'homing -> %s' % name)
        halt_axis = start_axis + signed
        toolhead.set_position([halt_axis, 0., 0., 0.])
        toolhead.flush_step_generation()
        _fire(endstop, print_time, eventtime)
        logging.debug('homing: %s tripped after %.3fmm (axis %.3f -> %.3f)',
                      name, travel, start_axis, halt_axis)

        for sp in self.stepper_positions:
            sp.note_home_end(print_time)
        return movepos

    def _resolve(self, model, leaves, delta, sought=True):
        """Pick the endstop that trips first, and how far the move gets."""
        if model is None or not delta:
            return None, None
        gate = self._gate()
        if gate is None:
            return None, None

        switch_names = []
        nfc_leaves = []
        for endstop, name in leaves:
            if 'mmu_nfc' in name or type(endstop).__name__ == 'MmuNfcEndstop':
                nfc_leaves.append((endstop, name))
            else:
                switch_names.append(name)

        candidates = []
        trip = model.trip_distance(gate, delta, switch_names, sought=sought)
        if trip is not None:
            name, distance = trip
            endstop = self._lookup(leaves, name)
            if endstop is not None:
                candidates.append((distance, endstop, name))

        # An NFC "endstop" trips on tag presence rather than a switch position
        for endstop, name in nfc_leaves:
            distance = model.nfc_trip_distance(gate, delta)
            if distance is not None:
                candidates.append((distance, endstop, name))

        if not candidates:
            return None, None
        candidates.sort(key=lambda c: c[0])
        distance, endstop, name = candidates[0]
        return (endstop, name), distance

    def _lookup(self, leaves, name):
        for endstop, endstop_name in leaves:
            if endstop_name == name:
                return endstop
        return None

    def _gate(self):
        mmu = self.printer.lookup_object('mmu', None)
        if mmu is None:
            return None
        gate = mmu.gate_selected
        return gate if gate is not None and gate >= 0 else None

    def check_no_movement(self):
        """Klipper returns the stepper name when nothing moved, else None."""
        for sp in self.stepper_positions:
            if sp.start_pos == sp.trig_pos:
                return sp.stepper_name
        return None


class Homing:
    def __init__(self, printer):
        self.printer = printer
        self.toolhead = printer.lookup_object('toolhead', None)
        self.changed_axes = []

    def set_axes(self, axes):
        self.changed_axes = axes

    def get_axes(self):
        return self.changed_axes

    def home_rails(self, rails, forcepos, movepos):
        raise NotImplementedError(
            'Homing.home_rails is only used by physical selectors, which the harness '
            'does not drive yet - use a VirtualSelector profile.')


class PrinterHoming:
    def __init__(self, config):
        self.printer = config.get_printer()

    def manual_home(self, toolhead, endstops, pos, speed,
                    probe_pos, triggered, check_triggered):
        """
        NOTE the `probe_pos` parameter. This is the NEWER Klipper signature - mainline
        v0.13.0-111 has manual_home(self, toolhead, endstops, pos, speed, triggered,
        check_triggered) with no probe_pos, while HH passes 7 arguments
        (extras/mmu_stepper.py:884). So this is a second independent confirmation that
        Happy Hare targets a bleeding-edge Klipper, alongside its hard requirement on
        extras/motion_queuing.py (absent from mainline entirely).
        """
        hmove = HomingMove(self.printer, endstops, toolhead)
        try:
            return hmove.homing_move(pos, speed, probe_pos=probe_pos,
                                     triggered=triggered,
                                     check_triggered=check_triggered)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    'Homing failed due to printer shutdown')
            raise

    def probing_move(self, mcu_probe, pos, speed):
        raise NotImplementedError('probing_move is not used by Happy Hare')


def load_config(config):
    # Required: HH does lookup_object('homing') (extras/mmu_stepper.py:883), and the
    # object only exists because ToolHead preloads this module by name. Without a
    # load_config here, load_object returns its default and the lookup fails with
    # "Unknown config object 'homing'".
    return PrinterHoming(config)
