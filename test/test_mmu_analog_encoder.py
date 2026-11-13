# Happy Hare test harness - the analog wheel encoder counter (MmuAnalogCounter).
#
# MmuAnalogCounter (extras/mmu/unit/mmu_analog_counter.py) turns one analog wheel sensor per
# gate into encoder pulses for MmuEncoder: a pulse each time the selected gate's reading
# crosses analog_high going up or analog_low going down. Like MmuAdcHelper it is driven
# entirely by ADC callbacks, so most of it is tested straight, with a fake config, pins and
# ADCs, the way test_mmu_adc_compat tests the shim. The installer's analog encoder type
# (CHOICE_ENCODER_TYPE_ANALOG) is tested through Kconfig and the rendered config, and the
# encoder_analog profile boots a whole machine on it.
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

PINS = {'analog_pin_0': 'unit0:PC1', 'analog_pin_1': 'unit0:PC3',
        'analog_pin_2': 'unit0:PB13', 'analog_pin_3': 'unit0:PB15'}

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

    def getlist(self, option, default=None):
        value = self._options.get(option)
        if value is None:
            return default
        return [v.strip() for v in value.split(',') if v.strip()]

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
        self.handlers = {}

    def lookup_object(self, name):
        assert name == 'pins', name
        return self.pins

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        for cb in self.handlers.get(event, []):
            cb(*args)


class FakeUnit:
    """A unit with gates first_gate .. first_gate+num_gates-1."""

    def __init__(self, num_gates=4, first_gate=0):
        self.num_gates = num_gates
        self.first_gate = first_gate

    def manages_gate(self, gate):
        return self.first_gate <= gate < self.first_gate + self.num_gates

    def local_gate(self, gate):
        return gate - self.first_gate


class CounterTestCase(unittest.TestCase):

    def make(self, adc_class=BatchAdc, unit=None, **options):
        self.printer = FakePrinter(adc_class)
        opts = dict(PINS)
        opts.update(options)
        self.counter = MmuAnalogCounter(FakeConfig(self.printer, opts), unit or FakeUnit())
        self.reports = []
        self.counter.setup_callback(lambda *args: self.reports.append(args))
        self.t = 0.
        return self.counter

    def adc(self, gate):
        return self.printer.pins.adcs[self.counter._pins[gate]]

    def feed(self, gate, values, batch=8, dt=0.02):
        """Deliver readings to a gate's ADC the way batched Klipper does."""
        cb = self.adc(gate).callback
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
        self.feed(0, self.turns(1))
        first = self.counter.count
        self.feed(0, self.turns(10))
        self.assertEqual(self.counter.count - first, 20)

    def test_first_reading_sets_the_state_without_a_pulse(self):
        """Booting with the wheel anywhere must not count: only crossings do."""
        self.make()
        self.feed(0, [0.81])
        self.assertEqual(self.counter.count, 0)
        self.feed(0, [0.50])
        self.assertEqual(self.counter.count, 1)

    def test_noise_inside_the_band_counts_nothing(self):
        self.make()
        self.feed(0, [0.60, 0.70, 0.61, 0.69, 0.65] * 20)
        self.assertEqual(self.counter.count, 0)

    def test_noise_around_one_threshold_counts_once(self):
        """Chatter at analog_high counts the first crossing only: it never reaches low."""
        self.make()
        self.feed(0, [0.50] + [0.71, 0.73] * 20)
        self.assertEqual(self.counter.count, 1)

    def test_a_standing_wheel_counts_nothing(self):
        self.make()
        self.feed(0, self.turns(1))
        before = self.counter.count
        self.feed(0, [WHEEL[-1]] * 200)     # stopped where the turn ended
        self.assertEqual(self.counter.count, before)

    def test_thresholds_are_configurable(self):
        self.make(analog_high='0.79', analog_low='0.52')
        self.feed(0, self.turns(1))
        first = self.counter.count
        self.feed(0, self.turns(5))
        self.assertEqual(self.counter.count - first, 10)
        self.feed(0, [0.77, 0.53] * 10)     # inside the narrower band now
        self.assertEqual(self.counter.count - first, 10)


class TestCallbackContract(CounterTestCase):
    """What MmuEncoder._counter_callback relies on, as pulse_counter.MCU_counter gives it."""

    def test_reports_once_per_batch_with_the_running_count(self):
        self.make()
        self.feed(0, self.turns(2), batch=13)
        self.assertEqual(len(self.reports), 2)
        self.assertEqual(self.reports[-1][1], self.counter.count)

    def test_print_time_is_the_last_sample_and_count_time_the_last_pulse(self):
        self.make()
        self.feed(0, [0.81, 0.50, 0.50, 0.50], batch=4)    # pulse at the 2nd sample
        print_time, count, count_time = self.reports[-1]
        self.assertAlmostEqual(print_time, 0.08)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(count_time, 0.04)

    def test_count_time_stays_while_the_wheel_stands(self):
        """MmuEncoder sees no new counts when count_time doesn't move past its last one."""
        self.make()
        self.feed(0, [0.81, 0.50], batch=2)
        pulse_time = self.reports[-1][2]
        self.feed(0, [0.50] * 16)
        self.assertEqual(self.reports[-1][2], pulse_time)

    def test_old_pair_payload(self):
        """Older Klipper calls back with (read_time, value), one sample at a time."""
        self.make()
        cb = self.adc(0).callback
        for i, v in enumerate(self.turns(3)):
            cb(0.02 * i, v)
        self.assertEqual(self.counter.count, 5)
        self.assertEqual(len(self.reports), len(self.turns(3)))

    def test_empty_batch_is_ignored(self):
        self.make()
        self.adc(0).callback([])
        self.assertEqual(self.reports, [])


class TestGates(CounterTestCase):

    def test_only_the_selected_gate_counts(self):
        self.make()
        self.feed(1, self.turns(5))
        self.assertEqual(self.counter.count, 0)
        self.assertEqual(self.reports, [], 'other gates must not report to the encoder')
        self.assertEqual(self.counter.values_raw[1], WHEEL[-1])

    def test_selecting_a_gate_switches_the_wheel(self):
        self.make()
        self.printer.send_event('mmu:gate_selected', 2, 0)
        self.assertEqual(self.counter.active_gate, 2)
        self.feed(0, self.turns(3))
        self.assertEqual(self.counter.count, 0)
        self.feed(2, self.turns(3))
        self.assertEqual(self.counter.count, 5)

    def test_switching_gates_counts_no_phantom_pulse(self):
        """Gate 0 last read high, gate 1 rests low: the switch itself is not a crossing."""
        self.make()
        self.feed(0, [0.50, 0.81])
        before = self.counter.count
        self.feed(1, [0.50])
        self.printer.send_event('mmu:gate_selected', 1, 0)
        self.feed(1, [0.50, 0.50])
        self.assertEqual(self.counter.count, before)

    def test_gates_of_other_units_are_ignored(self):
        self.make(unit=FakeUnit(num_gates=4, first_gate=4))
        self.printer.send_event('mmu:gate_selected', 1, -1)    # unit 0's gate
        self.assertEqual(self.counter.active_gate, 0)
        self.printer.send_event('mmu:gate_selected', 6, -1)    # this unit's local gate 2
        self.assertEqual(self.counter.active_gate, 2)

    def test_bypass_and_unknown_gate_keep_the_wheel(self):
        self.make()
        self.printer.send_event('mmu:gate_selected', 3, 0)
        for gate in (-1, -2, None):
            self.printer.send_event('mmu:gate_selected', gate, 3)
        self.assertEqual(self.counter.active_gate, 3)


class TestSetup(CounterTestCase):

    def test_batched_reports_when_klipper_has_them(self):
        self.make(analog_report_time='0.02', analog_batch='8')
        for gate in range(4):
            self.assertEqual(self.adc(gate).sample_args[0], 0.02)
            self.assertEqual(self.adc(gate).sample_args[3], 8)

    def test_unbatched_klipper_falls_back_to_the_adc_shim(self):
        self.make(adc_class=UnbatchedAdc)
        for gate in range(4):
            self.assertEqual(len(self.adc(gate).sample_args), 3)
            self.assertIsNotNone(self.adc(gate).callback)
        self.feed(0, self.turns(2))
        self.assertEqual(self.counter.count, 3)

    def test_every_gate_needs_a_pin(self):
        """A gate without a sensor would read as a clog to FlowGuard: refused."""
        with self.assertRaises(ConfigError) as ctx:
            self.make(analog_pin_1='', analog_pin_3=None)
        self.assertIn('analog_pin_1', str(ctx.exception))
        self.assertIn('analog_pin_3', str(ctx.exception))
        self.assertNotIn('analog_pin_2', str(ctx.exception))

    def test_configured_means_any_analog_pin(self):
        printer = FakePrinter()
        self.assertTrue(MmuAnalogCounter.configured(FakeConfig(printer, PINS), 4))
        self.assertTrue(MmuAnalogCounter.configured(
            FakeConfig(printer, {'analog_pin_2': 'unit0:PB13'}), 4))
        self.assertFalse(MmuAnalogCounter.configured(
            FakeConfig(printer, {'encoder_pin': 'unit0:PA6'}), 4))

    def test_low_must_be_below_high(self):
        with self.assertRaises(ConfigError):
            self.make(analog_high='0.6', analog_low='0.7')

    def test_status(self):
        self.make()
        self.feed(0, self.turns(2))
        status = self.counter.get_status(0.)
        self.assertEqual(status['active_gate'], 0)
        self.assertEqual(status['count'], self.counter.count)
        self.assertEqual(len(status['values_raw']), 4)


class TestInstaller(unittest.TestCase):
    """menuconfig's 'Analog sensor per gate' encoder type."""

    ANALOG = {'MMU_HAS_ENCODER': True, 'CHOICE_ENCODER_TYPE_ANALOG': True}

    @staticmethod
    def kconfig(name, syms):
        from test.hh import cfg
        with cfg._env(dict(cfg._SINGLE_UNIT_ENV)):
            return cfg._kconfig(name, syms)

    def test_renders_analog_pins_instead_of_encoder_pin(self):
        from test.hh import cfg, profiles
        parser = cfg.assemble(cfg.render(profiles.ENCODER_ANALOG))
        section = dict(parser.items('mmu_encoder unit0'))
        self.assertEqual([section.get('analog_pin_%d' % i) for i in range(5)],
                         ['unit0:PC0', 'unit0:PC1', 'unit0:PC2', 'unit0:PC3', None])
        self.assertEqual(float(section['analog_high']), 0.72)
        self.assertEqual(float(section['analog_low']), 0.58)
        self.assertEqual(float(section['encoder_resolution']), 5.0)
        self.assertNotIn('encoder_pin', section)
        blank = [k for k, v in section.items() if not str(v).strip()]
        self.assertEqual(blank, [], 'unpopulated keys in [mmu_encoder unit0]')

    def test_a_pin_encoder_still_renders_encoder_pin(self):
        from test.hh import cfg, profiles
        section = dict(cfg.assemble(cfg.render(profiles.ENCODER)).items('mmu_encoder unit0'))
        self.assertIn('encoder_pin', section)
        self.assertNotIn('analog_pin_0', section)

    def assert_not_encoder_homing(self, choice, base):
        """With a pin encoder `choice` defaults to the encoder; with an analog one it doesn't."""
        encoder = choice + '_ENCODER'
        pin = self.kconfig('pin_' + choice, dict(base, MMU_HAS_ENCODER=True))
        self.assertEqual(pin.named_choices[choice].selection.name, encoder,
                         'control: a pin encoder is still the default')
        analog = self.kconfig('analog_' + choice, dict(base, **self.ANALOG))
        self.assertNotEqual(analog.named_choices[choice].selection.name, encoder)

    def test_gate_homing_preference_skips_an_analog_encoder(self):
        """It counts on the host, so encoder homing would stop late: not a default.
        ERCF prefers encoder gate homing (PREFERS_CHOICE_GATE_HOMING_ENDSTOP_ENCODER); with
        a shared exit sensor as well there is something else to fall back to."""
        self.assert_not_encoder_homing('CHOICE_GATE_HOMING_ENDSTOP', {
            'MMU_FAMILY_ERCF': True, 'MMU_TYPE_ERCF_2_0': True, 'MMU_HAS_SENSOR_SHARED_EXIT': True})

    def test_extruder_homing_fallback_skips_an_analog_encoder(self):
        from test.hh import profiles
        self.assert_not_encoder_homing('CHOICE_EXTRUDER_HOMING_ENDSTOP',
                                       profiles.get('tradrack').syms)

    def test_a_gate_without_a_pin_warns(self):
        from test.hh import profiles
        syms = dict(profiles.ENCODER_ANALOG.syms, PIN_ENCODER_ANALOG_2='')
        kc = self.kconfig('analog_missing_pin', syms)
        self.assertTrue(kc.is_enabled('W36'))
        self.assertFalse(kc.is_enabled('W8'), 'no encoder_pin is expected')

    def test_all_pins_set_is_no_warning(self):
        from test.hh import profiles
        kc = self.kconfig('analog_all_pins', profiles.ENCODER_ANALOG.syms)
        self.assertFalse(kc.is_enabled('W36'))
        self.assertFalse(kc.is_enabled('W8'))

    def test_only_the_unit_gates_are_asked_for(self):
        from test.hh import profiles
        kc = self.kconfig('analog_prompts', profiles.ENCODER_ANALOG.syms)
        visible = [i for i in range(12)
                   if kc.syms['PIN_ENCODER_ANALOG_%d' % i].visibility]
        self.assertEqual(visible, [0, 1, 2, 3])
        self.assertEqual(kc.syms['PIN_ENCODER'].visibility, 0)


class TestMachine(unittest.TestCase):
    """A whole machine (encoder_analog profile) counting through the real MmuEncoder."""

    def setUp(self):
        from test.hh import session
        self.hh = session('encoder_analog')
        self.hh.boot()
        self.assertEqual(self.hh.errors, [], 'bootup was not clean')
        self.encoder = self.hh.mmu.mmu_unit(0).encoder

    def tearDown(self):
        self.hh.close()

    def turn(self, gate, turns=1):
        """Turn a gate's wheel. Samples need increasing read times, as from a real MCU:
        MmuEncoder only takes counts newer than the last ones it saw."""
        adc = self.encoder.counter.mcu_adcs[gate]
        self.t = getattr(self, 't', 100.)
        for v in WHEEL * turns:
            self.t += 0.02
            adc.feed(v, read_time=self.t)

    def test_the_encoder_counts_from_the_analog_pins(self):
        self.assertIsInstance(self.encoder.counter, MmuAnalogCounter)
        for pin in ('unit0:PC0', 'unit0:PC1', 'unit0:PC2', 'unit0:PC3'):
            self.hh.pins.assert_bound(pin, 'adc')

    def test_wheel_turns_are_encoder_distance(self):
        self.turn(0)
        start = self.encoder.get_distance()
        self.turn(0, 10)
        self.assertAlmostEqual(self.encoder.get_distance() - start,
                               20 * self.encoder.get_resolution())

    def test_the_selected_gate_is_counted(self):
        self.hh.run_gcode('MMU_SELECT GATE=2')
        self.assertEqual(self.encoder.counter.active_gate, 2)
        start = self.encoder.get_distance()
        self.turn(0, 5)
        self.assertEqual(self.encoder.get_distance(), start)
        self.turn(2, 5)
        self.assertGreater(self.encoder.get_distance(), start)


if __name__ == '__main__':
    unittest.main()
