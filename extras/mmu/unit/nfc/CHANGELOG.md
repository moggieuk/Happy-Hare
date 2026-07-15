# NFC subsystem changelog

Changes to the Happy Hare NFC/RFID reader subsystem on the `rfid` branch.

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
