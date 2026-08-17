# Happy Hare test harness - PHYSICAL selectors.
#
# Everything else in the suite runs on a VirtualSelector, where selecting a gate is
# bookkeeping. This file covers the machines that actually move a carriage, which until now
# could not move filament at all in the harness: selector homing failed with "No trigger on
# mmu_sel_home after full movement" because selector endstops were routed through the
# gate-filament model, which has no selector axis. See test/hh/selector.py.
#
# THREE SELECTOR FAMILIES, and they disagree about everything:
#
#   tradrack / ERCF   LinearServoSelector   one home switch; gates at calibrated offsets;
#                                           a servo grips and releases
#   ViViD             IndexedSelector       no home switch; one index switch PER gate,
#                                           visited in selector_gate_order
#   3D Chameleon      RotarySelector        one home switch, gates at calibrated offsets like
#                                           the linear family - but NO servo, so releasing
#                                           means driving to the OPPOSING gate's offset
#
# Built on tradrack first, deliberately: it is single-unit and has no encoder, so a failure
# there has one candidate cause. ercf_vvd then adds units, an encoder, and the second
# selector type on top of a known-good base.
#
#   ./venv/bin/python -m unittest test.test_mmu_selector
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session
from test.hh.profiles import PROFILES, Profile, clone_across_units

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
FILAMENT_POS_UNKNOWN = -1
TOOL_GATE_UNKNOWN = -1
TIP_AT_GATE = -40.0             # past the entry switch: where a user's push leaves it

# mmu_constants.py:209-211, mirrored here for the same reason FILAMENT_POS_* is
FILAMENT_RELEASE_STATE = 0
FILAMENT_DRIVE_STATE = 1


# The installer exposes LinearMultiGearSelector only for custom machines. MMB 2.0 has
# enough gear outputs but reuses its normal selector pins for those extra gears, so this
# fixture supplies a separate selector driver explicitly, as a real custom build must.
LINEAR_MULTI_GEAR = Profile(
    'linear_multi_gear',
    syms={
        'MMU_CUSTOM': True,
        'CHOICE_SELECTOR_TYPE_LINEAR_MULTI_GEAR_SELECTOR': True,
        'BOARD_TYPE_MMB_2_0': True,
        'MMU_HAS_SENSOR_SHARED_EXIT': True,
        'PIN_SELECTOR_STEP': 'unit0:PA8',
        'PIN_SELECTOR_DIR': 'unit0:PA9',
        'PIN_SELECTOR_ENABLE': 'unit0:PA10',
        'PIN_SELECTOR_UART': 'unit0:PA11',
        'PIN_SELECTOR_ENDSTOP': 'unit0:PA12',
    },
    description='custom four-gate LinearMultiGearSelector with a separate selector driver')

MACRO_SELECTOR = Profile(
    'macro_selector',
    syms={
        'MMU_CUSTOM': True,
        'CHOICE_SELECTOR_TYPE_MACRO_SELECTOR': True,
        'BOARD_TYPE_MMB_2_0': True,
        'MMU_HAS_SENSOR_SHARED_EXIT': True,
    },
    description='custom four-gate MacroSelector in direct (zero-switch) mode')


class SelectorTestCase(unittest.TestCase):
    """
    A booted, homed and calibrated physical-selector machine, REBUILT PER TEST.

    Per test rather than per class on purpose: these tests move a carriage and load filament,
    so they mutate exactly the state the next one asserts on. Sharing a session made
    test_homing_succeeds fail purely because an alphabetically earlier test had driven the
    selector off home. Same reasoning as ToolchangeTestCase.
    """

    PROFILE = None
    HOME_UNITS = (0,)

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.seeded = self.hh.calibrate()
        for unit in self.HOME_UNITS:
            # MMU_HOME is per-unit, and REQUIRED to name a unit on a multi-unit machine
            self.hh.run_gcode('MMU_HOME UNIT=%d' % unit)
        self.assertEqual(self.hh.errors, [], 'homing was not clean')
        self.hh.heat_extruder(220)

    def tearDown(self):
        self.hh.close()

    def axis(self, unit_name):
        for candidate in self.hh.printer.harness_selectors:
            if candidate.unit.name == unit_name:
                return candidate
        self.fail('no selector axis for %r' % unit_name)

    def selector(self, unit_name):
        return {u.name: u for u in self.hh.mmu.mmu_machine.units}[unit_name].selector

    def load_and_unload(self, gate):
        """Preload, select, load, unload. Returns (filament_pos after each)."""
        self.hh.place_filament(gate, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=%d' % gate)
        self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
        self.hh.run_gcode('MMU_LOAD')
        loaded = self.hh.mmu.filament_pos
        self.hh.run_gcode('MMU_UNLOAD')
        return loaded, self.hh.mmu.filament_pos


class TestLinearSelector(SelectorTestCase):
    """
    tradrack: a LinearServoSelector, single unit, NO encoder. The isolated case.
    """

    PROFILE = 'tradrack'

    def test_homing_succeeds(self):
        """
        The regression test for the whole file. This used to raise
        MmuError("Homing selector failed because of blockage or malfunction") wrapping
        Klipper's "No trigger on mmu_stepper unit0_selector after full movement".
        """
        selector = self.selector('unit0')
        self.assertTrue(selector.is_homed)
        self.assertEqual(self.axis('unit0').carriage, self.axis('unit0').home_position())

    def test_selector_status_is_published_under_mmu_selector(self):
        selector = self.selector('unit0')

        self.assertEqual(self.hh.mmu.get_status(0)['selector'], selector.get_status(0))

    def test_calibration_is_seeded_from_the_machines_own_cad_table(self):
        """
        Offsets come from HH's published quick method (cad_gate0_pos + i*cad_gate_width), so
        they must match tradrack's CAD table (2.5, 17.0) rather than any number chosen here.
        """
        offsets = self.seeded['unit0']['selector_offsets']
        self.assertEqual(len(offsets), self.hh.mmu.num_gates)
        self.assertAlmostEqual(offsets[0], 2.5, places=3)
        for i in range(1, len(offsets)):
            self.assertAlmostEqual(offsets[i] - offsets[i - 1], 17.0, places=3)

    def test_selecting_a_gate_moves_the_carriage_to_its_offset(self):
        """Selection is a plain move to a calibrated offset - assert it actually arrives."""
        selector, axis = self.selector('unit0'), self.axis('unit0')
        for gate in (0, 4, 9):
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                self.assertEqual(self.hh.mmu.gate_selected, gate)
                self.assertAlmostEqual(axis.carriage, selector.selector_offsets[gate],
                                       places=3)
                self.assertEqual(self.hh.errors, [])

    def test_full_load_and_unload(self):
        """The milestone: filament movement on a physical selector."""
        loaded, unloaded = self.load_and_unload(3)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])

    def test_the_carriage_stays_put_across_a_load(self):
        """
        A load must not disturb the selector. This would catch the filament model and the
        selector axis being conflated - the failure mode the separate axis exists to prevent.
        """
        axis = self.axis('unit0')
        self.hh.run_gcode('MMU_SELECT GATE=5')
        before = axis.carriage
        self.load_and_unload(5)
        self.assertAlmostEqual(axis.carriage, before, places=3)


class TestLinearMultiGearSelector(SelectorTestCase):
    """A type-C selector is still a physical axis and must home before absolute moves."""

    PROFILE = LINEAR_MULTI_GEAR

    def test_it_retains_physical_selector_homing(self):
        selector = self.selector('unit0')
        axis = self.axis('unit0')
        self.assertTrue(selector.requires_homing)
        self.assertTrue(selector.is_homed)
        self.assertAlmostEqual(axis.carriage, axis.home_position(), places=3)

    def test_selection_uses_the_homed_coordinate_frame(self):
        selector = self.selector('unit0')
        axis = self.axis('unit0')
        self.hh.run_gcode('MMU_SELECT GATE=3')
        self.assertAlmostEqual(axis.carriage, selector.selector_offsets[3], places=3)
        self.assertEqual(self.hh.errors, [])

    def test_per_gate_drives_do_not_depend_on_virtual_selector(self):
        """Type-C gear dispatch belongs to MmuUnit, independently of selector inheritance."""
        unit = self.hh.mmu.mmu_unit(0)
        self.assertTrue(unit.multigear)
        self.assertEqual(len({id(drive) for drive in unit.drives}), unit.num_gates)
        for lgate, expected_drive in enumerate(unit.drives):
            gate = unit.logical_gate(lgate)
            self.assertIs(self.hh.mmu.drive(gate), expected_drive)

    def test_full_load_and_unload(self):
        loaded, unloaded = self.load_and_unload(2)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])


class TestServoSelector(SelectorTestCase):
    """MMX provides valid vendor angles and exercises the otherwise-unused ServoSelector."""

    PROFILE = 'mmx'
    HOME_UNITS = ()

    def test_vendor_gate_angles_are_loaded(self):
        self.assertEqual(self.selector('unit0').servo_gate_angles, [60, 0, 180, 120])

    def test_full_load_and_unload(self):
        loaded, unloaded = self.load_and_unload(2)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])


class TestMacroSelector(SelectorTestCase):
    """The generated zero-switch config is direct mode and preserves the v3 GATE contract."""

    PROFILE = MACRO_SELECTOR
    HOME_UNITS = ()

    def test_direct_mode_boots_and_passes_both_gate_numbering_schemes(self):
        selector = self.selector('unit0')
        calls = []
        self.hh.mmu.wrap_gcode_command = calls.append

        selector.select_gate(2)

        self.assertFalse(selector.binary_mode)
        self.assertEqual(calls, ['select_tool_macro GATE=2 LGATE=2'])


class TestRotarySelector(SelectorTestCase):
    """
    chameleon: a RotarySelector, and the only machine here with NO servo.

    That one missing part changes the meaning of a gate position. Everywhere else a gate has
    exactly one place the carriage belongs and gripping is a separate axis (the servo); on a
    3D Chameleon the "opposing gate" mechanism means the carriage itself expresses grip:

        grip gate g     -> selector_offsets[g]
        release gate g  -> selector_offsets[selector_release_gates[g]]   i.e. ANOTHER gate

    and the single gear motor is reversed on the gates whose filament path runs backwards
    (selector_gate_directions). Both lists are per-gate config, and both are read on every
    _grip_release, which is why they get direct assertions rather than being taken on trust.

    Gripping is LAZY: with filament_always_gripped 0, _select_gate does not grip at all
    (mmu_rotary_selector.py:194-197) and the carriage only reaches the gate's own offset when
    something asks to drive the filament.
    """

    PROFILE = 'chameleon'

    # The machine's own config, from Kconfig.3d_chameleon:48-54. Repeated here so a test failure
    # says which permutation was expected, and pinned by
    # test_the_machine_still_has_the_geometry_these_tests_assume below.
    RELEASE_GATES = (2, 3, 0, 1)
    GATE_DIRECTIONS = (1, 1, 0, 0)

    def test_the_machine_still_has_the_geometry_these_tests_assume(self):
        """
        A guard, not a behaviour test. Everything below is only interesting because release
        goes to a DIFFERENT gate and because the machine is allowed to release at all - if a
        vendor default ever changes, this fails first and says so, rather than the assertions
        quietly becoming tautologies.
        """
        selector = self.selector('unit0')
        self.assertEqual(type(selector).__name__, 'RotarySelector')
        self.assertFalse(self.hh.mmu.mmu_unit(0).filament_always_gripped,
                         'with grip forced on, the release path under test is dead code')
        self.assertEqual(tuple(selector.p.selector_release_gates), self.RELEASE_GATES)
        self.assertEqual(tuple(selector.p.selector_gate_directions), self.GATE_DIRECTIONS)
        for gate, release_gate in enumerate(self.RELEASE_GATES):
            self.assertNotEqual(gate, release_gate,
                                'gate %d releases into itself, so it proves nothing' % gate)

    def test_releasing_parks_the_carriage_at_the_opposing_gates_offset(self):
        """
        THE regression test for this class. mmu_rotary_selector.py:229 read
        `self.selector_release_gates`, but that name is a ParamSpec and so lives on `self.p`
        (the same line's neighbour at :257 gets it right) - so every release raised
        AttributeError: 'RotarySelector' object has no attribute 'selector_release_gates'.

        Reached from MMU_RELEASE, and on this machine from plain MMU_SELECT as well:
        reset_sync_gear_to_extruder calls filament_release() on the way out of every wrapped
        operation (mmu_filament_movement.py:3607), so an unfixed rotary selector cannot select
        a gate at all.

        Asserts the offset of the OPPOSING gate specifically. "The carriage moved" would pass
        with the permutation applied in the wrong direction, or ignored.
        """
        selector, axis = self.selector('unit0'), self.axis('unit0')
        for gate, release_gate in enumerate(self.RELEASE_GATES):
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                self.hh.run_gcode('MMU_RELEASE')
                self.assertEqual(selector.grip_state, FILAMENT_RELEASE_STATE)
                self.assertAlmostEqual(axis.carriage,
                                       selector.selector_offsets[release_gate], places=3,
                                       msg='gate %d should release at gate %d'
                                           % (gate, release_gate))
                self.assertEqual(self.hh.errors, [])

    def test_gripping_parks_the_carriage_at_the_gates_own_offset(self):
        """
        The other half of the pair, and what makes the release assertion above mean something:
        the two positions are genuinely different, so a release that silently behaved like a
        grip would fail one of these.
        """
        selector, axis = self.selector('unit0'), self.axis('unit0')
        for gate in range(self.hh.mmu.num_gates):
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                self.hh.run_gcode('MMU_GRIP')
                self.assertEqual(selector.grip_state, FILAMENT_DRIVE_STATE)
                self.assertAlmostEqual(axis.carriage, selector.selector_offsets[gate],
                                       places=3)
                self.assertEqual(self.hh.errors, [])

    def test_selection_alone_leaves_the_filament_released(self):
        """
        LAZY GRIP. _select_gate skips the grip entirely when the machine can release, so a
        bare MMU_SELECT must leave the carriage parked at the release position - NOT at the
        gate it just selected. This is the state a rotary machine idles in.
        """
        selector, axis = self.selector('unit0'), self.axis('unit0')
        gate = 1
        self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)

        self.assertEqual(self.hh.mmu.gate_selected, gate)
        self.assertEqual(selector.grip_state, FILAMENT_RELEASE_STATE)
        self.assertAlmostEqual(axis.carriage,
                               selector.selector_offsets[self.RELEASE_GATES[gate]], places=3)
        self.assertEqual(self.hh.errors, [])

    def test_the_gear_direction_follows_the_selected_gate(self):
        """
        One gear motor serves all four gates, so the direction pin IS the per-gate wiring:
        _grip_release hands selector_gate_directions[gate] to MmuDrive.set_gear_direction on
        every grip and every release. Gates 0-1 are reversed on this machine and 2-3 are not,
        so a direction that was ignored, or applied from the release gate instead of the
        selected one, shows up as a mismatch here.
        """
        for gate, direction in enumerate(self.GATE_DIRECTIONS):
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                stepper = self.hh.mmu.drive(gate).mmu_gear_stepper.stepper
                inverted, _original = stepper.get_dir_inverted()
                self.assertEqual(bool(inverted), bool(direction),
                                 'gate %d should drive the gear %s'
                                 % (gate, 'reversed' if direction else 'forwards'))

    def test_full_load_and_unload(self):
        """
        The milestone for this machine: filament movement with grip expressed as selector
        position. Every load and unload crosses _grip_release repeatedly, so this is the test
        that would notice the release path failing anywhere other than at a bare MMU_RELEASE.
        """
        loaded, unloaded = self.load_and_unload(1)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])


class TestRotarySelectorOnALaterUnit(SelectorTestCase):
    """
    A rotary machine whose gates do NOT start at zero.

    Every gate number crossing a unit boundary exists in two numbering schemes - machine-wide
    and unit-local - and the rotary selector works almost entirely in local ones, because its
    per-gate config lists (offsets, release gates, directions) are local arrays. Handing one of
    those local indexes to an API that wants a machine gate is the mistake this class exists to
    catch, and it is invisible on the shipped chameleon profile: single unit, first gate zero,
    so the two numberings are the same number and every confusion is an identity.

    Two chameleons rather than a real machine. Nobody sells this, but the shape is what matters
    and clone_across_units is the sanctioned way to get it - see its note about why deriving a
    single-unit profile with unit names injected as params is quietly wrong.
    """

    PROFILE = clone_across_units(
        'two_chameleons', PROFILES['chameleon'], ('unit0', 'unit1'),
        description='two 3D Chameleons - a rotary selector on a unit that does not start at gate 0')
    HOME_UNITS = (0, 1)

    UNIT1_FIRST_GATE = 4

    def unit1(self):
        return self.hh.mmu.mmu_machine.get_mmu_unit_by_index(1)

    def test_the_second_unit_really_does_start_elsewhere(self):
        """
        A guard. If the two units ever collapse to the same numbering, everything below stops
        distinguishing a local index from a machine gate and passes for the wrong reason.
        """
        unit1 = self.unit1()
        self.assertEqual(unit1.first_gate, self.UNIT1_FIRST_GATE)
        self.assertEqual(type(unit1.selector).__name__, 'RotarySelector')
        self.assertNotEqual(unit1.logical_gate(1), 1, 'local and machine gates still coincide')

    def test_gripping_on_the_later_unit_reaches_its_own_drive(self):
        """
        The regression. _grip_release works in local gates but set_gear_direction is reached
        through drive_obj(), which takes a machine gate - so the local index was being resolved
        against the wrong unit. On the shipped single-unit profile that resolved to the right
        stepper by coincidence; here it names a gate unit1 does not own.
        """
        for lgate in range(self.hh.mmu.mmu_unit(self.UNIT1_FIRST_GATE).num_gates):
            gate = self.UNIT1_FIRST_GATE + lgate
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                self.hh.run_gcode('MMU_GRIP')
                self.assertEqual(self.selector('unit1').grip_state, FILAMENT_DRIVE_STATE)
                self.assertEqual(self.hh.errors, [])

    def test_the_gear_direction_is_read_from_the_units_own_table(self):
        """
        selector_gate_directions is a LOCAL array, so gate 4 must take entry 0. Reading it with
        the machine gate would run off the end; resolving the drive with the local one grabs
        another unit's stepper. Only a unit that does not start at zero can tell those apart.
        """
        selector = self.selector('unit1')
        for lgate, direction in enumerate(selector.p.selector_gate_directions):
            gate = self.UNIT1_FIRST_GATE + lgate
            with self.subTest(gate=gate):
                self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
                stepper = self.hh.mmu.drive(gate).mmu_gear_stepper.stepper
                inverted, _original = stepper.get_dir_inverted()
                self.assertEqual(bool(inverted), bool(direction))
                self.assertEqual(self.hh.errors, [])

    def test_calibration_takes_a_machine_gate_not_a_local_one(self):
        """
        The command's GATE was ranged against the unit's gate COUNT but used as a machine gate,
        so on a later unit the accepted values addressed another unit's gates entirely - and
        the measured offset landed in the wrong slot. It now ranges machine-wide and rejects a
        gate the named unit does not own, matching what the linear selector already did.
        """
        before = list(self.selector('unit1').selector_offsets)

        with self.assertRaises(Exception) as ctx:
            self.hh.run_gcode('MMU_CALIBRATE_ROTARY_SELECTOR UNIT=1 GATE=0 QUICK=1 SAVE=0')
        self.assertIn('not managed by unit1', str(ctx.exception))
        self.assertEqual(self.selector('unit1').selector_offsets, before,
                         'a foreign gate wrote into this unit\'s offsets')

    def test_calibrating_a_gate_the_unit_owns_is_accepted(self):
        """The other half: the machine-wide numbers in unit1's own range must still work."""
        self.hh.run_gcode('MMU_CALIBRATE_ROTARY_SELECTOR UNIT=1 GATE=%d QUICK=1 SAVE=0'
                          % self.UNIT1_FIRST_GATE)
        self.assertEqual(self.hh.errors, [])


class TestSelectorCalibration(unittest.TestCase):
    """
    Happy Hare's OWN MMU_CALIBRATE_SELECTOR, run for real against an UNSEEDED machine.

    This used to be impossible. measure_to_home() reports
    (trig_mcu_pos - init_mcu_pos) * step_dist (extras/mmu_stepper.py:414-459), and
    rail.home() rebases the axis to `forcepos` immediately beforehand - so while the harness
    read the carriage position off the stepper coordinate, every gate measured the same
    number, the homing SEARCH distance (170.5mm on tradrack), and every calibration was
    rejected as "more than the anticipated maximum". The carriage is tracked now; see the
    note at the top of test/hh/selector.py.

    Deliberately NOT a SelectorTestCase: the whole point is to start uncalibrated.
    """

    PROFILE = 'tradrack'
    CAD_GATE0 = 2.5             # tradrack's own cad_gate0_pos / cad_gate_width
    CAD_WIDTH = 17.0

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot()                              # no calibrate=, no seeding
        self.axis = self.hh.printer.harness_selectors[0]
        self.selector = self.axis.selector

    def tearDown(self):
        self.hh.close()

    def saved_offsets(self):
        from extras.mmu.mmu_constants import VARS_MMU_SELECTOR_OFFSETS
        return self.selector.var_manager.get(VARS_MMU_SELECTOR_OFFSETS, None,
                                             namespace='unit0')

    def test_a_gate_measures_the_distance_the_carriage_actually_travelled(self):
        """
        Each gate must report ITS OWN offset. Two gates, because one could still pass with a
        constant - which is exactly how this failed before.
        """
        for gate in (0, 4, 9):
            with self.subTest(gate=gate):
                self.axis.place(self.CAD_GATE0 + gate * self.CAD_WIDTH)
                self.hh.run_gcode('MMU_CALIBRATE_SELECTOR UNIT=0 GATE=%d' % gate)
                self.assertAlmostEqual(self.selector.selector_offsets[gate],
                                       self.CAD_GATE0 + gate * self.CAD_WIDTH, places=1)

    def test_a_calibrated_gate_is_persisted_to_mmu_vars(self):
        """Calibration is only worth anything if it survives into mmu_vars.cfg."""
        self.axis.place(self.CAD_GATE0)
        self.hh.run_gcode('MMU_CALIBRATE_SELECTOR UNIT=0 GATE=0')
        saved = self.saved_offsets()
        self.assertIsNotNone(saved, 'nothing was written to mmu_vars.cfg')
        self.assertAlmostEqual(saved[0], self.CAD_GATE0, places=1)

    def test_auto_calibration_derives_every_gate_from_two_measurements(self):
        """
        AUTO=1 measures gate 0, rams the far end of travel, measures back, and interpolates.
        It needs BOTH halves of the model: the tracked carriage for the two homing moves, and
        the travel_max clamp for the ram - without the clamp step 3 reports a length the
        machine does not have.
        """
        self.axis.place(self.CAD_GATE0)             # as AUTO=1 requires the user to do
        self.hh.run_gcode('MMU_CALIBRATE_SELECTOR UNIT=0 AUTO=1')
        offsets = self.selector.selector_offsets
        self.assertEqual(len(offsets), self.hh.mmu.num_gates)
        for i, offset in enumerate(offsets):
            self.assertAlmostEqual(offset, self.CAD_GATE0 + i * self.CAD_WIDTH, places=1)

    def test_a_carriage_in_the_wrong_place_is_still_rejected(self):
        """
        The harness must not have made calibration unconditionally succeed. Parked at the far
        gate, gate 0's measurement genuinely exceeds its CAD maximum and HH must refuse it.
        """
        self.axis.place(self.axis.travel_max)
        self.hh.run_gcode('MMU_CALIBRATE_SELECTOR UNIT=0 GATE=0')
        # handle_ready seeds the variable with -1 per gate, which IS the uncalibrated value
        self.assertEqual(self.saved_offsets(), [-1] * self.hh.mmu.num_gates,
                         'a bogus measurement was saved')


class TestStepperPositionSemantics(unittest.TestCase):
    """
    The invariant the whole calibration fix rests on: in the fake, redefining the coordinate
    origin and actually moving are DIFFERENT operations. Real Klipper gets this for free from
    step generation; the fake has none, so it is asserted here instead.
    """

    def setUp(self):
        self.hh = session('tradrack')
        self.hh.boot()
        self.stepper = self.hh.printer.harness_selectors[0].stepper.get_steppers()[0]

    def tearDown(self):
        self.hh.close()

    def test_set_position_does_not_register_as_movement(self):
        before = self.stepper.get_mcu_position()
        self.stepper.set_position([self.stepper.get_commanded_position() + 37., 0., 0., 0.])
        self.assertEqual(self.stepper.get_mcu_position(), before)

    def test_note_motion_does_register_as_movement(self):
        before = self.stepper.get_mcu_position()
        self.stepper.harness_note_motion(37.)
        travelled = ((self.stepper.get_mcu_position() - before)
                     * self.stepper.get_step_dist())
        self.assertAlmostEqual(travelled, 37., places=3)


class TestMultiUnitSelectors(SelectorTestCase):
    """
    ercf_vvd: two units, two selector classes, and an encoder on one of them.
    """

    PROFILE = 'ercf_vvd'
    HOME_UNITS = (0, 1)

    def test_both_selector_types_coexist(self):
        self.assertEqual(type(self.selector('unit0')).__name__, 'LinearServoSelector')
        self.assertEqual(type(self.selector('unit1')).__name__, 'IndexedSelector')

    def test_only_the_linear_selector_needs_seeded_offsets(self):
        """
        IndexedSelector marks itself calibrated at handle_ready
        (mmu_indexed_selector.py:137-140), so seeding it would be meaningless. unit1 still
        needs a bowden length, which is a per-unit property rather than a selector one.
        """
        self.assertIn('selector_offsets', self.seeded['unit0'])
        self.assertNotIn('selector_offsets', self.seeded['unit1'])
        self.assertIn('bowden_length', self.seeded['unit1'])

    def test_indexed_selector_switches_follow_selector_gate_order(self):
        """
        THE off-by-one trap. The ViViD visits its gates 0, 3, 1, 2 - so physical slot 1 holds
        gate 3, not gate 1. A model that assumed slot == gate would still 'work' for gate 0
        and fail silently for the rest.
        """
        axis = self.axis('unit1')
        order = list(self.selector('unit1').gate_sequence)
        self.assertEqual(order, [0, 3, 1, 2], 'profile no longer has the interesting order')

        positions = axis.gate_positions()
        self.assertEqual(len(positions), len(order))
        spacing = sorted(positions.values())[1] - sorted(positions.values())[0]
        for slot, lgate in enumerate(order):
            name = self.selector('unit1')._get_gate_endstop_name(lgate)
            self.assertAlmostEqual(positions[name], slot * spacing, places=3,
                                   msg='gate %d should sit in slot %d' % (lgate, slot))

    def test_indexed_selector_rejects_a_missed_target_index(self):
        """A missed sensor must not turn into a successful logical gate change."""
        selector = self.selector('unit1')
        self.hh.run_gcode('MMU_SELECT GATE=9')

        def miss_index(*args, **kwargs):
            raise self.hh.printer.command_error('simulated missed selector index')

        selector.selector_stepper.do_homing_move = miss_index
        self.hh.run_gcode('MMU_SELECT GATE=10')

        self.assertEqual(self.hh.mmu.gate_selected, -1)
        self.assertEqual(selector.lgate_selected, 0,
                         'the selector cache must retain the last physically located gate')
        self.assertTrue(any('Failed to locate selector index for gate 10' in error
                            for error in self.hh.errors))

    def test_indexed_selector_wraps_through_zero(self):
        """The fake ViViD ring must not turn its final index into a linear hard stop."""
        for gate in (9, 10, 11, 12):
            self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)

        self.assertEqual(self.hh.mmu.gate_selected, 12, self.hh.errors)
        self.assertEqual(self.selector('unit1').lgate_selected, 3)
        self.assertEqual(self.hh.errors, [])

    def test_unit0_loads_and_unloads(self):
        """
        Gate 2 is unit0: LinearServoSelector WITH an encoder, so this is the path that needs
        tip forming to actually move the extruder.
        """
        loaded, unloaded = self.load_and_unload(2)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])

    def test_unit1_loads_and_unloads(self):
        """
        Gate 10 is unit1: IndexedSelector, no encoder. Separate test rather than a subTest
        because a load mutates the machine and the two must not share one.
        """
        loaded, unloaded = self.load_and_unload(10)
        self.assertEqual(loaded, FILAMENT_POS_LOADED)
        self.assertEqual(unloaded, FILAMENT_POS_UNLOADED)
        self.assertEqual(self.hh.errors, [])

    def test_a_unit_scoped_sensor_ignores_the_other_units_filament(self):
        """
        unit0's shared-exit switch used to read TRIGGERED whenever unit1 had filament loaded,
        because a sensor with no _<gate> suffix fell through to "any gate on the machine".
        Every one of unit0's gates is empty here.
        """
        model = self.hh.filament()
        self.hh.run_gcode('MMU_SELECT GATE=10')
        self.hh.place_filament(10, position=model.layout['toolhead'] + 20.0)

        unit0 = {u.name: u for u in self.hh.mmu.mmu_machine.units}['unit0']
        for gate in range(unit0.first_gate, unit0.first_gate + unit0.num_gates):
            self.hh.place_filament(gate, position=-10000.0)

        self.assertTrue(model.triggered('unit1:mmu_shared_exit'))
        self.assertFalse(model.triggered('unit0:mmu_shared_exit'),
                         'unit0 sees unit1 filament - gates_visible_to() is not scoping')

    def test_the_extruder_entry_sensor_is_driven_by_the_model(self):
        """
        Not selector-specific, but found here and easy to regress. HH registers the
        extruder-entry switch as plain 'extruder', so without an 'extruder' layout alias it is
        never bound and reads EMPTY forever - which HH reports as
        "Extruder sensor reports no filament but toolhead sensor is still triggered"
        the moment the toolhead switch trips.
        """
        model = self.hh.filament()
        extruder = [n for n in self.hh.sensors() if n.split(':')[-1] == 'extruder']
        self.assertTrue(extruder, 'profile no longer has an extruder-entry sensor')
        for name in extruder:
            self.assertIsNotNone(model.position(name),
                                 '%s is not owned by the filament model' % name)
            self.assertIn(name, model.sensor_names(),
                          '%s is not bound, so it can never read triggered' % name)

    def test_recover_reports_unloaded_for_a_forward_parked_exit_sensor(self):
        """
        unit1's gate_homing_endstop is mmu_exit with gate_parking_distance = +10 (a
        forward park, not a retract), so a properly parked gate leaves the exit switch
        covered rather than clear. recover_filament_pos's gate-parked branch used to
        also require filament_detected, which is computed from
        get_all_sensors_for_gate() - and that deliberately excludes this exact sensor's
        position when parking is forward (mmu_sensor_manager.py's _get_sensors, "only
        valid if is not usually triggered i.e. parking retract"), so the requirement
        could never be satisfied and MMU_RECOVER concluded IN_BOWDEN for a perfectly
        normal parked gate.
        """
        model = self.hh.filament()
        self.hh.run_gcode('MMU_SELECT GATE=10')
        self.hh.place_filament(10, position=model.layout['mmu_exit'] + 10.0)
        self.assertTrue(model.triggered('mmu_exit_10'))

        self.hh.run_gcode('MMU_RECOVER')

        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

        # gate_status is left GATE_EMPTY by this path (filament_detected excludes
        # a forward-parked, non-active-endstop exit sensor - see the docstring
        # above), so the trailing state text correctly reads EMPTY: that text is
        # always gate_status's word, never second-guessed by a sensor. The exit
        # sensor's own trigger is still drawn in the fill regardless, so the two
        # facts - "gate_map says empty" and "something is physically detected" -
        # are both visible at once rather than one hiding the other.
        visual = self.hh.mmu.get_filament_position_string()
        self.assertIn('◉■■◉', visual)  # entry triggered, gap fully filled, exit triggered
        self.assertIn('EMPTY', visual)


class TestPersistedPositionRestore(unittest.TestCase):
    """
    A physical selector must come back from a reboot knowing where it is, so a printer that was
    running yesterday needs no re-home. Two records say where the carriage is:

      mmu_<unit>_selector_last_pos   the raw position (PRIMARY)
      mmu_state_gate_selected        the gate it corresponds to (SECONDARY)

    They are written and cleared TOGETHER (PhysicalSelector._invalidate_persisted_position), and
    that pairing is what makes the gate safe to fall back on: the gate can only be trusted when
    last_pos was never RECORDED - an upgrade, or a unit rename that orphaned the namespaced var -
    never when it was deliberately INVALIDATED by a motors-off.

    Sessions are built per test rather than shared: is_homed is sticky, and the whole point of
    these tests is what a FRESH klippy:ready decides.
    """

    GATE = 3    # A gate that is neither the first nor the bypass, on unit0 of both profiles

    def tearDown(self):
        if getattr(self, 'hh', None) is not None:
            self.hh.close()

    def boot(self, profile='tradrack', **kwargs):
        self.hh = session(profile)
        self.hh.boot(**kwargs)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        return self.hh

    def selector(self, unit_name='unit0'):
        return {u.name: u for u in self.hh.mmu.mmu_machine.units}[unit_name].selector

    def axis(self, unit_name='unit0'):
        for candidate in self.hh.printer.harness_selectors:
            if candidate.unit.name == unit_name:
                return candidate
        self.fail('no selector axis for %r' % unit_name)

    def var(self, name):
        return self.hh.save_variables.allVariables.get(name, 'MISSING')

    def believed_position(self, unit_name='unit0'):
        """
        Where Happy Hare thinks the carriage is.

        NOT axis().carriage, which is where the harness's fake carriage physically sits. A
        restore is do_set_position - a software rebase that moves nothing (extras/mmu_stepper.py)
        - so the two deliberately disagree in these tests: that IS the model of a reboot finding
        the carriage already parked where it was left.
        """
        return self.selector(unit_name).selector_stepper.commanded_pos

    # -- restore ------------------------------------------------------------------------------

    def test_a_power_off_comes_back_homed_on_the_same_gate(self):
        """
        The ordinary case, and the one that must never regress: both records present and
        agreeing. Power off at gate 3, boot, and the machine is at gate 3 and homed without
        having moved a step to find out.
        """
        hh = self.boot(calibrate=True, selected_gate=self.GATE, selector_last_pos=True)
        selector = self.selector()

        self.assertTrue(selector.is_homed, 'a restored position must count as homed')
        self.assertEqual(hh.mmu.gate_selected, self.GATE)
        self.assertEqual(hh.mmu.tool_selected, self.GATE)
        self.assertAlmostEqual(self.believed_position(), selector.selector_offsets[self.GATE],
                               places=3)

    def test_a_gate_with_no_position_on_record_falls_back_to_its_offset(self):
        """
        last_pos missing but the gate still persisted - what an upgrade or a renamed unit leaves
        behind, because the gate var is global while last_pos is namespaced per unit. There is
        nothing to distrust here (a motors-off would have cleared the gate too), so the gate's
        calibrated offset IS the position.
        """
        hh = self.boot(calibrate=True, selected_gate=self.GATE)
        selector = self.selector()

        self.assertTrue(selector.is_homed)
        self.assertEqual(hh.mmu.gate_selected, self.GATE)
        self.assertAlmostEqual(self.believed_position(), selector.selector_offsets[self.GATE],
                               places=3)
        self.assertAlmostEqual(self.var('mmu_unit0_selector_last_pos'),
                               selector.selector_offsets[self.GATE], places=3,
                               msg='the derived position should be written back')

    def test_an_uncalibrated_selector_still_refuses_to_claim_homed(self):
        """
        The fallback needs real offsets. Uncalibrated they are -1 placeholders, so it must
        decline and let the gate be dropped - which is also what keeps
        test_mmu_profiles.py's "unit0 is uncalibrated, so it must NOT claim homed" true.
        """
        hh = self.boot(selected_gate=self.GATE)

        self.assertFalse(self.selector().is_homed)
        self.assertEqual(hh.mmu.gate_selected, -1)

    def test_the_second_units_selector_ignores_a_gate_it_does_not_own(self):
        """
        The restore has to work on a multi-unit machine too, and unit_selected has to follow the
        restored gate rather than defaulting to unit 0 the way a dropped gate did.
        """
        hh = self.boot('ercf_vvd', calibrate=True, selected_gate=self.GATE)

        self.assertEqual(hh.mmu.gate_selected, self.GATE)
        self.assertEqual(hh.mmu.unit_selected, 0)
        self.assertTrue(self.selector('unit0').is_homed)
        self.assertAlmostEqual(self.believed_position('unit0'),
                               self.selector('unit0').selector_offsets[self.GATE], places=3)

    def test_a_selector_offers_no_position_for_a_gate_another_unit_owns(self):
        """
        The gate var is machine-wide, so every unit's selector reads the same number and must
        check it owns the gate first. Tested directly rather than through a boot: the only
        multi-unit profile pairs a linear selector with an IndexedSelector, which does not use
        this path at all - so a boot-level assertion about "the other unit" would pass even with
        the guard deleted.

        The guard is now load-bearing rather than cosmetic: local_gate() raises for a gate the
        unit does not own, so without the ownership check this would throw instead of declining.
        """
        from extras.mmu.mmu_constants import VARS_MMU_GATE_SELECTED

        hh = self.boot('ercf_vvd', calibrate=True)
        unit0 = self.selector('unit0')

        hh.mmu.var_manager.set(VARS_MMU_GATE_SELECTED, 12) # Gate 12 lives on unit1
        self.assertIsNone(unit0._persisted_gate_position(),
                          'unit0 offered a carriage position for a gate it does not manage')
        self.assertEqual(hh.errors, [], 'declining another unit\'s gate must be silent')

        hh.mmu.var_manager.set(VARS_MMU_GATE_SELECTED, self.GATE)
        self.assertAlmostEqual(unit0._persisted_gate_position(),
                               unit0.selector_offsets[self.GATE], places=3)

    # -- invalidation -------------------------------------------------------------------------

    def test_motors_off_invalidates_the_gate_as_well_as_the_position(self):
        """
        The correlation invariant. Nulling last_pos on its own was not enough: the gate alone is
        enough to reconstruct a position, so a surviving gate would resurrect exactly the
        position the user's motors-off just invalidated.
        """
        hh = self.boot(calibrate=True, selected_gate=self.GATE, selector_last_pos=True)
        hh.run_gcode('MMU_MOTORS_OFF')

        self.assertFalse(self.selector().is_homed)
        self.assertIsNone(self.var('mmu_unit0_selector_last_pos'))
        self.assertEqual(self.var('mmu_state_gate_selected'), -1)
        self.assertEqual(self.var('mmu_state_tool_selected'), -1)
        self.assertEqual(hh.errors, [])

    def test_motors_off_on_one_unit_leaves_another_units_selection_alone(self):
        """
        MMU_MOTORS_OFF is per-unit, so unit0 must not wipe a perfectly good selection that lives
        on unit1.

        Gate 12 is selected live rather than seeded: seeding it boots into an indeterminate
        filament position on unit1's per-gate switches ("Filament not detected as either unloaded
        or fully loaded"), which is pre-existing and nothing to do with the scoping being tested.
        """
        hh = self.boot('ercf_vvd')
        hh.calibrate()
        hh.run_gcode('MMU_SELECT GATE=12')
        self.assertEqual(hh.mmu.gate_selected, 12, 'gate 12 should be on unit1')
        self.assertEqual(self.var('mmu_state_gate_selected'), 12)

        hh.run_gcode('MMU_MOTORS_OFF UNIT=0')

        self.assertEqual(self.var('mmu_state_gate_selected'), 12,
                         "unit0's motors-off cleared unit1's selection")
        self.assertIsNone(self.var('mmu_unit0_selector_last_pos'))
        self.assertEqual(hh.errors, [])

    def test_a_failed_home_invalidates_the_pair_too(self):
        """
        A blocked or broken home is the other way the position becomes unknowable, and it must
        obey the same invariant as an explicit motors-off - otherwise the next boot would
        confidently restore a gate whose carriage is stuck somewhere else.

        A GUARD, not a demonstration: every route to a failed _home_selector clears the gate
        through home_unit(), including select_gate's autohome delegation. It is here so that if
        either path ever stops clearing, the invariant does not quietly depend on it.
        """
        hh = self.boot(calibrate=True, selected_gate=self.GATE, selector_last_pos=True)
        selector = self.selector()

        def explode():
            raise Exception('harness: selector jammed')
        selector.selector_stepper.do_home_rail = explode

        hh.run_gcode('MMU_HOME UNIT=0')

        self.assertFalse(selector.is_homed)
        self.assertIsNone(self.var('mmu_unit0_selector_last_pos'))
        self.assertEqual(self.var('mmu_state_gate_selected'), -1)

    # -- interaction with startup_home_selector ------------------------------------------------

    def test_startup_home_selector_homes_first_and_then_reselects_the_gate(self):
        """
        The opt-in path: when the user asks for homing at startup they get a real home, and the
        restored gate must survive it to be reselected afterwards. Before the records were
        paired the gate had already been dropped by then, so bootup silently re-selected the
        unit's FIRST gate instead of the one the user was on.

        startup_home_selector is set live rather than in config: the shipped template hardcodes
        0 for every physical selector (config/base/mmu_parameters.cfg), so there is no profile
        to switch to.
        """
        def enable_startup_homing():
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 startup_home_selector=1')

        hh = self.boot(calibrate=True, selected_gate=self.GATE, selector_last_pos=True,
                       pre_bootup=enable_startup_homing)

        self.assertIn('Homing MMU', '\n'.join(hh.console), 'bootup did not actually home')
        self.assertTrue(self.selector().is_homed)
        self.assertEqual(hh.mmu.gate_selected, self.GATE,
                         'the gate the user was on must be reselected after homing')
        self.assertAlmostEqual(self.axis().carriage,
                               self.selector().selector_offsets[self.GATE], places=3)

    def test_startup_home_selector_skips_rather_than_guess_on_unresolved_filament_state(self):
        """
        An unresolved filament_pos at boot (a sensor read failure, a fresh install with no
        persisted state - anything that leaves cmd_MMU_BOOTUP's own recovery attempt short of a
        definite answer) must not be treated as "safe to auto-home". Before the fix,
        FILAMENT_POS_UNKNOWN was explicitly exempted from the boot skip-check, so bootup would
        proceed into home_unit() -> PhysicalSelector.home(), whose "automatic unload case" branch
        then runs a real unload_sequence() - which can heat the extruder - based on nothing more
        than an ambiguous read. The fix (mmu_controller.py's autohoming loop) must skip and warn
        instead, leaving the unload untouched.

        No gate is seeded here: an unresolved filament_pos is most likely exactly when no gate
        has ever been selected either (a fresh install), and that combination is what let the old
        check's `gate_selected != TOOL_GATE_UNKNOWN` clause slip past it too.

        Not using self.boot(): a genuinely unresolved state legitimately produces two
        informational errors of its own (the rigged sensor failure itself, and
        report_necessary_recovery's "Filament detected but tool/gate is unknown" guidance,
        mmu_filament_movement.py:3351-3352) - orthogonal to this fix, which is only about what
        the autohoming loop does next. Pinning the exact pair below still catches anything ELSE
        going wrong (e.g. an unload attempt blowing up) as a third, unexpected error.
        """
        unload_calls = []

        def rig_unresolved_filament_state():
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 startup_home_selector=1')

            def explode(*args, **kwargs):
                raise Exception('harness: filament state sensors unavailable')
            self.hh.mmu.recover_filament_pos = explode
            self.hh.mmu.unload_sequence = lambda *args, **kwargs: unload_calls.append((args, kwargs))

        self.hh = session('tradrack')
        self.hh.boot(calibrate=True, pre_bootup=rig_unresolved_filament_state)
        hh = self.hh

        self.assertEqual(unload_calls, [], 'an unresolved filament state must never trigger an automatic unload')
        self.assertIn('may have filament loaded', '\n'.join(hh.console),
                      'bootup did not warn about the unresolved filament state')
        self.assertFalse(self.selector().is_homed, 'the selector must not be homed on an unresolved state either')
        self.assertEqual(hh.errors, [
            '!! harness: filament state sensors unavailable',
            '!! Filament detected but tool/gate is unknown. Please use MMU_RECOVER GATE=xx to correct state',
        ], 'only the rigged sensor failure and the resulting recovery guidance should be reported')

    def test_startup_home_selector_does_not_launder_a_sibling_units_loaded_gate(self):
        """
        The cross-unit case the other two startup_home_selector tests cannot reach: filament_pos
        is machine-wide, not per-unit, so on a multi-unit machine it reflects whichever gate is
        currently selected - here, one that belongs to unit0, not unit1.

        home_unit() now owns all unload policy and checks real ownership before changing
        gate_selected. PhysicalSelector.home() is mechanical only, so an unrelated unit never
        gets a second opportunity to infer ownership from the machine-wide UNKNOWN sentinel.
        This test pins that by construction: unit0 genuinely owns gate 3 with a real (not
        ambiguous) FILAMENT_POS_LOADED, and both units ask for startup homing at once.
        """
        unload_calls = []

        def rig_a_genuinely_loaded_sibling_unit():
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 startup_home_selector=1')
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=1 startup_home_selector=1')

            def force_loaded(*args, **kwargs):
                self.hh.mmu.filament_pos = FILAMENT_POS_LOADED
            self.hh.mmu.recover_filament_pos = force_loaded
            self.hh.mmu.unload_sequence = lambda *args, **kwargs: unload_calls.append((args, kwargs))

        hh = self.boot('ercf_vvd', calibrate=True, selected_gate=self.GATE, selector_last_pos=True,
                       pre_bootup=rig_a_genuinely_loaded_sibling_unit)

        self.assertEqual(unload_calls, [],
                         "unit1 homing must never trigger unit0's automatic unload")
        console = '\n'.join(hh.console)
        self.assertIn('Skipping autohome of unit0', console,
                      'unit0 (the genuinely loaded unit) should have been skipped, not homed')
        self.assertIn('Homing MMU unit1', console,
                      'unit1 (an unrelated unit) should have homed normally')
        self.assertTrue(self.selector('unit1').is_homed)
        self.assertEqual(hh.mmu.gate_selected, self.GATE,
                         "unit1's homing must not disturb unit0's gate selection")
        self.assertEqual(hh.errors, [])


class TestHomeUnitUnloadPolicy(unittest.TestCase):
    """Controller owns filament policy; selector.home() owns only selector motion."""

    GATE = 3

    def setUp(self):
        self.hh = session('tradrack')
        self.hh.boot()
        self.hh.calibrate()
        self.hh.run_gcode('MMU_HOME UNIT=0')
        self.hh.heat_extruder(220)
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit(self.GATE)
        self.selector = self.unit.selector

    def tearDown(self):
        self.hh.close()

    def _select_loaded_gate(self, state=FILAMENT_POS_LOADED):
        self.hh.run_gcode('MMU_SELECT GATE=%d' % self.GATE)
        self.mmu.set_filament_pos_state(state, silent=True)

    def _record_successful_unload(self):
        calls = []

        def unload(*args, **kwargs):
            calls.append((self.mmu.gate_selected, args, kwargs))
            self.mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED, silent=True)

        self.mmu.unload_sequence = unload
        return calls

    def test_automatic_policy_unloads_before_forgetting_the_owning_gate(self):
        self._select_loaded_gate()
        calls = self._record_successful_unload()

        self.mmu.home_unit(self.unit, force_unload=None, reselect=False)

        self.assertEqual(calls, [(self.GATE, (), {})])
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_UNKNOWN)
        self.assertTrue(self.selector.is_homed)

    def test_automatic_policy_can_recover_unknown_filament_when_gate_is_known(self):
        self._select_loaded_gate(FILAMENT_POS_UNKNOWN)
        calls = self._record_successful_unload()

        self.mmu.home_unit(self.unit, force_unload=None, reselect=False)

        self.assertEqual(calls, [(self.GATE, (), {})])
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_UNKNOWN)

    def test_forced_policy_preserves_gate_identity_during_state_recovery(self):
        self._select_loaded_gate(FILAMENT_POS_UNKNOWN)
        calls = self._record_successful_unload()

        self.mmu.home_unit(self.unit, force_unload=True, reselect=False)

        self.assertEqual(calls, [(self.GATE, (), {'check_state': True})])
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_UNKNOWN)

    def test_gcode_force_unload_one_uses_forced_policy(self):
        self._select_loaded_gate()
        calls = self._record_successful_unload()

        self.hh.run_gcode('MMU_HOME UNIT=0 FORCE_UNLOAD=1')

        self.assertEqual(calls, [(self.GATE, (), {'check_state': True})])
        self.assertEqual(self.hh.errors, [])

    def test_gcode_force_unload_zero_never_unloads(self):
        self._select_loaded_gate()
        calls = self._record_successful_unload()

        self.hh.run_gcode('MMU_HOME UNIT=0 FORCE_UNLOAD=0')

        self.assertEqual(calls, [])
        self.assertEqual(self.mmu.gate_selected, self.GATE)
        self.assertTrue(any('has filament loaded' in error for error in self.hh.errors))

    def test_default_gcode_home_performs_a_real_automatic_unload(self):
        self.hh.place_filament(self.GATE, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=%d' % self.GATE)
        self.hh.run_gcode('MMU_SELECT GATE=%d' % self.GATE)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_LOADED)

        self.hh.run_gcode('MMU_HOME UNIT=0')

        self.assertEqual(self.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertTrue(self.selector.is_homed)
        self.assertEqual(self.hh.errors, [])

    def test_failed_unload_keeps_the_owning_gate_for_recovery(self):
        self._select_loaded_gate()
        calls = []

        def fail_unload(*args, **kwargs):
            calls.append(self.mmu.gate_selected)
            from extras.mmu.mmu_utils import MmuError
            raise MmuError('simulated unload failure')

        self.mmu.unload_sequence = fail_unload

        with self.assertRaisesRegex(Exception, 'simulated unload failure'):
            self.mmu.home_unit(self.unit, force_unload=None, reselect=False)

        self.assertEqual(calls, [self.GATE])
        self.assertEqual(self.mmu.gate_selected, self.GATE)

    def test_autohome_unload_failure_also_keeps_the_owning_gate(self):
        self._select_loaded_gate()
        self.selector.is_homed = False

        def fail_unload(*args, **kwargs):
            from extras.mmu.mmu_utils import MmuError
            raise MmuError('simulated autohome unload failure')

        self.mmu.unload_sequence = fail_unload

        with self.assertRaisesRegex(Exception, 'simulated autohome unload failure'):
            self.mmu.select_gate(self.GATE + 1)

        self.assertEqual(self.mmu.gate_selected, self.GATE)

    def test_unknown_gate_with_unresolved_filament_requires_recovery(self):
        self.mmu.unselect_gate()
        self.mmu.set_filament_pos_state(FILAMENT_POS_UNKNOWN, silent=True)
        unload_calls = self._record_successful_unload()
        home_calls = []
        self.selector._home_selector = lambda: home_calls.append(True)

        with self.assertRaisesRegex(Exception, 'MMU_RECOVER GATE'):
            self.mmu.home_unit(self.unit, force_unload=None, reselect=False)

        self.assertEqual(unload_calls, [])
        self.assertEqual(home_calls, [])
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_UNKNOWN)

    def test_explicit_never_unload_can_home_with_unknown_ownership(self):
        self.mmu.unselect_gate()
        self.mmu.set_filament_pos_state(FILAMENT_POS_UNKNOWN, silent=True)
        unload_calls = self._record_successful_unload()
        home_calls = []
        self.selector._home_selector = lambda: home_calls.append(True)

        self.mmu.home_unit(self.unit, force_unload=False, reselect=False)

        self.assertEqual(unload_calls, [])
        self.assertEqual(home_calls, [True])
        self.assertEqual(self.mmu.gate_selected, TOOL_GATE_UNKNOWN)

    def test_selector_home_is_mechanical_and_never_infers_filament_ownership(self):
        self._select_loaded_gate()
        unload_calls = self._record_successful_unload()
        home_calls = []
        self.selector._home_selector = lambda: home_calls.append(self.mmu.gate_selected)

        self.selector.home()

        self.assertEqual(unload_calls, [])
        self.assertEqual(home_calls, [self.GATE])
        self.assertEqual(self.mmu.gate_selected, self.GATE)

    def test_homing_another_unit_never_unloads_the_active_units_gate(self):
        self.hh.close()
        self.hh = session('ercf_vvd')
        self.hh.boot()
        self.hh.calibrate()
        self.mmu = self.hh.mmu
        self.hh.run_gcode('MMU_HOME UNIT=0')
        self.hh.run_gcode('MMU_SELECT GATE=3')
        self.mmu.set_filament_pos_state(FILAMENT_POS_LOADED, silent=True)
        unload_calls = self._record_successful_unload()
        sibling = self.mmu.mmu_machine.units[1]

        self.mmu.home_unit(sibling, force_unload=True, reselect=False)

        self.assertEqual(unload_calls, [])
        self.assertEqual(self.mmu.gate_selected, self.GATE)
        self.assertTrue(sibling.selector.is_homed)


class TestTipFormingEffect(unittest.TestCase):
    """
    Macro bodies do not run in the harness, but HH MEASURES how far the extruder moved during
    _MMU_FORM_TIP and refuses the unload if the answer is zero. So tip forming is one of the
    few macros that needs a real effect - see Session.install_macro_effects.
    """

    def setUp(self):
        self.hh = session('ercf_vvd')
        self.hh.boot()
        # Needed for MMU_SELECT to succeed: without calibrated offsets gate_selected stays -1,
        # and the effect skips the filament model when no gate is selected.
        self.hh.calibrate()
        self.hh.run_gcode('MMU_HOME UNIT=0')

    def tearDown(self):
        self.hh.close()

    def test_an_effect_is_registered_for_tip_forming(self):
        self.assertIn('_MMU_FORM_TIP',
                      getattr(self.hh.printer, 'harness_macro_effects', {}))

    def test_the_effect_retracts_the_extruder_and_moves_the_filament(self):
        """
        Both halves matter and by the SAME amount: the extruder delta is what HH turns into
        park_pos, and the filament movement is what generates encoder pulses. If they
        disagreed, HH's idea of the machine would drift from the harness's.
        """
        model = self.hh.filament()
        macro = self.hh.printer.lookup_object('gcode_macro _MMU_FORM_TIP')
        variables = self.hh.printer.lookup_object(
            'gcode_macro _MMU_FORM_TIP_VARS').variables
        expected = (variables['cooling_tube_position'] + variables['cooling_tube_length'])
        self.assertGreater(expected, 0, 'profile has no tip-forming distance to model')

        gate = 2
        self.hh.run_gcode('MMU_SELECT GATE=%d' % gate)
        self.hh.place_filament(gate, position=model.layout['toolhead'] + 30.0)
        stepper = self.hh.printer.lookup_object(
            'toolhead').get_extruder().extruder_stepper.stepper

        before_extruder = stepper.get_commanded_position()
        before_tip = model.tip[gate]
        self.hh._effect_form_tip(macro, None)

        self.assertAlmostEqual(before_extruder - stepper.get_commanded_position(),
                               expected, places=3)
        self.assertAlmostEqual(before_tip - model.tip[gate], expected, places=3)


if __name__ == '__main__':
    unittest.main()
