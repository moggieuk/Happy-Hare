# MMU3 + Happy-Hare Setup Checklist

## Prerequisites

- [ ] MMU3 board physically connected to Raspberry Pi via USB
- [ ] Einsy (MK3S+) connected via `/dev/serial0` (internal UART)
- [ ] Klipper is running and MK3S+ printer works normally

---

## Step 1: Flash Klipper onto MMU3 Board

See `klipper-on-mmu3-board.md` for full details.

```bash
cd ~/klipper
make menuconfig   # AVR atmega32u4, Arduino CDC, USB
make
# Put MMU3 in DFU/bootloader mode, then:
dfu-programmer atmega32u4 erase && dfu-programmer atmega32u4 flash out/klipper.hex && dfu-programmer atmega32u4 launch
```

- [ ] MMU3 MCU appears after flashing:
  ```bash
  ls /dev/serial/by-id/ | grep Klipper_atmega32u4
  ```

---

## Step 2: Install shift_register.py Module

The Happy-Hare install script handles this automatically (it symlinks everything
from `extras/` to Klipper's `klippy/extras/`):

```bash
cd ~/software/Happy-Hare && bash install.sh
```

Or manually:
```bash
ln -sf ~/software/Happy-Hare/extras/shift_register.py ~/klipper/klippy/extras/
```

- [ ] File is symlinked at `~/klipper/klippy/extras/shift_register.py`

---

## Step 3: Update printer.cfg

Replace the old MMU3 MCU section in `~/printer_data/config/printer.cfg`:

**REMOVE** (or leave commented):
```ini
#[mcu mmu]
#bridge_mcu: mcu
#usart_number: 2
#baud: 76800
```

**ADD**:
```ini
[mcu mmu]
serial: /dev/serial/by-id/usb-Klipper_atmega32u4_XXXXXXXXXX-if00
restart_method: arduino
```

Also ensure:
```ini
[mcu]
serial: /dev/serial0        # Einsy board (unchanged)
restart_method: command
baud: 115200
```

- [ ] `printer.cfg` updated with MMU3 direct USB serial

---

## Step 4: Update mmu/base/mmu_hardware.cfg

Enable the shift register and MMU hardware definitions:

### 4a: Shift Register Section

In `~/printer_data/config/mmu/base/mmu_hardware.cfg` or in `printer.cfg`,
add/uncomment:

```ini
[shift_register mmu_sr]
mcu: mmu
num_registers: 2
data_pin: mmu:PB5
clock_pin: mmu:PC7
latch_pin: mmu:PB6
```

### 4b: Gear Stepper TMC (with SPI)

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

[stepper_mmu_gear]
step_pin: mmu:PB4
dir_pin: !mmu_sr:0
enable_pin: !mmu_sr:1
microsteps: 16
rotation_distance: 19.394
full_steps_per_rotation: 200
```

### 4c: Selector Stepper TMC

```ini
[tmc2130 stepper_mmu_selector]
cs_pin: !mmu:PD7
spi_software_mosi_pin: mmu:PB5
spi_software_miso_pin: mmu:PF6
spi_software_sclk_pin: mmu:PC7
run_current: 0.800
sense_resistor: 0.110
hold_current: 0.1
diag0_pin: ^!mmu:PF1
driver_SGT: 3

[stepper_mmu_selector]
step_pin: mmu:PD4
dir_pin: mmu_sr:2
enable_pin: !mmu_sr:3
microsteps: 16
rotation_distance: 8
full_steps_per_rotation: 200
endstop_pin: tmc2130_stepper_mmu_selector:virtual_endstop
endstop_name: mmu_sel_touch
homing_retract_dist: 0
```

### 4d: Idler Stepper TMC

```ini
[tmc2130 stepper_mmu_idler]
cs_pin: !mmu:PB7
spi_software_mosi_pin: mmu:PB5
spi_software_miso_pin: mmu:PF6
spi_software_sclk_pin: mmu:PC7
run_current: 0.800
sense_resistor: 0.110
hold_current: 0.1
diag0_pin: ^!mmu:PF0
driver_SGT: 12

[stepper_mmu_idler]
step_pin: mmu:PD6
dir_pin: !mmu_sr:4
enable_pin: !mmu_sr:5
microsteps: 16
endstop_pin: tmc2130_stepper_mmu_idler:virtual_endstop
endstop_name: mmu_idler_touch
full_steps_per_rotation: 200
rotation_distance: 128     # Tune this! Full rotation = num_gates positions
position_max: 100          # Tune based on physical travel
position_min: 0
homing_retract_dist: 0
position_endstop: 0
```

**Note on `rotation_distance` for idler**: This needs calibration. The idler is a
rotating barrel. For 5 gates, the idler rotates a fixed amount per gate. Start
with 128 and calibrate via `MMU_CALIBRATE_IDLER`.

### 4e: Gate Sensor

```ini
[mmu_sensors]
gate_switch_pin: ^mmu:PF6
```

- [ ] All stepper sections enabled in mmu_hardware.cfg
- [ ] Selector servo section is DISABLED (not applicable for Prusa MMU3)

---

## Step 5: Verify mmu_machine.cfg Settings

In `~/printer_data/config/mmu/base/mmu_parameters.cfg` (or equivalent):

```ini
[mmu_machine]
num_gates: 5
mmu_vendor: Prusa
mmu_version: 3.0
homing_extruder: 1
```

- [ ] Prusa vendor/version set correctly

---

## Step 6: Restart and Verify

```bash
sudo systemctl restart klipper
```

Check Klipper log for errors:
```bash
tail -f ~/printer_data/logs/klippy.log
```

Expected: Klipper connects to both `mcu` (Einsy) and `mmu` (MMU3).

- [ ] Both MCUs connect without errors
- [ ] No "Unknown pin" errors for `mmu_sr:N` pins
- [ ] shift_register module loads successfully

---

## Step 7: Basic Motor Tests

In Mainsail/Fluidd console:

```gcode
; Test gear stepper  
MMU_TEST_MOVE MOVE=10 SPEED=10

; Test selector (requires homing first)
MMU_HOME

; Test idler
MMU_IDLER HOME=1
MMU_IDLER GATE=0
MMU_IDLER GATE=4
```

- [ ] Gear stepper moves in correct direction
- [ ] Selector homes correctly using stallguard
- [ ] Idler moves to each gate position

---

## Step 8: Calibration

### Selector Calibration
```gcode
MMU_CALIBRATE_SELECTOR
```

### Idler Calibration

The idler calibration is currently manual — you need to physically determine 
the correct positions for each gate.

```gcode
; Move idler to gate 0 position, confirm, then save
MMU_CALIBRATE_IDLER GATE=0 POSITION=0

; Move idler to gate 1 position
MMU_CALIBRATE_IDLER GATE=1 POSITION=20  ; adjust value

; ... repeat for all 5 gates
MMU_CALIBRATE_IDLER GATE=5 POSITION=100  ; gate 5 = disengaged position
```

**TODO**: Implement auto-calibration for idler similar to selector auto-calibration.

---

## Known Issues / TODOs

1. **Idler calibration** is manual. Need auto-calibration routine.
2. **Shutdown safety**: The shift register doesn't actively disable steppers on
   emergency stop. The outputs hold their last latched values.
3. **SPI sharing**: TMC2130 SPI and shift register share PB5/PC7. Currently
   handled by time separation (TMC config at startup, SR writes during motion).
   If TMC StallGuard is active during motion this may cause conflicts — monitor.
4. **`mmu_idler.py`**: This file has a `load_config` function that is vestigial
   from an earlier approach. It's NOT included in the current mmu/ package
   `__init__.py` so shouldn't cause issues, but verify it's not being loaded.
5. **check_move idler limits**: `check_move` in `mmu_machine.py` now handles
   axis 2 (idler) speed limits, but needs testing.
