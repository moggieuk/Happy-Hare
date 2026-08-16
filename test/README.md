# Happy Hare test harness

**What it is:** a fake Klipper and a fake Moonraker, good enough that the *real* Happy
Hare code runs inside them. You can load a config, boot the MMU, push filament through
gates, present RFID tags and talk to Spoolman — on your laptop, with no printer, no
Klipper installed, and no hardware.

**Why it exists:** the NFC/RFID → Spoolman feature and its LED work were built across five
sessions and, per their own handoff notes, had *never been executed* — only checked for
syntax. Running it for the first time found seven real bugs. That is what this is for.

It has since grown well past NFC. Tool changes, gate homing (both kinds), endless spool and
runout now run here too — see the coverage map in §2 for what is and isn't covered.

**Who this is for:** you're comfortable with Happy Hare and Klipper concepts but new to
Python. Python-specific things are explained as they come up.

---

## 1. Running the tests

From the repo root:

```bash
make test
```

That is the whole setup. On a fresh clone the first run takes a few extra seconds to build
itself an environment, then goes straight into the tests.

<details>
<summary>What that first run is doing, and how to steer it</summary>

The tests need two libraries Happy Hare itself doesn't (`greenlet`, `jinja2`), so they run
in a **virtualenv** — a private Python install that lives in the repo directory but is
*not* part of the git repo, and is never installed onto a printer. `make test` creates it
at `venv/` and installs `test/requirements.txt` into it if it isn't already there.

**On a printer there is usually nothing to create.** Klipper's own `~/klippy-env` already
contains greenlet and Jinja2 — it requires both — which is the whole of
`test/requirements.txt`, so `make test` and `make console` use it directly and say so. That
matters most on Debian/Raspberry Pi OS, where `ensurepip` ships in the separate
`python3-venv` package and `python3 -m venv` would otherwise produce a venv with no pip.
Point `KLIPPY_ENV=` elsewhere if yours is not at `~/klippy-env`, or at a path that does not
exist to force the venv route.

Git ignores `venv/` (Python's `venv` module writes an ignore rule into it), so it will
never show up in `git status` or a commit.

The tests are not its only tenant. On a system whose Python refuses to install anything
outside a virtualenv (PEP 668 "externally managed" — Homebrew, Debian Bookworm), and where
Klipper's own `klippy-env` isn't there to be used instead, the installer needs it too — it
cannot render a config without `jinja2`. So `./install.sh`, and equally a bare `make build`,
`make verify_pickle` or any other goal that runs the builder, will create this venv and use
it. Those install only `installer/requirements.txt`, tracked by its own stamp file, so the
two sets never invalidate each other and the installer never pays for `greenlet`.

`make variables` prints which interpreter each half settled on, if you ever wonder.

It is only built once. Later runs reuse it and go straight to the tests; editing
`test/requirements.txt` reinstalls automatically. Some knobs:

```bash
make venv                       # build the venv, don't run anything
make clean_venv                 # throw it away (`make clean` deliberately does not)
make VENV=/somewhere/else test  # put the venv somewhere other than ./venv
make KLIPPY_ENV=/nonexistent test  # ignore klipper's env, build the venv instead
make NO_VENV=1 test             # don't use a venv at all (see below)
make PY=/usr/bin/python3 test   # ditto, against a named interpreter
```

If venv creation fails, the error says what to do — on Debian/Ubuntu it is usually
`sudo apt install python3-venv`.

`NO_VENV=1` runs the tests against whatever `python` is already on your PATH, and expects
you to have `greenlet` and `jinja2` installed there yourself. **You almost certainly don't
want it.** It exists for the two cases where a venv is the wrong tool: CI, which already
runs in a throwaway environment with the dependencies installed, and a machine where
`python -m venv` doesn't work at all and can't be made to.

In particular, it is *not* the way to test against a different Python version — that still
needs the dependencies installed for that interpreter, which is exactly what a venv is for.
Point the venv at the other interpreter instead:

```bash
make BOOTSTRAP_PY=python3.9 VENV=venv39 test
```

</details>

`make test` offers everything — currently **862 tests, about 1m40s** on a warm laptop.
Expect to see:

```
OK (skipped=1, expected failures=4)
```

`skipped` and `expected failures` are normal and explained in §6. Anything else — `FAILED
(failures=…)` or `(errors=…)` — is a genuine problem.

A minute and a half is still too long to sit through on every change, which is why `make
test` opens a file picker first rather than starting straight away.

### Running less than everything

`make test` opens a picker first. Everything starts ticked, so pressing Enter runs the whole
suite exactly as it always did — but untick the expensive files and you get a focused run:

```
Happy Hare tests - 862 tests in 27 files        times from last run (~ = reference, never run locally)

   1 [x] installer.test_build           1     0.0s
   2 [x] test_mmu_adc_compat           14     0.0s
   3 [x] test_mmu_bootup               34     2.3s
   …
   7 [x] test_mmu_console             176      37s
   …
  15 [x] test_mmu_nfc                  17     9.7s
   …
  27 [x] test_mmu_toolchange           20     0.9s

  selected: 27 files - 862 tests - ~1m42s last time

  [Enter] run    1 3 5-8 toggle    a all    n none    v invert
  +TEXT / -TEXT tick by name    p previous selection    s sort by time    q quit
>
```

The right-hand column is how long each file took **on your machine, last run**, and it is the
column to look at when deciding what to drop, because the cost is wildly uneven. From a real
full run: `test_mmu_console` took 37 s for 176 tests and `test_mmu_nfc` 9.7 s for 17, while
several files — including `test_mmu_tag_parser`'s 36 tests — came in under a tenth of a second
between them. A handful of the twenty-seven files account for most of the run. The times
fill in after your first run, `s` sorts by them, and the footer estimates what the current
selection will cost.

Never run the suite on this machine at all? The picker still isn't guessing: `test/benchmark.json`
ships a checked-in reference measurement, so every file you haven't personally timed yet shows
that number instead, marked with a trailing `~` (header and footer say so too, e.g. "reference
times only - never run locally"). Run any file for real and its row switches to your own number
immediately — reference and local times can be mixed in the same screen, one row at a time.

Two things to know about the numbers. They cover each file's **class fixtures** as well as its
tests, which is where nearly all the time actually is — `setUpClass` building a printer, not
the assertions. And part of that cost is shared and cached across a *run* (`test/hh/cfg.py`
caches Kconfig parsing per profile, and the fake `gcode_macro.py` caches compiled Jinja macro
templates), so a file run on its own can still cost a little more than the same file inside a
full run — `test_mmu_config` is 6.7 s in a full run and 7.5 s alone, the gap being the one
Kconfig parse and macro compile nothing earlier in that solo run had already warmed. That gap
used to be nearly two orders of magnitude wider (0.1 s vs 35 s) before those two caches existed —
most of the profile-parsing and macro-compiling cost used to be paid fresh on every single boot,
not just the first one in a run. Treat the column as a guide to relative cost within a run, not
an absolute per-file price.

Typing `n` then `+nfc` then Enter runs just the NFC files. `p` recalls the last selection you
narrowed to — a full run doesn't overwrite it, so you can alternate between a focused loop
and a full check without retyping. `q` quits without running anything, and exits non-zero on
purpose so `make test && git commit` can't sail past it — you will see make print
`*** [test] Error 1` after a quit, which is expected.

The picker is skipped, and everything runs, whenever it can't work or you didn't ask for it:

```bash
make ALL=1 test                 # no picker, run everything
make LAST=1 test                # no picker, re-run the last selection
make UT='test_mmu_nfc*.py' test # no picker, filename pattern (as before)
make test | tee log             # no picker — not a terminal, so it just runs
make test ARGS='-k homing'      # extra unittest flags, picker still opens
```

A file that fails to import also skips the picker: it can't be listed, so the run goes ahead
and fails loudly rather than quietly leaving it out.

The picker only deals in whole files. For one class or one test, go straight to the runner —
`-m unittest` means "run Python's built-in test runner", and the argument is a **dotted module
path**, so `test/test_mmu_leds.py` becomes `test.test_mmu_leds`:

```bash
# one file
./venv/bin/python -m unittest test.test_mmu_leds

# one class within a file
./venv/bin/python -m unittest test.test_mmu_leds.TestPendingOverlay

# one single test
./venv/bin/python -m unittest test.test_mmu_leds.TestPendingOverlay.test_cancel_clears_the_overlay

# -v prints each test name and its docstring as it runs
./venv/bin/python -m unittest -v test.test_mmu_motion
```

Your selection and the timings live in `.mmu_test_state` at the repo root. It's gitignored,
and deleting it just means the picker falls all the way back to `test/benchmark.json` — the
checked-in reference numbers described above, regenerated by hand from an occasional full run
(see that file's own comment) rather than by CI, since none runs `make test` today.

---

## 1a. The interactive console

Everything the tests drive, driven by hand instead — Mainsail's console pane, against the
simulation:

```bash
make console
```

```
> MMU_CHANGE_TOOL TOOL=1
Tool change requested: T1
...
------------------------------------------------------------------------
T1   gate 1    LOADED IN NOZZLE           868.0mm  Idle
  print=initialized  SYNCED  t=+2.51s  realtime=100%
  mmu_entry_1=1  mmu_exit_1=1  filament_compression=1  filament_tension=1  ...
            nfc pre ent exit/gate shex enc comp/extr nozl
   gate 0    ..  ..  ..     ..     ..   ..     ..     ..     -100.0
  *gate 1    ##  ##  ##     ##     ##   ##     ##     ##     +768.0
------------------------------------------------------------------------
```

Anything not starting with `/` is sent to the MMU as G-code. `/help` lists the
meta-commands, `MMU_HELP` lists Happy Hare's, and every HH command takes `HELP=1`.

The prompt is a bare `> ` — the tool, the gate and the paused state are all on the first
line of the status section, so repeating them would only cost columns.

The status section is separated from the log window by a **heavy rule**.

When something scribbles on the terminal, **`/redraw`** puts it all back: it clears the
screen, re-reserves the pinned band, redraws the status section, repaints the log from the
scrollback, and resets autowrap and the cursor. `/clear` does exactly the same but throws
the log away instead of repainting it. Useful meta-commands beyond `/help`:

| Command | Does |
|---|---|
| `/advance N` | jump the virtual clock forward N seconds |
| `/live [on\|off]` | run the clock while you sit at the prompt; **on by default** at a terminal, off is the reproducible mode |
| `/vars [mmu\|machine]` | `get_status()` of the `mmu` object, the `mmu_machine` object, or both |
| `/redraw` | repaint the whole screen, log and all — the way back from a corrupted display |
| `/clear` | as `/redraw`, but empty the log rather than repaint it |
| `/scroll [N]`, `/s` | scroll back through the log (see below) |
| `/sensor NAME on\|off\|enable\|disable` | `on/off` drives the switch through its real button callback; `enable/disable` flips `sensor_enabled` so Happy Hare treats it as **not fitted** |
| `/place`, `/preload`, `/exhaust` | set the scene: filament at a gate, preloaded, or run out |
| `/log [N]`, `/trace 0-4` | the log file, and how much detail goes into it |
| `/timestamp [on\|off]` | stamp MMU output with the virtual clock; **on by default** at a terminal |

### Multi-unit

Multi-unit configs work, and `ercf_vvd` — **the console default** — is one: a real two-unit
machine, ERCF 1.1sb (9 gates, `LinearServoSelector`, encoder) plus ViViD 1.0 (4 gates,
`IndexedSelector`), 13 gates in total. You can also point `--profile` at a multi-unit install
directory. Either way the harness builds every unit, with gates numbered contiguously across
them (here `unit0` 0-8, `unit1` 9-12). Sensors
are qualified per unit (`unit0:mmu_shared_exit`), so the header keeps the prefix and `/sensor`
needs the qualified name — a bare name that matches more than one unit is rejected rather
than silently resolved. The filament view groups gates under their unit, and the LED view
shows every unit rather than only the selected one.

**The clock is virtual.** At a terminal it is also **live**: it runs at wall speed while you
sit at the prompt, so timers fire on their own — the 8-second boot LED rainbow finishes, the
20-second pending-spool timeout expires, and on an NFC machine the poll loop keeps turning
without being asked. `/advance N` jumps it forward by N seconds whether live or not.

`/live off` freezes it, which is the **reproducible** mode: with the clock stopped the same
commands always produce the same transcript however long you took over them, which is what
you want when you are pinning down a specific sequence. `--no-live` starts that way. Live is
off automatically for `--script` and anything that is not a terminal, so command files stay
deterministic.

<details>
<summary>How the live clock works, and why it is a signal rather than a thread</summary>

A `setitimer` handler, armed only around `input()` and disarmed for the whole of a dispatch.

Not a background thread, and that is not a preference: the reactor is greenlet-based —
which is what gives it Klipper-faithful `pause()` and completion semantics — and greenlets
belong to the thread that created them. Pumping the reactor from a worker thread fails
outright with `greenlet.error: Cannot switch to a different thread`. A signal handler runs
on the **main** thread, so the greenlets stay consistent, and it does fire while blocked
inside readline's `input()`.

Arming only around `input()` is what keeps a tick out of a dispatch, where `advance()`
asserts on re-entry and where the scrollback tee could be caught halfway through
reassembling a line. Output produced by a tick is printed above the prompt and the prompt is
then rebuilt from `readline.get_line_buffer()`, so a tick landing while you are mid-command
cannot eat what you have typed.

The tick fires every `LIVE_INTERVAL` = **0.5 s** and advances by the *real* time measured
since the last one, not by that constant — so the interval sets how often the header is
repainted, not how fast the clock runs. Halving it (it was 1.0 s) therefore costs a second
repaint per second, not twice the reactor work, and it is what makes an `led_effect`
animation legible: at `frame_rate: 24` a one-second sample showed every 24th frame, which
reads as a jump rather than a fade.

The clock itself costs under **1% of one core**: measured on `ercf_vvd`, `advance(60)` is
8.6 ms of CPU per virtual second, and live mode spends that over a real second. (It was
7.7 ms before unit0 gained its entry/status/logo segments — 16 more LEDs to animate.)
Catching up is the expensive direction — an hour compressed into one call is ~30 s of CPU —
which is why a tick is capped at a few seconds rather than jumping to "now" after the
machine has slept.

`/advance` is sliced for the same reason. One `advance()` call has an iteration cap and the
LED effects animate at 24 fps, so on the default profile a single call dies partway through
the seventh virtual minute — `/advance 600` used to stop at 444 s and raise. The counter
resets per call, so the span is fed in 60-second slices; timers fire in the same order.

</details>

`/timestamp` shows that clock, dimmed, against each MMU reply — the time the simulator
started plus however far the reactor has been advanced since, so `/advance 3725` really does
move it an hour and two minutes while however long you spent reading moves it not at all.
Seconds are shown because the virtual clock usually moves in fractions of one: at minute
resolution a whole session reads as a single instant. Only the first line of a reply is
stamped; the rest are indented to line up under it:

```
> MMU_SENSORS
22:45:16 filament_compression  --> Open
         filament_tension      --> TRIGGERED
         mmu_entry_0           --> Open
> /advance 3725
> MMU_SENSORS
23:47:21 filament_compression  --> Open
```

Useful flags — `make console ARGS='...'`:

```bash
--profile boxturtle            # or tradrack, emu, encoder, nfc_single, nfc_spoolman, ...
                               # (default is ercf_vvd, a real 2-unit machine)
--profile /path/to/config      # your own installed config - see below
--header machine,sensors,filament,selector,gates,leds   # or 'all' / 'off'
--inline-header                # reprint above each prompt instead of pinning it
--scrollback 5000              # lines kept for /scroll; 0 disables it
--no-live                      # freeze the clock (default: live at a terminal)
--no-timestamp                 # no clock in the output (default: on at a terminal)
--color 256|truecolor|16|auto  # colour depth (see below)
--log-dir /tmp                 # where mmu.log goes; --no-log to discard it
--trace 4                      # full Happy Hare narration
--no-preload                   # leave every gate empty
--no-calibrate                 # boot cold: no seeded calibration, no homing, no preload
--no-prime                     # leave the gate map blank instead of filling it in
--seed N                       # seed for the primed gate map (default 0, reproducible)
--no-moonraker                 # don't attach the fake Moonraker/Spoolman
--pace FACTOR                  # 0=instant, 0.5=twice as fast as real (default), 1=real time
--wall / --no-wall             # with --pace, whether to sleep in real time (default: interactive only)
--script FILE                  # run non-interactively (this is how it is tested)
```

Startup shows Happy Hare's **real bootup output** — the welcome banner and the unit summary
— because `cmd_MMU_BOOTUP` runs here exactly as it does on a printer. Calibration is seeded
*inside* `boot()`, before bootup runs, so a default session boots clean; `--no-calibrate`
boots the machine cold and the calibration warnings then appear for real.

Two more things happen at startup that a printer does for itself and a frozen clock does not:

- **The gate map is primed** — every gate gets a vendor, material, colour and temperature, so
  the gate table and the LED `filament_color` effect have something to show instead of
  `Unknown | 200C | Unknown`. Seeded, so a session is reproducible; `--seed N` for a different
  spread, `--no-prime` for none.
- **`effect_initialized` is waited out.** It is a unit-wide 8s state flash from bootup, and
  while it holds a unit *every* transient flash is dropped (`mmu_led_manager.py:473`) — so an
  NFC read acknowledgment, for one, silently does nothing. `boot()` stops the clock 2.5s in, so
  without `Session.settle_leds()` an interactive session would never leave that window.
- **A fake Moonraker + Spoolman is attached**, seeded to agree with the primed gate map (gate
  N's tag UID is `BADCAFE<NN>`). The `MmuServer` inside it is *real*, so the round trip
  exercises the actual contract both ways. Without it every call Happy Hare makes to Moonraker
  goes unanswered and an NFC read ends in *"Automatic assignment of id timed out"* 20s later —
  which is what `--no-moonraker` gives you, and what a printer with Moonraker down looks like.

### Watching an operation happen — `/pace`

Moves complete instantly by default: an `MMU_LOAD` finishes without the virtual clock moving at
all. Fast, but nothing time-driven is observable — an LED effect never reaches a second frame,
and every action transition lands in the same instant.

`/pace FACTOR` spends that fraction of each move's *real* duration in virtual time: `0` is
instant, `0.5` twice as fast as real (the default), `1` roughly real time. While it is on, the
`machine` header carries `realtime=<n>%` next to the clock — that is the field it explains,
since `t=` only moves during an operation when pacing is on. Absent at `0`. Each move's duration
is already known — `MmuStepper._submit_move` computes the real trapezoid — so this is HH's own
arithmetic, not an invented number.

The pacer advances the reactor, which **runs timers** (a `pause()` would only jump the clock,
see `reactor._sys_pause`). That is the whole point. It cannot run inside a reactor callback, so
it no-ops there; top-level dispatch, which is what the console and the tests use, is where
pacing applies.

**Virtual time is free** — advancing the clock 11 seconds costs milliseconds — so pacing alone
makes an operation *report* the right timings while still finishing in an instant. To actually
watch one, it has to sleep, which it does at an interactive prompt and never in a script, a
pipe or the test suite (`--wall` / `--no-wall` to force it either way).

A paced move is **walked, not jumped** — sliced at `PACE_TICK` (50ms), with the filament model,
the clock, the pinned-header repaint and the sleep all advancing together. One `advance()`
followed by one `sleep()` would freeze for the whole move: no LED frames, no repaint, no
intermediate position. A single 13-gate load produces ~240 updates, and the totals stay exact —
paced and unpaced end with the filament in the same place.

Every kind of move is paced, not just the plain ones: homing moves never reach
`trapq_append`, so until `pace_move()` became reusable an unload spent all of its seconds
inside the one bowden move while every home-to-sensor step happened in the same instant.

**Tip forming and purging get a flat `Session.MACRO_DURATION` (4s at pace 1)** rather than a
distance/speed figure. Their bodies never run here, and what the harness models of tip forming
is only its *net* retraction — but the real macros spend their time ramming, cooling, dipping
and wiping over that span, so dividing the net distance by any one of the macro's own speeds
badly understates it (`unloading_speed_start` put the whole retract at 0.5s). A round number is
the honest answer for work that is deliberately not modelled.

Note the log still arrives in **blocks**, because Happy Hare only logs at operation-step
boundaries, not continuously. The header is what moves during a long move.

**One known consequence.** On `boxturtle` and `emu` — the shipped profiles with per-gate entry
sensors *and* `gate_autoload` — pacing makes them **preload twice**, and a subsequent load log a
spurious `Operation not possible. Filament is loaded`.

A *preload* is the operation that crosses the entry sensor (a load does not — the filament is
already past it), so it raises an insert event, and with `gate_autoload` set HH answers by
starting another preload. Happy Hare has the guard for exactly this
(`wrap_suspend_insert_events`, whose docstring describes it word for word) but applies it only on
the NFC-scan path. Unpaced it never surfaced, because the entry sensor's `event_delay` defers the
insert by 0.5s and no virtual time ever passed. Everything still completes correctly.

`/timestamp on` is what makes the pacing legible: output is stamped with the virtual clock as
of **when Happy Hare produced the line**, not when it was printed — `_drain()` runs after a
command returns, so stamping there gave every line of a load the same end-of-command reading.

```
> /pace 1
> MMU_LOAD
23:05:41 Loading filament...
23:05:41 [T9] ███◉█┈┈┈┈┈┈┈┈┈ ...  ▷▷▷    0.0mm
23:05:49 [T9] ███◉██████████ ...  ▷▷▷  680.0mm      <- the bowden move took 8s
23:05:53 [T9] ███◉██████████ ... LOADED 801.8mm
```

The **gate map is seeded the same way**, and for the same reason: bootup prints the gate
table, `_preload_all()` runs after `boot()` returns, and that table is the last thing on
screen when the prompt appears — so it used to report the whole machine unknown about one
that is fully loaded. `Session.seed_loaded_gates()` places filament at every gate and
persists `mmu_state_gate_status` before `klippy:ready`, which is exactly the state a real
printer restores from `mmu_vars.cfg`. Both halves are needed: the persisted map is the only
source for a unit with no per-gate switches (ERCF), and it is not enough for one that has
them (ViViD re-derives its gates from `mmu_entry_9..12` at bootup and would overwrite a
seeded map with `GATE_EMPTY`). `--no-preload` and `--no-calibrate` skip it, so a cold start
is still a cold start.

Lines the **console itself** adds are dimmed and prefixed `#`, so there is never a question
about which of them came off the MMU:

```
(")_(") Happy Hare v4.0.0 Ready...          <- Happy Hare, exactly as on a printer
Unit : ------------- unit0 -------------
...
# Happy Hare console  profile=ercf_vvd  gates=13     <- the simulator
# All 13 gates preloaded, extruder at 220 C.
# Log: /tmp/mmu.log
```

`#` and not `!`: `!! …` is already a command that raised and `?? …` one that does not
exist, so a lone `!` would read as a quieter error rather than as a note.

### The log

Happy Hare writes its own `mmu.log`, and the console keeps it at **`/tmp/mmu.log`**, replaced
fresh on every run. It is live, so the useful thing is to watch it in a second window:

```bash
tail -f /tmp/mmu.log
```

`/log [N]` prints the path and the last N lines without leaving the prompt. `--log-dir DIR`
moves it, `--no-log` leaves it in the session temp dir to be discarded on exit. Raise the
detail with `/trace 4` (Happy Hare's own `log_level`).

The harness on its own still writes the log into a temp directory it deletes on `close()`,
which is right for tests; `session(..., log_dir=...)` is what keeps it. Note that
`MmuLogger` binds to the process-global `logging.getLogger('mmu')`, so the **first** session
to boot in a process fixes the log path for all of them — one session per process, which is
how the console runs.

### If a warning shows up on a pink background

Run `make console ARGS='--color 16'`. Happy Hare's console messages carry HTML colours which
the console translates to ANSI, and 24-bit `ESC[38;2;R;G;Bm` is **not** safely ignored by a
terminal that lacks truecolor — the channels get read as separate SGR codes. HH's warning
colour is `#FF69B4`, whose green channel is `0x69` = 105, and SGR 105 means *bright magenta
background*. So the warning arrives on a pink background.

`--color` defaults to `auto`, which only uses truecolor when `$COLORTERM` says `truecolor`
or `24bit` and otherwise emits 256-colour (`38;5;N`). `--color 16` is the belt-and-braces
option: it emits nothing but plain `30-37`/`90-97`, which no terminal can misread.

The header is **pinned to the top of the terminal** while output scrolls beneath it, and it
is redrawn after every command and every `/advance`. There is nothing to poll and no
refresh thread — since the clock is frozen at the prompt, state cannot change while you
are typing. `/header GROUPS` switches groups live, `/header all` turns every group on, and
`/header off` hides it *and* releases the pinned band. `all` and `off` work on the
`--header` flag too — both go through the same parser. On a pipe or with `--inline-header`
it falls back to reprinting above each prompt.

### Reading the LED rows

```
  led unit0 exit     ██ ██ ██ ██ ██ ██ ██ ██ ██  [gate_status]
  led unit0 entry    ██ ██ ██ ██ ██ ██ ██ ██ ██  [filament_color]
  led unit0 status   ████████  [filament_color]
  led unit0 logo     ██████  [(0.0, 0.0, 0.3)]
  led unit1 exit     ██████████████ ██████████████ ██████████████ ██████████████  [gate_status]
```

One block per **physical** LED, in that LED's own colour: `██` lit, `▓▓` lit but too dim to
show honestly, `··` off (grey). The LEDs of one gate run together and the gates are separated
by a space, so ViViD's seven-per-gate strip reads as four groups rather than 28
undifferentiated cells — and fits in 100 columns, which the ungrouped 117-column version did
not. `[...]` is the segment's effect from `led_manager.effect_state`; `[?]` means nothing has
painted it yet.

`▓▓` is not a third state, just an honest one. `black_light` is `(0.01, 0, 0.02)` — what an
idle `status` segment under `filament_color` shows, and what any black filament shows — and
that paints to xterm 16, i.e. pure black, *less* visible than the grey used for off. Anything
below 25% is therefore painted at 25% with its hue kept, and the lighter glyph is what tells
you the brightness on screen is a floor rather than a reading.

A lit LED used to be `##`, which was a problem rather than a shorthand: the glyph was painted
in the LED's colour, and a white or grey LED — `mmu_breathing_white_fast` on `selecting`,
`mmu_sparkle` on `complete`, `white_light` for an uncoloured gate under `filament_color` — came
out indistinguishable from ordinary text, because the terminal's default foreground *is* white.
A block in the same colour still reads as a block.

All four segments are shown. `ercf_vvd`'s unit0 configures every one of them (9 exit, 9 entry,
4 status, 3 logo) precisely so every effect path has somewhere to land. Note `define_on` in
`config/base/mmu.cfg` restricts most effects to `exit`/`gates`/`status`: only
`mmu_breathing_red_slow`, `mmu_red_strobe` and `mmu_green_strobe_fast` can run on `logo`. That
restriction is deliberate — it caps how many effect instances get pre-computed, which grows
with gate count — so widen it in your own config, not in the shipped template.

### Scrolling back

Pinning costs you the terminal's own scrollback. The header is pinned with a DECSTBM scroll
region, and a terminal only saves a row when it scrolls off the top of the **full screen** —
rows that scroll out of a *region* are discarded. So the scrollbar and Cmd-Up show the
session up to the moment the header was installed and nothing after it.

The console therefore keeps its own copy of every line it printed — which is also what
`/redraw` repaints from — and **`/s`** (or `/scroll`) opens a viewer over it, header still
pinned:

```
  ...the log, scrolled back...
 scrollback  15-40 of 66 (26 back)   up/down  pgup/pgdn  home/end   q to return
```

Inside the viewer, `q`/Esc/Enter returns you to the prompt and these scroll:

| Keys | |
|---|---|
| Up/Down, or `j`/`k` | a line |
| `b`/`f`, or space | a page |
| `g`/`G` | oldest / newest |
| PgUp/PgDn | a page — *if your terminal lets them through*, see below |

It is **modal on purpose**: it runs between `input()` calls, so readline is not active and
plain Up/Down keep meaning *previous command* at the prompt, which is the whole reason not
to bind the arrows to scrolling instead.

**PgUp may never reach the console at all.** Terminal.app and iTerm2 keep fn-Up/PgUp for
their own window scrollback, and a key the emulator swallows cannot be seen by any program
running in it — that is why the letter keys are listed first and appear in the status bar.
If fn-Up scrolls your terminal window instead of the log, that is the emulator, not the
console, and the window it scrolls is showing the session from *before* the header was
pinned (see above for why).

`/scroll N` opens N rows back. `--scrollback 0` turns the buffer off; `/clear` empties it
along with the log.

#### Shift-Up, and why it only works on some machines

Where readline permits it, **Shift-Up** and **PgUp** open the viewer too, and whatever you
had half-typed is not lost: the binding is a readline macro bracketed with ctrl-a/ctrl-e, so
your text comes back with one press of Up afterwards.

That only holds on **GNU readline** — Linux, and therefore the printers. On **libedit**,
which is what Python's `readline` module is on macOS, a key binding cannot do this at all:
libedit delivers only the *first character* of a macro immediately and holds the rest until
the next input event. A one-character macro fires at once; `/scroll` puts a lone `/` on the
line and stops, and the remainder is then flushed into whatever you type next — turning your
next command into `/scroll MMU_STATUS`. There is no readline API, in either flavour, to bind
a key straight to Python.

So the console checks the backend and simply does not bind the keys on libedit, rather than
installing one that corrupts the next line. `/s` is the two-keystroke stand-in, and the
startup banner says which of the two you have. `/header off` remains the escape hatch: it
drops the pinned band and gives you the terminal's own scrollback back.

### Running against your own installed config

`--profile` takes a path as well as a profile name, so the console can run the config the
installer actually produced — hand edits included:

```bash
./install.sh -z -t                                       # writes /tmp/mmu_test
make console ARGS='--profile /tmp/mmu_test/printer_data/config'
```

Point it at the `printer_data/config` directory (its `printer.cfg` gives the authoritative
`[include mmu/...]` set and order) or straight at the `mmu/` directory. `mmu_vars.cfg` is
skipped and `[save_variables]` is redirected into a scratch copy, so the console never
writes to your install.

Pick your real hardware in `menuconfig` when the installer offers it. A default config
generated non-interactively (`make KCONFIG_CONFIG=... olddefconfig`) does *not* boot: it
leaves the gate-0 gear pins empty and fails with `Invalid pin description ''`.

### Three things that will look like bugs

1. **Macro bodies do not run.** The fake `gcode_macro` records a call and never renders the
   body, so `T1`, the print start/end and the park/cut/purge sequences produce **silence**.
   Use `MMU_CHANGE_TOOL TOOL=1`. The console notices a bare `T<n>` and says so.
2. **A physical selector must be calibrated and homed before it can select a gate.** The
   console does that for you at startup (`boot(calibrate=True, pre_bootup=...)`);
   in a test, call `hh.boot(calibrate=True)`, or `hh.boot()` then `hh.calibrate()`, then
   `MMU_HOME UNIT=<n>`. Skip it and every selection fails with *"Selector is not clibrated"*
   (sic). Calibration is **seeded** by default for speed, but `MMU_CALIBRATE_*` genuinely
   works — see § "Physical selectors" below.
   A real printer usually skips the homing because it restores the position it saved at
   shutdown; a harness session starts with no vars file, so there is nothing to restore. To
   model the printer instead, pass `boot(calibrate=True, selected_gate=<n>,
   selector_last_pos=True)` — see `seed_selection` / `seed_selector_last_pos`.
3. **Pause is sticky.** After a failed operation the MMU sits paused and later commands
   refuse. The prompt shows `PAUSED`; recover with `MMU_UNLOCK` / `MMU_RECOVER`.

One known limitation: commands are dispatched at top level, exactly as the tests do, so a
`ReactorCompletion.wait()` returns immediately instead of waiting. Dispatching inside the
reactor fixes that in theory but breaks `MMU_PRELOAD` in practice (every gate ends up
`EMPTY`), so the proven path wins — see the comment on `_dispatch()` in
[console.py](console.py) for the measurements.

---

## 1b. The filament-display sweep

```
make filament_display
make filament_display ARGS='-k UNKNOWN'
```

Renders `get_filament_position_string()` (the `[T0] ■◉■■◉■┈┈┈...` status line) across
every sensor/position/`gate_homing_endstop` combination — thousands of them, in
milliseconds — so a change to that method can be eyeballed everywhere at once instead
of one console prompt at a time. `filament_display.py` wraps a plain-data
`FilamentDisplayState` in a duck-typed stand-in for `self` and calls the *real*
`MmuController.get_filament_position_string` directly, so there's no copy of its logic
to keep in sync — only the stand-in's attribute names need updating if that method ever
touches something new on `self`.

Not part of `make test`: this repo's discovery pattern is `*`, not unittest's default
`test*.py`, so a `test_*.py`-style name alone wouldn't keep this out — `make test` would
sweep it in and print its whole render matrix as test output. Instead
`filament_display_review.py` defines a `load_tests(loader, tests, pattern)` hook, which
unittest's loader always consults in place of collecting `TestCase` subclasses; it
returns an empty suite unless `HH_FILAMENT_DISPLAY_REVIEW` is set, which only the
`filament_display` Makefile target does. It's a manual/visual review aid (most of it
renders combinations for a human to read, not asserts against them), not a correctness
suite that should gate CI — run it on demand when touching the status line.

`make UT='filament_display_review.py' test` won't work for this reason (same empty
suite, no env var set) — `make filament_display` is the only entry point.

---

## 2. What is where

```
test/
  test_mmu_*.py             the tests themselves — this is what you read and write
  select.py                 the file picker `make test` opens (§1)
  console.py                the interactive console (§1a)
  filament_display.py       duck-typed adapter for get_filament_position_string() (§1b)
  filament_display_review.py  the bulk sweep, run via `make filament_display` (§1b)
  hh/                       the harness: the fake Klipper and fake Moonraker
  hh/klippy_root/           41 stand-in modules that pretend to be Klipper's own code
  installer/                legacy installer tests, currently skipped (see §6)
```

The test files, grouped by what they're about:

| File | Tests | Covers |
|---|---:|---|
| **Foundation** | | |
| `test_mmu_import.py` | 10 | Happy Hare imports at all outside Klipper; repo-wide syntax check |
| `test_mmu_config.py` | 8 | the real shipped `config/` templates render correctly |
| `test_mmu_reactor.py` | 24 | the fake reactor itself (see §3) |
| `test_mmu_bootup.py` | 31 | config load → `klippy:connect` → `klippy:ready` → `mmu:bootup` |
| `test_mmu_profiles.py` | 19 | the same checks across BoxTurtle, Tradrack and EMU |
| `test_mmu_adc_compat.py` | 14 | the Klipper-version ADC compatibility shim |
| **Filament handling** | | |
| `test_mmu_motion.py` | 24 | loading, parking, preloading filament |
| `test_mmu_toolchange.py` | 20 | `MMU_CHANGE_TOOL`, load and unload end to end |
| `test_mmu_encoder.py` | 18 | gate homing by encoder motion instead of by switch |
| `test_mmu_endless_spool.py` | 17 | runout detection, clog-vs-runout, gate remapping |
| **NFC and Spoolman** | | |
| `test_mmu_nfc.py` | 17 | NFC readers are configured and instantiated |
| `test_mmu_nfc_scan.py` | 34 | `MMU_NFC_SCAN`, the preload NFC compound endstop, the homing presence probe |
| `test_mmu_nfc_i2c.py` | 20 | software (bit-banged) i2c for PN532/PN7160, bus-collision validation |
| `test_mmu_nfc_probe.py` | 16 | the non-blocking presence probe, driver-level (real RC522 over a scripted bus) |
| `test_mmu_nfc_uart.py` | 77 | PN532 over HSU/UART: the byte-stream framer, the probe invariants, the `interface` option |
| `test_mmu_compound_endstop.py` | 16 | which child stopped a first-wins compound home (pure logic) |
| `test_mmu_tag_parser.py` | 34 | RFID tag decoding (pure logic, no fakes at all) |
| `test_mmu_moonraker.py` | 46 | the Moonraker half: Spoolman lookups, auto-create |
| `test_mmu_roundtrip.py` | 35 | Klipper and Moonraker talking to each other |
| **Presentation** | | |
| `test_mmu_leds.py` | 22 | LED effects, flashes, the pending overlay |
| `test_mmu_console.py` | 148 | the interactive console of §1a — rendering, command dispatch |
| `test_mmu_dev_test.py` | 19 | every `_MMU_TEST` developer probe — breadth, not depth |

Counts as the picker reports them; `installer/test_build.py` adds the one skipped test that
makes up the 651 total.

### Coverage map

Green is not the same as covered. Roughly where things stand:

| Area | State | Notes |
|---|---|---|
| Config rendering and load | **solid** | real templates, eight shipped machine/unit profiles |
| Bootup sequence | **solid** | including the error sentinel that stops bootup faking success |
| Tag decoding, Spoolman round trip | **solid** | including auto-create and the miss cache |
| Load / unload / tool change | **good** | the happy path and its common failures |
| Gate homing — switch and encoder | **good** | both branches of `_home_to_gate` |
| Preload and insert handling | **good** | |
| Endless spool and runout | **good** | including the clog-vs-runout decision |
| LEDs | **good** | effects and overlays; not the neopixel protocol |
| Sync feedback / buffer sensors | **partial** | EMU's analog sensor boots; the tension logic has a known bug |
| Selector homing and selection | **good** | `LinearSelector`, `LinearServoSelector`, `LinearMultiGearSelector`, `RotarySelector`, `IndexedSelector`, `ServoSelector`, and `VirtualSelector` are booted or exercised — `test_mmu_selector.py`. `MacroSelector` direct-mode dispatch is covered without executing a user macro |
| Calibration | **partial** | seeded by default for speed, but `MMU_CALIBRATE_SELECTOR` (manual and `AUTO=1`) and `MMU_CALIBRATE_BOWDEN` run for real — `test_mmu_selector.py` |
| Developer commands (`_MMU_TEST`) | **partial** | every option is run and must not raise — `test_mmu_dev_test.py`. What the stress probes *provoke* is step-generation timing the harness does not model |
| Espooler, FlowGuard | **none** | |
| Multi-unit machines | **good** | `ercf_vvd` renders, boots and loads on both units |
| Klipper motion and timing | **none** | out of scope by design — see §9 |

Blunter version: of Happy Hare's **69 user-facing `MMU_*` commands, tests drive 14**. Those
14 are the ones a print depends on, and the internal `_MMU_*` sequence macros run
underneath them — but most administrative and calibration commands have never been called
here. A green suite says the operational core works, not that the command set does.

---

## 3. How the fakes work

You mostly won't touch these, but knowing the shape helps when something behaves oddly.

**The fake `klippy` tree.** Happy Hare installs by symlinking `extras/**.py` into
`<klipper>/klippy/extras/`, and its imports only resolve in that shape. So the harness
builds that exact layout in a temp directory — Happy Hare's real files symlinked
alongside 41 stand-in modules (`mcu.py`, `toolhead.py`, `pins.py`, and so on). Happy Hare
cannot tell the difference.

**The reactor and virtual time.** Klipper's "reactor" is its scheduler: it runs timers and
callbacks. The real one uses the wall clock. Ours uses a **fake clock you control**:

```python
hh.reactor.advance(20.0)     # 20 seconds pass instantly
```

This matters because Happy Hare is full of long timers — a 20-second pending-spool
timeout, a 5-second warning window, a 2.5-second boot delay. Real waiting would make the
suite unusable. `advance()` runs every timer that falls due, in order.

**The filament model.** Two numbers per gate: where the filament's leading edge (the *tip*)
is and where its trailing end (the *tail*) is, in millimetres, measured so that `0` is the
gate's sensor. Filament occupies everything between them, so a switch reads "triggered"
when it sits inside that span. When Happy Hare commands a move, the harness works out which
sensor trips first and how far the filament actually gets. Default layout:

```
spool ... park(-100) ... entry(-50) ... gate/exit(0) ... encoder(+20) ... extruder(+700)
```

The tail is normally infinitely far back — a spool is attached, so anything behind the tip
is filament. `fil.exhaust(gate)` gives it a real end, which is what a runout physically
*is*. Without that, every simulated runout looks like a clog to Happy Hare because the gate
sensor never releases.

The encoder is not a switch: it reports *motion*, so what matters is how much of a move
happened while filament covered the wheel. That is `fil.travel_over()`, and the harness
turns it into real pulses so Happy Hare's own counter callback does the accumulating.

**The fake Moonraker** provides a working in-memory Spoolman — not a mock. When Happy Hare
auto-creates a spool, a spool really is created, and the next scan of that tag really
resolves to it. That round trip *is* the thing under test.

---

## 4. Writing a test

Python note: a test is a method whose name starts with `test_`, inside a class inheriting
`unittest.TestCase`. `setUp` runs before each test, `tearDown` after. Assertions are
methods: `self.assertEqual(a, b)`, `self.assertTrue(x)`, `self.assertIsNone(x)`.

A minimal test:

```python
import unittest
from test.hh import session

class TestMyThing(unittest.TestCase):
    def setUp(self):
        self.hh = session('boxturtle')   # pick a machine profile
        self.hh.boot()                   # config load -> connect -> ready -> bootup
        self.assertEqual(self.hh.errors, [])

    def tearDown(self):
        self.hh.close()                  # always; it cleans up threads and temp files

    def test_gate_starts_empty(self):
        self.assertEqual(self.hh.mmu.gate_status[0], 0)
```

`self.hh.mmu` is the **real** `MmuController`. Anything you could inspect on a live
printer, you can inspect here.

### Things you'll commonly do

```python
# run a gcode command exactly as a user would
hh.run_gcode('MMU_PRELOAD GATE=1')

# put filament somewhere (quietly — see §5)
hh.place_filament(0)                       # at the park position
hh.place_filament(0, position=-40.0)       # somewhere specific

# drive a sensor through its real callback path
hh.sensor('mmu_entry_0').set(True)
hh.sensor('mmu_entry_0').present           # what Happy Hare currently believes

# run the spool out (see §5.6) — the filament keeps its tip, but loses its tail
hh.filament().exhaust(0)

# move time forward
hh.reactor.advance(5.0)

# check nothing went wrong
self.assertEqual(hh.errors, [])
```

### Choosing a profile

A profile is a set of menuconfig choices; the harness renders the **real shipped
templates** from them, so a broken template shows up as a test failure.

| Profile | What it gives you |
|---|---|
| `ercf_vvd` | **the console default.** The only multi-unit profile, and a transcription of a real machine: ERCF 1.1sb (9 gates, `LinearServoSelector`, encoder) + ViViD 1.0 (4 gates, `IndexedSelector`), 13 gates. Also the only one with a sparse per-gate device list, a filament heater, and full LED coverage: unit0 wires all four segments (9 exit on an external chain, 9 entry, 4 status, 3 logo) while unit1 has 28 exit LEDs over 4 gates |
| `boxturtle` | 4 gates, no NFC — the default for most tests |
| `tradrack` | a physical (servo) selector, single unit, no encoder — the simplest physical-selector case |
| `chameleon` | 3D Chameleon: the only `RotarySelector`, and the only machine with no servo — one gear motor reversed on half the gates, and "release" means driving the carriage to the opposing gate's offset |
| `pico_mmu` | PicoMMU's `ServoSelector`; deliberately boots uncalibrated because its gate angles depend on the physical cam build |
| `mmx` | MMX's `ServoSelector` with its vendor gate-angle order; also used for a full load/unload selector test |
| `emu` | 5 gates and the only shipped profile with an analog buffer sensor |
| `encoder` | BoxTurtle plus an encoder, homing to it instead of to the gate switch |
| `nfc_single` | one common NFC reader |
| `nfc_per_gate` | one reader per gate |
| `nfc_pn532_uart` | one common PN532 over HSU/UART — the only host-serial reader |
| `nfc_spoolman` | per-gate NFC + Spoolman enabled + auto-create |

There are more `nfc_*` profiles than these — one per reader type and transport
(`nfc_pn5180`, `nfc_pn532`, `nfc_pn532_sw_i2c`, `nfc_pn532_uart_per_gate`, ...). They
exist because each renders a *different* set of config keys, which is where template
bugs hide. `test/hh/profiles.py` is the list, with a comment on each explaining what it
catches.

### Physical selectors, and what "calibrated" means here

`test/hh/selector.py` models where a unit's selector endstops sit — a **separate axis** from
the filament path, which is one scalar per gate and has no carriage. Two **endstop** geometries,
because the shipped families disagree: the `LinearSelector` family (ERCF, Tradrack) has one home
switch and reaches gates by plain moves to calibrated offsets, while `IndexedSelector` (ViViD)
has no home switch at all and one index switch per gate, visited in `selector_gate_order`.
`LinearMultiGearSelector` shares the linear geometry but uses one gear drive per gate and still
has to home its physical selector axis. `RotarySelector` (3D Chameleon) shares the first
geometry but not its meaning: with no servo,
the carriage position *is* the grip — a released gate parks at another gate's offset
(`selector_release_gates`), so gate → position is not a bijection.

**The carriage is tracked**, in `SelectorAxis.carriage`, the same way `filament.py` tracks the
filament — because the two meanings of "position" have to be kept apart. `MmuGenericRail.home()`
rebases the axis to `forcepos` immediately before every homing move, so the stepper coordinate
says nothing about where the carriage physically is. In the fake `MCU_stepper`:

| | effect |
|---|---|
| `set_position()` | redefines the coordinate frame; **mcu-preserving**, as in real Klipper |
| `harness_note_motion()` | real travel; moves the mcu step count |

Motion reaches the axis from exactly two places — the fake `HomingMove` and
`Session._on_manual_move`. Both must call `advance()`; a plain move that is not observed leaves
the carriage on the home switch through the retract inside `rail.home()`, and the second homing
move then measures zero.

**Calibration is seeded by default, but not fake.** `Session.calibrate()` writes selector
offsets, bowden length, gear rotation distance and encoder resolution using HH's own published
formulas and the harness's own filament geometry, so no numbers are invented. It is a shortcut,
not a substitute: `MMU_CALIBRATE_SELECTOR` (manual and `AUTO=1`) measures real travel and is
covered by `TestSelectorCalibration`. To drive the real flow, boot uncalibrated
(`hh.boot()` with no `calibrate=`, or `make console ARGS='--no-calibrate'`) and place the
carriage where the procedure expects it — `axis.place(mm)` in a test, `/selector` in the console.

`boot(calibrate=True)` seeds *before* `klippy:ready`, so Happy Hare's own `handle_ready` loads
the variables and the "not found in mmu_vars.cfg" warnings never fire. Called after ready, as
tests do, `calibrate()` applies the values in memory instead. A bare `boot()` seeds nothing,
because uncalibrated is a real state HH has to cope with and tests assert it.

**Tip forming is the one macro with an effect.** Bodies do not run (see §"Three things that
will look like bugs"), but HH measures how far the extruder moved during `_MMU_FORM_TIP` and
refuses the unload if the answer is zero — so on a machine with an encoder a no-op tip form
reads as a jam. `printer.harness_macro_effects` maps a macro alias to a callable; the one
registered effect retracts the extruder and moves the filament model together, by a distance
read from the machine's own `_MMU_FORM_TIP_VARS`.

The first five profiles above are shipped machine types. `encoder` is derived — BoxTurtle with menuconfig
options flipped. That is only safe when the resulting config renders *complete*: enabling a
feature outside the starter that ships it can leave dependent parameters blank, producing a
machine that boots but behaves like nothing real. Render it and read the section before
trusting it; an earlier attempt to bolt a proportional buffer onto BoxTurtle had to be
reverted for exactly this reason.

For NFC reads, add `virtual_nfc=True` so readers return tags from the filament model:

```python
hh = session('nfc_per_gate', virtual_nfc=True)
hh.boot()
hh.filament().attach_tag(0, '04A1B2C3')     # gate 0's filament carries this tag
```

### Testing both halves together

```python
from test.hh.roundtrip import RoundTrip

with RoundTrip(profile='nfc_spoolman') as rt:
    rt.present_tag('DEADBEEF', gate=None, material='PETG', min_temp=230, max_temp=250)
    # Klipper asked Moonraker, Moonraker created a spool and called back
    self.assertEqual(rt.mmu.pending_spool_id, rt.db.created_spools[0])
```

`present_tag()` injects at the exact point a real reader hands off, so no hardware is
involved. `RoundTrip` pumps messages between the two sides until everything settles.

---

## 5. Six things that will bite you

These are all real behaviours, learned by getting them wrong.

**1. Placing filament is an event.** Covering the entry switch is an *insert*, and Happy
Hare responds by preloading that gate. `place_filament()` suppresses that by default so
you can set up a scenario. Pass `quiet=False` when you actually want to test insert
handling.

**2. LED tests must wait ~12 seconds first.** `effect_initialized` (the rainbow) is a
unit-wide timed effect lasting 8 seconds from boot, and a flash requested while it holds
the unit is *dropped*. Without `hh.reactor.advance(12.0)` you're measuring the rainbow.

**3. `effect_state` doesn't show the pending overlay.** It records the *underlying*
configured effect — that's deliberate. Check the overlay with
`led_manager._pending_overlay_effect(unit, 'exit')` instead.

**4. `spoolman_led_segment: gate_status` is not a segment name.** It means "the segments
showing per-gate availability", i.e. `exit` and `entry`. Passing `'gate_status'` where a
segment name is expected silently returns `None`.

**5. Geometry is constrained by Happy Hare, not free.** The entry switch must sit between
the park position and the gate sensor, so a parked filament leaves it clear — Happy Hare
marks a gate `GATE_UNKNOWN` if preload finishes with it still covered. Also: a preload can
only realistically start with filament *already past* the entry switch, because that's
what a user's push produces.

**6. A runout needs `fil.exhaust(gate)`, not just an empty gate.** Moving filament away
isn't a runout — the spool is still attached, so the gate sensor stays covered and Happy
Hare correctly calls it a clog. `exhaust()` gives the filament a real trailing end. Getting
this wrong makes Happy Hare look broken when it is being right about an impossible machine.

---

## 6. Skips and expected failures

```
OK (skipped=1, expected failures=4)
```

**`expected failures`** are known bugs, written as tests of what *should* happen and
marked `@unittest.expectedFailure`. They're **self-healing**: Python reports an
unexpected success as a *failure*, so the moment someone fixes the bug the suite goes red
and tells you to delete the marker. If a test you didn't touch suddenly fails that way,
you probably fixed something — check, then remove the marker and its comment.

Currently:

| Where | Bug |
|---|---|
| `test_mmu_profiles.py` ×2 | the proportional buffer reports TENSION almost always — its low threshold is computed positive when the config help says it should be about −0.9 |
| `test_mmu_tag_parser.py` | a blank tag is reported as a Bambu Lab tag |
| `test_mmu_motion.py` | a `synced` (print-time) move does not advance the filament model — unlike the other three drive modes it never reaches `MmuStepper._submit_move`, so `motion_queuing`'s trapq hook never fires. A HARNESS gap rather than a Happy Hare bug, which is the one entry here that will not be fixed by changing `extras/` |

**`skipped`** is `test/installer/test_build.py` — legacy installer tests that can't run
(the functions they call no longer exist). Its header explains what restoring it needs.

---

## 7. Debugging a failing test

**Read the assertion first.** Most have a message explaining what the code is supposed to
do and why.

**Ask Happy Hare what it did.** Turn on its own trace logging:

```python
hh.mmu.p.log_level = 4          # 4 = trace
hh.run_gcode('MMU_PRELOAD GATE=0')
for line in hh.console:
    print(line)
```

This prints Happy Hare's real internal narration — every move, every homing result, every
decision. It is by far the fastest way to see what actually happened. There's also a full
log file at `hh.tmpdir + '/mmu.log'`.

**Ask the model what moved:**

```python
print(hh.filament().history)
# [(0, 100.0, 'homing -> mmu_exit_0'), (0, -100.0, 'move')]
#  gate, millimetres, why
print(hh.filament().describe(0))
# gate 0 tip=-100.0 mmu_entry_0=0 mmu_exit_0=0 mmu_shared_exit=0 filament_compression=0
```

**Check what got sent where:**

```python
hh.gcode.executed        # every gcode command run
hh.errors                # anything Happy Hare reported as an error
hh.webhooks.calls        # calls Klipper made to Moonraker
hh.pins.types_by_pin()   # which pin was set up as what
```

**If a test hangs or times out**, it's usually the reactor waiting on something that never
happens. `advance()` has a watchdog that fails with the list of pending timers rather than
hanging forever.

---

## 8. Working on Happy Hare with this

A reasonable loop:

1. Run the file closest to your change first — it's seconds, not a minute. `make test`, `n`,
   `+`the file's name, Enter; after that `make LAST=1 test` repeats it with no picker.
2. Change the code.
3. Re-run that file, then `make test` + Enter (everything) before committing.
4. Add a test for what you changed. If you fixed a bug, the test should fail before your
   fix and pass after — check that, or you don't know it's testing anything.

**When you find a bug you're not fixing now**, write it as an `@unittest.expectedFailure`
describing the correct behaviour, with a comment explaining the cause. It documents the
problem, proves it's real, and cleans itself up when fixed.

**Prefer driving real commands** (`hh.run_gcode('MMU_PRELOAD GATE=1')`) over calling
internal methods. Internals skip the command wrapper that sets up state, which has already
caused confusing failures.

---

## 9. What this does *not* cover

Worth knowing so you don't over-trust a green run. The coverage map in §2 has the
per-area picture; these are the structural limits behind it.

- **No real motion.** No acceleration, step generation or timing. The harness tests
  Happy Hare's *sequencing*, not Klipper's motion planner. Timing bugs, "timer too close",
  and step-generation issues are invisible here.
- **No real hardware protocol.** The RC522 init sequence is exercised, but tag *reads* are
  faked at the driver level. The PN532 and PN7160 I²C drivers aren't covered at all.
- **Proprietary tag formats are untested.** Bambu, Creality, QIDI and Anycubic parsing
  needs captured dumps from real spools — synthesising them would only prove the test
  agrees with itself.
- **Encoder and gear calibration are seeded, never measured.** `MMU_CALIBRATE_ENCODER` and
  `MMU_CALIBRATE_GEAR` would only re-derive the numbers the harness generates its moves
  from, so they would confirm arithmetic rather than test anything.
- **Macros load but mostly don't run.** The shipped `config/macros/*.cfg` are read
  verbatim so sequences can find them, but a test that asserts on macro *behaviour* would
  be testing Klipper's Jinja, not Happy Hare.
- **The fakes could be wrong.** They're written against real Klipper's behaviour, but
  where they diverge, a test can pass while the real thing fails.

Green means "Happy Hare's logic does what we think" — not "this will work on a printer".
It is still the difference between finding a bug in ten seconds on a laptop and finding it
mid-print.
