---
name: kconfig-menuconfig
description: How to modify Happy Hare's Kconfig/menuconfig installer — the Kconfig tree under installer/, the HH-extended kconfiglib fork, the .mmu_config value flow (menuconfig → pickle → Jinja templates → merged .cfg), and the symbol-naming contract (PARAM_/PIN_/BOOL_/MMU_HAS_/CHOICE_/UNSELECT_ and the #~DEFAULT~# modifiable-defaults mechanism). Use this whenever adding, renaming or removing Kconfig symbols, restructuring menuconfig screens, changing defaults, touching installer/lib/kconfiglib or installer/build.py, wiring a config option through to config/ templates, debugging "my option doesn't show in menuconfig / doesn't persist / gets reset / renders wrong in the built .cfg", or dealing with the multi-unit entry-point vs per-unit config shape — even if the request is phrased as a plain config-option change, because in this repo a config option IS a Kconfig symbol.
---

# Kconfig / menuconfig

Happy Hare's machine configuration is a **Kconfig tree** (Linux-kernel style)
that `make menuconfig` turns into a plain `CONFIG_*` value file
(`.mmu_config`, gitignored), which the build then turns into the rendered
Klipper `.cfg` files. Kconfiglib is **vendored and forked** at
`installer/lib/kconfiglib/` — the fork adds HH-specific language features
([references/kconfiglib-extensions.md](references/kconfiglib-extensions.md) is
the full catalog). The block comment at the top of `installer/Kconfig` is the
authoritative in-repo documentation of those extensions — keep it updated when
you change the fork.

## The value flow (one pass, top to bottom)

```
env vars ──(expanded at PARSE time)──▶ installer/Kconfig tree
                                          │ make menuconfig  /  make olddefconfig (stale only)
                                          ▼
                     .mmu_config  (+ .mmu_config_<unit> per unit in multi-unit)
                                          │ python -m installer.build --pre-parse-kconfig
                                          ▼   (KConfig.as_dict() → values + choices)
                     out/.mmu_config.pickle   (regenerated only when .mmu_config is newer)
                                          │ build_config_file(): render_template (Jinja, [[ ]] / [% %])
                                          ▼                              + HHConfig merge of the
                     out/mmu/*.cfg                              user's existing .cfg values
                                          │ install
                                          ▼
                     $(KLIPPER_CONFIG_HOME)/mmu/*.cfg
```

Load-bearing facts about the flow:

- **Env is consumed at parse time, not load time.** kconfiglib expands
  `$(VAR)` and `$(shell, ...)` while `Kconfig()` is being constructed. The
  vars (`UNIT_NAME`, `MCU_NAME`, `UNIT_INDEX`, `F_MULTI_UNIT`,
  `F_MULTI_UNIT_ENTRY_POINT`, `F_PER_GATE_MCU`, `HH_VERSION`,
  `KLIPPER_HOME`, ...) must be in the environment *before* the parse — and in
  multi-unit they differ **per parse**, changing the tree's *shape* (whole
  symbol sets appear/disappear behind `if MULTI_UNIT_ENTRY_POINT`).
  `test/hh/cfg.py::_env()` is the reference implementation: assign (never
  `setdefault`) and restore around each parse.
- **Multi-unit = three parses, driven by `install.sh`**: one entry-point
  parse (`F_MULTI_UNIT_ENTRY_POINT=y F_MULTI_UNIT=y`, `UNIT_NAME="u0,u1,..."`
  becomes the `MMU_UNITS` list) plus one per unit
  (`F_MULTI_UNIT=y UNIT_NAME=uN MCU_NAME=uN UNIT_INDEX=N` plus the
  printer-level `HAS_SENSOR_TOOLHEAD/EXTRUDER/TOOLHEAD_CUTTER` read back out
  of the top-level file). See `install.sh` `run_kconfig_top` /
  `run_kconfig_units` / `run_kconfig_one`. The per-unit config file and the
  installed `.cfg` files both gain a `_<unit>` suffix.
- **`make` itself reads the value file**: the Makefile does
  `-include $(KCONFIG_CONFIG)` (Makefile:47), so any `CONFIG_*` symbol
  (`CONFIG_MULTI_UNIT`, `CONFIG_MMU_UNITS`, `CONFIG_KLIPPER_HOME`, ...)
  steers the build (e.g. `unit_names`). Renaming such a symbol is a Makefile
  change too.
- **Staleness**: `kconfig_sources` (Makefile:244) = every `installer/**/Kconfig*`
  plus `kconfigfunctions.py`, compared by mtime against the value file; when
  stale, `olddefconfig` (never menuconfig) refreshes the file with new
  defaults. New `Kconfig*` files are picked up automatically by the wildcard;
  files with any other name are invisible to this mechanism.
- **User values survive by design**: `olddefconfig` only fills in *new*
  symbols' defaults; explicit user assignments in an existing `.mmu_config`
  are preserved. This is why changing the *default or meaning of an existing*
  symbol is a breaking change for installed machines (see CONTRIBUTING: such
  changes "will probably be rejected").

## The symbol-naming contract

`installer/Kconfig`'s header comment is the spec; the machinery lives in the
kconfiglib fork. Three prefix lists are hard-coded in different places and
**must be kept in sync** when you introduce a new special prefix:

| Prefix | Meaning |
|---|---|
| `PARAM_*` | Parameters of various types that flow into template rendering (the main "real" options) |
| `VAR_*` | Gcode macro variables (land in `gcode_macro` sections via `VAR_SECTION_MAP`) |
| `PIN_*` | String symbols representing pins |
| `BOOL_*` | Booleans, often driving a promptless int PARAM (historical pair; BOOLINT now replaces it) |
| `MMU_HAS_*` | Hardware capability flags (encoder, leds, heaters, sensors, ...) |
| `CHOICE_X` / `CHOICE_X_*` | Named choice "X" and its members |
| `UNSELECT_*` | Force-off switch: a type/board does `select UNSELECT_X` to hide/disable feature X's prompt (`if !UNSELECT_X` in the Kconfig) |

**The special-default behaviour**: symbols (and `CHOICE_`-named choices) whose
name matches the list are written into `.mmu_config` with a trailing
` #~DEFAULT~#` magic token whenever they were *not* user-set (i.e. the saved
value is just the computed default). On the next menuconfig/olddefconfig load
(`load_config(..., filter_defaults=True)`) that assignment is parsed then
*cleared and marked `_was_default`* — so the symbol stays a **modifiable
default**: the user can change it, menuconfig shows
`(NOT DEFAULT)` and the **`r` key resets it** back to the default. Any other
name is a normal Kconfig variable whose saved value is always an explicit
assignment that never falls back to the default.

The three lists (verified anchors, will drift — re-grep):

1. **write side** — which values get the ` #~DEFAULT~#` token:
   `kconfiglib.py:~1762` →
   `('PARAM_', 'VAR_', 'PIN_', 'BOOL_', 'MMU_HAS_', 'CHOICE_', 'UNSELECT_')`
   plus unnamed-check: choices must be *named* `CHOICE_*`.
2. **display side** — which get the `(NOT DEFAULT)` marker (i.e. are
   `r`-resettable in the UI): `menuconfig.py:~3509` →
   `('PARAM_', 'VAR_', 'PIN_', 'BOOL_', 'MMU_HAS_')` for prompted symbols;
   choices by name `CHOICE_*`.
3. **reset side** — what `r` may actually clear:
   `menuconfig._reset_node` → the 7-prefix tuple plus choice-member/sibling
   clearing for `CHOICE_` choices.

Note the three lists are *deliberately not identical* (e.g. `UNSELECT_` gets
a default token but no `(NOT DEFAULT)` marker — it's a hidden switch the user
isn't meant to fiddle with). Don't "simplify" them into one without deciding
what the UI behaviour should be.

**`VAR_*`** (macro variables) and **`PIN_*`** get the token but their *cfg*
landing spot differs: `VAR_*` values are copied into `gcode_macro` sections
per `VAR_SECTION_MAP` in `installer/build.py` (`variable_<name>` options),
not into hardware files.

**Naming trap — array grouping.** `KConfig.as_dict()` collapses any symbol
matching `^(.+_)(\d+)$` with index ≤ 12 into a **list** in the render dict
(`PIN_EJECT_BUTTON_0..11` → `PIN_EJECT_BUTTON: [..]`) so Jinja can index it.
A *single* surviving match is ungrouped again (it's just a name that happens
to end in digits). Choice members are exempt (checked *before* grouping) —
which is exactly what keeps version-numbered names like
`MMU_TYPE_ERCF_1_1` from corrupting the dict. Consequences:

- Don't name two unrelated real symbols `FOO_1`, `FOO_2` unless you *want*
  them rendered as one indexed list.
- A non-choice symbol whose name ends in a small integer is only safe if no
  sibling with the same prefix exists.
- The `> 12` index guard is a warning, not an error — `FOO_13` passes through
  verbatim.

## Tree layout

```
installer/
  Kconfig                 # ROOT. Header = extension docs. Env plumbing,
                          # multi-unit branching (two different menu trees),
                          # source order, osource /tmp/.Kconfig.generated
  Kconfig.<topic>         # one file per feature: name, num_gates,
                          # selector_type, endstops, options, pins, heater,
                          # fans, leds, encoder, espooler, nfc_reader, ...
                          # Convention: "# Sets/Defines parameter tokens:" header
  mmu_types/ (+ starters/)  # rsource "Kconfig.*" — one file per machine type
  boards/ (+ custom/, per_gate/)  # MCU/board selection; pin defaults
  connection/           # MCU serial/CAN auto-discovery ($(shell) heavy)
  servos/  sensors/  toolheads/  macro_vars/
  lib/kconfiglib/       # VENDORED FORK: kconfiglib.py, menuconfig.py,
                        # olddefconfig.py, kconfigfunctions.py,
                        # test_kconfig_pickle_consistency.py
  build.py              # KConfig(as_dict/get/getint/is_enabled/is_selected),
                        # ParsedKConfig pickle, render_template, HHConfig merge,
                        # supplemental_params/hidden_params, VAR_SECTION_MAP
  parser.py             # layout-preserving .cfg parser (ConfigBuilder)
  upgrades.py           # version-upgrade transforms (Upgrades)
```

The root Kconfig builds **two different menus**: `if MULTI_UNIT_ENTRY_POINT`
(shared/printer-level options: MMU_UNITS list, toolhead, options, purging,
speeds, macro vars, shared pins, paths) vs `if !MULTI_UNIT_ENTRY_POINT`
(per-unit: MMU type, board, connection, pins, endstops, ...). Per-unit files
are excluded from the entry-point tree via `if !MULTI_UNIT` guards (e.g.
toolheads/Kconfig appears in *both* — standalone machines keep it per-unit).
Printer-level capabilities are handed down to unit parses as env
(`HAS_SENSOR_TOOLHEAD`, ... → `$(shell)` macros at root `Kconfig:~116`,
feeding promptless `MMU_HAS_*` defaults at `Kconfig:~203`).

## Checklist: adding / changing a symbol

1. **Name it per the contract** above. A symbol that should be a *modifiable
   default* (user-tweakable, `r`-resettable) needs a special prefix **and a
   prompt**. Promptless symbols are invisible in the UI, can't get
   `(NOT DEFAULT)`/`r`, and — critically (pitfall 2) — kconfiglib only honours
   `user_value` for *visible* symbols.
2. **Pick the tree**: shared (entry point, guarded so it's absent from units)
   or per-unit. New topic file → name it `Kconfig.*` so the Makefile
   `kconfig_sources` wildcard sees it (that's what triggers
   `olddefconfig` refresh on existing installs).
3. **Wire it into the menu**: `source` it from `installer/Kconfig` (or a
   parent Kconfig file) at the right position. Use `menu "..."` with `help`
   for grouping, `comment "_"` / `comment "_Heading"` for separators,
   `if !UNSELECT_X` for disable-able features, `@repeat` for per-gate
   expansions (**sparingly** — pitfall 10), `prompt "..." if PARAM_NUM_GATES > $(i)`
   to hide surplus
   gates.
4. **Land it in a .cfg** (if it should): update the Jinja template(s) under
   `config/` — the `.cfg` *option name* is chosen by the template author
   (`num_gates : [[PARAM_NUM_GATES]]`), the Kconfig symbol only supplies the
   value. `VAR_*` → `variable_<name>` in a `gcode_macro` section. A
   parameter that must survive upgrade but isn't in any template → add it to
   `supplemental_params` (documented-but-commented) or `hidden_params`
   (legal-but-unexposed) in `build.py`.
5. **Defaults**: new symbols get their default filled into existing
   (stale) `.mmu_config` files automatically by `olddefconfig` on the next
   `install.sh`/`make` run. *Changing an existing symbol's default or
   semantics* changes what gets rendered for every installed machine —
   additive options over reinterpretation (CONTRIBUTING).
6. **Test it** (all of these run on a laptop, no printer):
   - Add/extend a profile in `test/hh/profiles.py` (a profile *is* a dict of
     Kconfig symbols) and run
     `make test UT=test_mmu_profiles.py` (config breadth) and/or
     `UT=test_mmu_config.py` (rendering + a few direct-kconfiglib tests).
     The harness (`test/hh/cfg.py`) renders the **real** templates, so a
     renamed param or broken `[% if %]` guard shows up as a boot failure.
   - `make console ARGS='--profile boxturtle'` — boots the rendered config in
     the fake-Klipper harness.
   - **Pinning a *default* takes a profile that does NOT set the symbol.**
     Profile `syms` are explicit user values; an unset symbol is what
     exercises the computed default. Register render-only profiles in the
     trailing `PROFILES` tuple (not `CONSOLE_PROFILES`) and assert on the
     rendered text from `cfg.render(profile)` (parse the section; see
     `TestBoxTurtleRender.test_led_effect_defaults_are_preserved`).
   - **The fake Klipper never validates an i2c bus name against a chipdef**
     (`bus.py` just records it). A made-up bus like `i2c3_PC0_PC1` (stock
     STM32F446 i2c3 is PB3/PB4 or PC8/PC9 — the historical MMB 2.0
     default; the name no longer appears in the tree) passes the whole
     harness and dies at real-machine boot — check bus names against the
     target Klipper's chipdef by eye (a real one: `i2c2_PB10_PB11` in
     `boards/Kconfig.tzb_1_0`).
   - `make verify_pickle` — regression check that every explicit
     `CONFIG_*` assignment survives `as_dict()` (see pitfall 2).
   - `make menuconfig` interactively (needs the real env context; normally
     reached via `./install.sh -i`).
7. **If you touched the fork itself** (`as_dict`, load/write, menuconfig):
   the tests for it are `make verify_pickle` (pickle consistency),
   `make test UT=test_menuconfig.py` (menuconfig cursor behaviour), and
   profile tests. The vendored base is kconfiglib **v14.1** — the HH patches
   are marked `# Happy Hare:` inline; if you re-sync upstream, that grep is
   your change list.

## Board-specific defaults (cross-file)

Boards get their own pin/bus defaults by re-declaring the same `config` in
`boards/Kconfig.<board>` with just an added `default` — no prompt, no
repeated help. The nodes merge into one Symbol (upstream Kconfig: same-name
`config` definitions merge; prompts add, defaults add). Gotchas, all bitten
in this repo:

- **A default's condition is evaluated exactly as written.** The parser
  stores only the explicit `default <val> if <cond>` condition
  (kconfiglib.py:3608-3610); the node's *enclosing* `if`/`depends` block is
  NOT auto-ANDed in. Write `default "i2c3_PC0_PC1" if BOARD_TYPE_MMB_2_0`
  even inside an `if BOARD_TYPE_MMB_2_0` block.
- **The earliest-parsed default wins** for strings (`_node_ordered_string_default()`,
  kconfiglib.py:~5613 — also used by the *value-computation* path at :~5010,
  so it decides both the computed value and the min-config write). `boards/Kconfig`
  is sourced at Kconfig:277, *before* `Kconfig.mmu_additions` (:279), so a
  satisfied board default beats a feature file's later `default ""`.
- **`choice` members cannot come from another file.** A board file may steer
  an *existing* choice with `default <CHOICE_MEMBER> if <cond>` only; the
  members themselves must be declared inside the `choice ... endchoice`
  block (per-gate variants in its `@repeat` block). Selection is
  first-satisfied (`Choice._selection_from_defaults`, kconfiglib.py:~6130) —
  put the new board-specific default line *above* the older, more general
  ones.
- **Board type and per-gate MCU are mutually exclusive in the tree.**
  `boards/Kconfig` sources `boards/per_gate/` (EBB Gen1 / SLB) *instead of*
  the normal board set when `MMU_HAS_PER_GATE_MCU` is set — so
  `BOARD_TYPE_MMB_2_0` (and the other normal boards) cannot be selected in a
  per-gate-MCU config. A board file's defaults only ever apply to devices on
  *that board's* MCU: defaulting a per-gate device's bus from the main
  board's Kconfig is either dead code (per-gate-MCU configs) or a hardware
  collision (per-gate devices on one shared MCU can't share a fixed-address
  i2c bus — see the per-gate NFC help in `Kconfig.nfc_reader`).

## Pitfalls (each one has bitten a past session — they're in code comments)

1. **Env at parse time.** Get env wrong and pins silently render as `:PD5`
   (no chip) instead of `unit0:PD5` — wrong output, no error. `cfg.py`'s
   `assert_sane()` catches the chip-less form; only the per-parse
   assign/restore discipline catches the wrong-chip form.
2. **The visibility trap.** kconfiglib discards `Symbol.user_value` (and
   `Choice.user_selection`) when the symbol is *not currently visible*,
   falling back to the computed default. `build.py`'s `KConfig.get/getint/
   is_enabled/is_selected/as_dict` all deliberately read the raw user value
   first — many HH symbols are promptless-by-design and rely on that. If you
   add a new way of reading values, replicate the fallback and run
   `make verify_pickle`; `test_kconfig_pickle_consistency.py` exists because
   this exact class of silent value-dropping has happened several times.
   Related: BOOL `user_value` is an int tristate (0/1/2), not `"n"/"y"` —
   normalize with `TRI_TO_STR`.
3. **`#~DEFAULT~#` is live syntax** inside `.mmu_config`. `saved-config-value`
   (a kconfiglib preprocessor function, `kconfigfunctions.py`, cached by
   mtime+size) and the load path both parse it. Tooling that rewrites value
   files must preserve the token.
4. **`$(shell, ...)` is expensive.** The root Kconfig's serial/CAN discovery
   macros are *parameterized* Make functions, so kconfiglib re-forks a shell
   for **every reference** (~370 in one multi-unit parse; 22s of a 25s boot
   before the harness's per-parse cache in `cfg.py::_install_shell_cache`).
   Adding shell-heavy macros costs every menuconfig run — measure first, and
   note the cache *must* stay scoped to one parse (env changes between
   parses).
5. **Makefile traps**: inline `#` comments pad values with leading spaces
   (Makefile:3 — keep comments on their own line); any *new interactive*
   make goal must be added to the `MAKECMDGOALS` exclusion list
   (Makefile:39) or `--output-sync` buffers its prompt away; the value files
   are `.PRECIOUS` (Makefile:183). `make variables` prints which interpreter
   each half (test vs installer) settled on.
6. **`Kconfig` resolves via `srctree`**: the Makefile exports
   `srctree := $(SRC)/installer`, which is why `make menuconfig Kconfig`
   works from the repo root and why `rsource` (relative-to-file) vs `source`
   (relative to srctree) matters in this tree.
7. **Version coupling**: the `happy_hare_version` symbol renders
   `$(HH_VERSION)`, which `install.sh` sed-extracts from
   `extras/mmu/mmu_constants.py`; `mmu_machine.py` refuses to boot on a
   config whose major.minor is older than the code. Keep them in lockstep
   with the git tag.
8. **Config parse errors are reported, not fatal**: `build.py`'s
   `report_parse_errors` — unparseable lines in printer.cfg/moonraker.conf
   survive verbatim (marked `# !! HAPPY HARE PARSE ERROR`) but lines in the
   HH-generated files are *lost* because those files are regenerated from
   templates. Loud warnings on both.
9. **Array grouping can clobber a same-named scalar in `as_dict()`.**
   `FOO_0..N` are grouped into `result["FOO"]` in a post-pass that runs
   *after* the per-symbol loop, so a multi-element list silently overwrites
   a non-indexed scalar `FOO` stored earlier (and Jinja then renders the
   list's repr instead of the value). If a board file re-declares per-gate
   indexed symbols, gate them on exactly the conditions the feature file
   uses (e.g. `MMU_HAS_PER_GATE_NFC_READERS` && `PARAM_NUM_GATES > $(i)`) so
   they only exist when the feature file defines them anyway. (Singletons
   get ungrouped, so a clobber needs ≥2 — which `@repeat` happily
   provides.)
10. **`@repeat` is a per-parse multiplier — use sparingly.** It is a
   line-level preprocessor (reference item 6): the body is duplicated
   `max-min+1` times on *every* `Kconfig()` construction, uncached (multi-
   unit pays it per unit), and each expanded line becomes real symbol
   nodes in the parse — so a `min=0 max=11` block costs 12× the body's
   node count on every pass. In this fork `min`/`max` are literal
   integers only, so a `@repeat` is never dynamic: when a loop's count is
   static (here, always), **unroll it in the file** instead of adding a
   new `@repeat` block. The established per-gate blocks (`Kconfig.pins`,
   `Kconfig.nfc_reader`, `Kconfig.environment_sensor`, ...) predate this
   guidance — don't add to that footprint, and unroll when you end up
   editing one of them per-board anyway.

## Quick reference

```bash
make menuconfig                # interactive (usually via ./install.sh -i)
make olddefconfig              # fill new defaults into .mmu_config (stale-only in install.sh)
make verify_pickle             # every explicit CONFIG_* assignment survives as_dict()
                               # (needs an existing .mmu_config)
make variables                 # interpreters + file sets, printed
make test  UT=test_mmu_config.py | test_mmu_profiles.py | test_menuconfig.py
                               # or: make test ALL=1  — whole suite, non-interactive
make console ARGS='--profile boxturtle'
make diff                      # installed vs freshly built configs
```
