# Happy Hare MMU Software
#
# Copyright (C) 2026  Hunter Laux <hunterlaux@gmail.com>
#
# Goal: Encoder pulse counter from one analog sensor per gate
#
# An encoder can be read from one analog sensor per gate whose reading swings as the filament
# moves: for example a linear Hall sensor on a magnet wheel, or an optical sensor read without a
# comparator. While the filament moves the reading runs through its range once per wheel turn
# (or vane); it stays put while the filament stands still.
#
# This counts a pulse each time the selected gate's reading crosses `analog_high` going up or
# `analog_low` going down (hysteresis between them, so noise inside the band counts nothing),
# i.e. two pulses per turn, and reports the running count the way Klipper's
# pulse_counter.MCU_counter does. MmuEncoder uses it in place of MCU_counter when [mmu_encoder]
# has `analog_pin_0`..., so everything built on the encoder works unchanged.
#
# The ADCs are sampled on the host (default every 20ms), so this suits coarse sensors (several
# mm per pulse) at moderate speeds; a fine encoder needs a digital encoder_pin.
#
# [mmu_encoder] options when analog:
#   analog_pin_<n>:     ADC pin of gate n, one for every gate of the unit (replaces encoder_pin)
#   analog_high:        upper threshold, fraction of ADC full scale (default 0.72)
#   analog_low:         lower threshold, below analog_high (default 0.58)
#   analog_report_time: seconds between samples (default 0.02)
#   analog_batch:       samples per report from the MCU on Klipper with batched ADC reports
#                       (default 8; keeps the traffic low on a slow MCU link)
#   encoder_resolution: mm per pulse, as for any encoder (MMU_CALIBRATE_ENCODER sets it)
#
# Every gate needs a sensor: on a gate without one the encoder would see no movement and
# FlowGuard would take that for a clog. The pins belong to one unit's gates, so an analog
# encoder can't be shared between units.
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

    @staticmethod
    def pins(config, num_gates):
        """The analog_pin_<n> values for gates 0..num_gates-1 ('' or None where unset)."""
        return [config.get('analog_pin_%d' % gate, None) for gate in range(num_gates)]

    @classmethod
    def configured(cls, config, num_gates):
        """True when [mmu_encoder] reads analog pins instead of an encoder_pin."""
        return any(cls.pins(config, num_gates))

    def __init__(self, config, mmu_unit):
        self.printer = config.get_printer()
        self.mmu_unit = mmu_unit
        self._pins = self.pins(config, mmu_unit.num_gates)
        missing = [gate for gate, pin in enumerate(self._pins) if not pin]
        if missing:
            raise config.error("[%s] needs an analog_pin for every gate, missing: %s" % (
                config.get_name(), ", ".join("analog_pin_%d" % gate for gate in missing)))
        self.high = config.getfloat('analog_high', 0.72, minval=0., maxval=1.)
        self.low = config.getfloat('analog_low', 0.58, minval=0., below=self.high)
        report_time = config.getfloat('analog_report_time', 0.02, minval=0.005)
        batch = config.getint('analog_batch', 8, minval=1, maxval=24)

        self._callback = None
        self.values_raw = [0.] * len(self._pins)
        self.active_gate = 0        # Local gate whose wheel is counted
        self._state = None          # 1 above high, 0 below low, None not yet known
        self.count = 0
        self.count_time = 0.        # Print time of the last pulse

        ppins = self.printer.lookup_object('pins')
        self.mcu_adcs = []
        for idx, pin in enumerate(self._pins):
            adc = ppins.setup_pin('adc', pin)
            cb = lambda *args, g=idx: self._adc_callback(g, *args)
            try:
                adc.setup_adc_sample(report_time, self.SAMPLE_TIME, self.SAMPLE_COUNT,
                                     batch_num=batch)
                adc.setup_adc_callback(cb)
            except TypeError:
                # Klipper without batched ADC reports: one sample per report
                MmuAdcHelper.setup_adc_compat(adc, report_time, self.SAMPLE_TIME,
                                              self.SAMPLE_COUNT, cb)
            self.mcu_adcs.append(adc)

        self.printer.register_event_handler("mmu:gate_selected", self._handle_gate_selected)
        logging.info("MMU: Created analog encoder counter on %s" % ", ".join(self._pins))

    # Same interface as pulse_counter.MCU_counter
    def setup_callback(self, cb):
        self._callback = cb

    def _handle_gate_selected(self, gate, prev_gate):
        if gate is None or gate < 0 or not self.mmu_unit.manages_gate(gate):
            return
        lgate = self.mmu_unit.local_gate(gate)
        if 0 <= lgate < len(self._pins) and lgate != self.active_gate:
            self.active_gate = lgate
            self._state = None  # The new wheel counts from its next threshold crossing

    def _adc_callback(self, gate_idx, *args):
        if len(args) == 1:
            samples = args[0]   # Batched: [(read_time, value), ...]
        else:
            samples = [args]    # Older Klipper: (read_time, value)
        if not samples:
            return
        for read_time, value in samples:
            self.values_raw[gate_idx] = value
            if gate_idx != self.active_gate:
                continue
            if self._state is None:
                if value >= self.high:
                    self._state = 1
                elif value <= self.low:
                    self._state = 0
                if self._state is not None and not self.count_time:
                    self.count_time = read_time
            elif self._state == 0 and value >= self.high:
                self._state = 1
                self.count += 1
                self.count_time = read_time
            elif self._state == 1 and value <= self.low:
                self._state = 0
                self.count += 1
                self.count_time = read_time
        if gate_idx == self.active_gate and self._callback is not None:
            print_time = samples[-1][0]
            self._callback(print_time, self.count, self.count_time or print_time)

    def get_status(self, eventtime):
        return {
            'active_gate': self.active_gate,
            'values_raw': list(self.values_raw),
            'count': self.count,
        }
