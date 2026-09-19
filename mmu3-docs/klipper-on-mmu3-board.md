# Flashing Klipper onto the Prusa MMU3 Board

## MMU3 Board Hardware

- **MCU**: ATMega32U4 (same chip as Arduino Leonardo/Pro Micro)
- **Bootloader**: Arduino CDC bootloader (DFU-compatible)
- **USB**: Native USB on ATMega32U4 (no USB-to-UART chip needed)
- **Frequency**: 16 MHz crystal

## Building Klipper for ATMega32U4

On the Raspberry Pi (or host):

```bash
cd ~/klipper
make menuconfig
```

Select:
```
Micro-controller Architecture: AVR
Processor model: atmega32u4
Bootloader: Arduino/CDC bootloader (at 256)
Communication interface: USB
```

Then build:
```bash
make clean
make
```

Output: `out/klipper.elf` and `out/klipper.hex`

## Flashing

### Method 1: DFU (preferred if MMU3 is accessible)

Put the MMU3 board in DFU mode:
- Press and hold the RESET button on the MMU3 board
- OR short the RESET pin to GND twice quickly (double-reset to trigger bootloader)
- The board will appear as an ATmega32u4 DFU device

```bash
# Check if DFU device appeared
lsusb | grep "03eb:2ff4"   # Atmel DFU

# Flash
dfu-programmer atmega32u4 erase
dfu-programmer atmega32u4 flash out/klipper.hex
dfu-programmer atmega32u4 launch
```

Or using avrdude (if DFU not available):
```bash
avrdude -p atmega32u4 -c avr109 -P /dev/ttyACM0 -b 57600 -D -U flash:w:out/klipper.hex:i
```

Note: avr109 is the Arduino bootloader protocol. The board may appear as `/dev/ttyACM0` 
when in bootloader mode (after double-reset).

### Method 2: ICSP Header (if available)

If the MMU3 board has an ICSP header (6-pin):
```bash
avrdude -p atmega32u4 -c usbasp -U flash:w:out/klipper.hex:i
```

## Identifying the USB Device

Once Klipper firmware is running, the MMU3 board appears as:
```
/dev/serial/by-id/usb-Klipper_atmega32u4_XXXXXXXXXX-if00
```

To find it:
```bash
ls /dev/serial/by-id/
```

## printer.cfg Changes

Replace the old bridge_mcu approach with direct USB:

```ini
[mcu]
serial: /dev/serial0        # Einsy board via UART
restart_method: command
baud: 115200

[mcu mmu]
serial: /dev/serial/by-id/usb-Klipper_atmega32u4_XXXXXX-if00
restart_method: arduino     # Arduino CDC bootloader double-reset
```

The `restart_method: arduino` tells Klipper to use the DTR trick to reset the
ATMega32U4 into bootloader mode for firmware updates.

## Old Approach (Bridge MCU) — ABANDONED

The previous approach used the Einsy Rambo's USART2 to talk to the MMU3:

```ini
# OLD — DO NOT USE
[mcu mmu3]
bridge_mcu: mcu
usart_number: 2
baud: 76800
```

This forwarded Klipper communication from the Pi through the Einsy board. 
It was abandoned because:
1. Requires custom firmware on both Einsy and MMU3
2. Communication latency is higher
3. The MMU3 was running Prusa's firmware, not Klipper
4. The approach is fragile and hard to debug

The new approach (MMU3 directly on USB as a second MCU) is cleaner.

## ATMega32U4 Pin Reference for MMU3

Key pins as referenced in Klipper config (`mmu:PXN`):

```
Port B:
  PB4 = Gear stepper STEP
  PB5 = SHR DATA (shift register MOSI)
  PB6 = SHR LATCH
  PB7 = Idler TMC CS (SPI chip select)

Port C:
  PC6 = Gear TMC CS
  PC7 = SHR CLOCK

Port D:
  PD4 = Selector stepper STEP
  PD5 = (referenced as CS spare)
  PD6 = Idler stepper STEP
  PD7 = Selector TMC CS

Port F:
  PF0 = Idler TMC DIAG0 (StallGuard)
  PF1 = Selector TMC DIAG0 (StallGuard)
  PF4 = Gear TMC DIAG0 (StallGuard)
  PF6 = Gate filament sensor
  PF7 = (SPI CS for SHR - not used in soft bitbang)
```

## TMC2130 SPI Communication

All 3 TMC2130 drivers on the MMU3 use shared SPI bus (MOSI=PB5, SCK=PC7, MISO=PF6)
with individual CS pins (PC6, PD7, PB7).

**Important**: PB5 and PC7 are ALSO used for the shift register! The TMC drivers
and shift register share the same SPI data/clock lines. This works because:
- TMC SPI uses CS to select individual drivers
- Shift register uses LATCH (PB6) to latch data

They must NOT be used simultaneously. In practice, Klipper handles them at different
times:
- TMC configuration happens at startup (config phase)
- Shift register writes happen during motion

This sharing is by design in the MMU3 board hardware.

## SPI Configuration for TMC2130

The TMC2130 drivers use software SPI:

```ini
[tmc2130 stepper_mmu_gear]
cs_pin: !mmu:PC6
spi_software_mosi_pin: mmu:PB5
spi_software_miso_pin: mmu:PF6
spi_software_sclk_pin: mmu:PC7
run_current: 0.500
sense_resistor: 0.110
hold_current: 0.01
diag0_pin: ^!mmu:PF4
```

Note: Use `spi_software_*` pins rather than hardware SPI bus, since the hardware
SPI pins on ATMega32U4 are at different locations (PB2/PB1) than what the MMU3
board uses.
