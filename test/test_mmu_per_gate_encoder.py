# Happy Hare test harness - an encoder per gate.
#
# [mmu_unit] encoder names one encoder that every gate shares, or one per gate. Each physical
# encoder is its own MmuEncoder, with its own resolution, calibration and measurement state;
# the unit maps gates to them and mmu.encoder(gate) / unit.encoder resolve through that map, so
# the code written for one encoder per unit keeps working. Here BoxTurtle gets a digital wheel
# on each gate (g0..g3), added to the rendered config by hand as a user would, and a second
# fixture puts an analog wheel on each gate the way the Flashforge AD5X IFS has them.
#
#   ./venv/bin/python -m unittest test.test_mmu_per_gate_encoder
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

GATES = range(4)
NAMES = ['g%d' % g for g in GATES]
DIGITAL_PINS = ['unit0:PB%d' % g for g in GATES]
ANALOG_PINS = ['unit0:PC%d' % g for g in GATES]
TIP_AT_GATE = -40.0
WHEEL = [0.66, 0.74, 0.78, 0.81, 0.75, 0.66, 0.56, 0.515, 0.50, 0.50, 0.51, 0.54, 0.60]


def per_gate_session(names=NAMES, pin_key='encoder_pin', pins=DIGITAL_PINS, calibrate=True,
                     unit_names=None):
    def edit(fc):
        for name, pin in zip(names, pins):
            section = 'mmu_encoder %s' % name
            if not fc.has_section(section):
                fc.add_section(section)
            fc.set(section, pin_key, pin)
            fc.set(section, 'encoder_resolution', '1.0')
        fc.set('mmu_unit unit0', 'encoder', ', '.join(unit_names or names))

    hh = session('boxturtle_test', config_edit=edit)
    try:
        hh.boot(calibrate=calibrate)
    except Exception:
        hh.close()
        raise
    return hh


class PerGateTestCase(unittest.TestCase):

    def setUp(self):
        self.hh = per_gate_session()
        self.addCleanup(self.hh.close)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.unit = self.mmu.mmu_unit(0)
        self.enc = {g: self.hh.printer.lookup_object('mmu_encoder %s' % NAMES[g]) for g in GATES}


class TestMapping(PerGateTestCase):

    def test_each_gate_has_its_own_encoder(self):
        self.assertEqual(self.unit.encoders, [self.enc[g] for g in GATES])
        for g in GATES:
            self.assertIs(self.unit.encoder_for(g), self.enc[g])
            self.assertIs(self.mmu.encoder(g), self.enc[g])

    def test_the_unit_encoder_is_the_selected_gates(self):
        for g in (2, 0, 3):
            self.hh.run_gcode('MMU_SELECT GATE=%d' % g)
            self.assertIs(self.unit.encoder, self.enc[g])
            self.assertIs(self.mmu.encoder(), self.enc[g])

    def test_each_wheel_counts_its_own_gate_only(self):
        self.hh.run_gcode('MMU_SELECT GATE=1')
        self.hh.place_filament(1, position=self.hh.filament().layout['mmu_encoder'] + 5.0)
        start = {g: self.enc[g].get_distance() for g in GATES}
        self.hh.run_gcode('MMU_TEST_MOVE MOVE=30')
        self.assertEqual(self.hh.errors, [])
        self.assertGreater(self.enc[1].get_distance(), start[1])
        for g in (0, 2, 3):
            self.assertEqual(self.enc[g].get_distance(), start[g], g)

    def test_movement_of_an_unselected_gate_is_never_credited_to_the_selected_one(self):
        self.hh.run_gcode('MMU_SELECT GATE=0')
        before = self.mmu.encoder().get_distance()
        self.hh.filament().advance(2, 30.0, 'user pulls on gate 2')
        self.assertEqual(self.mmu.encoder().get_distance(), before)

    def test_the_unit_level_encoder_sensor_is_the_first_wheel(self):
        """The extra wheels are only added under their own names, not over the unit's 'encoder'."""
        unit_map = self.mmu.sensor_manager.unit_sensors[0]
        self.assertIs(unit_map['encoder'], self.enc[0].endstop_sensor)

    def test_each_encoder_has_its_endstop_sensor(self):
        for g in GATES:
            self.assertIsNotNone(self.hh.sensor('%s:encoder' % NAMES[g]))


class TestCalibration(PerGateTestCase):

    def test_calibration_is_per_encoder(self):
        from extras.mmu.mmu_constants import CALIBRATED_ENCODER
        self.unit.calibrator.calibration_status &= ~CALIBRATED_ENCODER
        for e in self.enc.values():
            e.calibrated = False
        self.hh.place_filament(2, position=TIP_AT_GATE)
        self.hh.run_gcode('MMU_PRELOAD GATE=2')
        self.hh.run_gcode('MMU_SELECT GATE=2')
        self.hh.place_filament(2, position=self.hh.filament().layout['mmu_encoder'] + 50.0)

        # Every wheel is named in the calibration reminder
        self.assertTrue(any('one encoder per gate): 0,1,2,3' in e for e in self.hh.errors), self.hh.errors)
        del self.hh.errors[:]

        self.hh.run_gcode('MMU_CALIBRATE_ENCODER LENGTH=100 REPEATS=1 SAVE=1')

        self.assertEqual([e for e in self.hh.errors if 'Calibration steps' not in e], [])
        vm = self.mmu.var_manager
        self.assertIsNotNone(vm.get('mmu_encoder_resolution', None, namespace='g2'))
        self.assertTrue(self.enc[2].calibrated)
        self.assertFalse(any(self.enc[g].calibrated for g in (0, 1, 3)))
        self.assertFalse(self.unit.calibrator.calibration_status & CALIBRATED_ENCODER,
                         'one of four encoders calibrated is not a calibrated unit')

    def test_a_calibrated_resolution_changes_only_its_encoder(self):
        self.enc[1].set_resolution(5.0)
        self.assertEqual([self.enc[g].get_resolution() for g in GATES], [1.0, 5.0, 1.0, 1.0])


class TestFlowGuard(PerGateTestCase):

    def test_monitoring_follows_the_selected_gate(self):
        self.hh.run_gcode('MMU_SELECT GATE=0')
        self.mmu._enable_filament_monitoring()
        self.assertTrue(self.enc[0].is_flowguard_enabled())
        generation = self.enc[0].get_flowguard_generation()

        self.hh.run_gcode('MMU_SELECT GATE=3')

        self.assertTrue(self.enc[3].is_flowguard_enabled())
        self.assertFalse(self.enc[0].is_flowguard_enabled())
        self.assertGreater(self.enc[0].get_flowguard_generation(), generation,
                           "the old wheel's queued events must be invalidated")
        self.assertEqual([g for g in GATES if self.enc[g].is_flowguard_enabled()], [3])

    def test_disabling_monitoring_disarms_every_wheel(self):
        self.hh.run_gcode('MMU_SELECT GATE=1')
        self.mmu._enable_filament_monitoring()
        self.mmu._disable_filament_monitoring()
        self.assertFalse(any(e.is_flowguard_enabled() for e in self.enc.values()))


class TestStatus(PerGateTestCase):

    def test_mmu_status_names_the_selected_gates_encoder(self):
        self.hh.run_gcode('MMU_SELECT GATE=2')
        at = len(self.hh.console)
        self.hh.run_gcode('MMU_STATUS')
        self.assertTrue(any('Encoder per gate, g2 reads' in l for l in self.hh.console[at:]),
                        self.hh.console[at:])

    def test_printer_status_is_the_selected_gates_encoder(self):
        self.hh.run_gcode('MMU_SELECT GATE=1')
        self.enc[1].set_distance(12.0)
        status = self.mmu.get_status(self.hh.reactor.monotonic())
        self.assertEqual(status['encoder']['encoder_pos'], 12.0)


class TestSharedPath(PerGateTestCase):

    def test_a_wheel_per_gate_still_protects_the_occupied_merge(self):
        """The gate's own wheel says nothing about the path downstream the loaded gate fills."""
        from extras.mmu.mmu_constants import SENSOR_ENCODER, FILAMENT_POS_LOADED
        self.hh.run_gcode('MMU_SELECT GATE=0')
        self.mmu.filament_pos = FILAMENT_POS_LOADED
        self.assertTrue(self.mmu._shared_gate_path_occupied(SENSOR_ENCODER, 1))
        self.assertFalse(self.mmu._shared_gate_path_occupied(SENSOR_ENCODER, 0))


class TestConfig(unittest.TestCase):

    def test_one_shared_name_still_means_every_gate(self):
        hh = per_gate_session(names=['shared'], pins=['unit0:PB0'])
        self.addCleanup(hh.close)
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(len(unit.encoders), 1)
        self.assertTrue(all(unit.encoder_for(g) is unit.encoders[0] for g in GATES))

    def test_the_wrong_number_of_names_is_a_config_error(self):
        with self.assertRaises(Exception) as ctx:
            per_gate_session(names=NAMES[:3], pins=DIGITAL_PINS[:3])
        self.assertIn('one per gate', str(ctx.exception))

    def test_gates_can_share_one_physical_encoder(self):
        hh = per_gate_session(names=['a', 'b'], pins=['unit0:PB0', 'unit0:PB1'],
                              unit_names=['a', 'a', 'b', 'b'])
        self.addCleanup(hh.close)
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual([e.name for e in unit.encoders], ['a', 'b'])
        self.assertIs(unit.encoder_for(0), unit.encoder_for(1))
        self.assertIsNot(unit.encoder_for(1), unit.encoder_for(2))


class TestAnalogWheelPerGate(unittest.TestCase):
    """The AD5X IFS arrangement: an analog wheel on every gate, each its own encoder."""

    def setUp(self):
        self.hh = per_gate_session(pin_key='analog_pin', pins=ANALOG_PINS)
        self.addCleanup(self.hh.close)
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.unit = self.hh.mmu.mmu_unit(0)
        self.t = 100.

    def turn(self, gate, turns=1):
        adc = self.unit.encoder_for(gate).counter.mcu_adc
        for v in WHEEL * turns:
            self.t += 0.02
            adc.feed(v, read_time=self.t)

    def test_an_unselected_wheel_keeps_its_state_and_its_counts(self):
        """Low before the gate is selected, high after: that crossing is a pulse, never lost."""
        self.hh.run_gcode('MMU_SELECT GATE=0')
        enc1 = self.unit.encoder_for(1)
        enc1.counter.mcu_adc.feed(0.50, read_time=self.t)
        before = enc1.counter.count
        self.hh.run_gcode('MMU_SELECT GATE=1')
        self.t += 0.02
        enc1.counter.mcu_adc.feed(0.81, read_time=self.t)
        self.assertEqual(enc1.counter.count, before + 1)

    def test_selection_switches_no_counter_and_invents_no_pulse(self):
        self.turn(0)
        self.turn(1)
        counts = [self.unit.encoder_for(g).counter.count for g in GATES]
        for g in (1, 0, 2):
            self.hh.run_gcode('MMU_SELECT GATE=%d' % g)
        self.assertEqual([self.unit.encoder_for(g).counter.count for g in GATES], counts)

    def test_each_wheel_is_its_gates_distance(self):
        self.turn(2)
        start = [self.unit.encoder_for(g).get_distance() for g in GATES]
        self.turn(2, 5)
        self.assertEqual(self.unit.encoder_for(2).get_distance() - start[2], 10.0)
        self.assertEqual(self.unit.encoder_for(0).get_distance(), start[0])


if __name__ == '__main__':
    unittest.main()
