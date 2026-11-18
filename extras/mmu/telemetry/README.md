# Happy Hare movement telemetry

## Overview

Motion telemetry records what the MMU does (filament moves, selector and servo moves, extruder moves, state changes) so the
web app (for example the Happy Hare Visualizer) can replay it live or from a saved file. Three parts cooperate: Klipper produces the records, Moonraker
relays and stores them, and the web app displays them.

```
Klipper:   moves / selectors / G1 / state events -> capture (baseline + deltas) -> 0.25 s batches
               |                                                     ^ ack
Moonraker: shared upstream -> 128-record window -> browsers (WebSocket)  +  recorder -> JSONL files
               |                                                     ^ ack
Web app:   replay -> animated steps (live)   |   load a saved JSONL (offline)
```

### Klipper (Happy Hare): produces the events and the replay state

**Event sources.** Each emits a record only while someone is listening, so there is no cost when nothing is subscribed.
- filament moves, as start and outcome pairs
- selector stepper and servo moves
- G1 extrusion inside MMU commands and macros
- state events: filament position, gate change, espooler start and stop, cut and release marks from the macros, configuration changes
- `toolchange` and `toolchange_end`, which bracket the load, unload and change-tool commands

**Capture.** When a consumer subscribes, Klipper starts a capture.
- It holds a baseline of the full replay state plus ordered records.
- At every event it reads the state, diffs it against the last read, and writes a `state_delta` before the event record.
- It reads once more 0.25 s after the last event (never in the middle of a move). This catches values that settle late, such as the selector grip.
- It keeps only records its consumers have not acknowledged. A 256-record cap bounds a consumer that stops acknowledging.

**Delivery.**
- A 0.25 s timer sends one batch at a time to each subscriber.
- A batch is resent after 5 s without an acknowledgement.
- A failed capture is replaced and the consumer gets a fresh baseline.

**Recording control.**
- The mode lives in Klipper: `off`, `on`, or `toolchange` (record one command, then stop).
- It is set with `MMU_TELEMETRY MODE=...`. With no parameters the command reports the current mode and the default.
- It starts at `default_telemetry_mode` from `mmu.cfg` (also a menuconfig option under Logging, default `off`, hidden from `MMU_TEST_CONFIG`).
- It is exposed as the printer variable `mmu.telemetry_mode`.
- A mode change commits the settled state, then emits a `record_mode` record so consumers act on it at the right point in the stream.
- If Moonraker is not subscribed, Klipper asks it to subscribe (`mmu_telemetry_wake`) and logs a warning if it has not within 5 s.

**File I/O.** Klipper does none.

### Moonraker (`[mmu_telemetry]`): relay, history and files

**Upstream.**
- One subscription to Klipper, shared by all browsers.
- It subscribes only while a browser is connected or a recording is running, and releases it when neither needs it.
- It acknowledges each batch to Klipper and resubscribes if Klipper closes the stream.
- At startup it reads `mmu.telemetry_mode` to resume a recording.

**Browsers.**
- It keeps a 128-record window for late joiners.
- It fans batches out over WebSocket: `server.mmu.telemetry.subscribe`, `ack`, `unsubscribe`, with `notify_mmu_telemetry` pushes. Each browser has one batch in flight.

**Recording.** The recorder follows the in-band `record_mode` records.
- `on` writes a rolling `logs/mmu-telemetry.jsonl` with up to five backups (`.1` to `.5`).
- Each `toolchange` capture is its own file, opened at the `toolchange` record. Klipper follows the `toolchange_end` with `record_mode` `off`, which closes it. It takes one of the five slots.
- A refused command, with nothing between its start and end, opens no file and leaves the one-toolchange arming in place.
- Switching from `on` to `toolchange` closes the continuous file.
- All disk work runs on a worker thread. A write failure stops recording with a warning, and recording can start again afterwards.
- Optional settings in `[mmu_telemetry]`: `max_log_size` (default 10485760 bytes) and `log_backups` (default 5).

### Clients: display only

- A client subscribes to the stream through Moonraker, replays the baseline and the records that follow, and acknowledges each batch.
- It can also read a saved JSONL recording offline: the format is the same.
- Recording is controlled from Klipper. A client can show `mmu.telemetry_mode` and send `MMU_TELEMETRY MODE=...`, but it writes no files of its own.

### In the simulator

The simulator's bridge takes Moonraker's place. It runs the same stream protocol over its own WebSocket, and pumps the capture itself. Recording to files and the Record control do not apply there.

### Reading a recording

- The first record is a `session` baseline: the full replay state at that moment.
- `state_delta` records hold only the changed values (`path` and `value`), and come before the event that observed them.
- Event records say what happened and why (for example `movement_start` and `movement_outcome`, `selector_movement_*`, `filament_pos_changed`, `gate_changed`, `toolchange` and `toolchange_end`).
- Sync mode and retracted length have no events: they arrive only as `state_delta`.
- A `session` record in the middle of a file is a fresh baseline; a consumer restarts its replay there.

## Event subscriptions

`[mmu_telemetry]` is a separate Moonraker component; `mmu_server.py` retains its
Spoolman and file-processing responsibilities. The installer links both components
and adds `[mmu_telemetry]` (with its `max_log_size` and `log_backups` defaults) to `moonraker.conf`. New
installs also list `moonraker` in the update manager's `managed_services`, so an update restarts it; existing
installs keep their value, so restart Moonraker after updating. Existing installations need to rerun
the installer and restart Klipper and Moonraker. No Klipper or Moonraker fork is needed.

A client uses the same protocol with a real printer and the simulator:

* `server.mmu.telemetry.subscribe` returns a first replay batch with a baseline.
* `notify_mmu_telemetry` delivers subsequent batches in `params[0]`.
* `server.mmu.telemetry.ack` accepts `stream_id` and `delivery_id` after the
  consumer has applied the batch. Only one batch per subscriber is outstanding.
* `server.mmu.telemetry.unsubscribe` stops that connection's subscription.

These are authenticated Moonraker WebSocket endpoints, separate from printer
object subscriptions. A client that only needs current status subscribes to printer
objects and uses none of this. Simulator command execution still advances virtual time
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
events; a client can use it to renew a bounded local history. The simulator
uses the same rules but retains its session capture for diagnostics.

Moonraker subscribes to Klipper only while a browser is connected or a recording is running, and
releases the subscription when neither needs it. The first record is a freshly sampled current state,
not cached startup configuration. It cannot recover events before capture began.
While active, new records are delivered to subscribers at 4 Hz. The printer
thread performs no file I/O and never waits for a browser acknowledgment.

### Recording

Recording is controlled entirely from Klipper; there are no recording options in `moonraker.conf`:

```
MMU_TELEMETRY MODE=on           # record everything until switched off
MMU_TELEMETRY MODE=toolchange    # record the next load, unload or toolchange, then stop
MMU_TELEMETRY MODE=off          # stop
```

`default_telemetry_mode` (`off`, `on` or `toolchange`; menuconfig Logging, `mmu.cfg`, hidden from
`MMU_TEST_CONFIG`) is the mode after a restart. `MMU_TELEMETRY` with no parameters reports the
current mode and the default, which is set only in `mmu.cfg`.

Klipper keeps the mode (it is not saved across a restart) and exposes it as the printer variable
`mmu.telemetry_mode`: that is what a client shows as the recording mode and what a Moonraker that restarts reads at readiness to resume a recording. A mode change also commits the
settled state and emits a `record_mode` record, so Moonraker's recorder acts on it at the right place in the stream. If nobody
is subscribed, Klipper asks Moonraker to subscribe (`mmu_telemetry_wake`). If Moonraker has not started a requested
recording within five seconds (not running, or `[mmu_telemetry]` not loaded) Klipper logs a warning; for a recording that is
the configured default it allows thirty seconds after startup. Repeating the current
mode changes nothing: it does not rotate the log. Moonraker persists the records
and clients only read them.

Files live under Moonraker's data directory as `logs/mmu-telemetry.jsonl` with up to five backups
(`.1` to `.5`). Every capture is one slot: `on` starts a new file (rotating the previous
one), and each `toolchange` capture is its own file, starting at the `toolchange` record with the
checkpoint in force at that point. Klipper emits `record_mode` `off` straight after the
`toolchange_end`, which closes the file, so consumers only follow `record_mode`. Switching from `on` to
`toolchange` closes that file. Arming
`toolchange` while a command is already running waits for the next command. `on` starts at a
checkpoint of the present state, which may fall inside a move. Size rotation (`max_log_size`
and `log_backups` in `[mmu_telemetry]`) defers until an in-flight movement pair has completed.
A single record/checkpoint or movement pair can exceed the size threshold.

Checkpoint and file rotation logic lives in `mmu_telemetry_store.py`; its
`TelemetryWriter` handles the dedicated worker and bounded 64-batch queue.
The Moonraker component handles subscriptions and lifecycle only.
Queue overflow or write failure stops recording and raises a Moonraker warning;
it never blocks motion or silently continues a damaged recording. Recording is
best-effort, not crash-durable: buffered records may be lost if the process exits
abruptly.

Tests: the telemetry stream, adapters, store and WebUI bridge
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

The stream carries no individual filament or stepper samples.

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
CUT to shorten its modeled filament; a client can hold the blade through the rip
until RELEASE. It no longer identifies cutter contact from coordinates or speed. The
general unit-test harness retains its simplified macro effects.

The servo cutter at the MMU (`SERVO_CUTTER_ACTION`, run as the post-unload extension)
emits `_MMU_SEND_TELEMETRY EVENT="cut" CUTTER="mmu"` after each cut. Its record has
`cutter: mmu` and no blade position; there is no `release`. When a post-unload cutter is
configured the baseline holds `mmu_machine.mmu_cutter.feed_length`, the distance from the
gate parking position to the blade; otherwise `mmu_cutter` is null.

Happy Hare emits structured records directly in `move_filament()`, after
resolving speed/acceleration and gate overrides.
The stream is independent of the selected log level.
TRACE (3) is the highest console and file log level. A saved level 4 still loads
and logs as trace. File logging still accepts -1 to disable it. Low-level
pin timing and drive-sync decisions use TRACE; motion details use telemetry.

Selector commands emit `selector_movement_start` / `selector_movement_outcome`
pairs with unit, selector type, actuator and move ID. Stepper records include
resolved speed/acceleration, requested and actual travel, and homing results.
Rail homing is one compound operation including retract/rehome, with trigger
travel separate from its rebased final position. Servo records include requested
angle or pulse width and duration; their outcome means submitted, not measured
mechanical completion. Errors are recorded before being propagated. Selector
travel is not filament movement.

Record on a printer with `MMU_TELEMETRY` as described above. Source callbacks remain available via
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
are suppressed. This is Happy Hare's discrete progress state, not a millimeter
coordinate.

A client can build the machine description from state Klipper already publishes:

- `configfile.settings` for startup configuration: units, gates, selector type,
  toolhead, encoder and buffer, and every parameter section.
- `mmu.config_changes`, a dictionary of values changed since startup by
  `MMU_TEST_CONFIG`, keyed by config section then parameter. It is empty until
  something is edited, and overrides the startup value.
- `save_variables` for calibration such as bowden lengths.
- `mmu.sensors` for the active sensors (short names),
  plus the existing `mmu_extruder`, `mmu_encoder` and proportional sensor objects.

Status alone is approximate: it has no inactive-sensor state and reports nothing between the start and end of a move.
Telemetry carries those. In telemetry the same edits arrive as `state_delta`
records, produced from a `config_changed` event emitted where the parameter changes,
so recordings are replayable without a printer. The session baseline holds each full
parameter set once; it is not re-read per event.

Buffers and encoders are simulator objects whose settings are not `MMU_TEST_CONFIG` parameters,
so they are edited from the simulator console instead of a client. `/buffer [NAME] [RANGE=mm]
[MAXRANGE=mm] [SPRING=none|tension|neutral|compression]` changes a buffer (and rebuilds its
physical model, keeping the filament where it is); `/encoder [NAME] [RESOLUTION=mm] ...` changes an
encoder's runtime numeric settings, never pins or MCU sampling. NAME is the object's own name,
which can differ from its unit and may be shared; it can be omitted when there is only one. With
no values the command prints the current ones. Each edit emits one `sim_settings_changed` event, so
the change is committed to the telemetry stream at once; a client sees it when it next connects. Restart
restores the profile defaults.
Each selector movement (the stepper moving the carriage, the servo changing grip, rail homing) is a
`selector_movement_start` and `selector_movement_outcome` pair with its own `move_id`. Configuration changes
are included in telemetry.

Sensor observations are full name-to-state maps at movement start and outcome:
true means triggered, false means clear, null means disabled. Absent sensors are
not listed. These are controller observations, not timestamped transitions inside
a move.

The final standalone-load G1 is annotated **Final load retraction**, without a
separate notification. The persisted per-extruder `retracted_length` is part of the replay state, so its
changes arrive as `state_delta` records. Extrusion totals remain cumulative across releases. Startup snapshots include `filament` context with the
configured residual, persisted per-extruder fragment length and retained color,
the selected gate color and persisted `retracted_length`. Missing sensors supply no positional evidence.

State records share the same versioned stream and sequence numbers:

- `gate_changed` records previous/new gates and the selected unit, once per
  change.
- `filament_pos_changed` records the previous/new logical filament state and its
  name.
- Drive sync changes have no event: only the selected gate's drive changes mode, and the previous
  gate's drive is unsynced as the selection moves, so `printer.mmu.sync_mode` in the replay state
  (a `state_delta`) is the whole record.
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
final state. Both records carry the same `toolchange_id`. Because the pair brackets the whole command,
a refused command (disabled, not calibrated, already loaded) emits an empty pair; recording ignores
it: it opens no file and leaves `toolchange` mode armed for the next command.
State records carry the active `move_id` when emitted inside a started movement,
otherwise null. They do not open or close movement pairs. Espooler records report spool direction and
activity; they are not timed movements. Idle gear stepper
disable messages use TRACE.

`elapsed_s` measures time between source callbacks, including waits and simulator
pacing; it is not a physical motion-duration measurement. Drive travel is not proof
of physical filament movement, and a submitted move is not proof of completion.
Encoder travel is reported separately for that reason. Origin changes are not
filament travel; before/after drive positions share the stated reference.

A client such as the Visualizer consumes these pairs and derives animation snapshots locally. The
console bridge captures source records without wrapping movement methods or
observing simulator geometry. The bridge owns a `TelemetryCapture`.
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
call `close()` when capture ends. Reads return detached copies. Source subscribers receive only
the event fields (`move_id`, `event`, `timestamp` and the event's own); the capture adds the sequence and the session header.

History is bounded. When an incremental cursor expires, `reset: true` accompanies
a replacement session baseline with `checkpoint: true`. That baseline restores
printer facts, but discarded movements cannot restore previously inferred purge
or fragment geometry; a consumer must restart its inference at that checkpoint.
Streamed and offline playback use only these captured facts and events. Neither loads
current printer settings to interpret old movement.

Coverage includes `move_filament()`, MMU-owned G1 extrusion, and explicit cutter
hooks, plus selector stepper and servo diagnostics. Simplified modeled macro
effects without source events are not in the stream. The simulator and
real-printer WebSocket transports supply the same capture format.
The legacy simulator HTTP endpoint remains available for capture diagnostics.

Tests: `make test UT=test_mmu_movement_telemetry.py`, plus the capture, stream, API and store suites.
