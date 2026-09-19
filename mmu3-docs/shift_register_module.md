# Shift Register Klipper Module — Design & Implementation

## Summary

The Prusa MMU3 board uses two cascaded 74HC595 shift registers (16 bits total) to
control stepper DIR and ENABLE pins. This is called "SHR16" in the Prusa firmware.

Klipper has no built-in support for shift-register-backed GPIO pins, so we provide
a custom extras module: `klippy/extras/shift_register.py`.

## Installation

The file is at `extras/shift_register.py` in the Happy-Hare repo. The Happy-Hare
install script (`install.sh`) automatically symlinks everything in `extras/` to
Klipper's `klippy/extras/`, so running the installer handles this:

```bash
cd ~/software/Happy-Hare && bash install.sh
```

Or manually:
```bash
ln -sf ~/software/Happy-Hare/extras/shift_register.py \
       ~/klipper/klippy/extras/shift_register.py
```

Restart Klipper after installing.

## Config

```ini
[shift_register mmu_sr]
mcu: mmu                # Which MCU the shift register is connected to
num_registers: 2        # Number of chained 74HC595 chips (2 × 8 = 16 bits)
data_pin: mmu:PB5       # MOSI/SER — serial data input to shift register
clock_pin: mmu:PC7      # SRCLK/SCK — shift clock
latch_pin: mmu:PB6      # RCLK/STCP — storage clock (latch pulse)
```

Virtual pins are then referenced as `mmu_sr:N` where N is the bit position (0-15):

```ini
[stepper_mmu_gear]
dir_pin: !mmu_sr:0      # Bit 0 = gear DIR, inverted (!)
enable_pin: !mmu_sr:1   # Bit 1 = gear ENABLE (active low), inverted

[stepper_mmu_selector]
dir_pin: mmu_sr:2       # Bit 2 = selector DIR
enable_pin: !mmu_sr:3   # Bit 3 = selector ENABLE (active low)

[stepper_mmu_idler]
dir_pin: !mmu_sr:4      # Bit 4 = idler DIR
enable_pin: !mmu_sr:5   # Bit 5 = idler ENABLE (active low)
```

## How It Works

### Bit Ordering

The 74HC595 shifts data in MSB-first. For two cascaded chips:
- First bit transmitted (MSB, bit 15) → ends up at Q7 of chip 2 (far end)
- Last bit transmitted (LSB, bit 0) → ends up at Q0 of chip 1 (near end)

So bit 0 of our `state` word appears at Q0 of the first chip. If the MMU3 board
connects Q0 to the gear DIR pin, bit index 0 = gear DIR. This matches the Prusa
SHR16 bit mapping.

### Write Protocol

The module implements the following bitbang protocol per register write:

```
For each bit from MSB (15) down to LSB (0):
  1. Set DATA pin to bit value
  2. Set CLOCK high  (74HC595 latches DATA on rising edge)
  3. Set CLOCK low   (ready for next bit)

After all 16 bits:
  4. Set LATCH high  (transfers shift register to output register)
  5. Set LATCH low   (LATCH pin returns to idle)
```

Total: 16 × 3 + 2 = 50 MCU commands per register write.

### Timing

Each MCU command gets a print_time 1 clock tick (1/MCU_freq) after the previous.
- At 16 MHz AVR: 1 tick = 62.5 ns
- 50 operations × 62.5 ns ≈ 3.125 μs total bitbang time
- 74HC595 minimum clock period: ~30 ns @ 3.3 V, 15 ns @ 5 V → well within spec

The MCU processes queued commands in order. Using incrementally larger print_times
guarantees the sequence: DATA→CLOCK_H→CLOCK_L→DATA→... even if commands are queued
with only 1 tick spacing.

### State Management

The module maintains a 16-bit `state` word. When any virtual pin calls
`set_digital(print_time, value)`, the state is updated and the **entire 16-bit
register is rewritten**. This is safe because:
- Only the target bit changes
- All 16 bits are always consistent
- The MMU3 only changes one bit at a time (direction or enable changes are single-bit)

### Shutdown / Emergency Stop

The control pins (DATA, CLOCK, LATCH) are configured with `max_duration=0` and
`start_value=0`, meaning they hold their state indefinitely and default to 0 on
init. On MCU emergency stop, the pins return to their configured shutdown value (0).

**Important**: This means the shift register DOES NOT actively disable steppers on
emergency stop — the pins return to 0 but the register output holds its last
latched value. Consider adding explicit motor disable in Klipper's emergency stop
handler if needed.

## SHR16 Bit Mapping (Prusa MMU3)

From `prusa3d/Prusa-Firmware-MMU/src/hal/avr/shr16.cpp`:

| Bit | Mask     | Purpose              | Config pin        |
|-----|----------|----------------------|-------------------|
|  0  | 0x0001   | Gear DIR             | `!mmu_sr:0`       |
|  1  | 0x0002   | Gear ENABLE          | `!mmu_sr:1`       |
|  2  | 0x0004   | Selector DIR         | `mmu_sr:2`        |
|  3  | 0x0008   | Selector ENABLE      | `!mmu_sr:3`       |
|  4  | 0x0010   | Idler DIR            | `!mmu_sr:4`       |
|  5  | 0x0020   | Idler ENABLE         | `!mmu_sr:5`       |
|  6  | 0x0040   | (4th stepper DIR)    | unused            |
|  7  | 0x0080   | (4th stepper ENABLE) | unused            |
| 8-15| 0xFF00   | LED control          | use as output_pin |

`SHR16_DIR_MSK = 0x0015` (bits 0, 2, 4)
`SHR16_ENA_MSK = 0x002A` (bits 1, 3, 5) — active LOW in Prusa firmware
`SHR16_LED_MSK = 0xFFC0` (bits 6-15)

## Physical Pins on MMU3 Board (ATMega32U4)

| Signal | MCU Pin | Klipper Reference |
|--------|---------|-------------------|
| SHR DATA  | PB5    | `mmu:PB5`        |
| SHR CLOCK | PC7    | `mmu:PC7`        |
| SHR LATCH | PB6    | `mmu:PB6`        |

## LED Control

Bits 8-15 (the upper byte) control 8 LEDs. These can be set via `output_pin`:

```ini
[output_pin mmu_led_0]
pin: mmu_sr:14    # LED 0 (bit 14)
value: 1
```

Note: updating an LED pin also rewrites the entire 16-bit state (including
stepper DIR/ENABLE bits). The module maintains the full state so stepper config
is preserved.

## Known Limitations

1. **No true async sequencing**: All 50 MCU commands for a register write are
   queued synchronously. If Klipper is scheduling many moves, this might briefly
   block the command queue. In practice for MMU3 speeds this is not a problem.

2. **Startup state**: The control pins start LOW. The shift register outputs are
   undefined until the first `_write_register` call in `handle_connect`. There's
   a ~100ms window at startup where stepper enables might be in an unknown state.

3. **No SPI acceleration**: We use pure bitbang rather than hardware or software
   SPI because the MMU3 board's data/clock pins (PB5, PC7) don't map to the
   ATMega32U4's SPI hardware pins, and Klipper's software SPI doesn't support a
   separate latch pin (it uses the CS pin which has different timing semantics).

## Alternative Approaches Considered

1. **Klipper MCU firmware module** (C code in `klipper/src/`): Most efficient,
   single `shift_reg_write oid=%c value=%u` command. Requires rebuilding Klipper
   firmware with custom code — too invasive.

2. **Hardware SPI**: ATMega32U4 hardware SPI uses PB2/PB1 (MOSI/SCK) which are
   different from PB5/PC7 on the MMU3 board. Would require trace cuts/rewiring.

3. **Klipper software SPI + digital latch**: Could use `spi_set_software_bus`
   for the data transfer then a separate latch command. Timing between SPI end
   and latch pulse is uncertain without MCU-side synchronization.

4. **Pure bitbang with same clock** (`set_digital` at same print_time): Relies
   on MCU command queue ordering — works but fragile. Using incremental clocks
   is more explicit and reliable.
