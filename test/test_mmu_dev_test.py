# Happy Hare test harness - the _MMU_TEST developer command.
#
# WHY THIS FILE EXISTS
#
# _MMU_TEST is a bag of ~30 independent developer probes and NOTHING exercised it. That is
# exactly the shape of code that rots silently: it reaches deep into internals by name, so
# every rename lands everywhere except here, and nobody notices because nobody runs it. Six
# options had gone stale that way, all against APIs that moved during the per-unit refactor:
#
#   SET_RD        mmu.calibration_manager      -> mmu_unit.calibrator
#   RUNOUT        mmu._enable_runout           -> mmu.sensor_manager.enable_runout
#   SYNC_STATE    lookup_object('mmu_sensors') -> mmu_unit.buffer (the sync sensors moved
#                                                 to MmuBuffer, and MmuSensors lost the
#                                                 `sensors` dict and the callbacks entirely)
#   SEL_MOVE      mmu.selector.move(...)       -> mmu.selector().move(...) - `selector`
#   SEL_HOMING_MOVE                               became a method; five call sites missed it
#   SEL_LOAD_TEST
#   AUTO_CALIBRATE  mmu._auto_calibrate        -> split into the calibrator's
#                                                 note_load_telemetry / note_unload_telemetry
#
# So this file is deliberately BREADTH, not depth: run each option, assert it did not throw.
# It is not trying to verify what the probes measure - several of them are stress tests whose
# whole output is "no exception" anyway.
#
# WHAT IS NOT COVERED, and why:
#
#   SYNC_STATE=loop     Cannot complete anywhere, harness or hardware - it busy-waits and
#                       wedges the reactor. Now REFUSED with an explanation rather than left to
#                       hang; see test_sync_state_loop_is_refused_rather_than_hanging.
#   TTC_TEST*,          Provoke timing faults in real Klipper step generation, which the
#   STEPCOMPRESS_TEST,  harness does not model at all (test/README.md section 9). They run
#   QUIESCE_TEST,       clean here and prove nothing; covered below only to the extent that
#   *SYNC_TEST          they must not raise.
#   RUN_SEQUENCE,       Run, but every timing is 0.0 because macro bodies never execute.
#   RUN_CHANGE_SEQUENCE
#
# PINNED TO THE DEFAULT PROFILE. ercf_vvd is the only shipped profile with a toolhead sensor
# (profiles.py:351), and the movement probes default to ENDSTOP=toolhead with no way to
# redirect some of them. It also has two units, an encoder and a buffer, so every option has
# the hardware it needs.
#
#   ./venv/bin/python -m unittest test.test_mmu_dev_test
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)


class DevTestCase(unittest.TestCase):
    """
    One calibrated, homed machine per test.

    Per test rather than per class because these probes mutate filament position, sync mode,
    rotation distance and the calibrated bowden length - i.e. precisely the state the next one
    would read.
    """

    PROFILE = 'ercf_vvd'

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot(calibrate=True)
        self.hh.heat_extruder(220)
        self.hh.filament()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

    def tearDown(self):
        self.hh.close()

    def run_option(self, args):
        """Run one _MMU_TEST invocation and insist it neither raised nor logged an error."""
        self.hh.run_gcode('_MMU_TEST %s' % args)
        self.assertEqual(self.hh.errors, [], 'errors from _MMU_TEST %s' % args)


class TestStateProbes(DevTestCase):
    """The read/write probes - the ones a developer reaches for most often."""

    def test_filament_position_state_round_trips(self):
        """
        SET_POS is a FILAMENT_POS_* STATE, not a distance - SET_POSITION is the mm one, and
        confusing the two is what made SET_POS look broken.

        State 0 matters: it used to be unreachable because the guard was `if pos > 0`, so
        SET_POS=0 was silently ignored and the machine stayed wherever it was.
        """
        from extras.mmu.mmu_constants import FILAMENT_POS_UNLOADED
        self.run_option('SET_POS=3')
        self.assertEqual(self.hh.mmu.filament_pos, 3)
        self.run_option('SET_POS=0')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_the_position_state_is_an_int_not_a_float(self):
        """It indexes FILAMENT_POS_NAME_MAP, so a float reads back as 'pos state: 3.0'."""
        self.run_option('SET_POS=3')
        self.assertIsInstance(self.hh.mmu.filament_pos, int)

    def test_filament_position_in_mm(self):
        self.run_option('SET_POSITION=100')
        self.assertAlmostEqual(self.hh.mmu.drive().get_filament_position(), 100., places=3)

    def test_the_read_only_probes(self):
        for args in ('GET_POS=1', 'GET_POSITION=1', 'GET_EXT_POSITION=1', 'DUMP_UNICODE=1',
                     'DUMP_ACTIVE_SENSORS=1', 'CALC_PURGE=1', 'HELP=1'):
            with self.subTest(option=args):
                self.run_option(args)

    def test_dump_mcu_endstops(self):
        """
        Walks es.get_mcu().get_name() over every tracked endstop. The fake TMC virtual
        endstop chip used to be registered with no mcu, so this raised
        "'NoneType' object has no attribute 'get_name'" on any profile with a *_touch endstop.
        """
        self.run_option('DUMP_MCU_ENDSTOPS=1')

    def test_the_setters_that_only_poke_state(self):
        for args in ('FILAMENT_POS=5', 'FILAMENT_DIR=1', 'FILAMENT_DIR=-1', 'SET_ACTION=2',
                     'SYNC=1', 'SYNC=0', "UPDATE_STATUS={'harness':1}", 'UPDATE_STATUS=OFF'):
            with self.subTest(option=args):
                self.run_option(args)


class TestCalibrationProbes(DevTestCase):
    """Everything that reaches the per-unit calibrator - where the refactor did most damage."""

    def test_set_rd_updates_the_gates_rotation_distance(self):
        """`mmu.calibration_manager` no longer exists; this is the unit's calibrator now."""
        self.run_option('SET_RD=23.5 GATE=0')
        calibrator = self.hh.mmu.mmu_unit(0).calibrator
        self.assertAlmostEqual(calibrator.rotation_distances[0], 23.5, places=3)

    def test_load_telemetry_with_no_delta_changes_nothing(self):
        """A bare call defaults TRAVEL to LENGTH, so autotune has nothing to act on."""
        calibrator = self.hh.mmu.mmu_unit(0).calibrator
        before = calibrator.get_bowden_length(0)
        self.run_option('NOTE_LOAD_TELEMETRY=1 GATE=0')
        self.assertAlmostEqual(calibrator.get_bowden_length(0), before, places=3)

    def test_telemetry_accepts_a_travel_delta_and_a_ratio(self):
        """
        The replacement for AUTO_CALIBRATE, which called a method that no longer exists.
        TRAVEL is the knob: its delta against LENGTH is what _autotune_bowden_length acts on.
        Whether it then adjusts depends on the machine's autotune setting, so assert the call
        lands rather than a particular correction.
        """
        for args in ('NOTE_LOAD_TELEMETRY=1 GATE=0 TRAVEL=750',
                     'NOTE_UNLOAD_TELEMETRY=1 GATE=0 TRAVEL=650',
                     'NOTE_LOAD_TELEMETRY=1 GATE=0 LENGTH=700 TRAVEL=720 RATIO=1.02'):
            with self.subTest(option=args):
                self.run_option(args)


class TestSensorProbes(DevTestCase):
    """The sensor and event probes."""

    def test_runout_can_be_disarmed_and_rearmed(self):
        """Runout arming is per-gate on the sensor manager, not a controller private."""
        self.run_option('RUNOUT=0')
        self.run_option('RUNOUT=1')

    def test_each_sync_state_can_be_driven(self):
        """
        The sync feedback sensors live on the unit's BUFFER now. This whole block used to die
        on lookup_object('mmu_sensors') before doing anything.
        """
        for state in ('compression', 'tension', 'both', 'neutral'):
            with self.subTest(state=state):
                self.run_option('SYNC_STATE=%s' % state)

    def test_sync_state_is_repeatable_when_it_has_to_fake_the_sensors(self):
        """
        With the real sensors disabled the command builds phony ones. Removing them used to
        happen only on the SYNC_STATE=loop path, so a second call died with "mux command
        QUERY_FILAMENT_SENSOR SENSOR filament_compression already registered".
        """
        buffer = self.hh.mmu.mmu_unit().buffer
        buffer.compression_sensor.runout_helper.sensor_enabled = False
        buffer.tension_sensor.runout_helper.sensor_enabled = False
        before = set(self.hh.printer.objects)
        for state in ('compression', 'tension', 'neutral'):
            with self.subTest(state=state):
                self.run_option('SYNC_STATE=%s' % state)
        self.assertEqual(set(self.hh.printer.objects) - before, set(),
                         'phony sensors were left registered')

    def test_sync_state_loop_is_refused_rather_than_hanging(self):
        """
        SYNC_STATE=loop gathers results with `while <cond>: pass` inside a gcode handler,
        which blocks the single reactor greenlet - so the mmu:sync_feedback_finished events it
        waits on, delivered by MmuSyncFeedback's settle timers, can never arrive. It wedges the
        reactor on a printer just as much as here.

        It only ever appeared to be "just broken" because the setup ahead of it died first on a
        stale lookup_object('mmu_sensors'). Repairing that made the busy-wait reachable, so it
        is refused explicitly. If someone rewrites the choreography to yield, this test is the
        thing to delete.
        """
        with self.assertRaises(Exception) as caught:
            self.hh.run_gcode('_MMU_TEST SYNC_STATE=loop LOOP=2')
        self.assertIn('busy-wait', str(caught.exception))

    def test_the_event_and_sensor_path_probes(self):
        for args in ('SYNC_EVENT=0.5', 'SYNC_EVENT=-0.5', 'SEND_PRINTING_EVENT=1',
                     'SEND_PRINTING_EVENT=0', 'ACTIVATE_FLOWGUARD=1',
                     'SENSOR=1 POS=3 GATE=0 LOADING=1', 'NFC_READ=1 UID=04A1B2C3D4E5'):
            with self.subTest(option=args):
                self.run_option(args)


class TestMovementProbes(DevTestCase):
    """
    The selector and stress probes. They assert only that the call lands: what they exist to
    provoke is real step-generation timing, which the harness does not model.
    """

    def test_the_selector_probes(self):
        """`selector` is a method; five call sites here were still treating it as attribute."""
        for args in ('SEL_MOVE=1 MOVE=10', 'SEL_HOMING_MOVE=1 MOVE=-30',
                     'SEL_LOAD_TEST=1 LOOP=2 HOME=1'):
            with self.subTest(option=args):
                self.run_option(args)

    def test_wrap_current_runs_for_both_motors(self):
        for args in ('WRAP_CURRENT=1 MOTOR=gear PERCENT=50',
                     'WRAP_CURRENT=1 MOTOR=extruder PERCENT=75'):
            with self.subTest(option=args):
                self.run_option(args)

    def test_the_stress_probes_run(self):
        for args in ('QUIESCE_TEST=1', 'TTC_TEST=1 LOOP=1', 'TTC_TEST2=1 LOOP=1',
                     'TTC_TEST3=1 LOOP=1', 'SYNC_LOAD_TEST=1 LOOP=1 SELECT=0',
                     'REALISTIC_SYNC_TEST=1 LOOP=1 SELECT=0',
                     'STEPCOMPRESS_TEST=1 LOOP=10 SELECT=0'):
            with self.subTest(option=args):
                self.run_option(args)

    def test_the_sequence_probes_run(self):
        """Timings all come back 0.0 - macro bodies do not execute - but they must not raise."""
        for args in ('RUN_SEQUENCE=1', 'RUN_CHANGE_SEQUENCE=1'):
            with self.subTest(option=args):
                self.run_option(args)


class TestEncoderProbes(DevTestCase):
    """The encoder probes need a machine with an encoder; unit0 of the default profile has one."""

    def test_the_encoder_distance_can_be_set_and_adjusted(self):
        for args in ('SET_ENCODER=42', 'ADJUST_ENCODER=5', 'ADJUST_ENCODER=-5'):
            with self.subTest(option=args):
                self.run_option(args)


if __name__ == '__main__':
    unittest.main()
