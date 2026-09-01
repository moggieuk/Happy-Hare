# Happy Hare test harness - runout event arming, the "late runout event" filter, and the
# clock they depend on.
#
# A runout callback does not run when the sensor trips. MmuRunoutHelper registers a reactor
# callback, that callback calls pause_resume.send_pause_command() and then gcode.run_script(),
# and run_script blocks on the gcode mutex. If the trip happened inside a Tn macro, the handler
# does not get the mutex until that whole macro finishes - by which time the toolchange has
# exited wrap_suspend_filament_monitoring and re-armed monitoring.
#
# __MMU_SENSOR_RUNOUT therefore has to decide whether a delayed event is stale. It used to
# compare against a single watermark (runout_last_enable_time), which cannot tell "raised while
# monitoring was suspended" from "raised while armed, delivered late" - so a genuine runout that
# straddled a toolchange was discarded and the print resumed onto an empty gate. It now brackets
# the suspend window and records when a runout was last actually handled.
#
# ALL OF THIS ASSUMES ONE CLOCK. Every timestamp in the chain - klipper's button #receive_time,
# note_filament_present's gating, the EVENTTIME on the gcode, and runout_last_*_time - is host
# reactor monotonic, never MCU print_time. The clock tests below pin that down, because a virtual
# endstop's trigger callback may arrive on either clock and the two have unrelated origins.
#
#   ./venv/bin/python -m unittest test.test_mmu_sensor_runout
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_LOADED = 10
GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1
TIP_AT_GATE = -40.0

RUNOUT_CMD = '__MMU_SENSOR_RUNOUT EVENTTIME=%.3f SENSOR=%s'


class RunoutFilterTestCase(unittest.TestCase):
    """
    Boxturtle: 4 gates, each with its own mmu_entry_N / mmu_exit_N switch, plus one
    unit0:mmu_shared_exit. The per-gate exit sensor is what the field report named.
    """

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.runouts = []
        self.mmu._runout = lambda **kwargs: self.runouts.append(kwargs)

    def tearDown(self):
        self.hh.close()

    def deliver(self, eventtime, sensor='mmu_exit_0', gate=0):
        """Run the runout handler as the reactor callback would, with a chosen event time."""
        cmd = RUNOUT_CMD % (eventtime, sensor)
        if gate is not None:
            cmd += ' GATE=%d' % gate
        self.hh.run_gcode(cmd)

    def arm(self):
        """Put monitoring into the armed state and return the reactor time it happened."""
        self.mmu._enable_filament_monitoring()
        return self.hh.reactor.monotonic()

    def set_sensor_state(self, name, present):
        """Set the level used by direct delayed-handler delivery tests."""
        self.hh.sensor(name).sensor.runout_helper.filament_present = bool(present)


class TestDelayedDelivery(RunoutFilterTestCase):

    def test_event_raised_while_armed_survives_a_suspend_cycle(self):
        """
        The regression. Sensor trips while monitoring is armed; a toolchange then runs a
        whole suspend/resume cycle before the queued handler gets the gcode mutex. The
        watermark implementation classified this as late and dropped it.
        """
        self.arm()
        self.hh.settle(1.0)
        tripped = self.hh.reactor.monotonic()

        self.hh.settle(1.0)
        with self.mmu.wrap_suspend_filament_monitoring(): # Stands in for MMU_CHANGE_TOOL
            self.hh.settle(5.0)
        self.hh.settle(1.0)

        self.assertGreater(self.mmu.runout_last_enable_time, tripped,
                           'test is not exercising anything: monitoring was not re-armed after the trip')
        self.deliver(tripped)
        self.assertEqual(len(self.runouts), 1, 'genuine runout was discarded as late')

    def test_event_raised_inside_a_suspend_window_is_dropped(self):
        """The reason the filter exists: an MMU-commanded move crossing its own sensor."""
        self.arm()
        self.hh.settle(1.0)
        with self.mmu.wrap_suspend_filament_monitoring():
            self.hh.settle(1.0)
            tripped = self.hh.reactor.monotonic()
            self.hh.settle(1.0)
        self.hh.settle(1.0)

        self.deliver(tripped)
        self.assertEqual(self.runouts, [], 'event raised while suspended should be ignored')

    def test_re_arming_while_already_armed_does_not_widen_the_window(self):
        """
        The window only means anything if both ends move together. If enable bumped its end
        without a matching disable, the window would grow to cover a period when monitoring
        was armed, and events raised in it would be dropped.
        """
        self.arm()
        self.hh.settle(1.0)
        tripped = self.hh.reactor.monotonic()

        self.hh.settle(1.0)
        self.mmu._enable_filament_monitoring() # Already armed - must be a no-op for the window
        self.hh.settle(1.0)

        self.deliver(tripped)
        self.assertEqual(len(self.runouts), 1, 'a redundant re-arm swallowed a genuine runout')

    def test_ignored_event_is_not_reported_as_an_assertion(self):
        """
        log_assertion writes '!!' plus a stack trace to the console. A suspend-window drop is
        routine, so it must not surface there - that is what the tester actually saw.
        """
        self.arm()
        self.hh.settle(1.0)
        with self.mmu.wrap_suspend_filament_monitoring():
            self.hh.settle(1.0)
            tripped = self.hh.reactor.monotonic()
            self.hh.settle(1.0)
        self.hh.settle(1.0)

        self.deliver(tripped)
        self.assertEqual(self.runouts, [], 'test is not exercising the ignore path')
        self.assertEqual(self.hh.errors, [])


class TestGateMap(RunoutFilterTestCase):

    def test_stale_entry_event_still_marks_a_non_selected_gate_empty(self):
        """
        Pre-v4 this update was the leading branch and the late check was the fallback, so a
        stale event still recorded the empty lane. Losing it lets endless spool later route
        to a gate it had been told was empty.
        """
        self.hh.place_filament(2, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.assertEqual(self.mmu.gate_maps.gate_status[2], GATE_AVAILABLE)
        self.assertNotEqual(self.mmu.gate_selected, 2, 'gate 2 must be idle for this test')

        self.arm()
        self.hh.settle(1.0)
        with self.mmu.wrap_suspend_filament_monitoring():
            self.hh.settle(1.0)
            tripped = self.hh.reactor.monotonic() # Stale: raised inside the suspend window
            self.hh.settle(1.0)
        self.hh.settle(1.0)

        # deliver() intentionally bypasses the physical edge; model the clear level
        # that the delayed remove/runout handler must re-check before trusting it.
        self.set_sensor_state('mmu_entry_2', False)
        self.deliver(tripped, sensor='mmu_entry_2', gate=2)
        self.assertEqual(self.runouts, [], 'idle-lane event should not drive runout handling')
        self.assertEqual(self.mmu.gate_maps.gate_status[2], GATE_EMPTY)

    def test_selected_entry_runout_marks_empty_before_processing(self):
        gate = self.mmu.gate_selected
        self.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
        self.mmu.endless_spool_enabled = False
        self.set_sensor_state('mmu_entry_%d' % gate, False)

        self.deliver(self.hh.reactor.monotonic(), sensor='mmu_entry_%d' % gate, gate=gate)

        self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_EMPTY)
        self.assertEqual(len(self.runouts), 1, 'selected gate must still enter normal runout handling')

    def test_bounced_idle_entry_runout_is_ignored_using_its_own_gate_sensor(self):
        # Gate 0 is active and clear; gate 2, named by the delayed event, has bounced
        # back to present. Reading the generic active sensor would accept this event.
        self.mmu.gate_maps.set_gate_status(2, GATE_AVAILABLE)
        self.set_sensor_state('mmu_entry_0', False)
        self.set_sensor_state('mmu_entry_2', True)

        self.deliver(self.hh.reactor.monotonic(), sensor='mmu_entry_2', gate=2)

        self.assertEqual(self.mmu.gate_maps.gate_status[2], GATE_AVAILABLE)
        self.assertEqual(self.runouts, [])
        self.assertTrue(any('sensor malfunction' in error for error in self.hh.errors))

    def test_unreadable_runout_sensor_is_ignored(self):
        sensor = self.hh.sensor('mmu_entry_2').sensor
        sensor.runout_helper.sensor_enabled = False
        self.mmu.gate_maps.set_gate_status(2, GATE_AVAILABLE)

        self.deliver(self.hh.reactor.monotonic(), sensor='mmu_entry_2', gate=2)

        self.assertEqual(self.mmu.gate_maps.gate_status[2], GATE_AVAILABLE)
        self.assertEqual(self.runouts, [])
        self.assertTrue(any('cannot be read' in error for error in self.hh.errors))


class TestInsertRemoveHandlers(RunoutFilterTestCase):

    def run_insert(self, gate):
        self.hh.run_gcode('__MMU_SENSOR_INSERT SENSOR=mmu_entry_%d GATE=%d' % (gate, gate))

    def run_remove(self, gate):
        self.hh.run_gcode('__MMU_SENSOR_REMOVE SENSOR=mmu_entry_%d GATE=%d' % (gate, gate))

    def test_insert_marks_empty_gate_unknown_before_every_autoload_guard(self):
        gate = 1
        unit = self.mmu.mmu_unit(gate)
        self.set_sensor_state('mmu_entry_%d' % gate, True)

        cases = (
            ('autoload disabled', False, 0, 0),
            ('printing', True, 0, 1),
            ('action busy', False, 1, 1),
        )
        for label, printing, action, autoload in cases:
            with self.subTest(label=label):
                self.mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)
                self.mmu.is_printing = lambda value=printing: value
                self.mmu.action = action
                unit.p.gate_autoload = autoload

                self.run_insert(gate)

                self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_UNKNOWN)

    def test_bounced_insert_is_ignored(self):
        gate = 1
        self.mmu.gate_maps.set_gate_status(gate, GATE_EMPTY)
        self.set_sensor_state('mmu_entry_%d' % gate, False)

        self.run_insert(gate)

        self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_EMPTY)
        self.assertTrue(any('sensor malfunction' in error for error in self.hh.errors))

    def test_remove_marks_gate_empty(self):
        gate = 1
        self.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
        self.set_sensor_state('mmu_entry_%d' % gate, False)

        self.run_remove(gate)

        self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_EMPTY)

    def test_remove_from_unrelated_gate_is_not_hidden_by_eject_gate(self):
        gate = 1
        self.mmu.endless_spool_enabled = True
        self.mmu.p.endless_spool_eject_gate = 2
        self.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
        self.set_sensor_state('mmu_entry_%d' % gate, False)

        self.run_remove(gate)

        self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_EMPTY)

    def test_remove_marks_designated_eject_gate_empty_too(self):
        self.mmu.endless_spool_enabled = True
        for gate in (0, 2):
            with self.subTest(gate=gate):
                self.mmu.p.endless_spool_eject_gate = gate
                self.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
                self.set_sensor_state('mmu_entry_%d' % gate, False)

                self.run_remove(gate)

                self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_EMPTY)

    def test_bounced_remove_is_ignored(self):
        gate = 1
        self.mmu.gate_maps.set_gate_status(gate, GATE_AVAILABLE)
        self.set_sensor_state('mmu_entry_%d' % gate, True)

        self.run_remove(gate)

        self.assertEqual(self.mmu.gate_maps.gate_status[gate], GATE_AVAILABLE)
        self.assertTrue(any('sensor malfunction' in error for error in self.hh.errors))


class TestNonGateInsertValidation(unittest.TestCase):

    def setUp(self):
        self.hh = session('3ms')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.sensor_name = 'default:extruder'

    def tearDown(self):
        self.hh.close()

    def set_sensor_state(self, present):
        self.hh.sensor(self.sensor_name).sensor.runout_helper.filament_present = bool(present)

    def test_bounced_extruder_insert_is_rejected_before_bypass_dispatch(self):
        self.set_sensor_state(False)

        self.hh.run_gcode('__MMU_SENSOR_INSERT SENSOR=%s' % self.sensor_name)

        self.assertTrue(any('sensor malfunction' in error for error in self.hh.errors))

    def test_current_extruder_insert_reaches_bypass_dispatch(self):
        self.set_sensor_state(True)

        self.hh.run_gcode('__MMU_SENSOR_INSERT SENSOR=%s' % self.sensor_name)

        self.assertEqual(self.hh.errors, [])


class TestRunoutArming(RunoutFilterTestCase):
    """
    enable_runout/disable_runout have to act on a set that does not move underneath them.
    active_sensors_map is re-pointed by _handle_gate_selected, so using it meant a toolchange
    disarmed one gate's sensors and re-armed a different gate's.
    """

    def suspended(self, name):
        return self.mmu.sensor_manager.all_sensors_map[name].runout_helper.runout_suspended

    def test_a_gate_change_inside_a_suspend_block_does_not_strand_the_old_gate(self):
        self.mmu._enable_filament_monitoring()
        self.assertFalse(self.suspended('mmu_entry_0'))

        with self.mmu.wrap_suspend_filament_monitoring():
            self.assertTrue(self.suspended('mmu_entry_0'), 'suspend did not reach the idle lanes')
            self.mmu.select_gate(1)

        self.assertFalse(self.suspended('mmu_entry_0'), 'gate 0 left suspended after a gate change')
        self.assertFalse(self.suspended('mmu_exit_0'))
        self.assertFalse(self.suspended('mmu_entry_1'))

    def test_unit_level_sensors_are_covered_when_no_unit_is_selected(self):
        """
        reinit() leaves unit_selected None until a gate is chosen, and MMU_RESET goes back
        through it. Skipping unit-level sensors in that state would strand whatever a
        disable-with-a-unit-selected had just suspended.
        """
        self.mmu._enable_filament_monitoring()
        self.mmu._disable_filament_monitoring()
        self.assertTrue(self.suspended('unit0:mmu_shared_exit'))

        self.mmu.unit_selected = None
        self.mmu._enable_filament_monitoring()
        self.assertFalse(self.suspended('unit0:mmu_shared_exit'),
                         'unit-level sensor stranded while no unit was selected')

    def test_idle_lane_runout_is_not_reported_as_an_assertion(self):
        """
        With idle lanes armed, pulling a spool from one raises a real runout event. It has
        nothing to handle beyond the gate map, but it used to hit the loud catch-all.
        """
        self.hh.place_filament(2, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.mmu._enable_filament_monitoring()
        self.hh.settle(1.0)
        now = self.hh.reactor.monotonic()

        # Direct delivery bypasses the physical switch edge, so publish the clear
        # levels the delayed callbacks are expected to confirm.
        self.set_sensor_state('mmu_entry_2', False)
        self.deliver(now, sensor='mmu_entry_2', gate=2)
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.mmu.gate_maps.gate_status[2], GATE_EMPTY)

        self.set_sensor_state('mmu_exit_2', False)
        self.deliver(now, sensor='mmu_exit_2', gate=2)
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.runouts, [], 'an idle lane must not drive runout handling')


class TestRunoutArmingAcrossUnits(unittest.TestCase):
    """Use the synthetic two-buffer variant to exercise both sides of the hand-off."""

    def setUp(self):
        self.hh = session('ercf_vvd_buffers')
        self.hh.boot(calibrate=True)
        self.mmu = self.hh.mmu

    def tearDown(self):
        self.hh.close()

    def suspended(self, name):
        return self.mmu.sensor_manager.all_sensors_map[name].runout_helper.runout_suspended

    def flowguard(self, index):
        return self.mmu.mmu_machine.get_mmu_unit_by_index(index).sync_feedback.flowguard_active

    def encoder_flowguard(self, index):
        encoder = self.mmu.mmu_machine.get_mmu_unit_by_index(index).encoder
        return encoder.is_flowguard_enabled() if encoder else False

    def test_flowguard_follows_the_selected_unit(self):
        """
        FlowGuard raises clog/tangle through note_clog_tangle, which does not consult the
        sensor arming at all, so it needs its own per-unit hand-off. A unit change that is
        not bracketed by a suspend block used to leave the previous unit armed.
        """
        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.assertTrue(self.flowguard(0))
        self.assertFalse(self.flowguard(1))

        self.mmu.select_gate(9) # No suspend block around this

        self.assertTrue(self.flowguard(1))
        self.assertFalse(self.flowguard(0), 'previous unit still armed to raise clog/tangle')
        self.assertFalse(self.encoder_flowguard(0), 'previous unit encoder still armed')
        self.assertEqual(self.hh.errors, [])

    def test_monitoring_off_disarms_every_unit(self):
        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.mmu._disable_filament_monitoring()

        for index in (0, 1):
            self.assertFalse(self.flowguard(index))
            self.assertFalse(self.encoder_flowguard(index))
        self.assertTrue(self.suspended('default:extruder'))

    def test_a_shared_sensor_survives_a_unit_change(self):
        """
        A common toolhead/extruder switch is in every unit's map, so the "disarm the other
        units" pass would disarm the selected unit's own sensor. Extruder runout is the one
        that demands manual intervention, so losing it is the worst case.
        """
        extruder = self.mmu.sensor_manager.all_sensors_map['default:extruder']
        self.assertTrue(all(extruder in sensors.values()
                            for sensors in self.mmu.sensor_manager.unit_sensors),
                        'profile no longer shares the extruder sensor - test is vacuous')

        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.assertFalse(self.suspended('default:extruder'))

        self.mmu.select_gate(9)
        self.assertFalse(self.suspended('default:extruder'),
                         'shared extruder sensor disarmed by a unit change')

    def test_the_newly_selected_unit_is_armed_without_waiting_for_a_re_enable(self):
        """
        Unit selection changes which sensors are in scope, not whether monitoring is on. A
        unit-level runout sensor on the incoming unit must not sit disarmed until whatever
        happens to call _enable_filament_monitoring next.
        """
        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.assertTrue(self.suspended('unit1:filament_compression'))

        self.mmu.select_gate(9)

        self.assertFalse(self.suspended('unit1:filament_compression'),
                         'incoming unit left disarmed after a unit change')
        self.assertTrue(self.suspended('unit0:filament_compression'))

    def test_crossing_units_hands_over_cleanly(self):
        self.mmu.select_gate(0)
        self.mmu._enable_filament_monitoring()
        self.assertFalse(self.suspended('unit0:filament_compression'))
        self.assertTrue(self.suspended('unit1:filament_compression'),
                        'the idle unit should already be gated by _handle_unit_selected')

        with self.mmu.wrap_suspend_filament_monitoring():
            self.mmu.select_gate(9) # Into unit 1
        self.assertEqual(self.mmu.unit_selected, 1)

        # Unit-level sensors follow the selected unit; per-gate sensors stay live throughout
        self.assertTrue(self.suspended('unit0:filament_compression'))
        self.assertFalse(self.suspended('unit1:filament_compression'))
        self.assertFalse(self.suspended('mmu_entry_9'))
        self.assertFalse(self.suspended('mmu_exit_9'))


class TestSuspendEvents(RunoutFilterTestCase):
    """
    suspend_events works the min_event_systime gate. Saving and restoring that value is only
    safe if NEVER - which means "a handler is in flight" - never gets written back.
    """

    def helper(self, name='mmu_entry_0'):
        return self.mmu.sensor_manager.all_sensors_map[name].runout_helper

    def test_restore_does_not_write_back_never(self):
        rh = self.helper()
        rh.min_event_systime = self.hh.reactor.NEVER # A runout handler is mid-flight

        rh.suspend_events(True)
        rh.suspend_events(False)
        self.assertNotEqual(rh.min_event_systime, self.hh.reactor.NEVER,
                            'sensor was silenced for the rest of the session')

    def test_nested_suspend_restores_the_original_gate(self):
        rh = self.helper()
        rh.min_event_systime = original = self.hh.reactor.monotonic()

        rh.suspend_events(True)
        rh.suspend_events(True)
        self.assertEqual(rh.min_event_systime, self.hh.reactor.NEVER)

        rh.suspend_events(False)
        rh.suspend_events(False)
        self.assertEqual(rh.min_event_systime, original)

    def test_nested_wrappers_keep_events_suspended_until_the_outer_one_exits(self):
        never = self.hh.reactor.NEVER
        gated = lambda: [s for s in set(self.mmu.sensor_manager.all_sensors_map.values())
                         if s.runout_helper.min_event_systime == never]

        with self.mmu.wrap_suspend_insert_events():
            outer = gated()
            self.assertTrue(outer, 'nothing was suspended')
            with self.mmu.wrap_suspend_insert_events():
                pass
            self.assertEqual(gated(), outer, 'inner exit released the outer block')

        self.assertEqual(gated(), [])

    def test_a_gate_change_inside_the_wrapper_does_not_strand_a_sensor(self):
        never = self.hh.reactor.NEVER
        with self.mmu.wrap_suspend_insert_events():
            gated = [s for s in set(self.mmu.sensor_manager.all_sensors_map.values())
                     if s.runout_helper.min_event_systime == never]
            self.assertTrue(gated, 'nothing was suspended')
            self.mmu.select_gate(1)

        still_gated = sorted(s.runout_helper.name for s in gated
                             if s.runout_helper.min_event_systime == never)
        self.assertEqual(still_gated, [])


class TestDuplicateSuppression(unittest.TestCase):
    """
    One physical runout trips more than one sensor, and each MmuRunoutHelper gates events
    independently - so the second sensor's event must be recognised as belonging to the
    runout already handled, not treated as a fresh one.
    """

    def setUp(self):
        self.hh = session('boxturtle')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.fil = self.hh.filament()
        for gate in range(4):
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1,1,1 ENABLE=1')

    def tearDown(self):
        self.hh.close()

    def test_event_predating_a_handled_runout_is_ignored(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)
        self.fil.exhaust(self.mmu.gate_selected)
        self.hh.settle()

        tripped = self.hh.reactor.monotonic() # Both sensors release at about this moment
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        self.hh.settle(1.0)
        self.assertGreater(self.mmu.runout_last_handled_time, 0.)

        runouts = []
        self.mmu._runout = lambda **kwargs: runouts.append(kwargs)
        self.hh.run_gcode(RUNOUT_CMD % (tripped, 'unit0:mmu_shared_exit'))
        self.assertEqual(runouts, [], 'duplicate of an already-handled runout was re-processed')


class TestEventTimeIsReactorClock(unittest.TestCase):

    def tearDown(self):
        self.hh.close()

    def test_switch_sensor_stamps_the_reactor_clock(self):
        """
        The EVENTTIME a switch sensor puts on the gcode has to be comparable with
        runout_last_enable_time, which is reactor.monotonic(). Klipper's own chain gets this
        right (#receive_time is get_monotonic(), the same call reactor.monotonic uses); this
        pins it so a future "print_time correction" cannot quietly break the filter.
        """
        self.hh = session('boxturtle')
        self.hh.boot()

        emitted = []
        run_script = self.hh.gcode.run_script

        def spy(script):
            if '__MMU_SENSOR_RUNOUT' in script:
                emitted.append(script)
            return run_script(script)

        self.hh.gcode.run_script = spy

        self.hh.heat_extruder(220)
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.hh.printer.lookup_object('print_stats').set_state('printing')
        self.hh.run_gcode('MMU_PRINT_START')
        self.hh.settle(1.0)

        tripped = self.hh.reactor.monotonic()
        self.hh.sensor('mmu_exit_0').clear()
        self.hh.settle(0.5)

        self.assertEqual(len(emitted), 1, 'no runout gcode was emitted: %s' % emitted)
        eventtime = float(emitted[0].split('EVENTTIME=')[1].split()[0])
        self.assertAlmostEqual(eventtime, tripped, delta=0.01)

    def test_virtual_endstop_gates_on_reactor_time_whatever_clock_it_is_handed(self):
        """
        trigger_handler's caller may hand it MCU print_time (ADC read_time, counter
        print_time) or reactor time depending on the source. note_filament_present gates on
        the reactor clock and stamps EVENTTIME from it, so it must never receive print_time -
        the two have unrelated origins and the offset can be either sign.
        """
        self.hh = session('emu')
        self.hh.boot()
        sensor = self.hh.sensor('unit0:filament_compression').sensor

        stamps = []
        note = sensor.runout_helper.note_filament_present
        sensor.runout_helper.note_filament_present = lambda *args: (
            stamps.append(args[0]), note(*args))[1]

        sensor.trigger_handler(-234.5, True) # A print_time, as the harness models it
        self.assertEqual(len(stamps), 1)
        self.assertAlmostEqual(stamps[0], self.hh.reactor.monotonic(), delta=0.01)


if __name__ == '__main__':
    unittest.main()
