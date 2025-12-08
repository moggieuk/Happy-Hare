# -*- coding: utf-8 -*-

# simulator.py
# Simulator/CLI for the filament tension controller (movement-based).
#
# Requires: controller.py (ControllerConfig, FilamentTensionControllerRD)

from __future__ import annotations
import argparse
import json
import math
import os
import time
import random
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt

from controller import ControllerConfig, FilamentTensionControllerRD

# ---------- optional readline for command history (Up/Down arrows) ----------
HAVE_READLINE = False
try:
    import readline  # POSIX / macOS
    HAVE_READLINE = True
except Exception:
    try:
        import pyreadline as readline  # legacy Windows
        HAVE_READLINE = True
    except Exception:
        readline = None  # plain input() fallback

def _setup_readline(history_limit: int = 500):
    if not HAVE_READLINE:
        return
    try:
        readline.set_history_length(history_limit)
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass
    except Exception:
        pass

def _add_history(line: str):
    if not HAVE_READLINE:
        return
    try:
        n = readline.get_current_history_length()
        if n == 0 or readline.get_history_item(n) != line:
            readline.add_history(line)
    except Exception:
        pass

# ------------------------------ JSON Logger -----------------------------

class SimLogger:
    """Append-only JSONL logger that clears on startup; tick starts at 0."""
    def __init__(self, path: str = "sim.jsonl", truncate_on_init: bool = True):
        self.path = os.path.abspath(path)
        self.records_in_session: List[Dict[str, Any]] = []
        if truncate_on_init:
            self.clear()
        else:
            self.tick = self._load_last_tick_plus_one()

    def clear(self):
        with open(self.path, "w", encoding="utf-8"):
            pass
        self.tick = 0
        self.records_in_session.clear()

    def _load_last_tick_plus_one(self) -> int:
        last_tick = -1
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if isinstance(rec, dict) and "tick" in rec:
                                last_tick = max(last_tick, int(rec["tick"]))
                        except Exception:
                            continue
            except Exception:
                pass
        return last_tick + 1

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        rec = dict(record)
        rec["tick"] = self.tick
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self.records_in_session.append(rec)
        self.tick += 1
        return rec

    def load_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not os.path.exists(self.path):
            return out
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

# -------------------------- Printer (physical model) --------------------

class FilamentSystemPrinter:
    """
    Printer model (normalized spring x_true; spring_mm = x_true * (buffer_range_mm/2)):
      x_true[k+1] = x_true[k] + (2 / buffer_range_mm) * Δ_rel_true
    where
      Δ_rel_true = d_ext * (extruder_rd_true / rd_prev - 1.0)
    This is reference-free and depends only on the *hardware* RD and the RD actually
    in effect during this step (rd_prev). It avoids any reaction when the controller
    changes its nominal baseline.
    """
    def __init__(self, controller: FilamentTensionControllerRD, extruder_rd_true: Optional[float] = None, initial_spring_mm: float = 0.0, chaos: float = 0.0):
        self.ctrl = controller
        self.buffer_range_mm = controller.cfg.buffer_range_mm
        self.buffer_max_range_mm = controller.cfg.buffer_max_range_mm
        self.K = 2.0 / self.buffer_range_mm
        self._norm_clip = max(1e-9, self.buffer_max_range_mm / self.buffer_range_mm)
        x0 = (2.0 * initial_spring_mm) / self.buffer_range_mm
        self.x_true = max(-self._norm_clip, min(self._norm_clip, x0))
        rd_ref = controller.cfg.rd_start
        self.extruder_rd_true = extruder_rd_true if extruder_rd_true is not None else rd_ref

        self.chaos = max(0.0, min(2.0, float(chaos)))
        self.x_meas = self.x_true

    def set_extruder_rd_true(self, rd_true: float):
        self.extruder_rd_true = float(rd_true)

    def spring_mm(self) -> float:
        return self.x_true * (self.buffer_range_mm / 2.0)

    def measure(self) -> float | int:
        if self.chaos <= 1e-12:
            self.x_meas = self.x_true
        else:
            # Move measured position toward true with a random "jerk".
            # Max jerk (mm) scales with chaos in [0..2] up to buffer_max_range_mm.
            jerk_mm_max = self.chaos * self.buffer_max_range_mm
            draw_mm = random.random() * jerk_mm_max
            # If we "hit the limit", friction is overcome -> snap close to true.
            if draw_mm >= (self.buffer_max_range_mm - 1e-12):
                self.x_meas = self.x_true
            else:
                gap_norm = self.x_true - self.x_meas
                gap_mm = abs(gap_norm) * (self.buffer_range_mm / 2.0)
                if gap_mm > 1e-12:
                    move_mm = min(draw_mm, gap_mm)
                    move_norm = (2.0 / self.buffer_range_mm) * move_mm
                    self.x_meas += math.copysign(move_norm, gap_norm)
                    self.x_meas = min(max(self.x_meas, -self._norm_clip), self._norm_clip)
        # -------------------------------------------------------------------

        if self.ctrl.cfg.sensor_type == "P":
            return max(-1.0, min(1.0, self.x_meas))  # <-- CHANGED: x_meas
        thr = self.ctrl.cfg.flowguard_extreme_threshold
        if self.ctrl.cfg.sensor_type == "D":
            if self.x_meas >= thr:   # <-- CHANGED: x_meas
                return 1
            if self.x_meas <= -thr:  # <-- CHANGED: x_meas
                return -1
            return 0
        if self.ctrl.cfg.sensor_type == "CO":
            return 1 if self.x_meas >= thr else 0  # <-- CHANGED: x_meas
        if self.ctrl.cfg.sensor_type == "TO":
            return -1 if self.x_meas <= -thr else 0  # <-- CHANGED: x_meas
        return 0

# ------------------------------- Plotting -------------------------------

def plot_sim(
    records: List[Dict[str, Any]],
    out_path: Optional[str] = None,
    dt_s: Optional[float] = None,
    sensor_label: Optional[str] = None,   # "A" or "B"
    stop_on_fg_trip: bool = True,
    rd_start: Optional[float] = None,
    show_rd_true: bool = True,
    show_ticks: bool = False,
):
    """Plot RD (left axis), sensor input/UI (right axis), Bowden/Buffer spring (2nd right), and autotune-applied markers."""
    if not records:
        raise ValueError("No records to plot.")

    RD_COLOR = "tab:blue"
    SENSOR_COLOR = "tab:orange"
    RD_TRUE_COLOR = "0.3"     # grey
    EVENT_COLOR = "0.5"
    SPRING_COLOR = "red"      # fine red dotted

    RD_MAIN_LW = 2.0
    SENSOR_INPUT_LW = 1.0
    SENSOR_UI_LW = 0.8 
    RD_TRUE_LW = 1.0
    TRIPBOX_ZORDER = 1000
    AUTOTUNE_ZORDER = 1001        # ensure dots are always on top

    # Prefer absolute t_s if present, else reconstruct from dt_s grid
    have_ts = all(("meta" in r) and ("t_s" in r["meta"]) for r in records)
    if have_ts:
        t_axis = [float(r["meta"]["t_s"]) for r in records]
    else:
        if dt_s is None:
            dt_s = records[0].get("meta", {}).get("dt_s", 1.0)
        t_axis = []
        t_acc = 0.0
        for r in records:
            step_dt = r.get("meta", {}).get("dt_s")
            if step_dt is None:
                step_dt = dt_s
            t_axis.append(t_acc)
            t_acc += float(step_dt)

    rd = [r["output"]["rd_applied"] for r in records]
    z  = [r["stimulus"]["sensor_reading"] for r in records]
    z_ui_raw = [r["output"]["sensor_ui"] for r in records]
    z_ui = [max(-1.0, min(1.0, v)) for v in z_ui_raw]

    rd_true_series = [r.get("truth", {}).get("rd_true") for r in records]
    spring_series  = [r.get("truth", {}).get("spring_mm") for r in records]

    clog   = [bool(r["output"]["flowguard"]["clog"]) for r in records]
    tangle = [bool(r["output"]["flowguard"]["tangle"]) for r in records]

    # Autotune markers (where controller reported a new default RD)
    autotune_applied = []
    for i, r in enumerate(records):
        auto_rd = r["output"].get("autotune_rd")
        if auto_rd is not None:
            autotune_applied.append((i, float(auto_rd)))

    first_trip_idx = next((i for i, (c, t) in enumerate(zip(clog, tangle)) if c or t), None)

    # Capture trip kind/reason *before* truncation so we can show a box
    trip_kind = None
    trip_reason = None
    if first_trip_idx is not None:
        trip_kind = "CLOG" if clog[first_trip_idx] else "TANGLE"
        trip_reason = records[first_trip_idx]["output"]["flowguard"].get("reason")

    end_idx = (first_trip_idx + 1) if (stop_on_fg_trip and first_trip_idx is not None) else len(records)
    t_axis = t_axis[:end_idx]
    rd = rd[:end_idx]
    z = z[:end_idx]
    z_ui = z_ui[:end_idx]
    rd_true_series = rd_true_series[:end_idx] if rd_true_series else None
    spring_series  = spring_series[:end_idx] if spring_series else None
    autotune_applied = [(i, v) for (i, v) in autotune_applied if i < end_idx]

    fig, ax_rd = plt.subplots(figsize=(12, 6))
    ax_sensor = ax_rd.twinx()
    ax_spring = ax_rd.twinx()
    ax_spring.spines["right"].set_position(("axes", 1.12))
    ax_spring.spines["right"].set_visible(True)
    ax_spring.set_frame_on(True)

    if len(t_axis) >= 2:
        if show_ticks:
            ax_rd.plot(t_axis, rd, label="rotation_distance", linewidth=RD_MAIN_LW, color=RD_COLOR, marker="o", markersize=2.5, markevery=1)
        else:
            ax_rd.plot(t_axis, rd, label="rotation_distance", linewidth=RD_MAIN_LW, color=RD_COLOR)

    elif t_axis:
        ax_rd.scatter(t_axis, rd, label="rotation_distance", color=RD_COLOR, zorder=3)

    if show_rd_true and rd_true_series and all(v is not None for v in rd_true_series):
        if len(t_axis) >= 2:
            ax_rd.plot(t_axis, rd_true_series, label="rd_true (expected)",
                       linestyle=":", linewidth=RD_TRUE_LW, color=RD_TRUE_COLOR)
        elif t_axis:
            ax_rd.scatter(t_axis, rd_true_series, label="rd_true (expected)", color=RD_TRUE_COLOR, zorder=3)

    ax_rd.set_ylabel("Rotation Distance (mm)")
    ax_rd.set_xlabel("Time (s)")
    ax_rd.grid(True, axis="both", alpha=0.3)

    rd0 = rd[0] if rd else 0.0
    rd_min_required = rd0 * 0.75
    rd_max_required = rd0 * 1.25
    candidates_min = [rd_min_required]
    candidates_max = [rd_max_required]
    if rd:
        candidates_min.append(min(rd)); candidates_max.append(max(rd))
    if show_rd_true and rd_true_series and all(v is not None for v in rd_true_series):
        candidates_min.append(min(rd_true_series)); candidates_max.append(max(rd_true_series))
    rd_min = min(candidates_min)
    rd_max = max(candidates_max)
    if rd_min == rd_max:
        span = abs(rd0) * 0.25 if rd0 != 0 else 1.0
        rd_min, rd_max = rd0 - span, rd0 + span
    ax_rd.set_ylim(rd_min, rd_max)

    if autotune_applied:
        t_marks = [t_axis[i] for (i, _) in autotune_applied]
        y_marks = [v for (_, v) in autotune_applied]
        ax_rd.scatter(
            t_marks, y_marks, marker="o", s=32, color="tab:green",
            label="RD autotune applied", zorder=AUTOTUNE_ZORDER
        )

    if len(t_axis) >= 2:
        ax_sensor.plot(t_axis, z,    label="Simulator: sensor_input",          linewidth=SENSOR_INPUT_LW, color=SENSOR_COLOR)
        ax_sensor.plot(t_axis, z_ui, label="sensor_ui (controller)", linewidth=SENSOR_UI_LW,  linestyle="--", color=SENSOR_COLOR)
    elif t_axis:
        ax_sensor.scatter(t_axis, z,    label="Simulator: sensor_input",          color=SENSOR_COLOR, zorder=2)
        ax_sensor.scatter(t_axis, z_ui, label="sensor_ui (controller)", color=SENSOR_COLOR, zorder=2)
    ax_sensor.set_ylabel("Sensor")
    ax_sensor.set_ylim(-1.1, 1.1)
    ax_sensor.axhline(0.0, linewidth=1.0, alpha=0.4, color="0.5", zorder=0)
    ax_sensor.grid(True, axis="y", alpha=0.2)

    has_spring = spring_series and any(v is not None for v in spring_series)
    if has_spring:
        y_spring = [float("nan") if v is None else float(v) for v in spring_series]
        finite_vals = [v for v in y_spring if not (math.isnan(v) or math.isinf(v))]
        span = max(abs(min(finite_vals)), abs(max(finite_vals))) if finite_vals else 1.0
        lim = max(span * 1.1, 0.5)
        ax_spring.set_ylim(-lim, +lim)
        if len(t_axis) >= 2:
            ax_spring.plot(t_axis, y_spring, label="Simulator: Bowden/Buffer spring (mm)",
                           linestyle=":", linewidth=1.0, color="red")
        elif t_axis:
            ax_spring.scatter(t_axis, y_spring, label="Simulator: Bowden/Buffer spring (mm)", color="red", zorder=2)
        ax_spring.axhline(0.0, linewidth=1.0, alpha=0.5, color="red")
        ax_spring.set_ylabel("Simulator: Bowden/Buffer spring (mm)")

    for i, (is_clog, is_tangle) in enumerate(zip(clog[:end_idx], tangle[:end_idx])):
        if is_clog or is_tangle:
            ax_rd.axvline(t_axis[i], linestyle=":", alpha=0.7, color="0.5")

    sensor_txt = f"Type {sensor_label}" if sensor_label else "Sensor"
    if stop_on_fg_trip and first_trip_idx is not None:
        plt.title(f"Filament Sync Simulation — {sensor_txt} | STOPPED at {trip_kind}")
    else:
        plt.title(f"Filament Sync Simulation — {sensor_txt}")

    if stop_on_fg_trip and first_trip_idx is not None and trip_reason:
        ax_spring.text(
            0.01, 0.99,
            f"{trip_kind} reason:\n{trip_reason}",
            transform=ax_rd.transAxes,
            va="top", ha="left",
            fontsize=9,
            wrap=True,
            bbox=dict(boxstyle="round", facecolor="0.92", edgecolor="0.6", alpha=0.95),
            zorder=1000,
            clip_on=False,
        )

    lines_l, labels_l = ax_rd.get_legend_handles_labels()
    lines_r1, labels_r1 = ax_sensor.get_legend_handles_labels()
    lines_r2, labels_r2 = ax_spring.get_legend_handles_labels()
    ax_rd.legend(lines_l + lines_r1 + lines_r2, labels_l + labels_r1 + labels_r2, loc="lower left")

    plt.tight_layout()
    if out_path:
        plt.savefig(out_path, dpi=150)
        plt.close(fig)
        print(f"Saved plot to {out_path}")
    else:
        plt.show()

# ------------------------------ CLI Helpers ----------------------------

def _print_cli_help():
    print("""
Commands (history enabled — use Up/Down arrows):
  t|tick <d_ext_mm> [<sensor|auto>]  - Manual one update. If sensor omitted or 'auto', uses printer's pre-move sensor.
  sim <v_mm_s> <time_s> [rd]         - Simulate average extruder speed for time. Uses --stride-mm chunk size per update.
  rd <value>                         - Set printer's *true* extruder rotation_distance immediately.
  clog                               - Realistic compression-extreme test (build + stuck), stop on FlowGuard.
  tangle                             - Realistic tension-extreme test (build + stuck), stop on FlowGuard.
  clear                              - Reset controller (full), printer spring, and sim.jsonl (tick=0).
  p | plot                           - Save plot to sim_plot.png (always saves to file).
  d | display                        - Display plot window (does not save).
  status                             - Show controller/printer state
  quit | q                           - Plot (save) and exit
""")

def _summary_print(ctrl: FilamentTensionControllerRD, last_sensor, last_sensor_ui, last_flowguard: Optional[Dict[str, Any]], spring_mm: float):
    sensor_str = "N/A" if last_sensor is None else (f"{last_sensor:.3f}" if isinstance(last_sensor, float) else str(last_sensor))
    sensor_ui_str = "N/A" if last_sensor_ui is None else f"{last_sensor_ui:.3f}"
    if isinstance(last_flowguard, dict):
        fg_str = f"clog={last_flowguard.get('clog', False)}, tangle={last_flowguard.get('tangle', False)}, reason={last_flowguard.get('reason')}"
    else:
        fg_str = "N/A"
    print(f"SUMMARY: RD={ctrl.rd_current:.4f} | sensor={sensor_str} | sensor_ui={sensor_ui_str} | Bowden/Buffer spring={spring_mm:.3f} mm | FlowGuard: {fg_str}")

def _plot_from_log(cfg: ControllerConfig, logger: SimLogger, *, mode: str, out_path: str = "sim_plot.png", show_ticks: bool = False):
    records = logger.load_all()
    if not records:
        print("sim.jsonl is empty; nothing to plot.")
        return
    try:
        if mode == "save":
            plot_sim(
                records,
                out_path=out_path,
                sensor_label=cfg.sensor_type,
                stop_on_fg_trip=True,
                rd_start=cfg.rd_start,
                show_rd_true=True,
                show_ticks=show_ticks,
            )
        else:
            plot_sim(
                records,
                out_path=None,
                sensor_label=cfg.sensor_type,
                stop_on_fg_trip=True,
                rd_start=cfg.rd_start,
                show_rd_true=True,
                show_ticks=show_ticks,
            )
    except Exception as e:
        print(f"Plotting failed: {e}")

# -------------------------- Extreme Test (realistic) -------------------

def _forced_extreme_test(
    ctrl: FilamentTensionControllerRD,
    logger: SimLogger,
    printer: FilamentSystemPrinter,
    kind: str,
    stride_mm: float,
    dt_s_step: float,
    sim_time_s: float,
) -> tuple[List[Dict[str, Any]], float]:
    """
    Two-phase realistic extreme test with plant-side fault prelude.
    Plant evolution uses Δ_rel_true = d_ext * (rd_true / rd_prev - 1.0) with
    a fault applied to the appropriate term (downstream vs upstream).
    Always passes an absolute timestamp into controller.update().
    """
    records: List[Dict[str, Any]] = []

    build_mm = max(stride_mm, ctrl.cfg.buffer_range_mm / 2.0)
    # --- FIX 1: always extrude forward (positive) for both tests ---
    d_ext_build = (+build_mm)  # clog and tangle both use +feed here

    fault_frac = 0.35  # 35% throughput loss while ramping to the peg

    norm_limit = max(1e-9, ctrl.cfg.buffer_max_range_mm / ctrl.cfg.buffer_range_mm)
    peg_thr = max(0.9, ctrl.cfg.flowguard_extreme_threshold)
    K = 2.0 / ctrl.cfg.buffer_range_mm

    # Phase 1: RAMP-IN with fault
    ramp_ticks_max = 400
    peg_need = 2
    peg_count = 0

    for _ in range(ramp_ticks_max):
        z_in = printer.measure()

        rd_prev = ctrl.rd_current
        sim_time_s += dt_s_step
        out = ctrl.update(d_ext_build, z_in, t_s=sim_time_s)

        ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
        base_gear = ratio * d_ext_build

        if kind == "clog":
            # Upstream restriction → effective push reduced (compression builds)
            d_ext_eff = (1.0 - fault_frac) * d_ext_build
            delta_rel = base_gear - d_ext_eff
        else:  # tangle
            # Downstream drag → effective gear motion reduced (tension builds)
            # --- FIX 2: reduce gear motion (not increase) when extruding forward ---
            gear_eff = (1.0 - fault_frac) * base_gear
            delta_rel = gear_eff - d_ext_build

        printer.x_true += K * delta_rel
        printer.x_true = min(max(printer.x_true, -norm_limit), norm_limit)
        spring_now_mm = printer.spring_mm()

        rec = {
            "stimulus": {"extruder_delta_mm": d_ext_build, "sensor_reading": z_in},
            "output": {
                "rd_applied": out["rd_applied"],
                "sensor_ui": out["sensor_expected"],
                "flowguard": out["flowguard"],
                "autotune_rd": out.get("autotune_rd"),
                "autotune_note": out.get("autotune_note"),
            },
            "truth": {"rd_true": printer.extruder_rd_true, "spring_mm": spring_now_mm},
            "meta": {"dt_s": dt_s_step, "t_s": sim_time_s},
        }
        logger.append(rec); records.append(rec)

        if ctrl.cfg.sensor_type == "P":
            pegged_now = abs(printer.x_true) >= peg_thr
        elif ctrl.cfg.sensor_type == "D":
            m = printer.measure()
            pegged_now = (m == 1 and kind == "clog") or (m == -1 and kind == "tangle")
        elif ctrl.cfg.sensor_type == "CO":
            if kind == "clog":
                pegged_now = (printer.measure() == 1)
            else:  # tangle side is unseen by sensor → use plant state
                pegged_now = (printer.x_true <= -peg_thr)
        else:  # "TO"
            if kind == "tangle":
                pegged_now = (printer.measure() == -1)
            else:  # clog side is unseen by sensor → use plant state
                pegged_now = (printer.x_true >= peg_thr)

        peg_count = peg_count + 1 if pegged_now else 0
        if peg_count >= peg_need:
            # Pin to the *physical* extreme for clarity; one-sided sensors then read 0 on the unseen side.
            if ctrl.cfg.sensor_type == "CO":
                printer.x_true = +norm_limit if kind == "clog" else -norm_limit
            elif ctrl.cfg.sensor_type == "TO":
                printer.x_true = -norm_limit if kind == "tangle" else +norm_limit
            else:  # P/D
                printer.x_true = +norm_limit if kind == "clog" else -norm_limit
            break

        if out["flowguard"]["clog"] or out["flowguard"]["tangle"]:
            print("FlowGuard detected during ramp-in.")
            return records, sim_time_s

    # Phase 2: STUCK (hard jam; sensor pegged)
    stuck_ticks_max = 120
    for _ in range(stuck_ticks_max):
        # Set stuck state first so the *measured* sensor reflects the stuck condition this tick.
        if ctrl.cfg.sensor_type == "CO":
            printer.x_true = +norm_limit if kind == "clog" else -norm_limit
        elif ctrl.cfg.sensor_type == "TO":
            printer.x_true = -norm_limit if kind == "tangle" else +norm_limit
        else:  # P/D
            printer.x_true = +norm_limit if kind == "clog" else -norm_limit

        z_in = printer.measure()

        rd_prev = ctrl.rd_current
        sim_time_s += dt_s_step
        out = ctrl.update(d_ext_build, z_in, t_s=sim_time_s)

        # Keep spring pinned
        spring_now_mm = printer.spring_mm()

        rec = {
            "stimulus": {"extruder_delta_mm": d_ext_build, "sensor_reading": z_in},
            "output": {
                "rd_applied": out["rd_applied"],
                "sensor_ui": out["sensor_expected"],
                "flowguard": out["flowguard"],
                "autotune_rd": out.get("autotune_rd"),
                "autotune_note": out.get("autotune_note"),
            },
            "truth": {"rd_true": printer.extruder_rd_true, "spring_mm": spring_now_mm},
            "meta": {"dt_s": dt_s_step, "t_s": sim_time_s},
        }
        logger.append(rec); records.append(rec)

        if out["flowguard"]["clog"] or out["flowguard"]["tangle"]:
            print("FlowGuard detected in stuck phase.")
            break

    return records, sim_time_s

# ---------------------------------- CLI --------------------------------

def _run_cli():
    random.seed(time.time_ns())
    _setup_readline(history_limit=500)

    ap = argparse.ArgumentParser(description="Filament Tension Controller + Printer Simulator CLI (movement-based)")
    ap.add_argument("--sensor-type", choices=["P", "D", "CO", "TO"], default="P")
    ap.add_argument("--buffer-range-mm", type=float, default=8.0)
    ap.add_argument("--buffer-max-range-mm", type=float, default=12.0)

    ap.add_argument("--use-twolevel", dest="use_twolevel", action="store_true", help="Enable user two-level behavior")
    ap.add_argument("--no-use-twolevel", dest="use_twolevel", action="store_false")
    ap.set_defaults(use_twolevel=False)

    ap.add_argument("--tick-dt-s", type=float, default=0.5, help="default dt used only for manual 'tick' and extreme tests")
    ap.add_argument("--rd-start", type=float, default=20.0)
    ap.add_argument("--sensor-lag-mm", type=float, default=0.0)
    ap.add_argument("--stride-mm", type=float, default=5.0, help="movement per controller update during 'sim' and extreme tests")

    ap.add_argument("--initial-sensor", choices=["neutral", "random"], default="neutral", help="Initial sensor reading used for startup/reset (default: neutral).")
    ap.add_argument("--chaos", type=float, default=0.0, help="Stick-slip in measured sensor: 0.0=exact (today), 2.0=max jerk.")
    ap.add_argument("--show-ticks", dest="show_ticks", action="store_true", help="Show individual updates on plot")

    args = ap.parse_args()

    cfg = ControllerConfig(
        buffer_range_mm=args.buffer_range_mm,
        buffer_max_range_mm=args.buffer_max_range_mm,
        use_twolevel_for_type_pd=args.use_twolevel,
        sensor_type=args.sensor_type,
        rd_start=args.rd_start,
        sensor_lag_mm=args.sensor_lag_mm,
    )
    default_dt_s = float(args.tick_dt_s)

    ctrl = FilamentTensionControllerRD(cfg)
    logger = SimLogger("sim.jsonl", truncate_on_init=True)
    printer = FilamentSystemPrinter(ctrl, extruder_rd_true=None, initial_spring_mm=0.0, chaos=max(0.0, min(1.0, args.chaos)))

    # --------- ADDED: set initial sensor state (neutral/random) BEFORE reset ----------
    if args.initial_sensor == "random":
        thr = cfg.flowguard_extreme_threshold
        norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)

        if cfg.sensor_type == "P":
            x0 = random.uniform(-1.0, 1.0)
        elif cfg.sensor_type == "D":
            choice = random.choice([-1, 0, 1])
            if choice == 0:
                x0 = random.uniform(-0.8 * thr, 0.8 * thr)
            elif choice == 1:
                x0 = min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = -min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
        elif cfg.sensor_type == "CO":
            choice = random.choice([0, 1])
            if choice == 1:
                x0 = min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = random.uniform(-min(norm_clip, thr - 0.05), thr - 0.05)
        elif cfg.sensor_type == "TO":
            choice = random.choice([0, -1])
            if choice == -1:
                x0 = -min(norm_clip, random.uniform(thr + 0.05, thr + 0.5))
            else:
                x0 = random.uniform(-thr + 0.05, min(norm_clip, thr - 0.05))
        else:
            x0 = 0.0

        printer.x_true = max(-norm_clip, min(norm_clip, x0))
        printer.x_meas = printer.x_true  # start measured at modeled state
    # ---------------------------------------------------------------------

    # Simulation clock (absolute time seconds since start)
    sim_time_s = 0.0
    # Timestamped reset at start (use *current* printer reading which matches initial mode)
    ctrl.reset(rd_init=cfg.rd_start, sensor_input=printer.measure(), t_s=sim_time_s)

    # Show whichever attribute exists on cfg
    print("=== Filament Tension Controller CLI (movement-based) ===")
    print(f" Sensor Type           : {cfg.sensor_type}")
    print(f" Use TwoLevel          : {cfg.use_twolevel_for_type_pd}")
    print(f" Initial sensor mode   : {args.initial_sensor}")
    print(f" Chaos factor          : {args.chaos}")
    print(f" Buffer Range (sensor) : {cfg.buffer_range_mm} mm")
    print(f" Buffer Max Range      : {cfg.buffer_max_range_mm} mm  (physical clamp)")
    print(f" Default dt            : {default_dt_s} s  (manual 'tick' & extreme tests)")
    print(f" RD start              : {cfg.rd_start} mm")
    print(f" Sensor lag            : {cfg.sensor_lag_mm} mm")
    print(f" Stride per update     : {args.stride_mm} mm  (sim/extr. tests)")
    print(f" JSON log              : {logger.path}")
    if not HAVE_READLINE:
        print(" (Tip: install 'pyreadline3' on Windows to enable Up/Down history)")
    _print_cli_help()

    # <-- ADDED: initialize last_* so summaries work without an initial update()
    last_sensor = None
    last_sensor_ui = None
    last_flowguard: Optional[Dict[str, Any]] = None
    # ---------------------------------------------------------------------

    while True:
        try:
            line = input("cmd> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            _plot_from_log(cfg, logger, mode="save", show_ticks=args.show_ticks)  # save on exit
            break

        if not line:
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        _add_history(line)
        low = line.lower()

        if low in ("q", "quit", "exit"):
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            _plot_from_log(cfg, logger, mode="save", show_ticks=args.show_ticks)
            break

        if low in ("h", "help"):
            _print_cli_help()
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low in ("p", "plot"):
            _plot_from_log(cfg, logger, mode="save", show_ticks=args.show_ticks)
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low in ("d", "display"):
            _plot_from_log(cfg, logger, mode="display", show_ticks=args.show_ticks)
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low == "status":
            print(f" RD: {ctrl.rd_current:.4f} mm | x={ctrl.state.x:.3f} | c={ctrl.state.c:.4f} | "
                  f"Bowden/Buffer spring={printer.spring_mm():.3f} mm | printer_RD_true={printer.extruder_rd_true:.4f}")
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low == "clear":
            # Reset clock and controller with timestamp
            sim_time_s = 0.0
            ctrl.reset(rd_init=cfg.rd_start, sensor_input=printer.measure(), t_s=sim_time_s)
            logger.clear()
            printer = FilamentSystemPrinter(ctrl, extruder_rd_true=printer.extruder_rd_true, initial_spring_mm=0.0, chaos=max(0.0, min(2.0, args.chaos)))

            print("Controller reset, printer spring rebased, and log cleared. Tick counter reset to 0.")
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low.startswith("rd "):
            parts = line.split()
            if len(parts) != 2:
                print("Usage: rd <new_true_extruder_rotation_distance>")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            try:
                new_rd = float(parts[1])
            except ValueError:
                print("Bad numeric value.")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            printer.set_extruder_rd_true(new_rd)
            print(f"Printer true extruder RD set to {printer.extruder_rd_true:.4f} mm.")
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        if low == "clog" or low == "tangle":
            kind = "clog" if low == "clog" else "tangle"
            print(f"Realistic {kind.upper()} test (autotune state, stride={args.stride_mm} mm, dt={default_dt_s}s, stop on FlowGuard)...")
            recs, sim_time_s = _forced_extreme_test(
                ctrl, logger, printer,
                kind=kind,
                stride_mm=args.stride_mm,
                dt_s_step=default_dt_s,
                sim_time_s=sim_time_s,
            )
            if recs:
                last_sensor = recs[-1]["stimulus"]["sensor_reading"]
                last_sensor_ui = recs[-1]["output"]["sensor_ui"]
                last_flowguard = recs[-1]["output"]["flowguard"]
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # Distance-based simulation: "sim <v_mm_s> <time_s> [rd]"
        if line.startswith("sim "):
            parts = line.split()
            if len(parts) not in (3, 4):
                print("Usage: sim <avg_extruder_speed_mm_s> <time_s> [<extruder_rotation_distance>]")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            try:
                v = float(parts[1]); T = float(parts[2])
                rd_true = float(parts[3]) if len(parts) == 4 else None
            except ValueError:
                print("Bad numeric values.")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue

            if rd_true is not None:
                printer.set_extruder_rd_true(rd_true)

            total_mm = v * T
            stride = max(1e-6, float(args.stride_mm))
            n_steps = max(1, int(round(abs(total_mm) / stride)))
            per_step = math.copysign(abs(total_mm) / n_steps, total_mm)
            dt_s = (abs(per_step) / abs(v)) if abs(v) > 1e-12 else default_dt_s

            print(f"Running sim: total={total_mm:.3f} mm at {v} mm/s, steps={n_steps}, stride≈{per_step:.3f} mm"
                  f", printer_RD_true={printer.extruder_rd_true:.4f} mm, spring0={printer.spring_mm():.3f} mm ...")

            # ----- CHANGED: split steps at switch-sensor state boundaries -----
            def _state_from_x(x: float) -> int:
                thr = getattr(cfg, "flowguard_extreme_threshold", 0.9)
                st = cfg.sensor_type
                if st == "P":
                    # not used for P, but keep consistent
                    if x >= thr: return 1
                    if x <= -thr: return -1
                    return 0
                if st == "D":
                    if x >= thr: return 1
                    if x <= -thr: return -1
                    return 0
                if st == "CO":
                    return 1 if x >= thr else 0
                if st == "TO":
                    return -1 if x <= -thr else 0
                return 0

            for _ in range(n_steps):
                remaining = per_step
                while abs(remaining) > 1e-12:
                    # PRE-MOVE sensor (may be stick-slip affected)
                    z = printer.measure()

                    rd_prev = ctrl.rd_current
                    ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
                    K = 2.0 / cfg.buffer_range_mm
                    g = K * (ratio - 1.0)  # x change per +1mm extruder

                    d_chunk = remaining  # default: take all

                    if cfg.sensor_type != "P":
                        # Predict when the *measured* switch state will flip.
                        # Use the current measured state (z) and add a chaos-dependent lag
                        # so update() is not perfectly aligned to the ideal boundary.
                        thr = getattr(cfg, "flowguard_extreme_threshold", 0.9)
                        s = 1.0 if remaining >= 0 else -1.0
                        state0 = int(z)  # starting measured state for switch sensors
                        x = printer.x_true
                        dir_pos = (g * s) > 0.0  # True if x increases along this move

                        anchor = None
                        st = cfg.sensor_type
                        if st == "D":
                            if state0 == 0:
                                anchor = (thr if dir_pos else -thr)
                            elif state0 == 1 and not dir_pos:
                                anchor = thr
                            elif state0 == -1 and dir_pos:
                                anchor = -thr
                        elif st == "CO":
                            # 0<->1 boundary is at +thr
                            if (state0 == 0 and dir_pos) or (state0 == 1 and not dir_pos):
                                anchor = thr
                        elif st == "TO":
                            # 0<->-1 boundary is at -thr
                            if (state0 == 0 and not dir_pos) or (state0 == -1 and dir_pos):
                                anchor = -thr

                        if anchor is not None and abs(g) > 1e-16:
                            # chaos-driven lag (normalized units): extra distance before measured state flips
                            lag_norm = 0.0
                            if getattr(printer, "chaos", 0.0) > 1e-12:
                                lag_mm_max = min(cfg.buffer_max_range_mm, float(printer.chaos) * cfg.buffer_max_range_mm)
                                lag_norm_max = (2.0 / cfg.buffer_range_mm) * lag_mm_max
                                lag_norm = random.random() * lag_norm_max

                            # Move the target further in the direction of motion to model stick–slip
                            x_target = anchor + (lag_norm if dir_pos else -lag_norm)

                            # Keep within physical limits
                            norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
                            x_target = max(-norm_clip + 1e-12, min(norm_clip - 1e-12, x_target))

                            d_needed = (x_target - x) / g  # extruder mm to reach the (lagged) boundary
                            # Use it only if it's ahead along this move
                            if d_needed * s > 1e-12:
                                d_chunk = math.copysign(min(abs(d_needed), abs(remaining)), s)

# PAUL - orig no chaos path..
#                    if cfg.sensor_type != "P":
#                        thr = getattr(cfg, "flowguard_extreme_threshold", 0.9)
#                        s = 1.0 if remaining >= 0 else -1.0
#                        state0 = _state_from_x(printer.x_true)  # use modeled x_true for prediction
#
#                        # Determine boundary along direction s
#                        x = printer.x_true
#                        dir_pos = (g * s) > 0.0
#
#                        x_target = None
#                        st = cfg.sensor_type
#                        if st == "D":
#                            if state0 == 0:
#                                x_target = (thr if dir_pos else -thr)
#                            elif state0 == 1 and not dir_pos:
#                                x_target = thr
#                            elif state0 == -1 and dir_pos:
#                                x_target = -thr
#                        elif st == "CO":
#                            if (state0 == 0 and dir_pos) or (state0 == 1 and not dir_pos):
#                                x_target = thr
#                        elif st == "TO":
#                            if (state0 == 0 and not dir_pos) or (state0 == -1 and dir_pos):
#                                x_target = -thr
#
#                        if x_target is not None and abs(g) > 1e-16:
#                            d_needed = (x_target - x) / g  # extruder mm to hit boundary
#                            # Only if the boundary lies ahead in this movement direction
#                            if d_needed * s > 1e-12:
#                                d_chunk = math.copysign(min(abs(d_needed), abs(remaining)), s)

                    # Time for this chunk
                    dt_chunk = (abs(d_chunk) / abs(v)) if abs(v) > 1e-12 else default_dt_s
                    sim_time_s += dt_chunk

                    # Controller update and plant evolution for the chunk
                    out = ctrl.update(d_chunk, z, t_s=sim_time_s)
                    delta_rel = d_chunk * (ratio - 1.0)
                    printer.x_true += (2.0 / cfg.buffer_range_mm) * delta_rel
                    # Physical clamp
                    norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
                    printer.x_true = min(max(printer.x_true, -norm_clip), norm_clip)

                    rec = {
                        "stimulus": {"extruder_delta_mm": d_chunk, "sensor_reading": z},
                        "output": {
                            "rd_applied": out["rd_applied"],
                            "sensor_ui": out["sensor_expected"],
                            "flowguard": out["flowguard"],
                            "autotune_rd": out.get("autotune_rd"),
                            "autotune_note": out.get("autotune_note"),
                        },
                        "truth": {
                            "rd_true": printer.extruder_rd_true,
                            "spring_mm": printer.spring_mm(),
                        },
                        "meta": {"dt_s": dt_chunk, "t_s": sim_time_s},
                    }
                    logger.append(rec)

                    last_sensor = z
                    last_sensor_ui = out["sensor_expected"]
                    last_flowguard = out["flowguard"]

                    remaining -= d_chunk

                    if out["autotune_rd"]:
                        print(f"Autotune: rd: {out['autotune_rd']:.3f}, reason: {out['autotune_note']}")

                    if out["flowguard"]["clog"] or out["flowguard"]["tangle"]:
                        print("FlowGuard trip; stopping simulation.")
                        remaining = 0.0
                        break
            # -----------------------------------------------------------------

            print(f"Simulation complete. Current RD={ctrl.rd_current:.4f}.")
            print("Type 'plot' to save a plot, or 'display' to open a window.")
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # Manual update: "t|tick <d_ext_mm> [<sensor|'auto'>]"
        if line.startswith("t ") or line.startswith("tick "):
            parts = line.split()
            if parts[0] in ("t", "tick"):
                parts = parts[1:]

            if len(parts) == 0:
                print("Usage: t <d_ext_mm> [<sensor|auto>]")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue
            if len(parts) > 2:
                print("Too many parameters. Usage: t <d_ext_mm> [<sensor|auto>]")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue

            try:
                d_ext = float(parts[0])
            except ValueError:
                print("Bad numeric value for d_ext_mm.")
                _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                continue

            # Sensor handling (pre-move)
            if len(parts) == 1 or parts[1].strip().lower() == "auto":
                z = printer.measure()
            else:
                if cfg.sensor_type == "P":
                    try:
                        z = float(parts[1])
                    except ValueError:
                        print("Bad sensor value. Expect float in [-1,1].")
                        _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue
                else:
                    try:
                        z = int(parts[1])
                    except ValueError:
                        print("Bad sensor value. Expect integer.")
                        _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue
                    allowed = {-1, 0, 1} if cfg.sensor_type == "D" else ({0, 1} if cfg.sensor_type == "CO" else {-1, 0})
                    if z not in allowed:
                        if cfg.sensor_type == "D":
                            print("Discrete sensor (D) must be -1, 0, or 1.")
                        elif cfg.sensor_type == "CO":
                            print("Compression-only sensor (CO) must be 0 or 1.")
                        else:
                            print("Tension-only sensor (TO) must be -1 or 0.")
                        _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
                        continue

            # Controller update (uses rd_prev for plant; new RD applies next step)
            rd_prev = ctrl.rd_current
            sim_time_s += default_dt_s
            out = ctrl.update(d_ext, z, t_s=sim_time_s)

            # Reference-free plant evolution
            ratio = printer.extruder_rd_true / max(1e-9, rd_prev)
            delta_rel = d_ext * (ratio - 1.0)
            printer.x_true += (2.0 / cfg.buffer_range_mm) * delta_rel
            norm_clip = max(1e-9, cfg.buffer_max_range_mm / cfg.buffer_range_mm)
            printer.x_true = min(max(printer.x_true, -norm_clip), norm_clip)

            rec = {
                "stimulus": {"extruder_delta_mm": d_ext, "sensor_reading": z},
                "output": {
                    "rd_applied": out["rd_applied"],
                    "sensor_ui": out["sensor_expected"],
                    "flowguard": out["flowguard"],
                    "autotune_rd": out.get("autotune_rd"),
                    "autotune_note": out.get("autotune_note"),
                },
                "truth": {"rd_true": printer.extruder_rd_true, "spring_mm": printer.spring_mm()},
                "meta": {"dt_s": default_dt_s, "t_s": sim_time_s},
            }
            logger.append(rec)

            last_sensor = z
            last_sensor_ui = out["sensor_expected"]
            last_flowguard = out["flowguard"]

            print(f"RD={out['rd_applied']:.4f} | x={out['x_est']:.3f} | c={out['c_est']:.4f} | "
                  f"sensor_ui={out['sensor_expected']:.3f} | Bowden/Buffer spring={printer.spring_mm():.3f} mm | FlowGuard: {out['flowguard']} | "
                  f"autotune={out['autotune_rd']:.3f} {out['autotune_note']}")
            _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())
            continue

        # Fallback
        print("Unknown command. Type 'help' for usage.")
        _summary_print(ctrl, last_sensor, last_sensor_ui, last_flowguard, printer.spring_mm())

# ------------------------------- Main ----------------------------------

if __name__ == "__main__":
    _run_cli()

