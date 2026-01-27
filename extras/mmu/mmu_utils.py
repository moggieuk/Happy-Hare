# Happy Hare MMU Software
# Utility classes for Happy Hare MMU
#
# DebugStepperMovement
# Goal: Internal testing class for debugging synced movement
#
# PurgeVolCalculator
# Goal: Purge volume calculator based on color change
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import math


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
