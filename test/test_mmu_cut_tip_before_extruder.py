# Happy Hare test harness - cutter blade before the extruder.
#
# cutter_before_extruder in _MMU_CUT_TIP_VARS puts the blade before the extruder gears. The real
# _MMU_CUT_TIP then tensions the filament with the gear, rips by extruding the fragment off the
# blade with the gear unsynced and pushes the fragment past the gears. The pushback returns the
# filament end to the blade, so it has already left the extruder and only the bowden unload
# remains, shortened by the distance from the blade to the gears.
#
# The cut is then verified: a missed cut leaves the filament gripped by the extruder, so the
# extruder sensor stays covered or a gear-only pull can't move it, and the unload pauses.
#
# The harness accounts for G-code and manual motor travel. A CUT effect around the real macro
# body separately moves the filament end to where the blade leaves it (park_pos from the nozzle).
# Happy Hare measures physical extruder steps to calculate the retained fragment.
# A MISSED cut skips that move, so the filament stays continuous. The model can't hold filament
# against the gear either, so hold() makes the verification pull report that nothing moved, as a
# gear slipping against a gripped filament would. The modelled buffer is always coupled to the
# extruder, so free() releases its tension after the cut and makes the good-cut pull an ordinary
# move that can't reach tension. A fragment on the toolhead sensor is pressed.
#
# GEOMETRY. 3ms: extruder entry sensor 80mm from the nozzle (toolhead_extruder_to_nozzle 72 +
# toolhead_entry_to_extruder 8), no toolhead sensor, encoder or bowden. ercf_vvd (entry sensor
# disabled): toolhead sensor 75.8mm from the nozzle, gears at 85.1mm, an encoder and a bowden.
# boxturtle_test has only a sync-feedback buffer and tradrack has nothing to verify the cut with.
#
#   ./venv/bin/python -m unittest test.test_mmu_cut_tip_before_extruder
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest import mock

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_END_BOWDEN = 4
FILAMENT_POS_HOMED_ENTRY = 5
FILAMENT_POS_IN_EXTRUDER = 9
FILAMENT_POS_LOADED = 10

CUT_MACROS = ('_MMU_CUT_TIP', '_CUT_TIP_MOVE_TO_CUTTER_PIN', '_CUT_TIP_DO_CUT_MOTION',
              '_CUT_TIP_GANTRY_SERVO_DOWN', '_CUT_TIP_GANTRY_SERVO_UP')
NOT_CUT = 'Tip may not have been cut'


class BeforeExtruderCutCase(unittest.TestCase):
    PROFILE = '3ms'
    TIP_AT_GATE = -40.0
    BLADE_POS = 76.0                # Between the gears (72) and the entry sensor (80)
    RETRACT_LENGTH = 40.0
    PUSHBACK_LENGTH = 20.0
    DISABLE_SENSORS = ()

    def setUp(self):
        self.hh = session(profiles.get(self.PROFILE).derive(self.PROFILE + '_cutter_before_extruder', syms={
            'MMU_HAS_TOOLHEAD_CUTTER': True,
            'BOOL_CUT_TIP_CUTTER_BEFORE_EXTRUDER': True,
            'VAR_CUT_TIP_BLADE_POS': self.BLADE_POS,
            'VAR_CUT_TIP_RETRACT_LENGTH': self.RETRACT_LENGTH,
            'VAR_CUT_TIP_PUSHBACK_LENGTH': self.PUSHBACK_LENGTH,
            'VAR_CUT_TIP_RIP_LENGTH': 1.0,
        }))
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.boot_errors = self.hh.errors
        self.hh.gcode.errors.clear()
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit(0)
        self.extruder = self.unit.extruder_wrapper
        self.fil = self.hh.filament()
        self.missed = False
        self.held = None
        self.freed = False
        effects = self.hh.printer.harness_macro_effects
        for name in CUT_MACROS:
            effects[name] = lambda macro, gcmd: macro.run_body(gcmd)

        def cut(macro, gcmd):
            macro.run_body(gcmd)
            self.cut_moves = len(self.moves)
            if not self.missed:
                gate = self.mmu.gate_selected
                self.fil.advance(gate, self.nozzle - macro.variables['output_park_pos'] - self.fil.tip[gate], 'cut')
            if self.freed:
                self.hh.sensor('filament_tension').set(False) # The cut releases the tension step

        effects['_MMU_CUT_TIP'] = cut
        self.hh.place_filament(0, position=self.TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [], 'load was not clean')
        # _MMU_PARK doesn't run, so nothing is retracted and a load stops residual_filament short of the nozzle
        self.nozzle = self.fil.tip[0] + self.unit.toolhead_wrapper.p.toolhead_residual_filament
        # The macro retracts the end to retract_length from the nozzle before the cut
        self.fragment = self.BLADE_POS - self.RETRACT_LENGTH
        for sensor in self.DISABLE_SENSORS:
            self.hh.run_gcode('MMU_SENSORS SENSOR=%s ENABLE=0' % sensor)
        self.moves = []
        move_filament = self.mmu.move_filament

        def record(trace, dist, *args, **kwargs):
            if self.held is not None and trace and trace.startswith('Verifying cut'):
                result = (dist, self.held, 0., abs(dist)) # Gear slips, the filament doesn't move
            elif self.freed and trace and trace.startswith('Verifying cut'):
                kwargs = dict(kwargs, homing_move=0, endstop_name='default')
                result = move_filament(trace, dist, *args, **kwargs)
            else:
                result = move_filament(trace, dist, *args, **kwargs)
            if trace:
                self.moves.append((trace, round(dist, 2), kwargs.get('motor')))
            return result

        self.mmu.move_filament = record
        self.warnings = []
        log_warning = self.mmu.log_warning
        patcher = mock.patch.object(self.mmu, 'log_warning',
                                    side_effect=lambda msg, *a, **k: (self.warnings.append(msg), log_warning(msg, *a, **k)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def hold(self, homed=False):
        """A missed cut: the extruder still grips the filament so the verification pull can't move it"""
        self.missed = True
        self.held = homed

    def free(self):
        """A good cut: the free filament end can't put the buffer into tension"""
        self.freed = True

    def unload(self):
        start = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_UNLOAD')
        return self.hh.gcode.executed[start:]

    def traces(self):
        return [m[0] for m in self.moves]

    def load_length(self):
        return [m[1] for m in self.moves if m[0] == 'Loading filament to nozzle']

    def assert_paused_in_extruder(self):
        self.assertTrue(any(NOT_CUT in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_IN_EXTRUDER)
        self.assertEqual(self.extruder.filament_remaining, 0.) # Still continuous, so no fragment
        self.assertNotIn('Fast unloading move through bowden', self.traces())
        self.assertNotIn('Final parking', self.traces())

    def assert_unloaded(self):
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        # Nothing moves the extruder once the cut has finished
        self.assertNotIn('extruder', [m[2] for m in self.moves[self.cut_moves:]])
        self.assertNotIn('gear+extruder', [m[2] for m in self.moves[self.cut_moves:]])


class TestCutBetweenGearsAndEntrySensor(BeforeExtruderCutCase):

    def test_cut_tensions_on_the_gear_rips_on_the_extruder_and_pushes_back_with_both(self):
        executed = self.unload()
        self.assertEqual(self.hh.errors, [])
        cut = [line.split('#')[0].strip() for line in executed[executed.index('_MMU_CUT_TIP'):]]
        steps = [line for line in cut if line.startswith(('_MMU_STEP_MOVE', '_CUT_TIP_DO_CUT_MOTION'))]
        self.assertEqual(steps, [
            '_MMU_STEP_MOVE MOVE=-10.0 SPEED=25.0 MOTOR=gear',                    # Tension
            '_CUT_TIP_DO_CUT_MOTION PIN_PARK_X_LOC=19.0 PIN_PARK_Y_LOC=250.0',
            '_MMU_STEP_MOVE MOVE=1.0 SPEED=3.0 MOTOR=extruder ALLOW_BYPASS=1',    # Rip pushes the fragment
            '_MMU_STEP_MOVE MOVE=20.0 SPEED=25.0 MOTOR=gear+extruder',            # Pushback
            '_MMU_STEP_MOVE MOVE=-20.0 SPEED=25.0 MOTOR=gear+extruder',
        ])
        self.assertNotIn('G1 E-1.0 F180.0', cut) # A retract would pull the fragment into the blade
        self.assertIn('SET_GCODE_VARIABLE MACRO=_MMU_CUT_TIP VARIABLE=output_park_pos VALUE=76.0', cut)

    def test_cut_is_verified_off_the_extruder_sensor(self):
        positions = []
        unload_gate = self.mmu._unload_gate
        self.mmu._unload_gate = lambda *a, **k: (positions.append(self.mmu.filament_pos), unload_gate(*a, **k))[1]
        self.unload()
        self.assert_unloaded()
        # 80 - 76 + toolhead_unload_safety_margin
        self.assertIn(('Verifying cut off extruder sensor', -14.0, 'gear'), self.moves)
        self.assertEqual(positions, [FILAMENT_POS_HOMED_ENTRY])
        self.assertAlmostEqual(self.extruder.filament_remaining, self.fragment, places=1)
        self.assertAlmostEqual(self.fil.tip[0], self.TIP_AT_GATE, places=2)

    def test_missed_cut_pauses_with_the_filament_in_the_extruder(self):
        self.hold()
        self.unload()
        self.assert_paused_in_extruder()

    def test_next_load_pushes_the_fragment_through(self):
        self.unload()
        self.moves.clear()
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])
        # toolhead_extruder_to_nozzle - fragment - residual - ooze - retracted_length
        self.assertAlmostEqual(self.load_length()[0], 72. - self.fragment - 2. - 0. - 0., places=1)
        self.assertEqual(self.extruder.filament_remaining, 0.)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_test_form_tip_records_the_fragment(self):
        self.mmu.gate_color[self.mmu.gate_selected] = 'ff0000'
        self.hh.run_gcode('MMU_TEST_FORM_TIP')
        self.assertAlmostEqual(self.extruder.filament_remaining, self.fragment, places=1)
        self.assertEqual(self.extruder.filament_remaining_color, 'ff0000')
        self.assertIn(('Ejecting filament for inspection', -80.0, 'gear'), self.moves)

    def test_blade_position_calibration_is_refused(self):
        self.unload()
        with self.assertRaisesRegex(self.hh.gcode.error, 'measure blade_pos by hand'):
            self.hh.run_gcode('MMU_CALIBRATE_TOOLHEAD CUT=1')

    def test_showconfig_describes_the_cut_not_the_skipped_unload_test(self):
        self.mmu.p.toolhead_unload_test = 2
        del self.hh.console[:]
        self.hh.run_gcode('MMU_STATUS SHOWCONFIG=1')
        shown = '\n'.join(str(msg) for msg in self.hh.console)
        self.assertIn('cut BEFORE the extruder', shown)
        self.assertIn('verified with the EXTRUDER sensor', shown)
        self.assertNotIn('Unload is tested', shown)

    def test_without_the_variable_a_blade_past_the_gears_is_after_the_extruder(self):
        vars = self.hh.printer.lookup_object('gcode_macro _MMU_CUT_TIP_VARS')
        self.assertTrue(self.mmu.cutter_before_extruder())
        vars.variables = {k: v for k, v in vars.variables.items() if k != 'cutter_before_extruder'}
        self.assertFalse(self.mmu.cutter_before_extruder())


class TestCutBeforeEntrySensor(BeforeExtruderCutCase):
    BLADE_POS = 85.0                # Before the entry sensor (80), as on the Smart Orbiter 3

    def test_clear_extruder_sensor_verifies_the_cut(self):
        self.unload()
        self.assert_unloaded()
        self.assertFalse(any(t.startswith('Verifying cut') for t in self.traces()))
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_covered_extruder_sensor_pauses(self):
        self.missed = True
        self.unload()
        self.assert_paused_in_extruder()

    def test_extruder_only_cut_on_runout_pauses_though_the_sensor_is_clear(self):
        # The clear sensor proves the cut, not that the fragment cleared the gears
        self.mmu.is_handling_runout = True
        self.addCleanup(setattr, self.mmu, 'is_handling_runout', False)
        self.hh.run_gcode('MMU_UNLOAD EXTRUDER_ONLY=1')
        self.assertTrue(any("cut fragment may be left in the extruder gears" in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_END_BOWDEN)


class TestCutPushbackTooShort(BeforeExtruderCutCase):
    BLADE_POS = 85.0
    PUSHBACK_LENGTH = 10.0          # Needs >= 85 - 72 + 3 = 16

    def test_reported_at_bootup(self):
        self.assertTrue(any("pushback 10.0mm can't clear the extruder: need >= 16.0mm" in e
                            for e in self.boot_errors), self.boot_errors)

    def test_cut_is_refused_before_any_blade_motion(self):
        executed = self.unload()
        self.assertTrue(any("can't clear the extruder" in e for e in self.hh.errors), self.hh.errors)
        self.assertFalse(any(line.startswith('_CUT_TIP_DO_CUT_MOTION') for line in executed))
        self.assertFalse(any(line.startswith('_MMU_STEP_MOVE') for line in executed))
        self.assertEqual(self.extruder.filament_remaining, 0.)


class TestCutterSetBeforeExtruderWithBladeAfterGears(BeforeExtruderCutCase):
    BLADE_POS = 60.0                # Gears at 72

    def test_reported_at_bootup(self):
        self.assertTrue(any("blade_pos 60.0mm isn't greater than toolhead_extruder_to_nozzle 72.0mm" in e
                            for e in self.boot_errors), self.boot_errors)


class TestCutPushbackShortenedByRecordedFragment(BeforeExtruderCutCase):
    BLADE_POS = 85.0
    RETRACT_LENGTH = 40.0           # 36mm after residual (2) and park retraction (2), needs >= 16

    def test_cut_is_refused_before_any_blade_motion(self):
        self.assertEqual(self.boot_errors, [])
        # The macro retracts only what the fragment, residual and the load's final retraction don't
        # already cover: 40 - (34 + 2 + 2) = 2
        self.extruder.set_filament_remaining(34.)
        executed = self.unload()
        self.assertTrue(any("pushback 2.0mm can't clear the extruder" in e for e in self.hh.errors), self.hh.errors)
        self.assertFalse(any(line.startswith('_CUT_TIP_DO_CUT_MOTION') for line in executed))


class TestCutRetractTooShort(BeforeExtruderCutCase):
    BLADE_POS = 85.0
    RETRACT_LENGTH = 18.0           # Only 14mm after residual (2) and park retraction (2), needs >= 16

    def test_reported_at_bootup(self):
        self.assertTrue(any("pushback 14.0mm can't clear the extruder: need >= 16.0mm" in e
                            for e in self.boot_errors), self.boot_errors)


class TestCutVerifiedByEncoder(BeforeExtruderCutCase):
    PROFILE = 'ercf_vvd'
    TIP_AT_GATE = -100.0
    BLADE_POS = 100.0               # Gears at 85.1
    PUSHBACK_LENGTH = 30.0
    DISABLE_SENSORS = ('extruder', 'toolhead')

    def test_gear_pull_moves_the_filament(self):
        self.unload()
        self.assert_unloaded()
        self.assertIn(('Verifying cut with encoder', -15.0, 'gear'), self.moves)
        # Bowden move shortened by the filament end being 100 - 85.1mm past the gears plus the 15mm pull
        self.assertIn(('Fast unloading move through bowden', -630.1, None), self.moves)

    def test_missed_cut_pauses_with_the_filament_in_the_extruder(self):
        self.hold()
        self.unload()
        self.assert_paused_in_extruder()

    def test_extruder_only_unload_doesnt_pull_with_the_gear(self):
        start = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_UNLOAD EXTRUDER_ONLY=1')
        self.assertFalse([m for m in self.moves if m[0].startswith('Verifying cut')], self.moves)
        self.assertTrue(any("couldn't follow the pushback" in w for w in self.warnings), self.warnings)
        # A step move would re-sync the gear for the rest of the macro
        executed = self.hh.gcode.executed[start:]
        self.assertFalse([line for line in executed if line.startswith('_MMU_STEP_MOVE')], executed)
        self.assertIn('G1 E1.0 F180.0', [line.split('#')[0].strip() for line in executed])

    def test_extruder_only_cut_on_runout_pauses_for_the_fragment(self):
        self.mmu.is_handling_runout = True
        self.addCleanup(setattr, self.mmu, 'is_handling_runout', False)
        self.hh.run_gcode('MMU_UNLOAD EXTRUDER_ONLY=1')
        self.assertTrue(any("cut fragment may be left in the extruder gears" in e for e in self.hh.errors), self.hh.errors)
        self.assertNotIn('Fast unloading move through bowden', self.traces())


class TestCutVerifiedByBuffer(BeforeExtruderCutCase):
    PROFILE = 'boxturtle_test'
    TIP_AT_GATE = -100.0
    BLADE_POS = 100.0
    RETRACT_LENGTH = 60.0
    PUSHBACK_LENGTH = 40.0

    def test_gear_pull_leaves_the_buffer_free(self):
        self.free()
        self.unload()
        self.assert_unloaded()
        self.assertIn('Verifying cut with sync-feedback buffer', self.traces())

    def test_buffer_going_into_tension_pauses(self):
        self.hold(homed=True)
        self.unload()
        self.assert_paused_in_extruder()


class TestCutCannotBeVerified(BeforeExtruderCutCase):
    PROFILE = 'tradrack'
    TIP_AT_GATE = -100.0
    BLADE_POS = 100.0
    RETRACT_LENGTH = 60.0
    PUSHBACK_LENGTH = 40.0

    def test_unload_continues(self):
        debug = []
        log_debug = self.mmu.log_debug
        with mock.patch.object(self.mmu, 'log_debug',
                               side_effect=lambda msg, *a, **k: (debug.append(msg), log_debug(msg, *a, **k))):
            self.unload()
        self.assert_unloaded()
        self.assertFalse(any(t.startswith('Verifying cut') for t in self.traces()))
        self.assertTrue(any("can't be verified" in d for d in debug))


class TestCutFragmentLeftOnToolheadSensor(BeforeExtruderCutCase):
    PROFILE = 'ercf_vvd'
    TIP_AT_GATE = -100.0
    BLADE_POS = 100.0
    PUSHBACK_LENGTH = 20.0          # Fragment [20, 80] still across the toolhead sensor at 75.8
    DISABLE_SENSORS = ('extruder',)

    # KNOWN ISSUE: the pushback is assumed to clear the toolhead sensor. If it doesn't, the
    # fragment trips the "Possible toolhead sensor malfunction" check on the next load
    @unittest.expectedFailure
    def test_next_load_with_the_fragment_on_the_toolhead_sensor(self):
        self.unload()
        self.assertEqual(self.hh.errors, [])
        # The model has no fragment, so press the sensor as the extruder load starts
        load_extruder = self.mmu._load_extruder
        self.mmu._load_extruder = lambda *a, **k: (self.hh.sensor('toolhead').set(True), load_extruder(*a, **k))[1]
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])


class TestCutClearOfToolheadSensor(BeforeExtruderCutCase):
    PROFILE = 'ercf_vvd'
    TIP_AT_GATE = -100.0
    BLADE_POS = 100.0               # Gears at 85.1, toolhead sensor at 75.8
    PUSHBACK_LENGTH = 30.0          # Fragment [10, 70] clears the toolhead sensor
    DISABLE_SENSORS = ('extruder',)

    def test_toolhead_sensor_homes_the_next_load(self):
        self.unload()
        self.assert_unloaded()
        self.assertTrue(self.mmu.sensor_manager.has_sensor('toolhead'))
        self.assertAlmostEqual(self.fil.tip[0], -3.07, delta=0.2) # Where a cut after the extruder parks
        self.moves.clear()
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertIn('Homing to toolhead sensor', self.traces())
        # toolhead_sensor_to_nozzle - fragment - residual - ooze - retracted_length
        self.assertAlmostEqual(self.load_length()[0], 75.8 - self.fragment - 2. - 0. - 0., places=1)


class TestCutAlwaysGripped(BeforeExtruderCutCase):
    PROFILE = 'qidi'
    TIP_AT_GATE = -100.0
    DISABLE_SENSORS = ('extruder',) # Its simulated layout doesn't match the Q2 extruder sensor position

    def test_unload_and_reload(self):
        self.assertTrue(self.unit.filament_always_gripped)
        self.free()
        self.unload()
        self.assert_unloaded()
        self.assertIn('Verifying cut with sync-feedback buffer', self.traces())
        self.assertAlmostEqual(self.extruder.filament_remaining, self.fragment, places=1)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)


del BeforeExtruderCutCase


if __name__ == '__main__':
    unittest.main()
