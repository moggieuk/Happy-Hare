# HH kconfiglib fork — extension catalog

`installer/lib/kconfiglib/` is a vendored **kconfiglib v14.1** (Nordic/UMS
upstream) carrying HH patches marked `# Happy Hare:` inline. Grep for that
marker to get the full patch list; this catalog explains what each extension
*does* and how to use it in a Kconfig file. Anchors are function and
symbol names; grep for them.

The root `installer/Kconfig` header comment is the user-facing summary of
most of items 1-11 below, numbered differently (its `array_size_mismatch`
item is part of item 7, its "if XX" comment-line construct of item 9, its
`Menuconfig:` items of items 7, 9-11). Items 12-15 (the source family, the
generated file, the pickle, default-resolution semantics) and the `@if`
macros of item 6 are not in the header; items 16-20 are its items 15-17,
13 and 14, items 21-22 are its items 18-19 (item 21 also `Menuconfig:`
item 7), item 13 is its item 20, and item 23 is its item 23. When you add an extension, update **both** this file and that
header block.

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
- Can appear multiple times on a symbol (`node.generated_defaults`, a list of
  `(template, args, separator, start_sym, stop_sym, cond)` tuples appended in
  the `_T_GENERATED_DEFAULT` branch of `_parse_props`); conditions work, and
  `_propagate_deps` ANDs enclosing dependencies into them as for `default`;
  dependencies are registered so the value invalidates when its inputs
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

`forceshow if <expr>` forces it only while `<expr>` holds (`menuconfig._forced`), e.g.
the buffer sensor bools, which a unit sharing another unit's buffer sees as a summary
instead.

## 4. ` #~DEFAULT~#` default token

The modifiable-defaults mechanism (see SKILL.md "naming contract").
Lifecycle, all in `kconfiglib.py`:

- Constant `HH_DEFAULT_TOKEN = " #~DEFAULT~#"` (module level; a copy lives in
  `kconfigfunctions.py`).
- **Write** (`write_config`): a symbol/choice's line is emitted
  with the token appended iff it was *not* user-set (`_was_set`) — so the
  value on the line is the computed default by construction — or, for a
  symbol, `_saved_as_default()` holds (item 16), and its name
  starts with one of `PARAM_ VAR_ PIN_ BOOL_ MMU_HAS_ CHOICE_ UNSELECT_`
  (choices: must be *named* `CHOICE_*`).
- **Load** (`load_config(..., filter_defaults=True)`): the
  `CONFIG_X=value #~DEFAULT~#` / `# CONFIG_X is not set #~DEFAULT~#` regexes
  (`_set_match` / `_unset_match`, built in `Kconfig._init`) recognize the
  token; the value is applied, then *unset and marked*
  `_was_default`, so the computed default applies and menuconfig treats the
  symbol as unmodified (resettable with `r`).
- **Who uses which**: `menuconfig`/`olddefconfig` go through
  `standard_kconfig` → `filter_defaults=True` (the default). `installer/
  build.py::KConfig` calls `load_config(..., filter_defaults=False)` so the
  stored value — default or explicit — is kept verbatim for template
  rendering. Don't mix these up: the pickle path is the *falsy* one.

## 5. `saved-config-value` preprocessor function

Registered in the `functions` dict of `kconfigfunctions.py`
(`"saved-config-value": (saved_config_value, 1, 1)`), alongside the other
HH preprocessor functions (`env-default`, `env-is-y`, `serial-device`,
`word-at`, `hh-pad`, `hh-multiline`, `hh-newline`, ...) that keep
deterministic helpers out of `$(shell, ...)`. Usable in Make-style `$(...)` expansions inside Kconfig files
(see the root Kconfig's `saved_canbus_connection` macro): reads symbol
`$(1)`'s **last assignment** from the `KCONFIG_CONFIG` file *before* normal
config loading, including lines carrying the default token (stripped), with
Kconfig string escaping applied in both directions. The value file is
cached keyed by `(realpath, mtime_ns, size)` — edits to the file invalidate
it; note the read happens during *parsing*, so it always sees the file as it
was at parse start, not mid-session menuconfig changes.

## 6. `@repeat` / `@if` / `@ifnot` line macros

Implemented in the line reader (`_next_line`):
the tokenizer sees expanded lines, so these look like ordinary Kconfig text
once expanded and can be nested.

- `@repeat var=i min=0 max=11@ ... @endrepeat@` — the body lines are
  emitted once per `i` in `[min..max]` with `$(i)` substituted (`var=`
  names the placeholder; `min`/`max` are required integers, or preprocessor
  variables or functions expanding to one, e.g. `max=$(owner-max,buffer)`). Used for
  per-gate pin/prompt blocks where the gate count is compile-time fixed
  (max 12) and prompts are conditionally hidden via
  `prompt "..." if PARAM_NUM_GATES > $(i)`.
- **`source` inside a body works.** Each sourced file starts with an empty
  line queue and the caller's queue resumes when it ends (`_enter_file` /
  `_leave_file` park it on `_line_queue_stack`), so the rest of the body
  follows the sourced file. `$(i)` is substituted only in the body's own
  lines, so pass it on in a variable assigned on the line before:
  `suffix := _$(i)` then `source "components/Kconfig.<fragment>"`.
  A stray `$(i)` inside the sourced file expands to empty and silently
  merges every copy into one symbol. Tested by
  `test/installer/test_kconfig_repeat_source.py`.
- **Cost is in the count, not the macro.** Every expanded line tokenizes
  into real symbol nodes (prompts, defaults, dep-graph entries), so a
  `min=0 max=11` block adds 12× the body's node count to **every parse**
  (menuconfig, olddefconfig, pre-parse-kconfig — and in multi-unit, one
  parse per unit *plus* the entry point). Writing the same 12 copies out
  by hand costs exactly the same; the expansion itself is a cheap line
  copy. So keep counts to what is actually needed, and when a body does
  repeat, prefer `@repeat` over hand-unrolled copies — one place to edit.
  When a count is shared — several blocks, or Python code — define it once
  as a preprocessor variable (`max=$(var)`; an undefined one is a parse
  error) and have Python read it from `kconf.variables` rather than keep a
  copy.
- `@if <ENV_VAR>@ ... @endif@` / `@ifnot <ENV_VAR>@ ... @endif@` — the
  block's lines are only fed to the tokenizer when the *environment* variable
  named by the arg is set to a truthy value (`y/yes/1/true`, case-insensitive;
  nested `@if`/`@ifnot` supported, terminator is `@endif@`). It is **not** a
  Kconfig expression — it is evaluated by the line reader
  (`_expand_if_macro`) before tokenization. (The root header's "'if XX'
  construct allowed on comment lines" is a different feature, item 9.)

## 7. `array_editor <separator> [size]`

String symbols can opt into a list-editing dialog in menuconfig instead of
raw text entry: `array_editor ","` (separator; optional second arg is an
*unquoted* INT/HEX symbol giving the required element count — a quoted or
literal value is a parse error; see the `_T_ARRAY_EDITOR` branch of
`_parse_props` and the `array_size_sym` docstring on `Symbol`). It suits
value lists that may be empty or repeat values, e.g. per-gate angles. The
editor splits on the separator and saves the lines joined by the separator
plus a space (`a, b`, `x; y` — `_join_array_lines`), the spacing the shipped
defaults use. `_check_valid` in `menuconfig.py` rejects an edit whose element
count differs from the size symbol's value.
A list of *names* whose identity matters (`MMU_UNITS`) uses item 21 instead.

`array_size_mismatch ARRAY` on a promptless BOOL makes it evaluate to `y`
when the STRING symbol `ARRAY` is non-empty and its element count differs
from `ARRAY`'s size symbol — for Kconfig warnings about values loaded from an
existing config, which the editor never saw.

## 8. `boolint` / `defboolint`

Boolean *UI/logic* with numeric `0`/`1` string output — introduced to
replace the historical `BOOL_X` (prompted) + `PARAM_X` (promptless int)
pair: one symbol, checked-box in menuconfig, integer in the rendered cfg.
`boolint` = user-settable, `defboolint` = default-only (like `def_bool`).
Internal representation is the normal tristate machinery
(`_normalize_boolint_default`, `kconfiglib.BOOLINT` type); the
pickle/`as_dict` path emits the strings `"0"`/`"1"` (see `KConfig.as_dict`'
BOOLINT branch and `getint`'s special case).

## 9. Menus get `help`, comments are layout

- `help` text on `menu` nodes (upstream kconfiglib only allows help on
  symbols) — rendered in the menuconfig menu screens.
- `comment "..." if <expr>` takes a condition directly on the comment line
  (`_expect_str_and_cond` in the `_T_COMMENT` branch of `_parse_block`), e.g.
  `comment "_" if SHOW_HIDDEN`.
- Comment lines are first-class UI: `comment "_"` draws a full-width
  separator, `comment "_Heading"` a section heading, plain `comment "..."`
  an inline note; flexible/multi-line comments are preserved. A `dim`
  property line under a comment (`_T_DIM`, `node.dim`, only valid on
  comments) makes `_node_str` wrap the whole row, `***` included, in
  `[[DIM]]` — used for the `$(misc_hardware_hint)` notes. Markup inside the
  text alone can't reach the `***` that menuconfig adds. The menuconfig
  cursor skips comment runs when navigating (regression-tested in
  `test/installer/test_menuconfig.py` — a comment-only menu is not entered).

## 10. Font markup in prompts/comments/help

`[[B]]…[[/B]]` (bold) plus `DIM`, `U`/`UNDERLINE`, `REV`/`REVERSE`,
`C:<n>`/`COLOR:<n>`, `RESET` can be embedded in `mainmenu`/`menu`/`comment`
text, prompts and help text (the help panel and the `?` information screen);
menuconfig renders them with curses attributes (`_safe_addstr_markup`).
Only those whitelisted names inside `[[...]]` are tags (`_TAG_RE`), so other
brackets such as `[, duration]` or `[[mmu_leds]]` stay literal.
The root Kconfig's `title`/`caption` macros use `[[B]]` to bold the unit
name. Comments are saved to the value file as `#` lines with their tags left
in, which is harmless because nothing parses them. As in menuconfig, a comment
whose own `if` condition is false isn't saved.

`[[VALUE:SYM]]` is replaced by SYM's *current* value every time the text is
drawn (`_expand_values`, called from `_safe_addstr_markup` and on prompts in
`_node_str`), so it follows edits made in the same session. Both that and
`write_config`, for saved comments, use `expand_value_markup` in
`kconfiglib.py`, so the file never holds the raw markup. Text fixed at
parse time can't do that. `[[VALUE:SYM:w]]` pads or truncates the value to `w`
characters for fixed-width layouts. An undefined SYM shows `?`, and
`TestValueMarkupNamesRealSymbols` in `test_kconfig_references.py` fails on one.
The header `caption` uses `$(unit-suffix,$(UNIT_NAME),20)` so a single unit
renamed in menuconfig shows its new name straight away. `$(pad,…)`
(`hh-pad`) measures displayed width: markup counts 0 and `[[VALUE:SYM:w]]`
counts `w`. A width-less `[[VALUE:SYM]]` can't be measured at parse time, so
use a width inside anything padded.

## 11. menuconfig changes

- **`r` resets to default** (the `"r"` branch of the main key loop, calling
  `_reset_node`): clears the user value of the selected symbol (or choice,
  incl. siblings) and unmarks it as set — only for names on the SKILL.md
  reset-side list, whether or not the value differs from the default. The
  `(NOT DEFAULT)` marker (`_node_str`, using `differs_from_default`) is what
  tells the user `r` would change anything.
- **`MENUCONFIG_STYLE`** env selects the screen theme; the Makefile forces
  `aquatic` for the multi-unit entry point (single unit: `default`).
- Comment-aware cursor navigation (item 9).
- The sequence editor dialog (item 21).

## 12. `source` family

Upstream kconfiglib features HH relies on heavily (documented in the
`kconfiglib.py` module docstring's `rsource`/`osource` sections): `source` is srctree-relative, `rsource` is *file*-relative
(`mmu_types/Kconfig` does `rsource "Kconfig.*"`), all accept **globs**, and
`osource`/`orsource` are the "ignore if missing" variants. Never source
anything from outside the tree: `test_kconfig_env_hygiene.py` fails the suite
if a Kconfig sources an absolute path or a parse reads a file outside
`installer/` (a generated `/tmp/.Kconfig.generated` used to leak into parses).

## 13. Shared components (`shared_components.py`, `owner-*` functions)

A unit can use an encoder or sync-feedback buffer it doesn't define: another unit's, or a
section in the user's own config. This works on single units too.

**Identity is a plain name.**
- The name is a string PARAM: `PARAM_ENCODER_NAME` or `PARAM_SYNC_FEEDBACK_BUFFER_NAME`, checked
  with `object_name_validator`, the lowercase Klipper object-name regex or blank.
- `MMU_SHARED_ENCODER` / `MMU_SHARED_SYNC_FEEDBACK_BUFFER` ("Use shared ...?", first in the
  component's menu) mean "this unit renders no section".
- The templates name the owner's section from the PARAM, e.g. `[mmu_encoder [[PARAM_ENCODER_NAME]]]`,
  and `[mmu_unit] encoder:`/`buffer:` render the same PARAM. The owner's default is `UNIT_NAME`,
  so the historical `[mmu_encoder <unit>]` output is unchanged.

**One symbol, two prompt nodes.**
- "Encoder name" `if !MMU_SHARED_ENCODER`; "Shared encoder object name" `if MMU_SHARED_ENCODER`.
  Only one shows at a time. Find a symbol's *visible* prompt node, not its first.
- A sharer's name defaults to "". A name saved by a sharer as `#~DEFAULT~#` (the old pick list
  did this) is kept via `saved-config-value`, except v4.0's `-specify name-` placeholder.
- A user value survives toggling the shared flag, so a typed owner name becomes the shared
  name and vice versa. W33/W34 catch the duplicate this can create.

**Owners are found by name, live.**
- Each unit is a separate parse, so a unit reads what the other units SAVED: `KCONFIG_PARENT`
  (the top-level `.mmu_config`, for `MMU_UNITS`) and each sibling `<parent>_<unit>`.
- `owners(kind)` lists the other units that have the component and don't share it, with their
  saved name (their unit name if none is saved).
- Kconfig reads them with `@repeat var=i min=0 max=$(owner-max,KIND)@` and compares the name
  symbol to `"$(owner-name,KIND,$(i))"` literals, guarded by `!= ""` for past-the-end slots.
  The repeat predates item 23, when a macro could only expand to one symbol name.
  Because the comparison is a real expression, a name edited in menuconfig is matched at once.
- Promptless `SHARED_BUFFER_FOUND` / `SHARED_ENCODER_FOUND` say the shared name is a sibling
  owner's.
- **Only unit parses run by install.sh read anything** (`KCONFIG_PARENT` set). The build's
  pickle parse and `verify_pickle` see no owners: FOUND is n there, and the saved values are
  applied. So every cross-unit value must be decided by a menuconfig/olddefconfig unit pass and
  SAVED in the unit's own file. `test_render_via_files.py` checks that the two agree.

**Buffer capabilities (`KINDS["buffer"].exports`).**
- The three `MMU_HAS_SENSOR_BUFFER_*` bools and `PARAM_BUFFER_SPRING_STATE` are the only buffer
  values another unit's menu reads, for endstop, autocal and homing defaults. The encoder
  exports nothing.
- `components/Kconfig.shared_export`, sourced once per export inside `if <shared flag>`, gives
  each symbol the matching owner's value (`$(owner-export,...)`). These defaults are parsed
  before the symbols' own definitions, so they win.
- Layout splits *capabilities* from *contents*:
  - The sensor bools and spring-state choice live in `if !MMU_SHARED... || !SHARED_BUFFER_FOUND`.
    The bools are `forceshow if` the same condition (item 3), so owners still see type-fixed
    ones.
  - Pins, analog tuning, range/maxrange and register are `!MMU_SHARED...` only. A name no sibling
    owns, i.e. the user's own config, therefore declares its sensors and spring state but no
    pins.
- Nothing is carried over when the owner goes (renamed, removed or switched off). The
  capability symbols fall back to their normal defaults for the user to enter, and
  olddefconfig's change report lists the derived defaults that move.
- Guard every machine-type `select`/`imply` of an exported symbol with `!<shared flag>`,
  otherwise it overrides the owner's value. kconfiglib warns about a select of an unmet bool.

**What a sharer sees.** All are dim comments, so they render as `*** ... ***`.
- Under the shared name: `Known shared buffers: $(first-nonempty,$(owner-names,buffer),none)`.
  The encoder has the same. This is evaluated at parse time, from what the siblings had saved.
- A known buffer: one block, padded with `$(pad,51,...)` so the `***` line up, with the owner's
  spring state as the choice label and each sensor as yes/no. The Fitted Sensors heading and
  its blank line are hidden.
- An unknown buffer name: "No other unit owns this buffer: enter its capabilities below".
- An unknown encoder name: "No other unit owns this encoder: define it in your own config".

**Staleness.**
- Exports come only from owners, never another sharer, so one refresh converges.
- `kconfig_needs_update` also marks a sharer stale when its saved exports differ from those of
  the owner its saved name names (`python -m shared_components stale`). install.sh runs a second
  stale-only unit pass, for an owner configured after its sharer.

**Warnings.**
- W29/W30: a blank shared name.
- W33/W34: an owner name that is blank or another sibling owner's, via `@repeat` over
  `owner-name`. Two sections of one name merge in Klipper.
- W15/W16 also check a share that declares its sensors.
- There is deliberately no warning for an unknown shared name: it is legitimate for a private
  cfg.

**Unit rename/remove (`installer/unit_migration.py`).**
- `rewrite_kconfig` rewrites an owner's name only when it is exactly the file's old unit name.
  A default keeps its token, so siblings read the new name before olddefconfig runs. A shared
  name is never rewritten, and never goes in the manual-edit list.
- `check` doesn't refuse over shared names. `shared_name_warnings` prints a WARNING, before
  "Apply these unit changes?", for a sharer whose name a rename or removal changes or drops.
- `mmu_<encoder>_encoder_*` vars move by *encoder* name, and only for units that own an
  encoder: `component_names` → `install["encoders"]`, from `prepare`. A removed owner's are
  dropped.

**Refresh/Merge.** `[mmu_unit] encoder`/`buffer` are `KCONFIG_OWNED_OPTIONS` (`build.py`,
matched by section type), because the section headers they name always come from the template.

**Adding a shareable kind.** You need:
- a `KINDS` entry;
- the shared flag and the two-node name symbol;
- a FOUND helper and the known-names comment;
- the exports sourced in the component's Kconfig;
- the W-checks;
- the `!<shared flag>` guards on machine-type selects.

**Printer-level capabilities** use the same reader with the top-level config as the only
owner. `$(printer-flag,SYM)` returns the parent's saved value, or n without one
(`PRINTER_FLAGS`). A unit goes stale when the parent is newer, so no extra staleness check is
needed.

**Test harness.** It writes each unit's config to a scratch dir in order
(`cfg._render_multi_unit`) and keys its parse cache on `shared_components.context_key()`.
Everything a parse reads must be in that key: owners' names and exports, and the unit's own
saved flag and name. Otherwise a cached tree leaks between profiles.

## 14. The pickle (value transport)

`build.py::pre_parse_kconfig` reduces a full `Kconfig` parse to a tiny
pickleable dict — `values` (from `as_dict()`) plus `choices`
(`{choice_name: selected_member_name}` from `user_selection or selection`)
— written atomically (`.tmp` + `os.replace`) to
`out/<basename>.pickle`; `load_parsed_kconfig` reads it back into a
`ParsedKConfig` that implements the same accessor surface
(`get/getint/is_enabled/is_selected/as_dict`). Full-`Kconfig` pickling was
abandoned at >20 000 recursion depth. The Makefile's pickle rules
(`$(OUT)/<config name>.pickle` and its `_%` per-unit twin)
depend on the value file *and* `$(kconfig_sources)`, so either changing
re-pickles, and `make verify_pickle` runs
`lib/kconfiglib/test_kconfig_pickle_consistency.py` against each — that
script re-reads the raw file with *stock* kconfiglib semantics and compares,
so it catches `as_dict` regressions without importing HH code.

## 15. String & choice default-resolution semantics (cross-file)

Not an extension per se, but the resolution rules the fork implements (they
are why board files can override feature-file defaults):

- **Strings**: both the *value-computation* path (`Symbol.str_value`) and
  the *min-config write* path (`Symbol._str_default`) call
  `_node_ordered_string_default()`, which walks `self.nodes` in
  **source parse order** and returns the **first** default whose condition is
  satisfied — plain `default` and `generated_default` entries interleave in
  that order. First-satisfied wins, NOT last-wins (unlike stock C kconfig's
  later-override).
- **Enclosing conditions are ANDed in**: the parser stores the explicit
  `default <val> if <cond>` expression, then `_propagate_deps` ANDs the
  node's own `depends on` and every enclosing `if`/`menu`/`choice`
  dependency into each `default` and `generated_default` condition. A plain
  `default` inside an `if BOARD_TYPE_X` block in a re-declaring file is
  therefore already scoped to that board; there is no need to repeat the
  condition.
- **Parse order decides**: the per-unit branch of `installer/Kconfig` sources
  `mmu_types/Kconfig`, then `boards/Kconfig`, *before* `Kconfig.mmu_additions`
  (and the per-topic feature files it pulls in) — so a type or board file's
  satisfied default beats a later feature-file `default ""`. Exception:
  `Kconfig.capabilities` (with `Kconfig.selector_type`, `Kconfig.bypass`,
  `Kconfig.filament_buffer`) and, in some types, `Kconfig.num_gates` and
  `servos/Kconfig` are sourced from inside the type files, so they precede
  `boards/Kconfig` and a board default for one of their symbols loses to a
  satisfied default in the selected type's copy.
- **Choices**: selection from defaults is first-satisfied in the order the
  `default <member>` lines are parsed (`Choice._selection_from_defaults`;
  member visibility is also checked). Only members of
  the *same* choice (same file) can be defaulted; steering an existing
  choice from another file means adding a `default <member> if <cond>` line
  above the older ones.

## 16. `default_when_hidden`

kconfiglib writes a symbol whose prompt is hidden with its *default* value,
but as a plain assignment if the user had set it, so on reload that frozen
default counts as a user value (NOT DEFAULT, stops tracking default changes).
`default_when_hidden` on a `menuconfig`, `menu` or symbol opts the symbols
under it into writing the #~DEFAULT~# token in that case
(`_saved_as_default()`, used by the write-side token check). Symbols without
a prompt, and visible ones, are unaffected. `Kconfig.leds` uses it on
`BOOL_CUSTOMIZE_LED_EFFECTS`, so clearing "Customize LED colors and effects?"
reverts the colors and effects to the per-type defaults. Within one menuconfig session a
re-ticked entry still shows the old values; the reset happens on save.

## 17. `validator "<regexp>"`

Optional property on a STRING symbol (`_T_VALIDATOR`; parsed in
`_parse_props`, stored compiled on `Symbol.validator`, so value-only
redeclarations in type/board files keep it). menuconfig's `_check_valid`
rejects an edit unless the stripped value matches it in full — per element for an
`array_editor` symbol (an empty array has no elements; an empty plain string
is checked like any other value). The error dialog names the value (and
element) and says "not valid syntax -- see help" -- the regexp is never shown,
so the symbol's help must document the format -- and the edit dialog reopens
with the rejected text. Only
edits are checked: loading, olddefconfig and the build never validate, so an
existing saved value keeps working.

- An invalid regexp is a parse error.
- Kconfig string lexing drops backslashes (`\\d` → `\d`) and `$(` starts a
  macro, so prefer `[0-9]`, `[(]`, `[.]`. A `:=` variable expanded inside the
  quotes is not re-scanned, which is how `Kconfig.leds` shares `led_rgb` /
  `led_effect` across the LED color and effect symbols.
- Pins: the root `installer/Kconfig` defines `pin_validator`
  (`[^|~] [!] [chip_name:]pin_name`, Klipper's `parse_pin` order; empty is
  valid) and `pin_help`. `pin_help`'s example (`hh-pin-example`) uses
  `[[VALUE:UNIT_NAME]]` (item 10) so it shows the unit's live name, except on
  the multi-unit entry screen, which has no unit. Every prompted `PIN_*` node carries
  `validator "$(pin_validator)"`, and gets `$(pin_help)` as its help when it has
  none. Add both to any new pin prompt; `TestPinValidator` fails otherwise and
  also checks every shipped pin default and every profile's pin values.
- `test/installer/test_kconfig_validator.py` checks every shipped LED default
  against its validator — extend it when you add a validator to a symbol
  whose defaults vary by type.

## 18. Macros in help text

`_parse_help` runs `_expand_whole` on a help text containing `$(`, after the
indentation is stripped, so a preprocessor variable can supply shared help
(`$(pin_help)`). The `hh-newline` function (`nl := $(hh-newline)` in the root
Kconfig) gives a real newline, so one variable can hold several lines:
`pin_help := $(pin_syntax)$(nl)$(pin_example)`. `hh-multiline` is not a
substitute: it writes a literal `\n` for Klipper values. Assignment strips
leading spaces, so a line can't start indented.

## 19. Macros in named choices

`$(macro)` references expand in a `choice <NAME>` line, not just in a
`config` name (the Happy Hare branch for `_T_CHOICE` in `_tokenize`). This is what lets a
shared fragment under `installer/components/` declare its own choices while
being sourced once per consumer:

```
prefix := GEAR
source "components/Kconfig.tmc_driver_types"
```

Preprocessor variables are global for the whole parse, so re-assign every
one immediately before each `source`. `test/installer/test_kconfig_macro_names.py`
covers the tokenizer change; `test_kconfig_structure.py` checks the generated
names against the prefix contract.

## 20. `HH_RENAMED_SYMBOLS`

Module-level dict in `kconfiglib.py` (old symbol name → new name), applied by
`Kconfig._migrate_renamed_symbols` at the end of `load_config(replace=True)`,
so every caller carries a saved value across a rename. See SKILL.md
("Renaming a symbol discards the user's value…") for the rules and its one
limit: a symbol `make` or `install.sh` reads by name cannot be renamed this way.

## 21. `sequence_editor <separator> [baseline]` / `append_only_unless <expr>`

An ordered list of **unique names** whose identity matters, edited as a list
that knows what happened to each entry. `MMU_UNITS` is the only use:

```
config MMU_UNITS
  prompt "MMU units"
  sequence_editor "," "$(env-default,F_UNITS_BASELINE,)"
  append_only_unless "$(env-default,F_UNITS_RESTRUCTURE,n)"
  validator "[a-z][a-z0-9_-]*"
```

- **Not a type.** The symbol stays `string` and its value a plain
  separator-joined list, so `make` and `install.sh` read it unchanged. The
  attributes only pick a different dialog (`_change_node` → `_sequence_dialog`),
  the way `array_editor` does. An edit is saved as `a, b`
  (`sequence_edit.join_sequence`, shared with the array editor); every reader
  strips the names. Closing the dialog with the names unchanged leaves the saved
  value as it was, so a differently spaced old value isn't a pending change.
- **Parsing** (the `_T_SEQUENCE_EDITOR` / `_T_APPEND_ONLY_UNLESS` branches of
  `_parse_props`): only the first argument is lexed as a string
  (`_STRING_LEX`). A quoted second argument arrives as a *constant symbol*
  and is unwrapped to its name, so the baseline must be quoted.
  `append_only_unless` takes an expression, so a quoted `"$(env…)"` constant
  works and no extra symbol is written to `.mmu_config`. Stored on the Symbol
  as `sequence_editor`, `sequence_baseline` and `append_only_unless`.
- **Baseline:** what renames/removals/moves are measured against. An empty
  baseline means the current value.
- **The model** is `installer/lib/kconfiglib/sequence_edit.py`, with no curses,
  so it is shared with `installer/unit_migration.py` and tested directly.
  `SequenceModel` rows are `[name, origin]`, where origin is the baseline
  name it came from or `None` for new. Removed = baseline names no longer
  used as an origin. `structural()` = anything other than appending new
  entries (an insert mid-list is structural). Adding a removed baseline name
  restores it instead of creating a new entry.
- **Validation lives in the model, not `_check_valid`:** entries are unique
  and at least one must remain (the last can't be deleted). `validator`
  applies only to new or renamed names, so existing names are never
  re-validated. `array_editor`'s duplicate/empty behaviour is untouched.
- **Append-only** (while the expression is n): the rows up to the last one
  that came from the baseline, plus the removed set, may not change
  (`_fixed`). So appending still works after a pending structural change,
  and `u` (reset) is always allowed. The refusal says "see help", so the
  symbol's help must explain how to unlock it.
- **The sidecar** is how identity survives. On menuconfig save
  (`_write_config`, which wraps `write_config`), each symbol *edited this
  session* is written to `<config>.<SYMBOL>.changes` as JSON
  `{from, to, origin}`, or the file is removed when nothing changed. It is
  trusted (`origins_from_changes`) only if `from` equals the baseline and `to`
  equals the current value; otherwise origins fall back to matching by name.
  olddefconfig and the build never touch it. The fork knows nothing about
  units; `installer/unit_migration.py` is what gives the sidecar meaning.
- **Dialog:** rows `> 2. box  (renamed from b)` (the selected row gets `>` and
  bold on the list background), `[new]`, and `x  c  [will be removed]` for
  removed entries (`d` on one restores it). A one-line summary goes below.
  Keys: `a` add at end, `i` insert before, `r` rename, `d` delete/restore,
  `K`/`J`, `-`/`+` or Shift-Up/Down to move, `u` undo all, `?` help,
  Enter/Ctrl-D done, ESC cancel. Changing only origins (e.g. a swap by rename
  instead of by move) still marks the config changed.
- **Shift-arrows:** these arrive as `KEY_SR`/`KEY_SF`, as ncurses extended
  keys (`kUP*`/`kDN*`), or, when the terminfo doesn't know them
  (e.g. `screen-256color`), as a raw `ESC [1;2A`. Because `ESCDELAY` is 0 the
  bytes can arrive split (e.g. over SSH), so after an ESC
  `_read_escape_sequence` waits up to `_ESCAPE_SEQUENCE_WAIT_MS` per byte,
  reads to the sequence's final byte, and maps it through
  `_SEQUENCE_ESCAPE_KEYS`. An unknown sequence is ignored; only a lone ESC
  cancels. macOS Terminal.app keeps Shift-Up/Down for scrolling, so they never
  arrive there.
- **Tests:** `test/installer/test_sequence_edit.py` (model, sidecar);
  `TestSequenceEditor` in `test/installer/test_menuconfig.py` (parsing, dialog
  keys driven by stubbing `_getch_compat`, drawing against `FakeDialogWindow`);
  `TestMmuUnitsValidator` in `test_kconfig_validator.py`. For a real check,
  drive `make menuconfig` in a pty (`pty.fork`, `TERM=xterm-256color`).

## 22. `reparse_env "<VAR> [<VAR>...]"`

For a symbol whose value the tree is *built from* through environment
variables, which are expanded at parse time (`UNIT_NAME` → `$(UNIT_NAME)` and
`$(MCU_NAME)` in ~850 defaults: pins, LED chains, NFC readers, `[mcu …]`).
Changing such a symbol in menuconfig can't update those defaults, so
`_set_val` calls `_reparse`, which:

1. writes the current values to a temporary config (`write_config`);
2. sets each named variable to the new value;
3. builds a fresh `Kconfig` with output suppressed at the fd level, since
   `$(shell …)` would otherwise write onto the curses screen;
4. loads the temporary config with `filter_defaults=True`, so `#~DEFAULT~#`
   values are recomputed while explicit values are kept;
5. swaps `_kconf` and re-points `_cur_menu`/`_shown`/`_sel_node_i`.

Nodes are matched by `_node_key` (file, line, item, prompt), which a re-parse
of unchanged Kconfig files doesn't change. A failed parse restores the
environment and keeps the old tree. It costs one menuconfig start-up and shows
an "Updating configuration…" box.

- Only values saved as defaults are recomputed: names on the `#~DEFAULT~#`
  prefix list, which is every unit-derived prompted symbol in this tree.
  An explicit value containing the old name stays as typed, and the installer
  rewrites it (`unit_migration._rename_single_unit`).
- `UNIT_NAME` uses `reparse_env "UNIT_NAME MCU_NAME"`. After a re-parse the
  saved `MCU_NAME` already follows the rename, so install.sh records the name
  before menuconfig (`F_UNIT_NAME_BEFORE`) to know a rename happened.
- **The prompt is locked like `MMU_UNITS`:** `prompt "Klipper object name" if
  !MULTI_UNIT && "$(env-default,F_UNITS_RESTRUCTURE,n)" = "y"` (Replace mode or
  a first install), with a read-only comment otherwise. Hiding it is safe only
  because the hidden symbol's default (`$(UNIT_NAME)`) is built by the Makefile
  from the saved `CONFIG_UNIT_NAME`. A locked symbol whose default didn't
  round-trip its value would lose it (SKILL.md pitfall 2).
- **Adding another `reparse_env` symbol:**
  - every prompted default derived from those variables needs a name on the
    `#~DEFAULT~#` prefix list, or the re-parse keeps its stale value;
  - whatever launches menuconfig (Makefile/install.sh) must pass the same
    variables from the saved value, or the next parse goes back to the old one;
  - keep the fork generic: it only knows which variables mirror the symbol.
    Meaning (renaming files, saved state) belongs in the installer.
- **Tests:** `TestReparseEnv` in `test/installer/test_menuconfig.py`. A fixture's
  derived symbols must use `PARAM_`/`PIN_` names for their defaults to update.
- **Checking it in a real terminal:** drive `make menuconfig` with `pty.fork`
  (`TERM=xterm-256color`, and `F_UNITS_RESTRUCTURE=y` so the prompt is
  editable outside install.sh).
  - **Finding the row:** a single-unit menu marks the selection through the
    help pane, not with `>`, so press `j` until the row's help text appears.
  - **Confirming the row:** curses redraws only what changed, so press Enter
    and look for the input dialog.
  - **Checking the result:** after saving, `CONFIG_MCU_NAME` equal to the new
    name proves the re-parse ran, and the `#~DEFAULT~#` pin lines should carry
    the new prefix.

## 23. Macros that expand to an expression

Upstream lexes a `$(macro)` in a symbol position as part of ONE name, however
it expands, so `if $(cond)` with `cond := A && !B` tested a junk symbol named
`A && !B`: always n, with no error. The Happy Hare branch after `_expand_name`
in `_tokenize` re-lexes the expansion in place, as the expression it spells,
when it holds whitespace or starts with `!` or `(` (`_expanded_expr_match`).
Write a condition with spaces around its operators.

Anything else stays one symbol name, exactly as upstream. That matters: an
undefined symbol's value is its own name, and a choice member named from a
device path (`$(mmu_serial_config,...)`, only `-` replaced) can hold `:`, `=`
or `+` and is both declared and defaulted to through the macro. No existing
macro expands to whitespace or a leading `!`/`(`, so nothing that parsed before
changes meaning.

This is what lets `components/Kconfig.servo` take its prompt condition from the
caller (`servo_visible`) instead of being sourced inside the caller's `if`.

- `test_kconfig_macro_names.py` (`TestMacroExpandingToAnExpression`) covers the
  tokenizer, including a device-style name; `test_kconfig_structure.py` fails
  on any symbol name with spaces or operators in the shipped (harness) tree,
  which is what catches `A&&B` written without spaces.
- An `if` block *between* a menu toggle and the prompts it should nest still
  ends kconfiglib's automatic submenu (`_finalize_node` only nests consecutive
  siblings that depend on the toggle). A prompt condition doesn't, which is the
  other reason to pass a condition rather than wrap the `source`.
