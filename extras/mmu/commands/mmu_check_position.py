# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_CHECK_POSITION command
#  - Reports whether an X,Y gcode position stays within the axis limits at every Z
#    height once gcode offset and skew correction are applied
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Happy Hare imports
from .mmu_base_command import *
from .mmu_safe_g1      import LIMIT_MARGIN


def _skewed_xy(factors, x, y, z):
    """Klipper's skew_correction calc_skew for (xy, xz, yz) factors, so a profile can be checked without loading it"""
    xy, xz, yz = factors
    return x - y * xy - z * (xz - xy * yz), y - z * yz


def _profile_factors(profile):
    return (profile['xy_skew'], profile['xz_skew'], profile['yz_skew'])


class MmuCheckPositionCommand(BaseCommand):

    CMD = "MMU_CHECK_POSITION"
    HELP_BRIEF = "Check an X,Y position is within the axis limits at all heights with skew correction and gcode offset"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "X     = #(mm) Gcode X position\n"
        + "Y     = #(mm) Gcode Y position\n"
        + "SKEW  = _name_ Saved skew profile to check with (default: current skew)\n"
        + "ERROR = [0|1] Raise an error if the position is out of range\n"
    )
    HELP_SUPPLEMENT = (
        "Skew correction moves the toolhead further as Z increases so the position is checked\n"
        + "from Z=0 to the axis maximum, reporting the height at which it goes out of range. The\n"
        + "current X,Y gcode offset is applied and an axis not given is checked at both ends of\n"
        + "its range. Nothing moves. ERROR=1 can be used in PRINT_START, after SKEW_PROFILE LOAD,\n"
        + "to stop a print that would fail\n"
        + "Examples:\n"
        + f"{CMD} X=19 Y=355              ...Check with the current skew\n"
        + f"{CMD} X=19 Y=355 SKEW=my_skew ...Check with saved skew profile 'my_skew'\n"
        + f"{CMD} Y=355 ERROR=1           ...Raise an error if out of range\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT, CATEGORY_TESTING)

    def _run(self, gcmd):
        mmu = self.mmu
        requested = [gcmd.get_float('X', None), gcmd.get_float('Y', None)]
        profile = gcmd.get('SKEW', None)
        error = bool(gcmd.get_int('ERROR', 0, minval=0, maxval=1))
        if requested == [None, None]:
            raise gcmd.error("X and/or Y must be specified")

        kin = mmu.toolhead.get_kinematics()
        if getattr(kin, 'limits', None) is None:
            mmu.log_always("Position check is only possible on kinematics with rectangular axis limits")
            return
        status = kin.get_status(mmu.reactor.monotonic())
        limits = [(status['axis_minimum'][i], status['axis_maximum'][i]) for i in range(3)]
        z_range = (max(limits[2][0], 0.), limits[2][1])

        factors, skew_str = self._skew(gcmd, profile)
        base = mmu.gcode_move.base_position
        position_str = " ".join("%s%.2f" % (a, v) for a, v in zip("XY", requested) if v is not None)
        lines = [
            skew_str,
            "Gcode offset: X%.2f Y%.2f" % (base[0], base[1]),
            "Checking %s from Z=%.1f to Z=%.1f:" % (position_str, z_range[0], z_range[1]),
        ]
        problems = []
        for i, axis in enumerate("XY"):
            if requested[i] is None:
                continue
            line, problem = self._check_axis(i, requested, base, factors, limits, z_range)
            lines.append("  %s: %s" % (axis, line))
            if problem:
                problems.append("%s%.2f %s" % (axis, requested[i], problem))

        mmu.log_always("\n".join(lines))
        if error and problems:
            raise gcmd.error("%s: position out of range: %s" % (self.CMD, "; ".join(problems)))

    def _skew(self, gcmd, profile):
        """Returns the (xy, xz, yz) factors to check with and a line saying where they came from"""
        skew = self.printer.lookup_object('skew_correction', None)
        profiles = getattr(skew, 'skew_profiles', {}) if skew else {}
        fmt = lambda f: "xy=%.6f xz=%.6f yz=%.6f" % f

        if profile is not None:
            if skew is None:
                raise gcmd.error("SKEW=%s given but [skew_correction] is not configured" % profile)
            if profile not in profiles:
                raise gcmd.error("Unknown skew profile '%s'. Saved profiles: %s" % (profile, ", ".join(sorted(profiles)) or "none"))
            factors = _profile_factors(profiles[profile])
            return factors, "Skew: profile '%s' (supplied) %s" % (profile, fmt(factors))

        if skew is None:
            return (0., 0., 0.), "Skew: skew_correction not configured"
        factors = (skew.xy_factor, skew.xz_factor, skew.yz_factor)
        if not any(factors):
            return factors, "Skew: no skew profile provided or loaded"
        # Klipper keeps current_profile_name after SET_SKEW or a failed load so confirm the factors match
        name = getattr(skew, 'current_profile_name', '')
        if name in profiles and _profile_factors(profiles[name]) == factors:
            return factors, "Skew: profile '%s' (currently loaded) %s" % (name, fmt(factors))
        return factors, "Skew: current factors, not from a saved profile %s" % fmt(factors)

    def _check_axis(self, i, requested, base, factors, limits, z_range):
        """
        Skewed position is linear in Z so checking at the lowest and highest Z covers every
        height. An axis not given could be anywhere so both ends of its range are tried.
        Returns the report line and a problem summary (or None)
        """
        lo, hi = limits[i]
        others = [requested[1 - i] + base[1 - i]] if requested[1 - i] is not None else list(limits[1 - i])
        raw = requested[i] + base[i]
        kins, worst = [], None # worst is (overrun, side, overrun at each z)
        for other in others:
            rx, ry = (raw, other) if i == 0 else (other, raw)
            k = [_skewed_xy(factors, rx, ry, z)[i] for z in z_range]
            kins.extend(k)
            for side, limit in ((1, hi), (-1, lo)):
                over = [(kz - limit) * side for kz in k]
                if max(over) > 0 and (worst is None or max(over) > worst[0]):
                    worst = (max(over), side, over)

        if worst is None:
            return "OK (machine %.2f..%.2f, limits %.2f..%.2f)" % (min(kins), max(kins), lo, hi), None

        overrun, side, over = worst
        if min(over) > 0:
            where = "at all heights"
        else:
            fail_z = z_range[0] - over[0] * (z_range[1] - z_range[0]) / (over[1] - over[0])
            where = "%s Z=%.1f" % ("above" if over[1] > 0 else "below", fail_z)
        problem = "%s %.2f by %.2f %s" % ("exceeds maximum" if side > 0 else "below minimum", hi if side > 0 else lo, overrun, where)
        safe = requested[i] - side * (overrun + LIMIT_MARGIN)
        return "%s. Use %s %s %.2f for all heights" % (problem, "XY"[i], "<=" if side > 0 else ">=", safe), problem
