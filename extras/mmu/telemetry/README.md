# Happy Hare movement telemetry

## Event subscriptions

`[mmu_telemetry]` is a separate Moonraker component; `mmu_server.py` retains its
Spoolman and file-processing responsibilities. The installer links both components
and adds `[mmu_telemetry]` to `moonraker.conf`. Existing installations need to rerun
the installer and restart Klipper and Moonraker. No Klipper or Moonraker fork is needed.

The viewer uses the same protocol with a real printer and the simulator:

* `server.mmu.telemetry.subscribe` returns a first replay batch with a baseline.
* `notify_mmu_telemetry` delivers subsequent batches in `params[0]`.
* `server.mmu.telemetry.ack` accepts `stream_id` and `delivery_id` after the
  consumer has applied the batch. Only one batch per subscriber is outstanding.
* `server.mmu.telemetry.unsubscribe` stops that connection's subscription.

These are authenticated Moonraker WebSocket endpoints, separate from printer
object subscriptions. Live view subscribes only to printer objects. Telemetry
playback subscribes to the event stream, or loads an offline file. Loading a file
or switching to live view unsubscribes from events. Missing component errors leave
file loading available. Simulator command execution still advances virtual time
without servicing sockets mid-command; recorded events are delivered afterwards.

Klipper's `extras/mmu/telemetry/api.py` exposes `mmu/telemetry/{subscribe,ack,unsubscribe}`.
Moonraker owns one upstream subscription, acknowledges accepted batches, and fans
out its own history to browsers. Browser acknowledgments only control that
browser's delivery; they do not hold up Klipper or another browser. An acknowledgment
means accepted into memory, not that animation or disk writes have completed.

Klipper keeps only records its consumers have not acknowledged: each acknowledgment folds the
acknowledged records into the capture's baseline. A consumer that stops acknowledging is bounded by a
256-record cap, after which it receives a replacement checkpoint with a new stream id. Moonraker
keeps a 128-record window so a browser that joins late sees recent events.

Delivery batches contain up to 128 records. Expired
cursors receive a replacement checkpoint instead of silently skipping records.
The checkpoint includes the parameters and state in force at that point, including
runtime configuration changes. Discarded motion cannot restore inferred geometry.

`server.mmu.telemetry.subscribe` accepts `history: "start"` (default) or
`history: "current"`. Start returns all *available* history, not records already
discarded. Current returns a checkpoint of the present state without retained
events; the viewer uses it when renewing its bounded local history. The simulator
uses the same rules but retains its session capture for diagnostics.

Moonraker subscribes to Klipper only while a browser is connected or a recording is running, and
releases the subscription when neither needs it. The first record is a freshly sampled current state,
not cached startup configuration. It cannot recover events before capture began.
While active, new records are delivered to subscribers at 4 Hz. The printer
thread performs no file I/O and never waits for a browser acknowledgment.

### Recording

Recording is controlled entirely from Klipper; there are no recording options in `moonraker.conf`:

```
MMU_TELEMETRY RECORD=continuous   # record everything until switched off
MMU_TELEMETRY RECORD=toolchange   # record the next load, unload or toolchange, then stop
MMU_TELEMETRY RECORD=off          # stop (no RECORD shows the current mode)
```

Klipper keeps the mode (it is not saved across a restart) and publishes it in the stream as
`printer.mmu.telemetry.record`. A mode change commits the settled state, then emits a `record_mode`
record, so Moonraker acts on it at the right place in the stream. If nobody is subscribed, Klipper
asks Moonraker to subscribe (`mmu_telemetry_wake`). A Moonraker that restarts while a recording
is requested reads `mmu/telemetry/status` at readiness and resumes. Moonraker persists the records
and the viewer only reads them.

Files live under Moonraker's data directory as `logs/mmu-telemetry.jsonl` with up to five backups
(`.1` to `.5`). Every capture is one slot: `continuous` starts a new file (rotating the previous
one), and each `toolchange` capture is its own file, starting at the `toolchange` record with the
checkpoint in force at that point and ending at `toolchange_end`. Switching from `continuous` to
`toolchange` closes the continuous file; after the single toolchange the mode is `off`. Arming
`toolchange` while a command is already running waits for the next command. `continuous` starts at a
checkpoint of the present state, which may fall inside a move. Size rotation (`max_log_size`
and `log_backups`, optional in `[mmu_telemetry]`) defers until an in-flight movement pair has completed.
A single record/checkpoint or movement pair can exceed the size threshold. Load a file in the viewer.

Checkpoint and file rotation logic lives in `mmu_telemetry_store.py`; its
`TelemetryWriter` handles the dedicated worker and bounded 64-batch queue.
The Moonraker component handles subscriptions and lifecycle only.
Queue overflow or write failure stops recording and raises a Moonraker warning;
it never blocks motion or silently continues a damaged recording. Recording is
best-effort, not crash-durable: buffered records may be lost if the process exits
abruptly.

Tests: the telemetry stream, adapters, store, WebUI bridge and browser transport
suites cover checkpoints, edited configuration, rotation, per-client backpressure,
cleanup and reconnects.

## Structured movement telemetry

MMU commands and macros invoked through `wrap_gcode_command` observe
`G1` commands with nonzero extrusion. Outside active printing, console and other
unscoped G1 extrusion is also observed, including while paused. Each produces one movement pair with
`source=mmu_gcode`, resolved E distance, extruder name and calling context.
`label` describes known moves such as final-load retraction; `commandline` retains
the original G1 text. Unannotated moves use that text as their label.
Absolute/relative extrusion, coordinate resets and flow overrides are respected.
During active printing, only the owning MMU execution context is observed;
ordinary print moves, `G0`, and arc-generated segments are excluded. A permanent
G1 wrapper is installed once at Klipper readiness, retaining the previous handler.
Unscoped printing moves and calls without subscribers immediately delegate without
reading coordinates. Scopes never change command registration, and errors always
clear their ownership state. Unscoped console events have an empty `context`.

These outcomes have `status=submitted`: positions describe accepted commands,
not measured physical completion. Estimated duration excludes acceleration and
queue planning. XYZ is used only to estimate the extrusion rate for coordinated
moves; XYZ positions are not included in these events. Existing MMU movement
operations suppress duplicate G1 events.

The simulator records physical extruder and synced motor steps for G-code and
MMU moves, independently of filament grip. Coordinate resets and saved-state
restoration do not count as motion. This lets Happy Hare measure real macro
retraction and calculate cut fragments without test-only step-count adjustments.

The viewer does not record individual filament or stepper samples. Model updates
still maintain filament positions and motor totals for movement snapshots.

Simulator macros that use simplified effects still produce modeled movement;
individual G1 events require an executed macro body. The console executes the
reference purge, `_MMU_CUT_TIP`, and `_MMU_FORM_TIP` bodies and their helpers, so
extrusion, retraction, push-back, and cooling moves are recorded individually with
G1 trace pills. The real cut motion helper emits `_MMU_SEND_TELEMETRY EVENT="cut"`
after blade contact and `_MMU_SEND_TELEMETRY EVENT="release"` after withdrawing the
blade. These commands only add ordered telemetry annotations: no motion, waits,
or MMU state changes. Without subscribers they return without emitting a record.
The existing `_MMU_EVENT EVENT="filament_cut"` still handles operational counting.

Both a printer and the simulator emit the same `cut` and `release` records with
unit, gate, cutter identity, and configured blade position. The simulator consumes
CUT to shorten its modeled filament; the viewer holds the blade through the rip
until RELEASE. It no longer identifies cutter contact from coordinates or speed. The
post-unload cutter is not covered by these hooks. The general unit-test harness
retains its simplified macro effects.

The viewer holds the severed fragment until an advancing filament end pushes it.
A withdrawn nozzle tip has a schematic cone and approximately 15mm hair; cutting
transfers that shape to the fragment and leaves the attached end square. Residual
melt stays at the nozzle. These shapes are illustrations, not measured tip geometry.


Happy Hare emits structured records directly in `move_filament()`, after
resolving speed/acceleration and gate overrides.
The stream is independent of the selected log level and works without the viewer.
TRACE (3) is the highest console and file log level. Existing configurations using
level 4 must change to 3. File logging still accepts -1 to disable it. Low-level
pin timing and drive-sync decisions use TRACE; motion details use telemetry.

Selector commands emit `selector_movement_start` / `selector_movement_outcome`
pairs with unit, selector type, actuator and move ID. Stepper records include
resolved speed/acceleration, requested and actual travel, and homing results.
Rail homing is one compound operation including retract/rehome, with trigger
travel separate from its rebased final position. Servo records include requested
angle or pulse width and duration; their outcome means submitted, not measured
mechanical completion. Errors are recorded before being propagated. The viewer
shows these diagnostics under **Show internal updates**, without treating selector
travel as filament movement.

Use **Save telemetry** in the viewer for simulator captures, or enable Moonraker
recording as described above. Source callbacks remain available via
`mmu.movement_telemetry.subscribe(callback)`; callbacks must not block the printer
thread. A failing callback is detached and logged without aborting motion.

Every record has `schema: happy_hare.motion`, `version: 1`, `session_id`,
`sequence`, `move_id`, `event` and a reactor-monotonic `timestamp` in seconds.
Pair records by session and move ID, not by adjacent lines:

- `movement_start`: source/label, unit/gate, motor mode and stepper names,
  signed requested distance (`requested_mm`), resolved feed speed (`speed_mm_s`),
  acceleration (`acceleration_mm_s2`, null for toolhead-managed acceleration),
  drive position/reference, homing endstop/trigger state, wait flag and sensor
  observations. Speed is a commanded setpoint, not instantaneous measured speed.
- `movement_outcome`: controller-reported `actual_mm`, end drive position,
  `homed`, encoder-measured travel and discrepancy, sensor observations,
  `elapsed_s`, error and status. Status distinguishes `submitted` from
  `completed` (homing or drained queue), `not_homed`, `error` and `incomplete`.
  Exceptional exits always close an emitted start; unavailable results are null.
  Rejected requests that never reach movement dispatch do not emit a start.

The console emits a `filament_pos_snapshot` after boot. Movement starts/outcomes
also include `filament_pos` and its readable name, so recording mid-session still
establishes the logical starting state. `filament_pos_changed` records previous
and new states (including silent changes), unit and gate; repeated assignments
are suppressed. This is Happy Hare's discrete progress state, not a millimetre
coordinate. The viewer displays it alongside the tip position.

The viewer has two independent modes, identified at the top left. Simulator
connections default to telemetry playback; other connections default to live view.
Explicit mode choices survive reconnects. To open a recording, select telemetry
playback and use **Load telemetry**:

- **Live view** uses Moonraker object queries/subscriptions only. It smoothly
  follows `mmu.filament_pos` and `mmu.bowden_progress`; transitions are cosmetic,
  not a reconstruction of motor travel. Heater targets, encoder readings,
  proportional buffer values and sensor states update directly from subscriptions.
- **Telemetry playback** uses only the loaded JSON/JSONL recording, or the
  simulator's piped telemetry stream. Current printer status never changes an
  old recording. Playback controls and the movement trace appear in this mode.
  While following the stream at rest (not paused, not animating, no move in
  progress) the viewer also subscribes to `mmu.sensors` and shows those readings
  on the live end, so a sensor toggled or triggered while idle appears at once
  instead of when the capture's next read records it (`overlay.js`). Nothing else is
  overlaid, and the recorded state carries the same values.

The Settings panel accepts a printer/simulator URL (default: the server hosting
this viewer). The parameter editor and Toolhead Config panel work on a real printer
because they only send `MMU_TEST_CONFIG` and `SET_GCODE_VARIABLE`; the Buffer and Encoder
panels, which set object attributes directly, are simulator-only.

Live view needs no Happy Hare specific configuration object. `live-config.js`
builds the machine description from state Klipper already publishes:

- `configfile.settings` for startup configuration: units, gates, selector type,
  toolhead, encoder and buffer, and every parameter section.
- `mmu.config_changes`, a dictionary of values changed since startup by
  `MMU_TEST_CONFIG`, keyed by config section then parameter. It is empty until
  something is edited, and overrides the startup value.
- `save_variables` for calibration such as bowden lengths.
- `mmu.sensors` for the active sensors (short names, requalified by the viewer),
  plus the existing `mmu_extruder`, `mmu_encoder` and proportional sensor objects.

Live view is approximate by design: it has no homing, grip or inactive-sensor state.
Telemetry mode carries those. In telemetry the same edits arrive as `state_delta`
records, produced from a `config_changed` event emitted where the parameter changes,
so recordings are replayable without a printer. The session baseline holds each full
parameter set once; it is not re-read per event.

`live-state.js` adapts subscriptions without retaining history; `telemetry.js`
reduces recordings. Both use `scene-state.js` for scene geometry and share the SVG
components. New movement recordings omit heater target changes; old recordings
containing these fields remain readable. Run the live browser regression with
`node --test viewer/live.browser.mjs` using the same Playwright/Chrome
settings as the spool browser test below.

The **Toolhead Config** panel appears when the machine has a toolhead section. A toolhead shared
by several units is listed once with those units, and edits are sent as `MMU_TEST_CONFIG`.

Buffers and encoders are simulator objects whose settings are not `MMU_TEST_CONFIG` parameters,
so they are edited from the simulator console instead of the viewer. `/buffer [NAME] [RANGE=mm]
[MAXRANGE=mm] [SPRING=none|tension|neutral|compression]` changes a buffer (and rebuilds its
physical model, keeping the filament where it is); `/encoder [NAME] [RESOLUTION=mm] ...` changes an
encoder's runtime numeric settings, never pins or MCU sampling. NAME is the object's own name,
which can differ from its unit and may be shared; it can be omitted when there is only one. With
no values the command prints the current ones. Each edit emits one `sim_settings_changed` event, so
the change is committed to the telemetry stream at once; reload the viewer to see it. Restart
restores the profile defaults.
Each selector movement (the stepper moving the carriage, the servo changing grip, rail homing) is a
visible step with its own **selector** pill, paired from the `selector_movement_start` and
`selector_movement_outcome` records. Like a filament move it is anchored at the start of the move
(the sequence and time of the start record), carries the outcome data, and its completion is an internal
marker; an unfinished start is held back until its outcome arrives. It has no animation of its own and no
artificial delay. While it
plays, or when you step onto it, the notice box shows a summary such as
`Selector: Positioning selector 4.2 → 46.2 mm (0.19s)`. After a stepper move the next step is normally the
gate change, which still owns the rack-slide animation: the box stays up through the slide, with
`· Gate 0 → 2` added, and until you leave that gate change. The notice is blue with an "i", unlike the amber
"!" warnings, and a sensor warning always keeps the warning look. A selector pair inside a filament move
stays internal, and a lone servo move is not left behind while following the stream at rest.
Configuration changes are included in telemetry. **Show internal updates** also
adds expandable raw telemetry details to each trace entry; movements expose both
the start and outcome messages without changing playback selection.

Sensor observations are full name-to-state maps at movement start and outcome:
true means triggered, false means clear, null means disabled. Absent sensors are
not listed. These are controller observations, not timestamped transitions inside
a move. The viewer estimates intervening crossings from geometry and preserves
recorded endpoint states.

Viewer snapshots retain `raw_state`/`raw_before` separately from displayed states.
A forward move with stationary extruder motors stops the displayed tip at the
configured gear position, while motor travel continues. The calibrated Bowden endpoint is the extruder gear position;
`toolhead_entry_to_extruder` locates the entry sensor upstream of it. Clear enabled presence
sensors bound occupied filament; buffer and encoder sensors are excluded. The
source does not report a continuous physical tail coordinate, so runout-tail
geometry is not reconstructed. These are
visual constraints: they do not change simulator physics, homing results or source
telemetry. Extruder-fed travel beyond the configured nozzle accumulates as
`extruded_mm` and grows a shaded purge coil. Retraction leaves deposited filament
in place and recovering that retraction does not count as new extrusion. The
recorded amount is input-filament length. The nozzle selector converts it to an
equivalent round strand using `length * (1.75 / nozzle_diameter)^2`, defaulting to
0.4mm, for the purge animation and strand-length label.
The non-modeled nozzle exit fills downward over 0.5 seconds after the true tip
reaches zero. Purged material blends to the incoming color over the next 2mm of
extrusion; waiting or retracting does not recolor material already deposited.

Coil size and blending are schematic, not a melt-flow model. A full coil falls
behind the lower screen and the next coil starts growing. Any actual extruder
retraction detaches accumulated purge within that movement's animation; gear-only
retraction does not. There is no separate purge-release animation event.
The final standalone-load G1 is annotated **Final load retraction**, without a
separate notification. `retracted_length_changed` records persisted
per-extruder changes. Extrusion totals remain cumulative across releases. Startup snapshots include `filament` context with the
configured residual, persisted per-extruder fragment length and retained color,
the selected gate color and persisted `retracted_length`. Missing colors are neutral gray. Retained material
is drawn at the nozzle and blends toward incoming color as material is extruded. Missing sensors supply no positional evidence.

State records share the same versioned stream and sequence numbers:

- `gate_changed` records previous/new gates and the selected unit, once per
  change. The viewer animates selection using captured per-gate colors.
- `filament_pos_changed` records the previous/new logical filament state and its
  name; the viewer displays these with a **state changed** pill.
- `drive_sync_changed` reports a successful
  drive mode change, with previous/new numeric modes and names and drive identity.
  Repeating the current mode does not emit another change.
- `espooler_started` covers continuous operation and bursts, with unit, gate,
  direction, requested/scaled output power and PWM/digital mode. A start while
  running replaces the previous direction/power setting.
- `espooler_stopped` marks the return to zero output, including burst completion
  or cancellation. Arming print assist without motor activity emits neither event.
  Repeated unchanged operations and rejected requests do not emit events.
  Power values are fractions, not percentages. These records describe accepted
  controller commands, not measured spool rotation or physical queue completion.

`toolchange` marks the start of `MMU_CHANGE_TOOL`, `MMU_LOAD`, and
`MMU_UNLOAD`, including the initiating command line and current tool/gate. `toolchange_end`
marks its end on every exit path, with the resulting tool, gate, `filament_pos` and a status of
`completed` or `error` (a raised error; errors the command handles itself still end as
`completed`). Both are emitted by the command registration (`toolchange=True`), only for the
outermost command, and nothing is emitted for `HELP=1`. The capture commits the settled state
just before `toolchange_end`, so the segment between the two replays from a known start to the
final state. Because the pair brackets the whole command, a refused command (disabled, not
calibrated, already loaded) emits an empty pair.
The viewer’s previous/next toolchange buttons navigate these markers; the
previous button returns to the current operation’s start before moving to an
earlier operation. Older recordings without markers disable these buttons.

State records carry the active `move_id` when emitted inside a started movement,
otherwise null. They do not open or close movement pairs. The viewer retains them
in its raw telemetry export. Gate, filament-position, and espooler changes also appear
in the movement trace. Espooler records update spool direction and activity without
adding a timed movement; diagnostic sync updates remain internal. Idle gear stepper
disable messages use TRACE.

`elapsed_s` measures time between source callbacks, including waits and simulator
pacing; it is not a physical motion-duration measurement. Drive travel is not proof
of physical filament movement, and a submitted move is not proof of completion.
Encoder travel is reported separately for that reason. Origin changes are not
filament travel; before/after drive positions share the stated reference.

The browser consumes these pairs and derives animation snapshots locally. The
console bridge captures source records without wrapping movement methods or
observing simulator geometry. `SourceViewer` owns capture and parameter controls;
the JavaScript replay tests cover derived animation behavior.
**Save motion telemetry** now downloads the compact v2 capture as JSONL.
The stream's first `session` record contains the schema, session identity and a baseline of relevant
MMU/machine state, calibration, sensors and toolhead macro dimensions. Later
`state_delta` records contain changed leaves only and precede the source event
that observed them. Paths are arrays of keys; `remove: true` deletes a value,
whereas `value: null` explicitly stores an unknown value. Edits that cancel before
the next read are omitted. State is read at every event and once more 0.25 s after the
last one (never in the middle of an open move), so a value assigned just after an event, such as a
selector's grip, is recorded as a trailing `state_delta` without any polling. A change that
raises no event of its own is captured by the next read, so it is stamped with that read's time.
A new session or expired cursor requires a reset.

`extras/mmu/telemetry/capture.py` owns this capture independently of the
simulator. Create `TelemetryCapture(mmu)` on the printer thread after readiness;
call `close()` when capture ends. Reads return detached copies. Source subscribers still receive the existing v1 callback
records. The recorder removes their repeated schema/session fields on export.

History is bounded. When an incremental cursor expires, `reset: true` accompanies
a replacement session baseline with `checkpoint: true`. That baseline restores
printer facts, but discarded movements cannot restore previously inferred purge
or fragment geometry; a consumer must restart its inference at that checkpoint.
Streamed and offline playback use only these captured facts and events. Neither loads
current printer settings to interpret old movement.

Coverage includes `move_filament()`, MMU-owned G1 extrusion, and explicit cutter
hooks, plus selector stepper and servo diagnostics. Simplified modeled macro
effects without source events are not animation inputs. The simulator and
real-printer WebSocket transports supply the same capture format.
The legacy simulator HTTP endpoint remains available for capture diagnostics.

Tests: `make test UT=test_mmu_movement_telemetry.py` and
`node --test viewer/*.test.mjs`, plus the capture and viewer suites.
The replay fixtures under `viewer/fixtures/` were captured with `ercf_vvd`
and `emu` console profiles using `MMU_LOAD`, `MMU_UNLOAD`, then `MMU_LOAD`.
They contain source records only. Tests compare batched live reduction with JSONL
loading and exercise every interpolated movement endpoint.

The optional spool browser regression compares retained SVG updates with fresh
renders across styles, fill levels, viewing angles, and rotation. With Playwright
and Chromium available, run `node --test viewer/spool.browser.mjs`.
Set `PLAYWRIGHT_MODULE` to its module path and `CHROME_PATH` to a Chrome executable
when using external installations. It also checks that motion preserves geometry
nodes and unchanged props do not write to the DOM.
