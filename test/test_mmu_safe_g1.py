# Happy Hare test harness - _SAFE_G1, the range-safe move used by the parking macros.
#
# Klipper adds the gcode offset, then applies skew_correction (skewed_y = y - z*yz),
# then range-checks the result. With YZ skew a park at the Y limit, or even a Z-only
# lift near it, ends past the limit:
#     "Move out of range: 47.893 357.271 68.092"   (Y max 356, yz ~ -0.0187)
# _SAFE_G1 computes the same mapping and pulls the target just inside the limit.
#
#   ./venv/bin/python -m unittest test.test_mmu_safe_g1
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import profiles, session
from test.hh.bootstrap import PRINTER_STUB

logging.getLogger().setLevel(logging.CRITICAL)

Y_MAX = 356.
YZ = (Y_MAX - 357.271) / 68.092     # The skew implied by the reported error
STUB = PRINTER_STUB + "\n[skew_correction]\n"


class SafeG1TestCase(unittest.TestCase):

    def setUp(self):
        self.hh = session(profiles.get('boxturtle').derive('cutter_safe_g1',
                          syms={'MMU_HAS_TOOLHEAD_CUTTER': True}), printer_stub=STUB)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.kin = self.toolhead.get_kinematics()
        self.kin.set_limits((0., 0., 0.), (350., Y_MAX, 340.))
        self.skew = self.hh.printer.lookup_object('skew_correction')
        self.hh.run_gcode("G90")

    def tearDown(self):
        self.hh.close()

    def kin_pos(self):
        return self.toolhead.get_position()[:3]

    def warnings(self):
        return [m for m in self.hh.console if 'adjusted to' in m]

    def test_reported_error_reproduces_with_plain_g1(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G1 X47.893 Y300 Z68.092")
        with self.assertRaises(self.hh.printer.command_error) as cm:
            self.hh.run_gcode("G1 Y356")
        self.assertIn("357.271", str(cm.exception))

    def test_park_at_limit_is_pulled_inside(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G1 X47.893 Y300 Z68.092")
        self.hh.run_gcode("_SAFE_G1 Y=356 F=12000")
        x, y, z = self.kin_pos()
        self.assertAlmostEqual(y, Y_MAX - 0.01, places=6)
        self.assertAlmostEqual(z, 68.092, places=6)
        self.assertEqual(len(self.warnings()), 1)

    def test_z_only_lift_clamps_y(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G1 X100 Y355.5 Z0.2")
        with self.assertRaises(self.hh.printer.command_error):
            self.hh.run_gcode("G1 Z40")
        self.hh.run_gcode("_SAFE_G1 Z=40")
        x, y, z = self.kin_pos()
        self.assertAlmostEqual(y, Y_MAX - 0.01, places=6)
        self.assertAlmostEqual(z, 40., places=6)
        self.assertAlmostEqual(x, 100., places=6)

    def test_in_range_move_is_issued_verbatim(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("_SAFE_G1 X=10 Y=20 Z=5 F=6000")
        self.assertEqual(self.hh.gcode.executed[-1], "G1 X10 Y20 Z5 F6000")
        self.assertEqual(self.warnings(), [])

    def test_gcode_offset_alone(self):
        self.hh.run_gcode("SET_GCODE_OFFSET Y=2")
        self.hh.run_gcode("_SAFE_G1 X=10 Y=356")
        self.assertAlmostEqual(self.kin_pos()[1], Y_MAX - 0.01, places=6)
        gpos = self.hh.printer.lookup_object('gcode_move').get_status()['gcode_position']
        self.assertAlmostEqual(gpos.y, Y_MAX - 0.01 - 2., places=6)

    def test_below_minimum(self):
        self.skew.set_factors(xy=0.01)
        self.hh.run_gcode("_SAFE_G1 X=0 Y=200")
        self.assertAlmostEqual(self.kin_pos()[0], 0.01, places=6)

    def test_warns_once_per_print(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G1 X100 Y300 Z50")
        for _ in range(3):
            self.hh.run_gcode("_SAFE_G1 Y=356")
            self.hh.run_gcode("_SAFE_G1 Y=300")
        self.assertEqual(len(self.warnings()), 1)
        self.hh.mmu.safe_g1.reset_print_state()
        self.hh.run_gcode("_SAFE_G1 Y=356")
        self.assertEqual(len(self.warnings()), 2)

    def test_relative_mode_preserved(self):
        self.hh.run_gcode("G91")
        self.hh.run_gcode("_SAFE_G1 X=20 Y=30")
        self.assertEqual(self.kin_pos()[:2], [20., 30.])
        self.assertFalse(self.hh.printer.lookup_object('gcode_move').absolute_coord)

    def test_axis_resting_on_limit_is_left_alone(self):
        # (255.307 - -0.7) + -0.7 rounds to just above 255.307, so rebuilding the position from
        # the gcode position would see Y over the limit even though Y isn't moving
        self.kin.set_limits((0., 0., 0.), (350., 255.307, 340.))
        self.hh.run_gcode("SET_GCODE_OFFSET Y=-0.7")
        gm = self.hh.printer.lookup_object('gcode_move')
        gm.last_position[1] = 255.307
        self.toolhead.commanded_pos[1] = 255.307
        self.hh.run_gcode("_SAFE_G1 X=10 F=6000")
        self.assertEqual(self.hh.gcode.executed[-1], "G1 X10 F6000")
        self.assertEqual(self.warnings(), [])

    def test_skew_rounding_at_limit(self):
        # After homing Klipper resets its gcode position from the toolhead through the inverse
        # skew. That round trip isn't exact, so moving to where the toolhead already is puts X
        # at 350.00000000000006, which Klipper sees as X moving past its 350 limit
        self.skew.set_factors(xy=0.001, yz=-0.0187)
        gm = self.hh.printer.lookup_object('gcode_move')
        self.toolhead.commanded_pos[:3] = [350., 100., 10.]
        gm.last_position[:3] = self.skew.calc_unskew([350., 100., 10., 0.])[:3]
        here = "X=%r Y=%r" % tuple(gm.last_position[:2])
        with self.assertRaisesRegex(self.hh.printer.command_error, "Move out of range: 350.000"):
            self.hh.run_gcode("G1 " + here.replace("=", ""))
        self.hh.run_gcode("_SAFE_G1 " + here)
        self.assertLessEqual(self.kin_pos()[0], 350.)
        self.assertGreater(self.kin_pos()[0], 349.98)

    def test_max_z_keeps_later_relative_z_moves_in_range(self):
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G1 X100 Y300 Z0.2")
        self.hh.run_gcode("_SAFE_G1 Y=355.9 MAX_Z=20.2")
        self.hh.run_gcode("G91\nG1 Z20\nG90")      # e.g. Blobifier raising Z while purging
        self.assertAlmostEqual(self.kin_pos()[1], Y_MAX - 0.01, places=6)
        self.assertAlmostEqual(self.kin_pos()[2], 20.2, places=6)

    def test_adjustment_limit_includes_margin(self):
        # 4.99mm overshoot + 0.01mm inset is exactly the allowed 5mm.
        self.hh.run_gcode("_SAFE_G1 Y=360.99")
        self.assertAlmostEqual(self.kin_pos()[1], 355.99)

    def test_adjustment_boundary_ignores_float_noise(self):
        self.kin.set_limits((0., 0., 0.), (350., 0.03, 340.))
        self.hh.run_gcode("_SAFE_G1 Y=5.0200000000000005")
        self.assertAlmostEqual(self.kin_pos()[1], 0.02)
        with self.assertRaisesRegex(self.hh.gcode.error, "Y adjustment"):
            self.hh.run_gcode("_SAFE_G1 Y=5.020001")

    def test_cutter_restores_current_after_errors_and_success(self):
        # Run the actual top-level macro and current helpers, including their saved map.
        for name in ('_MMU_CUT_TIP', '_CUT_TIP_ADJUST_CURRENT', '_CUT_TIP_RESTORE_CURRENT',
                     '_CUT_TIP_MOVE_TO_CUTTER_PIN', '_CUT_TIP_DO_CUT_MOTION'):
            self.hh.printer.harness_macro_effects[name] = lambda macro, gcmd: macro.run_body(gcmd)
        current = {'run_current': 0.8}
        driver = type('Driver', (), {'get_status': lambda _, eventtime=None: dict(current)})()
        self.hh.printer.add_object('tmc2209 stepper_x', driver)
        history = []
        def set_current(gcmd):
            current['run_current'] = gcmd.get_float('CURRENT')
            history.append(current['run_current'])
        self.hh.gcode.register_command('SET_TMC_CURRENT', None)
        self.hh.gcode.register_command('SET_TMC_CURRENT', set_current)
        vars = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        vars.variables = dict(vars.variables, cut_axis_steppers='stepper_x',
                              cut_stepper_current=150, retract_length=0., pushback_length=0.,
                              gantry_servo_enabled=True, cutting_axis='x', pin_park_dist=0.,
                              pin_loc_xy=[110., 100.], pin_loc_compressed_xy=[100., 100.],
                              rip_length=0., restore_position=False)
        for failure in ('approach', 'stroke', 'plain_g1', None):
            with self.subTest(failure=failure):
                self.hh.run_gcode("G90\nG1 X120 Y100 Z10")
                vars.variables['pin_loc_xy'] = [110., 362. if failure == 'approach' else 100.]
                vars.variables['pin_loc_compressed_xy'] = [100., 362. if failure == 'stroke' else 100.]
                self.hh.printer.harness_macro_effects['_CUT_TIP_DO_CUT_MOTION'] = (
                    (lambda macro, gcmd: self.hh.run_gcode("G1 Y400")) if failure == 'plain_g1'
                    else (lambda macro, gcmd: macro.run_body(gcmd)))
                if failure:
                    error = self.hh.printer.command_error if failure == 'plain_g1' else self.hh.gcode.error
                    with self.assertRaisesRegex(error, 'Move out of range' if failure == 'plain_g1' else 'adjustment'):
                        self.hh.run_gcode('_MMU_CUT_TIP')
                else:
                    self.hh.run_gcode('_MMU_CUT_TIP')
                self.assertAlmostEqual(current['run_current'], 0.8)
                self.assertAlmostEqual(history[-2], 1.2)
                saved = self.hh.printer.lookup_object('gcode_macro _CUT_TIP_ADJUST_CURRENT')
                self.assertEqual(saved.variables['current_map'], {})

    def test_excessive_adjustments_fail_without_moving(self):
        self.hh.run_gcode("G1 X100 Y100 Z10")
        self.hh.run_gcode("G91")
        for command in ("X=-5", "X=355", "Y=361", "Z=345"):
            with self.subTest(command=command):
                before = len(self.toolhead.moves)
                with self.assertRaisesRegex(self.hh.gcode.error, "adjustment.*exceeds.*5"):
                    self.hh.run_gcode("_SAFE_G1 " + command)
                self.assertEqual(len(self.toolhead.moves), before)
                self.assertEqual(self.kin_pos(), [100., 100., 10.])
                self.assertFalse(self.hh.mmu.gcode_move.absolute_coord)
                self.assertFalse(self.hh.mmu.safe_g1.warned)

    def test_excessive_implicit_axis_adjustment_fails(self):
        self.skew.set_factors(yz=-0.1)
        self.hh.run_gcode("G1 X100 Y355 Z0")
        with self.assertRaisesRegex(self.hh.gcode.error, "Y adjustment"):
            self.hh.run_gcode("_SAFE_G1 Z=100")
        self.assertEqual(self.kin_pos(), [100., 355., 0.])

    def test_excessive_max_z_adjustment_fails(self):
        self.skew.set_factors(yz=-0.1)
        with self.assertRaisesRegex(self.hh.gcode.error, "Y adjustment"):
            self.hh.run_gcode("_SAFE_G1 Y=356 MAX_Z=100")

    def test_excessive_offset_adjustment_fails(self):
        self.hh.run_gcode("SET_GCODE_OFFSET Y=6")
        with self.assertRaisesRegex(self.hh.gcode.error, "Y adjustment"):
            self.hh.run_gcode("_SAFE_G1 Y=356")

    def test_cutter_approach_and_stroke_clamp_small_skew_overshoot(self):
        for name in ('_CUT_TIP_MOVE_TO_CUTTER_PIN', '_CUT_TIP_DO_CUT_MOTION'):
            self.hh.printer.harness_macro_effects[name] = lambda macro, gcmd: macro.run_body(gcmd)
        vars = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        vars.variables = dict(vars.variables, gantry_servo_enabled=True,
                              pin_loc_compressed_xy=[100., 356.], cut_iterations=1,
                              cut_fast_move_fraction=0.5, rip_length=0.)
        self.skew.set_factors(yz=-0.01)
        self.hh.run_gcode("G1 X120 Y300 Z10")
        self.hh.run_gcode("_CUT_TIP_MOVE_TO_CUTTER_PIN PIN_PARK_X_LOC=110 PIN_PARK_Y_LOC=356")
        first = len(self.toolhead.moves)
        self.hh.run_gcode("_CUT_TIP_DO_CUT_MOTION PIN_PARK_X_LOC=110 PIN_PARK_Y_LOC=356")
        moves = [pos[:3] for pos, _ in self.toolhead.moves[first:]]
        self.assertEqual([p[0] for p in moves], [105., 100., 110.])
        for pos in moves:
            self.assertAlmostEqual(pos[1], 355.99)
        self.assertEqual(self.hh.errors, [])

    def test_unhomed_passes_through(self):
        self.kin.limits = [(1., -1.)] * 3
        with self.assertRaisesRegex(self.hh.printer.command_error, "Must home axis first"):
            self.hh.run_gcode("_SAFE_G1 X=10 Y=20")
        self.assertEqual(self.hh.gcode.executed[-1], "G1 X10 Y20")


class SafeG1NoLimitsTestCase(unittest.TestCase):
    """Kinematics without box limits (delta, polar...) are left to Klipper"""

    def test_passthrough(self):
        hh = session('boxturtle')
        try:
            hh.boot()
            hh.run_gcode("G90")
            hh.run_gcode("_SAFE_G1 X=500 Y=-20 F=3000")
            self.assertEqual(hh.gcode.executed[-1], "G1 X500 Y-20 F3000")
            self.assertEqual(hh.errors, [])
        finally:
            hh.close()


if __name__ == '__main__':
    unittest.main()


class ParkMacroTestCase(unittest.TestCase):
    """_MMU_PARK body run for real: ramp direction/length and range-safe park moves"""

    def setUp(self):
        self.hh = session('boxturtle', printer_stub=STUB)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        for gate in (0, 1):
            self.hh.place_filament(gate, position=-40.)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.settle(1.0)
        self.assertEqual(self.hh.errors, [], 'print start failed')
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.toolhead.get_kinematics().set_limits((0., 0., 0.), (350., Y_MAX, 340.))
        self.skew = self.hh.printer.lookup_object('skew_correction')
        self.hh.printer.harness_macro_effects['_MMU_PARK'] = lambda macro, gcmd: macro.run_body(gcmd)

    def tearDown(self):
        self.hh.close()

    def park(self, start, args):
        self.hh.run_gcode("G90\nG1 X%s Y%s Z%s" % start)
        first = len(self.toolhead.moves)
        self.hh.run_gcode("_MMU_PARK FORCE_PARK=1 " + args)
        self.assertEqual(self.hh.errors, [])
        return [pos[:3] for pos, _ in self.toolhead.moves[first:]]

    def assert_xyz(self, actual, expected):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, places=4)

    def test_ramp_heads_toward_bed_centre(self):
        moves = self.park((100, 350, 0.2), "X=-999 Y=-999 Z_HOP=5 RAMP=5")
        dx, dy = 175. - 100., 178. - 350.
        length = (dx * dx + dy * dy) ** 0.5
        self.assert_xyz(moves[0], (100. + 5 * dx / length, 350. + 5 * dy / length, 5.2))
        self.assert_xyz(moves[1], (100., 350., 5.2))

    def test_ramp_stops_at_centre(self):
        moves = self.park((170, 170, 0.2), "X=-999 Y=-999 Z_HOP=5 RAMP=100")
        self.assert_xyz(moves[0], (175., 178., 5.2))

    def test_ramp_from_centre_heads_to_min_corner(self):
        moves = self.park((175, 178, 0.2), "X=-999 Y=-999 Z_HOP=5 RAMP=5")
        length = (175. ** 2 + 178. ** 2) ** 0.5
        self.assert_xyz(moves[0], (175. - 5 * 175. / length, 178. - 5 * 178. / length, 5.2))

    def test_ramp_centre_accounts_for_gcode_offset(self):
        self.hh.run_gcode("SET_GCODE_OFFSET X=10")
        moves = self.park((165, 100, 0.2), "X=-999 Y=-999 Z_HOP=5 RAMP=200")
        self.assert_xyz(moves[0], (175., 178., 5.2))     # Machine centre, i.e. gcode X 165

    def test_restore_near_limit_has_no_sideways_move_at_print_height(self):
        # Printing 0.006mm inside Y max with a 20mm lift: the restore XY is out of range at the
        # lifted height so gets pulled in, and must be put back on the way down
        effects = self.hh.printer.harness_macro_effects
        for name in ('_MMU_SAVE_POSITION', '_MMU_RESTORE_POSITION'):
            effects[name] = lambda macro, gcmd: macro.run_body(gcmd)
        seq = self.hh.printer.lookup_object('gcode_macro _MMU_SEQUENCE_VARS')
        seq.variables = dict(seq.variables, park_toolchange=[-999, -999, 20, 0, 0], restore_xy_pos='last')
        self.skew.set_factors(yz=YZ)
        self.hh.run_gcode("G90\nG1 X100 Y355.99 Z0.2")
        printing_at = self.toolhead.get_position()[:3]
        first = len(self.toolhead.moves)
        self.hh.run_gcode("MMU_CHANGE_TOOL TOOL=1")
        self.hh.settle(0.5)
        self.assertEqual(self.hh.errors, [])
        moves = [pos[:3] for pos, _ in self.toolhead.moves[first:]]
        self.assertGreater(max(z for _, _, z in moves), 20.)
        back = next(i for i, m in enumerate(moves) if i > 0 and m[2] == printing_at[2] and moves[i - 1][2] > printing_at[2])
        for m in moves[back:]:
            self.assert_xyz(m, printing_at)

    def test_park_at_y_limit_with_skew_stays_in_range(self):
        self.skew.set_factors(yz=YZ)
        moves = self.park((100, 300, 63.092), "X=47.893 Y=356 Z_HOP=5")
        self.assertAlmostEqual(moves[-1][1], Y_MAX - 0.01, places=4)
        self.assertAlmostEqual(moves[-1][2], 68.092, places=4)


class BlobifierTestCase(unittest.TestCase):
    """Blobifier macros run for real: purge row at Y max, purge rises and shakes stay in range under skew"""

    PROFILE = profiles.get('boxturtle').derive('boxturtle_blobifier_skew', syms={
        'MMU_HAS_BLOBIFIER': True,
        'PIN_BLOBIFIER_SERVO': 'unit0:PA1',
    })

    def setUp(self):
        self.hh = session(self.PROFILE, printer_stub=STUB)
        self.hh.build()
        self.hh.printer.harness_macro_effects = {
            'BLOBIFIER_INIT': lambda macro, gcmd: setattr(
                macro.printer.lookup_object('gcode_macro BLOBIFIER'), 'variables',
                dict(macro.printer.lookup_object('gcode_macro BLOBIFIER').variables, initialized=1)),
            'BLOBIFIER_SHAKE_BUCKET': lambda macro, gcmd: macro.run_body(gcmd),
            'BLOBIFIER': lambda macro, gcmd: macro.run_body(gcmd),
            '__BLOBIFIER_EXTRUDER_MOVE': lambda macro, gcmd: macro.run_body(gcmd),
        }
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.toolhead = self.hh.printer.lookup_object('toolhead')
        self.toolhead.get_kinematics().set_limits((0., 0., 0.), (350., Y_MAX, 340.))
        self.hh.printer.lookup_object('skew_correction').set_factors(xy=-0.002, yz=YZ)
        configfile = self.hh.printer.lookup_object('configfile')
        configfile.status_settings = dict(configfile.status_settings, mmu_parameters={'log_m117_messages': 0})
        configfile.status_raw_config = dict(configfile.status_raw_config, extruder={'filament_diameter': '1.75'})
        fan = type('Fan', (), {'get_status': lambda self, eventtime=None: {'speed': 0.}})()
        self.hh.printer.add_object('fan', fan)
        self.hh.printer.add_object(self.hh.printer.lookup_object('gcode_macro _BLOBIFIER_VARS').variables['fan_name'], fan)

    def tearDown(self):
        self.hh.close()

    def test_shake_at_height_stays_in_range(self):
        self.hh.run_gcode("G90\nG1 X150 Y200 Z60")
        first = len(self.toolhead.moves)
        self.hh.run_gcode("BLOBIFIER_SHAKE_BUCKET SHAKES=3")
        moves = [pos[:3] for pos, _ in self.toolhead.moves[first:]]
        self.assertEqual(self.hh.errors, [])
        self.assertGreater(len(moves), 4)
        for x, y, z in moves:
            self.assertTrue(0. <= x <= 350. and 0. <= y <= Y_MAX, (x, y, z))

    def set_vars(self, macro, **values):
        obj = self.hh.printer.lookup_object('gcode_macro %s' % macro)
        obj.variables = dict(obj.variables, **values)

    def test_purge_without_tray_descent_stays_in_range_for_every_blob(self):
        # Can't descend to the tray so each blob's relative Z rise adds to the last
        self.set_vars('_BLOBIFIER_SAFE_DESCEND', tray=False, print_height=60.)
        self.set_vars('_BLOBIFIER_VARS', z_raise=10., purge_length_maximum=50., enable_shaker=False,
                      clean_macro='')
        self.hh.run_gcode("G90\nG1 X150 Y200 Z60")
        first = len(self.toolhead.moves)
        self.hh.run_gcode("BLOBIFIER PURGE_LENGTH=100")
        moves = [pos[:3] for pos, _ in self.toolhead.moves[first:]]
        self.assertEqual(self.hh.errors, [])
        self.assertGreater(max(z for _, _, z in moves), 79.)          # Both blobs rose
        for x, y, z in moves:
            self.assertTrue(0. <= x <= 350. and 0. <= y <= Y_MAX, (x, y, z))
