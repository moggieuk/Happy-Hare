# NFC/RFID driver architecture — reference

Verified against the `private_v4` tree. Re-grep symbol names if line numbers
have drifted.

## 1. Drivers and the factory

Four chip drivers, `extras/mmu/unit/nfc/`:

| Driver | Lines | Notes |
|---|---|---|
| `pn532_driver.py` | ~1988 | `_PN532Base`, shared across transports |
| `pn532_uart_driver.py` | ~739 | HSU/UART transport for PN532 |
| `pn5180_driver.py` | ~869 | no non-blocking probe (see below) |
| `pn7160_driver.py` | ~1848 | needs `irq_pin` for full-rate probing |
| `rc522_driver.py` | ~1359 | |

`reader_factory.py`:
- `SUPPORTED_READER_TYPES = ('pn532', 'pn5180', 'pn7160', 'rc522')`, default
  `pn532` (`:21-22`).
- `SUPPORTED_INTERFACES` maps each chip to the transports it has a *driver*
  for (`:29-34`): `pn532`→(i2c,spi,uart), `pn5180`→(spi,), `pn7160`→(i2c,),
  `rc522`→(spi,). This reflects driver coverage, not silicon capability.
- `create_reader()` (`:300-381`) is the single dispatch point building a
  concrete driver from a `[mmu_nfc_reader NAME]` config section.
- Driver contract (`:6-10`): `init()`, `is_alive()`, `read_tag()`,
  `read_target()`, optional `probe_start/probe_poll/probe_stop`.

**Maturity caveats, enforced in code, not just comments:**
- PN532-over-SPI logs "UNTESTED against real hardware" on every build
  (`reader_factory.py:320-329`).
- PN5180 has no presence-probe implementation — falls back to the blocking
  shim during homing, deliberately (`pn5180_driver.py:771-780`).
- PN7160 only gets full-rate non-blocking probing with a wired `irq_pin`;
  without it, same blocking-shim fallback.

**I2C NACK handling (`i2c_transport.py`).** Klipper's `bus.MCU_I2C.i2c_read()`
and `i2c_write()` shut the printer down on a NACK. On new firmware that is the
host-side `i2c_transfer()` wrapper calling `invoke_shutdown()`; on old firmware
it is `command_i2c_read` in the MCU. So PN532-over-I2C and PN7160 send
`i2c_transfer_cmd` themselves through `transfer_checked()`, which raises
`I2CStatusError` (`PN7160I2CStatusError` is a subclass). Whether that path is
available depends on each MCU's firmware, and `i2c_transfer_cmd` is only bound
in `build_config()`, after the driver has been built. So `status_supported()`
is checked on every call and never cached. The SPI and UART drivers have no
NACK and so no equivalent.

Only the supported path is shared. Each driver keeps its own fallback, and the
two are deliberately different:
- PN532 calls `i2c_read(write, n)` with no `retry=`, so it works on
  Klipper ≤ v0.13.0. Its `init()` logs a warning there (and on Kalico) that
  a NACK will shut down the MCU.
- PN7160 refuses all bus traffic in polled (no `irq_pin`) mode without
  status support, raising `PN7160PolledUnsupported` before any byte is sent,
  and `init()` fails fast with the reason. Polled mode learns "nothing
  pending" from a NACK, and there a NACK is an MCU shutdown. Don't rely on
  the `retry=False` `TypeError` for this: it happens on Klipper ≤ v0.13.0,
  but Kalico's `i2c_read()` accepts `retry`. IRQ-mode reads still go through
  `i2c_read(..., retry=False)`, and once setup succeeds there (Kalico)
  `init()` logs the same MCU-shutdown warning as the PN532.

Don't unify the two fallbacks.

Driver warnings reach the console through `startup_warnings`: a driver's
`init()` resets the list and appends to it, `MmuNfcReader.init()` copies it,
and `MmuNfcManager._init_reader()` and `MMU_RFID_INIT` log each entry with
`mmu.log_warning()`, or `log_debug()` when `suppress_klipper_warnings` is
set. The driver's own `logger.warning()` only reaches klippy.log, because
drivers have no gcode or MMU access by design.

**I2C support matrix.** What decides NACK safety is each MCU's firmware, not
the host version. A new Klipper host with un-reflashed MCU firmware behaves
like the Kalico column.

| Reader | New Klipper (has `i2c_transfer`) | Old Klipper (≤ v0.13.0) | Kalico |
|---|---|---|---|
| PN532 over I2C | Works. A NACK takes the reader offline | Works, with a startup warning. A NACK shuts down the MCU | Works, with a startup warning. A NACK shuts down the MCU |
| PN7160 with `irq_pin` | Works. A NACK is reported | Doesn't start (`i2c_read()` rejects `retry=`) | Works, with a startup warning. A NACK shuts down the MCU |
| PN7160 without `irq_pin` | Works, including the polled homing probe. A NACK is reported | Refused at startup (`PN7160PolledUnsupported`) | Refused at startup (`PN7160PolledUnsupported`) |

- The startup warnings go through `log_warning()`, so they reach the console
  and `mmu.log`, one line per physical reader, at bootup and on
  `MMU_RFID_INIT`. The PN7160 refusal is a startup error, so it always
  reaches the console.
- Retries: the status-checked path sends `retry=False` in both drivers,
  because resending a read or write to an NFC chip could consume or send a
  frame twice. The PN532 fallback omits `retry`, so that Klipper's default
  applies; the PN7160 fallback passes `retry=False`.
- Tag homing probe: the PN532 probe works everywhere the reader starts,
  because it polls a status byte that doesn't NACK in normal use. The
  PN7160's no-`irq_pin` polled probe only exists on new Klipper.
- Software (bit-banged) I2C behaves the same as hardware I2C. SPI and UART
  readers (RC522, PN5180, PN532 over SPI or UART) have no NACK and aren't
  affected.

`MmuNfcReader` (`mmu_nfc_reader.py`, ~785 lines) is the per-instance facade
above drivers. Its own docstring (`:11-12`) states it excludes lane state
machines, Spoolman lookups, LEDs, and scan-jog motion by design — those live
in `mmu_nfc_manager.py` and `mmu_filament_movement.py`.

## 2. jog_scan

Config: `nfc_gate_jog_scan_window`, `nfc_preload_jog_scan_window` — each a
`(neg, pos)` floatlist, default `[0.0, 0.0]` (off unless configured)
(`mmu_unit_parameters.py:189-190`).

Triggers: `MMU_NFC_SCAN` (`commands/mmu_nfc_scan.py:110` → `mmu._jog_scan()`),
and automatically during `MMU_PRELOAD` on a miss (via
`_home_to_gate_with_nfc`, `mmu_filament_movement.py:529`).

Implementation: orchestrator `_jog_scan()` (`mmu_filament_movement.py:632`)
delegates the actual motion to `_scan_sweeps()` (`:849`, sweeps the
larger-magnitude window side first since a hit short-circuits the rest) and
`_scan_datum_leg`/`_scan_leg` beneath it.

Why it exists: fast-path comment at `mmu_filament_movement.py:709-716` — the
tag may already be sitting on the reader (no jog needed); when it isn't, the
filament is jogged within the window, homing against the reader as a virtual
endstop (`MmuNfcEndstop`, `unit/nfc/mmu_nfc_endstop.py`), until the tag
enters the RF coupling volume, then re-parked exactly once off a real gate
datum — the `_jog_scan` docstring (`:640-653`) explains why the re-park must
happen once, off a datum, to avoid walking the filament backward on repeated
invocations.

## 3. Reader-pair sharing (shipped, `52737f53`)

User-facing config (`config/base/mmu_hardware.cfg:196-205`): with
`MMU_HAS_PER_GATE_NFC_READERS` set, list one `nfc_readers` name per gate;
**repeating a name shares one physical reader between neighboring gates**:

```
nfc_readers: a, a, b, b   # reader 'a' serves gates 0/1, reader 'b' serves gates 2/3
```

(matching explanation: `extras/mmu/mmu_unit.py:216-222`). In-tree example:
ViViD board, `installer/boards/custom/Kconfig.vvd:119-133` (two `rc522`
readers each serving a gate pair).

What `mmu_nfc_manager.py` does differently for a shared-pair reader:
- `_lookup_or_create_reader()` (`:154-167`) looks up `[mmu_nfc_reader NAME]`
  as a Klipper printer object first; a second gate naming the same section
  gets the *same* object back (`:158-160`).
- `_unique_readers()` (`:558-580`) dedupes `gate_readers` by object identity
  into `[(reader, [global_gate,...])]`. `_init_all_readers()` (`:582-596`)
  iterates *this*, not `gate_readers` directly — so a paired reader's
  hardware `init()` runs exactly once. **This was the actual bug `52737f53`
  fixed** — before it, a shared chip was re-initialized once per gate slot.
- Two distinct `MmuNfcEndstop` objects still exist, one per gate
  (`:143-151`), both wrapping the one reader — homing moves on the two
  paired gates are strictly serialized by the shared hardware.
- `enabled`/`active` flags stay **independent per gate slot** — plain
  arrays indexed by local-gate index (`self.gate_enabled[lg]`,
  `self.gate_active[lg]`, `:298-339`), not per-reader-object. Disabling gate
  0's reader does not disable gate 1's, even though it's the same chip.
- Bootup reader count (`_handle_mmu_bootup`, `:463`) is computed off the
  deduped set, so it logs "N readers" correctly rather than double-counting
  a shared pair.

**What it does not do:** no arbitration over whose tag is currently in the
shared reader's field between the two paired gates — the manager trusts
whichever tag the chip reports. This is a *different* gap from the
crosstalk problem below (this is one chip genuinely shared by two gates by
config; crosstalk is two *separate* chips picking up each other's field) —
see [noisy-neighbor-unmerged.md](noisy-neighbour-unmerged.md).
