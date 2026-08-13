# Shift register support (e.g. 74HC595) as virtual GPIO pin provider
#
# This module implements a chain of shift registers (e.g. 74HC595) as a
# Klipper "chip" that provides virtual digital_out pins addressable by
# bit position. Each virtual pin corresponds to one output bit of the
# shift register chain.
#
# This is used for the Prusa MMU3 which uses the SHR16 (two cascaded
# 74HC595 shift registers) to control stepper direction and enable pins.
#
# The DATA and CLOCK pins are shared with the TMC2130 software SPI bus
# (PB5/PC7 on the MMU3 ATMega32U4 board). To avoid Klipper's pin
# collision check we bypass ppins.setup_pin for those two pins and
# create raw MCU OIDs directly. Time separation ensures they never
# conflict at runtime: SPI runs at config/startup, SR writes run
# during moves.
#
# SHR16 bit mapping for Prusa MMU3:
#   Bit 0: Gear stepper DIR
#   Bit 1: Gear stepper ENABLE (active low)
#   Bit 2: Selector stepper DIR
#   Bit 3: Selector stepper ENABLE (active low)
#   Bit 4: Idler stepper DIR
#   Bit 5: Idler stepper ENABLE (active low)
#   Bit 6-7: Unused stepper / spare
#   Bit 8-15: LED control (SHR16_LED_MSK)
#
# Example config:
#   [shift_register mmu_sr]
#   mcu: mmu
#   num_registers: 2
#   data_pin: mmu:PB5
#   clock_pin: mmu:PC7
#   latch_pin: mmu:PB6
#
# Usage in stepper config:
#   dir_pin: !mmu_sr:0       # Gear DIR (inverted)
#   enable_pin: !mmu_sr:1    # Gear ENABLE (active low, inverted)
#
# Copyright (C) 2025  Hans Maritz
# Based on sx1509.py pattern by Florian Heilmann
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import pins


# on_ticks value used to drive a pin HIGH with max_duration=0.
# Klipper uses 0x80000000 as the "always on" sentinel; any non-zero
# value also works in the MCU firmware (firmware tests `on_ticks != 0`).
_HIGH = 0x80000000
_LOW  = 0


class _RawDigitalOut:
    """
    MCU digital output pin created via raw config commands, bypassing
    Klipper's PrinterPins registry entirely.

    This is used for DATA and CLOCK pins that are physically shared with
    the TMC2130 software SPI bus. Registering them via ppins.setup_pin
    would flag a pin-reuse error; going through the MCU directly avoids
    that check while still functioning identically at runtime.
    """

    def __init__(self, mcu, pin_name):
        self._mcu = mcu
        self._pin_name = pin_name
        self._oid = None
        self._set_cmd = None
        self._cmd_queue = None
        self._last_clock = 0
        mcu.register_config_callback(self._build_config)

    def _build_config(self):
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_digital_out oid=%d pin=%s value=0 default_value=0 max_duration=0"
            % (self._oid, self._pin_name))
        self._cmd_queue = self._mcu.alloc_command_queue()
        self._set_cmd = self._mcu.lookup_command(
            "queue_digital_out oid=%c clock=%u on_ticks=%u",
            cq=self._cmd_queue)

    def get_mcu(self):
        return self._mcu

    # Match the MCU_digital_out interface used by _write_register.
    def setup_max_duration(self, max_duration):
        pass  # hardcoded to 0 in config_cmd above

    def setup_start_value(self, start_value, shutdown_value):
        pass  # initial value=0 in config_cmd above

    def set_digital(self, print_time, value):
        clock = self._mcu.print_time_to_clock(print_time)
        # Use minclock=0 so ALL shift register commands are sent as a burst
        # and arrive at the MCU before their scheduled times. Each command
        # is scheduled with 250-tick spacing so the MCU timer handles
        # in-order execution. With minclock=last_clock the host would pace
        # sends to match the schedule and serial latency (~1ms) would push
        # every command past its scheduled time → "Timer too close".
        self._set_cmd.send([self._oid, clock, _HIGH if value else _LOW],
                           minclock=0, reqclock=clock)
        self._last_clock = clock


class ShiftRegisterBit:
    """
    Virtual digital_out pin backed by a single bit in the shift register.
    This implements the same interface as MCU_digital_out so it can be
    used anywhere a digital_out pin is expected (stepper dir/enable, etc.)
    """

    def __init__(self, sr, bit_num, invert):
        self._sr = sr
        self._bit_num = bit_num
        self._invert = invert
        self._start_value = 0

    def get_mcu(self):
        return self._sr.mcu

    def setup_max_duration(self, max_duration):
        # Shift register holds state indefinitely; max_duration not applicable.
        pass

    def setup_start_value(self, start_value, shutdown_value):
        logical = int(not not start_value)
        physical = logical ^ self._invert
        self._start_value = physical
        self._sr._set_bit(self._bit_num, physical)

    def next_aligned_print_time(self, print_time, allow_early=0.):
        return print_time

    def set_digital(self, print_time, value):
        """Set this bit at the given print_time.

        Returns the actual print_time the write was scheduled at (may be
        deferred behind another in-flight shift register write).
        """
        logical = int(not not value)
        physical = logical ^ self._invert
        return self._sr._set_bit_at_time(self._bit_num, physical, print_time)

    def set_pwm(self, print_time, value):
        """PWM fallback: treat as digital (>0.5 = HIGH)."""
        self.set_digital(print_time, value > 0.5)


class ShiftRegister:
    """
    74HC595 (or compatible) shift register chain as a Klipper pin provider.

    The shift register is controlled via 3 pins:
      - data_pin:  Serial data (MOSI/SER)   — may be shared with SPI MOSI
      - clock_pin: Shift clock (SRCLK/SCK)  — may be shared with SPI SCLK
      - latch_pin: Storage clock (RCLK)     — exclusive to shift register

    Data is shifted out MSB-first (bit N-1 first, bit 0 last), so:
      - Bit 0 appears at Q0 of the first chip (nearest the MCU)
      - Bit N-1 appears at Q(N-1) of the last chip in the chain

    This matches the Prusa MMU3 SHR16 wiring where bit 0 = gear DIR.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        ppins = self.printer.lookup_object('pins')

        # Optional mcu: key in config is for documentation; the actual MCU is
        # derived from the data_pin/clock_pin chip prefix (e.g. "mmu:PB5").
        config.get('mcu', None)

        # Number of chained 8-bit shift registers (total bits = num_registers * 8)
        num_regs = config.getint('num_registers', 1, minval=1, maxval=4)
        self.num_bits = num_regs * 8

        # Initial state (all outputs low)
        self.state = 0
        # End print_time of the most recently scheduled bitbang; used to
        # serialize writes so two overlapping bitbangs never send the MCU
        # timer dispatcher backwards ("Rescheduled timer in the past").
        self._last_write_end = 0.

        # DATA and CLOCK pins are shared with the TMC2130 software SPI bus
        # (PB5 = MOSI, PC7 = SCLK on the MMU3 board). Allow multi-use so
        # neither the shift register nor the TMC modules trigger a pin
        # collision error. Then parse without registering and create raw
        # MCU OIDs to drive the pins at runtime.
        data_pin_str  = config.get('data_pin')
        clock_pin_str = config.get('clock_pin')
        ppins.allow_multi_use_pin(data_pin_str)
        ppins.allow_multi_use_pin(clock_pin_str)

        data_params  = ppins.parse_pin(data_pin_str)
        clock_params = ppins.parse_pin(clock_pin_str)
        self.mcu = data_params['chip']

        self._data_pin_name  = data_params['pin']   # e.g. 'PB5' — exposed for stepper.py placeholder
        self._clock_pin_name = clock_params['pin']   # e.g. 'PC7'
        self._data_pin  = _RawDigitalOut(self.mcu, self._data_pin_name)
        self._clock_pin = _RawDigitalOut(self.mcu, self._clock_pin_name)

        # LATCH is exclusive to the shift register — also use _RawDigitalOut
        # so it participates in the same minclock=0 burst-send strategy as
        # DATA and CLOCK, avoiding "Timer too close" from minclock chaining.
        latch_params = ppins.parse_pin(config.get('latch_pin'))
        self._latch_pin_name = latch_params['pin']
        self._latch_pin = _RawDigitalOut(self.mcu, self._latch_pin_name)

        # Register as a pin chip provider so mmu_sr:N syntax works.
        ppins.register_chip(self.name, self)

        # Write initial state once Klipper is connected.
        self.printer.register_event_handler('klippy:connect', self._handle_connect)

    def _handle_connect(self):
        """Schedule the initial SR write via a delayed reactor timer.

        On a freshly reset MCU (especially ATmega32U4 via Caterina/USB CDC),
        estimated_print_time() can be off by 1.5-2 seconds immediately after
        connect because USB CDC latency causes slow clock-sync convergence.
        Using a fixed lead time (e.g. 10s) would still produce a scheduled
        MCU clock that is in the past by the time it fires.

        Instead, defer the actual write until 30 seconds of host wall-clock
        time have elapsed. By then Klipper's clock sync has converged to
        within <1ms, so a 100ms lead time is ample.

        NOTE: TMC2130 software SPI init completes in the first few seconds,
        so the 30s delay also cleanly avoids the SPI/SR race condition.
        """
        reactor = self.printer.get_reactor()
        reactor.register_timer(self._delayed_sr_write, reactor.monotonic() + 30.0)

    def _delayed_sr_write(self, eventtime):
        """Reactor timer callback: write SR state 30s after klippy:connect.

        Clock sync is fully converged at this point, so estimated_print_time
        with a short lead time is accurate.
        """
        print_time = self.mcu.estimated_print_time(eventtime + 0.100)
        self._write_register(print_time)
        return self.printer.get_reactor().NEVER

    def setup_pin(self, pin_type, pin_params):
        """Called by Klipper when something requests a pin from this chip."""
        if pin_type not in ('digital_out', 'pwm'):
            raise pins.error(
                "shift_register '%s' only supports digital_out/pwm pins, got '%s'"
                % (self.name, pin_type))

        pin_name = pin_params['pin']
        try:
            bit_num = int(pin_name)
        except ValueError:
            raise pins.error(
                "shift_register '%s' pin must be a bit number (0..%d), got '%s'"
                % (self.name, self.num_bits - 1, pin_name))

        if bit_num < 0 or bit_num >= self.num_bits:
            raise pins.error(
                "shift_register '%s' bit %d out of range (0..%d)"
                % (self.name, bit_num, self.num_bits - 1))

        invert = pin_params['invert']
        return ShiftRegisterBit(self, bit_num, invert)

    # -- Internal state management -------------------------------------------

    def _set_bit(self, bit_num, value):
        """Update bit state without triggering a hardware write."""
        if value:
            self.state |= (1 << bit_num)
        else:
            self.state &= ~(1 << bit_num)

    def _set_bit_at_time(self, bit_num, value, print_time):
        """Update bit state and write the full register at print_time.

        Ensures a minimum future margin so queue_digital_out commands don't
        arrive at the MCU after their scheduled time ("Rescheduled timer in
        the past"). This can happen when print_time comes from a stale
        toolhead position (e.g. SET_PIN with no recent moves).

        Returns the actual print_time the write was scheduled at (which may
        be later than requested if it had to be deferred behind another
        in-flight shift register write).
        """
        self._set_bit(bit_num, value)
        # Guarantee at least 100ms into the future from NOW.
        # This handles stale print_times (e.g. SET_PIN with no recent moves).
        # 100ms is ample once Klipper's clock sync has converged; for the
        # first ~5s after MCU connect the clock may be off by more, but
        # _delayed_sr_write ensures the hardware is in a known state by 30s.
        curtime = self.printer.get_reactor().monotonic()
        min_time = self.mcu.estimated_print_time(curtime + 0.100)
        if print_time < min_time:
            print_time = min_time
        return self._write_register(print_time)

    def _write_register(self, print_time):
        """
        Bitbang the current state to the shift register chain.

        Protocol (74HC595 SPI-like):
          For each bit from MSB (bit N-1) down to LSB (bit 0):
            1. Set DATA to bit value
            2. Pulse CLOCK high then low  (data latched on rising edge)
          After all bits:
            3. Pulse LATCH high then low  (shift register → storage register)

        Sequential MCU commands are sent with incrementally increasing
        print_times (8000 ticks apart) to guarantee in-order execution on
        the MCU and absorb USB CDC jitter. 50 ops × 8000 ticks ≈ 25ms.

        Writes are serialized: if another write is still in flight (its
        commands extend beyond this write's requested start) this write is
        deferred to start after it. Without this, two back-to-back writes
        (e.g. idler and selector homing a few ms apart, or a motor-enable
        write colliding with a DIR write) overlap in time and the MCU's
        timer dispatcher sees clock values going backwards once the later
        commands arrive -- "Rescheduled timer in the past" shutdown
        (verified on the live MMU3: two full bitbangs 5ms apart crashed the
        ATmega32U4 at every boot).

        Returns the actual print_time the write started at.
        """
        if print_time < self._last_write_end:
            print_time = self._last_write_end
        mcu_freq = self.mcu.seconds_to_clock(1.0)
        dt = 8000.0 / mcu_freq  # 8000 ticks = 500μs between commands (USB CDC jitter safety)

        state = self.state
        t = print_time

        # Shift out MSB first (bit num_bits-1 down to bit 0).
        for i in range(self.num_bits - 1, -1, -1):
            bit = (state >> i) & 1
            self._data_pin.set_digital(t, bit)
            t += dt
            self._clock_pin.set_digital(t, 1)   # rising edge — latch data
            t += dt
            self._clock_pin.set_digital(t, 0)   # falling edge — ready for next
            t += dt

        # Pulse LATCH to transfer shift register contents to output register.
        self._latch_pin.set_digital(t, 1)
        t += dt
        self._latch_pin.set_digital(t, 0)

        self._last_write_end = t + dt

        logging.debug(
            "shift_register '%s': wrote 0x%04x at print_time=%.6f",
            self.name, state, print_time)

        return print_time

    # -- Status ------------------------------------------------------------------

    def get_status(self, eventtime):
        return {'state': self.state}


def load_config_prefix(config):
    return ShiftRegister(config)
