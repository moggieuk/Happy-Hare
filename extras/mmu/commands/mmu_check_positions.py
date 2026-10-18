# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_CHECK_POSITIONS and _MMU_CHECK_POSITION commands
#  - Checks the X,Y positions configured for Happy Hare macros (parking, cut tip)
#    against the axis limits after gcode offset and skew correction
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *
from .mmu_safe_g1      import config_limits, gcode_to_kinematic, skew_fingerprint, LIMIT_MARGIN

# Each macro reports the points it moves to by calling _MMU_CHECK_POSITION
PARK_CHECK_MACRO      = "_MMU_PARK_CHECK_POSITIONS"
CUT_TIP_CHECK_MACRO   = "_MMU_CUT_TIP_CHECK_POSITIONS"

# Operations that move filament and so would run the cut tip
BLOCKING_OPERATIONS = ['toolchange', 'load', 'unload', 'runout']


class MmuCheckPositionsCommand(BaseCommand):

    CMD = "MMU_CHECK_POSITIONS"
    HELP_BRIEF = "Check configured macro X,Y positions are reachable with current skew correction and gcode offset"
    HELP_PARAMS = (
        f"{CMD}: {HELP_BRIEF}\n"
        + "PAUSE = [0|1] Pause the print if a cut tip position is out of range\n"
    )
    HELP_SUPPLEMENT = (
        "Positions are checked from Z=0 up to the axis maximum because skew correction moves\n"
        + "the toolhead further as Z increases. Park positions are adjusted automatically when\n"
        + "used so are only reported. The cut tip pin positions must be exact so, if out of\n"
        + "range, a print is paused before the next toolchange. This check also runs at\n"
        + "startup and, during a print, whenever skew correction or the gcode offset changes\n"
        + "Examples:\n"
        + f"{CMD}         ...Report any out of range positions\n"
        + f"{CMD} PAUSE=1 ...Also pause the print if a cut tip position is out of range\n"
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(self.CMD, self._run, self.HELP_BRIEF, self.HELP_PARAMS, self.HELP_SUPPLEMENT, CATEGORY_GENERAL)
        self.register("_MMU_CHECK_POSITION", self._run_check_position, "Report a macro X,Y position for MMU_CHECK_POSITIONS",
                      "_MMU_CHECK_POSITION: X= Y= LABEL= FATAL=[0|1]\n", None, CATEGORY_INTERNAL, log=False)
        mmu.position_checker = self
        self._points = None
        self.reset_print_state()

    def reset_print_state(self):
        self._last_summary = None
        self._last_fingerprint = None

    def _run(self, gcmd):
        mmu = self.mmu
        pause = bool(gcmd.get_int('PAUSE', 0, minval=0, maxval=1))
        summary, fatal = self.check(verbose=True)
        if pause and fatal and mmu.is_in_print():
            mmu.handle_mmu_error(fatal)

    def _run_check_position(self, gcmd):
        if self._points is None:
            return # Only meaningful while MMU_CHECK_POSITIONS is collecting
        self._points.append((
            gcmd.get('LABEL', 'position'),
            gcmd.get_float('X', None),
            gcmd.get_float('Y', None),
            bool(gcmd.get_int('FATAL', 0, minval=0, maxval=1)),
        ))

    def _check_macros(self):
        mmu = self.mmu
        macros = [PARK_CHECK_MACRO]
        if mmu._macro_name(mmu.p.form_tip_macro).upper() == MACRO_CUT_TIP:
            macros.append(CUT_TIP_CHECK_MACRO)
        return [m for m in macros if mmu.printer.lookup_object("gcode_macro %s" % m, None)]

    def _collect_points(self):
        self._points = []
        try:
            for macro in self._check_macros():
                self.mmu.wrap_gcode_command(macro)
            return self._points
        finally:
            self._points = None

    def _evaluate(self, label, x, y, fatal, limits):
        """
        Kinematic position is linear in Z so checking at the lowest and highest Z covers
        every height. An axis the macro doesn't move could be anywhere so both ends are tried
        """
        base = self.mmu.gcode_move.base_position
        z_range = [max(limits[2][0], 0.) - base[2], limits[2][1] - base[2]]
        ranges = [[x] if x is not None else [limits[0][0] - base[0], limits[0][1] - base[0]],
                  [y] if y is not None else [limits[1][0] - base[1], limits[1][1] - base[1]]]
        problems = []
        for i, (axis, requested) in enumerate((('x', x), ('y', y))):
            if requested is None:
                continue
            lo, hi = limits[i]
            worst = None # (overrun, z) - positive overrun past max, negative past min
            for gx in ranges[0]:
                for gy in ranges[1]:
                    k = [gcode_to_kinematic(self.mmu, [gx, gy, z])[i] for z in z_range]
                    for side, limit in ((1, hi), (-1, lo)):
                        over = [(kz - limit) * side for kz in k]
                        if max(over) <= 0:
                            continue
                        # Height above (or below) which the position is out of range
                        where = ""
                        if over[0] <= 0 or over[1] <= 0:
                            fail_z = z_range[0] - over[0] * (z_range[1] - z_range[0]) / (over[1] - over[0])
                            where = " %s Z=%.1f" % ("above" if over[1] > 0 else "below", fail_z)
                        if worst is None or max(over) > abs(worst[0]):
                            worst = (max(over) * side, where)
            if worst:
                overrun, where = worst
                safe = requested - overrun - LIMIT_MARGIN * (1 if overrun > 0 else -1)
                problems.append("%s (x:%s, y:%s): %s %s by %.2fmm%s, use %s %s %.2f%s" % (
                    label,
                    "%.1f" % x if x is not None else "n/a",
                    "%.1f" % y if y is not None else "n/a",
                    axis.upper(), "exceeds maximum" if overrun > 0 else "below minimum", abs(overrun), where,
                    axis, "<=" if overrun > 0 else ">=", safe,
                    " [cannot be adjusted - will pause print]" if fatal else "",
                ))
        return problems

    def check(self, verbose=False):
        """
        Returns (summary, fatal_summary) where either may be None. Logs the summary
        if verbose or it has changed since last reported this print
        """
        mmu = self.mmu
        limits = config_limits(mmu)
        if limits is None:
            if verbose:
                mmu.log_always("Position check is only possible on kinematics with rectangular axis limits")
            return None, None

        warnings, fatal = [], []
        for label, x, y, is_fatal in self._collect_points():
            problems = self._evaluate(label, x, y, is_fatal, limits)
            warnings.extend(problems)
            if is_fatal:
                fatal.extend(problems)

        summary = None
        if warnings:
            summary = ("Warning: Configured positions out of range with current skew_correction/gcode offset (checked Z=0 to %.1f):\n  %s"
                       % (limits[2][1], "\n  ".join(warnings)))
            if verbose or summary != self._last_summary:
                mmu.log_warning(summary)
            else:
                mmu.log_debug(summary)
        elif verbose:
            mmu.log_always("All configured positions are within axis limits (checked Z=0 to %.1f)" % limits[2][1])
        self._last_summary = summary
        fatal_summary = ("Configured position(s) are out of range and would cause a 'Move out of range' error:\n  %s\n"
                         "Adjust the configuration (MMU_CHECK_POSITIONS to re-check) or RESUME to continue anyway" % "\n  ".join(fatal)) if fatal else None
        return summary, fatal_summary

    def before_toolhead_operation(self, operation):
        """
        Called before Happy Hare parks the toolhead. Checks once per print and again only if
        skew correction or the XY gcode offset changes (skew is often loaded after the print
        starts). The Z offset is ignored because positions are checked over the whole machine
        Z range regardless. Resuming without a fix is the user's choice so isn't re-checked
        """
        mmu = self.mmu
        if operation not in BLOCKING_OPERATIONS or not mmu.is_printing():
            return
        if 'xy' not in mmu.toolhead.get_status(mmu.reactor.monotonic())['homed_axes']:
            return
        fingerprint = (tuple(mmu.gcode_move.base_position[:2]), skew_fingerprint(mmu))
        if fingerprint == self._last_fingerprint:
            return
        self._last_fingerprint = fingerprint
        _, fatal = self.check()
        if fatal:
            raise MmuError(fatal)
