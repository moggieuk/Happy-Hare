# Happy Hare viewer

A standalone, build-free web application that animates Happy Hare's filament movement. It is plain
ES modules, CSS and one `index.html`; it talks to a Moonraker-compatible API and has no dependency on
the simulator.

## Modes
- **Live**: subscribes to Moonraker printer objects (`configfile`, `save_variables`, `mmu`, macros).
  Needs nothing from Happy Hare beyond `mmu.homing` and `mmu.config_changes`.
- **Near-live**: follows the `server.mmu.telemetry.*` stream and overlays the latest sensor readings.
- **Replay**: plays a saved telemetry recording (`.jsonl`).

The parameter and Toolhead Config editors send `MMU_TEST_CONFIG` / `SET_GCODE_VARIABLE` through
`printer.gcode.script`, so they work against a real printer and the simulator alike.

## The unit rack
Every MMU unit is drawn side by side on one rack under the Bowden exit, and a unit with many gates wraps into rows.
The rack slides so the selected spool stays under the exit (a virtual-selector unit centres on it), and a gate change
slides it along with the existing selection animation. Narrow arrows at the panel edges appear when part of a unit is
off screen; they scroll the rack by hand until the next gate selection.

## Simulator-only features
Profile switching, loading a recording into the simulator and sensor toggles use `sim.viewer.*` RPCs that only
the simulator provides. The viewer probes for them once on connect and hides those controls elsewhere.

## Running it
- With the simulator: `make console` serves it at `http://127.0.0.1:7125/sim/viewer/`.
- Any other static host works if the page can reach a Moonraker URL (the host must be allowed by Moonraker's
  `cors_domains`). Hosting on a real printer is not decided yet.

## Tests
`node --test viewer/*.test.mjs` (optional browser tests: `viewer/*.browser.mjs`, need Playwright).
