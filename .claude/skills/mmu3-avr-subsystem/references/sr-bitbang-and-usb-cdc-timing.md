# Shift register bitbang architecture and USB CDC timing

## The bitbang

SHR16 = two cascaded 74HC595. A full register write shifts 16 bits out MSB
first, then latches:

```
per bit (MSB→LSB):  DATA = bit; CLOCK 1; CLOCK 0     (3 commands)
then:              LATCH 1; LATCH 0                  (2 commands)
= 50 queue_digital_out commands for 16 bits
```

- Commands are spaced `dt = 8000 ticks / 16MHz = 500µs` apart → ~25ms per
  write. The spacing absorbs USB CDC jitter.
- All 50 commands are sent as a **`minclock=0` burst** (see
  `_RawDigitalOut.set_digital`) so they arrive at the MCU before their
  scheduled times. With `minclock=last_clock` the host would pace sends to
  the schedule and USB CDC latency (~1ms) would push commands past their
  times → **"Timer too close"** shutdown.
- `_last_write_end` is published *before* the commands are generated
  (race safety: a request arriving mid-generation must see the write as
  in-flight).

## Failure modes observed live

1. **Two back-to-back bitbangs** (e.g. idler and selector homing ms apart):
   the MCU timer dispatcher saw backwards clock values →
   **"Rescheduled timer in the past"** shutdown at every boot (pre-serialization).
2. **2-3 writes piled around a gear move** (12:17 AM on the reference
   machine: `SR write` + two `SR defer` lines in the log, then
   **"Move queue overflow"**): the ~25ms timer-event storms + step execution
   together backed up the move pool.

## The four-layer fix (current design)

All in `extras/shift_register.py` (+ `mmu_stepper.py:_pre_set_dir_pin`).

### 1. Coalescing (bounded write count)

`_request_write(print_time, bit_num)`:

- If a write is **pending** (scheduled, not yet generated): merge — return
  the pending start, do nothing else. The pending write reads `self.state`
  when it generates → latest-state-wins.
- If a write is **in flight** (`print_time < _last_write_end`): schedule ONE
  follow-up at `_last_write_end + 0.001`.
- Otherwise: if `print_time` is far enough out, defer generation via a
  reactor timer (~50ms lead, `_fire_pending_write`); else generate now.

A DIR-flip storm + LED frames collapse to 1-2 bitbangs.

### 2. Real-time gating (no overlapping writes)

The in-flight branch above never overlaps bitbangs; generation happens no
earlier than ~50ms before the scheduled start.

### 3. Move-aware DIR scheduling (no bitbang during steps)

`_pre_set_dir_pin` (mmu_stepper.py):

```python
base = move_time if move_time is not None else now_pt
sched_time = max(now_pt + 0.100, base + 0.005)   # after previous move's steps
actual_time = vpin.set_digital(sched_time, new_dir)
write_end = actual_time + 0.035
return max(0., write_end - base)                  # move delayed past write
```

The move's first step lands ~40ms after the previous move ended — after the
25ms bitbang. `_set_bit_at_time` returns the *actual* scheduled start (after
merge/defer), which is what makes this delay correct. Keep that contract.

### 4. LED deferral (no cosmetic writes mid-move)

`led_mask` config option (MMU3: `0xFF00`, bits 8-15). A write touching only
masked bits while the toolhead is ahead of the MCU (`print_time >
est_print_time + 0.050`) is pushed to `est_print_time + 0.150`. LED frames
then cost at most one ~25ms bitbang per ~150ms during a move.

## USB CDC timing hazards

- **Clock-sync convergence**: after MCU connect, `estimated_print_time` can
  be off by 1.5-2s (Caterina/USB CDC latency). The initial SR write is
  deferred **30s** after `klippy:connect` (`_handle_connect` →
  `_delayed_sr_write`); bootup autohome likewise waits for a reliable clock
  (commit `04f93269`).
- **Timed commands vs serial latency**: never use `minclock=last_clock`
  pacing for bitbang commands; keep the burst + 500µs spacing.
- **RX window**: a 50-command burst ≈ 300 bytes vs the 192-byte RX buffer —
  overflow triggers CRC-retransmit storms (`bytes_retransmit` in Stats).
  Coalescing keeps bursts *few*, which matters more than their size.

## Tests

`test/test_mmu_selector.py` → `TestPrusaIdlerSelector`:

- `test_idler_moves_go_through_the_mmu_stepper_with_dir_pre_set` — SR bit
  state follows moves.
- `test_sr_writes_coalesce_into_a_single_pending_bitbang` — merge semantics,
  one timer, single generation on fire.
- `test_sr_led_writes_defer_while_steps_execute` — cosmetic deferral via a
  simulated busy toolhead.

Harness quirk: the fake MCU's print clock is offset negative and moves leave
a pending write behind — normalize `_pending_write_start` / `_last_write_end`
to `now_pt` before asserting timing (the tests show the pattern).
