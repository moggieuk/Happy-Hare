---
name: mmu3-avr-subsystem
description: Explains the Prusa MMU3's ATmega32U4 (2.5KB RAM, USB CDC) constraints that shape this fork's movement and shift-register code — the ~115-node MCU move-queue budget and why gear speed/accel defaults are 25mm/s @ accel 800 (accel-phase step budget v^2/2a), the SHR16 shift-register bitbang architecture and the write-coalescing / real-time gating / move-aware DIR / LED-deferral design in shift_register.py, and the USB CDC timing hazards (clock-sync convergence, the 30s delayed SR init, minclock=0 burst strategy, 500us command spacing). Use this whenever touching extras/shift_register.py, mmu_stepper.py's _pre_set_dir_pin / dir_sr_pin, gear/idler/selector speed or acceleration defaults in installer/mmu_types/Kconfig.prusa, the MMU3 board Kconfig, or debugging "Move queue overflow", "Rescheduled timer in the past", or "Timer too close" on unit0 — even for something as simple as "why is the gear so slow" or "the MMU3 shut down mid-move".
---

# Prusa MMU3 AVR subsystem (ATmega32U4 + SHR16)

The MMU3 runs its own 8-bit ATmega32U4 (2.5KB RAM, 16MHz, USB CDC serial) as
a Klipper MCU. Two things dominate every design decision in this fork's
movement and IO code:

1. **The MCU's move queue is tiny** — ~115 `queue_step` nodes, shared by
   everything. Klipper's normal step-generation batches exceed it for gear
   moves unless the accel phase is kept short.
2. **The SHR16 shift register is bitbanged over USB CDC** — every write is
   50 timed MCU commands spanning ~25ms, and USB CDC latency makes naive
   scheduling fail.

Both were discovered live on the reference MMU3 (this fork's development
machine); see the references for the full derivations:

- `references/avr-move-queue-budget.md` — pool size math, the step-compression
  model, why speed (not acceleration) used to overflow, the `v^2/2a` rule, and
  how the shipped defaults were derived.
- `references/sr-bitbang-and-usb-cdc-timing.md` — the bitbang protocol, the
  four-layer write-storm fix (coalescing, real-time gating, move-aware DIR
  scheduling, LED deferral), and the USB CDC clock-sync/timing hazards.

## Board and hardware facts

- **MCU**: ATmega32U4, 16MHz, 2.5KB SRAM, USB CDC (no native UART in this
  setup). Klipper firmware built with `CONFIG_AVR_STACK_SIZE=256`.
- **Move pool**: RAM − .data(48) − .bss(409) − stack(256) ≈ 1.8KB heap →
  `alloc_chunks(move_item_size≈16, 1024)` → **~115 move nodes**
  (`src/basecmd.c:move_finalize`). One node per outstanding compressed
  `queue_step` command; freed as steps execute.
- **SHR16** (two cascaded 74HC595): bits 0/1 gear DIR/ENABLE, 2/3 selector
  DIR/ENABLE, 4/5 idler DIR/ENABLE, 6-7 spare, 8-15 LEDs. DATA/CLOCK are
  shared with the TMC2130 software SPI bus (PB5/PC7) — `shift_register.py`
  bypasses Klipper's pin collision check via `allow_multi_use_pin` + raw MCU
  OIDs. LATCH (PB6) is exclusive.
- Stepper DIR pins are dummy GPIOs; the real direction lives on an SR bit
  (`dir_sr_pin`, written from Python before every move — see
  `mmu_stepper.py:_pre_set_dir_pin`). ENABLE also goes through the SR.
- The printer-side fork patches in klipper `src/avr/` are **bootloader only**
  (`HAVE_BOOTLOADER_REQUEST` + `avr/bootloader.c`) — nothing queue or
  timing related. No klippy patch is required.

## AVR move-queue budget — the rules that matter

- Per-flush step bursts must fit the pool. Klipper's step generation flushes
  in ~250ms batches (`BGFLUSH_SG_HIGH−LOW` in klippy `motion_queuing.py`)
  up to a ~0.7s lookahead.
- **Cruise compresses almost free** (constant interval → one command per huge
  run). **The acceleration phase is the budget killer**: `add` quantization
  limits it to ~2-4 steps/command near standstill, so its cost is
  `steps_in_accel = v^2/(2a) × steps_per_mm`.
- **The rule**: keep `v^2/(2a)` under ~0.7mm. At 165 steps/mm (gear,
  19.394mm/rev @ 16 microsteps) that means `a ≥ v^2/1.4`-ish: 25mm/s needs
  a ≥ ~450, so the shipped MMU3 defaults are **25mm/s @ accel 800** (load),
  **30mm/s @ accel 800** (short moves) — see
  `installer/mmu_types/Kconfig.prusa`.
- Empirically verified on the live MMU3: 25mm/s fails at accel 50 and 200,
  passes at accel 400+. **Microsteps don't fix it** (halving steps/mm still
  failed at 25mm/s). **Lower acceleration doesn't fix it** (the standstill
  burst is inside the first flush batch regardless) — only speed reduction
  or drip pacing helps. Don't repeat these experiments; trust the table in
  `references/avr-move-queue-budget.md`.
- Homing moves are **drip-paced** (50ms slices, `DRIP_SEGMENT_TIME`) and
  never overflow — the gear's *regular* moves are the vulnerable ones.
- If a change to MMU3 speed/accel defaults is needed, re-derive from the
  budget formula rather than tuning by feel; document the change with the
  `v^2/2a` numbers.

## Shift register bitbang — architecture and invariants

- A write shifts all 16 bits (data + clock rising + clock falling per bit,
  then latch pulse) = **50 `queue_digital_out` commands, 500µs apart
  (`dt = 8000 ticks / 16MHz`), ~25ms total**. Commands are sent as a
  `minclock=0` burst so they arrive before their scheduled times.
- **Invariant: no two bitbangs may overlap, and no bitbang may overlap step
  execution.** Two overlapping writes crash the timer dispatcher
  ("Rescheduled timer in the past"); 2-3 writes piled around a gear move
  overflow the move queue (the live 12:17 AM failure that motivated the fix).
- The four-layer design in `extras/shift_register.py` + `mmu_stepper.py`:
  1. **Coalescing**: at most one pending write; state changes merge into it
     (latest wins), generation deferred to ~50ms before start. A burst of
     DIR flips + LED frames collapses to 1-2 bitbangs.
  2. **Real-time gating**: a new write behind an in-flight one schedules
     after `_last_write_end` instead of overlapping.
  3. **Move-aware DIR** (`_pre_set_dir_pin`): the bitbang is scheduled at
     `max(now_pt + 0.100, prev_move_end + 0.005)` — strictly after the
     previous move's steps — and the move is delayed past `write_end`
     (returned start + 0.035) so the first step lands after the bitbang.
  4. **LED deferral**: bits in `led_mask` (config, `0xFF00` on MMU3) are
     cosmetic — while the toolhead is ahead of the MCU their writes are
     pushed to `est + 0.150`.
- `_set_bit_at_time` returns the *actual* scheduled start (post-merge /
  post-deferral) — `_pre_set_dir_pin` depends on that contract for its move
  delay. Keep it when extending.
- The harness instantiates the real `shift_register` module; tests live in
  `test/test_mmu_selector.py` (`TestPrusaIdlerSelector`): SR state checks,
  `test_sr_writes_coalesce_into_a_single_pending_bitbang`,
  `test_sr_led_writes_defer_while_steps_execute`. NOTE: the harness print
  clock is offset negative — normalize `_last_write_end`/pending state
  before asserting timing (see those tests).

## USB CDC timing hazards

- After MCU connect, **clock-sync takes ~30s to converge** over USB CDC
  (Caterina/CdcACM latency); a too-early scheduled write lands in the past.
  Hence the SR's initial write is deferred 30s after `klippy:connect`
  (`_handle_connect` → `_delayed_sr_write`).
- `queue_digital_out` commands with `minclock=last_clock` would have the host
  pace sends to the schedule — USB CDC latency (~1ms) pushes each command
  past its time → **"Timer too close"**. The burst (`minclock=0`) + 500µs
  spacing exists specifically to avoid this. Do not "optimize" the spacing
  without re-verifying on the AVR.
- Bootup autohome is deferred until the clock estimate is reliable
  (commit `04f93269`) for the same reason.
- A `minclock=0` burst of 50 commands ≈ 300 bytes can exceed the 192-byte
  RX window → retransmits (`bytes_retransmit` in Stats) — another reason to
  keep write *count* low (coalescing) rather than larger bursts.

## Reference links

- **Prusa MMU firmware** (official, ATmega32U4, MMU2S/MMU3): source +
  releases at https://github.com/prusa3d/Prusa-Firmware-MMU — `src/modules/shr16*`
  is the reference bitbang/register mapping.
- **MMU control board hardware + schematic** (MM-control-2.0, rev.03):
  schematic PDF at
  https://github.com/prusa3d/MM-control-2.0/blob/master/rev.03/MM-control.pdf
  (74HC595 shift register driving TMC2130 DIR/EN pins). Older board
  firmware reference: https://github.com/prusa3d/MM-control-01.
- **Upstream Klipper shift-register PR** (based on this fork's work,
  credits @Freakazo): https://github.com/Klipper3d/klipper/pull/7223 —
  useful context for how the same problem was solved in C for stock Klipper.

## Key file references

- `extras/shift_register.py` — the SR chip, `_write_register`,
  `_request_write` (coalescing/gating), `_defer_cosmetic_write`,
  `led_mask` config option.
- `extras/mmu_stepper.py:_pre_set_dir_pin` — SR DIR write + move-delay
  contract (lines ~866-920).
- `installer/mmu_types/Kconfig.prusa` — MMU3 gear speed/accel defaults with
  the `v^2/2a` rationale in help text.
- `installer/boards/custom/Kconfig.prusa_mmu3` — MMU3 pinouts, SR bits,
  gear `dir_sr_pin` polarity (NOT inverted — positive moves load).
- `config/base/mmu.cfg` — `[shift_register mmu_sr]` section incl. `led_mask`.
- `test/test_mmu_selector.py` — SR/coalescing tests.
