# Happy Hare MMU Software
#
# Copyright (C) 2026  Hunter Laux <hunterlaux@gmail.com>
#
# Goal: Encoder pulse counter from one analog input
#
# A counter backend for MmuEncoder, in place of Klipper's pulse_counter.MCU_counter, for a sensor
# whose reading swings as the filament moves: for example a linear Hall sensor on a magnet wheel,
# or an optical sensor read without a comparator. While the filament moves the reading runs
# through its range once per wheel turn (or vane); it stays put while the filament stands still.
#
# One physical input, one counter. It counts a pulse each time the reading crosses `analog_high`
# going up or `analog_low` going down (hysteresis between them, so noise inside the band counts
# nothing), i.e. two pulses per turn, and reports the running count and the time of the last
# pulse the way MCU_counter does. The hysteresis state is kept from the first sample on, whatever
# the MMU is doing, so no crossing is ever lost or invented by a change elsewhere.
#
# SAMPLING LIMITS. The ADC is sampled on the host side of the link: one sample every
# `analog_report_time` (default 20ms), delivered `analog_batch` samples at a time on Klipper with
# batched ADC reports. So:
#   - a pulse is only seen if the reading spends at least one sample past each threshold, which
#     bounds the filament speed to about resolution / (2 * analog_report_time), e.g. 125mm/s for
#     5mm per pulse at 20ms (max_speed() and the encoder's status report the figure)
#   - counts arrive up to report_latency() after the movement that made them. MmuEncoder waits
#     that long for samples before it trusts a reading, but anything that needs the count while a
#     move is still running (encoder homing, the encoder endstop) cannot be supported
#
# [mmu_encoder] options when analog:
#   analog_pin:         the ADC pin (replaces encoder_pin)
#   analog_high:        upper threshold, fraction of ADC full scale (default 0.72)
#   analog_low:         lower threshold, below analog_high (default 0.58)
#   analog_report_time: seconds between samples (default 0.02)
#   analog_batch:       samples per report from the MCU, on Klipper with batched ADC reports
#                       (default 8; keeps the traffic low on a slow MCU link)
#   encoder_resolution: mm per pulse, as for any encoder (MMU_CALIBRATE_ENCODER sets it)
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

# Happy Hare imports
from ..mmu_sensor_utils import MmuAdcHelper


class MmuAnalogCounter:

    SAMPLE_TIME = 0.001     # Between the MCU's conversions within one sample
    SAMPLE_COUNT = 4        # Conversions averaged per sample

    def __init__(self, config, pin):
        self.printer = config.get_printer()
        self.pin = pin
        self.high = config.getfloat('analog_high', 0.72, minval=0., maxval=1.)
        self.low = config.getfloat('analog_low', 0.58, minval=0., below=self.high)
        self.report_time = config.getfloat('analog_report_time', 0.02, minval=0.005)
        self.batch = config.getint('analog_batch', 8, minval=1, maxval=24)

        self._callback = None
        self.value = None           # Latest reading, fraction of full scale
        self._state = None          # 1 above high, 0 below low, None until the first reading past one
        self.count = 0
        self.count_time = 0.        # Print time of the last pulse
        self.last_sample_time = 0.  # Print time of the latest sample received

        ppins = self.printer.lookup_object('pins')
        self.mcu_adc = ppins.setup_pin('adc', pin)
        self._batched = True
        try:
            self.mcu_adc.setup_adc_sample(self.report_time, self.SAMPLE_TIME, self.SAMPLE_COUNT,
                                          batch_num=self.batch)
            self.mcu_adc.setup_adc_callback(self._adc_callback)
        except TypeError:
            # Klipper without batched ADC reports: one sample per report
            self._batched = False
            MmuAdcHelper.setup_adc_compat(self.mcu_adc, self.report_time, self.SAMPLE_TIME,
                                          self.SAMPLE_COUNT, self._adc_callback)
        logging.info("MMU: Created analog encoder counter on %s" % pin)

    # Same interface as pulse_counter.MCU_counter
    def setup_callback(self, cb):
        self._callback = cb

    def report_latency(self):
        """Longest time between a movement and its count reaching the host."""
        return self.report_time * (self.batch if self._batched else 1) + self.report_time

    def max_speed(self, resolution):
        """Fastest filament speed (mm/s) at which every pulse of `resolution` mm is still seen."""
        return resolution / (2. * self.report_time)

    def _adc_callback(self, *args):
        if len(args) == 1:
            samples = args[0]   # Batched: [(read_time, value), ...]
        else:
            samples = [args]    # Older Klipper: (read_time, value)
        if not samples:
            return
        for read_time, value in samples:
            self.value = value
            self.last_sample_time = read_time
            if self._state is None:
                if value >= self.high:
                    self._state = 1
                elif value <= self.low:
                    self._state = 0
            elif self._state == 0 and value >= self.high:
                self._state = 1
                self.count += 1
                self.count_time = read_time
            elif self._state == 1 and value <= self.low:
                self._state = 0
                self.count += 1
                self.count_time = read_time
        if self._callback is not None:
            print_time = samples[-1][0]
            self._callback(print_time, self.count, self.count_time or print_time)

    def get_status(self, eventtime):
        return {
            'value': self.value,
            'count': self.count,
        }
