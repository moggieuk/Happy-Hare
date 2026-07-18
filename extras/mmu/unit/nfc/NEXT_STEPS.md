# NFC subsystem — next steps

Started as open work following the 2026-07-16 changes (see `CHANGELOG.md`).
Target architecture: the gate holds no cache of Happy Hare's own state — it
reads Happy Hare's gate map directly when it needs to know something, and
pushes reads into it directly. Items 0-7 below (2026-07-17) closed that phase;
kept as a record of what changed and why, since several of these turned on
non-obvious reasoning that's easy to re-litigate by accident later.

## Addon-parity phase (started 2026-07-17)

Follow-on goal: NFC should be installed and behave like Blobifier — an addon
Happy Hare's own sequence calls into, not something requiring a manual
post-install config edit to activate, and not something that has to poll
Happy Hare to infer what it should be doing.

### A. Done, then partially reverted: auto-wire post-preload/post-unload hooks at install time

See the 2026-07-17 `CHANGELOG.md` entries for the full writeup, including the
part that got reverted 2026-07-18.

**Preload side stays auto-wired.** `installer/macro_vars/Kconfig.sequence`
defaults `variable_user_post_preload_extension` based on NFC topology
instead of leaving it empty for the user to fill in by hand — per-lane-only
gets `_NFC_SCAN_JOG_PRELOAD`, shared-only gets `_NFC_SHARED_PRELOAD`, hybrid
gets the new `_NFC_POST_PRELOAD` (calls both, unconditionally — safe because
it's only ever wired in when both genuinely exist). No known collision on
this slot.

**Fixed 2026-07-18 — was correctly called out as an unacceptable regression,
not just a caveat.** Auto-wiring turned "partial per-lane coverage in hybrid
topology hits a hard error" from a rare manual-misconfiguration edge case
into something that would fire automatically on every preload for any gate
without its own reader. The earlier claim that no non-raising existence
check was available was wrong — `nfc_gate_for_gate_number()` is Python-only,
but native per-lane `NFCGate` instances *are* registered under a predictable,
Jinja-addressable name: `mmu_unit.printer.add_object('nfc_gate lane%d' %
gate, lane)` (`extras/mmu/unit/mmu_nfc.py:49`), exactly parallel to the
shared reader's `nfc_gate shared`. Missed it because I'd only looked in
`manager.py`, not the native integration glue in `mmu_nfc.py`.

`_NFC_SCAN_JOG_PRELOAD` and `_NFC_GATE_UNLOADED` (`config/macros/mmu_nfc.cfg`)
now both check `printer['nfc_gate lane' ~ gate] is defined` before calling
into `NFC` for that gate, and skip quietly (an info message, not an error)
when it isn't. `printer[...] is defined` for an optional object is the
established idiom for this in the codebase already — `mmu_form_tip.cfg`,
`blobifier.cfg`, `mmu_cut_tip.cfg` all do the same thing. `_NFC_POST_PRELOAD`
needed no changes itself; it's automatically safe now since it just calls
`_NFC_SCAN_JOG_PRELOAD`.

**Unload side reverted (2026-07-18) — never auto-wire this slot.**
`variable_user_post_unload_extension` is also the slot `mmu_controller.py`'s
`has_mmu_cutter` detection reads (`'cut' in user_post_unload_extension`,
same pattern as `has_blobifier`) — i.e. it's already claimed by MMU-cutter
integrations like `EREC_CUTTER_ACTION` in real installs, not just a
theoretical collision. Auto-wiring `_NFC_GATE_UNLOADED` into it by default
would silently override a macro a cutter user already had configured.
`VAR_SEQUENCE_USER_POST_UNLOAD_EXTENSION` is back to a plain `default ""`
for every topology. `_NFC_GATE_UNLOADED` (`config/macros/mmu_nfc.cfg`) still
exists and still works — it now just requires the user to wire it in by hand
(`variable_user_post_unload_extension: '_NFC_GATE_UNLOADED'`), same as
before this phase started, calling it alongside any existing macro if that
slot is already in use.

**Consequence for item C below, resolved 2026-07-18 — turns out NFC mostly
doesn't need to know unload happened at all.** Traced whether the
`_handle_hh_unload()`/`_NFC_GATE_UNLOADED` push is actually load-bearing now
that it can't be relied on to be wired. It isn't, for the common case:
`_poll_hh_pause_check()` checks `self._scan_mode` *before* `_poll_enabled`
(`manager.py`), so while a scan-jog is running `_poll_enabled`'s value is
irrelevant — it's bypassed entirely. And `scan_jog.start()`, which fires at
the top of every scan-jog run (already auto-wired via the preload hook, see
above), already force-resets `GateState.current_uid`/`current_spool`
directly before searching. So for any gate using scan-jog, every load
attempt self-heals regardless of whether the prior unload was ever signalled.
Added one line — `start()` now also resets `gate._poll_enabled = True` —
closing the one gap that existed without it: an unload with no push wired,
followed by a scan-jog that finds nothing (empty gate), used to leave
`_poll_enabled` stuck `False` from the previous load, so a manual `POLL=1`
on that now-empty gate would wrongly report "already reported, skipping."

**Net effect: `_NFC_GATE_UNLOADED` only matters for per-lane gates that
don't use scan-jog at all** (pure manual `POLL=1`/`APPLY=1` workflow, no
automatic detection of any kind). For those, nothing else ever resets
`_poll_enabled`/`GateState`, so the unload push (manually wired, combined
with any existing cutter macro) is still the only way to avoid getting stuck
after the first manual assignment. That's a narrow, already-manual-workflow
audience — not worth building a general combining-macro mechanism for. The
"how do I add this to my existing cutter macro" question resolves to: for
scan-jog users, don't bother wiring it at all; for the rare non-scan-jog
case, wire it in by hand alongside whatever else is already there, per the
`config/macros/mmu_nfc.cfg` header comment.

### B. Not started: give Happy Hare's core a first-class "NFC exists" flag

Right now `mmu_controller.py` has zero awareness NFC is installed — not even
the hacky `'blob' in ...` string-sniff Blobifier gets (`self.has_blobifier`,
`mmu_controller.py:188`, itself flagged by Happy Hare's own maintainer as
"hacky until a more universal approach is implemented"). A `self.has_nfc`
equivalent would let Happy Hare's own status/diagnostics know NFC is present
without NFC announcing itself. Not attempted yet — touches Happy Hare's own
core (`mmu_controller.py`), not NFC's package, so needs more care about
scope/ownership before starting.

### C. Done: `_poll_hh_pause_check()` — pull to push

Was the last place NFC asked Happy Hare a question every poll ("are you done
with this gate yet?") instead of being told. Original plan was a new
`post_load` push from Happy Hare (`NFC GATE=<n> POLL_DISABLE=1`), symmetric
with the unload push — dropped once `user_post_load_extension` turned out to
already be a claimed slot: it's the hook Blobifier's legacy detection reads
(`mmu_controller.py:188`, `self.has_blobifier = 'blob' in ...`), and its
gcode macro's own comment says as much ("A good place to implement custom
purging logic ... e.g. Blobifier"). Auto-wiring `_NFC_...` into that variable
would silently break anyone using both.

Landed on something better than the original plan, not just a workaround:
NFC doesn't need Happy Hare to tell it "the gate is loaded" at all, because
NFC already knows the instant it happens — it's the one that just dispatched
the CHANGED/UID_ONLY event that *makes* Happy Hare's gate map show the gate
loaded. `_poll_klipper_dispatch()` (`manager.py`) now sets
`self._poll_enabled = False` right after that dispatch — both `CHANGED` and
`UID_ONLY` mark the gate `AVAILABLE=1`, so either one means "nothing left to
notice by re-reading." `REMOVED` sets it back to `True`. The only thing that
still has to be push-driven is re-enabling on an *unload*, since NFC has no
other way to learn Happy Hare unloaded a gate while its own polling is off —
that's `_handle_hh_unload()` (`NFC GATE=<n> UNLOADED=1`, already existed for
`_NFC_GATE_UNLOADED`), now also flips `_poll_enabled = True`. No new
Happy-Hare-side hook needed at all, and no collision risk with anything else.

`_poll_hh_pause_check()` now just checks `self._poll_enabled` — no
`_read_hh_status()` call in that path anymore. `_hh_gate_matches_current_spool()`
(the function whose only job was re-deriving the same answer by querying
Happy Hare) is deleted. Added `NFC GATE=<n> POLL_ENABLE=1`/`POLL_DISABLE=1`
as a manual debugging override on top of the automatic management
(`_set_poll_enabled()`).

**Not covered by this item, deliberately:** `_NFC_SPOOL_REMOVED`'s
miss-count-debounce path (NFC noticing a tag physically disappeared and
telling Happy Hare) is NFC's own domain judgment, not a guess about Happy
Hare's state, and stays exactly as it is — same as Blobifier deciding on its
own when to purge. "Addon, not state-holder" means not guessing about Happy
Hare's own state, not zero autonomy.

**Not yet added:** a test for the automatic `_poll_enabled` management —
no existing test constructs a full `NFCGate` (the suite only exercises
`GateState`/`KlipperInterface` directly), so this landed with the same test
coverage gap the rest of `manager.py` already has, not a new one.

## 0. Done: the three cached-opinion-of-Happy-Hare fields

`_hh_confirmed_spool`, `_hh_load_paused`, and `_suppress_next_dispatch_uid`/
`_suppress_next_dispatch_spool` are removed from `manager.py`/`scan_jog.py`.
(The `_suppress_next_dispatch_*` pair turned out to be write-only — set in
`_clear_spool_cache()` but never read anywhere — so removing it was pure
dead-code cleanup, not a behavior change.) `_poll_hh_pause_check()` is
stateless: it queries `_read_hh_status()`/`_hh_gate_matches_current_spool()`
fresh on every call and pauses/resumes purely from that, with nothing
persisted across polls. `_check_hh_cleared()` is deleted outright rather than
reimplemented statelessly, because an unconditional version of its comparison
reintroduces the exact dispatch-loop race its `_hh_confirmed_spool` guard used
to prevent (NFC dispatches spool 49 → Happy Hare hasn't processed the macro
yet → next poll sees stale gate map → resets local cache → re-dispatches →
loop).

**Regression from this, closed by item 2:** with the reset-on-transition
behavior gone, a lane's local `GateState` stopped self-healing when Happy
Hare's gate map was cleared out-of-band while the tag stayed physically
present — the UID wouldn't change, so `GateState.process_read()`'s dedup
would see "no change" and never re-dispatch. Fixed not by restoring dedup,
but by giving Happy Hare a direct way to reset that dedup memory at the one
moment it actually needs resetting (unload) — see item 2.

**Note on `GateState`/dedup itself:** considered and rejected removing
`process_read()`'s `current_uid`/`current_spool` comparison entirely
("always dispatch, let Happy Hare's write be idempotent"). That's overkill —
it's not a cached opinion of Happy Hare's state (nothing here mirrors
`gate_maps`), it's NFC's own record of what it already reacted to, and the
shared reader's continuous polling genuinely needs it to avoid re-triggering
LED effects/pending-spool staging every poll tick for an unchanged tag. The
actual bug wasn't "NFC compares against its last read" — it was "nothing
ever tells NFC its last read is now stale." Item 2 fixes that directly.

## 1. Done: `_poll_hh_pause_check()` shared-reader guard

The original framing here ("`_poll_hh_pause_check()`/
`_hh_gate_matches_current_spool()` are unreachable dead code for per-lane —
their timer is never armed") was wrong and got corrected mid-implementation:
per-lane gates *do* reach `_poll()` — via the manual `NFC GATE=<#> POLL=1`
command, which calls `gate._poll()` directly, bypassing the timer entirely.
So this logic is live and needed for per-lane, not dead code.

What *was* real: both functions ran unconditionally for the shared reader
too, even though `_gate_snapshot()` treats the shared reader's sentinel gate
(`_SHARED_GATE_SENTINEL = 255`) as out-of-range and returns
`spool=-1, status=GATE_EMPTY` — meaning `hh.available` and
`hh.spool == nfc_spool` can never be true for it. Two wasted
`mmu.gate_maps` reads every shared poll, for a condition that can't happen.
Fixed with `if self._scan_mode or self._shared: return False` at the top of
`_poll_hh_pause_check()`.

`_read_hh_status()`/`_gate_snapshot()` themselves were never removed, and
shouldn't be — they're the correct, actively-used mechanism for every
legitimate direct-query call site in this package (`_startup_check_unknown_gate()`,
`manual_jog_scan()`'s busy check, `get_active_gate()`,
`_read_hh_status_for_gate()`'s left-neighbor checks, etc.). "Query Happy
Hare directly" was always the target, not "stop querying Happy Hare."

## 2. Done: post-unload push, `NFC GATE=<#> UNLOADED=1`

Added `_NFC_GATE_UNLOADED` (`config/macros/mmu_nfc.cfg`), the symmetric
counterpart to `_NFC_SCAN_JOG_PRELOAD` — wire it into
`variable_user_post_unload_extension` the same way `_NFC_SCAN_JOG_PRELOAD` is
wired into `variable_user_post_preload_extension`. It resolves the gate from
`printer.mmu.gate` (no GATE param — `_MMU_POST_UNLOAD` invokes the extension
hook without one) and calls `NFC GATE=<n> UNLOADED=1`, which routes to
`NFCGate._handle_hh_unload()` (`manager.py`). That method calls
`GateState.reset()` (already existed, wasn't previously wired to anything
push-driven) plus the same Spoolman-cache/reader-card clearing
`_clear_spool_cache()` does. Not a status notification — Happy Hare isn't
being told anything new — it's purely resetting NFC's own dedup memory so the
next read of that gate is treated as new.

This only covers the Happy-Hare-initiated unload case, intentionally. A spool
pulled out of a gate *without* going through Happy Hare's sequence (no
`_MMU_POST_UNLOAD` call at all) still relies on the removal path's existing
miss-count-debounce (`GateState.process_read(uid_hex=None, ...)`) — that's a
genuinely different case (NFC is the only thing watching, so NFC has to be
the one to speak up) and is unaffected by this change.

## 3. Done: `_NFC_SPOOL_CHANGED` and friends retired

Resolved as: NFC writes Happy Hare's gate map directly by calling Happy
Hare's own `MMU_GATE_MAP`/`MMU_SPOOLMAN` commands from Python
(`gcode.run_script_from_command(...)`), not by reimplementing
`mmu.gate_maps.assign_spool_id()`/`set_gate_status()` logic bare. The literal
"call `mmu.gate_maps` directly" wording in the original version of this item
turned out to be the wrong target once `MMU_GATE_MAP`'s real implementation
was read: it does color validation (`MmuColorUtils.validate_color`),
`spoolman_support == pull`-mode branching, and its `persist_gate_map()` call
drives LED updates and webhook notification. Reimplementing that in NFC's own
package would duplicate — and risk silently drifting from — logic Happy Hare
already owns correctly. Calling `MMU_GATE_MAP`/`MMU_SPOOLMAN` directly still
delivers the actual goal (one hop instead of two, nothing left for a user to
desync by re-customizing a macro), without that duplication risk.

Retired entirely (deleted from `config/macros/mmu_nfc.cfg`): `_NFC_SPOOL_CHANGED`,
`_NFC_SPOOL_REMOVED`, `_NFC_TAG_NO_SPOOL`, `_NFC_GATE_CLEAR_CACHE`,
`_NFC_SCAN_UNRESOLVED`. All five did nothing beyond formatting a console
message and forwarding fixed params to `MMU_GATE_MAP`/`MMU_SPOOLMAN` — now
done directly in `KlipperInterface._update_gate_map()` (`klipper_interface.py`,
replacing the old `_run_gcode()`) and in `scan_jog.py`'s `clear_hh_gate_cache()`/
`clear_unresolved_scan()`.

Also discovered while reading `mmu_gate_map.py`'s real implementation and
faithfully porting the macros' behavior:
- The `SYNC=1`/`APPLY=1` params the macros passed to `MMU_GATE_MAP` were never
  read by it at all (`mmu_gate_map.py` never references either). The macros'
  second `MMU_GATE_MAP GATE={gate} APPLY=1` calls were no-op re-writes of the
  same values already set by the first call. Dropped, not ported.
- `SCAN_FINISH=1`, threaded from `scan_jog.py` through
  `KlipperInterface.dispatch()`, was documented in the macros as "accepted for
  compatibility" and never actually read by any macro body. Removed end to end
  (`dispatch()`, `_poll_klipper_dispatch()`, both `scan_jog.py` call sites) —
  the compatibility concern it existed for no longer applies once the macro
  layer it was compatible *with* is gone.
- `_NFC_SPOOL_REMOVED`'s `mmu_action` busy-guard (skip clearing the gate map
  while Happy Hare is mid load/unload/homing, so transient motion-noise
  absence isn't mistaken for a real removal) was ported faithfully into
  `klipper_interface.py` as `_BUSY_ACTIONS_IGNORE_REMOVAL`, matching exactly
  which actions the original Jinja substring match caught — including that
  `ACTION_UNLOADING_EXTRUDER` (label "Exiting Ext") was *not* caught by the
  original "load"/"unload"/"homing" substrings, which looks like it may have
  been an oversight. Preserved as-is rather than "fixed," to avoid changing
  behavior as a side effect of an unrelated refactor.

## 4. Done: diagnostic warning for inert per-lane config knobs

Of the four knobs originally listed here, only three are genuinely inert for
per-lane: `poll_interval`, `startup_polling`, `startup_poll_delay`. All three
only matter for a gate whose `_poll_timer` is auto-armed by a background
timer, and that's the shared reader only (`_handle_connect()` arms it inside
an explicit `if ... self._shared ...` block) — a per-lane gate's `_poll()`
only ever runs via scan-jog motion or manual `POLL=1`, neither of which
consults these.

`absent_threshold` is *not* inert for per-lane, contrary to the original
framing here — it's read unconditionally inside `GateState.process_read()`'s
miss-count debounce regardless of shared/per-lane, and per-lane's `_poll()`
is reachable (see item 1), so it does take effect across repeated manual
`POLL=1` calls. Left out of the warning.

Implemented in `NFCGate.__init__`'s per-lane branch: `_add_diagnostic_warning()`
fires if `config.fileconfig.has_option(config.get_name(), key)` for any of
the three inert keys — i.e. only when explicitly present in that gate's own
config section, not just inherited from a default.

## 5. Done: dead-code cleanup — subsumed by item 1

Nothing left to remove once item 1 landed. The original premise (per-lane
callers were dead) was wrong; the shared-reader dead-weight was fixed with a
guard, not a deletion.

## 6. Done: Moonraker proxy edge case

`MoonrakerSpoolmanTransport.request()` (`spoolman_client.py`) now wraps
`urlopen()` in `try/except HTTPError` and sets `._body_text` from the raw
response body on a Moonraker-level rejection of the proxy request itself
(distinct from the 200-wrapped Spoolman-side error branch, which already had
`._body_text`), matching what every other `HTTPError` in this package already
carries.

## 7. Done: `POLL=1` reports skips accurately

`gate._poll()`'s return value already distinguished a paused/skipped read
(`None`, from `_poll_hh_pause_check()` short-circuiting) from an actual read
attempt (`True`/`False`) — `mmu_nfc.py`'s `POLL=1` handler just wasn't using
it, so it always claimed "one poll complete" even when nothing was actually
read because Happy Hare already shows the gate loaded. Now reports "one poll
skipped; Happy Hare already shows this gate loaded" in that case instead.
