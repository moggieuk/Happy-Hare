# AVR move-queue budget — derivation and empirical table

Everything here was measured/derived on the live MMU3 (ATmega32U4, 16MHz,
2.5KB RAM) running this fork with the gear at `rotation_distance 19.394`,
16 microsteps, 200 full steps/rev → **165 steps/mm**.

## The pool

- RAM 2560 bytes; `.data` 48; `.bss` 409; stack 256 (`CONFIG_AVR_STACK_SIZE`).
- Heap ≈ 1847 bytes → `alloc_chunks(move_item_size≈16, 1024)` in
  `src/basecmd.c:move_finalize` → **~115 move nodes**.
- One node is consumed per outstanding compressed `queue_step` command and
  freed when its steps execute. "Move queue overflow" (`basecmd.c:move_alloc`)
  = the host had more commands in flight than nodes.

## Step generation / flushing

- Klippy flushes steps in **~250ms batches** (`BGFLUSH_SG_HIGH_TIME 0.700 −
  BGFLUSH_SG_LOW_TIME 0.450` in `klippy/extras/motion_queuing.py`), up to a
  ~0.7s lookahead from the MCU's live `estimated_print_time`.
- `queue_step` compression is quadratic (interval + integer `add` per step):
  - **Cruise** (`add` ≈ 0): near-perfect — one command covers thousands of
    steps. Effectively free.
  - **Acceleration**: `add` quantization error forces short commands —
    ~2-4 steps/command near standstill, improving as speed builds. This is
    the entire budget problem.

## The accel-phase cost model

Steps inside the accel phase: `N = v^2/(2a) × steps_per_mm`.

For the pool to survive a flush containing the accel phase plus the
standstill zone:

```
v^2/(2a) × 165  ≲  600        (≈115 nodes at ~3-5 steps/command)
⇒  v^2/(2a)     ≲  0.7mm      (⇒ a ≥ v^2/1.4)
```

Shipped defaults (from this rule): load **25mm/s @ accel 800**
(v²/2a = 0.39mm), short moves **30mm/s @ accel 800** (0.56mm). These live in
`installer/mmu_types/Kconfig.prusa` with the rationale in help text.

## Empirical table (live MMU3, 10mm moves unless noted)

| Test | Accel-phase steps (v²/2a×165) | Result |
|---|---|---|
| 25mm/s, a=50 | 1030 | FAIL |
| 25mm/s, a=25 | 2060 | FAIL |
| 25mm/s, a=200 | 258 | FAIL (marginal) |
| 25mm/s, a=400 | 129 | PASS |
| 10mm/s, a=50 | 165 | PASS |
| 10mm/s, a=200 | 41 | PASS |
| 5mm/s, a=25 | 82 | PASS |
| 8µsteps, 25mm/s, a=50 | 515 | FAIL (microsteps don't fix it) |
| 100mm @ defaults (25mm/s, a=800) | 0.39mm | PASS |

## Things that do NOT help (verified, don't re-try)

- **Lower acceleration** — the standstill zone stays inside the first flush
  batch; per-batch average is `v/2` regardless of `a`.
- **Microstep reduction** — 16→8 halved steps/mm yet still failed at 25mm/s.
- **"Smoother" profiles / S-curves** — higher-order motion breaks the
  quadratic model; worse, not better.

## What DOES help

- **Speed reduction** (linear reduction of per-flush steps).
- **High, punchy acceleration** (short accel phase — constant accel is the
  model's native shape).
- **Drip pacing** (homing already uses it; 50ms slices never burst).
