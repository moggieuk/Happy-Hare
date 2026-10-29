# Happy Hare test harness - toolhead_unload_test.
#
# With a toolhead or extruder entry sensor the reverse-homing distance of the extruder unload is
# compared with the distance expected from the tip forming/cutting park position. A missed cut
# leaves the filament longer by the cut fragment, so it takes that much further to clear the
# sensor. Without such a sensor the existing encoder (or sync-feedback buffer) check is
# used, and the same parameter decides whether a failure warns or pauses.
#
# GEOMETRY. 3ms homes off the extruder entry sensor and its layout matches its toolhead
# dimensions. ercf_vvd's layout matches its toolhead sensor dimensions but not its entry sensor
# ones, so the toolhead-sensor branch is exercised there with the entry sensor disabled.
#
# A CUT is modelled by wrapping the harness tip-forming effect: after the extruder retracts, the
# macro reports output_park_pos as a cut fragment further back than the retraction alone, and
# (unless the cut is "missed") the filament end is moved back by that fragment without moving
# the extruder - which is what the blade does.
#
#   ./venv/bin/python -m unittest test.test_mmu_toolhead_unload_test
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest
from unittest import mock

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

FILAMENT_POS_UNLOADED = 0
FILAMENT_POS_HOMED_ENTRY = 5
FILAMENT_POS_HOMED_TS = 8
TIP_AT_GATE = -40.0
FRAGMENT = 10.0


class UnloadTestCase(unittest.TestCase):
    PROFILE = '3ms'
    DISABLE_SENSORS = ()

    def setUp(self):
        self.hh = session(self.PROFILE)
        self.hh.boot(calibrate=True)
        self.addCleanup(self.hh.close)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.fil = self.hh.filament()
        self.unit = self.hh.mmu.mmu_unit(0)
        self.hh.place_filament(0, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=0')
        self.hh.heat_extruder(220)
        self.hh.mmu.select_gate(0)
        self.hh.run_gcode('MMU_LOAD')
        self.assertEqual(self.hh.errors, [], 'load was not clean')
        for sensor in self.DISABLE_SENSORS:
            self.hh.run_gcode('MMU_SENSORS SENSOR=%s ENABLE=0' % sensor)
        self.warnings = []
        self.debug = []
        self._patch_log('log_warning', self.warnings)
        self._patch_log('log_debug', self.debug)

    def _patch_log(self, name, sink):
        original = getattr(self.hh.mmu, name)

        def record(msg, *args, **kwargs):
            sink.append(msg)
            return original(msg, *args, **kwargs)

        patcher = mock.patch.object(self.hh.mmu, name, side_effect=record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_level(self, level):
        self.hh.mmu.p.toolhead_unload_test = level

    def tip_effect(self, fragment=0., missed=False, slip=0.):
        """
        Replace the tip-forming effect with a cut of `fragment` mm (missed or not), or with tip
        forming whose filament lags the extruder by `slip` mm.
        """
        effects = self.hh.printer.harness_macro_effects
        form_tip = effects['_MMU_FORM_TIP']
        toolhead = self.unit.toolhead_wrapper.p

        def effect(macro, gcmd):
            gate = self.hh.mmu.gate_selected
            before = self.fil.tip[gate]
            form_tip(macro, gcmd)
            retracted = before - self.fil.tip[gate]
            if fragment:
                park_pos = (retracted + toolhead.toolhead_residual_filament
                            + self.hh.mmu.park_retracted_length() + fragment)
                macro.variables = dict(macro.variables, output_park_pos=park_pos)
                if not missed:
                    self.fil.advance(gate, -fragment, 'cut')
            if slip:
                self.fil.advance(gate, slip, 'slip')

        self.addCleanup(effects.__setitem__, '_MMU_FORM_TIP', form_tip)
        effects['_MMU_FORM_TIP'] = effect

    def unload(self):
        self.hh.run_gcode('MMU_UNLOAD')

    def unload_warnings(self):
        return [w for w in self.warnings if 'to clear the' in w]


class TestExtruderEntrySensor(UnloadTestCase):

    def test_good_cut_passes(self):
        self.set_level(2)
        self.tip_effect(FRAGMENT)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        self.assertTrue(any('Unload homing took' in d for d in self.debug))

    def test_missed_cut_warns_and_continues(self):
        self.set_level(1)
        self.tip_effect(FRAGMENT, missed=True)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)
        warnings = self.unload_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn('extruder entry sensor', warnings[0])
        self.assertIn('may not have been cut', warnings[0])

    def test_missed_cut_pauses_at_the_sensor(self):
        self.set_level(2)
        self.tip_effect(FRAGMENT, missed=True)
        self.unload()
        self.assertTrue(any('may not have been cut' in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_HOMED_ENTRY)

    def test_missed_cut_is_ignored_by_default(self):
        self.tip_effect(FRAGMENT, missed=True)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.unload_warnings(), [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_fragment_too_small_to_resolve_is_not_checked(self):
        self.set_level(2)
        self.tip_effect(1.5, missed=True)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertTrue(any('too small to detect' in d for d in self.debug))

    def test_tip_forming_slip_beyond_the_margin_warns(self):
        self.set_level(1)
        self.tip_effect(slip=self.unit.p.toolhead_unload_safety_margin + 5.)
        self.unload()
        warnings = self.unload_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn('slipped during tip forming', warnings[0])

    def test_normal_tip_forming_passes(self):
        self.set_level(2)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_skip_tip_passes(self):
        self.set_level(2)
        self.hh.run_gcode('MMU_UNLOAD SKIP_TIP=1')
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_missed_cut_pauses_in_the_gcode_unload_sequence(self):
        self.set_level(2)
        self.tip_effect(FRAGMENT, missed=True)
        self.hh.mmu.p.gcode_unload_sequence = 1
        effects = self.hh.printer.harness_macro_effects
        self.addCleanup(effects.pop, '_MMU_UNLOAD_SEQUENCE', None)
        effects['_MMU_UNLOAD_SEQUENCE'] = lambda macro, gcmd: macro.run_body(gcmd)
        self.unload()
        self.assertTrue(any('may not have been cut' in e for e in self.hh.errors), self.hh.errors)


class TestToolheadSensor(UnloadTestCase):
    PROFILE = 'ercf_vvd'
    DISABLE_SENSORS = ('extruder',)

    def test_good_cut_passes(self):
        self.set_level(2)
        self.tip_effect(FRAGMENT)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_UNLOADED)

    def test_missed_cut_warns(self):
        self.set_level(1)
        self.tip_effect(FRAGMENT, missed=True)
        self.unload()
        self.assertEqual(self.hh.errors, [])
        warnings = self.unload_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn('toolhead sensor', warnings[0])

    def test_missed_cut_pauses_at_the_sensor(self):
        self.set_level(2)
        self.tip_effect(FRAGMENT, missed=True)
        self.unload()
        self.assertTrue(any('may not have been cut' in e for e in self.hh.errors), self.hh.errors)
        self.assertEqual(self.hh.mmu.filament_pos, FILAMENT_POS_HOMED_TS)


class TestEncoderCheck(UnloadTestCase):
    """Without toolhead sensors the parameter decides what the existing encoder check does."""
    PROFILE = 'ercf_vvd'
    DISABLE_SENSORS = ('extruder', 'toolhead')

    def setUp(self):
        super().setUp()
        move_filament = self.hh.mmu.move_filament

        def encoder_sees_nothing(trace_str, dist, *args, **kwargs):
            actual, homed, measured, delta = move_filament(trace_str, dist, *args, **kwargs)
            if trace_str == 'Unloading extruder':
                return actual, homed, 0., abs(actual)
            return actual, homed, measured, delta

        patcher = mock.patch.object(self.hh.mmu, 'move_filament', side_effect=encoder_sees_nothing)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_keeps_the_existing_warning(self):
        self.unload()
        self.assertIn(
            "Warning: Encoder not sensing any movement during final extruder retraction move\n"
            "Concluding filament either stuck in the extruder, tip forming erroneously completely "
            "ejected filament or filament was not fully loaded\n"
            "Will attempt to continue...",
            self.warnings)

    def test_pause_turns_it_into_an_error(self):
        self.set_level(2)
        self.unload()
        self.assertTrue(any('Encoder not sensing any movement' in e for e in self.hh.errors), self.hh.errors)


class TestThreshold(UnloadTestCase):
    """The threshold rules, driven directly."""

    def check(self, actual, expected, cut_fragment, buffer_slack=0.):
        self.set_level(1)
        self.hh.mmu._check_unload_homing_distance('toolhead', actual, expected, cut_fragment, buffer_slack)
        return self.unload_warnings()

    def test_cut_fires_past_half_the_fragment(self):
        self.assertEqual(self.check(34.9, 30., 10.), [])
        self.assertEqual(len(self.check(35.1, 30., 10.)), 1)

    def test_tip_forming_uses_the_safety_margin(self):
        margin = self.unit.p.toolhead_unload_safety_margin
        self.assertEqual(self.check(30. + margin - 0.1, 30., 0.), [])
        self.assertEqual(len(self.check(30. + margin + 0.1, 30., 0.)), 1)

    def test_buffer_slack_that_hides_the_fragment_skips_the_check(self):
        self.assertEqual(self.check(40., 30., 10., buffer_slack=5.), [])
        self.assertTrue(any('sync-feedback buffer travel' in d for d in self.debug))

    def test_smaller_buffer_slack_still_checks(self):
        self.assertEqual(len(self.check(40., 30., 10., buffer_slack=4.)), 1)

    def test_tip_forming_adds_buffer_slack_to_the_margin(self):
        margin = self.unit.p.toolhead_unload_safety_margin
        self.assertEqual(self.check(30. + margin + 4., 30., 0., buffer_slack=5.), [])


if __name__ == '__main__':
    unittest.main()
