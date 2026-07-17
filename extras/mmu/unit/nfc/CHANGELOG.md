# NFC subsystem changelog

Changes to the Happy Hare NFC/RFID reader subsystem on the `rfid` branch.

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
