# NFC/RFID driver architecture — reference

Anchors are symbol names; grep for them rather than trusting line numbers.

## 1. Drivers and the factory

Four chips, five driver modules, in `extras/mmu/unit/nfc/`:

| Module | Notes |
|---|---|
| `pn532_driver.py` | `_PN532Base`, shared across transports |
| `pn532_uart_driver.py` | HSU/UART transport for PN532 |
| `pn5180_driver.py` | no non-blocking probe (see below) |
| `pn7160_driver.py` | needs `irq_pin` for full-rate probing |
| `rc522_driver.py` | |

Supporting modules alongside them: `i2c_transport.py` (NACK-safe I2C, below),
`rx_gain.py` (per-chip receiver-gain tables, used by all four chips' `rx_gain`
option), `tag_parser.py` (tag payload decoding), `mmu_nfc_endstop.py`
(`MmuNfcEndstop`, the reader as a homing endstop) and `log.py`.

`reader_factory.py`:
- `SUPPORTED_READER_TYPES = ('pn532', 'pn5180', 'pn7160', 'rc522')`, default
  `pn532`.
- `SUPPORTED_INTERFACES` maps each chip to the transports it has a *driver*
  for: `pn532`→(i2c,spi,uart), `pn5180`→(spi,), `pn7160`→(i2c,),
  `rc522`→(spi,). This reflects driver coverage, not silicon capability.
- `create_reader()` is the single dispatch point building a concrete driver
  from a `[mmu_nfc_reader NAME]` config section.
- Driver contract (module header): `init()`, `is_alive()`, `read_tag()`,
  `read_target()`, optional `probe_start/probe_poll/probe_stop`.

**Maturity caveats, enforced in code, not just comments:**
- PN532-over-SPI logs "UNTESTED against real hardware" on every build
  (`create_reader()`).
- PN5180 has no presence-probe implementation — falls back to the blocking
  shim during homing, deliberately.
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
the host version. A new Klipper host with MCU firmware that hasn't been
re-flashed behaves like the Kalico column.

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

`MmuNfcReader` (`mmu_nfc_reader.py`) is the per-instance facade above
drivers. Its module header says it deliberately excludes lane state machines,
Spoolman lookups, LEDs, and scan-jog motion ("those live in your macros" — it
was extracted from a standalone extension). In Happy Hare they live in
`unit/mmu_nfc_manager.py`, `mmu_nfc_arbiter.py` and `mmu_filament_movement.py`.

## 2. jog_scan

Config: `nfc_gate_jog_scan_window`, `nfc_preload_jog_scan_window` — each a
`(neg, pos)` floatlist, default `[0.0, 0.0]` (off unless configured)
(ParamSpecs in `unit/mmu_unit_parameters.py`).

Triggers: `MMU_NFC_SCAN` (`commands/mmu_nfc_scan.py` → `mmu._jog_scan()`),
and automatically during `MMU_PRELOAD` on a miss (`_preload_gate()` →
`_home_to_gate_with_nfc()`, `mmu_filament_movement.py`).

Implementation: orchestrator `_jog_scan()` (`mmu_filament_movement.py`)
delegates the actual motion to `_scan_sweeps()` (sweeps the
larger-magnitude window side first since a hit short-circuits the rest) and
`_scan_datum_leg`/`_scan_leg` beneath it. Both `_jog_scan()` and
`_preload_gate()` run inside the field arbiter's `clear_field()` (§4).

Why it exists: the tag may already be sitting on the reader (the "tag already
at reader" fast path in `_jog_scan()`, no jog needed); when it isn't, the
filament is jogged within the window, homing against the reader as a virtual
endstop (`MmuNfcEndstop`, `unit/nfc/mmu_nfc_endstop.py`), until the tag
enters the RF coupling volume, then re-parked exactly once off a real gate
datum — the `_jog_scan` docstring explains why the re-park must
happen once, off a datum, to avoid walking the filament backward on repeated
invocations.

## 3. Reader-pair sharing (`52737f53`)

User-facing config (the `nfc_readers` block in `config/base/mmu_hardware.cfg`): with
`MMU_HAS_PER_GATE_NFC_READERS` set, list one `nfc_readers` name per gate;
**repeating a name shares one physical reader between neighboring gates**:

```
nfc_readers: a, a, b, b   # reader 'a' serves gates 0/1, reader 'b' serves gates 2/3
```

(parsed and validated in `MmuUnit.__init__`, `extras/mmu/mmu_unit.py`). In-tree
example: ViViD board, `installer/boards/custom/Kconfig.vvd` (two `rc522`
readers each serving a gate pair).

What `mmu_nfc_manager.py` does differently for a shared-pair reader:
- `_lookup_or_create_reader()` looks up `[mmu_nfc_reader NAME]` as a
  Klipper printer object first; a second gate naming the same section gets
  the *same* object back.
- `_unique_readers()` dedupes `gate_readers` by object identity into
  `[(reader, [global_gate,...])]`. `_init_all_readers()` iterates *this*, not `gate_readers` directly — so a paired reader's
  hardware `init()` runs exactly once. **This was the actual bug `52737f53`
  fixed** — before it, a shared chip was re-initialized once per gate slot.
- Two distinct `MmuNfcEndstop` objects still exist, one per gate
  (`_setup_readers()`), both wrapping the one reader — homing moves on the two
  paired gates are strictly serialized by the shared hardware.
- `enabled`/`active` flags stay **independent per gate slot** — plain
  arrays indexed by local-gate index (`self.gate_enabled[lg]`,
  `self.gate_active[lg]`), not per-reader-object. Disabling gate
  0's reader does not disable gate 1's, even though it's the same chip.
- Bootup reader count (`_handle_mmu_bootup()`) is computed off the
  deduped set, so it logs "N readers" correctly rather than double-counting
  a shared pair.

The manager itself trusts whichever tag the shared chip reports. Deciding
which of the two paired gates owns it is the field arbiter's job (§4).

## 4. Field arbitration (`mmu_nfc_arbiter.py`)

`MmuNfcFieldArbiter` (one per machine, `mmu.nfc_arbiter`) wraps each preload
and jog-scan in `clear_field(gate, nfc_mgr, ...)`, a context manager yielding
an `NfcFieldOutcome`:

- `_field_verdict()` classifies a UID purely from the gate map
  (`find_gate_by_rfid()`, then the Spoolman alias set
  `find_gate_by_rfid_alias()`) — no I/O, no motion. Verdict constants are
  `NFC_FIELD_*` in `mmu_constants.py`.
- `_settle()` re-probes and evicts candidates (`_neighbor_candidates()`:
  identified owner first, then `gate-1`/`gate+1`; eligibility re-checked per
  candidate by `_evict_reject()`) until clear or exhausted.
- `_restore_evicted()` re-parks evicted gates in reverse order on exit, even
  on error, and leaves `gate` selected.
- `_ratify()` settles a `PROVISIONAL` verdict after the `with` block,
  escalating to `_verify_by_self_jog()` only when the caller passes a
  clear distance.

Arming is decided by `_nfc_field_arm()` in `mmu_filament_movement.py`, which
returns `None` (passthrough) unless `nfc_neighbor_check`,
`nfc_neighbor_evict_distance` or the operation's own clear distance is set,
and never arms against an encoder endstop. Distance validation lives in
`_validate_nfc_neighbor_evict_distance()`,
`_validate_nfc_gate_clear_distance()` and
`_validate_nfc_preload_clear_distance()` (`unit/mmu_unit_parameters.py`),
re-run by the endstop, homing-max and parking-distance `on_change` hooks;
menuconfig mirrors the forward-jog rule as W17–W19 in
`installer/Kconfig.warnings`.

`_MMU_TEST NFC_FIELD=1` (`commands/mmu_dev_test.py`) reports whether
arbitration is armed and runs the classification ladder with no motion.
Tests: `test/test_mmu_nfc_neighbor.py` — read its header for what the
virtual chip can and can't simulate.
