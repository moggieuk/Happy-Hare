# utils/

Developer tools for Happy Hare. Everything here is a standalone script — none
of it is loaded by Klipper or by `make test`.

## Flowguard (sync-feedback-buffer) plotting and simulation

`sync_feedback_sim.py` is the shared engine behind both wrapper scripts below.
It imports the real production `SyncControllerConfig`/`SyncController` from
`extras/mmu/unit/mmu_sync_controller.py`, so plots and simulations reflect
actual controller behavior, not a mock.

### Plot real telemetry: `plot_sync_feedback.sh`

Plots a flowguard telemetry log recorded during a real print (written to
`~/printer_data/logs/sync_*.jsonl` when flowguard telemetry logging is
enabled — see the commented-out line in `config/base/mmu_parameters.cfg`).

```bash
./plot_sync_feedback.sh                    # picks from ~/printer_data/logs/sync_*.jsonl
./plot_sync_feedback.sh /path/to/sync_1.jsonl
make plot-sync                             # installs plotting deps and offers a gate picker
make plot-sync LOG=/path/to/sync_1.jsonl   # skip the picker
```

`make plot-sync` creates or reuses `./venv` and automatically installs the
dependencies in `utils/requirements.txt`. Override discovery or output with
`PLOT_LOG_DIR=/path/to/logs` and `PLOT_OUT=/path/to/graph.png`.

### Simulate: `sim_sync_feedback.sh`

Runs the controller against a synthetic sensor/extruder model instead of a
recorded log — useful for tuning or reasoning about controller behavior
without needing a printer. All options pass straight through to
`sync_feedback_sim.py`:

```bash
--sensor-type=[P|D|CO|TO]
--buffer-range-mm=          (default=8.0)
--buffer-max-range-mm       (default=12.0)
--initial-sensor=[random|neutral]
--stride-mm=10              (normal extruder movement between updates)
--tick-dt-s                 (default dt used only for manual 'tick', 'clog' and 'tangle', default: 1.0)
--rd-start                  (starting extruder rotation distance, default: 20.0)
--sensor-lag-mm             (lag in sensor reacting to movement, default: 0)
--chaos=2                   (simulates friction and jerky movements, multiple of buffer_max_range)
--sample-error=0.25         (simulates "late" updates from extruder movement Eg 0.25 = 100%-125% of stride)
--switch-hysteresis=0.2     (factor based on buffer_range)
--use-twolevel              (forces P type sensors to operation in twolevel mode instead of EKF default)
--log-debug                 (display debug trace log entries)
--out=<file>                (output PNG filename for plots, default: sim_plot.png)
--log=<file>                (simulator json log output, default: sim.jsonl)
```

Use `--chaos=0 --sample-error=0` for a "pure" simulation with no noise.

Examples:

```bash
# Realistic type-P proportional sensor simulation
./sim_sync_feedback.sh --sensor-type P --initial-sensor=random --stride-mm=2.5 --chaos=2 --sample-error=0.5 --sensor-lag=0

# Realistic type-CO switch sensor simulation
./sim_sync_feedback.sh --sensor-type CO --initial-sensor=random --stride-mm=2.5 --chaos=2 --sample-error=0.5 --sensor-lag=0 --switch-hysteresis=0.2
```

Both wrappers source `~/klippy-env/bin/activate` if present and put
`extras/mmu` on `PYTHONPATH` so the production controller module resolves.

## `mmu.log` load/unload tracing: `trace_load_unload.py`

Filters an `mmu.log` for `TRACE ENTRY` / `TRACE EXIT` method markers (and
`>` command lines) and prints an indented call tree annotated with
filament/encoder position — useful for debugging a load or unload sequence
without wading through the raw log.

```bash
python3 trace_load_unload.py mmu.log
python3 trace_load_unload.py < mmu.log
cat mmu.log | python3 trace_load_unload.py

# Save output for review/diffing
python3 trace_load_unload.py mmu.log > flow.txt
```
