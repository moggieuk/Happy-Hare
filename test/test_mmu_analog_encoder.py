# Happy Hare test harness - an analog input as an encoder (MmuAnalogCounter).
#
# MmuAnalogCounter (extras/mmu/unit/mmu_analog_counter.py) is a counter backend for one
# physical analog input: a pulse each time the reading crosses analog_high going up or
# analog_low going down. Like MmuAdcHelper it is driven entirely by ADC callbacks, so most of
# it is tested straight, with a fake config, pins and ADC, the way test_mmu_adc_compat tests the
# shim. The installer's analog encoder type (CHOICE_ENCODER_TYPE_ANALOG) is tested through
# Kconfig and the rendered config, and the encoder_analog profile boots a whole machine on it:
# that is where the uses an analog encoder can't support (homing, move validation) are refused.
#
# The waveform is the one measured on a Flashforge AD5X IFS stall sensor (a Hall sensor on
# a magnet wheel): from ~0.66 up to a ~0.81 peak, down to a ~0.50 dip and back, once per
# ~10.5 mm of filament. Between the default thresholds (0.72, 0.58) that is two pulses per
# turn.
#
#   ./venv/bin/python -m unittest test.test_mmu_analog_encoder
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import unittest

from test.hh import install

install()   # make `extras.*` importable

from extras.mmu.unit.mmu_analog_counter import MmuAnalogCounter  # noqa: E402

PIN = 'unit0:PC1'

# One wheel turn as measured (raw, fraction of full scale)
WHEEL = [0.66, 0.74, 0.78, 0.81, 0.75, 0.66, 0.56, 0.515, 0.50, 0.50, 0.51, 0.54, 0.60]


class ConfigError(Exception):
    pass


class FakeConfig:
    error = ConfigError

    def __init__(self, printer, options, name='mmu_encoder unit0'):
        self._printer = printer
        self._options = dict(options)
        self._name = name

    def get_printer(self):
        return self._printer

    def get_name(self):
        return self._name

    def get(self, option, default=None):
        return self._options.get(option, default)

    def getfloat(self, option, default=None, minval=None, maxval=None, above=None, below=None):
        value = float(self._options.get(option, default))
        if below is not None and value >= below:
            raise ConfigError('%s must be below %s' % (option, below))
        return value

    def getint(self, option, default=None, minval=None, maxval=None):
        return int(self._options.get(option, default))


class BatchAdc:
    """Klipper with batched ADC reports: setup_adc_sample takes batch_num."""

    def __init__(self, pin):
        self.pin = pin
        self.sample_args = None
        self.callback = None

    def setup_adc_sample(self, report_time, sample_time=0., sample_count=1, batch_num=1,
                         minval=0., maxval=1., range_check_count=0):
        self.sample_args = (report_time, sample_time, sample_count, batch_num)

    def setup_adc_callback(self, callback):
        self.callback = callback


class UnbatchedAdc:
    """
    Klipper before batched reports: no batch_num. MUST be declared without **kwargs so
    the counter's batch_num call raises a genuine TypeError and takes MmuAdcHelper's path.
    """

    def __init__(self, pin):
        self.pin = pin
        self.sample_args = None
        self.callback = None

    def setup_adc_sample(self, report_time, sample_time, sample_count):
        self.sample_args = (report_time, sample_time, sample_count)

    def setup_adc_callback(self, callback):
        self.callback = callback


class FakePins:
    def __init__(self, adc_class):
        self.adc_class = adc_class
        self.adcs = {}

    def setup_pin(self, pin_type, pin):
        assert pin_type == 'adc', pin_type
        adc = self.adc_class(pin)
        self.adcs[pin] = adc
        return adc


class FakePrinter:
    def __init__(self, adc_class=BatchAdc):
        self.pins = FakePins(adc_class)

    def lookup_object(self, name):
        assert name == 'pins', name
        return self.pins


class CounterTestCase(unittest.TestCase):

    def make(self, adc_class=BatchAdc, **options):
        self.printer = FakePrinter(adc_class)
        self.counter = MmuAnalogCounter(FakeConfig(self.printer, options), PIN)
        self.reports = []
        self.counter.setup_callback(lambda *args: self.reports.append(args))
        self.t = 0.
        return self.counter

    def adc(self):
        return self.printer.pins.adcs[PIN]

    def feed(self, values, batch=8, dt=0.02):
        """Deliver readings the way batched Klipper does."""
        cb = self.adc().callback
        for i in range(0, len(values), batch):
            samples = []
            for v in values[i:i + batch]:
                self.t += dt
                samples.append((self.t, v))
            cb(samples)

    def turns(self, n):
        return WHEEL * n


class TestCounting(CounterTestCase):

    def test_two_pulses_per_wheel_turn(self):
        self.make()
        self.feed(self.turns(1))
        first = self.counter.count
        self.feed(self.turns(10))
        self.assertEqual(self.counter.count - first, 20)

    def test_first_reading_sets_the_state_without_a_pulse(self):
        """Booting with the wheel anywhere must not count: only crossings do."""
        self.make()
        self.feed([0.81])
        self.assertEqual(self.counter.count, 0)
        self.feed([0.50])
        self.assertEqual(self.counter.count, 1)

    def test_the_state_is_kept_however_long_the_wheel_stands(self):
        """Last seen low, then high after any pause: that crossing is a pulse, never lost."""
        self.make()
        self.feed([0.50])
        self.feed([0.66] * 500)     # inside the band for a long time
        self.feed([0.81])
        self.assertEqual(self.counter.count, 1)

    def test_noise_inside_the_band_counts_nothing(self):
        self.make()
        self.feed([0.60, 0.70, 0.61, 0.69, 0.65] * 20)
        self.assertEqual(self.counter.count, 0)

    def test_noise_around_one_threshold_counts_once(self):
        """Chatter at analog_high counts the first crossing only: it never reaches low."""
        self.make()
        self.feed([0.50] + [0.71, 0.73] * 20)
        self.assertEqual(self.counter.count, 1)

    def test_a_standing_wheel_counts_nothing(self):
        self.make()
        self.feed(self.turns(1))
        before = self.counter.count
        self.feed([WHEEL[-1]] * 200)     # stopped where the turn ended
        self.assertEqual(self.counter.count, before)

    def test_thresholds_are_configurable(self):
        self.make(analog_high='0.79', analog_low='0.52')
        self.feed(self.turns(1))
        first = self.counter.count
        self.feed(self.turns(5))
        self.assertEqual(self.counter.count - first, 10)
        self.feed([0.77, 0.53] * 10)     # inside the narrower band now
        self.assertEqual(self.counter.count - first, 10)


class TestCallbackContract(CounterTestCase):
    """What MmuEncoder._counter_callback relies on, as pulse_counter.MCU_counter gives it."""

    def test_reports_once_per_batch_with_the_running_count(self):
        self.make()
        self.feed(self.turns(2), batch=13)
        self.assertEqual(len(self.reports), 2)
        self.assertEqual(self.reports[-1][1], self.counter.count)

    def test_print_time_is_the_last_sample_and_count_time_the_last_pulse(self):
        self.make()
        self.feed([0.81, 0.50, 0.50, 0.50], batch=4)    # pulse at the 2nd sample
        print_time, count, count_time = self.reports[-1]
        self.assertAlmostEqual(print_time, 0.08)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(count_time, 0.04)
        self.assertAlmostEqual(self.counter.last_sample_time, 0.08)

    def test_count_time_stays_while_the_wheel_stands(self):
        """MmuEncoder sees no new counts when count_time doesn't move past its last one."""
        self.make()
        self.feed([0.81, 0.50], batch=2)
        pulse_time = self.reports[-1][2]
        self.feed([0.50] * 16)
        self.assertEqual(self.reports[-1][2], pulse_time)

    def test_old_pair_payload(self):
        """Older Klipper calls back with (read_time, value), one sample at a time."""
        self.make()
        cb = self.adc().callback
        for i, v in enumerate(self.turns(3)):
            cb(0.02 * i, v)
        self.assertEqual(self.counter.count, 5)
        self.assertEqual(len(self.reports), len(self.turns(3)))

    def test_empty_batch_is_ignored(self):
        self.make()
        self.adc().callback([])
        self.assertEqual(self.reports, [])


class TestSetup(CounterTestCase):

    def test_setup_minmax_api_uses_the_compatibility_shim(self):
        from test.test_mmu_adc_compat import OldestApiAdc
        self.make(adc_class=lambda pin: OldestApiAdc())
        self.assertEqual(self.adc().calls[0], ('minmax', 0.001, 4))
        self.assertEqual(self.adc().calls[1][:2], ('callback', 0.02))
        self.adc().calls[1][2](0.02, 0.50)
        self.adc().calls[1][2](0.04, 0.81)
        self.assertEqual(self.counter.count, 1)
        self.assertAlmostEqual(self.counter.report_latency(), 0.04)

    def test_batched_reports_when_klipper_has_them(self):
        self.make(analog_report_time='0.02', analog_batch='8')
        self.assertEqual(self.adc().sample_args[0], 0.02)
        self.assertEqual(self.adc().sample_args[3], 8)

    def test_unbatched_klipper_falls_back_to_the_adc_shim(self):
        self.make(adc_class=UnbatchedAdc)
        self.assertEqual(len(self.adc().sample_args), 3)
        self.assertIsNotNone(self.adc().callback)
        self.feed(self.turns(2))
        self.assertEqual(self.counter.count, 3)

    def test_low_must_be_below_high(self):
        with self.assertRaises(ConfigError):
            self.make(analog_high='0.6', analog_low='0.7')

    def test_sampling_limits(self):
        """Counts can lag by a whole batch; a pulse needs a sample in each half of it."""
        self.make(analog_report_time='0.02', analog_batch='8')
        self.assertAlmostEqual(self.counter.report_latency(), 0.18)
        self.assertAlmostEqual(self.counter.max_speed(5.0), 125.0)
        self.make(adc_class=UnbatchedAdc, analog_report_time='0.02')
        self.assertAlmostEqual(self.counter.report_latency(), 0.04)

    def test_status(self):
        self.make()
        self.feed(self.turns(2))
        status = self.counter.get_status(0.)
        self.assertEqual(status['count'], self.counter.count)
        self.assertEqual(status['value'], WHEEL[-1])


class TestInstaller(unittest.TestCase):
    """menuconfig's 'Analog sensor' encoder type."""

    ANALOG = {'MMU_HAS_ENCODER': True, 'CHOICE_ENCODER_TYPE_ANALOG': True}

    @staticmethod
    def kconfig(name, syms):
        from test.hh import cfg
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            return cfg._kconfig(name, syms)

    def test_renders_analog_pin_instead_of_encoder_pin(self):
        from test.hh import cfg, profiles
        parser = cfg.assemble(cfg.render(profiles.ENCODER_ANALOG))
        section = dict(parser.items('mmu_encoder unit0'))
        self.assertEqual(section.get('analog_pin'), 'unit0:PC0')
        self.assertEqual(float(section['analog_high']), 0.72)
        self.assertEqual(float(section['analog_low']), 0.58)
        self.assertEqual(float(section['encoder_resolution']), 5.0)
        self.assertNotIn('encoder_pin', section)
        blank = [k for k, v in section.items() if not str(v).strip()]
        self.assertEqual(blank, [], 'unpopulated keys in [mmu_encoder unit0]')

    def test_move_validation_defaults_off_for_an_analog_encoder(self):
        from test.hh import cfg, profiles
        def value(profile):
            parser = cfg.assemble(cfg.render(profile))
            return [parser.get(sec, 'encoder_move_validation').strip() for sec in parser.sections()
                    if parser.has_option(sec, 'encoder_move_validation')]
        self.assertEqual(value(profiles.ENCODER_ANALOG), ['0'])
        self.assertEqual(value(profiles.ENCODER), ['1'])

    def test_a_pin_encoder_still_renders_encoder_pin(self):
        from test.hh import cfg, profiles
        section = dict(cfg.assemble(cfg.render(profiles.ENCODER)).items('mmu_encoder unit0'))
        self.assertIn('encoder_pin', section)
        self.assertNotIn('analog_pin', section)

    def assert_not_encoder_homing(self, choice, base):
        """With a pin encoder `choice` defaults to the encoder; with an analog one it doesn't."""
        encoder = choice + '_ENCODER'
        pin = self.kconfig('pin_' + choice, dict(base, MMU_HAS_ENCODER=True))
        self.assertEqual(pin.named_choices[choice].selection.name, encoder,
                         'control: a pin encoder is still the default')
        analog = self.kconfig('analog_' + choice, dict(base, **self.ANALOG))
        self.assertNotEqual(analog.named_choices[choice].selection.name, encoder)

    def test_gate_homing_preference_skips_an_analog_encoder(self):
        """It counts on the host and can't stop a homing move: not a default.
        ERCF prefers encoder gate homing (PREFERS_CHOICE_GATE_HOMING_ENDSTOP_ENCODER); with
        a shared exit sensor as well there is something else to fall back to."""
        self.assert_not_encoder_homing('CHOICE_GATE_HOMING_ENDSTOP', {
            'MMU_FAMILY_ERCF': True, 'MMU_TYPE_ERCF_2_0': True, 'MMU_HAS_SENSOR_SHARED_EXIT': True})

    def test_extruder_homing_fallback_skips_an_analog_encoder(self):
        from test.hh import profiles
        self.assert_not_encoder_homing('CHOICE_EXTRUDER_HOMING_ENDSTOP',
                                       profiles.get('tradrack').syms)

    def test_a_blank_analog_pin_warns(self):
        from test.hh import profiles
        kc = self.kconfig('analog_missing_pin', dict(profiles.ENCODER_ANALOG.syms, PIN_ENCODER_ANALOG=''))
        self.assertTrue(kc.is_enabled('W8'))

    def test_a_set_analog_pin_is_no_warning(self):
        from test.hh import profiles
        kc = self.kconfig('analog_pin_set', profiles.ENCODER_ANALOG.syms)
        self.assertFalse(kc.is_enabled('W8'), 'no encoder_pin is expected')
        self.assertEqual(kc.syms['PIN_ENCODER'].visibility, 0)


class TestMachine(unittest.TestCase):
    """A whole machine (encoder_analog profile) counting through the real MmuEncoder."""

    def setUp(self):
        from test.hh import session
        self.hh = session('encoder_analog')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.mmu = self.hh.mmu
        self.encoder = self.mmu.mmu_unit(0).encoder

    def tearDown(self):
        self.hh.close()

    def turn(self, turns=1, read_time=None):
        """Turn the wheel. Samples need increasing read times, as from a real MCU:
        MmuEncoder only takes counts newer than the last ones it saw."""
        adc = self.encoder.counter.mcu_adc
        self.t = getattr(self, 't', 100.) if read_time is None else read_time
        for v in WHEEL * turns:
            self.t += 0.02
            adc.feed(v, read_time=self.t)

    def test_the_encoder_counts_from_the_analog_pin(self):
        self.assertTrue(self.encoder.is_analog())
        self.hh.pins.assert_bound('unit0:PC0', 'adc')

    def test_wheel_turns_are_encoder_distance(self):
        self.turn()
        start = self.encoder.get_distance()
        self.turn(10)
        self.assertAlmostEqual(self.encoder.get_distance() - start,
                               20 * self.encoder.get_resolution())

    def test_selecting_another_gate_keeps_counting_the_same_wheel(self):
        """One physical input, one counter: gate selection doesn't touch it."""
        self.turn()
        self.hh.run_gcode('MMU_SELECT GATE=2')
        start = self.encoder.get_distance()
        self.turn(3)
        self.assertAlmostEqual(self.encoder.get_distance() - start, 6 * self.encoder.get_resolution())

    def test_capabilities(self):
        self.assertFalse(self.encoder.supports_homing())
        self.assertFalse(self.encoder.supports_move_validation())
        self.assertGreater(self.encoder.report_latency(), 0.)
        self.assertAlmostEqual(self.encoder.max_speed(),
                               self.encoder.get_resolution() / (2 * self.encoder.counter.report_time))
        status = self.encoder.get_status(0.)
        self.assertIn('max_speed', status['analog'])

    def test_move_validation_is_off_by_default(self):
        self.assertEqual(self.mmu.mmu_unit(0).p.encoder_move_validation, 0)
        self.assertFalse(self.mmu.can_use_encoder())

    def test_move_validation_is_opt_in(self):
        self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 encoder_move_validation=1')
        self.assertTrue(self.mmu.can_use_encoder())

    def test_encoder_homing_can_be_chosen_at_runtime(self):
        """Opt-in: nothing refuses it, the user has asked for it."""
        for param in ('gate_preload_endstop', 'extruder_homing_endstop'):
            self.hh.run_gcode('MMU_TEST_CONFIG UNIT=0 %s=encoder' % param)
            self.assertEqual(getattr(self.mmu.mmu_unit(0).p, param), 'encoder', param)

    def test_wait_for_counts_waits_for_samples_after_the_call(self):
        reactor = self.hh.reactor
        mcu = self.hh.printer.lookup_object('mcu')
        target = mcu.estimated_print_time(reactor.monotonic())
        # Samples from after the call arrive a little later, as a batch would
        reactor.register_callback(lambda e: self.turn(1, read_time=target + 0.01),
                                  reactor.monotonic() + 0.1)
        start = reactor.monotonic()
        self.encoder.wait_for_counts()
        self.assertGreaterEqual(self.encoder._last_report_time, target)
        self.assertLess(reactor.monotonic() - start, self.encoder.report_latency() + 0.5)

    def test_wait_for_counts_gives_up_without_samples(self):
        reactor = self.hh.reactor
        start = reactor.monotonic()
        self.encoder.wait_for_counts()
        self.assertLessEqual(reactor.monotonic() - start,
                             self.encoder.report_latency() + 0.5 + self.encoder.counter.report_time + 1e-6)


class TestHandWrittenConfig(unittest.TestCase):
    """What a hand-written [mmu_unit_parameters] gets with an analog encoder."""

    @staticmethod
    def boot(edit):
        from test.hh import session
        hh = session('encoder_analog', config_edit=edit)
        try:
            hh.boot()
        except Exception:
            hh.close()
            raise
        return hh

    def test_explicit_encoder_homing_starts_with_a_warning(self):
        def edit(fc):
            fc.set('mmu_unit_parameters unit0', 'gate_homing_endstop', 'encoder')
        with self.assertLogs(level='WARNING') as logs:
            hh = self.boot(edit)
        self.addCleanup(hh.close)
        self.assertEqual(hh.mmu.mmu_unit(0).p.gate_homing_endstop, 'encoder')
        self.assertTrue(any('homing stops late' in line for line in logs.output), logs.output)

    def test_encoder_homing_by_default_is_a_config_error(self):
        """gate_homing_endstop defaults to encoder: with an analog one that must be a choice."""
        def edit(fc):
            fc.remove_option('mmu_unit_parameters unit0', 'gate_homing_endstop')
        with self.assertRaises(Exception) as ctx:
            self.boot(edit)
        self.assertIn('defaults to', str(ctx.exception))

    def test_explicit_move_validation_is_honoured_with_a_warning(self):
        def edit(fc):
            fc.set('mmu_unit_parameters unit0', 'encoder_move_validation', '1')
        with self.assertLogs(level='WARNING') as logs:
            hh = self.boot(edit)
        self.addCleanup(hh.close)
        self.assertTrue(hh.mmu.can_use_encoder())
        self.assertTrue(any('judged short' in line for line in logs.output), logs.output)


if __name__ == '__main__':
    unittest.main()
