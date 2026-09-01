# Happy Hare test harness - MMU_SENSORS SENSOR=<name> ENABLE=[0|1].
#
# A sensor that is never registered with filament_switch_sensor (a virtual endstop, or the
# analog "proportional" buffer sensor) has no way to be disabled at all - Mainsail's own
# toggle only reaches sensors it can see. MMU_SENSORS SENSOR=... ENABLE=[0|1] drives the same
# MmuRunoutHelper.sensor_enabled flag Mainsail/SET_FILAMENT_SENSOR already drives, but reaches
# every sensor in MmuSensorManager.all_sensors_map and, unlike that toggle, persists.
#
# Two follow-up requirements shape the trickier tests here:
#   - a live SET_FILAMENT_SENSOR toggle (Mainsail) must be exactly as sticky as one made via
#     MMU_SENSORS, in both directions - disable via one, re-enable via the other, restart.
#   - disabling the analog proportional sensor must also suppress FlowGuard's clog/tangle
#     dispatch for it, even though FlowGuard's own sensor-selection can retarget to a
#     still-enabled derived vsensor and bypass the disabled one's own gating otherwise.
#
#   ./venv/bin/python -m unittest test.test_mmu_sensor_enable
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import ast
import configparser
import logging
import re
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

# extras.mmu.mmu_constants must NOT be imported at module level: the harness needs to be the
# first thing to import `extras`, so it can resolve to the fake klippy tree rather than the
# real repo (see bootstrap.py's own lazy imports inside seed_* helpers for the same reason).
VARS_MMU_SENSOR_ENABLED = 'mmu_state_sensor_enabled'


def read_vars_file(hh):
    """Parse mmu_vars.cfg off disk - the only assertion that proves durability."""
    parser = configparser.ConfigParser()
    parser.read(hh.save_variables.filename)
    if not parser.has_section('Variables'):
        return {}
    out = {}
    for name, raw in parser.items('Variables'):
        try:
            out[name] = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            out[name] = raw
    return out


class SensorEnableTestCase(unittest.TestCase):
    """Single-unit (boxturtle): the core enable/disable/report/validation behaviour."""

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

    def tearDown(self):
        self.hh.close()

    def test_disable_persists_and_report_always_shows_disabled_tag(self):
        hh = self.hh
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=0')
        self.assertEqual(hh.errors, [])

        sensor = hh.mmu.sensor_manager.all_sensors_map['mmu_exit_0']
        self.assertFalse(sensor.runout_helper.sensor_enabled)
        self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {'mmu_exit_0': False})

        hh.reactor.advance(0.) # let the SAVE_VARIABLE flush timer fire
        self.assertEqual(read_vars_file(hh).get(VARS_MMU_SENSOR_ENABLED), {'mmu_exit_0': False})

        # MMU_SENSORS always lists every sensor, disabled or not - no DETAIL= needed
        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS')
        self.assertIn('mmu_exit_0', hh.console[-1])
        self.assertIn('(DISABLE)', hh.console[-1])

    def test_reenable_clears_persisted_entry_and_is_idempotent(self):
        hh = self.hh
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=0')

        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=1')
        sensor = hh.mmu.sensor_manager.all_sensors_map['mmu_exit_0']
        self.assertTrue(sensor.runout_helper.sensor_enabled)
        self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {})
        self.assertIn('enabled', hh.console[0])
        self.assertNotIn('no change', hh.console[0])

        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=1')
        self.assertIn('no change', hh.console[0])

    def test_sensor_alone_shows_disabled_sensor(self):
        hh = self.hh
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=0')

        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0')
        self.assertEqual(hh.errors, [])
        self.assertIn('mmu_exit_0', hh.console[-1])
        self.assertIn('(DISABLE)', hh.console[-1])
        self.assertNotIn('Sensors configured for', hh.console[-1])

    def test_enable_without_sensor_errors_and_writes_nothing(self):
        hh = self.hh
        hh.run_gcode('MMU_SENSORS ENABLE=0')
        self.assertEqual(len(hh.errors), 1)
        self.assertIn('requires SENSOR=', hh.errors[0])
        self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {})

    def test_unknown_sensor_name_errors(self):
        hh = self.hh
        hh.run_gcode('MMU_SENSORS SENSOR=does_not_exist ENABLE=0')
        self.assertEqual(len(hh.errors), 1)
        self.assertIn('Unknown sensor', hh.errors[0])

    def test_disabling_shared_gate_endstop_warns(self):
        hh = self.hh
        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS SENSOR=unit0:mmu_shared_exit ENABLE=0')
        self.assertEqual(hh.errors, [])
        self.assertTrue(any('shared-gate endstop' in line for line in hh.console))

    def test_report_has_no_trailing_newline(self):
        hh = self.hh
        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS')
        self.assertFalse(hh.console[-1].endswith('\n'))

        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0')
        self.assertFalse(hh.console[-1].endswith('\n'))


class SensorEnableMultiUnitTestCase(unittest.TestCase):
    """ercf_vvd: a bare name that collides across units, and the no-cascading guarantee."""

    def setUp(self):
        self.hh = session('ercf_vvd_buffers')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

    def tearDown(self):
        self.hh.close()

    def test_bare_name_ambiguous_across_units_requires_unit(self):
        hh = self.hh
        sm = hh.mmu.sensor_manager

        hh.run_gcode('MMU_SENSORS SENSOR=filament_tension ENABLE=0')
        self.assertEqual(len(hh.errors), 1)
        self.assertIn('more than one unit', hh.errors[0])
        self.assertTrue(sm.all_sensors_map['unit0:filament_tension'].runout_helper.sensor_enabled)
        self.assertTrue(sm.all_sensors_map['unit1:filament_tension'].runout_helper.sensor_enabled)

        hh.run_gcode('MMU_SENSORS UNIT=1 SENSOR=filament_tension ENABLE=0')
        self.assertFalse(sm.all_sensors_map['unit1:filament_tension'].runout_helper.sensor_enabled)
        self.assertTrue(sm.all_sensors_map['unit0:filament_tension'].runout_helper.sensor_enabled)

    def test_analog_sensor_disable_does_not_affect_derived_vsensors(self):
        hh = self.hh
        sm = hh.mmu.sensor_manager

        hh.run_gcode('MMU_SENSORS SENSOR=unit0:filament_proportional ENABLE=0')
        self.assertEqual(hh.errors, [])
        self.assertFalse(sm.all_sensors_map['unit0:filament_proportional'].runout_helper.sensor_enabled)
        self.assertTrue(sm.all_sensors_map['unit0:filament_compression'].runout_helper.sensor_enabled)
        self.assertTrue(sm.all_sensors_map['unit0:filament_tension'].runout_helper.sensor_enabled)

    def test_report_sorts_gate_numbers_naturally(self):
        """unit1 (ViViD) is gates 9-12 - a plain string sort would list 10, 11, 12, 9."""
        hh = self.hh
        hh.gcode.console.clear()
        hh.run_gcode('MMU_SENSORS')
        report = hh.console[-1]

        entry_gates = [int(m.group(1)) for m in re.finditer(r'mmu_entry_(\d+)', report)]
        self.assertEqual(entry_gates, sorted(entry_gates))
        self.assertEqual(entry_gates[0], 9, 'gate 9 must list before gate 10')

        exit_gates = [int(m.group(1)) for m in re.finditer(r'mmu_exit_(\d+)', report)]
        self.assertEqual(exit_gates, sorted(exit_gates))


class SensorEnableRestartAndMainsailTestCase(unittest.TestCase):
    """A fresh session models "the printer was rebooted"; each test boots its own."""

    def test_persisted_disable_survives_simulated_restart(self):
        hh = session('boxturtle')
        hh.boot(sensors_disabled=['unit0:mmu_shared_exit'])
        try:
            self.assertEqual(hh.errors, [], 'bootup was not clean')
            sensor = hh.mmu.sensor_manager.all_sensors_map['unit0:mmu_shared_exit']
            self.assertFalse(sensor.runout_helper.sensor_enabled)

            hh.gcode.console.clear()
            hh.run_gcode('MMU_SENSORS')
            self.assertIn('mmu_shared_exit', hh.console[-1])
            self.assertIn('(DISABLE)', hh.console[-1])
        finally:
            hh.close()

    def test_stale_persisted_sensor_name_is_ignored(self):
        hh = session('boxturtle')
        hh.boot(sensors_disabled=['unit0:no_such_sensor'])
        try:
            self.assertEqual(hh.errors, [], 'a stale persisted sensor name must not block boot')
        finally:
            hh.close()

    def test_set_filament_sensor_toggle_persists_like_mmu_sensors(self):
        """A live SET_FILAMENT_SENSOR (Mainsail) disable/enable must be as sticky as MMU_SENSORS."""
        hh = session('boxturtle')
        hh.boot()
        try:
            hh.run_gcode('SET_FILAMENT_SENSOR SENSOR=mmu_exit_0 ENABLE=0')
            self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {'mmu_exit_0': False})

            hh.run_gcode('SET_FILAMENT_SENSOR SENSOR=mmu_exit_0 ENABLE=1')
            self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {})
        finally:
            hh.close()

    def test_mmu_sensors_disable_then_mainsail_reenable_survives_restart(self):
        """The exact round trip the user flagged: disable via MMU_SENSORS, re-enable via
        Mainsail, restart - must come back up enabled, not silently reverted."""
        hh = session('boxturtle')
        hh.boot()
        try:
            hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=0')
            self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {'mmu_exit_0': False})

            hh.run_gcode('SET_FILAMENT_SENSOR SENSOR=mmu_exit_0 ENABLE=1')
            self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {})
        finally:
            hh.close()

        hh2 = session('boxturtle')
        hh2.boot()
        try:
            self.assertEqual(hh2.errors, [])
            sensor = hh2.mmu.sensor_manager.all_sensors_map['mmu_exit_0']
            self.assertTrue(sensor.runout_helper.sensor_enabled)
        finally:
            hh2.close()

    def test_same_value_set_filament_sensor_then_mmu_sensors_still_persists(self):
        """Regression guard for set_sensor_enabled's unconditional assignment: a Mainsail
        toggle to a value the live flag already has must still land in the persisted record
        when MMU_SENSORS is then used to make it sticky."""
        hh = session('boxturtle')
        hh.boot()
        try:
            hh.run_gcode('SET_FILAMENT_SENSOR SENSOR=mmu_exit_0 ENABLE=0') # live only
            hh.run_gcode('MMU_SENSORS SENSOR=mmu_exit_0 ENABLE=0') # same value, now sticky
            self.assertEqual(hh.mmu.var_manager.get(VARS_MMU_SENSOR_ENABLED, {}), {'mmu_exit_0': False})
        finally:
            hh.close()


class FlowGuardSuppressionTestCase(unittest.TestCase):
    """
    emu: the only shipped profile with an analog buffer sensor, so the only one with a real
    FlowGuard/proportional-sensor path to test against.

    _process_status is called directly with a minimal but real status shape so the test can
    target the dispatch guard without having to manufacture the preceding ADC history.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.sf = self.hh.mmu.mmu_machine.units[0].sync_feedback
        self.sf.flowguard_active = True

    def tearDown(self):
        self.hh.close()

    def _trip(self, trigger='clog'):
        status = {
            'output': {
                'sensor_ui': 0.0,
                'flowguard': {'trigger': trigger, 'reason': 'test'},
                'autotune': {},
                'rd_current': 1.0,
                'rd_prev': 1.0,
                'rd_tuned': 1.0,
            }
        }
        self.sf._process_status(self.hh.reactor.monotonic(), status)

    def test_disabled_buffer_suppresses_flowguard_clog_tangle_dispatch(self):
        hh = self.hh
        sm = hh.mmu.sensor_manager
        for name in ('unit0:filament_proportional', 'unit0:filament_compression', 'unit0:filament_tension'):
            sm.set_sensor_enabled(name, False)

        compression = sm.all_sensors_map['unit0:filament_compression']
        called = []
        compression.runout_helper.note_clog_tangle = lambda event_type: called.append(event_type)

        self._trip('clog')
        self.assertEqual(called, [])
        # FlowGuard itself still ran and reported the trip - only the gcode dispatch is suppressed
        self.assertTrue(any('FlowGuard detected a clog' in e for e in hh.errors))

    def test_enabled_buffer_still_dispatches_flowguard_clog_tangle(self):
        """Control case: with the sensor enabled, the dispatch this change guards must still fire."""
        hh = self.hh
        sm = hh.mmu.sensor_manager

        proportional = sm.all_sensors_map['unit0:filament_proportional']
        called = []
        proportional.runout_helper.note_clog_tangle = lambda event_type: called.append(event_type)

        self._trip('clog')
        self.assertEqual(called, ['clog'])


class DiscreteFlowGuardDispatchTestCase(unittest.TestCase):
    """BoxTurtle's switch buffer must dispatch both FlowGuard event types."""

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.sf = self.mmu.mmu_machine.units[0].sync_feedback
        self.sf.flowguard_active = True

    def tearDown(self):
        self.hh.close()

    def _trip(self, trigger):
        calls = []

        def capture_runout(**kwargs):
            calls.append(kwargs)
            self.mmu.pause_resume.send_resume_command()

        self.mmu._runout = capture_runout
        status = {
            'output': {
                'sensor_ui': 0.0,
                'flowguard': {'trigger': trigger, 'reason': 'test'},
                'autotune': {},
                'rd_current': 1.0,
                'rd_prev': 1.0,
                'rd_tuned': 1.0,
            }
        }
        pause_calls = self.mmu.pause_resume.pause_calls
        resume_calls = self.mmu.pause_resume.resume_calls

        self.sf._process_status(self.hh.reactor.monotonic(), status)
        self.hh.reactor.advance(0.)

        self.assertEqual(self.mmu.pause_resume.pause_calls, pause_calls + 1)
        self.assertEqual(self.mmu.pause_resume.resume_calls, resume_calls + 1)
        self.assertFalse(self.mmu.pause_resume.is_paused)
        self.assertFalse(self.sf.flowguard_active)
        return calls

    def test_compression_sensor_dispatches_clog(self):
        self.assertEqual(
            self._trip('clog'),
            [{'event_type': 'clog', 'sensor': 'unit0:filament_compression'}]
        )

    def test_tension_sensor_dispatches_tangle(self):
        self.assertEqual(
            self._trip('tangle'),
            [{'event_type': 'tangle', 'sensor': 'unit0:filament_tension'}]
        )


class FlowGuardBufferStatusTestCase(unittest.TestCase):
    """The buffer status API must report the live monitoring flags, not cached telemetry."""

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.sf = self.mmu.mmu_machine.units[0].sync_feedback
        self.assertTrue(self.sf.mmu_unit.has_buffer())
        self.assertFalse(self.sf.mmu_unit.has_encoder())

    def tearDown(self):
        self.hh.close()

    def flowguard_status(self):
        return self.mmu.get_status(self.hh.reactor.monotonic())['flowguard']

    def test_buffer_only_status_tracks_live_flowguard_flags(self):
        self.assertEqual(
            (self.flowguard_status()['enabled'], self.flowguard_status()['active']),
            (True, False),
        )

        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.assertEqual(
            (self.flowguard_status()['enabled'], self.flowguard_status()['active']),
            (True, True),
        )

        # The controller's cached 'active' field is its private warm-up state. It must
        # not leak back out as the public monitoring state after monitoring is disabled.
        self.sf.flowguard_status['active'] = True
        self.mmu._disable_filament_monitoring()
        self.assertEqual(
            (self.flowguard_status()['enabled'], self.flowguard_status()['active']),
            (True, False),
        )


if __name__ == '__main__':
    unittest.main()
