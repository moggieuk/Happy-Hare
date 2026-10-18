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


def config_limits(mmu):
    """
    The configured axis limits (valid before homing), or None if the kinematics has
    no box limits
    """
    kin = mmu.toolhead.get_kinematics()
    if getattr(kin, 'limits', None) is None:
        return None
    status = kin.get_status(mmu.reactor.monotonic())
    lo, hi = status['axis_minimum'], status['axis_maximum']
    return [(lo[i], hi[i]) for i in range(3)]


def _skew(mmu):
    skew = mmu.printer.lookup_object('skew_correction', None)
    if callable(getattr(skew, 'calc_skew', None)) and callable(getattr(skew, 'calc_unskew', None)):
        return skew
    return None


def skew_fingerprint(mmu):
    """The skew in effect, read through the transform so it doesn't depend on attribute names"""
    skew = _skew(mmu)
    if skew is None:
        return None
    return tuple(tuple(skew.calc_skew(p)[:2]) for p in ([0., 1., 0., 0.], [0., 0., 1., 0.]))


def gcode_to_kinematic(mmu, gpos):
    """Where Klipper's range check will see an absolute gcode XYZ"""
    base = mmu.gcode_move.base_position
    raw = [gpos[i] + base[i] for i in range(3)]
    skew = _skew(mmu)
    return skew.calc_skew(raw + [0.])[:3] if skew else raw


def clamp_gcode_position(mmu, gpos, limits):
    """
    Map an absolute gcode XYZ the way Klipper does (offset, then skew) and pull any
    axis that would end outside 'limits' back inside. Returns the (possibly adjusted)
    gcode XYZ and a list of (axis, kinematic_pos, limit) for each axis that was over
    """
    base = mmu.gcode_move.base_position
    raw = [gpos[i] + base[i] for i in range(3)]
    overs = []

    # Skew passes Z through unchanged so Z is settled first
    z_hi = limits[2][1]
    if raw[2] > z_hi:
        overs.append(('z', raw[2], z_hi))
        raw[2] = z_hi - LIMIT_MARGIN

    skew = _skew(mmu)
    kpos = skew.calc_skew(raw + [0.])[:3] if skew else list(raw)
    adjusted = False
    for i, axis in ((0, 'x'), (1, 'y')):
        lo, hi = limits[i]
        if kpos[i] < lo:
            overs.append((axis, kpos[i], lo))
            kpos[i] = lo + LIMIT_MARGIN
            adjusted = True
        elif kpos[i] > hi:
            overs.append((axis, kpos[i], hi))
            kpos[i] = hi - LIMIT_MARGIN
            adjusted = True

    if adjusted:
        raw = skew.calc_unskew(kpos + [0.])[:3] if skew else kpos
    return [raw[i] - base[i] for i in range(3)], overs


class MmuSafeG1Command(BaseCommand):

    CMD = "_SAFE_G1"
    HELP_BRIEF = "Absolute G1 move kept within axis limits after gcode offset and skew correction"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "X = #(mm) Absolute X\n"
        + "Y = #(mm) Absolute Y\n"
        + "Z = #(mm) Absolute Z\n"
        + "F = #(mm/min) Speed\n"
    )
    HELP_SUPPLEMENT = (
        "Coordinates are always absolute, as with G90, and only X, Y, Z are supported. If the\n"
        + "move would end out of range once Klipper applies gcode offset and skew_correction\n"
        + "it is adjusted to just inside the limit and a warning is logged. Use in place of G1\n"
        + "in a 'user_park_move_macro'\n"
        + "Examples:\n"
        + f"{CMD} X=10 Y=300 F=12000 ...Move to (10, 300), adjusted if necessary\n"
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
        if all(r is None for r in requested):
            return

        gm = mmu.gcode_move
        new = None
        limits = live_limits(mmu)
        if limits:
            current = gm.get_status(mmu.reactor.monotonic())['gcode_position']
            target = [r if r is not None else current[i] for i, r in enumerate(requested)]
            new, overs = clamp_gcode_position(mmu, target, limits)
            if overs:
                over_str = ", ".join("%s %.3f beyond limit %.3f" % (a.upper(), p, l) for a, p, l in overs)
                msg = ("Move to (x:%.3f, y:%.3f, z:%.3f) adjusted to (x:%.3f, y:%.3f, z:%.3f) to stay within axis limits (%s)"
                       % (tuple(target) + tuple(new) + (over_str,)))
                if not self.warned:
                    self.warned = True
                    mmu.log_warning("Warning: %s. Likely skew_correction or gcode offset. Run MMU_CHECK_POSITIONS to review configured positions" % msg)
                else:
                    mmu.log_debug(msg)
            else:
                new = None

        if new is None:
            # Unchanged so issue as requested
            axes = " ".join("%s%s" % (a, params[a]) for a in "XYZ" if a in params)
        else:
            axes = " ".join("%s%.6f" % (a, v) for a, v, r, o in zip("XYZ", new, requested, target)
                            if r is not None or abs(v - o) > 1e-9)
        cmd = "G1 %s" % axes
        if speed is not None:
            cmd += " F%s" % params['F']
        if not gm.absolute_coord:
            cmd = "G90\n%s\nG91" % cmd
        mmu.gcode.run_script_from_command(cmd)
