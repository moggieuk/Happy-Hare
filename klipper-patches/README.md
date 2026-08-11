# Klipper patches for Prusa MMU3 support

The MMU3 board (ATmega32U4) has no spare GPIOs for the stepper DIR and ENABLE
pins - they are all driven through the SHR16 shift register (two cascaded
74HC595s), which `extras/shift_register.py` exposes to Klipper as virtual
`mmu_sr:N` pins.

The firmware normally flips a stepper's DIR pin itself during motion
(`set_next_step_dir`). A shift register bit cannot be driven from the MCU
firmware, so:

- the *host* must write the direction from Python before each move
  (Happy Hare's `MmuStepper._pre_set_dir_pin()`), and
- Klipper's `MCU_stepper` must be told that the dir pin is a virtual pin
  rather than refusing to configure it ("Stepper dir pin must be on same mcu
  as step pin").

This patch modifies stock Klipper's `klippy/stepper.py` to do the second part:
when the dir pin resolves to a virtual chip it checks the chip is backed by
the stepper's own MCU (the `shift_register` chip exposes its MCU as `.mcu`),
stores the chip's virtual pin object as `self._dir_pin_virtual` (which Happy
Hare reads back via `getattr`) and uses the chip's DATA pin name as a
placeholder for the firmware `config_stepper` dir pin - the firmware never
meaningfully drives it, the host's Python writes do.

Verified working on a live MMU3 (Raspberry Pi + MMU3 board via USB) with
Klipper `170c4b37`.

## Apply

```bash
cd ~/klipper
patch -p1 < <path-to>/stepper_dir_pin_virtual.patch
sudo systemctl restart klipper
```

Re-apply after every Klipper update (like `./install.sh -f` for the symlinks).

## Requires

- `extras/shift_register.py` installed into Klipper's `klippy/extras/`
  (Happy Hare's install script symlinks its `extras/` automatically)
- MMU3 hardware config using `dir_pin: !mmu_sr:N` style pins
  (see `config/base/mmu_hardware.cfg`, Prusa MMU3 vendor/board)
