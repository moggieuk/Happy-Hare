# Happy Hare test harness - runout detection and EndlessSpool.
#
# When a spool runs out mid-print, EndlessSpool remaps the active TOOL to another gate in
# the same group and continues. High user impact when it misbehaves: the failure mode is a
# ruined print, and the code only runs at the moment you least want to debug it.
#
# WHAT MAKES A RUNOUT DISTINGUISHABLE FROM A CLOG
#
# A runout means the END of the filament has passed the gate - the gate sensor releases
# while filament is still gripped downstream. A clog means filament stopped moving but is
# still present. Happy Hare decides between them by looking at the gate sensor, so the
# model needs a filament TAIL: filament occupies [tail, tip], normally with tail at
# -infinity because a spool is attached. fil.exhaust(gate) gives it a finite tail.
#
# Without that, every simulated runout reads as "a clog/tangle has been detected and
# requires manual intervention" - which is Happy Hare being right about an impossible
# machine, not a bug.
#
# NOTE the runout announcement goes through log_error ("A runout has been detected.
# Checking for alternative gates...") even though it is informational, so these tests
# assert on outcomes rather than on an empty error list.
#
#   ./venv/bin/python -m unittest test.test_mmu_endless_spool
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.filament import TIP_PARKED

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_LOADED = 10
GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1
GATE_AVAILABLE_FROM_BUFFER = 2
TIP_AT_GATE = -40.0


class EndlessSpoolTestCase(unittest.TestCase):
    GATES = (0, 1, 2, 3)
    GROUPS = '1,1,1,1'          # every gate substitutable for every other
    ENABLE = 1

    def setUp(self):
        self.hh = session('boxturtle_test')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        for gate in self.GATES:
            self.hh.place_filament(gate, position=TIP_AT_GATE)
            self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.heat_extruder(220)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=%s ENABLE=%d'
                          % (self.GROUPS, self.ENABLE))
        self.gate_maps = self.hh.mmu.gate_maps

    def tearDown(self):
        self.hh.close()

    def load_and_run_out(self, tool=0):
        """Load a tool, exhaust its spool, then trigger runout detection."""
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=%d' % tool)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)
        gate = self.hh.mmu.gate_selected
        self.fil.exhaust(gate)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        return gate

    def loaded_gates(self):
        return [g for g in range(self.hh.mmu.num_gates)
                if self.fil.tip[g] > self.fil.layout['extruder_entry']]


class TestConfiguration(EndlessSpoolTestCase):

    def test_groups_and_enable_are_applied(self):
        self.assertTrue(self.gate_maps.endless_spool_enabled)
        self.assertEqual(list(self.gate_maps.endless_spool_groups), [1, 1, 1, 1])

    def test_state_lives_on_the_gate_map_not_the_machine_param(self):
        """
        MMU_ENDLESS_SPOOL writes gate_maps.endless_spool_enabled; the machine param
        endless_spool_enabled is only the configured default. Reading the wrong one makes
        a test look enabled when it is not.
        """
        self.assertTrue(self.gate_maps.endless_spool_enabled)
        self.assertEqual(self.hh.mmu.p.endless_spool_enabled, 0)

    def test_reset_restores_per_gate_groups(self):
        self.hh.run_gcode('MMU_ENDLESS_SPOOL RESET=1')
        groups = list(self.gate_maps.endless_spool_groups)
        self.assertEqual(len(set(groups)), len(groups),
                         'reset should put each gate in its own group')

    def test_wrong_length_group_list_is_rejected(self):
        before = list(self.gate_maps.endless_spool_groups)
        errors_before = len(self.hh.errors)
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,1')      # 4 gates, 2 groups
        self.assertGreater(len(self.hh.errors), errors_before)
        self.assertEqual(list(self.gate_maps.endless_spool_groups), before)


class TestRunoutSwapsGate(EndlessSpoolTestCase):

    def test_runout_remaps_the_tool_to_another_gate(self):
        """
        The headline behaviour. The TOOL stays T0 - the slicer keeps asking for T0 - but
        it now resolves to a different gate.
        """
        original = self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.tool_selected, 0, 'the tool must not change')
        self.assertNotEqual(self.hh.mmu.gate_selected, original)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_the_new_gate_is_actually_loaded(self):
        """
        The runout swap also ejects the exhausted lane's remnant. _eject_from_gate homes
        BACKWARD against the entry switch until it releases (mmu_filament_movement.py:920-929)
        - "so we don't over eject if the user pulls out filament" - so the remnant ends at
        the switch, out of the gear, rather than at the park where it would still be gripped.
        """
        original = self.load_and_run_out(tool=0)
        new_gate = self.hh.mmu.gate_selected
        self.assertEqual(self.loaded_gates(), [new_gate])
        self.assertAlmostEqual(self.fil.tip[original], self.fil.layout['mmu_entry'], places=1)
        self.assertFalse(self.hh.sensor('mmu_entry_%d' % original).present,
                         'the ejected remnant must have cleared the entry switch')

    def test_the_exhausted_gate_is_marked_empty(self):
        original = self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.gate_status[original], GATE_EMPTY)

    def test_the_replacement_gate_stays_available(self):
        self.load_and_run_out(tool=0)
        self.assertEqual(self.hh.mmu.gate_status[self.hh.mmu.gate_selected],
                         GATE_AVAILABLE)

    def test_the_runout_is_announced(self):
        self.load_and_run_out(tool=0)
        announced = ' '.join(self.hh.errors + self.hh.console).lower()
        self.assertIn('runout', announced)
        self.assertIn('endlessspool', announced.replace(' ', ''))

    def test_gate_zero_can_be_used_as_the_designated_waste_gate(self):
        self.hh.mmu.p.endless_spool_eject_gate = 0

        original = self.load_and_run_out(tool=1)

        self.assertEqual(original, 1, 'test must run out a gate other than the waste gate')
        self.assertTrue(any('designated waste gate 0' in msg for msg in self.hh.console))
        self.assertNotEqual(self.hh.mmu.gate_selected, 0,
                            'replacement filament must be selected after waste ejection')

    def test_successive_runouts_walk_through_the_group(self):
        """
        Three spools in a row. Each runout must find the next available gate rather than
        re-trying an exhausted one - the failure mode being an infinite loop.
        """
        used = []
        for _ in range(3):
            used.append(self.load_and_run_out(tool=0))
        self.assertEqual(len(set(used)), 3, 'each runout should consume a distinct gate')
        for gate in used:
            self.assertEqual(self.hh.mmu.gate_status[gate], GATE_EMPTY)


class TestRunoutWithoutAlternatives(EndlessSpoolTestCase):
    GROUPS = '1,2,3,4'          # every gate in its own group: no substitutes

    def test_runout_with_no_group_partner_needs_intervention(self):
        """
        Gate 0 is alone in group 1, so there is nowhere to go. HH must stop and say so
        rather than silently picking an unrelated gate.
        """
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'must not remap outside the group')
        # HH leaves the filament where it is rather than unloading: there is nothing
        # behind it to pull back, so the run-out remainder stays in the extruder for the
        # user to deal with.
        self.assertEqual(self.loaded_gates(), [0],
                         'no gate from another group should have been loaded')

    def test_gates_in_other_groups_are_untouched(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        for gate in (1, 2, 3):
            with self.subTest(gate=gate):
                self.assertAlmostEqual(self.fil.tip[gate], TIP_PARKED, places=1)


class TestEndlessSpoolDisabled(EndlessSpoolTestCase):
    ENABLE = 0

    def test_runout_without_endless_spool_needs_intervention(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        self.assertTrue(self.hh.errors)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'no remap should have happened')


class TestClogVersusRunout(EndlessSpoolTestCase):
    """
    The distinction HH has to make, and the reason the model needs a tail. Filament still
    present at the gate means a clog - a jam needing a human. Filament gone means a runout
    - swap and carry on. Getting this backwards either strands a print or feeds a jam.
    """

    def test_filament_still_present_is_treated_as_a_clog(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertTrue(self.hh.sensor('mmu_exit_0').present,
                        'precondition: filament still spans the gate sensor')
        self.hh.run_gcode('MMU_TEST_RUNOUT')
        reported = ' '.join(self.hh.errors).lower()
        self.assertIn('clog', reported)
        self.assertEqual(self.hh.mmu.gate_selected, 0, 'a clog must not remap')

    def test_exhausting_the_spool_clears_the_gate_sensors(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.hh.settle()
        self.assertFalse(self.hh.sensor('mmu_exit_0').present)
        self.assertFalse(self.hh.sensor('mmu_entry_0').present)

    def test_filament_downstream_is_still_gripped_after_exhaust(self):
        """A runout is not "filament vanished" - the tip is still in the extruder."""
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.fil.exhaust(0)
        self.assertGreater(self.fil.tip[0], self.fil.layout['extruder_entry'])

    def test_refill_restores_an_attached_spool(self):
        self.fil.exhaust(0)
        self.hh.settle()
        self.fil.refill(0)
        self.hh.settle()
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.assertTrue(self.hh.sensor('mmu_entry_0').present)


class TestStaleGateStatusOnLoad(EndlessSpoolTestCase):
    """
    A gate can be empty in reality while gate_status still says otherwise: nothing corrects
    status between bootup and a load. The EndlessSpool-on-load trigger needs an exact
    GATE_EMPTY, so a gate sitting at GATE_UNKNOWN silently disables the whole feature and the
    load walks into an empty gate - with a correct group, endless_spool_enabled and
    endless_spool_on_load all in place. Observed on a real machine 2026-09-13.

    The fix refreshes the target gate from its own sensors immediately before the trigger.
    """

    GROUPS = '1,1,2,2'          # gates 0+1 substitutable, 2+3 a separate group

    def setUp(self):
        super().setUp()
        # endless_spool_on_load defaults to 0; the trigger under test is behind it.
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1')
        # Empty gate 0 without letting Happy Hare see it. A sensor event would call
        # set_gate_status(GATE_EMPTY) and fix the status for us, which is precisely the
        # correction that does NOT happen when filament is removed while the printer is off
        # or a gate check leaves the status stale.
        with self.hh.quiet_sensors():
            self.fil.remove(0)
        self.hh.settle()
        # One call, not two: MMU_GATE_MAP's TEMP defaults to default_extruder_temp rather
        # than to the gate's current value (mmu_gate_map.py:288), so a later call that omits
        # TEMP silently resets it and this test would be measuring that instead.
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=%d '
                          'MATERIAL=PLA TEMP=220 SPOOLID=3 QUIET=1' % GATE_UNKNOWN)
        self.assertFalse(self.hh.sensor('mmu_entry_0').present,
                         'precondition: gate 0 is physically empty')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_UNKNOWN,
                         'precondition: and Happy Hare has not noticed')
        self.assertEqual(self.hh.mmu.gate_temperature[0], 220,
                         'precondition: gate 0 carries its spool temperature')

    def test_load_remaps_away_from_an_unknown_but_empty_gate(self):
        """The headline: T0 maps to an empty gate, so it should resolve to its group partner."""
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 1)
        self.assertEqual(self.hh.mmu.gate_selected, 1)
        self.assertEqual(self.hh.mmu.tool_selected, 0, 'the tool must not change')
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_LOADED)

    def test_the_corrected_gate_is_marked_empty(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)

    def test_gate_metadata_survives_the_correction(self):
        """
        Correcting availability must not destroy filament identity. Nothing was ejected from
        gate 0 - a sensor merely reported the lane empty - so its material, temperature and
        spool id are still the best record of what belongs there. Losing them makes the next
        load heat to the fallback temperature instead of the spool's.
        """
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.hh.mmu.gate_material[0], 'PLA')
        self.assertEqual(self.hh.mmu.gate_temperature[0], 220)
        self.assertEqual(self.hh.mmu.gate_spool_id[0], 3)

    def test_only_the_target_gate_is_revalidated(self):
        """Loading one tool must not rewrite the status of gates it was not asked about."""
        before = list(self.hh.mmu.gate_status)
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        after = self.hh.mmu.gate_status
        for gate in (1, 2, 3):
            with self.subTest(gate=gate):
                self.assertEqual(after[gate], before[gate])


class TestUnknownGateThatActuallyHasFilament(EndlessSpoolTestCase):
    """
    The other half of the same question. GATE_UNKNOWN means "nobody has checked", not "empty",
    so a gate that turns out to be full must be loaded normally - never remapped away from.
    """

    GROUPS = '1,1,2,2'

    def setUp(self):
        super().setUp()
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1')
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=%d QUIET=1' % GATE_UNKNOWN)
        self.assertTrue(self.hh.sensor('mmu_entry_0').present,
                        'precondition: gate 0 still holds filament')

    def test_a_full_unknown_gate_is_not_remapped(self):
        self.hh.run_gcode('MMU_CHANGE_TOOL TOOL=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0, 'must not remap away from a good gate')
        self.assertEqual(self.hh.mmu.gate_selected, 0)


class TestNoGateSensors(unittest.TestCase):
    """
    Machines with no per-gate sensors must be unaffected. check_gate_sensor returns None when
    a sensor is absent, so neither correction branch fires and status is left exactly as the
    user set it. 3D Chameleon has no entry or per-gate exit sensors, which is the case to prove.

    This calls validate_gate_status directly rather than driving a toolchange: the point under
    test is the sensor-absent path itself, and a Chameleon toolchange would drag in rotary
    selector motion that has nothing to do with it.
    """

    def setUp(self):
        self.hh = session('chameleon')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')

    def tearDown(self):
        self.hh.close()

    def test_validation_is_a_no_op_without_sensors(self):
        maps = self.hh.mmu.gate_maps
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 MATERIAL=PLA TEMP=220 QUIET=1')
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=%d QUIET=1' % GATE_UNKNOWN)

        maps.validate_gate_status([0], clear_attributes=False)

        self.assertEqual(self.hh.mmu.gate_status[0], GATE_UNKNOWN)
        self.assertEqual(self.hh.mmu.gate_material[0], 'PLA')
        self.assertEqual(self.hh.errors, [])


class TestEndlessSpoolDestinationSelection(EndlessSpoolTestCase):
    """
    The trigger and the destination test disagree about what GATE_UNKNOWN means: the trigger
    requires == GATE_EMPTY (strict), while get_next_endless_spool_gate accepts != GATE_EMPTY
    (lenient). So an unchecked gate cannot trigger a switch but can be chosen as the gate to
    switch to. Fixing that is a separate change; this records the asymmetry so it self-heals.
    """

    GROUPS = '1,1,2,2'

    @unittest.expectedFailure
    def test_an_unknown_gate_is_not_chosen_as_the_destination(self):
        self.hh.run_gcode('MMU_GATE_MAP GATE=1 AVAILABLE=%d QUIET=1' % GATE_UNKNOWN)
        next_gate, _ = self.gate_maps.get_next_endless_spool_gate(0, 0)
        self.assertEqual(next_gate, -1,
                         'an unchecked gate should not be presented as a known-good target')


class TestGateCheckSubstitutesAnEmptyGate(EndlessSpoolTestCase):
    """
    Print start runs MMU_CHECK_GATE before any load. It used to mark a required tool's empty gate
    EMPTY and then raise, pausing the print - so the on-load remap never got its turn and a correctly
    configured EndlessSpool group could not help at the one moment users most expect it to.

    is_in_print is forced rather than driving a real print: the raise is guarded on it, and a print
    needs a homed toolhead and a heated bed the harness has no reason to provide.
    """

    GROUPS = '1,1,2,2'

    def setUp(self):
        super().setUp()
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1')
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        with self.hh.quiet_sensors():
            self.fil.remove(0)
        self.hh.settle()

    def test_gate_check_remaps_instead_of_pausing_the_print(self):
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 1, 'T0 should have been substituted to gate 1')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)
        self.assertFalse([e for e in self.hh.errors if 'marked EMPTY' in e],
                         'the print must not be paused when a substitute gate exists')

    def test_the_substitute_gate_is_itself_verified(self):
        """Remapping to a gate nobody checked would only move the failure later into the print."""
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.hh.mmu.gate_status[1], GATE_AVAILABLE)

    def test_still_pauses_when_the_whole_group_is_empty(self):
        with self.hh.quiet_sensors():
            self.fil.remove(1)
        self.hh.settle()
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertTrue([e for e in self.hh.errors if 'marked EMPTY' in e],
                        'with no alternative left the print must still be stopped')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)
        self.assertEqual(self.hh.mmu.gate_status[1], GATE_EMPTY)

    def test_no_substitution_without_a_group_partner(self):
        self.hh.run_gcode('MMU_ENDLESS_SPOOL GROUPS=1,2,3,4')
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0, 'must not remap outside the group')
        self.assertTrue([e for e in self.hh.errors if 'marked EMPTY' in e])


class TestBufferedStatusSurvivesTheRefresh(EndlessSpoolTestCase):
    """
    The pre-load refresh now runs before every tool load, so anything it discards is discarded
    every time. A gate parked with filament still covering its exit sensor keeps
    GATE_AVAILABLE_FROM_BUFFER, and that status is what selects gear_from_filament_buffer_speed and
    _accel for the load. The exit sensor proves filament is present, not where it came from, so
    confirming presence must not downgrade a buffered gate to plain GATE_AVAILABLE.
    """

    def setUp(self):
        super().setUp()
        # Park gate 0's filament forward, still covering its exit sensor - the configuration this
        # affects. place_filament is quiet, so no insert event rewrites the status underneath us.
        self.hh.place_filament(0, position=self.fil.position('mmu_exit_0') + 5.0)
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=%d QUIET=1' % GATE_AVAILABLE_FROM_BUFFER)
        self.assertTrue(self.hh.sensor('mmu_exit_0').present,
                        'precondition: the exit sensor is covered')
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE_FROM_BUFFER,
                         'precondition: the gate is marked as buffered')

    def test_the_refresh_preserves_buffered_availability(self):
        self.gate_maps.validate_gate_status([0], clear_attributes=False)
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE_FROM_BUFFER)

    def test_an_unknown_gate_still_becomes_available(self):
        """The upgrade direction must keep working - max() must not freeze a stale lower status."""
        self.hh.run_gcode('MMU_GATE_MAP GATE=0 AVAILABLE=%d QUIET=1' % GATE_UNKNOWN)
        self.gate_maps.validate_gate_status([0], clear_attributes=False)
        self.assertEqual(self.hh.mmu.gate_status[0], GATE_AVAILABLE)


class TestGateCheckParkingFailureIsNotAnEmptyGate(EndlessSpoolTestCase):
    """
    "Filament was found but could not be parked" is not "the gate is empty", and the two need
    opposite recovery. The check loads filament to prove it exists, then unloads it back to the park
    so another gate can be selected safely; both used to share one error handler. Substituting on a
    parking failure would select and load a second gate while the first gate's filament is still
    somewhere in the path - on a moving-selector machine, that is selector motion across filament.

    Only a pickup failure may authorize substitution.
    """

    GROUPS = '1,1,2,2'

    def setUp(self):
        super().setUp()
        self.hh.run_gcode('MMU_TEST_CONFIG endless_spool_on_load=1')
        self.hh.run_gcode('MMU_TEST_CONFIG test_force_in_print=1')
        # Every gate keeps its filament: gate 0 is NOT empty. The only fault injected is that
        # parking it again fails.
        from extras.mmu.mmu_utils import MmuError  # deferred: needs the fake klippy tree booted

        self.checked = []
        real_unload = self.hh.mmu._unload_gate

        def failing_unload(*args, **kwargs):
            self.checked.append(self.hh.mmu.gate_selected)
            if self.hh.mmu.gate_selected == 0:
                raise MmuError("simulated parking failure")
            return real_unload(*args, **kwargs)

        self.hh.mmu._unload_gate = failing_unload
        self.addCleanup(setattr, self.hh.mmu, '_unload_gate', real_unload)

    def test_a_parking_failure_stops_the_check(self):
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertTrue(self.hh.errors, 'the failure must be reported, not swallowed')

    def test_a_parking_failure_does_not_remap(self):
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertEqual(self.gate_maps.ttg_map[0], 0,
                         'T0 must stay on gate 0 - its filament is unaccounted for')

    def test_no_other_gate_is_touched(self):
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertNotIn(1, self.checked,
                         'gate 1 must not be selected while gate 0 is unparked')
        self.assertEqual(self.loaded_gates(), [],
                         'no gate should have been loaded past the extruder')

    def test_the_gate_is_not_marked_empty(self):
        """It demonstrably has filament - _load_gate succeeded. Calling it empty is a lie that
        would also let the next load remap away from a perfectly good gate."""
        self.hh.run_gcode('MMU_CHECK_GATE TOOLS=0')
        self.assertNotEqual(self.hh.mmu.gate_status[0], GATE_EMPTY)


if __name__ == '__main__':
    unittest.main()
