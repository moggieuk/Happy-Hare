# Documentation tooling

Screenshots of the menuconfig installer, generated from the real thing rather than
captured by hand. `doc_tools/capture.py` runs `menuconfig` against `installer/Kconfig`
in a pty, interprets what it draws, and renders the screen to a PNG.
`doc_tools/shots.py` is the list of images the documentation needs.

The split is deliberate: **this directory is code**, and everything it produces -
`doc/images/`, `doc/GettingStartedWithBoxTurtle.md` and its image folder, any future
page - lives under `doc/` instead. Nothing here is installed on a printer or imported
by Happy Hare, the installer or the tests. The dependencies (`pyte`, `Pillow`) live in
`doc_tools/requirements.txt` and are installed into `./venv` on demand by the `shots`
target.

## Regenerating the images

```bash
make shots                                       # everything, into doc/images
make shots ARGS='--list'                         # the sessions and what each covers
make shots ARGS='--only installer-tour'          # just one session
make shots ARGS='--only installer-tour -v'       # ...and print each screen as text
make shots ARGS='--seed ~/printer_data/.mmu_config'   # against a real machine
```

## Seeds — which machine the screenshots show

Every session starts from a config. Without one the screens show Custom Design / Not
listed / Other plus three config warnings, which is the least representative machine
a reader could be shown.

* **Default: `boxturtle`.** Generated, not committed — the tool parses the Kconfig
  tree, selects `MMU_TYPE_BOX_TURTLE_1_0` (the symbol `test/hh/profiles.py` uses for
  the same machine) and writes a config. A checked-in `.mmu_config` would go stale
  silently as Kconfig gains options; generating means the seed always matches the
  tree being documented.
* **A real config: `--seed path/to/.mmu_config`.** Whatever is on your printer.
* **A unit of a multi-unit setup: `--seed path/to/.mmu_config_gru`.** The `_gru`
  suffix is recognised, so the session parses as unit `gru` with `F_MULTI_UNIT=y`,
  and `UNIT_INDEX` plus the printer-level `HAS_SENSOR_*` capabilities are read out of
  the sibling `.mmu_config` — exactly what `install.sh:435-442` passes down. Point it
  at a top `.mmu_config` that has `CONFIG_MULTI_UNIT=y` and you get the shared-config
  entry point instead, in the aquatic style a user would really see there.
* **`--seed none`** for bare Kconfig defaults.

Seeds are inputs. The session copies one into a temporary directory and points
`KCONFIG_CONFIG` at the copy, so nothing you capture can write to your working
`.mmu_config`.

## One session, many screenshots

Parsing the Kconfig tree costs several seconds, so a session starts `menuconfig`
once, walks it, and captures along the way. In `doc_tools/shots.py` a session is a
function that receives a started driver and a `shot()` callback:

```python
def _purging_screens(mc, shot):
    mc.enter('Purging')
    shot('purging')
    mc.enter('Blobifier')
    shot('purging-blobifier')
    mc.back()

SESSIONS = [
    {
        'name': 'purging',
        'caption': 'Purging options, and the Blobifier sub-screen',
        'scenes': _purging_screens,
    },
]
```

Group screens belonging to one walkthrough into one session; start a new session when
the seed or the unit has to change.

### A getting-started page's images live next to the page

By convention, a page like `doc/GettingStartedWithBoxTurtle.md` keeps its images in
`doc/GettingStartedWithBoxTurtle/` — not in the shared `doc/images/` pool. Give the
session an `outdir` (relative to `doc/`) and it always writes there, regardless of
`--outdir`:

```python
SESSIONS = [
    {
        'name': 'getting-started-boxturtle',
        'caption': 'doc/GettingStartedWithBoxTurtle.md - first menuconfig pass',
        'scenes': _getting_started_boxturtle,
        'outdir': 'GettingStartedWithBoxTurtle',
        'seed': 'none',
    },
]
```

`seed: 'none'` there is deliberate, not the usual choice: that page is about the act
of choosing `MMU Type` and watching the warnings panel react, which only shows up if
the session starts before that choice is made. Most sessions want the `boxturtle`
default instead - see Seeds, above.

```bash
make shots ARGS='--only getting-started-boxturtle'
```

Embed the result centered at 70% width rather than plain Markdown `![]()` - full width
is wide for a 140-column capture sitting in prose, and GitHub renders the HTML fine
inside a `.md` file:

```html
<p align="center">
  <img src="GettingStartedWithBoxTurtle/01-first-run.png" alt="..." width="70%">
</p>
```

## Height looks after itself

Every shot fits the terminal to the screen in front of it, so no image ever contains
menuconfig's row of scroll arrows — the `↓↓↓↓` that tells a reader the menu is cut off
when in truth only the capture was — and none carries a band of dead space either.
Sessions do not set a height; the reported size per image (`100x26`) is what it chose.

It grows first, because a screen with arrows is cut off and nothing about how much is
hidden can be measured while it is; then it hands back the blank rows the menu window
is not using, down to a floor of **30 rows**. The floor is presentation, not a
technical limit — menuconfig lays out happily in about 15 — but a set of screenshots
reads badly at wildly different heights, and a two-item menu shrunk to fit looks like a
cropped fragment rather than the installer. Change it with `--min-rows`, or `min_rows`
on a session. The eight rows menuconfig reserves for the help pane below the separator
are fixed, so blank space *there* is overhead that no height can reclaim.

Reclaiming never goes all the way, either: **2 rows** (`GAP_ROWS` in
`doc_tools/capture.py`) always stay between the last menu item and the separator bar,
even on a menu that would otherwise fit exactly. A gap of zero reads as the help text
crowding the menu above it; autofit will GROW past a tight fit to make room for the
gap before it considers shrinking for the floor, so the two never fight each other.

Width is fixed, not fitted - there's no signal in the terminal comparable to a scroll
arrow that says "too narrow". The default is **110 columns**, wide enough that long
board names and pin lists sit on one line rather than wrapping; override with `--cols`
or `'cols'` on a session.

Two things are worth knowing if you touch this:

* A menu keeps its **scroll offset** across a resize. Coming back from a submenu on a
  short terminal leaves the list scrolled, and no amount of growing clears the
  up-arrows that go with it — the offset has to be reset (`g`), and the highlight put
  back afterwards.
* Autofit does nothing while the small **value editor** is open: resizing does not
  relayout the menu behind it, and the arrows the edit box draws itself mean the value
  is wider than the field, which no height fixes. `mc.edit()` therefore fits the menu
  before opening the box.

`--no-fit` (or `'fit': False` on a session) pins `--rows` instead. Either way, a shot
that ends up with arrows on it says so on stderr rather than shipping quietly.

Prefer `mc.enter()`, `mc.select()`, `mc.edit()` and `mc.step()`, which raise when the
expected screen does not arrive, over `mc.key()`, which tolerates a keypress that
changed nothing. A missed key otherwise yields a believable PNG of the wrong screen.

## Photographing an editor

`mc.edit('Display name')` opens a parameter's value editor and asserts that an editor
— not a submenu — actually appeared. `mc.write('Turtle Left')` replaces the field
contents, and `mc.cancel()` closes it without applying, so later screens in the same
session still show the machine the seed described.

## Exploring, before adding a session

`CAPTURE=1` swaps in the driver's own CLI. It navigates and then dumps the screen as
text — the fast way to find out what a menu looks like and what to assert on — and it
can capture mid-sequence with `shot:`, so a whole set of images can come out of one
command without editing a file:

```bash
make shots CAPTURE=1 ARGS='--dump'
make shots CAPTURE=1 ARGS='--keys "select:Purging,enter" --dump'
make shots CAPTURE=1 ARGS='--keys "enter:Purging,shot:/tmp/a.png,back,enter:MCU connection,shot:/tmp/b.png"'
make shots CAPTURE=1 ARGS='--keys "edit:Display name,type:Turtle Left,shot:/tmp/c.png,cancel"'
```

`--keys` takes a comma-separated list: `down`, `up`, `left`, `right`, `enter`, `esc`,
`back`, `space`, `pgdn`, `pgup`, `help`, plus `select:TEXT` (move the highlight),
`enter:TEXT` (select and open), `edit:TEXT` (open the value editor), `type:TEXT`,
`cancel`, `shot:PATH` and `repeat:down*5`.

Useful flags: `--cols`, `--seed`, `--unit`, `--multi-unit`, `--entry-point`, `--scale`,
`--expect TEXT` (fail unless it is on the final screen), `--min-rows` for the height
floor, and `--rows` / `--no-fit` to pin a height rather than let each shot fit itself.

## What is not reproducible

`Kconfig:107` globs `/dev/serial/by-id/*` and `Kconfig:110-118` asks
`canbus_query.py` what is on the CAN bus. Both read the machine doing the capture.
`KLIPPER_HOME` is pointed at a path that does not exist on a dev box so the CAN query
comes back empty, but the serial glob cannot be overridden: **regenerate these images
on a machine with no printer attached**, or the MCU screens will show your hardware.
