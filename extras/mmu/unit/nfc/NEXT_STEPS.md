# NFC subsystem — next steps

Open work following the 2026-07-16 changes (see `CHANGELOG.md`). Target
architecture: the gate holds no cache of its own — it reads Happy Hare's gate
map when it needs to know something, and pushes reads into it. Everything
below is either a piece of that not yet finished, or a capability that was
removed and needs an equivalent that fits the new model.

## 1. Finish removing `_read_hh_status()` / `_gate_snapshot()`

Flagged as no-longer-needed, not yet done. Still called from:

- `_poll_hh_pause_check()`, `_check_hh_cleared()`, `_hh_gate_matches_current_spool()`
  (`manager.py`) — all per-lane logic that is already unreachable dead code
  for per-lane gates now (their only caller, `_poll()`, never runs for
  per-lane since the background timer is never armed — see the 2026-07-16
  entry). Safe to delete once confirmed nothing else reaches them.
- `_poll()` itself, `status_line()`'s old sync-mismatch logic (already gone),
  and other call sites — **must** be re-traced the same way the background
  poll loop was: several of these are genuinely shared-by-both (`_poll()` is
  used by the shared reader's own polling), so this needs the same
  per-lane-vs-shared classification pass, not a blind delete. Don't repeat the
  mistake made mid-session where `_hh_sync` was removed as "dead" before its
  real registration in `extras/mmu/commands/mmu_nfc.py` was found — grep the
  whole repo (`extras/mmu/commands/`, not just `extras/mmu/unit/nfc/`) before
  deleting anything here.

## 2. Reimplement `_NFC_SPOOL_REMOVED`, triggered by Happy Hare

The removed background poll loop was the *only* source of tag-removal
detection (`GateState.process_read(uid_hex=None, ...)`'s miss-count debounce).
Nothing currently notices a tag leaving a gate outside of Happy Hare's own
eject/unload sequence.

Needs: a Happy Hare-side hook analogous to the existing post-preload load hook
(`_NFC_SCAN_JOG_PRELOAD` / `variable_user_post_preload_extension`) — something
that fires on eject/unload and lets NFC clear its record for that gate, rather
than NFC discovering removal on its own via polling.

## 3. Decide the fate of `_NFC_SPOOL_CHANGED`

Proposed for removal ("Happy Hare will tell when the state changed") but never
resolved — this is the biggest open design question, not just an
implementation task. Currently `scan_jog.py`'s `finish()` and
`rewind_and_exit()` still dispatch through it directly
(`gate._klipper.dispatch(...)` / `gate._poll_klipper_dispatch(...)` →
`_NFC_SPOOL_CHANGED` gcode macro → `MMU_GATE_MAP ... SYNC=1`) — this is scan-jog's
only way of reporting a resolved spool back to Happy Hare. Whatever replaces
it needs to still get that result into the gate map. Options worth weighing:

- Keep the macro (it already works, is synchronous, and is Happy Hare's
  existing extension point for gate-map writes) and only remove the *cache*
  NFC used to keep around it — which is largely done already.
- Have NFC write Happy Hare's gate map directly in Python
  (`mmu.gate_maps.assign_spool_id()` / `set_gate_status()`, the same calls
  `MMU_GATE_MAP` itself makes), skipping the gcode round-trip, consistent with
  how NFC already *reads* the gate map directly rather than through a macro.

Pick one before touching `scan_jog.py`'s dispatch calls — this is exactly the
"gate is just a utility to push to the gate map" question, so the answer
should be settled deliberately, not inferred mid-edit.

## 4. Config knobs now inert for per-lane readers

`poll_interval`, `startup_polling`, `startup_poll_delay`, `absent_threshold`
are still real config options (still used by the shared reader) but silently
do nothing if set on a per-lane `[nfc_gate laneN]` / `[mmu_unit_parameters]`
override — no warning today. Decide: emit a diagnostic warning if set
per-lane, hide them from per-lane Kconfig prompts, or leave as-is since
they're harmless.

## 5. Dead-code cleanup once (1) is settled

`_check_hh_cleared()`, `_hh_gate_matches_current_spool()`, and the per-lane
branch of `_poll_hh_pause_check()` are unreachable for per-lane today (their
timer is never armed) but still textually present. Remove once the
`_read_hh_status()` work (item 1) lands, so it's one pass instead of two.

## 6. Minor: Moonraker proxy edge case

If Moonraker itself rejects a proxy request (not a Spoolman-side error — e.g.
a malformed `path`), the resulting `HTTPError` doesn't get `._body_text` set
the way a Spoolman-side error does (see `MoonrakerSpoolmanTransport.request()`
in `spoolman_client.py`, and the live-verification notes in `CHANGELOG.md`).
Only matters if the transport itself builds a bad request — none of the
current call sites do, since they all go through the `/api/`-stripping logic.
Low priority; wrap the `urlopen()` call in `try/except HTTPError` to capture
the body for logging if it's ever worth the extra robustness.

## 7. Re-confirm `POLL=1` still makes sense

The per-lane `NFC GATE=<#> POLL=1` manual command still calls `gate._poll()`
directly (bypassing the timer entirely), which still runs
`_poll_hh_pause_check()`/`_check_hh_cleared()`/`_read_hh_status()` internally.
Once item 1 is done, `POLL=1`'s behavior needs re-checking — it may need to
become a simpler "read once and report" that skips the now-removed HH-pause
machinery.
