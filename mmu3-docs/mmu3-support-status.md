# Prusa MMU3 Support for Happy-Hare — Status & Design Doc

**Date**: 2026-08-09  
**Branch**: `mmu3-support`  
**Author notes**: Hans Maritz

---

## Overview

This document captures the current state of MMU3 support work, hardware details, the shift register challenge, and what needs to happen next.

**Target setup**: Raspberry Pi connects directly to MMU3 board via USB (running Klipper), AND to the Prusa MK3S+ Einsy board via UART (`/dev/serial0`). The old approach of bridging the MMU3 via Einsy USART2 is **abandoned**.

---

## GitHub Issue #310 Status

- **Status**: Open (since June 2024)
- **Repo**: moggieuk/Happy-Hare
- Initial stall due to MMU3's combined selector+idler mechanism
- Feb 2025: Renewed interest — `eoyilmaz` (Marlin MMU3 porter) + `agravelot` + others want to help
- Current work is in this branch `mmu3-support` — not yet published to upstream
- The branch implements the `LinearSelectorIdler` class which replaces the servo with the idler stepper

---

## MMU3 Hardware Architecture

### MCU
- **Chip**: ATMega32U4 (same as Arduino Leonardo/Pro Micro)
- **Connection**: USB to Raspberry Pi → appears as `/dev/serial/by-id/usb-Klipper_atmega32u4_...`
- **Klipper firmware**: Must be compiled for `atmega32u4` target

### SHR16 — The 16-bit Shift Register

The MMU3 board does **not** have enough GPIO pins for all stepper dir/enable signals directly. Instead, it uses a daisy-chained pair of **74HC595** 8-bit shift registers (= 16 bits total) controlled via 3 pins:

| Pin | ATMega32U4 Port | Function |
|-----|-----------------|----------|
| DATA | PB5 (MOSI) | Serial data |
| CLOCK | PC7 | Shift clock |
| LATCH | PB6 | Storage clock (latch) |

The 16-bit register is written **MSB-first** (bit 15 first, bit 0 last).

#### Bit Mapping

```
Bit  | Purpose              | Stepper
-----|----------------------|-------------------
  0  | DIR (gear)           | stepper_mmu_gear
  1  | ENABLE (gear)        | stepper_mmu_gear
  2  | DIR (selector)       | stepper_mmu_selector
  3  | ENABLE (selector)    | stepper_mmu_selector
  4  | DIR (idler)          | stepper_mmu_idler
  5  | ENABLE (idler)       | stepper_mmu_idler
  6  | DIR (4th, unused)    | —
  7  | ENABLE (4th, unused) | —
8-15 | LED control (LEDs 1-8)| SHR16_LED_MSK=0xffc0
```

From Prusa firmware (`shr16.cpp`):
- `SHR16_DIR_MSK = 0x0015` (bits 0, 2, 4)
- `SHR16_ENA_MSK = 0x002A` (bits 1, 3, 5)
- `SHR16_LED_MSK = 0xffc0` (bits 6-15)

Enable pins are **active-low** in Prusa firmware, hence `!` prefix in Klipper config.

### Stepper CS/STEP/DIAG Pins (direct MCU GPIO)

```
Stepper   | STEP | CS   | DIAG0
----------|------|------|------
Gear      | PB4  | !PC6 | !PF4
Selector  | PD4  | !PD7 | !PF1
Idler     | PD6  | !PB7 | !PF0
```

All 3 steppers use **TMC2130** drivers.

### Gate Sensor
- `PF6` — filament detection at gate

---

## Connection Topology (New Approach)

```
Raspberry Pi
  ├── /dev/serial0 (UART) ──────────────→ Einsy Rambo (MK3S+ printer MCU)
  └── /dev/serial/by-id/usb-Klipper_... → MMU3 board (ATMega32U4, Klipper)
```

In `printer.cfg`:
```ini
[mcu]
serial: /dev/serial0        # Einsy board

[mcu mmu]
serial: /dev/serial/by-id/usb-Klipper_atmega32u4_XXXXXX-if00
```

**Old approach (abandoned)**:
```ini
[mcu mmu3]
bridge_mcu: mcu
usart_number: 2
baud: 76800
```
This forwarded Klipper communication through the Einsy board's USART2 to the MMU3. No longer desired.

---

## The Shift Register Challenge

The Klipper config uses `mmu_sr:N` pin syntax (e.g., `dir_pin: !mmu_sr:0`), which requires a custom Klipper extras module `shift_register.py` that:
1. Registers as a Klipper "chip" named `mmu_sr`
2. Manages a 16-bit state
3. On any bit change: bitbangs the entire 16-bit value out to DATA/CLOCK/LATCH
4. Exposes virtual `digital_out` pins for each bit position

This module **is not in mainline Klipper**. It was written previously (local changes existed) and needs to be revived.

See `mmu3-docs/shift_register_module.md` for implementation details.

The config for the shift register is:
```ini
[shift_register mmu_sr]
mcu: mmu
num_registers: 2
data_pin: mmu:PB5
clock_pin: mmu:PC7
latch_pin: mmu:PB6
```

And the hardware config pins use:
```ini
[stepper_mmu_gear]
step_pin: mmu:PB4
dir_pin: !mmu_sr:0
enable_pin: !mmu_sr:1
...

[stepper_mmu_selector]
step_pin: mmu:PD4
dir_pin: mmu_sr:2
enable_pin: !mmu_sr:3
...

[stepper_mmu_idler]
step_pin: mmu:PD6
dir_pin: !mmu_sr:4
enable_pin: !mmu_sr:5
...
```

The shift register module needs to be placed in the Klipper installation's `klippy/extras/` directory on the host:
```
/home/freakazo/klipper/klippy/extras/shift_register.py
```

---

## Current Branch State (`mmu3-support`)

### Files Changed (vs main)
1. **`extras/mmu/mmu_selector.py`** (+308 lines)
   - Added `LinearSelectorIdler` class (lines 1027–1299) — replaces servo with idler stepper
   - The idler uses `mmu_toolhead.get_kinematics().rails[2]` (axis 2 of MmuToolhead)
   - Has `servo_down()`, `servo_move()`, `servo_up()`, `home()`, calibration commands
   - Gate idler offsets stored in `mmu_vars.cfg` as `mmu_idler_offsets`

2. **`extras/mmu_machine.py`** (+32 lines)
   - Added `IDLER_STEPPER_CONFIG = "stepper_mmu_idler"` constant
   - Added `idler_max_velocity`, `idler_max_accel` config parsing
   - Axis 2 (idler) added to `MmuToolhead`
   - `get_idler_limits()` method on toolhead
   - Homing for axis[2] added

3. **`extras/mmu.py`** (+3 lines)  
   - Minor additions related to Prusa vendor support

4. **`extras/mmu_idler.py`** (new file, 243 lines)
   - `MmuIdler` class — a standalone idler stepper with fake-toolhead interface
   - **Status**: This appears to be an earlier approach now superseded by integrating the idler into `MmuToolhead` as axis[2]
   - Code still exists but `LinearSelectorIdler` doesn't use it (it accesses `rails[2]` directly)
   - The buzzer/homing code in this file still references older approach patterns
   - May need cleanup or removal

### Known Issues / TODOs
1. `LinearSelectorIdler.buzz_motor()` still references `self.idler_positions['down']` and `self.idler.do_move()` — leftover from old MmuIdler approach, will crash
2. The idler calibration is manual-only (no auto-calibration), POSITION= parameter required
3. The `mmu_idler.py` `load_config` function would conflict if both are loaded — check if it needs to be removed or is never registered

---

## What Needs to Happen Next

### Phase 1: Shift Register Module (BLOCKING)
Write `shift_register.py` for Klipper. See the implementation in `mmu3-docs/shift_register_module.md`.

Install it to:
```
/home/freakazo/klipper/klippy/extras/shift_register.py
```

### Phase 2: Klipper Firmware for MMU3
Flash Klipper onto the ATMega32U4. Steps:
```bash
cd ~/klipper
make menuconfig
# Target: AVR atmega32u4
# Bootloader: CDC bootloader (Arduino Leonardo style)
# Communication: USB (virtual serial)
make
# Flash with avrdude or DFU
```

Then update `printer.cfg`:
```ini
[mcu mmu]
serial: /dev/serial/by-id/usb-Klipper_atmega32u4_XXXXXX-if00
```

### Phase 3: Verify mmu_hardware.cfg
The `mmu_hardware.cfg` already references the right pins. Just needs:
- `mcu: mmu` (uncomment the `[mcu mmu]` block with USB serial)
- Confirm `shift_register mmu_sr` config block is present and enabled
- Make sure selector servo section is commented out (Prusa uses idler, not servo)

### Phase 4: Code Fixes
- Fix `LinearSelectorIdler.buzz_motor()` — replace old `self.idler.do_move()` calls
- Check/remove `mmu_idler.py` load_config if it's redundant
- Add idler calibration wizard or auto-calibration
- Integrate idler homing into the main MMU homing sequence

### Phase 5: Testing
- Test homing with just the selector
- Test idler position for each gate
- Test full filament load/unload
- Validate gate sensor works

---

## MMU3 Mechanical Notes

The MMU3 selection mechanism:
1. **Selector** moves linearly to align with the target gate (like other MMUs)
2. **Idler** is a rotating barrel that presses against the filament at the aligned gate
   - Disengaged position: idler rotated to press on **no** filament path
   - Gate N engaged: idler rotated to press on filament path N
   - The idler rotation distance per gate step: `rotation_distance / num_gates`
3. When selector moves: idler must be disengaged first (at position = num_gates)
4. After selector positions: idler moves to gate position

This is modeled in `LinearSelectorIdler`:
- `servo_move()` → idler to `_disengaged_gate` position (num_gates)  
- `servo_down()` → idler to current `gate_selected` position
- `servo_up()` → idler to `_disengaged_gate` position

Idler homing uses stallguard (TMC2130 virtual_endstop) at position 0 (one end stop).

---

## Remote Machine Config State (192.168.50.118)

The printer is currently running with:
- Einsy board connected via `/dev/serial0`
- MMU3 **not plugged in** (disconnected)
- MMU config is set up but MCU `mmu` is commented out
- The `shift_register mmu_sr` section is commented out in `printer.cfg`
- `mmu_hardware.cfg` already has the right pin mappings using `mmu_sr:N`
- `mmu_machine.py` shows `mmu_vendor: Prusa` and `mmu_version: 3.0`

Key files on remote:
- `~/printer_data/config/printer.cfg` — main printer config  
- `~/printer_data/config/mmu/base/mmu_hardware.cfg` — MMU pin config
- `~/printer_data/config/mmu3.cfg` — MMU3-specific config stub
- `~/printer_data/config/printer-20250601_223149.cfg-old` — old config with working shift_register section

---

## Relevant References

- Prusa MMU3 firmware SHR16: https://github.com/prusa3d/Prusa-Firmware-MMU/blob/master/src/hal/avr/shr16.cpp
- GitHub issue: https://github.com/moggieuk/Happy-Hare/issues/310
- Klipper SX1509 (virtual chip pattern): `/home/freakazo/klipper/klippy/extras/sx1509.py`

## Session 2 (2026-08-10) — Hardware Bringup

### Key Hardware Finding: TMC2130 SPI Pins
**CRITICAL**: The TMC2130 SPI uses the HARDWARE SPI bus (PB1/PB2/PB3), NOT the
shift register pins. These are completely separate:

| Signal      | ATmega32U4 pin | Klipper config        |
|-------------|---------------|-----------------------|
| TMC MOSI    | PB2           | `spi_bus: spi`        |
| TMC MISO    | PB3           | (handled by spi_bus)  |
| TMC SCK     | PB1           | (handled by spi_bus)  |
| SR DATA     | PB5           | `data_pin: mmu:PB5`   |
| SR CLOCK    | PC7           | `clock_pin: mmu:PC7`  |
| SR LATCH    | PB6           | `latch_pin: mmu:PB6`  |

Use `spi_bus: spi` in TMC2130 sections (NOT `spi_software_*`).

### Confirmed Working
- Klipper on MMU3 via USB (1d50:614e)
- Shift register: all 10 LEDs respond to `SET_PIN` commands
  - SR bits 8-15: 8 primary LEDs
  - SR bits 6-7: 2 additional LEDs (spool 1 LEDs)
- TMC2130 SPI: all 3 drivers init ("Found tmc2130 on * Stallguard possible")
- restart.sh handles startup transients automatically

### Remaining
- gate_switch_pin (mmu:PF6) needs firmware rebuild with CONFIG_WANT_BUTTONS=y
- Stallguard homing needs diag0_pin + driver_SGT re-enabled in mmu_hardware.cfg
- Actual stepper movement testing + calibration
