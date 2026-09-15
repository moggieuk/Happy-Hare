# Happy Hare test harness - the sync-feedback state STRING.
#
# get_sync_feedback_string() is what Mainsail's buffer indicator renders and what
# MMU_STATUS / MMU_SYNC_FEEDBACK print. It had no coverage at all, which is how it
# shipped unable to say "neutral" on a proportional sensor: it sign-tested the raw
# ADC float, so neutral required the reading to be exactly 0.0.
#
# The fix asks _get_sensor_state() for the DISCRETE state, which the virtual
# tension/compression sensors already derive by threshold. These tests pin the
# resulting contract on both sensor families:
#
#   emu  proportional (type-P) - the regression this file exists for
#   kms  dual switches (type-D) - already worked; must not change
#
# A proportional sensor is driven by feeding a RAW ADC value and letting the virtual
# sensors derive from it; forcing them directly leaves them stuck, because derivation
# only re-evaluates on a threshold crossing (see test/hh/bootstrap.py).

import unittest

from test.hh import session


class TestProportionalSyncFeedbackString(unittest.TestCase):
    """
    EMU's analog buffer. The reported string must follow the same thresholds as the
    derived tension/compression sensors, so that the indicator agrees with what Happy
    Hare itself believes about the buffer.
    """

    def setUp(self):
        self.hh = session('emu')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.prop = self.hh.sensor('filament_proportional')
        self.sensor = self.prop.sensor
        self.sm = self.hh.mmu.sensor_manager
        self.sf = self.hh.mmu.mmu_unit(0).sync_feedback

    def tearDown(self):
        self.hh.close()

    def feed_normalised(self, value):
        """Feed a raw reading that normalises to `value` in [-1, +1]."""
        span = self.sensor._d_pos if value >= 0 else self.sensor._d_neg
        self.prop.feed(self.sensor._neutral_point + value * span)
        return self.sensor.value

    def string(self):
        # detail=True reports the state regardless of whether sync is currently active,
        # which is how MMU_STATUS and MMU_SYNC_FEEDBACK call it.
        return self.sf.get_sync_feedback_string(detail=True)

    def test_a_mid_range_buffer_reports_neutral(self):
        """
        The regression. A buffer sitting at half tension is not in tension as far as
        the machine is concerned - both virtual sensors read False - so the string must
        not claim it is. Before the fix this returned 'tension' for every reading except
        an exact 0.0, which an ADC never produces.
        """
        self.feed_normalised(-0.5)
        self.assertFalse(self.sm.check_sensor('filament_tension'))
        self.assertFalse(self.sm.check_sensor('filament_compression'))
        self.assertEqual(self.string(), 'neutral')

    def test_a_centred_buffer_reports_neutral(self):
        self.feed_normalised(0.0)
        self.assertEqual(self.string(), 'neutral')

    def test_a_near_zero_reading_reports_neutral(self):
        """An ADC lands just off zero; that must not read as compression."""
        self.feed_normalised(0.001)
        self.assertNotAlmostEqual(self.sensor.value, 0.0, places=6)
        self.assertEqual(self.string(), 'neutral')

    def test_extremes_still_report_tension_and_compression(self):
        """The neutral band must not swallow a real buffer extreme."""
        self.feed_normalised(-0.95)
        self.assertEqual(self.string(), 'tension')
        self.feed_normalised(0.95)
        self.assertEqual(self.string(), 'compressed')

    def test_resting_state_still_reports_tension(self):
        """EMU declares buffer_spring_state: tension, so it boots pegged at that end."""
        self.assertAlmostEqual(self.prop.value, -1.0, places=2)
        self.assertEqual(self.string(), 'tension')

    def test_string_follows_the_virtual_sensor_hysteresis(self):
        """
        The derived sensors assert at +/-0.900 and release one hysteresis interval
        inward at +/-0.864. The string must track that rather than re-deciding on the
        raw float, so the indicator cannot chatter at the boundary.
        """
        self.feed_normalised(-0.91)
        self.assertEqual(self.string(), 'tension')
        self.feed_normalised(-0.88)
        self.assertEqual(self.string(), 'tension',
                         'should stay in tension inside the hysteresis band')
        self.feed_normalised(-0.86)
        self.assertEqual(self.string(), 'neutral')

    def test_string_agrees_with_the_derived_sensors_across_the_range(self):
        """Whatever the thresholds are configured to, the two must never disagree."""
        for value in (-1.0, -0.95, -0.5, -0.1, 0.0, 0.1, 0.5, 0.95, 1.0):
            with self.subTest(value=value):
                # Re-centre first so hysteresis cannot carry state between cases.
                self.feed_normalised(0.0)
                self.feed_normalised(value)
                tension = bool(self.sm.check_sensor('filament_tension'))
                compression = bool(self.sm.check_sensor('filament_compression'))
                expected = ('tension' if tension else
                            'compressed' if compression else 'neutral')
                self.assertEqual(self.string(), expected)


class TestSwitchSyncFeedbackString(unittest.TestCase):
    """
    KMS has real tension and compression switches. This path already reported neutral
    correctly and must be unchanged by the proportional fix.
    """

    def setUp(self):
        self.hh = session('kms')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [])
        self.sf = self.hh.mmu.mmu_unit(0).sync_feedback

    def tearDown(self):
        self.hh.close()

    def set_switches(self, tension, compression):
        self.hh.sensor('filament_tension').set(tension)
        self.hh.sensor('filament_compression').set(compression)

    def string(self):
        return self.sf.get_sync_feedback_string(detail=True)

    def test_neither_switch_is_neutral(self):
        self.set_switches(False, False)
        self.assertEqual(self.string(), 'neutral')

    def test_tension_switch(self):
        self.set_switches(True, False)
        self.assertEqual(self.string(), 'tension')

    def test_compression_switch(self):
        self.set_switches(False, True)
        self.assertEqual(self.string(), 'compressed')

    def test_both_switches_is_neutral(self):
        """Contradictory switches are treated as neutral rather than as an error."""
        self.set_switches(True, True)
        self.assertEqual(self.string(), 'neutral')


if __name__ == '__main__':
    unittest.main()
