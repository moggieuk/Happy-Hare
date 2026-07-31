# Happy Hare test harness - PHYSICAL selectors.
#
# Everything else in the suite runs on a VirtualSelector, where selecting a gate is
# bookkeeping. This file covers the machines that actually move a carriage, which until now
# could not move filament at all in the harness: selector homing failed with "No trigger on
# mmu_sel_home after full movement" because selector endstops were routed through the
# gate-filament model, which has no selector axis. See test/hh/selector.py.
#
# TWO GEOMETRIES, and they disagree about everything:
#
#   tradrack / ERCF   LinearServoSelector   one home switch; gates at calibrated offsets
#   ViViD             IndexedSelector       no home switch; one index switch PER gate,
#                                           visited in selector_gate_order
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

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_LOADED = 10
TIP_AT_GATE = -40.0             # past the entry switch: where a user's push leaves it


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
