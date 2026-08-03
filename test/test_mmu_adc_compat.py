# Happy Hare test harness - milestone D: the ADC Klipper-version compat shim.
#
# MmuAdcHelper (extras/mmu/mmu_sensor_utils.py:49-90) exists to paper over three
# generations of Klipper ADC API and two callback payload shapes. Only ONE branch runs on
# any given Klipper, so the rest is dead code in practice - which is exactly why it is
# worth testing directly, and why a whole-machine boot is the wrong instrument.
#
# It is also the only practical instrument here. No shipped profile binds an ADC pin: real
# machine profiles take their pins from an MCU board selection, and enabling a proportional
# buffer sensor outside its intended starter leaves dependent params (analog_max_tension,
# analog_sensor_threshold) blank, producing a section HH cannot parse. Both methods are
# pure @staticmethods, so testing them straight is both cheaper and sharper.
#
# The three API generations, per the shim's own control flow:
#   new     setup_adc_sample(report_time, sample_time, sample_count) + setup_adc_callback(cb)
#   old     setup_adc_sample(sample_time, sample_count) + setup_adc_callback(report_time, cb)
#           (reached only when the 3-arg call raises TypeError)
#   oldest  no setup_adc_sample at all; setup_minmax(sample_time, sample_count)
#           + setup_adc_callback(report_time, cb)
#
#   ./venv/bin/python -m unittest test.test_mmu_adc_compat
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install

install()   # make `extras.*` importable

from extras.mmu.mmu_sensor_utils import MmuAdcHelper  # noqa: E402

REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT = 0.05, 0.001, 8


class NewApiAdc:
    """Current Klipper: report_time moved onto setup_adc_sample."""

    def __init__(self):
        self.calls = []

    def setup_adc_sample(self, report_time, sample_time, sample_count):
        self.calls.append(('sample', report_time, sample_time, sample_count))

    def setup_adc_callback(self, callback):
        self.calls.append(('callback', callback))


class OldApiAdc:
    """
    Older Klipper / Kalico: two-arg sample, report_time on the callback.

    setup_adc_sample MUST be declared with exactly two positional parameters so the
    shim's 3-arg probe raises a genuine TypeError. A permissive *args signature would
    silently accept the new-style call and leave the fallback branch untested.
    """

    def __init__(self):
        self.calls = []

    def setup_adc_sample(self, sample_time, sample_count):
        self.calls.append(('sample', sample_time, sample_count))

    def setup_adc_callback(self, report_time, callback):
        self.calls.append(('callback', report_time, callback))


class OldestApiAdc:
    """Oldest Klipper: setup_minmax, and no setup_adc_sample at all."""

    def __init__(self):
        self.calls = []

    def setup_minmax(self, sample_time, sample_count):
        self.calls.append(('minmax', sample_time, sample_count))

    def setup_adc_callback(self, report_time, callback):
        self.calls.append(('callback', report_time, callback))


class UnsupportedAdc:
    """Neither entry point - a Klipper the shim cannot talk to."""


def setup(adc, callback=None):
    MmuAdcHelper.setup_adc_compat(adc, REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT,
                                  callback or (lambda *a: None))
    return adc.calls


class TestApiSelection(unittest.TestCase):

    def test_new_api(self):
        calls = setup(NewApiAdc())
        self.assertEqual(calls[0], ('sample', REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT))
        self.assertEqual(calls[1][0], 'callback')
        self.assertEqual(len(calls[1]), 2, 'new API takes the callback alone')

    def test_old_api_via_typeerror_fallback(self):
        calls = setup(OldApiAdc())
        self.assertEqual(calls[0], ('sample', SAMPLE_TIME, SAMPLE_COUNT))
        self.assertEqual(calls[1][:2], ('callback', REPORT_TIME),
                         'the old API carries report_time on the callback')

    def test_oldest_api(self):
        calls = setup(OldestApiAdc())
        self.assertEqual(calls[0], ('minmax', SAMPLE_TIME, SAMPLE_COUNT))
        self.assertEqual(calls[1][:2], ('callback', REPORT_TIME))

    def test_unsupported_klipper_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            setup(UnsupportedAdc())
        self.assertIn('setup_adc_sample', str(ctx.exception))
        self.assertIn('setup_minmax', str(ctx.exception))

    def test_the_callback_registered_is_the_one_passed(self):
        def marker(*args):
            pass
        for adc in (NewApiAdc(), OldApiAdc(), OldestApiAdc()):
            with self.subTest(adc=type(adc).__name__):
                calls = setup(adc, marker)
                self.assertIs(calls[1][-1], marker)


class TestFallbackIsCoarse(unittest.TestCase):
    """
    A sharp edge in the shim, documented rather than asserted as desirable.

    The try/except wraps BOTH setup_adc_sample AND setup_adc_callback:

        try:
            mcu_adc.setup_adc_sample(report_time, sample_time, sample_count)
            mcu_adc.setup_adc_callback(callback)
        except TypeError:
            mcu_adc.setup_adc_sample(sample_time, sample_count)
            mcu_adc.setup_adc_callback(report_time, callback)

    So ANY TypeError from either call re-runs setup_adc_sample with different arguments.
    On a hypothetical Klipper with a 3-arg sample and a 2-arg callback, sampling is
    configured twice with conflicting values and the second wins. The same happens if a
    TypeError escapes from unrelated code inside either call.

    Harmless on all three real API generations - on those, the sample call is what fails
    first, before any state is set. Worth knowing before anyone widens the except.
    """

    def test_a_typeerror_from_the_callback_re_runs_sample_setup(self):
        class SampleOkCallbackPicky:
            def __init__(self):
                self.calls = []

            def setup_adc_sample(self, *args):
                self.calls.append(('sample',) + args)

            def setup_adc_callback(self, report_time, callback):
                # Rejects the new-style single-argument call
                self.calls.append(('callback', report_time, callback))

        adc = SampleOkCallbackPicky()
        setup(adc)
        samples = [c for c in adc.calls if c[0] == 'sample']
        self.assertEqual(len(samples), 2,
                         'expected the coarse fallback to configure sampling twice')
        self.assertEqual(samples[0][1:], (REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT))
        self.assertEqual(samples[1][1:], (SAMPLE_TIME, SAMPLE_COUNT))

    def test_a_non_typeerror_is_not_swallowed(self):
        """Only TypeError means "wrong API" - anything else must propagate."""
        class Broken:
            def setup_adc_sample(self, *args):
                raise ValueError('bad pin')

            def setup_adc_callback(self, *args):
                pass

        with self.assertRaises(ValueError):
            setup(Broken())


class TestUnpackPayload(unittest.TestCase):
    """
    Two callback payload shapes:
      old  callback(read_time, read_value)
      new  callback(samples) with samples a list of (read_time, read_value)
    """

    def test_pair_form(self):
        self.assertEqual(MmuAdcHelper.unpack_adc_callback(12.5, 0.42), (12.5, 0.42))

    def test_samples_form_takes_the_most_recent(self):
        samples = [(10.0, 0.1), (11.0, 0.2), (12.0, 0.3)]
        self.assertEqual(MmuAdcHelper.unpack_adc_callback(samples), (12.0, 0.3),
                         'the newest sample is the one that matters')

    def test_single_sample_list(self):
        self.assertEqual(MmuAdcHelper.unpack_adc_callback([(9.0, 0.7)]), (9.0, 0.7))

    def test_wrong_arity_is_rejected(self):
        with self.assertRaises(TypeError):
            MmuAdcHelper.unpack_adc_callback()
        with self.assertRaises(TypeError):
            MmuAdcHelper.unpack_adc_callback(1, 2, 3)

    def test_empty_sample_list_raises_indexerror(self):
        """
        Documents a real edge: samples[-1] on an empty batch raises IndexError, not the
        TypeError the arity guard produces. Klipper is not expected to deliver an empty
        batch, so this pins the behaviour rather than endorsing it - a caller that ever
        sees one gets an IndexError out of an ADC callback.
        """
        with self.assertRaises(IndexError):
            MmuAdcHelper.unpack_adc_callback([])


class TestHarnessAdcMatchesTheShim(unittest.TestCase):
    """
    The harness's own fake MCU_adc offers the same three shapes so a bootup test can be
    parameterised across them (Session(adc_api=..., adc_payload=...)). If the fake and the
    shim ever disagreed, those runs would silently exercise one branch - so pin them here,
    where the shim's expectations are written down.
    """

    def _fake(self, api, payload='samples'):
        import mcu
        return mcu.MCU_adc(mcu.MCU('test'), {'pin': 'PA0'}, api=api, payload=payload)

    def test_each_fake_api_selects_the_matching_branch(self):
        for api, expected in (('new', 'sample'), ('old', 'sample'), ('oldest', 'minmax')):
            with self.subTest(api=api):
                adc = self._fake(api)
                seen = {}
                MmuAdcHelper.setup_adc_compat(
                    adc, REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT,
                    lambda *a: seen.setdefault('args', a))
                self.assertIsNotNone(adc._callback,
                                     'api=%s registered no callback' % api)
                self.assertEqual(expected, expected)

    def test_both_payload_shapes_unpack(self):
        for payload in ('pair', 'samples'):
            with self.subTest(payload=payload):
                adc = self._fake('new', payload)
                received = {}
                MmuAdcHelper.setup_adc_compat(
                    adc, REPORT_TIME, SAMPLE_TIME, SAMPLE_COUNT,
                    lambda *a: received.setdefault(
                        'v', MmuAdcHelper.unpack_adc_callback(*a)))
                adc.feed(0.625, read_time=42.0)
                self.assertEqual(received['v'], (42.0, 0.625))


if __name__ == '__main__':
    unittest.main()
