# Documentation tooling

Screenshots of the menuconfig installer, generated from the real thing rather than
captured by hand. `doc/capture.py` runs `menuconfig` against `installer/Kconfig` in a
pty, interprets what it draws, and renders the screen to a PNG. `doc/shots.py` is the
list of images the documentation needs.

Nothing here is installed on a printer or imported by Happy Hare, the installer or
the tests. The dependencies (`pyte`, `Pillow`) live in `doc/requirements.txt` and are
installed into `./venv` on demand by the `shots` target.

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
once, walks it, and captures along the way. In `doc/shots.py` a session is a function
that receives a started driver and a `shot()` callback:

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
        'rows': 30,
    },
]
```

Group screens belonging to one walkthrough into one session; start a new session when
the seed, the terminal size or the unit has to change (the terminal is sized when the
session starts and stays that way).

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

Useful flags: `--rows` (terminal height — the honest way to cut dead space out of a
short menu), `--cols`, `--seed`, `--unit`, `--multi-unit`, `--entry-point`, `--scale`,
`--expect TEXT` (fail unless it is on the final screen).

## What is not reproducible

`Kconfig:107` globs `/dev/serial/by-id/*` and `Kconfig:110-118` asks
`canbus_query.py` what is on the CAN bus. Both read the machine doing the capture.
`KLIPPER_HOME` is pointed at a path that does not exist on a dev box so the CAN query
comes back empty, but the serial glob cannot be overridden: **regenerate these images
on a machine with no printer attached**, or the MCU screens will show your hardware.
