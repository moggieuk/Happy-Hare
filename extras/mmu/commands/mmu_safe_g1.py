# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements _SAFE_G1 command
#  - Absolute G1 that is adjusted to stay within the printer's axis limits after
#    gcode offsets and skew correction are applied
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Happy Hare imports
from .mmu_base_command import *

# Clamped positions are pulled this far inside the limit. Also absorbs the small
# error when bed_mesh sits ahead of skew_correction in the move transform chain
LIMIT_MARGIN = 0.01


def live_limits(mmu):
    """
    The limits Klipper's check_move enforces right now, or None if the kinematics has
    no box limits (delta, polar...) or an axis is unhomed (Klipper sets (1, -1))
    """
    limits = getattr(mmu.toolhead.get_kinematics(), 'limits', None)
    if not limits or any(lo > hi for lo, hi in limits[:3]):
        return None
    return [tuple(l) for l in limits[:3]]


def _skew(mmu):
    skew = mmu.printer.lookup_object('skew_correction', None)
    if callable(getattr(skew, 'calc_skew', None)) and callable(getattr(skew, 'calc_unskew', None)):
        return skew
    return None


def _xy_kinematic(skew, raw, z):
    pos = [raw[0], raw[1], z, 0.]
    return skew.calc_skew(pos)[:2] if skew else pos[:2]


def safe_target(mmu, requested, limits, max_z=None):
    """
    Build the target of an absolute move exactly as Klipper's G1 does (gcode value plus
    offset, current position for axes not given) and, like Klipper's range check, look
    only at axes that move once skew is applied. Movement is measured from the toolhead's
    actual position because skew's round trip isn't exact, so an axis resting on a limit
    can appear to move just past it. Any that would end outside 'limits' are
    pulled just inside. With max_z, X and Y are also kept in range for any later Z move up
    to max_z, since skew moves them as Z changes.
    Returns the (possibly adjusted) target in Klipper's offset coordinates and a list of
    (axis, kinematic_pos, limit) for each axis that was over
    """
    gm = mmu.gcode_move
    base = gm.base_position
    current = list(gm.last_position[:3])
    raw = [requested[i] + base[i] if requested[i] is not None else current[i] for i in range(3)]
    overs = []

    # Skew passes Z through unchanged so Z is settled first
    z_hi = limits[2][1]
    if raw[2] != current[2] and raw[2] > z_hi:
        overs.append(('z', raw[2], z_hi))
        raw[2] = z_hi - LIMIT_MARGIN

    skew = _skew(mmu)
    heights = [raw[2]]
    if max_z is not None and min(max_z + base[2], z_hi) > raw[2]:
        heights.append(min(max_z + base[2], z_hi))
    start = mmu.toolhead.get_position()[:2] # Where Klipper's range check measures movement from

    # Y first because skewed X depends on Y. Each skewed axis moves 1:1 with its own axis
    for i, axis in ((1, 'y'), (0, 'x')):
        lo, hi = limits[i]
        kin = [_xy_kinematic(skew, raw, z)[i] for z in heights]
        checked = kin if kin[0] != start[i] else kin[1:]
        if not checked:
            continue
        if max(checked) > hi:
            overs.append((axis, max(checked), hi))
            raw[i] += hi - LIMIT_MARGIN - max(checked)
        elif min(checked) < lo:
            overs.append((axis, min(checked), lo))
            raw[i] += lo + LIMIT_MARGIN - min(checked)
    return raw, overs


class MmuSafeG1Command(BaseCommand):

    CMD = "_SAFE_G1"
    HELP_BRIEF = "Absolute G1 move kept within axis limits after gcode offset and skew correction"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "X = #(mm) Absolute X\n"
        + "Y = #(mm) Absolute Y\n"
        + "Z = #(mm) Absolute Z\n"
        + "F = #(mm/min) Speed\n"
        + "MAX_Z = #(mm) Also keep X,Y in range for following Z moves up to this height\n"
    )
    HELP_SUPPLEMENT = (
        "Coordinates are always absolute, as with G90, and only X, Y, Z are supported. If the\n"
        + "move would end out of range once Klipper applies gcode offset and skew_correction\n"
        + "it is adjusted to just inside the limit and a warning is logged. Use in place of G1\n"
        + "in a 'user_park_move_macro'\n"
        + "Examples:\n"
        + f"{CMD} X=10 Y=300 F=12000 ...Move to (10, 300), adjusted if necessary\n"
        + f"{CMD} Y=350 MAX_Z=40     ...Move to Y=350, adjusted so later Z moves up to 40 stay in range\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT, CATEGORY_INTERNAL, log=False)
        mmu.safe_g1 = self
        self.warned = False

    def reset_print_state(self):
        self.warned = False

    def _run(self, gcmd):
        mmu = self.mmu
        params = gcmd.get_command_parameters()
        requested = [gcmd.get_float(a, None) for a in "XYZ"]
        speed = gcmd.get_float('F', None, above=0.)
        max_z = gcmd.get_float('MAX_Z', None)
        if all(r is None for r in requested):
            return

        gm = mmu.gcode_move
        axes = ["%s%s" % (a, params[a]) for a in "XYZ" if a in params] # As requested
        limits = live_limits(mmu)
        if limits:
            raw, overs = safe_target(mmu, requested, limits, max_z)
            if overs:
                base = gm.base_position
                target = [raw[i] - base[i] for i in range(3)]
                axes = []
                for i, a in enumerate("XYZ"):
                    if requested[i] is not None and raw[i] == requested[i] + base[i]:
                        axes.append("%s%s" % (a, params[a]))
                    elif requested[i] is not None or raw[i] != gm.last_position[i]:
                        axes.append("%s%.6f" % (a, target[i]))
                over_str = ", ".join("%s %.3f beyond limit %.3f" % (a.upper(), p, l) for a, p, l in overs)
                msg = ("Move to (%s) adjusted to (%s) to stay within axis limits (%s)"
                       % (" ".join("%s%s" % (a, params[a]) for a in "XYZ" if a in params), " ".join(axes), over_str))
                if not self.warned:
                    self.warned = True
                    mmu.log_warning(
                        "Warning: %s.\n"
                        "Check your Happy Hare macros for positions or movement that exceed your build volume envelope. "
                        "To allow for Klipper skew compensation (or an X,Y gcode offset) it is often necessary to move "
                        "park/place coordinates slightly inside the boundary. Further adjustments this print are only "
                        "logged at debug level" % msg
                    )
                else:
                    mmu.log_debug(msg)

        cmd = "G1 %s" % " ".join(axes)
        if speed is not None:
            cmd += " F%s" % params['F']
        if not gm.absolute_coord:
            cmd = "G90\n%s\nG91" % cmd
        mmu.gcode.run_script_from_command(cmd)
