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
                     out/.mmu_config.pickle   (rebuilt when .mmu_config or a kconfig_sources file is newer)
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
- **Multi-unit = one entry-point parse plus one per unit, driven by
  `install.sh`**: one entry-point
  parse (`F_MULTI_UNIT_ENTRY_POINT=y F_MULTI_UNIT=y`, `UNIT_NAME="u0,u1,..."`
  becomes the `MMU_UNITS` list) plus one per unit
  (`F_MULTI_UNIT=y UNIT_NAME=uN MCU_NAME=uN UNIT_INDEX=N KCONFIG_PARENT=<top>`;
  the unit parse reads printer-level values from the top-level file itself). See `install.sh` `run_kconfig_top` /
  `run_kconfig_units` / `run_kconfig_one`. The per-unit config file and the
  installed `.cfg` files both gain a `_<unit>` suffix. A unit parse also
  reads the OTHER units' saved files (`KCONFIG_PARENT`) for components one
  unit shares with another — the owners' encoder/buffer names and an owner's
  capabilities; see [extensions item 13](references/kconfiglib-extensions.md).
- **A unit's name is its identity** — per-unit Kconfig file, every section
  (except its encoder and buffer, named by their own PARAM, which defaults to the
  unit's name: item 13) and pin prefix, and the `mmu_<unit>_*` keys in `mmu_vars.cfg`. `MMU_UNITS`
  is a `sequence_editor` symbol (extension catalog item 21) so the list
  editor can record renames, removals and moves (a comma list alone can't) in
  `.mmu_config.MMU_UNITS.changes`, relative to `F_UNITS_BASELINE` (the
  *installed* `units:`). `installer/unit_migration.py` acts on it: `check`
  (only appending is allowed outside Replace mode, except before anything is
  installed), `kconfig` (renames/rewrites
  `.mmu_config_<unit>` before the per-unit menuconfig), `prepare` and `apply`
  (in `make install`, Klipper stopped: retire old unit files, rename unit
  keys and move global gate lists in the save_variables file). Its progress
  lives in `.mmu_config.unit_migration` so an aborted run resumes safely.
  A **single unit** names itself instead: `UNIT_NAME` has a "Klipper object
  name" prompt (in `Kconfig.name`) only when `!MULTI_UNIT` and
  `F_UNITS_RESTRUCTURE=y` (Replace mode or a first install, the same rule as
  the MMU units list; otherwise a read-only comment shows it), and the Makefile
  takes `UNIT_NAME`/`MCU_NAME` from the saved `CONFIG_UNIT_NAME` for a
  single-unit config. Its `reparse_env` (extension catalog item 22) makes
  menuconfig re-parse on a rename so every derived default follows at once.
  install.sh still spots a rename (`F_UNIT_NAME_BEFORE` or `CONFIG_MCU_NAME`
  differ from `CONFIG_UNIT_NAME`), rewrites explicit values and forces a
  top-level `olddefconfig`. In multi-unit the prompt is hidden, so the name
  always comes from `MMU_UNITS`.
- **`make` itself reads the value file**: the Makefile does
  `-include $(KCONFIG_CONFIG)`, so any `CONFIG_*` symbol
  (`CONFIG_MULTI_UNIT`, `CONFIG_MMU_UNITS`, `CONFIG_UNIT_NAME`, `CONFIG_KLIPPER_HOME`, ...)
  steers the build (e.g. `unit_names`). Renaming such a symbol is a Makefile
  change too.
- **Staleness**: `kconfig_sources` in the `Makefile` = every `Kconfig*` file in
  `installer/` and up to two directory levels below it, plus `kconfigfunctions.py`
  and `kconfiglib.py`. The `kconfig_needs_update` target compares them by mtime
  against the value file (a unit file is also compared against its top-level
  file, `KCONFIG_PARENT`); when stale, `olddefconfig` (never menuconfig)
  refreshes the file with new defaults. New `Kconfig*` files are picked up
  automatically by the wildcard; files with any other name, or nested deeper,
  are invisible to this mechanism. The pickle rules list the same
  `$(kconfig_sources)` as prerequisites, so a Kconfig change re-pickles even
  when the value file is untouched.
- **User values survive by design; recorded defaults do not**: explicit user
  assignments in an existing `.mmu_config` are preserved, but a value saved
  with `#~DEFAULT~#` (choices included) is recomputed by every `olddefconfig`
  and menuconfig load. So changing the *default or meaning of an existing*
  symbol or choice reaches installed machines that accepted it (see
  CONTRIBUTING: such changes "will probably be rejected"). `olddefconfig.py`
  prints every such change to stderr (`change_report`), which is what
  `install.sh` shows under "Updating Kconfig defaults".
- **Renaming a symbol discards the user's value unless it is in the rename
  table.** kconfiglib drops an assignment whose symbol no longer exists, and
  its `warn_assign_undef` is off unless `KCONFIG_WARN_UNDEF_ASSIGN=y` (nothing
  here sets it), so an unhandled rename loses the old line with *zero*
  diagnostics and the new name takes its default.
  `installer/upgrades.py` does not help: it renames options and sections in
  the generated Klipper `.cfg` files, not Kconfig symbols.
  **The hook is `HH_RENAMED_SYMBOLS`** in the kconfiglib fork (old name → new
  name), applied by `Kconfig._migrate_renamed_symbols` at the end
  of `load_config(replace=True)`.
  Add an entry in the same commit as the rename; entries can be dropped a
  major version later. Because it runs inside the loader, every caller gets
  it (menuconfig, olddefconfig, `build.KConfig`, the test harness), per-unit
  `.mmu_config_<unit>` files are covered for free, and there is no file
  rewriting — so none of the mtime/glob/ordering hazards of a text pass apply.
  Two things it must keep doing, both covered by
  `test/installer/test_kconfig_rename_migration.py`:
  - **Honour `filter_defaults`.** menuconfig/olddefconfig pass `True` and
    clear a `#~DEFAULT~#` line; `build.py` passes `False` and applies it.
    Treating them alike freezes an old default as a user value.
  - **Handle `# CONFIG_X is not set`**, a different regex from `KEY=VALUE`,
    and how a saved config records what was turned *off*.
  **Its one limit:** it runs after `make` has done `-include $(KCONFIG_CONFIG)`
  and after `install.sh` has sourced the same file as shell, so a symbol that
  *those* read by name (`MULTI_UNIT`, `MMU_UNITS`, `KLIPPER_HOME`, …) still
  cannot be renamed this way — that needs a pass that rewrites the file.
- **A shared definition can be a component.** `installer/components/Kconfig.*`
  is sourced once per consumer with `prefix := X` preprocessor variables, so
  one definition generates `PARAM_X_…` for each. `installer/Kconfig.purging`
  and `installer/components/Kconfig.tmc_driver_*` are the worked example;
  `components/Kconfig.environment_sensor` (type, i2c bus, address, pins) is
  sourced from inside an `@repeat` (once per gate) as well as once for the
  shared sensor
  ([references/environment-sensors.md](references/environment-sensors.md)
  has the Klipper sensor facts and design behind it). Three rules: re-assign every variable immediately before each `source`
  (they are global for the whole parse); keep an enclosing `if` away from
  the defaults, because `_propagate_deps` ANDs it into defaults as well as
  prompts; and pad prompts with `$(pad,width,text)` rather than literal
  spaces. TMC does the second by splitting declarations from prompts.
  `components/Kconfig.servo` (every `[mmu_servo]`: selector, vent incl.
  per gate, Blobifier, gantry, cutter) does it in one file: the caller
  sources it outside its feature `if` and passes the prompt condition as
  `servo_visible` (extension 23), straight after the toggle the prompts
  should nest under.
- **The one-file servo shape needs a prompt on every node.** A promptless
  node's automatic-submenu test looks at its `dep` (`_auto_menu_dep`), and
  outside the `if` that dep can't mention the toggle, so the first one ends
  the submenu. TMC's derived symbols (`PARAM_*_TMC`, `BOOL_*_TMC_*`) can't
  have prompts, so merging its types into the menu fragment drops the
  Blobifier stepper prompts out from under "Have Blobifier?"; that is why it
  stays split. Where losing defaults while hidden is fine, as for the
  environment sensor, source the whole thing inside the `if` instead.
  Callers' differences go in variables, servo style: an optional prompt is
  `prompt "…" if $(flag) && …` (`env_sensor_custom_bus`), extra help lines a
  `$(nl)`-prefixed hint, and extra choice members a re-opened named choice
  in the caller (the shared sensor's fixed i2c2/i2c3 list).

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

**The special-default behavior**: symbols (and `CHOICE_`-named choices) whose
name matches the list are written into `.mmu_config` with a trailing
` #~DEFAULT~#` magic token whenever they were *not* user-set (i.e. the saved
value is just the computed default). On the next menuconfig/olddefconfig load
(`load_config(..., filter_defaults=True)`) that assignment is parsed then
*cleared and marked `_was_default`* — so the symbol stays a **modifiable
default**: the user can change it, menuconfig shows
`(NOT DEFAULT)` and the **`r` key resets it** back to the default. Any other
name is a normal Kconfig variable whose saved value is always an explicit
assignment that never falls back to the default.

The three lists (re-grep the tuples when you touch one):

1. **write side** — which values get the ` #~DEFAULT~#` token:
   `Kconfig.write_config` in `kconfiglib.py` →
   `('PARAM_', 'VAR_', 'PIN_', 'BOOL_', 'MMU_HAS_', 'CHOICE_', 'UNSELECT_')`
   plus unnamed-check: choices must be *named* `CHOICE_*`.
2. **display side** — which get the `(NOT DEFAULT)` marker (i.e. are
   `r`-resettable in the UI): `_node_str()` in `menuconfig.py` →
   `('PARAM_', 'VAR_', 'PIN_', 'BOOL_', 'MMU_HAS_')` for prompted symbols;
   choices by name `CHOICE_*`.
3. **reset side** — what `r` may actually clear:
   `menuconfig._reset_node` → the 7-prefix tuple plus choice-member/sibling
   clearing for `CHOICE_` choices.

Note the three lists are *deliberately not identical* (e.g. `UNSELECT_` gets
a default token but no `(NOT DEFAULT)` marker — it's a hidden switch the user
isn't meant to fiddle with). Don't "simplify" them into one without deciding
what the UI behavior should be.

**`VAR_*`** (macro variables) and **`PIN_*`** get the token but their *cfg*
landing spot differs: `VAR_*` values are copied into `gcode_macro` sections
per `VAR_SECTION_MAP` in `installer/build.py` (`variable_<name>` options),
not into hardware files.

**Naming trap — array grouping.** `KConfig.as_dict()` collapses any symbol
matching `^(.+_)(\d+)$` with index ≤ 12 into a **list** in the render dict
keyed by the prefix *including its trailing underscore*
(`PIN_EJECT_BUTTON_0..11` → `PIN_EJECT_BUTTON_: [..]`, used in templates as
`(PIN_EJECT_BUTTON_|d)[i]`); the individual `FOO_<n>` keys are then absent.
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
                          # source order
  Kconfig.<topic>         # one file per feature: name, num_gates,
                          # selector_type, endstops, options, pins, heater,
                          # fans, leds, encoder, espooler, nfc_reader, ...
                          # Convention: "# Sets/Defines parameter tokens:" header
  mmu_types/ (+ starters/)  # rsource "Kconfig.*" — one file per machine type
                            # (each inlines Kconfig.capabilities; see the
                            #  cross-file section below)
  components/           # shared fragments sourced once per consumer (TMC driver)
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
Printer-level capabilities (`MMU_HAS_SENSOR_TOOLHEAD/EXTRUDER/TOOLHEAD_CUTTER`)
reach unit parses from the top-level config through `KCONFIG_PARENT`
(`$(printer-flag,SYM)` in the root `Kconfig`, `shared_components.PRINTER_FLAGS`; n
with no parent), feeding promptless `MMU_HAS_*` defaults in its
`if MULTI_UNIT && !MULTI_UNIT_ENTRY_POINT` block.

## Checklist: adding / changing a symbol

**Help display limit: at most seven lines per Kconfig `help` block, including
blank lines.** Menuconfig can display only seven lines. Keep lines short enough
to avoid wrapping beyond that limit; put longer explanations in documentation.

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
   expansions (count = nodes per parse — pitfall 10), `prompt "..." if PARAM_NUM_GATES > $(i)`
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
     tuple appended to `CONSOLE_PROFILES` in the `PROFILES` dict (not in
   `CONSOLE_PROFILES` itself) and assert on the
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
     reached via `./install.sh -i`). To automate it, drive it through a pty;
     see the notes at the end of extension catalog item 22.
7. **If you touched the fork itself** (`as_dict`, load/write, menuconfig):
   the tests for it are `make verify_pickle` (pickle consistency),
   `make test UT=test_menuconfig.py` (`test/installer/test_menuconfig.py`,
   menuconfig cursor behavior), and
   profile tests. The vendored base is kconfiglib **v14.1** — the HH patches
   are marked `# Happy Hare:` inline; if you re-sync upstream, that grep is
   your change list.

## Machine- and board-specific defaults (cross-file)

**Machine- and board-specific defaults MUST live in the matching machine type's `mmu_types/Kconfig.<mmu>` file (inside its `if MMU_TYPE_X` block, or as `BOARD_TYPE` choice default) or the matching board's `boards/Kconfig.<board>` file — never in a feature `Kconfig.<topic>`.** A feature file stays machine- and board-agnostic; a `default ... if MMU_TYPE_X` / `if BOARD_TYPE_X` line in a feature file is a rule violation even when it happens to work (earliest-default-wins, below).

Machines and boards get their own defaults by re-declaring the same `config` in
`mmu_types/Kconfig.<mmu>` (inside the `if MMU_TYPE_X` block — no `if` on the
default needed) or `boards/Kconfig.<board>` with just an added `default` —
no prompt, no repeated help. The nodes merge into one Symbol (upstream Kconfig: same-name
`config` definitions merge; prompts add, defaults add). Gotchas, all bitten
in this repo:

- **A default's condition is evaluated as an expression against the
  current values of the symbols it references.** The parser stores the
  explicit `default <val> if <cond>` condition (the `_T_DEFAULT` branch of
  `_parse_props`); `_propagate_deps` then ANDs in the node's own
  `depends on` and every enclosing `if`/`menu` dependency — so a plain
  `default <val>` inside an `if BOARD_TYPE_X` block is correctly scoped
  with no repeated condition (this is how all board/machine files are
  written). What surprises people is the *evaluation*: conditions are
  checked against the symbols' computed values, and for a promptless
  (invisible) symbol the user value is discarded (pitfall 2) — so a
  condition can silently fail to match for a reason that has nothing to
  do with scoping.
- **The earliest-parsed default wins** for strings (`Symbol._node_ordered_string_default()`,
  called from both `Symbol.str_value` and `Symbol._str_default`, so it decides
  both the computed value and the min-config write). The per-unit branch of
  the root `Kconfig` sources `mmu_types/Kconfig`, then `boards/Kconfig`, ahead
  of `Kconfig.mmu_additions` and the other feature files, so a satisfied
  machine-type or board default beats a feature file's later `default ""`
  (`test/installer/test_board_defaults.py` pins the board side of this).
  Exception: the files the type files inline (the `Kconfig.capabilities`
  bullet below) are parsed inside `mmu_types/`, *before* `boards/Kconfig`, so
  for their symbols a satisfied default in the selected type's copy beats a
  board's.
- **A named `choice` can gain members from another file; an unnamed one
  can't.** Re-opening `choice CHOICE_X ... endchoice` with just the new
  `config` lines adds them to the same choice; give each one a `depends on`
  its board or type so it shows only there. `boards/per_gate/Kconfig.slb`
  and `Kconfig.ebb_gen1` add their i2c buses to
  `CHOICE_ENVIRONMENT_SENSOR_I2C_BUS_$(gate)` this way. A machine/board file
  can also steer an existing choice with `default <CHOICE_MEMBER> if <cond>`.
  Selection is first-satisfied over the merged `choice.defaults` list, in parse order
  (`Choice._selection_from_defaults`; the member must also be visible) —
  a default in an earlier-sourced type or board file precedes the feature
  file's own `default` lines (same exception as above); within one file, put the new board-specific
  line *above* the older, more general ones.
- **`Kconfig.capabilities` is inlined once per machine type.** Every
  `mmu_types/Kconfig.*` (incl. `starters/`) sources it (and through it
  `Kconfig.selector_type`, `Kconfig.bypass`, `Kconfig.filament_buffer`) inside its own
  `if MMU_TYPE_X` / family block; some also source `Kconfig.num_gates` and
  `servos/Kconfig` there. `Kconfig.mmu_additions` and the other feature files
  are sourced only from the root. Each inlined copy is a definition site of
  the same ONE object, and `_propagate_deps` ANDs the copy's type guard into
  its defaults, so only the selected type's copy is live. To steer a
  capabilities symbol from a type file, put the `default` line *above* that
  file's own `source "Kconfig.capabilities"`. To see what actually merged,
  introspect `sym.nodes` (parse order) or `kc.choices` (`choice.defaults`
  accumulate in parse order) and evaluate conditions with module-level
  `kconfiglib.expr_value`; fork expr nodes are tuples, the tree root is
  `kc.top_node`, locations are `node.filename`/`node.linenr`.
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
   assign/restore discipline catches the wrong-chip form. The same fact means
   editing a symbol in menuconfig can't change defaults derived from env —
   unless the symbol has `reparse_env` (extension catalog item 22), which
   re-parses with the new value.
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
4. **`$(shell, ...)` is expensive.** kconfiglib forks a shell each time a
   `$(shell, ...)` is expanded, and a recursively-expanded (`=`)
   *parameterized* macro re-expands on **every reference**. The root
   Kconfig keeps its three discovery calls (serial devices, CAN interfaces,
   CAN UUIDs) in simply-expanded `:=` variables, evaluated once per parse;
   the per-gate lookups built on them (`serial_device`, `canbus_connection`,
   ...) call Python preprocessor functions registered in the `functions`
   dict of `kconfigfunctions.py` (tested by
   `test/installer/test_kconfigfunctions.py`). Put a new deterministic
   helper there, not in a `$(shell)` inside an `=` macro. The harness's
   `_render_kconfig_cache` in `test/hh/cfg.py` reuses a parsed tree only
   for an identical environment, because env changes between parses.
5. **Makefile traps**: inline `#` comments pad values with leading spaces
   (see the comment at the top of the `Makefile` — keep comments on their
   own line); any *new interactive* make goal must be added to the
   `$(filter ...,$(MAKECMDGOALS))` exclusion list in the output-sync probe
   or `--output-sync` buffers its prompt away; the value files are
   `.PRECIOUS`. `make variables` prints which interpreter
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
9. **Array grouping changes the shape of `as_dict()`.** `FOO_0..N` are
   collected in a post-pass into `result["FOO_"]`, and the individual
   `FOO_<n>` keys are not emitted — except a singleton, which is restored
   under its own name. So a second symbol with the same prefix turns a
   template's `[[FOO_0]]` into an undefined name, and a list key could
   overwrite a scalar literally named `FOO_`. `as_dict()` iterates every
   defined symbol, whether or not its `if` conditions hold, so conditions
   don't prevent this: if a board file re-declares per-gate indexed
   symbols, re-declare only the names the feature file already declares
   (a re-declaration merges; a new index is a new list element).
10. **`@repeat`'s cost is the count, not the macro.** Each expanded line
   becomes real symbol nodes on *every* `Kconfig()` construction (multi-
   unit pays it per unit), so a `min=0 max=11` block costs 12× the body's
   node count per parse — but writing the 12 copies out by hand costs
   exactly the same, so don't unroll for speed. Keep counts to what is
   needed and prefer `@repeat` over hand copies. A count used in more than
   one place belongs in a preprocessor variable (`max=$(var)`), which
   Python can read from `kconf.variables` instead of keeping a copy.

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
