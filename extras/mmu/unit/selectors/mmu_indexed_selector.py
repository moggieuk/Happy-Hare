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
import logging
from typing                 import Sequence

# Klipper imports

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
        ParamSpec('selector_accel',         'float', 1200.0, section="SELECTOR", limits=dict(above=1.0)),
        ParamSpec('selector_index_distance','float',    5.0, section="SELECTOR", limits=dict(minval=0.0)),

        ParamSpec('cad_gate_width',         'float',     90, section="CAD",      limits=dict(above=0.0),  hidden=True),
        ParamSpec('cad_max_rotations',      'int',        2, section="CAD",      limits=dict(minval=0),   hidden=True),
    )

    def __init__(self, config, selector):
        self._selector = selector
        super().__init__(config)



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

        self.selector_stepper_name = mmu_unit.config.get('selector_stepper') # Name of selector stepper
        stepper_section = f"mmu_stepper {self.selector_stepper_name}"

        # Force stepper loading now (TMC first)
        tmc_found = False
        for chip in TMC_CHIPS: 
            tmc_section = f"{chip} {stepper_section}"
            if config.has_section(tmc_section):
                _ = self.printer.load_object(config, tmc_section)
                logging.info("MMU: Loaded: [%s]" % tmc_section)
                tmc_found = True
                break
        if not tmc_found:
            raise config.error("Selector stepper TMC configuration not found for %s on mmu_unit %s" % (self.selector_stepper_name, self.name))

        # Now we can load the mmu_stepper object
        self.selector_stepper = self.printer.load_object(config, stepper_section)
        logging.info("MMU: Loaded: [%s]" % stepper_section)

        # Current local gate selected
        self.lgate_selected = None


    # Selector "Interface" methods ---------------------------------------------

    def handle_connect(self):
        super().handle_connect()


    def handle_ready(self):
        super().handle_ready()

        # Design don't need homing or calibration
        self.is_homed = True
        self.calibrator.mark_calibrated(CALIBRATED_SELECTOR)
        self._set_position(0)


    def enable_motors(self):
        self.selector_stepper.do_enable(True)


    def disable_motors(self):
        self.selector_stepper.do_enable(False)


    def buzz_motor(self, motor):
        if motor == "selector":
            pos = self.selector_stepper.commanded_pos
            self.move(None, pos + 5, wait=False)
            self.move(None, pos - 5, wait=False)
            self.move(None, pos, wait=False)
        else:
            return False
        return True


    # Internal Implementation --------------------------------------------------

    def _select_gate(self, lgate):
        """
        Select a gate by moving until the corresponding index endstop triggers.

        Verifies the configured extra endstop for the gate exists, then only
        moves if the endstop is not already triggered.
        """
        super()._select_gate(lgate)

        if lgate >= 0:
            activated = self._query_endstop(self._get_gate_endstop_name(lgate))
            if not activated:
                with self.mmu.wrap_action(ACTION_SELECTING):
                    self._find_gate(lgate)


    def _get_max_selector_movement(self):
        max_movement = self.mmu_unit.num_gates * self.p.cad_gate_width * self.p.cad_max_rotations
        return max_movement


    def _home_selector(self):
        """
        Home the selector by finding gate 0 via its index endstop.
        Ensures we are off the endstop and then performs a gate-find operation.
        """
        self.mmu.movequeue_wait()

        activated = self._query_endstop(self._get_gate_endstop_name(0))
        if activated:
            # Move half gate width to get off endstop
            self._move_rel("Moving of endstop", -(self.p.cad_gate_width / 2))

        self._find_gate(0)
        self._set_position(0)


    def _get_gate_endstop_name(self, lgate):
        return "%s_gate%d" % (self.mmu_unit.name, lgate)


    def _query_endstop(self, name):
        endstop = self.selector_stepper.rail.get_extra_endstop(name)
        if not endstop:
            raise MmuError(f"Extra endstop {name} not defined on the selector stepper")

        mcu_endstop = endstop[0][0]
        return mcu_endstop.query_endstop(self.mmu.toolhead.get_last_move_time())


    def _find_gate(self, lgate):
        """
        Move in the best direction until the target gate's index endstop triggers.

        Performs a homing move toward the selected endstop and, if homed, centers
        on the index by moving half of selector_index_distance.
        """
        rotation_dir = self._best_rotation_direction(self.lgate_selected, lgate)
        max_move = self._get_max_selector_movement() * rotation_dir
        delta,homed = self._homing_move_rel("Indexing selector", max_move, homing_move=1, endstop_name=self._get_gate_endstop_name(lgate))
        if abs(delta) > 0 and homed:
            # If we actually moved to home make sure we are centered on index endstop
            center_move = (self.p.selector_index_distance / 2) * rotation_dir
            self._move_rel("Centering selector", center_move)
        self.lgate_selected = lgate


    # TODO automate the setup of the sequence through homing move on startup
    def _best_rotation_direction(self, start_lgate, end_lgate):
        """
        Choose rotation direction that reaches end_lgate in the fewest steps.

        Uses a fixed forward gate sequence and compares forward vs reverse step
        counts from start_lgate to end_lgate.
        """
        if start_lgate is None:
            return 1 # Forward direction

        sequence = [0, 2, 1, 3] # Forward order of gates
        n = len(sequence)
        forward_distance = reverse_distance = 0

        # Find distance in forward direction
        start_idx = sequence.index(start_lgate)
        for i in range(1, n):
            if sequence[(start_idx + i) % n] == end_lgate:
                forward_distance = i
                break

        # Find distance in reverse direction
        rev_seq = sequence[::-1]
        start_idx = rev_seq.index(start_lgate)
        for i in range(1, n):
            if rev_seq[(start_idx + i) % n] == end_lgate:
                reverse_distance = i
                break

        return 1 if forward_distance <= reverse_distance else -1


    def _move_rel(self, trace_str, dist, speed=None, accel=None, wait=False):
        """
        Relative selector movement.
        Returns relative distance moved
        """
        start_pos = self.selector_stepper.commanded_pos
        new_pos = start_pos + dist
        actual,_ = self._move_selector(trace_str, new_pos, speed=speed, accel=accel, wait=wait)
        return (actual - start_pos)


    def _homing_move_rel(self, trace_str, dist, speed=None, accel=None, homing_move=0, endstop_name=None):
        """
        Relative selector homing movement.
        Returns relative distance actually moved and homing state
        """
        start_pos = self.selector_stepper.commanded_pos
        new_pos = start_pos + dist
        actual,homed = self._move_selector(trace_str, new_pos, speed=speed, accel=accel, homing_move=homing_move, endstop_name=endstop_name)
        return (actual - start_pos),homed


    def _move_selector(self, trace_str, new_pos, speed=None, accel=None, homing_move=0, endstop_name=None, wait=False):
        """
        Execute a selector move, optionally using a homing move to an endstop.

        Returns (position, homed). For homing moves, selects the requested
        endstop set (default or extra).
        """
        if trace_str:
            self.mmu.log_trace(trace_str)

        self.mmu.movequeue_wait()

        # Set appropriate speeds and accel if not supplied
        speed = speed or self.p.selector_move_speed
        accel = accel or self.p.selector_accel

        pos = self.selector_stepper.commanded_pos

        if homing_move != 0:
            # Check for valid endstop
            endstops = self.selector_stepper.rail.get_endstops() if endstop_name is None else self.selector_stepper.rail.get_extra_endstop(endstop_name)
            if endstops is None:
                self.mmu.log_error("Endstop '%s' not found" % endstop_name)
                return self.selector_stepper.commanded_pos, False

            home_result = {
                'halt_pos': pos,
                'trig_pos': pos,
            }
            homed = True

            try:
                home_result = self.selector_stepper.do_homing_move(
                    new_pos,
                    speed,
                    accel,
                    probe_pos=True,
                    triggered=(homing_move > 0),
                    check_trigger=True,
                    endstop_name=endstop_name,
                )
            except self.printer.command_error:
                homed = False
            finally:
                self.mmu.toolhead.flush_step_generation() # TTC mitigation when homing move + regular + get_last_move_time() in close succession

            actual = home_result['halt_pos'] - pos
            result = f"HOMED actual halt_pos={home_result['halt_pos']:.2f}, trig_pos={home_result['trig_pos']:.2f}" if homed else "DID NOT HOME"
            self.mmu.log_stepper(
                f"SELECTOR HOMING MOVE: requested position={new_pos:.1f}, "
                f"speed={speed:.1f}, accel={accel:.1f}, "
                f"endstop_name={endstop_name} >> {result} (actual_delta: {actual:.1f})"
            )

        else:
            homed = False
            self.selector_stepper.do_move(new_pos, speed, accel)

            actual = self.selector_stepper.commanded_pos - pos
            self.mmu.log_stepper(
                f"SELECTOR MOVE: requested position={new_pos:.1f}, "
                f"speed={speed:.1f}, accel={accel:.1f}, actual_delta={actual:.1f}"
            )

            self.mmu.toolhead.flush_step_generation() # TTC mitigation
            if wait:
                self.mmu.movequeue_wait()

        return self.selector_stepper.commanded_pos, homed


    def _set_position(self, position):
        self.selector_stepper.do_set_position(position)
        self.enable_motors()
