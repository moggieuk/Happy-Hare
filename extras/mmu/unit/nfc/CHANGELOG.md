# NFC subsystem changelog

Changes to the Happy Hare NFC/RFID reader subsystem on the `rfid` branch.

## `PARAM_NFC_SCAN_ENABLED` (scan-jog) now defaults on for per-lane readers (2026-07-18)

`installer/Kconfig.nfc_reader`: `PARAM_NFC_SCAN_ENABLED` now defaults to `y`
whenever `MMU_HAS_PER_GATE_NFC_READERS` (was unconditionally `n`). Shared-only
topology is unaffected (stays `n` — scan-jog doesn't apply there).

Reasoning, established over the preceding "should POLL/APPLY move to debug"
discussion: NFC tags are spool-mounted, never filament-mounted. A spool goes
into a gate at an arbitrary rotation, so a narrow-field reader only sees the
tag if scan-jog rotates the spool (via filament tension) until the tag
sweeps through the reader's detection window. A stationary `NFC GATE=<n>
POLL=1` read only succeeds if the tag happens to already face the reader —
not a reliable detection method for the default hardware. Leaving scan-jog
off by default left a base per-lane install with no reliable automatic
detection at all; `POLL`/`APPLY` were reclassified as debug-only tools in
the same pass (see `extras/mmu/commands/mmu_nfc.py`'s `HELP_PARAMS`), so
defaulting scan-jog on makes the base install's *supported* detection path
actually on by default.

Verified via `kconfiglib` (the project's patched copy) across all three
topologies: per-lane → `y`, hybrid → `y`, shared-only → `n`.

## Fix: partial per-lane coverage in hybrid topology no longer hard errors (2026-07-18)

Flagged in the 2026-07-17 auto-wire entry as a "known gap, out of scope" —
correctly pushed back on as an unacceptable regression instead. Auto-wiring
`_NFC_SCAN_JOG_PRELOAD` (via `_NFC_POST_PRELOAD` for hybrid topology) turned
a rare manual-misconfiguration edge case into an automatic hard error on
every single preload for any gate lacking its own per-lane reader.

The earlier claim that no non-raising existence check was available was
wrong. Native per-lane `NFCGate` instances are registered under a
predictable, Jinja-addressable printer object name —
`mmu_unit.printer.add_object('nfc_gate lane%d' % gate, lane)`
(`extras/mmu/unit/mmu_nfc.py:49`) — parallel to the shared reader's
`nfc_gate shared`. Missed on the first pass because the search only covered
`manager.py`, not the native integration glue in `mmu_nfc.py`.

- **`_NFC_SCAN_JOG_PRELOAD`** and **`_NFC_GATE_UNLOADED`**
  (`config/macros/mmu_nfc.cfg`) now check
  `printer['nfc_gate lane' ~ gate] is defined` before calling into `NFC` for
  that gate, and skip quietly (`action_respond_info`, not
  `action_raise_error`) when no per-lane reader is configured there —
  "no reader on this gate" is a legitimate, expected config (hybrid partial
  coverage, or a gate deliberately left off `PARAM_NFC_READER_GATE_$(i)`),
  not a fault condition.
- `printer[...] is defined` for an optional object is already the
  established idiom in this codebase for exactly this check —
  `mmu_form_tip.cfg`, `blobifier.cfg`, and `mmu_cut_tip.cfg` all use it the
  same way.
- `_NFC_POST_PRELOAD` needed no changes — it just calls
  `_NFC_SCAN_JOG_PRELOAD`, so it inherits the fix automatically.

## scan_jog.start() self-heals _poll_enabled — unload push mostly unneeded (2026-07-18)

Follow-on to reverting the post-unload auto-wire (below): once that couldn't
be relied on to be wired, the real question was whether it was needed at
all. It mostly isn't. `_poll_hh_pause_check()` checks `self._scan_mode`
before `_poll_enabled` (`manager.py`), so `_poll_enabled` is irrelevant
while a scan-jog is running, and `scan_jog.start()` — which runs at the top
of every scan-jog attempt, already reached via the auto-wired preload hook —
already force-resets `GateState.current_uid`/`current_spool` before
searching. So for any gate using scan-jog, every load attempt self-heals
regardless of whether the prior unload was ever signalled to NFC.

- **`scan_jog.start()`** now also resets `gate._poll_enabled = True`
  alongside its existing `GateState` reset. Closes the one gap that existed
  without it: unload not signalled → a scan-jog that finds nothing (empty
  gate) → `_scan_mode` returns to `False` → `_poll_enabled` stuck `False`
  from the previous load → a manual `POLL=1` on the now-empty gate wrongly
  reports "already reported, skipping."
- **`_NFC_GATE_UNLOADED` narrowed to a real but small audience**: per-lane
  readers running *without* scan-jog (pure manual `POLL=1`/`APPLY=1`, no
  automatic detection at all). Nothing else ever resets that gate's state
  between manual assignments, so those setups still need it wired by hand.
  `config/macros/mmu_nfc.cfg`'s header comment updated to say this plainly
  rather than presenting it as something every per-lane install needs.
- Net result: no combining-macro mechanism built for cutter-macro
  compatibility, because the common case (scan-jog) doesn't need this hook
  wired at all anymore, regardless of what else is already on
  `variable_user_post_unload_extension`.

## Revert auto-wiring the post-unload hook (2026-07-18)

`variable_user_post_unload_extension` is a real, already-claimed slot in
practice, not just a theoretical collision — `mmu_controller.py`'s
`has_mmu_cutter` detection (`'cut' in user_post_unload_extension`) exists
because MMU-cutter integrations like `EREC_CUTTER_ACTION` already use it.
The 2026-07-17 auto-wire would have silently overridden that for anyone
enabling NFC on a setup that already had a cutter macro wired there.

- `installer/macro_vars/Kconfig.sequence`: `VAR_SEQUENCE_USER_POST_UNLOAD_EXTENSION`
  back to a plain `default ""` for every topology — no conditional default
  based on `MMU_HAS_PER_GATE_NFC_READERS` anymore.
- `config/macros/mmu_nfc.cfg`: header comment updated — `_NFC_GATE_UNLOADED`
  documented as manual-wire-only again, same as before the 2026-07-17 phase,
  with an explicit note to call it *alongside* an existing macro rather than
  replace one if that slot is already in use.
- The preload side (`variable_user_post_preload_extension` →
  `_NFC_SCAN_JOG_PRELOAD`/`_NFC_SHARED_PRELOAD`/`_NFC_POST_PRELOAD`) is
  unaffected and stays auto-wired — no known collision was ever found there.
- `_NFC_GATE_UNLOADED` itself, and everything it does
  (`NFCGate._handle_hh_unload()` — `GateState.reset()` plus re-enabling
  `_poll_enabled`), is unchanged and still fully functional once wired in.
  The only thing reverted is the installer silently wiring it for you.
- **Consequence:** the self-managed poll-pause work from the same day (see
  below) re-enables per-lane polling on unload via this exact hook. A user
  who doesn't wire `_NFC_GATE_UNLOADED` in manually now gets per-lane polling
  that stays disabled after a gate's first load for the rest of the Klipper
  session, with no warning that this is happening. Not fixed in this pass —
  see `NEXT_STEPS.md` item A.

## Self-managed poll pause; no more querying Happy Hare to poll (2026-07-17)

`_poll_hh_pause_check()` (`manager.py`) no longer calls `_read_hh_status()`.
Previously it queried Happy Hare's gate map fresh on every poll to decide
whether to skip reading the tag. Replaced with a plain `self._poll_enabled`
flag that NFC manages itself:

- `_poll_klipper_dispatch()` sets it `False` immediately after dispatching a
  `CHANGED`/`UID_ONLY` event — NFC already knows firsthand it just told
  Happy Hare this gate is available, no need to query back and confirm its
  own report.
- Set back `True` on a `REMOVED` event, or by `_handle_hh_unload()`
  (`NFC GATE=<n> UNLOADED=1`, the existing post-unload push) — unload is the
  one direction that has to stay push-driven, since nothing else tells NFC
  Happy Hare unloaded a gate while its polling for it is off.
- `_hh_gate_matches_current_spool()` deleted — its only job was re-deriving
  the same answer via another `_read_hh_status()` call.
- Added `NFC GATE=<n> POLL_ENABLE=1`/`POLL_DISABLE=1` as a manual debugging
  override on top of the automatic management.

Originally planned as a new Happy-Hare-side push (`post_load` hook calling
`NFC GATE=<n> POLL_DISABLE=1`), symmetric with the existing unload push.
Dropped that plan after finding `variable_user_post_load_extension` is
already a claimed extension slot — it's what `mmu_controller.py`'s legacy
Blobifier detection reads (`self.has_blobifier`), and the macro's own comment
names it as the intended purging-macro hookpoint. The self-managed approach
needs no new hook and has no collision risk.

## Auto-wire post-preload/post-unload hooks at install time (2026-07-17)

Previously the NFC preload/unload hooks (`_NFC_SCAN_JOG_PRELOAD`,
`_NFC_SHARED_PRELOAD`, `_NFC_GATE_UNLOADED`) worked correctly once wired, but
wiring them required a user to hand-edit `mmu_macro_vars.cfg` themselves —
`variable_user_post_preload_extension`/`variable_user_post_unload_extension`
had no Kconfig default pointing at them. An addon that needs a manual
post-install config edit to activate doesn't behave like an installed addon
(c.f. Blobifier, which Happy Hare at least string-detects); this closes that
gap for NFC.

- **`installer/macro_vars/Kconfig.sequence`**: `VAR_SEQUENCE_USER_POST_PRELOAD_EXTENSION`
  and `VAR_SEQUENCE_USER_POST_UNLOAD_EXTENSION` now default based on which NFC
  topology is selected (`MMU_HAS_PER_GATE_NFC_READERS`/`MMU_HAS_SHARED_NFC_READER`,
  both from `Kconfig.nfc_reader`) — per-lane-only gets `_NFC_SCAN_JOG_PRELOAD`,
  shared-only gets `_NFC_SHARED_PRELOAD`, hybrid gets the new `_NFC_POST_PRELOAD`
  (calls both), and post-unload gets `_NFC_GATE_UNLOADED` whenever per-lane
  readers exist. No NFC configured keeps the prior empty default. Verified via
  `kconfiglib` directly (the project's own patched copy in `installer/lib/kconfiglib`,
  not the pip package — that one doesn't understand this project's
  `array_editor` extension and fails to parse `installer/Kconfig`) that all
  three topology selections resolve to the correct quoted macro name, and via
  a standalone `jinja2.Environment` with the project's `[[ ]]` delimiters that
  the rendered `mmu_macro_vars.cfg` line is a valid Python string literal.
- **`_NFC_POST_PRELOAD`** (`config/macros/mmu_nfc.cfg`, new): calls
  `_NFC_SHARED_PRELOAD` then `_NFC_SCAN_JOG_PRELOAD` unconditionally. Only
  ever wired in for hybrid topology, where both are expected to exist, so
  no runtime detection needed for the common case. Documented caveat: a
  hybrid setup with only *some* gates carrying their own per-lane reader
  (`PARAM_NFC_READER_GATE_$(i)` enabled per-gate) will still hit
  `_NFC_SCAN_JOG_PRELOAD`'s existing hard error via `NfcMixin._lane()` for a
  gate without one — same as it would if wired manually today. No generic
  non-raising "does this gate have a reader" query exists yet to fix that
  properly; flagged rather than worked around.
- Kconfig string defaults that resolve to a macro *name* need the quote
  characters baked into the Kconfig value itself (e.g.
  `default "'_NFC_GATE_UNLOADED'"`, not `default "_NFC_GATE_UNLOADED"`) —
  the `[[X]]` template substitution in `mmu_macro_vars.cfg` is plain Jinja
  interpolation with no auto-quoting, confirmed against the existing
  `VAR_SEQUENCE_ENABLE_PARK_PRINTING` convention.

## Gate-map writes go direct; NEXT_STEPS.md items 1-7 closed (2026-07-17)

Closes out the `NEXT_STEPS.md` list from the same day's earlier entry. Full
reasoning for each item lives there; this is the shipped-code summary.

- **`_NFC_SPOOL_CHANGED` / `_NFC_SPOOL_REMOVED` / `_NFC_TAG_NO_SPOOL` /
  `_NFC_GATE_CLEAR_CACHE` / `_NFC_SCAN_UNRESOLVED` retired.** Deleted from
  `config/macros/mmu_nfc.cfg`. `KlipperInterface._update_gate_map()`
  (`klipper_interface.py`, replacing `_run_gcode()`) and `scan_jog.py`'s
  `clear_hh_gate_cache()`/`clear_unresolved_scan()` now call Happy Hare's own
  `MMU_GATE_MAP`/`MMU_SPOOLMAN` commands directly
  (`gcode.run_script_from_command(...)`) instead of through a layer of
  NFC-specific macros that only formatted a console message and forwarded
  fixed params. Reuses Happy Hare's real command rather than poking
  `mmu.gate_maps` bare, since `MMU_GATE_MAP` owns real logic (color
  validation, `spoolman_support=pull`-mode branching, `persist_gate_map()`'s
  LED/webhook side effects) not worth duplicating in NFC's package.
- **Two dead params discovered and dropped while porting the macros
  faithfully**: `SYNC=1`/`APPLY=1` on `MMU_GATE_MAP` were never read by it —
  the macros' second `... APPLY=1` calls were no-op re-writes; and
  `SCAN_FINISH=1`, threaded from `scan_jog.py` through
  `KlipperInterface.dispatch()`, was documented "accepted for compatibility"
  and never read by any macro body. Both removed end to end.
- **`_poll_hh_pause_check()` shared-reader guard.** Was running unconditionally
  for the shared reader despite being a structural no-op there —
  `_gate_snapshot()`'s sentinel-gate handling means `hh.available`/
  `hh.spool == nfc_spool` can never be true for it. Added
  `if self._scan_mode or self._shared: return False`. Also corrected the
  premise that this logic was dead for per-lane — it's reachable via manual
  `NFC GATE=<#> POLL=1`, which calls `_poll()` directly.
- **Diagnostic warning for inert per-lane knobs.** `poll_interval`,
  `startup_polling`, `startup_poll_delay` only affect a gate whose
  `_poll_timer` is auto-armed by a background timer (shared reader only).
  `NFCGate.__init__` now warns via `_add_diagnostic_warning()` if any is
  explicitly present in a per-lane gate's own config section
  (`config.fileconfig.has_option(...)`, so inherited defaults don't
  false-positive). `absent_threshold` was also on the original inert list but
  isn't actually inert — `GateState.process_read()`'s miss-count debounce
  reads it regardless of shared/per-lane — so it's excluded from the warning.
- **`POLL=1` reports skips accurately.** `_poll()` already returned `None`
  when `_poll_hh_pause_check()` short-circuited the read vs. `True`/`False`
  for an actual attempt; `mmu_nfc.py`'s handler wasn't using that distinction
  and always said "one poll complete" even when nothing was read. Now says
  "one poll skipped; Happy Hare already shows this gate loaded" instead.
- **Moonraker proxy edge case fixed.** `MoonrakerSpoolmanTransport.request()`
  now sets `._body_text` on a Moonraker-level proxy rejection too (previously
  only the 200-wrapped Spoolman-side error branch got it).

## Removed cached opinions of Happy Hare's state from the gate (2026-07-17)

Continuation of the "gate holds no cache of its own" work from 2026-07-16 (see
`NEXT_STEPS.md` item 0 for the full writeup). Target: any Happy Hare status
the gate needs should be queried directly (`_read_hh_status()`), never stored
across polls.

- **`_hh_confirmed_spool` and `_check_hh_cleared()` removed** (`manager.py`).
  This pair existed to detect Happy Hare's gate map being cleared externally
  and reset the lane's local cache accordingly, guarded so it wouldn't
  self-trigger on the normal async delay between NFC dispatching a spool and
  Happy Hare's macro processing it. Removed rather than reimplemented
  statelessly — an unconditional version reintroduces the exact dispatch-loop
  race the guard existed to prevent. Known consequence: a lane no longer
  self-heals if Happy Hare's gate map is cleared out-of-band while the tag
  stays physically present (see `NEXT_STEPS.md` item 0/2).
- **`_hh_load_paused` removed** (`manager.py` and `scan_jog.py`, same
  attribute written from both files). `_poll_hh_pause_check()` is now
  stateless — it queries `_read_hh_status()`/`_hh_gate_matches_current_spool()`
  fresh every call and returns pause/resume from that alone, with nothing
  persisted between polls. The three call sites in `scan_jog.py` that wrote
  this attribute (scan start, post-dispatch, `rewind_and_exit()`) had it
  removed; `rewind_and_exit()`'s independent re-derivation of the same
  "does Happy Hare already show this spool loaded" check (via its own
  `_read_hh_status()` call) went with it.
- **`_suppress_next_dispatch_uid` / `_suppress_next_dispatch_spool` removed**
  (`manager.py`). Turned out to be write-only — set in `_clear_spool_cache()`
  but never read anywhere in the package — so this was pure dead-code removal,
  not a behavior change.

## Installer/parameter cleanup: dead `log_file` option, dead code (2026-07-17)

Audit of the NFC installer chain (`installer/Kconfig.nfc_reader` →
`config/base/mmu_parameters.cfg` → `ParamSpec` list in
`extras/mmu/unit/mmu_unit_parameters.py`) plus a pyflakes pass over the NFC
package, prompted by a check on whether the installer and its config
variables had actually been cleaned up after prior refactors.

- **`PARAM_NFC_LOG_FILE` / `log_file` removed.** The "Logging approach" work
  below (2026-01) already retired the standalone `nfc_reader.log` file in
  favour of routing everything through the shared `mmu.log`, but the Kconfig
  prompt ("NFC log file"), its line in `mmu_parameters.cfg`, and its
  `ParamSpec` in `mmu_unit_parameters.py` were never removed — the installer
  kept asking users to name a log file that nothing wrote to. Removed from all
  three places. The independent NFC verbosity levels (3=state, 4=trace) are
  unaffected — that detail still lands in `mmu.log`, gated separately from
  Happy Hare's own `debug`/`log_level`, just without a second file.
- **`manager.py` `NameError` fixed.** The `except` around NFC logging setup
  referenced a bare `log_file` name that was never defined in that scope — a
  leftover from before the file-logging removal. Would have raised
  `NameError` (masking the real exception) if `configure()` ever failed.
- **Dead code removed from the NFC package**, found via pyflakes:
  `resolve_moonraker_url` (unused import, `manager.py`),
  `DIRECT_METADATA_SPOOL` (unused import, `klipper_interface.py`),
  `info_both` (unused import, `scan_jog.py`), a copy-pasted
  `gcode = gate.printer.lookup_object('gcode')` in `run_rewind()`
  (`scan_jog.py`) that was never used — `run_hh_script()`, called right after,
  does its own lookup — `mode_tech = payload[2]` in
  `pn7160_driver.py`'s `select_discovered_endpoint()`, and
  `last_action = self._shared_last_action or ''` in `shared_reader.py`'s
  `_shared_next_action()`, both extracted but never consulted. Renamed an
  unused unpacked byte in `tag_parser.py`'s Bambu color parsing (`a2` →
  `_a2`) to match the file's existing underscore convention for
  intentionally-discarded values.
- **Two related installer-script bugs** (not NFC-specific, but found during
  the same audit): `installer/parser.py`'s `HHConfig.sections()` computed its
  return value twice via an identical, redundant call before returning the
  second one; `installer/build.py`'s `check_version()` loaded a `Kconfig` via
  `load_parsed_kconfig()` and discarded the result — kept as a bare call since
  the function has a real side effect (`exit(1)` on a corrupt pickle) that
  acts as an implicit validation gate, but the unused binding and the
  never-called `get_config_version()` helper were removed.

## Moonraker-routed Spoolman, and stripping NFC's own gate-map cache (2026-07-16)

Two connected pieces of work: (1) Spoolman connectivity now goes exclusively
through Moonraker's proxy instead of dialing Spoolman directly, gated on Happy
Hare's own `spoolman_support`; (2) began removing every place NFC kept a
second, locally-cached opinion of a gate's spool/UID/poll state that had to be
reconciled against Happy Hare's gate map, since the gate is meant to be a
utility that pushes reads into the gate map, not a second source of truth for
it. See `NEXT_STEPS.md` for what's left of (2).

### Spoolman via Moonraker

- **`MoonrakerSpoolmanTransport`** (`spoolman_client.py`, new): synchronous
  transport that POSTs to Moonraker's `/server/spoolman/proxy`
  (`use_v2_response=True`) instead of opening a connection to Spoolman
  directly. Normalises `/api/v1/...` paths to Moonraker's expected `/v1/...`
  and raises a real `urllib.error.HTTPError` (with `._body_text` set) on a
  Spoolman-side failure, so every existing `except HTTPError` call site needed
  no changes. Verified live against a running Moonraker instance: the success
  and Spoolman-error response shapes match exactly
  (`{"result": {"response": ..., "error": ...}}`, HTTP 200 even on a Spoolman
  404/400); a genuinely malformed proxy request gets a different,
  non-`result`-wrapped Moonraker-level error at a real non-200 status — noted
  as a known edge in `NEXT_STEPS.md`.
- **`SpoolmanClient`** (`spoolman_client.py`) rewritten to build one
  `MoonrakerSpoolmanTransport` and route `_fetch_spools`/`_fetch_spool_detail`/
  `_patch_spool` through it. Dropped the now-meaningless `base_url` constructor
  argument and the URL-discovery machinery it used to need
  (`_discover_base_url_from_moonraker`, `_resolve_base_url`).
- **`spooltag_decode.py` → `spoolman_catalog.py`**, class `SpoolmanClient` →
  `SpoolmanCatalogClient`. The old name/class collided with
  `spoolman_client.SpoolmanClient`, forcing an `as LBSpoolmanClient` alias at
  the one call site (`tag_handler.py`) — gone now that the name doesn't
  collide. `_req()` now delegates to the shared transport instead of building
  its own curl-equivalent/urlopen call, shrinking from ~70 lines to a thin
  wrapper. The one file/class rename before this (`lameandboard_spoolman.py`)
  also had a leftover console string, "auto-create via lameandboard client",
  fixed to "auto-create via Spoolman client".
- **`spoolman_enable` removed.** It was a second, independent on/off switch
  that never referenced Happy Hare's own `spoolman_support` ([mmu] section) —
  you could enable NFC lookups while `spoolman_support: off`, or vice versa.
  Spoolman is now enabled for NFC purely by `spoolman_support != off`, read via
  `NFCGate._resolve_spoolman()`, called from `_handle_connect()` (`mmu` isn't
  guaranteed loaded at raw config-parse time, so resolution is deferred to
  `klippy:connect`, same pattern as the existing `_get_mmu()` helper).
  `installer/Kconfig.nfc_reader`, `config/base/mmu_parameters.cfg`, and
  `extras/mmu/unit/mmu_unit_parameters.py` updated to match — the "IP address /
  Auto / Disabled" `CHOICE_NFC_SPOOLMAN_URL` menu is gone along with it, since
  a direct-dial address is no longer a supported connection mode.
- **`DEFAULT_MOONRAKER_URL`** (`spoolman_client.py`) is now the single
  definition of `http://127.0.0.1:7125`, replacing three duplicated literals
  across `manager.py`.

### NFC's own cache and background polling

- **Consolidated duplicated NDEF TLV parsing.** `tag_handler.py` had its own
  hand-rolled `_find_ndef_tlv`/`_decode_ndef_text_records`, nearly identical to
  `tag_parser.py`'s versions, existing only to support a mid-scan debug
  preview that tolerates a truncated read. `tag_parser._find_ndef_tlv` gained
  an opt-in `return_details=True` mode (dict + partial-tolerant, matching
  tag_handler's old behaviour exactly); tag_handler's copies are now thin
  wrappers delegating to it.
- **`_hh_seed_spool_id` / `_hh_seed_available` / `_seed_cache_from_hh()` /
  `_hh_sync()` / `HH_SYNC=1` / `NFC_HH_SYNC_CACHE` removed entirely** —
  a startup/manual re-seed mechanism that read Happy Hare's gate map through a
  gcode-macro round-trip (`printer.mmu.gate_spool_id` in Jinja) to avoid
  redispatching a spool Happy Hare already knew about. Redundant with the
  gate reading Happy Hare's live gate map directly (`_read_hh_status()`/
  `_gate_snapshot()`), which it already does elsewhere. Also removed the
  `_NFC_HH_SYNC_ONE` reference from help text — it had no implementation
  anywhere in the codebase; a stale doc reference, not a real command.
  (Note: this was removed once, incorrectly flagged as dead/unregistered —
  `extras/mmu/commands/mmu_nfc.py`'s `HH_SYNC` branch was live — restored,
  then removed correctly for real once that was confirmed.)
- **Per-lane background poll loop removed.** Traced the full `_poll_timer`/
  `_poll_timer_event`/`_poll()` call graph across `manager.py`, `scan_jog.py`,
  `shared_reader.py`, `shared_preload.py`, and `mmu_nfc_shared.py` to separate
  genuinely per-lane-only logic from logic shared with the shared reader
  (`_poll()` itself is used by both — shared reader's own operation runs
  through it). Confirmed `scan_jog.py` has zero references to `self._shared`
  anywhere and `shared_reader.py` explicitly does not use it, so its poll-timer
  interactions are per-lane-only by construction. Removed: the per-lane "poll
  suppression while Happy Hare has an opinion" block in `_poll_timer_event`
  (gated on `_scan_enabled`, always `False` for shared), `_delayed_init`'s
  per-lane auto-start-at-boot, the per-lane branch of `_set_reading` (now
  shared-only), `scan_jog.py`'s `resume_poll_after_rewind()` and the
  poll-timer re-arm in `abort_scan_on_error()`, and `READ=1`/`READ=0` from the
  per-lane `NFC` command (`extras/mmu/commands/mmu_nfc.py`) — `NFC_SHARED
  READ=1/0` (a separate command class in `mmu_nfc_shared.py`) is untouched.
  Per-lane tag detection is now purely event-driven: Happy Hare's post-preload
  hook (`_NFC_SCAN_JOG_PRELOAD`) triggers scan-jog, which reads and dispatches
  directly — nothing was riding on the background loop for that path.
  Config knobs `poll_interval`/`startup_polling`/`startup_poll_delay`/
  `absent_threshold` were **not** removed — the shared reader still uses all
  four — but are now silently inert if set on a per-lane gate/override; no
  warning is emitted (see `NEXT_STEPS.md`).
- **`NFC_STATUS` simplified to reader health only.** `status_line()` (confirmed
  the only real call path — shared has its own separate `shared_status_line()`
  — the `if self._shared` branch inside the old `status_line()` was dead)
  went from ~60 lines of Happy-Hare gate-map sync-mismatch comparison down to
  three states: disabled / failed / OK. The now-unused `_status_html_words()`
  helper (colored "available"/"empty"/"assigned" in the old output) was
  removed. `get_status()` (the Klipper status API, `printer['nfc_gate laneN']`)
  simplified the same way for per-lane gates. Investigated one real risk
  before finishing: `_NFC_SHARED_PRELOAD` (`config/macros/mmu_nfc.cfg`) reads
  `printer['nfc_gate shared'].pending_spool_id` — traced this to
  `MmuSharedNfcReader(NFCGate)` (`extras/mmu/unit/mmu_nfc.py`), whose
  `get_status()` override called `super().get_status()` then unconditionally
  set the real `pending_spool_id`/etc. values, so nothing was actually broken —
  but consolidated the duplicate logic into the base `get_status()`
  (shared-gated) and removed the now-redundant override in `shared_reader.py`,
  rather than leave two copies of the same field-population logic to drift.

### Verification

- Full `test/nfc/` suite (35 tests) green after every step above.
- Live-verified the Moonraker proxy request/response shapes against a running
  printer (not just source-read), including the Spoolman-error and
  Moonraker-level-error cases.
- Directly exercised `NFCGate.get_status()`/`_resolve_spoolman()` with mocked
  gate objects (no existing test coverage for either) to confirm per-lane vs.
  shared behavior before/after the changes, beyond what the pytest suite
  already covers.

## NFC file/package reorganization (2026-07-15)

Re-homed the standalone NFC subsystem to match Klipper's loading model and Happy
Hare's `mmu_unit` ownership hierarchy, and split the monolithic command surface
into per-command files.

### Final layout

```
extras/mmu_nfc_reader.py            # thin Klipper shim -> re-exports load_config hooks
extras/mmu/unit/mmu_nfc.py          # unit-owned wrapper (imports repointed)
extras/mmu/unit/nfc/                # ALL nfc code lives here
  __init__.py                       #   package doc refreshed
  reader.py                         #   reader chip-object impl (was unit/mmu_nfc_reader.py)
  reader_factory.py  pn532_driver.py  pn7160_driver.py  rc522_driver.py   # chip drivers
  log.py                            #   thin logging shim for the chip drivers
  nfc_logger.py                     #   dedicated gate/manager logger (was mmu/mmu_nfc_log.py)
  manager.py  gate_state.py  klipper_interface.py  lameandboard_spoolman.py
  reader_resolver.py  scan_jog.py  shared_preload.py  shared_reader.py
  spoolman_client.py  tag_handler.py  tag_parser.py     # 12 manager modules, prefix dropped
extras/mmu/commands/mmu_nfc_*.py    # 7 commands + shared NfcMixin
```

### What changed and why

- **Commands split into one-file-per-command** (`NFC`, `NFC_SHARED`, `NFC_STATUS`,
  `NFC_HELP`, `NFC_DOCTOR`, `NFC_REGISTER`, `NFC_LED_TEST`), each a `BaseCommand`
  subclass with its own `HELP_BRIEF`/`HELP_PARAMS`/`HELP_SUPPLEMENT`, sharing a
  `NfcMixin` for reader/spool resolution — mirroring the `MoveMixin` pattern used by
  `mmu_test_move` / `mmu_test_homing_move`.
- **`[mmu_nfc_reader]` now loads natively.** The reader impl registered
  `[mmu_nfc_reader]` sections but lived under `unit/`, where Klipper cannot autoload
  config sections (it only loads `klippy/extras/<section>.py`). Added a thin top-level
  `extras/mmu_nfc_reader.py` that re-exports `load_config` / `load_config_prefix` from
  `unit/nfc/reader.py`.
- **`mmu_rfid_reader` -> `mmu_nfc_reader` rename** in the reader impl (object name,
  class `MmuNfcReader`, `MMU_NFC_*` commands). The section name (per filename) was
  already `mmu_nfc_reader`, so the old internal `lookup_object('mmu_rfid_reader')`
  never matched — a latent defaults-lookup bug, now fixed. No external references
  existed. Human-readable command descriptions ("an RFID reader") left as-is.
- **Manager logic consolidated** under `extras/mmu/unit/nfc/`, dropping the redundant
  `mmu_nfc_` filename prefix; intra-package imports updated accordingly.
- **`[nfc_gate]` retired.** Deleted `extras/nfc_gate.py`. The macro reference
  `printer['nfc_gate shared']` still resolves because the unit wrapper registers that
  object via `add_object('nfc_gate shared', ...)` — only the config *section* was
  removed, not the object key. Migration note: any legacy config carrying literal
  `[nfc_gate ...]` sections will no longer load; the generated template already emits
  `[mmu_nfc_reader ...]`.
- **Makefile** needed no change — `extras/*.py` (shim) and `extras/mmu/unit/nfc/*.py`
  (package) were already in the symlink glob; the removed `extras/nfc_gate.py` drops
  out on its own.

### Logging approach: route through the standard MMU logger

Decision: NFC logging goes through the standard Happy Hare MMU logging layer rather
than the subsystem's own file. All output lands in `mmu.log` (owned and rotated by
`MmuLogger`) and on the Klipper console, like every other MMU component. No separate
`nfc_reader.log`, no second rotating handler (which would have fought `MmuLogger`'s own
`mmu.log` rotation).

- **`log.py` is the single logging module — an MMU-backed adapter.** The 459-line
  standalone file logger was replaced by an adapter whose module-level `logger` keeps the
  usual `.debug/.info/.warning/.error/.exception` surface (call sites unchanged) but
  forwards each record to the active `mmu` object's `log_debug/log_info/log_warning/
  log_error`. Every message is prefixed `NFC`. Removed the `_DateRotatingFileHandler`,
  archive pruning, klippy-forward handler, and gcode-console handler.
- **One logging module.** The interim split (`log.py` driver shim + `nfc_logger.py` gate
  logger) was collapsed: `nfc_logger.py` was renamed to `log.py` (the adapter), and
  `log.py`'s old `info/warning/error` wrapper functions were dropped — `pn532_driver` now
  calls `logger.info/warning/error` directly. All modules and chip drivers import from
  `.log`.
- **Scattered ad-hoc loggers unified.** `lameandboard_spoolman.py` (`rfid.spoolman_client`)
  and `tag_parser.py` (`rfid.tag_parser`) now route through the shared adapter via the same
  `try: from .log import logger except ImportError:` fallback `spoolman_client.py` uses, so
  they log to `mmu.log` in-package and stay usable standalone.
- **MMU discovery.** `configure(printer=...)` (called during NFC init) remembers the
  printer; the `mmu` object is resolved lazily via `printer.lookup_object('mmu')` so
  init ordering doesn't matter. Before it resolves, records fall back to the shared
  `'mmu'` Python logger (which `MmuLogger` wires to `mmu.log`). The log-file location is
  not rediscovered — `MmuLogger` owns it (derived from `printer.start_args['log_file']`).
- **Console color via Happy Hare's marker model.** `color_console_tags` reimplemented with
  the compiled-regex approach from `mmu_logger.py`: fixed `{0}..{6}` tokens + dynamic
  `{{RRGGBB}}`/`{{}}` spans (`re.compile`), NFC bracket tags (`[WARN]`, `[OK]`, …) applied
  via precompiled patterns, and coloring gated on `mmu.p.console_show_colored_text`.
- **`log.py`** remains the minimal driver shim (`logging.getLogger('mmu_nfc_reader')`) for
  the chip drivers — unchanged by this.

### Verification

- All touched files byte-compile; full `compileall extras/` passes.
- Import-graph sweep clean: no orphaned references to old module paths, `nfc_gate`, or
  `mmu_rfid_reader` / `MmuRfid` / `MMU_RFID` identifiers.
- Runtime import validation still requires a Klipper rig (the package pulls in `bus`,
  the reactor, and config objects that `py_compile` does not exercise).

---

## Copy-ready PR description

### Summary

This PR reorganizes the NFC/RFID subsystem under `extras/mmu/unit/nfc` so its
structure matches Happy Hare's MMU unit ownership model. It also fixes native
Klipper loading for `[mmu_nfc_reader]`, separates the NFC G-code commands into
focused command classes, consolidates NFC output into the standard `mmu.log`,
and adds a hardware-independent pytest regression suite.

The reorganization keeps reader hardware, tag parsing, Spoolman integration,
gate state, shared-reader behavior, and scan-jog logic together behind the MMU
unit while leaving only the thin loader required by Klipper at the top level.

### Changes

#### Package and loading

- Moved the NFC manager modules and reader implementation into
  `extras/mmu/unit/nfc`, removing the redundant `mmu_nfc_` filename prefixes.
- Added `extras/mmu_nfc_reader.py` as a thin Klipper entry point that re-exports
  `load_config` and `load_config_prefix` from the unit-owned reader module.
  This allows `[mmu_nfc_reader]` sections to load through Klipper's normal
  extension discovery without moving the implementation back to the top level.
- Renamed the remaining internal `mmu_rfid_reader` object and command
  identifiers to `mmu_nfc_reader`, matching the configuration section users
  already configure and fixing the previous defaults lookup mismatch.

#### Commands and runtime ownership

- Split the NFC G-code surface into focused `BaseCommand` implementations for
  `NFC`, `NFC_SHARED`, `NFC_STATUS`, `NFC_HELP`, `NFC_DOCTOR`, `NFC_REGISTER`,
  and `NFC_LED_TEST`.
- Added a shared `NfcMixin` for lane, shared-reader, and Spoolman resolution so
  command routing is consistent without duplicating lookup logic.
- Kept reader state, tag state, polling, scan-jog behavior, and shared preload
  coordination in the NFC unit rather than in the command layer.

#### Logging and protocol handling

- Replaced the dedicated NFC file logger with an MMU-backed adapter. NFC output
  is now prefixed consistently and written through Happy Hare's existing
  logging infrastructure to `mmu.log`.
- Unified logging across the manager, hardware drivers, tag parser, and
  Spoolman clients while retaining the familiar `logger.debug/info/warning/
  error/exception` interface.

#### Regression tests

- Added pytest coverage for gate-state transitions, macro dispatch, reader
  resolution, scan motion calculations, Spoolman lookup and caching, NDEF and
  tag parsing, protocol helpers, logging behavior, and package imports.
- Stubbed Klipper, Moonraker/Spoolman, and hardware boundaries so the NFC unit
  suite runs without external services, a Klipper checkout, or physical NFC
  readers.
- Added `make test_nfc`; the normal `make test` target now runs the same NFC
  regression suite. `make test_all` remains available for environments with
  the full repository development dependencies.

### Migration note

The obsolete top-level `[nfc_gate]` configuration loader has been removed.
Legacy configurations containing literal `[nfc_gate ...]` sections must migrate
to `[mmu_nfc_reader ...]` sections. This does not remove the runtime
`nfc_gate shared` object, so existing macro references such as
`printer['nfc_gate shared']` continue to work.

### Validation

- 35 NFC pytest tests passed, including 16 subtests.
- NFC modules and tests compile successfully.
- `git diff --check` passes.

Run the NFC regression suite with:

```sh
make test
```
