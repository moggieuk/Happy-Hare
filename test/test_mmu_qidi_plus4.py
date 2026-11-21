# Happy Hare test harness - QIDI Box on a QIDI Plus4.
#
# The qidi_plus4 profile is the Plus4 toolhead preset at its defaults: the extruder-entry
# sensor is the analog hall-effect filament width sensor (MmuHallEndstop over two ADC pins)
# and the cutter blade sits before the extruder gears. The harness drives the hall sensor
# from the filament model by feeding both ADCs readings on either side of hall_min_diameter
# (see _SensorHandle in test/hh/bootstrap.py).
#
# The preset's dimensions are placeholders until a commissioned Plus4 is measured, so these
# tests check what the machine does with whatever values it has, never the values themselves.
#
#   ./venv/bin/python -m unittest test.test_mmu_qidi_plus4
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import sys
import unittest
from unittest import mock

from test.hh import profiles, session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0


class Plus4Case(unittest.TestCase):

    def setUp(self):
        self.hh = session('qidi_plus4')
        self.addCleanup(self.hh.close)
        self.hh.boot(calibrate=True)
        self.assertEqual(self.hh.errors, [])
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit(0)
        self.fil = self.hh.filament()
        self.hall = self.hh.sensor('extruder')
        self.moves = []
        move_filament = self.mmu.move_filament

        def record(trace, dist, *args, **kwargs):
            if trace:
                self.moves.append((trace, kwargs.get('endstop_name')))
            return move_filament(trace, dist, *args, **kwargs)

        self.mmu.move_filament = record
        self.hh.heat_extruder(220)
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.run_gcode('MMU_SELECT GATE=0')

    def hall_reading(self):
        """What the hall sensor itself reports - the status Mainsail/Fluidd show."""
        status = self.hall.sensor.get_status(self.hh.reactor.monotonic())
        return status['Diameter'], status['filament_detected']

    def load(self):
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [], 'load was not clean')
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)


class TestLoad(Plus4Case):

    def test_load_homes_at_the_extruder_on_the_hall_sensor(self):
        self.assertFalse(self.hall.present)
        self.load()
        homing = [endstop for trace, endstop in self.moves if trace.startswith('Homing filament to extruder')]
        self.assertTrue(homing, self.moves)
        self.assertTrue(all(endstop.endswith('extruder') for endstop in homing), homing)
        diameter, detected = self.hall_reading()
        self.assertTrue(detected)
        self.assertGreater(diameter, self.hall.sensor.hall_min_diameter)

    def test_hall_sensor_reads_clear_before_loading(self):
        diameter, detected = self.hall_reading()
        self.assertFalse(detected)
        self.assertLess(diameter, self.hall.sensor.hall_min_diameter)


class TestUnload(Plus4Case):

    def setUp(self):
        super().setUp()
        self.load()
        self.moves.clear()

    def test_hall_sensor_reads_clear_after_unload(self):
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        diameter, detected = self.hall_reading()
        self.assertFalse(detected)
        self.assertLess(diameter, self.hall.sensor.hall_min_diameter)

    def test_unload_parks_back_from_the_shared_hub(self):
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.errors, [])
        hub = self.fil.layout['mmu_shared_exit']
        self.assertFalse(self.fil.triggered('unit0:mmu_shared_exit'))
        self.assertAlmostEqual(hub - self.fil.tip[0], abs(self.unit.p.gate_parking_distance), places=1)

    def test_unload_cuts_before_the_extruder_and_verifies_on_the_hall_sensor(self):
        start = len(self.hh.gcode.executed)
        self.hh.run_gcode('MMU_UNLOAD')
        self.assertEqual(self.hh.errors, [])
        self.assertIn('_MMU_CUT_TIP', [line.split()[0] for line in self.hh.gcode.executed[start:] if line.strip()])
        self.assertIn(('Verifying cut off extruder sensor', 'extruder'), self.moves)


class TestMotorsOffThenHoming(Plus4Case):
    """
    Happy Hare #1532: on v3, any homing move after MMU_MOTORS_OFF shut Klipper down with
    KeyError 'stepper_mmu_gear' from the kinematics' calc_position, which real Klipper calls
    to work out where a homing move stopped. The fake HomingMove doesn't need a position, so
    each homing move here also asks the kinematics for one, keyed exactly as Klipper's
    homing.py keys it: by the names of kin.get_steppers().
    """

    def setUp(self):
        super().setUp()
        self.hh.place_filament(1, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=1')
        self.positions = []
        homing = sys.modules['extras.homing'].HomingMove
        homing_move = homing.homing_move
        test = self

        def klipper_homing_move(self, *args, **kwargs):
            kin = self.toolhead.get_kinematics()
            stepper_positions = {s.get_name(): s.get_commanded_position() for s in kin.get_steppers()}
            test.positions.append(kin.calc_position(stepper_positions))
            return homing_move(self, *args, **kwargs)

        patcher = mock.patch.object(homing, 'homing_move', klipper_homing_move)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Step 1 of the report: a gate selected with its filament sitting at the gate sensor
        with self.hh.quiet_sensors():
            self.hh.place_filament(0, position=self.fil.layout['mmu_shared_exit'] + 10.)
        self.assertTrue(self.hh.sensor('mmu_shared_exit').present)
        self.hh.run_gcode('MMU_MOTORS_OFF')
        self.assertEqual(self.hh.errors, [])

    def test_homing_move_off_the_gate_sensor(self):
        self.hh.run_gcode('MMU_TEST_HOMING_MOVE MOVE=-50 ENDSTOP=mmu_shared_exit STOP_ON_ENDSTOP=-1')
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.positions)
        self.assertFalse(self.hh.sensor('mmu_shared_exit').present)

    def test_eject_homes_off_the_gate_sensor(self):
        self.hh.run_gcode('MMU_TEST_HOMING_MOVE MOVE=-50 ENDSTOP=mmu_shared_exit STOP_ON_ENDSTOP=-1')
        self.hh.run_gcode('MMU_MOTORS_OFF')
        self.positions.clear()
        self.hh.run_gcode('MMU_EJECT GATE=1')
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(self.positions)
        self.assertEqual(self.mmu.gate_status[1], 0)    # GATE_EMPTY


class TestDoublePressCut(unittest.TestCase):
    """
    QIDI's stock cut presses the cutter twice, holding each press for 0.5 s, because one
    press can leave the filament joined. On v4 that is configuration: _MMU_CUT_TIP_VARS
    cut_iterations (press, dwell cut_dwell_time, release, then again) - no code needed.
    The cut motion macro's body runs here, so this is the shipped macro's own G-code.
    """

    def presses(self, profile):
        hh = session(profile)
        self.addCleanup(hh.close)
        hh.boot(calibrate=True)
        hh.printer.harness_macro_effects['_CUT_TIP_DO_CUT_MOTION'] = lambda macro, gcmd: macro.run_body(gcmd)
        start = len(hh.gcode.executed)
        hh.run_gcode('_CUT_TIP_DO_CUT_MOTION PIN_PARK_X_LOC=304 PIN_PARK_Y_LOC=25')
        self.assertEqual(hh.errors, [])
        lines = hh.gcode.executed[start:]
        events = [line.split('EVENT=')[1].strip('"') for line in lines if line.startswith('_MMU_SEND_TELEMETRY')]
        dwells = [line for line in lines if line.startswith('G4 ')]
        return events, dwells

    def test_two_presses_with_half_second_holds_when_configured(self):
        events, dwells = self.presses(profiles.get('qidi_plus4').derive('qidi_plus4_double_cut', syms={
            'VAR_CUT_TIP_ITERATIONS': 2, 'VAR_CUT_TIP_CUT_DWELL_TIME': 500}))
        self.assertEqual(events, ['cut', 'release', 'cut', 'release'])
        self.assertEqual(dwells[0], 'G4 P500.0')     # The hold at the bottom of each press
        self.assertEqual(dwells[-1], 'G4 P500.0')

    def test_one_press_by_default(self):
        for name in ('qidi_plus4', 'qidi'):
            with self.subTest(profile=name):
                events, _ = self.presses(name)
                self.assertEqual(events, ['cut', 'release'])


del Plus4Case


if __name__ == '__main__':
    unittest.main()
