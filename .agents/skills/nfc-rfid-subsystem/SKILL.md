---
name: nfc-rfid-subsystem
description: Explains Happy Hare's NFC/RFID subsystem — the driver abstraction for pn532/pn5180/pn7160/rc522 readers, jog_scan mechanics, reader-pair sharing, and the "noisy neighbor" field arbiter (MmuNfcFieldArbiter) that stops a neighboring gate's tag being attributed to the wrong gate. Use this whenever touching NFC/RFID reader code, mmu_nfc_manager.py, mmu_nfc_arbiter.py, reader_factory.py, jog_scan, nfc_neighbor_check / nfc_neighbor_evict_distance / nfc_*_clear_distance, Spoolman tag lookups or auto-create, nfc_readers config, I2C NACK handling / i2c_transfer / Kalico or old-Klipper compatibility of the I2C readers, or debugging cross-gate tag misattribution — even for something as simple as "the NFC reader isn't detecting the right spool" or "how do I wire two gates to one reader."
---

# NFC/RFID subsystem

There are **two different sharing problems** that sound similar and are easy
to conflate: reader-*pairing* (one physical chip deliberately serving two
gates by config) vs. RF-*crosstalk* (a neighbor's tag showing up in your
reader's field even though each gate has its own chip). If you're debugging a
misattributed tag, work out which one you're actually looking at first. The
field arbiter below handles both, but only when it's switched on.

## Drivers

Chip drivers under `extras/mmu/unit/nfc/` (`pn532_driver.py`,
`pn532_uart_driver.py`, `pn5180_driver.py`, `pn7160_driver.py`,
`rc522_driver.py`), dispatched by `reader_factory.py`'s `create_reader()`.
Drivers share a contract (`init()`, `is_alive()`, `read_tag()`,
`read_target()`, optional `probe_start/probe_poll/probe_stop` for
non-blocking homing). Maturity isn't uniform — PN532-over-SPI logs
"UNTESTED against real hardware" on every build, PN5180 has no
non-blocking probe at all, PN7160 needs a wired `irq_pin` for full-rate
probing. Check the factory's own warnings before assuming a driver is as
solid as another. I2C readers (PN532, PN7160) also depend on the MCU
firmware having Klipper's `i2c_transfer` command: see the I2C support matrix
in [references/driver-architecture.md](references/driver-architecture.md)
before assuming how one behaves on Kalico or old Klipper.

`MmuNfcReader` (`extras/mmu/unit/nfc/mmu_nfc_reader.py`) is the per-instance
facade above the drivers and is hardware-only by design: no lane state
machines, Spoolman lookups, LEDs, or scan-jog motion. In Happy Hare those
live in `unit/mmu_nfc_manager.py`, `mmu_nfc_arbiter.py` and
`mmu_filament_movement.py`. Don't add them to the reader facade.

## jog_scan

A tag's position relative to the fixed reader isn't guaranteed, so
`_jog_scan()` (`mmu_filament_movement.py`) first checks for a tag already in
range (no motion needed), then jogs the filament within a configurable
`(neg, pos)` mm window, homing against the reader as a virtual endstop,
until the tag enters the RF coupling volume — then re-parks exactly once off
a real gate datum. Triggered by `MMU_NFC_SCAN` directly, and automatically by
`MMU_PRELOAD` on a miss.

## Reader-pair sharing

Repeating a reader name in `nfc_readers:` (e.g. `nfc_readers: a, a, b, b`)
shares one physical chip between two gates. `mmu_nfc_manager.py` looks up the
printer object by name, so both gates get the same Python object; hardware
`init()` runs once per *deduped* reader, not once per gate slot
(`_unique_readers()`); `enabled`/`active` state stays independent per gate
slot even though the chip is shared.

## Field arbitration ("noisy neighbor")

Per-gate readers can sit close enough that a neighboring gate's spool is
inside gate G's own RF field, and a paired reader always sees both gates'
tags. `MmuNfcFieldArbiter` (`extras/mmu/mmu_nfc_arbiter.py`) settles who owns
the tag in G's field before `_preload_gate()` or `_jog_scan()` trusts it,
via its `clear_field()` context manager. It classifies by the **gate map**,
not by which chip saw the tag, so it covers crosstalk and pairing alike:

- `MINE` — registered to G.
- `NEIGHBOR` — registered to another gate on the same unit: evicted by
  loading that gate and jogging it `nfc_neighbor_evict_distance` mm, then
  re-parked on exit, even on error. Internal only; never yielded.
- `FOREIGN` — registered to a gate on a different unit (the map is stale), or
  a neighbor that couldn't be evicted: the tag is not read.
- `PROVISIONAL` — unregistered: treated as G's until the caller's own motion
  shows it leaving the field (or a deliberate self-jog of
  `nfc_gate_clear_distance` / `nfc_preload_clear_distance` does).

Everything is **off by default** — `nfc_neighbor_check`,
`nfc_neighbor_evict_distance` and both clear distances are 0, and
`_nfc_field_arm()` then leaves `clear_field()` an inert passthrough. Two
constraints matter if you change it:

- **Forward jogs are only safe on a per-gate exit path.** On a shared
  endstop every gate's filament merges downstream, so a forward eviction or
  self-jog would push filament into the path being read. The unit-parameter
  validators reject a positive distance unless the matching homing endstop is
  `mmu_exit`, and menuconfig warns (W17/W18/W19). Same hazard as the
  [gate-endstop-invariants skill](../gate-endstop-invariants/SKILL.md).
- **The harness can't prove eviction works.** The virtual NFC chip is
  per-gate isolated, so `test/test_mmu_nfc_neighbor.py` exercises the
  classification ladder and eviction bookkeeping, not a neighbor's tag
  physically leaving the field. Eviction changes need hardware testing.

More detail and symbol anchors are in
[references/driver-architecture.md](references/driver-architecture.md).
