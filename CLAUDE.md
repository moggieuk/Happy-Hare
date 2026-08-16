# Happy Hare

A Klipper plugin for multi-material-unit (MMU) filament handling. This file
is the always-loaded orientation; deeper subsystem knowledge lives in
`.claude/skills/` and loads only when relevant — see the end of this file.

## Testing — read `test/README.md`, don't duplicate it here

```bash
make test
```

That's the whole setup — first run builds a venv, then opens a file picker
(everything ticked by default). `test/README.md` is a thorough, up-to-date
guide to the fake-Klipper harness, the interactive console (`make console`),
what's covered vs. not, and "six things that will bite you" — read it before
writing a new test rather than guessing at the harness's conventions from
first principles.

The one thing worth restating here: if you fix a bug, write a test that
fails before your fix and passes after. If you find a bug you're not fixing
now, write it as `@unittest.expectedFailure` with a comment — it documents
the bug and self-heals (goes red) the moment someone actually fixes it.

## Kconfig / menuconfig

Machine config is generated from `installer/Kconfig*` via `menuconfig`:

```bash
make menuconfig      # interactive config editor
make variables        # print which interpreter each half (test/installer) settled on
```

`KCONFIG_CONFIG` defaults to `.mmu_config` (gitignored, per-checkout). If
you add or change a Kconfig option, check whether `config/` templates or
`test/hh/profiles.py` need a matching update — profile tests
(`test_mmu_profiles.py`, `test_mmu_config.py`) render the real shipped
templates and will catch a mismatch, but only if a profile actually
exercises the option you touched.

## Contribution norms (see `.github/CONTRIBUTING.md` for the full text)

- **Changes that break existing setups will probably be rejected.** This is
  the single most important filter for any change to shipped behavior —
  favor additive config options over changing defaults, and favor a
  validator/warning over silently reinterpreting an existing parameter.
- Feature/behavior changes need positive feedback collected informally
  before a GitHub issue is opened; GitHub issues are for bugs and accepted
  feature requests, not proposals.
- Setup-specific questions belong in Discord, not GitHub issues.

## AI-assisted commits

A large fraction of recent history is Claude-assisted with attribution
(`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` /
`Claude Sonnet 5 <noreply@anthropic.com>`) — keep that convention on commits
where it applies rather than omitting it.

## Subsystem knowledge (loads automatically when relevant)

- **`gate-endstop-invariants`** — the shared-gate endstop occupancy rule and
  `gate_parking_distance` validation. Relevant any time you're touching gate
  homing, endstops, crossload logic, or `mmu_filament_movement.py`.
- **`nfc-rfid-subsystem`** — reader driver architecture, `jog_scan`, reader-
  pair sharing, and a preserved write-up of unmerged RF-crosstalk mitigation
  design work. Relevant any time you're touching NFC/RFID code or debugging
  tag misattribution.

These aren't a substitute for reading the code — they exist to save you from
rediscovering invariants and design decisions that already cost someone a
debugging session once.
