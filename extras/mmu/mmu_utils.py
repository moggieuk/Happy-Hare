# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Utility classes for Happy Hare MMU
#
# MmuError
# Goal: Wrapper exception for all MMU errors.
#
# SaveVariableManager
# Goal: Centralization of all save_variable manipulation for per-unit namespacing and efficiency.
#
# DebugStepperMovement
# Goal: Internal testing class for debugging synced movement.
#
# PurgeVolCalculator
# Goal: Purge volume calculator based on color change
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, math, contextlib

# Happy Hare imports
from .mmu_constants import *


# Mmu exception error class
class MmuError(Exception):
    pass


# Centralization of all save_variable manipulation for per-unit namespacing and efficiency
class SaveVariableManager:

    def __init__(self, mmu_machine, config):
        self.mmu_machine = mmu_machine
        self.gcode = self.mmu_machine.printer.lookup_object('gcode')
        self.save_variables = self.mmu_machine.printer.load_object(config, 'save_variables')

        self._can_write_variables = True

        # Sanity check to see that mmu_vars.cfg is included.  This will verify path
        # because default deliberately has 'mmu_revision' entry
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(VARS_MMU_REVISION, None)
            if revision_var is None:
                self.save_variables.allVariables[VARS_MMU_REVISION] = 0
        else:
            revision_var = None
        if not self.save_variables or revision_var is None:
            raise config.error("Calibration settings file (mmu_vars.cfg) not found. Check [save_variables] section in mmu_macro_vars.cfg\nAlso ensure you only have a single [save_variables] section defined in your printer config and it contains the line: mmu__revision = 0. If not, add this line and restart")

    # Namespace variable with mmu unit name if necessary
    def namespace(self, variable, namespace):
        if namespace is not None:
            return variable.replace("mmu_", "mmu_%s_" % namespace)
        return variable

    # Wrappers so we can minimize actual disk writes and batch updates
    def get(self, variable, default, namespace=None):
        return self.save_variables.allVariables.get(self.namespace(variable, namespace), default)

    def set(self, variable, value, namespace=None, write=False):
        self.save_variables.allVariables[self.namespace(variable, namespace)] = value
        if write:
            self.write()

    def delete(self, variable, namespace=None, write=False):
        _ = self.save_variables.allVariables.pop(self.namespace(variable, namespace), None)
        if write:
            self.write()

    def write(self):
        if self._can_write_variables:
            mmu_vars_revision = self.save_variables.allVariables.get(VARS_MMU_REVISION, 0) + 1
            self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (VARS_MMU_REVISION, mmu_vars_revision))

    def upgrade(self, variable, namespace): # PAUL need this method?
        val = self.get(variable, None)
        if val is not None:
            self.set(variable, val, namespace)
            self.delete(variable)

    @contextlib.contextmanager
    def wrap_suspend_write_variables(self):
        self._can_write_variables = False
        try:
            yield self
        finally:
            self._can_write_variables = True
            self.write()


# Internal testing class for debugging synced movement
# Add this around move logic:
#     with DebugStepperMovement(self):
#        <synced_move>
class DebugStepperMovement:
    def __init__(self, mmu, debug=False):
        self.mmu = mmu
        self.debug = debug

    def __enter__(self):
        if self.debug:
            self.g_steps0 = self.mmu.gear_rail.steppers[0].get_mcu_position()
            self.g_pos0 = self.mmu.gear_rail.steppers[0].get_commanded_position()
            self.e_steps0 = self.mmu.mmu_extruder_stepper.stepper.get_mcu_position()
            self.e_pos0 = self.mmu.mmu_extruder_stepper.stepper.get_commanded_position()
            self.rail_pos0 = self.mmu.mmu_toolhead.get_position()[1]

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.debug:
            self.mmu.log_always("Waiting for movement to complete...")
            self.mmu.movequeues_wait()
            g_steps1 = self.mmu.gear_rail.steppers[0].get_mcu_position()
            g_pos1 = self.mmu.gear_rail.steppers[0].get_commanded_position()
            e_steps1 = self.mmu.mmu_extruder_stepper.stepper.get_mcu_position()
            e_pos1 = self.mmu.mmu_extruder_stepper.stepper.get_commanded_position()
            rail_pos1 = self.mmu.mmu_toolhead.get_position()[1]
            self.mmu.log_always("Gear steps: %d = %.4fmm, commanded movement: %.4fmm" % (g_steps1 - self.g_steps0, (g_steps1 - self.g_steps0) * self.mmu.gear_rail.steppers[0].get_step_dist(), g_pos1 - self.g_pos0))
            self.mmu.log_always("Extruder steps: %d = %.4fmm, commanded movement: %.4fmm" % (e_steps1 - self.e_steps0, (e_steps1 - self.e_steps0) * self.mmu.mmu_extruder_stepper.stepper.get_step_dist(), e_pos1 - self.e_pos0))
            self.mmu.log_always("Rail movement: %.4fmm" % (rail_pos1 - self.rail_pos0))


class PurgeVolCalculator:
    def __init__(self, min_purge_vol, max_purge_vol, multiplier):
        self.min_purge_vol = min_purge_vol
        self.max_purge_vol = max_purge_vol
        self.multiplier = multiplier

    def calc_purge_vol_by_rgb(self, src_r, src_g, src_b, dst_r, dst_g, dst_b):
        src_r_f = float(src_r) / 255.0
        src_g_f = float(src_g) / 255.0
        src_b_f = float(src_b) / 255.0
        dst_r_f = float(dst_r) / 255.0
        dst_g_f = float(dst_g) / 255.0
        dst_b_f = float(dst_b) / 255.0

        from_hsv_h, from_hsv_s, from_hsv_v = self.RGB2HSV(src_r_f, src_g_f, src_b_f)
        to_hsv_h, to_hsv_s, to_hsv_v = self.RGB2HSV(dst_r_f, dst_g_f, dst_b_f)
        hs_dist = self.DeltaHS_BBS(from_hsv_h, from_hsv_s, from_hsv_v, to_hsv_h, to_hsv_s, to_hsv_v)
        from_lumi = self.get_luminance(src_r_f, src_g_f, src_b_f)
        to_lumi = self.get_luminance(dst_r_f, dst_g_f, dst_b_f)

        lumi_purge = 0.0
        if to_lumi >= from_lumi:
            lumi_purge = math.pow(to_lumi - from_lumi, 0.7) * 560.0
        else:
            lumi_purge = (from_lumi - to_lumi) * 80.0

        inter_hsv_v = 0.67 * to_hsv_v + 0.33 * from_hsv_v
        hs_dist = min(inter_hsv_v, hs_dist)
        hs_purge = 230.0 * hs_dist

        purge_volume = self.calc_triangle_3rd_edge(hs_purge, lumi_purge, 120.0)
        purge_volume = max(purge_volume, 0.0)
        purge_volume += self.min_purge_vol
        purge_volume *= self.multiplier
        purge_volume = min(int(purge_volume), self.max_purge_vol)

        return purge_volume

    def calc_purge_vol_by_hex(self, src_clr, dst_clr):
        src_rgb = self.hex_to_rgb(src_clr)
        dst_rgb = self.hex_to_rgb(dst_clr)
        return self.calc_purge_vol_by_rgb(*(src_rgb + dst_rgb))

    @staticmethod
    def RGB2HSV(r, g, b):
        Cmax = max(r, g, b)
        Cmin = min(r, g, b)
        delta = Cmax - Cmin

        if abs(delta) < 0.001:
            h = 0.0
        elif Cmax == r:
            h = 60.0 * math.fmod((g - b) / delta, 6.0)
        elif Cmax == g:
            h = 60.0 * ((b - r) / delta + 2)
        else:
            h = 60.0 * ((r - g) / delta + 4)
        s = 0.0 if abs(Cmax) < 0.001 else delta / Cmax
        v = Cmax
        return h, s, v

    @staticmethod
    def to_radians(degree):
        return degree / 180.0 * math.pi

    @staticmethod
    def get_luminance(r, g, b):
        return r * 0.3 + g * 0.59 + b * 0.11

    @staticmethod
    def calc_triangle_3rd_edge(edge_a, edge_b, degree_ab):
        return math.sqrt(edge_a * edge_a + edge_b * edge_b - 2 * edge_a * edge_b * math.cos(PurgeVolCalculator.to_radians(degree_ab)))

    @staticmethod
    def DeltaHS_BBS(h1, s1, v1, h2, s2, v2):
        h1_rad = PurgeVolCalculator.to_radians(h1)
        h2_rad = PurgeVolCalculator.to_radians(h2)

        dx = math.cos(h1_rad) * s1 * v1 - math.cos(h2_rad) * s2 * v2
        dy = math.sin(h1_rad) * s1 * v1 - math.sin(h2_rad) * s2 * v2
        dxy = math.sqrt(dx * dx + dy * dy)

        return min(1.2, dxy)

    @staticmethod
    def hex_to_rgb(hex_color):
        hex_color = hex_color.lstrip('#')
        if len(hex_color) == 3:
            hex_color = ''.join([c * 2 for c in hex_color])
        if len(hex_color) == 8:
            hex_color = hex_color[:6]
        if len(hex_color) != 6:
            raise ValueError("Invalid hex color code, it should be 3, 6 or 8 digits long")
        color_value = int(hex_color, 16)
        r = (color_value >> 16) & 0xFF
        g = (color_value >> 8) & 0xFF
        b = color_value & 0xFF
        return r, g, b

