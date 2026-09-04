# HH kconfiglib fork — extension catalog

`installer/lib/kconfiglib/` is a vendored **kconfiglib v14.1** (Nordic/UMS
upstream) carrying HH patches marked `# Happy Hare:` inline. Grep for that
marker to get the full patch list; this catalog explains what each extension
*does* and how to use it in a Kconfig file. Verified against the current
tree — line anchors drift, re-grep the identifiers.

The root `installer/Kconfig` header comment is the user-facing summary of
most of items 1-11 below, numbered differently (its `array_size_mismatch`
item is part of item 7, its "if XX" comment-line construct of item 6, its
`Menuconfig:` items of items 7, 9-11). Items 12-15 (the source family, the
generated file, the pickle, default-resolution semantics) are not in the
header. When you add an extension, update **both** this file and that header
block.

## 1. `generated_default`

A `default` whose value is computed from *other* symbols (or iterated).
Syntax:

```
generated_default "<template>" "<arg syms>" [<separator>] [[<start>] <stop>]
```

- `<template>`: `%s`/`%d` consume the arg list left-to-right (list a symbol
  twice to fill two placeholders), `%i` is the iterator value, `%%` is a
  literal `%`. `{...}` is a basic arithmetic expression (`+ - * / // % **`
  and parens) evaluated *after* substitution, referring to symbols by name,
  e.g. `{%i % %d}`.
- Without `<start>/<stop>` the template renders once; with them it renders
  once per value in `range(start, stop)` (start defaults to 0) and the
  results are joined with `<separator>` (default `", "`).
- Can appear multiple times on a symbol (list of `(template, args, separator,
  start_sym, stop_sym, cond)` tuples, `kconfiglib.py:~3739`); conditions
  work; dependencies are registered so the value invalidates when its inputs
  change (`_build_dep`).

Examples from the tree:

```
generated_default "%s_%d" "PARAM_VENDOR PARAM_VERSION"
generated_default "%i_%s" "PARAM_VENDOR" PARAM_NUM_GATES
generated_default "neopixel:$(UNIT_NAME)_gate%i_leds (1-%d)" "PARAM_NUM_GATES" PARAM_NUM_GATES
generated_default "{(%i + 1) % %d}" "PARAM_NUM_GATES" ", " 0 PARAM_NUM_GATES
```

## 2. FLOAT type

New symbol type `float` (and numeric validation hooked into default checks).
`Symbol.orig_type is kconfiglib.FLOAT`; string values stay strings (no int
coercion).

## 3. `forceshow`

Option on symbols *and menus*: shown in menuconfig even when the visibility
condition isn't met (used e.g. on `MMU_HAS_EJECT_BUTTONS` so users can
enable a capability the type didn't imply).

## 4. ` #~DEFAULT~#` default token

The modifiable-defaults mechanism (see SKILL.md "naming contract").
Lifecycle, all in `kconfiglib.py`:

- Constant `HH_DEFAULT_TOKEN = " #~DEFAULT~#"` (line ~567).
- **Write** (`write_config`, ~1758-1770): a symbol/choice's line is emitted
  with the token appended iff it was *not* user-set (`_was_set`) — so the
  value on the line is the computed default by construction — and its name
  starts with one of `PARAM_ VAR_ PIN_ BOOL_ MMU_HAS_ CHOICE_ UNSELECT_`
  (choices: must be *named* `CHOICE_*`).
- **Load** (`load_config(..., filter_defaults=True)`, ~1320-1400): the
  `CONFIG_X=value #~DEFAULT~#` / `# CONFIG_X is not set #~DEFAULT~#` regexes
  (~994) recognize the token; the value is applied, then *unset and marked*
  `_was_default`, so the computed default applies and menuconfig treats the
  symbol as unmodified (resettable with `r`).
- **Who uses which**: `menuconfig`/`olddefconfig` go through
  `standard_kconfig` → `filter_defaults=True` (the default). `installer/
  build.py::KConfig` calls `load_config(..., filter_defaults=False)` so the
  stored value — default or explicit — is kept verbatim for template
  rendering. Don't mix these up: the pickle path is the *falsy* one.
- Legacy migration: `_migrate_legacy_boolint_pairs()` runs on
  `load_config(replace=True, filter_defaults=True)` and recovers
  pre-BOOLINT `BOOL_X`+`PARAM_X` pairs from beta-era files (remove after the
  v4 beta window, per its docstring).

## 5. `saved-config-value` preprocessor function

Registered in `kconfigfunctions.py` (`functions = {"saved-config-value":
(fn, 1, 1)}`). Usable in Make-style `$(...)` expansions inside Kconfig files
(see the root Kconfig's `saved_canbus_connection` macro): reads symbol
`$(1)`'s **last assignment** from the `KCONFIG_CONFIG` file *before* normal
config loading, including lines carrying the default token (stripped), with
Kconfig string escaping applied in both directions. The value file is
cached keyed by `(realpath, mtime_ns, size)` — edits to the file invalidate
it; note the read happens during *parsing*, so it always sees the file as it
was at parse start, not mid-session menuconfig changes.

## 6. `@repeat` / `@if` / `@ifnot` line macros

Implemented in the line reader (`_next_line`, ~2384-2600; dispatch at ~2612):
the tokenizer sees expanded lines, so these look like ordinary Kconfig text
once expanded and can be nested.

- `@repeat var=i min=0 max=11@ ... @endrepeat@` — the body lines are
  emitted once per `i` in `[min..max]` with `$(i)` substituted (`var=`
  names the placeholder; `min`/`max` are required, integers). Used for
  per-gate pin/prompt blocks where the gate count is compile-time fixed
  (max 12) and prompts are conditionally hidden via
  `prompt "..." if PARAM_NUM_GATES > $(i)`.
- **Cost — use sparingly.** Expansion is not cached anywhere: every
  `Kconfig()` construction (menuconfig, olddefconfig, pre-parse-kconfig —
  and in multi-unit, one parse per unit *plus* the entry point) re-reads
  the raw file and re-duplicates the body. Every expanded line then
  tokenizes into real symbol nodes (prompts, defaults, dep-graph entries),
  so a `min=0 max=11` block adds **12× the body's node count to every
  parse**. In this fork `min`/`max` must be *literal* integers
  (`_to_int`, a parse error otherwise), so a `@repeat` here is never
  dynamic — it is a source-author convenience, not a mechanism. A new
  loop with a static count should be **unrolled in the file**; keep
  `@repeat` for the established per-gate blocks where editing one block
  beats editing twelve, and don't grow that footprint.
- `@if <ENV_VAR>@ ... @endif@` / `@ifnot <ENV_VAR>@ ... @endif@` — the
  block's lines are only fed to the tokenizer when the *environment* variable
  named by the arg is set to a truthy value (`y/yes/1/true`, case-insensitive;
  nested `@if`/`@ifnot` supported, terminator is `@endif@`). It is **not** a
  Kconfig expression — this is the root header's "'if XX' construct allowed
  on comment lines" feature, evaluated by the line reader
  (`_expand_if_macro`) before tokenization.

## 7. `array_editor <separator> [size]`

String symbols can opt into a list-editing dialog in menuconfig instead of
raw text entry: `array_editor ","` (separator; optional second arg is a
symbol naming the max element count — see the comment at ~3548-3580, it is
*not* a number literal: quote it if you mean a literal). `MMU_UNITS`
(`array_editor ","`) is the canonical use. The editor splits/joins on the
separator and validates count against the size symbol.

## 8. `boolint` / `defboolint`

Boolean *UI/logic* with numeric `0`/`1` string output — introduced to
replace the historical `BOOL_X` (prompted) + `PARAM_X` (promptless int)
pair: one symbol, checked-box in menuconfig, integer in the rendered cfg.
`boolint` = user-settable, `defboolint` = default-only (like `def_bool`).
Internal representation is the normal tristate machinery
(`_normalize_boolint_default`, `kconfiglib.BOOLINT` type at ~7802); the
pickle/`as_dict` path emits the strings `"0"`/`"1"` (see `KConfig.as_dict`'
BOOLINT branch and `getint`'s special case).

## 9. Menus get `help`, comments are layout

- `help` text on `menu` nodes (upstream kconfiglib only allows help on
  symbols) — rendered in the menuconfig menu screens.
- Comment lines are first-class UI: `comment "_"` draws a full-width
  separator, `comment "_Heading"` a section heading, plain `comment "..."`
  an inline note; flexible/multi-line comments are preserved. The menuconfig
  cursor skips comment runs when navigating (regression-tested in
  `test/installer/test_menuconfig.py` — a comment-only menu is not entered).

## 10. Font markup in prompts/comments

`[[B]]…[[/B]]` (bold) plus `DIM`, `U`/`UNDERLINE`, `REV`/`REVERSE`,
`C:<n>`/`COLOR:<n>`, `RESET` can be embedded in `mainmenu`/`menu`/`comment`
text and prompts; menuconfig renders them with the terminal's ANSI escapes.
The root Kconfig's `title`/`caption` macros use `[[B]]` to bold the unit
name. (This is a menuconfig-side concern — it never reaches the value file.)

## 11. menuconfig changes

- **`r` resets to default** (key handler ~956): clears the user value of
  the selected symbol (or choice, incl. siblings) and unmarks it as set —
  only for names on the SKILL.md lists, and only when the value currently
  differs from the computed default (`differs_from_default`, ~1738). The
  `(NOT DEFAULT)` marker (~3512) is what tells the user `r` is available.
- **`MENUCONFIG_STYLE`** env selects the screen theme; the Makefile forces
  `aquatic` for the multi-unit entry point (single unit: `default`).
- Comment-aware cursor navigation (item 9).

## 12. `source` family

Upstream kconfiglib features HH relies on heavily (docs at kconfiglib.py
~366-427): `source` is srctree-relative, `rsource` is *file*-relative
(`mmu_types/Kconfig` does `rsource "Kconfig.*"`), all accept **globs**, and
`osource`/`orsource` are the "ignore if missing" variants (the root Kconfig
does `osource "/tmp/.Kconfig.generated"` for the dynamic shared-component
choices — see item 13).

## 13. `/tmp/.Kconfig.generated` (dynamic shared choices)

`installer/build.py::gen_kconfig_options` (make target `gen_kconfig`) can
generate extra Kconfig rules in `/tmp/.Kconfig.generated` that let
printer-level shared components (toolhead name, encoder name,
sync-feedback buffer name) offer "use an existing one from another unit"
choices, derived by parsing the existing value files (`PARAM_REGEX`,
`to_symbol`). **Currently incomplete/unused** — marked TODO in build.py;
treat it as scaffolding, and if you resume it, the `MMU_SHARED_*` /
`CHOICE_*_TYPE` symbols it emits are the integration point.

## 14. The pickle (value transport)

`build.py::pre_parse_kconfig` reduces a full `Kconfig` parse to a tiny
pickleable dict — `values` (from `as_dict()`) plus `choices`
(`{choice_name: selected_member_name}` from `user_selection or selection`)
— written atomically (`.tmp` + `os.replace`) to
`out/<basename>.pickle`; `load_parsed_kconfig` reads it back into a
`ParsedKConfig` that implements the same accessor surface
(`get/getint/is_enabled/is_selected/as_dict`). Full-`Kconfig` pickling was
abandoned at >20 000 recursion depth. The Makefile regenerates the pickle
only when the source value file is newer (`out/*.pickle: $(KCONFIG_CONFIG)`,
:524-528), and `make verify_pickle` runs
`lib/kconfiglib/test_kconfig_pickle_consistency.py` against each — that
script re-reads the raw file with *stock* kconfiglib semantics and compares,
so it catches `as_dict` regressions without importing HH code.

## 15. String & choice default-resolution semantics (cross-file)

Not an extension per se, but the resolution rules the fork implements (they
are why board files can override feature-file defaults):

- **Strings**: both the *value-computation* path (`Symbol` value calc,
  kconfiglib.py:~5010) and the *min-config write* path (~5596-5608) call
  `_node_ordered_string_default()` (~5613), which walks `self.nodes` in
  **source parse order** and returns the **first** default whose condition is
  satisfied — plain `default` and `generated_default` entries interleave in
  that order. First-satisfied wins, NOT last-wins (unlike stock C kconfig's
  later-override).
- **Conditions are evaluated exactly as written**: the parser
  (kconfiglib.py:3608-3610) stores only the explicit `default <val> if <cond>`
  expression; the node's enclosing `if`/`depends` block is **not** auto-ANDed
  in. When re-declaring a symbol from another file, repeat the relevant
  symbol in the default's own `if`.
- **Parse order decides**: `installer/Kconfig` sources `boards/Kconfig` at
  :277, *before* `Kconfig.mmu_additions` at :279 (and the per-topic feature
  files it pulls in) — so a board file's satisfied default beats a later
  feature-file `default ""`.
- **Choices**: selection from defaults is first-satisfied in the order the
  `default <member>` lines are written (`Choice._selection_from_defaults`,
  kconfiglib.py:~6130; member visibility is also checked). Only members of
  the *same* choice (same file) can be defaulted; steering an existing
  choice from another file means adding a `default <member> if <cond>` line
  above the older ones.
