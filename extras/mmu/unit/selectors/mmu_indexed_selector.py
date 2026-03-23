# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Implementation of Indexed Selector
# - Stepper based Selector for ViViD with per-gate index sensors
#
# Implements commands:
#    MMU_SOAKTEST_SELECTOR (PhysicalSelector)
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, traceback
from typing                 import Sequence

# Klipper imports
from ....homing             import HomingMove

# Happy Hare imports
from ...mmu_constants       import *
from ...mmu_utils           import MmuError
from ...mmu_base_parameters import TunableParametersBase, ParamSpec
from ..mmu_calibrator       import CALIBRATED_SELECTOR
from .mmu_base_selectors    import PhysicalSelector


# -----------------------------------------------------------------------------------------------------------
# Parameters for indexed selector
# -----------------------------------------------------------------------------------------------------------

class IndexedSelectorParameters(TunableParametersBase):

    _SPECS: Sequence[ParamSpec] = (
        ParamSpec('selector_move_speed',    'float',  200.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_homing_speed',  'float',  100.0, section="SELECTOR", limits=dict(minval=1.0)),
        ParamSpec('selector_accel',         'float', 1200.0, section="SELECTOR", limits=dict(above=1.0)),
        ParamSpec('selector_index_distance','float',    5.0, section="SELECTOR", limits=dict(minval=0.0)),

        ParamSpec('cad_gate_width',         'float',     90, section="CAD",      limits=dict(above=0.0),  hidden=True),
        ParamSpec('cad_max_rotations',      'int',        2, section="CAD",      limits=dict(minval=0),   hidden=True),
    )



# -----------------------------------------------------------------------------------------------------------
# IndexedSelector implementation
# -----------------------------------------------------------------------------------------------------------

class IndexedSelector(PhysicalSelector):
    """
    Stepper-based indexed selector for type-A MMUs with per-gate index sensors.

    Uses a selector stepper for gate selection and an indexing sensor/endstop
    per gate to locate the target gate (e.g. BTT ViViD).
    """
    PARAMS_CLS = IndexedSelectorParameters

    def __init__(self, config, mmu_unit, params):
        super().__init__(config, mmu_unit, params)
        self.is_homed = True


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        """
        Bind toolhead selector components and reset selector position.

        Resolves the selector rail and stepper from the MMU toolhead kinematics
        and resets position to 0.
        """
        super().handle_connect()

        self.selector_rail = self.mmu_unit.mmu_toolhead.get_kinematics().rails[0]
        self.selector_stepper = self.selector_rail.steppers[0]
        self._set_position(0) # Reset pos


    def _select_gate(self, gate):
        """
        Select a gate by moving until the corresponding index endstop triggers.

        Verifies the configured extra endstop for the gate exists, then only
        moves if the endstop is not already triggered.
        """
        super().select_gate(gate)

        if gate >= 0:
            endstop = self.selector_rail.get_extra_endstop(self._get_gate_endstop(gate))
            if not endstop:
                raise MmuError("Extra endstop %s not defined on the selector stepper" % self._get_gate_endstop(gate))
            mcu_endstop = endstop[0][0]
            if not mcu_endstop.query_endstop(self.mmu_unit.mmu_toolhead.get_last_move_time()):
                with self.mmu.wrap_action(ACTION_SELECTING):
                    self._find_gate(gate)

    def disable_motors(self):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_disable(self.mmu_unit.mmu_toolhead.get_last_move_time())
        self.is_homed = False

    def enable_motors(self):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        se = stepper_enable.lookup_enable(self.selector_stepper.get_name())
        se.motor_enable(self.mmu_unit.mmu_toolhead.get_last_move_time())

    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.mmu_unit.mmu_toolhead.get_position()[0]
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        else:
            return False
        return True


    # Internal Implementation --------------------------------------------------

    def _get_max_selector_movement(self):
        max_movement = self.mmu_unit.num_gates * self.p.cad_gate_width * self.p.cad_max_rotations
        return max_movement

    def _home_selector(self):
        """
        Home the selector by finding gate 0 via its index endstop.

        Clears current gate selection, then performs a gate-find operation.
        Raises MmuError with Klipper context on failure.
        """
        self.mmu.movequeues_wait()
        try:
            self._find_gate(0)
            self.is_homed = True
        except Exception as e: # Homing failed
            logging.error(traceback.format_exc())
            raise MmuError("Homing selector failed because of blockage or malfunction. Klipper reports: %s" % str(e))

    def _get_gate_endstop(self, gate):
        return "unit0_gate%d" % gate

    def _find_gate(self, gate):
        """
        Move in the best direction until the target gate's index endstop triggers.

        Performs a homing move toward the selected endstop and, if homed, centers
        on the index by moving half of selector_index_distance.
        """
        rotation_dir = self._best_rotation_direction(self.mmu.gate_selected, gate)
        max_move = self._get_max_selector_movement() * rotation_dir
        self.mmu.movequeues_wait()
        actual,homed = self._trace_selector_move("Indexing selector", max_move, speed=self.p.selector_move_speed, homing_move=1, endstop_name=self._get_gate_endstop(gate))
        if abs(actual) > 0 and homed:
            # If we actually moved to home make sure we are centered on index endstop
            center_move = (self.p.selector_index_distance / 2) * rotation_dir
            self._trace_selector_move("Centering selector", center_move, speed=self.p.selector_move_speed)

    # TODO automate the setup of the sequence through homing move on startup
    def _best_rotation_direction(self, start_gate, end_gate):
        """
        Choose rotation direction that reaches end_gate in the fewest steps.

        Uses a fixed forward gate sequence and compares forward vs reverse step
        counts from start_gate to end_gate.
        """
        if start_gate < 0:
            return 1 # Forward direction

        sequence = [0, 2, 1, 3] # Forward order of gates
        n = len(sequence)
        forward_distance = reverse_distance = 0

        # Find distance in forward direction
        start_idx = sequence.index(start_gate)
        for i in range(1, n):
            if sequence[(start_idx + i) % n] == end_gate:
                forward_distance = i
                break

        # Find distance in reverse direction
        rev_seq = sequence[::-1]
        start_idx = rev_seq.index(start_gate)
        for i in range(1, n):
            if rev_seq[(start_idx + i) % n] == end_gate:
                reverse_distance = i
                break

        return 1 if forward_distance <= reverse_distance else -1

    # Internal raw wrapper around all selector moves
    # Returns position after move, and if homed (homing moves)
    def _trace_selector_move(self, trace_str, dist, speed=None, accel=None, homing_move=0, endstop_name="default", wait=False):
        """
        Execute a selector move, optionally as a homing move to a named endstop.

        Returns (actual_dist, homed). For homing moves, uses HomingMove against
        the requested (extra) endstop and reports actual travel based on halt
        position.
        """
        null_rtn = (0., False)
        homed = False
        actual = dist

        self.mmu_unit.mmu_toolhead.quiesce()

        if homing_move != 0:
            # Check for valid endstop
            endstops = self.selector_rail.get_endstops() if endstop_name is None else self.selector_rail.get_extra_endstop(endstop_name)
            if endstops is None:
                self.mmu.log_error("Endstop '%s' not found" % endstop_name)
                return null_rtn

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.p.selector_homing_speed if homing_move != 0 else self.p.selector_move_speed
        accel = accel or self.p.selector_accel

        pos = self.mmu_unit.mmu_toolhead.get_position()
        if homing_move != 0:
            try:
                with self.mmu.wrap_accel(accel):
                    init_pos = pos[0]
                    pos[0] += dist
                    trig_pos = [0., 0., 0., 0.]
                    hmove = HomingMove(self.printer, endstops, self.mmu_unit.mmu_toolhead)
                    trig_pos = hmove.homing_move(pos, speed, probe_pos=True, triggered=homing_move > 0, check_triggered=True)
                    homed = True
            except self.printer.command_error as e:
                homed = False

            halt_pos = self.mmu_unit.mmu_toolhead.get_position()
            actual = halt_pos[0] - init_pos
            if self.mmu.log_enabled(LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR HOMING MOVE: max dist=%.1f, speed=%.1f, accel=%.1f, endstop_name=%s, wait=%s >> %s" % (dist, speed, accel, endstop_name, wait, "%s halt_pos=%.1f (rail moved=%.1f), trig_pos=%.1f" % ("HOMED" if homed else "DID NOT HOMED",  halt_pos[0], actual, trig_pos[0])))

        else:
            with self.mmu.wrap_accel(accel):
                pos[0] += dist
                self.mmu_unit.mmu_toolhead.move(pos, speed)
            if self.mmu.log_enabled(LOG_STEPPER):
                self.mmu.log_stepper("SELECTOR MOVE: position=%.1f, speed=%.1f, accel=%.1f" % (dist, speed, accel))

        self.mmu_unit.mmu_toolhead.flush_step_generation() # TTC mitigation (TODO: still required?)
        self.mmu.toolhead.flush_step_generation() # TTC mitigation (TODO: still required?)
        if wait:
            self.mmu.movequeues_wait(toolhead=False, mmu_toolhead=True)

        if trace_str:
            if homing_move != 0:
                trace_str += ". Stepper: selector %s after moving %.1fmm (of max %.1fmm)"
                trace_str = trace_str % (("homed" if homed else "did not home"), actual, dist)
                trace_str += ". Pos: @%.1f" % self.mmu_unit.mmu_toolhead.get_position()[0]
            else:
                trace_str += ". Stepper: selector moved %.1fmm" % dist
            trace_str += ". Pos: @%.1f" % self.mmu_unit.mmu_toolhead.get_position()[0]
            self.mmu.log_trace(trace_str)

        return actual, homed

    def _set_position(self, position):
        pos = self.mmu_unit.mmu_toolhead.get_position()
        pos[0] = position
        self.mmu_unit.mmu_toolhead.set_position(pos)
        self.enable_motors()
        self.is_homed = True
        return position
