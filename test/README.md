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

Git ignores `venv/` (Python's `venv` module writes an ignore rule into it), so it will
never show up in `git status` or a commit.

The tests are not its only tenant. On a system whose Python refuses to install anything
outside a virtualenv (PEP 668 "externally managed" — Homebrew, Debian Bookworm), and where
Klipper's own `klippy-env` isn't there to be used instead, `install.sh` builds the same
venv via `make installer_venv` and runs the installer out of it. That installs only
`installer/requirements.txt`, tracked by its own stamp file, so the two sets never
invalidate each other and the installer never pays for `greenlet`.

It is only built once. Later runs reuse it and go straight to the tests; editing
`test/requirements.txt` reinstalls automatically. Some knobs:

```bash
make venv                       # build the venv, don't run anything
make clean_venv                 # throw it away (`make clean` deliberately does not)
make VENV=/somewhere/else test  # put the venv somewhere other than ./venv
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

`make test` offers everything — currently **620 tests, about six minutes** on a warm
laptop. Expect to see:

```
OK (skipped=1, expected failures=4)
```

`skipped` and `expected failures` are normal and explained in §6. Anything else — `FAILED
(failures=…)` or `(errors=…)` — is a genuine problem.

Six minutes is still too long to sit through on every change, which is why `make test` opens a
file picker first rather than starting straight away.

### Running less than everything

`make test` opens a picker first. Everything starts ticked, so pressing Enter runs the whole
suite exactly as it always did — but untick the expensive files and you get a focused run:

```
Happy Hare tests - 620 tests in 23 files                    times from last run

   1 [x] installer.test_build           1      0.0s
   2 [x] test_mmu_adc_compat           14      0.0s
   3 [x] test_mmu_bootup               31       36s
   …
   6 [x] test_mmu_console              66      134s
   …
  13 [x] test_mmu_nfc                  17      187s
   …
  22 [x] test_mmu_toolchange           20      5.4s

  selected: 23 files - 620 tests - ~6m00s last time

  [Enter] run    1 3 5-8 toggle    a all    n none    v invert
  +TEXT / -TEXT tick by name    p previous selection    s sort by time    q quit
>
```

The right-hand column is how long each file took **on your machine, last run**, and it is the
column to look at when deciding what to drop, because the cost is wildly uneven. From a real
full run: `test_mmu_nfc` took 187 s for 17 tests and `test_mmu_console` 152 s for 63, while
six files — including `test_mmu_tag_parser`'s 34 tests — came in under a tenth of a second
between them. A handful of the twenty-three files account for most of the run. The times
fill in after your first run, `s` sorts by them, and the footer estimates what the current
selection will cost.

Two things to know about the numbers. They cover each file's **class fixtures** as well as its
tests, which is where nearly all the time actually is — `setUpClass` building a printer, not
the assertions. And part of that cost is shared and cached across a run (`test/hh/cfg.py`
caches template rendering, `test/hh/root.py` builds the fake Klipper overlay once), so a file
run on its own can cost far more than the same file inside a full run — `test_mmu_config` is
0.1 s in a full run and 35 s alone. Treat the column as a guide to relative cost within a run,
not an absolute per-file price.

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
and deleting it just means the picker opens with no times to show.

---

## 1a. The interactive console

Everything the tests drive, driven by hand instead — Mainsail's console pane, against the
simulation:

```bash
make console
```

```
mmu[T0 g0]> MMU_CHANGE_TOOL TOOL=1
Tool change requested: T1
...
------------------------------------------------------------------------
T1   gate 1    LOADED IN NOZZLE           868.0mm  Idle
  print=initialized  SYNCED  t=+2.51s
  mmu_entry_1=1  mmu_exit_1=1  filament_compression=1  filament_tension=1  ...
            nfc pre ent exit/gate shex enc comp/extr nozl
   gate 0    ..  ..  ..     ..     ..   ..     ..     ..     -100.0
  *gate 1    ##  ##  ##     ##     ##   ##     ##     ##     +768.0
------------------------------------------------------------------------
```

Anything not starting with `/` is sent to the MMU as G-code. `/help` lists the
meta-commands, `MMU_HELP` lists Happy Hare's, and every HH command takes `HELP=1`.

The status section is separated from the log window by a **heavy rule**; `/clear` wipes the
log and leaves the status alone. Useful meta-commands beyond `/help`:

| Command | Does |
|---|---|
| `/advance N` | move the virtual clock N seconds (nothing is time-driven without it) |
| `/vars [mmu\|machine]` | `get_status()` of the `mmu` object, the `mmu_machine` object, or both |
| `/clear` | clear the log window, keep the status section |
| `/sensor NAME on\|off\|enable\|disable` | `on/off` drives the switch through its real button callback; `enable/disable` flips `sensor_enabled` so Happy Hare treats it as **not fitted** |
| `/place`, `/preload`, `/exhaust` | set the scene: filament at a gate, preloaded, or run out |
| `/log [N]`, `/trace 0-4` | the log file, and how much detail goes into it |

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

**The clock is virtual and frozen while you type.** Nothing happens at the prompt: no timer
fires, no LED animation ticks, no pending-spool timeout expires. That is what makes the
prompt safe, and it is why `/advance N` exists — without it you never see anything
time-driven. `/advance 12` clears the 8-second boot LED rainbow; the pending spool_id
timeout is 20 seconds.

Useful flags — `make console ARGS='...'`:

```bash
--profile boxturtle            # or tradrack, emu, encoder, nfc_single, nfc_spoolman, ...
                               # (default is ercf_vvd, a real 2-unit machine)
--profile /path/to/config      # your own installed config - see below
--header machine,sensors,filament,gates,leds     # or 'off'
--inline-header                # reprint above each prompt instead of pinning it
--color 256|truecolor|16|auto  # colour depth (see below)
--log-dir /tmp                 # where mmu.log goes; --no-log to discard it
--trace 4                      # full Happy Hare narration
--no-preload                   # leave every gate empty
--script FILE                  # run non-interactively (this is how it is tested)
```

Startup shows Happy Hare's **real bootup output** — the welcome banner, the unit summary and
the calibration warnings — because `cmd_MMU_BOOTUP` runs here exactly as it does on a
printer.

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
are typing. `/header GROUPS` switches groups live; `/header off` hides it. On a pipe or
with `--inline-header` it falls back to reprinting above each prompt.

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
   console does that for you at startup (`_prepare_selectors`); in a test, call
   `hh.calibrate()` then `MMU_HOME UNIT=<n>`. Skip it and every selection fails with
   *"Selector is not clibrated"* (sic). Calibration is **seeded**, not measured — see §
   "Physical selectors" below for why, and what that does not cover.
3. **Pause is sticky.** After a failed operation the MMU sits paused and later commands
   refuse. The prompt shows `PAUSED`; recover with `MMU_UNLOCK` / `MMU_RECOVER`.

One known limitation: commands are dispatched at top level, exactly as the tests do, so a
`ReactorCompletion.wait()` returns immediately instead of waiting. Dispatching inside the
reactor fixes that in theory but breaks `MMU_PRELOAD` in practice (every gate ends up
`EMPTY`), so the proven path wins — see the comment on `_dispatch()` in
[console.py](console.py) for the measurements.

---

## 2. What is where

```
test/
  test_mmu_*.py     the tests themselves — this is what you read and write
  select.py         the file picker `make test` opens (§1)
  console.py        the interactive console (§1a)
  hh/               the harness: the fake Klipper and fake Moonraker
  hh/klippy_root/   41 stand-in modules that pretend to be Klipper's own code
  installer/        legacy installer tests, currently skipped (see §6)
```

The test files, grouped by what they're about:

| File | Tests | Covers |
|---|---:|---|
| **Foundation** | | |
| `test_mmu_import.py` | 10 | Happy Hare imports at all outside Klipper; repo-wide syntax check |
| `test_mmu_config.py` | 8 | the real shipped `config/` templates render correctly |
| `test_mmu_reactor.py` | 21 | the fake reactor itself (see §3) |
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
| `test_mmu_console.py` | 63 | the interactive console of §1a — rendering, command dispatch |

Counts as the picker reports them; `installer/test_build.py` adds the one skipped test that
makes up the 563 total.

### Coverage map

Green is not the same as covered. Roughly where things stand:

| Area | State | Notes |
|---|---|---|
| Config rendering and load | **solid** | real templates, three machine profiles |
| Bootup sequence | **solid** | including the error sentinel that stops bootup faking success |
| Tag decoding, Spoolman round trip | **solid** | including auto-create and the miss cache |
| Load / unload / tool change | **good** | the happy path and its common failures |
| Gate homing — switch and encoder | **good** | both branches of `_home_to_gate` |
| Preload and insert handling | **good** | |
| Endless spool and runout | **good** | including the clog-vs-runout decision |
| LEDs | **good** | effects and overlays; not the neopixel protocol |
| Sync feedback / buffer sensors | **partial** | EMU's analog sensor boots; the tension logic has a known bug |
| Physical selector homing and selection | **good** | both selector families home, select and move filament — `test_mmu_selector.py` |
| Calibration | **thin** | seeded, not exercised: `MMU_CALIBRATE_*` is never run (see "Physical selectors") |
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
| `ercf_vvd` | **the console default.** The only multi-unit profile, and a transcription of a real machine: ERCF 1.1sb (9 gates, `LinearServoSelector`, encoder) + ViViD 1.0 (4 gates, `IndexedSelector`), 13 gates. Also the only one with a sparse per-gate device list, an external LED chain and a filament heater |
| `boxturtle` | 4 gates, no NFC — the default for most tests |
| `tradrack` | a physical (servo) selector, single unit, no encoder — the simplest physical-selector case |
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
the filament path, which is one scalar per gate and has no carriage. Two geometries, because
the shipped families disagree: the `LinearSelector` family (ERCF, Tradrack) has one home
switch and reaches gates by plain moves to calibrated offsets, while `IndexedSelector` (ViViD)
has no home switch at all and one index switch per gate, visited in `selector_gate_order`.

**Calibration is seeded, not measured.** `Session.calibrate()` writes selector offsets, bowden
length and gear rotation distance through HH's own setters, using HH's own published formulas
and the harness's own filament geometry — so no numbers are invented and no HH logic is
duplicated. It is not called from `boot()`, because uncalibrated is a real state HH has to cope
with and tests assert it.

What that does **not** cover: `MMU_CALIBRATE_SELECTOR AUTO=1` and friends never run. They
measure travel through the mcu step counter, which needs the mcu position to survive the
`set_position(forcepos)` that precedes every homing move. Real Klipper gets that from step
generation; the fake has none — `set_position` *is* how it effects motion — so making it
preserve the mcu position makes travel measure 0 and homing die with *"Endstop still triggered
after retract"*. The reasoning is recorded at the top of `test/hh/selector.py`.

**Tip forming is the one macro with an effect.** Bodies do not run (see §"Three things that
will look like bugs"), but HH measures how far the extruder moved during `_MMU_FORM_TIP` and
refuses the unload if the answer is zero — so on a machine with an encoder a no-op tip form
reads as a jam. `printer.harness_macro_effects` maps a macro alias to a callable; the one
registered effect retracts the extruder and moves the filament model together, by a distance
read from the machine's own `_MMU_FORM_TIP_VARS`.

The first four profiles above are shipped machine types. `encoder` is derived — BoxTurtle with menuconfig
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
- **One unit only.** Genuine multi-unit machines need per-unit Kconfig loading that the
  config layer deliberately bypasses, so nothing about unit selection is covered.
- **Calibration is untouched.** Every profile uses shipped defaults; no calibration
  command has ever been run here.
- **Macros load but mostly don't run.** The shipped `config/macros/*.cfg` are read
  verbatim so sequences can find them, but a test that asserts on macro *behaviour* would
  be testing Klipper's Jinja, not Happy Hare.
- **The fakes could be wrong.** They're written against real Klipper's behaviour, but
  where they diverge, a test can pass while the real thing fails.

Green means "Happy Hare's logic does what we think" — not "this will work on a printer".
It is still the difference between finding a bug in ten seconds on a laptop and finding it
mid-print.
