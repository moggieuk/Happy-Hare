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

The tests need two libraries Happy Hare itself doesn't (`greenlet`, `jinja2`), so they run
in a **virtualenv** — a private Python install that lives in the repo directory but is
*not* part of the git repo. You create it yourself, once, and it is yours alone:

```bash
python3 -m venv venv && ./venv/bin/pip install -r test/requirements.txt
```

That makes a `venv/` directory at the repo root. Git ignores it (Python's `venv` module
writes an ignore rule into it), so it will never show up in `git status` or a commit.
Delete it and re-run the line above any time you want a clean one.

Then, from the repo root:

```bash
make test
```

`make test` finds `venv/` on its own. If you haven't made one it falls back to plain
`python`, which will fail with `ModuleNotFoundError: No module named 'greenlet'` — that
error means "run the setup line above", not "the tests are broken". To test against a
different interpreter deliberately, say so: `make PY=/usr/bin/python3 test`.

That runs everything — currently **333 tests in about two minutes**. Expect to see:

```
OK (skipped=1, expected failures=5)
```

`skipped` and `expected failures` are normal and explained in §6. Anything else — `FAILED
(failures=…)` or `(errors=…)` — is a genuine problem.

### Running less than everything

While working, you usually want one file or one test. `-m unittest` means "run Python's
built-in test runner"; the argument is a **dotted module path**, so `test/test_mmu_leds.py`
becomes `test.test_mmu_leds`:

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

There is also `make UT='test_mmu_nfc*.py' test` to run a filename pattern, but the dotted
form above is usually easier.

---

## 2. What is where

```
test/
  test_mmu_*.py     the tests themselves — this is what you read and write
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
| `test_mmu_reactor.py` | 17 | the fake reactor itself (see §3) |
| `test_mmu_bootup.py` | 31 | config load → `klippy:connect` → `klippy:ready` → `mmu:bootup` |
| `test_mmu_profiles.py` | 19 | the same checks across BoxTurtle, Tradrack and EMU |
| `test_mmu_adc_compat.py` | 14 | the Klipper-version ADC compatibility shim |
| **Filament handling** | | |
| `test_mmu_motion.py` | 24 | loading, parking, preloading filament |
| `test_mmu_toolchange.py` | 20 | `MMU_CHANGE_TOOL`, load and unload end to end |
| `test_mmu_encoder.py` | 18 | gate homing by encoder motion instead of by switch |
| `test_mmu_endless_spool.py` | 17 | runout detection, clog-vs-runout, gate remapping |
| **NFC and Spoolman** | | |
| `test_mmu_nfc.py` | 12 | NFC readers are configured and instantiated |
| `test_mmu_nfc_scan.py` | 17 | `MMU_NFC_SCAN`, the preload NFC compound endstop |
| `test_mmu_tag_parser.py` | 34 | RFID tag decoding (pure logic, no fakes at all) |
| `test_mmu_moonraker.py` | 42 | the Moonraker half: Spoolman lookups, auto-create |
| `test_mmu_roundtrip.py` | 27 | Klipper and Moonraker talking to each other |
| **Presentation** | | |
| `test_mmu_leds.py` | 22 | LED effects, flashes, the pending overlay |

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
| Physical selector homing | **thin** | Tradrack boots, but `home_unit` is barely exercised |
| Calibration, espooler, FlowGuard | **none** | |
| Multi-unit machines | **none** | needs per-unit Kconfig the harness bypasses |
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
| `boxturtle` | 4 gates, no NFC — the default for most tests |
| `tradrack` | a physical (servo) selector rather than a virtual one |
| `emu` | 5 gates and the only shipped profile with an analog buffer sensor |
| `encoder` | BoxTurtle plus an encoder, homing to it instead of to the gate switch |
| `nfc_single` | one common NFC reader |
| `nfc_per_gate` | one reader per gate |
| `nfc_spoolman` | per-gate NFC + Spoolman enabled + auto-create |

The first three are shipped machine types. `encoder` is derived — BoxTurtle with menuconfig
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
OK (skipped=1, expected failures=5)
```

**`expected failures`** are known bugs, written as tests of what *should* happen and
marked `@unittest.expectedFailure`. They're **self-healing**: Python reports an
unexpected success as a *failure*, so the moment someone fixes the bug the suite goes red
and tells you to delete the marker. If a test you didn't touch suddenly fails that way,
you probably fixed something — check, then remove the marker and its comment.

Currently:

| Where | Bug |
|---|---|
| `test_mmu_nfc_scan.py` ×2 | `MMU_NFC_SCAN` retracts the filament ~100 mm every scan |
| `test_mmu_profiles.py` ×2 | the proportional buffer reports TENSION almost always — its low threshold is computed positive when the config help says it should be about −0.9 |
| `test_mmu_tag_parser.py` | a blank tag is reported as a Bambu Lab tag |

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

1. Run the file closest to your change first — it's seconds, not a minute.
2. Change the code.
3. Re-run that file, then `make test` before committing.
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
