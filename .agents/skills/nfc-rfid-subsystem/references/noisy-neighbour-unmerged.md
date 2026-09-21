# RF-crosstalk / "noisy neighbour" mitigation — unmerged design

**Status: proposed, never merged into `private_v4`. Off by default even
where it exists. The riskiest part (actual eviction motion) has zero test
coverage, by construction — read the caveat at the end before trusting the
green test count in the source commit.**

This document exists because the design lives in exactly one place: commit
`e78b5906` on branch `private_rfid_neighbor`. It never got pushed as a PR
against this repo's `private_v4` history, but the branch itself is on the
shared remote — fetch it from there, which works for any contributor, not
just from a specific machine:

```bash
git fetch origin private_rfid_neighbor && git show e78b5906
```

(Verify with `git ls-remote origin private_rfid_neighbor` first — if that
comes back empty, the branch has been deleted upstream since this was
written, and this file plus whichever local checkout still has it are all
that's left. In that case, treat this file as the source of truth and don't
rely on the `git show` above.)

Related but *not* this: `FUTURE/*.md` in that same sibling checkout is a
different, earlier, now-superseded body of session notes about the general
NFC→Spoolman architecture (most of which has since shipped). None of those
files mention crosstalk, neighbours, or eviction — don't confuse the two.

## The problem

Per-gate NFC readers can sit close enough together that a spool parked at a
*neighbouring* gate is physically inside gate G's own RF field. Two code
paths trusted "a tag is at my reader" to mean "this gate's tag":
`_jog_scan`'s fast path, and the preload compound endstop (which fires on
*any* UID in the field). Either can mis-assign a neighbour's spool to the
wrong gate, or let a foreign tag win anticollision and block the real one.

This is distinct from reader-*pair sharing* (shipped, see
[driver-architecture.md](driver-architecture.md) §3) — that's one physical
chip deliberately serving two gates by config. This is two *separate*
chips picking up each other's field by accident.

## The proposed fix: a classification ladder

New constants (in the diff, destined for `mmu_constants.py`):

```python
NFC_FIELD_CLEAR     = 0  # nothing in the field
NFC_FIELD_MINE      = 1  # this gate's own tag, or unknown to the gate map (assumed ours)
NFC_FIELD_NEIGHBOUR = 2  # registered to another gate on the SAME unit — evictable
NFC_FIELD_FOREIGN   = 3  # registered to a gate on a DIFFERENT unit, or a NEIGHBOUR that
                         # couldn't be cleared
```

A new `_nfc_field_verdict(gate, uid)` method probes the field and classifies
whatever's in it against the gate map before trusting the read:

- `MINE` or `CLEAR` → proceed exactly as before.
- `NEIGHBOUR` → evict by temporarily loading that other gate and jogging
  its filament off its park position, for the duration of this operation,
  re-parking it afterwards **even on error**.
- `FOREIGN` → a stale map (a tag from another unit, or a neighbour that
  couldn't be evicted) — warn and fall back to a plain non-NFC operation
  rather than attribute the tag to the wrong gate.

Gated behind a new per-unit param `nfc_neighbor_evict_distance`, default
`0.0` = off. At the default, the wrapper never arms — a stock machine does
zero extra reader I/O and behaves exactly as it does today.

### A prerequisite bug this exposed

`gate_spool_rfid` was only ever written by `_apply_metadata_to_gate`, which
needs a deep-read plus non-`PULL` Spoolman mode. A plain UID-only read
recorded nothing, so the gate map could never answer "whose tag is this?" —
which the classification ladder depends on. The fix makes `_nfc_tag_read`
record the UID unconditionally, regardless of read depth or Spoolman mode.
**If you resume this work, check whether that prerequisite fix is still
needed** — it may be worth landing on its own regardless of whether the
eviction feature itself is picked back up.

### The shared-exit direction constraint

A forward eviction jog is rejected at config load unless
`gate_homing_endstop` is the per-gate `mmu_exit` sensor, with a matching
menuconfig warning (`W17`). **This constraint has to be preserved in any
rework**: on a shared-exit path — including the extruder entry sensor,
which registers as `mmu_shared_exit` on no-bowden designs — every gate's
filament merges downstream, so jogging a neighbour's filament *forward* to
evict it would push it into the path of the gate being read. The fix jogs
**backward** in that case instead. This is the same shared-path hazard the
[gate-endstop-invariants skill](../../gate-endstop-invariants/SKILL.md)
guards against elsewhere — read that skill too if you're touching this.

### Bench-tuning parameter, explicitly not a fix

`rx_gain` is added as a per-reader tuning parameter (rc522/pn532; pn5180 and
pn7160 log unsupported). The commit is explicit that this is **not** a
mitigation for this problem — a neighbour's tag can sit physically closer to
the antenna than your own gate's tag, so gain alone can't discriminate them.

## Test coverage — read this before trusting "0 failures"

The commit adds `_MMU_TEST NFC_FIELD=1` (a dev-test probe exercising the
verdict ladder with no reader hardware and no motion) plus an
`nfc_neighbor` test profile and 17 harness tests covering: the verdict
ladder itself, eviction eligibility, arming, and the UID-plumbing
prerequisite above. Commit message states "Full suite: 430 tests, 0
failures."

**What is explicitly not covered: the eviction movement itself.** The test
harness's virtual NFC chip is per-gate isolated by design — no fixture can
put one gate's tag inside a neighbouring gate's field, so there is no way
for this test suite to exercise the actual physical mechanism the feature
exists to fix. That's left to on-hardware verification, per the commit
message. **A green run of these tests validates the bookkeeping around
eviction, not that eviction works.** Don't let a passing suite stand in for
hardware testing if you resume or extend this.

## If you're picking this up

1. Pull the commit (`git cherry-pick e78b5906` after the fetch above, or
   `git show e78b5906 | git apply`) rather than re-deriving the ladder from
   this description — the description is for orientation, the diff has the
   actual code.
2. Re-check the prerequisite `_nfc_tag_read` fix against current
   `private_v4` — this repo has moved since Jul 2026 and NFC code has
   changed (`52737f53` landed after this branch was cut).
3. Preserve the shared-exit forward-jog rejection (§ above) exactly — it's
   a safety constraint discovered the hard way, not an arbitrary limitation.
4. Plan actual hardware verification for the eviction motion before calling
   this done. The test suite cannot tell you it works.

## Diagrams

`diagrams/*.svg` in this reference folder are sequence diagrams for the
*already-shipped* NFC flows (preload, shared-reader init, jog-scan, and two
autocreate variants) — useful for orienting in this subsystem generally,
but they predate and do not depict the crosstalk/eviction design above.
