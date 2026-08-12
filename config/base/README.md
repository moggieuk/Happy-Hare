# config/base/

This directory holds the core Happy Hare config templates that are rendered
(Kconfig/Jinja substitution, then merged with the user's existing values) at
build/install time:

- `mmu.cfg`
- `mmu_hardware.cfg`
- `mmu_parameters.cfg`
- `mmu_macro_vars.cfg`

## Zero-length placeholder files

The other files in this directory (e.g. `mmu_cut_tip.cfg`, `mmu_form_tip.cfg`,
`mmu_sequence.cfg`, ...) are **intentionally empty**. They are not config
content — they're stubs.

The macros they're named after used to live in `config/base/` but have since
moved to the parallel [`config/macros/`](../macros/) directory. Some existing
installs still have a leftover reference (an include or symlink) resolving to
these filenames under `mmu/base/`. Without a file at that path, Klipper fails
to load on upgrade. These empty stubs exist purely so that path still
resolves to *something*, avoiding a startup error for anyone upgrading from
an older layout.

They are not meant to be installed on a fresh setup, and the build tooling
(`Makefile`'s `repo_cfgs`) explicitly filters out zero-length `.cfg` files so
a new install never creates config or symlinks for them. Don't add real
content to these files — if a macro belongs in `config/base/`, give it a
real, non-empty file instead.
