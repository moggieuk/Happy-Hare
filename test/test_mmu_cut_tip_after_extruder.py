# Happy Hare test harness - golden sequence for a cutter blade after the extruder gears.
#
# A blade between the extruder gears and the nozzle (blade_pos <= toolhead_extruder_to_nozzle)
# is the shipped behaviour. These tests pin the exact G-code the real _MMU_CUT_TIP emits and the
# filament moves of the unload and the following load, so support for other blade positions
# can't change it. 3ms reverse-homes off its extruder entry sensor; ercf_vvd (entry sensor
# disabled, see test_mmu_toolhead_unload_test) uses its toolhead sensor.
#
# G1 E moves transport filament while the extruder grips it, even with the gear unsynced. _MMU_PARK doesn't
# run, so the only retraction is the 2mm toolchange retraction the standalone load ends with: the
# macro retracts 2mm less; filament_remaining subtracts measured motor retraction, residual, and 2.
#
#   ./venv/bin/python -m unittest test.test_mmu_cut_tip_after_extruder
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10

CUT_MACROS = ('_MMU_CUT_TIP', '_CUT_TIP_MOVE_TO_CUTTER_PIN', '_CUT_TIP_DO_CUT_MOTION',
              '_CUT_TIP_GANTRY_SERVO_DOWN', '_CUT_TIP_GANTRY_SERVO_UP')

CUT_GCODE = [
    '_MMU_CUT_TIP',
    'SAVE_GCODE_STATE NAME=_MMU_CUT_TIP_state',
    'G90',
    'M83',
    'G92 E0',
    'MMU_LOG MSG="Retracting filament 28.5mm prior to cut"',
    'G1 E-28.5 F1500.0',
    'G1 E14.25 F1500.0',
    'G1 E-14.25 F1500.0',
    '_CUT_TIP_MOVE_TO_CUTTER_PIN PIN_PARK_X_LOC=19.0 PIN_PARK_Y_LOC=250.0',
    '_SAFE_G1 X=19.0 Y=250.0 F=9000.0',
    'G1 X19.0 Y250.0 F9000.0',
    '_SAFE_G1 X=19.0 Y=250.0 F=9000.0',
    'G1 X19.0 Y250.0 F9000.0',
    '_SAFE_G1 X=19.0 Y=250.0 F=9000.0',
    'G1 X19.0 Y250.0 F9000.0',
    '_CUT_TIP_GANTRY_SERVO_DOWN',
    '_CUT_TIP_DO_CUT_MOTION PIN_PARK_X_LOC=19.0 PIN_PARK_Y_LOC=250.0',
    '_SAFE_G1 X=0.5 Y=250.0 F=1920.0',
    'G1 X0.5 Y250.0 F1920.0',
    '_SAFE_G1 X=0.5 Y=250.0 F=480.0',
    'G1 X0.5 Y250.0 F480.0',
    '_MMU_TELEMETRY EVENT="cut"',
    'G4 P50.0',
    'G1 E-1.0 F180.0',
    '_SAFE_G1 X=19.0 Y=250.0 F=9000.0',
    'G1 X19.0 Y250.0 F9000.0',
    '_MMU_TELEMETRY EVENT="release"',
    '_SAFE_G1 X=49.0 F=9000.0',
    'G1 X49.0 F9000.0',
    '_CUT_TIP_GANTRY_SERVO_UP',
    '_MMU_EVENT EVENT="filament_cut"',
    'MMU_LOG MSG="Pushing filament fragment back 15.0mm after cut"',
    'G1 E15.0 F1500.0',
    'G4 P0',
    'G1 E-15.0 F1500.0',
    'SET_GCODE_VARIABLE MACRO=_MMU_CUT_TIP VARIABLE=output_park_pos VALUE=38.5',
    'RESTORE_GCODE_STATE NAME=_MMU_CUT_TIP_state MOVE=0 MOVE_SPEED=150.0',
]


class AfterExtruderGoldenCase(unittest.TestCase):
    PROFILE = None
    TIP_AT_GATE = None
    DISABLE_SENSORS = ()
    UNLOAD_MOVES = None
    UNLOAD_DRIVE_POS = None
    LOAD_MOVES = None

    def setUp(self):
        self.hh = session(profiles.get(self.PROFILE).derive(
            self.PROFILE + '_cutter', syms={'MMU_HAS_TOOLHEAD_CUTTER': True}))
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        for name in CUT_MACROS:
            self.hh.printer.harness_macro_effects[name] = lambda macro, gcmd: macro.run_body(gcmd)
        self.hh.place_filament(0, position=self.TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.heat_extruder(220)
        self.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [], 'load was not clean')
        for sensor in self.DISABLE_SENSORS:
            self.hh.run_gcode('MMU_SENSORS SENSOR=%s ENABLE=0' % sensor)
        self.moves = []
        move_filament = self.mmu.move_filament

        def record(trace, dist, *args, **kwargs):
            result = move_filament(trace, dist, *args, **kwargs)
            if trace:
                self.moves.append((trace, round(dist, 2), kwargs.get('motor'), kwargs.get('endstop_name')))
            return result

        self.mmu.move_filament = record

    def test_unload_then_load_is_unchanged(self):
        start = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.errors, [])
        executed = self.hh.gcode.executed[start:]
        first = executed.index('_MMU_CUT_TIP')
        self.assertEqual(executed[first:first + len(CUT_GCODE)], CUT_GCODE)
        self.assertEqual(self.moves, self.UNLOAD_MOVES)
        extruder = self.mmu.mmu_unit(0).extruder_wrapper
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertAlmostEqual(self.mmu.drive().get_filament_position(), self.UNLOAD_DRIVE_POS, places=2)
        self.assertAlmostEqual(extruder.filament_remaining, 5., places=2)

        self.moves.clear()
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.moves, self.LOAD_MOVES)
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.assertEqual(extruder.filament_remaining, 0.)


class TestAfterExtruderEntrySensor(AfterExtruderGoldenCase):
    PROFILE = '3ms'
    TIP_AT_GATE = -40.0
    UNLOAD_MOVES = [
        ('Reverse homing off extruder sensor', -86.0, 'gear+extruder', 'extruder'),
        ('Final parking', -250.0, None, None),
    ]
    UNLOAD_DRIVE_POS = -330.0
    LOAD_MOVES = [
        ('Initial homing to default:extruder sensor', 500.0, 'gear', 'default:extruder'),
        ('Homing filament to extruder endstop', 100.0, 'gear', 'extruder'),
        ('Aligning filament to extruder gear', 8.0, 'gear', None),
        ('Loading filament to nozzle', 65.0, 'gear+extruder', None),
    ]


class TestAfterExtruderToolheadSensor(AfterExtruderGoldenCase):
    PROFILE = 'ercf_vvd'
    TIP_AT_GATE = -100.0
    DISABLE_SENSORS = ('extruder',)
    UNLOAD_MOVES = [
        ('Reverse homing off toolhead sensor', -81.8, 'extruder', 'toolhead'),
        ('Unloading extruder', -19.3, 'extruder', None),
        ('Fast unloading move through bowden', -660.0, None, None),
        ('Unloading step #1 from encoder', -15.0, None, None),
        ('Unloading step #2 from encoder', -15.0, None, None),
        ('Unloading step #3 from encoder', -15.0, None, None),
        ('Final parking', -18.77, None, None),
    ]
    UNLOAD_DRIVE_POS = -808.87
    LOAD_MOVES = [
        ('Initial load into encoder', 100.0, None, None),
        # The real 1mm unsynced rip now contributes one 0.98mm encoder pulse.
        ('Fast loading move through bowden', 603.64, None, None),
        ('Homing to toolhead sensor', 60.0, 'gear+extruder', 'toolhead'),
        ('Loading filament to nozzle', 68.8, 'gear+extruder', None),
    ]


del AfterExtruderGoldenCase


if __name__ == '__main__':
    unittest.main()
