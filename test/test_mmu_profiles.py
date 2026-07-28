# Happy Hare test harness - machine profile breadth.
#
# Everything else in the suite runs on BoxTurtle. This file is the regression net for
# CONFIG BREADTH: it boots genuinely different machines, so a renamed parameter, a broken
# [% if %] guard or a missing template section shows up here rather than on a user's
# printer.
#
# There are 19 shipped machine types. Three boot in the harness today:
#
#   boxturtle  4 gates,  VirtualSelector       - Type B, the default everywhere else
#   tradrack  10 gates,  LinearServoSelector   - a PHYSICAL selector, so the suite is not
#                                                shaped around one selector type
#   emu        5 gates,  VirtualSelector       - the only shipped profile with a
#                                                PROPORTIONAL (analog) buffer sensor
#
# The other 16 need harness work, all mechanical rather than deep - see the coverage map
# in test/README.md. In short: 13 need the machine x board pin selection, 2 need a
# heater_generic fake, 1 needs an unselected choice param.
#
#   ./venv/bin/python -m unittest test.test_mmu_profiles
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import unittest

from test.hh import session

logging.getLogger().setLevel(logging.CRITICAL)

# profile -> (gates, selector class name)
BOOTABLE = {
    'boxturtle': (4, 'VirtualSelector'),
    'tradrack': (10, 'LinearServoSelector'),
    'emu': (5, 'VirtualSelector'),
}


class TestEveryBootableProfile(unittest.TestCase):
    """
    One boot per machine. Deliberately built as separate test methods rather than a loop
    so a failure names the machine that broke.
    """

    def _boot(self, name):
        hh = session(name)
        self.addCleanup(hh.close)       # runs even if an assertion fails
        hh.boot()
        return hh

    def _check(self, name):
        gates, selector = BOOTABLE[name]
        hh = self._boot(name)
        self.assertTrue(hh.fired('mmu:bootup'), '%s never reached bootup' % name)
        self.assertEqual(hh.errors, [], '%s booted with errors' % name)
        self.assertEqual(hh.mmu.num_gates, gates)
        unit = hh.mmu.mmu_unit(0)
        self.assertEqual(type(unit.selector).__name__, selector)
        return hh

    def test_boxturtle(self):
        self._check('boxturtle')

    def test_tradrack(self):
        """
        A physical selector, which matters: it takes a different construction path from
        BoxTurtle's VirtualSelector and gets no coverage anywhere else.
        """
        hh = self._check('tradrack')
        self.assertTrue(hasattr(hh.mmu.mmu_unit(0).selector, 'selector_stepper'))

    def test_emu(self):
        self._check('emu')

    def test_each_profile_reaches_a_determinate_filament_state(self):
        """
        A powered-on machine with no filament must know it is unloaded. Anything else and
        HH tells the user to run MMU_RECOVER, which is what the error assertion catches -
        but assert the state directly too, since it is the thing that matters.
        """
        for name in BOOTABLE:
            with self.subTest(profile=name):
                hh = self._boot(name)
                self.assertEqual(hh.mmu.filament_pos, 0)    # FILAMENT_POS_UNLOADED

    def test_gate_count_matches_the_rendered_config(self):
        """Guards against a profile silently rendering a different machine."""
        from test.hh import cfg, profiles
        for name, (gates, _selector) in BOOTABLE.items():
            with self.subTest(profile=name):
                parser = cfg.assemble(cfg.render(profiles.get(name)))
                unit = dict(parser.items('mmu_unit unit0'))
                self.assertEqual(int(unit['num_gates']), gates)


class TestSelectorCoverage(unittest.TestCase):
    """
    9 selector classes exist; 2 are reachable through a bootable profile. Recorded as a
    test so the gap is visible in the suite rather than only in a document.
    """

    def test_selector_registry_is_fully_populated(self):
        from extras.mmu.unit.selectors import SELECTOR_REGISTRY
        self.assertGreaterEqual(len(SELECTOR_REGISTRY), 8)

    def test_which_selectors_are_actually_exercised(self):
        exercised = {selector for _gates, selector in BOOTABLE.values()}
        self.assertEqual(exercised, {'VirtualSelector', 'LinearServoSelector'},
                         'update this and the README coverage map when a profile adds '
                         'another selector type')


class TestProportionalBufferSensor(unittest.TestCase):
    """
    EMU's analog buffer sensor - the only place a shipped profile exercises the ADC path.

    A proportional sensor reports a normalised value in [-1.0, +1.0] and DERIVES the
    virtual filament_compression / filament_tension sensors from it by threshold, rather
    than reading switches. Those derived sensors have no switch_pin at all, which is what
    made this profile fail to load before the harness learned to dispatch by sensor kind.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.prop = self.hh.sensor('filament_proportional')
        self.sm = self.hh.mmu.sensor_manager

    def tearDown(self):
        self.hh.close()

    def compression(self):
        return self.sm.check_sensor('filament_compression')

    def tension(self):
        return self.sm.check_sensor('filament_tension')

    def test_an_adc_pin_really_is_bound(self):
        adc = self.hh.pins.of_type('adc')
        self.assertTrue(adc, 'EMU should bind an analog buffer pin')

    def test_resting_state_matches_the_configured_spring(self):
        """
        The buffer declares buffer_spring_state: tension, so at rest the analog reading
        must sit at the tension end and the derived tension sensor must be the one
        triggered. Expressed as a RAW VALUE and left to derive - forcing the virtual
        sensor directly leaves it stuck, because the derivation only re-evaluates on a
        threshold crossing.
        """
        unit = self.hh.mmu.mmu_unit(0)
        self.assertEqual(unit.buffer.buffer_spring_state, 'tension')
        self.assertAlmostEqual(self.prop.value, -1.0, places=2)
        self.assertTrue(self.tension())
        self.assertFalse(self.compression())

    def test_compression_end(self):
        self.prop.feed(self.prop.neutral_value() + self.prop.sensor._d_pos)
        self.assertAlmostEqual(self.prop.value, 1.0, places=2)
        self.assertTrue(self.compression())
        self.assertFalse(self.tension())

    def test_reading_is_normalised_not_raw(self):
        sensor = self.prop.sensor
        self.prop.feed(sensor._neutral_point)
        self.assertAlmostEqual(self.prop.value, 0.0, places=2)
        self.assertAlmostEqual(sensor.value_raw, sensor._neutral_point, places=3)

    def test_derived_sensors_have_no_switch_pin(self):
        """The property that broke this profile, pinned so it cannot regress silently."""
        for name in ('filament_tension', 'filament_compression'):
            with self.subTest(sensor=name):
                self.assertEqual(self.hh.sensor(name).kind, 'virtual')
        self.assertEqual(self.hh.sensor('filament_proportional').kind, 'proportional')
        self.assertEqual(self.hh.sensor('mmu_entry_0').kind, 'switch')


class TestTensionThresholdSignError(unittest.TestCase):
    """
    A REAL BUG: a proportional buffer sensor reports TENSION almost all the time.

    The config help for analog_sensor_threshold states the intent unambiguously
    (installer/Kconfig.sync_feedback_buffer:208-215):

        a setting of 0.9 means:
          the virtual filament_compression sensor will trigger at 0.9
          the virtual filament_tension sensor will trigger at -0.9

    But the thresholds are computed without the negation
    (extras/mmu/unit/mmu_buffer.py:207-209):

        h = abs(self.analog_sensor_threshold) * self._vsensor_hysteresis
        self._vsensor_threshold_low  = self.analog_sensor_threshold - h    # +0.864
        self._vsensor_threshold_high = self.analog_sensor_threshold + h    # +0.936

    so with the default threshold of 0.9 the bands are:

        value > +0.936            -> compression
        +0.864 .. +0.936          -> neutral
        anything below +0.864     -> TENSION      <-- includes 0.0 and every -ve value

    The consequences: there is effectively no neutral state (it is a 0.072-wide sliver at
    the top of the compression range), and the virtual tension sensor is asserted for
    essentially every reading a real sensor produces - including a perfectly centred
    filament. Sync-feedback gear compensation reads permanent tension.

    `low` should be negative: -(threshold - h) = -0.864, or -(threshold + h) = -0.936
    depending on which side hysteresis is meant to widen. That choice is a design call,
    which is why this is reported rather than fixed here.

    Reachable with shipped defaults on any machine with an analog buffer sensor - EMU
    ships one.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.prop = self.hh.sensor('filament_proportional')
        self.sensor = self.prop.sensor
        self.sm = self.hh.mmu.sensor_manager

    def tearDown(self):
        self.hh.close()

    def feed_normalised(self, value):
        """Feed a raw reading that normalises to `value` in [-1, +1]."""
        span = self.sensor._d_pos if value >= 0 else self.sensor._d_neg
        self.prop.feed(self.sensor._neutral_point + value * span)
        return self.sensor.value

    def test_both_thresholds_are_currently_positive(self):
        """The mechanism, pinned directly - this is the whole bug in one assertion."""
        self.assertGreater(self.sensor._vsensor_threshold_low, 0.0,
                           'the tension threshold should be negative')
        self.assertAlmostEqual(self.sensor._vsensor_threshold_low, 0.864, places=3)
        self.assertAlmostEqual(self.sensor._vsensor_threshold_high, 0.936, places=3)

    def test_a_centred_filament_currently_reports_tension(self):
        """Documents today's behaviour so a fix shows up as a change here."""
        self.feed_normalised(0.0)
        self.assertTrue(self.sm.check_sensor('filament_tension'),
                        'precondition for the bug: 0.0 < +0.864 so it reads as tension')
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    def test_the_neutral_band_is_a_sliver_at_the_top(self):
        self.feed_normalised(0.90)      # inside +0.864..+0.936
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    @unittest.expectedFailure
    def test_a_centred_filament_should_read_neither(self):
        """
        What the config help promises. Flips green when the sign is corrected; delete this
        and invert test_a_centred_filament_currently_reports_tension then.
        """
        self.feed_normalised(0.0)
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertFalse(self.sm.check_sensor('filament_compression'))

    @unittest.expectedFailure
    def test_tension_should_trigger_near_minus_one(self):
        """Tension at -1.0 and nothing at -0.5, per the documented -0.9 trigger point."""
        self.feed_normalised(-1.0)
        self.assertTrue(self.sm.check_sensor('filament_tension'))
        self.feed_normalised(-0.5)
        self.assertFalse(self.sm.check_sensor('filament_tension'))

    def test_compression_end_is_unaffected(self):
        """The positive side works; only the tension threshold has the wrong sign."""
        self.feed_normalised(1.0)
        self.assertTrue(self.sm.check_sensor('filament_compression'))
        self.assertFalse(self.sm.check_sensor('filament_tension'))


class TestAdcCompatMatrixOnRealMachine(unittest.TestCase):
    """
    The ADC compat shim across all six combinations, on a real machine rather than in
    isolation: 3 Klipper API generations x 2 callback payload shapes. Only one combination
    ever runs on a given Klipper, so the rest is dead code that only a matrix reaches.
    (test_mmu_adc_compat.py covers the shim's own logic; this proves a whole machine boots
    and reads correctly under each.)
    """

    def test_every_api_and_payload_combination_boots_and_reads(self):
        for api in ('new', 'old', 'oldest'):
            for payload in ('pair', 'samples'):
                with self.subTest(api=api, payload=payload):
                    hh = session('emu', adc_api=api, adc_payload=payload)
                    try:
                        hh.boot()
                        self.assertEqual(hh.errors, [])
                        prop = hh.sensor('filament_proportional')
                        # resting value derived through this API/payload combination
                        self.assertAlmostEqual(prop.value, -1.0, places=2)
                        prop.feed(prop.neutral_value() + prop.sensor._d_pos)
                        self.assertAlmostEqual(prop.value, 1.0, places=2)
                    finally:
                        hh.close()


if __name__ == '__main__':
    unittest.main()
